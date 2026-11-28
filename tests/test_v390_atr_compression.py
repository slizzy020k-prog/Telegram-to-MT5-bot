"""v3.9.0 per-stage ATR trailing compression clamps (quant-lead / risk-guard).

Stage A (STANDARD, MFE < $10):   1.40x M5 ATR clamp [$2.50, $8.00]
Stage B (EXTENDED, $10..<$18):   1.00x M5 ATR clamp [$2.20, $5.50]
Stage C (HOME_RUN, MFE >= $18):  0.80x M5 ATR clamp [$2.00, $4.00]

Policies are built from an explicit spec config so these tests never depend on
ambient .env values (the controller owns the live .env).
"""

from types import SimpleNamespace

import pytest

import config
import dynamic_risk
import risk_lifecycle as rl
import startup_checks


SPEC_KNOBS = dict(
    DYNAMIC_ATR_TRAIL_ENABLED=True,
    DYNAMIC_ATR_TRAIL_MULT=1.40,
    DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS=10.00,
    DYNAMIC_ATR_TRAIL_EXTENDED_MULT=1.00,
    DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS=18.00,
    DYNAMIC_ATR_TRAIL_HOMERUN_MULT=0.80,
    DYNAMIC_ATR_TRAIL_ACTIVATION_MULT=1.00,
    DYNAMIC_ATR_TRAIL_MIN_DOLLARS=2.50,
    DYNAMIC_ATR_TRAIL_MAX_DOLLARS=8.00,
    DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS=2.20,
    DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS=5.50,
    DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS=2.00,
    DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS=4.00,
    DYNAMIC_ATR_TRAIL_STEP_DOLLARS=0.25,
    DYNAMIC_ATR_BE_ACTIVATION_MULT=0.75,
    DYNAMIC_ATR_BE_MIN_MFE_DOLLARS=3.50,
    DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS=0.50,
)


def _policy(**overrides):
    knobs = dict(SPEC_KNOBS)
    knobs.update(overrides)
    return dynamic_risk.read_dynamic_risk_policy(SimpleNamespace(**knobs))


def _trail(pol, mfe, atr, entry=3850.0, direction="BUY", current_sl=0.0, **kw):
    sign = 1.0 if direction == "BUY" else -1.0
    peak = entry + sign * mfe
    return dynamic_risk.compute_dynamic_atr_trail_stop(
        direction=direction,
        entry_price=entry,
        live_price=peak,
        prev_peak=peak,
        current_sl=current_sl,
        atr=atr,
        policy=pol,
        digits=2,
        **kw,
    )


# --- trail_zone_clamp boundaries ------------------------------------------


@pytest.mark.parametrize(
    "mfe,lo,hi",
    [
        (-5.0, 2.50, 8.00),   # negative MFE clamps to the STANDARD zone
        (0.0, 2.50, 8.00),
        (9.99, 2.50, 8.00),
        (10.0, 2.20, 5.50),   # inclusive EXTENDED boundary
        (17.99, 2.20, 5.50),
        (18.0, 2.00, 4.00),   # inclusive HOME_RUN boundary
        (100.0, 2.00, 4.00),
    ],
)
def test_trail_zone_clamp_boundaries(mfe, lo, hi):
    assert dynamic_risk.trail_zone_clamp(mfe, _policy()) == (pytest.approx(lo), pytest.approx(hi))


def test_trail_zone_clamp_mirrors_mult_zone_logic():
    # Custom thresholds: the clamp must follow the SAME boundary logic as the mult.
    pol = _policy(DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS=12.00, DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS=20.00)
    for mfe in (0.0, 11.99, 12.0, 19.99, 20.0, 50.0):
        _, zone = dynamic_risk.select_progressive_trail_mult(mfe, pol)
        expected = {
            "STANDARD": (pol.trail_min_dollars, pol.trail_max_dollars),
            "EXTENDED": (pol.trail_extended_min_dollars, pol.trail_extended_max_dollars),
            "HOME_RUN": (pol.trail_homerun_min_dollars, pol.trail_homerun_max_dollars),
        }[zone]
        assert dynamic_risk.trail_zone_clamp(mfe, pol) == expected


def test_select_progressive_trail_mult_contract_unchanged():
    pol = _policy()
    assert dynamic_risk.select_progressive_trail_mult(0.00, pol) == (1.40, "STANDARD")
    assert dynamic_risk.select_progressive_trail_mult(9.99, pol) == (1.40, "STANDARD")
    assert dynamic_risk.select_progressive_trail_mult(10.00, pol) == (1.00, "EXTENDED")
    assert dynamic_risk.select_progressive_trail_mult(17.99, pol) == (1.00, "EXTENDED")
    assert dynamic_risk.select_progressive_trail_mult(18.00, pol) == (0.80, "HOME_RUN")
    assert dynamic_risk.select_progressive_trail_mult(100.00, pol) == (0.80, "HOME_RUN")


# --- trail_dist grid across the three stages ------------------------------


@pytest.mark.parametrize(
    "mfe,atr,dist,zone,mult",
    [
        # ATR=6: raw 8.4 / 6.0 / 4.8 -> each stage caps at its OWN max.
        (5.0, 6.0, 8.0, "STANDARD", 1.40),
        (12.0, 6.0, 5.5, "EXTENDED", 1.00),
        (20.0, 6.0, 4.0, "HOME_RUN", 0.80),
        # ATR=1: the ATR-relative stages are min($10, 2.5 x ATR) = $2.50 and
        # min($18, 4.0 x ATR) = $4.00, so even a $5.00 MFE is already HOME_RUN.
        # Each stage still floors at its OWN min.
        (5.0, 1.0, 2.0, "HOME_RUN", 0.80),
        (12.0, 1.0, 2.0, "HOME_RUN", 0.80),
        (20.0, 1.0, 2.0, "HOME_RUN", 0.80),
        # ATR=4: interior values pass through unclamped in every stage.
        (5.0, 4.0, 5.6, "STANDARD", 1.40),
        (12.0, 4.0, 4.0, "EXTENDED", 1.00),
        (20.0, 4.0, 3.2, "HOME_RUN", 0.80),
        # ATR=2: the ATR-relative EXTENDED stage opens at min($10, 2.5 x ATR) =
        # $5.00 and the HOME_RUN stage at min($18, 4.0 x ATR) = $8.00.
        (5.0, 2.0, 2.2, "EXTENDED", 1.00),
        (12.0, 2.0, 2.0, "HOME_RUN", 0.80),
        (20.0, 2.0, 2.0, "HOME_RUN", 0.80),
        # ATR=10: every stage caps.
        (5.0, 10.0, 8.0, "STANDARD", 1.40),
        (12.0, 10.0, 5.5, "EXTENDED", 1.00),
        (20.0, 10.0, 4.0, "HOME_RUN", 0.80),
        # Exact zone boundaries with ATR=6.
        (9.99, 6.0, 8.0, "STANDARD", 1.40),
        (10.0, 6.0, 5.5, "EXTENDED", 1.00),
        (17.99, 6.0, 5.5, "EXTENDED", 1.00),
        (18.0, 6.0, 4.0, "HOME_RUN", 0.80),
        # Exact ATR-relative boundaries with ATR=1: EXTENDED at $2.50, HOME_RUN at $4.00.
        (2.49, 1.0, 2.5, "STANDARD", 1.40),
        (2.50, 1.0, 2.2, "EXTENDED", 1.00),
        (3.99, 1.0, 2.2, "EXTENDED", 1.00),
        (4.00, 1.0, 2.0, "HOME_RUN", 0.80),
    ],
)
def test_trail_dist_grid(mfe, atr, dist, zone, mult):
    res = _trail(_policy(), mfe, atr)
    assert res["trail_zone"] == zone
    assert res["trail_mult"] == pytest.approx(mult)
    assert res["trail_dist"] == pytest.approx(dist)
    if res["trail_armed"]:
        peak = 3850.0 + mfe
        assert res["atr_sl"] == pytest.approx(round(peak - dist, 2))


# --- reader / config / startup validation ---------------------------------


def test_reader_spec_defaults_and_defensive_normalisation():
    pol = dynamic_risk.read_dynamic_risk_policy(SimpleNamespace())
    assert pol.trail_min_dollars == pytest.approx(2.50)
    assert pol.trail_max_dollars == pytest.approx(8.00)
    assert pol.trail_extended_min_dollars == pytest.approx(2.20)
    assert pol.trail_extended_max_dollars == pytest.approx(5.50)
    assert pol.trail_homerun_min_dollars == pytest.approx(2.00)
    assert pol.trail_homerun_max_dollars == pytest.approx(4.00)
    # The dataclass defaults match the shipped spec too.
    dflt = dynamic_risk.DynamicRiskPolicy()
    assert (dflt.trail_max_dollars, dflt.trail_extended_min_dollars, dflt.trail_extended_max_dollars) == (8.00, 2.20, 5.50)
    assert (dflt.trail_homerun_min_dollars, dflt.trail_homerun_max_dollars) == (2.00, 4.00)
    # Garbage values fall back to the spec defaults.
    for bad in ("abc", None, float("nan")):
        p = _policy(
            DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS=bad,
            DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS=bad,
            DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS=bad,
            DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS=bad,
        )
        assert (p.trail_extended_min_dollars, p.trail_extended_max_dollars) == (2.20, 5.50)
        assert (p.trail_homerun_min_dollars, p.trail_homerun_max_dollars) == (2.00, 4.00)
    # Inverted pairs are normalised so min <= max always holds.
    p = _policy(
        DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS=6.0,
        DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS=3.0,
        DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS=5.0,
        DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS=1.0,
    )
    assert p.trail_extended_min_dollars <= p.trail_extended_max_dollars
    assert p.trail_homerun_min_dollars <= p.trail_homerun_max_dollars


def test_config_validate_pairs_swaps_new_inversions(monkeypatch):
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS", 6.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS", 3.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS", 5.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS", 1.00)
    config.validate_dynamic_risk_pairs()
    assert config.DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS == pytest.approx(3.00)
    assert config.DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS == pytest.approx(6.00)
    assert config.DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS == pytest.approx(1.00)
    assert config.DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS == pytest.approx(5.00)


def _startup_cfg(tmp_path, **overrides):
    base = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    base.update(
        LOG_FILE_PATH=tmp_path / "copier.log",
        STATE_FILE_PATH=tmp_path / "state.json",
        TRADES_LEDGER_PATH=tmp_path / "ledger.jsonl",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_startup_checks_fatal_on_inverted_stage_pairs(tmp_path):
    for lo_k, hi_k in (
        ("DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS", "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS"),
        ("DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS", "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS"),
    ):
        res = startup_checks.validate_environment(
            check_packages=False, cfg=_startup_cfg(tmp_path, **{lo_k: 6.0, hi_k: 3.0})
        )
        assert res["ok"] is False
        assert any(lo_k in e for e in res["errors"])
    # Non-positive stage clamps are fatal too.
    res = startup_checks.validate_environment(
        check_packages=False, cfg=_startup_cfg(tmp_path, DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS=0.0)
    )
    assert res["ok"] is False
    assert any("DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS" in e for e in res["errors"])
    # The shipped spec values pass cleanly.
    res_ok = startup_checks.validate_environment(check_packages=False, cfg=_startup_cfg(tmp_path, **SPEC_KNOBS))
    assert res_ok["ok"] is True


def test_executor_policy_proxy_carries_new_clamps(monkeypatch):
    from mt5_executor import MT5Executor

    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS", 2.30, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS", 3.90, raising=False)
    pol = MT5Executor(dry_run=True)._get_dynamic_risk_policy()
    assert pol.trail_extended_min_dollars == pytest.approx(2.30)
    assert pol.trail_homerun_max_dollars == pytest.approx(3.90)


# --- forward-only across zone transitions ---------------------------------


def test_forward_only_across_zone_transitions_buy():
    pol = _policy()
    atr = 6.0
    entry = 3850.0
    last_applied = 0.0
    prev_atr_sl = None
    prev_dist = None
    # Activation is 1.00x ATR = $6.00; sweep from there through both transitions.
    step = 0
    while True:
        mfe = 6.0 + step * 0.25
        if mfe > 30.0:
            break
        res = _trail(pol, mfe, atr, entry=entry, current_sl=last_applied)
        assert res["trail_armed"] is True
        assert res["atr_sl"] is not None
        if prev_atr_sl is not None:
            assert res["atr_sl"] >= prev_atr_sl - 1e-9, f"ATR trail loosened at MFE={mfe}"
            assert res["trail_dist"] <= prev_dist + 1e-9, f"trail_dist grew at MFE={mfe}"
        if res["should_update"]:
            if last_applied > 0.0:
                assert res["candidate_sl"] >= last_applied + 0.25 - 1e-9
            last_applied = res["candidate_sl"]
        elif last_applied > 0.0:
            # A rejected candidate is never applied; it must be below the step threshold.
            assert res["candidate_sl"] < last_applied + 0.25 + 1e-9
        prev_atr_sl = res["atr_sl"]
        prev_dist = res["trail_dist"]
        step += 1
    # Final pick: peak 3880, HOME_RUN distance 4.00 -> 3876.00.
    assert last_applied == pytest.approx(3876.0)

    # Explicit transition pins (ATR=6): 8.0 -> 5.5 -> 4.0, stop jumps FORWARD.
    a = _trail(pol, 9.99, atr, entry=entry)
    b = _trail(pol, 10.0, atr, entry=entry)
    c = _trail(pol, 17.99, atr, entry=entry)
    d = _trail(pol, 18.0, atr, entry=entry)
    assert (a["trail_dist"], b["trail_dist"], c["trail_dist"], d["trail_dist"]) == (8.0, 5.5, 5.5, 4.0)
    assert a["atr_sl"] < b["atr_sl"] < c["atr_sl"] < d["atr_sl"]

    # Step filter: inside Stage B the distance is fixed at 5.5, so a +$0.10 peak
    # rise yields a +$0.10 candidate (< $0.25 step) and must NOT update.
    first = _trail(pol, 12.0, atr, entry=entry, current_sl=0.0)
    assert first["candidate_sl"] == pytest.approx(3856.5) and first["should_update"] is True
    tiny = _trail(pol, 12.10, atr, entry=entry, current_sl=first["candidate_sl"])
    assert tiny["candidate_sl"] == pytest.approx(3856.6) and tiny["should_update"] is False
    full = _trail(pol, 12.25, atr, entry=entry, current_sl=first["candidate_sl"])
    assert full["candidate_sl"] == pytest.approx(3856.75) and full["should_update"] is True


def test_forward_only_across_zone_transitions_sell():
    pol = _policy()
    atr = 6.0
    entry = 3850.0
    last_applied = 0.0
    prev_atr_sl = None
    step = 0
    while True:
        mfe = 6.0 + step * 0.25
        if mfe > 30.0:
            break
        res = _trail(pol, mfe, atr, entry=entry, direction="SELL", current_sl=last_applied)
        assert res["trail_armed"] is True
        if prev_atr_sl is not None:
            assert res["atr_sl"] <= prev_atr_sl + 1e-9, f"SELL ATR trail loosened at MFE={mfe}"
        if res["should_update"]:
            if last_applied > 0.0:
                assert res["candidate_sl"] <= last_applied - 0.25 + 1e-9
            last_applied = res["candidate_sl"]
        prev_atr_sl = res["atr_sl"]
        step += 1
    # Final pick: peak 3820, HOME_RUN distance 4.00 -> 3824.00.
    assert last_applied == pytest.approx(3824.0)


# --- R-gated BE + R_LOCK staircase integration -----------------------------


def _lifecycle_policy():
    return rl.LifecyclePolicy(
        r_lock_enabled=True,
        r_lock_tiers=rl.DEFAULT_R_LOCK_TIERS,
        r_lock_min_r_dollars=0.50,
    )


def test_r_gated_be_and_r_lock_staircase_integration():
    pol = _policy()
    lc = _lifecycle_policy()
    entry, sl0 = 3900.0, 3880.0
    r = rl.compute_initial_r(entry, sl0)
    assert r == pytest.approx(20.0)
    # Wide ATR: trail activation $20; R-gated BE threshold max(0.5R=10, 0.75*ATR=15, 2.50) = 15.
    atr = 20.0

    def at(mfe, current_sl=0.0):
        live = entry + mfe
        floor = rl.compute_r_lock_floor("BUY", entry, sl0, mfe, live, lc)
        res = dynamic_risk.compute_dynamic_atr_trail_stop(
            direction="BUY",
            entry_price=entry,
            live_price=live,
            prev_peak=live,
            current_sl=current_sl,
            atr=atr,
            policy=pol,
            digits=2,
            r_lock_floor=floor,
            initial_r=r,
        )
        return res, floor

    # Below 0.5R nothing arms: R tier 10, BE gate 15, trail activation 20.
    res, floor = at(9.99)
    assert floor is None
    assert res["armed"] is False and res["should_sweep_pending"] is False

    # 0.5R: the R floor locks entry; BE still gated, trail not armed.
    res, floor = at(10.0)
    assert floor == pytest.approx(3900.0)
    assert res["stop_source"] == "R_LOCK" and res["candidate_sl"] == pytest.approx(3900.0)
    assert res["be_armed"] is False and res["should_sweep_pending"] is False

    # BE gate opens at $15 -> entry + the test policy's $0.50 offset and the pending
    # sweep (this file's SPEC_KNOBS use the legacy BE numbers). For a BUY the
    # HIGHER stop is tighter, so BE (3900.50) now beats the 0.5R floor (3900.00).
    res, _ = at(14.99)
    assert res["be_armed"] is False
    res, _ = at(15.0)
    assert res["be_armed"] is True and res["be_sl"] == pytest.approx(3900.50)
    assert res["should_sweep_pending"] is True
    assert res["stop_source"] == "BE_LOCK" and res["candidate_sl"] == pytest.approx(3900.50)
    assert res["r_lock_floor"] is None

    # 1.0R: trail arms in HOME_RUN (MFE 20 >= 18): dist clamp(16, [2, 4]) = 4 ->
    # atr_sl 3916 beats the R floor 3905 and BE 3900.50 (tightest candidate wins).
    res, floor = at(20.0)
    assert floor == pytest.approx(3905.0)
    assert res["trail_zone"] == "HOME_RUN" and res["trail_dist"] == pytest.approx(4.0)
    assert res["stop_source"] == "ATR_TRAIL" and res["candidate_sl"] == pytest.approx(3916.0)
    assert res["r_lock_floor"] is None  # provenance echo only when R_LOCK won

    # Staircase keeps rising: 1.5R -> +0.50R, 2.0R -> +0.75R.
    assert at(30.0)[1] == pytest.approx(3910.0)
    assert at(40.0)[1] == pytest.approx(3915.0)

    # Applied-stop forward-only sweep across the whole ladder.
    last = 0.0
    mfe = 5.0
    while mfe <= 45.0 + 1e-9:
        res, _ = at(mfe, current_sl=last)
        if res["should_update"]:
            if last > 0.0:
                assert res["candidate_sl"] >= last + 0.25 - 1e-9
            last = res["candidate_sl"]
        mfe += 0.5
    # Final pick: peak 3945, HOME_RUN distance 4.00 -> 3941.00.
    assert last == pytest.approx(3941.0)


def test_r_gate_delays_be_but_never_changes_the_lock_level():
    pol = _policy()
    entry = 3900.0
    # Wide stop R=40, ATR=4: gated threshold max(0.25 x 40 = 10, 0.75 x 4 = 3, 3.50) = 10
    # vs the flat (non-R-gated) max(3, 3.50) = 3.50.
    gated = _trail(pol, 5.0, 4.0, entry=entry, initial_r=40.0)
    flat = _trail(pol, 5.0, 4.0, entry=entry)
    assert gated["be_armed"] is False and flat["be_armed"] is True
    g10 = _trail(pol, 10.0, 4.0, entry=entry, initial_r=40.0)
    f10 = _trail(pol, 10.0, 4.0, entry=entry)
    assert g10["be_armed"] is True and g10["should_sweep_pending"] is True
    # The gate changes WHEN, never WHERE: both lock at entry + $0.50.
    assert g10["be_sl"] == pytest.approx(entry + 0.50) == f10["be_sl"]
    # Small R=2, ATR=4: the flat $3.50 minimum dominates -> arms at $3.50.
    small = _trail(pol, 3.49, 4.0, entry=entry, initial_r=2.0)
    assert small["be_armed"] is False
    small = _trail(pol, 3.50, 4.0, entry=entry, initial_r=2.0)
    assert small["be_armed"] is True and small["be_sl"] == pytest.approx(entry + 0.50)
    # SELL mirror: lock at entry - $0.50.
    sell = _trail(pol, 10.0, 4.0, entry=entry, direction="SELL", initial_r=40.0)
    assert sell["be_armed"] is True and sell["be_sl"] == pytest.approx(entry - 0.50)


def test_tightest_candidate_picker_under_zone_clamps():
    pol = _policy()
    res = _trail(pol, 12.0, 6.0)  # EXTENDED dist 5.5 -> atr_sl 3862 - 5.5 = 3856.5
    atr_sl = res["atr_sl"]
    assert atr_sl == pytest.approx(3856.5)
    looser = _trail(pol, 12.0, 6.0, r_lock_floor=atr_sl - 1.0)
    assert looser["stop_source"] == "ATR_TRAIL" and looser["r_lock_floor"] is None
    tighter = _trail(pol, 12.0, 6.0, r_lock_floor=atr_sl + 1.0)
    assert tighter["stop_source"] == "R_LOCK"
    assert tighter["candidate_sl"] == pytest.approx(atr_sl + 1.0)
    assert tighter["r_lock_floor"] == pytest.approx(atr_sl + 1.0)
