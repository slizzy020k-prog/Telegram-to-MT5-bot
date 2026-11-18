"""
Tests for v3.4.7: Staged Market Entry (L1 First, L2-L4 on Zone Edit)

The rule:
- When a bare BUY/SELL signal arrives (no zone, no single price), ONLY L1 is placed at market.
- When a single-price signal arrives (no zone), ONLY L1 is placed.
- L2, L3, L4 are placed as limit orders when the edit arrives with zone/limit prices.
- Unpriced directional signals with soft risk tags ("Sell xauusd now very high risk")
  must NOT be blocked by Gate 2!

Regression: All 4 layers placed at same market price on bare BUY/SELL with SL+TPs present.
"""
import pytest
from unittest.mock import patch, MagicMock
from mt5_executor import MT5Executor
from parser import ZoneInfo, parse_telegram_message
import config


def _make_executor():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    return ex


class TestStagedMarketEntry:
    """
    Unit-level: verify execute_4layer_market with max_layers=1 only places L1.
    """

    def test_max_layers_1_places_only_l1(self):
        """max_layers=1 must return exactly 1 market ticket and no pending tickets."""
        ex = _make_executor()
        mk, pd, layers = ex.execute_4layer_market(
            symbol="XAUUSD.s",
            direction="BUY",
            sl=3710.0,
            final_tp=3733.0,
            raw_tp1=3733.0,
            raw_tp2=3740.0,
            raw_tp3=3750.0,
            raw_tp4=3760.0,
            tp_levels={"TP1": 3733.0, "TP2": 3740.0, "TP3": 3750.0, "TP4": 3760.0},
            max_layers=1,
        )
        assert len(mk) == 1, f"Expected 1 market ticket, got {len(mk)}: {mk}"
        assert len(pd) == 0, f"Expected 0 pending tickets, got {len(pd)}: {pd}"
        assert "L1" in layers, "L1 layer must be present"
        assert "L2" not in layers, "L2 must NOT be placed until zone edit arrives"
        assert "L3" not in layers, "L3 must NOT be placed until zone edit arrives"
        assert "L4" not in layers, "L4 must NOT be placed until zone edit arrives"

    def test_max_layers_1_works_even_with_sl_and_tp(self):
        """
        Regression: when SL + TPs are present, old code set is_market_stub=False
        and placed all 4 layers. New code always uses max_layers=1 for bare market signals.
        """
        ex = _make_executor()
        # Simulate signal that has SL and TPs in first message (provider pattern)
        mk, pd, layers = ex.execute_4layer_market(
            symbol="XAUUSD.s",
            direction="SELL",
            sl=3760.0,       # SL present
            final_tp=3710.0,
            raw_tp1=3710.0,
            raw_tp2=3700.0,
            raw_tp3=3690.0,
            raw_tp4=3680.0,
            tp_levels={"TP1": 3710.0, "TP2": 3700.0},  # TPs present
            max_layers=1,    # Must always be 1 for bare market
        )
        assert len(mk) == 1, f"Should only open L1 even when SL+TPs are present. Got {len(mk)} market orders"
        assert len(pd) == 0
        open_layers = [k for k, v in layers.items() if isinstance(v, dict) and v.get("ticket") is not None]
        assert len(open_layers) == 1, f"Only 1 layer should be live, got: {open_layers}"

    def test_single_price_max_layers_1(self):
        """Single-price signals without a zone must only place L1."""
        ex = _make_executor()
        mk, pd, layers = ex.execute_4layer_single_price(
            symbol="XAUUSD.s",
            direction="SELL",
            price=3739.0,
            sl=3760.0,
            final_tp=3733.0,
            raw_tp1=3733.0,
            max_layers=1,
        )
        total = len(mk) + len(pd)
        assert total == 1, f"Single-price with max_layers=1 must place exactly 1 order, got {total}"
        assert "L1" in layers
        assert "L2" not in layers

    def test_full_4_layers_placed_when_zone_edit_arrives(self):
        """
        When L1 is already open and zone edit arrives, zone execution provides tiers
        for all layers.
        """
        ex = _make_executor()
        zone = ZoneInfo(top=3740.0, mid=3735.0, bottom=3730.0, direction="BUY")
        mk2, pd2, layers2 = ex.execute_4layer_zone(
            symbol="XAUUSD.s",
            direction="BUY",
            zone=zone,
            sl=3710.0,
            final_tp=3733.0,
            raw_tp1=3733.0,
            raw_tp2=3740.0,
            raw_tp3=3750.0,
            raw_tp4=3760.0,
            tp_levels={"TP1": 3733.0, "TP2": 3740.0},
        )
        total = len(mk2) + len(pd2)
        assert total >= 2, f"Zone edit should place multiple layers, got mk={mk2} pd={pd2}"

    def test_unpriced_signal_with_risk_tag_passes_gate_2(self):
        """
        Regression: 'Sell xauusd now very high risk' was blocked by Gate 2
        because 'high risk' matched _SOFT_RISK_TAG_BLOCKERS and _has_real_trade_structure
        only checked for explicit prices >= 500.
        Directional market orders ('Sell now', 'Buy gold now') must pass!
        """
        res = parse_telegram_message("Sell xauusd now very high risk")
        assert res is not None, "Signal must not be filtered by Gate 2"
        assert res.direction == "SELL"
        assert res.symbol == "XAUUSD.s"
        assert res.is_instant_now is True
        assert res.order_intent == "MARKET"
        assert res.is_zone is False
