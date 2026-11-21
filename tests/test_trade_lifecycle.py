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
