"""Regression tests: the per-signal order cap and the visibility of the fix.

Two additions to the 2026-11-27 fix:

1. MAX_ORDERS_PER_SIGNAL is documented as a hard cap on live orders per signal for
   EVERY entry type (ARCHITECTURE_v3.md), but the zone path placed all four layers
   unconditionally, so the cap was silently ignored for the entry type that carries
   the most live exposure. On live account 36393319 the ladder depth was the entire
   loss: L1-only +GBP 33.95 (n=33) vs every deeper ladder -GBP 232.73 (n=13).

2. A broker stops-level rejection (retcode 10016) is an expected outcome, but it must
   stay VISIBLE in the production log: the copier log runs at INFO, so the rejection
   is logged once per ticket at INFO (repeats at DEBUG) instead of never.

Hermetic: dry-run executors, fake deals, no broker connection, no live trades.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import mt5_executor
from parser import ZoneInfo

DEAL_REASON_EXPERT = 3
DEAL_REASON_SO = 6
DEAL_REASON_VMARGIN = 8
DEAL_REASON_SPLIT = 9


def _ex(capital=200.0):
    ex = mt5_executor.MT5Executor(dry_run=True)
    ex._dry_run_capital = capital
    return ex


def _zone_ex(ex, **kw):
    return ex.execute_4layer_zone(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=ZoneInfo(3895.0, 3891.0, 3887.0, "BUY"),
        sl=3875.0,
        final_tp=3899.5,
        raw_tp1=3900.0,
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. The per-signal order cap
# ---------------------------------------------------------------------------

def test_zone_entry_places_all_four_layers_by_default():
    """Default behaviour (MAX_ORDERS_PER_SIGNAL = 4) is unchanged."""
    ex = _ex()
    mk, pd, layers = _zone_ex(ex)
    assert set(layers.keys()) == {"L1", "L2", "L3", "L4"}
    assert len(mk) + len(pd) == 4


def test_zone_entry_honours_the_per_signal_order_cap():
    ex = _ex()
    mk, pd, layers = _zone_ex(ex, max_layers=1)
    assert set(layers.keys()) == {"L1"}
    assert len(mk) + len(pd) == 1
    assert round(sum(v["lot"] for v in layers.values()), 4) == 0.01


def test_zone_entry_cap_of_two_places_l1_and_l2():
    ex = _ex()
    mk, pd, layers = _zone_ex(ex, max_layers=2)
    assert set(layers.keys()) == {"L1", "L2"}
    assert len(mk) + len(pd) == 2


def test_zone_entry_cap_never_places_a_zero_lot_layer():
    """The cap must drop the deeper layers entirely, not place them with 0.0 lots."""
    ex = _ex()
    mk, pd, layers = _zone_ex(ex, max_layers=1)
    for _name, layer in layers.items():
        assert layer["lot"] > 0.0


def test_zone_entry_cap_is_ignored_when_max_layers_covers_all_layers():
    ex = _ex()
    mk4, pd4, layers4 = _zone_ex(ex, max_layers=4)
    assert set(layers4.keys()) == {"L1", "L2", "L3", "L4"}


# ---------------------------------------------------------------------------
# 2. The rejection stays visible
# ---------------------------------------------------------------------------

def _rejecting_ex():
    ex = mt5_executor.MT5Executor(dry_run=False)
    ex._connected = True
    fake_pos = SimpleNamespace(ticket=70001, symbol="XAUUSD.s", sl=3843.56, tp=3862.75)

    def _send(req):
        return SimpleNamespace(retcode=10016, comment="Invalid stops")

    patches = (
        patch.object(ex, "_ensure_connected", return_value=True),
        patch.object(ex, "get_symbol_info", return_value=SimpleNamespace(
            digits=2, point=0.01, trade_stops_level=50)),
        patch.object(mt5_executor.mt5, "positions_get", return_value=[fake_pos]),
        patch.object(mt5_executor.mt5, "order_send", side_effect=_send),
    )
    return ex, patches


def test_stops_level_rejection_is_logged_once_per_ticket_at_info(caplog):
    ex, patches = _rejecting_ex()
    for p in patches:
        p.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="copier.mt5"):
            for _ in range(4):
                assert ex.modify_position_sltp(70001, sl=3857.90, tp=None) is False
    finally:
        for p in patches:
            p.stop()
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(infos) == 1, "the rejection must be visible in the production log exactly once"
    assert "stops level" in infos[0].getMessage()
    assert len(debugs) == 3, "the repeats must be demoted to DEBUG"


# ---------------------------------------------------------------------------
# 3. The EA-side reason family
# ---------------------------------------------------------------------------

def _classify(reason, comment="TG Copier Close"):
    in_deal = SimpleNamespace(
        ticket=1, entry=0, reason=DEAL_REASON_EXPERT, profit=0.0, commission=0.0,
        swap=0.0, fee=0.0, price=3890.0, volume=0.01, time=1700000000, comment="TG L1 (Market)",
    )
    out_deal = SimpleNamespace(
        ticket=2, entry=1, reason=reason, profit=1.0, commission=0.0, swap=0.0,
        fee=0.0, price=3890.0, volume=0.01, time=1700000600, comment=comment,
    )
    ex = mt5_executor.MT5Executor(dry_run=True)
    return ex._compute_close_info_from_deals(701, [in_deal, out_deal])


def test_expert_reason_is_an_ea_close():
    assert _classify(DEAL_REASON_EXPERT)["reason"] == "EA_CLOSE"


def test_stop_out_reason_is_a_stop_loss():
    """DEAL_REASON_SO (6) is the broker's stop-out: it is a stop-loss exit."""
    assert _classify(DEAL_REASON_SO, "[so 18.14%/3.16/17.42]")["reason"] == "SL"


@pytest.mark.parametrize("reason", [DEAL_REASON_VMARGIN, DEAL_REASON_SPLIT])
def test_vmargin_and_split_are_not_ea_closes(reason):
    """8 = VMARGIN and 9 = SPLIT are not expert closes; they must not be EA_CLOSE."""
    assert _classify(reason)["reason"] != "EA_CLOSE"
