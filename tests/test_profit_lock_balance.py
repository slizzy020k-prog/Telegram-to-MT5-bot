"""Regression tests: the profit lock must keep a real share of the peak AND stay
clear of the noise.

The complaint this fixes (live account 36393319, 2026-11-27): trades were "stopped out
at a pound and then it went on for three". The cause was the profit lock taking a FIXED
FRACTION of the peak: at a small peak (say $1.56/oz) a 45% lock put the stop $0.86
below the peak -- inside gold's $0.52 spread plus the noise around it -- so the lock
fired on noise and the trade exited at a fraction of what it had found. At the same time
the ATR trail gave back everything, so the winners that did survive captured only
26-67% of their peak excursion.

The rule now:
    noise_floor = max(cost_floor, min(0.25 * bounded_ATR, 0.35 * peak_MFE))
    retained    = min(tier_fraction * peak_MFE, peak_MFE - noise_floor)
so the stop never sits closer to the peak than the noise, the giveback never eats
more than 35% of the peak, and the tier fraction (now 60% -> 93%) keeps as much of
the peak as the noise allows.

Hermetic: pure engine calls, no broker connection, no live trades.
"""
from __future__ import annotations

import pytest

import dynamic_risk as dr
from dynamic_risk import DynamicRiskPolicy, compute_dynamic_atr_trail_stop

LADDER = "0.30:0.60,0.50:0.70,0.75:0.78,1.00:0.84,1.40:0.88,1.80:0.91,2.50:0.93"
COST_FLOOR = 0.60


def _pol(**kw):
    base = dict(mfe_lock_enabled=True, mfe_lock_tiers=LADDER, mfe_lock_cost_floor_dollars=COST_FLOOR)
    base.update(kw)
    return DynamicRiskPolicy(**base)


def _eng(entry, live, atr, direction="BUY", prev_pct=None, prev_profit=None, **kw):
    extra = {}
    if prev_pct is not None:
        extra["mfe_lock_prev_pct"] = prev_pct
    if prev_profit is not None:
        extra["mfe_lock_prev_profit"] = prev_profit
    return compute_dynamic_atr_trail_stop(
        direction=direction,
        entry_price=entry,
        live_price=live,
        prev_peak=live,
        current_sl=0.0,
        atr=atr,
        policy=_pol(**kw),
        digits=2,
        stops_buffer=0.25,
        **extra,
    )


def _capture(res, peak):
    """Share of the peak excursion that the lock keeps."""
    return res["mfe_lock_profit"] / peak


def test_lock_never_sits_inside_the_noise():
    """The retained profit keeps the stop at least the noise floor below the peak."""
    atr = 2.70
    for peak in (1.50, 2.07, 2.42, 3.50, 4.39, 5.25):
        res = _eng(3890.0, 3890.0 + peak, atr)
        floor = res["mfe_lock_noise_floor"]
        assert floor >= COST_FLOOR
        giveback = peak - res["mfe_lock_profit"]
        assert giveback <= max(COST_FLOOR, 0.35 * peak) + 1e-9, (
            "the giveback is the smaller of a quarter of the ATR and 35% of the peak"
        )
        if res["mfe_lock_sl"] is not None:
            assert peak - res["mfe_lock_profit"] >= floor - 1e-9, (
                "the locked stop must stay at least the noise floor below the peak"
            )


def test_a_big_peak_keeps_most_of_the_move():
    """The complaint's 'we could have made three pounds but got stopped out'."""
    atr = 2.70
    peak = 4.39                       # ~GBP 3.34 on a 0.01 lot
    res = _eng(3890.0, 3890.0 + peak, atr)
    assert res["mfe_lock_sl"] is not None
    assert _capture(res, peak) >= 0.80
    assert res["mfe_lock_profit"] >= 3.50    # USD/oz -> ~GBP 2.67 kept


def test_capture_is_materially_better_than_the_old_fixed_fraction():
    """The old rule kept only 45-67% of the peak; the balance must not regress."""
    atr = 2.70
    for peak in (1.56, 2.07, 2.42, 3.50, 4.39):
        res = _eng(3890.0, 3890.0 + peak, atr)
        assert res["mfe_lock_sl"] is not None
        assert _capture(res, peak) >= 0.55, (
            "the lock must keep at least 55% of the peak excursion"
        )


def test_retained_profit_is_exactly_the_noise_bounded_value():
    """Exact numbers: reverting the noise rule changes them, so this pins the fix.

    noise = max(cost_floor, min(0.25 * ATR, 0.35 * peak))
    retained = min(tier_fraction * peak, peak - noise)
    """
    atr = 2.70
    expected = {1.56: 0.96, 2.07: 1.395, 2.42: 1.745, 3.50: 2.825, 4.39: 3.715}
    for peak, want in expected.items():
        res = _eng(3890.0, 3890.0 + peak, atr)
        assert res["mfe_lock_profit"] == pytest.approx(want), (
            "peak %.2f: the retained profit must be the noise-bounded value" % peak
        )


def test_the_noise_rule_is_what_bounds_the_retained_profit():
    """Without the noise rule the retained profit would be the raw tier fraction."""
    peak = 4.39
    res = _eng(3890.0, 3890.0 + peak, 2.70)
    raw_fraction = res["mfe_lock_fraction"] * peak
    assert res["mfe_lock_profit"] < raw_fraction, (
        "the noise floor must pull the retained profit below the raw tier fraction"
    )
    assert res["mfe_lock_profit"] == pytest.approx(peak - res["mfe_lock_noise_floor"])


def test_a_peak_too_small_to_lock_stays_off():
    """A peak below the first tier leaves the lock off instead of clipping."""
    atr = 4.0
    # peak 1.00 on an ATR of 4.00 is a ratio of 0.25 -> below the first tier (0.30),
    # so no profit is locked at all and the position is left to the breakeven lock and
    # the ATR trail.
    res = _eng(3700.0, 3701.00, atr)
    assert res["mfe_lock_sl"] is None
    assert res["mfe_lock_profit"] == pytest.approx(0.0)
    assert res["mfe_lock_fraction"] == pytest.approx(0.0)


def test_sell_side_mirrors_buy():
    atr = 2.70
    peak = 4.39
    buy = _eng(3890.0, 3890.0 + peak, atr, direction="BUY")
    sell = _eng(3890.0, 3890.0 - peak, atr, direction="SELL")
    assert buy["mfe_lock_profit"] == pytest.approx(sell["mfe_lock_profit"])
    assert sell["mfe_lock_sl"] == pytest.approx(3890.0 - sell["mfe_lock_profit"], abs=0.011)  # broker 2dp


def test_the_profit_ratchet_still_never_regresses():
    """A locked profit is never given back when the peak dips."""
    res = _eng(3890.0, 3890.0 + 4.0, 12.0, prev_pct=0.88, prev_profit=3.52)
    assert res["mfe_lock_fraction"] == pytest.approx(0.88)
    assert res["mfe_lock_profit"] == pytest.approx(3.52)
