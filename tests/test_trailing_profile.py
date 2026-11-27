"""TRAILING_PROFILE (baseline|t2|t3) - production-selectable validated A/B trailing arms.

Development/validation only: every executor here is dry-run; no live trades, no
broker connection, no .env mutation. Covers the enable-t2t3 brief:

  * baseline/empty keeps the shipped defaults (ACTIVATION 1.00 / MAX 8.00);
  * t2 -> ACTIVATION 1.00 + MAX 5.50, t3 -> ACTIVATION 0.70 + MAX 5.50, and the
    profile overrides ONLY those two knobs (all other trail knobs unchanged);
  * the profile effect reaches mt5_executor._get_dynamic_risk_policy() through
    the existing whitelist, while TRAILING_PROFILE itself stays OUT of the
    policy proxy / import snapshot (it is applied at import, not a policy field);
  * protective invariants under t2/t3: forward-only never-loosens sweeps, the
    tightest-candidate picker, a tighter MAX never loosening a protective stop,
    stops_buffer clamping and the retcode-10016 / freeze-level two-step abort
    (patterns reused from test_v390_atr_compression.py and
    test_dynamic_risk_management.py);
  * invalid values fall back to baseline via _env_choice;
  * a non-vacuous mutation check proving the mapping assertions fail when the
    profile override is wrong.

Config isolation mirrors the conftest 'shipped_config' neutralized-reload
pattern: dotenv loading is patched to a no-op, every strategy knob is delenv'd,
and config is importlib.reload()'d so the asserted values are the shipped
literals plus the requested TRAILING_PROFILE, independent of the ambient .env.
"""
from __future__ import annotations

import copy
import importlib
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
import dynamic_risk
import mt5_executor

# Runtime-isolation knobs managed by tests/conftest.py::_apply_isolated_env.
# Kept set so a neutralized reload still runs DRY_RUN=True against temp paths.
_ISOLATION_ENV_KNOBS = frozenset({
    "DRY_RUN",
    "LOG_FILE_PATH",
    "STATE_FILE_PATH",
    "TRADES_LEDGER_PATH",
    "LOCK_FILE_PATH",
    "PREVENT_SYSTEM_SLEEP",
    "CONSOLE_TITLE_UPDATES",
})

ACT_KNOB = "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT"
MAX_KNOB = "DYNAMIC_ATR_TRAIL_MAX_DOLLARS"

_PROFILE_EXPECT = {
    "baseline": {ACT_KNOB: 1.00, MAX_KNOB: 8.00},
    "t2": {ACT_KNOB: 1.00, MAX_KNOB: 5.50},
    "t3": {ACT_KNOB: 0.70, MAX_KNOB: 5.50},
}

# Every OTHER trail / BE knob the profile must leave exactly as parsed.
_OTHER_TRAIL_KNOBS = {
    "DYNAMIC_ATR_TRAIL_ENABLED": True,
    "DYNAMIC_ATR_TRAIL_TIMEFRAME": "M5",
    "DYNAMIC_ATR_TRAIL_MULT": 1.40,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS": 10.00,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MULT": 1.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS": 18.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MULT": 0.80,
    "DYNAMIC_ATR_TRAIL_MIN_DOLLARS": 2.50,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS": 2.20,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS": 5.50,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS": 2.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS": 4.00,
    "DYNAMIC_ATR_TRAIL_STEP_DOLLARS": 0.25,
    "DYNAMIC_ATR_BE_ACTIVATION_MULT": 0.50,
    "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS": 0.60,
    "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS": 1.20,
    "DYNAMIC_ATR_BE_MIN_DISTANCE_DOLLARS": 0.60,
    "DYNAMIC_ATR_BE_R_ACTIVATION_MULT": 0.25,
}


@pytest.fixture
def profile_loader():
    """Factory reloading config with .env neutralized and the given env applied.

    Mirrors conftest.shipped_config, but allows injecting TRAILING_PROFILE (or
    any knob override) BEFORE the reload so the import-time profile preset runs
    against a clean, .env-independent environment. Teardown restores the original
    environment + dotenv loader and reloads config back to the ambient values.
    """
    import dotenv

    knob_names = sorted({k for k in vars(config) if k.isupper()} - _ISOLATION_ENV_KNOBS)
    saved_env = {k: os.environ.get(k) for k in knob_names}
    saved_loader = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: None

    def load(env=None):
        for k in knob_names:
            os.environ.pop(k, None)
        for k, v in (env or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        importlib.reload(config)
        return config

    try:
        yield SimpleNamespace(load=load)
    finally:
        dotenv.load_dotenv = saved_loader
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)


def _assert_field(actual, expected, label):
    if isinstance(expected, float):
        assert actual == pytest.approx(expected), label
    else:
        assert actual == expected, label


def _assert_profile(cfg, profile):
    """Canonical mapping assertion (also used by the mutation check)."""
    assert cfg.TRAILING_PROFILE == profile, "TRAILING_PROFILE"
    for knob, expected in _PROFILE_EXPECT[profile].items():
        _assert_field(getattr(cfg, knob), expected, knob)
    for knob, expected in _OTHER_TRAIL_KNOBS.items():
        _assert_field(getattr(cfg, knob), expected, knob + " must be unchanged")
    # R-lock default must stay False; the profile never touches it.
    assert cfg.R_LOCK_ENABLED is False, "R_LOCK_ENABLED default must stay False"
    # MIN <= MAX pair validation still holds after the override (2.50 <= 5.50).
    cfg.validate_dynamic_risk_pairs()
    assert cfg.DYNAMIC_ATR_TRAIL_MIN_DOLLARS <= cfg.DYNAMIC_ATR_TRAIL_MAX_DOLLARS


def _trail(pol, mfe, atr, entry=3850.0, direction="BUY", current_sl=0.0, **kw):
    """Same helper shape as tests/test_v390_atr_compression.py::_trail."""
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


# --- 1. baseline default + t2/t3 knob mapping -------------------------------


def test_baseline_default_when_unset(profile_loader):
    cfg = profile_loader.load({})
    _assert_profile(cfg, "baseline")


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_profile_is_baseline(profile_loader, raw):
    cfg = profile_loader.load({"TRAILING_PROFILE": raw})
    _assert_profile(cfg, "baseline")


@pytest.mark.parametrize("profile", ["t2", "t3"])
def test_profile_knob_mapping(profile_loader, profile):
    cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    _assert_profile(cfg, profile)


@pytest.mark.parametrize("raw,expected", [("T3", "t3"), (" t3 ", "t3"), ("T2", "t2"), ("Baseline", "baseline")])
def test_profile_choice_is_case_insensitive(profile_loader, raw, expected):
    # _env_choice upper-cases before matching; the config normalises back to lower.
    cfg = profile_loader.load({"TRAILING_PROFILE": raw})
    _assert_profile(cfg, expected)


@pytest.mark.parametrize("raw", ["t1", "t4", "experimental", "bogus", "0.7"])
def test_invalid_profile_falls_back_to_baseline(profile_loader, raw):
    # t1/t4 are replay-only arms and must NOT be production-selectable.
    cfg = profile_loader.load({"TRAILING_PROFILE": raw})
    _assert_profile(cfg, "baseline")


# --- 2. propagation through the executor whitelist policy proxy -------------


@pytest.mark.parametrize("profile", ["baseline", "t2", "t3"])
def test_profile_effect_reaches_executor_policy_proxy(profile_loader, profile):
    cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    expected = _PROFILE_EXPECT[profile]
    assert pol.trail_activation_atr_mult == pytest.approx(expected[ACT_KNOB])
    assert pol.trail_max_dollars == pytest.approx(expected[MAX_KNOB])
    # Untouched knobs stay canonical through the same proxy.
    assert pol.trail_atr_mult == pytest.approx(1.40)
    assert pol.trail_min_dollars == pytest.approx(2.50)
    assert pol.trail_extended_max_dollars == pytest.approx(5.50)
    assert pol.trail_homerun_max_dollars == pytest.approx(4.00)
    assert pol.trail_step_dollars == pytest.approx(0.25)
    assert ex._get_lifecycle_policy().r_lock_enabled is False


def test_trailing_profile_not_added_to_policy_proxy(profile_loader, monkeypatch):
    profile_loader.load({"TRAILING_PROFILE": "t3"})
    captured = {}
    original = mt5_executor.read_dynamic_risk_policy

    def capture(proxy):
        captured["keys"] = set(vars(proxy))
        return original(proxy)

    monkeypatch.setattr(mt5_executor, "read_dynamic_risk_policy", capture)
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._get_dynamic_risk_policy()
    assert "TRAILING_PROFILE" not in captured["keys"], "profile is applied at import, not a policy field"
    assert "TRAILING_PROFILE" not in mt5_executor._CFG_IMPORT_SNAPSHOT
    assert not hasattr(ex._get_dynamic_risk_policy(), "TRAILING_PROFILE")


# --- 3. protective invariants under t2/t3 ------------------------------------


@pytest.mark.parametrize("profile", ["t2", "t3"])
@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_forward_only_never_loosens_sweep(profile_loader, profile, direction):
    cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    pol = dynamic_risk.read_dynamic_risk_policy(cfg)
    sign = 1.0 if direction == "BUY" else -1.0
    atr = 6.0
    entry = 3850.0
    activation = _PROFILE_EXPECT[profile][ACT_KNOB] * atr
    last_applied = 0.0
    prev_atr_sl = None
    prev_dist = None
    mfe = activation
    while mfe <= 30.0 + 1e-9:
        res = _trail(pol, mfe, atr, entry=entry, direction=direction, current_sl=last_applied)
        assert res["trail_armed"] is True
        assert res["atr_sl"] is not None
        if prev_atr_sl is not None:
            if sign > 0:
                assert res["atr_sl"] >= prev_atr_sl - 1e-9, f"ATR trail loosened at MFE={mfe}"
            else:
                assert res["atr_sl"] <= prev_atr_sl + 1e-9, f"ATR trail loosened at MFE={mfe}"
            assert res["trail_dist"] <= prev_dist + 1e-9, f"trail_dist grew at MFE={mfe}"
        if res["should_update"]:
            if last_applied > 0.0:
                assert sign * (res["candidate_sl"] - last_applied) >= 0.25 - 1e-9
            last_applied = res["candidate_sl"]
        elif last_applied > 0.0:
            # A rejected candidate is never applied; it must be below the step threshold.
            assert sign * (res["candidate_sl"] - last_applied) < 0.25 + 1e-9
        prev_atr_sl = res["atr_sl"]
        prev_dist = res["trail_dist"]
        mfe += 0.25
    assert last_applied > 0.0, "sweep must actually apply protective stops (non-vacuous)"


@pytest.mark.parametrize("profile", ["t2", "t3"])
def test_tightest_candidate_picker(profile_loader, profile):
    cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    pol = dynamic_risk.read_dynamic_risk_policy(cfg)
    # EXTENDED zone (MFE 12): dist clamp(6.0, [2.20, 5.50]) = 5.5 -> atr_sl 3856.5.
    res = _trail(pol, 12.0, 6.0)
    atr_sl = res["atr_sl"]
    assert atr_sl == pytest.approx(3856.5)
    assert res["stop_source"] == "ATR_TRAIL"
    looser = _trail(pol, 12.0, 6.0, r_lock_floor=atr_sl - 1.0)
    assert looser["stop_source"] == "ATR_TRAIL" and looser["r_lock_floor"] is None
    tighter = _trail(pol, 12.0, 6.0, r_lock_floor=atr_sl + 1.0)
    assert tighter["stop_source"] == "R_LOCK"
    assert tighter["candidate_sl"] == pytest.approx(atr_sl + 1.0)
    # STANDARD zone under the tighter MAX: dist clamp(8.4, [2.50, 5.50]) = 5.5 ->
    # atr_sl 3852.5 still beats the armed BE lock (3851.20): tightest wins.
    std = _trail(pol, 8.0, 6.0)
    assert std["trail_zone"] == "STANDARD"
    assert std["trail_dist"] == pytest.approx(5.5)
    assert std["be_armed"] is True and std["be_sl"] == pytest.approx(3851.2)
    assert std["stop_source"] == "ATR_TRAIL" and std["candidate_sl"] == pytest.approx(3852.5)


@pytest.mark.parametrize("profile", ["t2", "t3"])
@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_tighter_max_never_loosens_protective_stop(profile_loader, profile, direction):
    base_cfg = profile_loader.load({})
    base_pol = dynamic_risk.read_dynamic_risk_policy(base_cfg)
    prof_cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    prof_pol = dynamic_risk.read_dynamic_risk_policy(prof_cfg)
    sign = 1.0 if direction == "BUY" else -1.0
    # STANDARD zone with a binding clamp: raw 1.40 * 8.0 = 11.20 -> baseline caps
    # at 8.00, t2/t3 cap at 5.50, so the profile stop is strictly TIGHTER.
    atr, mfe, entry = 8.0, 8.0, 3850.0
    base = _trail(base_pol, mfe, atr, entry=entry, direction=direction)
    prof = _trail(prof_pol, mfe, atr, entry=entry, direction=direction)
    assert base["trail_dist"] == pytest.approx(8.0)
    assert prof["trail_dist"] == pytest.approx(5.5)
    assert sign * (prof["candidate_sl"] - base["candidate_sl"]) > 0.0, "tighter MAX must tighten, not loosen"
    # Feeding the baseline protective stop as current_sl: the profile may only
    # tighten it (forward-only), never propose a looser stop.
    res = _trail(prof_pol, mfe, atr, entry=entry, direction=direction, current_sl=base["candidate_sl"])
    assert sign * (res["candidate_sl"] - base["candidate_sl"]) >= -1e-9
    assert res["should_update"] is True
    # A current stop already tighter than the new candidate is never loosened.
    tighter_cur = entry + sign * (mfe - 2.0)
    keep = _trail(prof_pol, mfe, atr, entry=entry, direction=direction, current_sl=tighter_cur)
    assert keep["should_update"] is False


@pytest.mark.parametrize("profile", ["t2", "t3"])
def test_stops_buffer_clamp_under_profile(profile_loader, profile):
    cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    pol = dynamic_risk.read_dynamic_risk_policy(cfg)
    # BUY: raw atr_sl 3892.5 would sit inside the broker minimum distance;
    # the stops_buffer clamp pulls it down to live - buffer (never closer).
    buy = _trail(pol, 8.0, 6.0, entry=3890.0, direction="BUY", stops_buffer=6.0)
    assert buy["atr_sl"] == pytest.approx(3892.5)
    assert buy["candidate_sl"] == pytest.approx(3892.0)
    assert buy["candidate_sl"] <= 3898.0 - 6.0 + 1e-9
    # SELL mirror: clamp pushes the stop up to live + buffer.
    sell = _trail(pol, 8.0, 6.0, entry=3890.0, direction="SELL", stops_buffer=6.0)
    assert sell["atr_sl"] == pytest.approx(3887.5)
    assert sell["candidate_sl"] == pytest.approx(3888.0)
    assert sell["candidate_sl"] >= 3882.0 + 6.0 - 1e-9


def _engine_sig():
    """Same shape as the two-step abort sig in test_dynamic_risk_management.py."""
    return {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3875.0,
        "layers": {"L1": {"lot": 0.01, "entry_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1",
                          "ticket": 701, "order_type": "MARKET", "status": "OPEN", "role": "CORE_RUNNER"}},
        "market_tickets": [701],
        "runner_tickets": [701],
        "runner_layers": ["L1"],
        "runner_ticket": 701,
        "runner_layer": "L1",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0},
    }


@pytest.mark.parametrize("profile", ["t2", "t3"])
def test_engine_stops_and_freeze_two_step_abort_under_profile(profile_loader, monkeypatch, tmp_path, profile):
    """Retcode 10016 / freeze-level aborts never touch broker SL/TP under t2/t3.

    Reuses the two-step pattern from test_dynamic_risk_management.py, with a
    positive control first so the rejection assertions are non-vacuous.
    """
    import main
    from state_manager import StateManager
    from trade_ledger import TradeLedger

    cfg = profile_loader.load({"TRAILING_PROFILE": profile})
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / (profile + "_state.json")))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / (profile + "_ledger.jsonl")))
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 6.0
    monkeypatch.setattr(main, "executor", ex)
    monkeypatch.setattr(main, "TRADING_HALTED", False)

    pol = ex._get_dynamic_risk_policy()
    assert pol.trail_activation_atr_mult == pytest.approx(_PROFILE_EXPECT[profile][ACT_KNOB])
    assert pol.trail_max_dollars == pytest.approx(_PROFILE_EXPECT[profile][MAX_KNOB])
    tick = SimpleNamespace(bid=3898.0, ask=3898.2, time=1700000000.0)

    # A) Positive control: the engine DOES trail under the profile, and every SL
    #    sent to the broker is at/above the initial protective stop (never loosens).
    with patch.object(ex, "modify_position_sltp", wraps=ex.modify_position_sltp) as spy_ok:
        r_ok = ex.evaluate_dynamic_atr_engine(copy.deepcopy(_engine_sig()), tick=tick)
    updated = [t[0] if isinstance(t, (tuple, list)) else t for t in r_ok["updated_tickets"]]
    assert 701 in updated, "positive control: profile must actually trail"
    assert spy_ok.call_count > 0
    sl_sent_values = []
    for call in spy_ok.call_args_list:
        sl_sent = call.kwargs.get("sl")
        if sl_sent is None and len(call.args) > 1:
            sl_sent = call.args[1]
        if sl_sent is not None:
            assert sl_sent >= 3875.0 - 1e-9, "engine must never loosen the broker SL"
            sl_sent_values.append(sl_sent)
    # STANDARD-zone stop under the tighter MAX: peak 3898 - clamp(8.4, [2.5, 5.5]) = 3892.5.
    assert sl_sent_values, "positive control must send a protective SL"
    assert max(sl_sent_values) == pytest.approx(3892.5)

    # B) 10016 SL rejection -> aborted, ticket not updated.
    def _reject_sl(ticket, sl=None, tp=None, require_both=False):
        ex.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": 10016}
        return False

    with patch.object(ex, "modify_position_sltp", side_effect=_reject_sl):
        r_10016 = ex.evaluate_dynamic_atr_engine(copy.deepcopy(_engine_sig()), tick=tick)
    assert r_10016["updated_tickets"] == []
    assert r_10016["tp_lifted_tickets"] == []

    # C) freeze_level aborts without even calling modify_position_sltp.
    with patch.object(ex, "is_within_freeze_level", return_value=True), \
         patch.object(ex, "modify_position_sltp", wraps=ex.modify_position_sltp) as spy_frz:
        r_frz = ex.evaluate_dynamic_atr_engine(copy.deepcopy(_engine_sig()), tick=tick)
    assert spy_frz.call_count == 0
    assert r_frz["updated_tickets"] == []
    assert r_frz["tp_lifted_tickets"] == []


# --- 4. non-vacuous mutation check -------------------------------------------


def test_mapping_assertions_fail_under_mutation(profile_loader):
    """The canonical mapping assertion must FAIL when the override is wrong."""
    cfg = profile_loader.load({"TRAILING_PROFILE": "t3"})
    _assert_profile(cfg, "t3")  # sanity: passes on the real implementation

    # Mutant 1: the wrong arm selected (t2 instead of t3) fails the t3 assertion.
    t2_cfg = profile_loader.load({"TRAILING_PROFILE": "t2"})
    with pytest.raises(AssertionError):
        _assert_profile(t2_cfg, "t3")

    # Mutant 2: a broken override that forgets the MAX tightening (stays 8.00).
    mut_max = profile_loader.load({"TRAILING_PROFILE": "t3"})
    mut_max.DYNAMIC_ATR_TRAIL_MAX_DOLLARS = 8.00
    with pytest.raises(AssertionError):
        _assert_profile(mut_max, "t3")

    # Mutant 3: a broken override that forgets the ACTIVATION tightening (stays 1.00).
    mut_act = profile_loader.load({"TRAILING_PROFILE": "t3"})
    mut_act.DYNAMIC_ATR_TRAIL_ACTIVATION_MULT = 1.00
    with pytest.raises(AssertionError):
        _assert_profile(mut_act, "t3")
