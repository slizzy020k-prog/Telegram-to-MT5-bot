"""
v3.4.3 Regression & Unit Tests for:
1. Bank TP1 Feature (BANK_ALL_AT_TP1 & BANK_SINGLE_LAYER_AT_TP1): Bank all potential profit at TP1.
2. Tight Gold Scalp Reversal Guard Arming Calibration: Ensure near-misses on short legs (<= $4.00) arm and bank givebacks.
3. Telegram /bank command on RUNNER_ACTIVE trades: Immediately closes runner and banks profit.
4. Telegram /bank_tp1 on|off interactive toggle.
5. Capital Preservation on 50% Daily Loss Pause with DAILY_LOSS_CLOSE_OPEN_TRADES=True.
"""

import copy
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import config
import main
from main import (
    StateManager,
    TradeLedger,
    derive_tp_levels,
    _process_pre_tp1_or_backstop_tick,
    handle_saved_messages_command,
    check_and_enforce_daily_drawdown,
)
from mt5_executor import MT5Executor
from tests.test_copier_scenarios import MockTelethonEvent


def _tick(bid: float, ask: float = None):
    m = MagicMock()
    m.bid = float(bid)
    m.ask = float(ask) if ask is not None else float(bid) + 0.20
    return m


@pytest.mark.asyncio
async def test_bank_all_at_tp1_closes_all_layers_and_records_ledger(tmp_path, monkeypatch):
    """
    Verify BANK_ALL_AT_TP1=True:
    When TP1 is hit, ALL open layers (L1..L4) are closed at market, pending limits cancelled,
    status becomes CLOSED, and 100% of profit is banked into ledger.
    """
    monkeypatch.setattr(config, "BANK_ALL_AT_TP1", True)
    main.state_mgr = StateManager(tmp_path / "bank_all_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "bank_all_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    tps = derive_tp_levels([3850.0], direction="BUY")
    main.state_mgr.create_signal(
        message_id=8801,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3840.0, "tp": 3849.5, "ticket": 4001, "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3837.0, "tp": 3849.5, "ticket": 4002, "status": "OPEN"},
            "L3": {"lot": 0.01, "target_price": 3834.0, "tp": 3849.5, "ticket": 4003, "status": "OPEN"},
            "L4": {"lot": 0.02, "target_price": 3830.0, "tp": 3849.5, "ticket": 4004, "status": "PENDING"},
        },
        market_tickets=[4001, 4002, 4003],
        pending_tickets=[4004],
        raw_tp1=3850.0,
        tp_levels=tps,
        final_tp=3849.5,
        recorded_entry_price=3840.0,
        initial_price=3840.0,
    )

    alerts = []
    with patch.object(main, "notify_saved_messages", new=AsyncMock(side_effect=lambda m: alerts.append(m))):
        # Tick reaches TP1 (3850.20)
        with patch.object(main.executor, "get_tick", return_value=_tick(3850.20)):
            await _process_pre_tp1_or_backstop_tick("8801", main.state_mgr.get_signal(8801))

    sig = main.state_mgr.get_signal(8801)
    assert sig["status"] == "CLOSED"
    assert sig["market_tickets"] == []
    assert sig["pending_tickets"] == []
    assert sig["runner_ticket"] is None
    assert sig["tp1_reached"] is True
    # All 3 market tickets were banked
    assert set(sig.get("last_banked_tickets") or []) == {4001, 4002, 4003}
    assert any("100% Banked" in a for a in alerts)
    assert len(main.trade_ledger.read_trades()) == 1


@pytest.mark.asyncio
async def test_bank_single_layer_at_tp1(tmp_path, monkeypatch):
    """
    Verify BANK_SINGLE_LAYER_AT_TP1=True:
    When only 1 layer was filled and reaches TP1, it closes and banks that layer
    instead of leaving it as an unbanked runner.
    """
    monkeypatch.setattr(config, "BANK_ALL_AT_TP1", False)
    monkeypatch.setattr(config, "BANK_SINGLE_LAYER_AT_TP1", True)
    main.state_mgr = StateManager(tmp_path / "single_bank_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "single_bank_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    tps = derive_tp_levels([3845.0], direction="BUY")
    main.state_mgr.create_signal(
        message_id=8802,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3840.0, "tp": 3844.5, "ticket": 4101, "status": "OPEN"},
        },
        market_tickets=[4101],
        pending_tickets=[],
        raw_tp1=3845.0,
        tp_levels=tps,
        final_tp=3844.5,
        recorded_entry_price=3840.0,
    )

    with patch.object(main.executor, "get_tick", return_value=_tick(3845.10)):
        await _process_pre_tp1_or_backstop_tick("8802", main.state_mgr.get_signal(8802))

    sig = main.state_mgr.get_signal(8802)
    assert sig["status"] == "CLOSED"
    assert sig["market_tickets"] == []
    assert sig["runner_ticket"] is None
    assert sig["last_banked_tickets"] == [4101]


def test_tight_scalp_reversal_guard_arming():
    """
    Verify Gold Scalp Arming Calibration on short TP1 leg (<= $4.00):
    Signal: Entry 3842.0, TP1 3844.5 ($2.50 leg).
    Price 3844.06 gives $2.06 profit (82.4% progress) -> MUST ARM!
    Reversal from 3844.06 to 3842.80 gives $1.26 giveback (61.2% giveback >= 30% AND >= $1.125 floor) -> MUST TRIGGER!
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3842.0, "ticket": 5001, "status": "OPEN"},
        },
        "market_tickets": [5001],
        "runner_layers": ["L1"],
        "runner_ticket": 5001,
        "raw_tp1": 3844.5,
        "tp_levels": {"TP1": 3844.5, "TP2": 3855.0},
        "recorded_entry_price": 3842.0,
        "initial_price": 3842.0,
    }

    # At price 3844.06 (82.4% of leg, profit=$2.06 >= $1.50):
    t_peak = _tick(3844.06)
    res_arm = executor.evaluate_gold_reversal_guard(sig, t_peak)
    assert res_arm["armed"] is True
    assert sig.get("reversal_guard_armed") is True
    assert sig.get("peak_profit") == pytest.approx(2.06)

    # Reversal to 3842.80: giveback = $1.26 >= floor ($1.125) and >= 30% giveback -> TRIGGERS!
    t_pullback = _tick(3842.80)
    res_trigger = executor.evaluate_gold_reversal_guard(sig, t_pullback)
    assert res_trigger["armed"] is True
    assert res_trigger["triggered"] is True
    assert res_trigger["pullback_dollars"] == pytest.approx(1.26)


@pytest.mark.asyncio
async def test_telegram_bank_command_on_runner_active(tmp_path):
    """
    Verify manual /bank command when trade is already in RUNNER_ACTIVE mode:
    Must immediately close the runner, cancel pending orders, and bank profit.
    """
    main.state_mgr = StateManager(tmp_path / "cmd_bank_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "cmd_bank_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    main.state_mgr.create_signal(
        message_id=8803,
        symbol="XAUUSD.s",
        direction="BUY",
        status="RUNNER_ACTIVE",
        layers={
            "L4": {"lot": 0.02, "target_price": 3830.0, "ticket": 6004, "status": "OPEN"},
        },
        market_tickets=[6004],
        pending_tickets=[],
        runner_ticket=6004,
        runner_layers=["L4"],
        runner_layer="L4",
        raw_tp1=3850.0,
        recorded_entry_price=3830.0,
    )

    replies = []
    evt = MockTelethonEvent(msg_id=9901, text="/bank")
    evt.reply = MagicMock(side_effect=lambda text: (replies.append(text), asyncio.sleep(0))[1])

    import asyncio
    await handle_saved_messages_command(evt)

    sig = main.state_mgr.get_signal(8803)
    assert sig["status"] == "CLOSED"
    assert sig["market_tickets"] == []
    assert sig["runner_ticket"] is None
    assert sig["runner_close_reason"] == "Manual /bank on Runner"
    assert any("Runner Banked Manually" in r for r in replies)


@pytest.mark.asyncio
async def test_telegram_bank_tp1_toggle_command():
    """
    Verify /bank_tp1 on and /bank_tp1 off live toggle commands in chat.
    """
    replies = []
    import asyncio

    # Test status check
    evt_status = MockTelethonEvent(msg_id=9902, text="/bank_tp1")
    evt_status.reply = MagicMock(side_effect=lambda text: (replies.append(text), asyncio.sleep(0))[1])
    await handle_saved_messages_command(evt_status)
    assert any("Bank TP1 Feature Status" in r for r in replies)

    # Test /bank_tp1 on
    replies.clear()
    evt_on = MockTelethonEvent(msg_id=9903, text="/bank_tp1 on")
    evt_on.reply = MagicMock(side_effect=lambda text: (replies.append(text), asyncio.sleep(0))[1])
    await handle_saved_messages_command(evt_on)
    assert config.BANK_ALL_AT_TP1 is True
    assert any("ENABLED (100% Bank All)" in r for r in replies)

    # Test /bank_tp1 off
    replies.clear()
    evt_off = MockTelethonEvent(msg_id=9904, text="/bank_tp1 off")
    evt_off.reply = MagicMock(side_effect=lambda text: (replies.append(text), asyncio.sleep(0))[1])
    await handle_saved_messages_command(evt_off)
    assert config.BANK_ALL_AT_TP1 is False
    assert any("RUNNER MODE" in r for r in replies)


@pytest.mark.asyncio
async def test_50pct_loss_pause_closes_losing_trades_when_enabled(tmp_path, monkeypatch):
    """
    Verify capital preservation on 50% daily loss pause when DAILY_LOSS_CLOSE_OPEN_TRADES=True:
    Losing trades are cut immediately at 50% loss, preventing margin stop-out liquidation.
    """
    monkeypatch.setattr(config, "DAILY_LOSS_CLOSE_OPEN_TRADES", True)
    main.state_mgr = StateManager(tmp_path / "dd_50_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "dd_50_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 120.0
    main._daily_50pct_alert_date = None

    # Open signal with 1 market order + 1 pending limit
    main.state_mgr.create_signal(
        message_id=8805,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3840.0, "ticket": 7001, "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3835.0, "ticket": 7002, "status": "PENDING"},
        },
        market_tickets=[7001],
        pending_tickets=[7002],
        raw_tp1=3850.0,
    )

    alerts = []
    main.notify_saved_messages = AsyncMock(side_effect=lambda txt: alerts.append(txt))

    # Floating loss hits 50% (-£62.00)
    main.executor._dry_run_floating_pnl = -62.0
    st = await check_and_enforce_daily_drawdown()
    assert st == "PAUSED_50PCT"

    sig = main.state_mgr.get_signal(8805)
    # The losing trade was cut and pending limit cancelled to protect capital!
    assert sig["status"] == "CLOSED"
    assert sig["market_tickets"] == []
    assert sig["pending_tickets"] == []
    assert any("Capital Preservation Active" in a for a in alerts)
