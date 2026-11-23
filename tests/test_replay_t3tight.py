"""T3-upgraded replay validation - reproducibility + behavior regression gate.

Aligns the replay arm with the DEFINITIVE ATR-relative spec (engine commit
5f47b48), superseding the earlier fractional/absolute-dollar MFE-lock arm:
  * ATR-RELATIVE MFE protection ladder (ratio:pct, NO fixed-dollar MFE tiers),
    bounded ATR, per-layer hysteresis (pct/profit ratchet, never regress);
  * exactly 2% Relative ATR adjustment, applied ONCE at source, gated on the
    opt-in ladder (shipped defaults unchanged);
  * automatic 30-pip BE (verified gold pip $0.10 -> $3.00) through the same
    tightest-stop picker; explicit Telegram BE command recognition;
  * moving TP1-TP4 never resets MFE / peak / ladder;
  * the NEW Nov-23 tape stays pinned; canonical Nov 17-19 tape + existing
    arms (a/b/c/cx/t1..t4) stay untouched;
  * two dry-run upgraded replays on the Nov-23 tape are byte-identical;
  * scenario behavior: sharp reversal / auto-BE / explicit-BE protect where
    T3 takes the full wide stop; strong trend keeps MFE accumulating.

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
CANONICAL_TAPE = FIXTURE_DIR / "tape_2026-11-17_19.json"

# Literal pins (the tests must not trust the harness dicts).
CANONICAL_BARS_SHA256 = "9fd14005ae882638d864701e0ba4cbb7e7247d281cb6bc61d2e0f07025771c4f"
NOV23_BARS_SHA256 = "43955ab51f929b1e975f5cbb74993bdbeeec6056d6e328128a6d3f1ae61bde86"
NOV23_MSG_IDS = {"10283", "10289", "13305", "10304", "10306", "10310", "10321", "13312", "10328"}

T3_DELTA = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50,
}
# Definitive ATR-relative upgrade (5f47b48). SUPERSEDES the fractional pins.
TIGHT_KNOBS = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": 4.00,
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": 3.00,
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "6:3.50,10:3.00,15:2.75,20:2.50",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": True,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "0.40:0.45,0.60:0.55,0.80:0.65,1.00:0.70,1.25:0.75,1.50:0.80,2.00:0.85",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": 0.60,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS": 1.00,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS": 12.00,
    "DYNAMIC_ATR_RELATIVE_SCALE_MULT": 1.02,
    "DYNAMIC_ATR_AUTO_BE_ENABLED": True,
    "DYNAMIC_ATR_AUTO_BE_PIPS": 30.0,
    "DYNAMIC_ATR_AUTO_BE_PIP_SIZE_DOLLARS": 0.0,
}
# Verified pip convention: gold $0.10/pip -> 30 provider pips = $3.00.
AUTO_BE_THRESHOLD_DOLLARS = 3.00


def _ladder():
    pairs = []
    for tok in TIGHT_KNOBS["DYNAMIC_ATR_MFE_LOCK_TIERS"].split(","):
        thr, pct = tok.split(":")
        pairs.append((float(thr), float(pct)))
    return tuple(sorted(pairs))


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


def test_t3tight_params_are_t3_plus_exactly_upgrade_knobs():
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
    assert pol.trail_distance_tiers == "6:3.50,10:3.00,15:2.75,20:2.50"
    assert pol.mfe_lock_enabled is True
    assert pol.mfe_lock_tiers == TIGHT_KNOBS["DYNAMIC_ATR_MFE_LOCK_TIERS"]
    assert pol.mfe_lock_cost_floor_dollars == pytest.approx(0.60)
    assert pol.mfe_lock_atr_min_dollars == pytest.approx(1.00)
    assert pol.mfe_lock_atr_max_dollars == pytest.approx(12.00)
    assert pol.relative_scale_mult == pytest.approx(1.02)
    assert pol.auto_be_enabled is True
    assert pol.auto_be_pips == pytest.approx(30.0)
    assert pol.auto_be_pip_size_dollars == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Production-engine behavior (drives the real functions; expectations are
# DERIVED from the definitive spec ladder, not stale hardcoded numbers)
# ---------------------------------------------------------------------------

def _policy(**overrides):
    import dynamic_risk

    base = {
        "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70,
        "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50,
    }
    base.update(overrides)
    return dynamic_risk.read_dynamic_risk_policy(SimpleNamespace(**base))


def _pick(pol, *, direction="BUY", entry=3600.0, live, peak=None, atr, sl=3562.0, **kw):
    import dynamic_risk

    return dynamic_risk.compute_dynamic_atr_trail_stop(
        direction=direction, entry_price=entry, live_price=live,
        prev_peak=peak if peak is not None else live, current_sl=sl,
        atr=atr, policy=pol, auto_be_threshold=AUTO_BE_THRESHOLD_DOLLARS, **kw,
    )


def test_production_picker_atr_relative_ladder():
    pol = _policy(**TIGHT_KNOBS)
    entry, live, atr = 3600.0, 3605.0, 12.0
    res = _pick(pol, live=live, atr=atr)
    mfe = live - entry
    atr_bound = min(max(atr, TIGHT_KNOBS["DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS"]),
                    TIGHT_KNOBS["DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS"])
    ratio = mfe / atr_bound
    exp_pct, exp_tier = 0.0, 0.0
    for thr, p in _ladder():
        if ratio + 1e-9 >= thr:
            exp_pct, exp_tier = p, thr
    protected = exp_pct * mfe
    assert exp_pct > 0.0  # non-vacuous: this MFE/ATR must reach a tier
    assert res["mfe_lock_ratio"] == pytest.approx(ratio, abs=5e-5)  # engine rounds ratio to 4dp
    assert res["mfe_lock_tier"] == pytest.approx(exp_tier)
    assert res["mfe_lock_fraction"] == pytest.approx(exp_pct)
    assert res["mfe_lock_profit"] == pytest.approx(protected)
    assert res["mfe_lock_sl"] == pytest.approx(entry + protected, abs=0.011)  # broker 2dp rounding
    # Activation ceilings retained (NOT fixed-dollar MFE tiers).
    assert res["trail_armed"] is True
    assert res["trail_activation_threshold"] == pytest.approx(4.00)
    assert res["be_armed"] is True
    assert res["be_threshold"] == pytest.approx(3.00)
    assert res["auto_be_armed"] is True
    assert res["auto_be_sl"] == pytest.approx(entry)
    # Tightest-stop picker: the ladder lock beats BE/auto-BE/trail here.
    assert res["stop_source"] == "MFE_LOCK"
    assert res["candidate_sl"] == pytest.approx(entry + protected, abs=0.011)


@pytest.mark.parametrize("thr,pct", _ladder())
def test_production_picker_all_seven_atr_tiers(thr, pct):
    pol = _policy(**TIGHT_KNOBS)
    atr = 10.0  # inside the [1, 12] bounds -> ratio == MFE/10
    mfe = thr * atr + 0.05  # just above this tier, below the next
    res = _pick(pol, live=3600.0 + mfe, atr=atr)
    assert res["mfe_lock_tier"] == pytest.approx(thr)
    assert res["mfe_lock_fraction"] == pytest.approx(pct)
    assert res["mfe_lock_profit"] == pytest.approx(pct * mfe, abs=5e-5)
    assert res["mfe_lock_sl"] == pytest.approx(3600.0 + pct * mfe, abs=0.011)  # broker 2dp
    # The picker can only be TIGHTER than the ladder lock, never looser
    # (allowing the broker 2-digit rounding of the lock itself).
    assert res["candidate_sl"] >= 3600.0 + pct * mfe - 0.011


def test_production_picker_tier_hysteresis_no_regression():
    pol = _policy(**TIGHT_KNOBS)
    # Highest tier previously reached: 1.50 -> pct 0.80, locked profit $8.
    # ATR now spikes (bounded to 12) so the CURRENT ratio would pick tier 0.40.
    res = _pick(pol, live=3605.0, atr=20.0, mfe_lock_prev_pct=0.80, mfe_lock_prev_profit=8.0)
    assert res["mfe_lock_fraction"] == pytest.approx(0.80)  # never regress
    assert res["mfe_lock_profit"] == pytest.approx(max(0.80 * 5.0, 8.0))  # profit ratchets
    assert res["mfe_lock_sl"] == pytest.approx(3608.0)


def test_production_picker_atr_bounds_and_cost_floor():
    pol = _policy(**TIGHT_KNOBS)
    hi = _pick(pol, live=3606.0, atr=25.0)  # ATR bounded to 12 -> ratio 6/12
    assert hi["mfe_lock_ratio"] == pytest.approx(0.5)
    lo = _pick(pol, live=3600.6, atr=0.5)  # ATR bounded to 1.0 -> ratio 0.6
    assert lo["mfe_lock_ratio"] == pytest.approx(0.6)
    assert lo["mfe_lock_fraction"] == pytest.approx(0.55)
    # protected = 0.55 * 0.6 = 0.33 < cost floor 0.60 -> no nominal-loss lock
    assert lo["mfe_lock_sl"] is None


def test_relative_atr_two_percent_applied_once_and_gated():
    import dynamic_risk

    pol_on = _policy(**TIGHT_KNOBS)
    assert dynamic_risk.compute_volatility_ratio(4.0, pol_on) == pytest.approx(1.02)
    assert dynamic_risk.compute_volatility_ratio(6.0, pol_on) == pytest.approx(1.53)
    # Idempotent across repeated calcs (recomputed from ATR/baseline each call).
    assert dynamic_risk.compute_volatility_ratio(6.0, pol_on) == pytest.approx(1.53)
    # Gated on the opt-in ladder: shipped defaults unchanged.
    pol_off = _policy()
    assert dynamic_risk.compute_volatility_ratio(4.0, pol_off) == pytest.approx(1.00)
    pol_one = _policy(**{**TIGHT_KNOBS, "DYNAMIC_ATR_RELATIVE_SCALE_MULT": 1.0})
    assert dynamic_risk.compute_volatility_ratio(4.0, pol_one) == pytest.approx(1.00)


def test_production_picker_auto_be_30pip_never_loosens():
    pol = _policy(**TIGHT_KNOBS)
    below = _pick(pol, live=3602.9, atr=12.0)
    assert below["auto_be_armed"] is False
    at = _pick(pol, live=3603.0, atr=12.0)
    assert at["auto_be_armed"] is True
    assert at["auto_be_sl"] == pytest.approx(3600.0)  # SL -> entry, exactly 30 pips
    # BE lock (entry + $0.50) is tighter than auto-BE; picker keeps the tighter.
    assert at["candidate_sl"] >= at["auto_be_sl"]
    assert at["stop_source"] in ("BE_LOCK", "MFE_LOCK", "ATR_TRAIL", "AUTO_BE")


def test_moving_tp_does_not_reset_mfe_or_ladder():
    import dynamic_risk

    pol = _policy(**TIGHT_KNOBS)
    kw = dict(live=3606.0, peak=3608.0, atr=10.0, sl=3601.0)
    r1 = _pick(pol, tp_levels={"TP1": 3605.0, "TP2": 3610.0}, cleared_milestones=["TP1"], **kw)
    r2 = _pick(pol, tp_levels={"TP1": 3607.0, "TP2": 3612.0, "TP3": 3618.0},
               cleared_milestones=["TP1", "TP2"], **kw)
    # MFE comes from entry + monotonic peak, independent of TP management.
    assert r1["mfe_dollars"] == r2["mfe_dollars"] == pytest.approx(8.0)
    assert r1["peak_price"] == r2["peak_price"] == pytest.approx(3608.0)
    assert r1["mfe_lock_profit"] == r2["mfe_lock_profit"]
    assert r1["mfe_lock_fraction"] == r2["mfe_lock_fraction"]
    # The bot's own TP lift still advances stages against the moved targets.
    step = dynamic_risk.compute_dynamic_tp_step(
        direction="BUY", entry_price=3600.0, live_price=3609.0, peak_price=3609.0,
        atr=10.0, tp_levels={"TP1": 3605.0, "TP2": 3610.0, "TP3": 3615.0},
        current_stage_idx=1, max_stage=5, cleared_milestones=["TP1"], policy=pol,
    )
    assert step["should_lift"] is True
    assert step["target_stage_idx"] >= 2


def test_production_picker_sell_mirror():
    pol = _policy(**TIGHT_KNOBS)
    res = _pick(pol, direction="SELL", live=3595.0, atr=12.0, sl=3638.0)
    mfe = 3600.0 - 3595.0
    ratio = mfe / 12.0
    exp_pct = max((p for thr, p in _ladder() if ratio + 1e-9 >= thr), default=0.0)
    assert res["mfe_lock_fraction"] == pytest.approx(exp_pct)
    assert res["mfe_lock_sl"] == pytest.approx(3600.0 - exp_pct * mfe, abs=0.011)
    assert res["stop_source"] == "MFE_LOCK"
    assert res["auto_be_sl"] == pytest.approx(3600.0)


@pytest.mark.parametrize("text", [
    "Go breakeven", "Set breakeven", "Set break even", "Break even all entries",
])
def test_explicit_be_commands_parse_to_breakeven_action(text):
    import parser

    res = parser.parse_message(text)
    assert res is not None
    assert res.management_action == "ACTION_BREAKEVEN"


def test_explicit_be_close_worst_command_recognized():
    import parser

    text = "Set break even on best entries close worst"
    res = parser.parse_message(text)
    assert res is not None
    assert res.management_action in ("ACTION_BREAKEVEN", "ACTION_CLOSE_WORST_HOLD_LAYERS")


# ---------------------------------------------------------------------------
# Two-run determinism (dry-run replay, upgraded arm on the NEW Nov-23 tape)
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
        assert "stop_modify_failures" in r


# ---------------------------------------------------------------------------
# Scenario behavior (production engine via the replay worker)
# ---------------------------------------------------------------------------

def _run_scenario(tmp_path, kind, arm):
    tape = FIXTURE_DIR / f"tape_scenario_{kind}.json"
    signals = FIXTURE_DIR / f"signals_scenario_{kind}.json"
    bars = harness.fetch_m1_bars(bars_json_path=tape)
    bars_file = tmp_path / f"bars_{kind}.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    return harness._run_arm_subprocess(
        arm, REPO_ROOT, bars_file, tmp_path / f"{kind}_{arm}.json",
        catalog_path=signals, protection_telemetry=True,
    )


def test_sharp_reversal_scenario_protection_delta(tmp_path):
    t3 = _run_scenario(tmp_path, "sharp_reversal", "t3")
    tight = _run_scenario(tmp_path, "sharp_reversal", "t3tight")
    assert t3["summary"]["error_count"] == 0
    assert tight["summary"]["error_count"] == 0
    r3, rt = t3["results"][0], tight["results"][0]
    # T3: ~$5 MFE never reaches the 0.70x ATR arm threshold on a wide R=$38
    # stop -> no protection, full initial stop loss (the Nov-23 13305 shape).
    assert r3["first_protect_utc"] is None
    assert r3["exit_reason"] == "BROKER_SL"
    assert float(r3["sim_gbp"]) < 0.0
    # Upgraded: ATR-relative ladder + ceilings arm early; reversal exits at a
    # locked profit instead of the full wide stop.
    assert rt["first_protect_utc"] is not None
    assert rt["time_to_first_protection_min"] is not None
    assert rt["exit_reason"] != "BROKER_SL"
    assert rt["exit_reason"].endswith("_SL")
    assert float(rt["sim_gbp"]) > 0.0
    assert int(rt["stop_modify_failures"]) == 0


def test_auto_be_30pip_scenario(tmp_path):
    t3 = _run_scenario(tmp_path, "auto_be_30pip", "t3")
    tight = _run_scenario(tmp_path, "auto_be_30pip", "t3tight")
    r3, rt = t3["results"][0], tight["results"][0]
    assert t3["summary"]["error_count"] == 0
    assert tight["summary"]["error_count"] == 0
    # T3 never arms (MFE ~3.6 < 0.70x ATR ~7.5 and < 0.5R) -> full wide stop.
    assert r3["exit_reason"] == "BROKER_SL"
    assert float(r3["sim_gbp"]) < 0.0
    # Upgraded: 30-pip auto-BE / BE ceiling fires at $3.00; exit ~breakeven.
    assert rt["exit_reason"] != "BROKER_SL"
    assert rt["first_auto_be_utc"] is not None or rt["first_be_arm_utc"] is not None
    assert abs(float(rt["realized_pts_per_oz"])) <= 0.60


def test_explicit_be_scenario(tmp_path):
    t3 = _run_scenario(tmp_path, "explicit_be", "t3")
    tight = _run_scenario(tmp_path, "explicit_be", "t3tight")
    r3, rt = t3["results"][0], tight["results"][0]
    assert t3["summary"]["error_count"] == 0
    assert tight["summary"]["error_count"] == 0
    assert r3["exit_reason"] == "BROKER_SL"
    assert float(r3["sim_gbp"]) < 0.0
    # The scripted "Set breakeven" command drove the production BE path.
    assert rt["explicit_be"] is not None
    assert "error" not in rt["explicit_be"]
    assert rt["explicit_be"]["command"] == "Set breakeven"
    assert rt["exit_reason"] != "BROKER_SL"
    assert float(rt["sim_gbp"]) >= 0.0


def test_strong_trend_mfe_accumulates_with_moving_tp(tmp_path):
    tight = _run_scenario(tmp_path, "strong_trend", "t3tight")
    assert tight["summary"]["error_count"] == 0
    rt = tight["results"][0]
    # Strong trend: MFE keeps accumulating while the bot lifts TP1->TP2+;
    # the ladder ratchets to a high tier and never resets.
    assert float(rt["peak_mfe_dollars"]) >= 15.0
    assert int(rt["max_stage"]) >= 2
    assert float(rt["max_mfe_lock_pct"]) >= 0.65
    assert int(rt["stop_modify_failures"]) == 0


def test_rapid_atr_change_tier_hysteresis_in_replay(tmp_path):
    tight = _run_scenario(tmp_path, "rapid_atr_change", "t3tight")
    assert tight["summary"]["error_count"] == 0
    rt = tight["results"][0]
    # Pre-expansion MFE/ATR (~5.8/3.4) reaches tier >= 1.25 (pct 0.75); the
    # later ATR spike must not regress the persisted pct (hysteresis).
    assert float(rt["max_mfe_lock_pct"]) >= 0.75
    assert float(rt["sim_gbp"]) > 0.0


# ---------------------------------------------------------------------------
# 5% trail-distance scale backtest (engine 09aae20): upgraded config at
# DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT 1.00 vs 1.05. Scale applies AFTER the
# zone clamp + distance-tier cap; trail distance ONLY; never-loosen preserved.
# ---------------------------------------------------------------------------

SCALE_KNOB = "DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT"


def test_scale_arms_are_upgraded_plus_single_knob():
    base = harness.ARM_T3TIGHT_EXPERIMENTAL_PARAMS
    assert harness.ARM_T3TIGHT_SCALE100_EXPERIMENTAL_PARAMS == {**base, SCALE_KNOB: 1.00}
    assert harness.ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS == {**base, SCALE_KNOB: 1.05}
    delta = {
        k: v for k, v in harness.ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS.items()
        if harness.ARM_T3TIGHT_SCALE100_EXPERIMENTAL_PARAMS.get(k) != v
    }
    assert delta == {SCALE_KNOB: 1.05}
    assert set(harness.SCALE_COMPARISON_ARMS) == {"s100", "s105"}
    # Existing registries untouched.
    assert set(harness.TIGHT_COMPARISON_ARMS) == {"t3", "t3tight"}
    assert set(harness.TRAILING_EXPERIMENT_ARMS) == {"t1", "t2", "t3", "t4"}


def test_production_picker_distance_scale_widens_after_cap():
    import dynamic_risk

    pol100 = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.00})
    pol105 = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05})
    # MFE $12, ATR $10: EXTENDED clamp gives 5.5, distance tier (MFE>=10) caps
    # to 3.00; the scale must widen EVEN THOUGH the cap binds.
    kw = dict(live=3612.0, peak=3612.0, atr=10.0, sl=3562.0)
    r100 = _pick(pol100, **kw)
    r105 = _pick(pol105, **kw)
    assert r100["trail_dist"] == pytest.approx(3.00)
    assert r105["trail_dist"] == pytest.approx(round(3.00 * 1.05, 2))
    assert r105["trail_dist"] / r100["trail_dist"] == pytest.approx(1.05, abs=0.005)
    assert r100["atr_sl"] == pytest.approx(3612.0 - 3.00)
    assert r105["atr_sl"] == pytest.approx(3612.0 - round(3.00 * 1.05, 2))
    # Everything except the ATR trail distance is unchanged.
    for key in ("mfe_lock_sl", "mfe_lock_profit", "mfe_lock_fraction", "mfe_lock_tier",
                "mfe_lock_ratio", "trail_activation_threshold", "be_threshold",
                "auto_be_sl", "be_sl"):
        assert r100[key] == r105[key], f"scale leaked into {key}"
    # Here the trail (peak - 3.00 = 3609.00) is TIGHTER than the ladder lock
    # (entry + 0.70*12 = 3608.40), so ATR_TRAIL wins the picker. The widened
    # trail (3608.85) still beats the lock but is slightly looser than 1.00.
    assert r100["stop_source"] == r105["stop_source"] == "ATR_TRAIL"
    assert r100["candidate_sl"] == pytest.approx(3609.00)
    assert r105["candidate_sl"] == pytest.approx(3612.0 - round(3.00 * 1.05, 2))
    assert r105["candidate_sl"] <= r100["candidate_sl"]
    assert r105["candidate_sl"] >= r105["mfe_lock_sl"]  # picker keeps the tightest
    # Uncapped case: MFE $5, ATR $12 -> clamp 5.5, no tier cap; scale still applies.
    u100 = _pick(pol100, live=3605.0, atr=12.0)
    u105 = _pick(pol105, live=3605.0, atr=12.0)
    assert u100["trail_dist"] == pytest.approx(5.50)
    assert u105["trail_dist"] == pytest.approx(round(5.50 * 1.05, 2))


def test_production_picker_distance_scale_never_loosens():
    pol105 = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05})
    # An already-tighter established stop must never be loosened by the wider trail.
    res = _pick(pol105, live=3612.0, peak=3612.0, atr=10.0, sl=3609.50)
    assert res["should_update"] is False
    # Constant scale preserves monotonic forward-only trail as the peak rises.
    early = _pick(pol105, live=3612.0, peak=3612.0, atr=10.0, sl=3562.0)
    later = _pick(pol105, live=3615.0, peak=3615.0, atr=10.0, sl=early["candidate_sl"])
    assert later["atr_sl"] >= early["atr_sl"]
    assert later["trail_dist"] <= early["trail_dist"]


def test_two_run_determinism_s105_on_nov23(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=NOV23_TAPE)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    run1 = harness._run_arm_subprocess(
        "s105", REPO_ROOT, bars_file, tmp_path / "s105_run1.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True,
    )
    run2 = harness._run_arm_subprocess(
        "s105", REPO_ROOT, bars_file, tmp_path / "s105_run2.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True,
    )
    assert run1["bars_sha256"] == run2["bars_sha256"] == NOV23_BARS_SHA256
    assert run1["arm_params"] == run2["arm_params"]
    assert run1["arm_params"]["pinned_params"][SCALE_KNOB] == 1.05
    assert run1["summary"]["error_count"] == 0
    assert len(run1["results"]) == 9
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)


@pytest.mark.parametrize("kind", ["normal_retracement", "high_atr"])
def test_scale_retracement_scenarios_premature_not_worse(tmp_path, kind):
    s100 = _run_scenario(tmp_path, kind, "s100")
    s105 = _run_scenario(tmp_path, kind, "s105")
    assert s100["summary"]["error_count"] == 0
    assert s105["summary"]["error_count"] == 0
    m100 = harness._tight_comparison_metrics(s100["results"])
    m105 = harness._tight_comparison_metrics(s105["results"])
    # More retracement room must not INCREASE premature stop-outs.
    assert m105["premature_stop_outs"] <= m100["premature_stop_outs"]
    assert m105["stop_modify_failures"] == 0


# ---------------------------------------------------------------------------
# RESEARCH binding-ladder candidates (-2pp / -3pp on clipping tiers only).
# NOT adopted; these tests pin the research arms' semantics + reproducibility.
# ---------------------------------------------------------------------------

def _parse_tiers(raw):
    out = {}
    for tok in raw.split(","):
        thr, pct = tok.split(":")
        out[float(thr)] = float(pct)
    return out


def test_ladder_candidates_reduce_only_clipping_tiers():
    cur = _parse_tiers(harness.LADDER_CURRENT_TIERS)
    assert cur == _parse_tiers(harness.ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS["DYNAMIC_ATR_MFE_LOCK_TIERS"])
    clipping = {0.40, 0.60, 1.00}  # identified by exit binding telemetry
    for arm, pp in (("l2pp", 0.02), ("l3pp", 0.03)):
        params = harness.LADDER_RESEARCH_ARMS[arm]["params"]
        cand = _parse_tiers(params["DYNAMIC_ATR_MFE_LOCK_TIERS"])
        assert set(cand) == set(cur)  # activation ratios UNCHANGED
        for thr in cur:
            expected_drop = pp if thr in clipping else 0.0
            assert cur[thr] - cand[thr] == pytest.approx(expected_drop), f"tier {thr}"
        # Every non-tier knob identical to the upgraded + 1.05 base.
        for k, v in harness.ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS.items():
            if k != "DYNAMIC_ATR_MFE_LOCK_TIERS":
                assert params[k] == v, f"knob {k} changed"


def test_production_picker_candidate_ladder_protects_less_at_clipping_tier():
    pol_cur = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05})
    pol2 = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05,
                      "DYNAMIC_ATR_MFE_LOCK_TIERS": harness.LADDER_MINUS2PP_TIERS})
    pol3 = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05,
                      "DYNAMIC_ATR_MFE_LOCK_TIERS": harness.LADDER_MINUS3PP_TIERS})
    # MFE 5.5 / ATR 10 -> ratio 0.55 -> clipping tier 0.40.
    kw = dict(live=3605.5, peak=3605.5, atr=10.0, sl=3562.0)
    rc, r2, r3 = _pick(pol_cur, **kw), _pick(pol2, **kw), _pick(pol3, **kw)
    assert rc["mfe_lock_tier"] == r2["mfe_lock_tier"] == r3["mfe_lock_tier"] == pytest.approx(0.40)
    assert rc["mfe_lock_fraction"] == pytest.approx(0.45)
    assert r2["mfe_lock_fraction"] == pytest.approx(0.43)
    assert r3["mfe_lock_fraction"] == pytest.approx(0.42)
    mfe = 5.5
    assert rc["mfe_lock_profit"] == pytest.approx(0.45 * mfe, abs=5e-5)
    assert r2["mfe_lock_profit"] == pytest.approx(0.43 * mfe, abs=5e-5)
    assert r3["mfe_lock_profit"] == pytest.approx(0.42 * mfe, abs=5e-5)
    assert r2["mfe_lock_sl"] == pytest.approx(3600.0 + 0.43 * mfe, abs=0.011)
    assert r3["mfe_lock_sl"] == pytest.approx(3600.0 + 0.42 * mfe, abs=0.011)
    # Activation / BE / auto-BE untouched by the ladder reduction.
    for key in ("trail_activation_threshold", "be_threshold", "auto_be_sl", "be_sl"):
        assert rc[key] == r2[key] == r3[key]


def test_production_picker_candidate_never_loosens_established_stop():
    pol_cur = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05})
    pol2 = _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05,
                      "DYNAMIC_ATR_MFE_LOCK_TIERS": harness.LADDER_MINUS2PP_TIERS})
    kw = dict(live=3605.5, peak=3605.5, atr=10.0, sl=3562.0)
    established = _pick(pol_cur, **kw)
    # Hysteresis carries the CURRENT ladder's established pct/profit into the
    # candidate policy: the protected stop must not loosen.
    carried = _pick(pol2, mfe_lock_prev_pct=established["mfe_lock_fraction"],
                    mfe_lock_prev_profit=established["mfe_lock_profit"], **kw)
    assert carried["mfe_lock_fraction"] == pytest.approx(established["mfe_lock_fraction"])
    assert carried["mfe_lock_profit"] == pytest.approx(established["mfe_lock_profit"])
    assert carried["mfe_lock_sl"] == pytest.approx(established["mfe_lock_sl"])


def test_two_run_determinism_l2pp_on_nov23(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=NOV23_TAPE)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    run1 = harness._run_arm_subprocess(
        "l2pp", REPO_ROOT, bars_file, tmp_path / "l2pp_run1.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True, exit_binding=True,
    )
    run2 = harness._run_arm_subprocess(
        "l2pp", REPO_ROOT, bars_file, tmp_path / "l2pp_run2.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True, exit_binding=True,
    )
    assert run1["bars_sha256"] == run2["bars_sha256"] == NOV23_BARS_SHA256
    assert run1["arm_params"] == run2["arm_params"]
    assert run1["summary"]["error_count"] == 0
    assert len(run1["results"]) == 9
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)
    # Exit binding telemetry is present and well-formed.
    for r in run1["results"]:
        assert isinstance(r.get("exit_binding"), list) and r["exit_binding"]
        for eb in r["exit_binding"]:
            assert set(eb["component_sls"]) == {
                "atr_sl", "mfe_lock_sl", "r_lock_floor", "be_sl", "auto_be_sl", "milestone_floor"
            }
            if eb["exit_reason_code"] == "SL":
                assert eb["binding_component"]


@pytest.mark.parametrize("kind", ["normal_retracement", "high_atr", "sharp_reversal"])
def test_ladder_candidates_premature_not_worse(tmp_path, kind):
    runs = {m: _run_scenario(tmp_path, kind, m) for m in ("s105", "l2pp", "l3pp")}
    mets = {}
    for m, payload in runs.items():
        assert payload["summary"]["error_count"] == 0
        mets[m] = harness._tight_comparison_metrics(payload["results"])
    # Reducing clipping-tier protection must not INCREASE premature stop-outs.
    assert mets["l2pp"]["premature_stop_outs"] <= mets["s105"]["premature_stop_outs"]
    assert mets["l3pp"]["premature_stop_outs"] <= mets["s105"]["premature_stop_outs"]
    assert mets["l2pp"]["stop_modify_failures"] == 0
    assert mets["l3pp"]["stop_modify_failures"] == 0


# ---------------------------------------------------------------------------
# RESEARCH wick-aware activation (scripts-only gate; production unchanged).
# A = candle-close confirmation; B = N-consecutive persistence; C = immediate.
# ---------------------------------------------------------------------------

WICK_TIERS = harness._parse_wick_tiers(harness.LADDER_CURRENT_TIERS)


def _wick_pol():
    return _policy(**{**TIGHT_KNOBS, SCALE_KNOB: 1.05})


def _wick_pick(pol, **kw):
    return _pick(pol, **kw)


def test_wick_gate_close_confirmation_suppresses_until_close():
    tr = harness._WickConfirmationTracker("close", WICK_TIERS)
    pol = _wick_pol()
    # Intra-bar wick pushes MFE to 5 (ratio 0.5 >= 0.40) but NO bar has closed.
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
              current_sl=3562.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    import dynamic_risk
    res = dynamic_risk.compute_dynamic_atr_trail_stop(**kw)
    assert res["mfe_lock_sl"] is not None  # production arms immediately
    gated = harness._apply_wick_gate(res, kw, tr)
    assert gated["mfe_lock_sl"] is None
    assert gated["wick_gate_suppressed"] is True
    # Re-pick = tightest OTHER candidate: BE_LOCK 3600.50 (> auto-BE 3600, > trail).
    assert gated["stop_source"] == "BE_LOCK"
    assert gated["candidate_sl"] == pytest.approx(3600.50)
    assert gated["should_update"] is True
    # MFE/peak fields untouched (wick tracking preserved, independent of TP moves).
    assert gated["mfe_dollars"] == res["mfe_dollars"]
    assert gated["peak_price"] == res["peak_price"]
    # A wick-only bar that CLOSES below the threshold does NOT confirm.
    tr.register(3600.0, 1.0)
    tr.finalize_bar(3603.0, 10.0)  # close ratio 0.30 < 0.40
    gated2 = harness._apply_wick_gate(dynamic_risk.compute_dynamic_atr_trail_stop(**kw), kw, tr)
    assert gated2["mfe_lock_sl"] is None
    # A bar CLOSING with ratio >= 0.40 confirms the tier -> lock allowed.
    tr.finalize_bar(3604.5, 10.0)  # close ratio 0.45 >= 0.40
    allowed = harness._apply_wick_gate(dynamic_risk.compute_dynamic_atr_trail_stop(**kw), kw, tr)
    assert allowed["mfe_lock_sl"] == res["mfe_lock_sl"]
    assert allowed["stop_source"] == res["stop_source"]
    assert "wick_gate_suppressed" not in allowed


def test_wick_gate_persistence_requires_n_consecutive_closes():
    pol = _wick_pol()
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
              current_sl=3562.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    import dynamic_risk
    for mode, n in (("persist2", 2), ("persist3", 3)):
        tr = harness._WickConfirmationTracker(mode, WICK_TIERS)
        tr.register(3600.0, 1.0)
        for i in range(n - 1):
            tr.finalize_bar(3604.5, 10.0)
            gated = harness._apply_wick_gate(dynamic_risk.compute_dynamic_atr_trail_stop(**kw), kw, tr)
            assert gated["mfe_lock_sl"] is None, f"{mode} confirmed early after {i+1} closes"
        tr.finalize_bar(3604.5, 10.0)
        allowed = harness._apply_wick_gate(dynamic_risk.compute_dynamic_atr_trail_stop(**kw), kw, tr)
        assert allowed["mfe_lock_sl"] is not None, f"{mode} not confirmed after {n} closes"
        # A close below threshold resets the streak for a FRESH tracker.
        tr2 = harness._WickConfirmationTracker(mode, WICK_TIERS)
        tr2.register(3600.0, 1.0)
        tr2.finalize_bar(3604.5, 10.0)
        tr2.finalize_bar(3603.0, 10.0)  # breaks the streak
        tr2.finalize_bar(3604.5, 10.0)
        if n == 3:
            gated = harness._apply_wick_gate(dynamic_risk.compute_dynamic_atr_trail_stop(**kw), kw, tr2)
            assert gated["mfe_lock_sl"] is None


def test_wick_gate_never_loosens_and_sell_mirror():
    tr = harness._WickConfirmationTracker("close", WICK_TIERS)
    pol = _wick_pol()
    import dynamic_risk
    # Established tighter stop must not be loosened by the re-picked candidate.
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
              current_sl=3603.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    gated = harness._apply_wick_gate(dynamic_risk.compute_dynamic_atr_trail_stop(**kw), kw, tr)
    assert gated["mfe_lock_sl"] is None
    assert gated["should_update"] is False
    # SELL mirror: re-pick takes the MIN (tightest) other candidate.
    trs = harness._WickConfirmationTracker("close", WICK_TIERS)
    kws = dict(direction="SELL", entry_price=3600.0, live_price=3595.0, prev_peak=3595.0,
               current_sl=3638.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    ress = dynamic_risk.compute_dynamic_atr_trail_stop(**kws)
    gsell = harness._apply_wick_gate(ress, kws, trs)
    assert gsell["mfe_lock_sl"] is None
    assert gsell["stop_source"] == "BE_LOCK"
    assert gsell["candidate_sl"] == pytest.approx(3599.50)


def test_wick_research_arms_params_identical_to_baseline():
    for arm in ("wc", "wp2", "wp3"):
        assert harness.WICK_RESEARCH_ARMS[arm]["params"] == harness.ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS
    assert harness.WICK_RESEARCH_ARMS["wc"]["gate"] == "close"
    assert harness.WICK_RESEARCH_ARMS["wp2"]["gate"] == "persist2"
    assert harness.WICK_RESEARCH_ARMS["wp3"]["gate"] == "persist3"


def _run_wick_scenario(tmp_path, kind, arm):
    tape = FIXTURE_DIR / f"tape_scenario_{kind}.json"
    signals = FIXTURE_DIR / f"signals_scenario_{kind}.json"
    bars = harness.fetch_m1_bars(bars_json_path=tape)
    bars_file = tmp_path / f"bars_{kind}_{arm}.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    gate = harness.WICK_RESEARCH_ARMS[arm]["gate"] if arm in harness.WICK_RESEARCH_ARMS else None
    return harness._run_arm_subprocess(
        arm, REPO_ROOT, bars_file, tmp_path / f"{kind}_{arm}.json",
        catalog_path=signals, protection_telemetry=True, wick_gate=gate,
    )


def test_two_run_determinism_wc_on_nov23(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=NOV23_TAPE)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    run1 = harness._run_arm_subprocess(
        "wc", REPO_ROOT, bars_file, tmp_path / "wc_run1.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True, wick_gate="close",
    )
    run2 = harness._run_arm_subprocess(
        "wc", REPO_ROOT, bars_file, tmp_path / "wc_run2.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True, wick_gate="close",
    )
    assert run1["bars_sha256"] == run2["bars_sha256"] == NOV23_BARS_SHA256
    assert run1["arm_params"] == run2["arm_params"]
    assert run1["arm_params"]["wick_gate"] == "close"
    assert run1["summary"]["error_count"] == 0
    assert len(run1["results"]) == 9
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)
    for r in run1["results"]:
        assert r.get("wick_gate", {}).get("mode") == "close"


@pytest.mark.parametrize("kind", ["high_atr", "sharp_reversal", "normal_retracement"])
def test_wick_variants_replay_invariants(tmp_path, kind):
    # NOTE: premature stop-outs are NOT guaranteed to decrease - on
    # normal_retracement the gate lets the trade ride higher but the lagging
    # confirmation exits at the looser BE fallback (a measured research finding,
    # reported to the controller). The guaranteed invariants are: zero errors,
    # zero stop-mod failures, gate telemetry present, and the gate actually
    # engaging (suppression or confirmation) on these wicky tapes.
    runs = {m: _run_wick_scenario(tmp_path, kind, m) for m in ("s105", "wc", "wp2", "wp3")}
    for m, payload in runs.items():
        assert payload["summary"]["error_count"] == 0
        met = harness._tight_comparison_metrics(payload["results"])
        assert met["stop_modify_failures"] == 0
    for m in ("wc", "wp2", "wp3"):
        r = runs[m]["results"][0]
        wg = r.get("wick_gate") or {}
        assert wg.get("mode") == harness.WICK_RESEARCH_ARMS[m]["gate"]
        assert int(wg.get("suppressed_calls", 0)) > 0 or float(wg.get("max_confirmed_tier", 0.0)) > 0.0


# ---------------------------------------------------------------------------
# RESEARCH ATR-scaled interim floor (extra tightest-stop candidate; immediate
# MFE ladder UNCHANGED; never-loosen; binds only when tighter).
# ---------------------------------------------------------------------------

def _floor_pick(pol, **kw):
    import dynamic_risk
    return dynamic_risk.compute_dynamic_atr_trail_stop(**kw)

def test_interim_floor_arms_only_after_peak_reaches_level():
    pol = _wick_pol()
    stats = {}
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3602.4, prev_peak=3602.4,
              current_sl=3562.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    res = _floor_pick(pol, **kw)
    out = harness._apply_interim_floor(res, kw, "k25", stats)
    assert out is res  # MFE 2.4 < k*ATR 2.5 -> untouched
    kw2 = {**kw, "live_price": 3602.5, "prev_peak": 3602.5}
    res2 = _floor_pick(pol, **kw2)
    out2 = harness._apply_interim_floor(res2, kw2, "k25", stats)
    assert out2["interim_floor_armed"] is True
    assert out2["interim_floor_sl"] == pytest.approx(3602.5)
    assert out2["stop_source"] == "INTERIM_FLOOR"
    assert out2["candidate_sl"] == pytest.approx(3602.5)
    assert out2["should_update"] is True

def test_interim_floor_binds_only_when_tighter():
    pol = _wick_pol()
    stats = {}
    # MFE 5: ladder lock 3602.25; k25 floor 3602.50 is TIGHTER -> floor binds.
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
              current_sl=3562.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    out = harness._apply_interim_floor(_floor_pick(pol, **kw), kw, "k25", stats)
    assert out["stop_source"] == "INTERIM_FLOOR"
    assert out["candidate_sl"] == pytest.approx(3602.5)
    # MFE 8: ladder lock 3605.20 is tighter than the 3602.50 floor -> ladder wins,
    # exactly the production candidate (no conflict).
    kw8 = {**kw, "live_price": 3608.0, "prev_peak": 3608.0}
    res8 = _floor_pick(pol, **kw8)
    out8 = harness._apply_interim_floor(res8, kw8, "k25", stats)
    assert out8["stop_source"] == res8["stop_source"] == "MFE_LOCK"
    assert out8["candidate_sl"] == res8["candidate_sl"]

def test_interim_floor_never_loosens_and_preserves_mfe_activation():
    pol = _wick_pol()
    stats = {}
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3605.0, prev_peak=3605.0,
              current_sl=3604.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    res = _floor_pick(pol, **kw)
    out = harness._apply_interim_floor(res, kw, "k25", stats)
    assert out["should_update"] is False  # established 3604 tighter than floor 3602.5
    # MFE activation NOT delayed: ladder fields identical with/without the floor.
    for key in ("mfe_lock_sl", "mfe_lock_fraction", "mfe_lock_profit", "mfe_lock_tier",
                "mfe_dollars", "peak_price", "trail_activation_threshold"):
        assert out[key] == res[key]

def test_interim_floor_fraction_mode_and_sell_mirror():
    pol = _wick_pol()
    stats = {}
    # f30 arms at ratio >= 0.20: MFE 2.0 / ATR 10 -> floor 3600 + 0.30*2 = 3600.6.
    kw = dict(direction="BUY", entry_price=3600.0, live_price=3602.0, prev_peak=3602.0,
              current_sl=3562.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    out = harness._apply_interim_floor(_floor_pick(pol, **kw), kw, "f30", stats)
    assert out["interim_floor_sl"] == pytest.approx(3600.6)
    kw_below = {**kw, "live_price": 3601.9, "prev_peak": 3601.9}
    res_below = _floor_pick(pol, **kw_below)
    assert harness._apply_interim_floor(res_below, kw_below, "f30", stats) is res_below
    # SELL mirror: floor below entry; tightest = MIN.
    kws = dict(direction="SELL", entry_price=3600.0, live_price=3597.5, prev_peak=3597.5,
               current_sl=3638.0, atr=10.0, policy=pol, auto_be_threshold=3.0)
    outs = harness._apply_interim_floor(_floor_pick(pol, **kws), kws, "k25", stats)
    assert outs["interim_floor_sl"] == pytest.approx(3597.5)
    assert outs["stop_source"] == "INTERIM_FLOOR"
    assert outs["candidate_sl"] == pytest.approx(3597.5)

def test_floor_arms_params_identical_to_baseline():
    for arm in harness.FLOOR_RESEARCH_ARMS:
        assert harness.FLOOR_RESEARCH_ARMS[arm]["params"] == harness.ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS
    assert [harness.FLOOR_RESEARCH_ARMS[a]["floor"] for a in ("ifk25", "ifk35", "ifk50", "iff30", "iff40")] == ["k25", "k35", "k50", "f30", "f40"]

def _run_floor_scenario(tmp_path, kind, arm):
    tape = FIXTURE_DIR / f"tape_scenario_{kind}.json"
    signals = FIXTURE_DIR / f"signals_scenario_{kind}.json"
    bars = harness.fetch_m1_bars(bars_json_path=tape)
    bars_file = tmp_path / f"bars_{kind}_{arm}.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    spec = harness.FLOOR_RESEARCH_ARMS[arm]["floor"] if arm in harness.FLOOR_RESEARCH_ARMS else None
    return harness._run_arm_subprocess(
        arm, REPO_ROOT, bars_file, tmp_path / f"{kind}_{arm}.json",
        catalog_path=signals, protection_telemetry=True, interim_floor=spec,
    )

def test_two_run_determinism_ifk35_on_nov23(tmp_path):
    bars = harness.fetch_m1_bars(bars_json_path=NOV23_TAPE)
    bars_file = tmp_path / "bars.json"
    bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    run1 = harness._run_arm_subprocess(
        "ifk35", REPO_ROOT, bars_file, tmp_path / "ifk35_run1.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True, interim_floor="k35",
    )
    run2 = harness._run_arm_subprocess(
        "ifk35", REPO_ROOT, bars_file, tmp_path / "ifk35_run2.json",
        catalog_path=NOV23_SIGNALS, protection_telemetry=True, interim_floor="k35",
    )
    assert run1["bars_sha256"] == run2["bars_sha256"] == NOV23_BARS_SHA256
    assert run1["arm_params"] == run2["arm_params"]
    assert run1["arm_params"]["interim_floor"] == "k35"
    assert run1["summary"]["error_count"] == 0
    assert len(run1["results"]) == 9
    assert json.dumps(run1["results"], sort_keys=True) == json.dumps(run2["results"], sort_keys=True)
    assert json.dumps(run1["summary"], sort_keys=True) == json.dumps(run2["summary"], sort_keys=True)
    for r in run1["results"]:
        assert r.get("interim_floor", {}).get("spec") == "k35"

@pytest.mark.parametrize("kind", ["sharp_reversal", "normal_retracement"])
def test_floor_replay_invariants(tmp_path, kind):
    for arm in ("s105", "ifk25", "ifk35", "ifk50", "iff30", "iff40"):
        payload = _run_floor_scenario(tmp_path, kind, arm)
        assert payload["summary"]["error_count"] == 0
        met = harness._tight_comparison_metrics(payload["results"])
        assert met["stop_modify_failures"] == 0
        if arm in harness.FLOOR_RESEARCH_ARMS:
            r = payload["results"][0]
            assert r.get("interim_floor", {}).get("spec") == harness.FLOOR_RESEARCH_ARMS[arm]["floor"]
