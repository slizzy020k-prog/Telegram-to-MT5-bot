"""Phase 1.1 - replay reproducibility regression gate.

Covers the two audit holes:
  * the historical M1 tape is pinned to a canonical validated fixture
    (count / range / sha256 / gaps), and corruption fails loudly;
  * ALL DYNAMIC_ATR_* trail + BE knobs are pinned in BOTH arm param sets,
    so the arms are independent of the ambient .env;
  * two dry-run replay runs on the pinned fixture are byte-identical
    (per-trade rows, summary, picker counts).

Development/validation only: every replay here runs DRY_RUN=true with
isolated temporary runtime paths (the harness worker enforces this).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "tape_2026-11-17_19.json"

# Canonical pins (literal copies - the tests must not trust the harness dicts).
PINNED_BARS_SHA256 = "9fd14005ae882638d864701e0ba4cbb7e7247d281cb6bc61d2e0f07025771c4f"
PINNED_BAR_COUNT = 3477
PINNED_FIRST_UTC = "2026-11-16T23:00:00Z"
PINNED_LAST_UTC = "2026-11-19T11:00:00Z"

CANONICAL_TRAIL_PINS = {
    "DYNAMIC_ATR_SLTP_ENABLED": True,
    "DYNAMIC_SLTP_MODE": "ADAPTIVE",
    "DYNAMIC_ATR_TRAIL_ENABLED": True,
    "DYNAMIC_ATR_TRAIL_TIMEFRAME": "M5",
    "DYNAMIC_ATR_TRAIL_MULT": 1.40,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS": 10.00,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MULT": 1.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS": 18.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MULT": 0.80,
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 1.00,
    "DYNAMIC_ATR_TRAIL_MIN_DOLLARS": 2.50,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 8.00,
    "DYNAMIC_ATR_TRAIL_STEP_DOLLARS": 0.25,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS": 2.20,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS": 5.50,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS": 2.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS": 4.00,
    "DYNAMIC_ATR_BE_ACTIVATION_MULT": 0.75,
    "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS": 3.50,
    "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS": 0.50,
}


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "ab_replay_proof_under_test", REPO_ROOT / "scripts" / "ab_replay_proof.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load_harness()


# ---------------------------------------------------------------------------
# Fixture load + validation
# ---------------------------------------------------------------------------

def test_pinned_fixture_loads_and_validates():
    bars, meta, pinned = harness.load_tape(FIXTURE_PATH)
    assert pinned is True
    assert len(bars) == PINNED_BAR_COUNT
    assert meta["bar_count"] == PINNED_BAR_COUNT
    assert meta["first_utc"] == PINNED_FIRST_UTC
    assert meta["last_utc"] == PINNED_LAST_UTC
    assert meta["symbol"] == "XAUUSD.s"
    assert meta["timeframe"] == "M1"
    assert meta["server_offset_sec"] == 7200
    assert meta["gbp_rate_per_pt_001_lot"] == pytest.approx(0.7589)
    assert meta["bars_sha256"] == PINNED_BARS_SHA256
    assert harness._bars_sha256(bars) == PINNED_BARS_SHA256
    assert meta["gaps"]["count"] == 2
    assert meta["gaps"]["max_gap_sec"] == 3780


def test_fetch_m1_bars_on_fixture_is_stable_and_pinned():
    bars1 = harness.fetch_m1_bars(bars_json_path=FIXTURE_PATH)
    bars2 = harness.fetch_m1_bars(bars_json_path=FIXTURE_PATH)
    assert len(bars1) == PINNED_BAR_COUNT
    assert bars1 == bars2
    # Normalization (spread floor + closed-bar ATR recompute) is deterministic
    # and reproduces the pinned tape exactly.
    assert harness._bars_sha256(bars1) == PINNED_BARS_SHA256


def _corrupted_fixture(tmp_path, mutate):
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    p = tmp_path / "corrupt_tape.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_corrupted_bar_fails_sha256_loudly(tmp_path):
    def mut(p):
        p["bars"][100]["close"] = float(p["bars"][100]["close"]) + 0.01

    with pytest.raises(RuntimeError, match="sha256"):
        harness.load_tape(_corrupted_fixture(tmp_path, mut))


def test_wrong_bar_count_fails_loudly(tmp_path):
    def mut(p):
        p["bar_count"] = PINNED_BAR_COUNT - 1

    with pytest.raises(RuntimeError, match="bar_count"):
        harness.load_tape(_corrupted_fixture(tmp_path, mut))


def test_wrong_range_fails_loudly(tmp_path):
    def mut(p):
        p["first_utc"] = "2026-11-16T23:01:00Z"

    with pytest.raises(RuntimeError, match="range"):
        harness.load_tape(_corrupted_fixture(tmp_path, mut))


def test_gap_structure_mismatch_fails_loudly(tmp_path):
    def mut(p):
        p["gaps"]["positions"] = []

    with pytest.raises(RuntimeError, match="gap"):
        harness.load_tape(_corrupted_fixture(tmp_path, mut))


def test_missing_fixture_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        harness.load_tape(tmp_path / "does_not_exist.json")


def test_fetch_never_silently_substitutes_corrupt_fixture(tmp_path):
    def mut(p):
        p["bars"][500]["high"] = float(p["bars"][500]["high"]) + 1.0

    corrupt = _corrupted_fixture(tmp_path, mut)
    with pytest.raises(RuntimeError, match="Pinned tape validation failed"):
        harness.fetch_m1_bars(bars_json_path=corrupt)


# ---------------------------------------------------------------------------
# Arm param pinning completeness + .env independence
# ---------------------------------------------------------------------------

def test_arm_params_pin_all_dynamic_atr_knobs():
    for params in (harness.ARM_B_CALIBRATED_PARAMS, harness.ARM_C_CALIBRATED_PARAMS):
        for key, val in CANONICAL_TRAIL_PINS.items():
            assert key in params, f"missing pin {key}"
            assert params[key] == val, f"wrong pin value for {key}"
    assert harness.ARM_B_CALIBRATED_PARAMS["R_LOCK_ENABLED"] is False
    assert harness.ARM_C_CALIBRATED_PARAMS["R_LOCK_ENABLED"] is True


def _drift_value(key, val):
    """A deliberately WRONG ambient value for a pinned knob."""
    if isinstance(val, bool):
        return not val
    if isinstance(val, float):
        return round(val * 2.0 + 1.0, 4)
    if isinstance(val, int):
        return val + 7
    if key == "DYNAMIC_ATR_TRAIL_TIMEFRAME":
        return "M15"
    if key == "DYNAMIC_SLTP_MODE":
        return "PURE_ATR"
    return str(val) + "_drift"


def _apply_ambient_drift(monkeypatch, params, config, mt5_executor, main):
    for key, val in params.items():
        drift = _drift_value(key, val)
        monkeypatch.setenv(key, str(drift).lower() if isinstance(drift, bool) else str(drift))
        monkeypatch.setattr(config, key, drift, raising=False)
        if hasattr(mt5_executor, key):
            monkeypatch.setattr(mt5_executor, key, drift)
        if hasattr(main, key):
            monkeypatch.setattr(main, key, drift)
    if hasattr(main, "_MAIN_CFG_SNAPSHOT") and isinstance(main._MAIN_CFG_SNAPSHOT, dict):
        monkeypatch.setattr(main, "_MAIN_CFG_SNAPSHOT", dict(main._MAIN_CFG_SNAPSHOT))


def _assert_policy_canonical(pol):
    assert pol.sltp_enabled is True
    assert pol.sltp_mode == "ADAPTIVE"
    assert pol.trail_timeframe == "M5"
    assert pol.trail_atr_mult == pytest.approx(1.40)
    assert pol.trail_extended_atr_mult == pytest.approx(1.00)
    assert pol.trail_homerun_atr_mult == pytest.approx(0.80)
    assert pol.trail_activation_atr_mult == pytest.approx(1.00)
    assert pol.trail_min_dollars == pytest.approx(2.50)
    assert pol.trail_max_dollars == pytest.approx(8.00)
    assert pol.trail_step_dollars == pytest.approx(0.25)
    assert pol.be_activation_atr_mult == pytest.approx(0.75)
    assert pol.be_min_mfe_dollars == pytest.approx(3.50)
    assert pol.be_lock_offset_dollars == pytest.approx(0.50)


def test_arm_b_pinning_overrides_ambient_env(monkeypatch):
    import config
    import main
    import mt5_executor

    _apply_ambient_drift(monkeypatch, harness.ARM_B_CALIBRATED_PARAMS, config, mt5_executor, main)
    harness._pin_arm_b_params(config, mt5_executor, main)

    for key, val in harness.ARM_B_CALIBRATED_PARAMS.items():
        assert getattr(config, key) == val, f"config drift survived for {key}"
    snap = getattr(main, "_MAIN_CFG_SNAPSHOT", {})
    for key, val in harness.ARM_B_CALIBRATED_PARAMS.items():
        if key in snap:
            assert snap[key] == val, f"main snapshot drift survived for {key}"

    # The executor whitelist proxy must resolve the pinned values end-to-end.
    ex = mt5_executor.MT5Executor(dry_run=True)
    _assert_policy_canonical(ex._get_dynamic_risk_policy())
    assert ex._get_lifecycle_policy().r_lock_enabled is False


def test_arm_c_pinning_overrides_ambient_env(monkeypatch):
    import config
    import main
    import mt5_executor

    combined = dict(harness.ARM_B_CALIBRATED_PARAMS)
    combined.update(harness.ARM_C_CALIBRATED_PARAMS)
    _apply_ambient_drift(monkeypatch, combined, config, mt5_executor, main)
    # Mirror the worker order: Arm B pins first, then the Arm C increment.
    harness._pin_arm_b_params(config, mt5_executor, main)
    harness._pin_arm_c_params(config, mt5_executor, main)

    for key, val in combined.items():
        assert getattr(config, key) == val, f"config drift survived for {key}"
    snap = getattr(main, "_MAIN_CFG_SNAPSHOT", {})
    for key, val in harness.ARM_C_CALIBRATED_PARAMS.items():
        if key in snap:
            assert snap[key] == val, f"main snapshot drift survived for {key}"

    ex = mt5_executor.MT5Executor(dry_run=True)
    _assert_policy_canonical(ex._get_dynamic_risk_policy())
    assert ex._get_lifecycle_policy().r_lock_enabled is True


# ---------------------------------------------------------------------------
# Two-run determinism on the pinned fixture (dry-run replay, Arm B)
# ---------------------------------------------------------------------------

def test_two_run_determinism_on_pinned_fixture(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=FIXTURE_PATH)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

    run1 = harness._run_arm_subprocess("b", REPO_ROOT, bars_file, tmp_path / "run1.json")
    run2 = harness._run_arm_subprocess("b", REPO_ROOT, bars_file, tmp_path / "run2.json")

    assert run1["bars_sha256"] == run2["bars_sha256"] == PINNED_BARS_SHA256
    assert run1["arm_params"] == run2["arm_params"]
    assert run1["summary"]["error_count"] == 0
    assert run2["summary"]["error_count"] == 0

    # Byte-identical per-trade rows, summary and picker counts across runs.
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)
    for a, b in zip(run1["results"], run2["results"]):
        assert a["engine_picker_calls"] == b["engine_picker_calls"]
        assert a["stop_source_counts"] == b["stop_source_counts"]
        assert a["r_lock_wins"] == b["r_lock_wins"]
