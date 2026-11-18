"""
test_v348_zone_edit_recovery_and_auto_market.py
Unit tests for v3.4.8:
1. Gate 5 strips invalid directional TP even when SL is None (e.g. SELL 3728-3732 TP 4320).
2. MT5 order placement drops chemically invalid TP to 0.0 rather than failing with 10016.
3. Zone edit recovery: when previous orders failed/were cancelled (0 active tickets),
   subsequent zone edit deploys the zone orders.
4. Auto-market routing: when signal specifies "SELL <price>" or "BUY <price>" without
   explicit LIMIT keyword, enters at market if live price is within 15 pips.
"""

import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import main
import parser
from mt5_executor import MT5Executor


class FakeMsg:
    def __init__(self, msg_id: int, text: str, reply_to_msg_id=None, is_forwarded: bool = False):
        self.id = msg_id
        self.message = text
        self.raw_text = text
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to = None
        self.forward = MagicMock() if is_forwarded else None
        self.fwd_from = MagicMock() if is_forwarded else None
        self.out = False
        self.date = datetime.now(timezone.utc)


class FakeEvent:
    def __init__(self, msg_id: int, text: str, reply_to=None, is_forwarded: bool = False, chat_id: int = -1004369388188):
        self.message = FakeMsg(msg_id, text, reply_to, is_forwarded)
        self.id = msg_id
        self.text = text
        self.chat_id = chat_id
        self.is_private = False
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, msg: str):
        self.replies.append(msg)

    async def get_reply_message(self):
        return None


def _reset_runtime(tmp_path, sub: str):
    from state_manager import StateManager
    from trade_ledger import TradeLedger
    base = tmp_path / sub
    base.mkdir(parents=True, exist_ok=True)
    state_file = base / "signals_state.json"
    ledger_file = base / "trades_ledger.jsonl"
    main.state_mgr = StateManager(str(state_file))
    main.trade_ledger = TradeLedger(str(ledger_file))
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    if hasattr(main, "_recent_management_commands"):
        main._recent_management_commands.clear()
    main._deleted_messages.clear()
    main._signal_locks.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False


def test_gate5_strips_invalid_tp_without_sl():
    """Gate 5 must reject TP 4320 on a SELL zone 3728-3732 even if SL is absent."""
    p = parser.parse_message("SELL XAUUSD 3728-3732\n\nTP 4320\nTP 4300\nTP 4250")
    assert p is not None
    assert p.direction == "SELL"
    # TP 4320 >= 3728 is invalid for SELL -> final_tp must be stripped to None
    assert p.final_tp is None

    # Conversely, valid SELL TPs below entry are preserved
    p_ok = parser.parse_message("SELL XAUUSD 3728-3732\n\nTP 3720\nTP 3700\nTP 3650")
    assert p_ok is not None
    assert p_ok.direction == "SELL"
    assert p_ok.final_tp == 3720.5  # 3720.0 + 0.50 offset


def test_broker_guard_drops_invalid_tp_in_mt5_executor():
    """MT5Executor drops invalid TP to 0.0 rather than sending a bad request to MT5."""
    ex = MT5Executor(dry_run=True)
    ex.connect()
    # Mock live tick at bid=3727.68, ask=3728.20
    ex._dry_run_tick = SimpleNamespace(bid=3727.68, ask=3728.20, time=1700000000)

    with patch.object(ex, "send_order_with_filling_fallback") as mock_send:
        mock_send.return_value = (True, SimpleNamespace(order=99901, deal=99901))
        # Place market SELL with invalid TP 4320.5 (above entry ~3727.68)
        t = ex.execute_market_order("XAUUSD.s", "SELL", lot=0.01, sl=3743.0, tp=4320.5)
        assert t is not None
        req = mock_send.call_args[0][0]
        # Must have dropped TP to 0.0
        assert req["tp"] == 0.0

        # Place pending SELL LIMIT at 3730.0 with invalid TP 4320.5
        t_pend = ex.place_limit_order("XAUUSD.s", "SELL", price=3730.0, lot=0.01, sl=3745.0, tp=4320.5)
        assert t_pend is not None
        req_pend = mock_send.call_args[0][0]
        assert req_pend["tp"] == 0.0


@pytest.mark.asyncio
async def test_auto_market_routing_within_tolerance(tmp_path):
    """'SELL XAUUSD 3728' (no LIMIT) routes to MARKET when price is 3727.68 (within 15 pips)."""
    _reset_runtime(tmp_path, "auto_mkt")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    # Live price is at 3727.68 (0.32 points = 3.2 pips below target 3728.0)
    main.executor._dry_run_tick = SimpleNamespace(bid=3727.68, ask=3728.18, time=1700000000)

    # 1. 'SELL XAUUSD 3728' without LIMIT keyword -> routes to MARKET
    with patch.object(main.executor, "execute_market_order", wraps=main.executor.execute_market_order) as m_mkt, \
         patch.object(main.executor, "place_limit_order", wraps=main.executor.place_limit_order) as m_lim:
        t, o_type = main.executor._execute_layer_price(
            symbol="XAUUSD.s",
            direction="SELL",
            target_price=3728.0,
            lot=0.01,
            sl=3743.0,
            tp=3720.5,
            tick=main.executor._dry_run_tick,
            market_comment="TG L1 (Market)",
            limit_comment="TG L1 (Limit)",
            order_intent="AUTO",
        )
        assert o_type == "MARKET"
        assert t is not None
        assert m_mkt.call_count == 1
        assert m_lim.call_count == 0

    # 2. 'SELL LIMIT XAUUSD 3728' with explicit LIMIT keyword -> routes to LIMIT
    with patch.object(main.executor, "execute_market_order", wraps=main.executor.execute_market_order) as m_mkt, \
         patch.object(main.executor, "place_limit_order", wraps=main.executor.place_limit_order) as m_lim:
        t2, o_type2 = main.executor._execute_layer_price(
            symbol="XAUUSD.s",
            direction="SELL",
            target_price=3728.0,
            lot=0.01,
            sl=3743.0,
            tp=3720.5,
            tick=main.executor._dry_run_tick,
            market_comment="TG L1 (Market)",
            limit_comment="TG L1 (Limit)",
            order_intent="LIMIT",
        )
        assert o_type2 == "LIMIT"
        assert t2 is not None
        assert m_lim.call_count == 1
        assert m_mkt.call_count == 0


@pytest.mark.asyncio
async def test_zone_edit_recovery_when_zero_active_tickets(tmp_path):
    """
    Simulate Msg 9919 flow:
    1. Initial single price stub 'SELL XAUUSD 4328' -> 1 pending limit at 4328.
    2. Edit to 'SELL XAUUSD 3728-3732' with TP 3720 and SL 3735:
       Cancels 4328 pending order, deploys the 3728-3732 zone (L1 market + L2..L4 limits).
    """
    _reset_runtime(tmp_path, "zone_recovery")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    tick_live = SimpleNamespace(bid=3727.68, ask=3728.20, time=1700000000)
    main.executor._dry_run_tick = tick_live

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_tick", return_value=tick_live):

        # Step 1: Initial single price 'SELL XAUUSD 4328'
        await main.handle_new_message(FakeEvent(9919, "SELL XAUUSD 4328"))
        sig = main.state_mgr.get_signal(9919, chat_id=-1004369388188)
        assert sig is not None
        assert len(sig["pending_tickets"]) == 1
        old_ticket = sig["pending_tickets"][0]

        # Step 2: Edit to zone with corrected prices
        await main.handle_message_edited(
            FakeEvent(
                9919,
                "SELL XAUUSD 3728-3732\n\nTP 3720\nTP 3700\nTP 3650\n\nSL 3735"
            )
        )
        sig_edited = main.state_mgr.get_signal(9919, chat_id=-1004369388188)
        assert sig_edited is not None
        # Old 4328 ticket must be cancelled
        assert old_ticket not in sig_edited["pending_tickets"]
        # Must have deployed the 4 layers: L1 market + L2, L3, L4 limits
        total_orders = len(sig_edited["market_tickets"]) + len(sig_edited["pending_tickets"])
        assert total_orders == 4
        assert len(sig_edited["market_tickets"]) == 1  # L1 at market
        assert len(sig_edited["pending_tickets"]) == 3  # L2..L4 limits
        assert sig_edited["sl"] == 3735.0
        assert sig_edited["final_tp"] == 3720.5


@pytest.mark.asyncio
async def test_exact_msg_9919_typo_tp_then_corrected_edit_lifecycle(tmp_path):
    """
    Exact simulation of Msg 9919:
    1. 'SELL XAUUSD 4328'
    2. Edit to 'SELL XAUUSD 3728-3732' with TYPO TP 4320 (no SL):
       Gate 5 / MT5 guard protects: orders deploy without MT5 rejection!
    3. Edit to 'SELL XAUUSD 3728-3732\nTP 3720\nTP 3700\nTP 3650\nSL 3735':
       Corrected TP & SL are updated on all active layers!
    """
    _reset_runtime(tmp_path, "exact_9919")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    tick_live = SimpleNamespace(bid=3727.68, ask=3728.20, time=1700000000)
    main.executor._dry_run_tick = tick_live

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_tick", return_value=tick_live):

        # 1. Initial typo single-price
        await main.handle_new_message(FakeEvent(9919, "SELL XAUUSD 4328"))
        sig1 = main.state_mgr.get_signal(9919, chat_id=-1004369388188)
        assert len(sig1["pending_tickets"]) == 1

        # 2. Edit with corrected zone but still typo TPs (4320) and no SL
        await main.handle_message_edited(
            FakeEvent(9919, "SELL XAUUSD 3728-3732\n\nTP 4320\nTP 4300\nTP 4250")
        )
        sig2 = main.state_mgr.get_signal(9919, chat_id=-1004369388188)
        assert sig2 is not None
        # Zone must be deployed (4 orders) and NOT rejected!
        total2 = len(sig2["market_tickets"]) + len(sig2["pending_tickets"])
        assert total2 == 4
        # Bogus TP was stripped, fallback or 0.0 applied, not 4320
        assert sig2.get("final_tp") != 4320.5

        # 3. Edit with corrected TPs (3720) and SL (3735)
        await main.handle_message_edited(
            FakeEvent(9919, "SELL XAUUSD 3728-3732\n\nTP 3720\nTP 3700\nTP 3650\n\nSL 3735")
        )
        sig3 = main.state_mgr.get_signal(9919, chat_id=-1004369388188)
        assert sig3["sl"] == 3735.0
        assert sig3["final_tp"] == 3720.5
        total3 = len(sig3["market_tickets"]) + len(sig3["pending_tickets"])
        assert total3 == 4
