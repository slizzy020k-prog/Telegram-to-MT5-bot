"""Phase 3+4 - EXPERIMENTAL trailing A/B arm-param overrides (replay only).

Development/validation only. Covers the reusable override mechanism added to
scripts/ab_replay_proof.py for the gate_3 / trailing-distance experiments:
  * the experimental arms (t1..t4) differ from the pinned baseline B ONLY in
    the named trail knob(s), keep R-lock OFF, and never mutate ARM_B itself;
  * overrides apply end-to-end through the executor whitelist policy proxy
    even under hostile ambient .env drift, and the DEFAULT (no-override)
    Arm B pinning still resolves the canonical shipped values afterwards;
  * the full trailing metric set and the per-variant g1-g5 gate formulas;
  * two-run determinism of an experimental arm on the pinned tape.

No production default (config.py / dynamic_risk.py / .env) is touched here.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "tape_2026-11-17_19.json"


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "ab_replay_proof_trailing_under_test", REPO_ROOT / "scripts" / "ab_replay_proof.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load_harness()

EXPECTED_OVERRIDES = {
    "t1": {"DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70},
    "t2": {"DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50},
    "t3": {"DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50},
    "t4": {"DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 6.50},
}


# ---------------------------------------------------------------------------
# Arm param hygiene: experimental deltas are exactly the named knobs
# ---------------------------------------------------------------------------

def test_experimental_arms_differ_from_b_only_in_named_knobs():
    assert set(harness.TRAILING_EXPERIMENT_ARMS) == set(EXPECTED_OVERRIDES)
    for mode, expected in EXPECTED_OVERRIDES.items():
        params = harness.TRAILING_EXPERIMENT_ARMS[mode]["params"]
        diff = {k: v for k, v in params.items() if harness.ARM_B_CALIBRATED_PARAMS.get(k) != v}
        assert diff == expected, f"arm {mode} delta {diff} != {expected}"
        assert set(params) == set(harness.ARM_B_CALIBRATED_PARAMS)
        assert params["R_LOCK_ENABLED"] is False  # baseline B is the no-R-lock arm
        assert params is not harness.ARM_B_CALIBRATED_PARAMS


def test_baseline_b_trail_pins_untouched_by_experiments():
    # The shipped/pinned baseline values must remain the production defaults.
    assert harness.ARM_B_CALIBRATED_PARAMS["DYNAMIC_ATR_TRAIL_ACTIVATION_MULT"] == 1.00
    assert harness.ARM_B_CALIBRATED_PARAMS["DYNAMIC_ATR_TRAIL_MAX_DOLLARS"] == 8.00
    assert harness.ARM_B_CALIBRATED_PARAMS["DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS"] == 5.50
    assert harness.ARM_B_CALIBRATED_PARAMS["DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS"] == 4.00


# ---------------------------------------------------------------------------
# Override mechanism: applies end-to-end, default unchanged
# ---------------------------------------------------------------------------

def _drift_value(key, val):
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


@pytest.mark.parametrize("mode", sorted(EXPECTED_OVERRIDES))
def test_experimental_override_applies_end_to_end(monkeypatch, mode):
    import config
    import main
    import mt5_executor

    params = harness.TRAILING_EXPERIMENT_ARMS[mode]["params"]
    _apply_ambient_drift(monkeypatch, params, config, mt5_executor, main)
    # Mirror the worker order: Arm B pins first, then the experimental delta.
    harness._pin_arm_b_params(config, mt5_executor, main)
    harness._pin_arm_experimental_params(params, config, mt5_executor, main, f"Arm {mode.upper()}")

    for key, val in params.items():
        assert getattr(config, key) == val, f"config drift survived for {key}"
    snap = getattr(main, "_MAIN_CFG_SNAPSHOT", {})
    for key, val in params.items():
        if key in snap:
            assert snap[key] == val, f"main snapshot drift survived for {key}"

    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    expected = EXPECTED_OVERRIDES[mode]
    assert pol.trail_activation_atr_mult == pytest.approx(expected.get("DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00))
    assert pol.trail_max_dollars == pytest.approx(expected.get("DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 8.00))
    # Untouched knobs stay canonical.
    assert pol.trail_atr_mult == pytest.approx(1.40)
    assert pol.trail_min_dollars == pytest.approx(2.50)
    assert pol.trail_extended_max_dollars == pytest.approx(5.50)
    assert pol.trail_homerun_max_dollars == pytest.approx(4.00)
    assert ex._get_lifecycle_policy().r_lock_enabled is False


def test_default_no_override_pinning_unchanged(monkeypatch):
    import config
    import main
    import mt5_executor

    combined = dict(harness.ARM_B_CALIBRATED_PARAMS)
    _apply_ambient_drift(monkeypatch, combined, config, mt5_executor, main)
    harness._pin_arm_b_params(config, mt5_executor, main)
    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    assert pol.trail_activation_atr_mult == pytest.approx(1.00)
    assert pol.trail_max_dollars == pytest.approx(8.00)

    # Applying an experimental override and then re-pinning the DEFAULT arm
    # must restore the canonical values exactly (no sticky mutation), and the
    # ARM_B dict itself must never be mutated by the experimental mechanism.
    harness._pin_arm_experimental_params(
        harness.ARM_T3_EXPERIMENTAL_PARAMS, config, mt5_executor, main, "Arm T3"
    )
    pol2 = ex._get_dynamic_risk_policy()
    assert pol2.trail_activation_atr_mult == pytest.approx(0.70)
    assert pol2.trail_max_dollars == pytest.approx(5.50)
    harness._pin_arm_b_params(config, mt5_executor, main)
    pol3 = ex._get_dynamic_risk_policy()
    assert pol3.trail_activation_atr_mult == pytest.approx(1.00)
    assert pol3.trail_max_dollars == pytest.approx(8.00)
    assert harness.ARM_B_CALIBRATED_PARAMS["DYNAMIC_ATR_TRAIL_ACTIVATION_MULT"] == 1.00
    assert harness.ARM_B_CALIBRATED_PARAMS["DYNAMIC_ATR_TRAIL_MAX_DOLLARS"] == 8.00


# ---------------------------------------------------------------------------
# Metric set (pure reporting over per-trade rows)
# ---------------------------------------------------------------------------

def _row(msg_id, gbp, realized, peak, lot=0.01, r=10.0, reason="ATR_TRAIL_SL",
         stage=1, sources=None, open_utc="2026-11-18T10:00:00Z", exit_utc="2026-11-18T10:10:00Z"):
    return {
        "msg_id": msg_id,
        "sim_gbp": gbp,
        "sim_pts_lot": round(realized * lot, 6),
        "realized_pts_per_oz": realized,
        "peak_mfe_dollars": peak,
        "total_lot": lot,
        "initial_r_per_oz": r,
        "mfe_capture_pct": round(realized / peak * 100.0, 1) if peak > 0 else None,
        "max_stage": stage,
        "exit_reason": reason,
        "exit_price": 3700.0,
        "exit_utc": exit_utc,
        "open_utc": open_utc,
        "stop_source_counts": sources or {},
        "r_lock_wins": 0,
    }


def test_trailing_metrics_full_set():
    rows = [
        _row("w1", 3.0, 4.0, 8.0, reason="ATR_TRAIL_SL", stage=2, sources={"ATR_TRAIL": 5}),
        _row("l1", -2.0, -20.0, 1.0, reason="BROKER_SL", stage=0),
        _row("f1", 0.0, 0.5, 6.0, r=25.0, reason="BE_LOCK_SL", stage=1, sources={"BE_LOCK": 3}),
    ]
    m = harness._trailing_experiment_metrics(rows)
    assert m["total_trades"] == 3
    assert (m["wins"], m["losses"], m["flats_be"]) == (1, 1, 1)
    assert m["win_rate_pct"] == pytest.approx(33.3)
    assert m["gross_profit_gbp"] == pytest.approx(3.0)
    assert m["gross_loss_gbp"] == pytest.approx(-2.0)
    assert m["net_profit_gbp"] == pytest.approx(1.0)
    assert m["profit_factor"] == pytest.approx(1.5)
    # Equity curve 3 -> 1 -> 1: peak-to-trough drawdown = 2.
    assert m["max_drawdown_gbp"] == pytest.approx(2.0)
    assert m["avg_r_per_trade"] == pytest.approx((0.4 - 2.0 + 0.02) / 3.0, abs=1e-4)
    assert m["stop_loss_frequency"] == 1
    assert m["be_closure_frequency"] == 1
    assert m["be_closure_trades"] == ["f1"]
    assert m["atr_trail_exit_trades"] == ["w1"]
    # f1 realized 0.5 < 50% of peak 6.0 -> premature; w1 realized exactly 50% -> not.
    assert m["premature_trailing_exit_frequency"] == 1
    assert m["premature_trailing_exit_trades"] == ["f1"]
    assert m["atr_trail_engagement"]["trades"] == ["w1"]
    assert m["atr_trail_engagement"]["picker_wins"] == 5
    assert m["tp_stage_distribution"] == {"TP2": 1, "none": 1, "TP1": 1}
    assert m["avg_holding_time_min"] == pytest.approx(10.0)
    assert "not measured" in m["mae"]
    # Surrender floors realized at 0: w1 (8-4), l1 (1-0), f1 (6-0.5), lot-weighted.
    expected_surrender = (4.0 + 1.0 + 5.5) * 0.7589
    assert m["profit_surrendered_after_peak_gbp"] == pytest.approx(expected_surrender, abs=0.01)
    assert m["per_trade_surrender"]["w1"]["giveback_usd_per_oz"] == pytest.approx(4.0)


def test_exit_reason_classification():
    assert harness._classify_exit_reason("BROKER_SL") == "initial_stop_loss"
    assert harness._classify_exit_reason("ATR_TRAIL_SL") == "protected_stop"
    assert harness._classify_exit_reason("BE_LOCK_SL") == "protected_stop"
    assert harness._classify_exit_reason("MILESTONE_FLOOR_SL") == "protected_stop"
    assert harness._classify_exit_reason("PROFIT_RATCHET_SL") == "protected_stop"
    assert harness._classify_exit_reason("BROKER_TP") == "tp_family"
    assert harness._classify_exit_reason("TP1_SHORTFALL") == "tp_family"
    assert harness._classify_exit_reason("GOLD_GUARD") == "other"


# ---------------------------------------------------------------------------
# Per-variant gates g1-g5 (same formulas as the shipped gates)
# ---------------------------------------------------------------------------

def _payload(net, tail, trio_gbp, trio_pts_lot, stage, banked=1.0, errors=0,
             cap_9905=80.0, cap_9705=60.0, gbp_9905=1.0, gbp_9705=0.5):
    return {
        "summary": {
            "net_gbp": net,
            "tail_loss_gbp_13078_9953": tail,
            "choke_trio_gbp_13153_9934_13167": trio_gbp,
            "choke_trio_pts_lot": trio_pts_lot,
            "trade_13153_max_stage": stage,
            "trade_9934_banked_tracked": banked,
            "error_count": errors,
            "unflagged_rollover_exits": [],
        },
        "results": [
            {"msg_id": "9905", "sim_gbp": gbp_9905, "mfe_capture_pct": cap_9905},
            {"msg_id": "9705", "sim_gbp": gbp_9705, "mfe_capture_pct": cap_9705},
        ],
    }


def test_trailing_gates_formulas_and_gate3_boundary():
    arm_a = _payload(net=-10.0, tail=-50.0, trio_gbp=1.0, trio_pts_lot=0.02, stage=0)
    arm_b = _payload(net=5.0, tail=-48.0, trio_gbp=6.26, trio_pts_lot=0.0825, stage=3)
    # v_fail reproduces the measured baseline gate_3 failure (pts*lot leg only).
    v_fail = _payload(net=5.0, tail=-48.0, trio_gbp=6.26, trio_pts_lot=0.0825, stage=3)
    # v_pass closes the pts*lot leg exactly at the 0.090 boundary.
    v_pass = _payload(net=6.0, tail=-48.0, trio_gbp=6.90, trio_pts_lot=0.090, stage=3)
    gates = harness._evaluate_trailing_gates(arm_a, arm_b, {"fail": v_fail, "pass": v_pass})
    gf = gates["fail"]
    assert gf["gate_1_net_profit"] is True
    assert gf["gate_2_tail_protection"] is True
    assert gf["gate_3_mfe_capture_and_13153_stage2"] is False  # 0.0825 < 0.090
    assert gf["gate_3_legs"]["trio_gbp"] == pytest.approx(6.26)
    assert gf["gate_4_zero_regression"] is True
    assert gf["g5_vs_baseline_b"] is True
    assert gf["all_gates_passed"] is False
    gp = gates["pass"]
    assert gp["gate_3_mfe_capture_and_13153_stage2"] is True  # >= 0.090 boundary
    assert gp["all_gates_passed"] is True


def test_trailing_gates_winner_non_regression_and_g5():
    arm_a = _payload(net=-10.0, tail=-50.0, trio_gbp=1.0, trio_pts_lot=0.02, stage=0)
    arm_b = _payload(net=5.0, tail=-48.0, trio_gbp=6.26, trio_pts_lot=0.0825, stage=3)
    # Variant degrades winner 9905 capture by 6pp (> 5pp material threshold)
    # and loses net vs baseline -> g5 False.
    v_deg = _payload(net=4.0, tail=-48.0, trio_gbp=7.0, trio_pts_lot=0.095, stage=3, cap_9905=74.0)
    gates = harness._evaluate_trailing_gates(arm_a, arm_b, {"deg": v_deg})
    gd = gates["deg"]
    assert gd["g5_vs_baseline_b"] is False
    assert gd["winner_non_regression"]["9905"]["material_degradation"] is True
    assert gd["winner_non_regression"]["9705"]["material_degradation"] is False
    assert gd["winner_non_regression_ok"] is False
    assert gd["all_gates_passed"] is False
    # g4 fails when 9934 banked profit tracking is lost.
    v_nobank = _payload(net=6.0, tail=-48.0, trio_gbp=7.0, trio_pts_lot=0.095, stage=3, banked=0.0)
    gates2 = harness._evaluate_trailing_gates(arm_a, arm_b, {"nb": v_nobank})
    assert gates2["nb"]["gate_4_zero_regression"] is False


# ---------------------------------------------------------------------------
# Two-run determinism of an experimental arm on the pinned tape
# ---------------------------------------------------------------------------

def test_two_run_determinism_experimental_arm_t3(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=FIXTURE_PATH)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

    run1 = harness._run_arm_subprocess("t3", REPO_ROOT, bars_file, tmp_path / "t3_run1.json")
    run2 = harness._run_arm_subprocess("t3", REPO_ROOT, bars_file, tmp_path / "t3_run2.json")

    assert run1["bars_sha256"] == run2["bars_sha256"]
    assert run1["summary"]["error_count"] == 0
    assert run2["summary"]["error_count"] == 0
    assert run1["arm_params"]["experimental"] is True
    assert run1["arm_params"]["pinned_params"]["DYNAMIC_ATR_TRAIL_ACTIVATION_MULT"] == pytest.approx(0.70)
    assert run1["arm_params"]["pinned_params"]["DYNAMIC_ATR_TRAIL_MAX_DOLLARS"] == pytest.approx(5.50)
    assert run1["arm_params"]["config_verified"]["DYNAMIC_ATR_TRAIL_ACTIVATION_MULT"] == pytest.approx(0.70)
    assert run1["arm_params"]["config_verified"]["DYNAMIC_ATR_TRAIL_MAX_DOLLARS"] == pytest.approx(5.50)
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)
    assert json.dumps(run1["arm_params"], sort_keys=True) == json.dumps(run2["arm_params"], sort_keys=True)
