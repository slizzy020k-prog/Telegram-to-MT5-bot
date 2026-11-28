"""P5: a TP1 that sits inside a few spreads of the entry is rejected pre-flight.

The entry pays the spread and so does the exit, so a target closer to the entry
than MIN_TP1_SPREAD_MULTIPLE live spreads cannot pay for the round trip. The
guard lives in the LIVE pre-flight path (the dry-run path returns before the
spread is known), so these tests drive a live-mode executor with a stubbed MT5.

The reference price is the one the copier can REALLY fill at, so these cases
follow the executor's own routing:
  * a layer that executes at MARKET now fills at the live quote
    (ask for a BUY, bid for a SELL);
  * a pending LIMIT/STOP fills at its own order price, and a pending LIMIT is
    placed with the front-run shift applied (+$0.50 on a BUY, -$0.50 on a SELL).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _live_executor(monkeypatch, bid: float, ask: float, trade_mode: int = 4):
    """A live-mode executor whose MT5 surface is stubbed with a fixed quote."""
    from mt5_executor import MT5Executor

    ex = MT5Executor(dry_run=False)
    monkeypatch.setattr(ex, "_ensure_connected", lambda: True)
    monkeypatch.setattr(ex, "is_trade_allowed", lambda: True)
    monkeypatch.setattr(ex, "get_daily_drawdown_status", lambda: {"pause_new_signals": False})
    monkeypatch.setattr(ex, "resolve_symbol", lambda symbol: symbol)
    monkeypatch.setattr(ex, "ensure_symbol", lambda symbol: True)
    monkeypatch.setattr(
        ex,
        "get_symbol_info",
        lambda symbol: SimpleNamespace(trade_mode=trade_mode, point=0.01, digits=2),
    )
    monkeypatch.setattr(
        ex,
        "get_tick",
        lambda symbol: SimpleNamespace(bid=bid, ask=ask, time=1700000000.0),
    )
    return ex


def _zone(l1: float, top: float, bottom: float):
    return SimpleNamespace(tiers={"L1": l1}, top=top, bottom=bottom)


# --- pure market entries ---------------------------------------------------


def test_market_tp1_inside_three_spreads_is_rejected(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)   # spread 1.00
    ok, msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3897.50, sl=3875.0)
    assert ok is False
    assert "too close to the entry" in msg


def test_market_tp1_exactly_three_spreads_away_is_accepted(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)   # spread 1.00
    ok, msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3898.00, sl=3875.0)
    assert ok is True, msg


def test_sell_mirror_anchors_on_the_bid(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight("XAUUSD.s", "SELL", raw_tp1=3895.00, sl=3910.0)
    assert ok is False
    assert "too close to the entry" in msg


# --- zone entries: the reference follows the executor's routing ------------


def test_sell_zone_market_leg_is_measured_at_the_bid(monkeypatch):
    """A SELL whose L1 executes at MARKET fills at the bid, below its tier.

    The L1 tier would overstate the TP1 edge, so the guard must measure the edge
    from the price the market leg really gets.
    """
    ex = _live_executor(monkeypatch, bid=3740.50, ask=3741.50)   # spread 1.00
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "SELL",
        raw_tp1=3738.00,          # $2.50 from the market fill at 3740.50
        sl=3765.0,
        zone=_zone(l1=3742.0, top=3752.0, bottom=3742.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_buy_zone_market_leg_is_measured_at_the_ask(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3897.00,          # $2.00 from the market fill at 3895.00
        sl=3875.0,
        zone=_zone(l1=3900.0, top=3900.0, bottom=3892.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_buy_zone_pending_limit_is_measured_at_its_own_price(monkeypatch):
    """A BUY zone the market has NOT reached becomes a pending limit at its tier.

    The live quote must NOT be used as the reference here: the order fills at the
    tier price, so the real edge is much larger than the quote would suggest.
    """
    ex = _live_executor(monkeypatch, bid=3903.00, ask=3905.00)   # spread 2.00
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3910.00,          # $10.00 from the tier at 3900.00
        sl=3875.0,
        zone=_zone(l1=3900.0, top=3900.0, bottom=3892.0),
    )
    assert ok is True, msg


def test_sell_zone_pending_limit_is_measured_at_its_own_price(monkeypatch):
    """A SELL zone below the market stays a pending limit at (below) its tier."""
    ex = _live_executor(monkeypatch, bid=3739.78, ask=3740.30)   # spread 0.52
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "SELL",
        raw_tp1=3739.00,          # $3.00 from the tier at 3742.00
        sl=3765.0,
        zone=_zone(l1=3742.0, top=3752.0, bottom=3742.0),
    )
    assert ok is True, msg


def test_pending_limit_uses_the_front_run_shifted_price(monkeypatch):
    """The pending LIMIT is placed with the front-run shift applied."""
    import config

    monkeypatch.setattr(config, "LIMIT_ENTRY_FRONT_RUN_OFFSET", 0.50)
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    # A SELL zone whose market leg does not fire: the order sits $0.50 BELOW the
    # 3900.00 tier, so the edge is measured from 3899.50 and not from 3900.00.
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "SELL",
        raw_tp1=3897.00,          # $2.50 from the shifted price at 3899.50
        sl=3920.0,
        zone=_zone(l1=3900.0, top=3900.0, bottom=3900.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


# --- single-price entries --------------------------------------------------


def test_buy_zone_front_run_shift_is_reflected_in_the_reference(monkeypatch):
    """The reference must be the price the broker actually holds.

    The pending LIMIT is placed with the front-run shift applied, and the shift is
    only kept when the TP room allows it - a decision that depends on final_tp.
    The guard must use the same inputs, or it measures the edge from a price the
    order never gets (here: 3900.00 instead of 3900.50).
    """
    import config

    monkeypatch.setattr(config, "LIMIT_ENTRY_FRONT_RUN_OFFSET", 0.50)
    ex = _live_executor(monkeypatch, bid=3904.65, ask=3905.00)   # spread 0.35
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3901.40,          # $0.90 from the placed order price at 3900.50
        sl=3875.0,
        final_tp=3910.00,
        zone=_zone(l1=3900.0, top=3900.0, bottom=3892.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_zone_entry_ignores_a_non_auto_intent(monkeypatch):
    """A zone entry always routes with AUTO in the placement path.

    The caller may pass MARKET or STOP, but execute_4layer_zone still places the
    layers by the zone routing. The guard must use the same routing, otherwise it
    measures the edge from a price the order never gets.
    """
    for intent in ("MARKET", "STOP"):
        ex = _live_executor(monkeypatch, bid=3899.00, ask=3900.00)   # spread 1.00
        ok, msg = ex.check_preflight(
            "XAUUSD.s",
            "BUY",
            raw_tp1=3894.00,          # $1.50 from the pending limit at 3895.50
            sl=3875.0,
            order_intent=intent,
            is_stop_order=(intent == "STOP"),
            zone=_zone(l1=3895.0, top=3895.0, bottom=3887.0),
        )
        assert ok is False, f"intent {intent} must not bypass the spread guard"
        assert "too close to the entry" in msg


def test_single_price_is_the_entry_reference(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3896.00,          # $1.00 from the market fill at 3895.00
        sl=3875.0,
        single_price=3900.0,
    )
    assert ok is False
    assert "too close to the entry" in msg


# --- the knob ---------------------------------------------------------------


def test_guard_is_disabled_when_the_multiple_is_zero(monkeypatch):
    import config

    monkeypatch.setattr(config, "MIN_TP1_SPREAD_MULTIPLE", 0.0)
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    # $2.10 from the entry: inside 3 x the $1.00 spread, so the guard would reject
    # it, but far enough outside the 20-pip TP1 proximity window to be accepted
    # once the guard is off.
    ok, msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3897.10, sl=3875.0)
    assert ok is True, msg


def test_executor_level_knob_override_takes_effect(monkeypatch):
    """The executor's own copy of the knob must be reachable (no dead import)."""
    import mt5_executor

    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3897.10, sl=3875.0)
    assert ok is False, "the shipped multiple of 3.0 rejects this target"
    assert "too close to the entry" in msg

    monkeypatch.setattr(mt5_executor, "MIN_TP1_SPREAD_MULTIPLE", 0.0)
    ex2 = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok2, msg2 = ex2.check_preflight("XAUUSD.s", "BUY", raw_tp1=3897.10, sl=3875.0)
    assert ok2 is True, msg2


def test_shipped_default_is_three_spreads():
    import config

    assert config.MIN_TP1_SPREAD_MULTIPLE == pytest.approx(3.0)
