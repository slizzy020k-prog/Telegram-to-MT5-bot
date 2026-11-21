# Trade Lifecycle Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an R-relative profit-protection floor and a trade-age guard to the Telegram→MT5 copier, so a trade that runs far into profit can no longer round-trip to zero, and no trade can sit unattended for 20+ hours.

**Architecture:** A new pure domain module `risk_lifecycle.py` decides *what* should happen (pure functions, no MT5 import, no I/O). `dynamic_risk.py` gains one additive keyword that accepts the R floor as an extra stop candidate inside the existing forward-only candidate picker. `mt5_executor.py` computes the floor per layer and gains three lifecycle actions. `main.py` evaluates the age guard once per second inside the existing pre-TP1 tick processor and exposes `/rlock`, `/lifecycle` and `/stats` as Saved Messages commands. `trade_ledger.py` records cash MFE, cash giveback, R at exit and exit age so the change can be measured.

**Tech Stack:** Python 3 (stdlib only — `dataclasses`, `datetime`, `math`), pytest, Telethon, MetaTrader5. Windows / PowerShell.

**Design spec:** `docs/superpowers/specs/2026-11-20-trade-lifecycle-guard-design.md`

---

## Global Constraints

- **No new runtime dependencies.** `risk_lifecycle.py` imports only the Python standard library.
- **Both features ship disabled.** `R_LOCK_ENABLED=false` and `LIFECYCLE_ENABLED=false` are the defaults in `config.py`, `.env.example` and every test fixture. A fresh clone must behave identically to v3.7.0.
- **Never loosen a broker SL or TP.** Every new stop is a *floor candidate* in the existing picker; the age guard's breakeven move applies only when strictly tighter than the current SL.
- **Never emit retcode `10016` (Invalid stops).** Every new SL change must pass the existing `is_within_freeze_level` + `stops_buffer` + `trade_stops_level` validation used by the ATR trail path.
- **No public signature may be renamed or removed.** `compute_dynamic_atr_trail_stop` gains one keyword-only argument with a `None` default; every existing call site stays valid.
- **Stdlib-only timestamps.** `signal_data["timestamp"]` is ISO-8601 with a timezone offset (e.g. `2026-11-17T19:31:51.123456+00:00`). New stamps use `datetime.now(timezone.utc).isoformat()`.
- **Working tree uses LF**; do not rewrite existing line endings.
- **Never commit runtime artifacts:** `.env`, `*.log`, `*.log.*`, `signals_state*.json`, `trades_ledger*.jsonl`, `*.session`, `*.session-journal`, `copier.lock`.
- **No VPS deployment** in this plan. The new `.env` knobs must be added by hand on the server at deploy time.
- **The full pytest suite must finish with 0 failures.** Baseline before any task: **241 passed**.
- Do not modify `README.md` or `AI_HANDOVER.md` in this plan — the doc refresh is a separate change wave (spec §9.6).

---

## File Structure

| File | Responsibility after this plan |
|---|---|
| `risk_lifecycle.py` | **New.** Pure lifecycle domain: policy dataclass, R-ladder math, age evaluation, Saved-Messages command handlers. No MT5, no I/O. |
| `config.py` | Owns the 10 lifecycle knobs and their import-time validation. |
| `startup_checks.py` | Fails startup on an invalid lifecycle configuration. |
| `dynamic_risk.py` | Owns the ATR trail candidate picker; gains the `R_LOCK` candidate. |
| `mt5_executor.py` | Carries out lifecycle actions against the broker; captures `initial_sl` / `first_fill_at`; computes the R floor per layer. |
| `main.py` | Drives the age guard each second; exposes `/rlock`, `/lifecycle`, `/stats`. |
| `trade_ledger.py` | Writes schema-4 records with cash MFE / giveback / R / age. |
| `tests/test_trade_lifecycle.py` | **New.** Unit + integration coverage for every task in this plan. |

---

## Task 1: `risk_lifecycle.py` — the pure domain core

**Files:**
- Create: `risk_lifecycle.py`
- Create: `tests/test_trade_lifecycle.py`
- Modify: `config.py:545` (append the knob block after `DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT`)
- Modify: `config.py:548-565` (append `validate_lifecycle_pairs()`)
- Modify: `startup_checks.py:465` (add `_validate_lifecycle_config`)
- Modify: `.env.example` (append the knob block)

**Interfaces:**
- Consumes: nothing (first task in the chain).
- Produces:
  - `LifecyclePolicy` — frozen dataclass, fields: `r_lock_enabled: bool`, `r_lock_tiers: Tuple[Tuple[float, float], ...]`, `r_lock_min_r_dollars: float`, `lifecycle_enabled: bool`, `derisk_after_seconds: int`, `flatten_after_seconds: int`, `pending_expire_seconds: int`, `session_flatten_enabled: bool`, `session_flatten_at: str`, `max_close_retries: int`.
  - `DEFAULT_R_LOCK_TIERS: Tuple[Tuple[float, float], ...]` = `((0.50, 0.00), (1.00, 0.25), (1.50, 0.50), (2.00, 0.75))`.
  - `parse_r_lock_tiers(raw: Any) -> Tuple[Tuple[float, float], ...]`
  - `read_lifecycle_policy(cfg_mod: Any) -> LifecyclePolicy`
  - `compute_initial_r(entry_price: float, initial_sl: float) -> Optional[float]`
  - `compute_r_lock_floor(direction: str, entry_price: float, initial_sl: float, mfe_dollars: float, live_price: float, policy: LifecyclePolicy, digits: int = 2) -> Optional[float]` — returns the greatest reached lock fraction, and `None` when the floor would sit on the wrong side of `live_price`.
  - `evaluate_trade_age(signal_data: Dict[str, Any], now_utc: datetime, policy: LifecyclePolicy, server_offset_seconds: int = 0) -> Dict[str, Any]`
  - `apply_lifecycle_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]`
  - `apply_rlock_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]`
  - Action constants: `ACTION_NONE="NONE"`, `ACTION_DERISK="DERISK"`, `ACTION_FLATTEN="FLATTEN"`, `ACTION_EXPIRE_PENDING="EXPIRE_PENDING"`, `ACTION_SESSION_FLATTEN="SESSION_FLATTEN"`.
  - Config constants: `R_LOCK_ENABLED`, `R_LOCK_TIERS`, `R_LOCK_MIN_R_DOLLARS`, `LIFECYCLE_ENABLED`, `LIFECYCLE_DERISK_SECONDS`, `LIFECYCLE_FLATTEN_SECONDS`, `LIFECYCLE_PENDING_EXPIRE_SECONDS`, `LIFECYCLE_SESSION_FLATTEN_ENABLED`, `LIFECYCLE_SESSION_FLATTEN_AT`, `LIFECYCLE_MAX_CLOSE_RETRIES`.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_trade_lifecycle.py`:

```python
<!-- SNAPSHOT: this block is the state of the file as of Task 2. Every later task
     appends to the same file; the live file in the repo is authoritative. -->


"""Unit tests for the pure trade-lifecycle domain module (`risk_lifecycle.py`)."""

from datetime import datetime, timedelta, timezone

import pytest

import risk_lifecycle as rl


def _policy(**overrides):
    base = dict(
        r_lock_enabled=True,
        r_lock_tiers=rl.DEFAULT_R_LOCK_TIERS,
        r_lock_min_r_dollars=0.50,
        lifecycle_enabled=True,
        derisk_after_seconds=7200,
        flatten_after_seconds=21600,
        pending_expire_seconds=43200,
        session_flatten_enabled=False,
        session_flatten_at="23:30",
        max_close_retries=3,
    )
    base.update(overrides)
    return rl.LifecyclePolicy(**base)


# --- parse_r_lock_tiers -------------------------------------------------

def test_parse_r_lock_tiers_from_string():
    assert rl.parse_r_lock_tiers("0.5:0.00,1.0:0.25") == ((0.5, 0.0), (1.0, 0.25))


def test_parse_r_lock_tiers_sorts_ascending_and_skips_malformed():
    parsed = rl.parse_r_lock_tiers("1.0:0.25,garbage,0.5:0.00,0.2:1.5,-1:0.5")
    assert parsed == ((0.5, 0.0), (1.0, 0.25))


def test_parse_r_lock_tiers_empty_returns_empty_tuple():
    assert rl.parse_r_lock_tiers("") == ()


# --- compute_initial_r --------------------------------------------------

def test_compute_initial_r_is_direction_agnostic():
    assert rl.compute_initial_r(3900.0, 3880.0) == pytest.approx(20.0)
    assert rl.compute_initial_r(3900.0, 3920.0) == pytest.approx(20.0)


def test_compute_initial_r_returns_none_for_invalid_inputs():
    assert rl.compute_initial_r(0.0, 3880.0) is None
    assert rl.compute_initial_r(3900.0, 0.0) is None
    assert rl.compute_initial_r(3900.0, 3900.0) is None


# --- compute_r_lock_floor ----------------------------------------------

def test_r_lock_floor_none_below_first_tier():
    # R = 20.0, first tier 0.5R -> needs MFE 10.0
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 9.99, 3920.0, _policy()) is None


def test_r_lock_floor_locks_entry_at_half_r():
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 10.0, 3920.0, _policy()) == pytest.approx(3900.0)


def test_r_lock_floor_ladder_boundaries():
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 20.0, 3920.0, _policy()) == pytest.approx(3905.0)
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 30.0, 3920.0, _policy()) == pytest.approx(3910.0)
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.0, 3920.0, _policy()) == pytest.approx(3915.0)


def test_r_lock_floor_scales_with_stop_distance():
    wide = rl.compute_r_lock_floor("BUY", 3900.0, 3878.0, 11.0, 3920.0, _policy())   # R = 22.0
    tight = rl.compute_r_lock_floor("BUY", 3900.0, 3898.0, 1.0, 3920.0, _policy())    # R = 2.00
    assert wide == pytest.approx(3900.0)
    assert tight == pytest.approx(3900.0)
    # 1R on the wide stop locks a much larger absolute distance than on the tight stop
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3878.0, 22.0, 3920.0, _policy()) == pytest.approx(3905.5)
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3898.0, 2.0, 3920.0, _policy()) == pytest.approx(3900.5)


def test_r_lock_floor_sell_is_mirrored():
    assert rl.compute_r_lock_floor("SELL", 3900.0, 3920.0, 20.0, 3880.0, _policy()) == pytest.approx(3895.0)


def test_r_lock_floor_disabled_returns_none():
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.0, 3920.0, _policy(r_lock_enabled=False)) is None


def test_r_lock_floor_below_min_r_returns_none():
    # R = 0.20 which is under r_lock_min_r_dollars = 0.50
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3899.8, 1.0, 3920.0, _policy()) is None


# --- evaluate_trade_age --------------------------------------------------

def _sig(**overrides):
    base = {
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "market_tickets": [101],
        "pending_tickets": [],
    }
    base.update(overrides)
    return base


def test_evaluate_trade_age_none_when_disabled():
    res = rl.evaluate_trade_age(_sig(), datetime.now(timezone.utc), _policy(lifecycle_enabled=False))
    assert res["action"] == rl.ACTION_NONE


def test_evaluate_trade_age_none_when_young():
    sig = _sig(timestamp=datetime.now(timezone.utc).isoformat())
    res = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert res["action"] == rl.ACTION_NONE


def test_evaluate_trade_age_derisk_after_two_hours():
    sig = _sig(timestamp=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    res = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert res["action"] == rl.ACTION_DERISK


def test_evaluate_trade_age_flatten_beats_derisk():
    sig = _sig(timestamp=(datetime.now(timezone.utc) - timedelta(hours=9)).isoformat())
    res = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert res["action"] == rl.ACTION_FLATTEN


def test_evaluate_trade_age_expires_unfilled_pending():
    sig = _sig(
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(),
        market_tickets=[],
        pending_tickets=[201],
    )
    res = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert res["action"] == rl.ACTION_EXPIRE_PENDING


def test_evaluate_trade_age_never_expires_a_filled_signal():
    sig = _sig(
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(),
        market_tickets=[101],
        pending_tickets=[201],
    )
    res = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert res["action"] == rl.ACTION_FLATTEN


def test_evaluate_trade_age_none_without_orders():
    sig = _sig(market_tickets=[], pending_tickets=[])
    res = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert res["action"] == rl.ACTION_NONE


def test_evaluate_trade_age_reports_ages():
    now = datetime.now(timezone.utc)
    sig = _sig(timestamp=(now - timedelta(hours=3)).isoformat())
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["signal_age_seconds"] >= 10800


# --- command handlers ----------------------------------------------------

def test_apply_rlock_command_toggles(monkeypatch):
    import config
    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    changed, msg = rl.apply_rlock_command(config, ["on"])
    assert changed is True
    assert config.R_LOCK_ENABLED is True
    changed, msg = rl.apply_rlock_command(config, ["off"])
    assert changed is True
    assert config.R_LOCK_ENABLED is False


def test_apply_rlock_command_status_does_not_change(monkeypatch):
    import config
    monkeypatch.setattr(config, "R_LOCK_ENABLED", True)
    changed, msg = rl.apply_rlock_command(config, [])
    assert changed is False
    assert config.R_LOCK_ENABLED is True
    assert "R_LOCK" in msg


def test_apply_lifecycle_command_toggles(monkeypatch):
    import config
    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)
    changed, msg = rl.apply_lifecycle_command(config, ["on"])
    assert changed is True
    assert config.LIFECYCLE_ENABLED is True


def test_apply_lifecycle_command_rejects_garbage(monkeypatch):
    import config
    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)
    changed, msg = rl.apply_lifecycle_command(config, ["banana"])
    assert changed is False
    assert config.LIFECYCLE_ENABLED is False


# --- market-side guard, direction validation, policy normalisation ---------

def test_r_lock_floor_is_none_when_price_retraced_past_it():
    # entry 3900, R = 20, MFE 40 (2.0R) -> floor 3915; live price back at 3905
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.0, 3905.0, _policy()) is None


def test_r_lock_floor_survives_a_retrace_that_keeps_it_below_market():
    # entry 3900, R = 20, MFE 40 -> floor 3915; live price 3920 still above it
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.0, 3920.0, _policy()) == pytest.approx(3915.0)


def test_r_lock_floor_rejects_an_unknown_direction():
    assert rl.compute_r_lock_floor("FOOP", 3900.0, 3880.0, 40.0, 3920.0, _policy()) is None
    assert rl.compute_r_lock_floor("", 3900.0, 3880.0, 40.0, 3920.0, _policy()) is None


def test_r_lock_floor_is_none_when_the_live_price_is_unusable():
    # An unknown price cannot validate the floor -> fail closed (no floor at all)
    for unusable in (0, -1.0, None, float("nan"), "not-a-price"):
        assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.0, unusable, _policy()) is None
        assert rl.compute_r_lock_floor("SELL", 3900.0, 3920.0, 20.0, unusable, _policy()) is None


def test_r_lock_floor_is_order_independent():
    unsorted_policy = _policy(r_lock_tiers=((2.0, 0.75), (0.5, 0.0)))
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.0, 3920.0, unsorted_policy) == pytest.approx(3915.0)


def test_r_lock_floor_never_loosens_as_mfe_grows():
    pol = _policy()
    floors = [
        rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, mfe, 3999.0, pol)
        for mfe in (10.0, 20.0, 30.0, 40.0, 80.0)
    ]
    values = [f for f in floors if f is not None]
    assert values == sorted(values)


def test_r_lock_floor_is_unchanged_just_past_the_top_tier():
    # 2.01R is past the last tier (2.0R) -> the floor stays at +0.75R
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 40.2, 3920.0, _policy()) == pytest.approx(3915.0)


def test_read_lifecycle_policy_repairs_an_inverted_threshold_pair():
    import config

    class _Cfg:
        LIFECYCLE_DERISK_SECONDS = 7200
        LIFECYCLE_FLATTEN_SECONDS = 3600
        LIFECYCLE_SESSION_FLATTEN_AT = "99:99"
        R_LOCK_TIERS = ""

    pol = rl.read_lifecycle_policy(_Cfg())
    assert pol.flatten_after_seconds >= pol.derisk_after_seconds
    assert pol.session_flatten_at == "23:30"
    assert pol.r_lock_tiers == rl.DEFAULT_R_LOCK_TIERS


def test_session_flatten_fires_only_with_an_open_position():
    pol = _policy(session_flatten_enabled=True, session_flatten_at="00:00")
    now = datetime.now(timezone.utc)
    pending_only = {
        "timestamp": now.isoformat(),
        "market_tickets": [],
        "pending_tickets": [201],
    }
    res = rl.evaluate_trade_age(pending_only, now, pol, server_offset_seconds=0)
    assert res["action"] != rl.ACTION_SESSION_FLATTEN


def test_session_flatten_respects_the_server_time_offset():
    pol = _policy(session_flatten_enabled=True, session_flatten_at="12:00")
    now = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
    sig = {
        "timestamp": now.isoformat(),
        "market_tickets": [101],
        "pending_tickets": [],
    }
    on = rl.evaluate_trade_age(sig, now, pol, server_offset_seconds=0)
    off = rl.evaluate_trade_age(sig, now, pol, server_offset_seconds=-3600)
    assert on["action"] == rl.ACTION_SESSION_FLATTEN
    assert off["action"] != rl.ACTION_SESSION_FLATTEN


def test_signal_without_a_timestamp_reports_an_unknown_age():
    pol = _policy()
    res = rl.evaluate_trade_age({"market_tickets": [101]}, datetime.now(timezone.utc), pol)
    assert res["action"] == rl.ACTION_NONE
    assert "unknown age" in res["reason"]


# --- dynamic_risk R_LOCK candidate ---------------------------------------

def _atr_policy():
    """An explicit policy, so the tests do not depend on ambient config values."""
    import dynamic_risk

    return dynamic_risk.DynamicRiskPolicy(
        sltp_enabled=True,
        sltp_mode="ADAPTIVE",
        atr_timeframe="M5",
        atr_period=14,
        atr_baseline_dollars=4.00,
        sl_atr_mult=2.50,
        tp_atr_mult=1.50,
        tp_step_atr_mult=1.50,
        sl_min_dollars=4.50,
        sl_max_dollars=35.00,
        tp_min_dollars=2.50,
        tp_max_dollars=50.00,
        scale_min=0.75,
        scale_max=1.75,
        trail_enabled=True,
        trail_timeframe="M5",
        trail_atr_mult=1.40,
        trail_extended_mfe_dollars=10.00,
        trail_extended_atr_mult=1.00,
        trail_homerun_mfe_dollars=18.00,
        trail_homerun_atr_mult=0.80,
        trail_activation_atr_mult=1.00,
        trail_min_dollars=2.50,
        trail_max_dollars=12.00,
        trail_step_dollars=0.25,
        be_activation_atr_mult=0.75,
        be_min_mfe_dollars=3.50,
        be_lock_offset_dollars=0.50,
        tp_trail_enabled=True,
        tp_lift_atr_mult=0.50,
        tp_lift_progress_pct=0.75,
        tp1_lift_min_progress_pct=0.50,
    )


def test_trail_stop_unchanged_when_r_lock_floor_is_none():
    """With the default floor the engine must behave exactly as it did before."""
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=2.0,
        policy=pol,
        digits=2,
    )
    assert res["r_lock_floor"] is None
    assert res["stop_source"] == "ATR_TRAIL"
    # MFE 10.00 -> EXTENDED zone -> mult 1.00 -> trail_dist = clamp(2.00, 2.50, 12.00)
    # = 2.50 -> atr_sl = 3905.00 - 2.50 = 3902.50 (pinned from the pre-R_LOCK baseline).
    assert res["candidate_sl"] == pytest.approx(3902.50)
    assert res["should_update"] is True


def test_trail_stop_is_unchanged_across_a_baseline_grid():
    """Permanent regression grid: `r_lock_floor=None` must reproduce the baseline exactly."""
    import dynamic_risk

    pol = _atr_policy()
    # Exact baseline values, pinned from the pre-R_LOCK implementation (git 4c88faa)
    # with this explicit policy: (candidate_sl, stop_source, trail_mult, trail_zone).
    # BUY atr=0.0 -> trail disabled by an invalid ATR; BUY atr=2.0 -> ATR trail armed;
    # BUY atr=20.0 -> MFE 10.00 is below the 20.00 trail activation, so nothing is armed.
    # SELL never reaches a positive MFE here, so it is never armed.
    expected = {
        ("BUY", 0.0): (None, None, 1.4, "STANDARD"),
        ("BUY", 2.0): (3902.5, "ATR_TRAIL", 1.0, "EXTENDED"),
        ("BUY", 20.0): (None, None, 1.0, "EXTENDED"),
        ("SELL", 0.0): (None, None, 1.4, "STANDARD"),
        ("SELL", 2.0): (None, None, 1.4, "STANDARD"),
        ("SELL", 20.0): (None, None, 1.4, "STANDARD"),
    }
    for direction in ("BUY", "SELL"):
        for atr in (0.0, 2.0, 20.0):
            for current_sl in (0.0, 3880.0, 3900.0, 3920.0):
                res = dynamic_risk.compute_dynamic_atr_trail_stop(
                    direction=direction,
                    entry_price=3895.0,
                    live_price=3905.0,
                    prev_peak=3905.0,
                    current_sl=current_sl,
                    atr=atr,
                    policy=pol,
                    digits=2,
                )
                assert res["r_lock_floor"] is None
                assert (
                    res["candidate_sl"],
                    res["stop_source"],
                    res["trail_mult"],
                    res["trail_zone"],
                ) == expected[(direction, atr)]
                assert res["stop_source"] in ("ATR_TRAIL", "BE_LOCK", "MILESTONE_FLOOR", None)
                if res["stop_source"] is not None:
                    # the engine may propose a looser candidate (the ATR trail trails
                    # the broker stop), but it must never APPLY one: a looser pick is
                    # always reported as "no update", so the stop is left alone
                    if direction == "BUY" and current_sl > 0.0:
                        assert res["candidate_sl"] >= current_sl or res["should_update"] is False
                    elif direction == "SELL" and current_sl > 0.0:
                        assert res["candidate_sl"] <= current_sl or res["should_update"] is False


def test_trail_stop_uses_r_lock_floor_when_tighter():
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=20.0,              # wide ATR -> the ATR trail distance clamps to the $12 ceiling
        policy=pol,
        digits=2,
        r_lock_floor=3898.0,
    )
    assert res["r_lock_floor"] == pytest.approx(3898.0)
    assert res["candidate_sl"] == pytest.approx(3898.0)
    assert res["stop_source"] == "R_LOCK"


def test_trail_stop_r_lock_never_loosens_an_existing_stop():
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3903.0,     # already tighter than the R floor
        atr=2.0,
        policy=pol,
        digits=2,
        r_lock_floor=3902.8,   # the R lock wins the picker, but it is looser than the stop in place
    )
    assert res["stop_source"] == "R_LOCK"
    assert res["candidate_sl"] >= 3900.0
    # the existing stop is already tighter, so no SL update may be sent
    assert res["should_update"] is False


def test_trail_stop_sell_r_lock_is_mirrored():
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="SELL",
        entry_price=3905.0,
        live_price=3895.0,
        prev_peak=3895.0,
        current_sl=3920.0,
        atr=20.0,
        policy=pol,
        digits=2,
        r_lock_floor=3902.0,
    )
    assert res["candidate_sl"] == pytest.approx(3902.0)
    assert res["stop_source"] == "R_LOCK"


def test_trail_stop_reports_no_floor_when_another_candidate_wins():
    """`r_lock_floor` must be None unless the R lock actually won the picker."""
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=2.0,               # narrow ATR -> the ATR trail is the tighter candidate
        policy=pol,
        digits=2,
        r_lock_floor=3890.0,   # valid, but looser than the ATR trail
    )
    assert res["stop_source"] == "ATR_TRAIL"
    assert res["r_lock_floor"] is None


def test_trail_stop_ignores_a_non_numeric_or_invalid_floor():
    """A malformed floor must never raise inside the per-tick stop computation."""
    import dynamic_risk

    pol = _atr_policy()
    for bad in ("abc", 0.0, -5.0, None):
        res = dynamic_risk.compute_dynamic_atr_trail_stop(
            direction="BUY",
            entry_price=3895.0,
            live_price=3905.0,
            prev_peak=3905.0,
            current_sl=3880.0,
            atr=2.0,
            policy=pol,
            digits=2,
            r_lock_floor=bad,
        )
        assert res["r_lock_floor"] is None
        assert res["stop_source"] != "R_LOCK"


def test_the_picker_trusts_the_floor_and_the_producer_guards_the_market():
    """Document the split of responsibility for the R-lock floor.

    `risk_lifecycle.compute_r_lock_floor` is the market guard: it returns None
    when the floor is not valid against the live price, so a floor that reaches
    the picker has already been validated by the producer. The picker itself
    only checks that the floor is a positive price -- it does not re-validate
    it against the market -- and the executor applies its own broker
    minimum-distance check before sending any stop. This test pins that
    contract, so a future change to either side is deliberate.
    """
    import dynamic_risk

    # The producer refuses the floor: it is not valid against the live price.
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 10.0, 3899.0, _policy()) is None

    # The picker trusts it -- it only checks that the floor is a positive price.
    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3900.0,
        live_price=3899.0,
        prev_peak=3900.0,
        current_sl=3880.0,
        atr=20.0,
        policy=pol,
        digits=2,
        r_lock_floor=3900.0,
    )
    assert res["stop_source"] == "R_LOCK"
    assert res["r_lock_floor"] == pytest.approx(3900.0)
    assert res["candidate_sl"] == pytest.approx(3900.0)


def test_the_reported_r_lock_floor_is_pre_clamp_and_the_candidate_is_post_clamp():
    """The reported floor and the effective stop can differ.

    `"r_lock_floor"` is the R floor that won the picker, before the broker
    minimum-distance clamp is applied; `"candidate_sl"` is the effective stop
    price that would actually be sent. A consumer must take the price from
    `"candidate_sl"` and use `"r_lock_floor"` only as the provenance signal.
    """
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3900.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=20.0,
        policy=pol,
        digits=2,
        stops_buffer=2.0,
        r_lock_floor=3904.0,
    )
    # The floor won the picker and is reported unchanged...
    assert res["stop_source"] == "R_LOCK"
    assert res["r_lock_floor"] == pytest.approx(3904.0)
    # ...but the broker minimum-distance clamp moved the effective stop.
    assert res["candidate_sl"] == pytest.approx(3903.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
cd C:\Users\kambo\Code\telegram-mt5-copier
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'risk_lifecycle'`.

- [ ] **Step 3: Create `risk_lifecycle.py`**

Create the file with exactly this content:

```python
"""Trade lifecycle guards: R-relative profit floor and trade-age management.

Pure domain module (standard library only, no MT5 import, no I/O), following the
discipline of `dynamic_risk.py` and `risk_controls.py`. It decides *what* should
happen to a signal; `mt5_executor.py` carries the decision out against the broker.

Why the R floor exists
-----------------------
The ATR engine arms its breakeven lock at ``max(0.75 * ATR, $3.50)`` of favourable
excursion regardless of how far the trade can lose. A $22/oz stop and a $2.00 scalp
therefore both become risk-free after the same $3.50 -- the wide trade risks $22 to
make $3.50 risk-free. The R floor scales the lock to each trade's own initial risk
(``R = |entry - initial SL|``), so a wide trade waits until it has earned a fair
multiple before it is protected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Dict, Optional, Sequence, Tuple


__all__ = [
    "ACTION_NONE",
    "ACTION_DERISK",
    "ACTION_FLATTEN",
    "ACTION_EXPIRE_PENDING",
    "ACTION_SESSION_FLATTEN",
    "DEFAULT_R_LOCK_TIERS",
    "LifecyclePolicy",
    "apply_lifecycle_command",
    "apply_rlock_command",
    "compute_initial_r",
    "compute_r_lock_floor",
    "evaluate_trade_age",
    "parse_r_lock_tiers",
    "read_lifecycle_policy",
]

ACTION_NONE = "NONE"
ACTION_DERISK = "DERISK"
ACTION_FLATTEN = "FLATTEN"
ACTION_EXPIRE_PENDING = "EXPIRE_PENDING"
ACTION_SESSION_FLATTEN = "SESSION_FLATTEN"

#: ``(r_threshold, lock_fraction)`` pairs. The floor locks ``lock_fraction * R``
#: above entry once the trade has travelled ``r_threshold * R`` in its favour.
DEFAULT_R_LOCK_TIERS: Tuple[Tuple[float, float], ...] = (
    (0.50, 0.00),
    (1.00, 0.25),
    (1.50, 0.50),
    (2.00, 0.75),
)

_ON_TOKENS = frozenset({"on", "true", "1", "enable", "enabled"})
_OFF_TOKENS = frozenset({"off", "false", "0", "disable", "disabled", "none"})

_LIFECYCLE_USAGE = (
    "⚠️ **Invalid value.** Usage: `/lifecycle on|off`\n"
    "Examples: `/lifecycle on` or `/lifecycle off`."
)
_RLOCK_USAGE = (
    "⚠️ **Invalid value.** Usage: `/rlock on|off`\n"
    "Examples: `/rlock on` or `/rlock off`."
)


def _safe_float(value: Any, default: float) -> float:
    """Return ``float(value)`` when it is a finite real number, else ``default``."""
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int) -> int:
    """Return ``int(value)`` when it is convertible, else ``default``."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool) -> bool:
    """Return a bool from a real bool, a numeric flag, or a known token."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return bool(value)
    token = str(value or "").strip().lower()
    if token in _ON_TOKENS:
        return True
    if token in _OFF_TOKENS:
        return False
    return default


@dataclass(frozen=True)
class LifecyclePolicy:
    """Immutable snapshot of the R-lock and trade-age guard settings."""

    r_lock_enabled: bool = False
    r_lock_tiers: Tuple[Tuple[float, float], ...] = DEFAULT_R_LOCK_TIERS
    r_lock_min_r_dollars: float = 0.50
    lifecycle_enabled: bool = False
    derisk_after_seconds: int = 7200
    flatten_after_seconds: int = 21600
    pending_expire_seconds: int = 43200
    session_flatten_enabled: bool = False
    session_flatten_at: str = "23:30"
    max_close_retries: int = 3


def parse_r_lock_tiers(raw: Any) -> Tuple[Tuple[float, float], ...]:
    """Parse ``"0.5:0.00,1.0:0.25"`` into ascending ``((r_threshold, lock_fraction), ...)``.

    Malformed entries are skipped rather than raising, so a typo in `.env` can
    never stop the bot from booting. The result is always sorted ascending by
    ``r_threshold`` so "the highest reached tier" is well defined.
    """
    if isinstance(raw, (list, tuple)):
        items: Sequence[Any] = list(raw)
    else:
        items = str(raw or "").split(",")

    parsed = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            r_thr = _safe_float(item[0], -1.0)
            frac = _safe_float(item[1], -1.0)
        else:
            text = str(item or "").strip()
            if not text:
                continue
            parts = text.split(":")
            if len(parts) != 2:
                continue
            r_thr = _safe_float(parts[0], -1.0)
            frac = _safe_float(parts[1], -1.0)
        if r_thr <= 0.0 or frac < 0.0 or frac > 1.0:
            continue
        parsed.append((r_thr, frac))
    parsed.sort(key=lambda pair: pair[0])
    return tuple(parsed)


def read_lifecycle_policy(cfg_mod: Any) -> LifecyclePolicy:
    """Read the lifecycle policy from a config-like module.

    Every value is normalised defensively: an inverted or malformed setting is
    repaired rather than rejected, so the bot always boots.
    """
    tiers = parse_r_lock_tiers(getattr(cfg_mod, "R_LOCK_TIERS", DEFAULT_R_LOCK_TIERS))
    if not tiers:
        tiers = DEFAULT_R_LOCK_TIERS

    derisk = max(0, _safe_int(getattr(cfg_mod, "LIFECYCLE_DERISK_SECONDS", 7200), 7200))
    flatten = max(0, _safe_int(getattr(cfg_mod, "LIFECYCLE_FLATTEN_SECONDS", 21600), 21600))
    if flatten < derisk:
        flatten = derisk

    session_at = str(getattr(cfg_mod, "LIFECYCLE_SESSION_FLATTEN_AT", "23:30") or "23:30").strip()
    parts = session_at.split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        hh, mm = 23, 30
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        hh, mm = 23, 30
    session_at = f"{hh:02d}:{mm:02d}"

    return LifecyclePolicy(
        r_lock_enabled=_safe_bool(getattr(cfg_mod, "R_LOCK_ENABLED", False), False),
        r_lock_tiers=tiers,
        r_lock_min_r_dollars=max(0.0, _safe_float(getattr(cfg_mod, "R_LOCK_MIN_R_DOLLARS", 0.50), 0.50)),
        lifecycle_enabled=_safe_bool(getattr(cfg_mod, "LIFECYCLE_ENABLED", False), False),
        derisk_after_seconds=derisk,
        flatten_after_seconds=flatten,
        pending_expire_seconds=max(0, _safe_int(getattr(cfg_mod, "LIFECYCLE_PENDING_EXPIRE_SECONDS", 43200), 43200)),
        session_flatten_enabled=_safe_bool(getattr(cfg_mod, "LIFECYCLE_SESSION_FLATTEN_ENABLED", False), False),
        session_flatten_at=session_at,
        max_close_retries=max(0, _safe_int(getattr(cfg_mod, "LIFECYCLE_MAX_CLOSE_RETRIES", 3), 3)),
    )


def compute_initial_r(entry_price: float, initial_sl: float) -> Optional[float]:
    """Return the initial risk per ounce (``|entry - initial SL|``) or ``None``."""
    entry = _safe_float(entry_price, 0.0)
    stop = _safe_float(initial_sl, 0.0)
    if entry <= 0.0 or stop <= 0.0:
        return None
    r = abs(entry - stop)
    return r if r > 1e-9 else None


def compute_r_lock_floor(
    direction: str,
    entry_price: float,
    initial_sl: float,
    mfe_dollars: float,
    live_price: float,
    policy: LifecyclePolicy,
    digits: int = 2,
) -> Optional[float]:
    """Return the R-relative stop floor for the current favourable excursion.

    The active fraction is the **greatest lock fraction among the tiers whose
    ``r_threshold * R`` has been reached**. Taking the greatest reached fraction
    (rather than "the highest threshold reached") keeps the floor order-
    independent and monotone: the lock can never loosen as the excursion grows,
    even if a caller builds ``LifecyclePolicy`` directly with unsorted tiers.
    For the default ladder the two rules agree exactly:
    ``0.5R -> entry``, ``1.0R -> +0.25R``, ``1.5R -> +0.50R``, ``2.0R -> +0.75R``.

    The floor is ``entry + sign * (lock_fraction * R)`` -- never below entry for
    a BUY and never above entry for a SELL.

    Returns ``None`` when:
    - the R lock is disabled, the direction is not ``BUY``/``SELL``, or the
      initial risk is unknown / below ``r_lock_min_r_dollars``;
    - no tier has been reached yet;
    - the live price is unusable (missing, non-numeric or non-positive), so
      no floor can be validated against the market;
    - the floor is not valid against the current market (for a BUY the floor
      must stay strictly below ``live_price``, for a SELL strictly above it).
      The floor is derived from the peak excursion, so a retracement can carry
      price back past it; submitting a stop on the wrong side of the market
      would be rejected by the broker with retcode 10016. The consumer's own
      stops-buffer clamp is a second line of defence, not the only one.

    Note: the existing ``BE_LOCK`` candidate in `dynamic_risk.py` may sit
    slightly tighter than this floor (it adds the $0.50 breakeven offset), and
    the candidate picker keeps the tightest stop.
    """
    if not policy.r_lock_enabled:
        return None

    dir_token = str(direction or "").strip().upper()
    if dir_token not in ("BUY", "SELL"):
        return None
    sign = 1.0 if dir_token == "BUY" else -1.0

    entry = _safe_float(entry_price, 0.0)
    if entry <= 0.0:
        return None

    r = compute_initial_r(entry, initial_sl)
    if r is None or r < policy.r_lock_min_r_dollars:
        return None

    mfe = _safe_float(mfe_dollars, 0.0)
    if mfe <= 0.0:
        return None

    reached = [
        fraction
        for r_threshold, fraction in policy.r_lock_tiers
        if mfe + 1e-9 >= r_threshold * r
    ]
    if not reached:
        return None
    lock_fraction = max(reached)

    floor_px = round(entry + sign * (lock_fraction * r), digits)

    live = _safe_float(live_price, 0.0)
    if live <= 0.0:
        return None
    if sign > 0 and floor_px >= live:
        return None
    if sign < 0 and floor_px <= live:
        return None

    return floor_px


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or ``None``."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_session_at(session_at: str) -> Tuple[int, int]:
    """Parse ``"23:30"`` into ``(hour, minute)``."""
    parts = str(session_at or "23:30").split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return 23, 30
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return 23, 30
    return hh, mm


def evaluate_trade_age(
    signal_data: Dict[str, Any],
    now_utc: datetime,
    policy: LifecyclePolicy,
    server_offset_seconds: int = 0,
) -> Dict[str, Any]:
    """Decide which lifecycle action (if any) applies to a signal right now.

    Returns a dict with ``action`` (one of the ``ACTION_*`` constants),
    ``reason``, ``signal_age_seconds``, ``fill_age_seconds`` and ``server_time``.

    Precedence: ``SESSION_FLATTEN`` > ``FLATTEN`` > ``DERISK`` > ``EXPIRE_PENDING``.
    """
    sig = signal_data if isinstance(signal_data, dict) else {}
    has_market = bool(sig.get("market_tickets"))
    has_pending = bool(sig.get("pending_tickets"))

    now = _parse_iso_utc(now_utc) or datetime.now(timezone.utc)
    signal_dt = _parse_iso_utc(sig.get("timestamp"))
    # The fill clock is the EARLIEST known fill (the signal stamp or any layer
    # stamp). Fall back to the signal timestamp when no fill time is known: the
    # position is at least as old as the signal, so the age guard must not stand down.
    fill_dt = resolve_fill_time(sig) or signal_dt
    signal_age = int((now - signal_dt).total_seconds()) if signal_dt else 0
    fill_age = int((now - fill_dt).total_seconds()) if fill_dt else None

    server_dt = now + timedelta(seconds=int(server_offset_seconds or 0))
    server_time = server_dt.strftime("%H:%M")
    hh, mm = _parse_session_at(policy.session_flatten_at)
    server_minutes = server_dt.hour * 60 + server_dt.minute
    session_minutes = hh * 60 + mm

    result: Dict[str, Any] = {
        "action": ACTION_NONE,
        "reason": "",
        "signal_age_seconds": signal_age,
        "fill_age_seconds": fill_age,
        "server_time": server_time,
    }

    if not policy.lifecycle_enabled:
        result["reason"] = "lifecycle guard disabled"
        return result

    if not has_market and not has_pending:
        result["reason"] = "no open or pending orders"
        return result

    if policy.session_flatten_enabled and has_market and server_minutes >= session_minutes:
        result["action"] = ACTION_SESSION_FLATTEN
        result["reason"] = f"session cut reached ({server_time} >= {policy.session_flatten_at} broker time)"
        return result

    if has_market and fill_age is not None and fill_age >= policy.flatten_after_seconds:
        result["action"] = ACTION_FLATTEN
        result["reason"] = f"position age {fill_age}s >= {policy.flatten_after_seconds}s"
        return result

    if has_market and fill_age is not None and fill_age >= policy.derisk_after_seconds:
        result["action"] = ACTION_DERISK
        result["reason"] = f"position age {fill_age}s >= {policy.derisk_after_seconds}s"
        return result

    if not has_market and has_pending and signal_age >= policy.pending_expire_seconds:
        result["action"] = ACTION_EXPIRE_PENDING
        result["reason"] = f"signal age {signal_age}s >= {policy.pending_expire_seconds}s and never filled"
        return result

    if signal_dt is None and fill_dt is None:
        result["reason"] = "unknown age (signal carries no timestamp)"
        return result

    result["reason"] = "within thresholds"
    return result


def _format_tiers(policy: LifecyclePolicy) -> str:
    parts = []
    for r_threshold, fraction in policy.r_lock_tiers:
        label = "BE" if fraction <= 0.0 else f"{fraction:.0%}"
        parts.append(f"{r_threshold:g}R -> {label}")
    return ", ".join(parts)


def apply_rlock_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]:
    """Query or toggle the R-relative profit floor via Saved Messages (`/rlock`)."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    policy = read_lifecycle_policy(cfg_mod)
    if not cleaned:
        badge = "🟢 ON" if policy.r_lock_enabled else "⚪ OFF"
        return False, (
            f"📐 **R-Relative Profit Floor (`/rlock`): {badge}**\n"
            f"• Tiers: `{_format_tiers(policy)}`\n"
            f"• Minimum R: `${policy.r_lock_min_r_dollars:.2f}` per ounce\n"
            "• Toggles `R_LOCK_ENABLED`\n"
            "• Change: `/rlock on` | `/rlock off`\n"
            "• The floor is a peak-derived level: it disappears if price retraces past it,"
            " so it can never place a stop on the wrong side of the market."
        )
    token = cleaned[0].lower()
    if token in _ON_TOKENS:
        setattr(cfg_mod, "R_LOCK_ENABLED", True)
        return True, (
            f"📐 **R-Relative Profit Floor Enabled → `ON`**\n"
            f"• Tiers: `{_format_tiers(policy)}`\n"
            "• The floor only ever raises the stop, and it disappears if price retraces past it."
        )
    if token in _OFF_TOKENS:
        setattr(cfg_mod, "R_LOCK_ENABLED", False)
        return True, "⚪ **R-Relative Profit Floor Disabled → `OFF`** (ATR trail only)."
    return False, _RLOCK_USAGE


def apply_lifecycle_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]:
    """Query or toggle the trade-age guard via Saved Messages (`/lifecycle`)."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    policy = read_lifecycle_policy(cfg_mod)
    if not cleaned:
        badge = "🟢 ON" if policy.lifecycle_enabled else "⚪ OFF"
        session = (
            f"`{policy.session_flatten_at}` broker time"
            if policy.session_flatten_enabled
            else "⚪ OFF"
        )
        return False, (
            f"⏱ **Trade Lifecycle Guard (`/lifecycle`): {badge}**\n"
            f"• De-risk at: `{policy.derisk_after_seconds}s` (cancel pending + move to BE)\n"
            f"• Flatten at: `{policy.flatten_after_seconds}s` (close at market)\n"
            f"• Pending expiry: `{policy.pending_expire_seconds}s`\n"
            f"• Session cut: {session}\n"
            f"• Max close retries: `{policy.max_close_retries}`\n"
            "• Toggles `LIFECYCLE_ENABLED`\n"
            "• Change: `/lifecycle on` | `/lifecycle off`"
        )
    token = cleaned[0].lower()
    if token in _ON_TOKENS:
        setattr(cfg_mod, "LIFECYCLE_ENABLED", True)
        return True, (
            "⏱ **Trade Lifecycle Guard Enabled → `ON`**\n"
            f"• De-risk at `{policy.derisk_after_seconds}s`, flatten at `{policy.flatten_after_seconds}s`, "
            f"pending expiry `{policy.pending_expire_seconds}s`."
        )
    if token in _OFF_TOKENS:
        setattr(cfg_mod, "LIFECYCLE_ENABLED", False)
        return True, "⚪ **Trade Lifecycle Guard Disabled → `OFF`** (no age-based de-risk or flatten)."
    return False, _LIFECYCLE_USAGE
```

> **Note (Task 3, fix round 2):** the `fill_dt` line in the block above was updated in
> Task 3 (fix round 2), because Task 3 introduced `resolve_fill_time` — the fill clock is
> now the EARLIEST known fill instead of the raw `first_fill_at` key.

- [ ] **Step 4: Add the config knobs**

In `config.py`, immediately after the `DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT` block (line 543-545), insert:

```python

# Trade Lifecycle Guard (R-relative profit floor + trade-age management)
# Both features ship DISABLED: enable live with `/rlock on` and `/lifecycle on`.
R_LOCK_ENABLED: bool = _env_bool("R_LOCK_ENABLED", False)
R_LOCK_TIERS: str = os.getenv("R_LOCK_TIERS", "0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75").strip()
R_LOCK_MIN_R_DOLLARS: float = _env_float(
    "R_LOCK_MIN_R_DOLLARS", 0.50, min_val=0.0, max_val=100.0
)
LIFECYCLE_ENABLED: bool = _env_bool("LIFECYCLE_ENABLED", False)
LIFECYCLE_DERISK_SECONDS: int = _env_int(
    "LIFECYCLE_DERISK_SECONDS", 7200, min_val=0, max_val=604800
)
LIFECYCLE_FLATTEN_SECONDS: int = _env_int(
    "LIFECYCLE_FLATTEN_SECONDS", 21600, min_val=0, max_val=604800
)
LIFECYCLE_PENDING_EXPIRE_SECONDS: int = _env_int(
    "LIFECYCLE_PENDING_EXPIRE_SECONDS", 43200, min_val=0, max_val=604800
)
LIFECYCLE_SESSION_FLATTEN_ENABLED: bool = _env_bool("LIFECYCLE_SESSION_FLATTEN_ENABLED", False)
LIFECYCLE_SESSION_FLATTEN_AT: str = os.getenv("LIFECYCLE_SESSION_FLATTEN_AT", "23:30").strip()
LIFECYCLE_MAX_CLOSE_RETRIES: int = _env_int(
    "LIFECYCLE_MAX_CLOSE_RETRIES", 3, min_val=0, max_val=50
)
```

> **Do not use `_env_choice` for `R_LOCK_TIERS` or `LIFECYCLE_SESSION_FLATTEN_AT`.** `_env_choice` validates the value against a tuple of allowed values and falls back to the default when the value is not in that tuple — it cannot hold a free-form string like `"0.5:0.00,1.0:0.25"`. Both knobs above therefore use `os.getenv` directly. `config.py` already imports `os`.

Then append after the existing `validate_dynamic_risk_pairs()` function and its call (i.e. after line 565):

```python


def validate_lifecycle_pairs() -> None:
    """Ensure the trade-age guard thresholds are ordered (flatten must not precede de-risk)."""
    g = globals()
    derisk = int(g.get("LIFECYCLE_DERISK_SECONDS", 0))
    flatten = int(g.get("LIFECYCLE_FLATTEN_SECONDS", 0))
    if flatten < derisk:
        _cfg_logger.warning(
            f"LIFECYCLE_FLATTEN_SECONDS ({flatten}) < LIFECYCLE_DERISK_SECONDS ({derisk}); "
            "raising the flatten threshold to match."
        )
        g["LIFECYCLE_FLATTEN_SECONDS"] = derisk


validate_lifecycle_pairs()
```

- [ ] **Step 5: Add the startup validation**

In `startup_checks.py`, add a new function immediately after `_validate_dynamic_risk_config` (which ends at line 563) and call it from `validate_environment` right after the `_validate_dynamic_risk_config(cfg, errors)` call at line 566:

```python


def _validate_lifecycle_config(cfg: Any, errors: List[str]) -> None:
    """Validate the Trade Lifecycle Guard knobs (R floor + trade-age guard)."""
    for bool_flag in ("R_LOCK_ENABLED", "LIFECYCLE_ENABLED", "LIFECYCLE_SESSION_FLATTEN_ENABLED"):
        if hasattr(cfg, bool_flag):
            flag_val = getattr(cfg, bool_flag)
            if not isinstance(flag_val, bool):
                errors.append(f"{bool_flag} must be a bool, got {flag_val!r}.")

    derisk = getattr(cfg, "LIFECYCLE_DERISK_SECONDS", 7200)
    flatten = getattr(cfg, "LIFECYCLE_FLATTEN_SECONDS", 21600)
    if not isinstance(derisk, int) or isinstance(derisk, bool) or derisk < 0:
        errors.append(f"LIFECYCLE_DERISK_SECONDS must be a non-negative int, got {derisk!r}.")
    if not isinstance(flatten, int) or isinstance(flatten, bool) or flatten < 0:
        errors.append(f"LIFECYCLE_FLATTEN_SECONDS must be a non-negative int, got {flatten!r}.")
    if isinstance(derisk, int) and isinstance(flatten, int) and not isinstance(derisk, bool) and not isinstance(flatten, bool):
        if flatten < derisk:
            errors.append(
                f"LIFECYCLE_FLATTEN_SECONDS ({flatten}) must be >= LIFECYCLE_DERISK_SECONDS ({derisk})."
            )

    tiers_raw = getattr(cfg, "R_LOCK_TIERS", "")
    if str(tiers_raw).strip():
        from risk_lifecycle import parse_r_lock_tiers

        parsed = parse_r_lock_tiers(tiers_raw)
        if not parsed:
            errors.append(
                f"R_LOCK_TIERS ({tiers_raw!r}) contains no valid 'r:fraction' pair."
            )

    session_at = str(getattr(cfg, "LIFECYCLE_SESSION_FLATTEN_AT", "23:30")).strip()
    parts = session_at.split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except (TypeError, ValueError, IndexError):
        errors.append(f"LIFECYCLE_SESSION_FLATTEN_AT must be 'HH:MM', got {session_at!r}.")
```

And change the call site at line 566 from:

```python
    _validate_dynamic_risk_config(cfg, errors)
```

to:

```python
    _validate_dynamic_risk_config(cfg, errors)
    _validate_lifecycle_config(cfg, errors)
```

- [ ] **Step 6: Add the knobs to `.env.example`**

Append to `.env.example`:

```
# Trade Lifecycle Guard (R-relative profit floor + trade-age management)
# Both ship DISABLED. Enable live from Saved Messages: /rlock on, /lifecycle on.
R_LOCK_ENABLED=false
R_LOCK_TIERS=0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75
R_LOCK_MIN_R_DOLLARS=0.50
LIFECYCLE_ENABLED=false
LIFECYCLE_DERISK_SECONDS=7200
LIFECYCLE_FLATTEN_SECONDS=21600
LIFECYCLE_PENDING_EXPIRE_SECONDS=43200
LIFECYCLE_SESSION_FLATTEN_ENABLED=false
LIFECYCLE_SESSION_FLATTEN_AT=23:30
LIFECYCLE_MAX_CLOSE_RETRIES=3
```

Do **not** modify the local `.env` in this task — `.env` is gitignored and is a runtime artifact.

- [ ] **Step 7: Run the tests to verify they pass**

```powershell
cd C:\Users\kambo\Code\telegram-mt5-copier
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS (19 tests).

- [ ] **Step 8: Run the full suite to confirm nothing regressed**

```powershell
python -m pytest -q
```

Expected: `241 passed` plus the new tests, **0 failed**.

- [ ] **Step 9: Commit**

```powershell
git add risk_lifecycle.py tests/test_trade_lifecycle.py config.py startup_checks.py .env.example
git commit -m "feat(risk_lifecycle): pure trade-lifecycle domain core + config knobs

Adds the R-relative profit floor and the trade-age guard as a pure, stdlib-only
domain module. Both features ship disabled (R_LOCK_ENABLED=false,
LIFECYCLE_ENABLED=false) so a fresh clone behaves identically to v3.7.0."
```

---

## Task 2: `dynamic_risk.py` — the `R_LOCK` stop candidate

**Files:**
- Modify: `dynamic_risk.py:471-586` (`compute_dynamic_atr_trail_stop`)
- Modify: `tests/test_trade_lifecycle.py` (append the candidate tests)

**Interfaces:**
- Consumes: nothing from Task 1 (this task is independent of `risk_lifecycle`).
- Produces: `compute_dynamic_atr_trail_stop(..., *, r_lock_floor: Optional[float] = None) -> Dict[str, Any]` — the result dict gains `"r_lock_floor"`, which is the R floor **only when it won the picker** (`stop_source == "R_LOCK"`), otherwise `None`. Tie-break priority becomes `R_LOCK` > `MILESTONE_FLOOR` > `ATR_TRAIL` > `BE_LOCK`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- dynamic_risk R_LOCK candidate ---------------------------------------

def _atr_policy():
    """An explicit policy, so the tests do not depend on ambient config values."""
    import dynamic_risk

    return dynamic_risk.DynamicRiskPolicy(
        sltp_enabled=True,
        sltp_mode="ADAPTIVE",
        atr_timeframe="M5",
        atr_period=14,
        atr_baseline_dollars=4.00,
        sl_atr_mult=2.50,
        tp_atr_mult=1.50,
        tp_step_atr_mult=1.50,
        sl_min_dollars=4.50,
        sl_max_dollars=35.00,
        tp_min_dollars=2.50,
        tp_max_dollars=50.00,
        scale_min=0.75,
        scale_max=1.75,
        trail_enabled=True,
        trail_timeframe="M5",
        trail_atr_mult=1.40,
        trail_extended_mfe_dollars=10.00,
        trail_extended_atr_mult=1.00,
        trail_homerun_mfe_dollars=18.00,
        trail_homerun_atr_mult=0.80,
        trail_activation_atr_mult=1.00,
        trail_min_dollars=2.50,
        trail_max_dollars=12.00,
        trail_step_dollars=0.25,
        be_activation_atr_mult=0.75,
        be_min_mfe_dollars=3.50,
        be_lock_offset_dollars=0.50,
        tp_trail_enabled=True,
        tp_lift_atr_mult=0.50,
        tp_lift_progress_pct=0.75,
        tp1_lift_min_progress_pct=0.50,
    )


def test_trail_stop_unchanged_when_r_lock_floor_is_none():
    """With the default floor the engine must behave exactly as it did before."""
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=2.0,
        policy=pol,
        digits=2,
    )
    assert res["r_lock_floor"] is None
    assert res["stop_source"] == "ATR_TRAIL"
    # MFE 10.00 -> EXTENDED zone -> mult 1.00 -> trail_dist = clamp(2.00, 2.50, 12.00)
    # = 2.50 -> atr_sl = 3905.00 - 2.50 = 3902.50 (pinned from the pre-R_LOCK baseline).
    assert res["candidate_sl"] == pytest.approx(3902.50)
    assert res["should_update"] is True


def test_trail_stop_is_unchanged_across_a_baseline_grid():
    """Permanent regression grid: `r_lock_floor=None` must reproduce the baseline exactly."""
    import dynamic_risk

    pol = _atr_policy()
    # Exact baseline values, pinned from the pre-R_LOCK implementation (git 4c88faa)
    # with this explicit policy: (candidate_sl, stop_source, trail_mult, trail_zone).
    # BUY atr=0.0 -> trail disabled by an invalid ATR; BUY atr=2.0 -> ATR trail armed;
    # BUY atr=20.0 -> MFE 10.00 is below the 20.00 trail activation, so nothing is armed.
    # SELL never reaches a positive MFE here, so it is never armed.
    expected = {
        ("BUY", 0.0): (None, None, 1.4, "STANDARD"),
        ("BUY", 2.0): (3902.5, "ATR_TRAIL", 1.0, "EXTENDED"),
        ("BUY", 20.0): (None, None, 1.0, "EXTENDED"),
        ("SELL", 0.0): (None, None, 1.4, "STANDARD"),
        ("SELL", 2.0): (None, None, 1.4, "STANDARD"),
        ("SELL", 20.0): (None, None, 1.4, "STANDARD"),
    }
    for direction in ("BUY", "SELL"):
        for atr in (0.0, 2.0, 20.0):
            for current_sl in (0.0, 3880.0, 3900.0, 3920.0):
                res = dynamic_risk.compute_dynamic_atr_trail_stop(
                    direction=direction,
                    entry_price=3895.0,
                    live_price=3905.0,
                    prev_peak=3905.0,
                    current_sl=current_sl,
                    atr=atr,
                    policy=pol,
                    digits=2,
                )
                assert res["r_lock_floor"] is None
                assert (
                    res["candidate_sl"],
                    res["stop_source"],
                    res["trail_mult"],
                    res["trail_zone"],
                ) == expected[(direction, atr)]
                assert res["stop_source"] in ("ATR_TRAIL", "BE_LOCK", "MILESTONE_FLOOR", None)
                if res["stop_source"] is not None:
                    # the engine may propose a looser candidate (the ATR trail trails
                    # the broker stop), but it must never APPLY one: a looser pick is
                    # always reported as "no update", so the stop is left alone
                    if direction == "BUY" and current_sl > 0.0:
                        assert res["candidate_sl"] >= current_sl or res["should_update"] is False
                    elif direction == "SELL" and current_sl > 0.0:
                        assert res["candidate_sl"] <= current_sl or res["should_update"] is False


def test_trail_stop_uses_r_lock_floor_when_tighter():
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=20.0,              # wide ATR -> the ATR trail distance clamps to the $12 ceiling
        policy=pol,
        digits=2,
        r_lock_floor=3898.0,
    )
    assert res["r_lock_floor"] == pytest.approx(3898.0)
    assert res["candidate_sl"] == pytest.approx(3898.0)
    assert res["stop_source"] == "R_LOCK"


def test_trail_stop_r_lock_never_loosens_an_existing_stop():
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3903.0,     # already tighter than the R floor
        atr=2.0,
        policy=pol,
        digits=2,
        r_lock_floor=3902.8,   # the R lock wins the picker, but it is looser than the stop in place
    )
    assert res["stop_source"] == "R_LOCK"
    assert res["candidate_sl"] >= 3900.0
    # the existing stop is already tighter, so no SL update may be sent
    assert res["should_update"] is False


def test_trail_stop_sell_r_lock_is_mirrored():
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="SELL",
        entry_price=3905.0,
        live_price=3895.0,
        prev_peak=3895.0,
        current_sl=3920.0,
        atr=20.0,
        policy=pol,
        digits=2,
        r_lock_floor=3902.0,
    )
    assert res["candidate_sl"] == pytest.approx(3902.0)
    assert res["stop_source"] == "R_LOCK"


def test_trail_stop_reports_no_floor_when_another_candidate_wins():
    """`r_lock_floor` must be None unless the R lock actually won the picker."""
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=2.0,               # narrow ATR -> the ATR trail is the tighter candidate
        policy=pol,
        digits=2,
        r_lock_floor=3890.0,   # valid, but looser than the ATR trail
    )
    assert res["stop_source"] == "ATR_TRAIL"
    assert res["r_lock_floor"] is None


def test_trail_stop_ignores_a_non_numeric_or_invalid_floor():
    """A malformed floor must never raise inside the per-tick stop computation."""
    import dynamic_risk

    pol = _atr_policy()
    for bad in ("abc", 0.0, -5.0, None):
        res = dynamic_risk.compute_dynamic_atr_trail_stop(
            direction="BUY",
            entry_price=3895.0,
            live_price=3905.0,
            prev_peak=3905.0,
            current_sl=3880.0,
            atr=2.0,
            policy=pol,
            digits=2,
            r_lock_floor=bad,
        )
        assert res["r_lock_floor"] is None
        assert res["stop_source"] != "R_LOCK"


def test_the_picker_trusts_the_floor_and_the_producer_guards_the_market():
    """Document the split of responsibility for the R-lock floor.

    `risk_lifecycle.compute_r_lock_floor` is the market guard: it returns None
    when the floor is not valid against the live price, so a floor that reaches
    the picker has already been validated by the producer. The picker itself
    only checks that the floor is a positive price -- it does not re-validate
    it against the market -- and the executor applies its own broker
    minimum-distance check before sending any stop. This test pins that
    contract, so a future change to either side is deliberate.
    """
    import dynamic_risk

    # The producer refuses the floor: it is not valid against the live price.
    assert rl.compute_r_lock_floor("BUY", 3900.0, 3880.0, 10.0, 3899.0, _policy()) is None

    # The picker trusts it -- it only checks that the floor is a positive price.
    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3900.0,
        live_price=3899.0,
        prev_peak=3900.0,
        current_sl=3880.0,
        atr=20.0,
        policy=pol,
        digits=2,
        r_lock_floor=3900.0,
    )
    assert res["stop_source"] == "R_LOCK"
    assert res["r_lock_floor"] == pytest.approx(3900.0)
    assert res["candidate_sl"] == pytest.approx(3900.0)


def test_the_reported_r_lock_floor_is_pre_clamp_and_the_candidate_is_post_clamp():
    """The reported floor and the effective stop can differ.

    `"r_lock_floor"` is the R floor that won the picker, before the broker
    minimum-distance clamp is applied; `"candidate_sl"` is the effective stop
    price that would actually be sent. A consumer must take the price from
    `"candidate_sl"` and use `"r_lock_floor"` only as the provenance signal.
    """
    import dynamic_risk

    pol = _atr_policy()
    res = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3900.0,
        live_price=3905.0,
        prev_peak=3905.0,
        current_sl=3880.0,
        atr=20.0,
        policy=pol,
        digits=2,
        stops_buffer=2.0,
        r_lock_floor=3904.0,
    )
    # The floor won the picker and is reported unchanged...
    assert res["stop_source"] == "R_LOCK"
    assert res["r_lock_floor"] == pytest.approx(3904.0)
    # ...but the broker minimum-distance clamp moved the effective stop.
    assert res["candidate_sl"] == pytest.approx(3903.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `TypeError: compute_dynamic_atr_trail_stop() got an unexpected keyword argument 'r_lock_floor'`.

- [ ] **Step 3: Implement the candidate**

In `dynamic_risk.py`, change the signature at line 471-484 to add the new keyword:

```python
def compute_dynamic_atr_trail_stop(
    direction: str,
    entry_price: float,
    live_price: float,
    prev_peak: Optional[float],
    current_sl: Optional[float],
    atr: float,
    policy: DynamicRiskPolicy,
    digits: int = 2,
    stops_buffer: float = 0.0,
    cleared_milestones: Optional[Sequence[str]] = None,
    tp_levels: Optional[Dict[str, float]] = None,
    apply_milestone_floor: bool = True,
    *,
    r_lock_floor: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute forward-only BE Lock, Dynamic ATR Trail SL, and Milestone Floor for an open position.

    `r_lock_floor` is an optional extra floor contributed by the R-relative profit
    lock (`risk_lifecycle.compute_r_lock_floor`). It joins the same candidate list
    as the milestone floor, the ATR trail and the BE lock, so the picker keeps the
    tightest stop and the forward-only invariant is preserved. Passing `None`
    (the default) leaves every existing caller byte-for-byte unchanged.

    The returned `"r_lock_floor"` key means **the R floor that won the picker**:
    it is the rounded floor only when `stop_source == "R_LOCK"`, and `None`
    otherwise (the floor was rejected as invalid, or another candidate was
    tighter). A consumer can therefore treat `res["r_lock_floor"] is not None`
    as exactly "the R lock is the active stop".

    `"r_lock_floor"` is the **pre-clamp** floor, i.e. the price the R lock
    itself produced. `"candidate_sl"` is the **post-clamp** effective stop, i.e.
    the price after the broker minimum-distance clamp was applied. The two can
    differ; a consumer must take the price from `"candidate_sl"` and use
    `"r_lock_floor"` only as the provenance signal.
    """
```

Then replace the candidate block at lines 529-535 with:

```python
    # Validate the R floor ONCE, defensively, and reuse the validated value in
    # the candidate list and in every return path. The candidate guard must
    # never raise on a value the echo would happily `float()`.
    r_lock_val = _safe_float(r_lock_floor, 0.0)
    r_lock_candidate: Optional[float] = round(r_lock_val, digits) if r_lock_val > 0.0 else None

    candidates: List[Tuple[int, str, float]] = []
    if r_lock_candidate is not None:
        candidates.append((0, "R_LOCK", r_lock_candidate))
    if milestone_floor is not None:
        candidates.append((1, "MILESTONE_FLOOR", milestone_floor))
    if atr_sl is not None:
        candidates.append((2, "ATR_TRAIL", atr_sl))
    if be_sl is not None:
        candidates.append((3, "BE_LOCK", be_sl))
```

The tie-break comment above the picker at line 558 must be updated to record the new priority, and the winning source must be captured so the returned key can report the floor that actually won:

```python
    # Tie-break priority: R_LOCK > MILESTONE_FLOOR > ATR_TRAIL > BE_LOCK.
    # The picker always keeps the tightest stop, so a floor can only tighten it.
    _, stop_source, raw_sl = min(
        candidates,
        key=lambda item: (-round(item[2], digits) if sign > 0 else round(item[2], digits), item[0]),
    )
    # The R floor is "in effect" only when it won the picker.
    active_r_lock = r_lock_candidate if stop_source == "R_LOCK" else None
```

Finally, the `"r_lock_floor"` key must be present in **every** return path with **one consistent meaning** — the R floor that won the picker, otherwise `None`:

```python
            "r_lock_floor": None,
```

in the **early-return** dict (the `not policy.trail_enabled or atr_f <= 0.0 ...` branch at lines 488-506) and in the **`not armed`** branch (lines 539-556) — in both cases no candidate was created, so no floor is in effect.

In the **final** return dict (lines 569-586), add:

```python
        "r_lock_floor": active_r_lock,
```

Never echo the raw argument: it may be a non-numeric value, and it may be a floor that was rejected as invalid (a negative price must never be advertised as an active stop).

- [ ] **Step 4: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**. The existing trail tests must still pass — `r_lock_floor=None` keeps the previous behaviour exactly.

- [ ] **Step 6: Commit**

```powershell
git add dynamic_risk.py tests/test_trade_lifecycle.py
git commit -m "feat(dynamic_risk): add R_LOCK stop candidate to the forward-only picker

compute_dynamic_atr_trail_stop gains an additive r_lock_floor keyword. The
floor joins the existing candidate list, so the picker keeps the tightest stop
and the never-loosens invariant is preserved. Default None leaves every
existing caller unchanged."
```

---

## Task 3: `mt5_executor.py` — capture `initial_sl` and `first_fill_at`

**Files:**
- Modify: `mt5_executor.py:541-546` (`MT5Executor.__init__` — the `_last_order_sls` side-channel)
- Modify: `mt5_executor.py:844-853` (`MT5Executor.get_last_placed_sl` — the new accessor)
- Modify: `mt5_executor.py:1575-1580` (`execute_market_order` — record the stop that was sent)
- Modify: `mt5_executor.py:1790-1794` (`_place_pending_order` — record the stop that was sent)
- Modify: `mt5_executor.py:2345-2394` (`_execute_4layer_specs` — the layer dict and its `initial_sl`)
- Modify: `mt5_executor.py:4968-4975` (`apply_signal_breakeven` — the BE fill-race promotion)
- Modify: `mt5_executor.py:6138-6144` (`hydrate_signal_layers_from_mt5` — the startup fill transition)
- Modify: `main.py:3554-3562` (`_reconcile_live_mt5_signals` — the LIVE fill transition)
- Modify: `main.py:5787-5813` (`_resolve_edit_initial_sl` — the new helper)
- Modify: `main.py:6000-6002` (`_handle_message_edited_unlocked` — the symbol digits)
- Modify: `main.py:6637-6648` (the retiered-order branch layer record)
- Modify: `main.py:6880-6892` (the fresh-limit branch layer record)
- Modify: `main.py:6936-6938` (persist the signal-level `first_fill_at`)
- Modify: `main.py:6300` (the pre-edit reconciliation promotion)
- Modify: `main.py:6666` (the re-tier fill-race promotion)
- Modify: `main.py:6721` (the pending-order modify fill-race promotion)
- Modify: `risk_lifecycle.py` (add `stamp_fill_times` + `resolve_fill_time`; use `resolve_fill_time` in `evaluate_trade_age`)
- Modify: `tests/test_trade_lifecycle.py` (append the capture tests)

**Interfaces:**
- Consumes: nothing from Task 1-2.
- Produces:
  - `signal_data["layers"][<layer>]["initial_sl"]` — `Optional[float]`, the stop the order was placed with.
  - `signal_data["layers"][<layer>]["filled_at"]` — `Optional[str]`, ISO-8601 UTC timestamp of that layer's fill.
  - `signal_data["first_fill_at"]` — `Optional[str]`, ISO-8601 UTC timestamp of the first fill (written only when unset).
  - `risk_lifecycle.stamp_fill_times(signal_data, layer_data=None, now=None) -> bool`
  - `risk_lifecycle.resolve_fill_time(signal_data) -> Optional[datetime]`

**Why every fill site matters (do not skip Steps 5-8):**

`hydrate_signal_layers_from_mt5` is called from exactly ONE place — `main.py:3421`, inside
`_startup_hydrate_and_backfill_ledger()` — so it only runs at STARTUP. The LIVE
`PENDING -> FILLED` transition happens in `main.py:3554`, inside `_reconcile_live_mt5_signals`
(`async def` at `main.py:3450`), which sets `l_data["status"] = "FILLED"` directly and never
calls `hydrate_signal_layers_from_mt5`. Stamping only in the executor leaves the live path
unstamped, and `evaluate_trade_age` then falls back to the SIGNAL timestamp — so a limit order
that filled hours after the signal was created makes a brand-new position look hours old and
fires the de-risk/flatten guard on it immediately.

MARKET fills never go through `PENDING -> FILLED` at all: `_execute_4layer_specs` sets
`status = "OPEN"` immediately for MARKET layers. A zone signal normally fills L1 at market and
L2-L4 later as limits, so the MARKET fill is the FIRST fill of the trade and has to be stamped
where the order is placed.

There are TWO MORE places that can fill a layer, and both have to stamp:

- `apply_signal_breakeven` (`mt5_executor.py:4968-4975`) promotes a pending limit that filled
  in the sub-second window BEFORE the 1-second reconciler ran, and then drops the ticket from
  `pending_tickets`. The reconciler's stamping loop lives inside `for t in pd_tickets:`, so a
  ticket removed here is never revisited and never stamped — the promoted position would have
  NO fill time at all, and the age guard would fall back to the signal timestamp.
- The signal-edit path in `main.py` builds its own layer records (`new_l_rec` at
  `main.py:6620`, `new_l_rec_a` at `main.py:6855`). Without a stamp, a MARKET layer created
  by an edit has no fill time, and every layer created by an edit has no `initial_sl`.
- THREE MORE branches of the signal-edit path promote a pending layer that has ALREADY
  FILLED on the broker: the pre-edit reconciliation (`main.py:6294`), the re-tier cancel
  race (`main.py:6663`) and the pending-order SL/TP modify race (`main.py:6718`). Each one
  flips the layer to `order_type="MARKET"`, `status="OPEN"` and REMOVES the ticket from
  `pending_tickets`, so the 1-second reconciler — whose stamping loop lives inside
  `for t in pd_tickets:` — never sees that ticket again and never stamps it. Each branch
  has to stamp the fill itself (see Step 8).

The stop that goes to the broker is NOT always the `sl` argument: when `sl` is `None` or
`0.0`, `_compute_fallback_sl` (`mt5_executor.py:1398`) computes an ATR-based or
`FALLBACK_SL_PIPS`-based stop and THAT is what the broker holds. `initial_sl` must therefore
record the stop that was actually sent — see Step 4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- executor state capture ---------------------------------------------

def test_execute_4layer_specs_records_initial_sl(monkeypatch):
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    captured = {}

    def _fake_layer(symbol, direction, target_price, lot, sl, tp, tick, **kwargs):
        captured["sl"] = sl
        return 9001, "MARKET"

    monkeypatch.setattr(ex, "_execute_layer_price", _fake_layer)
    market, pending, layers = ex._execute_4layer_specs(
        symbol="XAUUSD.s",
        direction="BUY",
        layer_specs=[("L1", 3895.0, 0.01, 3905.0)],
        sl=3880.0,
        tick=SimpleNamespace(bid=3895.0, ask=3895.2, time=1700000000.0),
    )
    assert captured["sl"] == 3880.0
    assert layers["L1"]["initial_sl"] == pytest.approx(3880.0)
    # A MARKET layer fills at placement, so the fill time is stamped right here.
    assert layers["L1"].get("filled_at"), "a MARKET layer must record its fill time"


def test_execute_4layer_specs_does_not_stamp_a_pending_limit_layer(monkeypatch):
    """A LIMIT layer has not filled yet, so it must not carry a fill timestamp."""
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    def _fake_layer(symbol, direction, target_price, lot, sl, tp, tick, **kwargs):
        return 9002, "LIMIT"

    monkeypatch.setattr(ex, "_execute_layer_price", _fake_layer)
    market, pending, layers = ex._execute_4layer_specs(
        symbol="XAUUSD.s",
        direction="BUY",
        layer_specs=[("L2", 3890.0, 0.01, 3905.0)],
        sl=3880.0,
        tick=SimpleNamespace(bid=3895.0, ask=3895.2, time=1700000000.0),
    )
    assert layers["L2"]["status"] == "PENDING"
    assert layers["L2"].get("filled_at") is None
    assert layers["L2"]["initial_sl"] == pytest.approx(3880.0)


def test_hydrate_signal_layers_stamps_first_fill_at(monkeypatch):
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {
            "L1": {"ticket": 101, "status": "PENDING", "lot": 0.01, "remaining_lot": 0.01},
        },
        "market_tickets": [],
        "pending_tickets": [101],
    }
    positions = {
        101: SimpleNamespace(
            ticket=101,
            type=0,
            price_open=3895.0,
            volume=0.01,
            sl=3880.0,
            tp=3905.0,
            profit=0.0,
        )
    }
    ex.hydrate_signal_layers_from_mt5(sig, live_positions_by_ticket=positions)
    assert sig["layers"]["L1"]["status"] == "FILLED"
    assert sig.get("first_fill_at"), "first_fill_at must be stamped on the first fill"
    assert sig["layers"]["L1"].get("filled_at"), "the layer must record its fill time"


def test_stamp_fill_times_never_overwrites_the_first_fill():
    sig = {"first_fill_at": "2026-11-19T06:00:00+00:00"}
    layer = {"filled_at": "2026-11-19T05:00:00+00:00"}
    changed = rl.stamp_fill_times(sig, layer, now=datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc))
    assert changed is False
    assert sig["first_fill_at"] == "2026-11-19T06:00:00+00:00"
    assert layer["filled_at"] == "2026-11-19T05:00:00+00:00"


def test_stamp_fill_times_sets_both_when_missing():
    sig = {}
    layer = {}
    now = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
    assert rl.stamp_fill_times(sig, layer, now=now) is True
    assert sig["first_fill_at"] == now.isoformat()
    assert layer["filled_at"] == now.isoformat()


def test_stamp_fill_times_reports_no_change_without_a_target():
    sig = {"first_fill_at": "2026-11-19T06:00:00+00:00"}
    now = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
    assert rl.stamp_fill_times(sig, None, now=now) is False
    assert rl.stamp_fill_times(None, None, now=now) is False
    assert sig["first_fill_at"] == "2026-11-19T06:00:00+00:00"


def test_resolve_fill_time_prefers_the_earliest_known_fill():
    sig = {
        # The signal stamp records the FIRST LIMIT fill, hours after L1 filled
        # at market. The trade age must run from the market fill.
        "first_fill_at": "2026-11-19T09:00:00+00:00",
        "layers": {
            "L1": {"filled_at": "2026-11-19T06:00:00+00:00"},
            "L2": {"filled_at": "2026-11-19T09:00:00+00:00"},
        },
    }
    assert rl.resolve_fill_time(sig) == datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)


def test_resolve_fill_time_ignores_layers_without_a_fill():
    sig = {"layers": {"L1": {"status": "PENDING"}, "L2": {"filled_at": "2026-11-19T09:00:00+00:00"}}}
    assert rl.resolve_fill_time(sig) == datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)


def test_resolve_fill_time_is_none_without_any_fill_stamp():
    assert rl.resolve_fill_time({}) is None
    assert rl.resolve_fill_time({"layers": {"L1": {"status": "PENDING"}}}) is None
    assert rl.resolve_fill_time({"first_fill_at": "not-a-timestamp"}) is None


def test_evaluate_trade_age_uses_the_fill_clock_not_the_signal_stamp():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        # The position filled 10 minutes ago; the signal is 9 hours old.
        "first_fill_at": (now - timedelta(minutes=10)).isoformat(),
        "layers": {"L1": {"filled_at": (now - timedelta(minutes=10)).isoformat()}},
        "market_tickets": [101],
        "pending_tickets": [],
    }
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["action"] == rl.ACTION_NONE
    # The fill clock is exactly 10 minutes old, NOT the 9-hour-old signal stamp.
    assert res["fill_age_seconds"] == 600


def test_evaluate_trade_age_still_flattens_an_old_fill():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        "first_fill_at": (now - timedelta(hours=7)).isoformat(),
        "layers": {"L1": {"filled_at": (now - timedelta(hours=7)).isoformat()}},
        "market_tickets": [101],
        "pending_tickets": [],
    }
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["action"] == rl.ACTION_FLATTEN


def test_evaluate_trade_age_falls_back_to_the_signal_timestamp_without_fill_stamps():
    """Regression guard: a signal with no fill stamp keeps the signal-timestamp age."""
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        "market_tickets": [101],
        "pending_tickets": [],
    }
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["action"] == rl.ACTION_FLATTEN
    assert res["fill_age_seconds"] == 9 * 3600


@pytest.mark.asyncio
async def test_live_reconcile_stamps_the_fill_time_on_the_filled_layer(monkeypatch):
    """The LIVE PENDING -> FILLED path (main._reconcile_live_mt5_signals) must stamp."""
    import copy
    from types import SimpleNamespace

    import main

    sig = {
        "message_id": 4242,
        "chat_id": -1001234567,
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "ACTIVE",
        "market_tickets": [],
        "pending_tickets": [7002],
        "layers": {
            "L2": {
                "lot": 0.01,
                "target_price": 3890.0,
                "tp": 3905.0,
                "ticket": 7002,
                "order_type": "BUY_LIMIT",
                "status": "PENDING",
            },
        },
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(),
    }

    class _FakeExecutor:
        dry_run = True

        def get_live_positions(self, symbol=None):
            return [
                SimpleNamespace(
                    ticket=7002,
                    symbol="XAUUSD.s",
                    type=0,
                    volume=0.01,
                    price_open=3890.0,
                    sl=3880.0,
                    tp=3905.0,
                    profit=0.0,
                )
            ]

        def get_live_orders(self, symbol=None):
            return []

        def get_symbol_info(self, symbol):
            return SimpleNamespace(digits=2)

        def is_ticket_in_reconcile_grace(self, ticket, grace_seconds=3.0):
            return False

        def assign_or_preserve_layer_roles(self, signal_data):
            return {}

    class _FakeState:
        def __init__(self, record):
            self.record = record

        def get_signal(self, message_id, chat_id=None):
            return copy.deepcopy(self.record)

        def update_signal(self, message_id, updates, chat_id=None):
            self.record.update(copy.deepcopy(updates))
            return copy.deepcopy(self.record)

    fake_state = _FakeState(sig)
    monkeypatch.setattr(main, "executor", _FakeExecutor())
    monkeypatch.setattr(main, "state_mgr", fake_state)

    await main._reconcile_live_mt5_signals([("4242", copy.deepcopy(sig))])

    stored = fake_state.record
    assert stored["layers"]["L2"]["status"] == "FILLED"
    assert stored["layers"]["L2"].get("filled_at"), "the live path must record the fill time"
    assert stored.get("first_fill_at"), "the live path must stamp the signal-level first fill"
    # The guard must read the fill clock, not the 9-hour-old signal timestamp.
    res = rl.evaluate_trade_age(stored, datetime.now(timezone.utc), _policy())
    assert res["fill_age_seconds"] < 600
    assert res["action"] == rl.ACTION_NONE


# --- fix round 1: BE fill-race promotion, broker stop capture, edit path ---


def test_apply_signal_breakeven_stamps_the_fill_time_when_it_promotes_a_pending_layer(monkeypatch):
    """A pending limit that filled before the 1s reconciler must still be stamped.

    `apply_signal_breakeven` promotes the layer to FILLED and drops the ticket
    from `pending_tickets`, so the reconciler's stamping loop never sees it
    again. Without a stamp here the promoted position has NO fill time at all:
    the trade-age guard falls back to the signal timestamp and treats a
    brand-new position as hours old.
    """
    from types import SimpleNamespace

    import MetaTrader5 as mt5
    import mt5_executor as ex_mod
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=False)
    ex._connected = True

    position = SimpleNamespace(
        ticket=7002,
        symbol="XAUUSD.s",
        type=0,
        volume=0.01,
        price_open=3890.0,
        sl=3880.0,
        tp=3905.0,
        profit=0.0,
    )
    monkeypatch.setattr(
        ex_mod.mt5,
        "positions_get",
        lambda *a, **k: [position],
    )
    monkeypatch.setattr(
        ex_mod.mt5,
        "symbol_info",
        lambda sym: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    monkeypatch.setattr(
        ex_mod.mt5,
        "terminal_info",
        lambda: SimpleNamespace(connected=True, trade_allowed=True),
    )
    monkeypatch.setattr(
        ex_mod.mt5,
        "order_send",
        lambda req: SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=7002, comment="Mock done"),
    )

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "ACTIVE",
        "market_tickets": [],
        "pending_tickets": [7002],
        "layers": {
            "L2": {
                "lot": 0.01,
                "target_price": 3890.0,
                "tp": 3905.0,
                "ticket": 7002,
                "order_type": "BUY_LIMIT",
                "status": "PENDING",
            },
        },
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(),
    }

    res = ex.apply_signal_breakeven(sig, be_layers=["L2"])

    assert 7002 in res["promoted_tickets"], "the pending ticket must be promoted"
    assert sig["layers"]["L2"]["status"] == "FILLED"
    assert sig["layers"]["L2"].get("filled_at"), "the promoted layer must record its fill time"
    assert sig.get("first_fill_at"), "the promoted layer must stamp the signal-level first fill"
    # The age guard must read the fill clock, not the 9-hour-old signal timestamp.
    age = rl.evaluate_trade_age(sig, datetime.now(timezone.utc), _policy())
    assert age["fill_age_seconds"] < 600
    assert age["action"] == rl.ACTION_NONE


def test_evaluate_trade_age_uses_the_earliest_fill_when_the_signal_stamp_is_later():
    """The MARKET fill (L1) is OLDER than the LIMIT fill the signal stamped.

    The trade-age guard must measure from the MARKET fill, so the position is
    already past the de-risk threshold and must be de-risked. The later signal
    stamp must not hide the age.
    """
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        # The signal-level stamp records the FIRST LIMIT fill (L2), 5 minutes ago...
        "first_fill_at": (now - timedelta(minutes=5)).isoformat(),
        # ...but L1 filled at MARKET three hours ago.
        "layers": {
            "L1": {"filled_at": (now - timedelta(hours=3)).isoformat()},
            "L2": {"filled_at": (now - timedelta(minutes=5)).isoformat()},
        },
        "market_tickets": [101],
        "pending_tickets": [],
    }
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["action"] == rl.ACTION_DERISK, "the earlier MARKET fill must win"
    assert res["fill_age_seconds"] == 3 * 3600


def test_evaluate_trade_age_reports_none_when_the_market_fill_is_young_and_the_signal_is_old():
    """The reverse direction: the SIGNAL is old, but the position only filled now.

    The guard measures the position, not the signal: a position that opened ten
    minutes ago must not be de-risked because the signal was posted hours ago.
    """
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        "first_fill_at": (now - timedelta(minutes=10)).isoformat(),
        "layers": {
            "L1": {"filled_at": (now - timedelta(minutes=10)).isoformat()},
            "L2": {"status": "PENDING"},
        },
        "market_tickets": [101],
        "pending_tickets": [201],
    }
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["action"] == rl.ACTION_NONE
    assert res["fill_age_seconds"] == 600


def test_evaluate_trade_age_keeps_the_old_signal_stamp_when_the_market_fill_is_young():
    """An OLD signal-level stamp is never ignored: the EARLIEST fill wins.

    This is the mirror of the mixed case above: here the signal-level stamp is
    the older fill, so the guard must act on it even though a layer carries a
    younger fill.
    """
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        "first_fill_at": (now - timedelta(hours=5)).isoformat(),
        "layers": {"L1": {"filled_at": (now - timedelta(minutes=10)).isoformat()}},
        "market_tickets": [101],
        "pending_tickets": [],
    }
    res = rl.evaluate_trade_age(sig, now, _policy())
    assert res["action"] == rl.ACTION_DERISK
    assert res["fill_age_seconds"] == 5 * 3600


def test_execute_4layer_specs_records_the_stop_the_broker_actually_got(monkeypatch):
    """`sl=None` means the executor computes a fallback stop for the broker.

    `initial_sl` must capture THAT stop, not the `None` argument, otherwise the
    R-relative floor has no initial risk R to work with and is silently skipped.
    """
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    sent = []
    real_send = ex.send_order_with_filling_fallback

    def _capture(request):
        sent.append(dict(request))
        return real_send(request)

    monkeypatch.setattr(ex, "send_order_with_filling_fallback", _capture)

    market, pending, layers = ex._execute_4layer_specs(
        symbol="XAUUSD.s",
        direction="BUY",
        layer_specs=[("L1", 3895.0, 0.01, 3905.0)],
        sl=None,
        tick=SimpleNamespace(bid=3894.8, ask=3895.0, time=1700000000.0),
    )

    assert len(sent) == 1, "the L1 layer must place exactly one order"
    broker_sl = sent[0]["sl"]
    assert broker_sl and broker_sl > 0.0, "the broker must have received a real stop"
    assert layers["L1"]["initial_sl"] is not None, "initial_sl must capture the broker stop"
    assert layers["L1"]["initial_sl"] == pytest.approx(broker_sl)
    assert ex.get_last_placed_sl(layers["L1"]["ticket"]) == pytest.approx(broker_sl)


def test_execute_4layer_specs_records_the_stop_of_a_pending_layer(monkeypatch):
    """The pending path records the same broker stop as the market path."""
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    sent = []
    real_send = ex.send_order_with_filling_fallback

    def _capture(request):
        sent.append(dict(request))
        return real_send(request)

    monkeypatch.setattr(ex, "send_order_with_filling_fallback", _capture)

    market, pending, layers = ex._execute_4layer_specs(
        symbol="XAUUSD.s",
        direction="BUY",
        layer_specs=[("L2", 3850.0, 0.01, 3905.0)],
        sl=None,
        tick=SimpleNamespace(bid=3894.8, ask=3895.0, time=1700000000.0),
    )

    assert len(sent) == 1
    broker_sl = sent[0]["sl"]
    assert broker_sl and broker_sl > 0.0
    assert layers["L2"]["initial_sl"] == pytest.approx(broker_sl)
    assert layers["L2"].get("filled_at") is None


def test_execute_4layer_specs_rounds_the_captured_stop_to_the_symbol_digits(monkeypatch):
    """The captured stop must use the broker's `digits`, not a hardcoded 2."""
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    sym_info = SimpleNamespace(
        digits=3,
        point=0.001,
        spread=15,
        trade_mode=4,
        filling_mode=2,
        visible=True,
        trade_stops_level=50,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )
    monkeypatch.setattr(ex, "get_symbol_info", lambda symbol: sym_info)

    market, pending, layers = ex._execute_4layer_specs(
        symbol="XAUUSD.s",
        direction="BUY",
        layer_specs=[("L1", 3895.0, 0.01, 3905.0)],
        sl=3880.126,
        tick=SimpleNamespace(bid=3894.8, ask=3895.0, time=1700000000.0),
    )

    assert layers["L1"]["initial_sl"] == pytest.approx(3880.126)


@pytest.mark.asyncio
async def test_edit_path_records_the_placed_stop_and_the_fill_time_on_the_new_layers(monkeypatch, tmp_path):
    """The signal-edit path builds its own layer records.

    A layer created by an edit must carry the stop it was placed with and, for a
    MARKET layer, its fill time -- otherwise the R floor has no R and the
    trade-age guard falls back to the (possibly hours-old) signal timestamp.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import main
    from mt5_executor import MT5Executor
    from state_manager import StateManager
    from trade_ledger import TradeLedger

    class _FakeMsg:
        def __init__(self, msg_id, text, date=None, edit_date=None):
            self.id = msg_id
            self.message = text
            self.raw_text = text
            self.reply_to_msg_id = None
            self.reply_to = None
            self.forward = None
            self.fwd_from = None
            self.out = False
            self.date = date or datetime.now(timezone.utc)
            self.edit_date = edit_date

    class _FakeEvent:
        def __init__(self, msg_id, text, chat_id=-1004369388188, date=None, edit_date=None):
            self.message = _FakeMsg(msg_id, text, date=date, edit_date=edit_date)
            self.chat_id = chat_id
            self.is_private = False
            self.is_reply = False
            self.replies = []

        async def reply(self, txt):
            self.replies.append(txt)

    executor = MT5Executor(dry_run=True)
    monkeypatch.setattr(main, "executor", executor)
    monkeypatch.setattr(main, "state_mgr", StateManager(str(tmp_path / "edit_state.json")))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(str(tmp_path / "edit_ledger.jsonl")))
    executor.connect()

    chat_id = -1004369388188
    msg_id = 9755
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    tick = SimpleNamespace(bid=3856.00, ask=3856.20, time=1700000000)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), patch.object(
        executor, "get_tick", return_value=tick
    ):
        # A SELL limit above the market stays pending.
        await main.handle_new_message(
            _FakeEvent(msg_id, "SELL XAUUSD 3860\nTP 3800\nSL 3870", chat_id=chat_id, date=t0)
        )
        # The edit re-tiers L1 into the zone. With the market at 3856 the new L1
        # tier (3856) executes at MARKET and L2-L4 are placed as limits.
        await main.handle_message_edited(
            _FakeEvent(
                msg_id,
                "SELL XAUUSD 3856-3860\nTP 3800\nSL 3870",
                chat_id=chat_id,
                date=t0,
                edit_date=datetime.now(timezone.utc),
            )
        )

    sig = main.state_mgr.get_signal(msg_id, chat_id=chat_id)
    assert sig is not None
    l1 = sig["layers"]["L1"]
    assert l1["order_type"] == "MARKET", "the re-tiered L1 must execute at market"
    assert l1["initial_sl"] == pytest.approx(3870.0), "the layer must carry the stop it was placed with"
    assert l1.get("filled_at"), "a MARKET layer created by an edit must record its fill time"
    assert sig.get("first_fill_at"), "the edit must stamp the signal-level first fill"

    # The fresh LIMIT layers must carry the stop too -- and no fill time.
    for name in ("L2", "L3", "L4"):
        assert sig["layers"][name]["initial_sl"] == pytest.approx(3870.0)
        assert sig["layers"][name].get("filled_at") is None


# --- signal-edit path: fill stamps on promoted / freshly placed layers -------


class _EditFakeMsg:
    """Minimal stand-in for a Telethon message used by the edit-path tests."""

    def __init__(self, msg_id, text, date=None, edit_date=None):
        self.id = msg_id
        self.message = text
        self.raw_text = text
        self.reply_to_msg_id = None
        self.reply_to = None
        self.forward = None
        self.fwd_from = None
        self.out = False
        self.date = date or datetime.now(timezone.utc)
        self.edit_date = edit_date


class _EditFakeEvent:
    """Minimal stand-in for a Telethon event used by the edit-path tests."""

    def __init__(self, msg_id, text, chat_id=-1004369388188, date=None, edit_date=None):
        self.message = _EditFakeMsg(msg_id, text, date=date, edit_date=edit_date)
        self.chat_id = chat_id
        self.is_private = False
        self.is_reply = False
        self.replies = []

    async def reply(self, txt):
        self.replies.append(txt)


class _LiveExecutorView:
    """Report `dry_run=False` to main.py while the wrapped executor stays in dry-run mode.

    The pre-edit reconciliation only queries the broker when the executor is
    live. This view lets the test drive that branch without a real MT5
    terminal, while every order operation still goes through the dry-run
    executor.
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    @property
    def dry_run(self):
        return False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


async def _drive_signal_edit(
    monkeypatch,
    tmp_path,
    msg_id,
    initial_text,
    edit_text,
    configure=None,
    live_view=False,
):
    """Run `handle_new_message` then `handle_message_edited` with MT5 mocked.

    Returns `(signal_before_edit, signal_after_edit)` as persisted by the
    state manager, so the assertions read the stored record and not the
    broker calls.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import config
    import main
    import mt5_executor
    from mt5_executor import MT5Executor
    from state_manager import StateManager
    from trade_ledger import TradeLedger

    # Other test modules set this flag at import time and never restore it, so
    # the number of layers a single-price signal creates is not deterministic
    # across the whole suite. Pin it to the shipped default (one consolidated
    # order) so the scenario below always has the same shape.
    monkeypatch.setattr(config, "SINGLE_PRICE_CONSOLIDATE_ORDERS", True)
    monkeypatch.setattr(mt5_executor, "SINGLE_PRICE_CONSOLIDATE_ORDERS", True)

    executor = MT5Executor(dry_run=True)
    monkeypatch.setattr(main, "executor", executor)
    monkeypatch.setattr(main, "state_mgr", StateManager(str(tmp_path / "edit_state.json")))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(str(tmp_path / "edit_ledger.jsonl")))
    executor.connect()

    chat_id = -1004369388188
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    tick = SimpleNamespace(bid=3856.00, ask=3856.20, time=1700000000)

    ctxs = [
        patch.object(main, "notify_saved_messages", new_callable=AsyncMock),
        patch.object(executor, "get_tick", return_value=tick),
    ]
    extra = []
    for ctx in ctxs:
        ctx.start()
    try:
        await main.handle_new_message(_EditFakeEvent(msg_id, initial_text, chat_id=chat_id, date=t0))
        before = main.state_mgr.get_signal(msg_id, chat_id=chat_id) or {}

        if configure is not None:
            extra = configure(executor, before) or []
            for ctx in extra:
                ctx.start()
        if live_view:
            monkeypatch.setattr(main, "executor", _LiveExecutorView(executor))

        await main.handle_message_edited(
            _EditFakeEvent(
                msg_id,
                edit_text,
                chat_id=chat_id,
                date=t0,
                edit_date=datetime.now(timezone.utc),
            )
        )
        after = main.state_mgr.get_signal(msg_id, chat_id=chat_id) or {}
        return before, after
    finally:
        for ctx in reversed(extra + ctxs):
            ctx.stop()


def _configure_pending_order_already_filled(executor, before):
    """The broker reports the pending layer as an open position."""
    from types import SimpleNamespace
    from unittest.mock import patch

    ticket = (before.get("pending_tickets") or [None])[0]
    position = SimpleNamespace(ticket=ticket, price_open=3856.20, volume=0.01, sl=3870.0)
    return [
        patch.object(executor, "get_live_positions", return_value=[position]),
        patch.object(executor, "get_live_orders", return_value=[]),
    ]


def _configure_lost_cancel_race(executor, before):
    """Cancelling the pending order fails because it already filled."""
    from unittest.mock import patch

    return [
        patch.object(executor, "cancel_pending_order", return_value=False),
        patch.object(executor, "modify_position_sltp", return_value=True),
    ]


def _configure_pending_modify_reports_the_fill(executor, before):
    """The pending-order modify reports the order is gone; the position modify wins."""
    from unittest.mock import patch

    return [
        patch.object(executor, "modify_pending_order_sltp", return_value=False),
        patch.object(executor, "modify_position_sltp", return_value=True),
    ]


def _configure_retier_placement_rejected(executor, before):
    """The re-tier's replacement order is rejected, so the layer is placed fresh."""
    from unittest.mock import patch

    real_execute = executor._execute_layer_price
    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (None, "FAILED")
        return real_execute(*args, **kwargs)

    return [patch.object(executor, "_execute_layer_price", side_effect=side_effect)]


@pytest.mark.asyncio
async def test_edit_path_stamps_the_fill_time_when_a_pending_order_is_found_already_filled(monkeypatch, tmp_path):
    """A pending layer that already filled must carry its fill time.

    The pre-edit reconciliation promotes the layer to MARKET/OPEN and drops
    the ticket from `pending_tickets`, so the 1-second reconciler never sees
    that ticket again and can never stamp its fill time.
    """
    before, after = await _drive_signal_edit(
        monkeypatch,
        tmp_path,
        msg_id=98601,
        initial_text="SELL XAUUSD 3860\nTP 3800\nSL 3870",
        edit_text="SELL XAUUSD 3860\nTP 3800\nSL 3871",
        configure=_configure_pending_order_already_filled,
        live_view=True,
    )

    assert before.get("pending_tickets"), "the signal must start with a pending order"
    promoted_ticket = before["pending_tickets"][0]
    l1 = after["layers"]["L1"]
    assert l1["ticket"] == promoted_ticket, "the promoted layer keeps its original ticket"
    assert l1["order_type"] == "MARKET", "the filled pending order must be promoted to MARKET"
    assert l1["status"] == "OPEN", "the filled pending order must be promoted to OPEN"
    assert l1.get("filled_at"), "the promoted layer must record its fill time"
    assert after.get("first_fill_at"), "the promoted layer must stamp the signal-level first fill"
    assert promoted_ticket not in (after.get("pending_tickets") or []), "the promoted ticket leaves pending_tickets"


@pytest.mark.asyncio
async def test_edit_path_stamps_the_fill_time_when_the_retier_loses_the_cancel_race(monkeypatch, tmp_path):
    """The re-tier cannot cancel the pending order because it already filled.

    The layer is promoted in place, so its fill time has to be stamped on the
    spot -- the ticket leaves `pending_tickets` and the reconciler will never
    see it again.
    """
    before, after = await _drive_signal_edit(
        monkeypatch,
        tmp_path,
        msg_id=98602,
        initial_text="SELL XAUUSD 3860\nTP 3800\nSL 3870",
        edit_text="SELL XAUUSD 3856-3860\nTP 3800\nSL 3870",
        configure=_configure_lost_cancel_race,
    )

    original_ticket = before["layers"]["L1"]["ticket"]
    l1 = after["layers"]["L1"]
    assert l1["ticket"] == original_ticket, "the promoted layer keeps its original ticket"
    assert l1["order_type"] == "MARKET", "the filled layer must be promoted to MARKET"
    assert l1["status"] == "OPEN", "the filled layer must be promoted to OPEN"
    assert l1.get("filled_at"), "the promoted layer must record its fill time"
    assert after.get("first_fill_at"), "the promoted layer must stamp the signal-level first fill"
    assert original_ticket not in (after.get("pending_tickets") or []), "the filled ticket leaves pending_tickets"


@pytest.mark.asyncio
async def test_edit_path_stamps_the_fill_time_when_a_pending_modify_reports_the_fill(monkeypatch, tmp_path):
    """A pending layer whose SL/TP modify finds it already filled must be stamped.

    `modify_pending_order_sltp` fails because the order is gone; the position
    modify succeeds, so the layer is promoted to MARKET/OPEN and leaves
    `pending_tickets` -- the reconciler will never stamp it.
    """
    before, after = await _drive_signal_edit(
        monkeypatch,
        tmp_path,
        msg_id=98603,
        initial_text="SELL XAUUSD 3856-3860\nTP 3800\nSL 3870",
        edit_text="SELL XAUUSD 3856-3860\nTP 3800\nSL 3871",
        configure=_configure_pending_modify_reports_the_fill,
    )

    assert before["layers"]["L2"]["status"] == "PENDING", "L2 must start out pending"
    l2 = after["layers"]["L2"]
    assert l2["order_type"] == "MARKET", "the filled layer must be promoted to MARKET"
    assert l2["status"] == "OPEN", "the filled layer must be promoted to OPEN"
    assert l2.get("filled_at"), "the promoted layer must record its fill time"
    assert after.get("first_fill_at"), "the promoted layer must stamp the signal-level first fill"
    assert l2["ticket"] not in (after.get("pending_tickets") or []), "the filled ticket leaves pending_tickets"


@pytest.mark.asyncio
async def test_edit_path_stamps_the_fill_time_on_a_fresh_market_layer(monkeypatch, tmp_path):
    """A layer the edit places fresh at MARKET must record its fill time.

    The re-tier's replacement order is rejected, so the layer is placed fresh
    by the edit (the `new_l_rec_a` branch). A MARKET layer fills at placement
    and never goes through the PENDING -> FILLED transition, so its fill time
    has to be stamped where it is created.
    """
    before, after = await _drive_signal_edit(
        monkeypatch,
        tmp_path,
        msg_id=98604,
        initial_text="BUY XAUUSD 3850\nTP 3900\nSL 3840",
        edit_text="BUY XAUUSD 3850-3861\nTP 3900\nSL 3840",
        configure=_configure_retier_placement_rejected,
    )

    assert before["layers"]["L1"]["status"] == "PENDING", "L1 must start out pending"
    l1 = after["layers"]["L1"]
    assert l1["order_type"] == "MARKET", "the freshly placed layer must execute at market"
    assert l1["status"] == "OPEN", "the freshly placed layer must be OPEN"
    assert l1.get("filled_at"), "a MARKET layer placed fresh by the edit must record its fill time"
    assert after.get("first_fill_at"), "the fresh MARKET layer must stamp the signal-level first fill"


def test_resolve_edit_initial_sl_treats_zero_as_no_stop(monkeypatch):
    """A stop of 0.0 means "no stop given", not a stop at zero.

    The executor treats `sl == 0.0` as "no stop" and computes a fallback
    stop instead, so recording 0.0 as the layer's initial stop would be a
    bogus R.
    """
    import main
    from mt5_executor import MT5Executor

    monkeypatch.setattr(main, "executor", MT5Executor(dry_run=True))

    assert main._resolve_edit_initial_sl(4242, 0.0, 2) is None, "0.0 is not a stop"
    assert main._resolve_edit_initial_sl(4242, None, 2) is None, "no stop stays no stop"
    assert main._resolve_edit_initial_sl(4242, 3870.0, 2) == pytest.approx(3870.0), "a real stop is kept"
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `KeyError: 'initial_sl'` and `first_fill_at` is `None`. The
fix-round-1 tests fail as well: the BE fill-race promotion leaves no `filled_at`,
`initial_sl` is `None` when the executor computed a fallback stop, the symbol digits are
ignored, and the signal-edit path records neither `initial_sl` nor `filled_at`.

- [ ] **Step 3: Capture `initial_sl` at placement**

In `mt5_executor.py`, inside `_execute_4layer_specs`, the broker's price precision is
needed first — the captured stop is rounded to the SYMBOL's digits, exactly like the stop
that is sent to the broker. Add at lines 2345-2348 (right after `layers_data` is created):

```python
        # The broker's price precision for this symbol: the captured stop is
        # rounded to it, exactly like the stop that is sent to the broker.
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
```

Then extend the `layer_dict` construction at lines 2383-2412 to:

```python
                # The stop the broker actually holds for this ticket. The
                # `sl` argument is only a REQUEST: when it is None (or 0.0)
                # the executor computes an ATR-based or FALLBACK_SL_PIPS stop
                # and sends THAT to the broker, so the recorded broker stop is
                # the authoritative initial risk R.
                placed_sl = self.get_last_placed_sl(t)
                if placed_sl is not None:
                    initial_sl = round(float(placed_sl), digits)
                elif sl is not None:
                    initial_sl = round(float(sl), digits)
                else:
                    initial_sl = None
                layer_dict: Dict[str, Any] = {
                    "lot": l_lot,
                    "remaining_lot": l_lot,
                    "target_price": target_px,
                    "tp": l_tp,
                    "tp_stage": "TP1",
                    "ticket": t,
                    "order_type": o_type,
                    "status": status,
                    # The stop the order was ACTUALLY placed with. The `sl`
                    # argument is only a request: when it is None the executor
                    # computes a fallback stop and the broker holds that one, so
                    # the recorded broker stop wins. The trailing engine
                    # overwrites `sl`/`broker_sl`, so the initial risk R must be
                    # captured here to stay available for the R-relative profit
                    # floor.
                    "initial_sl": initial_sl,
                }
                if o_type == "MARKET":
                    # A MARKET layer fills immediately at placement, so this is the
                    # fill time. MARKET layers never go through the PENDING ->
                    # FILLED transition, so without this stamp the first fill of a
                    # normal zone signal (L1 at market, L2-L4 as limits) would
                    # never be recorded.
                    layer_dict["filled_at"] = datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Record the stop that was actually sent to the broker**

`initial_sl` must be the stop the BROKER holds, not the `sl` argument: when `sl` is
`None` or `0.0`, `_compute_fallback_sl` computes an ATR-based or
`FALLBACK_SL_PIPS`-based stop, and that is the stop the broker gets. Mirror the
`_last_fill_prices` idiom with a side-channel.

In `mt5_executor.py`, in `MT5Executor.__init__`, add at lines 542-546:

```python
        # Stop actually sent to the broker per ticket. The `sl` argument of an
        # order is only a REQUEST: when it is None the executor computes a
        # fallback stop and the broker holds that one, so the initial risk R
        # has to be captured from here.
        self._last_order_sls: Dict[int, float] = {}
```

In `execute_market_order`, record the stop right after the fill price, at lines 1575-1580:

```python
            # Record the stop that was actually sent to the broker (the `sl`
            # argument is only a request -- the broker may hold a computed
            # fallback stop instead).
            self._last_order_sls[ticket] = float(final_sl)
            if len(self._last_order_sls) > 512:
                self._last_order_sls = dict(list(self._last_order_sls.items())[-256:])
```

In `_place_pending_order`, do the same for the pending path, at lines 1790-1794:

```python
            # Record the stop that was actually sent to the broker (see
            # `execute_market_order`): the `sl` argument is only a request.
            self._last_order_sls[ticket] = float(final_sl)
            if len(self._last_order_sls) > 512:
                self._last_order_sls = dict(list(self._last_order_sls.items())[-256:])
```

`_place_pending_with_stops_fallback` needs no change: its LIMIT path goes through
`_place_pending_order` and its MARKET fallback goes through `execute_market_order`, so
both record the stop.

Add the accessor next to the other accessors, at lines 844-853:

```python
    def get_last_placed_sl(self, ticket: Optional[int]) -> Optional[float]:
        """Return the stop that was actually sent to the broker for a ticket, if known.

        This is pure bookkeeping: it RECORDS the stop an order was already
        carrying, so the caller can capture the real initial risk R.
        """
        try:
            return self._last_order_sls.get(int(ticket))
        except (TypeError, ValueError):
            return None
```

This is PURE bookkeeping: the `request` dict, `final_sl` and everything that is sent to
the broker stay exactly as they were.

- [ ] **Step 5: Stamp `first_fill_at` on the first fill**

In `mt5_executor.py`, inside `hydrate_signal_layers_from_mt5`, replace the fill transition at lines 6138-6140:

```python
                if st_u == "PENDING":
                    lv["status"] = "FILLED"
                    changed = True
```

with:

```python
                if st_u == "PENDING":
                    lv["status"] = "FILLED"
                    changed = True
                    # First fill of this signal: start the position-age clock used
                    # by the trade-age guard (`risk_lifecycle.evaluate_trade_age`).
                    if stamp_fill_times(signal_data, lv):
                        changed = True
```

`mt5_executor.py` already imports `datetime` and `timezone` (`from datetime import datetime,
timedelta, timezone`). Add the `risk_lifecycle` import next to the other local imports:

```python
from risk_lifecycle import stamp_fill_times
```

- [ ] **Step 6: Stamp the LIVE fill path in `main.py`**

`hydrate_signal_layers_from_mt5` is called from exactly ONE place — `main.py:3421`, inside
`_startup_hydrate_and_backfill_ledger()` — so it only runs at STARTUP. The live
`PENDING -> FILLED` transition happens in `main.py:3554`, inside `_reconcile_live_mt5_signals`
(`async def` at `main.py:3450`), which sets `l_data["status"] = "FILLED"` directly and never
calls `hydrate_signal_layers_from_mt5`.

In `main.py`, inside `_reconcile_live_mt5_signals`, replace the fill transition:

```python
                        if isinstance(l_data, dict) and _safe_ticket_int(l_data.get("ticket")) == ti:
                            l_data["status"] = "FILLED"
```

with:

```python
                        if isinstance(l_data, dict) and _safe_ticket_int(l_data.get("ticket")) == ti:
                            l_data["status"] = "FILLED"
                            # First fill of this signal: start the position-age clock
                            # used by the trade-age guard. Without this stamp a limit
                            # order that filled hours after the signal was created
                            # falls back to the signal timestamp, which makes a
                            # brand-new position look hours old.
                            if stamp_fill_times(sig, l_data):
                                updated = True
```

Add the import to `main.py` next to the other local imports:

```python
from risk_lifecycle import stamp_fill_times
```

The reconciler persists a field-wise `updates` dict (`state_mgr.update_signal(msg_id, updates, ...)`),
not the whole signal record, so the signal-level stamp has to be copied into it explicitly:

```python
            if sig.get("first_fill_at"):
                updates["first_fill_at"] = sig["first_fill_at"]
```

- [ ] **Step 7: Stamp the BE fill-race promotion in `mt5_executor.py`**

`apply_signal_breakeven` promotes a pending limit that filled in the sub-second window
BEFORE the 1-second reconciler ran, and then drops the ticket from `pending_tickets`.
The reconciler's stamping loop lives inside `for t in pd_tickets:`, so a ticket removed
here is NEVER revisited and never stamped: the promoted position would have no fill time
at all, and the age guard would fall back to the signal timestamp.

In `mt5_executor.py`, inside `apply_signal_breakeven`, extend the promotion branch at
lines 4968-4975 to:

```python
                if layer_name and isinstance(layers.get(layer_name), dict):
                    layers[layer_name]["status"] = "FILLED"
                    # This ticket is about to leave `pending_tickets`, so the
                    # 1-second reconciler will never see it again. Stamp the fill
                    # HERE, otherwise the promoted position has no fill time at
                    # all and the trade-age guard falls back to the signal
                    # timestamp -- making a brand-new position look hours old.
                    stamp_fill_times(signal_data, layers[layer_name])
                    if p_open > 0.0:
                        layers[layer_name]["entry_price"] = round(p_open, digits)
                        layers[layer_name]["entry"] = round(p_open, digits)
                    if p_vol > 0.0:
                        layers[layer_name]["remaining_lot"] = p_vol
```

- [ ] **Step 8: Give the signal-edit path its `initial_sl` and `filled_at`**

The signal-edit path builds its own layer records, so it needs the same treatment as
`_execute_4layer_specs`: the stop the order was actually placed with, and — for a MARKET
layer — the fill time.

In `main.py`, add the helper at lines 5787-5807 (next to `_populate_fill_entry_from_exec`):

```python
def _resolve_edit_initial_sl(ticket: Any, sl: Optional[float], digits: int) -> Optional[float]:
    """Return the stop an edit-placed layer was actually given, for `initial_sl`.

    The executor records the stop it sent to the broker per ticket; that is the
    authoritative value. The `sl` argument is only the fallback for orders the
    executor did not place (or when the record is unavailable).
    """
    getter = getattr(executor, "get_last_placed_sl", None)
    if callable(getter):
        placed = getter(ticket)
        if placed is not None:
            try:
                return round(float(placed), digits)
            except (TypeError, ValueError):
                pass
    if sl is not None:
        try:
            value = float(sl)
        except (TypeError, ValueError):
            return None
        # The executor treats `sl == 0.0` as "no stop given" and computes a
        # fallback stop instead, so 0.0 is not a stop and must not be recorded
        # as the layer's initial risk.
        if value == 0.0:
            return None
        return round(value, digits)
    return None
```

In `main.py`, inside `_handle_message_edited_unlocked`, resolve the symbol digits at
lines 5992-5996:

```python
    pip_size = await _mt5_call(executor.get_pip_size, symbol)
    # The broker's price precision for this symbol: the captured stop is rounded
    # to it, exactly like the stop that is sent to the broker.
    _sym_info = await _mt5_call(executor.get_symbol_info, symbol)
    symbol_digits = int(getattr(_sym_info, "digits", 2) or 2) if _sym_info else 2
```

In the retiered-order branch, extend the record at lines 6620-6637:

```python
                    new_l_rec = {
                        "lot": l_lot,
                        "target_price": desired_px,
                        "tp": l_tp_target,
                        "tp_stage": "TP1",
                        "ticket": new_t,
                        "order_type": new_otype,
                        "status": l_status,
                        # The stop the order was actually placed with: the R
                        # floor needs the original risk, and the trailing
                        # engine later overwrites `sl`/`broker_sl`.
                        "initial_sl": _resolve_edit_initial_sl(new_t, new_sl, symbol_digits),
                    }
                    if new_otype == "MARKET":
                        _populate_fill_entry_from_exec(existing_signal, new_l_rec, new_t, l_lot, tick, direction, desired_px)
                        # A MARKET layer fills at placement and never goes through
                        # the PENDING -> FILLED transition, so its fill time has to
                        # be stamped here.
                        stamp_fill_times(existing_signal, new_l_rec)
                    layers[l_name] = new_l_rec
```

In the fresh-limit branch, do the same at lines 6855-6873:

```python
                    new_l_rec_a = {
                        "lot": l_lot,
                        "target_price": target_px,
                        "tp": l_tp,
                        "tp_stage": "TP1",
                        "ticket": t,
                        "order_type": o_type,
                        "status": "OPEN" if o_type == "MARKET" else "PENDING",
                        # The stop the order was actually placed with: the R
                        # floor needs the original risk, and the trailing
                        # engine later overwrites `sl`/`broker_sl`.
                        "initial_sl": _resolve_edit_initial_sl(t, new_sl, symbol_digits),
                    }
                    if o_type == "MARKET":
                        market_tickets.append(t)
                        _populate_fill_entry_from_exec(existing_signal, new_l_rec_a, t, l_lot, tick, direction, target_px)
                        # A MARKET layer fills at placement and never goes through
                        # the PENDING -> FILLED transition, so its fill time has to
                        # be stamped here.
                        stamp_fill_times(existing_signal, new_l_rec_a)
                    else:
```

THREE MORE branches of the same edit path promote a pending layer that has ALREADY
FILLED on the broker: the pre-edit reconciliation, the re-tier cancel race, and the
pending-order SL/TP modify race. Each one flips the layer to `order_type="MARKET"`,
`status="OPEN"` and REMOVES the ticket from `pending_tickets` — so the 1-second
reconciler, whose stamping loop lives inside `for t in pd_tickets:`, never sees that
ticket again and never stamps it. Each branch has to stamp the fill itself.

In the pre-edit reconciliation, extend the promotion branch at lines 6294-6297:

```python
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and l_data.get("ticket") == pt:
                            l_data["status"] = "OPEN"
                            l_data["order_type"] = "MARKET"
                            # The ticket is leaving `pending_tickets`, so the 1-second
                            # reconciler will never see it again and can never stamp its
                            # fill time -- stamp it here.
                            stamp_fill_times(existing_signal, l_data)
                            if p_open_ed > 0.0:
```

In the re-tier cancel race (the cancel fails because the order already filled), extend
the promotion branch at lines 6663-6666:

```python
                    l_data["order_type"] = "MARKET"
                    l_data["status"] = "OPEN"
                    _populate_fill_entry_from_exec(existing_signal, l_data, t, l_lot, tick, direction, desired_px)
                    # The ticket is leaving `pending_tickets`, so the 1-second
                    # reconciler will never see it again and can never stamp its
                    # fill time -- stamp it here.
                    stamp_fill_times(existing_signal, l_data)
```

In the pending-order SL/TP modify race (`modify_pending_order_sltp` reports the order is
gone and the position modify succeeds), extend the promotion branch at lines 6718-6721:

```python
                        l_data["order_type"] = "MARKET"
                        l_data["status"] = "OPEN"
                        _populate_fill_entry_from_exec(existing_signal, l_data, t, l_data.get("lot"), tick, direction, l_data.get("target_price"))
                        # The ticket is leaving `pending_tickets`, so the 1-second
                        # reconciler will never see it again and can never stamp its
                        # fill time -- stamp it here.
                        stamp_fill_times(existing_signal, l_data)
```

The reconciler persists a field-wise `updates` dict, so the signal-level stamp the edit
creates has to be copied into it explicitly — at lines 6917-6920:

```python
    if existing_signal.get("first_fill_at"):
        # A MARKET layer created by the edit stamps the signal-level first fill;
        # persist it so the trade-age guard has a fill clock to read.
        signal_updates["first_fill_at"] = existing_signal["first_fill_at"]
```

- [ ] **Step 9: Add the fill-time helpers to `risk_lifecycle.py`**

In `risk_lifecycle.py`, add:

```python
def stamp_fill_times(
    signal_data: Optional[Dict[str, Any]],
    layer_data: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Record a fill timestamp on a layer and on its signal.

    The layer always gets ``filled_at``. The signal gets ``first_fill_at`` only when it is
    unset, so the first fill of the trade wins. Returns True when something changed.
    """
    changed = False
    ts = (now or datetime.now(timezone.utc)).isoformat()
    if isinstance(layer_data, dict) and not layer_data.get("filled_at"):
        layer_data["filled_at"] = ts
        changed = True
    if isinstance(signal_data, dict) and not signal_data.get("first_fill_at"):
        signal_data["first_fill_at"] = ts
        changed = True
    return changed


def resolve_fill_time(signal_data: Dict[str, Any]) -> Optional[datetime]:
    """Return the EARLIEST known fill time for a signal, or None.

    A zone signal normally fills L1 at market and L2-L4 later as limits, so the
    trade age must be measured from the FIRST fill -- never from the signal
    timestamp, and never from the latest fill.
    """
    sig = signal_data if isinstance(signal_data, dict) else {}
    best = _parse_iso_utc(sig.get("first_fill_at"))
    layers = sig.get("layers")
    if isinstance(layers, dict):
        for lv in layers.values():
            if not isinstance(lv, dict):
                continue
            ldt = _parse_iso_utc(lv.get("filled_at"))
            if ldt is not None and (best is None or ldt < best):
                best = ldt
    return best
```

Then change `evaluate_trade_age` to use it. Replace:

```python
    # Fall back to the signal timestamp when the fill time is unknown: the position
    # is at least as old as the signal, so the age guard must not stand down.
    fill_dt = _parse_iso_utc(sig.get("first_fill_at")) or signal_dt
```

with:

```python
    # The fill clock is the EARLIEST known fill (the signal stamp or any layer
    # stamp). Fall back to the signal timestamp when no fill time is known: the
    # position is at least as old as the signal, so the age guard must not stand down.
    fill_dt = resolve_fill_time(sig) or signal_dt
```

Export the two new helpers from the module's `__all__`:

```python
    "parse_r_lock_tiers",
    "read_lifecycle_policy",
    "resolve_fill_time",
    "stamp_fill_times",
]
```

- [ ] **Step 10: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 11: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 12: Commit**

```powershell
git add mt5_executor.py main.py risk_lifecycle.py tests/test_trade_lifecycle.py
git commit -m "feat(mt5_executor): capture initial_sl and stamp fill times on every fill path

initial_sl records the stop the order was ACTUALLY placed with -- the
executor may compute a fallback stop when the sl argument is None -- because
the trailing engine overwrites sl/broker_sl and the R-relative floor needs
the original risk.

Fill timestamps are recorded at every fill site: MARKET placement, the
startup hydration path, the live reconcile path, the BE fill-race promotion
and the signal-edit path. The live path is the one that matters: a limit
order that fills hours after the signal was created would otherwise inherit
the signal timestamp and make a new position look hours old, firing the age
guard on it immediately."
```

---

## Task 4: `mt5_executor.py` — compute and apply the R floor

**Files:**
- Modify: `mt5_executor.py:16-112` (the `from config import (...)` block — add the ten lifecycle knobs)
- Modify: `mt5_executor.py:114-127` (the `dynamic_risk` / `risk_lifecycle` import block)
- Modify: `mt5_executor.py:136-231` (`_CFG_IMPORT_SNAPSHOT` name list — add the new knobs)
- Modify: `mt5_executor.py:1362-1399` (`_get_dynamic_risk_policy` — add a lifecycle policy resolver after it, at lines 1400-1422)
- Modify: `mt5_executor.py:2437-2446` (`_execute_4layer_specs` — the `initial_sl` fallback: a zero stop is not a stop)
- Modify: `mt5_executor.py:8258-8298` (`evaluate_dynamic_atr_engine` — the floor computation and the trail call)
- Modify: `mt5_executor.py:8359` (`evaluate_dynamic_atr_engine` — the ATR-trail bookkeeping whitelist)
- Modify: `tests/test_trade_lifecycle.py` (append the integration tests, lines 1482-1679)

**Interfaces:**
- Consumes: `risk_lifecycle.read_lifecycle_policy`, `risk_lifecycle.compute_r_lock_floor`, `LifecyclePolicy` (Task 1); `compute_dynamic_atr_trail_stop(..., r_lock_floor=...)` (Task 2); `layers[...]["initial_sl"]` (Task 3).
- Produces: `MT5Executor._get_lifecycle_policy() -> LifecyclePolicy`; the trail engine now receives a per-layer `r_lock_floor`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- executor R-floor wiring ---------------------------------------------

def _atr_signal(entry: float, sl: float, ticket: int = 101):
    return {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "ACTIVE",
        "layers": {
            "L1": {
                "ticket": ticket,
                "status": "OPEN",
                "lot": 0.01,
                "remaining_lot": 0.01,
                "entry_price": entry,
                "initial_sl": sl,
                "broker_sl": sl,
            },
        },
        "market_tickets": [ticket],
        "pending_tickets": [],
        # The TP levels sit BEYOND the live price used by the tests below, so
        # the milestone floor (which is locked to entry + $0.50 once TP1 is
        # cleared) cannot win the picker. These tests are about the R floor.
        "tp_levels": {"TP1": entry + 20.0, "TP2": entry + 30.0},
        "runner_cleared_milestones": [],
    }


def test_executor_applies_r_lock_floor(monkeypatch):
    import config
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 20.0     # wide ATR -> the ATR trail distance clamps to the $12 ceiling

    sig = _atr_signal(3895.0, 3880.0)   # R = 15.0
    # Live price is $11.00 in favour -> 0.73R -> tier 0.5R reached -> floor = entry
    res = ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    assert res.get("updated_tickets"), "the R floor must move the stop"
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3895.0)
    assert sig["layers"]["L1"]["initial_sl"] == pytest.approx(3880.0)
    assert sig.get("active_stop_source") == "R_LOCK"


def test_executor_r_lock_is_inert_when_disabled(monkeypatch):
    import config
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", False)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 20.0

    sig = _atr_signal(3895.0, 3880.0)
    ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_executor_r_lock_skipped_without_initial_sl(monkeypatch):
    import config
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 20.0

    sig = _atr_signal(3895.0, 3880.0)
    sig["layers"]["L1"].pop("initial_sl")     # a signal opened before the upgrade
    res = ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_execute_4layer_specs_treats_a_zero_stop_as_no_stop(monkeypatch):
    """The executor computes a fallback when sl == 0.0, so 0.0 is not a stop."""
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    monkeypatch.setattr(ex, "_execute_layer_price", lambda **kwargs: (9002, "MARKET"))
    market, pending, layers = ex._execute_4layer_specs(
        symbol="XAUUSD.s",
        direction="BUY",
        layer_specs=[("L1", 3895.0, 0.01, 3905.0)],
        sl=0.0,
        tick=SimpleNamespace(bid=3894.8, ask=3895.0, time=1700000000.0),
    )
    assert layers["L1"]["initial_sl"] is None


# --- executor R-floor: the invariants that make the floor safe ------------


def test_executor_r_lock_never_loosens_an_existing_stop(monkeypatch):
    """The R floor may only ever RAISE a stop.

    The floor is 3895.00 (entry) but the broker already holds 3896.00, so the
    R lock must not pull the stop back down to 3895.00.
    """
    import config
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 20.0

    sig = _atr_signal(3895.0, 3880.0)
    sig["layers"]["L1"]["broker_sl"] = 3896.0   # already tighter than the R floor
    res = ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    assert not res.get("updated_tickets"), "a looser stop must never be sent"
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3896.0)


def test_executor_r_lock_skips_a_layer_without_initial_sl_and_logs_it_once(monkeypatch, caplog):
    """A layer without an initial stop is skipped, and the skip is logged ONCE."""
    import config
    import json
    import logging
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 20.0

    sig = _atr_signal(3895.0, 3880.0)
    sig["layers"]["L1"].pop("initial_sl")

    with caplog.at_level(logging.INFO, logger="copier.mt5"):
        ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
        ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))

    skips = [rec for rec in caplog.records if "R-LOCK SKIPPED" in rec.getMessage()]
    assert len(skips) == 1, "the skip must be logged exactly once per layer"
    assert sig["layers"]["L1"].get("_r_lock_skipped_logged") is True
    # The marker lives on the persisted layer record, so it must stay JSON-safe.
    json.dumps(sig["layers"]["L1"])
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_executor_r_lock_records_the_trail_bookkeeping_when_it_wins(monkeypatch):
    """A layer protected by an R-lock stop must still look trail-protected.

    `dynamic_atr_trail_sl` drives the protection heuristics, so the R lock
    has to write it; `_mult`/`_zone` stay the engine's live tier telemetry.
    """
    import config
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", True)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 20.0

    sig = _atr_signal(3895.0, 3880.0)
    ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    lv = sig["layers"]["L1"]
    assert lv.get("dynamic_atr_trail_sl") == pytest.approx(3895.0)
    # The tier telemetry describes the engine, not the author of the stop.
    assert lv.get("dynamic_atr_trail_mult") is not None
    assert lv.get("dynamic_atr_trail_zone") is not None

```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `mt5_executor` does not expose the new knobs yet, so every new test
dies in `monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", ...)` with
`AttributeError: module 'mt5_executor' has no attribute 'R_LOCK_ENABLED'`.

Observed: `python -m pytest tests/test_trade_lifecycle.py -q -k "r_lock or zero_stop"` ->
**7 failed**, 22 passed, 48 deselected.

- [ ] **Step 3: Import the lifecycle module in `mt5_executor.py`**

At the `from dynamic_risk import (...)` block (lines 114-120), the existing
`from risk_lifecycle import stamp_fill_times` becomes (lines 122-127):

```python
from risk_lifecycle import (
    LifecyclePolicy,
    compute_r_lock_floor,
    read_lifecycle_policy,
    stamp_fill_times,
)
```

- [ ] **Step 4: Register the new knobs in `_CFG_IMPORT_SNAPSHOT`**

In `mt5_executor.py`, add the ten new knob names to the `_CFG_IMPORT_SNAPSHOT` name list (the tuple that ends at line 219 with `"MT5_PATH",`), immediately before the closing `)`:

```python
        "R_LOCK_ENABLED",
        "R_LOCK_TIERS",
        "R_LOCK_MIN_R_DOLLARS",
        "LIFECYCLE_ENABLED",
        "LIFECYCLE_DERISK_SECONDS",
        "LIFECYCLE_FLATTEN_SECONDS",
        "LIFECYCLE_PENDING_EXPIRE_SECONDS",
        "LIFECYCLE_SESSION_FLATTEN_ENABLED",
        "LIFECYCLE_SESSION_FLATTEN_AT",
        "LIFECYCLE_MAX_CLOSE_RETRIES",
```

Then import the constants into the module globals so `_cfg()` can resolve them. `mt5_executor.py` already has `import config as _config` (line 15) and a `from config import (...)` block (lines 16-112) — add the ten names to that existing block. The shipped file keeps the block alphabetical, so the names land in two hunks:

```python
from config import (
    ...
    LEGACY_LAYER_MAP,
    LIFECYCLE_DERISK_SECONDS,
    LIFECYCLE_ENABLED,
    LIFECYCLE_FLATTEN_SECONDS,
    LIFECYCLE_MAX_CLOSE_RETRIES,
    LIFECYCLE_PENDING_EXPIRE_SECONDS,
    LIFECYCLE_SESSION_FLATTEN_AT,
    LIFECYCLE_SESSION_FLATTEN_ENABLED,
    M1_ATR_PERIOD,
    ...
    RISK_LOT_MULTIPLIER,
    R_LOCK_ENABLED,
    R_LOCK_MIN_R_DOLLARS,
    R_LOCK_TIERS,
    RUNNER_DEEPEST_COUNT_FULL,
    ...
)
```

- [ ] **Step 5: Add the lifecycle policy resolver**

In `mt5_executor.py`, add this method immediately after `_get_dynamic_risk_policy` (which ends at line 1399 in the shipped file; the new method occupies lines 1400-1422):

```python
    def _get_lifecycle_policy(self) -> LifecyclePolicy:
        """Resolve the LifecyclePolicy respecting monkeypatches on either config or mt5_executor."""
        proxy = SimpleNamespace(
            R_LOCK_ENABLED=_cfg("R_LOCK_ENABLED", R_LOCK_ENABLED),
            R_LOCK_TIERS=_cfg("R_LOCK_TIERS", R_LOCK_TIERS),
            R_LOCK_MIN_R_DOLLARS=_cfg("R_LOCK_MIN_R_DOLLARS", R_LOCK_MIN_R_DOLLARS),
            LIFECYCLE_ENABLED=_cfg("LIFECYCLE_ENABLED", LIFECYCLE_ENABLED),
            LIFECYCLE_DERISK_SECONDS=_cfg("LIFECYCLE_DERISK_SECONDS", LIFECYCLE_DERISK_SECONDS),
            LIFECYCLE_FLATTEN_SECONDS=_cfg("LIFECYCLE_FLATTEN_SECONDS", LIFECYCLE_FLATTEN_SECONDS),
            LIFECYCLE_PENDING_EXPIRE_SECONDS=_cfg(
                "LIFECYCLE_PENDING_EXPIRE_SECONDS", LIFECYCLE_PENDING_EXPIRE_SECONDS
            ),
            LIFECYCLE_SESSION_FLATTEN_ENABLED=_cfg(
                "LIFECYCLE_SESSION_FLATTEN_ENABLED", LIFECYCLE_SESSION_FLATTEN_ENABLED
            ),
            LIFECYCLE_SESSION_FLATTEN_AT=_cfg(
                "LIFECYCLE_SESSION_FLATTEN_AT", LIFECYCLE_SESSION_FLATTEN_AT
            ),
            LIFECYCLE_MAX_CLOSE_RETRIES=_cfg(
                "LIFECYCLE_MAX_CLOSE_RETRIES", LIFECYCLE_MAX_CLOSE_RETRIES
            ),
        )
        return read_lifecycle_policy(proxy)
```

- [ ] **Step 6: Treat a zero stop as no stop in the executor**

`main.py:_resolve_edit_initial_sl` already returns `None` for a zero stop, because the executor
treats `sl == 0.0` as "no stop given" and computes a fallback instead (`_compute_fallback_sl`,
`mt5_executor.py:1463`). The executor's own `initial_sl` fallback does not: at
`mt5_executor.py:2437-2446` it records `sl == 0.0` as a stop.

A zero stop is not a stop. Record `None` for it, so the layer has no initial risk R and the
R-lock is skipped for that layer (exactly like a missing `initial_sl`) rather than being given a
bogus R.

In `_execute_4layer_specs`, change:

```python
                placed_sl = self.get_last_placed_sl(t)
                if placed_sl is not None:
                    initial_sl = round(float(placed_sl), digits)
                elif sl is not None:
                    initial_sl = round(float(sl), digits)
                else:
                    initial_sl = None
```

to:

```python
                placed_sl = self.get_last_placed_sl(t)
                if placed_sl is not None and float(placed_sl) > 0.0:
                    initial_sl = round(float(placed_sl), digits)
                elif sl is not None and float(sl) > 0.0:
                    initial_sl = round(float(sl), digits)
                else:
                    # A zero stop is not a stop: the executor treats `sl == 0.0` as
                    # "no stop given" and computes a fallback instead, so there is
                    # no initial risk R to record.
                    initial_sl = None
```

**Test:** a layer placed with `sl=0.0` records `initial_sl is None`, and the R-lock is skipped
for that layer (the stop is not moved to a floor).

- [ ] **Step 7: Compute and pass the R floor**

In `mt5_executor.py`, inside `evaluate_dynamic_atr_engine`, insert the floor computation immediately before the `# 2) Dynamic Trailing SL` comment (the shipped block occupies lines 8258-8280).

Use the layer's persisted `lv["mfe_dollars"]` as the maximum favourable excursion, NOT a fresh
`sign * (layer_peak - entry_px)`. That field is already computed a few lines above
(`mt5_executor.py:8139`) as the running, non-negative maximum over every tick, and it survives a
restart; recomputing it would drop the monotonic guarantee.

```python
            # 1b) R-relative profit floor.
            # The ATR trail owns the tight stop; this floor only ever raises the
            # stop, and it scales with the trade's own initial risk R so a wide
            # stop is not protected after the same absolute move as a scalp.
            r_lock_floor = None
            lifecycle_pol = self._get_lifecycle_policy()
            initial_sl = _num(lv.get("initial_sl"), 0.0)
            if lifecycle_pol.r_lock_enabled and initial_sl > 0.0:
                r_lock_floor = compute_r_lock_floor(
                    direction=direction,
                    entry_price=entry_px,
                    initial_sl=initial_sl,
                    mfe_dollars=_num(lv.get("mfe_dollars"), 0.0),
                    live_price=live_price,
                    policy=lifecycle_pol,
                    digits=digits,
                )
            elif lifecycle_pol.r_lock_enabled and not lv.get("_r_lock_skipped_logged"):
                lv["_r_lock_skipped_logged"] = True
                logger.info(
                    f"[R-LOCK SKIPPED] {lk} has no initial_sl (the layer was placed without a stop, "
                    "or the signal predates the upgrade); ATR trail only."
                )
```

Then add the new keyword to the `compute_dynamic_atr_trail_stop(...)` call (the shipped call occupies lines 8284-8298):

```python
            trail_step = compute_dynamic_atr_trail_stop(
                direction=direction,
                entry_price=entry_px,
                live_price=live_price,
                prev_peak=lv.get("dynamic_atr_trail_peak"),
                current_sl=cur_sl,
                atr=atr,
                policy=policy,
                digits=digits,
                stops_buffer=stops_buffer,
                cleared_milestones=sig_cleared,
                tp_levels=tp_levels,
                apply_milestone_floor=(role not in ("BANK_TP1", "LATE_RECOVERY")),
                r_lock_floor=r_lock_floor,
            )
```

**Also add `"R_LOCK"` to the ATR-trail bookkeeping whitelist.** Before this change the condition read:

```python
            if stop_source in ("ATR_TRAIL", "MILESTONE_FLOOR"):
                lv["dynamic_atr_trail_sl"] = cand_sl
                lv["dynamic_atr_trail_dist"] = trail_step["trail_dist"]
                lv["dynamic_atr_trail_atr"] = trail_step["atr"]
                lv["dynamic_atr_trail_mult"] = trail_step.get("trail_mult") or last_trail_mult
                lv["dynamic_atr_trail_zone"] = trail_step.get("trail_zone") or last_trail_zone
```

When the R lock wins the picker, `stop_source == "R_LOCK"` and that block is skipped, so `lv["dynamic_atr_trail_sl"]` would lag the actual broker SL. That field drives the protection heuristics at `main.py:3372`, `main.py:5703-5710`, `mt5_executor.py:7033` and `mt5_executor.py:7355`, so a layer protected by an R-lock stop could be reported as not trail-protected. The shipped file reads (line 8359):

```python
            if stop_source in ("ATR_TRAIL", "MILESTONE_FLOOR", "R_LOCK"):
```

The `dynamic_atr_trail_mult` / `_zone` values reported for an R-lock stop are the trail's own telemetry (the trail may not have been the winning candidate); that is acceptable — they describe the engine's live tier, not the stop's author.

Verified on the shipped implementation: with the R lock enabled and winning, `dynamic_atr_trail_mult`/`_zone` are `1.0`/`"EXTENDED"` both with and without `"R_LOCK"` in the whitelist — the whitelist only adds `dynamic_atr_trail_sl`/`_dist`/`_atr`. (Both `_mult` and `_zone` are also written unconditionally right after the whitelist block, so they can never describe the stop's author.)

- [ ] **Step 8: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS — 77 passed (70 pre-existing + 7 new).

- [ ] **Step 9: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**. Pay attention to `tests/test_dynamic_risk_management.py` and `tests/test_msg_10180_10196_regressions.py` — they assert on `stop_source` values; with `R_LOCK_ENABLED=false` by default the tie-break change must not alter them.

Observed on the shipped implementation: **318 passed**, 0 failed (311 before this task + 7 new).

- [ ] **Step 10: Commit**

```powershell
git add mt5_executor.py tests/test_trade_lifecycle.py
git commit -m "feat(mt5_executor): wire the R-relative profit floor into the trail engine

Each layer computes its own R floor from entry and the captured initial_sl and
passes it to compute_dynamic_atr_trail_stop as an extra candidate. The ATR trail
still owns the tight stop and the never-loosens invariant holds. Layers without
initial_sl (signals opened before the upgrade) are skipped and logged once.

A zero stop is recorded as no stop, matching the executor's own treatment of
sl == 0.0 as a request to compute a fallback.

The feature ships DISABLED: R_LOCK_ENABLED defaults to False in config.py, so
with the default the trail engine behaves exactly as before."
```

**Shipped notes — deviations from the original plan and their reasons:**

1. **The test fixture's TP levels moved beyond the live price.** The plan's fixture used
   `TP1 = entry + 10.0` / `TP2 = entry + 20.0` with a live price of `entry + 11.0`, which clears
   TP1. Clearing TP1 arms the **milestone floor** at `entry + $0.50` — *tighter* than the R floor
   (`entry`) — so the milestone floor wins the picker and the R lock is never exercised. The
   shipped fixture uses `TP1 = entry + 20.0` / `TP2 = entry + 30.0` so TP1 is not cleared and the
   R floor is the only candidate. The live price, the entry, the stop and every assertion are
   unchanged.
2. **`from risk_lifecycle import (...)` keeps `stamp_fill_times`.** The plan's block listed only
   the three new names; the shipped file already imported `stamp_fill_times`, so it is kept in
   the same parenthesised import.
3. **Three extra tests** cover the invariants this task has to prove end-to-end: the R floor
   never loosens an existing stop, a layer without `initial_sl` is skipped and logged exactly
   once (and the `_r_lock_skipped_logged` marker stays JSON-serialisable), and an R-lock stop
   still writes the `dynamic_atr_trail_*` protection fields.

---

## Task 5: `mt5_executor.py` — the lifecycle actions

**Files:**
- Modify: `mt5_executor.py` (add four methods after `close_signal_exposure`, which starts at line 5512)
- Modify: `tests/test_trade_lifecycle.py` (append the action tests)

**Interfaces:**
- Consumes: `risk_lifecycle` action constants (Task 1); `close_signal_exposure`, `cancel_signal_pending_orders`, `modify_position_sltp`, `is_within_freeze_level` (existing).
- Produces:
  - `MT5Executor.apply_lifecycle_derisk(signal_data: Dict[str, Any]) -> Dict[str, Any]`
  - `MT5Executor.apply_lifecycle_flatten(signal_data: Dict[str, Any]) -> Dict[str, Any]`
  - `MT5Executor.apply_lifecycle_pending_expire(signal_data: Dict[str, Any]) -> Dict[str, Any]`
  - `MT5Executor.apply_lifecycle_action(signal_data: Dict[str, Any], action: str) -> Dict[str, Any]`

  All four return a dict with the keys `"action"`, `"moved"`, `"closed"`, `"cancelled"`, `"failed"`, `"ok"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- lifecycle actions ----------------------------------------------------

def _lifecycle_signal():
    return {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "ACTIVE",
        "layers": {
            "L1": {
                "ticket": 101,
                "status": "OPEN",
                "lot": 0.01,
                "remaining_lot": 0.01,
                "entry_price": 3895.0,
                "initial_sl": 3880.0,
                "broker_sl": 3880.0,
            },
            "L2": {
                "ticket": 102,
                "status": "PENDING",
                "lot": 0.01,
                "remaining_lot": 0.01,
                "target_price": 3890.0,
            },
        },
        "market_tickets": [101],
        "pending_tickets": [102],
    }


def test_apply_lifecycle_derisk_moves_open_layers_to_be(monkeypatch):
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert res["ok"] is True
    assert res["action"] == "DERISK"
    # ONLY the open layer is moved. L2 is a PENDING limit that was just cancelled;
    # modifying it would fail at the broker and report a failed de-risk.
    assert [t for t, _ in calls] == [101]
    assert calls[0][1] == pytest.approx(3895.50)   # entry + the $0.50 BE offset
    assert res["cancelled"] == [102]
    assert res["failed"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3895.50)


def test_apply_lifecycle_derisk_leaves_pending_layers_untouched(monkeypatch):
    """A pending limit has no position to move; touching it would report a false failure."""
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert [t for t, _ in calls] == [101]
    assert res["ok"] is True
    assert res["failed"] == []
    assert res["moved"] and res["moved"][0]["layer"] == "L1"


def test_apply_lifecycle_derisk_never_loosens_an_existing_stop(monkeypatch):
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [])

    sig = _lifecycle_signal()
    sig["layers"]["L1"]["broker_sl"] = 3900.0    # already tighter than BE
    res = ex.apply_lifecycle_derisk(sig)

    assert calls == []
    assert res["moved"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3900.0)


def test_apply_lifecycle_flatten_closes_everything(monkeypatch):
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [101], "cancelled_orders": [102]},
    )

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is True
    assert res["action"] == "FLATTEN"
    assert res["closed"] == [101]
    assert res["cancelled"] == [102]


def test_apply_lifecycle_pending_expire_cancels_only(monkeypatch):
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0

    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["market_tickets"] = []
    res = ex.apply_lifecycle_pending_expire(sig)

    assert res["ok"] is True
    assert res["action"] == "EXPIRE_PENDING"
    assert res["closed"] == []
    assert res["cancelled"] == [102]


def test_apply_lifecycle_action_dispatches(monkeypatch):
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [101], "cancelled_orders": [102]},
    )

    res = ex.apply_lifecycle_action(_lifecycle_signal(), "FLATTEN")
    assert res["action"] == "FLATTEN"

    res_none = ex.apply_lifecycle_action(_lifecycle_signal(), "NONE")
    assert res_none["action"] == "NONE"
    assert res_none["ok"] is True


# --- lifecycle actions: the extra invariants -------------------------------

def _lifecycle_executor():
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    return ex


def test_apply_lifecycle_derisk_sell_moves_to_breakeven_below_the_entry(monkeypatch):
    """The SELL mirror of the BUY case: the stop goes to entry - offset."""
    ex = _lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["direction"] = "SELL"
    sig["layers"]["L1"]["entry_price"] = 3905.0
    sig["layers"]["L1"]["broker_sl"] = 3920.0
    res = ex.apply_lifecycle_derisk(sig)

    assert res["ok"] is True
    assert [t for t, _ in calls] == [101]
    assert calls[0][1] == pytest.approx(3904.50)   # entry - the $0.50 BE offset
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3904.50)


def test_apply_lifecycle_derisk_sell_never_loosens_an_existing_stop(monkeypatch):
    """A SELL stop at (or below) the breakeven price is already tight enough."""
    ex = _lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [])

    sig = _lifecycle_signal()
    sig["direction"] = "SELL"
    sig["layers"]["L1"]["entry_price"] = 3905.0
    sig["layers"]["L1"]["broker_sl"] = 3904.50    # already tighter than BE (3904.50)
    res = ex.apply_lifecycle_derisk(sig)

    assert calls == []
    assert res["moved"] == []
    assert res["ok"] is True
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3904.50)


def test_apply_lifecycle_derisk_uses_the_configured_breakeven_offset(monkeypatch):
    """The offset comes from DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS; 0 means exactly breakeven."""
    import config
    import mt5_executor

    ex = _lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 0.0)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 0.0)
    res = ex.apply_lifecycle_derisk(_lifecycle_signal())
    assert res["ok"] is True
    assert calls[0][1] == pytest.approx(3895.0)   # offset 0 -> exactly the entry price

    calls.clear()
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 1.25)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 1.25)
    res = ex.apply_lifecycle_derisk(_lifecycle_signal())
    assert res["ok"] is True
    assert calls[0][1] == pytest.approx(3896.25)


def test_apply_lifecycle_derisk_publishes_the_mean_initial_risk_per_ounce(monkeypatch):
    """`initial_r_per_oz` is the mean of the per-layer initial risks."""
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["layers"]["L3"] = {
        "ticket": 103,
        "status": "OPEN",
        "lot": 0.01,
        "remaining_lot": 0.01,
        "entry_price": 3900.0,
        "initial_sl": 3880.0,
        "broker_sl": 3880.0,
    }
    res = ex.apply_lifecycle_derisk(sig)

    assert res["ok"] is True
    # L1 risk = 15.00, L3 risk = 20.00 -> mean 17.50
    assert sig["initial_r_per_oz"] == pytest.approx(17.5)


def test_apply_lifecycle_derisk_does_not_publish_a_zero_initial_risk(monkeypatch):
    """With no layer that has both an entry and an initial stop there is no R to publish."""
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["layers"]["L1"].pop("initial_sl")
    ex.apply_lifecycle_derisk(sig)

    assert "initial_r_per_oz" not in sig


def test_apply_lifecycle_derisk_reports_a_rejected_stop_as_a_failure(monkeypatch):
    """A broker rejection is reported, and the existing stop is left in place."""
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: False)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert res["ok"] is False
    assert res["failed"] == ["L1:modify_failed"]
    assert res["moved"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_apply_lifecycle_derisk_never_raises_a_broker_error(monkeypatch):
    """An exception from the broker is recorded in `failed`, never propagated."""
    ex = _lifecycle_executor()

    def _boom(t, sl=None, tp=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(ex, "modify_position_sltp", _boom)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert res["ok"] is False
    assert res["failed"] and res["failed"][0].startswith("L1:")
    assert res["moved"] == []


def test_apply_lifecycle_derisk_skips_a_stop_on_the_wrong_side_of_the_market(monkeypatch):
    """A BUY stop above the market is invalid (retcode 10016) and must not be sent."""
    from types import SimpleNamespace

    ex = _lifecycle_executor()
    ex.dry_run = False   # the market-side guard only runs against a live broker

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    # The market fell BELOW the entry: entry + 0.50 is above the market now.
    monkeypatch.setattr(ex, "get_tick", lambda symbol: SimpleNamespace(bid=3890.0, ask=3890.2))

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert calls == [], "an invalid stop must never reach the broker"
    assert res["moved"] == []
    assert res["ok"] is True
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_apply_lifecycle_flatten_counts_a_failed_close_as_a_retry(monkeypatch):
    """A close that leaves the position open is a failure, not a success."""
    from types import SimpleNamespace

    ex = _lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [], "cancelled_orders": []},
    )
    # The broker still shows the position: the close did not happen.
    monkeypatch.setattr(ex, "get_live_positions", lambda symbol=None: [SimpleNamespace(ticket=101)])

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is False
    assert res["failed"]
    assert res.get("gave_up") is not True
    assert sig["lifecycle_retry_count"] == 1


def test_apply_lifecycle_flatten_gives_up_after_max_close_retries(monkeypatch):
    """After the retry budget the guard stops closing and leaves the position to its broker SL."""
    from types import SimpleNamespace

    import config
    import mt5_executor

    ex = _lifecycle_executor()

    monkeypatch.setattr(config, "LIFECYCLE_MAX_CLOSE_RETRIES", 3)
    monkeypatch.setattr(mt5_executor, "LIFECYCLE_MAX_CLOSE_RETRIES", 3)

    attempts = []

    def _fail(sig):
        attempts.append(1)
        return {"closed_tickets": [], "cancelled_orders": []}

    monkeypatch.setattr(ex, "close_signal_exposure", _fail)
    monkeypatch.setattr(ex, "get_live_positions", lambda symbol=None: [SimpleNamespace(ticket=101)])

    sig = _lifecycle_signal()
    results = [ex.apply_lifecycle_flatten(sig) for _ in range(4)]

    assert len(attempts) == 3, "the guard must not exceed the retry budget"
    assert results[-1].get("gave_up") is True
    assert results[-1]["ok"] is False
    assert sig["lifecycle_retry_count"] == 3
    # The give-up path closes nothing by another route.
    assert results[-1]["closed"] == []


def test_apply_lifecycle_flatten_resets_the_retry_count_after_a_success(monkeypatch):
    ex = _lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [101], "cancelled_orders": [102]},
    )

    sig = _lifecycle_signal()
    sig["lifecycle_retry_count"] = 2
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is True
    assert sig["lifecycle_retry_count"] == 0


def test_apply_lifecycle_actions_reject_a_signal_that_is_not_a_dict():
    """Every action returns a well-formed result, even for a malformed signal."""
    ex = _lifecycle_executor()

    for action in (ex.apply_lifecycle_derisk, ex.apply_lifecycle_flatten, ex.apply_lifecycle_pending_expire):
        res = action(None)
        assert res["ok"] is False
        assert res["moved"] == []
        assert res["closed"] == []
        assert res["cancelled"] == []
        assert res["failed"] == []


def test_apply_lifecycle_action_ignores_an_unknown_action(monkeypatch):
    """An unknown action is a no-op: no broker call, no failure."""
    ex = _lifecycle_executor()

    called = []
    monkeypatch.setattr(ex, "close_signal_exposure", lambda sig: called.append(1) or {})
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: called.append(1) or [])

    res = ex.apply_lifecycle_action(_lifecycle_signal(), "BANANA")

    assert res == {
        "action": "NONE",
        "ok": True,
        "moved": [],
        "closed": [],
        "cancelled": [],
        "failed": [],
    }
    assert called == []


def test_apply_lifecycle_action_dispatches_every_action(monkeypatch):
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [101], "cancelled_orders": [102]},
    )
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)

    assert ex.apply_lifecycle_action(_lifecycle_signal(), "DERISK")["action"] == "DERISK"
    assert ex.apply_lifecycle_action(_lifecycle_signal(), "FLATTEN")["action"] == "FLATTEN"
    assert ex.apply_lifecycle_action(_lifecycle_signal(), "SESSION_FLATTEN")["action"] == "FLATTEN"
    assert ex.apply_lifecycle_action(_lifecycle_signal(), "EXPIRE_PENDING")["action"] == "EXPIRE_PENDING"
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `AttributeError: 'MT5Executor' object has no attribute 'apply_lifecycle_derisk'`.

- [ ] **Step 3: Implement the four methods**

Add the following to `mt5_executor.py`, immediately after `close_signal_exposure` (which starts at line 5512):

```python
    # --- trade lifecycle actions (age-based de-risk / flatten) -------------

    @staticmethod
    def _lifecycle_tickets(values: Any) -> List[int]:
        """Normalise a ticket collection into positive integers (never raises)."""
        out: List[int] = []
        try:
            items = list(values or [])
        except Exception:
            return out
        for v in items:
            try:
                t = int(v)
            except Exception:
                continue
            if t > 0:
                out.append(t)
        return out

    def _lifecycle_open_tickets(self, signal_data: Dict[str, Any]) -> List[int]:
        """The market tickets this signal believes are open.

        Mirrors the ticket selection of `close_signal_exposure` (pending orders
        excluded) so the flatten can tell "the close did not happen" from
        "there was nothing left to close".
        """
        accounted = {
            _int_num(x, 0)
            for x in (signal_data.get("accounted_tickets") or [])
            if _int_num(x, 0) > 0
        }
        tickets: List[int] = []
        seen: Set[int] = set()

        def _add(val: Any) -> None:
            t = _int_num(val, 0)
            if t > 0 and t not in seen and t not in accounted:
                seen.add(t)
                tickets.append(t)

        for t in (signal_data.get("market_tickets") or []):
            _add(t)
        for t in (signal_data.get("runner_tickets") or []):
            _add(t)
        if signal_data.get("runner_ticket") is not None:
            _add(signal_data.get("runner_ticket"))
        layers = signal_data.get("layers")
        if isinstance(layers, dict):
            for lv in layers.values():
                if not isinstance(lv, dict):
                    continue
                if str(lv.get("status") or "").strip().upper() in ("OPEN", "FILLED", "RUNNER"):
                    _add(lv.get("ticket"))
        return tickets

    def apply_lifecycle_derisk(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Cancel unfilled pending limits and move every open layer to breakeven.

        Never loosens an existing stop: a layer whose broker SL is already
        tighter than the breakeven price is left untouched.
        """
        result: Dict[str, Any] = {
            "action": ACTION_DERISK,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
        if not isinstance(signal_data, dict):
            result["ok"] = False
            return result

        try:
            cancelled = self.cancel_signal_pending_orders(signal_data) or []
            result["cancelled"] = self._lifecycle_tickets(cancelled)
        except Exception as exc:
            logger.warning(f"[LIFECYCLE DERISK] cancel pending failed: {exc}")
            result["failed"].append(f"cancel:{exc}")

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        sign = 1.0 if is_buy else -1.0

        sym_info = None
        try:
            sym_info = self.get_symbol_info(symbol)
        except Exception as exc:
            logger.warning(f"[LIFECYCLE DERISK] symbol info for {symbol} failed: {exc}")
            result["failed"].append(f"sym_info:{exc}")
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        offset = max(
            0.0,
            _cfg("DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS),
        )
        layers = signal_data.get("layers") or {}

        # Publish the signal's initial risk (per ounce) so the ledger can record
        # `initial_r_per_oz` and `r_at_exit` at close time.
        _risks: List[float] = []
        for _lv in layers.values():
            if not isinstance(_lv, dict):
                continue
            _e = _num(_lv.get("entry_price") or _lv.get("entry") or _lv.get("target_price"), 0.0)
            _s = _num(_lv.get("initial_sl"), 0.0)
            if _e > 0.0 and _s > 0.0:
                _risks.append(abs(_e - _s))
        if _risks:
            signal_data["initial_r_per_oz"] = round(sum(_risks) / len(_risks), 4)

        # The market decides whether the breakeven stop is even a valid stop: a
        # BUY stop above the market (or a SELL stop below it) is rejected by the
        # broker with retcode 10016 (invalid stops). The check runs against a
        # live broker only -- a dry run has nothing that could reject it.
        tick = None
        live_price = 0.0
        if not self.dry_run:
            try:
                tick = self.get_tick(symbol)
            except Exception as exc:
                logger.warning(f"[LIFECYCLE DERISK] tick for {symbol} failed: {exc}")
                result["failed"].append(f"tick:{exc}")
            if tick is not None:
                _raw_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
                live_price = _num(_raw_px, 0.0)
        stops_buffer = self._compute_stops_buffer(sym_info)

        for lk, lv in list(layers.items()):
            if not isinstance(lv, dict):
                continue
            # Only a layer that HOLDS a position can be moved to breakeven. A
            # PENDING layer is an unfilled limit that `cancel_signal_pending_orders`
            # has just cancelled -- modifying it would fail at the broker and
            # would mark an otherwise successful de-risk as failed.
            if str(lv.get("status") or "").strip().upper() in ("PENDING", "CANCELLED", "CLOSED"):
                continue
            ticket = _int_num(lv.get("ticket"), 0)
            if ticket <= 0:
                continue
            entry = _num(lv.get("entry_price") or lv.get("entry") or lv.get("target_price"), 0.0)
            if entry <= 0.0:
                result["failed"].append(f"{lk}:no_entry")
                continue
            be_sl = round(entry + offset if is_buy else entry - offset, digits)
            cur_sl = _num(lv.get("broker_sl") or lv.get("sl"), 0.0)
            if cur_sl > 0.0:
                already_tighter = (cur_sl >= be_sl - 1e-6) if is_buy else (cur_sl <= be_sl + 1e-6)
                if already_tighter:
                    continue
            try:
                # The breakeven stop has to sit on the correct side of the
                # market and far enough away from it, otherwise the broker
                # rejects the modify with retcode 10016 (invalid stops).
                if live_price > 0.0 and sign * (live_price - be_sl) < stops_buffer - 1e-9:
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: BE stop {be_sl} is not a "
                        f"valid stop at the market ({live_price}); the existing stop is kept."
                    )
                    continue
                if self.is_within_freeze_level(symbol, be_sl, tick=tick, sym_info=sym_info):
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: BE stop {be_sl} is inside the "
                        f"broker freeze level; the existing stop is kept."
                    )
                    continue
                if self.modify_position_sltp(ticket, sl=be_sl, tp=None):
                    lv["sl"] = be_sl
                    lv["broker_sl"] = be_sl
                    result["moved"].append({"layer": lk, "ticket": ticket, "sl": be_sl})
                else:
                    result["failed"].append(f"{lk}:modify_failed")
            except Exception as exc:
                logger.warning(f"[LIFECYCLE DERISK] layer {lk} ticket {ticket} failed: {exc}")
                result["failed"].append(f"{lk}:{exc}")

        result["ok"] = not result["failed"]
        logger.info(
            f"[LIFECYCLE DERISK] moved={len(result['moved'])} cancelled={len(result['cancelled'])} "
            f"failed={len(result['failed'])}"
        )
        return result

    def apply_lifecycle_flatten(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Close every open position at market and cancel every pending limit."""
        result: Dict[str, Any] = {
            "action": ACTION_FLATTEN,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
        if not isinstance(signal_data, dict):
            result["ok"] = False
            return result

        max_attempts = max(1, self._get_lifecycle_policy().max_close_retries)
        retries = _int_num(signal_data.get("lifecycle_retry_count"), 0)

        # The retry budget is spent: the guard stops closing and leaves the
        # position to its broker SL (the safe fallback).
        if retries >= max_attempts:
            result["ok"] = False
            result["gave_up"] = True
            result["failed"].append("close:give_up")
            logger.error(
                f"[LIFECYCLE FLATTEN] giving up after {retries} failed attempts "
                f"(max {max_attempts}); the position stays on its broker SL."
            )
            return result

        expected_open = self._lifecycle_open_tickets(signal_data)
        try:
            exposure = self.close_signal_exposure(signal_data) or {}
            result["closed"] = self._lifecycle_tickets(exposure.get("closed_tickets") or [])
            result["cancelled"] = self._lifecycle_tickets(exposure.get("cancelled_orders") or [])
        except Exception as exc:
            logger.warning(f"[LIFECYCLE FLATTEN] close failed: {exc}")
            result["failed"].append(f"close:{exc}")

        if not result["failed"] and expected_open:
            # A close that left the position open is a FAILURE: the guard has to
            # retry (and eventually give up) instead of claiming a flatten that
            # never happened.
            closed_set = set(result["closed"])
            try:
                live_ids = {
                    _int_num(getattr(p, "ticket", 0), 0)
                    for p in (self.get_live_positions() or [])
                }
            except Exception as exc:
                logger.warning(f"[LIFECYCLE FLATTEN] live position check failed: {exc}")
                live_ids = set()
            still_open = [t for t in expected_open if t not in closed_set and t in live_ids]
            if still_open:
                result["failed"].append(
                    "close:still_open:" + ",".join(str(t) for t in still_open)
                )

        if result["failed"]:
            result["ok"] = False
            retries += 1
            signal_data["lifecycle_retry_count"] = retries
            if retries >= max_attempts:
                logger.error(
                    f"[LIFECYCLE FLATTEN] giving up after {retries} failed attempts "
                    f"(max {max_attempts}); the position stays on its broker SL."
                )
                result["gave_up"] = True
        else:
            signal_data["lifecycle_retry_count"] = 0

        logger.info(
            f"[LIFECYCLE FLATTEN] closed={len(result['closed'])} cancelled={len(result['cancelled'])} "
            f"failed={len(result['failed'])}"
        )
        return result

    def apply_lifecycle_pending_expire(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Cancel the unfilled pending limits of a signal that never filled."""
        result: Dict[str, Any] = {
            "action": ACTION_EXPIRE_PENDING,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
        if not isinstance(signal_data, dict):
            result["ok"] = False
            return result
        try:
            cancelled = self.cancel_signal_pending_orders(signal_data) or []
            result["cancelled"] = self._lifecycle_tickets(cancelled)
        except Exception as exc:
            logger.warning(f"[LIFECYCLE EXPIRE_PENDING] cancel failed: {exc}")
            result["failed"].append(f"cancel:{exc}")
            result["ok"] = False
        logger.info(f"[LIFECYCLE EXPIRE_PENDING] cancelled={len(result['cancelled'])}")
        return result

    def apply_lifecycle_action(self, signal_data: Dict[str, Any], action: str) -> Dict[str, Any]:
        """Dispatch a lifecycle action. Unknown actions are a no-op."""
        token = str(action or "").strip().upper()
        if token == ACTION_DERISK:
            return self.apply_lifecycle_derisk(signal_data)
        if token in (ACTION_FLATTEN, ACTION_SESSION_FLATTEN):
            return self.apply_lifecycle_flatten(signal_data)
        if token == ACTION_EXPIRE_PENDING:
            return self.apply_lifecycle_pending_expire(signal_data)
        return {
            "action": ACTION_NONE,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 6: Commit**

```powershell
git add mt5_executor.py tests/test_trade_lifecycle.py
git commit -m "feat(mt5_executor): add derisk / flatten / pending-expire lifecycle actions

apply_lifecycle_derisk cancels unfilled pending limits and moves open layers to
breakeven without ever loosening an existing stop. apply_lifecycle_flatten
reuses close_signal_exposure so the validated market-close path is shared with
/closeall. Every action returns a structured result for logging and alerting."
```

---

## Task 6: `main.py` — run the age guard every second

**Files:**
- Modify: `main.py:4111-4120` (`_process_pre_tp1_or_backstop_tick`)
- Modify: `tests/test_trade_lifecycle.py` (append the watcher tests)

**Interfaces:**
- Consumes: `risk_lifecycle.evaluate_trade_age`, `risk_lifecycle.read_lifecycle_policy` (Task 1); `MT5Executor.apply_lifecycle_action` (Task 5).
- Produces: the age guard runs inside `_process_pre_tp1_or_backstop_tick`, stamping `sig["lifecycle_derisk_at"]`, `sig["lifecycle_flatten_at"]`, `sig["lifecycle_pending_expired_at"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- main.py age guard ---------------------------------------------------

def _install_signal(monkeypatch, tmp_path, sig):
    import main
    from state_manager import StateManager
    from trade_ledger import TradeLedger

    main.state_mgr = StateManager(tmp_path / "lifecycle_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "lifecycle_ledger.jsonl")
    main.state_mgr.create_signal(
        message_id="9001",
        symbol="XAUUSD.s",
        direction="BUY",
        timestamp=sig.get("timestamp"),
    )
    main.state_mgr.update_signal("9001", sig)
    return main


@pytest.mark.asyncio
async def test_age_guard_derisks_an_old_signal(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", True)
    monkeypatch.setattr(config, "LIFECYCLE_DERISK_SECONDS", 7200)
    monkeypatch.setattr(config, "LIFECYCLE_FLATTEN_SECONDS", 21600)

    sig = _lifecycle_signal()
    sig["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sig["first_fill_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    applied = []

    class _FakeExecutor:
        def get_tick(self, symbol):
            from types import SimpleNamespace

            return SimpleNamespace(bid=3895.0, ask=3895.2, time=1700000000.0)

        def get_pip_size(self, symbol):
            return 0.1

        def apply_lifecycle_action(self, signal_data, action):
            applied.append(action)
            return {"action": action, "ok": True, "moved": [], "closed": [], "cancelled": [], "failed": []}

    monkeypatch.setattr(main_mod, "executor", _FakeExecutor())

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert applied == ["DERISK"]
```

> The repo already uses `@pytest.mark.asyncio` for async tests (see `tests/test_v3_concurrency_and_ops.py:89`), so use that marker exactly as shown. If pytest-asyncio is not installed in the environment, `pip install pytest-asyncio` is required — check with `python -m pytest tests/test_v3_concurrency_and_ops.py -q` first.

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — the watcher does not call `apply_lifecycle_action`.

- [ ] **Step 3: Implement the age guard**

In `main.py`, at the top of `_process_pre_tp1_or_backstop_tick` (after the `direction` assignment at line 4116 and **before** the `tick = await _mt5_call(executor.get_tick, symbol)` call at line 4118), insert:

```python
    # Trade Lifecycle Guard: age-based de-risk / flatten, evaluated every second.
    # Runs before the TP1 branches so an aged trade is de-risked first.
    lifecycle_pol = read_lifecycle_policy(config)
    if lifecycle_pol.lifecycle_enabled:
        now_utc = datetime.now(timezone.utc)
        server_offset = 0
        try:
            server_offset = int(await _mt5_call(executor.get_server_time_offset_seconds) or 0)
        except Exception:
            server_offset = 0
        age_res = evaluate_trade_age(sig, now_utc, lifecycle_pol, server_offset)
        action = str(age_res.get("action") or "NONE")
        stamp_key = {
            "DERISK": "lifecycle_derisk_at",
            "FLATTEN": "lifecycle_flatten_at",
            "SESSION_FLATTEN": "lifecycle_flatten_at",
            "EXPIRE_PENDING": "lifecycle_pending_expired_at",
        }.get(action)
        already_done = bool(sig.get(stamp_key)) if stamp_key else False
        if action != "NONE" and not already_done:
            res: Optional[Dict[str, Any]] = None
            try:
                res = await _mt5_call(executor.apply_lifecycle_action, sig, action)
                sig[stamp_key] = now_utc.isoformat()
                sig["lifecycle_action"] = action
                sig["lifecycle_reason"] = age_res.get("reason")
                state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                logger.info(
                    f"[LIFECYCLE {action}] msg={msg_id} reason={age_res.get('reason')} "
                    f"result={res}"
                )
                if action in ("FLATTEN", "SESSION_FLATTEN", "EXPIRE_PENDING"):
                    sig["status"] = "CLOSED"
                    sig["closed_at"] = now_utc.isoformat()
                    state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                    await notify_saved_messages(
                        f"⏱ **Trade Lifecycle Guard — `{action}`**\n"
                        f"• Signal: `{msg_id}` ({direction} {symbol})\n"
                        f"• Reason: {age_res.get('reason')}\n"
                        f"• Closed: `{res.get('closed')}` | Cancelled: `{res.get('cancelled')}`"
                    )
                    return
            except Exception as exc:
                logger.warning(f"[LIFECYCLE {action}] msg={msg_id} failed: {exc}")

            if isinstance(res, dict) and res.get("gave_up"):
                await notify_saved_messages(
                    f"⚠️ **Trade Lifecycle Guard gave up** on signal `{msg_id}` ({direction} {symbol})\n"
                    f"• Reason: {age_res.get('reason')}\n"
                    f"• The position keeps its broker SL and the bot stops retrying "
                    f"({lifecycle_pol.max_close_retries} attempts)."
                )
                return
```

Add the required imports to `main.py`:

```python
from risk_lifecycle import evaluate_trade_age, read_lifecycle_policy
```

If `datetime` / `timezone` are not already imported in `main.py`, add `from datetime import datetime, timezone`.

- [ ] **Step 4: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 6: Commit**

```powershell
git add main.py tests/test_trade_lifecycle.py
git commit -m "feat(main): run the trade-age guard every second

_process_pre_tp1_or_backstop_tick now evaluates the age guard before the TP1
branches, so an aged trade is de-risked or flattened first. Each action is
stamped on the signal, which makes it idempotent across repeated ticks."
```

---

## Task 7: `main.py` — the `/rlock` and `/lifecycle` commands

**Files:**
- Modify: `main.py:7045-7049` (`_is_known_saved_command` list)
- Modify: `main.py:7350` (insert the new command handlers before the `# LIVE CONTROL: /floatcap` block at line 7350)
- Modify: `main.py:8433` (the command list in the `/status` reply)
- Modify: `tests/test_trade_lifecycle.py` (append the command tests)

**Interfaces:**
- Consumes: `risk_lifecycle.apply_rlock_command`, `risk_lifecycle.apply_lifecycle_command` (Task 1).
- Produces: the Saved Messages commands `/rlock` and `/lifecycle`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- Saved Messages commands ---------------------------------------------

class _MockEvent:
    def __init__(self, text: str):
        self.message = type("M", (), {"message": text, "out": True})()
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


@pytest.mark.asyncio
async def test_rlock_command_toggles_at_runtime(monkeypatch, tmp_path):
    import config
    import main

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/rlock on")
    await main.handle_saved_messages_command(ev)
    assert config.R_LOCK_ENABLED is True
    assert any("R-Relative Profit Floor" in r for r in ev.replies)

    ev2 = _MockEvent("/rlock off")
    await main.handle_saved_messages_command(ev2)
    assert config.R_LOCK_ENABLED is False


@pytest.mark.asyncio
async def test_lifecycle_command_toggles_at_runtime(monkeypatch, tmp_path):
    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/lifecycle on")
    await main.handle_saved_messages_command(ev)
    assert config.LIFECYCLE_ENABLED is True
    assert any("Trade Lifecycle Guard" in r for r in ev.replies)
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — the commands are not recognised.

- [ ] **Step 3: Implement the commands**

In `main.py`, insert the following block immediately **before** the `# LIVE CONTROL: /floatcap` comment (line 7350):

```python
    # LIVE CONTROL: /rlock [on|off] — toggle the R-relative profit floor
    if text in ("/rlock", "rlock") or any(
        text.startswith(p) for p in ("/rlock ", "rlock ")
    ):
        _cfg_mod = sys.modules.get("config") or config
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        changed, reply_msg = apply_rlock_command(_cfg_mod, [arg] if arg else [])
        if changed:
            logger.info(f"[/rlock] R_LOCK_ENABLED updated to {getattr(_cfg_mod, 'R_LOCK_ENABLED', False)}")
        await event.reply(reply_msg)
        return

    # LIVE CONTROL: /lifecycle [on|off] — toggle the trade-age guard
    if text in ("/lifecycle", "lifecycle") or any(
        text.startswith(p) for p in ("/lifecycle ", "lifecycle ")
    ):
        _cfg_mod = sys.modules.get("config") or config
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        changed, reply_msg = apply_lifecycle_command(_cfg_mod, [arg] if arg else [])
        if changed:
            logger.info(f"[/lifecycle] LIFECYCLE_ENABLED updated to {getattr(_cfg_mod, 'LIFECYCLE_ENABLED', False)}")
        await event.reply(reply_msg)
        return
```

Add the import at the top of `main.py`:

```python
from risk_lifecycle import apply_lifecycle_command, apply_rlock_command, evaluate_trade_age, read_lifecycle_policy
```

- [ ] **Step 4: Register the commands as known**

In `main.py`, in the `_is_known_saved_command` list (lines 7045-7049), add the new commands:

```python
    "/rlock", "rlock",
    "/lifecycle", "lifecycle",
```

- [ ] **Step 5: Add the commands to the `/status` help line**

In `main.py` line 8433, extend the command list with `/rlock` and `/lifecycle`:

```python
f"📱 **Commands:** `/health`, `/status`, `/resume`, `/bank_tp1`, `/bank`, `/lotcap`, `/losslimit`, `/floatcap`, `/dynamicrisk`, `/atrtrail`, `/rlock`, `/lifecycle`, `/trade`, `/sl`, `/tp`, `/trail`, `/be`, `/partial`, `dailywin`, `dailyloss`, `/set_daily_loss`, `/status_daily_loss`, `/closeall`, `/sim_tp1 1|2|3|4`, `/sim_shortfall`, `/sim_trail 2|3|4`, `/test`, `/testrunner`, `/testorder`"
```

- [ ] **Step 6: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 7: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 8: Commit**

```powershell
git add main.py tests/test_trade_lifecycle.py
git commit -m "feat(main): add /rlock and /lifecycle Saved Messages commands

Both toggle their feature at runtime with no restart, and double as the live
kill switches for the rollout."
```

---

## Task 8: `trade_ledger.py` — schema 4 with cash MFE, giveback, R and age

**Files:**
- Modify: `trade_ledger.py:456-488` (`build_trade_record`)
- Modify: `trade_ledger.py:502` (`append_trade` — the `schema_version` default)
- Modify: `trade_ledger.py` (import `risk_lifecycle.resolve_fill_time`)
- Modify: `tests/test_trade_lifecycle.py` (append the ledger tests)

**Interfaces:**
- Consumes: nothing from earlier tasks (independent), except `risk_lifecycle.resolve_fill_time` for the exit age (see the fill-time rule below).
- Produces: every closed-signal record gains `mfe_cash`, `giveback_cash`, `initial_r_per_oz`, `r_at_exit`, `exit_age_seconds`, `lifecycle_action`; `schema_version` becomes `4`.

**Fill-time rule:** the ledger records the RESOLVED fill time via
`risk_lifecycle.resolve_fill_time(signal_data)` — NEVER the raw
`signal_data["first_fill_at"]` key. The signal-level stamp records the first
`PENDING -> FILLED` transition, which for a normal zone signal is the first LIMIT fill
(L2) and is therefore LATER than the MARKET fill of L1 that is stamped on the layer.
`resolve_fill_time` returns the EARLIEST known fill, so `exit_age_seconds` measures the
TRADE and not its first limit fill.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- trade ledger schema 4 -----------------------------------------------

def test_build_trade_record_adds_cash_mfe_and_giveback():
    from trade_ledger import build_trade_record

    sig = {
        "timestamp": "2026-11-19T06:19:09+00:00",
        "chat_id": -100,
        "message_id": 42,
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "total_lots": 0.05,
        "filled_lots": 0.05,
        "layers": {
            "L1": {"ticket": 1, "status": "CLOSED", "lot": 0.01, "entry_price": 3740.0},
        },
        "max_favorable_excursion": {"peak_price": 3728.55, "peak_profit_per_oz": 11.12},
        "realized_pnl": 0.56,
        "initial_r_per_oz": 20.0,
        "first_fill_at": "2026-11-19T06:19:09+00:00",
        "closed_at": "2026-11-19T09:18:51+00:00",
    }

    rec = build_trade_record(sig, signal_key="-100:42", realized_pnl=0.56)

    assert rec["schema_version"] == 4
    # 11.12 $/oz * 0.05 lots * 100 oz/lot = 55.60
    assert rec["mfe_cash"] == pytest.approx(55.60)
    assert rec["giveback_cash"] == pytest.approx(55.04)
    assert rec["initial_r_per_oz"] == pytest.approx(20.0)
    assert rec["r_at_exit"] == pytest.approx(0.278)
    assert rec["exit_age_seconds"] == pytest.approx(10842, abs=2)
    assert rec["lifecycle_action"] is None


def test_build_trade_record_tolerates_missing_new_fields():
    from trade_ledger import build_trade_record

    rec = build_trade_record(
        {"chat_id": -100, "message_id": 7, "direction": "BUY"},
        signal_key="-100:7",
        realized_pnl=1.0,
    )
    assert rec["schema_version"] == 4
    assert rec["mfe_cash"] is None
    assert rec["giveback_cash"] is None
    assert rec["exit_age_seconds"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `schema_version` is `3` and the new keys are missing.

- [ ] **Step 3: Implement the schema-4 fields**

In `trade_ledger.py`, in `build_trade_record`, insert the computation block **before** the `record: Dict[str, Any] = {` assignment at line 456:

```python
    # --- Schema 4 telemetry: cash excursion, giveback, R and exit age -------
    # `max_favorable_excursion.peak_profit_per_oz` is a PER-OUNCE figure; it is
    # converted to cash here (contract = 100 oz per lot) so giveback is finally
    # measurable.
    CONTRACT_OZ_PER_LOT = 100.0
    mfe_per_oz = _safe_float((mfe_dict or {}).get("peak_profit_per_oz"), 0.0) if mfe_dict else 0.0
    lots_for_mfe = _safe_float(total_lots, 0.0)
    mfe_cash: Optional[float] = None
    if mfe_per_oz > 0.0 and lots_for_mfe > 0.0:
        mfe_cash = round(mfe_per_oz * lots_for_mfe * CONTRACT_OZ_PER_LOT, 2)

    realized_for_giveback = _safe_float(realized_pnl, 0.0)
    giveback_cash: Optional[float] = None
    if mfe_cash is not None:
        giveback_cash = round(mfe_cash - realized_for_giveback, 2)

    initial_r = _safe_float(signal_data.get("initial_r_per_oz"), 0.0)
    if initial_r <= 0.0:
        # Fallback: derive the signal's initial risk per ounce from its layers.
        _layer_risks = []
        for _lv in (signal_data.get("layers") or {}).values():
            if not isinstance(_lv, dict):
                continue
            _e = _safe_float(_lv.get("entry_price") or _lv.get("entry") or _lv.get("target_price"), 0.0)
            _s = _safe_float(_lv.get("initial_sl"), 0.0)
            if _e > 0.0 and _s > 0.0:
                _layer_risks.append(abs(_e - _s))
        if _layer_risks:
            initial_r = sum(_layer_risks) / len(_layer_risks)
    initial_r_per_oz: Optional[float] = round(initial_r, 4) if initial_r > 0.0 else None
    r_at_exit: Optional[float] = None
    if initial_r_per_oz and mfe_cash is not None and initial_r_per_oz > 0.0:
        risk_cash = initial_r_per_oz * lots_for_mfe * CONTRACT_OZ_PER_LOT
        if risk_cash > 0.0:
            r_at_exit = round(mfe_cash / risk_cash, 4)

    exit_age_seconds: Optional[int] = None
    # The RESOLVED fill time -- never the raw `first_fill_at` key. For a normal
    # zone signal L1 fills at MARKET (stamped on the LAYER) and L2-L4 later as
    # limits (stamped on the SIGNAL), so the raw signal-level stamp can be
    # LATER than the true first fill. `risk_lifecycle.resolve_fill_time`
    # returns the EARLIEST known fill.
    _filled_at = resolve_fill_time(signal_data)
    _closed_at = signal_data.get("closed_at") or datetime.now(timezone.utc).isoformat()
    if _filled_at and _closed_at:
        try:
            from datetime import datetime as _dt

            _f = _filled_at if isinstance(_filled_at, _dt) else _dt.fromisoformat(str(_filled_at).replace("Z", "+00:00"))
            _c = _dt.fromisoformat(str(_closed_at).replace("Z", "+00:00"))
            exit_age_seconds = int((_c - _f).total_seconds())
        except (TypeError, ValueError):
            exit_age_seconds = None

    lifecycle_action = signal_data.get("lifecycle_action")
```

Then add the new keys to the `record` dict (after `"giveback_from_peak": giveback_from_peak,` at line 479):

```python
        "mfe_cash": mfe_cash,
        "giveback_cash": giveback_cash,
        "initial_r_per_oz": initial_r_per_oz,
        "r_at_exit": r_at_exit,
        "exit_age_seconds": exit_age_seconds,
        "lifecycle_action": (str(lifecycle_action) if lifecycle_action else None),
```

And change `"schema_version": 3,` to `"schema_version": 4,` at line 457.

Also change the default in `append_trade` at line 502:

```python
        entry.setdefault("schema_version", 4)
```

> If `Optional` is not imported in `trade_ledger.py`, add it to the `typing` import.

- [ ] **Step 4: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 6: Commit**

```powershell
git add trade_ledger.py tests/test_trade_lifecycle.py
git commit -m "feat(trade_ledger): schema 4 - cash MFE, cash giveback, R at exit, exit age

max_favorable_excursion.peak_profit_per_oz is a per-ounce figure and was never
converted to cash, so no tool could see the giveback. Schema 4 records mfe_cash,
giveback_cash, initial_r_per_oz, r_at_exit, exit_age_seconds and
lifecycle_action. Older records are read unchanged."
```

---

## Task 9: `main.py` — the `/stats` command

**Files:**
- Modify: `main.py` (new module-level helper `_build_ledger_stats()` plus the `/stats` handler)
- Modify: `main.py:7045-7049` (register `/stats` as a known command)
- Modify: `main.py:8433` (add `/stats` to the help line)
- Modify: `tests/test_trade_lifecycle.py` (append the stats tests)

**Interfaces:**
- Consumes: `trade_ledger.jsonl` on disk (Task 8's records).
- Produces: `main._build_ledger_stats(records: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]` and the `/stats` Saved Messages command.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- /stats ---------------------------------------------------------------

def test_build_ledger_stats_summarises_pnl_and_giveback():
    import main

    records = [
        {
            "closed_at": "2026-11-19T09:00:00+00:00",
            "realized_pnl": 10.0,
            "exit_reason_code": "ATR_TRAIL",
            "mfe_cash": 30.0,
            "giveback_cash": 20.0,
            "exit_age_seconds": 7200,
            "lifecycle_action": None,
        },
        {
            "closed_at": "2026-11-19T10:00:00+00:00",
            "realized_pnl": -5.0,
            "exit_reason_code": "BROKER_SL",
            "mfe_cash": 12.0,
            "giveback_cash": 17.0,
            "exit_age_seconds": 90000,
            "lifecycle_action": "FLATTEN",
        },
    ]

    stats = main._build_ledger_stats(records)

    assert stats["count"] == 2
    assert stats["net_pnl"] == pytest.approx(5.0)
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(0.5)
    assert stats["total_giveback"] == pytest.approx(37.0)
    assert stats["max_exit_age_seconds"] == 90000
    assert stats["lifecycle_closes"] == 1
    assert stats["exit_reasons"]["ATR_TRAIL"] == 1
    assert stats["exit_reasons"]["BROKER_SL"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `AttributeError: module 'main' has no attribute '_build_ledger_stats'`.

- [ ] **Step 3: Implement the stats helper**

Add this module-level function to `main.py` (place it next to `_close_all_bot_exposure_and_signals` around line 1208):

```python
def _build_ledger_stats(records: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Aggregate closed-trade records into the `/stats` summary."""
    rows = [r for r in (records or []) if isinstance(r, dict)]
    now = now or datetime.now(timezone.utc)

    def _closed_dt(row: Dict[str, Any]) -> Optional[datetime]:
        raw = str(row.get("closed_at") or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    cutoff_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_7d = now - timedelta(days=7)

    def _in_window(row: Dict[str, Any], cutoff: datetime) -> bool:
        dt = _closed_dt(row)
        return bool(dt and dt >= cutoff)

    def _slice(cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
        if cutoff is None:
            return list(rows)
        return [r for r in rows if _in_window(r, cutoff)]

    def _aggregate(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
        pnls = [float(r.get("realized_pnl") or 0.0) for r in subset]
        wins = sum(1 for p in pnls if p > 0.0)
        losses = sum(1 for p in pnls if p < 0.0)
        givebacks = [float(r.get("giveback_cash")) for r in subset if r.get("giveback_cash") is not None]
        ages = [int(r.get("exit_age_seconds")) for r in subset if r.get("exit_age_seconds") is not None]
        reasons: Dict[str, int] = {}
        for r in subset:
            code = str(r.get("exit_reason_code") or "UNKNOWN")
            reasons[code] = reasons.get(code, 0) + 1
        return {
            "count": len(subset),
            "net_pnl": round(sum(pnls), 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(subset), 4) if subset else 0.0,
            "total_giveback": round(sum(givebacks), 2),
            "max_exit_age_seconds": max(ages) if ages else 0,
            "lifecycle_closes": sum(1 for r in subset if r.get("lifecycle_action")),
            "exit_reasons": reasons,
        }

    return {
        "today": _aggregate(_slice(cutoff_today)),
        "seven_day": _aggregate(_slice(cutoff_7d)),
        "all_time": _aggregate(rows),
    }
```

- [ ] **Step 4: Implement the `/stats` command**

Add the command handler in `handle_saved_messages_command`, immediately before the `# LIVE CONTROL: /floatcap` block (line 7350), alongside the `/rlock` and `/lifecycle` handlers added in Task 7:

```python
    # LIVE CONTROL: /stats — closed-trade analytics from the trade ledger
    if text in ("/stats", "stats"):
        _cfg_mod = sys.modules.get("config") or config
        ledger_path = Path(getattr(_cfg_mod, "TRADES_LEDGER_PATH", Path("trades_ledger.jsonl")))
        records: List[Dict[str, Any]] = []
        try:
            if ledger_path.exists():
                for line in ledger_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            logger.warning(f"[/stats] could not read ledger {ledger_path}: {exc}")

        if not records:
            await event.reply(f"📊 **Trade Stats**\n• No ledger data found at `{ledger_path}`.")
            return

        stats = _build_ledger_stats(records)
        today = stats["today"]
        seven = stats["seven_day"]
        allt = stats["all_time"]
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")

        def _line(label: str, agg: Dict[str, Any]) -> str:
            return (
                f"• **{label}:** `{agg['net_pnl']:+.2f} {curr}` | "
                f"`{agg['count']}` trades | win rate `{agg['win_rate'] * 100:.0f}%` | "
                f"giveback `{agg['total_giveback']:.2f} {curr}` | "
                f"max age `{agg['max_exit_age_seconds']}s`\n"
            )

        await event.reply(
            "📊 **Trade Stats**\n"
            + _line("Today", today)
            + _line("7 days", seven)
            + _line("All time", allt)
            + f"• Lifecycle closes: `{allt['lifecycle_closes']}`\n"
            + f"• Exit reasons: `{allt['exit_reasons']}`\n\n"
            f"{fin['formatted']}"
        )
        return
```

Ensure `json`, `Path`, `List`, `Dict`, `Any`, `Optional`, `timedelta` are imported in `main.py` (add any that are missing).

- [ ] **Step 5: Register `/stats` and add it to the help line**

In `main.py`, add `"/stats", "stats",` to the `_is_known_saved_command` list (lines 7045-7049), and add `/stats` to the help line at line 8433.

- [ ] **Step 6: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 7: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 8: Commit**

```powershell
git add main.py tests/test_trade_lifecycle.py
git commit -m "feat(main): add /stats - closed-trade analytics from the ledger

Reports net P&L, win rate, total cash giveback, longest trade age and the exit
reason mix for today, 7 days and all time. This is the instrument that makes
the giveback measurable rather than anecdotal."
```

---

## Task 10: `scripts/ab_replay_proof.py` — Arm C

**Files:**
- Modify: `scripts/ab_replay_proof.py:52` (`ARM_B_CALIBRATED_PARAMS` — add the Arm C params)
- Modify: `scripts/ab_replay_proof.py:792-814` (`_pin_arm_b_params` — add `_pin_arm_c_params`)
- Modify: `scripts/ab_replay_proof.py:993-1022` (`_run_worker_main` — handle `arm_mode == "c"`)
- Modify: `scripts/ab_replay_proof.py:1105-1126` (`_run_arm_subprocess` — accept `"c"`)
- Modify: `scripts/ab_replay_proof.py:1154-1190` (`_evaluate_gates` — add the C-vs-B gate)
- Modify: `scripts/ab_replay_proof.py:1275-1326` (`main` — add the `--arm c` branch)
- Modify: `scripts/ab_replay_proof.py:1192-1273` (`_render_markdown` — render Arm C)

**Interfaces:**
- Consumes: every feature from Tasks 1-9.
- Produces: `--arm c` runs the same 12 recorded trades with the R lock and the age guard enabled, and the gate `g5` asserts Arm C ≥ Arm B on net P&L and ≤ Arm B on tail loss.

- [ ] **Step 1: Add the Arm C parameter block**

In `scripts/ab_replay_proof.py`, immediately after the `ARM_B_CALIBRATED_PARAMS` dict (which starts at line 52), add:

```python
# Arm C params: Arm B plus the R-relative profit floor and the trade-age guard.
ARM_C_CALIBRATED_PARAMS: Dict[str, Any] = {
    "R_LOCK_ENABLED": True,
    "LIFECYCLE_ENABLED": True,
    "LIFECYCLE_DERISK_SECONDS": 7200,
    "LIFECYCLE_FLATTEN_SECONDS": 21600,
    "LIFECYCLE_PENDING_EXPIRE_SECONDS": 43200,
    "LIFECYCLE_SESSION_FLATTEN_ENABLED": False,
}
```

- [ ] **Step 2: Add the Arm C pinning function**

In `scripts/ab_replay_proof.py`, immediately after `_pin_arm_b_params` (which ends at line 814), add:

```python
def _pin_arm_c_params(config_mod: Any, executor_mod: Any, main_mod: Any) -> None:
    """Explicitly pin and verify the Arm C lifecycle parameters across config, executor, and main."""
    targets = (config_mod, executor_mod, main_mod)
    for key, expected in ARM_C_CALIBRATED_PARAMS.items():
        for mod in targets:
            if hasattr(mod, key):
                setattr(mod, key, expected)
        actual = getattr(config_mod, key, None)
        if isinstance(expected, float):
            if abs(float(actual or 0.0) - expected) > 1e-9:
                raise RuntimeError(f"Arm C calibration mismatch for {key}: {actual} != {expected}")
        elif actual != expected:
            raise RuntimeError(f"Arm C calibration mismatch for {key}: {actual!r} != {expected!r}")
```

- [ ] **Step 3: Handle `arm_mode == "c"` in the worker**

In `scripts/ab_replay_proof.py`, inside `_run_worker_main` (line 993), change the branch at line 1019-1022:

```python
        if arm_mode == "a":
            if hasattr(config, "PROFIT_RATCHET_ENABLED"):
                raise RuntimeError("Arm A contamination: PROFIT_RATCHET_ENABLED found in 4544f7f worktree!")
        elif arm_mode == "b":
            _pin_arm_b_params(config, mt5_executor, main)
        else:
            _pin_arm_b_params(config, mt5_executor, main)
            _pin_arm_c_params(config, mt5_executor, main)
```

- [ ] **Step 4: Extend the gate evaluation**

In `scripts/ab_replay_proof.py`, in `_evaluate_gates` (line 1154), add the Arm C gate. Change the function signature to accept `arm_c` and add the gate:

```python
def _evaluate_gates(
    arm_a: Optional[Dict[str, Any]],
    arm_b: Optional[Dict[str, Any]],
    arm_c: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
```

and, after the existing `g4_single` computation, add:

```python
    if arm_c and arm_b:
        sc = arm_c["summary"]
        sb2 = arm_b["summary"]
        g5 = (
            sc["net_gbp"] >= sb2["net_gbp"]
            and sc["tail_loss_gbp_13078_9953"] <= sb2["tail_loss_gbp_13078_9953"]
            and sc["error_count"] == 0
        )
    else:
        g5 = None
```

Add `"g5": g5` to the returned gates dict.

> The arm summary key is **`tail_loss_gbp_13078_9953`** (see `_summarize_arm` in `scripts/ab_replay_proof.py:1092`). The other keys used above are `net_gbp` (:1089), `error_count` (:1100), `trade_count` (:1088), `choke_trio_gbp_13153_9934_13167` (:1093) and `unflagged_rollover_exits` (:1099).

- [ ] **Step 5: Extend `main()` and the markdown renderer**

In `scripts/ab_replay_proof.py` `main()` (line 1275):

```python
        if args.arm in ("a", "both"):
            arm_a_res = _run_arm_a_isolated(bars_file, io_dir / "arm_a.json")
        if args.arm in ("b", "both"):
            arm_b_res = _run_arm_subprocess("b", REPO_ROOT, bars_file, io_dir / "arm_b.json")
        if args.arm in ("c", "both"):
            arm_c_res = _run_arm_subprocess("c", REPO_ROOT, bars_file, io_dir / "arm_c.json")
```

declare `arm_c_res: Optional[Dict[str, Any]] = None` next to `arm_a_res` / `arm_b_res`, pass it into `_evaluate_gates(arm_a_res, arm_b_res, arm_c_res)`, and add `"arm_c": arm_c_res` to the report dict.

In `_render_markdown`, add an Arm C section after the existing Arm A/B table:

```python
    if arm_c:
        sc = arm_c["summary"]
        lines += [
            "",
            "### Arm C (v3.8.0 Trade Lifecycle Guard)",
            "",
            f"- **Net PnL:** `£{sc['net_gbp']}` (Arm B: `£{sb['net_gbp']}`)",
            f"- **Tail Loss (13078 + 9953):** `£{sc['tail_loss_gbp_13078_9953']}` "
            f"(Arm B: `£{sb['tail_loss_gbp_13078_9953']}`)",
        ]
```

- [ ] **Step 6: Run the replay proof**

```powershell
cd C:\Users\kambo\Code\telegram-mt5-copier
python scripts/ab_replay_proof.py --arm c
```

Expected: the script writes the Arm C report and prints the gate table. If the bars must be fetched from MT5, run it with the MT5 terminal open; if a saved bars JSON exists, pass it with the script's existing `--bars` option.

**If gate `g5` fails** (Arm C net P&L below Arm B, or a worse tail loss), do **not** enable the feature. Adjust `ARM_C_CALIBRATED_PARAMS` / `R_LOCK_TIERS` and re-run. Record the outcome in the commit message.

- [ ] **Step 7: Commit**

```powershell
git add scripts/ab_replay_proof.py
git commit -m "feat(replay): add Arm C (R lock + trade-age guard) to the A/B replay proof

Arm C replays the same 12 recorded trades with R_LOCK_ENABLED and
LIFECYCLE_ENABLED on, and gate g5 requires Arm C >= Arm B on net P&L and
<= Arm B on tail loss. This is the pre-live acceptance evidence."
```

---

## Task 11: Changelog, environment verification and final push

**Files:**
- Modify: `AUTOMATION_CHANGELOG.md` (prepend the v3.8.0 section)
- Modify: `.env.example` (verify the knob block is present — done in Task 1)

**Interfaces:**
- Consumes: everything.
- Produces: a verified, pushed release.

- [ ] **Step 1: Run the full verification suite**

```powershell
cd C:\Users\kambo\Code\telegram-mt5-copier
python -m pytest -q
python -c "import parser, mt5_executor, main, risk_lifecycle, config; print('IMPORT OK')"
python check_algo.py
python -c "import startup_checks, config; r = startup_checks.validate_environment(); print(r)"
python -c "import config; print('R_LOCK_ENABLED', config.R_LOCK_ENABLED); print('LIFECYCLE_ENABLED', config.LIFECYCLE_ENABLED); print('R_LOCK_TIERS', config.R_LOCK_TIERS)"
```

Expected:

- pytest: **0 failed**.
- `IMPORT OK`.
- `check_algo.py`: `ALL CHECKS PASSED`.
- `startup_checks.validate_environment()`: `{'ok': True, 'errors': [], 'warnings': []}`.
- `R_LOCK_ENABLED False` and `LIFECYCLE_ENABLED False` (both ship disabled).

- [ ] **Step 2: Confirm the working tree is clean of runtime artifacts**

```powershell
git status --porcelain
```

Expected: no `.env`, no `*.log`, no `signals_state*.json`, no `trades_ledger*.jsonl`, no `*.session`.

- [ ] **Step 3: Write the changelog entry**

Prepend to `AUTOMATION_CHANGELOG.md`:

```markdown
## [2026-11-20] — v3.8.0 Trade Lifecycle Guard (R-Relative Profit Floor + Trade-Age Guard)

- **Spec:** `docs/superpowers/specs/2026-11-20-trade-lifecycle-guard-design.md`
- **Plan:** `docs/superpowers/plans/2026-11-20-trade-lifecycle-guard.md`
- **Motivation:** the live ledger (19 closed signals, `dry_run=false`) showed `£154.93` of
  favourable excursion turning into `-£33.15` realized — `£188.08` given back, 121% of
  everything the market gave. Two trades held 21.0 h and 20.3 h (`-£105.43`, 61% of the
  total loss).

### 1. R-Relative Profit Floor (`risk_lifecycle.py`, `dynamic_risk.py`, `mt5_executor.py`)

- The ATR engine armed its breakeven lock at `max(0.75 x ATR, $3.50)` regardless of stop
  distance, so a `$22/oz` stop and a `$2.00` scalp both became risk-free after the same
  `$3.50`. The new floor scales to each trade's own risk `R = |entry - initial SL|`
  (tiers `0.5R -> entry`, `1.0R -> +0.25R`, `1.5R -> +0.50R`, `2.0R -> +0.75R`).
- `compute_dynamic_atr_trail_stop` gained an additive `r_lock_floor` keyword; the floor
  joins the existing candidate list, so the ATR trail still owns the tight stop and the
  forward-only (never-loosens) invariant is preserved.
- `initial_sl` is captured per layer at placement (the trail overwrites `sl`/`broker_sl`,
  so the original risk would otherwise be lost).

### 2. Trade-Age Guard (`risk_lifecycle.py`, `mt5_executor.py`, `main.py`)

- No trade-age, pending-expiry or session guard existed in the live code.
- Two-step de-risk: at `2 h` the unfilled pending limits are cancelled and every open layer
  moves to breakeven (never loosening an existing stop); at `6 h` everything is closed at
  market. Unfilled signals are cancelled at `12 h`.
- The session/rollover cut ships **disabled** (`LIFECYCLE_SESSION_FLATTEN_ENABLED=false`).

### 3. Instrumentation (`trade_ledger.py`)

- Ledger schema `3 -> 4`: `mfe_cash`, `giveback_cash`, `initial_r_per_oz`, `r_at_exit`,
  `exit_age_seconds`, `lifecycle_action`. `peak_profit_per_oz` is per-ounce and was never
  converted to cash, so giveback was not measurable.
- New `/stats` command: today / 7-day / all-time P&L, win rate, total cash giveback,
  longest trade age, exit-reason mix.

### 4. Live Control

- `/rlock on|off` and `/lifecycle on|off` toggle both features at runtime with no restart
  and double as the kill switches.

### 5. Deploy Readiness

- **Both features ship DISABLED** (`R_LOCK_ENABLED=false`, `LIFECYCLE_ENABLED=false`).
  A fresh clone behaves identically to v3.7.0.
- **No VPS deployment was performed.**
- **VPS `.env` drift is UNRESOLVED and must be corrected by hand at deploy time.** `.env` is
  gitignored, so the push cannot fix it. The knobs to add are: `R_LOCK_ENABLED`,
  `R_LOCK_TIERS`, `R_LOCK_MIN_R_DOLLARS`, `LIFECYCLE_ENABLED`, `LIFECYCLE_DERISK_SECONDS`,
  `LIFECYCLE_FLATTEN_SECONDS`, `LIFECYCLE_PENDING_EXPIRE_SECONDS`,
  `LIFECYCLE_SESSION_FLATTEN_ENABLED`, `LIFECYCLE_SESSION_FLATTEN_AT`,
  `LIFECYCLE_MAX_CLOSE_RETRIES`.
- The server still pins `DYNAMIC_ATR_TRAIL_MULT=1.50` while the repo says `1.40` — fix that
  at the same time.

```

- [ ] **Step 4: Commit and push**

```powershell
git add AUTOMATION_CHANGELOG.md
git commit -m "docs(changelog): v3.8.0 - Trade Lifecycle Guard (R floor + trade-age guard)"
git push origin main
git ls-remote origin main
```

Expected: `origin/main` points at the new commit and `git status -sb` shows `## main...origin/main` with nothing ahead or behind.

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| §4.2 `risk_lifecycle.py` policy + 7 functions | Task 1 |
| §4.3 10 config knobs + startup validation | Task 1 |
| §4.4 `r_lock_floor` candidate + tie-break | Task 2 |
| §4.4 `initial_sl` capture | Task 3 |
| §4.4 R floor wired into `evaluate_dynamic_atr_engine` | Task 4 |
| §4.5 age guard actions | Task 5 |
| §4.5 watcher integration | Task 6 |
| §4.5/§4.10 `/rlock` + `/lifecycle` | Task 7 |
| §4.8 ledger schema 4 | Task 8 |
| §4.9 `/stats` | Task 9 |
| §8.4 Arm C replay proof | Task 10 |
| §9 rollout + VPS `.env` drift note | Task 11 |
| §6 invariants 1-7 | Enforced in Tasks 2, 4, 5, 6; asserted in Tasks 2, 4, 5, 6 tests |
| §7 error handling (exception containment, retry counter, missing `initial_sl`, missing ledger) | Tasks 4, 5, 6, 9 |
| §9.6 doc refresh | **Out of scope by design** (spec §9.6) |

Gap check: the spec's `lifecycle_retry_count` state key (Error handling table) is **not** implemented by any task. Either add it to Task 5's `apply_lifecycle_flatten` (increment `signal_data["lifecycle_retry_count"]` on a failed close and refuse to retry past `policy.max_close_retries`) or remove the row from the spec. **Implement it in Task 5** — it is the difference between "retry forever" and "give up and alert".

**2. Placeholder scan** — no `TBD`, no `TODO`, no "similar to Task N", no "add appropriate error handling". Every code step carries real code. Two deliberate conditional instructions are marked as such: the `_env_choice` correction in Task 1 Step 4 (the helper cannot accept free-form strings) and the `tail_loss_gbp` key name in Task 10 Step 4 (the summary key must be confirmed against the real output).

**3. Type consistency**

- `LifecyclePolicy` field names are identical in Task 1 (definition), Task 4 (`_get_lifecycle_policy`), Task 5 (no field access), Task 6 (`read_lifecycle_policy`).
- `compute_r_lock_floor(direction, entry_price, initial_sl, mfe_dollars, live_price, policy, digits)` — Task 1 defines it, Task 4 calls it with exactly those keyword names (including `live_price=live_price`).
- `compute_dynamic_atr_trail_stop(..., r_lock_floor=None)` — Task 2 defines it, Task 4 passes `r_lock_floor=r_lock_floor`.
- `apply_lifecycle_action(signal_data, action)` — Task 5 defines it, Task 6 calls it with two positional args.
- `evaluate_trade_age(signal_data, now_utc, policy, server_offset_seconds)` — Task 1 defines it, Task 6 calls it with four positional args.
- Action strings `DERISK` / `FLATTEN` / `SESSION_FLATTEN` / `EXPIRE_PENDING` — identical in Tasks 1, 5, 6.
- `_build_ledger_stats(records)` — Task 9 defines it, its own test calls it with one arg.
- The ledger field names `mfe_cash`, `giveback_cash`, `initial_r_per_oz`, `r_at_exit`, `exit_age_seconds`, `lifecycle_action` — Task 8 writes them, Task 9 reads `giveback_cash`, `exit_age_seconds`, `lifecycle_action`, `exit_reason_code`, `realized_pnl`. Consistent.

**Gaps found in self-review and closed inline**

1. `lifecycle_retry_count` was specified but not implemented — **now implemented in Task 5** (`apply_lifecycle_flatten` increments it on a failed close and sets `gave_up` once it exceeds `policy.max_close_retries`).
2. `initial_r_per_oz` was read by the ledger but never written — **now written in Task 5** (`apply_lifecycle_derisk` averages the layers' `|entry - initial_sl|` onto the signal) **and** derived independently by the ledger in Task 8 from the layers, so the field is populated even when the guard never fires.
3. `closed_at` was read by the ledger but never stamped — **now stamped in Task 6** when the guard marks a signal `CLOSED`, **and** the ledger falls back to `datetime.now(timezone.utc)` when it is absent.

**Open items carried into execution**

1. `LIFECYCLE_MAX_CLOSE_RETRIES` is honoured by `apply_lifecycle_flatten` but the "gave up and alert the operator" notification is raised by the caller in `main.py` (Task 6) when `res.get("gave_up")` is true. Confirm the alert path is wired when Task 6 is implemented.
2. The `tail_loss_gbp` summary key in Task 10 Step 4 must be confirmed against the real `arm_b["summary"]` output before use.
