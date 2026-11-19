"""
v3.4.5 Regression & Verification Tests:
1. Daily Loss Realized & Passed:
   - When all positions close after a loss, the loss is realized and has passed.
   - The baseline updates to current balance, unpauses 50% drawdown, and actions new trades.
   - /resume command in Saved Messages unpauses and resets baseline.
2. Max Lots & Dynamic Risk Guide Scaling:
   - £50   account -> High: 0.05 | Mid: 0.02 | Low: 0.01 -> [0.01, 0.01, 0.01, 0.02]
   - £100  account -> High: 0.10 | Mid: 0.05 | Low: 0.01 -> [0.02, 0.02, 0.02, 0.04]
   - £200  account -> High: 0.20 | Mid: 0.10 | Low: 0.02 -> [0.04, 0.04, 0.04, 0.08]
   - £500  account -> High: 0.50 | Mid: 0.25 | Low: 0.05 -> [0.10, 0.10, 0.10, 0.20]
   - £1000 account -> High: 1.00 | Mid: 0.50 | Low: 0.10 -> [0.20, 0.20, 0.20, 0.40]
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import config
import main
from main import StateManager, TradeLedger, check_and_enforce_daily_drawdown, handle_saved_messages_command
import mt5_executor
from mt5_executor import MT5Executor, calculate_risk_guide_thresholds, compute_dynamic_layer_lots_for_balance
from tests.test_copier_scenarios import MockTelethonEvent


# ---------------------------------------------------------------------------
# Test 1: User's exact Risk Guide Guideline for all account tiers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "balance, exp_high, exp_mid, exp_low, exp_layers",
    [
        (50.0, 0.05, 0.02, 0.01, [0.01, 0.01, 0.01, 0.02]),
        (100.0, 0.10, 0.05, 0.01, [0.02, 0.02, 0.02, 0.04]),
        (200.0, 0.20, 0.10, 0.02, [0.04, 0.04, 0.04, 0.08]),
        (500.0, 0.50, 0.25, 0.05, [0.10, 0.10, 0.10, 0.20]),
        (1000.0, 1.00, 0.50, 0.10, [0.20, 0.20, 0.20, 0.40]),
    ],
)
def test_user_risk_guide_guideline_tiers(balance, exp_high, exp_mid, exp_low, exp_layers):
    """
    Verify the user's exact account guideline:
    £50   -> High: 0.05 | Mid: 0.02 | Low: 0.01 -> [0.01, 0.01, 0.01, 0.02]
    £100  -> High: 0.10 | Mid: 0.05 | Low: 0.01 -> [0.02, 0.02, 0.02, 0.04]
    £200  -> High: 0.20 | Mid: 0.10 | Low: 0.02 -> [0.04, 0.04, 0.04, 0.08]
    £500  -> High: 0.50 | Mid: 0.25 | Low: 0.05 -> [0.10, 0.10, 0.10, 0.20]
    £1000 -> High: 1.00 | Mid: 0.50 | Low: 0.10 -> [0.20, 0.20, 0.20, 0.40]
    """
    thresh = calculate_risk_guide_thresholds(balance)
    assert thresh["high_risk_lots"] == exp_high
    assert thresh["mid_risk_lots"] == exp_mid
    assert thresh["low_risk_lots"] == exp_low

    dynamic_layers = compute_dynamic_layer_lots_for_balance(balance, risk_mode="high")
    assert dynamic_layers == exp_layers
    assert round(sum(dynamic_layers), 2) == exp_high


# ---------------------------------------------------------------------------
# Test 2: Live allocate_4layer_lots adjusts dynamically to live balance
# ---------------------------------------------------------------------------
def test_allocate_4layer_lots_adjusts_to_live_balance(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ADJUST_LOTS_TO_BALANCE", True)
    monkeypatch.setattr(config, "ENFORCE_HIGH_RISK_LOT_CAP", True)
    ex = MT5Executor(dry_run=True)
    ex.connect()

    # Case A: Live balance = £50 -> allocates [0.01, 0.01, 0.01, 0.02] (0.05 total)
    ex._dry_run_capital = 50.0
    ex._dry_run_ticket_lots = {}
    lots_50, warn_50 = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_50 == [0.01, 0.01, 0.01, 0.02]
    assert round(sum(lots_50), 2) == 0.05

    # Case B: Live balance = £100 -> allocates [0.02, 0.02, 0.02, 0.04] (0.10 total)
    ex._dry_run_capital = 100.0
    lots_100, warn_100 = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_100 == [0.02, 0.02, 0.02, 0.04]
    assert round(sum(lots_100), 2) == 0.10

    # Case C: Live balance = £200 -> allocates [0.04, 0.04, 0.04, 0.08] (0.20 total)
    ex._dry_run_capital = 200.0
    lots_200, warn_200 = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_200 == [0.04, 0.04, 0.04, 0.08]
    assert round(sum(lots_200), 2) == 0.20

    # Case D: Live balance = £500 -> allocates [0.10, 0.10, 0.10, 0.20] (0.50 total)
    ex._dry_run_capital = 500.0
    lots_500, warn_500 = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_500 == [0.10, 0.10, 0.10, 0.20]
    assert round(sum(lots_500), 2) == 0.50


# ---------------------------------------------------------------------------
# Test 3: Daily loss realized and passed unpauses and actions new trades
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_loss_realized_and_passed_unpauses(tmp_path, monkeypatch):
    """
    Simulate user's exact production incident:
    - Previous loss of -£154.78 occurred earlier in the day.
    - Account now has £55.26 balance, and 0 open positions.
    - Because the loss has passed and all positions are closed, the bot updates
      the baseline, unpauses 50% drawdown, and check_preflight allows new trades.
    """
    monkeypatch.setattr(config, "DAILY_LOSS_PAUSE_PCT", 50.0)
    monkeypatch.setattr(config, "EMERGENCY_CUTOFF_LOSS_PCT", 90.0)
    main.state_mgr = StateManager(tmp_path / "dd_passed_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "dd_passed_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 55.26
    main.executor._dry_run_daily_realized_pnl = -154.78
    main.executor._dry_run_floating_pnl = 0.0
    main.executor._dry_run_ticket_lots = {}

    # Initial check before baseline update would pause:
    dd_raw = main.executor.get_daily_drawdown_status()
    assert dd_raw["pause_new_signals"] is True

    # check_and_enforce_daily_drawdown detects 0 active signals & 0 open positions:
    # It recognizes the loss has passed, updates the baseline, and returns "OK"!
    st = await check_and_enforce_daily_drawdown()
    assert st == "OK"

    # Status is now unpaused:
    dd_after = main.executor.get_daily_drawdown_status()
    assert dd_after["pause_new_signals"] is False
    assert dd_after["total_daily_loss"] == 0.0
    assert dd_after["reference_balance"] == 55.26

    # check_preflight now passes cleanly to action new trades!
    ok, msg = main.executor.check_preflight("XAUUSD.s", "BUY", raw_tp1=3860.0, sl=3840.0)
    assert ok is True
    assert "Pre-flight OK" in msg


# ---------------------------------------------------------------------------
# Test 4: /resume command in Saved Messages resets daily loss baseline
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_saved_messages_resume_command(tmp_path, monkeypatch):
    """
    Verify user can type /resume in Saved Messages to unpause and reset baseline.
    """
    main.state_mgr = StateManager(tmp_path / "dd_resume_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "dd_resume_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 55.26
    main.executor._dry_run_daily_realized_pnl = -154.78
    main.executor._dry_run_floating_pnl = 0.0
    main.executor._dry_run_ticket_lots = {}
    main._daily_loss_locked = True
    main.TRADING_HALTED = True

    evt = MockTelethonEvent(msg_id=9999, text="/resume")
    await handle_saved_messages_command(evt)

    assert main._daily_loss_locked is False
    assert main.TRADING_HALTED is False

    # Check reply confirms reset
    replies = evt.replies
    assert len(replies) >= 1
    reply_text = replies[0]
    assert "Trading Resumed" in reply_text
    assert "55.26 GBP" in reply_text
    assert "0.0%" in reply_text

    # Drawdown status is unpaused
    dd = main.executor.get_daily_drawdown_status()
    assert dd["pause_new_signals"] is False
