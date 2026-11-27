"""Regression tests: the unified stops-level choke point, the entry blackout,
the R-relative breakeven activation and the ledger reconciliation.

Live evidence (account 36393319 / PUPrime-Live 6):
  * 45 "Invalid stops" rejections in two live days, 14,488 in the demo log on
    2026-11-24 alone; every rejected stop protected nothing, and 90.7% of the peak
    profit on winning trades was surrendered (MFE $471.58 -> realised GBP 33.78);
  * 68% of the live loss came from the 02:00 broker-server hour, and the whole
    -GBP 104.99 day of 2026-11-18 sat inside that window.

Hermetic: fake positions / ticks / deals only. No broker connection, no live trades.
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
import dynamic_risk
import main
import mt5_executor
import trade_ledger
from dynamic_risk import DynamicRiskPolicy, compute_dynamic_atr_trail_stop

STOPS_LEVEL = 20      # trade_stops_level in points
POINT = 0.01          # $0.01 per point
CLAMP_DIST = 2.0 * STOPS_LEVEL * POINT   # $0.40 with the shipped multiplier


def _sym():
    return SimpleNamespace(digits=2, point=POINT, trade_stops_level=STOPS_LEVEL)


def _pos(ticket=70001, side=0, sl=3875.0, tp=3899.5, open_price=3890.0):
    return SimpleNamespace(ticket=ticket, symbol="XAUUSD.s", type=side, volume=0.01,
                            price_open=open_price, sl=sl, tp=tp, magic=777001)


def _tick(bid, ask):
    return SimpleNamespace(bid=bid, ask=ask, time=1700000000.0)


def _executor(tick):
    """A live executor whose tick and order_send are fully controlled."""
    ex = mt5_executor.MT5Executor(dry_run=False)
    ex._connected = True
    sent = []

    def _send(req):
        sent.append(dict(req))
        return SimpleNamespace(retcode=10009, comment="Request executed")

    ex._sent = sent
    return ex, sent


def _run(ex, sent, pos, sl, tp=None, tick=None):
    with patch.object(ex, "_ensure_connected", return_value=True), \
         patch.object(ex, "get_symbol_info", return_value=_sym()), \
         patch.object(mt5_executor.mt5, "positions_get", return_value=[pos]), \
         patch.object(ex, "get_tick", return_value=tick), \
         patch.object(mt5_executor.mt5, "order_send", side_effect=lambda r: (sent.append(dict(r)), SimpleNamespace(retcode=10009, comment="Request executed"))[1]):
        ok = ex.modify_position_sltp(pos.ticket, sl=sl, tp=tp)
    return ok


# ---------------------------------------------------------------------------
# 1. The choke point
# ---------------------------------------------------------------------------

def test_clamp_distance_is_twice_the_broker_minimum():
    ex = mt5_executor.MT5Executor(dry_run=True)
    assert ex._compute_sl_clamp_distance(_sym()) == pytest.approx(CLAMP_DIST)


def test_stop_too_close_to_the_market_is_pulled_back_and_sent():
    """The race case: the candidate is legal but marginal -> clamp to 2x the minimum."""
    ex, sent = _executor(None)
    pos = _pos(sl=3875.0)                       # BUY, entry 3890.0
    # Market at 3890.00 -> the legal limit is 3890.00 - 0.40 = 3890.60? No: a BUY's
    # stop must be BELOW the market, so the limit is 3890.00 - 0.40 = 3889.60.
    ok = _run(ex, sent, pos, sl=3889.80, tick=_tick(3890.00, 3890.20))
    assert ok is True
    assert sent[-1]["sl"] == pytest.approx(3889.60)


def test_stop_on_the_wrong_side_of_the_market_is_refused_not_relaxed():
    """A stop above the market cannot be placed: the existing protection is kept."""
    ex, sent = _executor(None)
    pos = _pos(sl=3875.0)
    ok = _run(ex, sent, pos, sl=3895.00, tick=_tick(3890.00, 3890.20))
    assert ok is False
    assert ex.last_modify_result["retcode"] == 10016
    assert ex.last_modify_result["sl_ok"] is False
    assert not sent, "no order may be sent for a stop on the wrong side of the market"


def test_clamp_never_loosens_an_existing_protective_stop():
    ex, sent = _executor(None)
    pos = _pos(sl=3889.70)                       # already tighter than the clamp limit
    ok = _run(ex, sent, pos, sl=3889.80, tick=_tick(3890.00, 3890.20))
    assert ok is False
    assert ex.last_modify_result["sl_ok"] is False
    assert not sent


def test_without_a_tick_the_requested_stop_is_sent_unchanged():
    ex, sent = _executor(None)
    pos = _pos(sl=3875.0)
    ok = _run(ex, sent, pos, sl=3880.00, tick=None)
    assert ok is True
    assert sent[-1]["sl"] == pytest.approx(3880.00)


def test_a_legal_stop_is_sent_unchanged():
    ex, sent = _executor(None)
    pos = _pos(sl=3875.0)
    ok = _run(ex, sent, pos, sl=3885.00, tick=_tick(3890.00, 3890.20))
    assert ok is True
    assert sent[-1]["sl"] == pytest.approx(3885.00)


def test_take_profit_too_close_to_the_market_is_pushed_clear_of_the_ask():
    """A BUY's take-profit must clear the ASK, not the bid."""
    ex, sent = _executor(None)
    pos = _pos(sl=3875.0, tp=3890.0)              # BUY, entry 3890.0
    ok = _run(ex, sent, pos, sl=3875.0, tp=3890.70, tick=_tick(3890.00, 3890.52))
    assert ok is True
    # ask 3890.52 + 0.40 = 3890.92
    assert sent[-1]["tp"] == pytest.approx(3890.92)


def test_take_profit_on_the_wrong_side_of_the_market_is_refused():
    ex, sent = _executor(None)
    pos = _pos(sl=3875.0, tp=3890.0)
    ok = _run(ex, sent, pos, sl=3875.0, tp=3890.40, tick=_tick(3890.00, 3890.52))
    assert ok is False
    assert ex.last_modify_result["retcode"] == 10016


def test_sell_side_uses_the_ask_and_the_upper_limit():
    ex, sent = _executor(None)
    pos = _pos(side=1, sl=3905.0)                # SELL
    ok = _run(ex, sent, pos, sl=3890.30, tick=_tick(3890.00, 3890.20))
    assert ok is True
    # A SELL's stop must be ABOVE the ask: 3890.20 + 0.40 = 3890.60.
    assert sent[-1]["sl"] == pytest.approx(3890.60)


# ---------------------------------------------------------------------------
# 2. The entry blackout
# ---------------------------------------------------------------------------

def test_blackout_blocks_the_configured_utc_window(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_UTC_START_HOUR", 0, raising=False)
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_UTC_END_HOUR", 2, raising=False)
    blocked, window = main._entry_blackout_active(dt.datetime(2026, 11, 27, 1, 30, tzinfo=dt.timezone.utc))
    assert blocked is True
    assert "00:00-02:00" in window
    assert main._entry_blackout_active(dt.datetime(2026, 11, 27, 3, 0, tzinfo=dt.timezone.utc))[0] is False


def test_blackout_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_ENABLED", False, raising=False)
    assert main._entry_blackout_active(dt.datetime(2026, 11, 27, 1, 0, tzinfo=dt.timezone.utc))[0] is False


def test_blackout_window_wraps_over_midnight(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_UTC_START_HOUR", 22, raising=False)
    monkeypatch.setattr(config, "ENTRY_BLACKOUT_UTC_END_HOUR", 2, raising=False)
    assert main._entry_blackout_active(dt.datetime(2026, 11, 27, 23, 0, tzinfo=dt.timezone.utc))[0] is True
    assert main._entry_blackout_active(dt.datetime(2026, 11, 27, 1, 0, tzinfo=dt.timezone.utc))[0] is True
    assert main._entry_blackout_active(dt.datetime(2026, 11, 27, 12, 0, tzinfo=dt.timezone.utc))[0] is False


# ---------------------------------------------------------------------------
# 3. The R-relative breakeven activation
# ---------------------------------------------------------------------------

def _be_armed(mfe, initial_r, mult, atr=6.0):
    pol = DynamicRiskPolicy(be_r_activation_mult=mult)
    res = compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3890.0,
        live_price=3890.0 + mfe,
        prev_peak=3890.0 + mfe,
        current_sl=0.0,
        atr=atr,
        policy=pol,
        digits=2,
        stops_buffer=0.25,
        initial_r=initial_r,
    )
    return res["be_armed"]


def test_be_arms_at_a_quarter_of_the_trades_own_risk():
    # R = 20 -> 0.25R = $5.00; the ATR term (0.75 x 6.0 = 4.50) and the $2.50 floor
    # are both lower, so the R term decides.
    assert _be_armed(4.60, 20.0, 0.25) is False
    assert _be_armed(5.10, 20.0, 0.25) is True
    # With the shipped 0.50R gate the same excursion does NOT arm.
    assert _be_armed(5.10, 20.0, 0.50) is False


# ---------------------------------------------------------------------------
# 4. The ledger reconciliation and the new summary stats
# ---------------------------------------------------------------------------

def _signal(layers, realized_pnl):
    return {
        "signal_key": "1:1",
        "chat_id": 1,
        "message_id": 1,
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "entry_type": "ZONE",
        "total_lots": 0.02,
        "filled_lots": 0.02,
        "tickets": [1, 2],
        "layers_filled": ["L1", "L2"],
        "layers": layers,
        "exit_reason": "DYNAMIC_ATR_TRAIL_SL",
        "realized_pnl": realized_pnl,
        "currency": "GBP",
        "server_date": "2026-11-27",
    }


def _layer(ticket, pnl, deal_tickets):
    return {
        "ticket": ticket,
        "lot": 0.01,
        "remaining_lot": 0.0,
        "status": "CLOSED",
        "entry_price": 3890.0,
        "exit_price": 3892.0,
        "realized_pnl": pnl,
        "exit_deal_tickets": deal_tickets,
    }


def test_realized_pnl_is_reconciled_against_the_exit_deals():
    sig = _signal({"L1": _layer(1, 1.20, [9001]), "L2": _layer(2, -0.70, [9002])}, realized_pnl=2.50)
    rec = trade_ledger.build_trade_record("1:1", sig, 2.50, "DYNAMIC_ATR_TRAIL_SL", "GBP", False)
    assert rec["realized_pnl"] == pytest.approx(0.50)
    assert rec["realized_pnl_variance"] == pytest.approx(2.00)
    assert rec["realized_pnl_layers_counted"] == 2


def test_realized_pnl_is_kept_when_the_deals_agree():
    sig = _signal({"L1": _layer(1, 1.20, [9001]), "L2": _layer(2, -0.70, [9002])}, realized_pnl=0.50)
    rec = trade_ledger.build_trade_record("1:1", sig, 0.50, "DYNAMIC_ATR_TRAIL_SL", "GBP", False)
    assert rec["realized_pnl"] == pytest.approx(0.50)
    assert rec["realized_pnl_variance"] is None


def test_a_partial_deal_sum_never_replaces_the_realized_pnl():
    """A layer with deal tickets but no P&L must not silently shrink the record."""
    layers = {
        "L1": _layer(1, 1.20, [9001]),
        "L2": _layer(2, None, [9002]),
    }
    sig = _signal(layers, realized_pnl=1.20)
    rec = trade_ledger.build_trade_record("1:1", sig, 1.20, "DYNAMIC_ATR_TRAIL_SL", "GBP", False)
    assert rec["realized_pnl"] == pytest.approx(1.20)
    assert rec["realized_pnl_variance"] is None
    assert rec["realized_pnl_layers_counted"] == 1


def test_per_layer_exit_deal_tickets_are_stored():
    sig = _signal({"L1": _layer(1, 1.20, [9001])}, realized_pnl=1.20)
    rec = trade_ledger.build_trade_record("1:1", sig, 1.20, "DYNAMIC_ATR_TRAIL_SL", "GBP", False)
    assert rec["layer_exits"][0]["exit_deal_tickets"] == [9001]


def test_stats_report_the_mfe_surrendered_and_the_r_distribution(tmp_path):
    ledger = trade_ledger.TradeLedger(tmp_path / "l.jsonl")
    ledger.append_trade({
        "realized_pnl": 1.20, "giveback_cash": 4.50, "r_at_exit": 0.25,
        "server_date": "2026-11-27", "exit_reason": "X",
    })
    ledger.append_trade({
        "realized_pnl": -3.10, "giveback_cash": 1.00, "r_at_exit": 0.80,
        "server_date": "2026-11-27", "exit_reason": "Y",
    })
    stats = ledger.get_stats(server_date="2026-11-27")
    assert stats["mfe_surrendered_cash"] == pytest.approx(5.50)
    assert stats["today_mfe_surrendered_cash"] == pytest.approx(5.50)
    assert stats["r_at_exit_max"] == pytest.approx(0.80)
    assert stats["r_at_exit_avg"] == pytest.approx(0.525)
    assert stats["trades_reached_0_5r"] == 1
    assert stats["trades_reached_1r"] == 0
