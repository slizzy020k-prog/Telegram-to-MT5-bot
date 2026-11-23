"""T3 aggressive profit protection (opt-in) -- engine + propagation tests.

Every new knob defaults to OFF / current-behavior. These tests prove:
  * baseline unchanged: with default knobs the classic engine output and the new
    logging keys stay neutral across a BUY/SELL grid (oracle + invariant checks);
  * trail activation ceiling arms the trail earlier on a high-ATR wide-stop case;
  * BE activation ceiling overrides the R-gate (arms before 0.5R), opt-in only;
  * distance tiers tighten trail_dist monotonically (never expand), BUY/SELL;
  * fractional-MFE lock: non-decreasing fraction, cost-floor arming, forward-only,
    and the single tightest-stop picker (R_LOCK > ATR_TRAIL > MFE_LOCK > BE_LOCK);
  * defensive tier parsing; non-vacuous mutation (each knob changes behavior);
  * propagation of all six knobs to _get_dynamic_risk_policy via config AND
    mt5_executor monkeypatches, plus registration in snapshot/proxy/reader.

Hermetic: engine tests build DynamicRiskPolicy directly (no ambient .env dependence).
"""
from __future__ import annotations

import inspect
import logging
import re
from types import SimpleNamespace

import pytest

import config
import dynamic_risk as dr
import mt5_executor
from dynamic_risk import DynamicRiskPolicy, compute_dynamic_atr_trail_stop

DIST_TIERS = "4:3.50,6:3.00,10:2.50,18:2.00"
MFE_TIERS = "4:0.30,6:0.50,10:0.65,18:0.75"

NEW_KNOBS = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": "trail_activation_ceiling_dollars",
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": "be_activation_ceiling_dollars",
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "trail_distance_tiers",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": "mfe_lock_enabled",
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "mfe_lock_tiers",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": "mfe_lock_cost_floor_dollars",
}


def _pol(**kw) -> DynamicRiskPolicy:
    return DynamicRiskPolicy(**kw)


def _t3(**kw) -> DynamicRiskPolicy:
    base = dict(
        trail_activation_ceiling_dollars=4.00,
        be_activation_ceiling_dollars=3.00,
        trail_distance_tiers=DIST_TIERS,
        mfe_lock_enabled=True,
        mfe_lock_tiers=MFE_TIERS,
        mfe_lock_cost_floor_dollars=0.60,
    )
    base.update(kw)
    return DynamicRiskPolicy(**base)


def _eng(direction, entry, live, pol, atr=9.78, peak=None, sl=None, r=None,
         buf=0.0, milestones=None, tp=None, r_lock_floor=None):
    return compute_dynamic_atr_trail_stop(
        direction=direction, entry_price=entry, live_price=live, prev_peak=peak,
        current_sl=sl, atr=atr, policy=pol, digits=2, stops_buffer=buf,
        cleared_milestones=milestones, tp_levels=tp, initial_r=r, r_lock_floor=r_lock_floor,
    )


def _live_for(entry, mfe, direction):
    return entry + mfe if direction == "BUY" else entry - mfe


# ---------------------------------------------------------------------------
# 1. Baseline unchanged at defaults (BUY/SELL grid)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("atr,mfe,r", [(4.0, 5.0, None), (4.0, 12.0, 8.0), (9.78, 5.0, 32.0),
                                       (2.0, 3.0, None), (6.0, 20.0, 10.0), (9.78, 1.0, 32.0)])
def test_baseline_new_keys_neutral_and_thresholds_match(direction, atr, mfe, r):
    pol = _pol()
    entry = 3700.0
    live = _live_for(entry, mfe, direction)
    res = _eng(direction, entry, live, pol, atr=atr, r=r)
    sign = 1.0 if direction == "BUY" else -1.0
    # New logging keys are neutral with every knob at its default.
    assert res["mfe_lock_sl"] is None
    assert res["mfe_lock_fraction"] == 0.0
    assert res["distance_cap_applied"] is False
    assert res["trail_activation_threshold"] == pytest.approx(round(pol.trail_activation_atr_mult * atr, 4))
    if r is not None and r > 0:
        exp_be = max(0.5 * r, pol.be_activation_atr_mult * atr, pol.trail_min_dollars)
    else:
        exp_be = max(pol.be_activation_atr_mult * atr, pol.be_min_mfe_dollars)
    assert res["be_threshold"] == pytest.approx(round(exp_be, 4))
    # Classic semantics preserved.
    if res["be_armed"]:
        assert res["be_sl"] == pytest.approx(round(entry + sign * pol.be_lock_offset_dollars, 2))
    if res["trail_armed"]:
        assert res["atr_sl"] == pytest.approx(round(res["peak_price"] - sign * res["trail_dist"], 2))


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_baseline_picker_oracle_beats_trail_when_tighter(direction):
    # mfe 5, atr 4: BE lock (entry+/-0.5) is tighter than the ATR trail (peak-/+5.6).
    pol = _pol()
    entry = 3700.0
    live = _live_for(entry, 5.0, direction)
    res = _eng(direction, entry, live, pol, atr=4.0)
    sign = 1.0 if direction == "BUY" else -1.0
    assert res["be_armed"] is True and res["trail_armed"] is True
    assert res["stop_source"] == "BE_LOCK"
    assert res["candidate_sl"] == pytest.approx(round(entry + sign * 0.5, 2))
    assert res["should_update"] is True


def test_baseline_disabled_path_has_neutral_new_keys():
    res = _eng("BUY", 3700.0, 3705.0, _pol(trail_enabled=False), atr=4.0)
    assert res["armed"] is False
    assert res["mfe_lock_sl"] is None and res["mfe_lock_fraction"] == 0.0
    assert res["trail_activation_threshold"] is None and res["be_threshold"] is None
    assert res["distance_cap_applied"] is False


# ---------------------------------------------------------------------------
# 2. Trail activation ceiling arms earlier on high ATR
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_trail_ceiling_arms_between_ceiling_and_atr_multiple(direction):
    entry = 3700.0
    atr = 9.78
    mfe = 5.0  # ceiling 4 < mfe 5 < 0.70*ATR = 6.846 (t3 activation mult)
    live = _live_for(entry, mfe, direction)
    base = _pol(trail_activation_atr_mult=0.70)
    tight = _pol(trail_activation_atr_mult=0.70, trail_activation_ceiling_dollars=4.00)
    r_default = _eng(direction, entry, live, base, atr=atr)
    r_tight = _eng(direction, entry, live, tight, atr=atr)
    assert r_default["trail_armed"] is False
    assert r_default["atr_sl"] is None
    assert r_tight["trail_armed"] is True
    assert r_tight["trail_activation_threshold"] == pytest.approx(4.0)
    assert r_tight["atr_sl"] is not None
    # Ceiling never raises the threshold above the ATR multiple (only lowers it).
    low_atr = _eng(direction, entry, _live_for(entry, 5.0, direction),
                   _pol(trail_activation_atr_mult=0.70, trail_activation_ceiling_dollars=4.00), atr=2.0)
    assert low_atr["trail_activation_threshold"] == pytest.approx(round(0.70 * 2.0, 4))


# ---------------------------------------------------------------------------
# 3. BE activation ceiling overrides the R-gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("r", [32.0, None])
def test_be_ceiling_overrides_r_gate(direction, r):
    entry = 3700.0
    atr = 9.78
    mfe = 3.0
    live = _live_for(entry, mfe, direction)
    sign = 1.0 if direction == "BUY" else -1.0
    default = _eng(direction, entry, live, _pol(), atr=atr, r=r)
    tight = _eng(direction, entry, live, _pol(be_activation_ceiling_dollars=3.00), atr=atr, r=r)
    # Default stays R-gated (0.5R = 16) or flat (max(7.335, 3.5)); mfe 3 arms neither.
    assert default["be_armed"] is False
    assert default["be_sl"] is None
    # Ceiling 3 arms BE at mfe 3, before 0.5R, locking at the classic offset.
    assert tight["be_armed"] is True
    assert tight["be_threshold"] == pytest.approx(3.0)
    assert tight["be_sl"] == pytest.approx(round(entry + sign * 0.5, 2))


# ---------------------------------------------------------------------------
# 4. Distance tiers: monotonic tightening, never expands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_distance_tiers_monotonic_non_increasing(direction):
    pol = _pol(trail_distance_tiers=DIST_TIERS)
    entry = 3700.0
    dists = []
    for mfe in (4.0, 6.0, 10.0, 18.0):
        res = _eng(direction, entry, _live_for(entry, mfe, direction), pol, atr=9.78)
        dists.append(res["trail_dist"])
        assert res["distance_cap_applied"] is True
    assert dists == [3.50, 3.00, 2.50, 2.00]
    assert all(a >= b for a, b in zip(dists, dists[1:]))


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_distance_tiers_never_expand_on_rising_mfe(direction):
    pol = _pol(trail_distance_tiers=DIST_TIERS)
    entry = 3700.0
    prev = None
    peak = None
    for mfe in [x * 0.5 for x in range(2, 41)]:  # 1.0 .. 20.0 rising
        live = _live_for(entry, mfe, direction)
        res = _eng(direction, entry, live, pol, atr=9.78, peak=peak)
        peak = res["peak_price"]
        d = res["trail_dist"]
        if prev is not None:
            assert d <= prev + 1e-9
        prev = d


def test_distance_cap_not_applied_when_base_already_tighter():
    pol = _pol(trail_distance_tiers=DIST_TIERS)
    res = _eng("BUY", 3700.0, 3704.0, pol, atr=2.0)  # base 2.8 < cap 3.5
    assert res["trail_dist"] == pytest.approx(2.8)
    assert res["distance_cap_applied"] is False


# ---------------------------------------------------------------------------
# 5. Fractional-MFE lock
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_mfe_lock_wins_picker_when_tightest(direction):
    pol = _pol(mfe_lock_enabled=True)
    entry = 3700.0
    live = _live_for(entry, 4.0, direction)
    res = _eng(direction, entry, live, pol, atr=4.0)
    sign = 1.0 if direction == "BUY" else -1.0
    assert res["mfe_lock_fraction"] == pytest.approx(0.30)
    assert res["mfe_lock_sl"] == pytest.approx(round(entry + sign * 1.20, 2))
    assert res["stop_source"] == "MFE_LOCK"
    assert res["candidate_sl"] == pytest.approx(round(entry + sign * 1.20, 2))


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_mfe_lock_below_cost_floor_not_armed(direction):
    pol = _pol(mfe_lock_enabled=True, mfe_lock_cost_floor_dollars=2.00)
    entry = 3700.0
    res = _eng(direction, entry, _live_for(entry, 4.0, direction), pol, atr=4.0)
    assert res["mfe_lock_sl"] is None
    assert res["mfe_lock_fraction"] == 0.0
    assert res["stop_source"] != "MFE_LOCK"


def test_mfe_lock_below_first_tier_not_armed():
    pol = _pol(mfe_lock_enabled=True)
    res = _eng("BUY", 3700.0, 3703.0, pol, atr=4.0)  # mfe 3 < first tier 4
    assert res["mfe_lock_sl"] is None
    assert res["mfe_lock_fraction"] == 0.0


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_mfe_lock_fraction_and_sl_forward_only(direction):
    pol = _pol(mfe_lock_enabled=True)
    entry = 3700.0
    sign = 1.0 if direction == "BUY" else -1.0
    fracs, sls = [], []
    for mfe in (4.0, 6.0, 10.0, 18.0):
        res = _eng(direction, entry, _live_for(entry, mfe, direction), pol, atr=2.0)
        fracs.append(res["mfe_lock_fraction"])
        sls.append(res["mfe_lock_sl"])
        assert res["mfe_lock_sl"] == pytest.approx(round(entry + sign * round(res["mfe_lock_fraction"] * mfe, 2), 2))
    assert fracs == [0.30, 0.50, 0.65, 0.75]
    assert all(a <= b for a, b in zip(fracs, fracs[1:]))
    if direction == "BUY":
        assert all(a <= b for a, b in zip(sls, sls[1:]))
    else:
        assert all(a >= b for a, b in zip(sls, sls[1:]))


def test_picker_r_lock_beats_mfe_lock():
    pol = _pol(mfe_lock_enabled=True)
    entry = 3700.0
    res = _eng("BUY", entry, 3710.0, pol, atr=4.0, r=8.0, r_lock_floor=3709.0)
    assert res["stop_source"] == "R_LOCK"
    assert res["r_lock_floor"] == pytest.approx(3709.0)
    assert res["candidate_sl"] == pytest.approx(3709.0)
    assert res["mfe_lock_sl"] is not None  # lock computed but lost the picker


def test_mfe_lock_disabled_by_default_no_candidate():
    res = _eng("BUY", 3700.0, 3704.0, _pol(), atr=4.0)
    assert res["mfe_lock_sl"] is None
    assert res["stop_source"] != "MFE_LOCK"


# ---------------------------------------------------------------------------
# 6. Defensive tier parsing
# ---------------------------------------------------------------------------

def test_distance_tiers_parse_defensively():
    pol = _pol(trail_distance_tiers="abc,,4:-1,6:xyz, :3,10:2.50")
    res = _eng("BUY", 3700.0, 3710.0, pol, atr=9.78)  # mfe 10 -> only valid tier 10:2.5
    assert res["trail_dist"] == pytest.approx(2.50)
    assert res["distance_cap_applied"] is True


def test_mfe_lock_fraction_clamped_to_one():
    pol = _pol(mfe_lock_enabled=True, mfe_lock_tiers="4:0.30,6:2.00")
    res = _eng("BUY", 3700.0, 3706.0, pol, atr=2.0)  # mfe 6 -> fraction clamped 1.0
    assert res["mfe_lock_fraction"] == pytest.approx(1.0)
    assert res["mfe_lock_sl"] == pytest.approx(3706.0)


def test_empty_tiers_disable_features():
    res = _eng("BUY", 3700.0, 3710.0, _pol(trail_distance_tiers="", mfe_lock_enabled=True, mfe_lock_tiers=""), atr=9.78)
    assert res["distance_cap_applied"] is False
    assert res["mfe_lock_sl"] is None


# ---------------------------------------------------------------------------
# 7. Non-vacuous mutation: each knob changes behavior on the wide-stop case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_each_knob_mutates_wide_stop_case(direction):
    entry = 3700.0
    atr = 9.78
    mfe = 5.0
    r = 32.0
    live = _live_for(entry, mfe, direction)
    default = _eng(direction, entry, live, _pol(), atr=atr, r=r)
    assert default["armed"] is False and default["candidate_sl"] is None
    variants = {
        "trail_ceiling": _pol(trail_activation_ceiling_dollars=4.00),
        "be_ceiling": _pol(be_activation_ceiling_dollars=3.00),
        "distance_tiers": _pol(trail_distance_tiers=DIST_TIERS),
        "mfe_lock": _pol(mfe_lock_enabled=True),
    }
    for name, pol in variants.items():
        res = _eng(direction, entry, live, pol, atr=atr, r=r)
        assert res != default, f"{name} knob did not change engine output (vacuous)"
    # Full T3 arms protection earlier: a concrete protective stop appears.
    full = _eng(direction, entry, live, _t3(), atr=atr, r=r)
    sign = 1.0 if direction == "BUY" else -1.0
    assert full["armed"] is True
    assert full["be_armed"] is True and full["trail_armed"] is True
    assert full["candidate_sl"] == pytest.approx(round(entry + sign * 1.50, 2))


# ---------------------------------------------------------------------------
# 8. Propagation to _get_dynamic_risk_policy + registration completeness
# ---------------------------------------------------------------------------

def _sentinel(knob, current):
    if knob == "DYNAMIC_ATR_MFE_LOCK_ENABLED":
        return not bool(current)
    if knob == "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS":
        return "4:3.50,6:3.00" if current != "4:3.50,6:3.00" else "5:2.00"
    if knob == "DYNAMIC_ATR_MFE_LOCK_TIERS":
        return "4:0.40,6:0.60" if current != "4:0.40,6:0.60" else "5:0.50"
    if knob == "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS":
        return 4.0 if current != 4.0 else 5.0
    if knob == "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS":
        return 3.0 if current != 3.0 else 4.0
    if knob == "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS":
        return 0.75 if current != 0.75 else 0.90
    raise AssertionError(knob)


@pytest.mark.parametrize("target", ["config", "mt5_executor"])
@pytest.mark.parametrize("knob", sorted(NEW_KNOBS))
def test_new_knob_propagates_to_policy(monkeypatch, knob, target):
    field = NEW_KNOBS[knob]
    current = getattr(config, knob)
    sentinel = _sentinel(knob, current)
    assert sentinel != current
    mod = config if target == "config" else mt5_executor
    monkeypatch.setattr(mod, knob, sentinel)
    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    assert getattr(pol, field) == sentinel, f"{knob} -> {field} via {target}"


def test_new_knobs_registered_in_all_four_registries(monkeypatch):
    captured = {}
    original = mt5_executor.read_dynamic_risk_policy

    def capture(proxy):
        captured["keys"] = set(vars(proxy))
        return original(proxy)

    monkeypatch.setattr(mt5_executor, "read_dynamic_risk_policy", capture)
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._get_dynamic_risk_policy()
    config_set = {k for k in vars(config) if k in NEW_KNOBS}
    proxy_set = {k for k in captured["keys"] if k in NEW_KNOBS}
    snapshot_set = {k for k in mt5_executor._CFG_IMPORT_SNAPSHOT if k in NEW_KNOBS}
    src = inspect.getsource(dr.read_dynamic_risk_policy)
    reader_set = {n for n in re.findall(r'getattr\(\s*cfg_mod\s*,\s*"([A-Za-z0-9_]+)"', src) if n in NEW_KNOBS}
    expected = set(NEW_KNOBS)
    assert config_set == proxy_set == snapshot_set == reader_set == expected


def test_new_policy_defaults_match_shipped():
    pol = DynamicRiskPolicy()
    assert pol.trail_activation_ceiling_dollars == 0.0
    assert pol.be_activation_ceiling_dollars == 0.0
    assert pol.trail_distance_tiers == ""
    assert pol.mfe_lock_enabled is False
    assert pol.mfe_lock_tiers == MFE_TIERS
    assert pol.mfe_lock_cost_floor_dollars == pytest.approx(0.60)
    # Reader fallback on an empty namespace agrees with the dataclass defaults.
    assert dr.read_dynamic_risk_policy(SimpleNamespace()) == pol


def test_executor_t3_logging_helper_is_reporting_only(caplog):
    ex = mt5_executor.MT5Executor(dry_run=True)
    lv = {}
    trail_step = {
        "mfe_dollars": 5.0, "be_armed": True, "be_threshold": 3.0, "be_sl": 3700.5,
        "trail_armed": True, "trail_activation_threshold": 4.0, "trail_zone": "STANDARD",
        "trail_dist": 3.5, "distance_cap_applied": True, "mfe_lock_fraction": 0.30,
        "mfe_lock_sl": 3701.5,
    }
    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        ex._log_t3_protection("L1", 123, lv, trail_step, _t3(), 2)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[T3 BE-ARM]" in m for m in msgs)
    assert any("[T3 TRAIL-ARM]" in m for m in msgs)
    assert any("[T3 MFE-LOCK]" in m for m in msgs)
    assert lv.get("_t3_be_logged") is True
