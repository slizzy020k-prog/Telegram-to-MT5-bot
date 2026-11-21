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


