"""
Comprehensive Verification & Regression Suite for XAUUSD Copier v3.4.2:
Dynamic ATR-Calibrated TP1 Trailing Stop (`TP1_TRAIL_ENABLED`, `TP1_TRAIL_ATR_MULT=1.5`,
`TP1_TRAIL_MIN_DOLLARS=2.00`, `TP1_TRAIL_MAX_DOLLARS=3.00`, `TP1_TRAIL_STEP_DOLLARS=0.20`):

Scenarios covered (BUY TP1=3850, entry=3840, ATR->$2.50 trail_dist + SELL mirror):
  a) Touch 3850.00 -> trail_stop=3847.50 applied as broker SL across all open layers (runner + L2/L3);
     wiggle to 3848.70 and 3848.00 -> NOT closed (old 12p rule would have closed prematurely).
  b) Rally 3850.00 -> 3853.40 -> trail_stop ratchets to 3850.90; dip to 3851.50 holds;
     drop to 3850.85 -> all open layers closed with reason `"TP1 Trailing Stop"`.
     (Plus full SELL mirror: entry=3860, TP1=3850 -> touch 3850.00 sets stop=3852.50,
      wiggle 3851.80 holds, rally to 3846.60 ratchets stop to 3849.10, bounce to 3849.15 closes!)
  c) Stop never moves backward when ATR expands (e.g. $2.00 -> $3.00);
     broker SL modifications only sent on >= $0.20 (`TP1_TRAIL_STEP_DOLLARS`) improvement.
  d) ATR clamp: tiny ATR (0.50) -> $2.00 min; huge ATR (5.00) -> $3.00 max; ATR unavailable (0.0) -> $2.50 midpoint.
  e) Entry very close to TP1 (BUY entry=3849.50, TP1=3850.00) -> trail_stop floored at breakeven + BE_OFFSET_PIPS (3849.70).
  f) Clean break +5p past TP1 (3850.50) no longer locks tight TP1-$0.50 (3849.50) choke when TP1_TRAIL_ENABLED=True;
     hard $3.50 peak giveback ceiling still forces exit.
  g) Pre-TP1 85%/30% near-miss reversal guard behavior remains 100% intact.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import config
import main
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger


def _reset_runtime(tmp_path, name: str = "v342"):
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
    main.CHANNEL_IDS = [-1003712081715]
    state_file = tmp_path / f"{name}_state.json"
    ledger_file = tmp_path / f"{name}_ledger.jsonl"
    main.state_mgr = StateManager(str(state_file))
    main.trade_ledger = TradeLedger(str(ledger_file))
    return state_file, ledger_file


def _seed_4layer_signal(
    msg_id: int = 8401,
    direction: str = "BUY",
    entry_top: float = 3840.0,
    entry_bot: float = 3837.0,
    sl: float = 3825.0,
    tp1: float = 3850.0,
    tp2: float = 3860.0,
    tp3: float = 3870.0,
    tp4: float = 3880.0,
    tp5: float = 3890.0,
    tickets=(8001, 8002, 8003, 8004),
):
    is_buy = direction.upper() == "BUY"
    final_tp = round(tp1 - 0.5, 2) if is_buy else round(tp1 + 0.5, 2)
    l1_px = entry_top if is_buy else entry_bot
    l4_px = entry_bot if is_buy else entry_top
    step = (l4_px - l1_px) / 3.0
    l2_px = round(l1_px + step, 2)
    l3_px = round(l1_px + 2.0 * step, 2)

    for t, lot in zip(tickets, (0.01, 0.01, 0.01, 0.02)):
        main.executor._dry_run_ticket_lots[t] = lot

    return main.state_mgr.create_signal(
        message_id=msg_id,
        symbol="XAUUSD.s",
        direction=direction.upper(),
        zone={"top": entry_top, "mid": round((entry_top + entry_bot) / 2.0, 2), "bottom": entry_bot},
        sl=sl,
        final_tp=final_tp,
        raw_tp1=tp1,
        raw_tp2=tp2,
        raw_tp3=tp3,
        raw_tp4=tp4,
        raw_tp5=tp5,
        tp_levels={"TP1": tp1, "TP2": tp2, "TP3": tp3, "TP4": tp4, "TP5": tp5},
        market_tickets=list(tickets),
        pending_tickets=[],
        layers={
            "L1": {"ticket": tickets[0], "lot": 0.01, "status": "OPEN", "target_price": l1_px, "sl": sl, "tp": final_tp, "tp_stage": "TP1"},
            "L2": {"ticket": tickets[1], "lot": 0.01, "status": "OPEN", "target_price": l2_px, "sl": sl, "tp": final_tp, "tp_stage": "TP1"},
            "L3": {"ticket": tickets[2], "lot": 0.01, "status": "OPEN", "target_price": l3_px, "sl": sl, "tp": final_tp, "tp_stage": "TP1"},
            "L4": {"ticket": tickets[3], "lot": 0.02, "status": "OPEN", "target_price": l4_px, "sl": sl, "tp": final_tp, "tp_stage": "TP1"},
        },
        status="ACTIVE",
        chat_id=-1003712081715,
    )


# ---------------------------------------------------------------------------
# SCENARIOS A & B: BUY & SELL TP1 TOUCH -> WIGGLE HOLDS -> RALLY RATCHETS -> PULLBACK BANKS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ab_buy_and_sell_tp1_trailing_stop_wiggle_holds_rally_ratchets_and_pullback_banks(tmp_path, monkeypatch):
    import mt5_executor as _mt5_mod
    monkeypatch.setattr(config, "STAGE1_ATR_ACTIVATION_PCT", 0.0)
    if hasattr(_mt5_mod, "STAGE1_ATR_ACTIVATION_PCT"):
        monkeypatch.setattr(_mt5_mod, "STAGE1_ATR_ACTIVATION_PCT", 0.0)
    _reset_runtime(tmp_path, "scen_ab")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # M1_ATR_14 = 5.0 / 3.0 (~1.6667) -> 1.5 * ATR = $2.50 trail_dist
    atr_250 = 2.50 / 1.5

    # --- PART 1: BUY (entry=3840, TP1=3850, trail_dist=$2.50) ---
    _seed_4layer_signal(msg_id=8401, direction="BUY", entry_top=3840.0, entry_bot=3837.0, sl=3825.0, tp1=3850.0)

    mod_sl_by_ticket = {}
    orig_mod_pos = main.executor.modify_position_sltp

    def _spy_mod(ticket, sl=None, tp=None):
        if sl is not None:
            mod_sl_by_ticket[int(ticket)] = float(sl)
        return orig_mod_pos(ticket, sl=sl, tp=tp)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_m1_atr", return_value=atr_250), \
         patch.object(main.executor, "modify_position_sltp", side_effect=_spy_mod), \
         patch.object(main.executor, "get_position_close_info", return_value={"profit": 18.0, "reason": "MANUAL"}):

        # a1) Touch TP1 = 3850.00 -> trail_stop = 3847.50 on runner (and any surviving open layers)
        tk_touch = SimpleNamespace(bid=3850.00, ask=3850.30, time=1700100000)
        with patch.object(main.executor, "get_tick", return_value=tk_touch):
            sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8401, sig)

        sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
        assert sig["status"] == "RUNNER_ACTIVE"
        assert sig.get("tp1_touched") is True
        assert sig.get("trail_peak") == pytest.approx(3850.00)
        assert sig.get("trail_dist") == pytest.approx(2.50)
        assert sig.get("trail_stop") == pytest.approx(3847.50)
        assert mod_sl_by_ticket.get(8004) == pytest.approx(3847.50)

        # Keep L2 (#8002) open alongside L4 (#8004) to verify multi-layer trailing SL & atomic close
        main.executor._dry_run_ticket_lots[8002] = 0.01
        sig["market_tickets"] = [8002, 8004]
        sig["layers"]["L2"]["status"] = "OPEN"
        sig["layers"]["L2"]["tp"] = 3860.0
        sig["layers"]["L2"]["tp_stage"] = "TP2"
        main.state_mgr.update_signal(8401, sig, chat_id=-1003712081715)

        # a2) Wiggle down to 3848.70 and 3848.00 (> 3847.50 trail_stop) -> MUST NOT CLOSE!
        for wiggle_bid in (3848.70, 3848.00):
            tk_wig = SimpleNamespace(bid=wiggle_bid, ask=wiggle_bid + 0.30, time=1700100010)
            with patch.object(main.executor, "get_tick", return_value=tk_wig):
                sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
                await main._process_runner_active_tick(8401, sig)
            sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
            assert sig["status"] == "RUNNER_ACTIVE", f"Prematurely closed on wiggle to {wiggle_bid}!"
            assert sig["trail_stop"] == pytest.approx(3847.50)

        # b1) Rally 3850.00 -> 3853.40 -> trail_stop ratchets to 3850.90 across all open layers (L2 & L4)!
        tk_rally = SimpleNamespace(bid=3853.40, ask=3853.70, time=1700100020)
        with patch.object(main.executor, "get_tick", return_value=tk_rally):
            sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
            await main._process_runner_active_tick(8401, sig)

        sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
        assert sig["status"] == "RUNNER_ACTIVE"
        assert sig["trail_peak"] == pytest.approx(3853.40)
        assert sig["trail_stop"] == pytest.approx(3850.90)
        assert mod_sl_by_ticket.get(8004) == pytest.approx(3850.90)
        assert mod_sl_by_ticket.get(8002) == pytest.approx(3850.90)
        assert sig["layers"]["L2"]["tp"] == pytest.approx(3860.0)  # L2 keeps its TP2 target!

        # b2) Dip to 3851.50 (> 3850.90) -> holds!
        tk_dip = SimpleNamespace(bid=3851.50, ask=3851.80, time=1700100025)
        with patch.object(main.executor, "get_tick", return_value=tk_dip):
            sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
            await main._process_runner_active_tick(8401, sig)
        assert main.state_mgr.get_signal(8401, chat_id=-1003712081715)["status"] == "RUNNER_ACTIVE"

        # b3) Drop to 3850.85 (<= 3850.90 trail_stop) -> closes runner + L2 with "TP1 Trailing Stop"!
        tk_hit = SimpleNamespace(bid=3850.85, ask=3851.15, time=1700100030)
        with patch.object(main.executor, "get_tick", return_value=tk_hit):
            sig = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
            await main._process_runner_active_tick(8401, sig)

        sig_closed = main.state_mgr.get_signal(8401, chat_id=-1003712081715)
        assert sig_closed["status"] == "CLOSED"
        assert sig_closed["market_tickets"] == []
        assert "TP1 Trailing Stop" in str(sig_closed.get("runner_close_reason", ""))

    # --- PART 2: SELL MIRROR (entry=3860, TP1=3850, trail_dist=$2.50) ---
    _seed_4layer_signal(
        msg_id=8402,
        direction="SELL",
        entry_top=3863.0,
        entry_bot=3860.0,
        sl=3875.0,
        tp1=3850.0,
        tp2=3840.0,
        tp3=3830.0,
        tp4=3820.0,
        tp5=3810.0,
        tickets=(8101, 8102, 8103, 8104),
    )
    mod_sl_sell = {}

    def _spy_mod_sell(ticket, sl=None, tp=None):
        if sl is not None:
            mod_sl_sell[int(ticket)] = float(sl)
        return orig_mod_pos(ticket, sl=sl, tp=tp)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_m1_atr", return_value=atr_250), \
         patch.object(main.executor, "modify_position_sltp", side_effect=_spy_mod_sell), \
         patch.object(main.executor, "get_position_close_info", return_value={"profit": 18.0, "reason": "MANUAL"}):

        # Touch SELL TP1 = 3850.00 (ask=3850.00) -> trail_stop = 3852.50
        tk_s_touch = SimpleNamespace(bid=3849.70, ask=3850.00, time=1700100100)
        with patch.object(main.executor, "get_tick", return_value=tk_s_touch):
            sig_s = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8402, sig_s)

        sig_s = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
        assert sig_s["status"] == "RUNNER_ACTIVE"
        assert sig_s["trail_stop"] == pytest.approx(3852.50)
        assert mod_sl_sell.get(8104) == pytest.approx(3852.50)

        # Wiggle up to ask=3851.80 (< 3852.50) -> holds!
        tk_s_wig = SimpleNamespace(bid=3851.50, ask=3851.80, time=1700100110)
        with patch.object(main.executor, "get_tick", return_value=tk_s_wig):
            sig_s = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
            await main._process_runner_active_tick(8402, sig_s)
        assert main.state_mgr.get_signal(8402, chat_id=-1003712081715)["status"] == "RUNNER_ACTIVE"

        # Rally down to ask=3846.60 -> trail_stop ratchets down to 3849.10
        tk_s_rally = SimpleNamespace(bid=3846.30, ask=3846.60, time=1700100120)
        with patch.object(main.executor, "get_tick", return_value=tk_s_rally):
            sig_s = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
            await main._process_runner_active_tick(8402, sig_s)
        sig_s = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
        assert sig_s["trail_stop"] == pytest.approx(3849.10)

        # Bounce to ask=3849.15 (>= 3849.10) -> closes with "TP1 Trailing Stop"!
        tk_s_hit = SimpleNamespace(bid=3848.85, ask=3849.15, time=1700100130)
        with patch.object(main.executor, "get_tick", return_value=tk_s_hit):
            sig_s = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
            await main._process_runner_active_tick(8402, sig_s)
        sig_s_closed = main.state_mgr.get_signal(8402, chat_id=-1003712081715)
        assert sig_s_closed["status"] == "CLOSED"
        assert "TP1 Trailing Stop" in str(sig_s_closed.get("runner_close_reason", ""))


# ---------------------------------------------------------------------------
# SCENARIOS C, D, E: FORWARD-ONLY RATCHET ON ATR EXPANSION, STEP GATE, ATR CLAMP, BE FLOOR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cde_forward_ratchet_step_gate_atr_clamp_and_be_floor(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "scen_cde")
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)
    monkeypatch.setattr(config, "TP1_TRAIL_MIN_DOLLARS", 2.00)
    monkeypatch.setattr(config, "TP1_TRAIL_MAX_DOLLARS", 3.00)
    monkeypatch.setattr(config, "TP1_TRAIL_STEP_DOLLARS", 0.20)
    monkeypatch.setattr(config, "BE_OFFSET_PIPS", 2.0)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # --- Scenario D: ATR clamp ($2.00 min, $3.00 max, $2.50 fallback when ATR=0.0) ---
    _seed_4layer_signal(msg_id=8410, direction="BUY", entry_top=3840.0, entry_bot=3837.0, sl=3825.0, tp1=3850.0, tickets=(8201, 8202, 8203, 8204))

    sl_mod_history = []
    orig_mod_pos = main.executor.modify_position_sltp

    def _spy_sl_mod(ticket, sl=None, tp=None):
        if sl is not None and int(ticket) == 8204:
            sl_mod_history.append(round(float(sl), 2))
        return orig_mod_pos(ticket, sl=sl, tp=tp)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "modify_position_sltp", side_effect=_spy_sl_mod):

        # 1) Tiny ATR (0.40 -> 1.5*0.40 = 0.60 -> clamped to MIN $2.00): touch 3850.00 -> trail_stop = 3848.00
        with patch.object(main.executor, "get_m1_atr", return_value=0.40), \
             patch.object(main.executor, "get_tick", return_value=SimpleNamespace(bid=3850.00, ask=3850.30, time=1700200000)):
            sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8410, sig)

        sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
        assert sig["trail_dist"] == pytest.approx(2.00)
        assert sig["trail_stop"] == pytest.approx(3848.00)
        assert sl_mod_history == [3848.00]

        # --- Scenario C1: ATR suddenly expands (5.00 -> clamped to MAX $3.00) at same price 3850.00
        #     raw candidate = 3850.00 - 3.00 = 3847.00, but trail_stop MUST NOT move backward from 3848.00!
        with patch.object(main.executor, "get_m1_atr", return_value=5.00), \
             patch.object(main.executor, "get_tick", return_value=SimpleNamespace(bid=3850.00, ask=3850.30, time=1700200005)):
            sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
            await main._process_runner_active_tick(8410, sig)

        sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
        assert sig["trail_dist"] == pytest.approx(3.00)
        assert sig["trail_stop"] == pytest.approx(3848.00)  # Forward-only ratchet preserved!
        assert sl_mod_history == [3848.00]  # 0 new broker SL calls!

        # --- Scenario C2: Broker SL step gate (TP1_TRAIL_STEP_DOLLARS = 0.20)
        #     With tiny ATR ($2.00 dist), price edges up +$0.10 to 3850.10 -> trail_stop = 3848.10 (+$0.10 < $0.20)
        #     -> virtual trail_stop updates to 3848.10, but NO broker modify_position_sltp spam yet!
        with patch.object(main.executor, "get_m1_atr", return_value=0.40), \
             patch.object(main.executor, "get_tick", return_value=SimpleNamespace(bid=3850.10, ask=3850.40, time=1700200010)):
            sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
            await main._process_runner_active_tick(8410, sig)

        sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
        assert sig["trail_stop"] == pytest.approx(3848.10)
        assert sl_mod_history == [3848.00]  # Still 1 call (< $0.20 step)!

        # Now price advances to 3850.25 (+$0.25 >= $0.20 step) -> broker SL modified to 3848.25!
        with patch.object(main.executor, "get_m1_atr", return_value=0.40), \
             patch.object(main.executor, "get_tick", return_value=SimpleNamespace(bid=3850.25, ask=3850.55, time=1700200015)):
            sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
            await main._process_runner_active_tick(8410, sig)

        sig = main.state_mgr.get_signal(8410, chat_id=-1003712081715)
        assert sig["trail_stop"] == pytest.approx(3848.25)
        assert sl_mod_history == [3848.00, 3848.25]

        # --- Scenario D3: ATR unavailable (0.0) -> defaults to midpoint ($2.00 + $3.00)/2 = $2.50
        _seed_4layer_signal(msg_id=8411, direction="BUY", entry_top=3840.0, entry_bot=3837.0, sl=3825.0, tp1=3850.0, tickets=(8301, 8302, 8303, 8304))
        with patch.object(main.executor, "get_m1_atr", return_value=0.0), \
             patch.object(main.executor, "get_tick", return_value=SimpleNamespace(bid=3850.00, ask=3850.30, time=1700200020)):
            sig11 = main.state_mgr.get_signal(8411, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8411, sig11)
        sig11 = main.state_mgr.get_signal(8411, chat_id=-1003712081715)
        assert sig11["trail_dist"] == pytest.approx(2.50)
        assert sig11["trail_stop"] == pytest.approx(3847.50)

        # --- Scenario E: Entry very close to TP1 (entry=3849.50, TP1=3850.00) -> floored at BE + BE_OFFSET_PIPS (3849.70)
        _seed_4layer_signal(msg_id=8412, direction="BUY", entry_top=3849.50, entry_bot=3848.00, sl=3835.0, tp1=3850.0, tickets=(8401, 8402, 8403, 8404))
        with patch.object(main.executor, "get_m1_atr", return_value=0.0), \
             patch.object(main.executor, "get_tick", return_value=SimpleNamespace(bid=3850.00, ask=3850.30, time=1700200030)):
            sig12 = main.state_mgr.get_signal(8412, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8412, sig12)
        sig12 = main.state_mgr.get_signal(8412, chat_id=-1003712081715)
        # 3850.00 - 2.50 = 3847.50 < primary entry BE (3849.50 + 0.20 = 3849.70) -> floored at 3849.70!
        assert sig12["trail_stop"] == pytest.approx(3849.70)


# ---------------------------------------------------------------------------
# SCENARIOS F & G: NO TP1-$0.50 CHOKE ON +5P CLEAN BREAK, $3.50 CEILING, PRE-TP1 85%/30% INTACT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fg_clean_break_no_tp1_050_choke_hard_ceiling_and_pre_tp1_guard_intact(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "scen_fg")
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    atr_250 = 2.50 / 1.5

    # --- Scenario F: Clean break +5p past TP1 (3850.55) does NOT lock TP1-$0.50 (3849.50)! ---
    _seed_4layer_signal(msg_id=8420, direction="BUY", entry_top=3840.0, entry_bot=3837.0, sl=3825.0, tp1=3850.0, tickets=(8501, 8502, 8503, 8504))
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_m1_atr", return_value=atr_250), \
         patch.object(main.executor, "get_position_close_info", return_value={"profit": 15.0, "reason": "MANUAL"}):

        tk_clean = SimpleNamespace(bid=3850.55, ask=3850.85, time=1700300000)
        with patch.object(main.executor, "get_tick", return_value=tk_clean):
            sig = main.state_mgr.get_signal(8420, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8420, sig)

        sig = main.state_mgr.get_signal(8420, chat_id=-1003712081715)
        assert sig["status"] == "RUNNER_ACTIVE"
        # Trail stop is 3850.55 - 2.50 = 3848.05 (NOT choked at 3849.50!)
        assert sig["trail_stop"] == pytest.approx(3848.05)
        assert sig["runner_sl_floor"] == pytest.approx(3848.05)

        # Pullback to 3849.20 (which is below 3849.50 old lock, but above 3848.05 trail stop) -> MUST HOLD!
        tk_retest = SimpleNamespace(bid=3849.20, ask=3849.50, time=1700300010)
        with patch.object(main.executor, "get_tick", return_value=tk_retest):
            sig = main.state_mgr.get_signal(8420, chat_id=-1003712081715)
            await main._process_runner_active_tick(8420, sig)
        assert main.state_mgr.get_signal(8420, chat_id=-1003712081715)["status"] == "RUNNER_ACTIVE"

        # Hard $3.50 peak giveback ceiling backstop still forces exit if trail is disabled or overridden
        sig_hard = main.state_mgr.get_signal(8420, chat_id=-1003712081715)
        sig_hard["runner_peak_price"] = 3858.00
        sig_hard["peak_profit"] = 21.0
        sig_hard["reversal_guard_peak_profit"] = 21.0
        res_hard = main.executor.evaluate_gold_reversal_guard(
            sig_hard, tick=SimpleNamespace(bid=3854.40, ask=3854.70, time=1700300020)
        )
        assert res_hard["triggered"] is True
        assert res_hard.get("hard_ceiling_triggered") is True

    # --- Scenario G: Pre-TP1 85%/30% near-miss reversal guard behavior unchanged ---
    _seed_4layer_signal(msg_id=8421, direction="BUY", entry_top=3840.0, entry_bot=3837.0, sl=3825.0, tp1=3850.0, tickets=(8601, 8602, 8603, 8604))
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(main.executor, "get_m1_atr", return_value=1.0), \
         patch.object(main.executor, "get_position_close_info", return_value={"profit": 8.0, "reason": "MANUAL"}):

        # Reach 90% of TP1 leg (3840 -> 3849.00, +$9.00 on L1 / +$12.00 on L4) -> arms pre-TP1 guard!
        tk_90 = SimpleNamespace(bid=3849.00, ask=3849.30, time=1700300100)
        with patch.object(main.executor, "get_tick", return_value=tk_90):
            sig21 = main.state_mgr.get_signal(8421, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8421, sig21)

        sig21 = main.state_mgr.get_signal(8421, chat_id=-1003712081715)
        assert sig21["status"] == "ACTIVE"
        assert sig21.get("reversal_guard_armed") is True

        # Drop > 30% of peak profit (3849.00 -> 3845.20 = -$3.80 giveback >= 30% of $12.00 = $3.60) -> banks pre-TP1!
        tk_rev = SimpleNamespace(bid=3845.20, ask=3845.50, time=1700300110)
        with patch.object(main.executor, "get_tick", return_value=tk_rev):
            sig21 = main.state_mgr.get_signal(8421, chat_id=-1003712081715)
            await main._process_pre_tp1_or_backstop_tick(8421, sig21)

        sig21_closed = main.state_mgr.get_signal(8421, chat_id=-1003712081715)
        assert sig21_closed["status"] == "CLOSED"
        assert "Gold Reversal Guard" in str(sig21_closed.get("runner_close_reason", ""))
