import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from config import DEFAULT_SYMBOL, MAGIC_NUMBER
from parser import parse_message
from mt5_executor import MT5Executor
from state_manager import StateManager
import main


USER_EXAMPLES = [
    # Example 1
    """Buy xauusd now tight sl

Tp 3890
Tp 3900
Tp 3930
Tp 3970
Tp 4000

Sl 3875

Won’t count it as a loss if it’s hits risk very tight sl enter at own risk""",

    # Example 2
    """Buy xauusd 3895-3887

Tp 3900
Tp 3910
Tp 3930
Tp 3950
Tp 4000

Sl 3872""",

    # Example 3
    """SELL XAUUSD 3897-3900

TP 3880 
TP 3860 
TP 3800 

SL 3907""",

    # Example 4
    """buy now 3896-3886
SL 3870
TP 3899
TP 3906
TP 3916
TP 3920
TP 3950""",

    # Example 5
    """BUY 3885-3875
SL 3860
TP 3888
TP 3895
TP 3898
TP 3905
TP 3920
TP 3950
TP 4000""",

    # Example 6
    """BUY 3889-3879
SL 3869
TP 3892
TP 3899
TP 3902
TP 3910
TP 3925
TP 3950
TP 4000""",

    # Example 7
    """BUY 3905-3895
SL 3880
TP 3908
TP 3915
TP 3925
TP 3930
TP 3950
TP 3970
TP 4000""",
]


def test_all_seven_user_examples_pass_and_extract_tp2():
    """Verify that all 7 real trader signal examples pass all 6 gates, validate SL, and extract TP2."""
    for i, ex in enumerate(USER_EXAMPLES, start=1):
        parsed = parse_message(ex)
        assert parsed is not None, f"Example {i} failed parsing: {ex}"
        assert parsed.sl_valid is True, f"Example {i} SL marked invalid: SL={parsed.sl}"
        assert parsed.raw_tp1 is not None, f"Example {i} raw_tp1 missing"
        assert parsed.raw_tp2 is not None, f"Example {i} raw_tp2 missing"
        assert parsed.raw_tp2 != parsed.raw_tp1, f"Example {i} raw_tp2 should differ from raw_tp1"


def test_tp1_scaling_shallow_pullback_case_a():
    """Case A: Only Layer 1 filled -> Bank Layer 1, cancel Layers 2 & 3, mark CLOSED."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    layers = {
        "TOP": {"lot": 0.01, "ticket": 101, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 102, "order_type": "LIMIT", "status": "PENDING"},
        "BOTTOM": {"lot": 0.02, "ticket": 103, "order_type": "LIMIT", "status": "PENDING"},
    }
    sig_data = {
        "layers": layers,
        "market_tickets": [101],
        "pending_tickets": [102, 103],
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
    }

    runner_ticket = executor.handle_tp1_scaling_and_select_runner(sig_data)
    assert runner_ticket is None
    assert sig_data["status"] == "CLOSED"
    assert sig_data["runner_layer"] is None
    assert sig_data["market_tickets"] == []
    assert sig_data["pending_tickets"] == []
    assert layers["TOP"]["status"] == "CLOSED"


def test_tp1_scaling_medium_pullback_case_b():
    """Case B: Layers 1 & 2 filled -> Bank Layer 1, cancel Layer 3, Layer 2 designated runner."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    layers = {
        "TOP": {"lot": 0.01, "ticket": 201, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 202, "order_type": "LIMIT", "status": "OPEN"},
        "BOTTOM": {"lot": 0.02, "ticket": 203, "order_type": "LIMIT", "status": "PENDING"},
    }
    sig_data = {
        "layers": layers,
        "market_tickets": [201, 202],
        "pending_tickets": [203],
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
    }

    runner_ticket = executor.handle_tp1_scaling_and_select_runner(sig_data)
    assert runner_ticket == 202
    assert sig_data["status"] == "RUNNER_ACTIVE"
    assert sig_data["runner_layer"] == "MID"
    assert sig_data["soft_tp1_floor"] == 3899.50  # 3900.00 - 0.50
    assert sig_data["market_tickets"] == [202]
    assert sig_data["pending_tickets"] == []
    assert layers["TOP"]["status"] == "CLOSED"
    assert layers["MID"]["status"] == "RUNNER"


def test_tp1_scaling_full_zone_fill_case_c():
    """Case C: All 3 layers filled -> Bank Layers 1 & 2, Layer 3 (0.02 lot) designated primary runner."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    layers = {
        "TOP": {"lot": 0.01, "ticket": 301, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 302, "order_type": "LIMIT", "status": "OPEN"},
        "BOTTOM": {"lot": 0.02, "ticket": 303, "order_type": "LIMIT", "status": "OPEN"},
    }
    sig_data = {
        "layers": layers,
        "market_tickets": [301, 302, 303],
        "pending_tickets": [],
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
    }

    runner_ticket = executor.handle_tp1_scaling_and_select_runner(sig_data)
    assert runner_ticket == 303
    assert sig_data["status"] == "RUNNER_ACTIVE"
    assert sig_data["runner_layer"] == "BOTTOM"
    assert sig_data["soft_tp1_floor"] == 3899.50
    assert sig_data["market_tickets"] == [303]
    assert layers["TOP"]["status"] == "CLOSED"
    assert layers["MID"]["status"] == "CLOSED"
    assert layers["BOTTOM"]["status"] == "RUNNER"


def test_manage_designated_runner_virtual_tp1_floor():
    """Test Virtual TP1 Floor: If price drops back to TP1 - 0.50, closes runner immediately at market."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sig_data = {
        "runner_ticket": 303,
        "runner_layer": "BOTTOM",
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "soft_tp1_floor": 3899.50,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "market_tickets": [303],
        "status": "RUNNER_ACTIVE",
    }

    # Price drops to 3899.40 (below floor 3899.50)
    mock_tick = MagicMock()
    mock_tick.bid = 3899.40
    mock_tick.ask = 3899.70

    with patch.object(executor, "get_tick", return_value=mock_tick):
        still_active = executor.manage_designated_runner(sig_data)

    assert still_active is False
    assert sig_data["status"] == "CLOSED"
    assert sig_data["runner_ticket"] is None


def test_manage_designated_runner_tp2_shortfall_guard():
    """Test TP2 Shortfall Guard: Surges near TP2 (within $2.00) then drops $0.80 -> instant close."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sig_data = {
        "runner_ticket": 303,
        "runner_layer": "BOTTOM",
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "soft_tp1_floor": 3899.50,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "runner_peak_price": 3900.0,
        "tp2_shortfall_armed": False,
        "market_tickets": [303],
        "status": "RUNNER_ACTIVE",
    }

    # 1. Price climbs to 3908.50 (within 20 pips of TP2 3910.00) -> Arms guard
    mock_tick_surge = MagicMock()
    mock_tick_surge.bid = 3908.50
    mock_tick_surge.ask = 3908.80

    with patch.object(executor, "get_tick", return_value=mock_tick_surge):
        active1 = executor.manage_designated_runner(sig_data)

    assert active1 is True
    assert sig_data["tp2_shortfall_armed"] is True
    assert sig_data["runner_peak_price"] == 3908.50

    # 2. Price rolls over to 3907.60 (dropped 0.90 >= 0.80 pullback) -> Triggers instant bank!
    mock_tick_drop = MagicMock()
    mock_tick_drop.bid = 3907.60
    mock_tick_drop.ask = 3907.90

    with patch.object(executor, "get_tick", return_value=mock_tick_drop):
        active2 = executor.manage_designated_runner(sig_data)

    assert active2 is False
    assert sig_data["status"] == "CLOSED"
    assert sig_data["runner_ticket"] is None


@pytest.mark.asyncio
async def test_saved_messages_commands():
    """Verify all saved messages commands: /bank, /closeall, /sim_tp1, /sim_shortfall, /sim_trail, /status."""
    mock_event = MagicMock()
    mock_event.reply = AsyncMock()

    # 1. /sim_tp1 1
    mock_event.message.message = "/sim_tp1 1"
    await main.handle_saved_messages_command(mock_event)
    assert mock_event.reply.called
    reply_text = mock_event.reply.call_args[0][0]
    assert "Shallow Pullback" in reply_text
    assert "Banked Layer 1" in reply_text

    # 2. /sim_tp1 2
    mock_event.message.message = "/sim_tp1 2"
    await main.handle_saved_messages_command(mock_event)
    reply_text = mock_event.reply.call_args[0][0]
    assert "Medium Pullback" in reply_text
    assert "Designated Runner: Layer 2" in reply_text

    # 3. /sim_tp1 3
    mock_event.message.message = "/sim_tp1 3"
    await main.handle_saved_messages_command(mock_event)
    reply_text = mock_event.reply.call_args[0][0]
    assert "Full Zone Fill" in reply_text
    assert "Designated Runner: Layer 3" in reply_text

    # 4. /sim_shortfall
    mock_event.message.message = "/sim_shortfall"
    await main.handle_saved_messages_command(mock_event)
    reply_text = mock_event.reply.call_args[0][0]
    assert "Near-Miss Protection" in reply_text
    assert "3899.20" in reply_text
    assert "SHORTFALL GUARD TRIGGERS INSTANTLY" in reply_text

    # 5. /sim_trail 3
    mock_event.message.message = "/sim_trail 3"
    await main.handle_saved_messages_command(mock_event)
    reply_text = mock_event.reply.call_args[0][0]
    assert "Runner Trailing & TP2 Guard" in reply_text
    assert "Virtual TP1 Floor: `3899.50`" in reply_text

    # 6. /status
    mock_event.message.message = "/status"
    await main.handle_saved_messages_command(mock_event)
    reply_text = mock_event.reply.call_args[0][0]
    assert "Telegram Copier System Status" in reply_text
    assert "/sim_tp1" in reply_text
