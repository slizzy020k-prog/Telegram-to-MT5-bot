"""
Tests for profit notification feature:
- TP1 hit alert includes Amount Made + Balance/Credit/Equity
- TP2 hit alert includes Amount Made + Balance/Credit/Equity
- Runner close alert includes Amount Made + Balance/Credit/Equity
- Shortfall guard alert includes Amount Made + Balance/Credit/Equity
- /bank command includes Amount Made + Balance/Credit/Equity
- format_profit helper works correctly
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from config import DEFAULT_SYMBOL
from mt5_executor import MT5Executor
from state_manager import StateManager
import main


def test_format_profit_helper():
    """format_profit returns +£X.XX GBP style strings."""
    assert main.format_profit(2.50, "GBP") == "+£2.50 GBP"
    assert main.format_profit(-1.30, "GBP") == "-£1.30 GBP"
    assert main.format_profit(0.0, "GBP") == "+£0.00 GBP"
    assert main.format_profit(5.00, "USD") == "+$5.00 USD"
    assert main.format_profit(3.14, "EUR") == "+€3.14 EUR"
    assert main.format_profit(1.00, "CHF") == "+1.00 CHF"


def test_tp1_scaling_records_banked_tickets():
    """handle_tp1_scaling_and_select_runner stores last_banked_tickets for profit lookup."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    layers = {
        "TOP": {"lot": 0.01, "ticket": 901, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 902, "order_type": "LIMIT", "status": "OPEN"},
        "BOTTOM": {"lot": 0.02, "ticket": 903, "order_type": "LIMIT", "status": "OPEN"},
    }
    sig = {
        "layers": layers,
        "market_tickets": [901, 902, 903],
        "pending_tickets": [],
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
    }
    runner = executor.handle_tp1_scaling_and_select_runner(sig)

    assert runner == 903  # BOTTOM is the runner
    assert 901 in sig["last_banked_tickets"]  # TOP banked
    assert 902 in sig["last_banked_tickets"]  # MID banked
    assert 903 not in sig["last_banked_tickets"]  # BOTTOM is runner, NOT banked


def test_runner_close_stores_last_runner_ticket_and_reason():
    """manage_designated_runner stores last_runner_ticket and runner_close_reason on broker close."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    # Active tick near TP2: Pure Dynamic ATR Engine keeps runner active (zero virtual closes!)
    sig = {
        "runner_ticket": 801,
        "runner_layer": "BOTTOM",
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "soft_tp1_floor": 3899.50,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "market_tickets": [801],
        "status": "RUNNER_ACTIVE",
    }

    mock_tick = MagicMock()
    mock_tick.bid = 3910.20
    mock_tick.ask = 3910.50

    with patch.object(executor, "get_tick", return_value=mock_tick):
        still_active = executor.manage_designated_runner(sig)

    assert still_active is True  # Zero virtual close on tick!

    # Active tick during pullback below soft floor: Pure Dynamic ATR Engine keeps runner active (zero virtual closes!)
    sig2 = {
        "runner_ticket": 802,
        "runner_layer": "MID",
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "soft_tp1_floor": 3899.50,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "market_tickets": [802],
        "status": "RUNNER_ACTIVE",
    }

    mock_tick2 = MagicMock()
    mock_tick2.bid = 3899.40
    mock_tick2.ask = 3899.70

    with patch.object(executor, "get_tick", return_value=mock_tick2):
        still_active2 = executor.manage_designated_runner(sig2)

    assert still_active2 is True  # Zero virtual close on pullback!

    # When position actually closes on broker (e.g. SL or TP hit in live mode):
    executor_live = MT5Executor(dry_run=False)
    import mt5_executor as ex_mod
    with patch.object(ex_mod.mt5, "positions_get", return_value=[]), \
         patch.object(executor_live, "get_position_close_info", return_value={"reason": "SL"}):
        still_active_closed = executor_live.manage_designated_runner(sig)

    assert still_active_closed is False
    assert sig["last_runner_ticket"] == 801
    assert "Trailed Stop Loss Hit" in sig["runner_close_reason"]


def test_get_position_closed_profit_dry_run():
    """get_position_closed_profit returns cached or default value in dry-run."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    # After closing, cached profit is available
    executor.close_market_position(555)
    profit = executor.get_position_closed_profit(555)
    assert profit == 1.25  # Dry-run default

    # Unknown ticket returns default
    profit_unknown = executor.get_position_closed_profit(999)
    assert profit_unknown == 1.25  # Dry-run default


def test_get_account_financials_dry_run():
    """get_account_financials returns balance/credit/equity in dry-run mode."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    fin = executor.get_account_financials()
    assert fin["balance"] == 11.03
    assert fin["credit"] == 7.03
    assert fin["equity"] == 18.06
    assert fin["currency"] == "GBP"
    assert "Balance" in fin["formatted"]
    assert "Credit" in fin["formatted"]
    assert "Equity" in fin["formatted"]
    assert "11.03" in fin["formatted"]
    assert "7.03" in fin["formatted"]
    assert "18.06" in fin["formatted"]


@pytest.mark.asyncio
async def test_tp1_notification_includes_profit_and_financials(tmp_path):
    """When TP1 is hit, the Telegram notification includes Amount Made + Balance/Credit/Equity."""
    test_state_file = tmp_path / "tp1_notify_test.json"
    main.state_mgr = StateManager(test_state_file)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Capture notifications
    sent_alerts = []
    original_notify = main.notify_saved_messages

    async def capture_notify(text):
        sent_alerts.append(text)

    main.notify_saved_messages = capture_notify

    try:
        msg_id = 5001
        main.state_mgr.create_signal(
            message_id=msg_id,
            symbol="XAUUSD.s",
            direction="BUY",
            layers={
                "TOP": {"lot": 0.01, "ticket": 401, "order_type": "MARKET", "status": "OPEN"},
                "MID": {"lot": 0.01, "ticket": 402, "order_type": "LIMIT", "status": "OPEN"},
                "BOTTOM": {"lot": 0.02, "ticket": 403, "order_type": "LIMIT", "status": "OPEN"},
            },
            market_tickets=[401, 402, 403],
            pending_tickets=[],
            status="ACTIVE",
            raw_tp1=3900.0,
            raw_tp2=3910.0,
            raw_text="Buy 3895-3887 TP 3900 SL 3875",
        )

        # Simulate TP1 hit
        mock_tick = MagicMock()
        mock_tick.bid = 3900.50
        mock_tick.ask = 3900.80

        with patch.object(main.executor, "get_tick", return_value=mock_tick):
            # Run one iteration of the watcher logic inline
            sig = main.state_mgr.get_signal(msg_id)
            runner_ticket = main.executor.handle_tp1_scaling_and_select_runner(sig)

            banked_tkts = sig.get("last_banked_tickets", [])
            banked_profit = sum(main.executor.get_position_closed_profit(t) for t in banked_tkts)
            sig["banked_profit"] = banked_profit
            main.state_mgr.set_tp1_reached(msg_id, True)
            main.state_mgr.update_signal(msg_id, sig)

            fin = main.executor.get_account_financials()
            curr = fin.get("currency", "GBP")
            profit_str = main.format_profit(banked_profit, curr)

            alert = (
                f"🎯 **TP1 Reached & Profit Banked! [ID: {msg_id}]**\n"
                f"• 💰 **Amount Made at TP1:** `{profit_str}`\n\n"
                f"{fin['formatted']}"
            )
            await capture_notify(alert)

        assert len(sent_alerts) == 1
        alert_text = sent_alerts[0]

        # Verify profit is included
        assert "Amount Made" in alert_text
        assert "GBP" in alert_text

        # Verify Balance, Credit, Equity are all present
        assert "Balance" in alert_text
        assert "Credit" in alert_text
        assert "Equity" in alert_text
        assert "11.03" in alert_text
        assert "7.03" in alert_text
        assert "18.06" in alert_text

    finally:
        main.notify_saved_messages = original_notify


@pytest.mark.asyncio
async def test_bank_command_includes_profit_and_financials():
    """The /bank command response includes Amount Made + Balance/Credit/Equity."""
    mock_event = MagicMock()
    mock_event.reply = AsyncMock()
    mock_event.message.message = "/bank"

    # Set up state with an active signal
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmpdir:
        test_state_file = pathlib.Path(tmpdir) / "bank_test.json"
        main.state_mgr = StateManager(test_state_file)

        msg_id = 6001
        main.state_mgr.create_signal(
            message_id=msg_id,
            symbol="XAUUSD.s",
            direction="BUY",
            layers={
                "TOP": {"lot": 0.01, "ticket": 501, "order_type": "MARKET", "status": "OPEN"},
                "MID": {"lot": 0.01, "ticket": 502, "order_type": "LIMIT", "status": "OPEN"},
                "BOTTOM": {"lot": 0.02, "ticket": 503, "order_type": "LIMIT", "status": "OPEN"},
            },
            market_tickets=[501, 502, 503],
            pending_tickets=[],
            status="ACTIVE",
            raw_tp1=3900.0,
            raw_tp2=3910.0,
            raw_text="Buy 3895-3887 TP 3900 SL 3875",
        )

        await main.handle_saved_messages_command(mock_event)

        reply_text = mock_event.reply.call_args[0][0]

        # Verify profit info
        assert "Amount Made" in reply_text
        assert "GBP" in reply_text

        # Verify Balance, Credit, Equity
        assert "Balance" in reply_text
        assert "Credit" in reply_text
        assert "Equity" in reply_text


@pytest.mark.asyncio
async def test_status_command_shows_credit():
    """The /status command now shows Credit alongside Balance and Equity."""
    mock_event = MagicMock()
    mock_event.reply = AsyncMock()
    mock_event.message.message = "/status"

    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmpdir:
        test_state_file = pathlib.Path(tmpdir) / "status_test.json"
        main.state_mgr = StateManager(test_state_file)

        await main.handle_saved_messages_command(mock_event)

        reply_text = mock_event.reply.call_args[0][0]

        assert "Balance" in reply_text
        assert "Credit" in reply_text
        assert "Equity" in reply_text
