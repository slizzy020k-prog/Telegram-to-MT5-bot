"""Phase 1.2 - .env-independent shipped-default tests.

Pins the SHIPPED config defaults (the literal 2nd args of the _env_* helpers in config.py)
and the DynamicRiskPolicy / LifecyclePolicy dataclass + reader fallbacks, WITHOUT depending
on the ambient (live-activated) .env.

Two complementary, .env-proof patterns:
  1. No-reload: DynamicRiskPolicy() dataclass defaults and
     read_dynamic_risk_policy(SimpleNamespace()) reader fallbacks. These never touch config
     or the environment (mirrors tests/test_v390_atr_compression.py).
  2. Neutralized reload: the shared shipped_config fixture (tests/conftest.py) patches
     dotenv.load_dotenv to a no-op, delenvs every strategy knob, and importlib.reload()s
     config so the module holds the shipped literals regardless of the ambient .env.
"""

import dataclasses
from types import SimpleNamespace

import pytest

import dynamic_risk
import risk_lifecycle


def _assert_field(actual, expected, label):
    if isinstance(expected, float):
        assert actual == pytest.approx(expected), label
    else:
        assert actual == expected, label


# Every DynamicRiskPolicy field -> its shipped default (dataclass + reader fallback).
_SHIPPED_POLICY_DEFAULTS = {
    "sltp_enabled": True,
    "sltp_mode": "ADAPTIVE",
    "atr_timeframe": "M5",
    "atr_period": 14,
    "atr_baseline_dollars": 4.00,
    "sl_atr_mult": 2.50,
    "tp_atr_mult": 1.50,
    "tp_step_atr_mult": 1.50,
    "sl_min_dollars": 4.50,
    "sl_max_dollars": 35.00,
    "tp_min_dollars": 2.50,
    "tp_max_dollars": 50.00,
    "scale_min": 0.75,
    "scale_max": 1.75,
    "trail_enabled": True,
    "trail_timeframe": "M5",
    "trail_atr_mult": 1.40,
    "trail_extended_mfe_dollars": 10.00,
    "trail_extended_atr_mult": 1.00,
    "trail_homerun_mfe_dollars": 18.00,
    "trail_homerun_atr_mult": 0.80,
    "trail_activation_atr_mult": 1.00,
    "trail_min_dollars": 2.50,
    "trail_max_dollars": 8.00,
    "trail_extended_min_dollars": 2.20,
    "trail_extended_max_dollars": 5.50,
    "trail_homerun_min_dollars": 2.00,
    "trail_homerun_max_dollars": 4.00,
    "trail_step_dollars": 0.25,
    "be_activation_atr_mult": 0.75,
    "be_min_mfe_dollars": 3.50,
    "be_lock_offset_dollars": 0.50,
    "trail_activation_ceiling_dollars": 0.0,
    "be_activation_ceiling_dollars": 0.0,
    "trail_distance_tiers": "",
    "mfe_lock_enabled": False,
    "mfe_lock_tiers": "0.40:0.45,0.60:0.55,0.80:0.65,1.00:0.70,1.25:0.75,1.50:0.80,2.00:0.85",
    "mfe_lock_cost_floor_dollars": 0.60,
    "mfe_lock_atr_min_dollars": 1.00,
    "mfe_lock_atr_max_dollars": 12.00,
    "relative_scale_mult": 1.02,
    "auto_be_enabled": False,
    "auto_be_pips": 30.0,
    "auto_be_pip_size_dollars": 0.0,
    "tp_trail_enabled": True,
    "tp_lift_atr_mult": 0.50,
    "tp_lift_progress_pct": 0.75,
    "tp1_lift_min_progress_pct": 0.50,
}


# --- Pattern 1: dataclass defaults + empty-reader fallbacks (no reload) -----


def test_policy_default_table_covers_every_dataclass_field():
    fields = {f.name for f in dataclasses.fields(dynamic_risk.DynamicRiskPolicy)}
    assert fields == set(_SHIPPED_POLICY_DEFAULTS)


def test_dataclass_defaults_match_shipped_spec():
    pol = dynamic_risk.DynamicRiskPolicy()
    for field, expected in _SHIPPED_POLICY_DEFAULTS.items():
        _assert_field(getattr(pol, field), expected, "DynamicRiskPolicy()." + field)


def test_reader_fallbacks_on_empty_namespace_match_shipped_spec():
    pol = dynamic_risk.read_dynamic_risk_policy(SimpleNamespace())
    for field, expected in _SHIPPED_POLICY_DEFAULTS.items():
        _assert_field(getattr(pol, field), expected, "reader fallback " + field)


def test_dataclass_and_empty_reader_agree():
    assert dynamic_risk.read_dynamic_risk_policy(SimpleNamespace()) == dynamic_risk.DynamicRiskPolicy()


def test_lifecycle_dataclass_and_empty_reader_defaults():
    pol = risk_lifecycle.LifecyclePolicy()
    assert pol.r_lock_enabled is False
    assert pol.r_lock_tiers == risk_lifecycle.DEFAULT_R_LOCK_TIERS
    assert pol.r_lock_min_r_dollars == pytest.approx(0.50)
    assert risk_lifecycle.read_lifecycle_policy(SimpleNamespace()) == pol


# --- Pattern 2: neutralized reload asserts the shipped config literals -----


# config knob -> shipped literal (the literal default in config.py). Covers every DYNAMIC_*
# knob plus the R-lock / unlimited-cap / BE-layer / deleted-signal defaults the brief pins.
_SHIPPED_CONFIG_LITERALS = {
    "DYNAMIC_ATR_SLTP_ENABLED": True,
    "DYNAMIC_SLTP_MODE": "ADAPTIVE",
    "DYNAMIC_ATR_TIMEFRAME": "M5",
    "DYNAMIC_ATR_PERIOD": 14,
    "DYNAMIC_ATR_BASELINE_DOLLARS": 4.00,
    "DYNAMIC_SL_ATR_MULT": 2.50,
    "DYNAMIC_TP_ATR_MULT": 1.50,
    "DYNAMIC_TP_STEP_ATR_MULT": 1.50,
    "DYNAMIC_SL_MIN_DOLLARS": 4.50,
    "DYNAMIC_SL_MAX_DOLLARS": 35.00,
    "DYNAMIC_TP_MIN_DOLLARS": 2.50,
    "DYNAMIC_TP_MAX_DOLLARS": 50.00,
    "DYNAMIC_SLTP_SCALE_MIN": 0.75,
    "DYNAMIC_SLTP_SCALE_MAX": 1.75,
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
    "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS": 2.20,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS": 5.50,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS": 2.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS": 4.00,
    "DYNAMIC_ATR_TRAIL_STEP_DOLLARS": 0.25,
    "DYNAMIC_ATR_BE_ACTIVATION_MULT": 0.75,
    "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS": 3.50,
    "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS": 0.50,
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": 0.0,
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": 0.0,
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": False,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "0.40:0.45,0.60:0.55,0.80:0.65,1.00:0.70,1.25:0.75,1.50:0.80,2.00:0.85",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": 0.60,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS": 1.00,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS": 12.00,
    "DYNAMIC_ATR_RELATIVE_SCALE_MULT": 1.02,
    "DYNAMIC_ATR_AUTO_BE_ENABLED": False,
    "DYNAMIC_ATR_AUTO_BE_PIPS": 30.0,
    "DYNAMIC_ATR_AUTO_BE_PIP_SIZE_DOLLARS": 0.0,
    "DYNAMIC_ATR_TP_TRAIL_ENABLED": True,
    "DYNAMIC_ATR_TP_LIFT_ATR_MULT": 0.50,
    "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT": 0.75,
    "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT": 0.50,
    "R_LOCK_ENABLED": False,
    "R_LOCK_MIN_R_DOLLARS": 0.50,
    "ENFORCE_HIGH_RISK_LOT_CAP": False,
    "DAILY_LOSS_PAUSE_PCT": 0.0,
    "EMERGENCY_CUTOFF_LOSS_PCT": 0.0,
    "FLOAT_LOSS_FORCE_CLOSE_PCT": 0.0,
    "DAILY_LOSS_CLOSE_OPEN_TRADES": False,
    "AUTO_ADJUST_LOTS_TO_BALANCE": False,
    "BE_LAYERS": ["L1", "L2", "L3", "L4"],
    "CLOSE_ON_SIGNAL_DELETED": True,
    "DELETED_SIGNAL_POLL_INTERVAL_SECONDS": 1.5,
}


def test_shipped_config_literals(shipped_config):
    for knob, expected in _SHIPPED_CONFIG_LITERALS.items():
        _assert_field(getattr(shipped_config, knob), expected, knob)


def test_shipped_config_dynamic_knob_set_is_complete(shipped_config):
    dynamic_knobs = {k for k in vars(shipped_config) if k.startswith("DYNAMIC_")}
    pinned = {k for k in _SHIPPED_CONFIG_LITERALS if k.startswith("DYNAMIC_")}
    assert dynamic_knobs == pinned
    assert len(dynamic_knobs) == 48


def test_shipped_r_lock_via_reader_fallback(shipped_config):
    # R_LOCK_TIERS is a raw os.getenv knob; assert the reader fallback, not the raw string.
    pol = risk_lifecycle.read_lifecycle_policy(shipped_config)
    assert pol.r_lock_enabled is False
    assert pol.r_lock_tiers == risk_lifecycle.DEFAULT_R_LOCK_TIERS
    assert pol.r_lock_min_r_dollars == pytest.approx(0.50)


def test_shipped_config_reader_equals_dataclass_defaults(shipped_config):
    # Strong .env-independence proof: the neutralized config reads exactly the shipped
    # dataclass defaults, whatever the ambient .env contains.
    assert dynamic_risk.read_dynamic_risk_policy(shipped_config) == dynamic_risk.DynamicRiskPolicy()
