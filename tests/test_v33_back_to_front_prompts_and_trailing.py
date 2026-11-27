"""
Comprehensive Back-to-Front Audit & QA Suite for XAUUSD Copier v3.3:
1. End-to-End Trade Reading, Execution & Exact Broker SL/TP Placement (BUY & SELL across Zone, Limit, Stop, Market Now, Unpriced Delayed Now).
2. Channel Prompts & Replies While Active — Adjust SL, TP1, TP2, TP3 (`ACTION_ADJUST_TARGETS` vs `ACTION_BREAKEVEN`).
3. Channel Prompts, Replies & Edits — Trail SL & Step `TP1 -> TP2 -> TP3 -> TP4` (`ACTION_TRAIL_SL`, `ACTION_TP2_HIT`, `ACTION_TP3_HIT`, `ratchet_sl_only=True`).
4. User Prompts in Saved Messages (`"me"`) End-to-End Lifecycle (`/trade`, `/sl`, `move sl`, `/tp2`, `/tp`, `/be`, `close 50%`, `tp1 hit`, `trail tp2`, `/trail`, `#<msg_id>`, `dailywin`/`dailyloss`).
5. Automatic 1-Second Runner Trailing + Anti-Choke + Deleted-Signal Regression.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import main
import mt5_executor as ex_mod
import parser
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
    def __init__(self, msg_id: int, text: str, reply_to=None, is_forwarded: bool = False, chat_id=-1001234567890):
        self.message = FakeMsg(msg_id, text, reply_to, is_forwarded)
        self.chat_id = chat_id
        self.is_private = chat_id == "me"
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, msg_txt: str):
        self.replies.append(msg_txt)

    async def get_reply_message(self):
        return None


class FakeDeleteEvent:
    def __init__(self, deleted_ids=None, chat_id=-1001234567890):
        self.deleted_ids = deleted_ids
        self.deleted_id = None
        self.chat_id = chat_id
        self.original_update = None


def _reset_runtime(tmp_path, name: str = "v33"):
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
# 1. END-TO-END TRADE READING, EXECUTION & EXACT BROKER SL/TP PLACEMENT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1_end_to_end_trade_reading_execution_and_exact_broker_sl_tp_placement(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "e2e_exec")
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(main, "STAGED_MARKET_ENTRY", False)  # test validates 4-layer-at-once behaviour

    ex = MT5Executor(dry_run=False)
    ex._connected = True
    main.executor = ex

    sent_requests = []
    ticket_seq = [10000]

    def _fake_order_send(req):
        ticket_seq[0] += 1
        sent_requests.append(dict(req))
        return SimpleNamespace(retcode=ex_mod.mt5.TRADE_RETCODE_DONE, order=ticket_seq[0], deal=ticket_seq[0])

    sym_info = SimpleNamespace(
        visible=True,
        digits=2,
        point=0.01,
        trade_stops_level=20,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        filling_mode=2,
    )
    term_info = SimpleNamespace(connected=True, trade_allowed=True)
    acc_info = SimpleNamespace(balance=1000.0, credit=0.0, equity=1000.0, currency="GBP", trade_allowed=True)
    live_tick = SimpleNamespace(bid=3895.00, ask=3895.20, time=1700000000)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex, "_ensure_connected", return_value=True), \
         patch.object(ex_mod.mt5, "terminal_info", return_value=term_info), \
         patch.object(ex_mod.mt5, "account_info", return_value=acc_info), \
         patch.object(ex_mod.mt5, "symbol_info", return_value=sym_info), \
         patch.object(ex_mod.mt5, "symbol_info_tick", return_value=live_tick), \
         patch.object(ex_mod.mt5, "positions_get", return_value=[]), \
         patch.object(ex_mod.mt5, "orders_get", return_value=[]), \
         patch.object(ex_mod.mt5, "order_send", side_effect=_fake_order_send):

        # 1A: BUY Zone (Buy 3895-3887 TP1 3900 TP2 3910 TP3 3920 SL 3875)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(101, "Buy 3895-3887\nTP1 3900\nTP2 3910\nTP3 3920\nSL 3875"))
        assert len(sent_requests) == 4
        assert [r["volume"] for r in sent_requests] == [0.02, 0.02, 0.02, 0.02]
        assert all(r["sl"] == pytest.approx(3875.0) for r in sent_requests)
        assert all(r["tp"] == pytest.approx(3899.5) for r in sent_requests)  # v3.1 All-at-TP1 (final_tp)
        sig_101 = main.state_mgr.get_signal(101, chat_id=-1001234567890)
        assert sig_101["tp_levels"] == {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}

        # 1B: SELL Zone (Sell 3910-3918 TP1 3900 TP2 3890 TP3 3880 SL 3928)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(102, "Sell 3910-3918\nTP1 3900\nTP2 3890\nTP3 3880\nSL 3928"))
        assert len(sent_requests) == 4
        assert [r["volume"] for r in sent_requests] == [0.02, 0.02, 0.02, 0.02]
        assert all(r["sl"] == pytest.approx(3928.0) for r in sent_requests)
        assert all(r["tp"] == pytest.approx(3900.5) for r in sent_requests)

        # 1C: Buy limit (single price below ask)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(103, "Buy limit XAUUSD 3885\nTP1 3900\nTP2 3910\nSL 3870"))
        assert len(sent_requests) == 4
        assert all(r["type"] == ex_mod.mt5.ORDER_TYPE_BUY_LIMIT for r in sent_requests)
        assert all(r["price"] == pytest.approx(3885.0) for r in sent_requests)
        assert all(r["sl"] == pytest.approx(3870.0) and r["tp"] == pytest.approx(3899.5) for r in sent_requests)

        # 1D: Sell limit (single price above bid)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(104, "Sell limit XAUUSD 3905\nTP1 3890\nTP2 3880\nSL 3920"))
        assert len(sent_requests) == 4
        assert all(r["type"] == ex_mod.mt5.ORDER_TYPE_SELL_LIMIT for r in sent_requests)
        assert all(r["price"] == pytest.approx(3905.0) for r in sent_requests)
        assert all(r["sl"] == pytest.approx(3920.0) and r["tp"] == pytest.approx(3890.5) for r in sent_requests)

        # 1E: Buy stop (single price above ask)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(105, "Buy stop XAUUSD 3902\nTP1 3912\nTP2 3922\nSL 3888"))
        assert len(sent_requests) == 4
        assert all(r["type"] == ex_mod.mt5.ORDER_TYPE_BUY_STOP for r in sent_requests)
        assert all(r["price"] == pytest.approx(3902.0) for r in sent_requests)
        assert all(r["sl"] == pytest.approx(3888.0) and r["tp"] == pytest.approx(3911.5) for r in sent_requests)

        # 1F: Sell stop (single price below bid)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(106, "Sell stop XAUUSD 3888\nTP1 3878\nTP2 3868\nSL 3902"))
        assert len(sent_requests) == 4
        assert all(r["type"] == ex_mod.mt5.ORDER_TYPE_SELL_STOP for r in sent_requests)
        assert all(r["price"] == pytest.approx(3888.0) for r in sent_requests)
        assert all(r["sl"] == pytest.approx(3902.0) and r["tp"] == pytest.approx(3878.5) for r in sent_requests)

        # 1G: Priced Buy now (market orders)
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(107, "Buy XAUUSD now\nTP1 3905\nTP2 3915\nSL 3880"))
        assert len(sent_requests) == 4
        assert all(r["type"] == ex_mod.mt5.ORDER_TYPE_BUY for r in sent_requests)
        assert all(r["sl"] == pytest.approx(3880.0) and r["tp"] == pytest.approx(3904.5) for r in sent_requests)

        # 1H: Unpriced "Sell now" stub -> opens L1 (0.01) with Fallback SL + Fallback TP1 applied on watcher tick!
        sent_requests.clear()
        await main.handle_new_message(FakeEvent(108, "Sell XAUUSD now"))
        assert len(sent_requests) == 1
        assert all(r["type"] == ex_mod.mt5.ORDER_TYPE_SELL for r in sent_requests)
        # Fallback SL for SELL @ bid=3895.00 with FALLBACK_SL_PIPS=150 -> 3910.0
        assert all(r["sl"] == pytest.approx(3910.0) for r in sent_requests)

        sig_108 = main.state_mgr.get_signal(108, chat_id=-1001234567890)
        assert sig_108 is not None
        monkeypatch.setattr(main, "UNPRICED_TP_GRACE_SECONDS", 0.0)
        with patch.object(ex, "modify_position_sltp", return_value=True) as mock_mod_pos:
            await main._process_pre_tp1_or_backstop_tick(108, sig_108)
            assert mock_mod_pos.call_count == 1
        sig_108_after = main.state_mgr.get_signal(108, chat_id=-1001234567890)
        assert sig_108_after["raw_tp1"] == pytest.approx(round(3895.20 - config.FALLBACK_TP1_PIPS * 0.10, 2))
        assert sig_108_after["final_tp"] == pytest.approx(round(sig_108_after["raw_tp1"] + config.TP_FRONT_RUN_OFFSET, 2))


# ---------------------------------------------------------------------------
# 2. CHANNEL PROMPTS & REPLIES WHILE ACTIVE — ADJUST SL, TP1, TP2, TP3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2_channel_prompts_and_replies_adjust_sl_tp1_tp2_tp3(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "chan_adj")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_atr = 4.00

    # 2A: Verify parser disambiguation between Move SL to <price> vs Move SL to BE
    p_sl_price = parser.parse_message("Move SL to 3885", is_reply=True)
    assert p_sl_price is not None
    assert p_sl_price.management_action == "ACTION_ADJUST_TARGETS"
    assert p_sl_price.sl == pytest.approx(3885.0) and p_sl_price.sl_valid is True

    p_sl_be = parser.parse_message("Move SL to BE", is_reply=True)
    assert p_sl_be is not None and p_sl_be.management_action == "ACTION_BREAKEVEN"
    p_sl_bare = parser.parse_message("Move SL", is_reply=True)
    assert p_sl_bare is not None and p_sl_bare.management_action == "ACTION_BREAKEVEN"

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Open 4-layer Zone trade (#201): 1 market (L1) + 3 pending limits (L2, L3, L4)
        await main.handle_new_message(
            FakeEvent(201, "Buy 3895-3887\nTP1 3900\nTP2 3910\nTP3 3920\nSL 3875", chat_id=-1001234567890)
        )

        # 1) Reply "Move SL to 3885" -> updates SL on open & pending orders to 3885.0, does NOT trigger BE!
        await main.handle_new_message(FakeEvent(202, "Move SL to 3885", reply_to=201, chat_id=-1001234567890))
        sig = main.state_mgr.get_signal(201, chat_id=-1001234567890)
        assert sig["status"] == "ACTIVE"  # Not BE_APPLIED
        assert sig["sl"] == pytest.approx(3885.0)

        # 2) Standalone channel prompt "TP2 3918 TP3 3930" -> updates TP2 & TP3 while preserving TP1=3900.0!
        await main.handle_new_message(FakeEvent(203, "TP2 3918 TP3 3930", chat_id=-1001234567890))
        sig = main.state_mgr.get_signal(201, chat_id=-1001234567890)
        assert sig["raw_tp1"] == pytest.approx(3900.0)
        assert sig["final_tp"] == pytest.approx(3899.5)
        assert sig["raw_tp2"] == pytest.approx(3918.0)
        assert sig["raw_tp3"] == pytest.approx(3930.0)
        assert sig["tp_levels"]["TP1"] == pytest.approx(3900.0)
        assert sig["tp_levels"]["TP2"] == pytest.approx(3918.0)
        assert sig["tp_levels"]["TP3"] == pytest.approx(3930.0)
        assert sig["tp_levels"]["TP4"] == pytest.approx(3940.0)
        assert sig["tp_levels"]["TP5"] == pytest.approx(3950.0)

        # 3) Combined reply "SL 3884 TP1 3904 TP2 3916 TP3 3928" -> updates SL & TP1..TP3 on all layers
        await main.handle_new_message(
            FakeEvent(204, "SL 3884 TP1 3904 TP2 3916 TP3 3928", reply_to=201, chat_id=-1001234567890)
        )
        sig = main.state_mgr.get_signal(201, chat_id=-1001234567890)
        assert sig["sl"] == pytest.approx(3884.0)
        assert sig["raw_tp1"] == pytest.approx(3904.0)
        assert sig["final_tp"] == pytest.approx(3903.5)
        assert sig["raw_tp2"] == pytest.approx(3916.0)
        assert sig["raw_tp3"] == pytest.approx(3928.0)
        assert all(l["tp"] == pytest.approx(3903.5) for l in sig["layers"].values())

        # 4) Reply "Move SL to BE" -> triggers selective Breakeven (BE_APPLIED)
        await main.handle_new_message(FakeEvent(205, "Move SL to BE", reply_to=201, chat_id=-1001234567890))
        sig = main.state_mgr.get_signal(201, chat_id=-1001234567890)
        assert sig["status"] == "BE_APPLIED"

        # 5) Delayed PENDING signal adjusted before hold timer expires
        monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 10.0)
        await main.handle_new_message(FakeEvent(210, "Buy XAUUSD now", chat_id=-1001234567890))
        sig_pend = main.state_mgr.get_signal(210, chat_id=-1001234567890)
        assert sig_pend["status"] == "PENDING"

        await main.handle_new_message(
            FakeEvent(211, "SL 3878 TP1 3905 TP2 3915 TP3 3925", reply_to=210, chat_id=-1001234567890)
        )
        sig_pend_adj = main.state_mgr.get_signal(210, chat_id=-1001234567890)
        assert sig_pend_adj["sl"] == pytest.approx(3878.0)
        assert sig_pend_adj["raw_tp1"] == pytest.approx(3905.0)
        assert sig_pend_adj["final_tp"] == pytest.approx(3904.5)
        assert sig_pend_adj["raw_tp2"] == pytest.approx(3915.0)
        assert sig_pend_adj["raw_tp3"] == pytest.approx(3925.0)
        main._cancel_delayed_sell_now(210, chat_id=-1001234567890)


# ---------------------------------------------------------------------------
# 3. CHANNEL PROMPTS, REPLIES & EDITS — TRAIL SL & STEP TP1 -> TP2 -> TP3 -> TP4
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_3_channel_prompts_replies_and_edits_trail_and_step_tp1_to_tp4(tmp_path, monkeypatch):
    import mt5_executor as ex_mod
    for key in ("TIERED_STAGE_BANKING_ENABLED",):
        monkeypatch.setattr(config, key, False, raising=False)
        monkeypatch.setattr(ex_mod, key, False, raising=False)
        monkeypatch.setattr(main, key, False, raising=False)
    _reset_runtime(tmp_path, "chan_trail")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_atr = 4.00
    orig_staged = main.STAGED_MARKET_ENTRY
    main.STAGED_MARKET_ENTRY = False  # test expects 4 open market layers at once

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # --- BUY Trade (#301): 4 open market layers ---
        await main.handle_new_message(
            FakeEvent(301, "Buy XAUUSD now\nTP1 3900\nTP2 3910\nTP3 3920\nSL 3875", chat_id=-1001234567890)
        )
        sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
        assert len(sig_buy["market_tickets"]) == 4

        # Step 1: Reply "TP1 hit" -> L1..L3 banked, L4 becomes RUNNER_ACTIVE targeting TP2 (3910.0)
        main.executor._dry_run_tick = SimpleNamespace(bid=3901.0, ask=3901.2, time=1700000100)
        with patch.object(main.executor, "get_tick", return_value=main.executor._dry_run_tick):
            await main.handle_new_message(FakeEvent(302, "TP1 hit", reply_to=301, chat_id=-1001234567890))

            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["status"] == "RUNNER_ACTIVE"
            assert sig_buy["runner_layer"] == "L4"
            assert len(sig_buy["market_tickets"]) == 1
            assert sig_buy["tp_stage"] == "TP2"
            assert sig_buy["layers"]["L4"]["tp"] == pytest.approx(3910.0)
            floor_tp1 = sig_buy["runner_sl_floor"]
            assert floor_tp1 == pytest.approx(3896.2)   # entry + the $1.20 BE lock offset

            # Step 2: Edit original message #301 during RUNNER_ACTIVE with looser SL (3870) + new TP2 3912 TP3 3924
            # Verify ratchet_sl_only=True blocks loosening SL back to 3870 while updating TP2 & TP3!
            await main.handle_message_edited(
                FakeEvent(301, "Buy XAUUSD now\nTP1 3900\nTP2 3912\nTP3 3924\nSL 3870", chat_id=-1001234567890)
            )
            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["runner_sl_floor"] == pytest.approx(floor_tp1)  # Preserved!
            assert sig_buy["tp_levels"]["TP2"] == pytest.approx(3912.0)
            assert sig_buy["tp_levels"]["TP3"] == pytest.approx(3924.0)
            assert sig_buy["layers"]["L4"]["tp"] == pytest.approx(3912.0)

            # Now edit #301 with tighter SL above floor (3900.2) -> accepted!
            await main.handle_message_edited(
                FakeEvent(301, "Buy XAUUSD now\nTP1 3900\nTP2 3912\nTP3 3924\nSL 3900.2", chat_id=-1001234567890)
            )
            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["runner_sl_floor"] == pytest.approx(3900.2)

        # Step 3: Reply "TP2 hit" -> floor ratchets up, runner steps to TP3 (3924.0)
        tick_tp2 = SimpleNamespace(bid=3913.0, ask=3913.2, time=1700000200)
        with patch.object(main.executor, "get_tick", return_value=tick_tp2):
            await main.handle_new_message(FakeEvent(303, "TP2 hit", reply_to=301, chat_id=-1001234567890))
            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["tp_stage"] == "TP3"
            assert "TP2" in sig_buy["runner_cleared_milestones"]
            assert sig_buy["layers"]["L4"]["tp"] == pytest.approx(3924.0)
            assert sig_buy["runner_sl_floor"] >= 3900.2

        # Step 4: Edit #301 text to "TP3 hit! 🔥" -> runner steps to TP4 (3930.0)
        tick_tp3 = SimpleNamespace(bid=3925.0, ask=3925.2, time=1700000300)
        with patch.object(main.executor, "get_tick", return_value=tick_tp3):
            await main.handle_message_edited(FakeEvent(301, "TP3 hit! 🔥", chat_id=-1001234567890))
            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["tp_stage"] == "TP4"
            assert "TP3" in sig_buy["runner_cleared_milestones"]
            assert sig_buy["layers"]["L4"]["tp"] == pytest.approx(3930.0)
            assert sig_buy["runner_sl_floor"] >= 3912.0

            # Step 5: Prompt "Trail SL to 3922" and "Trail 15 pips"
            await main.handle_new_message(FakeEvent(304, "Trail SL to 3922", reply_to=301, chat_id=-1001234567890))
            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["sl"] == pytest.approx(3922.5)

            await main.handle_new_message(FakeEvent(305, "Trail 15 pips", reply_to=301, chat_id=-1001234567890))
            sig_buy = main.state_mgr.get_signal(301, chat_id=-1001234567890)
            assert sig_buy["sl"] == pytest.approx(3925.0 - 1.5)  # 3923.5

        # --- SELL Trade (#350): test "Trail TP2" & "Lock TP3" + live stop-distance clamping ---
        await main.handle_new_message(
            FakeEvent(350, "Sell XAUUSD now\nTP1 3890\nTP2 3880\nTP3 3870\nSL 3910", chat_id=-1001234567890)
        )
        sell_tick = SimpleNamespace(bid=3878.8, ask=3879.0, time=1700000400)
        with patch.object(main.executor, "get_tick", return_value=sell_tick):
            await main.handle_new_message(FakeEvent(351, "Trail TP2", reply_to=350, chat_id=-1001234567890))
            sig_sell = main.state_mgr.get_signal(350, chat_id=-1001234567890)
            assert sig_sell["status"] == "RUNNER_ACTIVE"
            assert sig_sell["tp_stage"] == "TP3"
            assert sig_sell["layers"]["L4"]["tp"] == pytest.approx(3870.0)

        sell_tick_tp3 = SimpleNamespace(bid=3868.8, ask=3869.0, time=1700000500)
        with patch.object(main.executor, "get_tick", return_value=sell_tick_tp3):
            await main.handle_new_message(FakeEvent(352, "Lock TP3", reply_to=350, chat_id=-1001234567890))
            sig_sell = main.state_mgr.get_signal(350, chat_id=-1001234567890)
            assert sig_sell["tp_stage"] == "TP4"
            assert sig_sell["layers"]["L4"]["tp"] == pytest.approx(3860.0)

    main.STAGED_MARKET_ENTRY = orig_staged


# ---------------------------------------------------------------------------
# 4. USER PROMPTS IN SAVED MESSAGES ("me") END-TO-END LIFECYCLE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_4_saved_messages_user_prompts_end_to_end_lifecycle(tmp_path):
    _reset_runtime(tmp_path, "saved_prompts")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    async def _send_saved(msg_id: int, text: str) -> FakeEvent:
        ev = FakeEvent(msg_id, text, chat_id="me")
        await main.handle_saved_messages_command(ev)
        return ev

    # 1) Execute trade #401 directly from Saved Messages via `/trade`
    ev_trade1 = await _send_saved(401, "/trade Buy 3895-3887 TP1 3900 TP2 3910 TP3 3920 SL 3875")
    assert any("Saved Messages 4-Layer Trade Executed" in r for r in ev_trade1.replies)
    sig_401 = main.state_mgr.get_signal(401, chat_id="me")
    assert sig_401 is not None
    assert sig_401["sl"] == pytest.approx(3875.0)
    assert sig_401["final_tp"] == pytest.approx(3899.5)

    # 2) Adjust SL & TPs via Saved Messages prompts: `/sl 3883`, `move sl to 3885`, `/tp2 3914`, `/tp 3902 3915 3928`
    await _send_saved(402, "/sl 3883")
    assert main.state_mgr.get_signal(401, chat_id="me")["sl"] == pytest.approx(3883.0)

    await _send_saved(403, "move sl to 3885")
    assert main.state_mgr.get_signal(401, chat_id="me")["sl"] == pytest.approx(3885.0)

    await _send_saved(404, "/tp2 3914")
    sig_401 = main.state_mgr.get_signal(401, chat_id="me")
    assert sig_401["raw_tp1"] == pytest.approx(3900.0)  # TP1 preserved!
    assert sig_401["raw_tp2"] == pytest.approx(3914.0)

    await _send_saved(405, "/tp 3902 3915 3928")
    sig_401 = main.state_mgr.get_signal(401, chat_id="me")
    assert sig_401["raw_tp1"] == pytest.approx(3902.0)
    assert sig_401["final_tp"] == pytest.approx(3901.5)
    assert sig_401["raw_tp2"] == pytest.approx(3915.0)
    assert sig_401["raw_tp3"] == pytest.approx(3928.0)

    # 3) `/be` and `close 50%` (ACTION_PARTIAL_CLOSE)
    ev_be = await _send_saved(406, "/be")
    assert any("Selective Breakeven Applied" in r for r in ev_be.replies)
    assert main.state_mgr.get_signal(401, chat_id="me")["status"] == "BE_APPLIED"

    # Open a second trade (#410: 4 market orders) to test explicit `#<msg_id>` selector + partial close + trail
    await _send_saved(410, "/trade Buy XAUUSD now TP1 3905 TP2 3915 TP3 3925 SL 3880")
    sig_410 = main.state_mgr.get_signal(410, chat_id="me")
    assert len(sig_410["market_tickets"]) == 4

    # Target #401 explicitly while #410 is the latest signal!
    # Wait: #401 already applied /be (be_sl=3896.2), so loosening back to 3886.0 MUST be blocked by v3.6 guard,
    # whereas tightening SL to 3896.5 succeeds on #401 while #410 stays 3880.0!
    await _send_saved(411, "/sl 3886 #401")
    assert main.state_mgr.get_signal(401, chat_id="me")["sl"] == pytest.approx(3885.0)
    await _send_saved(4111, "/sl 3896.5 #401")
    assert main.state_mgr.get_signal(401, chat_id="me")["sl"] == pytest.approx(3896.5)
    assert main.state_mgr.get_signal(410, chat_id="me")["sl"] == pytest.approx(3880.0)

    # `close 50%` on latest (#410) -> closes 1 market ticket, leaves 3 open
    ev_part = await _send_saved(412, "close 50%")
    assert any("Partial Close Executed" in r for r in ev_part.replies)
    assert len(main.state_mgr.get_signal(410, chat_id="me")["market_tickets"]) == 3

    # `tp1 hit`, `trail tp2`, `/trail 12` on #410
    tick_high = SimpleNamespace(bid=3916.0, ask=3916.2, time=1700000600)
    with patch.object(main.executor, "get_tick", return_value=tick_high):
        await _send_saved(413, "tp1 hit #410")
        sig_410 = main.state_mgr.get_signal(410, chat_id="me")
        assert sig_410["status"] == "RUNNER_ACTIVE"
        assert sig_410["tp_stage"] == "TP2"

        await _send_saved(414, "trail tp2 #410")
        sig_410 = main.state_mgr.get_signal(410, chat_id="me")
        assert sig_410["tp_stage"] == "TP3"
        assert sig_410["layers"][sig_410["runner_layer"]]["tp"] == pytest.approx(3925.0)

        await _send_saved(415, "/trail 12 #410")
        sig_410 = main.state_mgr.get_signal(410, chat_id="me")
        assert sig_410["sl"] == pytest.approx(3916.0 - 1.2)  # 3914.8

    # 4) Interactive `dailywin` / `dailyloss` prompts + `/sl` slash-escape
    await _send_saved(420, "dailywin")
    assert main._awaiting_dailywin_input is True
    ev_bad = await _send_saved(421, "not-a-number")
    assert any("Invalid Daily Win Amount" in r for r in ev_bad.replies)

    await _send_saved(422, "dailywin")
    ev_ok = await _send_saved(423, "£20")
    assert main._awaiting_dailywin_input is False
    assert any("20.00" in r for r in ev_ok.replies)

    # Start `dailyloss` prompt, then escape cleanly with `/sl 3897 #401` (forward-only tighten above BE 3896.0)
    await _send_saved(424, "dailyloss")
    assert main._awaiting_dailyloss_input is True
    await _send_saved(425, "/sl 3897 #401")
    assert main._awaiting_dailyloss_input is False
    assert main.state_mgr.get_signal(401, chat_id="me")["sl"] == pytest.approx(3897.0)


# ---------------------------------------------------------------------------
# 5. AUTOMATIC 1S RUNNER TRAILING + ANTI-CHOKE + DELETED-SIGNAL REGRESSION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_5_automatic_watcher_trailing_antichoke_and_deleted_signal_regression(tmp_path):
    _reset_runtime(tmp_path, "watcher_reg")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_atr = 4.00

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Open 4-layer Zone BUY (#500): L1 market (0.01) + L2..L4 pending limits
        await main.handle_new_message(
            FakeEvent(500, "Buy 3895-3887\nTP1 3900\nTP2 3910\nTP3 3920\nSL 3875", chat_id=-1001234567890)
        )
        sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
        assert sig["layers"]["L1"]["tp"] == pytest.approx(3899.5)

        # 1) Pre-TP1 tight zone (3899.2): N=1 active open layer (L1) -> proactively lifted to TP2 (3910.0)
        #    and 6-pip micro-dip (3898.6) does NOT choke/close the trade!
        for bid_px in (3899.2, 3898.6, 3899.4):
            tk = SimpleNamespace(bid=bid_px, ask=bid_px + 0.2, time=1700001000)
            with patch.object(main.executor, "get_tick", return_value=tk):
                sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
                await main._process_pre_tp1_or_backstop_tick(500, sig)

        sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
        assert sig["status"] != "CLOSED"
        assert sig["layers"]["L1"]["tp"] == pytest.approx(3910.0)
        assert sig["layers"]["L1"]["tp_stage"] == "TP2"

        # 2) Clean breakout past TP1 (3900.6) -> transitions to RUNNER_ACTIVE (L1 is sole runner),
        #    then breaks past TP2 (3910.6) -> steps to TP3 (3920.0) and trails SL upward!
        tk_break1 = SimpleNamespace(bid=3900.6, ask=3900.8, time=1700001100)
        with patch.object(main.executor, "get_tick", return_value=tk_break1):
            sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
            await main._process_pre_tp1_or_backstop_tick(500, sig)
            sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
            assert sig["status"] == "RUNNER_ACTIVE"
            assert sig["runner_layers"] == ["L1"]
            await main._process_runner_active_tick(500, sig)

        tk_break2 = SimpleNamespace(bid=3910.6, ask=3910.8, time=1700001200)
        with patch.object(main.executor, "get_tick", return_value=tk_break2):
            sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
            await main._process_runner_active_tick(500, sig)

        sig = main.state_mgr.get_signal(500, chat_id=-1001234567890)
        assert "TP2" in sig["runner_cleared_milestones"]
        assert sig["tp_stage"] == "TP3"
        assert sig["layers"]["L1"]["tp"] == pytest.approx(3920.0)
        assert sig["runner_sl_floor"] >= 3900.0

        # 3) Signal #500 deleted in Telegram mid-run -> closed immediately & recorded to TradeLedger!
        with patch.object(main.executor, "get_position_close_info", return_value={"profit": 15.60, "reason": "MANUAL"}):
            await main.handle_message_deleted(FakeDeleteEvent(deleted_ids=[500], chat_id=-1001234567890))

        sig_final = main.state_mgr.get_signal(500, chat_id=-1001234567890)
        assert sig_final["status"] == "CLOSED"
        assert sig_final["deleted_from_chat"] is True
        assert sig_final["market_tickets"] == []
        trades = main.trade_ledger.read_trades()
        assert len(trades) == 1
        assert trades[0]["realized_pnl"] == pytest.approx(15.60)
