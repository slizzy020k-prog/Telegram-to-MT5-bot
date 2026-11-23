"""T3-tight replay validation - reproducibility + pinning regression gate.

Covers the replay-validation brief (2026-11-20 trade-lifecycle-guard):
  * the NEW Nov-23 tape fixture is schema-tagged, pinned (sha256/range/gaps)
    and validated on load; the canonical Nov 17-19 fixture stays untouched;
  * the T3-tight arm is EXACTLY T3 plus the six opt-in 5ac5fe2 knobs, and the
    existing experimental arms (t1..t4) are unchanged;
  * T3-tight pinning overrides ambient .env drift end-to-end into the
    production DynamicRiskPolicy;
  * the six knobs behave as documented when driving the PRODUCTION
    dynamic_risk.compute_dynamic_atr_trail_stop (early arm, distance cap,
    fractional-MFE lock) - not a reimplementation;
  * two dry-run T3-tight replays on the Nov-23 tape are byte-identical;
  * the sharp-reversal scenario reproduces the protection delta: T3 never
    arms and takes the full wide stop, T3-tight arms early and locks profit.

Development/validation only: every replay runs DRY_RUN=true with isolated
temporary runtime paths (the harness worker enforces this). No live orders.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
NOV23_TAPE = FIXTURE_DIR / "tape_2026-11-23.json"
NOV23_SIGNALS = FIXTURE_DIR / "signals_2026-11-23.json"
SHARP_TAPE = FIXTURE_DIR / "tape_scenario_sharp_reversal.json"
SHARP_SIGNALS = FIXTURE_DIR / "signals_scenario_sharp_reversal.json"
CANONICAL_TAPE = FIXTURE_DIR / "tape_2026-11-17_19.json"

# Literal pins (the tests must not trust the harness dicts).
CANONICAL_BARS_SHA256 = "9fd14005ae882638d864701e0ba4cbb7e7247d281cb6bc61d2e0f07025771c4f"
NOV23_BARS_SHA256 = "43955ab51f929b1e975f5cbb74993bdbeeec6056d6e328128a6d3f1ae61bde86"
NOV23_MSG_IDS = {"10283", "10289", "13305", "10304", "10306", "10310", "10321", "13312", "10328"}

T3_DELTA = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50,
}
TIGHT_KNOBS = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": 4.00,
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": 3.00,
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "4:3.50,6:3.00,10:2.50,18:2.00",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": True,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "4:0.30,6:0.50,10:0.65,18:0.75",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": 0.60,
}


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "ab_replay_proof_t3tight_under_test", REPO_ROOT / "scripts" / "ab_replay_proof.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load_harness()


# ---------------------------------------------------------------------------
# Fixtures: NEW Nov-23 tape pinned; canonical tape + existing arms untouched
# ---------------------------------------------------------------------------

def test_nov23_tape_loads_and_validates():
    bars, meta, pinned = harness.load_tape(NOV23_TAPE)
    assert pinned is True
    assert meta["canonical"] is False
    assert len(bars) == 181
    assert meta["first_utc"] == "2026-11-22T23:00:00Z"
    assert meta["last_utc"] == "2026-11-23T02:00:00Z"
    assert meta["symbol"] == "XAUUSD.s"
    assert meta["bars_sha256"] == NOV23_BARS_SHA256
    assert harness._bars_sha256(bars) == NOV23_BARS_SHA256
    assert meta["gaps"]["count"] == 0


def test_canonical_fixture_unchanged():
    bars, meta, pinned = harness.load_tape(CANONICAL_TAPE)
    assert pinned is True
    assert meta["canonical"] is True
    assert meta["bars_sha256"] == CANONICAL_BARS_SHA256
    assert harness._bars_sha256(bars) == CANONICAL_BARS_SHA256


def test_existing_experimental_arms_unchanged():
    assert set(harness.TRAILING_EXPERIMENT_ARMS) == {"t1", "t2", "t3", "t4"}
    assert harness.ARM_T3_EXPERIMENTAL_PARAMS == {**harness.ARM_B_CALIBRATED_PARAMS, **T3_DELTA}
    assert harness.ARM_B_CALIBRATED_PARAMS["R_LOCK_ENABLED"] is False
    # The tight registry is separate and never mutates the trailing registry.
    assert set(harness.TIGHT_COMPARISON_ARMS) == {"t3", "t3tight"}
    assert harness.TIGHT_COMPARISON_ARMS["t3"]["params"] == harness.ARM_T3_EXPERIMENTAL_PARAMS


def test_nov23_signal_set_shape():
    payload = json.loads(NOV23_SIGNALS.read_text(encoding="utf-8"))
    assert payload["schema"] == "mt5-copier-replay-signals/1"
    trades = payload["trades"]
    assert len(trades) == 9
    assert {t["msg_id"] for t in trades} == NOV23_MSG_IDS
    for t in trades:
        for key in ("msg_id", "dir", "open_utc", "close_utc", "layers", "sl", "tp_levels"):
            assert key in t
        assert t["layers"] and t["sl"] > 0.0
        assert "TP1" in t["tp_levels"]
        assert t["dir"] in ("BUY", "SELL")


def test_t3tight_params_are_t3_plus_exactly_six_knobs():
    expected = {**harness.ARM_T3_EXPERIMENTAL_PARAMS, **TIGHT_KNOBS}
    assert harness.ARM_T3TIGHT_EXPERIMENTAL_PARAMS == expected
    delta = {
        k: v for k, v in harness.ARM_T3TIGHT_EXPERIMENTAL_PARAMS.items()
        if harness.ARM_T3_EXPERIMENTAL_PARAMS.get(k) != v
    }
    assert delta == TIGHT_KNOBS


# ---------------------------------------------------------------------------
# Pinning overrides ambient .env drift end-to-end
# ---------------------------------------------------------------------------

def _drift_value(key, val):
    if isinstance(val, bool):
        return not val
    if isinstance(val, float):
        return round(val * 2.0 + 1.0, 4)
    return str(val) + "_drift"


def test_t3tight_pinning_overrides_ambient_env(monkeypatch):
    import config
    import main
    import mt5_executor

    params = harness.ARM_T3TIGHT_EXPERIMENTAL_PARAMS
    for key, val in params.items():
        drift = _drift_value(key, val)
        monkeypatch.setenv(key, str(drift).lower() if isinstance(drift, bool) else str(drift))
        monkeypatch.setattr(config, key, drift, raising=False)
        if hasattr(mt5_executor, key):
            monkeypatch.setattr(mt5_executor, key, drift)
        if hasattr(main, key):
            monkeypatch.setattr(main, key, drift)

    harness._pin_arm_b_params(config, mt5_executor, main)
    harness._pin_arm_experimental_params(params, config, mt5_executor, main, "Arm T3TIGHT")

    for key, val in params.items():
        assert getattr(config, key) == val, f"config drift survived for {key}"

    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    assert pol.trail_activation_atr_mult == pytest.approx(0.70)
    assert pol.trail_max_dollars == pytest.approx(5.50)
    assert pol.trail_activation_ceiling_dollars == pytest.approx(4.00)
    assert pol.be_activation_ceiling_dollars == pytest.approx(3.00)
    assert pol.trail_distance_tiers == "4:3.50,6:3.00,10:2.50,18:2.00"
    assert pol.mfe_lock_enabled is True
    assert pol.mfe_lock_tiers == "4:0.30,6:0.50,10:0.65,18:0.75"
    assert pol.mfe_lock_cost_floor_dollars == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Production-engine knob semantics (drives the real function, no reimplementation)
# ---------------------------------------------------------------------------

def _policy(**overrides):
    import dynamic_risk

    base = {
        "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70,
        "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50,
    }
    base.update(overrides)
    return dynamic_risk.read_dynamic_risk_policy(SimpleNamespace(**base))


def test_production_picker_t3tight_arms_early_and_caps_distance():
    import dynamic_risk

    pol = _policy(**TIGHT_KNOBS)
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
        current_sl=3562.0, atr=12.0, policy=pol,
    )
    # MFE $5 with ATR $12: T3 alone would need 0.70*12 = $8.40 to arm the trail.
    assert res["trail_activation_threshold"] == pytest.approx(4.00)
    assert res["trail_armed"] is True
    assert res["be_armed"] is True  # BE ceiling $3 overrides the R-gate
    assert res["be_threshold"] == pytest.approx(3.00)
    # Distance: clamp(1.4*12, [2.5, 5.5]) = 5.5, capped to 3.5 by the MFE>=4 tier.
    assert res["trail_dist"] == pytest.approx(3.50)
    assert res["distance_cap_applied"] is True
    # Fractional MFE lock: 0.30 * 5.00 = 1.50 >= cost floor 0.60.
    assert res["mfe_lock_sl"] == pytest.approx(3601.50)
    assert res["mfe_lock_fraction"] == pytest.approx(0.30)
    # Tightest-stop picker: ATR_TRAIL and MFE_LOCK tie at 3601.50 > BE 3600.50.
    assert res["candidate_sl"] == pytest.approx(3601.50)
    assert res["stop_source"] == "ATR_TRAIL"
    assert res["should_update"] is True


def test_production_picker_defaults_do_not_arm_at_mfe5_atr12():
    import dynamic_risk

    pol = dynamic_risk.read_dynamic_risk_policy(SimpleNamespace())
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
        current_sl=3562.0, atr=12.0, policy=pol,
    )
    assert res["trail_armed"] is False
    assert res["be_armed"] is False
    assert res["armed"] is False
    assert res["mfe_lock_sl"] is None


# ---------------------------------------------------------------------------
# Two-run determinism (dry-run replay, T3-tight on the NEW Nov-23 tape)
# ---------------------------------------------------------------------------

def test_two_run_determinism_t3tight_on_nov23(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=NOV23_TAPE)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

    run1 = harness._run_arm_subprocess(
        "t3tight", REPO_ROOT, bars_file, tmp_path / "run1.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True,
    )
    run2 = harness._run_arm_subprocess(
        "t3tight", REPO_ROOT, bars_file, tmp_path / "run2.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True,
    )

    assert run1["bars_sha256"] == run2["bars_sha256"] == NOV23_BARS_SHA256
    assert run1["catalog_sha256"] == run2["catalog_sha256"]
    assert run1["arm_params"] == run2["arm_params"]
    assert run1["summary"]["error_count"] == 0
    assert run2["summary"]["error_count"] == 0
    assert len(run1["results"]) == 9

    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)
    for r in run1["results"]:
        assert "time_to_first_protection_min" in r
        assert "first_protect_utc" in r


def test_sharp_reversal_scenario_protection_delta(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=SHARP_TAPE)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

    t3 = harness._run_arm_subprocess(
        "t3", REPO_ROOT, bars_file, tmp_path / "t3.json",
        catalog_path=SHARP_SIGNALS, protection_telemetry=True,
    )
    tight = harness._run_arm_subprocess(
        "t3tight", REPO_ROOT, bars_file, tmp_path / "tight.json",
        catalog_path=SHARP_SIGNALS, protection_telemetry=True,
    )
    tight2 = harness._run_arm_subprocess(
        "t3tight", REPO_ROOT, bars_file, tmp_path / "tight2.json",
        catalog_path=SHARP_SIGNALS, protection_telemetry=True,
    )
    assert json.dumps(tight["results"], sort_keys=True) == json.dumps(tight2["results"], sort_keys=True)

    r3 = t3["results"][0]
    rt = tight["results"][0]
    assert t3["summary"]["error_count"] == 0
    assert tight["summary"]["error_count"] == 0
    # T3: ~$5 MFE never reaches the 0.70x ATR (~$7.5) arm threshold on a wide
    # R=$38 stop -> no protection, full initial stop loss (the Nov-23 13305 shape).
    assert r3["first_protect_utc"] is None
    assert r3["exit_reason"] == "BROKER_SL"
    assert float(r3["sim_gbp"]) < 0.0
    # T3-tight: ceilings arm protection early; the reversal exits at a locked
    # profit instead of the full wide stop.
    assert rt["first_protect_utc"] is not None
    assert rt["time_to_first_protection_min"] is not None
    assert rt["exit_reason"] != "BROKER_SL"
    assert rt["exit_reason"].endswith("_SL")
    assert float(rt["sim_gbp"]) > 0.0
