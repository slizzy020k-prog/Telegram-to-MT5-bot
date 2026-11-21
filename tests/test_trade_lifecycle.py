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
