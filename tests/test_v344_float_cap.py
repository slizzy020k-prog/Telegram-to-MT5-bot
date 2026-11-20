"""
v3.4.4 Regression Tests — Floating-Loss Hard Cap & ATR Trailing Verification

Tests:
  1. float_force_close_triggered fires when floating loss >= FLOAT_LOSS_FORCE_CLOSE_PCT of balance
  2. float_force_close_triggered does NOT fire when floating loss is below the cap
  3. float_force_close_triggered fires independently of emergency_cutoff_triggered
  4. FLOAT_LOSS_FORCE_CLOSE_PCT is live-patchable (simulating /floatcap command)
  5. ATR trailing: _compute_tp1_trail_dist clamps to [min, max] range
  6. ATR trailing: _update_tp1_trail_state ratchets forward-only (never moves SL backwards)
  7. ATR trailing: trail_stop honours breakeven floor from primary entry
"""

import sys
import types
import importlib
import pytest


import config  # noqa: E402
import mt5_executor  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(balance: float = 300.0, floating_pnl: float = 0.0) -> mt5_executor.MT5Executor:
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._dry_run_capital = balance
    ex._dry_run_floating_pnl = float(floating_pnl)
    ex._connected = False  # stays in dry-run branch
    return ex


# ---------------------------------------------------------------------------
# 1. Float force-close fires at exactly the cap
# ---------------------------------------------------------------------------
def test_float_force_close_triggered_at_cap(monkeypatch):
    monkeypatch.setattr(config, "FLOAT_LOSS_FORCE_CLOSE_PCT", 60.0)
    # Floating loss = exactly 60% of £300 = £180
    ex = _make_executor(balance=300.0, floating_pnl=-180.0)
    dd = ex.get_daily_drawdown_status()
    assert dd["float_force_close_triggered"] is True, (
        f"Expected float_force_close_triggered=True at exactly 60% floating loss, got {dd}"
    )


# ---------------------------------------------------------------------------
# 2. Float force-close does NOT fire below the cap
# ---------------------------------------------------------------------------
def test_float_force_close_not_triggered_below_cap(monkeypatch):
    monkeypatch.setattr(config, "FLOAT_LOSS_FORCE_CLOSE_PCT", 60.0)
    # Floating loss = 59% of £300 = £177
    ex = _make_executor(balance=300.0, floating_pnl=-177.0)
    dd = ex.get_daily_drawdown_status()
    assert dd["float_force_close_triggered"] is False, (
        f"Expected float_force_close_triggered=False at 59% floating loss, got {dd}"
    )


# ---------------------------------------------------------------------------
# 3. Float force-close fires independent of emergency_cutoff (which needs 90%)
# ---------------------------------------------------------------------------
def test_float_force_close_fires_before_emergency_cutoff(monkeypatch):
    monkeypatch.setattr(config, "FLOAT_LOSS_FORCE_CLOSE_PCT", 60.0)
    monkeypatch.setattr(config, "EMERGENCY_CUTOFF_LOSS_PCT", 90.0)
    # Floating loss = 65% (above 60% cap, below 90% emergency)
    ex = _make_executor(balance=300.0, floating_pnl=-195.0)
    dd = ex.get_daily_drawdown_status()
    assert dd["float_force_close_triggered"] is True
    assert dd["emergency_cutoff_triggered"] is False  # 65% floating is below 90% emergency cutoff


# ---------------------------------------------------------------------------
# 4. FLOAT_LOSS_FORCE_CLOSE_PCT is live-patchable (simulates /floatcap command)
# ---------------------------------------------------------------------------
def test_float_cap_live_patch(monkeypatch):
    monkeypatch.setattr(config, "FLOAT_LOSS_FORCE_CLOSE_PCT", 60.0)
    ex = _make_executor(balance=300.0, floating_pnl=-130.0)  # 43% — below 60%
    dd_before = ex.get_daily_drawdown_status()
    assert dd_before["float_force_close_triggered"] is False

    # Simulate /floatcap 40 — now 43% > 40% so it should fire
    monkeypatch.setattr(config, "FLOAT_LOSS_FORCE_CLOSE_PCT", 40.0)
    dd_after = ex.get_daily_drawdown_status()
    assert dd_after["float_force_close_triggered"] is True, (
        "After patching cap to 40%, 43% floating loss should trigger force-close"
    )


# ---------------------------------------------------------------------------
# 5. ATR trailing: compute_dynamic_atr_trail_stop clamps trail_dist to [$2.50, max]
# ---------------------------------------------------------------------------
def test_atr_trail_dist_clamps_to_range(monkeypatch):
    import dynamic_risk
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.5)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00)
    policy = dynamic_risk.read_dynamic_risk_policy(config)

    # ATR=10.0 -> 1.5 * 10 = 15.0 clamped to max=12.00
    res_high = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3850.0,
        live_price=3860.0,
        prev_peak=3860.0,
        current_sl=3840.0,
        atr=10.0,
        policy=policy,
        digits=2,
    )
    assert res_high["trail_dist"] == 12.00, f"Expected clamped to max=12.00, got {res_high['trail_dist']}"

    # ATR=0.5 -> 1.5 * 0.5 = 0.75 clamped to min=$2.50 floor
    res_low = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3850.0,
        live_price=3860.0,
        prev_peak=3860.0,
        current_sl=3840.0,
        atr=0.5,
        policy=policy,
        digits=2,
    )
    assert res_low["trail_dist"] == 2.50, f"Expected clamped to min=$2.50 floor, got {res_low['trail_dist']}"


# ---------------------------------------------------------------------------
# 6. ATR trailing: compute_dynamic_atr_trail_stop ratchets forward-only
# ---------------------------------------------------------------------------
def test_atr_trail_ratchets_forward_only(monkeypatch):
    import dynamic_risk
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.5)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25)
    policy = dynamic_risk.read_dynamic_risk_policy(config)

    # First tick at 3850 (entry=3840, peak=3850, atr=2.0 -> trail_dist = max(3.0, 2.50) = 3.0)
    # raw cand = 3850 - 3.0 = 3847.0
    r1 = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3840.0,
        live_price=3850.0,
        prev_peak=3850.0,
        current_sl=3835.0,
        atr=2.0,
        policy=policy,
        digits=2,
    )
    assert r1["should_update"] is True
    stop1 = r1["candidate_sl"]
    assert stop1 == 3847.0, f"Initial candidate_sl wrong: {stop1}"

    # Second tick — price moves up to 3853 (peak=3853)
    r2 = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3840.0,
        live_price=3853.0,
        prev_peak=3853.0,
        current_sl=stop1,
        atr=2.0,
        policy=policy,
        digits=2,
    )
    assert r2["should_update"] is True
    stop2 = r2["candidate_sl"]
    assert stop2 == 3850.0, f"Trail stop should ratchet up with price: {stop2} <= {stop1}"
    assert stop2 > stop1

    # Third tick — price pulls back to 3851 (peak remains 3853, current_sl=3850.0)
    r3 = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3840.0,
        live_price=3851.0,
        prev_peak=3853.0,
        current_sl=stop2,
        atr=2.0,
        policy=policy,
        digits=2,
    )
    # On pullback, peak is still 3853, so candidate_sl is still 3850.0 -> no forward progress
    assert r3["should_update"] is False
    assert r3["candidate_sl"] == stop2, f"Trail stop must NOT move backwards on a pullback: {r3['candidate_sl']} != {stop2}"
    assert r3["peak_price"] == 3853.0, f"Trail peak must NOT move backwards: {r3['peak_price']}"


# ---------------------------------------------------------------------------
# 7. ATR trailing: candidate_sl honours breakeven floor from entry
# ---------------------------------------------------------------------------
def test_atr_trail_respects_breakeven_floor(monkeypatch):
    import dynamic_risk
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.5)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_ACTIVATION_MULT", 0.75)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS", 3.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 0.50)
    policy = dynamic_risk.read_dynamic_risk_policy(config)

    # Entry at 3840.0 — BE floor = 3840 + 0.50 = 3840.50
    # Price reaches 3843.50 (MFE = $3.50 >= be_min_mfe_dollars 3.50)
    # With ATR=4.0, raw atr_sl = 3843.50 - 6.00 = 3837.50 (< entry!)
    # But BE lock is armed -> candidate_sl must be at least BE floor 3840.50!
    r = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3840.0,
        live_price=3843.50,
        prev_peak=3843.50,
        current_sl=3830.0,
        atr=4.0,
        policy=policy,
        digits=2,
    )
    assert r["be_armed"] is True
    be_floor = 3840.50
    assert r["candidate_sl"] >= be_floor, f"Candidate SL {r['candidate_sl']} must not go below BE floor {be_floor}"
    assert r["stop_source"] == "BE_LOCK"
