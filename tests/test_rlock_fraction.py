"""Phase 2 - R-lock lock_fraction instrumentation + structural wide-stop finding.

Development/validation only. Covers:
  * the replay picker trace records the winning tier's lock_fraction f_k, the
    per-layer R, trail_dist and MFE for every R_LOCK win (reporting only - the
    traced picker returns the production result unchanged);
  * fraction-0 wins are counted as BE-equivalents and recovered fractions are
    snapped to the configured tier (price-rounding artefacts removed);
  * the structural wide-stop test: R_LOCK can only win a profit tier while
    trail_dist > (t_k - f_k) * R, so a layer whose easiest profit margin exceeds
    the largest trail distance ever observed is structurally unreachable;
  * arm summary aggregates (fraction 0 vs >0, winning-R distribution);
  * two-run determinism of the NEW reporting fields on the pinned tape, and the
    measured structural finding (trio peaks < 0.5R; no win on R > 10.67 layers);
  * the experimental re-tier arm is a labelled replay-only override that leaves
    the production R_LOCK_TIERS default untouched.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "tape_2026-11-17_19.json"

DEFAULT_TIERS = ((0.5, 0.0), (1.0, 0.25), (1.5, 0.5), (2.0, 0.75))
# Shipped ladder: easiest profit margin = (1.0 - 0.25) * R = 0.75R. With the $8
# trail clamp ceiling, any layer with R > 8 / 0.75 = 10.667 is unreachable.
STRUCTURAL_R_CUTOFF = 8.0 / 0.75


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "ab_replay_proof_rlock_under_test", REPO_ROOT / "scripts" / "ab_replay_proof.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load_harness()


def _win_res(floor, trail_dist, mfe, zone="STANDARD"):
    return {
        "stop_source": "R_LOCK",
        "r_lock_floor": floor,
        "trail_dist": trail_dist,
        "mfe_dollars": mfe,
        "trail_zone": zone,
    }


def _install_fake(res):
    fake = SimpleNamespace()
    calls = {"n": 0}

    def orig(**kwargs):
        calls["n"] += 1
        return res

    fake.compute_dynamic_atr_trail_stop = orig
    harness._install_picker_trace(fake)
    return fake, calls


# ---------------------------------------------------------------------------
# Trace instrumentation (reporting only)
# ---------------------------------------------------------------------------

def test_reset_trace_has_lock_fraction_fields():
    harness._reset_picker_trace("t")
    st = harness._PICKER_TRACE
    for key in (
        "r_lock_fraction_zero_wins", "r_lock_fraction_pos_wins",
        "r_lock_winning_r_min", "r_lock_winning_r_max",
        "r_lock_max_trail_dist_at_win", "r_lock_win_details",
        "max_trail_dist", "r_values_seen",
    ):
        assert key in st


def test_trace_records_fraction_pos_win_and_is_reporting_only():
    # SELL, entry 3700, R 8, f=0.25 -> floor 3700 - 2 = 3698.
    res = _win_res(floor=3698.0, trail_dist=7.0, mfe=8.0)
    fake, calls = _install_fake(res)
    harness._reset_picker_trace("9937")
    out = fake.compute_dynamic_atr_trail_stop(
        direction="SELL", entry_price=3700.0, initial_r=8.0
    )
    # Reporting only: the production result is returned unchanged.
    assert out == res
    assert calls["n"] == 1
    st = harness._PICKER_TRACE
    assert st["r_lock_wins"] == 1
    assert st["r_lock_fraction_pos_wins"] == 1
    assert st["r_lock_fraction_zero_wins"] == 0
    assert st["r_lock_winning_r_min"] == 8.0
    assert st["r_lock_winning_r_max"] == 8.0
    assert st["max_trail_dist"] == 7.0
    assert st["r_lock_max_trail_dist_at_win"] == 7.0
    assert 8.0 in st["r_values_seen"]
    d = st["r_lock_win_details"][0]
    assert d["fraction"] == pytest.approx(0.25)
    assert d["r"] == 8.0
    assert d["trail_dist"] == 7.0
    assert d["mfe"] == 8.0


def test_trace_records_fraction_zero_be_equivalent():
    # f=0 -> floor == entry (BE-equivalent). SELL sign gives -0.0 before snapping.
    res = _win_res(floor=3700.0, trail_dist=6.0, mfe=4.0)
    fake, _ = _install_fake(res)
    harness._reset_picker_trace("9953")
    fake.compute_dynamic_atr_trail_stop(direction="SELL", entry_price=3700.0, initial_r=8.0)
    st = harness._PICKER_TRACE
    assert st["r_lock_fraction_zero_wins"] == 1
    assert st["r_lock_fraction_pos_wins"] == 0
    assert st["r_lock_win_details"][0]["fraction"] == pytest.approx(0.0)


def test_trace_non_r_lock_call_updates_distance_but_not_wins():
    res = {"stop_source": "ATR_TRAIL", "trail_dist": 8.0, "mfe_dollars": 12.0}
    fake, _ = _install_fake(res)
    harness._reset_picker_trace("x")
    fake.compute_dynamic_atr_trail_stop(direction="BUY", entry_price=3700.0, initial_r=25.0)
    st = harness._PICKER_TRACE
    assert st["r_lock_wins"] == 0
    assert st["max_trail_dist"] == 8.0
    assert 25.0 in st["r_values_seen"]
    assert st["r_lock_win_details"] == []


# ---------------------------------------------------------------------------
# Fraction snapping + structural enrichment
# ---------------------------------------------------------------------------

def test_snap_tier_removes_price_rounding_artifacts():
    # 0.2495 (half-cent/R artefact) snaps to the 1.0R / 0.25 tier.
    t, f = harness._rlock_snap_tier(0.2495, DEFAULT_TIERS, 9.74, 9.74)
    assert t == 1.0 and f == pytest.approx(0.25)
    # -0.0 normalises to the 0.5R / 0.0 tier.
    t, f = harness._rlock_snap_tier(-0.0, DEFAULT_TIERS, 8.0, 4.0)
    assert t == 0.5 and f == 0.0
    t, f = harness._rlock_snap_tier(0.5, DEFAULT_TIERS, 8.0, 12.0)
    assert t == 1.5 and f == pytest.approx(0.5)


def test_enrich_structural_blocked_on_wide_stop():
    harness._reset_picker_trace("wide")
    st = harness._PICKER_TRACE
    st["r_values_seen"] = {25.0}
    st["max_trail_dist"] = 8.0
    harness._enrich_rlock_trace(st, DEFAULT_TIERS)
    assert st["r_lock_widest_r"] == 25.0
    assert st["r_lock_min_profit_margin"] == pytest.approx(18.75)  # 0.75 * 25
    assert st["r_lock_structural_blocked"] is True


def test_enrich_not_blocked_on_narrow_stop():
    harness._reset_picker_trace("narrow")
    st = harness._PICKER_TRACE
    st["r_values_seen"] = {8.0}
    st["max_trail_dist"] = 8.0
    harness._enrich_rlock_trace(st, DEFAULT_TIERS)
    assert st["r_lock_min_profit_margin"] == pytest.approx(6.0)  # 0.75 * 8
    assert st["r_lock_structural_blocked"] is False


def test_enrich_labels_win_threshold_and_margin():
    harness._reset_picker_trace("w")
    st = harness._PICKER_TRACE
    st["r_lock_win_details"] = [
        {"fraction": 0.2495, "r": 9.74, "trail_dist": 8.0, "mfe": 9.74, "zone": "STANDARD"}
    ]
    harness._enrich_rlock_trace(st, DEFAULT_TIERS)
    d = st["r_lock_win_details"][0]
    assert d["fraction"] == pytest.approx(0.25)
    assert d["threshold"] == 1.0
    assert d["margin_needed"] == pytest.approx(0.75 * 9.74)
    assert d["trail_gt_margin"] is True  # 8.0 > 7.305


# ---------------------------------------------------------------------------
# Arm summary aggregates
# ---------------------------------------------------------------------------

def _row(msg_id, **kw):
    base = {
        "msg_id": msg_id, "sim_gbp": 0.0, "sim_pts_lot": 0.0, "actual_mt5_gbp": 0.0,
        "rollover_flag": False, "exit_utc": "2026-11-18T12:00:00Z", "error": None,
        "mfe_capture_pct": None, "max_stage": 0, "banked_profit_tracked": 0.0,
        "engine_picker_calls": 0, "r_lock_wins": 0, "r_lock_wide_wins": 0,
        "r_lock_max_r": 0.0, "r_lock_fraction_zero_wins": 0, "r_lock_fraction_pos_wins": 0,
        "r_lock_max_trail_dist_at_win": 0.0, "max_trail_dist_observed": 0.0,
        "r_lock_widest_r": 0.0, "r_lock_structural_blocked": False,
        "peak_mfe_r_multiple": 0.0, "r_lock_win_details": [],
    }
    base.update(kw)
    return base


def test_summarize_arm_lock_fraction_aggregates():
    rows = [
        _row("9937", sim_gbp=6.0, r_lock_wins=6, r_lock_fraction_pos_wins=6,
             r_lock_max_trail_dist_at_win=8.0, max_trail_dist_observed=8.0,
             r_lock_widest_r=12.57, r_lock_structural_blocked=True,
             r_lock_win_details=[{"fraction": 0.25, "r": 9.74}]),
        _row("9953", sim_gbp=-10.0, r_lock_wins=30, r_lock_fraction_zero_wins=10,
             r_lock_fraction_pos_wins=20, r_lock_max_trail_dist_at_win=6.89,
             max_trail_dist_observed=6.89, r_lock_widest_r=9.14,
             r_lock_win_details=[{"fraction": 0.0, "r": 4.0}, {"fraction": 0.25, "r": 5.67}]),
        _row("13153", sim_gbp=4.0, peak_mfe_r_multiple=0.4388, max_trail_dist_observed=7.56),
        _row("9934", sim_gbp=0.38, peak_mfe_r_multiple=0.2506),
        _row("13167", sim_gbp=1.54, peak_mfe_r_multiple=0.3388),
    ]
    s = harness._summarize_arm(rows, "c")
    assert s["r_lock_fraction_zero_wins"] == 10
    assert s["r_lock_fraction_pos_wins"] == 26
    assert s["r_lock_be_equivalent_wins"] == 10
    assert s["r_lock_winning_r_min"] == 4.0
    assert s["r_lock_winning_r_max"] == 9.74
    assert s["r_lock_fraction_distribution"] == {"0.25": 2, "0.0": 1}
    assert s["max_trail_dist_observed"] == 8.0
    assert s["trio_peaks_reach_half_r"] is False
    assert s["trio_peak_r_multiples"]["13153"] == pytest.approx(0.4388)
    assert "9937" in s["r_lock_structural_blocked_wide_trades"]


# ---------------------------------------------------------------------------
# Experimental arm is a labelled replay-only override
# ---------------------------------------------------------------------------

def test_experimental_cx_is_labelled_and_only_overrides_tiers():
    cx = harness.ARM_CX_EXPERIMENTAL_PARAMS
    c = harness.ARM_C_CALIBRATED_PARAMS
    # The ONLY delta vs Arm C is the tier ladder.
    delta = {k for k in set(cx) | set(c) if cx.get(k) != c.get(k)}
    assert delta == {"R_LOCK_TIERS"}
    assert cx["R_LOCK_TIERS"] != "0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75"
    # Arm C does NOT pin tiers, so the production default is untouched.
    assert "R_LOCK_TIERS" not in c
    assert cx["R_LOCK_ENABLED"] is True


# ---------------------------------------------------------------------------
# Integration: two-run determinism + measured structural finding (Arm C)
# ---------------------------------------------------------------------------

def test_arm_c_two_run_determinism_and_structural_finding(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=FIXTURE_PATH)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

    run1 = harness._run_arm_subprocess("c", REPO_ROOT, bars_file, tmp_path / "c1.json")
    run2 = harness._run_arm_subprocess("c", REPO_ROOT, bars_file, tmp_path / "c2.json")

    assert run1["bars_sha256"] == run2["bars_sha256"]
    # New reporting fields must be deterministic, and PnL unchanged.
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)

    s = run1["summary"]
    assert s["error_count"] == 0
    assert s["r_lock_wins"] > 0
    # BE-equivalent (fraction-0) wins exist; the staircase engages past f=0 too.
    assert s["r_lock_fraction_zero_wins"] > 0
    assert s["r_lock_fraction_pos_wins"] > 0
    # Histogram keys are clean tier fractions (no rounding artefacts).
    assert set(s["r_lock_fraction_distribution"]) <= {"0.0", "0.25", "0.5", "0.75"}
    # Trio peaks never reach 0.5R -> R-lock is irrelevant to gate_3.
    assert s["trio_peaks_reach_half_r"] is False
    for mid in ("13153", "9934", "13167"):
        assert s["trio_peak_r_multiples"][mid] < 0.5
    # STRUCTURAL: no R_LOCK win on a layer wider than the clamp cutoff, and the
    # widest-stop trades are structurally blocked.
    assert s["r_lock_winning_r_max"] < STRUCTURAL_R_CUTOFF
    assert len(s["r_lock_structural_blocked_wide_trades"]) > 0
    for r in run1["results"]:
        for d in r["r_lock_win_details"]:
            assert d["r"] < STRUCTURAL_R_CUTOFF
            assert d["threshold"] is not None
