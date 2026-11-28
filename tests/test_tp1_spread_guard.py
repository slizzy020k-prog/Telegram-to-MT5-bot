"""P5: a TP1 that sits inside a few spreads of the entry is rejected pre-flight.

The entry pays the spread and so does the exit, so a target closer to the entry
than MIN_TP1_SPREAD_MULTIPLE live spreads cannot pay for the round trip. The
guard lives in the LIVE pre-flight path (the dry-run path returns before the
spread is known), so these tests drive a live-mode executor with a stubbed MT5.
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


def test_market_tp1_inside_three_spreads_is_rejected(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)   # spread 1.00
    ok, msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3897.50, sl=3875.0)
    assert ok is False
    assert "too close to the entry" in msg


def test_market_tp1_exactly_three_spreads_away_is_accepted(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)   # spread 1.00
    ok, msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3898.00, sl=3875.0)
    assert ok is True, msg


def test_zone_uses_the_l1_tier_as_the_entry_reference(monkeypatch):
    """A BUY zone below the market fills at its L1 tier, so L1 is the reference."""
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3901.00,          # $1.00 from the L1 tier at 3900.00
        sl=3875.0,
        zone=_zone(l1=3900.0, top=3900.0, bottom=3892.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_buy_zone_above_the_market_is_anchored_on_the_market_leg(monkeypatch):
    """A BUY zone whose market already sits above the tier fills L1 at the quote.

    The L1 tier would overstate the TP1 edge, so the guard must measure the edge
    from the price the market leg really gets.
    """
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3896.00,          # $1.00 from the market fill at 3895.00
        sl=3875.0,
        zone=_zone(l1=3890.0, top=3890.0, bottom=3882.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_sell_zone_is_anchored_on_its_l1_tier(monkeypatch):
    """A SELL zone fills at its tier or above, so the tier is the worst case."""
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "SELL",
        raw_tp1=3740.00,
        sl=3910.0,
        zone=_zone(l1=3742.0, top=3752.0, bottom=3742.0),
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_single_price_is_the_entry_reference(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight(
        "XAUUSD.s",
        "BUY",
        raw_tp1=3900.50,          # $0.50 from the single price at 3900.00
        sl=3875.0,
        single_price=3900.0,
    )
    assert ok is False
    assert "too close to the entry" in msg


def test_sell_mirror_anchors_on_the_bid(monkeypatch):
    ex = _live_executor(monkeypatch, bid=3894.00, ask=3895.00)
    ok, msg = ex.check_preflight("XAUUSD.s", "SELL", raw_tp1=3895.00, sl=3910.0)
    assert ok is False
    assert "too close to the entry" in msg


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
