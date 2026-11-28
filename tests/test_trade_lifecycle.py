"""Unit tests for the R-lock domain module (`risk_lifecycle.py`), fill-time telemetry and ledger schema 4."""

from datetime import datetime, timedelta, timezone

import pytest

import risk_lifecycle as rl


def _policy(**overrides):
    base = dict(
        r_lock_enabled=True,
        r_lock_tiers=rl.DEFAULT_R_LOCK_TIERS,
        r_lock_min_r_dollars=0.50,
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
        trail_max_dollars=8.00,
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
    # MFE 10.00 at ATR 2.0 -> EXTENDED zone (the ATR-relative stage opens at
    # min($10, 2.5 x ATR) = $5.00) -> mult 1.00 and the trail IS armed
    # (the activation is 1.00 x ATR = 2.00), so the ATR trail (3905.00 - 2.00)
    # is the tightest candidate.
    assert res["candidate_sl"] == pytest.approx(3903.00)
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
        # ATR-relative stages at ATR 2.0: the HOME_RUN threshold is
        # min($18, 4.0 x ATR) = $8.00, so a $10.00 MFE is already HOME_RUN ->
        # mult 0.80, HOME_RUN floor $2.00 -> 3905.00 - 2.00 = 3903.00.
        ("BUY", 2.0): (3903.0, "ATR_TRAIL", 0.8, "HOME_RUN"),
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
        atr=20.0,              # wide ATR -> the ATR trail is not armed (MFE < activation), so the R floor wins
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
        atr=20.0,              # wide ATR -> the ATR trail is not armed, so the R floor wins the picker
        policy=pol,
        digits=2,
        r_lock_floor=3902.8,   # the R lock wins the picker, but it is looser than the stop in place
    )
    assert res["stop_source"] == "R_LOCK"
    assert res["candidate_sl"] == pytest.approx(3902.8)
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


# --- fix round 1: BE fill-race promotion, broker stop capture, edit path ---


def test_apply_signal_breakeven_stamps_the_fill_time_when_it_promotes_a_pending_layer(monkeypatch):
    """A pending limit that filled before the 1s reconciler must still be stamped.

    `apply_signal_breakeven` promotes the layer to FILLED and drops the ticket
    from `pending_tickets`, so the reconciler's stamping loop never sees it
    again. Without a stamp here the promoted position has NO fill time at all
    and the ledger cannot measure its true age.
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
    ledger cannot measure the trade's true age.
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
        # the milestone floor (which is locked to entry + the shipped $1.20 BE
        # offset once TP1 is cleared) cannot win the picker. These tests are
        # about the R floor.
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
    ex._dry_run_atr = 40.0     # a very wide ATR: neither the ATR trail (0.70 x ATR = 28.00) nor the
                              # BE lock (0.50 x ATR = 20.00) arms at MFE $11.00, so the R floor wins

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
    ex._dry_run_atr = 40.0     # wide ATR -> neither the trail nor the BE lock arms

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
    ex._dry_run_atr = 40.0

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
    ex._dry_run_atr = 40.0

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


def test_trail_zone_latch_is_persisted_even_when_no_stop_is_applied(monkeypatch):
    """The latched zone must survive a cycle that sends no SL modification.

    The zone only ever advances (STANDARD -> EXTENDED -> HOME_RUN). If the zone
    were recorded only when an SL modify succeeded, a cycle that the step filter
    or the broker rejects would drop the advance, and an ATR expansion on the next
    cycle could regress the zone.
    """
    import config
    from mt5_executor import MT5Executor
    from types import SimpleNamespace

    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 2.0

    sig = _atr_signal(3895.0, 3880.0)
    # ATR 2.00 -> the ATR-relative HOME_RUN stage opens at min($18, 4.0 x ATR) = $8.00.
    # MFE 8.00 -> HOME_RUN, trail_dist = clamp(0.80 x 2.00, [2.00, 4.00]) = 2.00,
    # so the candidate stop is 3903.00 - 2.00 = 3901.00.
    sig["layers"]["L1"]["broker_sl"] = 3901.0    # already at the candidate stop
    res = ex.evaluate_dynamic_atr_engine(
        sig, tick=SimpleNamespace(bid=3903.0, ask=3903.2, time=1700000000.0)
    )
    assert not res.get("updated_tickets"), "no SL update may be sent"
    # The zone advance must be persisted even though no stop was applied.
    assert sig["layers"]["L1"].get("dynamic_atr_trail_zone") == "HOME_RUN"


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
    ex._dry_run_atr = 40.0

    sig = _atr_signal(3895.0, 3880.0)
    ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3906.2, time=1700000000.0))
    lv = sig["layers"]["L1"]
    assert lv.get("dynamic_atr_trail_sl") == pytest.approx(3895.0)
    # The tier telemetry describes the engine, not the author of the stop.
    assert lv.get("dynamic_atr_trail_mult") is not None
    assert lv.get("dynamic_atr_trail_zone") is not None


def _install_signal(monkeypatch, tmp_path, sig):
    """Install `sig` in a throw-away state manager and return the main module."""
    import main
    from state_manager import StateManager
    from trade_ledger import TradeLedger

    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "rlock_state.json"))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / "rlock_ledger.jsonl"))
    main.state_mgr.create_signal(
        message_id="9001",
        symbol="XAUUSD.s",
        direction="BUY",
    )
    main.state_mgr.update_signal("9001", sig)
    return main


@pytest.mark.parametrize(
    "tick_spec",
    [
        {"bid": 0.0, "ask": 0.0},
        {"bid": -1.0, "ask": -1.0},
        {"ask": 3895.2},
        {"bid": "not-a-number", "ask": "not-a-number"},
    ],
)
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
async def test_rlock_command_rejects_an_invalid_value_without_toggling(monkeypatch, tmp_path):
    import config
    import main

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/rlock maybe")
    await main.handle_saved_messages_command(ev)

    assert config.R_LOCK_ENABLED is False
    assert any("Usage" in r for r in ev.replies)


def test_rlock_is_a_known_saved_command():
    """An unknown command would be treated as a numeric prompt reply."""
    import main

    assert main._is_known_saved_command("/rlock")
    assert main._is_known_saved_command("rlock")
    assert main._is_known_saved_command("/rlock on")


@pytest.mark.asyncio
async def test_rlock_toggle_is_visible_to_the_executor_policy(monkeypatch, tmp_path):
    """The toggle must reach the executor, not only the config module."""
    import config
    import main
    import mt5_executor

    monkeypatch.setattr(config, "R_LOCK_ENABLED", False)
    _install_signal(monkeypatch, tmp_path, {})

    ev = _MockEvent("/rlock on")
    await main.handle_saved_messages_command(ev)

    assert mt5_executor._cfg("R_LOCK_ENABLED", False) is True
    ex = mt5_executor.MT5Executor(dry_run=True)
    assert ex._get_lifecycle_policy().r_lock_enabled is True


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
    # 11.12 $/oz * 0.01 lots (FILLED, not the 0.05 planned) * 100 oz/lot = 11.12
    assert rec["mfe_cash"] == pytest.approx(11.12)
    assert rec["giveback_cash"] == pytest.approx(10.56)
    assert rec["initial_r_per_oz"] == pytest.approx(20.0)
    # R at exit = MFE cash / risk cash = 11.12 / (20.0 * 0.01 * 100) = 0.556.
    # The ratio is scale-invariant: both the excursion and the risk now use the
    # FILLED lots, so the R figure is unchanged while the cash figures are real.
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
    assert rec["mfe_cash"] == pytest.approx(11.12)
    assert rec["r_at_exit"] == pytest.approx(0.556)
    # The giveback is converted into the ACCOUNT currency BEFORE the subtraction.
    assert rec["giveback_cash"] == pytest.approx(round(11.12 * 0.7645 - 0.56, 2))
    assert rec["giveback_cash"] != pytest.approx(10.56)
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
    assert rec["mfe_cash"] == pytest.approx(11.12)
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
    assert flat["be_sl"] == pytest.approx(3900.50)   # entry + the test policy's $0.50 offset
    assert flat["stop_source"] == "BE_LOCK"

    for bad in (None, 0.0, -5.0):
        res = _gated_stop(live_price=3905.0, initial_r=bad)
        assert res["be_armed"] is flat["be_armed"]
        assert res["be_sl"] == flat["be_sl"]
        assert res["stop_source"] == flat["stop_source"]
        assert res["candidate_sl"] == flat["candidate_sl"]


def test_gated_be_lock_waits_for_half_the_initial_risk():
    """A wide-stop trade (R = $22) must not be BE-locked at $3.50 of MFE.

    The explicit test policy (_atr_policy) uses the legacy BE numbers: a $3.50
    flat minimum, a 0.75x ATR term and a $0.50 lock offset.
    """
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
        (44.0, 4.0, 10.99, False),    # 0.25 * R = $11.00 dominates
        (44.0, 4.0, 11.00, True),
        (3.0, 20.0, 14.99, False),    # 0.75 * ATR = $15.00 dominates (test policy)
        (3.0, 20.0, 15.00, True),
        (3.0, 2.0, 3.49, False),      # the $3.50 flat BE minimum dominates (test policy)
        (3.0, 2.0, 3.50, True),
    ],
)
def test_gated_be_threshold_is_half_r_the_atr_multiple_or_the_flat_floor(
    initial_r, atr, mfe, armed
):
    """The gated threshold is max(0.25 * R, 0.75 * ATR, $3.50, offset + cost floor).

    These are the explicit test policy's numbers (_atr_policy): a 0.75x ATR term,
    a $3.50 flat minimum and a $0.50 lock offset (+ $0.60 cost floor = $1.10).

    The old $2.50 trail minimum no longer gates the lock: it was the term that
    stopped the breakeven lock from ever arming on a real move.
    """
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
    stop (entry + the test policy's $0.50 offset) is the tighter -- and therefore the winning --
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
    assert res["be_sl"] == pytest.approx(3899.50)     # entry - the test policy's $0.50 offset (SELL)
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
    # Pin the baseline STANDARD-zone trail regime (activation 1.00x ATR, max $8.00) so these
    # R-gated BE-lock tests stay hermetic against the ambient TRAILING_PROFILE (live t3 =
    # 0.70x / $5.50). Assertions are unchanged; only the assumed baseline precondition is pinned.
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00, raising=False)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 8.00, raising=False)
    monkeypatch.setattr(mt5_executor, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 8.00, raising=False)

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

    # The flat (non-R-gated) BE lock sits at entry + the $1.20 lock offset.
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3901.20)
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
    # the BE lock keeps the classic breakeven level (entry + the $1.20 offset);
    # the R ladder only changed WHEN it arms, so the R_LOCK candidate is the
    # tighter of the two
    assert one_r["layers"]["L1"]["be_sl"] == pytest.approx(3901.20)

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

    The classic BE lock of the explicit test policy sits at entry + $0.50 (BUY)
    / entry - $0.50 (SELL).
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
    """A wide-stop trade (R = $22) must not be BE-locked at the flat $3.50.

    The explicit test policy (_atr_policy) uses the legacy BE numbers: the flat
    minimum is $3.50 and the R-gated threshold is 0.25 * 22.00 = $5.50.
    """
    classic = _gated_stop(live_price=3903.50, atr=4.0)
    assert classic["be_armed"] is True              # the flat lock arms at $3.50
    assert classic["be_sl"] == pytest.approx(3900.50)

    gated = _gated_stop(live_price=3903.50, atr=4.0, initial_r=22.0)
    assert gated["be_armed"] is False                # 0.25 * 22.00 = $5.50 dominates
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
