"""
v3.5.0 Staged Gold Runner Exit Architecture — Comprehensive 23-Test Verification Suite.

Covers:
  1-2, 8, 13. Stage 0 (pre-TP1) Gold Guard + Shortfall Base vs Stage >= 1 hard isolation
  3-6, 9-12, 14-17. Staged FSM (Stages 1-4), pure structural fsm_floor vs atr_stop tie-breaker,
                    monotonic ratchet on stage handoff, gap candles, proactive TP lift,
                    spread-spike pause, broker SL throttle/immediate stage push/fallback,
                    SELL mirror, and STAGED_EXIT_ENABLED=False rollback
  18-20. Exit telemetry -> trades_ledger.jsonl & Saved Messages, mid-runner restart
         continuity, config.validate_staged_trail_tiers & raw defaults
  21-23. Dynamic effective_max_stage safeguard (4-TP / TP5=None + reconciliation) & manual
         /trail / /sl monotonic ratchet & pure fsm_floor isolation
"""

import importlib.util
import logging
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

import config
import main
import mt5_executor as ex_mod
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger


def _tick(bid: float, ask: Optional[float] = None, spread: float = 0.20) -> SimpleNamespace:
    ask_px = round(ask if ask is not None else (bid + spread), 2)
    return SimpleNamespace(bid=round(float(bid), 2), ask=ask_px, time=1795000000.0)


@pytest.fixture(autouse=True)
def _pin_v350_staged_defaults(monkeypatch):
    """
    Ensure deterministic v3.5.0 defaults for all tests regardless of local `.env` overrides
    (e.g. `.env` overriding RUNNER_MAX_TP_STAGE=4 or BE_OFFSET_PIPS=2.0).
    """
    defaults = {
        "STAGED_EXIT_ENABLED": True,
        "PROFIT_RATCHET_ENABLED": False,
        "TIERED_STAGE_BANKING_ENABLED": False,
        "BE_OFFSET_PIPS": 5.0,
        "BE_AFTER_TP1": True,
        "RUNNER_MAX_TP_STAGE": 5,
        "RUNNER_CLEAN_BREAK_PIPS": 5.0,
        "RUNNER_EARLY_TP_LIFT_PCT": 0.70,
        "RUNNER_PROACTIVE_TP_LIFT": True,
        "RUNNER_DEEPEST_COUNT_FULL": 1,
        "GOLD_REVERSAL_GUARD_ENABLED": True,
        "ARM_THRESHOLD_PCT": 0.85,
        "GIVEBACK_PCT": 0.30,
        "MIN_GOLD_PULLBACK_DOLLARS": 1.50,
        "ATR_NOISE_FLOOR_MULT": 0.35,
        "HARD_GIVEBACK_CEILING_DOLLARS": 3.50,
        "MAX_GIVEBACK_DOLLARS": 4.50,
        "REVERSAL_CONFIRM_TICKS": 2,
        "TP1_TRAIL_ENABLED": True,
        "TP1_TRAIL_ATR_MULT": 1.5,
        "TP1_TRAIL_MIN_DOLLARS": 2.50,
        "TP1_TRAIL_MAX_DOLLARS": 3.50,
        "TP1_TRAIL_STEP_DOLLARS": 0.25,
        "TRAIL_TP2_ATR_MULT": 2.0,
        "TRAIL_TP2_MIN": 3.00,
        "TRAIL_TP2_MAX": 4.50,
        "TRAIL_TP3_ATR_MULT": 2.5,
        "TRAIL_TP3_MIN": 4.00,
        "TRAIL_TP3_MAX": 6.00,
        "TRAIL_TP4_ATR_MULT": 3.0,
        "TRAIL_TP4_MIN": 5.00,
        "TRAIL_TP4_MAX": 8.00,
        "TP1_TOUCH_BANK_ENABLED": True,
        "TP1_TOUCH_BANK_BUFFER_PIPS": 12.0,
        "BANK_ALL_AT_TP1": False,
        "BANK_SINGLE_LAYER_AT_TP1": False,
        "CANCEL_PENDING_ON_TP1": True,
        "MAX_ALLOWED_SPREAD_DOLLARS": 0.40,
        "ADAPTIVE_SPREAD_CEILING_DOLLARS": 0.80,
    }
    for key, val in defaults.items():
        monkeypatch.setattr(config, key, val, raising=False)
        if hasattr(ex_mod, key):
            monkeypatch.setattr(ex_mod, key, val, raising=False)
        if hasattr(main, key):
            monkeypatch.setattr(main, key, val, raising=False)


def _reset_main_runtime(tmp_path: Path, name: str) -> MT5Executor:
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
    main.CHANNEL_IDS = [-1003712081715]
    main.state_mgr = StateManager(tmp_path / f"{name}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{name}_ledger.jsonl")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    main.executor = ex
    return ex


def _make_4layer_sig(
    direction: str = "BUY",
    entry: float = 3890.0,
    sl: float = 3875.0,
    tp_levels: Optional[Dict[str, Optional[float]]] = None,
    tickets: tuple = (1001, 1002, 1003, 1004),
    ex: Optional[MT5Executor] = None,
    chat_id: int = -1003712081715,
) -> Dict[str, Any]:
    is_buy = direction.upper() == "BUY"
    if tp_levels is None:
        if is_buy:
            tp_levels = {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}
        else:
            tp_levels = {"TP1": 3880.0, "TP2": 3870.0, "TP3": 3860.0, "TP4": 3850.0, "TP5": 3840.0}

    tp1 = float(tp_levels["TP1"])  # type: ignore[arg-type]
    final_tp = round(tp1 - 0.50, 2) if is_buy else round(tp1 + 0.50, 2)
    lots = (0.01, 0.01, 0.01, 0.02)

    if ex is not None:
        for t, lot in zip(tickets, lots):
            ex._dry_run_ticket_lots[t] = lot

    layers = {
        f"L{i + 1}": {
            "lot": lots[i],
            "target_price": entry,
            "tp": final_tp,
            "tp_stage": "TP1",
            "ticket": tickets[i],
            "order_type": "MARKET",
            "status": "OPEN",
        }
        for i in range(4)
    }
    sig: Dict[str, Any] = {
        "symbol": "XAUUSD.s",
        "direction": direction.upper(),
        "chat_id": chat_id,
        "sl": sl,
        "layers": layers,
        "market_tickets": list(tickets),
        "pending_tickets": [],
        "final_tp": final_tp,
        "raw_tp1": tp_levels.get("TP1"),
        "raw_tp2": tp_levels.get("TP2"),
        "raw_tp3": tp_levels.get("TP3"),
        "raw_tp4": tp_levels.get("TP4"),
        "raw_tp5": tp_levels.get("TP5"),
        "tp_levels": dict(tp_levels),
        "tp_stage": "TP1",
        "recorded_entry_price": entry,
        "initial_price": entry,
        "status": "ACTIVE",
    }
    return sig


def _seed_sig_in_state(sig: Dict[str, Any], msg_id: int = 9001) -> Dict[str, Any]:
    return main.state_mgr.create_signal(
        message_id=msg_id,
        chat_id=sig.get("chat_id", -1003712081715),
        symbol=sig["symbol"],
        direction=sig["direction"],
        sl=sig["sl"],
        layers=sig["layers"],
        market_tickets=sig["market_tickets"],
        pending_tickets=sig["pending_tickets"],
        final_tp=sig["final_tp"],
        raw_tp1=sig.get("raw_tp1"),
        raw_tp2=sig.get("raw_tp2"),
        raw_tp3=sig.get("raw_tp3"),
        raw_tp4=sig.get("raw_tp4"),
        raw_tp5=sig.get("raw_tp5"),
        tp_levels=sig["tp_levels"],
        recorded_entry_price=sig["recorded_entry_price"],
        initial_price=sig["initial_price"],
        status=sig["status"],
    )


# ---------------------------------------------------------------------------
# 1. Stage 0 Gold Guard triggers pre-TP1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage0_gold_guard_triggers_before_tp1(tmp_path):
    ex = _reset_main_runtime(tmp_path, "t01_stage0_guard")
    ex._dry_run_m1_atr = 1.0  # noise floor = max(1.50, 0.35*1.0) = $1.50
    sig = _seed_sig_in_state(_make_4layer_sig("BUY", entry=3890.0, ex=ex), msg_id=9001)
    assert ex.get_runner_fsm_stage(sig) == 0

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex, "get_position_close_info", return_value={"profit": 6.0, "reason": "MANUAL"}):
        # Reach 88% of TP1 (3898.80 >= 85%): Gold Guard arms
        with patch.object(ex, "get_tick", return_value=_tick(3898.80)):
            await main._process_pre_tp1_or_backstop_tick("9001", main.state_mgr.get_signal(9001))
        s_arm = main.state_mgr.get_signal(9001)
        assert s_arm["reversal_guard_armed"] is True
        assert s_arm["status"] == "ACTIVE"

        # Pullback 31.8% ($2.80 >= 30% and >= $1.50): 2 confirming ticks close ALL 4 layers at Stage 0
        with patch.object(ex, "get_tick", return_value=_tick(3896.00)):
            await main._process_pre_tp1_or_backstop_tick("9001", main.state_mgr.get_signal(9001))
            assert main.state_mgr.get_signal(9001)["status"] == "ACTIVE"
            await main._process_pre_tp1_or_backstop_tick("9001", main.state_mgr.get_signal(9001))

    s_closed = main.state_mgr.get_signal(9001)
    assert s_closed["status"] == "CLOSED"
    assert s_closed["market_tickets"] == []
    assert all(s_closed["layers"][k]["status"] == "CLOSED" for k in ("L1", "L2", "L3", "L4"))
    assert s_closed["exit_reason_code"] == "GOLD_GUARD"
    assert s_closed["exit_stage"] == 0
    assert s_closed["giveback_from_peak"] == pytest.approx(2.80)
    assert s_closed["trail_tier_used"] is None
    assert s_closed["max_favorable_excursion"]["peak_price"] == pytest.approx(3898.80)
    trades = main.trade_ledger.read_trades()
    assert len(trades) == 1 and trades[0]["exit_reason_code"] == "GOLD_GUARD"

    # Also verify ATR noise floor scaling: M1_ATR_14=6.0 -> floor=max(1.50, 0.35*6.0)=$2.10
    ex._dry_run_m1_atr = 6.0
    sig_atr = _make_4layer_sig("BUY", entry=3894.0, tp_levels={"TP1": 3900.0, "TP2": 3910.0}, tickets=(1011, 1012, 1013, 1014))
    res_arm = ex.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3899.40))
    assert res_arm["armed"] is True and res_arm["noise_floor_dollars"] == pytest.approx(2.10)
    assert ex.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3897.60))["triggered"] is False  # $1.80 < $2.10
    assert ex.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3897.20))["triggered"] is False  # tick 1/2
    assert ex.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3897.20))["triggered"] is True   # $2.20 >= $2.10 (2 ticks)


# ---------------------------------------------------------------------------
# 2. Stage >= 1 hard-disables Stage 0 Gold Guard
# ---------------------------------------------------------------------------

def test_stage1_gold_guard_disabled_after_tp1():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.0  # Stage 1 trail_dist = 1.5 * 2.0 = $3.00
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.20)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    assert ex.get_runner_fsm_stage(sig) == 1
    assert sig["reversal_guard_armed"] is False

    # Rally to 3908.00 -> Stage 1 trail_stop = 3905.00
    with patch.object(ex, "get_tick", return_value=_tick(3908.00)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["trail_peak"] == pytest.approx(3908.00)
    assert sig["trail_stop"] == pytest.approx(3905.00)

    # Even huge >= $3.50 giveback OR spread spike NEVER triggers evaluate_gold_reversal_guard post-TP1
    for tk in (_tick(3904.00, spread=0.20), _tick(3903.50, spread=0.90)):
        res = ex.evaluate_gold_reversal_guard(sig, tick=tk)
        assert res["triggered"] is False
        assert res["armed"] is False
        assert res["hard_ceiling_triggered"] is False
        assert res.get("disabled_post_tp1") is True
        assert sig["reversal_guard_armed"] is False

    # Only staged stop exits (holds 3905.50 > 3905.00, exits 3904.90 <= 3905.00)
    with patch.object(ex, "get_tick", return_value=_tick(3905.50)):
        assert ex.manage_designated_runner(sig) is True
    with patch.object(ex, "get_tick", return_value=_tick(3904.90)):
        assert ex.manage_designated_runner(sig) is False
    assert sig["exit_reason_code"] == "ATR_TRAIL"
    assert sig["exit_stage"] == 1


# ---------------------------------------------------------------------------
# 3. Stage handoff monotonic ratchet prevents SL loosening
# ---------------------------------------------------------------------------

def test_stage_handoff_ratchet_prevents_sl_loosening():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.20  # Stage 1: 1.5*2.20=$3.30; Stage 2: 2.0*2.20=$4.40 (wider!)
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.20)):
        ex.handle_tp1_scaling_and_select_runner(sig)

    # Rally to 3909.80 -> trail_stop = 3909.80 - 3.30 = 3906.50
    with patch.object(ex, "get_tick", return_value=_tick(3909.80)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["trail_stop"] == pytest.approx(3906.50)
    broker_sl_before = sig["broker_sl"]
    assert broker_sl_before == pytest.approx(3906.50)

    # Clear TP2 cleanly (3910.60): Stage 1 -> Stage 2 widens trail_dist to $4.40 (raw atr_stop = 3906.20 < 3906.50)
    with patch.object(ex, "get_tick", return_value=_tick(3910.60)):
        assert ex.manage_designated_runner(sig) is True
    assert ex.get_runner_fsm_stage(sig) == 2
    assert sig["trail_dist"] == pytest.approx(4.40)
    assert sig["atr_stop"] == pytest.approx(3906.20)
    assert sig["trail_stop"] == pytest.approx(3906.50)  # Never loosened!
    assert sig["runner_sl_floor"] == pytest.approx(3906.50)
    assert sig["broker_sl"] >= broker_sl_before - 1e-9
    assert sig["layers"]["L4"]["sl"] >= broker_sl_before - 1e-9
    assert sig["active_stop_source"] == "ATR_TRAIL"

    # Sudden ATR expansion inside Stage 2 (5.0 -> $4.50 max): still never loosens
    ex._dry_run_m1_atr = 5.0
    with patch.object(ex, "get_tick", return_value=_tick(3910.60)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["trail_dist"] == pytest.approx(4.50)
    assert sig["trail_stop"] == pytest.approx(3906.50)


# ---------------------------------------------------------------------------
# 4. FSM floor overrides wider ATR trail & binds active_stop_source
# ---------------------------------------------------------------------------

def test_fsm_floor_overrides_wider_atr_trail():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.0  # Stage 1=$3.00, Stage 2=$4.00, Stage 3=$5.00

    # Stage 1: entry 3890 -> fsm_floor = 3890.50; tight TP1=3892.0 -> atr_stop=3889.00 < fsm_floor
    sig1 = _make_4layer_sig("BUY", entry=3890.0, tp_levels={"TP1": 3892.0, "TP2": 3910.0}, ex=ex)
    with patch.object(ex, "get_tick", return_value=_tick(3892.00)):
        ex.handle_tp1_scaling_and_select_runner(sig1)
    assert sig1["fsm_floor"] == pytest.approx(3890.50)
    assert sig1["atr_stop"] == pytest.approx(3889.00)
    assert sig1["trail_stop"] == pytest.approx(3890.50)
    assert sig1["active_stop_source"] == "FSM_FLOOR"

    with patch.object(ex, "get_tick", return_value=_tick(3890.40)):
        assert ex.manage_designated_runner(sig1) is False
    assert sig1["exit_reason_code"] == "FSM_FLOOR"
    assert sig1["exit_stage"] == 1

    # Stage 2: TP1=3900.0, TP2=3902.0 -> break TP2 at 3902.60 -> atr_stop=3898.60 < fsm_floor (3900.0)
    sig2 = _make_4layer_sig(
        "BUY",
        entry=3890.0,
        tp_levels={"TP1": 3900.0, "TP2": 3902.0, "TP3": 3915.0, "TP4": 3925.0, "TP5": 3935.0},
        tickets=(2001, 2002, 2003, 2004),
        ex=ex,
    )
    with patch.object(ex, "get_tick", return_value=_tick(3900.10)):
        ex.handle_tp1_scaling_and_select_runner(sig2)
    with patch.object(ex, "get_tick", return_value=_tick(3902.60)):
        assert ex.manage_designated_runner(sig2) is True
    assert ex.get_runner_fsm_stage(sig2) == 2
    assert sig2["fsm_floor"] == pytest.approx(3900.0)
    assert sig2["trail_stop"] == pytest.approx(3900.0)
    assert sig2["active_stop_source"] == "FSM_FLOOR"

    # Exact tie (peak=3904.00, trail_dist=4.00 -> atr_stop == 3900.0 == fsm_floor): FSM_FLOOR wins!
    with patch.object(ex, "get_tick", return_value=_tick(3904.00)):
        assert ex.manage_designated_runner(sig2) is True
    assert sig2["atr_stop"] == pytest.approx(3900.0)
    assert sig2["active_stop_source"] == "FSM_FLOOR"

    with patch.object(ex, "get_tick", return_value=_tick(3899.90)):
        assert ex.manage_designated_runner(sig2) is False
    assert sig2["exit_reason_code"] == "FSM_FLOOR"
    assert sig2["exit_stage"] == 2

    # Stage 3: TP2=3910.0, TP3=3912.0 -> break TP3 at 3912.60 -> atr_stop=3907.60 < fsm_floor (3910.0)
    sig3 = _make_4layer_sig(
        "BUY",
        entry=3890.0,
        tp_levels={"TP1": 3900.0, "TP2": 3910.0, "TP3": 3912.0, "TP4": 3925.0, "TP5": 3935.0},
        tickets=(3001, 3002, 3003, 3004),
        ex=ex,
    )
    with patch.object(ex, "get_tick", return_value=_tick(3900.10)):
        ex.handle_tp1_scaling_and_select_runner(sig3)
    with patch.object(ex, "get_tick", return_value=_tick(3912.60)):
        assert ex.manage_designated_runner(sig3) is True
    assert ex.get_runner_fsm_stage(sig3) == 3
    assert sig3["fsm_floor"] == pytest.approx(3910.0)
    assert sig3["trail_stop"] == pytest.approx(3910.0)
    assert sig3["active_stop_source"] == "FSM_FLOOR"
    with patch.object(ex, "get_tick", return_value=_tick(3909.90)):
        assert ex.manage_designated_runner(sig3) is False
    assert sig3["exit_reason_code"] == "FSM_FLOOR"
    assert sig3["exit_stage"] == 3


# ---------------------------------------------------------------------------
# 5. ATR trail overrides FSM floor near peak
# ---------------------------------------------------------------------------

def test_atr_trail_overrides_fsm_floor_near_peak():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 1.5  # Stage 1=$2.50, Stage 2=$3.00
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.20)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    with patch.object(ex, "get_tick", return_value=_tick(3910.60)):
        assert ex.manage_designated_runner(sig) is True
    with patch.object(ex, "get_tick", return_value=_tick(3919.00)):
        assert ex.manage_designated_runner(sig) is True

    assert ex.get_runner_fsm_stage(sig) == 2
    assert sig["fsm_floor"] == pytest.approx(3900.0)  # Pure structural floor unpolluted!
    assert sig["atr_stop"] == pytest.approx(3916.00)
    assert sig["trail_stop"] == pytest.approx(3916.00)
    assert sig["active_stop_source"] == "ATR_TRAIL"

    with patch.object(ex, "get_tick", return_value=_tick(3915.90)):
        assert ex.manage_designated_runner(sig) is False
    assert sig["status"] == "CLOSED"
    assert sig["exit_reason_code"] == "ATR_TRAIL"
    assert sig["exit_stage"] == 2
    assert sig["giveback_from_peak"] == pytest.approx(3.10)


# ---------------------------------------------------------------------------
# 6. Multi-TP gap candle sequential transition (Stage 1 -> 2 -> 3)
# ---------------------------------------------------------------------------

def test_multi_tp_gap_candle_sequential_transition():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.0  # Stage 3 tier (2.5, 4.0, 6.0) -> 2.5*2.0 = $5.00
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.10)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    assert ex.get_runner_fsm_stage(sig) == 1
    assert sig["runner_cleared_milestones"] == ["TP1"]
    assert sig["runner_milestone_idx"] == 2

    mod_calls: List[tuple] = []
    orig_mod = ex.modify_position_sltp

    def _spy_mod(ticket, sl=None, tp=None):
        mod_calls.append((ticket, sl, tp))
        return orig_mod(ticket, sl=sl, tp=tp)

    # Single gap tick jumps past TP1(3900.5), TP2(3910.5), TP3(3920.5) to 3921.00
    with patch.object(ex, "modify_position_sltp", side_effect=_spy_mod), \
         patch.object(ex, "get_tick", return_value=_tick(3921.00)):
        assert ex.manage_designated_runner(sig) is True

    assert sig["runner_cleared_milestones"] == ["TP1", "TP2", "TP3"]
    assert sig["runner_milestone_idx"] == 4
    assert ex.get_runner_fsm_stage(sig) == 3
    assert sig["fsm_floor"] == pytest.approx(3910.0)  # TP2!
    assert sig["trail_tier_used"]["stage"] == 3
    assert (
        sig["trail_tier_used"]["atr_mult"],
        sig["trail_tier_used"]["min_dollars"],
        sig["trail_tier_used"]["max_dollars"],
    ) == (2.5, 4.0, 6.0)
    assert sig["trail_dist"] == pytest.approx(5.0)
    assert sig["trail_stop"] == pytest.approx(3916.00)
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3930.0)
    assert any(t == 1004 and sl == pytest.approx(3916.00) and tp == pytest.approx(3930.0) for t, sl, tp in mod_calls)


# ---------------------------------------------------------------------------
# 7. Stage 4 / TP4 hard ceiling closes ALL remaining layers (TP4 mode)
# ---------------------------------------------------------------------------

def test_stage4_tp4_hard_ceiling_closes_all(monkeypatch):
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
        ex.handle_tp1_scaling_and_select_runner(sig)

    # Simulate an extra non-runner layer (L3) also still open alongside runner L4
    ex._dry_run_ticket_lots[1003] = 0.01
    sig["layers"]["L3"]["status"] = "OPEN"
    sig["market_tickets"] = [1003, 1004]

    for px in (3910.60, 3920.60):
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig) is True

    with patch.object(ex, "get_tick", return_value=_tick(3930.10)):
        assert ex.manage_designated_runner(sig) is False

    assert sig["status"] == "CLOSED"
    assert sig["market_tickets"] == []
    assert sig["layers"]["L3"]["status"] == "CLOSED"
    assert sig["layers"]["L4"]["status"] == "CLOSED"
    assert sig["tp4_reached"] is True
    assert sig["exit_reason_code"] == "TP_CEILING"
    assert sig["giveback_from_peak"] == 0.0


# ---------------------------------------------------------------------------
# 8. Stage 0 guard stays OFF mid-range (~50% progress, >= $3.50 wiggle)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage0_guard_stays_off_midrange(tmp_path):
    ex = _reset_main_runtime(tmp_path, "t08_midrange")
    sig = _seed_sig_in_state(_make_4layer_sig("BUY", entry=3890.0, ex=ex), msg_id=9008)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        for px in (3895.00, 3891.00):  # 50% progress -> $4.00 (> $3.50) wiggle
            with patch.object(ex, "get_tick", return_value=_tick(px)):
                await main._process_pre_tp1_or_backstop_tick("9008", main.state_mgr.get_signal(9008))

    s_after = main.state_mgr.get_signal(9008)
    assert s_after["reversal_guard_armed"] is False
    assert s_after["shortfall_armed"] is False
    assert s_after["status"] == "ACTIVE"
    assert len(s_after["market_tickets"]) == 4


# ---------------------------------------------------------------------------
# 9. Broker TP lifted proactively ahead of FSM stage at 70% of leg (Stages 1-4)
# ---------------------------------------------------------------------------

def test_broker_tp_lifted_ahead_of_fsm_stage():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    # Stage 1 (k=2, TP1 cleared): broker TP starts at TP2=3910.0
    assert ex.get_runner_fsm_stage(sig) == 1
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3910.0)

    # 70% of TP1(3900)->TP2(3910) = 3907.00 -> lifts broker TP proactively to TP3=3920.0!
    with patch.object(ex, "get_tick", return_value=_tick(3907.00)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3920.0)
    assert sig["layers"]["L4"]["tp_stage"] == "TP3"

    # Stage 2 (TP2 cleared at 3910.60) -> 70% of TP2->TP3 (3917.00) lifts broker TP to TP4=3930.0
    with patch.object(ex, "get_tick", return_value=_tick(3910.60)):
        assert ex.manage_designated_runner(sig) is True
    assert ex.get_runner_fsm_stage(sig) == 2
    with patch.object(ex, "get_tick", return_value=_tick(3917.00)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3930.0)
    assert sig["layers"]["L4"]["tp_stage"] == "TP4"

    # Stage 3 (TP3 cleared at 3920.60) -> 70% of TP3->TP4 (3927.00) lifts broker TP to TP5=3940.0
    with patch.object(ex, "get_tick", return_value=_tick(3920.60)):
        assert ex.manage_designated_runner(sig) is True
    assert ex.get_runner_fsm_stage(sig) == 3
    with patch.object(ex, "get_tick", return_value=_tick(3927.00)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3940.0)
    assert sig["layers"]["L4"]["tp_stage"] == "TP5"

    # Stage 4 (TP4 cleared at 3930.60) -> 70% of TP4->TP5 (3937.00) keeps broker TP capped at TP5=3940.0
    with patch.object(ex, "get_tick", return_value=_tick(3930.60)):
        assert ex.manage_designated_runner(sig) is True
    assert ex.get_runner_fsm_stage(sig) == 4
    with patch.object(ex, "get_tick", return_value=_tick(3937.00)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3940.0)
    assert sig["layers"]["L4"]["tp_stage"] == "TP5"


# ---------------------------------------------------------------------------
# 10. Stage 1 trigger is TP1 scaling event (even before clean break +5p)
# ---------------------------------------------------------------------------

def test_stage1_trigger_is_tp1_scaling_event():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 1.80  # Stage 1: 1.5 * 1.80 = $2.70
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    # Trigger scaling at 3899.20 (BEFORE TP1=3900.0 and clean break=3900.50)
    with patch.object(ex, "get_tick", return_value=_tick(3899.20)):
        rt = ex.handle_tp1_scaling_and_select_runner(sig)

    assert rt == 1004
    assert sig["status"] == "RUNNER_ACTIVE"
    assert sig["tp1_reached"] is True
    assert sig["runner_cleared_milestones"] == ["TP1"]
    assert sig["runner_milestone_idx"] == 2
    assert ex.get_runner_fsm_stage(sig) == 1
    assert sig["fsm_floor"] == pytest.approx(3890.50)  # entry + 5p ($0.50)
    assert sig["stage1_atr_armed"] is False
    assert sig["trail_stop"] == pytest.approx(3890.50)
    assert sig["broker_sl"] == pytest.approx(3890.50)
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3910.0)


# ---------------------------------------------------------------------------
# 11. TP5 mode uses Stage 4 tier (3.0, 5.0, 8.0) and closes at TP5 ceiling
# ---------------------------------------------------------------------------

def test_tp5_mode_uses_stage4_tier_and_ceiling_at_tp5():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.00  # Stage 4: 3.0 * 2.00 = $6.00 (within [5.0, 8.0])
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    for px in (3910.60, 3920.60, 3930.60):
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig) is True

    assert ex.get_runner_fsm_stage(sig) == 4
    assert sig["fsm_floor"] == pytest.approx(3920.0)  # TP3!
    assert sig["trail_tier_used"]["stage"] == 4
    assert (
        sig["trail_tier_used"]["atr_mult"],
        sig["trail_tier_used"]["min_dollars"],
        sig["trail_tier_used"]["max_dollars"],
    ) == (3.0, 5.0, 8.0)
    assert sig["trail_dist"] == pytest.approx(6.00)
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3940.0)  # TP5!

    # Reach TP5 ceiling (3940.0)
    with patch.object(ex, "get_tick", return_value=_tick(3940.00)):
        assert ex.manage_designated_runner(sig) is False
    assert sig["tp5_reached"] is True
    assert sig["status"] == "CLOSED"
    assert sig["market_tickets"] == []
    assert sig["exit_reason_code"] == "TP_CEILING"
    assert sig["exit_stage"] == 4
    assert sig["giveback_from_peak"] == 0.0


# ---------------------------------------------------------------------------
# 12. Legacy post-TP1 exits stay disabled under STAGED_EXIT_ENABLED=True
# ---------------------------------------------------------------------------

def test_legacy_post_tp1_exits_disabled(monkeypatch):
    monkeypatch.setattr(config, "RUNNER_CONTINUOUS_TRAIL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TP2_SHORTFALL_ARM_PIPS", 20.0, raising=False)
    monkeypatch.setattr(config, "TP2_SHORTFALL_PULLBACK_PIPS", 8.0, raising=False)
    monkeypatch.setattr(config, "RUNNER_PROXIMITY_BUFFER_PIPS", 10.0, raising=False)
    monkeypatch.setattr(config, "RUNNER_REVERSAL_PULLBACK_PIPS", 12.0, raising=False)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.0  # Stage 1 trail=$3.00
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.00)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    assert sig["trail_stop"] == pytest.approx(3890.50)

    # 1) Old TP1-touch fallback bank (< TP1 - $1.20 = 3898.80) must NOT fire!
    with patch.object(ex, "get_tick", return_value=_tick(3898.50)):
        assert ex.manage_designated_runner(sig) is True

    # 2) Old TP2 shortfall (3902.0 -> 3901.0 for 4 ticks) must NOT fire!
    for px in (3902.00, 3901.00, 3901.00, 3901.00, 3901.00):
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig) is True
    assert sig.get("tp2_shortfall_armed") is False

    # 3) Old FSM 12p proximity guard near TP2 (3909.50 -> 3908.00 = 15p pullback) must NOT fire!
    #    (Stage 1 trail_stop = 3909.50 - 3.00 = 3906.50, so 3908.00 holds!)
    for px in (3909.50, 3908.00, 3908.00, 3908.00):
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig) is True
    assert sig.get("runner_proximity_armed") is False

    # 4) Old post-TP1 Gold Guard $3.50 hard ceiling in Stage 2 (trail_dist=$4.50) must NOT fire!
    ex._dry_run_m1_atr = 2.25  # Stage 2: 2.0 * 2.25 = $4.50
    for px in (3910.60, 3918.00, 3914.20):  # -$3.80 pullback >= $3.50 old cap, < $4.50 staged trail
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig) is True
    assert ex.evaluate_gold_reversal_guard(sig, tick=_tick(3914.20))["triggered"] is False
    assert sig["status"] == "RUNNER_ACTIVE"


# ---------------------------------------------------------------------------
# 13. Pre-TP1 Shortfall Guard closes ONLY base layers L1-L3, sparing L4
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shortfall_guard_closes_base_layers_only_pre_tp1(tmp_path):
    ex = _reset_main_runtime(tmp_path, "t13_shortfall")
    sig = _seed_sig_in_state(_make_4layer_sig("BUY", entry=3890.0, ex=ex), msg_id=9013)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex, "get_position_close_info", return_value={"profit": 7.0, "reason": "MANUAL"}):
        # Peak 3899.50 (peak_profit=$9.50 -> L4 30% Gold Guard=$2.85), then $2.10 pullback for 3 ticks
        for px in (3899.50, 3897.40, 3897.40, 3897.40):
            with patch.object(ex, "get_tick", return_value=_tick(px)):
                await main._process_pre_tp1_or_backstop_tick("9013", main.state_mgr.get_signal(9013))

    s_after = main.state_mgr.get_signal(9013)
    assert s_after["status"] == "BE_APPLIED"
    assert s_after["market_tickets"] == [1004]
    assert all(s_after["layers"][k]["status"] == "CLOSED" for k in ("L1", "L2", "L3"))
    assert s_after["layers"]["L4"]["status"] == "OPEN"
    assert s_after["layers"]["L4"]["be_sl"] == pytest.approx(3890.50)
    assert main.trade_ledger.read_trades() == []

    # When no runner survives (base-only shortfall closes all), telemetry logs SHORTFALL_BASE / stage 0
    sig_base = _seed_sig_in_state(
        _make_4layer_sig("BUY", entry=3890.0, tickets=(2001, 2002, 2003, 2004), ex=ex),
        msg_id=9014,
    )
    sig_base["market_tickets"] = [2001, 2002]
    sig_base["layers"]["L3"]["status"] = "CANCELLED"
    sig_base["layers"]["L4"]["status"] = "CANCELLED"
    main.state_mgr.update_signal(
        9014,
        {"market_tickets": [2001, 2002], "layers": sig_base["layers"]},
        chat_id=sig_base["chat_id"],
    )
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(config, "GOLD_REVERSAL_GUARD_ENABLED", False), \
         patch.object(main, "GOLD_REVERSAL_GUARD_ENABLED", False):
        # Set runner_tickets_pre empty via multi-base without gold_guard override
        for px in (3899.50, 3897.40, 3897.40):
            with patch.object(ex, "get_tick", return_value=_tick(px)):
                await main._process_pre_tp1_or_backstop_tick("9014", main.state_mgr.get_signal(9014))
    s_base_closed = main.state_mgr.get_signal(9014)
    assert s_base_closed["status"] == "CLOSED"
    assert s_base_closed["exit_reason_code"] == "SHORTFALL_BASE"
    assert s_base_closed["exit_stage"] == 0


# ---------------------------------------------------------------------------
# 14. Spread spike pauses internal trail exit while broker SL >= fsm_floor
# ---------------------------------------------------------------------------

def test_spread_spike_pauses_internal_trail_exit():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 1.5  # Stage 2: 2.0 * 1.5 = $3.00
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    for px in (3910.60, 3916.00):
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig) is True

    assert sig["trail_stop"] == pytest.approx(3913.00)
    assert sig["fsm_floor"] == pytest.approx(3900.00)
    assert sig["broker_sl"] >= sig["fsm_floor"]

    # Spread spike ($1.10 > $0.80 adaptive ceiling) with bid 3911.50 <= trail_stop (3913.00) -> PAUSED!
    with patch.object(ex, "get_tick", return_value=_tick(3911.50, ask=3912.60)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["spread_spike_paused"] is True
    assert sig["status"] == "RUNNER_ACTIVE"
    assert sig["broker_sl"] >= sig["fsm_floor"]

    # Spread normalizes while still <= trail_stop -> exits cleanly!
    with patch.object(ex, "get_tick", return_value=_tick(3912.50, spread=0.20)):
        assert ex.manage_designated_runner(sig) is False
    assert sig["status"] == "CLOSED"
    assert sig["exit_reason_code"] == "ATR_TRAIL"


# ---------------------------------------------------------------------------
# 15. Broker SL throttle ($0.25), stage-change immediate push, & rejection fallback
# ---------------------------------------------------------------------------

def test_broker_sl_throttle_and_floor_push_on_stage_change():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.0 / 1.5  # Stage 1 raw=2.0 -> clamped to $2.50
    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)

    sl_pushes: List[tuple] = []
    orig_mod = ex.modify_position_sltp

    def _spy_mod(ticket, sl=None, tp=None):
        if sl is not None:
            sl_pushes.append((ticket, round(float(sl), 2), tp))
        return orig_mod(ticket, sl=sl, tp=tp)

    with patch.object(ex, "modify_position_sltp", side_effect=_spy_mod):
        with patch.object(ex, "get_tick", return_value=_tick(3900.20)):
            ex.handle_tp1_scaling_and_select_runner(sig)
        # Initial Stage 1 holds fsm_floor (3890.50) until >= 45% progress on TP1->TP2
        assert sl_pushes[-1][1] == pytest.approx(3890.50)

        # Rally to 3909.00 -> trail_stop = 3906.50, pushed to broker
        with patch.object(ex, "get_tick", return_value=_tick(3909.00)):
            assert ex.manage_designated_runner(sig) is True
        assert sig["broker_sl"] == pytest.approx(3906.50)
        count_before_throttle = len(sl_pushes)

        # Sub-$0.25 move (+$0.15 to 3909.15 -> trail_stop=3906.65): throttled on broker!
        with patch.object(ex, "get_tick", return_value=_tick(3909.15)):
            assert ex.manage_designated_runner(sig) is True
        assert sig["trail_stop"] == pytest.approx(3906.65)
        assert len(sl_pushes) == count_before_throttle
        assert sig["broker_sl"] == pytest.approx(3906.50)

        # >= $0.25 move from broker_sl (3909.30 -> trail_stop=3906.80, +$0.30 >= $0.25): pushes!
        with patch.object(ex, "get_tick", return_value=_tick(3909.30)):
            assert ex.manage_designated_runner(sig) is True
        assert sig["broker_sl"] == pytest.approx(3906.80)
        assert len(sl_pushes) == count_before_throttle + 1

        # Stage transition (Stage 1 -> Stage 2 at 3910.90 with ATR=2.0 -> Stage 2 dist=$4.00 -> stop=3906.90):
        # Improvement over broker_sl (3906.80) is only $0.10 (< $0.25), yet stage change forces immediate push!
        ex._dry_run_m1_atr = 2.0
        count_before_stage = len(sl_pushes)
        with patch.object(ex, "get_tick", return_value=_tick(3910.90)):
            assert ex.manage_designated_runner(sig) is True
        assert ex.get_runner_fsm_stage(sig) == 2
        assert len(sl_pushes) == count_before_stage + 1
        assert sl_pushes[-1][1] == pytest.approx(3906.90)
        assert sig["broker_sl"] == pytest.approx(3906.90)

    # Broker SL rejection fallback: reject desired_broker_sl (3913.00), accept fallback_sl (fsm_floor=3900.0 -> wait:
    # ensure fallback > cur_sl by using Stage 3 where fsm_floor=TP2=3910.0 > cur_sl=3906.90!)
    with patch.object(ex, "get_tick", return_value=_tick(3920.60)):
        assert ex.manage_designated_runner(sig) is True
    assert ex.get_runner_fsm_stage(sig) == 3
    assert sig["fsm_floor"] == pytest.approx(3910.0)
    # At 3920.60 with ATR=2.0 (Stage 3 dist=5.0), trail_stop=3915.60, broker_sl=3915.60 -> let's test rejection
    # starting from broker_sl=3906.90 by running Stage 2->3 with rejection on > 3911.0
    sig_rej = _make_4layer_sig("BUY", entry=3890.0, tickets=(4001, 4002, 4003, 4004), ex=ex)
    ex._dry_run_m1_atr = 2.0
    with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
        ex.handle_tp1_scaling_and_select_runner(sig_rej)
    with patch.object(ex, "get_tick", return_value=_tick(3910.60)):
        assert ex.manage_designated_runner(sig_rej) is True
    assert sig_rej["broker_sl"] == pytest.approx(3906.60)

    rejected_calls: List[tuple] = []

    def _reject_tight_sl(ticket, sl=None, tp=None):
        rejected_calls.append((ticket, sl, tp))
        if sl is not None and float(sl) > 3911.0:
            return False  # Simulate broker 10016 rejecting tight SL
        return orig_mod(ticket, sl=sl, tp=tp)

    with patch.object(ex, "modify_position_sltp", side_effect=_reject_tight_sl), \
         patch.object(ex, "get_tick", return_value=_tick(3920.60)):
        assert ex.manage_designated_runner(sig_rej) is True

    # Attempted desired SL (3915.60), fell back to fsm_floor (3910.0), and kept internal trail_stop=3915.60!
    assert any(sl == pytest.approx(3915.60) for _, sl, _ in rejected_calls if sl is not None)
    assert any(sl == pytest.approx(3910.00) for _, sl, _ in rejected_calls if sl is not None)
    assert sig_rej["broker_sl"] == pytest.approx(3910.00)
    assert sig_rej["trail_stop"] == pytest.approx(3915.60)

    # Even if BOTH desired SL and fallback SL fail on broker, broker_sl retains 3910.00 while trail_stop still exits internally
    with patch.object(ex, "modify_position_sltp", return_value=False), \
         patch.object(ex, "get_tick", return_value=_tick(3922.00)):
        assert ex.manage_designated_runner(sig_rej) is True
    assert sig_rej["broker_sl"] == pytest.approx(3910.00)
    assert sig_rej["trail_stop"] == pytest.approx(3917.00)
    with patch.object(ex, "get_tick", return_value=_tick(3916.90)):
        assert ex.manage_designated_runner(sig_rej) is False
    assert sig_rej["exit_reason_code"] == "ATR_TRAIL"


# ---------------------------------------------------------------------------
# 16. SELL-side full lifecycle mirror (ask, min, BE - $0.50, MFE/giveback)
# ---------------------------------------------------------------------------

def test_sell_side_mirror():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 2.0  # Stage 1=$3.00, Stage 2=$4.00, Stage 3=$5.00, Stage 4=$6.00
    sell_tps = {"TP1": 3900.0, "TP2": 3890.0, "TP3": 3880.0, "TP4": 3870.0, "TP5": 3860.0}
    sig = _make_4layer_sig("SELL", entry=3910.0, sl=3925.0, tp_levels=sell_tps, ex=ex)

    # Stage 0 mid-range stays OFF
    assert ex.evaluate_gold_reversal_guard(sig, tick=_tick(3904.80, ask=3905.00))["armed"] is False
    assert ex.evaluate_gold_reversal_guard(sig, tick=_tick(3908.40, ask=3908.60))["triggered"] is False

    # TP1 scaling triggered at ask=3900.00 (SELL uses ask!)
    with patch.object(ex, "get_tick", return_value=_tick(3899.80, ask=3900.00)):
        ex.handle_tp1_scaling_and_select_runner(sig)
    assert ex.get_runner_fsm_stage(sig) == 1
    assert sig["fsm_floor"] == pytest.approx(3909.50)  # 3910.0 - $0.50
    assert sig["trail_peak"] == pytest.approx(3900.00)
    assert sig["trail_stop"] == pytest.approx(3909.50)  # holds fsm_floor until >= 45% on TP1->TP2

    # Sequential clean breaks down through TP1 -> TP2 -> TP3 -> TP4 -> TP5 ceiling
    for ask_px, exp_stage, exp_floor in (
        (3899.40, 1, 3909.50),
        (3889.40, 2, 3900.00),
        (3879.40, 3, 3890.00),
        (3869.40, 4, 3880.00),
    ):
        prev_ts = sig["trail_stop"]
        with patch.object(ex, "get_tick", return_value=_tick(ask_px - 0.20, ask=ask_px)):
            assert ex.manage_designated_runner(sig) is True
        assert ex.get_runner_fsm_stage(sig) == exp_stage
        assert sig["fsm_floor"] == pytest.approx(exp_floor)
        assert sig["trail_stop"] <= prev_ts + 1e-9  # Monotonic downward ratchet for SELL
        assert sig["trail_peak"] == pytest.approx(ask_px)

    assert sig["trail_tier_used"]["stage"] == 4
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3860.0)  # TP5!

    with patch.object(ex, "get_tick", return_value=_tick(3859.80, ask=3860.00)):
        assert ex.manage_designated_runner(sig) is False
    assert sig["tp5_reached"] is True
    assert sig["exit_reason_code"] == "TP_CEILING"
    assert sig["giveback_from_peak"] == 0.0
    assert sig["max_favorable_excursion"]["peak_price"] == pytest.approx(3860.0)
    assert sig["max_favorable_excursion"]["peak_profit_per_oz"] == pytest.approx(50.0)

    # Also verify SELL pullback ATR_TRAIL exit sign & giveback calculation
    sig_pb = _make_4layer_sig(
        "SELL", entry=3910.0, sl=3925.0, tp_levels=sell_tps, tickets=(2001, 2002, 2003, 2004), ex=ex
    )
    with patch.object(ex, "get_tick", return_value=_tick(3899.80, ask=3900.00)):
        ex.handle_tp1_scaling_and_select_runner(sig_pb)
    with patch.object(ex, "get_tick", return_value=_tick(3895.80, ask=3896.00)):
        assert ex.manage_designated_runner(sig_pb) is True
    assert sig_pb["trail_peak"] == pytest.approx(3896.00)
    assert sig_pb["trail_stop"] == pytest.approx(3899.00)
    with patch.object(ex, "get_tick", return_value=_tick(3899.00, ask=3899.20)):
        assert ex.manage_designated_runner(sig_pb) is False
    assert sig_pb["exit_reason_code"] == "ATR_TRAIL"
    assert sig_pb["giveback_from_peak"] == pytest.approx(3.20)
    assert sig_pb["max_favorable_excursion"]["peak_profit_per_oz"] == pytest.approx(14.0)


# ---------------------------------------------------------------------------
# 17. STAGED_EXIT_ENABLED=False restores exact legacy v3.4.2 / v3.4.1 behavior
# ---------------------------------------------------------------------------

def test_staged_exit_disabled_restores_legacy_behavior(monkeypatch):
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)
    ex = MT5Executor(dry_run=True)
    ex.connect()

    # 1) Legacy post-TP1 Gold Reversal Guard $3.50 hard giveback ceiling fires after TP1
    sig_ceil = _make_4layer_sig("BUY", entry=3890.0, ex=ex)
    sig_ceil.update({
        "status": "RUNNER_ACTIVE",
        "tp1_reached": True,
        "tp1_touched": True,
        "reversal_guard_armed": True,
        "runner_peak_price": 3915.0,
        "peak_profit": 25.0,
        "reversal_guard_peak_profit": 25.0,
        "reversal_guard_runner_entry": 3890.0,
        "runner_tickets": [1004],
        "runner_layers": ["L4"],
    })
    res_ceil = ex.evaluate_gold_reversal_guard(sig_ceil, tick=_tick(3911.40, spread=0.70))
    assert res_ceil["triggered"] is True
    assert res_ceil.get("hard_ceiling_triggered") is True

    # 2) Legacy TP1-Touch Fallback Bank works when TP1_TRAIL_ENABLED=False
    monkeypatch.setattr(config, "TP1_TRAIL_ENABLED", False)
    sig_touch = _make_4layer_sig("BUY", entry=3890.0, tickets=(2001, 2002, 2003, 2004), ex=ex)
    with patch.object(ex, "get_tick", return_value=_tick(3900.00)):
        ex.handle_tp1_scaling_and_select_runner(sig_touch)
    assert sig_touch["tp1_touched"] is True
    assert sig_touch["tp1_touch_floor"] == pytest.approx(3898.80)  # TP1 - $1.20
    with patch.object(ex, "get_tick", return_value=_tick(3898.70)):
        assert ex.manage_designated_runner(sig_touch) is False
    assert "TP1-Touch Fallback Bank" in sig_touch["runner_close_reason"]


# ---------------------------------------------------------------------------
# 18. Full E2E exit telemetry written to trades_ledger.jsonl & Saved Messages alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exit_telemetry_written_to_ledger(tmp_path):
    ex = _reset_main_runtime(tmp_path, "t18_ledger")
    ex._dry_run_m1_atr = 1.5  # Stage 1=$2.50, Stage 2=$3.00
    sig = _seed_sig_in_state(_make_4layer_sig("BUY", entry=3890.0, ex=ex), msg_id=9018)

    alerts: List[str] = []

    async def _cap_alert(txt: str):
        alerts.append(txt)

    def _close_info(ticket: int) -> Dict[str, Any]:
        return {"profit": 42.50 if int(ticket) == 1004 else 10.00, "reason": "MANUAL"}

    with patch.object(main, "notify_saved_messages", side_effect=_cap_alert), \
         patch.object(ex, "get_position_close_info", side_effect=_close_info):
        # Hit TP1 -> Stage 1 -> Stage 2 -> Peak 3918.00 -> Pullback 3914.90 <= 3915.00
        with patch.object(ex, "get_tick", return_value=_tick(3900.20)):
            await main._process_pre_tp1_or_backstop_tick("9018", main.state_mgr.get_signal(9018))
        for px in (3910.60, 3918.00, 3914.90):
            with patch.object(ex, "get_tick", return_value=_tick(px)):
                await main._process_runner_active_tick("9018", main.state_mgr.get_signal(9018))

    trades = main.trade_ledger.read_trades()
    assert len(trades) == 1
    rec = trades[0]
    assert rec["exit_reason"] == "ATR_TRAIL"
    assert rec["exit_reason_code"] == "ATR_TRAIL"
    assert "Staged ATR Trail" in rec["exit_reason_detail"]
    assert rec["exit_stage"] == 2
    assert rec["exit_price"] == pytest.approx(3914.90)
    assert rec["max_favorable_excursion"]["peak_price"] == pytest.approx(3918.00)
    assert rec["max_favorable_excursion"]["peak_profit_per_oz"] == pytest.approx(28.00)
    assert rec["giveback_from_peak"] == pytest.approx(3.10)
    assert rec["trail_tier_used"]["stage"] == 2
    assert rec["realized_pnl"] == pytest.approx(72.50)

    runner_alerts = [a for a in alerts if "Runner Trade Closed" in a]
    assert len(runner_alerts) == 1
    alert_txt = runner_alerts[0]
    assert "Stage 2" in alert_txt
    assert "ATR_TRAIL" in alert_txt
    assert "3918.0" in alert_txt
    assert "$3.10" in alert_txt


# ---------------------------------------------------------------------------
# 19. Restart mid-runner resumes exact stage, peak, fsm_floor, and trail_stop
# ---------------------------------------------------------------------------

def test_restart_mid_runner_resumes_stage_and_stop(tmp_path):
    state_path = tmp_path / "restart_state.json"
    sm1 = StateManager(state_path)
    ex1 = MT5Executor(dry_run=True)
    ex1.connect()
    ex1._dry_run_m1_atr = 1.8  # Stage 3: 2.5*1.8=$4.50

    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex1)
    with patch.object(ex1, "get_tick", return_value=_tick(3900.60)):
        ex1.handle_tp1_scaling_and_select_runner(sig)
    for px in (3910.60, 3920.60, 3926.00):
        with patch.object(ex1, "get_tick", return_value=_tick(px)):
            assert ex1.manage_designated_runner(sig) is True

    assert ex1.get_runner_fsm_stage(sig) == 3
    assert sig["trail_peak"] == pytest.approx(3926.00)
    assert sig["fsm_floor"] == pytest.approx(3910.00)
    assert sig["trail_stop"] == pytest.approx(3921.50)

    sm1.create_signal(
        message_id=9019,
        chat_id=-1003712081715,
        **{k: v for k, v in sig.items() if k != "chat_id"},
    )

    # Simulate process restart: fresh StateManager + fresh MT5Executor + wider ATR (2.5 -> dist=$6.00)
    sm2 = StateManager(state_path)
    ex2 = MT5Executor(dry_run=True)
    ex2.connect()
    ex2._dry_run_m1_atr = 2.50  # Stage 3 raw atr_stop = 3926.0 - 6.0 = 3920.0 < 3921.50!
    resumed = sm2.get_signal(9019, chat_id=-1003712081715)
    assert resumed is not None
    assert ex2.get_runner_fsm_stage(resumed) == 3

    with patch.object(ex2, "get_tick", return_value=_tick(3924.00)):
        assert ex2.manage_designated_runner(resumed) is True
    assert ex2.get_runner_fsm_stage(resumed) == 3
    assert resumed["trail_peak"] == pytest.approx(3926.00)
    assert resumed["fsm_floor"] == pytest.approx(3910.00)
    assert resumed["trail_stop"] == pytest.approx(3921.50)  # Never loosened across restart!

    with patch.object(ex2, "get_tick", return_value=_tick(3921.40)):
        assert ex2.manage_designated_runner(resumed) is False
    assert resumed["status"] == "CLOSED"
    assert resumed["exit_reason_code"] == "ATR_TRAIL"
    assert resumed["exit_stage"] == 3

    # Also verify pre-v3.5.0 legacy state without `fsm_floor` / `trail_stop` resumes safely
    legacy_resumed = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "RUNNER_ACTIVE",
        "tp1_reached": True,
        "runner_cleared_milestones": ["TP1", "TP2"],
        "runner_milestone_idx": 3,
        "runner_layer": "L4",
        "runner_layers": ["L4"],
        "runner_ticket": 1004,
        "runner_tickets": [1004],
        "market_tickets": [1004],
        "layers": {"L4": {"lot": 0.02, "target_price": 3890.0, "tp": 3920.0, "tp_stage": "TP3", "ticket": 1004, "status": "RUNNER"}},
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
        "runner_sl_floor": 3915.0,
        "runner_peak_price": 3918.0,
    }
    with patch.object(ex2, "get_tick", return_value=_tick(3916.00)):
        assert ex2.manage_designated_runner(legacy_resumed) is True
    assert ex2.get_runner_fsm_stage(legacy_resumed) == 2
    assert legacy_resumed["fsm_floor"] == pytest.approx(3900.0)
    assert legacy_resumed["trail_stop"] >= 3915.0


# ---------------------------------------------------------------------------
# 20. config.validate_staged_trail_tiers & config.py raw defaults
# ---------------------------------------------------------------------------

def test_config_validate_staged_trail_tiers_swaps_inverted_and_warns_non_widening(monkeypatch, caplog):
    # 1) Verify config.py raw defaults (independent of local .env overrides)
    clean_keys = (
        "STAGED_EXIT_ENABLED",
        "BE_OFFSET_PIPS",
        "RUNNER_MAX_TP_STAGE",
        "TP1_TRAIL_MIN_DOLLARS",
        "TP1_TRAIL_MAX_DOLLARS",
        "TP1_TRAIL_STEP_DOLLARS",
    )
    for k in clean_keys:
        monkeypatch.delenv(k, raising=False)
    with patch("dotenv.load_dotenv", return_value=False):
        raw_cfg = runpy.run_path(str(Path(config.__file__).resolve()))
    assert raw_cfg["STAGED_EXIT_ENABLED"] is True
    assert raw_cfg["BE_OFFSET_PIPS"] == pytest.approx(5.0)
    assert raw_cfg["RUNNER_MAX_TP_STAGE"] == 5
    assert raw_cfg["TP1_TRAIL_MIN_DOLLARS"] == pytest.approx(2.50)
    assert raw_cfg["TP1_TRAIL_MAX_DOLLARS"] == pytest.approx(3.50)
    assert raw_cfg["TP1_TRAIL_STEP_DOLLARS"] == pytest.approx(0.25)

    # 2) Swaps inverted min > max and updates module globals with warning
    monkeypatch.setattr(config, "TRAIL_TP2_MIN", 5.50)
    monkeypatch.setattr(config, "TRAIL_TP2_MAX", 3.25)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="copier.config"):
        res_swap = config.validate_staged_trail_tiers()
    assert res_swap[2] == (2.0, 3.25, 5.50)
    assert config.TRAIL_TP2_MIN == pytest.approx(3.25)
    assert config.TRAIL_TP2_MAX == pytest.approx(5.50)
    assert any("min (5.50) > max (3.25)" in r.message for r in caplog.records)

    # 3) Warns on non-widening tiers without crashing or mutating passed dict
    caplog.clear()
    non_widening = {
        1: (2.0, 3.0, 5.0),
        2: (1.5, 2.5, 4.0),
        3: (2.5, 4.0, 6.0),
        4: (3.0, 5.0, 8.0),
    }
    with caplog.at_level(logging.WARNING, logger="copier.config"):
        res_warn = config.validate_staged_trail_tiers(non_widening)
    assert res_warn == non_widening
    assert any("do not widen from Stage 1 to Stage 2" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 21. Engineering Safeguard: 4-TP / TP5=None signal under TP5 mode caps cleanly at TP4
# ---------------------------------------------------------------------------

def test_4tp_signal_under_tp5_mode_caps_cleanly_at_tp4():
    assert config.RUNNER_MAX_TP_STAGE == 5
    assert config.STAGED_EXIT_ENABLED is True

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 1.5

    # Case A: explicit 4-TP dict (no TP5 key); Case B: explicit TP5: None dict
    for label, tp_dict in (
        ("4-TP dict", {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0}),
        ("TP5=None dict", {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": None}),
    ):
        sig = _make_4layer_sig("BUY", entry=3890.0, tp_levels=tp_dict, ex=ex)
        with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
            ex.handle_tp1_scaling_and_select_runner(sig)

        for px in (3910.60, 3920.60, 3927.50):  # TP2 -> TP3 -> 75% toward TP4
            with patch.object(ex, "get_tick", return_value=_tick(px)):
                assert ex.manage_designated_runner(sig) is True, f"{label} closed prematurely at {px}"

        assert sig["effective_max_stage"] == 4, label
        assert sig["runner_max_tp_stage"] == 5, label
        assert "TP5" not in sig["tp_levels"], f"{label} fabricated TP5!"
        assert sig["layers"]["L4"]["tp"] == pytest.approx(3930.0), label
        assert sig["layers"]["L4"]["tp_stage"] == "TP4", label

        # Reach TP4 = 3930.0 -> clean TP_CEILING close!
        with patch.object(ex, "get_tick", return_value=_tick(3930.00)):
            assert ex.manage_designated_runner(sig) is False, label
        assert sig["exit_reason_code"] == "TP_CEILING", label
        assert sig["tp4_reached"] is True, label
        assert sig["status"] == "CLOSED", label
        assert sig["market_tickets"] == [], label

    # Case C: subsequent edit adds TP5 = 3940.0 mid-run -> runner_max_tp_stage == 5 unlocks TP5 cleanly!
    sig_edit = _make_4layer_sig(
        "BUY",
        entry=3890.0,
        tp_levels={"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0},
        ex=ex,
    )
    with patch.object(ex, "get_tick", return_value=_tick(3900.60)):
        ex.handle_tp1_scaling_and_select_runner(sig_edit)
    for px in (3910.60, 3920.60):
        with patch.object(ex, "get_tick", return_value=_tick(px)):
            assert ex.manage_designated_runner(sig_edit) is True
    assert sig_edit["effective_max_stage"] == 4 and sig_edit["runner_max_tp_stage"] == 5

    # Provider edit adds TP5=3940.0
    with patch.object(ex, "get_tick", return_value=_tick(3927.50)):
        ex.adjust_active_signal_sl_tp(sig_edit, labeled_tps={"TP5": 3940.0})
    with patch.object(ex, "get_tick", return_value=_tick(3930.60)):
        assert ex.manage_designated_runner(sig_edit) is True
    assert sig_edit["effective_max_stage"] == 5
    assert ex.get_runner_fsm_stage(sig_edit) == 4
    assert sig_edit["trail_tier_used"]["stage"] == 4
    assert sig_edit["layers"]["L4"]["tp"] == pytest.approx(3940.0)

    with patch.object(ex, "get_tick", return_value=_tick(3940.00)):
        assert ex.manage_designated_runner(sig_edit) is False
    assert sig_edit["tp5_reached"] is True
    assert sig_edit["exit_reason_code"] == "TP_CEILING"
    assert sig_edit["status"] == "CLOSED"


# ---------------------------------------------------------------------------
# 22. Manual /trail and /sl commands never loosen broker SL or pollute fsm_floor
# ---------------------------------------------------------------------------

def test_manual_trail_and_sl_never_loosen_broker_sl_or_pollute_fsm_floor():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 1.5  # Stage 2 trail = 2.0 * 1.5 = $3.00

    sig = _make_4layer_sig("BUY", entry=3890.0, ex=ex)
    sig.update({
        "status": "RUNNER_ACTIVE",
        "tp1_reached": True,
        "tp1_touched": True,
        "runner_layer": "L4",
        "runner_layers": ["L4"],
        "runner_ticket": 1004,
        "runner_tickets": [1004],
        "market_tickets": [1004],
        "runner_milestone_idx": 3,
        "runner_milestone_name": "TP3",
        "tp_stage": "TP3",
        "runner_cleared_milestones": ["TP1", "TP2"],
        "fsm_floor": 3900.0,
        "trail_stop": 3915.0,
        "runner_sl_floor": 3915.0,
        "soft_tp1_floor": 3915.0,
        "broker_sl": 3915.0,
        "sl": 3915.0,
        "trail_peak": 3918.0,
    })
    sig["layers"]["L4"].update({"status": "RUNNER", "sl": 3915.0, "tp": 3920.0, "tp_stage": "TP3"})

    mod_calls: List[tuple] = []
    orig_mod = ex.modify_position_sltp

    def _spy_mod(ticket, sl=None, tp=None):
        mod_calls.append((ticket, sl, tp))
        return orig_mod(ticket, sl=sl, tp=tp)

    with patch.object(ex, "get_tick", return_value=_tick(3916.00)), \
         patch.object(ex, "modify_position_sltp", side_effect=_spy_mod):
        # 1) /trail 30 (3916.0 - 3.0 = 3913.0 < 3915.0) must NOT loosen or call MT5
        res_trail = ex.trail_active_signal(sig, trail_pips=30)
        assert mod_calls == []
        assert res_trail["modified_tickets"] == []
        assert sig["sl"] == pytest.approx(3915.0)
        assert sig["broker_sl"] == pytest.approx(3915.0)
        assert sig["trail_stop"] == pytest.approx(3915.0)
        assert sig["fsm_floor"] == pytest.approx(3900.0)

        # 2) adjust_active_signal_sl_tp(new_sl=3912.0, ratchet_sl_only=False) rejects loosening
        res_adj = ex.adjust_active_signal_sl_tp(sig, new_sl=3912.0, ratchet_sl_only=False)
        assert res_adj["rejected_sl_reason"] is not None
        assert all(sl is None for _, sl, _ in mod_calls)
        assert sig["sl"] == pytest.approx(3915.0)
        assert sig["trail_stop"] == pytest.approx(3915.0)
        assert sig["broker_sl"] == pytest.approx(3915.0)
        assert sig["fsm_floor"] == pytest.approx(3900.0)

    # 3) Tighter new_sl=3915.5 where broker rejects -> broker_sl stays 3915.0
    with patch.object(ex, "get_tick", return_value=_tick(3916.00)), \
         patch.object(ex, "modify_position_sltp", return_value=False):
        res_rej = ex.adjust_active_signal_sl_tp(sig, new_sl=3915.5, ratchet_sl_only=False)
        assert res_rej["modified_positions"] == []
        assert sig["broker_sl"] == pytest.approx(3915.0)

        res_trail_rej = ex.trail_active_signal(sig, explicit_sl_price=3915.6)
        assert res_trail_rej["modified_tickets"] == []
        assert sig["broker_sl"] == pytest.approx(3915.0)

    # 4) Milestone `trail tp2` keeps fsm_floor strictly structural (TP1=3900.0) and ratchets trail_stop monotonically
    sig_m = _make_4layer_sig("BUY", entry=3890.0, tickets=(2001, 2002, 2003, 2004), ex=ex)
    ex._dry_run_m1_atr = 2.0  # Stage 1=$3.00, Stage 2=$4.00
    with patch.object(ex, "get_tick", return_value=_tick(3900.20)):
        ex.handle_tp1_scaling_and_select_runner(sig_m)
    with patch.object(ex, "get_tick", return_value=_tick(3910.20)):  # Still Stage 1 (< 3910.50 clean break)
        assert ex.manage_designated_runner(sig_m) is True
        ex.trail_active_signal(sig_m, explicit_sl_price=3909.50)
    prev_stop = sig_m["trail_stop"]
    assert prev_stop == pytest.approx(3909.50)

    # At live=3911.0, Stage 2 raw ATR stop = 3911.0 - 4.0 = 3907.0 (< 3909.50) -> must keep 3909.50 & fsm_floor=3900.0
    with patch.object(ex, "get_tick", return_value=_tick(3911.00)):
        res_m = ex.trail_active_signal(sig_m, milestone="TP2")
    assert ex.get_runner_fsm_stage(sig_m) == 2
    assert sig_m["fsm_floor"] == pytest.approx(3900.0)
    assert sig_m["trail_stop"] == pytest.approx(prev_stop)
    assert sig_m["broker_sl"] == pytest.approx(prev_stop)
    assert res_m["new_sl"] == pytest.approx(prev_stop)


# ---------------------------------------------------------------------------
# 23. Broker-TP close reconciliation on 4-TP signal under TP5 mode sets TP4 & exit_price
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_4tp_broker_tp_close_under_tp5_mode_sets_tp4_and_exit_price(tmp_path):
    assert config.RUNNER_MAX_TP_STAGE == 5
    assert config.STAGED_EXIT_ENABLED is True

    ex = _reset_main_runtime(tmp_path, "t23_reconcile_4tp")

    # Seed both: (a) 4-TP dict with effective_max_stage=4 and (b) TP5=None without pre-set effective_max_stage
    for idx, (msg_id, tkt, tp_map, set_eff) in enumerate((
        (9023, 5004, {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0}, True),
        (9024, 5005, {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": None}, False),
    )):
        ex._dry_run_ticket_lots[tkt] = 0.02
        sig = main.state_mgr.create_signal(
            message_id=msg_id,
            chat_id=-1003712081715,
            symbol="XAUUSD.s",
            direction="BUY",
            sl=3910.0,
            layers={
                "L4": {
                    "lot": 0.02,
                    "target_price": 3890.0,
                    "tp": 3930.0,
                    "tp_stage": "TP4",
                    "ticket": tkt,
                    "order_type": "MARKET",
                    "status": "RUNNER",
                }
            },
            market_tickets=[tkt],
            pending_tickets=[],
            final_tp=3899.5,
            raw_tp1=3900.0,
            raw_tp2=3910.0,
            raw_tp3=3920.0,
            raw_tp4=3930.0,
            tp_levels=tp_map,
            tp_stage="TP4",
            status="RUNNER_ACTIVE",
            tp1_reached=True,
            runner_ticket=tkt,
            runner_tickets=[tkt],
            runner_layer="L4",
            runner_layers=["L4"],
            runner_milestone_idx=4,
            runner_milestone_name="TP4",
            runner_cleared_milestones=["TP1", "TP2", "TP3"],
            runner_max_tp_stage=5,
            trail_peak=3930.0,
            recorded_entry_price=3890.0,
            initial_price=3890.0,
            **({"effective_max_stage": 4} if set_eff else {}),
        )
        assert sig is not None

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex, "get_live_positions", return_value=[]), \
         patch.object(ex, "get_live_orders", return_value=[]), \
         patch.object(ex, "get_position_close_info", return_value={"profit": 20.0, "reason": "TP"}):
        await main._reconcile_live_mt5_signals(main.state_mgr.get_active_signals())

    for msg_id in (9023, 9024):
        s_closed = main.state_mgr.get_signal(msg_id, chat_id=-1003712081715)
        assert s_closed["status"] == "CLOSED"
        assert s_closed.get("tp4_reached") is True
        assert not s_closed.get("tp5_reached")
        assert s_closed["exit_stage"] == 4
        assert s_closed["exit_reason_code"] == "TP_CEILING"
        assert s_closed["exit_price"] == pytest.approx(3930.0)

    trades = main.trade_ledger.read_trades()
    assert len(trades) == 2
    for rec in trades:
        assert rec["exit_reason_code"] == "TP_CEILING"
        assert rec["exit_stage"] == 4
        assert rec["exit_price"] == pytest.approx(3930.0)
        assert rec["giveback_from_peak"] == 0.0
        assert rec["realized_pnl"] == pytest.approx(20.0)
