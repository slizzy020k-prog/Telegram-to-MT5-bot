import os
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pytest
from state_manager import StateManager


@pytest.fixture
def temp_state_file(tmp_path):
    return tmp_path / "test_signals_state.json"


def test_state_manager_create_and_load(temp_state_file):
    mgr = StateManager(temp_state_file)

    layers = {
        "TOP": {"lot": 0.01, "target_price": 3895.0, "ticket": 1001, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "target_price": 3891.0, "ticket": 1002, "order_type": "LIMIT", "status": "PENDING"},
        "BOTTOM": {"lot": 0.02, "target_price": 3887.0, "ticket": 1003, "order_type": "LIMIT", "status": "PENDING"},
    }

    signal = mgr.create_signal(
        message_id=94821,
        symbol="XAUUSD",
        direction="BUY",
        layers=layers,
        zone={"top": 3895.0, "mid": 3891.0, "bottom": 3887.0},
        market_tickets=[1001],
        pending_tickets=[1002, 1003],
        zone_completed=True,
        sl=3875.0,
        raw_tp1=3900.0,
        final_tp=3899.50,
        proximity_close_price=3898.00,
        raw_text="Buy 3895-3887",
    )

    assert signal["symbol"] == "XAUUSD"
    assert signal["direction"] == "BUY"
    assert signal["layers"]["TOP"]["lot"] == 0.01
    assert signal["layers"]["MID"]["lot"] == 0.01
    assert signal["layers"]["BOTTOM"]["lot"] == 0.02
    assert signal["market_tickets"] == [1001]
    assert signal["pending_tickets"] == [1002, 1003]
    assert signal["final_tp"] == 3899.50
    assert signal["tp1_reached"] is False
    assert signal["proximity_close_price"] == 3898.00

    # Test persistence by reloading
    mgr2 = StateManager(temp_state_file)
    loaded = mgr2.get_signal(94821)
    assert loaded is not None
    assert loaded["layers"]["BOTTOM"]["lot"] == 0.02
    assert loaded["zone"]["mid"] == 3891.0


def test_state_manager_layer_updates(temp_state_file):
    mgr = StateManager(temp_state_file)
    mgr.create_signal(message_id="12345", symbol="XAUUSD", direction="BUY")

    # Update layer
    mgr.update_layer("12345", "MID", ticket=555, status="FILLED", target_price=3891.0)
    sig = mgr.get_signal("12345")
    assert sig["layers"]["MID"]["ticket"] == 555
    assert sig["layers"]["MID"]["status"] == "FILLED"
    assert sig["layers"]["MID"]["target_price"] == 3891.0

    # Mark tp1_reached
    mgr.set_tp1_reached("12345", True)
    sig = mgr.get_signal("12345")
    assert sig["tp1_reached"] is True


def test_state_manager_active_and_counts(temp_state_file):
    mgr = StateManager(temp_state_file)
    mgr.create_signal(message_id="1", symbol="XAUUSD", direction="BUY")
    assert mgr.count_open_signals() == 1

    mgr.update_signal("1", {"status": "CLOSED"})
    assert mgr.count_open_signals() == 0


def test_state_manager_cleanup_old_signals(temp_state_file):
    mgr = StateManager(temp_state_file)

    # 1. Closed signal from 10 days ago
    mgr.create_signal(message_id="old_closed", symbol="XAUUSD", direction="BUY")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    mgr.update_signal("old_closed", {"status": "CLOSED", "timestamp": old_ts})

    # 2. Active signal from 10 days ago (must NOT be pruned)
    mgr.create_signal(message_id="old_active", symbol="XAUUSD", direction="BUY")
    mgr.update_signal("old_active", {"status": "ACTIVE", "timestamp": old_ts})

    # 3. Closed signal from 2 days ago (must NOT be pruned with days=7)
    mgr.create_signal(message_id="recent_closed", symbol="XAUUSD", direction="BUY")
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    mgr.update_signal("recent_closed", {"status": "CLOSED", "timestamp": recent_ts})

    removed = mgr.cleanup_old_signals(days=7)
    assert removed == 1
    assert mgr.get_signal("old_closed") is None
    assert mgr.get_signal("old_active") is not None
    assert mgr.get_signal("recent_closed") is not None
