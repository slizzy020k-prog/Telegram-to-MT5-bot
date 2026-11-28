"""T3 upgrade -- ATR-relative MFE ladder + 2% Relative ATR + auto 30-pip BE (opt-in).

Supersedes the fractional/absolute MFE-lock. All new behavior is OPT-IN: with defaults
the engine/executor are byte-for-byte unchanged (original suite stays green). Covers:
  * baseline-unchanged grid + neutral new logging keys;
  * all 7 ATR-relative tiers (ratio:pct), BUY/SELL, exact protected profit + SL;
  * configurable ATR bounds (high ATR cannot delay the first tier);
  * cost floor for profit locks; hysteresis (ATR rise / MFE dip never regress tier);
  * protected-profit ratchet; persistence of MFE+tier on the layer across restart/TP moves;
  * 2% Relative ATR adjustment (1.00->1.02, 1.50->1.53, 2.00->2.04) + idempotence;
  * auto 30-pip BE through the single picker (never loosens), BUY/SELL;
  * protection-marker gap fix + edit-loosening regression (main._resolve_edit_layer_sl);
  * trail/BE activation ceilings + MODERATE distance tiers (monotonic, never expand);
  * propagation of every new knob + registration completeness; non-vacuous mutation.

Hermetic: engine tests build DynamicRiskPolicy directly; executor tests use dry-run.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from types import SimpleNamespace

import pytest

import config
import dynamic_risk as dr
import main
import mt5_executor
from dynamic_risk import DynamicRiskPolicy, compute_dynamic_atr_trail_stop, compute_volatility_ratio

LADDER = "0.30:0.60,0.50:0.70,0.75:0.78,1.00:0.84,1.40:0.88,1.80:0.91,2.50:0.93"
DIST_TIERS = "6:3.50,10:3.00,15:2.75,20:2.50"  # MODERATE caps (preserve $2-4 Gold retracements)
TIERS7 = [(0.30, 0.60), (0.50, 0.70), (0.75, 0.78), (1.00, 0.84), (1.40, 0.88), (1.80, 0.91), (2.50, 0.93)]

NEW_KNOBS = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": "trail_activation_ceiling_dollars",
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": "be_activation_ceiling_dollars",
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "trail_distance_tiers",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": "mfe_lock_enabled",
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "mfe_lock_tiers",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": "mfe_lock_cost_floor_dollars",
    "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS": "mfe_lock_atr_min_dollars",
    "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS": "mfe_lock_atr_max_dollars",
    "DYNAMIC_ATR_RELATIVE_SCALE_MULT": "relative_scale_mult",
    "DYNAMIC_ATR_AUTO_BE_ENABLED": "auto_be_enabled",
    "DYNAMIC_ATR_AUTO_BE_PIPS": "auto_be_pips",
    "DYNAMIC_ATR_AUTO_BE_PIP_SIZE_DOLLARS": "auto_be_pip_size_dollars",
    "DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT": "trail_distance_scale_mult",
    "DYNAMIC_ATR_INTERIM_FLOOR_ENABLED": "interim_floor_enabled",
    "DYNAMIC_ATR_INTERIM_FLOOR_K": "interim_floor_k",
}


def _pol(**kw):
    return DynamicRiskPolicy(**kw)


def _ladder(**kw):
    base = dict(mfe_lock_enabled=True, mfe_lock_tiers=LADDER)
    base.update(kw)
    return DynamicRiskPolicy(**base)


def _eng(direction, entry, live, pol, atr=9.78, peak=None, sl=None, r=None, buf=0.0,
         milestones=None, tp=None, r_lock_floor=None, prev_pct=None, prev_profit=None, auto_be=None):
    return compute_dynamic_atr_trail_stop(
        direction=direction, entry_price=entry, live_price=live, prev_peak=peak,
        current_sl=sl, atr=atr, policy=pol, digits=2, stops_buffer=buf,
        cleared_milestones=milestones, tp_levels=tp, initial_r=r, r_lock_floor=r_lock_floor,
        mfe_lock_prev_pct=prev_pct, mfe_lock_prev_profit=prev_profit, auto_be_threshold=auto_be,
    )


def _live(entry, mfe, direction):
    return entry + mfe if direction == "BUY" else entry - mfe


def _sig(entry=3700.0, sl=3690.0, ticket=101, direction="BUY"):
    sgn = 1.0 if direction == "BUY" else -1.0
    return {
        "symbol": "XAUUSD.s", "direction": direction, "status": "ACTIVE",
        "layers": {"L1": {"ticket": ticket, "status": "OPEN", "lot": 0.01, "remaining_lot": 0.01,
                          "entry_price": entry, "initial_sl": sl, "broker_sl": sl}},
        "market_tickets": [ticket], "pending_tickets": [],
        "tp_levels": {"TP1": entry + sgn * 20.0, "TP2": entry + sgn * 30.0},
        "runner_cleared_milestones": [],
    }


def _tick(entry, mfe, direction):
    if direction == "BUY":
        bid = entry + mfe
        return SimpleNamespace(bid=bid, ask=bid + 0.2, time=1700000000.0)
    ask = entry - mfe
    return SimpleNamespace(bid=ask - 0.2, ask=ask, time=1700000000.0)


def _ex(atr):
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = atr
    return ex


# ---------------------------------------------------------------------------
# 1. Baseline unchanged at defaults
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("atr,mfe,r", [(4.0, 5.0, None), (4.0, 12.0, 8.0), (9.78, 5.0, 32.0),
                                       (2.0, 3.0, None), (6.0, 20.0, 10.0), (9.78, 1.0, 32.0)])
def test_baseline_new_keys_neutral(direction, atr, mfe, r):
    pol = _pol()
    entry = 3700.0
    res = _eng(direction, entry, _live(entry, mfe, direction), pol, atr=atr, r=r)
    sign = 1.0 if direction == "BUY" else -1.0
    assert res["mfe_lock_sl"] is None
    assert res["mfe_lock_fraction"] == 0.0
    assert res["mfe_lock_profit"] == 0.0
    assert res["mfe_lock_ratio"] == 0.0
    assert res["auto_be_sl"] is None and res["auto_be_armed"] is False
    assert res["distance_cap_applied"] is False
    assert res["trail_activation_threshold"] == pytest.approx(round(pol.trail_activation_atr_mult * atr, 4))
    be_floor = pol.be_lock_offset_dollars + pol.be_min_distance_dollars
    if r is not None and r > 0:
        exp_be = max(
            pol.be_r_activation_mult * r,
            pol.be_activation_atr_mult * atr,
            pol.be_min_mfe_dollars,
            be_floor,
        )
    else:
        exp_be = max(pol.be_activation_atr_mult * atr, pol.be_min_mfe_dollars, be_floor)
    assert res["be_threshold"] == pytest.approx(round(exp_be, 4))
    if res["be_armed"]:
        assert res["be_sl"] == pytest.approx(round(entry + sign * pol.be_lock_offset_dollars, 2))
    if res["trail_armed"]:
        assert res["atr_sl"] == pytest.approx(round(res["peak_price"] - sign * res["trail_dist"], 2))


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_baseline_picker_oracle(direction):
    pol = _pol()
    entry = 3700.0
    res = _eng(direction, entry, _live(entry, 5.0, direction), pol, atr=4.0)
    sign = 1.0 if direction == "BUY" else -1.0
    assert res["be_armed"] and res["trail_armed"]
    assert res["stop_source"] == "BE_LOCK"
    assert res["candidate_sl"] == pytest.approx(round(entry + sign * 1.2, 2))  # the $1.20 BE lock offset


def test_baseline_disabled_path_neutral():
    res = _eng("BUY", 3700.0, 3705.0, _pol(trail_enabled=False), atr=4.0)
    assert res["armed"] is False
    assert res["mfe_lock_sl"] is None and res["auto_be_sl"] is None
    assert res["trail_activation_threshold"] is None and res["be_threshold"] is None


# ---------------------------------------------------------------------------
# 2. ATR-relative MFE ladder (all 7 tiers)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("ratio,pct", TIERS7)
def test_ladder_seven_tiers_exact(direction, ratio, pct):
    pol = _ladder()
    entry = 3700.0
    atr = 4.0
    mfe = ratio * atr
    sign = 1.0 if direction == "BUY" else -1.0
    res = _eng(direction, entry, _live(entry, mfe, direction), pol, atr=atr)
    # The retained profit is the tier fraction of the peak, but never closer to the
    # peak than the noise floor (the cost floor + a slice of the bounded ATR), so
    # ordinary noise cannot clip the lock.
    noise = max(pol.mfe_lock_cost_floor_dollars, min(0.25 * atr, 0.35 * mfe))
    expected = min(pct * mfe, mfe - noise)
    assert res["mfe_lock_ratio"] == pytest.approx(ratio)
    assert res["mfe_lock_fraction"] == pytest.approx(pct)
    assert res["mfe_lock_noise_floor"] == pytest.approx(noise)
    assert res["mfe_lock_profit"] == pytest.approx(expected)
    # The lock only binds when the retained profit clears the cost floor. On the
    # first tier the peak is too small for that (the cost floor eats it), so the
    # lock stays disarmed. (The "peak too small to lock" case is also covered in
    # tests/test_profit_lock_balance.py.)
    if expected + 1e-9 >= pol.mfe_lock_cost_floor_dollars:
        assert res["mfe_lock_sl"] == pytest.approx(round(entry + sign * round(expected, 2), 2))
        if expected >= pol.be_lock_offset_dollars:
            # The ladder lock is at least as far from the entry as the breakeven lock,
            # so it is the tighter candidate and wins the picker.
            assert res["stop_source"] == "MFE_LOCK"
            assert res["candidate_sl"] == pytest.approx(res["mfe_lock_sl"])
        else:
            # The breakeven lock (entry +/- the $1.20 offset) is TIGHTER than this
            # ladder tier, so it wins the picker; the ladder lock is still recorded.
            assert res["stop_source"] == "BE_LOCK"
            assert sign * (res["candidate_sl"] - res["mfe_lock_sl"]) >= 0
    else:
        assert res["mfe_lock_sl"] is None


def test_ladder_no_fixed_dollar_activation():
    # Same MFE, different ATR -> different tier (ratio-keyed, NOT fixed-dollar).
    pol = _ladder()
    low = _eng("BUY", 3700.0, 3704.0, pol, atr=4.0)    # ratio 1.00 -> 84%
    high = _eng("BUY", 3700.0, 3704.0, pol, atr=8.0)   # ratio 0.50 -> 70%
    assert low["mfe_lock_fraction"] == pytest.approx(0.84)
    assert high["mfe_lock_fraction"] == pytest.approx(0.70)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_ladder_atr_max_bound_prevents_delay(direction):
    pol = _ladder(mfe_lock_atr_max_dollars=12.0)
    entry = 3700.0
    # ATR 50 (extreme) but bounded to 12 -> MFE 4.8 gives ratio 0.40 -> first tier arms.
    res = _eng(direction, entry, _live(entry, 4.8, direction), pol, atr=50.0)
    assert res["mfe_lock_fraction"] == pytest.approx(0.60)
    assert res["mfe_lock_sl"] is not None
    unbounded = _eng(direction, entry, _live(entry, 4.8, direction), _ladder(mfe_lock_atr_max_dollars=500.0), atr=50.0)
    assert unbounded["mfe_lock_fraction"] == 0.0  # ratio 0.096 < 0.30 -> no tier


def test_ladder_atr_min_bound_prevents_instant_arm():
    pol = _ladder(mfe_lock_atr_min_dollars=1.0)
    # Tiny ATR 0.2 clamped to 1.0 -> MFE 0.4 gives ratio 0.40 (not 2.0).
    res = _eng("BUY", 3700.0, 3700.4, pol, atr=0.2)
    assert res["mfe_lock_ratio"] == pytest.approx(0.40)
    assert res["mfe_lock_fraction"] == pytest.approx(0.60)


def test_ladder_cost_floor_gates_profit_lock():
    pol = _ladder(mfe_lock_cost_floor_dollars=5.0)
    res = _eng("BUY", 3700.0, 3704.0, pol, atr=4.0)  # MFE 4.0 < floor + noise 10
    assert res["mfe_lock_fraction"] == pytest.approx(0.84)  # tier reached
    assert res["mfe_lock_sl"] is None                       # but not armed
    assert res["stop_source"] != "MFE_LOCK"
    # The lock only arms when the RETAINED profit still covers the cost floor, i.e.
    # when the peak leaves room for the noise floor AND the cost floor.
    ok = _eng("BUY", 3700.0, 3714.0, _ladder(mfe_lock_cost_floor_dollars=5.0), atr=4.0)  # MFE 14
    assert ok["mfe_lock_sl"] is not None


# ---------------------------------------------------------------------------
# 3. Hysteresis + ratchet + persistence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_hysteresis_atr_rise_no_tier_regression(direction):
    pol = _ladder()
    entry = 3700.0
    sign = 1.0 if direction == "BUY" else -1.0
    # Reached 85% at MFE 8 / ATR 4 (ratio 2.0). ATR rises to 12 -> ratio 0.667 (55% tier),
    # but the persisted 85% must hold.
    res = _eng(direction, entry, _live(entry, 8.0, direction), pol, atr=12.0, prev_pct=0.85, prev_profit=6.8)
    assert res["mfe_lock_fraction"] == pytest.approx(0.85)
    assert res["mfe_lock_profit"] == pytest.approx(6.8)
    assert res["mfe_lock_sl"] == pytest.approx(round(entry + sign * 6.8, 2))
    without = _eng(direction, entry, _live(entry, 8.0, direction), pol, atr=12.0)
    assert without["mfe_lock_fraction"] == pytest.approx(0.70)  # non-vacuous


def test_protected_profit_ratchet_never_decreases():
    res = _eng("BUY", 3700.0, 3708.0, _ladder(), atr=4.0, prev_pct=0.85, prev_profit=10.0)
    assert res["mfe_lock_profit"] == pytest.approx(10.0)  # ratchet holds prior high
    assert res["mfe_lock_sl"] == pytest.approx(3710.0)


def test_executor_persists_tier_and_survives_restart_and_tp_move(monkeypatch):
    monkeypatch.setattr(config, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    ex = _ex(4.0)
    sig = _sig(3700.0, 3690.0)
    ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 4.0, "BUY"))
    lv = sig["layers"]["L1"]
    assert lv["mfe_lock_pct_reached"] == pytest.approx(0.84)
    assert lv["mfe_lock_profit_reached"] == pytest.approx(3.0)
    assert lv["dynamic_atr_trail_sl"] == pytest.approx(3703.0)  # protection marker stamped
    peak = lv["dynamic_atr_trail_peak"]
    # TP management moves targets -> MFE/peak/tier must NOT reset.
    sig["tp_levels"] = {"TP1": 3705.0, "TP2": 3712.0}
    ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 4.0, "BUY"))
    assert lv["dynamic_atr_trail_peak"] == peak
    assert lv["mfe_lock_pct_reached"] == pytest.approx(0.84)
    # Simulate restart/reconnect: JSON round-trip the signal, fresh executor, ATR rises.
    restored = json.loads(json.dumps(sig))
    ex2 = _ex(12.0)
    ex2.evaluate_dynamic_atr_engine(restored, tick=_tick(3700.0, 4.0, "BUY"))
    lv2 = restored["layers"]["L1"]
    assert lv2["mfe_lock_pct_reached"] == pytest.approx(0.84)  # hysteresis across restart + ATR rise
    assert lv2["mfe_lock_profit_reached"] >= 3.0


def test_mfe_independent_of_tp_levels():
    pol = _ladder()
    a = _eng("BUY", 3700.0, 3706.0, pol, atr=4.0, tp={"TP1": 3710.0})
    b = _eng("BUY", 3700.0, 3706.0, pol, atr=4.0, tp={"TP1": 3702.0, "TP2": 3704.0}, milestones=["TP1"])
    assert a["mfe_lock_sl"] == b["mfe_lock_sl"]
    assert a["mfe_dollars"] == b["mfe_dollars"]


# ---------------------------------------------------------------------------
# 4. 2% Relative ATR adjustment
# ---------------------------------------------------------------------------

def _scale_pol(**kw):
    base = dict(mfe_lock_enabled=True, atr_baseline_dollars=4.0, scale_min=0.5, scale_max=3.0, relative_scale_mult=1.02)
    base.update(kw)
    return DynamicRiskPolicy(**base)


@pytest.mark.parametrize("atr,expected", [(4.0, 1.02), (6.0, 1.53), (8.0, 2.04)])
def test_relative_atr_exactly_two_percent(atr, expected):
    assert compute_volatility_ratio(atr, _scale_pol()) == pytest.approx(expected)


def test_relative_atr_default_unchanged_and_disable():
    assert compute_volatility_ratio(4.0, DynamicRiskPolicy()) == pytest.approx(1.0)  # opt-in off
    assert compute_volatility_ratio(6.0, _scale_pol(relative_scale_mult=1.0)) == pytest.approx(1.5)  # disabled


def test_relative_atr_idempotent_no_double_application():
    pol = _scale_pol()
    first = compute_volatility_ratio(6.0, pol)
    for _ in range(5):
        assert compute_volatility_ratio(6.0, pol) == pytest.approx(first)
    # Repeated SL/TP scaling (reconnect) must not compound the multiplier.
    s1 = dr.scale_sl_tp_by_atr("BUY", 3700.0, 3700.0, 6.0, pol, sl=3690.0, tp_levels={"TP1": 3706.0})
    s2 = dr.scale_sl_tp_by_atr("BUY", 3700.0, 3700.0, 6.0, pol, sl=3690.0, tp_levels={"TP1": 3706.0})
    assert s1["vol_ratio"] == s2["vol_ratio"] == pytest.approx(1.53)


# ---------------------------------------------------------------------------
# 5. Auto 30-pip break-even (single picker, never loosens)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_auto_be_arms_at_30_pips(direction):
    entry = 3700.0
    res = _eng(direction, entry, _live(entry, 3.0, direction), _pol(), atr=9.78, auto_be=3.0)
    assert res["auto_be_armed"] is True
    assert res["auto_be_sl"] == pytest.approx(entry)
    below = _eng(direction, entry, _live(entry, 2.99, direction), _pol(), atr=9.78, auto_be=3.0)
    assert below["auto_be_armed"] is False


def test_auto_be_never_loosens_tighter_be_lock():
    # ATR BE locks entry+0.50 (tighter than auto-BE entry); picker keeps the tighter.
    res = _eng("BUY", 3700.0, 3704.0, _pol(), atr=4.0, auto_be=3.0)
    assert res["be_armed"] is True
    assert res["auto_be_armed"] is True
    assert res["stop_source"] == "BE_LOCK"
    assert res["candidate_sl"] == pytest.approx(3701.2)  # the $1.20 BE lock offset


def test_auto_be_independent_of_ladder():
    res = _eng("BUY", 3700.0, 3703.0, _pol(mfe_lock_enabled=False), atr=9.78, auto_be=3.0)
    assert res["auto_be_armed"] is True
    assert res["mfe_lock_sl"] is None
    assert res["stop_source"] == "AUTO_BE"


def test_executor_auto_be_applies_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(config, "DYNAMIC_ATR_AUTO_BE_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_AUTO_BE_ENABLED", True)
    ex = _ex(9.78)
    sig = _sig(3700.0, 3690.0)
    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        res = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 3.0, "BUY"))
    lv = sig["layers"]["L1"]
    assert lv["broker_sl"] == pytest.approx(3700.0)
    assert lv.get("dynamic_atr_trail_sl") == pytest.approx(3700.0)  # marker stamped for AUTO_BE
    assert any("[T3 AUTO-BE]" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Protection-marker gap + edit-loosening regression
# ---------------------------------------------------------------------------

def test_pre_tp1_mfe_lock_then_loosening_edit_is_dropped(monkeypatch):
    monkeypatch.setattr(config, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    ex = _ex(4.0)
    monkeypatch.setattr(main, "executor", ex)
    sig = _sig(3700.0, 3690.0)
    ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 4.0, "BUY"))
    lv = sig["layers"]["L1"]
    assert lv["dynamic_atr_trail_sl"] == pytest.approx(3703.0)
    assert sig.get("tp1_reached") in (None, False)  # pre-TP1
    # A loosening provider SL edit must NOT loosen the protective stop.
    assert main._resolve_edit_layer_sl(sig, lv, 3695.0, "BUY") is None
    assert main._resolve_edit_layer_sl(sig, lv, 3702.8, "BUY") is None  # tie also dropped
    # A tightening edit still passes.
    assert main._resolve_edit_layer_sl(sig, lv, 3703.5, "BUY") == pytest.approx(3703.5)


def test_edit_guard_live_broker_sl_fallback(monkeypatch):
    ex = _ex(4.0)
    monkeypatch.setattr(main, "executor", ex)
    # Tracked-state gap: layer broker_sl is loose, but the LIVE broker stop is protective.
    sig = _sig(3700.0, 3690.0)
    sig["status"] = "BE_APPLIED"
    lv = sig["layers"]["L1"]
    lv["broker_sl"] = 3690.0
    monkeypatch.setattr(ex, "_resolve_ticket_current_sl", lambda *a, **k: 3705.0)
    # Without the live fallback the floor would be 3690 and 3700 would pass (loosening).
    assert main._resolve_edit_layer_sl(sig, lv, 3700.0, "BUY") is None


def test_marker_stamped_for_mfe_lock_source(monkeypatch):
    monkeypatch.setattr(config, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    ex = _ex(4.0)
    sig = _sig(3700.0, 3690.0)
    res = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 4.0, "BUY"))
    assert res["updated_tickets"]
    assert sig["layers"]["L1"].get("dynamic_atr_trail_sl") is not None
    assert sig.get("active_stop_source") == "MFE_LOCK"


# ---------------------------------------------------------------------------
# 7. Ceilings + MODERATE distance tiers (retain from opt-in protection)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_trail_ceiling_arms_early(direction):
    entry = 3700.0
    base = _pol(trail_activation_atr_mult=0.70)
    tight = _pol(trail_activation_atr_mult=0.70, trail_activation_ceiling_dollars=4.0)
    d = _eng(direction, entry, _live(entry, 5.0, direction), base, atr=9.78)
    t = _eng(direction, entry, _live(entry, 5.0, direction), tight, atr=9.78)
    assert d["trail_armed"] is False
    assert t["trail_armed"] is True
    assert t["trail_activation_threshold"] == pytest.approx(4.0)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("r", [32.0, None])
def test_be_ceiling_overrides_r_gate(direction, r):
    entry = 3700.0
    sign = 1.0 if direction == "BUY" else -1.0
    d = _eng(direction, entry, _live(entry, 3.0, direction), _pol(), atr=9.78, r=r)
    t = _eng(direction, entry, _live(entry, 3.0, direction), _pol(be_activation_ceiling_dollars=3.0), atr=9.78, r=r)
    assert d["be_armed"] is False
    assert t["be_armed"] is True
    assert t["be_threshold"] == pytest.approx(3.0)
    assert t["be_sl"] == pytest.approx(round(entry + sign * 1.20, 2))  # the $1.20 BE lock offset


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_moderate_distance_tiers_monotonic(direction):
    pol = _pol(trail_distance_tiers=DIST_TIERS)
    entry = 3700.0
    dists = []
    for mfe in (6.0, 10.0, 15.0, 20.0):
        res = _eng(direction, entry, _live(entry, mfe, direction), pol, atr=9.78)
        dists.append(res["trail_dist"])
        assert res["distance_cap_applied"] is True
    assert dists == [3.50, 3.00, 2.75, 2.50]
    assert all(a >= b for a, b in zip(dists, dists[1:]))


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_distance_tiers_never_expand(direction):
    pol = _pol(trail_distance_tiers=DIST_TIERS)
    entry = 3700.0
    prev = None
    peak = None
    for mfe in [x * 0.5 for x in range(2, 41)]:
        res = _eng(direction, entry, _live(entry, mfe, direction), pol, atr=9.78, peak=peak)
        peak = res["peak_price"]
        if prev is not None:
            assert res["trail_dist"] <= prev + 1e-9
        prev = res["trail_dist"]


def test_distance_cap_not_applied_when_base_tighter():
    res = _eng("BUY", 3700.0, 3704.0, _pol(trail_distance_tiers=DIST_TIERS), atr=2.0)
    assert res["trail_dist"] == pytest.approx(2.8)
    assert res["distance_cap_applied"] is False


# ---------------------------------------------------------------------------
# 8. Defensive parsing + mutation
# ---------------------------------------------------------------------------

def test_ladder_parse_defensively():
    pol = _ladder(mfe_lock_tiers="abc,,0.4:-1,0.6:xyz, :0.5,1.00:0.70")
    res = _eng("BUY", 3700.0, 3704.0, pol, atr=4.0)  # only valid tier 1.00:0.70
    assert res["mfe_lock_fraction"] == pytest.approx(0.70)


def test_empty_ladder_disables():
    res = _eng("BUY", 3700.0, 3708.0, _ladder(mfe_lock_tiers=""), atr=4.0)
    assert res["mfe_lock_sl"] is None


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_each_knob_mutates_wide_stop_case(direction):
    entry = 3700.0
    atr = 9.78
    mfe = 6.0
    live = _live(entry, mfe, direction)
    default = _eng(direction, entry, live, _pol(), atr=atr, r=32.0)
    assert default["armed"] is False
    variants = {
        "trail_ceiling": _pol(trail_activation_ceiling_dollars=4.0),
        "be_ceiling": _pol(be_activation_ceiling_dollars=3.0),
        "distance_tiers": _pol(trail_distance_tiers=DIST_TIERS),
        "ladder": _ladder(),
        "auto_be": _pol(auto_be_enabled=True),
    }
    for name, pol in variants.items():
        ab = 3.0 if name == "auto_be" else None
        res = _eng(direction, entry, live, pol, atr=atr, r=32.0, auto_be=ab)
        assert res != default, f"{name} did not change output (vacuous)"


# ---------------------------------------------------------------------------
# 9. Propagation + registration completeness + shipped defaults
# ---------------------------------------------------------------------------

def _sentinel(knob, current):
    if knob in ("DYNAMIC_ATR_MFE_LOCK_ENABLED", "DYNAMIC_ATR_AUTO_BE_ENABLED", "DYNAMIC_ATR_INTERIM_FLOOR_ENABLED"):
        return not bool(current)
    if knob == "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS":
        return "6:3.50,10:3.00" if current != "6:3.50,10:3.00" else "5:2.00"
    if knob == "DYNAMIC_ATR_MFE_LOCK_TIERS":
        return "0.50:0.50,1.00:0.70" if current != "0.50:0.50,1.00:0.70" else "0.40:0.45"
    if knob == "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS":
        return 4.0 if current != 4.0 else 5.0
    if knob == "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS":
        return 3.0 if current != 3.0 else 4.0
    if knob == "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS":
        return 0.75 if current != 0.75 else 0.90
    if knob == "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS":
        return 1.5 if current != 1.5 else 2.0
    if knob == "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS":
        return 15.0 if current != 15.0 else 16.0
    if knob == "DYNAMIC_ATR_RELATIVE_SCALE_MULT":
        return 1.05 if current != 1.05 else 1.03
    if knob == "DYNAMIC_ATR_AUTO_BE_PIPS":
        return 25.0 if current != 25.0 else 35.0
    if knob == "DYNAMIC_ATR_AUTO_BE_PIP_SIZE_DOLLARS":
        return 0.20 if current != 0.20 else 0.10
    if knob == "DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT":
        return 1.05 if current != 1.05 else 1.10
    if knob == "DYNAMIC_ATR_INTERIM_FLOOR_K":
        return 0.75 if current != 0.75 else 0.60
    raise AssertionError(knob)


@pytest.mark.parametrize("target", ["config", "mt5_executor"])
@pytest.mark.parametrize("knob", sorted(NEW_KNOBS))
def test_new_knob_propagates(monkeypatch, knob, target):
    field = NEW_KNOBS[knob]
    current = getattr(config, knob)
    sentinel = _sentinel(knob, current)
    assert sentinel != current
    mod = config if target == "config" else mt5_executor
    monkeypatch.setattr(mod, knob, sentinel)
    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    assert getattr(pol, field) == sentinel, f"{knob} -> {field} via {target}"


def test_new_knobs_registered_everywhere(monkeypatch):
    captured = {}
    original = mt5_executor.read_dynamic_risk_policy

    def capture(proxy):
        captured["keys"] = set(vars(proxy))
        return original(proxy)

    monkeypatch.setattr(mt5_executor, "read_dynamic_risk_policy", capture)
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._get_dynamic_risk_policy()
    expected = set(NEW_KNOBS)
    config_set = {k for k in vars(config) if k in expected}
    proxy_set = {k for k in captured["keys"] if k in expected}
    snapshot_set = {k for k in mt5_executor._CFG_IMPORT_SNAPSHOT if k in expected}
    src = inspect.getsource(dr.read_dynamic_risk_policy)
    reader_set = {n for n in re.findall(r'getattr\(\s*cfg_mod\s*,\s*"([A-Za-z0-9_]+)"', src) if n in expected}
    assert config_set == proxy_set == snapshot_set == reader_set == expected


def test_shipped_defaults_match():
    pol = DynamicRiskPolicy()
    assert pol.mfe_lock_tiers == LADDER
    assert pol.mfe_lock_enabled is False
    assert pol.mfe_lock_atr_min_dollars == pytest.approx(1.0)
    assert pol.mfe_lock_atr_max_dollars == pytest.approx(12.0)
    assert pol.relative_scale_mult == pytest.approx(1.02)
    assert pol.auto_be_enabled is False
    assert pol.auto_be_pips == pytest.approx(30.0)
    assert pol.auto_be_pip_size_dollars == pytest.approx(0.0)
    assert pol.trail_distance_scale_mult == pytest.approx(1.0)
    assert pol.interim_floor_enabled is False
    assert pol.interim_floor_k == pytest.approx(0.50)
    assert dr.read_dynamic_risk_policy(SimpleNamespace()) == pol


def test_pip_convention_verified():
    ex = mt5_executor.MT5Executor(dry_run=True)
    assert ex.get_pip_size("XAUUSD.s") == pytest.approx(0.10)
    assert 30 * ex.get_pip_size("XAUUSD.s") == pytest.approx(3.00)
    assert config.BE_OFFSET_PIPS * ex.get_pip_size("XAUUSD.s") == pytest.approx(1.20)


@pytest.mark.parametrize("text", ["Go breakeven", "Set breakeven", "Set break even",
                                  "Break even all entries", "Set break even on best entries close worst"])
def test_explicit_be_variations_recognized(text):
    import parser
    lo = text.lower()
    assert any(re.search(p, lo) for p in parser.BREAKEVEN_PATTERNS)
    assert parser._is_unambiguous_breakeven_cmd(lo) is True


# ---------------------------------------------------------------------------
# 10. ATR trailing distance scale (5% wider, opt-in; ATR trail ONLY)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_scale_widen_five_percent(direction):
    entry = 3700.0
    sign = 1.0 if direction == "BUY" else -1.0
    base = _eng(direction, entry, _live(entry, 5.0, direction), _pol(), atr=4.0, r=32.0)
    scaled = _eng(direction, entry, _live(entry, 5.0, direction), _pol(trail_distance_scale_mult=1.05), atr=4.0, r=32.0)
    assert base["trail_dist"] == pytest.approx(5.6)
    assert base["trail_dist_original"] == pytest.approx(5.6)
    assert scaled["trail_dist_original"] == pytest.approx(5.6)
    assert scaled["trail_dist"] == pytest.approx(round(5.6 * 1.05, 2))  # 5.88
    assert scaled["atr_sl"] == pytest.approx(round(entry + sign * 5.0 - sign * round(5.6 * 1.05, 2), 2))
    assert sign * (scaled["atr_sl"] - base["atr_sl"]) < 0  # further from peak
    assert scaled["stop_source"] == "ATR_TRAIL"
    assert scaled != base  # non-vacuous


def test_scale_widens_even_when_cap_binds():
    pol = _pol(trail_distance_tiers=DIST_TIERS, trail_distance_scale_mult=1.05)
    res = _eng("BUY", 3700.0, 3706.0, pol, atr=9.78)  # MFE 6 -> cap 3.50 binds
    assert res["distance_cap_applied"] is True
    assert res["trail_dist_original"] == pytest.approx(3.50)
    assert res["trail_dist"] == pytest.approx(round(3.50 * 1.05, 2))
    assert res["trail_dist"] > 3.50  # widened despite the cap


def test_scale_default_byte_identical():
    entry = 3700.0
    for direction in ("BUY", "SELL"):
        for atr, mfe in [(4.0, 5.0), (9.78, 12.0), (2.0, 3.0)]:
            a = _eng(direction, entry, _live(entry, mfe, direction), _pol(), atr=atr, r=8.0)
            b = _eng(direction, entry, _live(entry, mfe, direction), _pol(trail_distance_scale_mult=1.0), atr=atr, r=8.0)
            assert a == b
            assert a["trail_dist"] == a["trail_dist_original"]


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_scale_leaves_mfe_activation_autobe_unchanged(direction):
    entry = 3700.0
    live = _live(entry, 6.0, direction)
    a = _eng(direction, entry, live, _ladder(), atr=4.0, auto_be=3.0)
    b = _eng(direction, entry, live, _ladder(trail_distance_scale_mult=1.05), atr=4.0, auto_be=3.0)
    for key in ("mfe_lock_fraction", "mfe_lock_profit", "mfe_lock_sl", "mfe_lock_ratio", "mfe_lock_tier",
                "trail_activation_threshold", "be_threshold", "be_sl", "auto_be_sl", "auto_be_armed"):
        assert a[key] == b[key], "scale changed " + key
    assert a["trail_dist"] != b["trail_dist"]  # only the ATR trail distance differs


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_scale_monotonic_with_distance_tiers(direction):
    pol = _pol(trail_distance_tiers=DIST_TIERS, trail_distance_scale_mult=1.05)
    entry = 3700.0
    prev = None
    for mfe in (6.0, 10.0, 15.0, 20.0):
        res = _eng(direction, entry, _live(entry, mfe, direction), pol, atr=9.78)
        assert res["trail_dist"] == pytest.approx(round(res["trail_dist_original"] * 1.05, 2))
        if prev is not None:
            assert res["trail_dist"] <= prev + 1e-9
        prev = res["trail_dist"]


def test_scale_never_loosens_tighter_mfe_lock():
    pol = _ladder(trail_distance_scale_mult=1.05)
    res = _eng("BUY", 3700.0, 3706.0, pol, atr=4.0)
    assert res["stop_source"] == "MFE_LOCK"
    assert res["candidate_sl"] == pytest.approx(res["mfe_lock_sl"])  # 3704.8, not the wider trail
    assert res["atr_sl"] < res["candidate_sl"]


def test_scale_step_guard_blocks_loosening_existing_stop():
    pol = _pol(trail_distance_scale_mult=1.05)
    res = _eng("BUY", 3700.0, 3706.0, pol, atr=4.0, r=32.0, sl=3705.0)
    assert res["should_update"] is False


def test_scale_atr_rise_cannot_loosen_established_stop():
    pol = _pol(trail_distance_scale_mult=1.05)
    entry = 3700.0
    first = _eng("BUY", entry, 3708.0, pol, atr=4.0, r=32.0)
    established = first["candidate_sl"]
    assert first["should_update"] is True
    second = _eng("BUY", entry, 3708.0, pol, atr=8.0, r=32.0, peak=3708.0, sl=established)
    assert second["atr_sl"] < established
    assert second["should_update"] is False


# ---------------------------------------------------------------------------
# 11. Consolidated per-component binding-SL log (logging only, opt-in)
# ---------------------------------------------------------------------------

def test_binding_sl_log_lists_components_and_winner(monkeypatch, caplog):
    monkeypatch.setattr(config, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_MFE_LOCK_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_ATR_AUTO_BE_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_AUTO_BE_ENABLED", True)
    ex = _ex(4.0)
    sig = _sig(3700.0, 3690.0)
    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 6.0, "BUY"))
    lines = [r.getMessage() for r in caplog.records if "[T3 BINDING-SL]" in r.getMessage()]
    assert lines, "consolidated binding-SL log must be emitted when armed + opt-in"
    line = lines[-1]
    for label in ("ATR_TRAIL=", "MFE_LOCK=", "R_LOCK=", "BE_LOCK=", "AUTO_BE=", "MILESTONE=", "WINNER=", "broker_sl="):
        assert label in line, "missing component label " + label
    assert "WINNER=MFE_LOCK" in line
    assert "broker_sl=3705.0" in line


def test_binding_sl_log_silent_at_defaults(caplog):
    ex = _ex(4.0)
    sig = _sig(3700.0, 3690.0)
    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 6.0, "BUY"))
    assert not any("[T3 BINDING-SL]" in r.getMessage() for r in caplog.records)


def test_binding_sl_helper_formats_absent_as_dash(caplog):
    ex = mt5_executor.MT5Executor(dry_run=True)
    ts = {"atr_sl": 3700.4, "mfe_lock_sl": 3704.8, "mfe_lock_tier": 1.5, "mfe_lock_fraction": 0.80,
          "r_lock_floor": None, "be_sl": 3700.5, "auto_be_sl": 3700.0, "milestone_floor": None,
          "stop_source": "MFE_LOCK", "candidate_sl": 3704.8}
    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        ex._log_t3_binding_sl("L1", 101, ts)
    line = [r.getMessage() for r in caplog.records if "[T3 BINDING-SL]" in r.getMessage()][-1]
    assert "R_LOCK=-" in line and "MILESTONE=-" in line
    assert "ATR_TRAIL=3700.4" in line and "MFE_LOCK=3704.8" in line
    assert "BE_LOCK=3700.5" in line and "AUTO_BE=3700.0" in line
    assert "tier=1.50, pct=0.80" in line
    assert "WINNER=MFE_LOCK -> broker_sl=3704.8" in line


# ---------------------------------------------------------------------------
# 12. ATR-scaled interim fallback floor (research ifk50, opt-in)
# ---------------------------------------------------------------------------

def _ifpol(k=0.50, **kw):
    base = dict(interim_floor_enabled=True, interim_floor_k=k)
    base.update(kw)
    return DynamicRiskPolicy(**base)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_interim_floor_arms_at_mfe_ge_k_atr(direction):
    entry = 3700.0
    sign = 1.0 if direction == "BUY" else -1.0
    atr = 4.0  # k=0.5 -> lvl 2.0
    armed = _eng(direction, entry, _live(entry, 2.0, direction), _ifpol(), atr=atr, r=32.0)
    below = _eng(direction, entry, _live(entry, 1.99, direction), _ifpol(), atr=atr, r=32.0)
    assert armed["interim_floor_armed"] is True
    assert armed["interim_floor_sl"] == pytest.approx(round(entry + sign * 2.0, 2))
    assert below["interim_floor_armed"] is False
    assert below["interim_floor_sl"] is None


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_interim_floor_matches_research_ifk50(direction):
    # Research k-mode: lvl = k * trail_ATR (no bound), floor = round(entry + sign*lvl, 2).
    entry = 3600.0
    atr = 12.0
    k = 0.50
    sign = 1.0 if direction == "BUY" else -1.0
    mfe = k * atr  # 6.0 -> peak reached the floor
    res = _eng(direction, entry, _live(entry, mfe, direction), _ifpol(k), atr=atr, r=32.0)
    expected = round(entry + sign * (k * round(atr, 4)), 2)
    assert res["interim_floor_sl"] == pytest.approx(expected)
    assert res["interim_floor_armed"] is True
    assert res["stop_source"] == "INTERIM_FLOOR"
    assert res["candidate_sl"] == pytest.approx(expected)


def test_interim_floor_binds_only_when_tighter_than_ladder():
    entry = 3700.0
    # MFE 2 / ATR 4: ladder 70% of 2.00 = 1.40, but the noise floor (the larger of
    # the $1.00 cost floor and the smaller of 0.25 * ATR = 1.00 / 35% of the peak =
    # 0.70) caps the retained profit at 1.00; floor k0.5 -> 2.00 (tighter for BUY).
    res = _eng("BUY", entry, 3702.0, _ifpol(0.50, mfe_lock_enabled=True), atr=4.0)
    assert res["mfe_lock_sl"] == pytest.approx(3701.0)
    assert res["interim_floor_sl"] == pytest.approx(3702.0)
    assert res["stop_source"] == "INTERIM_FLOOR"
    assert res["candidate_sl"] == pytest.approx(3702.0)


def test_interim_floor_never_loosens_tighter_mfe_lock():
    entry = 3700.0
    # MFE 8 / ATR 4: ladder 91% of 8.00 = 7.28, capped by the noise floor
    # (0.25 x ATR = 1.00) to 7.00; floor 2.00 is looser and must NOT win.
    res = _eng("BUY", entry, 3708.0, _ifpol(0.50, mfe_lock_enabled=True), atr=4.0)
    assert res["mfe_lock_sl"] == pytest.approx(3707.0)
    assert res["interim_floor_sl"] == pytest.approx(3702.0)
    assert res["stop_source"] == "MFE_LOCK"
    assert res["candidate_sl"] == pytest.approx(3707.0)


def test_interim_floor_step_guard_blocks_loosening():
    res = _eng("BUY", 3700.0, 3702.0, _ifpol(), atr=4.0, r=32.0, sl=3703.0)
    assert res["interim_floor_armed"] is True
    assert res["should_update"] is False  # existing 3703 tighter than floor 3702


def test_interim_floor_leaves_ladder_and_activation_unchanged():
    entry = 3700.0
    live = 3706.0
    a = _eng("BUY", entry, live, _ladder(), atr=4.0, auto_be=3.0)
    b = _eng("BUY", entry, live, _ladder(interim_floor_enabled=True), atr=4.0, auto_be=3.0)
    for key in ("mfe_lock_fraction", "mfe_lock_profit", "mfe_lock_sl", "mfe_lock_ratio", "mfe_lock_tier",
                "trail_activation_threshold", "be_threshold", "be_sl", "auto_be_sl", "auto_be_armed",
                "trail_dist", "atr_sl"):
        assert a[key] == b[key], "interim floor changed " + key


def test_interim_floor_default_off_byte_identical():
    entry = 3700.0
    for direction in ("BUY", "SELL"):
        for atr, mfe in [(4.0, 2.0), (9.78, 6.0), (12.0, 8.0)]:
            a = _eng(direction, entry, _live(entry, mfe, direction), _pol(), atr=atr, r=8.0)
            b = _eng(direction, entry, _live(entry, mfe, direction), _pol(interim_floor_enabled=False), atr=atr, r=8.0)
            assert a == b
            assert a["interim_floor_sl"] is None and a["interim_floor_armed"] is False


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_interim_floor_k_configurable(direction):
    entry = 3700.0
    sign = 1.0 if direction == "BUY" else -1.0
    for k in (0.25, 0.50, 1.00):
        res = _eng(direction, entry, _live(entry, 4.0, direction), _ifpol(k), atr=4.0, r=32.0)
        assert res["interim_floor_sl"] == pytest.approx(round(entry + sign * (k * 4.0), 2))
    low = _eng(direction, entry, _live(entry, 4.0, direction), _ifpol(0.25), atr=4.0, r=32.0)
    high = _eng(direction, entry, _live(entry, 4.0, direction), _ifpol(1.00), atr=4.0, r=32.0)
    assert low["interim_floor_sl"] != high["interim_floor_sl"]


def test_interim_floor_respects_stops_buffer():
    # Floor winner is clamped against live price by the existing stops-buffer clamp.
    res = _eng("BUY", 3700.0, 3702.0, _ifpol(), atr=4.0, r=32.0, buf=0.5)
    assert res["stop_source"] == "INTERIM_FLOOR"
    assert res["candidate_sl"] == pytest.approx(3701.5)  # min(3702, live 3702 - 0.5)


def test_interim_floor_non_vacuous_mutation():
    entry = 3700.0
    default = _eng("BUY", entry, 3702.0, _pol(), atr=4.0, r=32.0)
    enabled = _eng("BUY", entry, 3702.0, _ifpol(), atr=4.0, r=32.0)
    assert default["armed"] is False
    assert enabled["armed"] is True
    assert enabled != default


def test_executor_interim_floor_applies_logs_and_marks(monkeypatch, caplog):
    monkeypatch.setattr(config, "DYNAMIC_ATR_INTERIM_FLOOR_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_INTERIM_FLOOR_ENABLED", True)
    ex = _ex(4.0)
    sig = _sig(3700.0, 3690.0)
    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3700.0, 2.0, "BUY"))
    lv = sig["layers"]["L1"]
    assert 3690.0 < lv["broker_sl"] <= 3702.0  # protective; clamped by the existing stops-buffer
    assert lv.get("dynamic_atr_trail_sl") == lv["broker_sl"]  # protection marker stamped
    assert sig.get("active_stop_source") == "INTERIM_FLOOR"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[T3 INTERIM-FLOOR]" in m and "proposed_sl=3702.0" in m for m in msgs)
    assert any("[T3 BINDING-SL]" in m and "INTERIM_FLOOR=" in m and "WINNER=INTERIM_FLOOR" in m for m in msgs)
