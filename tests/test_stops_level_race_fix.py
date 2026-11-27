"""Regression tests: the broker stops-level race behind every retcode-10016 rejection.

Live evidence (account 36393319 / PUPrime-Live 6, 2026-11-27):
  * 45 "Invalid stops" errors in two live days; 14,488 in the demo log on 2026-11-24
    alone (7,118 auto-BE armings produced only 4 successful BE moves);
  * the failing line was always the ATR engine's binding-SL apply path
    ("[T3 BINDING-SL] ... WINNER=INTERIM_FLOOR -> broker_sl=..."), NOT auto-BE;
  * the candidate stop was clamped to EXACTLY the broker minimum distance
    (trade_stops_level * point + $0.05), so any price tick between our read and the
    broker's evaluation of the order pushed it back inside the minimum -> 10016;
  * a rejected stop protects nothing: 90.7% of the peak profit on winning live trades
    was surrendered (MFE $471.58 -> realised GBP 33.78).

The fix has three parts, each covered below:
  1. the clamp distance carries an explicit tick-race tolerance
     (STOP_LEVEL_TICK_RACE_TOLERANCE_DOLLARS) on top of the broker minimum;
  2. retcode 10016 is an expected rejection, logged at DEBUG and not at ERROR;
  3. a layer backs off after a 10016 rejection instead of re-sending the same doomed
     stop every engine cycle.

Hermetic: dry-run executors only, no broker connection, no live trades.
"""
from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
import dynamic_risk as dr
import mt5_executor
from dynamic_risk import DynamicRiskPolicy, compute_dynamic_atr_trail_stop

LIVE_STOPS_LEVEL = 20          # broker trade_stops_level for XAUUSD.s (points)
LIVE_POINT = 0.01
STOPS_DISTANCE_MULT = 2.0      # shipped default clamp distance multiplier


def _sym(stops_level=LIVE_STOPS_LEVEL, point=LIVE_POINT):
    return SimpleNamespace(digits=2, point=point, trade_stops_level=stops_level)


def _ex():
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    ex._dry_run_atr = 6.0
    return ex


def _engine_sig():
    """Same shape as the two-step abort sig in test_trailing_profile.py."""
    return {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3875.0,
        "layers": {"L1": {"lot": 0.01, "entry_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1",
                          "ticket": 701, "order_type": "MARKET", "status": "OPEN", "role": "CORE_RUNNER"}},
        "market_tickets": [701],
        "runner_tickets": [701],
        "runner_layers": ["L1"],
        "runner_ticket": 701,
        "runner_layer": "L1",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0},
    }


# ---------------------------------------------------------------------------
# 1. The clamp distance absorbs the price race
# ---------------------------------------------------------------------------

def test_clamp_distance_is_twice_the_broker_minimum():
    ex = _ex()
    sym = _sym()
    base = ex._compute_stops_buffer(sym)
    clamp = ex._compute_sl_clamp_distance(sym)
    assert base == pytest.approx(LIVE_STOPS_LEVEL * LIVE_POINT + 0.05)
    # Twice the RAW broker minimum: $0.40 on XAUUSD.s (20 points x $0.01).
    assert clamp == pytest.approx(2.0 * LIVE_STOPS_LEVEL * LIVE_POINT)
    assert clamp > base, "the clamp must clear the broker minimum, not sit on it"


def test_clamp_distance_is_zero_when_the_broker_has_no_stops_requirement():
    ex = _ex()
    assert ex._compute_sl_clamp_distance(_sym(stops_level=0)) == 0.0


def test_shipped_stops_distance_multiplier_default():
    assert float(config.STOP_LEVEL_STOPS_DISTANCE_MULT) == pytest.approx(2.0)
    assert float(config.SL_REJECT_BACKOFF_SECONDS) > 0.0


# ---------------------------------------------------------------------------
# 2. The picker keeps the clamped stop clear of the price
# ---------------------------------------------------------------------------

def _trail(live, entry=3890.0, peak=3895.0, atr=6.0, buf=0.25, clamp=None, current_sl=0.0):
    kwargs = {}
    if clamp is not None:
        kwargs["clamp_buffer"] = clamp
    return compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=entry,
        live_price=live,
        prev_peak=peak,
        current_sl=current_sl,
        atr=atr,
        policy=DynamicRiskPolicy(),
        digits=2,
        stops_buffer=buf,
        **kwargs,
    )


def test_clamped_candidate_sits_exactly_on_the_broker_minimum_without_the_tolerance():
    """The old (buggy) behaviour: the stop is pinned to the broker minimum."""
    res = _trail(live=3890.30, buf=0.25)
    assert res["candidate_sl"] == pytest.approx(3890.30 - 0.25)


def test_clamp_buffer_keeps_the_candidate_clear_of_the_price():
    """With the tolerance the stop survives a price tick inside the broker minimum."""
    res = _trail(live=3890.30, buf=0.25, clamp=0.40)
    assert res["candidate_sl"] == pytest.approx(3890.30 - 0.40)
    assert 3890.30 - res["candidate_sl"] >= 0.40 - 1e-9


def test_clamp_buffer_never_moves_the_stop_toward_the_price():
    """A larger clamp distance can only push the stop FURTHER from the price."""
    tight = _trail(live=3890.30, buf=0.25)["candidate_sl"]
    loose = _trail(live=3890.30, buf=0.25, clamp=0.40)["candidate_sl"]
    assert loose < tight, "a BUY stop further from the price is a lower stop"


def test_clamp_buffer_never_loosens_an_existing_protective_stop():
    """Forward-only invariant: a tighter broker SL is never widened by the clamp."""
    current = 3890.25
    res = _trail(live=3890.30, buf=0.25, clamp=0.40, current_sl=current)
    assert res["candidate_sl"] < current
    assert res["should_update"] is False, "a clamped candidate must not widen the broker SL"


def test_clamp_buffer_defaults_to_the_plain_stops_buffer():
    """Backwards compatibility: omitting clamp_buffer keeps the old clamp exactly."""
    with_extra = _trail(live=3890.30, buf=0.25, clamp=0.25)["candidate_sl"]
    without = _trail(live=3890.30, buf=0.25)["candidate_sl"]
    assert with_extra == pytest.approx(without)


# ---------------------------------------------------------------------------
# 3. Retcode 10016 is an expected rejection, not an error
# ---------------------------------------------------------------------------

def _rejecting_ex(retcode, comment):
    ex = mt5_executor.MT5Executor(dry_run=False)
    ex._connected = True
    fake_pos = SimpleNamespace(ticket=70001, symbol="XAUUSD.s", sl=3843.56, tp=3862.75)

    def _send(req):
        return SimpleNamespace(retcode=retcode, comment=comment)

    patches = (
        patch.object(ex, "_ensure_connected", return_value=True),
        patch.object(ex, "get_symbol_info", return_value=_sym(stops_level=50)),
        patch.object(mt5_executor.mt5, "positions_get", return_value=[fake_pos]),
        patch.object(mt5_executor.mt5, "order_send", side_effect=_send),
    )
    return ex, patches


def test_invalid_stops_is_logged_at_debug_and_not_as_an_error(caplog):
    ex, patches = _rejecting_ex(10016, "Invalid stops")
    for p in patches:
        p.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="copier.mt5"):
            ok = ex.modify_position_sltp(70001, sl=3857.90, tp=None)
    finally:
        for p in patches:
            p.stop()
    assert ok is False
    assert ex.last_modify_result["retcode"] == 10016
    assert ex.last_modify_result["sl_ok"] is False
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records), (
        "retcode 10016 is an expected broker rejection and must not log at ERROR"
    )
    assert any("stops level" in rec.getMessage() for rec in caplog.records)


def test_other_retcodes_still_log_at_error(caplog):
    ex, patches = _rejecting_ex(10019, "Not enough money")
    for p in patches:
        p.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="copier.mt5"):
            ok = ex.modify_position_sltp(70001, sl=3857.90, tp=None)
    finally:
        for p in patches:
            p.stop()
    assert ok is False
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


# ---------------------------------------------------------------------------
# 4. The engine backs off after a stops-level rejection
# ---------------------------------------------------------------------------

def test_engine_backs_off_after_a_stops_level_rejection():
    ex = _ex()
    calls = []

    def _reject(ticket, sl=None, tp=None, require_both=False):
        # The engine also issues a TP-only modify per cycle; only the SL modify
        # (sl is not None) is the one the back-off has to govern.
        if sl is not None:
            calls.append((ticket, sl))
        ex.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": 10016}
        return False

    sig = _engine_sig()
    tick = SimpleNamespace(bid=3898.0, ask=3898.2, time=1700000000.0)

    with patch.object(ex, "modify_position_sltp", side_effect=_reject):
        ex.evaluate_dynamic_atr_engine(sig, tick=tick)
        assert len(calls) == 1, "the first attempt must actually reach the broker"
        assert sig["layers"]["L1"].get("_sl_reject_until", 0.0) > 0.0

        # Same cycle cadence, inside the back-off window: no second attempt.
        ex.evaluate_dynamic_atr_engine(sig, tick=tick)
        assert len(calls) == 1, "the back-off must suppress the doomed retry"

        # Once the window expires the engine retries (the price has had time to move).
        sig["layers"]["L1"]["_sl_reject_until"] = 0.0
        ex.evaluate_dynamic_atr_engine(sig, tick=tick)
        assert len(calls) == 2, "the engine must retry after the back-off window"


def test_engine_backoff_is_disabled_when_the_knob_is_zero(monkeypatch):
    monkeypatch.setattr(config, "SL_REJECT_BACKOFF_SECONDS", 0.0, raising=False)
    ex = _ex()
    calls = []

    def _reject(ticket, sl=None, tp=None, require_both=False):
        if sl is not None:
            calls.append((ticket, sl))
        ex.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": 10016}
        return False

    sig = _engine_sig()
    tick = SimpleNamespace(bid=3898.0, ask=3898.2, time=1700000000.0)

    with patch.object(ex, "modify_position_sltp", side_effect=_reject):
        ex.evaluate_dynamic_atr_engine(sig, tick=tick)
        ex.evaluate_dynamic_atr_engine(sig, tick=tick)
    assert len(calls) == 2, "a zero back-off must keep the old every-cycle retry"
