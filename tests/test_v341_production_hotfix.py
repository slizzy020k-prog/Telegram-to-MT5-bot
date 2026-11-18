"""
Production Hotfix Regression Suite (`v3.4.1`) covering all 4 live production incidents:
1. Bug 1 (Strict Signal Intent Whitelist):
   - Chatter rejections: `"lmao + 410 pips sell straight into a TP1 buy"` (reply & non-reply),
     `"sell straight"`, Msg 13043 (`"When u sent the sell limit i went and brought and sold it at 3879.55😂😂"`),
     Msg 9678 (`"alhamdullilah was able to buy my mum a gift all thanks to you"`),
     Msg 12983 (`"£150 to £600 , bro x4 his money with 1 sell from last night lol"`),
     and `"How to place a buy limit"` -> `parse_message(...) is None` and 0 orders via `main.handle_new_message`.
   - Whitelist positives: `Buy now`, `Sell now`, `Buy xauusd now`, `BUY NOW TP1 2655 SL 2640`,
     `Buy limit 3851-3841`, `BUY LIMIT 3851-3841\nSL 3825\nTP 3854`, `Sell limit 3890`,
     `Buy limits 3840 & 3830`, `Buy stop 3905`, `BUY XAUUSD 3853`, `SELL XAUUSD 3860`,
     `BUY 3847-3837\nSL 3820\nTP 3850`, `Buy 3895-3887 TP 3900 SL 3875`,
     `sell with limit 3890`, `buy with limit 3840`.
2. Bug 2 (Stub L1-Only Allocation & 4-Layer Zone Expansion Cap):
   - `Buy now` stub (no SL/TP) -> opens ONLY `L1` (0.01 market); edited to `BUY 3847-3837\nSL 3820\nTP 3850`
     -> keeps L1 market + places 3 pending limits (`L2=3843.67`, `L3=3840.33`, `L4=3837.0`), total 4 orders (never 5).
   - `BUY XAUUSD 3853` single-price stub (no SL/TP) -> opens ONLY `L1` (0.01 @ 3853.0);
     edited with TP/SL only (`BUY XAUUSD 3853\nTP 3860\nSL 3840`) -> updates SL/TP on L1, still 1 order (no stacking);
     edited with zone (`BUY XAUUSD 3853-3848\nTP 3860\nSL 3840`) -> expands to 4 layers across zone (`3853.0 / 3851.33 / 3849.67 / 3848.0`).
3. Bug 3 (Entry-First TP1 Invalidation Gate):
   - `BUY 3847-3837\nSL 3820\nTP 3850` placed while market is above zone (`ask=3852.0` / `3849.68`)
     and ticks up to `bid=3850.05` BEFORE ever entering `<= 3847.0` -> pending limits stay ACTIVE (`has_entered_zone=False`)!
   - Once `ask <= 3847.0` (`has_entered_zone=True`, 0 market fills) and price subsequently bounces to `bid >= 3850.0`
     -> invalidated (`status == "INVALIDATED_TP1"`).
4. Bug 4 (TP1-Touch Bank System & Hard Giveback Ceiling):
   - Runner / 4-layer trade with `TP1=3850.0`: single tick touches `3850.00` (`tp1_touched=True`),
     locking broker SL & virtual floor at `>= 3848.80` (`TP1 - $1.20`); subsequent drop to `3848.70`
     immediately banks runner + any remaining open layers at market!
   - Hard giveback ceiling (`HARD_GIVEBACK_CEILING_DOLLARS = 3.50`) forces exit even during spread spike or large peak profit.
5. Bug 5 (`ACTION_REACTIVATE` Guard & Edited Reply Deduplication):
   - `ACTION_REACTIVATE` (`"Active"` reply) on a `RUNNER_ACTIVE` (or `ACTIVE` / `BE_APPLIED`) signal is ignored
     and NEVER overwrites open runner/market tickets.
   - An edited `"Active"` reply message (`MessageEdited` on an untracked reply) is deduplicated and not re-executed.
"""

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
    def __init__(self, msg_id: int, text: str, reply_to=None, is_forwarded: bool = False, chat_id: int = -1003712081715):
        self.message = FakeMsg(msg_id, text, reply_to, is_forwarded)
        self.chat_id = chat_id
        self.is_private = False
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, msg_txt: str):
        self.replies.append(msg_txt)

    async def get_reply_message(self):
        return None


def _reset_runtime(tmp_path, name: str = "v341"):
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
    main.CHANNEL_IDS = [-1003712081715, -1004369388188]
    state_file = tmp_path / f"{name}_state.json"
    ledger_file = tmp_path / f"{name}_ledger.jsonl"
    main.state_mgr = StateManager(str(state_file))
    main.trade_ledger = TradeLedger(str(ledger_file))
    return state_file, ledger_file


# ---------------------------------------------------------------------------
# 1. BUG 1: STRICT SIGNAL INTENT WHITELIST & CHATTER REJECTION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1_strict_signal_intent_whitelist_blocks_chatter_and_allows_real_signals(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "bug1_whitelist")
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 0.0)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    chatter_samples = [
        ("lmao + 410 pips sell straight into a TP1 buy", True),
        ("lmao + 410 pips sell straight into a TP1 buy", False),
        ("sell straight", False),
        ("sell straight", True),
        ("When u sent the sell limit i went and brought and sold it at 3879.55😂😂", False),
        ("alhamdullilah was able to buy my mum a gift all thanks to you", False),
        ("£150 to £600 , bro x4 his money with 1 sell from last night lol 😆😮‍💨", False),
        ("How to place a buy limit .", False),
        ("How to place a buy limit", False),
    ]

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        for idx, (text, as_reply) in enumerate(chatter_samples, start=13060):
            parsed = parser.parse_message(text, message_id=idx, is_reply=as_reply)
            assert parsed is None, f"Chatter must be rejected by parser: {text!r} (is_reply={as_reply}) -> {parsed}"

            ev = FakeEvent(idx, text, reply_to=13030 if as_reply else None, chat_id=-1003712081715)
            await main.handle_new_message(ev)
            assert main.state_mgr.get_signal(idx, chat_id=-1003712081715) is None
            assert main.executor._dry_run_ticket_lots == {}

    # Whitelist positives must all parse cleanly as NEW_SIGNAL
    whitelist_positives = [
        ("Buy now", "BUY", "MARKET"),
        ("Sell now", "SELL", "MARKET"),
        ("Buy xauusd now", "BUY", "MARKET"),
        ("BUY NOW TP1 2655 SL 2640", "BUY", "MARKET"),
        ("Buy limit 3851-3841", "BUY", "LIMIT"),
        ("BUY LIMIT 3851-3841\nSL 3825\nTP 3854", "BUY", "LIMIT"),
        ("Sell limit 3890", "SELL", "LIMIT"),
        ("Buy limits 3840 & 3830", "BUY", "LIMIT"),
        ("Buy stop 3905", "BUY", "STOP"),
        ("BUY XAUUSD 3853", "BUY", "AUTO"),
        ("SELL XAUUSD 3860", "SELL", "AUTO"),
        ("BUY 3847-3837\nSL 3820\nTP 3850\nTP 3857", "BUY", "AUTO"),
        ("Buy 3895-3887 TP 3900 SL 3875", "BUY", "AUTO"),
        ("sell with limit 3890", "SELL", "LIMIT"),
        ("buy with limit 3840", "BUY", "LIMIT"),
    ]
    for idx, (sig_text, exp_dir, exp_intent) in enumerate(whitelist_positives, start=14000):
        res = parser.parse_message(sig_text, message_id=idx)
        assert res is not None, f"Valid signal was rejected: {sig_text!r}"
        assert res.event_type == "NEW_SIGNAL"
        assert res.direction == exp_dir
        assert res.order_intent == exp_intent


# ---------------------------------------------------------------------------
# 2. BUG 2: STUB L1-ONLY ALLOCATION & 4-LAYER ZONE EXPANSION (NEVER 5 ORDERS)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2_stub_l1_only_and_zone_edit_expansion_never_exceeds_4_layers(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "bug2_stub_zone")
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 0.0)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Live price at 3849.50 (above 3847-3837 zone, and below 3853 stub or above 3853 stub)
    tick_now = SimpleNamespace(bid=3849.20, ask=3849.70, time=1700010000)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_tick", return_value=tick_now):

        # --- Part A: "Buy now" stub (no SL, no TP) -> opens ONLY L1 (0.01 market) ---
        await main.handle_new_message(FakeEvent(9801, "Buy now", chat_id=-1003712081715))
        sig_a = main.state_mgr.get_signal(9801, chat_id=-1003712081715)
        assert sig_a is not None
        assert len(sig_a["market_tickets"]) == 1
        assert len(sig_a["pending_tickets"]) == 0
        assert set(sig_a["layers"].keys()) == {"L1"}
        assert sig_a["layers"]["L1"]["lot"] == pytest.approx(0.01)

        # Edit "Buy now" with TP/SL only (no zone) -> stays at 1 layer!
        await main.handle_message_edited(
            FakeEvent(9801, "Buy now\nSL 3820\nTP 3850\nTP 3857", chat_id=-1003712081715)
        )
        sig_a_tpsl = main.state_mgr.get_signal(9801, chat_id=-1003712081715)
        assert len(sig_a_tpsl["market_tickets"]) == 1
        assert len(sig_a_tpsl["pending_tickets"]) == 0
        assert set(sig_a_tpsl["layers"].keys()) == {"L1"}
        assert sig_a_tpsl["sl"] == pytest.approx(3820.0)
        assert sig_a_tpsl["final_tp"] == pytest.approx(3849.5)

        # Edit "Buy now" -> "BUY 3847-3837\nSL 3820\nTP 3850"
        # Keeps L1 market + places 3 pending limits at L2=3843.67, L3=3840.33, L4=3837.0 -> total 4!
        await main.handle_message_edited(
            FakeEvent(9801, "BUY 3847-3837\nSL 3820\nTP 3850\nTP 3857", chat_id=-1003712081715)
        )
        sig_a2 = main.state_mgr.get_signal(9801, chat_id=-1003712081715)
        assert len(sig_a2["market_tickets"]) == 1
        assert len(sig_a2["pending_tickets"]) == 3
        assert len(sig_a2["market_tickets"]) + len(sig_a2["pending_tickets"]) == 4
        assert set(sig_a2["layers"].keys()) == {"L1", "L2", "L3", "L4"}
        assert sig_a2["layers"]["L2"]["target_price"] == pytest.approx(3843.67)
        assert sig_a2["layers"]["L3"]["target_price"] == pytest.approx(3840.33)
        assert sig_a2["layers"]["L4"]["target_price"] == pytest.approx(3837.0)
        assert sig_a2["layers"]["L4"]["lot"] == pytest.approx(0.02)

        # --- Part A2: Zone edit when L1..L4 are ALL already filled -> 0 new orders placed! ---
        # Disable staged entry so 9802 fires all 4 layers at once (simulates full market fill scenario)
        _orig_staged = main.STAGED_MARKET_ENTRY
        main.STAGED_MARKET_ENTRY = False
        await main.handle_new_message(
            FakeEvent(9802, "Buy now\nSL 3820\nTP 3855", chat_id=-1003712081715)
        )
        main.STAGED_MARKET_ENTRY = _orig_staged
        sig_full = main.state_mgr.get_signal(9802, chat_id=-1003712081715)
        assert len(sig_full["market_tickets"]) == 4
        orig_full_tkts = list(sig_full["market_tickets"])
        await main.handle_message_edited(
            FakeEvent(9802, "BUY 3847-3837\nSL 3820\nTP 3855", chat_id=-1003712081715)
        )
        sig_full_after = main.state_mgr.get_signal(9802, chat_id=-1003712081715)
        assert sig_full_after["market_tickets"] == orig_full_tkts
        assert sig_full_after["pending_tickets"] == []

    # --- Part B: Single-price stub "BUY XAUUSD 3853" (Msg 9759 pattern) ---
    tick_9759 = SimpleNamespace(bid=3853.10, ask=3853.60, time=1700010100)
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_tick", return_value=tick_9759):

        # 1) Initial stub "BUY XAUUSD 3853" (no SL/TP) -> ONLY L1 (0.01 limit @ 3853.0)
        await main.handle_new_message(FakeEvent(9759, "BUY XAUUSD 3853", chat_id=-1004369388188))
        sig_b1 = main.state_mgr.get_signal(9759, chat_id=-1004369388188)
        assert sig_b1 is not None
        assert len(sig_b1["market_tickets"]) + len(sig_b1["pending_tickets"]) == 1
        assert set(sig_b1["layers"].keys()) == {"L1"}
        assert sig_b1["layers"]["L1"]["target_price"] == pytest.approx(3853.0)
        assert sig_b1["layers"]["L1"]["lot"] == pytest.approx(0.01)

        # 2) Edit adding TP & SL only (still single price 3853) -> updates SL/TP on L1, still 1 order (no stacking!)
        await main.handle_message_edited(
            FakeEvent(9759, "BUY XAUUSD 3853\n\nTP 3860\nTP 3870\n\nSL 3840", chat_id=-1004369388188)
        )
        sig_b2 = main.state_mgr.get_signal(9759, chat_id=-1004369388188)
        assert len(sig_b2["market_tickets"]) + len(sig_b2["pending_tickets"]) == 1
        assert set(sig_b2["layers"].keys()) == {"L1"}
        assert sig_b2["sl"] == pytest.approx(3840.0)
        assert sig_b2["final_tp"] == pytest.approx(3859.5)

        # 3) Edit adding zone "BUY XAUUSD 3853-3848" -> expands to 4 layers across zone (3853 / 3851.33 / 3849.67 / 3848)!
        await main.handle_message_edited(
            FakeEvent(9759, "BUY XAUUSD 3853-3848\n\nTP 3860\nTP 3870\n\nSL 3840", chat_id=-1004369388188)
        )
        sig_b3 = main.state_mgr.get_signal(9759, chat_id=-1004369388188)
        assert len(sig_b3["market_tickets"]) + len(sig_b3["pending_tickets"]) == 4
        assert set(sig_b3["layers"].keys()) == {"L1", "L2", "L3", "L4"}
        prices = [sig_b3["layers"][k]["target_price"] for k in ("L1", "L2", "L3", "L4")]
        assert prices == [pytest.approx(3853.0), pytest.approx(3851.33), pytest.approx(3849.67), pytest.approx(3848.0)]
        assert len(set(round(p, 2) for p in prices)) == 4  # Never duplicated at 3853.0!


# ---------------------------------------------------------------------------
# 3. BUG 3: ENTRY-FIRST GATE BEFORE PENDING TP1 INVALIDATION (MSG 13067)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_3_pending_zone_never_invalidated_at_tp1_before_price_enters_zone(tmp_path):
    _reset_runtime(tmp_path, "bug3_entry_gate")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Signal 13067: BUY 3847-3837 | SL 3820 | TP 3850 placed when live market is above zone (3849.16 / 3849.68)
    tick_initial = SimpleNamespace(bid=3849.16, ask=3849.68, time=1700020000)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        with patch.object(main.executor, "get_tick", return_value=tick_initial):
            await main.handle_new_message(
                FakeEvent(13067, "BUY 3847-3837\nSL 3820\nTP 3850\nTP 3857\nTP 3860", chat_id=-1003712081715)
            )

        sig = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
        assert sig is not None
        assert len(sig["pending_tickets"]) == 4
        assert sig.get("tp1_guard_armed") is False

        # Market drifts above TP1 (3850.05 and 3852.00) WITHOUT ever entering <= 3847.0 -> pending limits MUST STAY!
        for bid_px, ask_px in ((3850.05, 3850.57), (3852.00, 3852.52)):
            tk_above = SimpleNamespace(bid=bid_px, ask=ask_px, time=1700020020)
            with patch.object(main.executor, "get_tick", return_value=tk_above):
                sig = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
                await main._process_pre_tp1_or_backstop_tick(13067, sig)

        sig_still_open = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
        assert sig_still_open["status"] == "ACTIVE"
        assert len(sig_still_open["pending_tickets"]) == 4

        # Now ask dips into the zone (ask=3846.90 <= 3847.0) without filling, arming has_entered_zone / tp1_guard_armed
        tk_enter = SimpleNamespace(bid=3846.40, ask=3846.90, time=1700020100)
        with patch.object(main.executor, "get_tick", return_value=tk_enter):
            sig = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(13067, sig)

        sig_armed = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
        assert sig_armed.get("has_entered_zone") is True
        assert sig_armed.get("tp1_guard_armed") is True
        assert sig_armed["status"] == "ACTIVE"

        # Now price bounces to TP1 (bid=3850.05 >= 3850.0) with 0 fills -> invalidated!
        tk_bounce_tp1 = SimpleNamespace(bid=3850.05, ask=3850.55, time=1700020200)
        with patch.object(main.executor, "get_tick", return_value=tk_bounce_tp1):
            sig = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(13067, sig)

        sig_inv = main.state_mgr.get_signal(13067, chat_id=-1003712081715)
        assert sig_inv["status"] == "INVALIDATED_TP1"
        assert sig_inv["pending_tickets"] == []


# ---------------------------------------------------------------------------
# 4. BUG 4: TP1-TOUCH BANK SYSTEM & HARD GIVEBACK CEILING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_4_tp1_touch_bank_floor_and_pullback_closes_all_remaining_layers(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "bug4_tp1_touch_bank")
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "TP1_TRAIL_ENABLED", False, raising=False)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Seed 4-layer BUY trade (Entry 3845..3841, TP1=3850.0, TP2=3860.0, TP3=3870.0, SL=3830.0)
    for t, lot in ((7001, 0.01), (7002, 0.01), (7003, 0.01), (7004, 0.02)):
        main.executor._dry_run_ticket_lots[t] = lot

    main.state_mgr.create_signal(
        message_id=13070,
        symbol="XAUUSD.s",
        direction="BUY",
        zone={"top": 3845.0, "mid": 3843.0, "bottom": 3841.0},
        sl=3830.0,
        final_tp=3849.5,
        raw_tp1=3850.0,
        raw_tp2=3860.0,
        raw_tp3=3870.0,
        raw_tp4=3880.0,
        raw_tp5=3890.0,
        tp_levels={"TP1": 3850.0, "TP2": 3860.0, "TP3": 3870.0, "TP4": 3880.0, "TP5": 3890.0},
        market_tickets=[7001, 7002, 7003, 7004],
        pending_tickets=[],
        layers={
            "L1": {"ticket": 7001, "lot": 0.01, "status": "OPEN", "target_price": 3845.0, "sl": 3830.0, "tp": 3849.5, "tp_stage": "TP1"},
            "L2": {"ticket": 7002, "lot": 0.01, "status": "OPEN", "target_price": 3843.67, "sl": 3830.0, "tp": 3849.5, "tp_stage": "TP1"},
            "L3": {"ticket": 7003, "lot": 0.01, "status": "OPEN", "target_price": 3842.33, "sl": 3830.0, "tp": 3849.5, "tp_stage": "TP1"},
            "L4": {"ticket": 7004, "lot": 0.02, "status": "OPEN", "target_price": 3841.0, "sl": 3830.0, "tp": 3849.5, "tp_stage": "TP1"},
        },
        status="ACTIVE",
        chat_id=-1003712081715,
    )

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # 1) Single tick touches TP1 = 3850.00 -> TP1 handoff sets tp1_touched=True and locks floor/SL >= 3848.80
        #    Also keep L2 (#7002) & L3 (#7003) open post-TP1 to verify broker SL on L2/L3 + atomic close on fallback bank!
        tk_touch = SimpleNamespace(bid=3850.00, ask=3850.50, time=1700030000)
        mod_sl_calls = {}
        orig_mod_pos = main.executor.modify_position_sltp

        def _spy_mod_pos(ticket, sl=None, tp=None):
            if sl is not None:
                mod_sl_calls[int(ticket)] = float(sl)
            return orig_mod_pos(ticket, sl=sl, tp=tp)

        with patch.object(main.executor, "get_tick", return_value=tk_touch), \
             patch.object(main.executor, "modify_position_sltp", side_effect=_spy_mod_pos):
            sig = main.state_mgr.get_signal(13070, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(13070, sig)

        sig_touched = main.state_mgr.get_signal(13070, chat_id=-1003712081715)
        assert sig_touched["status"] == "RUNNER_ACTIVE"
        assert sig_touched.get("tp1_touched") is True
        assert float(sig_touched.get("runner_sl_floor") or 0.0) >= 3848.80 - 1e-6
        assert mod_sl_calls.get(7004, 0.0) >= 3848.80 - 1e-6

        # Simulate surviving L2/L3 held open alongside L4 runner when TP1-touch protection applies
        main.executor._dry_run_ticket_lots[7002] = 0.01
        main.executor._dry_run_ticket_lots[7003] = 0.01
        sig_touched["market_tickets"] = [7002, 7003, 7004]
        sig_touched["layers"]["L2"]["status"] = "OPEN"
        sig_touched["layers"]["L3"]["status"] = "OPEN"
        main.state_mgr.update_signal(13070, sig_touched, chat_id=-1003712081715)

        with patch.object(main.executor, "get_tick", return_value=tk_touch), \
             patch.object(main.executor, "modify_position_sltp", side_effect=_spy_mod_pos):
            main.executor._apply_tp1_touch_protection(
                sig_touched, "XAUUSD.s", "BUY", 3850.0, 0.10, 2, tick=tk_touch
            )
        assert mod_sl_calls.get(7002, 0.0) >= 3848.80 - 1e-6
        assert mod_sl_calls.get(7003, 0.0) >= 3848.80 - 1e-6

        # 2) Subsequent tick drops to 3848.70 (below 3850.0 - 1.20 = 3848.80) -> banks runner & remaining L2/L3!
        tk_drop = SimpleNamespace(bid=3848.70, ask=3849.20, time=1700030005)
        with patch.object(main.executor, "get_tick", return_value=tk_drop), \
             patch.object(main.executor, "get_position_close_info", return_value={"profit": 12.50, "reason": "MANUAL"}):
            sig = main.state_mgr.get_signal(13070, chat_id=-1003712081715)
            await main._process_runner_active_tick(13070, sig)

        sig_banked = main.state_mgr.get_signal(13070, chat_id=-1003712081715)
        assert sig_banked["status"] == "CLOSED"
        assert sig_banked["market_tickets"] == []
        assert "TP1-Touch Fallback Bank" in str(sig_banked.get("runner_close_reason", ""))

        # 3) Verify HARD_GIVEBACK_CEILING_DOLLARS ($3.50) forces exit even during a spread spike
        sig_ceil = {
            "symbol": "XAUUSD.s",
            "direction": "BUY",
            "status": "RUNNER_ACTIVE",
            "tp1_reached": True,
            "tp1_touched": True,
            "reversal_guard_armed": True,
            "raw_tp1": 3850.0,
            "runner_peak_price": 3865.0,
            "peak_profit": 24.0,
            "reversal_guard_peak_profit": 24.0,
            "reversal_guard_runner_entry": 3841.0,
            "layers": {"L4": {"ticket": 7999, "lot": 0.02, "status": "RUNNER", "target_price": 3841.0}},
            "market_tickets": [7999],
            "runner_tickets": [7999],
            "runner_layers": ["L4"],
        }
        # 3865.0 -> 3861.40 (-$3.60 >= $3.50 hard ceiling) with $0.70 spread spike
        tk_ceil = SimpleNamespace(bid=3861.40, ask=3862.10, time=1700030010)
        res_ceil = main.executor.evaluate_gold_reversal_guard(sig_ceil, tick=tk_ceil)
        assert res_ceil["triggered"] is True
        assert res_ceil.get("hard_ceiling_triggered") is True


# ---------------------------------------------------------------------------
# 5. BUG 5: ACTION_REACTIVATE ON RUNNER_ACTIVE IGNORED & EDITED "ACTIVE" DEDUPED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_5_reactivate_ignored_on_runner_active_and_edited_active_reply_not_rerun(tmp_path):
    _reset_runtime(tmp_path, "bug5_reactivate")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Create signal 13055: BUY LIMIT 3851-3841, L1 filled (#152769467), status transitions to RUNNER_ACTIVE
        main.executor._dry_run_ticket_lots[152769467] = 0.01
        main.state_mgr.create_signal(
            message_id=13055,
            symbol="XAUUSD.s",
            direction="BUY",
            zone={"top": 3851.0, "mid": 3846.0, "bottom": 3841.0},
            sl=3825.0,
            final_tp=3853.5,
            raw_tp1=3854.0,
            raw_tp2=3861.0,
            raw_tp3=3864.0,
            raw_tp4=3874.0,
            raw_tp5=3891.0,
            tp_levels={"TP1": 3854.0, "TP2": 3861.0, "TP3": 3864.0, "TP4": 3874.0, "TP5": 3891.0},
            market_tickets=[152769467],
            pending_tickets=[],
            layers={
                "L1": {"ticket": 152769467, "lot": 0.01, "status": "RUNNER", "target_price": 3851.0, "sl": 3852.8, "tp": 3861.0, "tp_stage": "TP2"},
            },
            status="RUNNER_ACTIVE",
            chat_id=-1003712081715,
        )
        main.state_mgr.update_signal(
            13055,
            {
                "tp1_reached": True,
                "runner_ticket": 152769467,
                "runner_tickets": [152769467],
                "runner_layer": "L1",
                "runner_layers": ["L1"],
            },
            chat_id=-1003712081715,
        )

        with patch.object(main.executor, "execute_4layer_zone") as spy_exec_zone:
            # 1) New reply 13058: "Active" replying to 13055 while RUNNER_ACTIVE -> MUST BE IGNORED!
            await main.handle_new_message(FakeEvent(13058, "Active", reply_to=13055, chat_id=-1003712081715))
            assert spy_exec_zone.call_count == 0

            # 2) Edited reply 13058: "Active" (same text edited) -> MUST NOT re-execute!
            await main.handle_message_edited(FakeEvent(13058, "Active", reply_to=13055, chat_id=-1003712081715))
            assert spy_exec_zone.call_count == 0

        sig_13055 = main.state_mgr.get_signal(13055, chat_id=-1003712081715)
        assert sig_13055["status"] == "RUNNER_ACTIVE"
        assert sig_13055["market_tickets"] == [152769467]
        assert sig_13055["runner_ticket"] == 152769467
