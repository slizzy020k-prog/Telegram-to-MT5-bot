"""Display-only tests for the v3.9.0 Protection Profile status line.

Covers risk_controls.format_protection_profile_line: the exact live T3 line
(t3 / R-Lock ON / Lot Cap ON / 80% daily-loss pause), the all-OFF baseline
line, the t2 50% variant, the missing-profile default, the R-Lock fallback
when risk_lifecycle cannot be imported, and a non-vacuous mutation check
proving the assertions fail when the helper returns a wrong string.
No config/.env mutation, no broker access, no trading-logic changes.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import risk_controls


def _make_cfg(
    trailing_profile: str,
    r_lock_enabled: bool,
    lot_cap_enabled: bool,
    daily_loss_pause_pct: float,
) -> SimpleNamespace:
    return SimpleNamespace(
        TRAILING_PROFILE=trailing_profile,
        R_LOCK_ENABLED=r_lock_enabled,
        R_LOCK_TIERS="0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75",
        R_LOCK_MIN_R_DOLLARS=0.50,
        ENFORCE_HIGH_RISK_LOT_CAP=lot_cap_enabled,
        DAILY_LOSS_PAUSE_PCT=daily_loss_pause_pct,
        EMERGENCY_CUTOFF_LOSS_PCT=0.0,
        FLOAT_LOSS_FORCE_CLOSE_PCT=0.0,
    )


def test_live_t3_protection_line_exact() -> None:
    line = risk_controls.format_protection_profile_line(_make_cfg("t3", True, True, 80.0))
    assert line == (
        "• Protection Profile: TRAILING_PROFILE=`t3` | R-Lock: `ON` "
        "| Lot Cap: `ON` | Daily Loss Pause: `80%`"
    )


def test_baseline_all_off_line() -> None:
    line = risk_controls.format_protection_profile_line(_make_cfg("baseline", False, False, 0.0))
    assert "TRAILING_PROFILE=`baseline`" in line
    assert "R-Lock: `OFF`" in line
    assert "Lot Cap: `OFF`" in line
    assert "Daily Loss Pause: `OFF`" in line


def test_t2_profile_half_pause_line() -> None:
    line = risk_controls.format_protection_profile_line(_make_cfg("t2", True, True, 50.0))
    assert "TRAILING_PROFILE=`t2`" in line
    assert "Daily Loss Pause: `50%`" in line


def test_missing_trailing_profile_defaults_to_baseline() -> None:
    cfg = _make_cfg("t3", True, True, 80.0)
    del cfg.TRAILING_PROFILE
    line = risk_controls.format_protection_profile_line(cfg)
    assert "TRAILING_PROFILE=`baseline`" in line


def test_r_lock_fallback_when_lifecycle_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # None in sys.modules makes the lazy `from risk_lifecycle import ...` raise,
    # exercising the bool(R_LOCK_ENABLED) fallback branch.
    monkeypatch.setitem(sys.modules, "risk_lifecycle", None)
    on_line = risk_controls.format_protection_profile_line(_make_cfg("t3", True, True, 80.0))
    assert "R-Lock: `ON`" in on_line
    off_line = risk_controls.format_protection_profile_line(_make_cfg("t3", False, True, 80.0))
    assert "R-Lock: `OFF`" in off_line


def test_mutation_guard_proves_line_assertions_non_vacuous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(risk_controls, "format_protection_profile_line", lambda cfg: "MUTATED WRONG LINE")
    with pytest.raises(AssertionError):
        test_live_t3_protection_line_exact()
