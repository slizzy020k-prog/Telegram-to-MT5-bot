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
# 5. ATR trailing: _compute_tp1_trail_dist clamps output to [min, max]
# ---------------------------------------------------------------------------
def test_atr_trail_dist_clamps_to_range(monkeypatch):
    monkeypatch.setattr(config, "TP1_TRAIL_ATR_MULT", 1.5)
    monkeypatch.setattr(config, "TP1_TRAIL_MIN_DOLLARS", 2.00)
    monkeypatch.setattr(config, "TP1_TRAIL_MAX_DOLLARS", 3.00)
    ex = _make_executor()

    # Patch get_m1_atr to return a very large value (should clamp to max)
    monkeypatch.setattr(ex, "get_m1_atr", lambda sym: 10.0)
    dist_high = ex._compute_tp1_trail_dist("XAUUSD.s", digits=2)
    assert dist_high == 3.00, f"Expected clamped to max=3.00, got {dist_high}"

    # Patch get_m1_atr to return a very small value (should clamp to min)
    monkeypatch.setattr(ex, "get_m1_atr", lambda sym: 0.1)
    dist_low = ex._compute_tp1_trail_dist("XAUUSD.s", digits=2)
    assert dist_low == 2.00, f"Expected clamped to min=2.00, got {dist_low}"

    # Fallback: ATR unavailable → returns midpoint
    monkeypatch.setattr(ex, "get_m1_atr", lambda sym: None)
    dist_fallback = ex._compute_tp1_trail_dist("XAUUSD.s", digits=2)
    assert dist_fallback == 2.50, f"Expected midpoint fallback=2.50, got {dist_fallback}"


# ---------------------------------------------------------------------------
# 6. ATR trailing: trail_stop ratchets forward-only (never moves back against trade)
# ---------------------------------------------------------------------------
def test_atr_trail_ratchets_forward_only(monkeypatch):
    monkeypatch.setattr(config, "TP1_TRAIL_ATR_MULT", 1.5)
    monkeypatch.setattr(config, "TP1_TRAIL_MIN_DOLLARS", 2.00)
    monkeypatch.setattr(config, "TP1_TRAIL_MAX_DOLLARS", 3.00)
    monkeypatch.setattr(config, "BE_OFFSET_PIPS", 0.0)
    ex = _make_executor()
    monkeypatch.setattr(ex, "get_m1_atr", lambda sym: 1.5)  # trail_dist = 2.25

    signal_data: dict = {}

    # First tick at 3850 (just touched TP1)
    peak1, dist1, stop1 = ex._update_tp1_trail_state(
        signal_data, "XAUUSD.s", "BUY", 3850.0, 0.10, 2
    )
    assert stop1 == round(3850.0 - 2.25, 2), f"Initial trail_stop wrong: {stop1}"

    # Second tick — price moves up to 3853
    peak2, dist2, stop2 = ex._update_tp1_trail_state(
        signal_data, "XAUUSD.s", "BUY", 3853.0, 0.10, 2
    )
    assert stop2 > stop1, f"Trail stop should ratchet up with price: {stop2} <= {stop1}"

    # Third tick — price pulls back to 3851 (still above new stop)
    peak3, dist3, stop3 = ex._update_tp1_trail_state(
        signal_data, "XAUUSD.s", "BUY", 3851.0, 0.10, 2
    )
    assert stop3 == stop2, f"Trail stop must NOT move backwards on a pullback: {stop3} != {stop2}"
    assert peak3 == peak2, f"Trail peak must NOT move backwards: {peak3} != {peak2}"


# ---------------------------------------------------------------------------
# 7. ATR trailing: trail_stop honours breakeven floor
# ---------------------------------------------------------------------------
def test_atr_trail_respects_breakeven_floor(monkeypatch):
    monkeypatch.setattr(config, "TP1_TRAIL_ATR_MULT", 1.5)
    monkeypatch.setattr(config, "TP1_TRAIL_MIN_DOLLARS", 2.00)
    monkeypatch.setattr(config, "TP1_TRAIL_MAX_DOLLARS", 3.00)
    monkeypatch.setattr(config, "BE_OFFSET_PIPS", 5.0)  # 5 pip BE offset
    ex = _make_executor()
    monkeypatch.setattr(ex, "get_m1_atr", lambda sym: 1.5)

    # Entry at 3840.0 — BE floor = 3840 + (5 * 0.10) = 3840.50
    signal_data = {"recorded_entry_price": 3840.0}

    # Price only 0.1 above TP1 (3841.0 say); trail_dist=2.25 → raw cand_stop = 3838.75
    # But BE floor = 3840.50 → trail_stop must be >= 3840.50
    _, _, stop = ex._update_tp1_trail_state(
        signal_data, "XAUUSD.s", "BUY", 3841.0, 0.10, 2
    )
    be_floor = round(3840.0 + 5.0 * 0.10, 2)  # 3840.50
    assert stop >= be_floor, (
        f"Trail stop {stop} must not go below BE floor {be_floor}"
    )
