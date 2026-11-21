"""v3.8.1 calibration: the widened trade-age timers (4 h de-risk / 8 h flatten).

The Arm C replay proof showed the old 2 h de-risk COST GBP 16.99 on the
recorded tape: two of the twelve replayed trades (9937 and 9943) were moved
to breakeven at ~2 h and stopped at +0.76 each instead of +4.44 and +14.07.
A 2 h de-risk clips normal 2-3 hour London/NY consolidations, while the trades
it was designed to catch ran 20-80 hours. The timers are therefore widened:

    LIFECYCLE_DERISK_SECONDS   7200  (2 h) -> 14400  (4 h)
    LIFECYCLE_FLATTEN_SECONDS 21600  (6 h) -> 28800  (8 h)

LIFECYCLE_PENDING_EXPIRE_SECONDS (43200 / 12 h) is UNCHANGED.

Every location that carries the default is asserted here, because a stale
default anywhere is a real bug: the shipped config.py default, the getattr
fallbacks of risk_lifecycle.read_lifecycle_policy and
startup_checks._validate_lifecycle_config, the LifecyclePolicy dataclass
defaults, .env.example, the replay harness calibration block, the design spec
knob table and the local (gitignored) .env.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import risk_lifecycle as rl
import startup_checks


REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_DERISK = 14400
EXPECTED_FLATTEN = 28800
EXPECTED_PENDING_EXPIRE = 43200

OLD_DERISK = 7200
OLD_FLATTEN = 21600


def _env_values(path: Path) -> dict:
    """Parse a KEY=VALUE .env file into a dict (last wins)."""
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _config_namespace(**overrides) -> SimpleNamespace:
    """A config-like module carrying every shipped setting, with overrides."""
    base = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    base.update(overrides)
    return SimpleNamespace(**base)


# --- shipped defaults (config.py) ---------------------------------------

def test_config_ships_the_widened_derisk_and_flatten_defaults():
    assert config.LIFECYCLE_DERISK_SECONDS == EXPECTED_DERISK
    assert config.LIFECYCLE_FLATTEN_SECONDS == EXPECTED_FLATTEN


def test_config_leaves_the_pending_expiry_unchanged():
    assert config.LIFECYCLE_PENDING_EXPIRE_SECONDS == EXPECTED_PENDING_EXPIRE


def test_config_keeps_both_features_disabled():
    assert config.R_LOCK_ENABLED is False
    assert config.LIFECYCLE_ENABLED is False


def test_config_source_declares_the_widened_default_literals():
    """The EFFECTIVE value can be masked by the local .env; assert the SOURCE."""
    source = (REPO_ROOT / "config.py").read_text(encoding="utf-8")
    assert '"LIFECYCLE_DERISK_SECONDS", ' + str(EXPECTED_DERISK) in source
    assert '"LIFECYCLE_FLATTEN_SECONDS", ' + str(EXPECTED_FLATTEN) in source
    assert '"LIFECYCLE_DERISK_SECONDS", ' + str(OLD_DERISK) not in source
    assert '"LIFECYCLE_FLATTEN_SECONDS", ' + str(OLD_FLATTEN) not in source


# --- read_lifecycle_policy (the getattr fallback path) -------------------

def test_read_lifecycle_policy_falls_back_to_the_widened_defaults():
    """No attributes at all: the getattr fallbacks must be the shipped values."""
    policy = rl.read_lifecycle_policy(SimpleNamespace())
    assert policy.derisk_after_seconds == EXPECTED_DERISK
    assert policy.flatten_after_seconds == EXPECTED_FLATTEN
    assert policy.pending_expire_seconds == EXPECTED_PENDING_EXPIRE


def test_read_lifecycle_policy_uses_the_configured_values_when_present():
    policy = rl.read_lifecycle_policy(
        SimpleNamespace(
            LIFECYCLE_DERISK_SECONDS=EXPECTED_DERISK,
            LIFECYCLE_FLATTEN_SECONDS=EXPECTED_FLATTEN,
        )
    )
    assert policy.derisk_after_seconds == EXPECTED_DERISK
    assert policy.flatten_after_seconds == EXPECTED_FLATTEN


def test_read_lifecycle_policy_still_honours_an_explicit_override():
    """A configured value is never silently replaced by the fallback."""
    policy = rl.read_lifecycle_policy(
        SimpleNamespace(
            LIFECYCLE_DERISK_SECONDS=OLD_DERISK,
            LIFECYCLE_FLATTEN_SECONDS=OLD_FLATTEN,
        )
    )
    assert policy.derisk_after_seconds == OLD_DERISK
    assert policy.flatten_after_seconds == OLD_FLATTEN


def test_read_lifecycle_policy_repairs_flatten_below_derisk():
    policy = rl.read_lifecycle_policy(
        SimpleNamespace(
            LIFECYCLE_DERISK_SECONDS=EXPECTED_FLATTEN,
            LIFECYCLE_FLATTEN_SECONDS=EXPECTED_DERISK,
        )
    )
    assert policy.flatten_after_seconds == policy.derisk_after_seconds


def test_lifecycle_policy_dataclass_defaults_are_the_widened_values():
    policy = rl.LifecyclePolicy()
    assert policy.derisk_after_seconds == EXPECTED_DERISK
    assert policy.flatten_after_seconds == EXPECTED_FLATTEN
    assert policy.pending_expire_seconds == EXPECTED_PENDING_EXPIRE


# --- startup validation --------------------------------------------------

def test_startup_validation_accepts_the_widened_defaults():
    errors = []
    startup_checks._validate_lifecycle_config(config, errors)
    assert errors == []


def test_startup_validation_passes_with_the_shipped_config():
    res = startup_checks.validate_environment(check_packages=False, cfg=config)
    assert res["ok"] is True, res["errors"]


def test_startup_validation_uses_the_widened_flatten_fallback():
    """The flatten fallback must be 28800: a 25000s de-risk must not be rejected."""
    errors = []
    startup_checks._validate_lifecycle_config(
        SimpleNamespace(LIFECYCLE_DERISK_SECONDS=25000), errors
    )
    assert errors == []


def test_startup_validation_uses_the_widened_derisk_fallback():
    """The de-risk fallback must be 14400: a 10000s flatten must be rejected."""
    errors = []
    startup_checks._validate_lifecycle_config(
        SimpleNamespace(LIFECYCLE_FLATTEN_SECONDS=10000), errors
    )
    assert errors, "the 14400s de-risk fallback must reject a 10000s flatten"


def test_startup_validation_still_rejects_flatten_below_derisk():
    bad_cfg = _config_namespace(LIFECYCLE_FLATTEN_SECONDS=EXPECTED_DERISK - 1)
    res = startup_checks.validate_environment(check_packages=False, cfg=bad_cfg)
    assert res["ok"] is False
    assert any("LIFECYCLE_FLATTEN_SECONDS" in err for err in res["errors"])


# --- .env.example --------------------------------------------------------

def test_env_example_documents_the_widened_timers():
    values = _env_values(REPO_ROOT / ".env.example")
    assert values["LIFECYCLE_DERISK_SECONDS"] == str(EXPECTED_DERISK)
    assert values["LIFECYCLE_FLATTEN_SECONDS"] == str(EXPECTED_FLATTEN)
    assert values["LIFECYCLE_PENDING_EXPIRE_SECONDS"] == str(EXPECTED_PENDING_EXPIRE)


def test_env_example_keeps_both_features_disabled():
    values = _env_values(REPO_ROOT / ".env.example")
    assert values["R_LOCK_ENABLED"].lower() == "false"
    assert values["LIFECYCLE_ENABLED"].lower() == "false"


# --- the local (gitignored) .env -----------------------------------------

def test_local_env_has_the_widened_timers_and_the_features_off():
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        pytest.skip("no local .env in this checkout")
    values = _env_values(env_path)
    assert values["LIFECYCLE_DERISK_SECONDS"] == str(EXPECTED_DERISK)
    assert values["LIFECYCLE_FLATTEN_SECONDS"] == str(EXPECTED_FLATTEN)
    assert values["LIFECYCLE_PENDING_EXPIRE_SECONDS"] == str(EXPECTED_PENDING_EXPIRE)
    assert values["R_LOCK_ENABLED"].lower() == "false"
    assert values["LIFECYCLE_ENABLED"].lower() == "false"
    assert values["LIFECYCLE_SESSION_FLATTEN_ENABLED"].lower() == "false"


def test_local_env_keeps_the_calibrated_atr_trail_mult():
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        pytest.skip("no local .env in this checkout")
    values = _env_values(env_path)
    assert values["DYNAMIC_ATR_TRAIL_MULT"] == "1.40"


# --- the replay harness calibration block --------------------------------

def _load_replay_module():
    path = REPO_ROOT / "scripts" / "ab_replay_proof.py"
    spec = spec_from_file_location("ab_replay_proof_v381", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_harness_pins_the_widened_timers():
    module = _load_replay_module()
    params = module.ARM_C_CALIBRATED_PARAMS
    assert params["LIFECYCLE_DERISK_SECONDS"] == EXPECTED_DERISK
    assert params["LIFECYCLE_FLATTEN_SECONDS"] == EXPECTED_FLATTEN
    assert params["LIFECYCLE_PENDING_EXPIRE_SECONDS"] == EXPECTED_PENDING_EXPIRE


def test_replay_harness_enables_the_features_only_inside_its_own_subprocess():
    module = _load_replay_module()
    params = module.ARM_C_CALIBRATED_PARAMS
    assert params["R_LOCK_ENABLED"] is True
    assert params["LIFECYCLE_ENABLED"] is True
    # The shipped defaults must stay OFF even though the replay turns them on.
    assert config.R_LOCK_ENABLED is False
    assert config.LIFECYCLE_ENABLED is False


# --- the design spec -----------------------------------------------------

def test_spec_knob_table_documents_the_widened_timers():
    spec_path = (
        REPO_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-11-20-trade-lifecycle-guard-design.md"
    )
    text = spec_path.read_text(encoding="utf-8")
    assert "| " + chr(96) + "LIFECYCLE_DERISK_SECONDS" + chr(96) + " | " + chr(96) + str(EXPECTED_DERISK) + chr(96) + " |" in text
    assert "| " + chr(96) + "LIFECYCLE_FLATTEN_SECONDS" + chr(96) + " | " + chr(96) + str(EXPECTED_FLATTEN) + chr(96) + " |" in text
    assert "| " + chr(96) + "LIFECYCLE_PENDING_EXPIRE_SECONDS" + chr(96) + " | " + chr(96) + str(EXPECTED_PENDING_EXPIRE) + chr(96) + " |" in text
