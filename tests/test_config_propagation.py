"""Phase 1.3 - config whitelist guard + DYNAMIC_* / R_LOCK_* propagation tests.

Pins the _cfg monkeypatch-precedence semantics (mt5_executor.py:229-235) and the whitelist
proxies (_get_dynamic_risk_policy / _get_lifecycle_policy, mt5_executor.py:1398-1447):

  1. Parametrized sentinel-per-knob propagation for EVERY DYNAMIC_* knob (36) on both the
     config and mt5_executor monkeypatch targets, plus the 3 R_LOCK_* knobs via the
     lifecycle proxy. A dry-run executor is used; no connection / live trade is involved.
  2. Set-equality drift guards scoped STRICTLY to DYNAMIC_* (and R_LOCK_* for lifecycle):
     config knob set == proxy keys == _CFG_IMPORT_SNAPSHOT entries == reader getattr names.
  3. A test documenting that a knob MISSING from the proxy silently falls back to the
     dataclass default (the no-op failure mode the drift guard prevents).

These tests are .env-independent: sentinels are chosen distinct from the ambient value and
valid under the reader normalization, and the drift guards compare name sets, not values.
"""

import inspect
import re
from types import SimpleNamespace

import pytest

import config
import dynamic_risk
import mt5_executor
import risk_lifecycle


def _assert_field(actual, expected, label):
    if isinstance(expected, float):
        assert actual == pytest.approx(expected), label
    else:
        assert actual == expected, label


# Explicit knob -> DynamicRiskPolicy field map (all 36 DYNAMIC_* knobs).
_DYNAMIC_KNOB_FIELD = {
    "DYNAMIC_ATR_SLTP_ENABLED": "sltp_enabled",
    "DYNAMIC_SLTP_MODE": "sltp_mode",
    "DYNAMIC_ATR_TIMEFRAME": "atr_timeframe",
    "DYNAMIC_ATR_PERIOD": "atr_period",
    "DYNAMIC_ATR_BASELINE_DOLLARS": "atr_baseline_dollars",
    "DYNAMIC_SL_ATR_MULT": "sl_atr_mult",
    "DYNAMIC_TP_ATR_MULT": "tp_atr_mult",
    "DYNAMIC_TP_STEP_ATR_MULT": "tp_step_atr_mult",
    "DYNAMIC_SL_MIN_DOLLARS": "sl_min_dollars",
    "DYNAMIC_SL_MAX_DOLLARS": "sl_max_dollars",
    "DYNAMIC_TP_MIN_DOLLARS": "tp_min_dollars",
    "DYNAMIC_TP_MAX_DOLLARS": "tp_max_dollars",
    "DYNAMIC_SLTP_SCALE_MIN": "scale_min",
    "DYNAMIC_SLTP_SCALE_MAX": "scale_max",
    "DYNAMIC_ATR_TRAIL_ENABLED": "trail_enabled",
    "DYNAMIC_ATR_TRAIL_TIMEFRAME": "trail_timeframe",
    "DYNAMIC_ATR_TRAIL_MULT": "trail_atr_mult",
    "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS": "trail_extended_mfe_dollars",
    "DYNAMIC_ATR_TRAIL_EXTENDED_MULT": "trail_extended_atr_mult",
    "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS": "trail_homerun_mfe_dollars",
    "DYNAMIC_ATR_TRAIL_HOMERUN_MULT": "trail_homerun_atr_mult",
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": "trail_activation_atr_mult",
    "DYNAMIC_ATR_TRAIL_MIN_DOLLARS": "trail_min_dollars",
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": "trail_max_dollars",
    "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS": "trail_extended_min_dollars",
    "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS": "trail_extended_max_dollars",
    "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS": "trail_homerun_min_dollars",
    "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS": "trail_homerun_max_dollars",
    "DYNAMIC_ATR_TRAIL_STEP_DOLLARS": "trail_step_dollars",
    "DYNAMIC_ATR_BE_ACTIVATION_MULT": "be_activation_atr_mult",
    "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS": "be_min_mfe_dollars",
    "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS": "be_lock_offset_dollars",
    "DYNAMIC_ATR_TP_TRAIL_ENABLED": "tp_trail_enabled",
    "DYNAMIC_ATR_TP_LIFT_ATR_MULT": "tp_lift_atr_mult",
    "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT": "tp_lift_progress_pct",
    "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT": "tp1_lift_min_progress_pct",
}

_R_LOCK_KNOB_FIELD = {
    "R_LOCK_ENABLED": "r_lock_enabled",
    "R_LOCK_TIERS": "r_lock_tiers",
    "R_LOCK_MIN_R_DOLLARS": "r_lock_min_r_dollars",
}

_BOOL_KNOBS = {"DYNAMIC_ATR_SLTP_ENABLED", "DYNAMIC_ATR_TRAIL_ENABLED", "DYNAMIC_ATR_TP_TRAIL_ENABLED"}
_MIN_KNOBS = {
    "DYNAMIC_SL_MIN_DOLLARS", "DYNAMIC_TP_MIN_DOLLARS", "DYNAMIC_SLTP_SCALE_MIN",
    "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS",
    "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS",
}
_MAX_KNOBS = {
    "DYNAMIC_SL_MAX_DOLLARS", "DYNAMIC_TP_MAX_DOLLARS", "DYNAMIC_SLTP_SCALE_MAX",
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS",
    "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS",
}
_MULT_KNOBS = {
    "DYNAMIC_SL_ATR_MULT", "DYNAMIC_TP_ATR_MULT", "DYNAMIC_TP_STEP_ATR_MULT",
    "DYNAMIC_ATR_TRAIL_MULT", "DYNAMIC_ATR_TRAIL_EXTENDED_MULT", "DYNAMIC_ATR_TRAIL_HOMERUN_MULT",
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", "DYNAMIC_ATR_BE_ACTIVATION_MULT", "DYNAMIC_ATR_TP_LIFT_ATR_MULT",
}


def _distinct(base, current, step):
    """Return base, or base+step when base collides with the ambient value (keeps validity)."""
    return base if base != current else base + step


def _dynamic_sentinel(knob, current):
    """A valid sentinel distinct from the ambient value, robust to reader normalization."""
    if knob in _BOOL_KNOBS:
        return not bool(current)
    if knob == "DYNAMIC_SLTP_MODE":
        return "PURE_ATR" if current != "PURE_ATR" else "ADAPTIVE"
    if knob in ("DYNAMIC_ATR_TIMEFRAME", "DYNAMIC_ATR_TRAIL_TIMEFRAME"):
        return "M1" if current != "M1" else "M5"
    if knob == "DYNAMIC_ATR_PERIOD":
        return _distinct(21, current, 1)
    if knob == "DYNAMIC_ATR_BASELINE_DOLLARS":
        return _distinct(7.25, current, 0.125)
    if knob in _MIN_KNOBS:
        # >= the reader 0.10 floor; the min field is not upper-clamped by the reader.
        return _distinct(0.50, current, 0.125)
    if knob in _MAX_KNOBS:
        # >= any plausible paired min, so max(lo, sentinel) == sentinel.
        return _distinct(99.0, current, 0.125)
    if knob == "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS":
        # min(ext, home) == ext for any home >= 0.50, so this is robust to ambient home.
        return _distinct(0.50, current, 0.125)
    if knob == "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS":
        # max(ext, home) == home for any ext <= 99, so this is robust to ambient ext.
        return _distinct(99.0, current, 0.125)
    if knob in _MULT_KNOBS:
        return _distinct(3.75, current, 0.125)
    if knob == "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS":
        return _distinct(1.25, current, 0.125)
    if knob == "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS":
        return _distinct(0.75, current, 0.125)
    if knob == "DYNAMIC_ATR_TRAIL_STEP_DOLLARS":
        return _distinct(0.60, current, 0.125)
    if knob == "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT":
        return _distinct(0.42, current, 0.125)  # stays within the reader [0.10, 0.99] clamp
    if knob == "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT":
        return _distinct(0.33, current, 0.125)  # stays within the reader [0.05, 0.99] clamp
    raise AssertionError("no sentinel defined for knob " + knob)


def _r_lock_sentinel(knob, current):
    if knob == "R_LOCK_ENABLED":
        return not bool(current)
    if knob == "R_LOCK_MIN_R_DOLLARS":
        return _distinct(0.75, current, 0.125)
    if knob == "R_LOCK_TIERS":
        # A valid, distinct tier string; parse_r_lock_tiers yields ((0.5, 0.1), (1.0, 0.3)).
        return "0.5:0.10,1.0:0.30"
    raise AssertionError("no sentinel defined for knob " + knob)


# --- 1. Parametrized sentinel-per-knob propagation -------------------------


@pytest.mark.parametrize("target", ["config", "mt5_executor"])
@pytest.mark.parametrize("knob", sorted(_DYNAMIC_KNOB_FIELD))
def test_dynamic_knob_propagates_to_policy(monkeypatch, knob, target):
    field = _DYNAMIC_KNOB_FIELD[knob]
    current = getattr(config, knob)
    sentinel = _dynamic_sentinel(knob, current)
    assert sentinel != current, "sentinel must differ from ambient to prove propagation"
    mod = config if target == "config" else mt5_executor
    monkeypatch.setattr(mod, knob, sentinel)
    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_dynamic_risk_policy()
    _assert_field(getattr(pol, field), sentinel, knob + " -> " + field + " via " + target)


@pytest.mark.parametrize("target", ["config", "mt5_executor"])
@pytest.mark.parametrize("knob", sorted(_R_LOCK_KNOB_FIELD))
def test_r_lock_knob_propagates_to_lifecycle_policy(monkeypatch, knob, target):
    field = _R_LOCK_KNOB_FIELD[knob]
    current = getattr(config, knob)
    sentinel = _r_lock_sentinel(knob, current)
    mod = config if target == "config" else mt5_executor
    monkeypatch.setattr(mod, knob, sentinel)
    ex = mt5_executor.MT5Executor(dry_run=True)
    pol = ex._get_lifecycle_policy()
    if knob == "R_LOCK_TIERS":
        assert pol.r_lock_tiers == ((0.5, 0.10), (1.0, 0.30)), knob + " via " + target
    else:
        _assert_field(getattr(pol, field), sentinel, knob + " -> " + field + " via " + target)


def test_executor_monkeypatch_takes_precedence_over_config(monkeypatch):
    # When BOTH modules are patched to distinct non-ambient sentinels, the mt5_executor
    # value wins (_cfg returns mod_val when it differs from the import snapshot).
    knob = "DYNAMIC_ATR_TRAIL_MULT"
    current = getattr(config, knob)
    cfg_sentinel = _distinct(3.75, current, 0.125)
    exe_sentinel = _distinct(5.25, current, 0.125)
    assert cfg_sentinel != exe_sentinel
    monkeypatch.setattr(config, knob, cfg_sentinel)
    monkeypatch.setattr(mt5_executor, knob, exe_sentinel)
    ex = mt5_executor.MT5Executor(dry_run=True)
    assert ex._get_dynamic_risk_policy().trail_atr_mult == pytest.approx(exe_sentinel)


# --- 2. Set-equality drift guards (scoped strictly to DYNAMIC_* / R_LOCK_*) --


def _reader_getattr_names(func):
    src = inspect.getsource(func)
    return set(re.findall(r"getattr\(\s*cfg_mod\s*,\s*\"([A-Za-z0-9_]+)\"", src))


def test_dynamic_knob_whitelist_drift_guard(monkeypatch):
    captured = {}
    original = mt5_executor.read_dynamic_risk_policy

    def capture(proxy):
        captured["keys"] = set(vars(proxy))
        return original(proxy)

    monkeypatch.setattr(mt5_executor, "read_dynamic_risk_policy", capture)
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._get_dynamic_risk_policy()

    config_set = {k for k in vars(config) if k.startswith("DYNAMIC_")}
    proxy_set = {k for k in captured["keys"] if k.startswith("DYNAMIC_")}
    snapshot_set = {k for k in mt5_executor._CFG_IMPORT_SNAPSHOT if k.startswith("DYNAMIC_")}
    reader_set = {n for n in _reader_getattr_names(dynamic_risk.read_dynamic_risk_policy) if n.startswith("DYNAMIC_")}

    assert config_set == proxy_set, (
        "config vs proxy drift: only-config=" + str(sorted(config_set - proxy_set))
        + " only-proxy=" + str(sorted(proxy_set - config_set))
    )
    assert config_set == snapshot_set, (
        "config vs snapshot drift: only-config=" + str(sorted(config_set - snapshot_set))
        + " only-snapshot=" + str(sorted(snapshot_set - config_set))
    )
    assert config_set == reader_set, (
        "config vs reader drift: only-config=" + str(sorted(config_set - reader_set))
        + " only-reader=" + str(sorted(reader_set - config_set))
    )
    assert len(config_set) == 51, "expected 51 DYNAMIC_* knobs, got " + str(len(config_set))


def test_r_lock_knob_whitelist_drift_guard(monkeypatch):
    captured = {}
    original = mt5_executor.read_lifecycle_policy

    def capture(proxy):
        captured["keys"] = set(vars(proxy))
        return original(proxy)

    monkeypatch.setattr(mt5_executor, "read_lifecycle_policy", capture)
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._get_lifecycle_policy()

    config_set = {k for k in vars(config) if k.startswith("R_LOCK_")}
    proxy_set = {k for k in captured["keys"] if k.startswith("R_LOCK_")}
    snapshot_set = {k for k in mt5_executor._CFG_IMPORT_SNAPSHOT if k.startswith("R_LOCK_")}
    reader_set = {n for n in _reader_getattr_names(risk_lifecycle.read_lifecycle_policy) if n.startswith("R_LOCK_")}

    assert config_set == proxy_set == snapshot_set == reader_set, (
        "R_LOCK_* drift: config=" + str(sorted(config_set)) + " proxy=" + str(sorted(proxy_set))
        + " snapshot=" + str(sorted(snapshot_set)) + " reader=" + str(sorted(reader_set))
    )
    assert config_set == {"R_LOCK_ENABLED", "R_LOCK_TIERS", "R_LOCK_MIN_R_DOLLARS"}


# --- 3. Missing-from-proxy knob silently falls back to the dataclass default --


def test_knob_missing_from_proxy_falls_back_to_dataclass_default(monkeypatch):
    # Documents the silent-no-op failure mode the drift guard prevents: if a knob is absent
    # from the whitelist proxy, read_dynamic_risk_policy's getattr falls back to the shipped
    # default and the configured value is silently ignored.
    knob = "DYNAMIC_ATR_TRAIL_MULT"
    field = "trail_atr_mult"
    current = getattr(config, knob)
    sentinel = _distinct(3.75, current, 0.125)
    monkeypatch.setattr(config, knob, sentinel)

    # Build the proxy exactly as the executor does, but OMIT the one knob.
    proxy = SimpleNamespace(**{
        k: getattr(config, k) for k in _DYNAMIC_KNOB_FIELD if k != knob
    })
    pol = dynamic_risk.read_dynamic_risk_policy(proxy)
    shipped = getattr(dynamic_risk.DynamicRiskPolicy(), field)
    assert getattr(pol, field) == pytest.approx(shipped)
    assert getattr(pol, field) != pytest.approx(sentinel)

    # With the knob present the same reader honours the configured sentinel.
    setattr(proxy, knob, sentinel)
    pol2 = dynamic_risk.read_dynamic_risk_policy(proxy)
    assert getattr(pol2, field) == pytest.approx(sentinel)
