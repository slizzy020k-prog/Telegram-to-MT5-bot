"""
Comprehensive QA & Regression Suite for XAUUSD Copier v3.2:
Deleted-Signal Trade Protection (`events.MessageDeleted`, Periodic/Reconnect Sweep,
Surgical Exposure Close, Fill-Race Guard, Out-of-Order Tombstone Guard, and Config Validation).
"""

import asyncio
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import main
import mt5_executor as ex_mod
import startup_checks
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger


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
    def __init__(self, msg_id: int, text: str, reply_to=None, is_forwarded: bool = False, chat_id: int = -1001234567890):
        self.message = FakeMsg(msg_id, text, reply_to, is_forwarded)
        self.chat_id = chat_id
        self.is_private = False
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, msg_txt: str):
        self.replies.append(msg_txt)

    async def get_reply_message(self):
        return None


class FakeDeleteEvent:
    def __init__(self, deleted_ids=None, chat_id=-1001234567890, deleted_id=None, original_update=None):
        self.deleted_ids = deleted_ids
        self.deleted_id = deleted_id
        self.chat_id = chat_id
        self.original_update = original_update


def _reset_runtime(tmp_path, name: str = "del_prot"):
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    main._deleted_messages.clear()
    main._signal_locks.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main._daily_loss_lock_notified_date = None
    main._awaiting_dailywin_input = False
    main._awaiting_dailyloss_input = False
    main._daily_50pct_alert_date = None
    main._mt5_connected_state = None
    main._mt5_last_disconnect_at = None
    main._telegram_reconnect_count = 0
    main.CLOSE_ON_SIGNAL_DELETED = True
    main.CHANNEL_IDS = [-1001234567890]
    config.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    ex_mod.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    main.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    state_file = tmp_path / f"{name}_state.json"
    ledger_file = tmp_path / f"{name}_ledger.jsonl"
    main.state_mgr = StateManager(str(state_file))
    main.trade_ledger = TradeLedger(str(ledger_file))
    return state_file, ledger_file


# ---------------------------------------------------------------------------
# 1. ACTIVE 4-LAYER SIGNAL DELETED + MULTI-SIGNAL ISOLATION + BARE CHANNEL_ID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1_active_4layer_deleted_and_multi_signal_isolation(tmp_path):
    _reset_runtime(tmp_path, "scen1")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    saved_alerts = []

    async def _cap_alert(msg: str):
        saved_alerts.append(msg)

    with patch.object(main, "notify_saved_messages", side_effect=_cap_alert):
        # Signal #501: 4-layer Market Now (4 open market positions: L1..L4)
        ev_501 = FakeEvent(501, "Buy XAUUSD 3895-3887 now\nTP 3905\nTP 3915\nSL 3875", chat_id=-1001234567890)
        await main.handle_new_message(ev_501)

        sig_501_before = main.state_mgr.get_signal(501, chat_id=-1001234567890)
        assert sig_501_before is not None
        assert len(sig_501_before["market_tickets"]) + len(sig_501_before["pending_tickets"]) == 4
        tickets_501 = set(sig_501_before["market_tickets"]) | set(sig_501_before["pending_tickets"])

        # Signal #502: 2 market layers + 2 pending limit layers seeded explicitly in same channel
        main.executor._dry_run_ticket_lots[95021] = 0.01
        main.executor._dry_run_ticket_lots[95022] = 0.01
        main.executor._dry_run_ticket_lots[95023] = 0.01
        main.executor._dry_run_ticket_lots[95024] = 0.02
        main.state_mgr.create_signal(
            message_id=502,
            symbol="XAUUSD.s",
            direction="BUY",
            zone={"top": 3895.0, "mid": 3891.0, "bottom": 3887.0},
            sl=3875.0,
            final_tp=3904.5,
            raw_tp1=3905.0,
            raw_tp2=3915.0,
            raw_tp3=3925.0,
            raw_tp4=3935.0,
            raw_tp5=3945.0,
            tp_levels={"TP1": 3905.0, "TP2": 3915.0, "TP3": 3925.0, "TP4": 3935.0, "TP5": 3945.0},
            market_tickets=[95021, 95022],
            pending_tickets=[95023, 95024],
            layers={
                "L1": {"ticket": 95021, "lot": 0.01, "status": "OPEN", "entry": 3895.0, "sl": 3875.0, "tp": 3904.5, "tp_stage": "TP1"},
                "L2": {"ticket": 95022, "lot": 0.01, "status": "OPEN", "entry": 3892.33, "sl": 3875.0, "tp": 3904.5, "tp_stage": "TP1"},
                "L3": {"ticket": 95023, "lot": 0.01, "status": "PENDING", "entry": 3889.67, "sl": 3875.0, "tp": 3904.5, "tp_stage": "TP1"},
                "L4": {"ticket": 95024, "lot": 0.02, "status": "PENDING", "entry": 3887.0, "sl": 3875.0, "tp": 3904.5, "tp_stage": "TP1"},
            },
            status="PARTIAL_FILLED",
            chat_id=-1001234567890,
        )

        saved_alerts.clear()
        with patch.object(main.executor, "get_position_close_info", return_value={"profit": 2.40, "reason": "MANUAL"}):
            # Fire handle_message_deleted for #502 in channel -1001234567890
            await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[502], chat_id=-1001234567890))

        # Verify #502 closed + pending cancelled + state & ledger updated + Saved Messages alert sent
        sig_502_after = main.state_mgr.get_signal(502, chat_id=-1001234567890)
        assert sig_502_after["status"] == "CLOSED"
        assert sig_502_after["deleted_from_chat"] is True
        assert sig_502_after["market_tickets"] == []
        assert sig_502_after["pending_tickets"] == []
        assert sig_502_after["layers"]["L1"]["status"] == "CLOSED"
        assert sig_502_after["layers"]["L2"]["status"] == "CLOSED"
        assert sig_502_after["layers"]["L3"]["status"] == "CANCELLED"
        assert sig_502_after["layers"]["L4"]["status"] == "CANCELLED"
        assert sig_502_after["banked_profit"] == pytest.approx(4.80)
        assert sig_502_after["ledger_recorded"] is True

        for t in (95021, 95022, 95023, 95024):
            assert t not in main.executor._dry_run_ticket_lots

        trades = main.trade_ledger.read_trades()
        assert len(trades) == 1
        assert trades[0]["realized_pnl"] == pytest.approx(4.80)
        assert "Signal Deleted" in trades[0]["exit_reason"]
        assert any("🗑️ **Signal Deleted in Chat — Trade Taken Out! [ID: 502]**" in a for a in saved_alerts)

        # Verify #501 remains 100% OPEN and untouched!
        sig_501_after = main.state_mgr.get_signal(501, chat_id=-1001234567890)
        assert sig_501_after["status"] in ("ACTIVE", "OPEN", "PARTIAL_FILLED")
        assert set(sig_501_after["market_tickets"]) | set(sig_501_after["pending_tickets"]) == tickets_501
        for t in sig_501_after["market_tickets"]:
            assert t in main.executor._dry_run_ticket_lots
        for t in sig_501_after["pending_tickets"]:
            assert t in main.executor._dry_run_pending_ticket_lots

        # Now test bare original_update.channel_id=1234567890 normalization deleting #501
        bare_upd = SimpleNamespace(channel_id=1234567890, messages=[501])
        ev_bare = FakeDeleteEvent(deleted_ids=None, chat_id=None, original_update=bare_upd)
        with patch.object(main.executor, "get_position_close_info", return_value={"profit": 1.10, "reason": "MANUAL"}):
            await main.handle_message_deleted(ev_bare)

        sig_501_deleted = main.state_mgr.get_signal(501, chat_id=-1001234567890)
        assert sig_501_deleted["status"] == "CLOSED"
        assert sig_501_deleted["deleted_from_chat"] is True
        assert sig_501_deleted["market_tickets"] == []
        assert sig_501_deleted["pending_tickets"] == []


# ---------------------------------------------------------------------------
# 2. RUNNER_ACTIVE TRADE DELETED MID-RUN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2_runner_active_trade_deleted_mid_run(tmp_path):
    _reset_runtime(tmp_path, "scen2")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    main.executor._dry_run_ticket_lots[96004] = 0.02
    main.state_mgr.create_signal(
        message_id=510,
        symbol="XAUUSD.s",
        direction="BUY",
        zone={"top": 3895.0, "mid": 3891.0, "bottom": 3887.0},
        sl=3875.0,
        final_tp=3899.5,
        raw_tp1=3900.0,
        raw_tp2=3910.0,
        raw_tp3=3920.0,
        raw_tp4=3930.0,
        raw_tp5=3940.0,
        tp_levels={"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
        market_tickets=[96004],
        pending_tickets=[],
        layers={
            "L1": {"ticket": 96001, "lot": 0.01, "status": "CLOSED", "entry": 3895.0, "sl": 3875.0, "tp": 3899.5, "tp_stage": "TP1"},
            "L2": {"ticket": 96002, "lot": 0.01, "status": "CLOSED", "entry": 3892.33, "sl": 3875.0, "tp": 3899.5, "tp_stage": "TP1"},
            "L3": {"ticket": 96003, "lot": 0.01, "status": "CLOSED", "entry": 3889.67, "sl": 3875.0, "tp": 3899.5, "tp_stage": "TP1"},
            "L4": {"ticket": 96004, "lot": 0.02, "status": "RUNNER", "entry": 3887.0, "sl": 3908.5, "tp": 3930.0, "tp_stage": "TP4"},
        },
        status="RUNNER_ACTIVE",
        chat_id=-1001234567890,
    )
    main.state_mgr.update_signal(
        510,
        {
            "tp1_reached": True,
            "runner_ticket": 96004,
            "runner_tickets": [96004],
            "runner_layer": "L4",
            "runner_layers": ["L4"],
            "runner_current_milestone": "TP3",
            "banked_profit": 3.75,
            "accounted_tickets": [96001, 96002, 96003],
        },
        chat_id=-1001234567890,
    )

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_position_close_info", return_value={"profit": 42.25, "reason": "MANUAL"}):
        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[510], chat_id=-1001234567890))

    sig = main.state_mgr.get_signal(510, chat_id=-1001234567890)
    assert sig["status"] == "CLOSED"
    assert sig["deleted_from_chat"] is True
    assert sig["runner_ticket"] is None
    assert sig["runner_tickets"] == []
    assert sig["runner_layer"] is None
    assert sig["runner_layers"] == []
    assert sig["market_tickets"] == []
    assert sig["banked_profit"] == pytest.approx(46.00)  # 3.75 banked + 42.25 runner
    assert sig["ledger_recorded"] is True
    assert 96004 not in main.executor._dry_run_ticket_lots

    trades = main.trade_ledger.read_trades()
    assert len(trades) == 1
    assert trades[0]["realized_pnl"] == pytest.approx(46.00)
    assert trades[0]["exit_reason"] == "Signal Deleted (Telegram Delete Event)"


# ---------------------------------------------------------------------------
# 3. PENDING-TO-MARKET FILL RACE GUARD (MT5Executor.close_signal_exposure)
# ---------------------------------------------------------------------------

def test_3_pending_to_market_fill_race_guard_in_close_signal_exposure():
    ex = MT5Executor(dry_run=False)
    ex._connected = True

    # Case A: #8001 was recorded in pending_tickets, but JUST filled into a live MT5 position
    # right before MessageDeleted arrives; #8002 is still a live pending order.
    sig_a = {
        "market_tickets": [],
        "pending_tickets": [8001, 8002],
        "layers": {
            "L1": {"ticket": 8001, "status": "PENDING"},
            "L2": {"ticket": 8002, "status": "PENDING"},
        },
    }
    live_positions_a = [SimpleNamespace(ticket=8001), SimpleNamespace(ticket=99999)]  # 99999 is another signal!
    live_orders_a = [SimpleNamespace(ticket=8002), SimpleNamespace(ticket=88888)]

    with patch.object(ex, "_ensure_connected", return_value=True), \
         patch.object(ex_mod.mt5, "positions_get", return_value=live_positions_a), \
         patch.object(ex_mod.mt5, "orders_get", return_value=live_orders_a), \
         patch.object(ex, "close_market_position", return_value=True) as mock_close, \
         patch.object(ex, "cancel_pending_order", return_value=True) as mock_cancel, \
         patch.object(ex, "invalidate_realized_pnl_cache") as mock_inv:
        res_a = ex.close_signal_exposure(sig_a)

    assert res_a == {"closed_tickets": [8001], "cancelled_orders": [8002]}
    mock_close.assert_called_once_with(8001)
    mock_cancel.assert_called_once_with(8002)
    mock_inv.assert_called_once()

    # Case B: Sub-race where orders_get initially listed #8002, cancel_pending_order(8002) returns False
    # because #8002 filled microseconds later, and positions_get(ticket=8002) finds the new position!
    sig_b = {
        "market_tickets": [],
        "pending_tickets": [8002],
        "layers": {"L2": {"ticket": 8002, "status": "PENDING"}},
    }

    def _pos_get_side_effect(*args, **kwargs):
        if kwargs.get("ticket") == 8002:
            return [SimpleNamespace(ticket=8002)]
        return []

    with patch.object(ex, "_ensure_connected", return_value=True), \
         patch.object(ex_mod.mt5, "positions_get", side_effect=_pos_get_side_effect), \
         patch.object(ex_mod.mt5, "orders_get", return_value=[SimpleNamespace(ticket=8002)]), \
         patch.object(ex, "cancel_pending_order", return_value=False) as mock_cancel_b, \
         patch.object(ex, "close_market_position", return_value=True) as mock_close_b:
        res_b = ex.close_signal_exposure(sig_b)

    assert res_b == {"closed_tickets": [8002], "cancelled_orders": []}
    mock_cancel_b.assert_called_once_with(8002)
    mock_close_b.assert_called_once_with(8002)


# ---------------------------------------------------------------------------
# 4. PURELY UNFILLED PENDING SETUP DELETED -> 0 LEDGER ENTRIES
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_4_purely_unfilled_pending_setup_deleted_skips_ledger(tmp_path):
    _reset_runtime(tmp_path, "scen4")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Place a 4-layer BUY STOP signal above current dry-run ask (3894.50) -> 4 pending orders, 0 market
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        ev_stop = FakeEvent(604, "Buy stop XAUUSD 3905\nTP 3915\nTP 3925\nSL 3890", chat_id=-1001234567890)
        await main.handle_new_message(ev_stop)

        sig_before = main.state_mgr.get_signal(604, chat_id=-1001234567890)
        assert sig_before is not None
        assert sig_before["market_tickets"] == []
        assert len(sig_before["pending_tickets"]) == 4

        # Delete before any pending stop order triggers
        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[604], chat_id=-1001234567890))

    sig_after = main.state_mgr.get_signal(604, chat_id=-1001234567890)
    assert sig_after["status"] == "CLOSED"
    assert sig_after["deleted_from_chat"] is True
    assert sig_after["pending_tickets"] == []
    assert all(l["status"] == "CANCELLED" for l in sig_after["layers"].values())
    assert main.trade_ledger.read_trades() == []  # 0 bogus entries written!


# ---------------------------------------------------------------------------
# 5. DELAYED "SELL NOW" / "BUY NOW" DELETED DURING HOLD TIMER
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_5_delayed_sell_now_deleted_during_hold_timer(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "scen5")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 10.0)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        ev_delay = FakeEvent(605, "Sell XAUUSD now\nTP 3880\nSL 3910", chat_id=-1001234567890)
        await main.handle_new_message(ev_delay)

        sig_pending = main.state_mgr.get_signal(605, chat_id=-1001234567890)
        assert sig_pending is not None
        assert sig_pending["status"] == "PENDING"
        assert len(main._pending_sell_now_tasks) == 1

        # Delete message during hold timer
        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[605], chat_id=-1001234567890))
        await asyncio.sleep(0)

    assert len(main._pending_sell_now_tasks) == 0
    sig_closed = main.state_mgr.get_signal(605, chat_id=-1001234567890)
    assert sig_closed["status"] == "CLOSED"
    assert sig_closed["deleted_from_chat"] is True
    assert sig_closed["market_tickets"] == []
    assert sig_closed["pending_tickets"] == []
    assert main.executor._dry_run_ticket_lots == {}
    assert main.trade_ledger.read_trades() == []


# ---------------------------------------------------------------------------
# 6. OUT-OF-ORDER TOMBSTONE GUARD & SUPERGROUP VS DM ISOLATION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_6_out_of_order_tombstone_and_supergroup_dm_isolation(tmp_path):
    _reset_runtime(tmp_path, "scen6")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Part A: MessageDeleted for (-1001234567890, 777) arrives BEFORE handle_new_message / handle_message_edited
        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[777], chat_id=-1001234567890))
        assert main._is_message_tombstoned(-1001234567890, 777) is True

        await main.handle_new_message(FakeEvent(777, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))
        await main.handle_message_edited(FakeEvent(777, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))
        assert main.state_mgr.get_signal(777, chat_id=-1001234567890) is None
        assert main.executor._dry_run_ticket_lots == {}

        # Part B: Private DM delete with chat_id=None, deleted_ids=[778] does NOT tombstone supergroup -1001234567890 msg 778
        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[778], chat_id=None))
        assert main._is_message_tombstoned(-1001234567890, 778) is False

        await main.handle_new_message(FakeEvent(778, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))
        sig_778 = main.state_mgr.get_signal(778, chat_id=-1001234567890)
        assert sig_778 is not None
        assert sig_778["status"] in ("ACTIVE", "OPEN", "PARTIAL_FILLED")

        # Part C: Active supergroup signal #778 exists when chat_id=None, deleted_ids=[778] fires:
        # 1) When telethon_client.get_messages(-1001234567890, ids=[778]) returns [SimpleNamespace(id=778)], NOT closed!
        fake_tg = MagicMock()
        fake_tg.get_messages = AsyncMock(return_value=[SimpleNamespace(id=778)])
        orig_tg = main.telethon_client
        main.telethon_client = fake_tg
        try:
            await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[778], chat_id=None))
            sig_still_open = main.state_mgr.get_signal(778, chat_id=-1001234567890)
            assert sig_still_open["status"] != "CLOSED"

            # 2) When get_messages returns [None] (actually deleted in channel), #778 IS closed!
            fake_tg.get_messages = AsyncMock(return_value=[None])
            await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[778], chat_id=None))
            sig_now_closed = main.state_mgr.get_signal(778, chat_id=-1001234567890)
            assert sig_now_closed["status"] == "CLOSED"
            assert sig_now_closed["deleted_from_chat"] is True
        finally:
            main.telethon_client = orig_tg


# ---------------------------------------------------------------------------
# 7. OFFLINE / RECONNECT CATCH-UP & PERIODIC SWEEP (poll_deleted_active_signals)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_7_offline_reconnect_catchup_and_periodic_sweep(tmp_path):
    _reset_runtime(tmp_path, "scen7")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        await main.handle_new_message(FakeEvent(801, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))
        await main.handle_new_message(FakeEvent(802, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))

        fake_tg = MagicMock()
        orig_tg = main.telethon_client
        main.telethon_client = fake_tg
        try:
            # 1) Non-authoritative / mismatched get_messages response is ignored safely
            fake_tg.get_messages = AsyncMock(return_value=[SimpleNamespace(id=99999), SimpleNamespace(id=88888)])
            closed_non_auth = await main.poll_deleted_active_signals(source_label="Periodic Sweep")
            assert closed_non_auth == 0
            assert main.state_mgr.get_signal(801, chat_id=-1001234567890)["status"] != "CLOSED"
            assert main.state_mgr.get_signal(802, chat_id=-1001234567890)["status"] != "CLOSED"

            # 2) Exception in get_messages is handled gracefully
            fake_tg.get_messages = AsyncMock(side_effect=RuntimeError("network blip"))
            closed_err = await main.poll_deleted_active_signals(source_label="Periodic Sweep")
            assert closed_err == 0

            # 3) Authoritative response: #801 is None (deleted while offline), #802 still exists
            def _get_msgs_side_effect(chat_id, **kwargs):
                ids = kwargs.get("ids")
                if ids is not None:
                    out = []
                    for mid in ids:
                        out.append(None if mid == 801 else SimpleNamespace(id=mid))
                    return out
                return []

            fake_tg.get_messages = AsyncMock(side_effect=_get_msgs_side_effect)
            summary = await main.run_reconnect_catchup(reason="reconnect")
            assert summary["deleted"] == 1

            sig_801 = main.state_mgr.get_signal(801, chat_id=-1001234567890)
            sig_802 = main.state_mgr.get_signal(802, chat_id=-1001234567890)
            assert sig_801["status"] == "CLOSED"
            assert sig_801["deleted_from_chat"] is True
            assert sig_802["status"] != "CLOSED"

            # 4) MessageEmpty class name at index 0 closes #802 on next periodic sweep
            MessageEmpty = type("MessageEmpty", (), {"id": 802})
            fake_tg.get_messages = AsyncMock(return_value=[MessageEmpty()])
            closed_empty = await main.poll_deleted_active_signals(source_label="Periodic Sweep")
            assert closed_empty == 1
            assert main.state_mgr.get_signal(802, chat_id=-1001234567890)["status"] == "CLOSED"
        finally:
            main.telethon_client = orig_tg


# ---------------------------------------------------------------------------
# 8. PROTECTIVE EXIT DURING session_trading_blocked() + NOISE/TERMINAL IGNORED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_8_protective_exit_when_trading_halted_and_noise_ignored(tmp_path):
    _reset_runtime(tmp_path, "scen8")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    saved_alerts = []

    async def _cap_alert(msg: str):
        saved_alerts.append(msg)

    with patch.object(main, "notify_saved_messages", side_effect=_cap_alert):
        await main.handle_new_message(FakeEvent(850, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))
        saved_alerts.clear()

        # Halt trading & lock daily loss -> handle_message_deleted MUST STILL close open deleted signal!
        main.TRADING_HALTED = True
        main._daily_loss_locked = True
        assert main.session_trading_blocked() is True

        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[850], chat_id=-1001234567890))
        sig_850 = main.state_mgr.get_signal(850, chat_id=-1001234567890)
        assert sig_850["status"] == "CLOSED"
        assert sig_850["deleted_from_chat"] is True
        assert len(saved_alerts) == 1

        # Deleting an untracked noise message ID or an already-CLOSED signal (#850) -> 0 MT5 calls, 0 spam
        saved_alerts.clear()
        with patch.object(main.executor, "close_signal_exposure") as spy_close:
            await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[999999, 850], chat_id=-1001234567890))
            assert spy_close.call_count == 0
            assert saved_alerts == []


# ---------------------------------------------------------------------------
# 9. CLOSE_ON_SIGNAL_DELETED = False TOGGLE + STARTUP PREFLIGHT VALIDATION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_9_close_on_signal_deleted_config_toggle_and_startup_validation(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "scen9")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        await main.handle_new_message(FakeEvent(901, "Buy XAUUSD 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890))

        # Disable CLOSE_ON_SIGNAL_DELETED -> both real-time delete & periodic sweep leave trade open
        monkeypatch.setattr(main, "CLOSE_ON_SIGNAL_DELETED", False)
        await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[901], chat_id=-1001234567890))
        swept = await main.poll_deleted_active_signals(source_label="Periodic Sweep")
        assert swept == 0

        sig_901 = main.state_mgr.get_signal(901, chat_id=-1001234567890)
        assert sig_901["status"] != "CLOSED"
        assert not sig_901.get("deleted_from_chat")

    # Startup preflight validation for CLOSE_ON_SIGNAL_DELETED & DELETED_SIGNAL_POLL_INTERVAL_SECONDS
    fake_cfg_ok = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    fake_cfg_ok.DRY_RUN = True
    fake_cfg_ok.CLOSE_ON_SIGNAL_DELETED = True
    fake_cfg_ok.DELETED_SIGNAL_POLL_INTERVAL_SECONDS = 0.0  # 0.0 is valid (disables periodic sweep)
    res_ok = startup_checks.validate_environment(check_packages=False, cfg=fake_cfg_ok)
    assert res_ok["ok"] is True

    fake_cfg_bad = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    fake_cfg_bad.DRY_RUN = True
    fake_cfg_bad.CLOSE_ON_SIGNAL_DELETED = "yes"  # Invalid non-bool
    fake_cfg_bad.DELETED_SIGNAL_POLL_INTERVAL_SECONDS = -5.0  # Invalid negative
    res_bad = startup_checks.validate_environment(check_packages=False, cfg=fake_cfg_bad)
    assert res_bad["ok"] is False
    assert any("CLOSE_ON_SIGNAL_DELETED" in e for e in res_bad["errors"])
    assert any("DELETED_SIGNAL_POLL_INTERVAL_SECONDS" in e for e in res_bad["errors"])
    assert config.DELETED_SIGNAL_POLL_INTERVAL_SECONDS == pytest.approx(1.5)


def test_10_real_channel_chatter_false_positives_blocked():
    """Verify real channel educational/testimonial chatter ('How to place a buy limit', '£150 to £600...') never opens trades."""
    from parser import parse_message

    assert parse_message("How to place a buy limit .") is None
    assert parse_message("How to place a buy limit") is None
    assert parse_message("Sell stop") is None
    assert parse_message("£150 to £600 , bro x4 his money with 1 sell from last night lol 😆😮‍💨") is None
