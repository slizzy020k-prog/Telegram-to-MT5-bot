"""Regression tests: the ledger must record WHY a trade really closed.

Live evidence (account 36393319 / PUPrime-Live 6):
  * two ledger records worth -GBP 105.43 were labelled
    "Closed manually / from MT5 app" while MT5 showed reason GATEWAY (6) -- a broker
    stop-out -- on the copier's own magic;
  * three records labelled as SL exits were in fact reason EXPERT (3), i.e. the copier
    closed them itself;
  * only 21 of 47 live labels mapped cleanly onto a broker SL.

Root cause: the close classifier looked up "DEAL_REASON_SL" / "DEAL_REASON_TP", which
MT5 does not define, so both lookups silently fell back to the literals 4 / 5. Every
other deal reason (3 = EA close, 6 = gateway stop-out) therefore collapsed into
"MANUAL/BOT" and was booked as a manual close.

Hermetic: fake deal objects only; no broker connection, no live trades.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
import mt5_executor
import trade_ledger

DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_EXPERT = 3
DEAL_REASON_BROKER = 4
DEAL_REASON_SIGNAL = 5
DEAL_REASON_GATEWAY = 6


def _deals(reason, comment, exit_price=3619.75, profit=-41.12):
    in_deal = SimpleNamespace(
        ticket=1, entry=0, reason=DEAL_REASON_EXPERT, profit=0.0, commission=0.0,
        swap=0.0, fee=0.0, price=3890.0, volume=0.01, time=1700000000, comment="TG L1 (Market)",
    )
    out_deal = SimpleNamespace(
        ticket=2, entry=1, reason=reason, profit=profit, commission=0.0, swap=0.0,
        fee=0.0, price=exit_price, volume=0.01, time=1700000600, comment=comment,
    )
    return [in_deal, out_deal]


def _classify(reason, comment, **kw):
    ex = mt5_executor.MT5Executor(dry_run=True)
    return ex._compute_close_info_from_deals(701, _deals(reason, comment, **kw))


# ---------------------------------------------------------------------------
# The raw vocabulary
# ---------------------------------------------------------------------------

def test_gateway_stop_out_is_a_stop_loss_not_a_manual_close():
    """Reason 6 is the trade gateway executing a stop-out: it IS a stop-loss exit."""
    res = _classify(DEAL_REASON_GATEWAY, "[so 18.14%/3.16/17.42]")
    assert res["reason"] == "SL"
    assert res["exit_deals"][0]["reason"] == "SL"


def test_expert_close_is_reported_as_an_ea_close():
    """Reason 3 is the copier closing the position itself."""
    res = _classify(DEAL_REASON_EXPERT, "TG Copier Close", exit_price=3863.01, profit=3.80)
    assert res["reason"] == "EA_CLOSE"
    assert res["exit_deals"][0]["reason"] == "EA_CLOSE"


def test_broker_stop_execution_is_a_stop_loss():
    res = _classify(DEAL_REASON_BROKER, "[sl 3619.75]")
    assert res["reason"] == "SL"


def test_signal_execution_is_a_take_profit():
    res = _classify(DEAL_REASON_SIGNAL, "[tp 3883.50]", exit_price=3883.50, profit=3.51)
    assert res["reason"] == "TP"


def test_human_closes_stay_manual():
    for reason in (DEAL_REASON_CLIENT, DEAL_REASON_MOBILE, DEAL_REASON_WEB):
        res = _classify(reason, "")
        assert res["reason"] == "MANUAL/BOT", reason


def test_profit_is_the_deal_profit_not_a_guess():
    res = _classify(DEAL_REASON_GATEWAY, "[so ...]", profit=-41.12)
    assert res["profit"] == pytest.approx(-41.12)


# ---------------------------------------------------------------------------
# The ledger vocabulary
# ---------------------------------------------------------------------------

def test_ea_close_is_a_canonical_ledger_code():
    assert "EA_CLOSE" in trade_ledger.VALID_EXIT_REASON_CODES
    assert trade_ledger.normalize_exit_reason_code("Closed by the copier (EA)") == "EA_CLOSE"


def test_ea_close_survives_the_signal_exit_reason_code_path():
    sig = {"exit_reason_code": "EA_CLOSE"}
    assert trade_ledger.normalize_exit_reason_code("Closed by the copier (EA)", signal_data=sig) == "EA_CLOSE"


def test_manual_close_still_maps_to_manual():
    assert trade_ledger.normalize_exit_reason_code("Closed manually / from MT5 app") == "MANUAL"


def test_classifier_maps_the_raw_ea_close_code():
    sig = {"direction": "BUY", "recorded_entry_price": 3890.0}
    out = main._classify_deal_exit_code(sig, 701, None, {"reason": "EA_CLOSE"})
    assert out == "EA_CLOSE"


def test_classifier_still_maps_sl_and_manual():
    sig = {"direction": "BUY", "recorded_entry_price": 3890.0}
    layer = {"entry_price": 3890.0}
    assert main._classify_deal_exit_code(sig, 701, layer, {"reason": "SL", "exit_price": 3619.75}) == "INITIAL_SL"
    assert main._classify_deal_exit_code(sig, 701, layer, {"reason": "MANUAL/BOT"}) == "MANUAL"
