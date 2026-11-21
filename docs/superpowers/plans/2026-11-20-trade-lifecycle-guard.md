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
    derisk_after_seconds: int = 14400
    flatten_after_seconds: int = 28800
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

    derisk = max(0, _safe_int(getattr(cfg_mod, "LIFECYCLE_DERISK_SECONDS", 14400), 14400))
    flatten = max(0, _safe_int(getattr(cfg_mod, "LIFECYCLE_FLATTEN_SECONDS", 28800), 28800))
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

    Note: while the R lock is enabled the ``BE_LOCK`` candidate's THRESHOLD is
    R-gated (it arms at ``max(0.5 * R, 0.75 * ATR, $2.50)``), but its LEVEL
    stays the classic breakeven offset (``entry +/- $0.50``), so the R-gated BE
    lock is never less protective than the flat BE lock it replaced. The
    candidate picker keeps the tightest of the two: ``BE_LOCK`` wins at the
    0.5R tier (the R floor is only the entry) and ``R_LOCK`` wins from the
    1.0R tier upward (the R floor is ``entry + 0.25 * R``).
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
    "LIFECYCLE_DERISK_SECONDS", 14400, min_val=0, max_val=604800
)
LIFECYCLE_FLATTEN_SECONDS: int = _env_int(
    "LIFECYCLE_FLATTEN_SECONDS", 28800, min_val=0, max_val=604800
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

    derisk = getattr(cfg, "LIFECYCLE_DERISK_SECONDS", 14400)
    flatten = getattr(cfg, "LIFECYCLE_FLATTEN_SECONDS", 28800)
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
LIFECYCLE_DERISK_SECONDS=14400
LIFECYCLE_FLATTEN_SECONDS=28800
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
- Produces: `compute_dynamic_atr_trail_stop(..., *, r_lock_floor: Optional[float] = None, initial_r: Optional[float] = None) -> Dict[str, Any]` — the result dict gains `"r_lock_floor"`, which is the R floor **only when it won the picker** (`stop_source == "R_LOCK"`), otherwise `None`. Tie-break priority becomes `R_LOCK` > `MILESTONE_FLOOR` > `ATR_TRAIL` > `BE_LOCK`.
- Since v3.8.1 the signature also gains `initial_r` (the trade's initial risk per ounce, `|entry - initial SL|`). When it is a positive number the BE lock becomes **R-gated**: it waits for `max(0.5R, 0.75 x ATR, $2.50)` of MFE instead of the flat `max(0.75 x ATR, $3.50)`, so a wide-stop trade is not locked at breakeven long before the R ladder could matter. The gate changes only WHEN the BE lock arms, never WHERE it locks: the stop stays at the classic breakeven offset (`entry +/- $0.50`), so the R-gated path is never less protective than the flat BE lock it replaced. The picker keeps the tightest candidate, so `BE_LOCK` wins at the 0.5R tier (the R floor is only the entry) and `R_LOCK` wins from the 1.0R tier upward (the R floor is `entry + 0.25R`). `initial_r=None` (the default) keeps the behaviour byte-for-byte.

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


# --- v3.8.1: the R-gated BE lock ----------------------------------------


def _gated_stop(**overrides):
    """Call the trail engine with an explicit policy and overridable inputs."""
    import dynamic_risk

    params = {
        "direction": "BUY",
        "entry_price": 3900.0,
        "live_price": 3905.0,
        "prev_peak": None,
        "current_sl": 3880.0,
        "atr": 4.0,
        "policy": _atr_policy(),
        "digits": 2,
    }
    params.update(overrides)
    return dynamic_risk.compute_dynamic_atr_trail_stop(**params)


def test_be_lock_stays_flat_when_the_initial_risk_is_unknown():
    """Without a valid initial R the BE lock must behave exactly as it always has."""
    flat = _gated_stop(live_price=3905.0)          # MFE 5.00, ATR 4.00
    assert flat["be_armed"] is True
    assert flat["be_sl"] == pytest.approx(3900.50)   # entry + $0.50
    assert flat["stop_source"] == "BE_LOCK"

    for bad in (None, 0.0, -5.0):
        res = _gated_stop(live_price=3905.0, initial_r=bad)
        assert res["be_armed"] is flat["be_armed"]
        assert res["be_sl"] == flat["be_sl"]
        assert res["stop_source"] == flat["stop_source"]
        assert res["candidate_sl"] == flat["candidate_sl"]


def test_gated_be_lock_waits_for_half_the_initial_risk():
    """A wide-stop trade (R = $22) must not be BE-locked at $3.50 of MFE."""
    flat = _gated_stop(live_price=3905.0)                 # MFE 5.00
    gated = _gated_stop(live_price=3905.0, initial_r=22.0)

    assert flat["be_armed"] is True
    assert flat["stop_source"] == "BE_LOCK"

    # The gated threshold is max(0.5 * 22.00, 0.75 * 4.00, 2.50) = $11.00
    assert gated["be_armed"] is False
    assert gated["be_sl"] is None
    assert gated["stop_source"] == "ATR_TRAIL"


@pytest.mark.parametrize(
    "initial_r,atr,mfe,armed",
    [
        (22.0, 4.0, 10.99, False),    # 0.5R = $11.00 dominates
        (22.0, 4.0, 11.00, True),
        (3.0, 20.0, 14.99, False),    # 0.75 * ATR = $15.00 dominates
        (3.0, 20.0, 15.00, True),
        (3.0, 2.0, 2.49, False),      # the $2.50 trail minimum dominates
        (3.0, 2.0, 2.50, True),
    ],
)
def test_gated_be_threshold_is_half_r_the_atr_multiple_or_the_trail_minimum(
    initial_r, atr, mfe, armed
):
    """The gated threshold is max(0.5R, 0.75 * ATR, $2.50) - whichever is largest."""
    res = _gated_stop(
        entry_price=3900.0,
        live_price=3900.0 + mfe,
        atr=atr,
        initial_r=initial_r,
    )
    assert res["be_armed"] is armed


def test_gated_be_lock_keeps_the_classic_level_and_the_be_lock_wins_at_half_r():
    """The R gate changes WHEN the BE lock arms, never WHERE it locks.

    At the 0.5R tier the R floor is only the entry, so the classic breakeven
    stop (entry + $0.50) is the tighter -- and therefore the winning --
    candidate.
    """
    res = _gated_stop(
        entry_price=3900.0,
        live_price=3911.0,        # MFE 11.00 = 0.5R
        atr=12.0,                 # the ATR trail is not armed yet
        current_sl=3878.0,
        initial_r=22.0,
        r_lock_floor=3900.0,      # the 0.5R tier locks the stop at the entry
    )
    assert res["be_armed"] is True
    assert res["be_sl"] == pytest.approx(3900.50)     # the classic breakeven level
    assert res["stop_source"] == "BE_LOCK"           # tighter than the R floor
    assert res["candidate_sl"] == pytest.approx(3900.50)
    assert res["r_lock_floor"] is None               # the R lock did not win


def test_gated_be_lock_at_a_higher_tier_is_tighter_than_the_flat_be_stop():
    """At 1.0R the R floor (entry + 0.25R) is tighter than the flat BE stop."""
    res = _gated_stop(
        entry_price=3900.0,
        live_price=3922.0,        # MFE 22.00 = 1.0R
        atr=23.0,                 # the ATR trail is not armed yet
        current_sl=3878.0,
        initial_r=22.0,
        r_lock_floor=3905.5,      # the 1.0R tier: entry + 0.25 * R
    )
    assert res["be_armed"] is True
    assert res["stop_source"] == "R_LOCK"
    assert res["candidate_sl"] == pytest.approx(3905.5)
    assert res["candidate_sl"] > 3900.50             # tighter than the flat BE stop


def test_gated_be_lock_never_lowers_the_stop_that_is_already_in_place():
    """The forward-only invariant survives the gate."""
    res = _gated_stop(
        entry_price=3900.0,
        live_price=3911.0,
        atr=12.0,
        current_sl=3900.50,       # already at the flat breakeven stop
        initial_r=22.0,
        r_lock_floor=3900.0,
    )
    assert res["candidate_sl"] == pytest.approx(3900.50)
    assert res["should_update"] is False


def test_gated_be_lock_is_mirrored_for_a_sell():
    res = _gated_stop(
        direction="SELL",
        entry_price=3900.0,
        live_price=3889.0,        # MFE 11.00 = 0.5R
        atr=12.0,
        current_sl=3922.0,
        initial_r=22.0,
        r_lock_floor=3900.0,      # the 0.5R tier locks the stop at the entry
    )
    assert res["be_armed"] is True
    assert res["be_sl"] == pytest.approx(3899.50)     # entry - $0.50 (SELL)
    assert res["stop_source"] == "BE_LOCK"           # tighter than the R floor
    assert res["candidate_sl"] == pytest.approx(3899.50)


def test_gated_be_armed_flag_still_drives_the_pending_sweep():
    """The BE arming flag still drives the pending sweep, gated or not."""
    below = _gated_stop(live_price=3905.0, initial_r=22.0)
    assert below["be_armed"] is False
    assert below["should_sweep_pending"] is False

    at = _gated_stop(
        live_price=3911.0,
        atr=12.0,
        initial_r=22.0,
        r_lock_floor=3900.0,
    )
    assert at["be_armed"] is True
    assert at["should_sweep_pending"] is True


def test_gated_be_lock_never_produces_a_stop_on_the_wrong_side_of_the_current_stop():
    """No gated case may propose a stop looser than the one already in place."""
    for direction in ("BUY", "SELL"):
        for atr in (2.0, 4.0, 12.0, 23.0):
            for mfe in (0.5, 2.5, 5.0, 11.0, 22.0):
                for current_sl in (0.0, 3878.0, 3900.50, 3922.0):
                    entry = 3900.0
                    live = entry + mfe if direction == "BUY" else entry - mfe
                    res = _gated_stop(
                        direction=direction,
                        entry_price=entry,
                        live_price=live,
                        atr=atr,
                        current_sl=current_sl,
                        initial_r=22.0,
                        r_lock_floor=entry,
                    )
                    cand = res["candidate_sl"]
                    if cand is None or current_sl <= 0.0:
                        continue
                    if direction == "BUY":
                        assert cand >= current_sl or res["should_update"] is False
                    else:
                        assert cand <= current_sl or res["should_update"] is False


# --- executor: the R-gated BE lock ---------------------------------------

def _gated_executor(monkeypatch, atr, r_lock_enabled):
    """Build a dry-run executor with the R lock on or off."""
    import config
    import mt5_executor
    from mt5_executor import MT5Executor

    monkeypatch.setattr(config, "R_LOCK_ENABLED", r_lock_enabled)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", r_lock_enabled)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = atr
    return ex


def test_executor_passes_the_initial_r_only_when_the_r_lock_is_enabled(monkeypatch):
    """With the R lock off the engine must not receive an initial R at all."""
    import mt5_executor
    from types import SimpleNamespace

    captured = []
    original = mt5_executor.compute_dynamic_atr_trail_stop

    def spy(**kwargs):
        captured.append(kwargs.get("initial_r"))
        return original(**kwargs)

    monkeypatch.setattr(mt5_executor, "compute_dynamic_atr_trail_stop", spy)

    ex = _gated_executor(monkeypatch, atr=20.0, r_lock_enabled=True)
    sig = _atr_signal(3895.0, 3880.0)          # R = 15.0
    ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    assert captured[-1] == pytest.approx(15.0)

    ex_off = _gated_executor(monkeypatch, atr=20.0, r_lock_enabled=False)
    sig_off = _atr_signal(3895.0, 3880.0)
    ex_off.evaluate_dynamic_atr_engine(sig_off, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    assert captured[-1] is None


def test_executor_does_not_be_lock_a_wide_stop_before_half_r(monkeypatch):
    """A wide stop (R = $22) must not be BE-locked before MFE $11."""
    from types import SimpleNamespace

    ex = _gated_executor(monkeypatch, atr=4.0, r_lock_enabled=True)
    sig = _atr_signal(3900.0, 3878.0)          # R = 22.0
    ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3905.0, ask=3905.2, time=1700000000.0))

    # The flat BE lock would have moved the stop to $3900.50 at MFE $5.00.
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3899.40)
    assert sig.get("active_stop_source") == "ATR_TRAIL"
    assert not sig["layers"]["L1"].get("be_locked")


def test_executor_keeps_the_flat_be_lock_when_the_r_lock_is_off(monkeypatch):
    """The control case: with the R lock off the flat BE lock still arms."""
    from types import SimpleNamespace

    ex = _gated_executor(monkeypatch, atr=4.0, r_lock_enabled=False)
    sig = _atr_signal(3900.0, 3878.0)          # R = 22.0
    ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3905.0, ask=3905.2, time=1700000000.0))

    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3900.50)
    assert sig.get("active_stop_source") == "BE_LOCK"


def test_executor_r_lock_wins_the_picker_from_half_r_upward(monkeypatch):
    """With a wide ATR the R ladder owns the stop from the 0.5R tier upward."""
    from types import SimpleNamespace

    ex = _gated_executor(monkeypatch, atr=23.0, r_lock_enabled=True)

    half_r = _atr_signal(3900.0, 3878.0)       # R = 22.0, MFE 11.00 = 0.5R
    ex.evaluate_dynamic_atr_engine(half_r, tick=SimpleNamespace(bid=3911.0, ask=3911.2, time=1700000000.0))
    assert half_r.get("active_stop_source") == "R_LOCK"
    assert half_r["layers"]["L1"]["broker_sl"] == pytest.approx(3900.0)     # entry

    one_r = _atr_signal(3900.0, 3878.0)        # R = 22.0, MFE 22.00 = 1.0R
    ex.evaluate_dynamic_atr_engine(one_r, tick=SimpleNamespace(bid=3922.0, ask=3922.2, time=1700000000.0))
    assert one_r.get("active_stop_source") == "R_LOCK"
    assert one_r["layers"]["L1"]["broker_sl"] == pytest.approx(3905.5)      # entry + 0.25R
    # the BE lock keeps the classic breakeven level; the R ladder only changed
    # WHEN it arms, so the R_LOCK candidate is the tighter of the two
    assert one_r["layers"]["L1"]["be_sl"] == pytest.approx(3900.50)

# --- v3.8.1 (F4): the R-gated BE lock must never be less protective --------

_GRID_ENTRY = 3900.0
_GRID_CLASSIC_OFFSET = 0.50
_GRID_R = (2.0, 3.0, 5.0, 10.0, 22.0, 40.0)
_GRID_ATR = (2.0, 4.0, 12.0, 23.0)
_GRID_MFE = (0.5, 2.5, 3.5, 5.0, 11.0, 22.0, 44.0)


def _gated_grid_stop(direction, entry, live, atr, current_sl, initial_r, digits=2):
    """Engine stop for a grid case, with the R lock enabled end to end."""
    import dynamic_risk
    import risk_lifecycle as rl

    sign = 1.0 if direction == "BUY" else -1.0
    floor = rl.compute_r_lock_floor(
        direction=direction,
        entry_price=entry,
        initial_sl=entry - sign * initial_r,
        mfe_dollars=sign * (live - entry),
        live_price=live,
        policy=rl.LifecyclePolicy(r_lock_enabled=True),
        digits=digits,
    )
    return dynamic_risk.compute_dynamic_atr_trail_stop(
        direction=direction,
        entry_price=entry,
        live_price=live,
        prev_peak=None,
        current_sl=current_sl,
        atr=atr,
        policy=_atr_policy(),
        digits=digits,
        r_lock_floor=floor,
        initial_r=initial_r,
    )


def test_the_r_gated_be_lock_is_never_less_protective_than_the_classic_be_lock():
    """The R gate may only DELAY the BE lock; its LEVEL stays the classic one.

    The classic BE lock sits at entry + $0.50 (BUY) / entry - $0.50 (SELL).
    The R-gated BE lock must never place a stop below (BUY) / above (SELL)
    that level, otherwise arming the R-gated path would loosen the stop it
    replaced.
    """
    import dynamic_risk

    entry = _GRID_ENTRY
    armed = 0
    looser = 0
    tighter = 0
    for direction in ("BUY", "SELL"):
        sign = 1.0 if direction == "BUY" else -1.0
        classic_level = entry + sign * _GRID_CLASSIC_OFFSET
        for initial_r in _GRID_R:
            for atr in _GRID_ATR:
                for mfe in _GRID_MFE:
                    live = entry + sign * mfe
                    gated = _gated_grid_stop(direction, entry, live, atr, 0.0, initial_r)
                    classic = dynamic_risk.compute_dynamic_atr_trail_stop(
                        direction=direction,
                        entry_price=entry,
                        live_price=live,
                        prev_peak=None,
                        current_sl=0.0,
                        atr=atr,
                        policy=_atr_policy(),
                        digits=2,
                    )
                    if not gated["be_armed"]:
                        continue
                    armed += 1
                    be_sl = gated["be_sl"]
                    assert be_sl is not None
                    if sign > 0:
                        assert be_sl >= classic_level - 1e-9, (direction, initial_r, atr, mfe, be_sl)
                        assert gated["candidate_sl"] >= classic_level - 1e-9
                    else:
                        assert be_sl <= classic_level + 1e-9, (direction, initial_r, atr, mfe, be_sl)
                        assert gated["candidate_sl"] <= classic_level + 1e-9
                    if not classic["be_armed"]:
                        continue
                    if sign > 0:
                        if be_sl > classic["be_sl"] + 1e-9:
                            tighter += 1
                        elif be_sl < classic["be_sl"] - 1e-9:
                            looser += 1
                    else:
                        if be_sl < classic["be_sl"] - 1e-9:
                            tighter += 1
                        elif be_sl > classic["be_sl"] + 1e-9:
                            looser += 1
    assert armed > 0
    assert looser == 0
    assert tighter == 0


def test_the_r_gated_be_lock_arms_at_the_r_threshold_and_not_at_the_flat_3_50():
    """A wide-stop trade (R = $22) must not be BE-locked at the old flat $3.50."""
    classic = _gated_stop(live_price=3903.50, atr=4.0)
    assert classic["be_armed"] is True              # the flat lock arms at $3.50
    assert classic["be_sl"] == pytest.approx(3900.50)

    gated = _gated_stop(live_price=3903.50, atr=4.0, initial_r=22.0)
    assert gated["be_armed"] is False                # 0.5 * 22.00 = $11.00 dominates
    assert gated["be_sl"] is None

    at = _gated_stop(live_price=3911.0, atr=4.0, initial_r=22.0)
    assert at["be_armed"] is True
    assert at["be_sl"] == pytest.approx(3900.50)     # the classic level, unchanged


def test_the_be_lock_wins_the_picker_at_the_half_r_tier():
    """At 0.5R the classic BE stop is tighter than the R floor, so it wins."""
    half_r = _gated_stop(
        entry_price=3900.0,
        live_price=3911.0,        # MFE 11.00 = 0.5R
        atr=12.0,                 # the ATR trail is not armed yet
        current_sl=3878.0,
        initial_r=22.0,
        r_lock_floor=3900.0,      # the 0.5R tier: the R floor is the entry
    )
    assert half_r["be_armed"] is True
    assert half_r["be_sl"] == pytest.approx(3900.50)   # the classic level
    assert half_r["stop_source"] == "BE_LOCK"           # tighter than the R floor
    assert half_r["candidate_sl"] == pytest.approx(3900.50)
    assert half_r["r_lock_floor"] is None               # the R lock did not win


def test_the_r_lock_wins_the_picker_from_the_one_r_tier_upward():
    """From 1.0R the R floor (entry + 0.25R) is tighter than the classic stop."""
    one_r = _gated_stop(
        entry_price=3900.0,
        live_price=3922.0,        # MFE 22.00 = 1.0R
        atr=23.0,                 # the ATR trail is not armed yet
        current_sl=3878.0,
        initial_r=22.0,
        r_lock_floor=3905.5,      # the 1.0R tier: entry + 0.25 * R
    )
    assert one_r["be_armed"] is True
    assert one_r["be_sl"] == pytest.approx(3900.50)     # the classic level
    assert one_r["stop_source"] == "R_LOCK"
    assert one_r["candidate_sl"] == pytest.approx(3905.5)
    assert one_r["r_lock_floor"] == pytest.approx(3905.5)


def test_at_one_r_the_r_floor_beats_the_classic_be_stop_on_a_wide_stop():
    """R = $22, ATR = $12, MFE = $22: the R floor is tighter than the BE stop.

    The ATR trail is tighter still at this ATR, so the effective stop is at
    least as tight as the R floor -- but the R floor has already overtaken the
    classic BE lock, which is what the picker compares here.
    """
    res = _gated_stop(
        entry_price=3900.0,
        live_price=3922.0,        # MFE 22.00 = 1.0R
        atr=12.0,
        current_sl=3878.0,
        initial_r=22.0,
        r_lock_floor=3905.5,      # the 1.0R tier: entry + 0.25 * R
    )
    assert res["be_armed"] is True
    assert res["be_sl"] == pytest.approx(3900.50)      # the classic BE level
    assert res["stop_source"] != "BE_LOCK"             # the R floor is tighter
    assert res["candidate_sl"] >= 3905.5               # never looser than the R floor
    assert res["candidate_sl"] > 3900.50               # tighter than the classic BE stop


def test_the_r_lock_disabled_differential_is_zero_in_the_pure_engine():
    """With the R lock off the engine is byte-for-byte the pre-R-lock engine."""
    import dynamic_risk

    pol = _atr_policy()
    entry = _GRID_ENTRY
    differences = 0
    cases = 0
    for direction in ("BUY", "SELL"):
        sign = 1.0 if direction == "BUY" else -1.0
        for atr in _GRID_ATR:
            for mfe in _GRID_MFE:
                live = entry + sign * mfe
                for current_sl in (0.0, 3878.0, 3900.50):
                    baseline = dynamic_risk.compute_dynamic_atr_trail_stop(
                        direction=direction,
                        entry_price=entry,
                        live_price=live,
                        prev_peak=None,
                        current_sl=current_sl,
                        atr=atr,
                        policy=pol,
                        digits=2,
                    )
                    disabled = dynamic_risk.compute_dynamic_atr_trail_stop(
                        direction=direction,
                        entry_price=entry,
                        live_price=live,
                        prev_peak=None,
                        current_sl=current_sl,
                        atr=atr,
                        policy=pol,
                        digits=2,
                        r_lock_floor=None,
                        initial_r=None,
                    )
                    cases += 1
                    if baseline != disabled:
                        differences += 1
    assert cases > 0
    assert differences == 0


def test_the_r_lock_disabled_differential_is_zero_in_the_executor(monkeypatch):
    """With the R lock off the executor feeds the engine no R-lock input."""
    import config
    import mt5_executor
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    monkeypatch.setattr(mt5_executor, "R_LOCK_ENABLED", False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)

    captured = []
    original = mt5_executor.compute_dynamic_atr_trail_stop

    def spy(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(mt5_executor, "compute_dynamic_atr_trail_stop", spy)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 4.0

    differences = 0
    cases = 0
    for entry, sl, bid in (
        (3895.0, 3880.0, 3906.0),
        (3900.0, 3888.0, 3905.0),
        (3900.0, 3890.0, 3892.0),
        (3900.0, 3878.0, 3911.0),
    ):
        sig = _atr_signal(entry, sl)
        ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=bid, ask=bid + 0.2, time=1700000000.0))
        cases += 1
        kwargs = captured[-1]
        # the disabled executor never supplies an R-lock input
        if kwargs.get("initial_r") is not None or kwargs.get("r_lock_floor") is not None:
            differences += 1
        # and the stop it leaves in place is the engine own candidate
        reference = original(**kwargs)
        applied = sig["layers"]["L1"]["broker_sl"]
        cand = reference.get("candidate_sl")
        allowed = {round(float(sl), 2)}
        if cand is not None:
            allowed.add(round(float(cand), 2))
        if round(float(applied), 2) not in allowed:
            differences += 1
    assert cases > 0
    assert differences == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — `TypeError: compute_dynamic_atr_trail_stop() got an unexpected keyword argument 'r_lock_floor'` (and `'initial_r'` for the v3.8.1 gate tests).

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
    initial_r: Optional[float] = None,
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

    `initial_r` is the trade's initial risk per ounce (`|entry - initial SL|`).
    When it is a positive number the BE lock becomes **R-gated**: it then waits
    for `max(0.5 * R, 0.75 * ATR, $2.50)` of favourable excursion instead of the
    flat `max(0.75 * ATR, $3.50)`, so a wide-stop trade is not locked at
    breakeven long before the R ladder could matter. The gate changes only
    WHEN the BE lock arms, never WHERE it locks: the stop stays at the classic
    breakeven offset (`entry +/- $0.50`), so the R-gated path can never be less
    protective than the flat BE lock it replaced. The picker keeps the tightest
    candidate, so `BE_LOCK` wins at the 0.5R tier (the R floor is only `entry`)
    and `R_LOCK` wins from the 1.0R tier upward (the R floor is
    `entry + 0.25 * R`). When `initial_r` is `None` or non-positive the
    behaviour is unchanged.
    """
```

Then replace the BE block (the old lines 535-538) with the R-gated version, and move the R-floor validation above it — the gate needs the *validated* floor, and the `R_LOCK` candidate joins the picker beside the (unchanged) classic BE candidate, so the picker keeps the tightest of the two:

```python
    # Validate the R floor ONCE, defensively, and reuse the validated value in
    # the candidate list and in every return path. The candidate guard must
    # never raise on a value the echo would happily `float()`.
    r_lock_val = _safe_float(r_lock_floor, 0.0)
    r_lock_candidate: Optional[float] = round(r_lock_val, digits) if r_lock_val > 0.0 else None

    # The BE lock is R-gated when the trade's own initial risk is known. The
    # gate only ever DELAYS the flat breakeven lock until the trade has earned
    # half of its own risk, so it can never loosen a stop.
    r_val = _safe_float(initial_r, 0.0)
    r_gated = r_val > 0.0
    if r_gated:
        # The BE lock waits for half the trade's own risk, and never arms closer
        # than the trail's minimum distance ($2.50).
        be_threshold = max(
            0.5 * r_val,
            max(0.0, policy.be_activation_atr_mult) * atr_f,
            max(0.0, policy.trail_min_dollars),
        )
    else:
        be_threshold = max(max(0.0, policy.be_activation_atr_mult) * atr_f, max(0.0, policy.be_min_mfe_dollars))
    be_armed = mfe + 1e-9 >= be_threshold
    if not be_armed:
        be_sl = None
    else:
        # The R gate only changes WHEN the BE lock arms, never WHERE it locks.
        # The stop stays at the classic breakeven offset, so the R-gated path can
        # never be less protective than the flat BE lock it replaced.
        be_sl = round(entry_price + sign * max(0.0, policy.be_lock_offset_dollars), digits)
    should_sweep_pending = be_armed
```

The candidate list itself stays exactly as it is:

```python
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
- Consumes: `risk_lifecycle.read_lifecycle_policy`, `risk_lifecycle.compute_r_lock_floor`, `risk_lifecycle.compute_initial_r`, `LifecyclePolicy` (Task 1); `compute_dynamic_atr_trail_stop(..., r_lock_floor=..., initial_r=...)` (Task 2); `layers[...]["initial_sl"]` (Task 3).
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
    ACTION_DERISK,
    ACTION_EXPIRE_PENDING,
    ACTION_FLATTEN,
    ACTION_NONE,
    ACTION_SESSION_FLATTEN,
    LifecyclePolicy,
    compute_initial_r,
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
            initial_r = None
            lifecycle_pol = self._get_lifecycle_policy()
            initial_sl = _num(lv.get("initial_sl"), 0.0)
            if lifecycle_pol.r_lock_enabled and initial_sl > 0.0:
                # The trade's own initial risk per ounce. It gates the BE lock,
                # so a wide stop is not BE-locked long before the R ladder could
                # matter. None keeps the flat (pre-R) behaviour byte-for-byte.
                initial_r = compute_initial_r(entry_px, initial_sl)
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

Since v3.8.1 the same block also derives the layer's `initial_r` (`|entry - initial SL|`) and
hands it to the trail engine, which uses it to gate the BE lock (`max(0.5R, 0.75 x ATR,
$2.50)` instead of the flat `max(0.75 x ATR, $3.50)`) and to let the R ladder replace the
flat BE candidate. It is only derived while `R_LOCK_ENABLED` is true, so the shipped
default passes `initial_r=None` and the engine behaves byte-for-byte as before — verified
by a before/after differential over 2,304 pure-engine cases (0 differences) and 144
executor cases (0 differences).

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
                initial_r=initial_r,
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


# --- fix round 1 (Task 5): the fail-closed guards ---------------------------


def _live_lifecycle_executor():
    """A LIVE executor: the market-side guards only run against a broker."""
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=False)
    ex._connected = True
    return ex


def test_apply_lifecycle_flatten_treats_an_unverified_close_as_a_failure(monkeypatch):
    """A close that could not be VERIFIED is a failed close, never a success.

    When the broker is unreachable the position query returns nothing, so the
    "is the position still open?" check cannot see the open position. The
    flatten used to report success, reset `lifecycle_retry_count` to 0 and
    silently disable the retry/alert path.
    """
    ex = _live_lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [], "cancelled_orders": []},
    )

    def _query_failed(symbol=None):
        # The broker is unreachable: the query returns nothing AND records the
        # failure -- exactly what `get_live_positions` does in real life.
        ex._last_live_pos_query_ok = False
        return []

    monkeypatch.setattr(ex, "get_live_positions", _query_failed)

    sig = _lifecycle_signal()
    sig["lifecycle_retry_count"] = 0
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is False, "an unverified close must never be reported as a success"
    assert "close:unverified" in res["failed"]
    assert res.get("gave_up") is not True
    assert sig["lifecycle_retry_count"] == 1, "the retry counter must be incremented, not reset"


def test_apply_lifecycle_flatten_never_resets_the_retry_count_on_an_unverified_close(monkeypatch):
    """An unverified close must never zero the retry counter."""
    ex = _live_lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [], "cancelled_orders": []},
    )

    def _query_failed(symbol=None):
        ex._last_live_pos_query_ok = False
        return []

    monkeypatch.setattr(ex, "get_live_positions", _query_failed)

    sig = _lifecycle_signal()
    sig["lifecycle_retry_count"] = 2
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is False
    assert sig["lifecycle_retry_count"] == 3, "the counter must keep counting, never reset to 0"


def test_apply_lifecycle_flatten_accepts_a_verified_close(monkeypatch):
    """The broker answered and the position is gone: the flatten is a success.

    This is the counterpart of the unverified close: the SAME empty position
    list is a success when the query itself succeeded.
    """
    ex = _live_lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [], "cancelled_orders": []},
    )

    def _query_ok(symbol=None):
        ex._last_live_pos_query_ok = True    # the broker answered: no positions
        return []

    monkeypatch.setattr(ex, "get_live_positions", _query_ok)

    sig = _lifecycle_signal()
    sig["lifecycle_retry_count"] = 2
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is True
    assert res["failed"] == []
    assert sig["lifecycle_retry_count"] == 0


def test_apply_lifecycle_flatten_in_dry_run_never_reports_an_unverified_close(monkeypatch):
    """A dry run has no broker to verify against: the flatten still succeeds."""
    # In dry run `get_live_positions` returns [] AND flags the query as failed,
    # so the dry run must not turn every flatten into a failure.
    ex = _lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [], "cancelled_orders": []},
    )

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_flatten(sig)

    assert res["ok"] is True, "a dry run must not turn every flatten into a failure"
    assert "close:unverified" not in res["failed"]
    assert sig["lifecycle_retry_count"] == 0


def test_apply_lifecycle_derisk_skips_the_be_move_when_the_live_price_is_unavailable(monkeypatch):
    """No tick -> no market validation -> the BE stop is NOT sent (fail closed).

    A BUY whose price has fallen below the entry would be moved to a stop
    above the market, which the broker rejects with retcode 10016.
    """
    from types import SimpleNamespace

    ex = _live_lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    monkeypatch.setattr(ex, "get_tick", lambda symbol: None)

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert calls == [], "a stop that cannot be validated must never reach the broker"
    assert res["moved"] == []
    assert res["ok"] is True, "an unavailable price is a skip, not a failure"
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_apply_lifecycle_derisk_skips_a_sell_stop_on_the_wrong_side_of_the_market(monkeypatch):
    """The SELL mirror of the BUY case: a stop BELOW the market is invalid.

    The price moved against the SELL (it rose), so the breakeven stop
    (entry - 0.50) now sits below the market -- the broker rejects that with
    retcode 10016 (invalid stops).
    """
    from types import SimpleNamespace

    ex = _live_lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    monkeypatch.setattr(ex, "get_tick", lambda symbol: SimpleNamespace(bid=3910.0, ask=3910.2))

    sig = _lifecycle_signal()
    sig["direction"] = "SELL"
    sig["layers"]["L1"]["entry_price"] = 3905.0
    sig["layers"]["L1"]["broker_sl"] = 3920.0
    res = ex.apply_lifecycle_derisk(sig)

    assert calls == [], "an invalid stop must never reach the broker"
    assert res["moved"] == []
    assert res["ok"] is True
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3920.0)


def test_apply_lifecycle_derisk_skips_a_stop_inside_the_broker_freeze_level(monkeypatch):
    """A stop inside the broker freeze level is skipped, and the check runs."""
    from types import SimpleNamespace

    ex = _live_lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(
            digits=2, point=0.01, trade_stops_level=50, trade_freeze_level=200
        ),
    )
    # A fixed buffer keeps the test independent of the broker's stops level.
    monkeypatch.setattr(ex, "_compute_stops_buffer", lambda sym_info: 0.55)
    # The market is $1.00 above the BE stop: far enough for a valid stop, but
    # inside the 200-point ($2.00) freeze level.
    monkeypatch.setattr(ex, "get_tick", lambda symbol: SimpleNamespace(bid=3896.5, ask=3896.7))

    freeze_calls = []
    real_freeze = ex.is_within_freeze_level

    def _spy(symbol, target_price, tick=None, sym_info=None):
        freeze_calls.append(target_price)
        return real_freeze(symbol, target_price, tick=tick, sym_info=sym_info)

    monkeypatch.setattr(ex, "is_within_freeze_level", _spy)

    sig = _lifecycle_signal()
    res = ex.apply_lifecycle_derisk(sig)

    assert freeze_calls == [3895.50], "the freeze level must be consulted"
    assert calls == [], "a stop inside the freeze level must never reach the broker"
    assert res["moved"] == []
    assert res["ok"] is True
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)


def test_apply_lifecycle_derisk_sell_never_loosens_a_tighter_existing_stop(monkeypatch):
    """A SELL stop already TIGHTER than the breakeven price must be left alone.

    The BUY comparison copied verbatim (`cur_sl >= be_sl`) would read a SELL
    stop of 3904.00 as looser than the 3904.50 breakeven and would move it --
    which would LOOSEN the broker stop.
    """
    ex = _lifecycle_executor()

    calls = []
    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: calls.append((t, sl)) or True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [])

    sig = _lifecycle_signal()
    sig["direction"] = "SELL"
    sig["layers"]["L1"]["entry_price"] = 3905.0
    sig["layers"]["L1"]["broker_sl"] = 3904.00   # tighter than the BE (3904.50)
    res = ex.apply_lifecycle_derisk(sig)

    assert calls == []
    assert res["moved"] == []
    assert res["ok"] is True
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3904.00)


def test_apply_lifecycle_derisk_publishes_the_risk_of_the_filled_layers_only(monkeypatch):
    """A PENDING layer's PLANNED risk must not be averaged into the signal R."""
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["layers"]["L2"]["initial_sl"] = 3880.0    # a planned stop on an unfilled limit
    ex.apply_lifecycle_derisk(sig)

    # Only L1 holds a position (entry 3895.00, initial stop 3880.00) -> R = 15.00
    assert sig["initial_r_per_oz"] == pytest.approx(15.0)


def test_apply_lifecycle_derisk_never_uses_a_planned_price_as_the_entry(monkeypatch):
    """A layer without a REAL entry must not contribute its planned risk.

    The `target_price` fallback is a planned price for an unfilled layer, so
    it must never stand in for the layer's actual entry price.
    """
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["layers"]["L4"] = {
        "ticket": 104,
        "status": "OPEN",
        "lot": 0.01,
        "remaining_lot": 0.01,
        "target_price": 3900.0,      # a PLANNED price, not a realised entry
        "initial_sl": 3880.0,
        "broker_sl": 3880.0,
    }
    ex.apply_lifecycle_derisk(sig)

    # Only L1 has a real entry (3895.00) and an initial stop (3880.00) -> R = 15.00
    assert sig["initial_r_per_oz"] == pytest.approx(15.0)


def test_apply_lifecycle_derisk_ignores_a_layer_without_any_risk(monkeypatch):
    """A layer whose entry equals its initial stop has no risk to average in."""
    ex = _lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])

    sig = _lifecycle_signal()
    sig["layers"]["L1"]["initial_sl"] = 3895.0    # entry == stop -> risk 0
    ex.apply_lifecycle_derisk(sig)

    assert "initial_r_per_oz" not in sig, "a zero R must never be published"


def test_apply_lifecycle_derisk_does_not_dilute_the_risk_with_a_zero_risk_layer(monkeypatch):
    """A zero-risk layer must not drag the published mean down."""
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
        "initial_sl": 3900.0,     # entry == stop -> risk 0
        "broker_sl": 3900.0,
    }
    ex.apply_lifecycle_derisk(sig)

    # L1 risk = 15.00, L3 risk = 0.00 (ignored) -> mean 15.00
    assert sig["initial_r_per_oz"] == pytest.approx(15.0)


def test_apply_lifecycle_flatten_publishes_the_mean_initial_risk(monkeypatch):
    """A signal flattened before any de-risk still has to carry its R."""
    ex = _lifecycle_executor()

    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda sig: {"closed_tickets": [101], "cancelled_orders": [102]},
    )

    sig = _lifecycle_signal()
    ex.apply_lifecycle_flatten(sig)

    assert sig["initial_r_per_oz"] == pytest.approx(15.0)


def test_apply_lifecycle_flatten_gives_up_on_the_call_that_reaches_the_budget(monkeypatch):
    """The call that exhausts the retry budget reports the give-up itself."""
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
    results = [ex.apply_lifecycle_flatten(sig) for _ in range(3)]

    assert len(attempts) == 3
    assert results[0].get("gave_up") is not True
    assert results[1].get("gave_up") is not True
    assert results[2].get("gave_up") is True, "the call that reaches the budget must report the give-up"
    assert results[2]["ok"] is False
    assert sig["lifecycle_retry_count"] == 3

    # The next call must not attempt another close.
    res = ex.apply_lifecycle_flatten(sig)
    assert len(attempts) == 3, "the give-up path must not close anything"
    assert res.get("gave_up") is True
    assert res["failed"] == ["close:give_up"]
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

    def _publish_lifecycle_initial_r(self, signal_data: Dict[str, Any]) -> Optional[float]:
        """Publish the signal's mean initial risk per ounce (`initial_r_per_oz`).

        Only the layers that HOLD a position contribute: a PENDING layer has
        no entry yet, so its planned `target_price` is a planned price and not
        a realised risk. A layer whose risk is zero (entry == initial stop) is
        ignored, and the field is only written when the mean is greater than
        zero -- a zero R would make the ledger's `r_at_exit` meaningless.
        """
        layers = signal_data.get("layers") or {}
        if not isinstance(layers, dict):
            return None
        risks: List[float] = []
        for lv in layers.values():
            if not isinstance(lv, dict):
                continue
            if str(lv.get("status") or "").strip().upper() in ("PENDING", "CANCELLED", "CLOSED"):
                continue
            entry = _num(lv.get("entry_price") or lv.get("entry"), 0.0)
            stop = _num(lv.get("initial_sl"), 0.0)
            if entry <= 0.0 or stop <= 0.0:
                continue
            risk = abs(entry - stop)
            if risk <= 0.0:
                continue
            risks.append(risk)
        if not risks:
            return None
        mean_r = round(sum(risks) / len(risks), 4)
        if mean_r <= 0.0:
            return None
        signal_data["initial_r_per_oz"] = mean_r
        return mean_r

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
        self._publish_lifecycle_initial_r(signal_data)

        # The market decides whether the breakeven stop is even a valid stop: a
        # BUY stop above the market (or a SELL stop below it) is rejected by the
        # broker with retcode 10016 (invalid stops). The check runs against a
        # live broker only -- a dry run has nothing that could reject it.
        tick = None
        live_price = 0.0
        price_unavailable = False
        if not self.dry_run:
            try:
                tick = self.get_tick(symbol)
            except Exception as exc:
                logger.warning(f"[LIFECYCLE DERISK] tick for {symbol} failed: {exc}")
                result["failed"].append(f"tick:{exc}")
            if tick is None:
                price_unavailable = True
            else:
                _raw_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
                live_price = _num(_raw_px, 0.0)
                if not (live_price > 0.0):
                    price_unavailable = True
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
                # Fail closed: without a usable live price the breakeven stop
                # cannot be validated against the market, so it is NOT sent.
                # The layer keeps its existing (already valid) stop and the
                # next tick tries again.
                if price_unavailable:
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: no live price for {symbol}; "
                        f"the BE stop {be_sl} is not sent and the existing stop is kept."
                    )
                    continue
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

        # A signal that is flattened before any de-risk still has to carry
        # its initial risk for the ledger.
        self._publish_lifecycle_initial_r(signal_data)

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
            live_ids: Set[int] = set()
            query_ok = True
            try:
                live_positions = self.get_live_positions() or []
                # `get_live_positions` records whether the broker query really
                # succeeded. A FAILED query returns an empty list, which is
                # indistinguishable from "the account has no positions" unless
                # the flag is read.
                query_ok = bool(self._last_live_pos_query_ok)
                live_ids = {
                    _int_num(getattr(p, "ticket", 0), 0)
                    for p in live_positions
                }
            except Exception as exc:
                logger.warning(f"[LIFECYCLE FLATTEN] live position check failed: {exc}")
                live_ids = set()
                query_ok = False
            unverified = [t for t in expected_open if t not in closed_set]
            if unverified and not self.dry_run and not query_ok:
                # The broker could not be asked, so the close cannot be
                # verified: an unverified close is a FAILED close. The retry
                # counter must never be reset on it.
                logger.warning(
                    f"[LIFECYCLE FLATTEN] the live position query failed; the close of "
                    f"{','.join(str(t) for t in unverified)} could not be verified."
                )
                result["failed"].append("close:unverified")
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

- [ ] **Shipped notes (the deviations that shipped)**

The implementation above shipped with four deliberate deviations from the plan as written:

1. **The retry threshold is "`LIFECYCLE_MAX_CLOSE_RETRIES` attempts, then give up"**,
   matching the design spec, rather than the original "retries > max_retries" which gave up on
   attempt max+1.
2. **A close that leaves the position OPEN counts as a failure** (checked against the live
   positions), because "`close_signal_exposure`" swallows per-ticket failures and the counter would
   otherwise never increment in production. A close that cannot be VERIFIED at all -- the live
   position query failed, so the open position is invisible -- is a failure too, and it never
   resets "`lifecycle_retry_count`".
3. **The de-risk refuses to send a BE stop on the wrong side of the market, inside the broker
   freeze level, or when the live price is unavailable**, because the original code would have
   emitted retcode 10016 on a losing BUY.
4. **The dispatch uses the "`risk_lifecycle.ACTION_*`" constants.**

---

## Task 6: `main.py` — run the age guard every second

**Files:**
- Modify: `main.py` (`_process_pre_tp1_or_backstop_tick` — the age guard hook, and the `risk_lifecycle` import)
- Modify: `mt5_executor.py` (`apply_lifecycle_derisk` — report a `"skipped"` result when the action could not be attempted)
- Modify: `tests/test_trade_lifecycle.py` (append the watcher tests)

**Interfaces:**
- Consumes: `risk_lifecycle.evaluate_trade_age`, `risk_lifecycle.read_lifecycle_policy` (Task 1); `MT5Executor.apply_lifecycle_action` (Task 5).
- Produces: the age guard runs inside `_process_pre_tp1_or_backstop_tick`, stamping `sig["lifecycle_derisk_at"]`, `sig["lifecycle_flatten_at"]`, `sig["lifecycle_pending_expired_at"]`.
- Consumes (new): the action result's optional `"skipped"` key — a short reason string set when the action could NOT be attempted (for example `"no_live_price"`). A skipped action is NOT done and must be retried on the next tick.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- main.py age guard ---------------------------------------------------


def _install_signal(monkeypatch, tmp_path, sig):
    """Install `sig` in a throw-away state manager and return the main module."""
    import main
    from state_manager import StateManager
    from trade_ledger import TradeLedger

    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "lifecycle_state.json"))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / "lifecycle_ledger.jsonl"))
    main.state_mgr.create_signal(
        message_id="9001",
        symbol="XAUUSD.s",
        direction="BUY",
    )
    main.state_mgr.update_signal("9001", sig)
    return main


def _age_guard_signal(hours_old: float = 3.0):
    """A signal that carries an open market position and an aged clock."""
    from datetime import datetime, timedelta, timezone

    sig = _lifecycle_signal()
    sig["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    sig["first_fill_at"] = sig["timestamp"]
    # A priced TP1 keeps the tick out of the fallback-TP1 branch: these tests
    # are about the age guard, not about the fallback TP1 attachment.
    sig["raw_tp1"] = 3999.0
    return sig


class _StubExecutor:
    """An executor whose lifecycle action returns scripted results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.seen = []
        self.tick_calls = 0
        self.atr_calls = 0

    def get_tick(self, symbol):
        from types import SimpleNamespace

        self.tick_calls += 1
        return SimpleNamespace(bid=3895.0, ask=3895.2, time=1700000000.0)

    def get_pip_size(self, symbol):
        return 0.1

    def get_server_time_offset_seconds(self):
        return 0

    def evaluate_dynamic_atr_engine(self, signal_data, tick=None):
        self.atr_calls += 1
        return {}

    def apply_lifecycle_action(self, signal_data, action):
        self.calls.append(action)
        self.seen.append(signal_data)
        if self._results:
            return self._results.pop(0)
        return {
            "action": action,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }


def _enable_lifecycle(monkeypatch, **overrides):
    """Switch the trade-age guard ON for the duration of the test."""
    import config

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", True)
    for name, value in overrides.items():
        monkeypatch.setattr(config, name, value)


@pytest.mark.asyncio
async def test_age_guard_derisks_an_old_signal(monkeypatch, tmp_path):
    """An aged trade is de-risked by the watcher."""
    import main

    _enable_lifecycle(monkeypatch, LIFECYCLE_DERISK_SECONDS=7200, LIFECYCLE_FLATTEN_SECONDS=21600)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert stub.calls == ["DERISK"]
    assert stub.seen[0].get("lifecycle_derisk_at"), "a completed de-risk must be stamped"
    assert stub.atr_calls == 1, "the tick must continue after the de-risk"


@pytest.mark.asyncio
async def test_age_guard_does_not_stamp_a_failed_dersisk(monkeypatch, tmp_path):
    """A failed action is not done: the next tick must retry it."""
    import main

    _enable_lifecycle(monkeypatch)
    # The retry is bounded by an interval; the interval is 0 for this test.
    monkeypatch.setattr(main, "_LIFECYCLE_RETRY_INTERVAL_SECONDS", 0.0)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([
        {
            "action": "DERISK",
            "ok": False,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": ["L1:boom"],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert stub.calls == ["DERISK"]
    assert not stub.seen[0].get("lifecycle_derisk_at"), "a failed action must not be stamped"

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(stub.seen[-1]))
    assert stub.calls == ["DERISK", "DERISK"], "the next tick must retry"


@pytest.mark.asyncio
async def test_age_guard_does_not_stamp_a_skipped_dersisk(monkeypatch, tmp_path):
    """A skipped action (no live price) is not done either."""
    import main

    _enable_lifecycle(monkeypatch)
    # The retry is bounded by an interval; the interval is 0 for this test.
    monkeypatch.setattr(main, "_LIFECYCLE_RETRY_INTERVAL_SECONDS", 0.0)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([
        {
            "action": "DERISK",
            "ok": True,
            "skipped": "no_live_price",
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert not stub.seen[0].get("lifecycle_derisk_at"), "a skipped action must not be stamped"

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(stub.seen[-1]))
    assert stub.calls == ["DERISK", "DERISK"], "the next tick must retry"


@pytest.mark.asyncio
async def test_age_guard_closes_the_signal_only_after_a_successful_flatten(monkeypatch, tmp_path):
    """A failed flatten must not close the signal: the position may still be open."""
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(7.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([
        {
            "action": "FLATTEN",
            "ok": False,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": ["close:boom"],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert stub.seen[0].get("status") != "CLOSED", "a failed flatten must not close the signal"
    assert not stub.seen[0].get("lifecycle_flatten_at")
    assert main_mod.state_mgr.get_signal("9001").get("status") != "CLOSED"


@pytest.mark.asyncio
async def test_age_guard_alerts_once_when_the_flatten_gives_up(monkeypatch, tmp_path):
    """The give-up path must be reachable, alert once, and stop retrying."""
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(7.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    alerts = []

    async def _notify(text, *args, **kwargs):
        alerts.append(text)

    monkeypatch.setattr(main_mod, "notify_saved_messages", _notify)

    stub = _StubExecutor([
        {
            "action": "FLATTEN",
            "ok": False,
            "gave_up": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": ["close:give_up"],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert len(alerts) == 1, "the give-up must be reported once"
    assert "gave up" in alerts[0]
    assert stub.seen[0].get("status") != "CLOSED", "a gave-up flatten must not close the signal"
    assert stub.seen[0].get("lifecycle_flatten_at"), "the give-up must stop the retries"

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(stub.seen[-1]))
    assert len(alerts) == 1, "the give-up alert must not repeat"
    assert stub.calls == ["FLATTEN"], "a gave-up flatten must not be retried"


@pytest.mark.asyncio
async def test_age_guard_does_not_repeat_a_completed_action(monkeypatch, tmp_path):
    """A completed action is stamped, so the guard does not run it again."""
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert stub.calls == ["DERISK"]
    assert stub.seen[0].get("lifecycle_derisk_at")

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(stub.seen[-1]))
    assert stub.calls == ["DERISK"], "a completed action must not run again"


@pytest.mark.asyncio
async def test_age_guard_is_a_no_op_when_the_guard_is_disabled(monkeypatch, tmp_path):
    """With the guard OFF nothing is touched and the tick behaves exactly as before."""
    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)

    sig = _age_guard_signal(7.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert stub.calls == [], "a disabled guard must not touch the broker"
    assert stub.tick_calls == 1, "the tick must still run"
    assert stub.atr_calls == 1, "the tick must continue exactly as before"
    assert not stub.seen
    assert not sig.get("lifecycle_flatten_at")
    assert not sig.get("lifecycle_derisk_at")
    assert sig.get("status") != "CLOSED"


@pytest.mark.asyncio
async def test_age_guard_runs_before_the_tp1_branches(monkeypatch, tmp_path):
    """An aged trade is de-risked FIRST, before the TP1 branches run."""
    import config
    import main

    _enable_lifecycle(monkeypatch)
    monkeypatch.setattr(config, "BANK_ALL_AT_TP1", True)

    sig = _age_guard_signal(3.0)
    sig["raw_tp1"] = 3895.0          # the tick (bid 3895.0) reaches TP1
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    order = []
    stub = _StubExecutor([])
    monkeypatch.setattr(main_mod, "executor", stub)

    _real_apply = stub.apply_lifecycle_action

    def _apply(signal_data, action):
        order.append("lifecycle")
        return _real_apply(signal_data, action)

    stub.apply_lifecycle_action = _apply

    async def _handoff(msg_id, sig, chat_id=None, source_label="", curr_price=None):
        order.append("tp1")
        return None, 0.0, ""

    monkeypatch.setattr(main_mod, "_execute_tp1_scaling_handoff", _handoff)

    async def _notify(text, *args, **kwargs):
        return None

    monkeypatch.setattr(main_mod, "notify_saved_messages", _notify)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert order == ["lifecycle", "tp1"], "the age guard must run before the TP1 branches"


@pytest.mark.asyncio
async def test_age_guard_contains_an_exception_from_the_action(monkeypatch, tmp_path):
    """A broker error inside the action must not escape the tick."""
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    class _RaisingExecutor(_StubExecutor):
        def apply_lifecycle_action(self, signal_data, action):
            self.calls.append(action)
            raise RuntimeError("the broker pipe is closed")

    stub = _RaisingExecutor([])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert stub.calls == ["DERISK"]
    assert stub.atr_calls == 1, "the tick must continue after a contained error"
    assert not stub.seen


@pytest.mark.asyncio
async def test_age_guard_does_not_notify_on_a_failed_or_skipped_action(monkeypatch, tmp_path):
    """Only a close and a give-up may talk to Saved Messages."""
    import main

    _enable_lifecycle(monkeypatch)
    # The retry is bounded by an interval; the interval is 0 for this test.
    monkeypatch.setattr(main, "_LIFECYCLE_RETRY_INTERVAL_SECONDS", 0.0)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    alerts = []

    async def _notify(text, *args, **kwargs):
        alerts.append(text)

    monkeypatch.setattr(main_mod, "notify_saved_messages", _notify)

    stub = _StubExecutor([
        {"action": "DERISK", "ok": False, "failed": ["L1:boom"]},
        {"action": "DERISK", "ok": True, "skipped": "no_live_price"},
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(stub.seen[-1]))

    assert stub.calls == ["DERISK", "DERISK"]
    assert alerts == [], "a failed or skipped action must stay quiet"


@pytest.mark.asyncio
async def test_age_guard_closes_the_signal_after_a_successful_flatten(monkeypatch, tmp_path):
    """A completed flatten closes the signal and reports it once."""
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(7.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    alerts = []

    async def _notify(text, *args, **kwargs):
        alerts.append(text)

    monkeypatch.setattr(main_mod, "notify_saved_messages", _notify)

    stub = _StubExecutor([
        {
            "action": "FLATTEN",
            "ok": True,
            "moved": [],
            "closed": [101],
            "cancelled": [102],
            "failed": [],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert stub.calls == ["FLATTEN"]
    assert stub.seen[0].get("status") == "CLOSED"
    assert stub.seen[0].get("lifecycle_flatten_at")
    assert main_mod.state_mgr.get_signal("9001").get("status") == "CLOSED"
    assert len(alerts) == 1, "a completed flatten reports once"
    assert "FLATTEN" in alerts[0]


@pytest.mark.asyncio
async def test_age_guard_persists_the_retry_counter_until_the_give_up(monkeypatch, tmp_path):
    """A failed flatten must persist its retry counter so the give-up can fire.

    The executor increments `lifecycle_retry_count` IN PLACE on the signal. The
    guard's own save is dirty-checked, so without an explicit partial update the
    counter never reaches the disk: every tick would restart from zero, the
    retry budget would never be spent and the guard would retry forever.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import main
    import mt5_executor
    from mt5_executor import MT5Executor

    _enable_lifecycle(monkeypatch, LIFECYCLE_MAX_CLOSE_RETRIES=3)
    monkeypatch.setattr(mt5_executor, "LIFECYCLE_MAX_CLOSE_RETRIES", 3)

    sig = _age_guard_signal(7.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    # The REAL executor action: the broker refuses to close the position.
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    monkeypatch.setattr(
        ex,
        "close_signal_exposure",
        lambda s: {"closed_tickets": [], "cancelled_orders": []},
    )
    monkeypatch.setattr(ex, "get_live_positions", lambda symbol=None: [SimpleNamespace(ticket=101)])
    ex._last_live_pos_query_ok = True

    class _RealActionExecutor(_StubExecutor):
        """The watcher stub, but the REAL lifecycle action underneath it."""

        def apply_lifecycle_action(self, signal_data, action):
            self.calls.append(action)
            self.seen.append(signal_data)
            return ex.apply_lifecycle_action(signal_data, action)

    stub = _RealActionExecutor([])
    monkeypatch.setattr(main_mod, "executor", stub)

    alerts = []

    async def _notify(text, *args, **kwargs):
        alerts.append(text)

    monkeypatch.setattr(main_mod, "notify_saved_messages", _notify)

    counters = []
    for _ in range(3):
        # The watcher reloads the signal from the state manager every tick.
        current = main_mod.state_mgr.get_signal("9001")
        await main_mod._process_pre_tp1_or_backstop_tick("9001", current)
        counters.append(main_mod.state_mgr.get_signal("9001").get("lifecycle_retry_count"))
        # Let the retry interval elapse between the attempts.
        main_mod.state_mgr.update_signal(
            "9001",
            {
                "lifecycle_last_attempt_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=60)
                ).isoformat()
            },
        )

    assert counters == [1, 2, 3], "the persisted retry counter must increase"
    assert len(alerts) == 1, "the give-up must be reported once"
    assert "gave up" in alerts[0]
    assert main_mod.state_mgr.get_signal("9001").get("status") != "CLOSED", (
        "a gave-up flatten must not close the signal"
    )


@pytest.mark.asyncio
async def test_age_guard_flattens_an_aged_runner_before_the_runner_logic(monkeypatch, tmp_path):
    """A RUNNER_ACTIVE signal past the flatten threshold must be flattened.

    The runner tick is the ONLY tick a post-TP1 trade ever sees, so the age
    guard has to run there too: without it the longest-held trades are never
    de-risked and never flattened.
    """
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(7.0)
    sig["status"] = "RUNNER_ACTIVE"
    sig["runner_ticket"] = 101
    sig["runner_tickets"] = [101]
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    order = []

    class _RunnerExecutor(_StubExecutor):
        def apply_lifecycle_action(self, signal_data, action):
            order.append("lifecycle")
            return super().apply_lifecycle_action(signal_data, action)

        def manage_designated_runner(self, signal_data):
            order.append("runner")
            return True

    stub = _RunnerExecutor([
        {
            "action": "FLATTEN",
            "ok": True,
            "moved": [],
            "closed": [101],
            "cancelled": [],
            "failed": [],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    async def _notify(text, *args, **kwargs):
        return None

    monkeypatch.setattr(main_mod, "notify_saved_messages", _notify)

    await main_mod._process_runner_active_tick("9001", dict(sig))

    assert stub.calls == ["FLATTEN"], "an aged runner must be flattened"
    assert order == ["lifecycle"], "the guard must run before the runner logic"
    assert main_mod.state_mgr.get_signal("9001").get("status") == "CLOSED"


@pytest.mark.asyncio
async def test_age_guard_does_not_stamp_a_stop_the_market_refused(monkeypatch, tmp_path):
    """A de-risk the market refused is NOT done: it must not be stamped."""
    import main

    _enable_lifecycle(monkeypatch)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([
        {
            "action": "DERISK",
            "ok": True,
            "skipped": "stop_not_valid",
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))

    assert not stub.seen[0].get("lifecycle_derisk_at"), "a refused de-risk must not be stamped"


@pytest.mark.asyncio
async def test_age_guard_throttles_the_retry_to_the_configured_interval(monkeypatch, tmp_path):
    """A not-completed action is retried at most once per retry interval."""
    from datetime import datetime, timedelta, timezone

    import main

    _enable_lifecycle(monkeypatch)

    assert main._LIFECYCLE_RETRY_INTERVAL_SECONDS == 30.0

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([
        {
            "action": "DERISK",
            "ok": False,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": ["L1:boom"],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert stub.calls == ["DERISK"]

    # The next tick is INSIDE the interval: the action must not run again.
    await main_mod._process_pre_tp1_or_backstop_tick("9001", main_mod.state_mgr.get_signal("9001"))
    assert stub.calls == ["DERISK"], "a retry inside the interval must be throttled"

    # After the interval the action runs again.
    main_mod.state_mgr.update_signal(
        "9001",
        {
            "lifecycle_last_attempt_at": (
                datetime.now(timezone.utc)
                - timedelta(seconds=main_mod._LIFECYCLE_RETRY_INTERVAL_SECONDS + 1)
            ).isoformat()
        },
    )
    await main_mod._process_pre_tp1_or_backstop_tick("9001", main_mod.state_mgr.get_signal("9001"))
    assert stub.calls == ["DERISK", "DERISK"], "a retry after the interval must run"


@pytest.mark.asyncio
async def test_age_guard_continues_the_tick_after_a_failed_or_skipped_action(monkeypatch, tmp_path):
    """A not-completed action must not stop the tick: the position is still open."""
    import main

    _enable_lifecycle(monkeypatch)
    monkeypatch.setattr(main, "_LIFECYCLE_RETRY_INTERVAL_SECONDS", 0.0)

    sig = _age_guard_signal(3.0)
    main_mod = _install_signal(monkeypatch, tmp_path, sig)

    stub = _StubExecutor([
        {
            "action": "DERISK",
            "ok": False,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": ["L1:boom"],
        },
        {
            "action": "DERISK",
            "ok": True,
            "skipped": "stop_not_valid",
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        },
    ])
    monkeypatch.setattr(main_mod, "executor", stub)

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(sig))
    assert stub.atr_calls == 1, "the tick must continue after a failed action"

    await main_mod._process_pre_tp1_or_backstop_tick("9001", dict(stub.seen[-1]))
    assert stub.calls == ["DERISK", "DERISK"]
    assert stub.atr_calls == 2, "the tick must continue after a skipped action"


# --- the "skipped" action marker (Task 6, executor side) -------------------


def test_apply_lifecycle_derisk_reports_a_skip_when_the_tick_is_unavailable(monkeypatch):
    """No tick -> no live price -> the de-risk is SKIPPED, not failed."""
    from types import SimpleNamespace

    ex = _live_lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    monkeypatch.setattr(ex, "get_tick", lambda symbol: None)

    res = ex.apply_lifecycle_derisk(_lifecycle_signal())

    assert res.get("skipped") == "no_live_price"
    assert res["ok"] is True, "a skip is not a failure"
    assert res["failed"] == [], "a skip must not be recorded as a failure"
    assert res["moved"] == []


def test_apply_lifecycle_derisk_reports_a_skip_when_the_tick_raises(monkeypatch):
    """A terminal that is briefly unreachable must not fail the de-risk every second."""
    from types import SimpleNamespace

    ex = _live_lifecycle_executor()

    def _raise(symbol):
        raise RuntimeError("terminal not connected")

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    monkeypatch.setattr(ex, "get_tick", _raise)

    res = ex.apply_lifecycle_derisk(_lifecycle_signal())

    assert res.get("skipped") == "no_live_price"
    assert res["ok"] is True, "a skip is not a failure"
    assert res["failed"] == [], "a skip must not be recorded as a failure"
    assert res["moved"] == []


@pytest.mark.parametrize(
    "tick_spec",
    [
        {"bid": 0.0, "ask": 0.0},
        {"bid": -1.0, "ask": -1.0},
        {"ask": 3895.2},
        {"bid": "not-a-number", "ask": "not-a-number"},
    ],
)
def test_apply_lifecycle_derisk_reports_a_skip_for_an_unusable_price(monkeypatch, tick_spec):
    """A missing, non-numeric or non-positive price is an unusable price."""
    from types import SimpleNamespace

    tick_value = SimpleNamespace(**tick_spec)
    ex = _live_lifecycle_executor()

    monkeypatch.setattr(ex, "modify_position_sltp", lambda t, sl=None, tp=None: True)
    monkeypatch.setattr(ex, "cancel_signal_pending_orders", lambda sig: [102])
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(digits=2, point=0.01, trade_stops_level=50),
    )
    monkeypatch.setattr(ex, "get_tick", lambda symbol: tick_value)

    res = ex.apply_lifecycle_derisk(_lifecycle_signal())

    assert res.get("skipped") == "no_live_price"
    assert res["ok"] is True, "a skip is not a failure"
    assert res["failed"] == []
    assert res["moved"] == []


def test_apply_lifecycle_derisk_reports_a_refused_stop_as_stop_not_valid(monkeypatch):
    """A BE stop the market refuses must be reported as a refusal, not as done.

    The breakeven stop sits on the wrong side of the market, so the broker
    would reject it with retcode 10016. The de-risk did NOT complete: the
    watcher must not stamp it and must retry it once the price recovers.
    """
    from types import SimpleNamespace

    ex = _live_lifecycle_executor()

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
    assert res.get("skipped") == "stop_not_valid"
    assert res["ok"] is True, "a refusal is a skip, not a failure"
    assert res["moved"] == []
    assert res["failed"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3880.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: FAIL — the watcher does not call `apply_lifecycle_action`.

- [ ] **Step 3: Implement the age guard**

In `main.py`, add the retry cadence, the two helpers and the guard itself, then call the guard
from BOTH tick functions.

**The action is stamped on the signal only when it actually COMPLETED.** A skipped action (the
de-risk could not read a live price, or the market refused the breakeven stop) and a failed
action (the broker refused the close) are NOT done: the stamp stays unset, the signal is not
closed in the state manager, and the action is retried once the retry interval has elapsed.
Stamping unconditionally would permanently disable the de-risk after one unavailable tick
and would make the retry/give-up path unreachable.

**A NOT-COMPLETED action is retried on a BOUNDED cadence** (`_LIFECYCLE_RETRY_INTERVAL_SECONDS`,
30 seconds by default) instead of on the very next tick. `apply_lifecycle_derisk` calls
`cancel_signal_pending_orders` on EVERY invocation, so an unbounded retry is a broker sweep once
per second per aged trade.

**The retry counter and the attempt stamp are persisted with an explicit PARTIAL update.** The
whole-signal save is dirty-checked against a baseline taken AFTER the guard ran, so the guard's
own mutations never reach the disk on their own: the retry counter would restart from zero on
every tick, the give-up path could never fire and the guard would retry forever.

Add to `main.py` (module level, just above `_process_runner_active_tick`):

```python
#: The trade-age guard retries a NOT-COMPLETED action at most once per this
#: many seconds. `apply_lifecycle_derisk` cancels the unfilled pending orders
#: on EVERY invocation, so an unbounded retry would sweep the broker once per
#: second per aged trade.
_LIFECYCLE_RETRY_INTERVAL_SECONDS = 30.0


def _lifecycle_last_attempt_at(sig: Dict[str, Any]) -> Optional[datetime]:
    """Return the last lifecycle attempt as an aware UTC datetime (or None)."""
    raw = str(sig.get("lifecycle_last_attempt_at") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _persist_lifecycle_attempt(msg_id: str, sig: Dict[str, Any], chat_id: Any = None) -> None:
    """Persist the retry counter and the attempt stamp with a PARTIAL update.

    The whole-signal save is dirty-checked and throttled, so the guard's own
    mutations never reach the disk on their own: the retry counter would
    restart from zero on every tick and the give-up path could never fire.
    """
    updates: Dict[str, Any] = {}
    retry_count = sig.get("lifecycle_retry_count")
    if retry_count is not None:
        updates["lifecycle_retry_count"] = retry_count
    last_attempt = sig.get("lifecycle_last_attempt_at")
    if last_attempt:
        updates["lifecycle_last_attempt_at"] = last_attempt
    if updates:
        state_mgr.update_signal(msg_id, updates, chat_id=chat_id)


async def _run_trade_lifecycle_guard(msg_id: str, sig: Dict[str, Any]) -> bool:
    """Run the trade-age guard. Returns True when the caller should stop the tick.

    The guard de-risks an aged trade (cancel the unfilled limits and move every
    open layer to breakeven) and flattens a very old one. The action is stamped
    on the signal only when it COMPLETED: a skipped or failed action is retried
    once the retry interval has elapsed, and the retry counter is persisted so
    the give-up path can fire.

    The helper derives the direction and the symbol from the signal itself, so
    every tick function (the pre-TP1 tick AND the runner tick) can call it.
    """
    sig_chat = sig.get("chat_id")
    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = str(sig.get("direction") or "BUY").upper()

    lifecycle_pol = read_lifecycle_policy(config)
    if not lifecycle_pol.lifecycle_enabled:
        return False

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
    if action == "NONE" or already_done:
        return False

    # A NOT-COMPLETED action is retried on a BOUNDED cadence: the de-risk
    # cancels the pending orders on every call, so an unbounded retry would
    # sweep the broker once per second per aged trade.
    last_attempt = _lifecycle_last_attempt_at(sig)
    if last_attempt is not None:
        elapsed = (now_utc - last_attempt).total_seconds()
        if elapsed < _LIFECYCLE_RETRY_INTERVAL_SECONDS:
            logger.info(
                f"[LIFECYCLE {action}] msg={msg_id} throttled: the last attempt was "
                f"{elapsed:.1f}s ago (< {_LIFECYCLE_RETRY_INTERVAL_SECONDS}s)."
            )
            return False

    res: Optional[Dict[str, Any]] = None
    try:
        res = await _mt5_call(executor.apply_lifecycle_action, sig, action)
    except Exception as exc:
        logger.warning(f"[LIFECYCLE {action}] msg={msg_id} failed: {exc}")
        res = None
    sig["lifecycle_last_attempt_at"] = now_utc.isoformat()

    if isinstance(res, dict):
        gave_up = bool(res.get("gave_up"))
        skipped = bool(res.get("skipped"))
        ok = bool(res.get("ok"))
        if gave_up:
            # The retry budget is spent. Stop retrying and alert ONCE.
            # The signal is NOT marked CLOSED: the position may still be
            # open, and it keeps its broker SL. The tick CONTINUES so the
            # still-open position is managed normally.
            sig[stamp_key] = now_utc.isoformat()
            sig["lifecycle_action"] = action
            sig["lifecycle_reason"] = age_res.get("reason")
            sig["lifecycle_gave_up_at"] = now_utc.isoformat()
            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
            await notify_saved_messages(
                f"⚠️ **Trade Lifecycle Guard gave up** on signal `{msg_id}` ({direction} {symbol})\n"
                f"• Reason: {age_res.get('reason')}\n"
                f"• The position keeps its broker SL and the bot stops retrying "
                f"({lifecycle_pol.max_close_retries} attempts)."
            )
        elif ok and not skipped:
            # The action COMPLETED. Stamp it so the guard does not run it
            # again, and persist the outcome.
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
                return True
        else:
            # NOT done: a skip (no live price, a stop the market refused) or a
            # failed action. The stamp stays unset, so the action is retried
            # once the retry interval has elapsed. Nothing is closed and
            # nothing is closed-out in the state manager, but the retry
            # counter and the attempt stamp ARE persisted: the counter is the
            # only thing that can exhaust the retry budget.
            _persist_lifecycle_attempt(msg_id, sig, sig_chat)
            logger.info(
                f"[LIFECYCLE {action}] msg={msg_id} not completed "
                f"(ok={ok} skipped={skipped}); retrying in "
                f"{_LIFECYCLE_RETRY_INTERVAL_SECONDS}s. result={res}"
            )
    else:
        # The action raised: the attempt is still bounded, so the attempt
        # stamp is persisted and the retry waits for the interval.
        _persist_lifecycle_attempt(msg_id, sig, sig_chat)
        logger.info(
            f"[LIFECYCLE {action}] msg={msg_id} did not complete; retrying in "
            f"{_LIFECYCLE_RETRY_INTERVAL_SECONDS}s."
        )
    return False
```

```python
    # Trade Lifecycle Guard: age-based de-risk / flatten, evaluated every second.
    # Runs before the TP1 branches so an aged trade is de-risked first.
    if await _run_trade_lifecycle_guard(msg_id, sig):
        return

```

Call the guard from the RUNNER tick, BEFORE `manage_designated_runner`, so an aged runner is
de-risked or flattened before the runner logic touches it:

```python
    # Trade Lifecycle Guard: an aged runner is de-risked or flattened BEFORE
    # the runner logic touches it.
    if await _run_trade_lifecycle_guard(msg_id, sig):
```

Add the required import to `main.py`:

```python
from risk_lifecycle import evaluate_trade_age, read_lifecycle_policy, stamp_fill_times
```

`datetime` / `timezone` are already imported in `main.py`.

- [ ] **Step 4: Report a "skipped" action from the executor**

`apply_lifecycle_derisk` must tell the watcher that the action could not be ATTEMPTED, as opposed
to having failed. Today a de-risk that cannot read a live price simply moves nothing, and the
watcher would read that as "done" and never retry — which would silently disable the de-risk for
the rest of the trade's life.

In `mt5_executor.py`, inside `apply_lifecycle_derisk`:

```python
        tick = None
        live_price = 0.0
        price_unavailable = False
        stops_buffer = self._compute_stops_buffer(sym_info)
        if not self.dry_run:
            try:
                tick = self.get_tick(symbol)
            except Exception as exc:
                # A terminal that is briefly unreachable is a SKIP, not a
                # failure: the de-risk is retried on the next tick instead of
                # being reported as failed every second.
                logger.warning(f"[LIFECYCLE DERISK] tick for {symbol} failed: {exc}")
                result["skipped"] = "no_live_price"
                price_unavailable = True
            if tick is None:
                price_unavailable = True
                result["skipped"] = "no_live_price"
            else:
                _raw_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
                live_price = _num(_raw_px, 0.0)
                if not (live_price > 0.0):
                    price_unavailable = True
```

1. When the live price is unusable (the tick is None, the tick raised, or the price is not
   positive), set `result["skipped"] = "no_live_price"`.
2. A tick fetch that RAISES is a skip, not a failure: record it as `skipped` and do NOT append a
   `failed` entry for it. A terminal that is briefly unreachable must not make the de-risk report
   a failure every second.
3. The per-layer skip that the unusable price causes stays a skip: the layer keeps its existing
   stop and no `failed` entry is recorded for it.

```python
                if live_price > 0.0 and sign * (live_price - be_sl) < stops_buffer - 1e-9:
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: BE stop {be_sl} is not a "
                        f"valid stop at the market ({live_price}); the existing stop is kept."
                    )
                    if not result.get("skipped"):
                        # The market REFUSED the breakeven stop: the de-risk did
                        # not complete, so the watcher must retry it later
                        # instead of stamping it as done.
                        result["skipped"] = "stop_not_valid"
                    continue
                if self.is_within_freeze_level(symbol, be_sl, tick=tick, sym_info=sym_info):
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: BE stop {be_sl} is inside the "
                        f"broker freeze level; the existing stop is kept."
                    )
                    if not result.get("skipped"):
                        # The broker freeze level REFUSED the breakeven stop: the
                        # de-risk did not complete, so the watcher must retry it
                        # later instead of stamping it as done.
                        result["skipped"] = "stop_not_valid"
                    continue
```

4. When a layer's breakeven stop is refused because it sits on the wrong side of the market or
   inside the broker freeze level, set `result["skipped"] = "stop_not_valid"`. The existing stop
   is kept and the broker is never asked for an invalid stop (no retcode 10016).
5. `"no_live_price"` wins when both apply: the marker means "the action did NOT complete", so
   the watcher must not stamp it and must retry it later.

The action result contract is now:

- `ok` — True when nothing went wrong.
- `skipped` — a short reason string when the action could not be attempted:
  `"no_live_price"` (the price was unusable) or `"stop_not_valid"` (the market or the freeze
  level refused a breakeven stop). The action is NOT done and must be retried.
- `gave_up` — the retry budget is spent. Stop retrying and alert.

- [ ] **Step 5: Run the tests to verify they pass**

```powershell
python -m pytest tests/test_trade_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 6: Run the full suite**

```powershell
python -m pytest -q
```

Expected: **0 failed**.

- [ ] **Step 7: Commit**

```powershell
git add main.py mt5_executor.py tests/test_trade_lifecycle.py
git commit -m "feat(main): run the trade-age guard every second

_process_pre_tp1_or_backstop_tick now evaluates the age guard before the TP1
branches, so an aged trade is de-risked or flattened first.

The action is stamped on the signal only when it actually COMPLETED, so a
skipped or failed action is retried on the next tick instead of being
silently marked done. The signal is marked CLOSED only after a successful
flatten; when the retry budget is spent the guard alerts once and leaves the
position to its broker SL."
```

### Shipped notes (Task 6)

The code blocks above are byte-identical to the shipped `main.py`, `mt5_executor.py` and
`tests/test_trade_lifecycle.py`. These deviations from the original plan text were needed to make
the watcher tests run against the shipped tick functions:

1. `_install_signal` installs the throw-away `StateManager` / `TradeLedger` with
   `monkeypatch.setattr` instead of a plain assignment, so the module globals are restored after
   every test. A plain assignment would leak the temporary state manager into the rest of the
   session.
2. `create_signal` is called WITHOUT a `timestamp` kwarg: the signature does not accept one, it
   stamps its own "now", and the aged timestamp is written by the following
   `update_signal("9001", sig)` call.
3. The test signals carry a `raw_tp1` so the tick does not fall into the fallback-TP1 branch: the
   fallback attachment notifies Saved Messages and would break the "the give-up alerts once" and
   "a failed action stays quiet" assertions. The stub executor therefore also implements
   `evaluate_dynamic_atr_engine`, which the tick calls once the guard has run.
4. The stub executor records the tick and ATR-engine calls, so the tests can assert that the tick
   still runs (and continues) exactly as before when the guard is disabled, that a contained
   action error does not stop the tick, and that the guard runs BEFORE the TP1 branches.
5. The plan's watcher stubs were MERGED into a single `_StubExecutor` that implements the tick,
   the pip size, the server time offset, the ATR engine and the lifecycle action, so every
   watcher test drives the same stub.
6. The guard was extracted into the module-level helper `_run_trade_lifecycle_guard`, which
   derives the direction and the symbol from the signal itself, so BOTH tick functions can call
   it. The inline block that used to live in `_process_pre_tp1_or_backstop_tick` is gone.
7. The guard now also runs in the RUNNER tick, BEFORE `manage_designated_runner`: a post-TP1
   trade is exactly the population the feature was designed for (the two longest held trades in
   the live ledger ran 21.0 h and 20.3 h and accounted for 61%% of the realised loss), and
   without this call the runner path was never de-risked and never flattened.
8. A NOT-COMPLETED action is retried on a BOUNDED cadence
   (`_LIFECYCLE_RETRY_INTERVAL_SECONDS = 30.0`) instead of on the next tick. This is a
   deliberate deviation from the design spec's "retried next tick": `apply_lifecycle_derisk`
   calls `cancel_signal_pending_orders` on EVERY invocation, so an unbounded retry is a broker
   sweep once per second per aged trade. The 6-hour flatten remains the backstop and the retry
   budget still exhausts.
9. A breakeven stop the market or the freeze level refuses is reported with its own reason,
   `skipped == "stop_not_valid"` (the existing reason `"no_live_price"` still wins when the
   price itself was unusable). Without it the refusal looked like a completed de-risk and was
   never retried.
10. The retry counter and the attempt stamp are persisted with an explicit PARTIAL update
    (`state_manager.update_signal` MERGES the dict). The whole-signal save is dirty-checked
    against a baseline taken AFTER the guard ran, so the guard's own mutations never reached the
    disk: `lifecycle_retry_count` was never saved, the give-up path was unreachable and the
    guard retried every second forever.


---

## Task 7: `main.py` -- the `/rlock` and `/lifecycle` commands

**Files:**
- Modify: `main.py` -- the `risk_lifecycle` import, the `_KNOWN_SAVED_COMMANDS` and
  `_KNOWN_SAVED_PREFIXES` sets, the command handlers inside `handle_saved_messages_command`,
  and the command list in the `/status` reply
- Modify: `tests/test_trade_lifecycle.py` (append the command tests)

**Interfaces:**
- Consumes: `risk_lifecycle.apply_rlock_command`, `risk_lifecycle.apply_lifecycle_command` (Task 1).
- Produces: the Saved Messages commands `/rlock` and `/lifecycle`.

- [x] **Step 1: the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- Saved Messages commands ---------------------------------------------

class _MockEvent:
    """A minimal Saved Messages event that records the replies it receives."""

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

    ev2 = _MockEvent("/lifecycle off")
    await main.handle_saved_messages_command(ev2)
    assert config.LIFECYCLE_ENABLED is False


@pytest.mark.asyncio
async def test_rlock_command_without_argument_reports_status_and_changes_nothing(
    monkeypatch, tmp_path
):
    """A bare /rlock is a status query: it must report and toggle nothing."""
    import config
    import main

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/rlock")
    await main.handle_saved_messages_command(ev)

    assert config.R_LOCK_ENABLED is False, "a status query must not enable the guard"
    assert ev.replies, "a status query must reply"
    assert any("R-Relative Profit Floor" in r for r in ev.replies)


@pytest.mark.asyncio
async def test_lifecycle_command_without_argument_reports_status_and_changes_nothing(
    monkeypatch, tmp_path
):
    """A bare /lifecycle is a status query: it must report and toggle nothing."""
    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/lifecycle")
    await main.handle_saved_messages_command(ev)

    assert config.LIFECYCLE_ENABLED is False, "a status query must not enable the guard"
    assert ev.replies, "a status query must reply"
    assert any("Trade Lifecycle Guard" in r for r in ev.replies)


@pytest.mark.asyncio
async def test_rlock_command_rejects_an_invalid_value_without_toggling(monkeypatch, tmp_path):
    import config
    import main

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/rlock maybe")
    await main.handle_saved_messages_command(ev)

    assert config.R_LOCK_ENABLED is False
    assert any("Usage" in r for r in ev.replies)


@pytest.mark.asyncio
async def test_lifecycle_command_rejects_an_invalid_value_without_toggling(monkeypatch, tmp_path):
    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/lifecycle maybe")
    await main.handle_saved_messages_command(ev)

    assert config.LIFECYCLE_ENABLED is False
    assert any("Usage" in r for r in ev.replies)


def test_rlock_and_lifecycle_are_known_saved_commands():
    """An unknown command would be treated as a numeric prompt reply."""
    import main

    assert main._is_known_saved_command("/rlock")
    assert main._is_known_saved_command("rlock")
    assert main._is_known_saved_command("/rlock on")
    assert main._is_known_saved_command("/lifecycle")
    assert main._is_known_saved_command("lifecycle")
    assert main._is_known_saved_command("/lifecycle on")


@pytest.mark.asyncio
async def test_rlock_toggle_is_visible_to_the_executor_policy(monkeypatch, tmp_path):
    """The toggle must reach the executor, not only the config module."""
    import config
    import main
    import mt5_executor

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/rlock on")
    await main.handle_saved_messages_command(ev)

    assert mt5_executor._cfg("R_LOCK_ENABLED", False) is True
    ex = mt5_executor.MT5Executor(dry_run=True)
    assert ex._get_lifecycle_policy().r_lock_enabled is True

    ev2 = _MockEvent("/lifecycle on")
    await main.handle_saved_messages_command(ev2)

    assert mt5_executor._cfg("LIFECYCLE_ENABLED", False) is True
    assert ex._get_lifecycle_policy().lifecycle_enabled is True
```

- [x] **Step 2: the tests fail** -- the commands are not recognised.

- [x] **Step 3: the commands**

In `main.py`, add the module-level helper immediately **before** `handle_saved_messages_command`
(it reports the per-signal ages and the next action the age guard would take, as the spec's §4.10
requires):

```python
async def _format_lifecycle_signal_ages() -> str:
    """Per-signal fill ages and the action the age guard would take right now.

    The `/lifecycle` query must tell the operator WHICH trades the guard will
    touch and WHEN, not only the thresholds. The fill age comes from
    `risk_lifecycle.evaluate_trade_age`, so it uses the same resolved fill
    clock (the earliest known fill) as the guard itself.
    """
    pol = read_lifecycle_policy(sys.modules.get("config") or config)
    if not pol.lifecycle_enabled:
        return ""
    now_utc = datetime.now(timezone.utc)
    server_offset = 0
    try:
        server_offset = int(await _mt5_call(executor.get_server_time_offset_seconds) or 0)
    except Exception:
        server_offset = 0
    try:
        active = state_mgr.get_active_signals() or []
    except Exception:
        return ""
    rows: List[str] = []
    for msg_id, sig in active:
        if not isinstance(sig, dict):
            continue
        try:
            res = evaluate_trade_age(sig, now_utc, pol, server_offset)
        except Exception:
            continue
        age = res.get("fill_age_seconds")
        age_txt = f"{int(age)}s" if isinstance(age, (int, float)) and not isinstance(age, bool) else "unknown"
        action = str(res.get("action") or "NONE")
        rows.append(
            f"• `{msg_id}` ({sig.get('direction', '?')} {sig.get('symbol', '')}): "
            f"age `{age_txt}` → next `{action}`"
        )
    if not rows:
        return ""
    return "\n\n**Active signals (what the guard would do now):**\n" + "\n".join(rows)

```

In `main.py`, insert the following block immediately **before** the `# LIVE CONTROL: /floatcap`
comment:

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
        if not arg:
            # The query form must also show which trades the guard would touch.
            reply_msg += await _format_lifecycle_signal_ages()
        await event.reply(reply_msg)
        return
```

The import at the top of `main.py` becomes:

```python
from risk_lifecycle import (
    apply_lifecycle_command,
    apply_rlock_command,
    evaluate_trade_age,
    read_lifecycle_policy,
    stamp_fill_times,
)
```

- [x] **Step 4: register the commands as known**

In `main.py`, in the `_KNOWN_SAVED_COMMANDS` set:

```python
    "/rlock", "rlock",
    "/lifecycle", "lifecycle",
```

and in the `_KNOWN_SAVED_PREFIXES` tuple, so that `/rlock on` and `/lifecycle on` are
recognised as commands and not as a numeric prompt reply:

```python
    "/rlock ", "rlock ",
    "/lifecycle ", "lifecycle ",
```

- [x] **Step 5: add the commands to the `/status` help line**

In `main.py`, the command list in the `/status` reply becomes:

```python
            f"📱 **Commands:** `/health`, `/status`, `/resume`, `/bank_tp1`, `/bank`, `/lotcap`, `/losslimit`, `/floatcap`, `/dynamicrisk`, `/atrtrail`, `/rlock`, `/lifecycle`, `/stats`, `/trade`, `/sl`, `/tp`, `/trail`, `/be`, `/partial`, `dailywin`, `dailyloss`, `/set_daily_loss`, `/status_daily_loss`, `/closeall`, `/sim_tp1 1|2|3|4`, `/sim_shortfall`, `/sim_trail 2|3|4`, `/test`, `/testrunner`, `/testorder`"
```

- [x] **Step 6: the tests pass.**

- [x] **Step 7: the full suite passes.**

### Shipped notes (Task 7)

- The plan described the command list as a "list"; in the shipped `main.py` it is the
  `_KNOWN_SAVED_COMMANDS` **set** plus the `_KNOWN_SAVED_PREFIXES` tuple. Both were updated.
- The plan only registered the bare command names. `_is_known_saved_command` matches exact
  names OR a known prefix, so `/rlock on` would have fallen through to the numeric-prompt
  path. The prefixes `/rlock  / rlock ` and `/lifecycle  / lifecycle ` were added as well.
- The handlers use `sys.modules.get("config") or config` so the toggled module is the one the
  executor reads through `mt5_executor._cfg`; `config.R_LOCK_ENABLED` and
  `config.LIFECYCLE_ENABLED` are the live attributes.
- A bare `/rlock` or `/lifecycle` is a status query: it reports the policy and changes
  nothing. An unrecognised argument is answered with the usage line and changes nothing.

## Task 8: `trade_ledger.py` -- schema 4 with cash MFE, giveback, R and age

**Files:**
- Modify: `trade_ledger.py` -- `build_trade_record` (the schema-4 telemetry block, the new record
  keys and `schema_version`), `append_trade` (the `schema_version` default), and the
  `risk_lifecycle.resolve_fill_time` import
- Modify: `mt5_executor.py` -- `MT5Executor.get_profit_currency_rate()` (the ACCOUNT-currency
  rate lookup that the giveback needs)
- Modify: `tests/test_trade_lifecycle.py` (append the ledger tests)

**Interfaces:**
- Consumes: `risk_lifecycle.resolve_fill_time` for the exit age (see the fill-time rule below).
- Consumes: `MT5Executor.get_profit_currency_rate(symbol)` -- the number of ACCOUNT-currency
  units one unit of the symbol's PROFIT currency is worth (see the currency rule below).
- Produces: every closed-signal record gains `mfe_cash`, `giveback_cash`,
  `initial_r_per_oz`, `r_at_exit`, `exit_age_seconds`, `lifecycle_action`; `schema_version`
  becomes `4`.

**Currency rule (fix round 1):** `mfe_cash` is a price-based amount in the symbol's PROFIT
currency (USD for XAUUSD), while `realized_pnl` comes from the broker in the ACCOUNT currency
(GBP). The two may only be subtracted once the MFE has been converted, so
`giveback_cash = mfe_cash * profit_currency_rate - realized_pnl` and is denominated in the
ACCOUNT currency. When the rate is unknown the giveback is `None` -- never a mixed-currency
number. `mfe_cash` is deliberately NOT converted: `r_at_exit` divides it by a price-based risk
amount, so the R multiple stays currency-neutral. The record also carries
`giveback_currency` (the account currency) and `fx_rate_to_account` (the rate actually used)
for auditability.

**Fill-time rule:** the ledger records the RESOLVED fill time via
`risk_lifecycle.resolve_fill_time(signal_data)` -- NEVER the raw
`signal_data["first_fill_at"]` key. The signal-level stamp records the first
`PENDING -> FILLED` transition, which for a normal zone signal is the first LIMIT fill (L2) and
is therefore LATER than the MARKET fill of L1 that is stamped on the layer.
`resolve_fill_time` returns the EARLIEST known fill, so `exit_age_seconds` measures the TRADE
and not its first limit fill.

- [x] **Step 1: the failing tests**

Append to `tests/test_trade_lifecycle.py`:

```python
# --- trade ledger schema 4 -----------------------------------------------

def _schema4_signal():
    """A representative closed signal carrying the schema-4 inputs."""
    return {
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
        "closed_at": "2026-11-19T09:19:51+00:00",
    }


def test_build_trade_record_adds_cash_mfe_and_giveback():
    from trade_ledger import build_trade_record

    rec = build_trade_record(
        signal_key="-100:42",
        signal_data=_schema4_signal(),
        realized_pnl=0.56,
        exit_reason="ATR_TRAIL",
        # The account currency IS the profit currency here, so the rate is 1.0
        # and the giveback is the MFE minus the realized PnL.
        profit_currency_rate=1.0,
    )

    assert rec["schema_version"] == 4
    # 11.12 $/oz * 0.05 lots * 100 oz/lot = 55.60
    assert rec["mfe_cash"] == pytest.approx(55.60)
    assert rec["giveback_cash"] == pytest.approx(55.04)
    assert rec["initial_r_per_oz"] == pytest.approx(20.0)
    # R at exit = MFE cash / risk cash = 55.60 / (20.0 * 0.05 * 100) = 0.556.
    # (The plan's draft expectation of 0.278 is inconsistent with its own
    #  mfe_cash/initial_r_per_oz expectations: 11.12 / 20.0 = 0.556.)
    assert rec["r_at_exit"] == pytest.approx(0.556)
    # 06:19:09 -> 09:19:51 = 3h00m42s
    assert rec["exit_age_seconds"] == pytest.approx(10842, abs=2)
    assert rec["lifecycle_action"] is None


def test_build_trade_record_tolerates_missing_new_fields():
    from trade_ledger import build_trade_record

    rec = build_trade_record(
        signal_key="-100:7",
        signal_data={"chat_id": -100, "message_id": 7, "direction": "BUY"},
        realized_pnl=1.0,
        exit_reason="MANUAL",
    )
    assert rec["schema_version"] == 4
    assert rec["mfe_cash"] is None
    assert rec["giveback_cash"] is None
    assert rec["exit_age_seconds"] is None
    assert rec["initial_r_per_oz"] is None
    assert rec["r_at_exit"] is None


def test_build_trade_record_keeps_every_schema_3_field():
    """Schema 4 is an UPGRADE: the old fields must not be dropped."""
    from trade_ledger import build_trade_record

    rec = build_trade_record(
        signal_key="-100:42",
        signal_data=_schema4_signal(),
        realized_pnl=0.56,
        exit_reason="ATR_TRAIL",
    )

    for key in (
        "schema_version",
        "closed_at",
        "opened_at",
        "signal_key",
        "chat_id",
        "message_id",
        "symbol",
        "direction",
        "entry_type",
        "total_lots",
        "filled_lots",
        "tickets",
        "layers_filled",
        "exit_reason",
        "exit_reason_code",
        "exit_price",
        "max_favorable_excursion",
        "giveback_from_peak",
        "layer_exits",
        "realized_pnl",
        "currency",
        "dry_run",
    ):
        assert key in rec, f"schema 4 dropped the schema 3 field {key}"
    assert rec["realized_pnl"] == pytest.approx(0.56)
    assert rec["max_favorable_excursion"]["peak_profit_per_oz"] == pytest.approx(11.12)


def test_build_trade_record_exit_age_uses_the_earliest_resolved_fill():
    """The L1 MARKET fill (on the layer) precedes the signal's LIMIT stamp."""
    from trade_ledger import build_trade_record

    sig = _schema4_signal()
    # The signal-level stamp is the first LIMIT fill (L2) and is LATER.
    sig["first_fill_at"] = "2026-11-19T07:19:09+00:00"
    sig["layers"]["L1"]["filled_at"] = "2026-11-19T06:19:09+00:00"
    sig["closed_at"] = "2026-11-19T09:19:51+00:00"

    rec = build_trade_record(
        signal_key="-100:42", signal_data=sig, realized_pnl=0.56, exit_reason="ATR_TRAIL"
    )

    assert rec["exit_age_seconds"] == pytest.approx(10842, abs=2), (
        "the trade age must be measured from the EARLIEST fill, not the signal stamp"
    )


def test_trade_ledger_reads_legacy_schema_3_records(tmp_path):
    """A record written by the old code must still be readable and complete."""
    import json

    from trade_ledger import TradeLedger

    path = tmp_path / "trades_ledger.jsonl"
    legacy = {
        "schema_version": 3,
        "closed_at": "2026-11-18T09:19:51+00:00",
        "opened_at": "2026-11-18T06:19:09+00:00",
        "signal_key": "-100:1",
        "chat_id": -100,
        "message_id": 1,
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "total_lots": 0.05,
        "filled_lots": 0.05,
        "realized_pnl": 12.34,
        "currency": "GBP",
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    ledger = TradeLedger(path)
    trades = ledger.read_trades()

    assert len(trades) == 1
    rec = trades[0]
    assert rec["schema_version"] == 3
    assert rec["realized_pnl"] == pytest.approx(12.34)
    assert rec["symbol"] == "XAUUSD.s"
    # The new fields are simply absent on an old record -- never an error.
    assert rec.get("mfe_cash") is None
    assert rec.get("exit_age_seconds") is None
    stats = ledger.get_stats()
    assert stats["total_trades"] == 1
    assert stats["total_realized_pnl"] == pytest.approx(12.34)


def test_append_trade_defaults_to_schema_4(tmp_path):
    from trade_ledger import TradeLedger

    ledger = TradeLedger(tmp_path / "trades_ledger.jsonl")
    entry = ledger.append_trade({"realized_pnl": 1.0})

    assert entry["schema_version"] == 4
```
```

- [x] **Step 2: the tests fail**
```

- [x] **Step 2: the tests fail** -- `schema_version` is `3` and the new keys are missing.

- [x] **Step 3: the schema-4 fields**

In `trade_ledger.py`, the computation block sits immediately **before** the
`record: Dict[str, Any] = {` assignment:

```python
    # --- Schema 4 telemetry: cash excursion, giveback, R and exit age -------
    # `max_favorable_excursion.peak_profit_per_oz` is a PER-OUNCE figure; it is
    # converted to cash here (contract = 100 oz per lot) so giveback is finally
    # measurable.
    CONTRACT_OZ_PER_LOT = 100.0
    mfe_per_oz = _safe_float((mfe_dict or {}).get("peak_profit_per_oz"), 0.0) if mfe_dict else 0.0
    lots_for_mfe = _safe_float(signal_data.get("total_lots"), 0.0)
    if lots_for_mfe <= 0.0:
        # Fall back to the layer-derived total when the signal carries no size.
        lots_for_mfe = _safe_float(total_lots, 0.0)
    mfe_cash: Optional[float] = None
    if mfe_per_oz > 0.0 and lots_for_mfe > 0.0:
        mfe_cash = round(mfe_per_oz * lots_for_mfe * CONTRACT_OZ_PER_LOT, 2)

    # The giveback can only be computed once the MFE -- a price-based amount in
    # the symbol's PROFIT currency -- has been converted into the ACCOUNT
    # currency of `realized_pnl`. The conversion is applied to the finished
    # record below; without a known rate the giveback stays UNKNOWN (None).
    giveback_cash: Optional[float] = None

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
            _f = _filled_at if isinstance(_filled_at, datetime) else datetime.fromisoformat(str(_filled_at).replace("Z", "+00:00"))
            _c = datetime.fromisoformat(str(_closed_at).replace("Z", "+00:00"))
            if _f.tzinfo is None:
                _f = _f.replace(tzinfo=timezone.utc)
            if _c.tzinfo is None:
                _c = _c.replace(tzinfo=timezone.utc)
            exit_age_seconds = int((_c - _f).total_seconds())
        except (TypeError, ValueError):
            exit_age_seconds = None

    lifecycle_action = signal_data.get("lifecycle_action")
```
```

The record gains the new keys:```

The record gains the new keys:

```python
        "mfe_cash": mfe_cash,
        "giveback_cash": giveback_cash,
        "giveback_currency": str(currency or "GBP"),
        "fx_rate_to_account": None,
        "initial_r_per_oz": initial_r_per_oz,
        "r_at_exit": r_at_exit,
        "exit_age_seconds": exit_age_seconds,
        "lifecycle_action": (str(lifecycle_action) if lifecycle_action else None),
```
```

and its version becomes:```

and its version becomes:

```python
        "schema_version": 4,
```

In `append_trade` the default becomes:

```python
        entry.setdefault("schema_version", 4)
```

`trade_ledger.py` imports the resolver:

```python
from risk_lifecycle import resolve_fill_time
```

The giveback is denominated by a helper that is applied to the finished record:

```python
def _apply_giveback_currency(record: Dict[str, Any], profit_currency_rate: Optional[float]) -> None:
    """Denominate `giveback_cash` in the ACCOUNT currency -- or leave it unknown.

    `mfe_cash` is a price-based cash amount in the symbol's PROFIT currency
    (USD for XAUUSD), while `realized_pnl` comes from the broker in the ACCOUNT
    currency (GBP). The two can only be subtracted once the MFE has been
    converted, so the giveback is `mfe_cash * rate - realized_pnl`, denominated
    in the account currency.

    Without a known rate the giveback stays `None`: a mixed-currency number is
    never written. `mfe_cash` itself is left untouched, so `r_at_exit` -- a
    ratio of two price-based amounts -- stays currency-neutral.
    """
    if record.get("fx_rate_to_account") is not None:
        return  # already denominated
    rate = _safe_float(profit_currency_rate, 0.0) if profit_currency_rate is not None else 0.0
    if rate <= 0.0:
        return
    mfe_cash = record.get("mfe_cash")
    if not _is_num(mfe_cash):
        return
    realized = _safe_float(record.get("realized_pnl"), 0.0)
    record["giveback_cash"] = round(float(mfe_cash) * rate - realized, 2)
    record["giveback_currency"] = str(record.get("currency") or "GBP")
    record["fx_rate_to_account"] = round(rate, 8)
```
`build_trade_record` takes the rate as an optional argument:

```python
def build_trade_record(
    signal_key: str,
    signal_data: Dict[str, Any],
    realized_pnl: float,
    exit_reason: str,
    currency: str = "GBP",
    dry_run: bool = False,
    extra: Optional[Dict[str, Any]] = None,
    profit_currency_rate: Optional[float] = None,
) -> Dict[str, Any]:
```
`TradeLedger.append_trade` and `TradeLedger.record_closed_signal` take the same optional
argument and thread it through:

```python
    def append_trade(
        self,
        record: Dict[str, Any],
        profit_currency_rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Append a single closed-trade record atomically as one JSON line."""
        entry = dict(record)
        entry.setdefault("schema_version", 4)
        entry.setdefault("closed_at", datetime.now(timezone.utc).isoformat())
        entry["realized_pnl"] = round(_safe_float(entry.get("realized_pnl"), 0.0), 2)
        # Every appended record carries the giveback contract, even when the
        # giveback is unknown (no rate) -- an absent key is not the same thing
        # as a known zero.
        entry.setdefault("giveback_cash", None)
        entry.setdefault("giveback_currency", str(record.get("currency") or "GBP"))
        entry.setdefault("fx_rate_to_account", None)
        _apply_giveback_currency(entry, profit_currency_rate)

        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        return entry
```
```python
    def record_closed_signal(
        self,
        signal_key: str,
        signal_data: Dict[str, Any],
        realized_pnl: float,
        exit_reason: str,
        currency: str = "GBP",
        dry_run: bool = False,
        extra: Optional[Dict[str, Any]] = None,
        profit_currency_rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Construct and append a canonical closed-signal ledger entry."""
        rec = build_trade_record(
            signal_key=signal_key,
            signal_data=signal_data,
            realized_pnl=realized_pnl,
            exit_reason=exit_reason,
            currency=currency,
            dry_run=dry_run,
            extra=extra,
            profit_currency_rate=profit_currency_rate,
        )
        return self.append_trade(rec, profit_currency_rate=profit_currency_rate)
```
The rate itself comes from the executor:

```python
    def get_profit_currency_rate(self, symbol: str) -> Optional[float]:
        """Return how many ACCOUNT-currency units one unit of the symbol's profit currency is worth.

        The ACCOUNT currency comes from the broker account (e.g. "GBP") and the
        symbol's PROFIT currency from its symbol info (e.g. "USD" for XAUUSD.s).
        When the two differ the rate is read from the FX symbol
        "{account}{profit_currency}" (e.g. "GBPUSD"), whose bid is quoted as
        account units per profit unit -- so the conversion factor is 1 / bid.

        Returns None when the conversion cannot be established: the caller must
        then record NO giveback rather than a mixed-currency number. Never guess.
        """
        try:
            financials = self.get_account_financials() or {}
            account_currency = str(financials.get("currency") or "").strip().upper()
            if not account_currency:
                return None

            symbol_info = self.get_symbol_info(symbol)
            if symbol_info is None:
                return None
            profit_currency = str(getattr(symbol_info, "currency_profit", None) or "USD").strip().upper()
            if not profit_currency:
                profit_currency = "USD"
            if account_currency == profit_currency:
                return 1.0

            fx_symbol = f"{account_currency}{profit_currency}"
            fx_info = self.get_symbol_info(fx_symbol)
            bid = _num(getattr(fx_info, "bid", 0.0), 0.0) if fx_info is not None else 0.0
            if bid <= 0.0:
                logger.warning(
                    f"No usable {fx_symbol} bid for {symbol} (profit currency {profit_currency}); "
                    "the giveback cannot be converted."
                )
                return None
            return 1.0 / bid
        except Exception as exc:
            logger.warning(f"Could not establish the profit currency rate for {symbol}: {exc}")
            return None

```
- [x] **Step 4: the tests pass.**- [x] **Step 4: the tests pass.**

- [x] **Step 5: the full suite passes.**

- [x] **Step 6 (fix round 1): the currency-correctness tests**

Appended to `tests/test_trade_lifecycle.py` (this block also covers the Task 9 call site and
the `/stats` label):

```python
# --- giveback currency correctness (A1) ----------------------------------

def _fx_signal():
    """A closed signal whose MFE is denominated in the symbol profit currency."""
    return {
        "timestamp": "2026-11-19T06:19:09+00:00",
        "chat_id": -100,
        "message_id": 42,
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "total_lots": 0.05,
        "filled_lots": 0.05,
        "layers": {"L1": {"ticket": 1, "status": "CLOSED", "lot": 0.01, "entry_price": 3740.0}},
        "max_favorable_excursion": {"peak_price": 3728.55, "peak_profit_per_oz": 11.12},
        "initial_r_per_oz": 20.0,
        "first_fill_at": "2026-11-19T06:19:09+00:00",
        "closed_at": "2026-11-19T09:19:51+00:00",
    }


def test_build_trade_record_converts_the_giveback_into_the_account_currency():
    """The MFE is USD and the realized PnL is GBP -- they can only meet after conversion."""
    from trade_ledger import build_trade_record

    rec = build_trade_record(
        signal_key="-100:42",
        signal_data=_fx_signal(),
        realized_pnl=0.56,
        exit_reason="ATR_TRAIL",
        currency="GBP",
        profit_currency_rate=0.7645,  # 1 / 1.30805 (the GBPUSD bid)
    )

    # mfe_cash stays in the symbol PROFIT currency (USD), so r_at_exit -- a
    # ratio of two USD amounts -- is unchanged and currency-neutral.
    assert rec["mfe_cash"] == pytest.approx(55.60)
    assert rec["r_at_exit"] == pytest.approx(0.556)
    # The giveback is converted into the ACCOUNT currency BEFORE the subtraction.
    assert rec["giveback_cash"] == pytest.approx(round(55.60 * 0.7645 - 0.56, 2))
    assert rec["giveback_cash"] != pytest.approx(55.04)
    assert rec["giveback_currency"] == "GBP"
    assert rec["fx_rate_to_account"] == pytest.approx(0.7645)


def test_build_trade_record_leaves_the_giveback_unknown_without_a_rate():
    """Without the rate the giveback is UNKNOWN -- never a mixed-currency number."""
    from trade_ledger import build_trade_record

    rec = build_trade_record(
        signal_key="-100:42",
        signal_data=_fx_signal(),
        realized_pnl=0.56,
        exit_reason="ATR_TRAIL",
        currency="GBP",
    )

    assert rec["giveback_cash"] is None
    assert rec["fx_rate_to_account"] is None
    # The price-based telemetry is unaffected by the missing rate.
    assert rec["mfe_cash"] == pytest.approx(55.60)
    assert rec["r_at_exit"] == pytest.approx(0.556)


def test_append_trade_denominates_the_giveback_in_the_account_currency(tmp_path):
    from trade_ledger import TradeLedger

    ledger = TradeLedger(tmp_path / "trades_ledger.jsonl")
    entry = ledger.append_trade(
        {"realized_pnl": 0.56, "currency": "GBP", "mfe_cash": 55.60},
        profit_currency_rate=0.7645,
    )

    assert entry["giveback_cash"] == pytest.approx(round(55.60 * 0.7645 - 0.56, 2))
    assert entry["giveback_currency"] == "GBP"
    assert entry["fx_rate_to_account"] == pytest.approx(0.7645)


def test_append_trade_leaves_the_giveback_unknown_without_a_rate(tmp_path):
    from trade_ledger import TradeLedger

    ledger = TradeLedger(tmp_path / "trades_ledger.jsonl")
    entry = ledger.append_trade({"realized_pnl": 0.56, "currency": "GBP", "mfe_cash": 55.60})

    assert entry["giveback_cash"] is None
    assert entry["fx_rate_to_account"] is None


def _rate_executor(monkeypatch, account_currency, symbols):
    """An executor stub that answers the account financials and the symbol lookups."""
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=True)
    monkeypatch.setattr(ex, "get_account_financials", lambda: {"currency": account_currency})
    monkeypatch.setattr(ex, "get_symbol_info", lambda symbol: symbols.get(symbol))
    return ex


def test_executor_profit_currency_rate_is_one_when_the_currencies_match(monkeypatch):
    from types import SimpleNamespace

    ex = _rate_executor(monkeypatch, "USD", {"XAUUSD.s": SimpleNamespace(currency_profit="USD")})

    assert ex.get_profit_currency_rate("XAUUSD.s") == pytest.approx(1.0)


def test_executor_profit_currency_rate_converts_a_usd_symbol_on_a_gbp_account(monkeypatch):
    """A GBP account trading a USD symbol converts at 1 / the GBPUSD bid."""
    from types import SimpleNamespace

    ex = _rate_executor(
        monkeypatch,
        "GBP",
        {
            "XAUUSD.s": SimpleNamespace(currency_profit="USD"),
            "GBPUSD": SimpleNamespace(currency_base="GBP", currency_profit="USD", bid=1.30805),
        },
    )

    assert ex.get_profit_currency_rate("XAUUSD.s") == pytest.approx(1.0 / 1.30805)


def test_executor_profit_currency_rate_defaults_the_profit_currency_to_usd(monkeypatch):
    from types import SimpleNamespace

    ex = _rate_executor(
        monkeypatch,
        "GBP",
        {
            "XAUUSD.s": SimpleNamespace(),
            "GBPUSD": SimpleNamespace(bid=1.30805),
        },
    )

    assert ex.get_profit_currency_rate("XAUUSD.s") == pytest.approx(1.0 / 1.30805)


def test_executor_profit_currency_rate_is_none_when_the_fx_symbol_is_unavailable(monkeypatch):
    from types import SimpleNamespace

    ex = _rate_executor(monkeypatch, "GBP", {"XAUUSD.s": SimpleNamespace(currency_profit="USD")})

    assert ex.get_profit_currency_rate("XAUUSD.s") is None


def test_executor_profit_currency_rate_is_none_when_the_fx_symbol_has_no_price(monkeypatch):
    from types import SimpleNamespace

    ex = _rate_executor(
        monkeypatch,
        "GBP",
        {
            "XAUUSD.s": SimpleNamespace(currency_profit="USD"),
            "GBPUSD": SimpleNamespace(bid=0.0),
        },
    )

    assert ex.get_profit_currency_rate("XAUUSD.s") is None


def test_stats_excludes_an_unknown_giveback_from_the_total():
    """A missing giveback is unknown, not zero."""
    import main

    records = [
        {
            "closed_at": "2026-11-19T09:00:00+00:00",
            "realized_pnl": 10.0,
            "giveback_cash": 12.34,
            "giveback_currency": "GBP",
        },
        {
            "closed_at": "2026-11-19T10:00:00+00:00",
            "realized_pnl": 5.0,
            "giveback_cash": None,
            "giveback_currency": "GBP",
        },
    ]

    allt = main._build_ledger_stats(records)["all_time"]

    assert allt["total_giveback"] == pytest.approx(12.34)
    assert allt["giveback_currency"] == "GBP"


@pytest.mark.asyncio
async def test_stats_labels_the_giveback_with_the_record_currency(monkeypatch, tmp_path):
    """The giveback label comes from the record, not from the live account currency."""
    import json

    import config
    import main

    ledger = tmp_path / "trades_ledger.jsonl"
    rec = {
        "closed_at": "2026-11-19T09:00:00+00:00",
        "realized_pnl": 10.0,
        "giveback_cash": 20.0,
        "giveback_currency": "USD",
    }
    ledger.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "TRADES_LEDGER_PATH", ledger)
    monkeypatch.setattr(main, "executor", _StubFinancialsExecutor())
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/stats")
    await main.handle_saved_messages_command(ev)

    body = ev.replies[0]
    assert "20.00 USD" in body


@pytest.mark.asyncio
async def test_record_signal_to_ledger_passes_the_profit_currency_rate(monkeypatch, tmp_path):
    """The call site looks the rate up once per record and hands it to the ledger."""
    import main

    main = _install_signal(monkeypatch, tmp_path, {})

    captured = {}

    class _RateExecutor:
        dry_run = False

        def get_account_financials(self):
            return {"currency": "GBP", "formatted": ""}

        def get_profit_currency_rate(self, symbol):
            captured["calls"] = captured.get("calls", 0) + 1
            captured["symbol"] = symbol
            return 0.7645

    monkeypatch.setattr(main, "executor", _RateExecutor())

    def _capture(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(main.trade_ledger, "record_closed_signal", _capture)

    sig = {
        "message_id": 9001,
        "chat_id": -100,
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "market_tickets": [101],
        "banked_profit": 1.5,
        "layers": {},
    }

    rec = await main._record_signal_to_ledger("9001", sig, exit_reason="MANUAL", currency="GBP")

    assert rec is not None
    assert captured["calls"] == 1, "the rate is looked up once per record"
    assert captured["symbol"] == "XAUUSD.s"
    assert captured["profit_currency_rate"] == pytest.approx(0.7645)


@pytest.mark.asyncio
async def test_record_signal_to_ledger_survives_a_failing_rate_lookup(monkeypatch, tmp_path):
    """A failed rate lookup must never break the ledger write."""
    import main

    main = _install_signal(monkeypatch, tmp_path, {})

    captured = {}

    class _BrokenRateExecutor:
        dry_run = False

        def get_account_financials(self):
            return {"currency": "GBP", "formatted": ""}

        def get_profit_currency_rate(self, symbol):
            raise RuntimeError("MT5 is not connected")

    monkeypatch.setattr(main, "executor", _BrokenRateExecutor())

    def _capture(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(main.trade_ledger, "record_closed_signal", _capture)

    sig = {
        "message_id": 9001,
        "chat_id": -100,
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "market_tickets": [101],
        "banked_profit": 1.5,
        "layers": {},
    }

    rec = await main._record_signal_to_ledger("9001", sig, exit_reason="MANUAL", currency="GBP")

    assert rec is not None
    assert captured["profit_currency_rate"] is None


# --- the "no fill stamp" fallback must be logged once (A11) ---------------

def test_the_fill_age_fallback_is_logged_once_per_signal(caplog):
    """The spec requires the no-fill-stamp fallback to be logged once per signal."""
    import logging

    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    sig = {
        "timestamp": (now - timedelta(hours=9)).isoformat(),
        "market_tickets": [101],
        "pending_tickets": [],
    }

    with caplog.at_level(logging.INFO, logger="copier.lifecycle"):
        rl.evaluate_trade_age(sig, now, _policy())
        rl.evaluate_trade_age(sig, now, _policy())

    messages = [r.getMessage() for r in caplog.records if r.name == "copier.lifecycle"]
    assert len(messages) == 1, f"the fallback must be logged once, got {messages}"
    assert "fill stamp" in messages[0].lower()
    assert sig.get("_fill_age_fallback_logged") is True
```
- [x] **Step 7 (fix round 1): the tests pass.**

### Shipped notes (Task 8)

- The plan's test snippet called- The plan's test snippet called `build_trade_record(sig, signal_key=...)` -- that passes
  `signal_data` positionally into `signal_key` and raises
  `TypeError: build_trade_record() got multiple values for argument 'signal_key'`. The shipped
  test calls it as `build_trade_record(signal_key=..., signal_data=..., realized_pnl=...,
  exit_reason=...)`, the real signature (`exit_reason` is required).
- The plan's fixture used `closed_at` `09:18:51` and expected `exit_age_seconds` `10842`. Those
  two disagree: `06:19:09 -> 09:18:51` is `10782` s, and `10842` s is
  `06:19:09 -> 09:19:51`. The shipped fixture uses `09:19:51`, so the planned expectation
  `10842` is kept.
- The plan's test expected `r_at_exit == 0.278`, which is inconsistent with the plan's own
  `mfe_cash` and `initial_r_per_oz` expectations: `mfe_cash / (initial_r_per_oz * lots * 100)`
  = `55.60 / 100` = `0.556`, and `peak_profit_per_oz / initial_r_per_oz` = `11.12 / 20.0` =
  `0.556` as well. No formula satisfies `0.278` together with `mfe_cash == 55.60` and
  `initial_r_per_oz == 20.0`. The shipped test asserts `0.556` (the correct R multiple);
  shipping `0.278` would have halved every real trade's R at exit.
- `lots_for_mfe` prefers the signal's own `total_lots` and falls back to the layer-derived
  total. `build_trade_record` derives `total_lots` from the LAYERS, and the plan's fixture
  carries a single 0.01-lot layer next to a 0.05-lot signal; using the signal's size is what
  makes `mfe_cash` the cash excursion of the whole trade. Live signals carry no `total_lots`
  key, so the layer-derived total is used there.
- Schema 4 is an UPGRADE: the schema-3 fields are all still written, and a record written by
  the old code is still read (the new keys are simply absent). Both are covered by the shipped
  tests.

- (fix round 1) The giveback was mixing currencies: `mfe_cash` is a price difference on
  XAUUSD (USD) and `realized_pnl` comes from the broker in the ACCOUNT currency (GBP), so
  `mfe_cash - realized_pnl` was overstated by the GBP/USD rate (`1.31x on this account) while
  `/stats` labelled the result `GBP`. The giveback is now computed in ONE currency --
  `mfe_cash * profit_currency_rate - realized_pnl`, denominated in the account currency -- and
  is `None` when the rate cannot be established (fail closed, never a mixed number).
- (fix round 1) `mfe_cash` is deliberately left in the symbol's profit currency: `r_at_exit`
  divides it by `initial_r_per_oz * lots * 100`, a price-based amount, so the R multiple stays
  currency-neutral and unchanged.
- (fix round 1) `MT5Executor.get_profit_currency_rate()` returns `None` -- never a guess --
  when the account currency is unknown, the symbol info is missing, or the FX symbol
  `{account}{profit_currency}` (e.g. `GBPUSD`) has no usable bid. Verified on this machine:
  MT5 exposes `GBPUSD` with `bid=1.30805` (`currency_base='GBP'`, `currency_profit='USD'`);
  `USDGBP` does not exist.
- (fix round 1) The one pre-existing test that asserted the mixed-currency number,
  `test_build_trade_record_adds_cash_mfe_and_giveback`, now passes
  `profit_currency_rate=1.0` (an account whose currency IS the profit currency). Every
  assertion in it is unchanged; the rate-aware behaviour is covered by the additional tests.
- (fix round 1) The `no fill stamp -> fall back to signal age` fallback in
  `risk_lifecycle.evaluate_trade_age` is now logged ONCE per signal through the
  `copier.lifecycle` logger, using the `_fill_age_fallback_logged` marker on the signal dict
  (the same idiom as `_r_lock_skipped_logged`).

## Task 9: `main.py` -- the `/stats` command

**Files:**
- Modify: `main.py` -- the module-level helper `_build_ledger_stats()`, the `/stats` handler
  inside `handle_saved_messages_command`, the `_KNOWN_SAVED_COMMANDS` set and the `/status`
  help line
- Modify: `tests/test_trade_lifecycle.py` (append the stats tests)

**Interfaces:**
- Consumes: `trades_ledger.jsonl` on disk (Task 8's records).
- Produces: `main._build_ledger_stats(records: List[Dict[str, Any]], now: Optional[datetime] =
  None) -> Dict[str, Any]` and the `/stats` Saved Messages command.

- [x] **Step 1: the failing tests**

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

    # The helper returns one aggregate per window; the plan's draft asserted
    # these fields on the top level, so they are asserted on the all-time
    # aggregate that carries them.
    allt = stats["all_time"]
    assert allt["count"] == 2
    assert allt["net_pnl"] == pytest.approx(5.0)
    assert allt["wins"] == 1
    assert allt["losses"] == 1
    assert allt["win_rate"] == pytest.approx(0.5)
    assert allt["total_giveback"] == pytest.approx(37.0)
    assert allt["max_exit_age_seconds"] == 90000
    assert allt["lifecycle_closes"] == 1
    assert allt["exit_reasons"]["ATR_TRAIL"] == 1
    assert allt["exit_reasons"]["BROKER_SL"] == 1
    assert set(stats) == {"today", "seven_day", "all_time"}


def test_build_ledger_stats_windows_are_time_scoped():
    import main
    from datetime import datetime, timezone

    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
    records = [
        {"closed_at": "2026-11-19T09:00:00+00:00", "realized_pnl": 10.0},
        {"closed_at": "2026-11-17T09:00:00+00:00", "realized_pnl": 20.0},
        {"closed_at": "2026-01-01T09:00:00+00:00", "realized_pnl": 30.0},
    ]

    stats = main._build_ledger_stats(records, now=now)

    assert stats["today"]["count"] == 1
    assert stats["today"]["net_pnl"] == pytest.approx(10.0)
    assert stats["seven_day"]["count"] == 2
    assert stats["seven_day"]["net_pnl"] == pytest.approx(30.0)
    assert stats["all_time"]["count"] == 3
    assert stats["all_time"]["net_pnl"] == pytest.approx(60.0)


def test_build_ledger_stats_tolerates_garbage_records():
    import main

    stats = main._build_ledger_stats([None, "nonsense", {"realized_pnl": None}])

    assert stats["all_time"]["count"] == 1
    assert stats["all_time"]["net_pnl"] == pytest.approx(0.0)
    assert stats["all_time"]["total_giveback"] == pytest.approx(0.0)


class _StubFinancialsExecutor:
    """An executor stub that answers the account financials call."""

    def get_account_financials(self):
        return {"currency": "GBP", "balance": 1000.0, "formatted": "• Balance: 1000.00 GBP"}


@pytest.mark.asyncio
async def test_stats_command_reports_no_ledger_data(monkeypatch, tmp_path):
    """A missing ledger must degrade gracefully, never raise."""
    import config
    import main

    monkeypatch.setattr(config, "TRADES_LEDGER_PATH", tmp_path / "missing_ledger.jsonl")
    monkeypatch.setattr(main, "executor", _StubFinancialsExecutor())
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/stats")
    await main.handle_saved_messages_command(ev)

    assert ev.replies
    assert "no ledger data" in ev.replies[0].lower()


@pytest.mark.asyncio
async def test_stats_command_tolerates_a_corrupt_ledger(monkeypatch, tmp_path):
    """A corrupt ledger must degrade gracefully, never raise."""
    import config
    import main

    ledger = tmp_path / "trades_ledger.jsonl"
    ledger.write_text("this is not json\n{broken\n", encoding="utf-8")
    monkeypatch.setattr(config, "TRADES_LEDGER_PATH", ledger)
    monkeypatch.setattr(main, "executor", _StubFinancialsExecutor())
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/stats")
    await main.handle_saved_messages_command(ev)

    assert ev.replies
    assert "no ledger data" in ev.replies[0].lower()


@pytest.mark.asyncio
async def test_stats_command_reports_the_ledger_summary(monkeypatch, tmp_path):
    import json

    import config
    import main

    ledger = tmp_path / "trades_ledger.jsonl"
    rec = {
        "schema_version": 4,
        "closed_at": "2026-11-19T09:00:00+00:00",
        "realized_pnl": 10.0,
        "exit_reason_code": "ATR_TRAIL",
        "mfe_cash": 30.0,
        "giveback_cash": 20.0,
        "exit_age_seconds": 7200,
        "lifecycle_action": None,
    }
    ledger.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "TRADES_LEDGER_PATH", ledger)
    monkeypatch.setattr(main, "executor", _StubFinancialsExecutor())
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/stats")
    await main.handle_saved_messages_command(ev)

    assert ev.replies
    body = ev.replies[0]
    assert "Trade Stats" in body
    assert "+10.00 GBP" in body
    assert "giveback" in body


def test_stats_is_a_known_saved_command():
    import main

    assert main._is_known_saved_command("/stats")
    assert main._is_known_saved_command("stats")


@pytest.mark.asyncio
async def test_lifecycle_command_lists_the_active_signals_and_their_ages(monkeypatch, tmp_path):
    """The spec requires the `/lifecycle` query to show per-signal ages and the next action."""
    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", True)

    sig = _lifecycle_signal()
    sig["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sig["first_fill_at"] = sig["timestamp"]
    _install_signal(monkeypatch, tmp_path, sig)

    class _OffsetExecutor:
        def get_server_time_offset_seconds(self):
            return 0

    monkeypatch.setattr(main, "executor", _OffsetExecutor())

    ev = _MockEvent("/lifecycle")
    await main.handle_saved_messages_command(ev)

    body = "\n".join(ev.replies)
    assert "Active signals" in body
    assert "9001" in body
    assert "DERISK" in body


@pytest.mark.asyncio
async def test_lifecycle_command_omits_the_signal_list_when_the_guard_is_off(monkeypatch, tmp_path):
    """With the guard disabled there is no action to preview."""
    import config
    import main

    monkeypatch.setattr(config, "LIFECYCLE_ENABLED", False)

    sig = _lifecycle_signal()
    sig["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    _install_signal(monkeypatch, tmp_path, sig)

    ev = _MockEvent("/lifecycle")
    await main.handle_saved_messages_command(ev)

    body = "\n".join(ev.replies)
    assert "Active signals" not in body
```
```

- [x] **Step 2: the tests fail** -- `AttributeError:```

- [x] **Step 2: the tests fail** -- `AttributeError: module 'main' has no attribute
  '_build_ledger_stats'`.

- [x] **Step 3: the stats helper**

Added to `main.py` as a module-level function:

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

    def _giveback_currency(subset: List[Dict[str, Any]]) -> Optional[str]:
        """The currency the giveback total is denominated in, taken from the records."""
        for r in subset:
            if r.get("giveback_cash") is None:
                continue
            curr = str(r.get("giveback_currency") or "").strip()
            if curr:
                return curr
        return None

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
            "giveback_currency": _giveback_currency(subset),
            "max_exit_age_seconds": max(ages) if ages else 0,
            "lifecycle_closes": sum(1 for r in subset if r.get("lifecycle_action")),
            "exit_reasons": reasons,
        }

    return {
        "today": _aggregate(_slice(cutoff_today)),
        "seven_day": _aggregate(_slice(cutoff_7d)),
        "all_time": _aggregate(rows),
    }




# ---------------------------------------------------------------------------
# EXPOSURE SHUTDOWN & DAILY RISK LIMITS
```
```

- [x] **Step 4: the `/stats` command**```

- [x] **Step 4: the `/stats` command**

In `handle_saved_messages_command`, immediately before the `# LIVE CONTROL: /floatcap` block:

```python
    # LIVE CONTROL: /stats — closed-trade analytics from the trade ledger
    if text in ("/stats", "stats"):
        _cfg_mod = sys.modules.get("config") or config
        ledger_path = Path(getattr(_cfg_mod, "TRADES_LEDGER_PATH", Path("trades_ledger.jsonl")))
        records: List[Dict[str, Any]] = []
        try:
            if ledger_path.exists():
                # Streamed line by line: the ledger is JSONL, so no whole-file
                # text blob is ever held in memory.
                with ledger_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict):
                            records.append(obj)
        except Exception as exc:
            logger.warning(f"[/stats] could not read ledger {ledger_path}: {exc}")

        if not records:
            await event.reply(f"📊 **Trade Stats**\n• No ledger data found at `{ledger_path}`.")
            return

        stats = _build_ledger_stats(records)
        today = stats["today"]
        seven = stats["seven_day"]
        allt = stats["all_time"]
        try:
            fin = await _mt5_call(executor.get_account_financials)
        except Exception as exc:
            logger.warning(f"[/stats] account financials unavailable: {exc}")
            fin = {"currency": "GBP", "formatted": ""}
        curr = fin.get("currency", "GBP")

        def _line(label: str, agg: Dict[str, Any]) -> str:
            # The giveback is denominated in the account currency of the records
            # themselves, so it is labelled from the ledger -- not from the live
            # account currency, and not with a hard-coded currency.
            giveback_currency = agg.get("giveback_currency") or curr
            return (
                f"• **{label}:** `{agg['net_pnl']:+.2f} {curr}` | "
                f"`{agg['count']}` trades | win rate `{agg['win_rate'] * 100:.0f}%` | "
                f"giveback `{agg['total_giveback']:.2f} {giveback_currency}` | "
                f"max age `{agg['max_exit_age_seconds']}s`\n"
            )

        await event.reply(
            "📊 **Trade Stats**\n"
            + _line("Today", today)
            + _line("7 days", seven)
            + _line("All time", allt)
            + f"• Lifecycle closes: `{allt['lifecycle_closes']}`\n"
            + f"• Exit reasons: `{allt['exit_reasons']}`\n\n"
            f"{fin.get('formatted', '')}"
        )
        return
```
```

- [x] **Step 5: register `/stats```

- [x] **Step 4b (fix round 1): the rate reaches the ledger**

`_record_signal_to_ledger` looks the rate up ONCE per record and tolerates every failure --
a failing lookup simply yields a record with no giveback:

```python
async def _record_signal_to_ledger(
    msg_id: Any,
    sig: Dict[str, Any],
    exit_reason: str,
    currency: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Persist a closed signal once to trades_ledger.jsonl when all its positions are closed.
    Skips purely unentered/invalidated setups and avoids double recording via `ledger_recorded`.
    """
    if not isinstance(sig, dict) or sig.get("ledger_recorded") or not _has_executed_exposure(sig):
        return None

    chat_id = sig.get("chat_id")
    mid_str = str(msg_id)
    if chat_id is not None and ":" not in mid_str:
        signal_key = f"{chat_id}:{mid_str}"
    else:
        signal_key = mid_str

    if not currency:
        fin = await _mt5_call(executor.get_account_financials)
        currency = fin.get("currency", "GBP")
    srv_date = await _trading_day_str_async()
    realized_pnl = round(float(sig.get("banked_profit") or 0.0), 2)

    # The giveback is the cash MFE -- a price-based amount in the symbol's
    # PROFIT currency -- minus the realized PnL, which the broker reports in
    # the ACCOUNT currency. The two can only be subtracted once the MFE has
    # been converted, so the rate is looked up ONCE per record here. Every
    # failure is tolerated: the record then simply carries no giveback rather
    # than a mixed-currency number.
    profit_currency_rate: Optional[float] = None
    try:
        _rate_fn = getattr(executor, "get_profit_currency_rate", None)
        if _rate_fn is not None:
            profit_currency_rate = await _mt5_call(
                _rate_fn, sig.get("symbol", DEFAULT_SYMBOL)
            )
    except Exception as exc:
        logger.warning(f"Could not establish the profit currency rate for {signal_key}: {exc}")
        profit_currency_rate = None

    try:
        entry = trade_ledger.record_closed_signal(
            signal_key=signal_key,
            signal_data=sig,
            realized_pnl=realized_pnl,
            exit_reason=exit_reason,
            currency=currency,
            dry_run=executor.dry_run,
            extra={"server_date": srv_date},
            profit_currency_rate=profit_currency_rate,
        )
        sig["ledger_recorded"] = True
        state_mgr.update_signal(msg_id, {"ledger_recorded": True}, chat_id=chat_id)
        return entry
    except Exception as exc:
        logger.warning(f"Failed to append closed trade ledger entry for {signal_key}: {exc}")
        return None
```

- [x] **Step 5: register `/stats` and add it to the help line**

In `main.py`, in the `_KNOWN_SAVED_COMMANDS` set:In `main.py`, in the `_KNOWN_SAVED_COMMANDS` set:

```python
    "/stats", "stats",
```

and in the `/status` reply:

```python
            f"📱 **Commands:** `/health`, `/status`, `/resume`, `/bank_tp1`, `/bank`, `/lotcap`, `/losslimit`, `/floatcap`, `/dynamicrisk`, `/atrtrail`, `/rlock`, `/lifecycle`, `/stats`, `/trade`, `/sl`, `/tp`, `/trail`, `/be`, `/partial`, `dailywin`, `dailyloss`, `/set_daily_loss`, `/status_daily_loss`, `/closeall`, `/sim_tp1 1|2|3|4`, `/sim_shortfall`, `/sim_trail 2|3|4`, `/test`, `/testrunner`, `/testorder`"
```

- [x] **Step 6: the tests pass.**

- [x] **Step 7: the full suite passes.**

### Shipped notes (Task 9)

- The plan's test asserted the aggregate fields on the top level of the helper's return value,
  but the plan's own helper returns `{"today": ..., "seven_day": ..., "all_time": ...}`. The
  shipped test asserts the same values on the `all_time` aggregate.
- The ledger is read line by line (JSONL), so no whole-file text blob is ever held in memory;
  only the parsed records are kept. The live ledger is ~19 KB (19 closed trades), so the read is
  bounded in practice.
- The account-financials call is wrapped: if the broker connection is down the command still
  answers with the ledger figures instead of raising.
- A missing or corrupt ledger answers "No ledger data found at `<path>`" -- it never raises.

- (fix round 1) The giveback is no longer labelled with a hard-coded currency: each window's
  aggregate carries a `giveback_currency` taken from the records that contributed to the
  total, and the `/stats` line uses it (falling back to the live account currency only when
  no record carries one -- e.g. for legacy schema-3 records).
- (fix round 1) Records whose `giveback_cash` is `None` (the FX rate was unknown) are
  EXCLUDED from `total_giveback`: unknown is not zero.
- (fix round 1) `_record_signal_to_ledger` obtains the profit-currency rate from the executor
  once per record and passes it to `trade_ledger.record_closed_signal`. Any failure -- the
  lookup raising, the executor lacking the method, or the rate coming back `None` -- yields a
  record with no giveback, and never breaks the ledger write.

## Task 10: `scripts/ab_replay_proof.py` — Arm C

**Files:**
- Modify: `scripts/ab_replay_proof.py:52` (`ARM_B_CALIBRATED_PARAMS` — add the Arm C params)
- Modify: `scripts/ab_replay_proof.py:684` (`_build_signal_record` — stamp `initial_sl` on the replayed layers)
- Modify: `scripts/ab_replay_proof.py:828` (`_pin_arm_b_params` — add `_pin_arm_c_params`)
- Modify: `scripts/ab_replay_proof.py:846` (add `_ReplayClock` / `_install_replay_clock`)
- Modify: `scripts/ab_replay_proof.py:922` / `:940` (`_run_single_trade_worker` — drive the simulated clock)
- Modify: `scripts/ab_replay_proof.py:1080-1111` (`_run_worker_main` — handle `arm_mode == "c"`)
- Modify: `scripts/ab_replay_proof.py:1243-1262` (`_evaluate_gates` — add the C-vs-B gate `g5`)
- Modify: `scripts/ab_replay_proof.py:1300-1396` (`_render_markdown` — render Arm C)
- Modify: `scripts/ab_replay_proof.py:1403-1470` (`main` — add the `--arm c` branch)

**Interfaces:**
- Consumes: every feature from Tasks 1-9.
- Produces: `--arm c` runs the same 12 recorded trades with the R lock and the age guard enabled, and the gate `g5` asserts Arm C ≥ Arm B on net P&L and ≤ Arm B on tail loss.

> **Deviation from the original task text (recorded 2026-11-21).** The task text
> assumed the trade-age guard reads the replayed week. It does not: the guard
> reads the WALL clock, so on a replay of the recorded Nov 17-19 week every
> recorded trade would look four days old and the guard would flatten it on its
> first tick. The replay therefore runs Arms B and C on a SIMULATED clock that
> follows the M1 bars (Step 3). Arm A keeps the wall clock because it is the
> frozen historical baseline. The replayed layers also need `initial_sl`
> (Step 5): without it the R lock is silently skipped and Arm C would not
> exercise the R-relative profit floor at all.

- [ ] **Step 1: Add the Arm C parameter block**

In `scripts/ab_replay_proof.py`, immediately after the `ARM_B_CALIBRATED_PARAMS` dict, add:

```python
# Arm C params: Arm B plus the R-relative profit floor and the trade-age guard.
ARM_C_CALIBRATED_PARAMS: Dict[str, Any] = {
    "R_LOCK_ENABLED": True,
    "LIFECYCLE_ENABLED": True,
    "LIFECYCLE_DERISK_SECONDS": 14400,
    "LIFECYCLE_FLATTEN_SECONDS": 28800,
    "LIFECYCLE_PENDING_EXPIRE_SECONDS": 43200,
    "LIFECYCLE_SESSION_FLATTEN_ENABLED": False,
}
```

- [ ] **Step 2: Add the Arm C pinning function**

In `scripts/ab_replay_proof.py`, immediately after `_pin_arm_b_params`, add:

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

- [ ] **Step 3: Add the simulated replay clock**

The trade-age guard calls `datetime.now(timezone.utc)` inside `main.py`, so the
replay has to hand `main` a clock that follows the recorded bars. Add this
immediately after `_pin_arm_c_params`:

```python
class _ReplayClock:
    """The simulated UTC clock of a replay worker.

    The trade-age guard reads the wall clock (datetime.now(timezone.utc)),
    but the replay simulates the RECORDED Nov 17-19 week. Without a simulated
    clock every recorded trade would look days old and the guard would flatten
    it on its very first tick, which is not a replay of the recorded week at
    all. The clock therefore follows the M1 bars, and Arm A keeps the wall
    clock because it is the frozen historical baseline.
    """

    def __init__(self) -> None:
        self._now: Optional[datetime] = None

    def set_epoch(self, epoch_sec: int) -> None:
        """Move the simulated clock to the given UTC epoch second."""
        self._now = datetime.fromtimestamp(int(epoch_sec), tz=timezone.utc)

    def now(self, tz: Optional[Any] = None) -> datetime:
        """Return the simulated time (the wall clock when no bar was seen yet)."""
        base = self._now or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        if tz is None:
            return base.replace(tzinfo=None)
        return base.astimezone(tz)


_REPLAY_CLOCK = _ReplayClock()


def _install_replay_clock(main_mod: Any) -> None:
    """Make main read the simulated replay clock instead of the wall clock."""
    real_datetime = datetime

    class _SimulatedDatetime(real_datetime):
        @classmethod
        def now(cls, tz: Optional[Any] = None) -> datetime:
            return _REPLAY_CLOCK.now(tz)

    main_mod.datetime = _SimulatedDatetime
```

- [ ] **Step 4: Handle `arm_mode == "c"` in the worker**

In `scripts/ab_replay_proof.py`, inside `_run_worker_main`, the Arm C branch pins
the Arm B params, then the Arm C params, and installs the simulated clock:

```python
        if arm_mode in ("b", "c"):
            for k, v in ARM_B_CALIBRATED_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        if arm_mode == "c":
            for k, v in ARM_C_CALIBRATED_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)

        import config  # noqa: E402
        import main  # noqa: E402
        import mt5_executor  # noqa: E402
        from state_manager import StateManager  # noqa: E402
        from trade_ledger import TradeLedger  # noqa: E402

        assert config.DRY_RUN is True, "DRY_RUN must be True in replay worker"
        assert Path(config.STATE_FILE_PATH).parent == rdir, "STATE_FILE_PATH isolation failed"

        if arm_mode == "a":
            if hasattr(config, "PROFIT_RATCHET_ENABLED"):
                raise RuntimeError("Arm A contamination: PROFIT_RATCHET_ENABLED found in 4544f7f worktree!")
        elif arm_mode == "b":
            _pin_arm_b_params(config, mt5_executor, main)
        else:
            _pin_arm_b_params(config, mt5_executor, main)
            _pin_arm_c_params(config, mt5_executor, main)

        # Arms B and C run on the simulated replay clock: the trade-age guard
        # reads the wall clock, and the replay simulates the RECORDED week.
        if arm_mode in ("b", "c"):
            _install_replay_clock(main)
```

- [ ] **Step 5: Make the replayed layers carry `initial_sl` and the simulated clock**

The live path captures the stop the order was placed with as `initial_sl`
(`mt5_executor`), and the R-relative profit floor reads it to compute R. The
replay built its layer records without it, so the R lock was silently skipped
and Arm C would not have exercised the feature. In `_build_signal_record`:

```python
            # The live path captures the stop the order was placed with as
            # `initial_sl` (mt5_executor); the R-relative profit floor reads
            # it to compute R. Without it the R lock is silently skipped and
            # Arm C would not exercise the feature at all.
            "initial_sl": sl,
```

In `_run_single_trade_worker`, start the simulated clock at the entry bar and
advance it with every bar:

```python
    # The simulated clock starts at the entry bar and follows the M1 bars,
    # so the trade-age guard measures the age of the RECORDED trade.
    _REPLAY_CLOCK.set_epoch(int(bars[idx0]["time_utc"]))
```

and, inside the bar loop:

```python
            _REPLAY_CLOCK.set_epoch(int(bar["time_utc"]))
```

- [ ] **Step 6: Extend the gate evaluation**

In `scripts/ab_replay_proof.py`, `_evaluate_gates` accepts `arm_c` and computes
the C-vs-B gate BEFORE the A/B branches, so the gate is present in both the
"both" and the single-arm modes:

```python
def _evaluate_gates(
    arm_a: Optional[Dict[str, Any]],
    arm_b: Optional[Dict[str, Any]],
    arm_c: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Arm C is compared against ARM B (not against Arm A): the R lock and the
    # trade-age guard are an increment on Arm B, and the gate is absent when
    # Arm C was not run.
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

Add `"g5": g5` to both returned gates dicts and let a failing `g5` fail
`all_gates_passed`:

```python
            "g5": g5,
            "all_gates_passed": bool(g1 and g2 and g3 and g4 and g5 is not False),
```

> The arm summary key is **`tail_loss_gbp_13078_9953`** (see `_summarize_arm` in
> `scripts/ab_replay_proof.py:1092`). The other keys used above are `net_gbp`
> (:1089), `error_count` (:1100), `trade_count` (:1088),
> `choke_trio_gbp_13153_9934_13167` (:1093) and `unflagged_rollover_exits`
> (:1099).
>
> `g5` compares ARM C against ARM B (never against Arm A) and is `None` when
> Arm C was not run.

- [ ] **Step 7: Extend `main()` and the markdown renderer**

In `scripts/ab_replay_proof.py` `main()`:

```python
        arm_a_res: Optional[Dict[str, Any]] = None
        arm_b_res: Optional[Dict[str, Any]] = None
        arm_c_res: Optional[Dict[str, Any]] = None

        if args.arm in ("a", "both"):
            arm_a_res = _run_arm_a_isolated(bars_file, io_dir / "arm_a.json")
        if args.arm in ("b", "both"):
            arm_b_res = _run_arm_subprocess("b", REPO_ROOT, bars_file, io_dir / "arm_b.json")
        if args.arm in ("c", "both"):
            arm_c_res = _run_arm_subprocess("c", REPO_ROOT, bars_file, io_dir / "arm_c.json")
```

and pass the result into `_evaluate_gates(arm_a_res, arm_b_res, arm_c_res)` and
add `"arm_c": arm_c_res` to the report dict.

In `_render_markdown`, add an Arm C section after the existing Arm A/B table:

```python
    if arm_c:
        sc = arm_c["summary"]
        sb_c = arm_b["summary"] if arm_b else None
        net_b = f"`£{sb_c['net_gbp']}`" if sb_c else "`n/a`"
        tail_b = f"`£{sb_c['tail_loss_gbp_13078_9953']}`" if sb_c else "`n/a`"
        lines += [
            "",
            "### Arm C (v3.8.0 Trade Lifecycle Guard)",
            "",
            f"- **Net PnL:** `£{sc['net_gbp']}` (Arm B: {net_b})",
            f"- **Tail Loss (13078 + 9953):** `£{sc['tail_loss_gbp_13078_9953']}` "
            f"(Arm B: {tail_b})",
            f"- **Trade Count:** `{sc['trade_count']}` | Errors: `{sc['error_count']}`",
        ]
```

- [ ] **Step 8: Run the replay proof**

```powershell
cd C:\Users\kambo\Code\telegram-mt5-copier
python scripts/ab_replay_proof.py --arm both
```

The bars are fetched read-only from the connected MT5 terminal. To reuse a
previously saved bar set, pass `--bars-json <path>` (the flag is `--bars-json`,
not `--bars`) and `--save-bars <path>` to persist a fetched set.

**If gate `g5` fails** (Arm C net P&L below Arm B, or a worse tail loss), do
**not** enable the feature and **do not** touch the gate logic, the summary
numbers or the comparison. Diagnose the cause, fix only what is genuinely a
bug, re-run, and record the real numbers in the commit message.

- [ ] **Step 9: Commit**

```powershell
git add scripts/ab_replay_proof.py
git commit -m "feat(replay): add Arm C (R lock + trade-age guard) to the A/B replay proof

Arm C replays the same 12 recorded trades with R_LOCK_ENABLED and
LIFECYCLE_ENABLED on, and gate g5 requires Arm C >= Arm B on net P&L and
<= Arm B on tail loss. This is the pre-live acceptance evidence.

g5 FAILED on the recorded tape: Arm C net GBP -16.85 vs Arm B +0.14 (tail
loss identical at -61.99, 0 errors). The whole delta is the 2 h de-risk
scratching the two trades the harness holds open past 2 h (9937 and 9943);
on the recorded live tape every trade closed within 47.1 min, so the guard
could never have fired live. The R lock never wins the stop picker on these
trades. The feature stays disabled.
```

### Task 10 outcome (2026-11-21)

`python scripts/ab_replay_proof.py --arm both` (bars fetched from the connected
MT5 terminal, 3477 M1 bars, `2026-11-16T23:00Z` -> `2026-11-19T11:00Z`):

| Arm | Net P&L | Trades | Tail loss (`13078+9953`) | Errors |
|---|---:|---:|---:|---:|
| A (`4544f7f`) | `£+11.89` | 12 | `£-61.99` | 0 |
| B (`v3.6.0` calibrated) | `£+0.14` | 12 | `£-61.99` | 0 |
| C (`R lock` + `age guard`) | `£-16.85` | 12 | `£-61.99` | 0 |

| Gate | Result |
|---|---|
| `gate_1_net_profit` | `False` (pre-existing, Arm B vs Arm A) |
| `gate_2_tail_protection` | `False` (pre-existing, Arm B vs Arm A) |
| `gate_3_mfe_capture_and_13153_stage2` | `False` (pre-existing, Arm B vs Arm A) |
| `gate_4_zero_regression` | `True` |
| **`g5` (Arm C vs Arm B)** | **`False`** |
| `all_gates_passed` | `False` |

**Why `g5` fails.** The 2 h de-risk fired on exactly two of the twelve trades —
`9937` and `9943`, the only two the harness keeps open past two hours — and
scratched both at breakeven: `£+4.44 -> £+0.76` and `£+14.07 -> £+0.76`,
i.e. `-£16.99` against Arm B. The tail loss is unchanged because both tail
trades died inside an hour (`13078` after 47.1 min, `9953` after 19.9 min), so
no age threshold could have reached them.

**Why that is a harness artefact, not a verdict on the live feature.** On the
recorded live tape every one of the twelve trades closed within **47.1 minutes**,
so a 2 h de-risk could never have fired live; and both affected trades were in
fact closed at breakeven in the live market (`9937` `BROKER_SL BE`, `9943`
`BROKER_SL BE`), while the replay's Arm B lets them run to `EOD_MTM` / `TP1`
because the harness replays a generic FSM instead of the recorded message
flow. Arm C reproduces the live scratch; Arm B does not.

**R lock.** With the `initial_sl` stamp of Step 5 in place the R lock is
exercised, but it never wins the stop picker on these twelve trades: the
existing `BE_LOCK`/`ATR_TRAIL` candidates are always tighter than the default
`0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75` floor (a measured run with an engaging
`1.0:0.25,1.5:0.50,2.0:0.75,2.5:1.00` ladder changed no trade either).

**Sensitivity (measured, NOT shipped).** `LIFECYCLE_DERISK_SECONDS=10800`
(3 h) makes `g5` pass (Arm C net `£+9.77`), and `21600` (6 h) makes the guard
never fire (Arm C identical to Arm B). Both are fitted to the two aged trades
of this twelve-trade sample and are therefore not shipped: the honest verdict
on this tape is that the gate cannot be won with the shipped parameters, and
the feature must stay disabled.

---

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
