"""
Comprehensive regression & integration tests for unlimited defaults (all loss/lot caps OFF
by default) and runtime manual Saved Messages controls (/floatcap, /losslimit, /lotcap,
dailyloss, dailywin).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import config
import main
import risk_controls
import startup_checks
from mt5_executor import MT5Executor
from state_manager import StateManager
from tests.test_copier_scenarios import MockTelethonEvent
from trade_ledger import TradeLedger


def _reset_main_for_test(tmp_path, tag: str, balance: float = 100.0, credit: float = 0.0) -> MT5Executor:
    """Create isolated StateManager, TradeLedger, and dry-run MT5Executor on main."""
    main.state_mgr = StateManager(tmp_path / f"{tag}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{tag}_ledger.jsonl")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = balance
    ex._dry_run_daily_realized_pnl = 0.0
    ex._dry_run_floating_pnl = 0.0
    ex._dry_run_ticket_lots = {}
    ex._dry_run_pending_ticket_lots = {}
    main.executor = ex
    main._daily_50pct_alert_date = None
    main._daily_float_cap_alert_date = None
    main._daily_loss_locked = False
    main._daily_loss_terminate_requested = False
    main._daily_win_terminate_requested = False
    main._awaiting_dailywin_input = False
    main._awaiting_dailyloss_input = False
    main.TRADING_HALTED = False
    return ex


def _pin_unlimited_defaults(monkeypatch) -> None:
    """Restore default unlimited values on config after any runtime toggle in a test."""
    monkeypatch.setattr(config, "ENFORCE_HIGH_RISK_LOT_CAP", False)
    monkeypatch.setattr(config, "DAILY_LOSS_PAUSE_PCT", 0.0)
    monkeypatch.setattr(config, "EMERGENCY_CUTOFF_LOSS_PCT", 0.0)
    monkeypatch.setattr(config, "FLOAT_LOSS_FORCE_CLOSE_PCT", 0.0)
    monkeypatch.setattr(config, "DAILY_LOSS_CLOSE_OPEN_TRADES", False)
    monkeypatch.setattr(config, "AUTO_ADJUST_LOTS_TO_BALANCE", False)


# ---------------------------------------------------------------------------
# 1. Default Config Values (All Caps OFF / Unlimited)
# ---------------------------------------------------------------------------

def test_default_config_values_are_all_unlimited_off():
    assert config.ENFORCE_HIGH_RISK_LOT_CAP is False
    assert config.DAILY_LOSS_PAUSE_PCT == 0.0
    assert config.EMERGENCY_CUTOFF_LOSS_PCT == 0.0
    assert config.FLOAT_LOSS_FORCE_CLOSE_PCT == 0.0
    assert config.DAILY_LOSS_CLOSE_OPEN_TRADES is False
    assert config.AUTO_ADJUST_LOTS_TO_BALANCE is False

    snap = risk_controls.read_risk_limits(config)
    assert snap == risk_controls.RiskLimitsSnapshot(
        lot_cap_enabled=False,
        daily_loss_pause_pct=0.0,
        emergency_cutoff_pct=0.0,
        float_loss_cap_pct=0.0,
    )


# ---------------------------------------------------------------------------
# 2. Extreme Loss (-95% Realized & -95% Floating) Never Triggers With Defaults
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extreme_loss_never_triggers_or_closes_with_unlimited_defaults(tmp_path, monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    ex = _reset_main_for_test(tmp_path, "extreme_loss", balance=100.0)

    # Zero-loss edge case must never false-trigger (guard against 0.0 >= 0.0 - 1e-9)
    dd_zero = ex.get_daily_drawdown_status()
    assert dd_zero["pause_new_signals"] is False
    assert dd_zero["emergency_cutoff_triggered"] is False
    assert dd_zero["float_force_close_triggered"] is False
    assert dd_zero["pause_limit_amount"] == 0.0
    assert dd_zero["emergency_cutoff_amount"] == 0.0
    assert dd_zero["float_cap_limit"] == 0.0

    # Seed an active signal with 2 open market orders and 2 pending limits
    main.state_mgr.create_signal(
        message_id=50101,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3895.0, "ticket": 1001, "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3892.0, "ticket": 1002, "status": "OPEN"},
            "L3": {"lot": 0.01, "target_price": 3889.0, "ticket": 1003, "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3886.0, "ticket": 1004, "status": "PENDING"},
        },
        market_tickets=[1001, 1002],
        pending_tickets=[1003, 1004],
        raw_tp1=3905.0,
    )
    ex._dry_run_ticket_lots = {1001: 0.01, 1002: 0.01}
    ex._dry_run_pending_ticket_lots = {1003: 0.01, 1004: 0.02}

    # Extreme -95% realized AND -95% floating drawdown
    ex._dry_run_daily_realized_pnl = -95.0
    ex._dry_run_floating_pnl = -95.0

    dd = ex.get_daily_drawdown_status()
    assert dd["pause_pct"] == 0.0
    assert dd["cutoff_pct"] == 0.0
    assert dd["float_cap_pct"] == 0.0
    assert dd["pause_new_signals"] is False
    assert dd["emergency_cutoff_triggered"] is False
    assert dd["float_force_close_triggered"] is False

    ok, preflight_msg = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3905.0, sl=3875.0)
    assert ok is True
    assert "Pre-flight OK" in preflight_msg

    alerts = []
    monkeypatch.setattr(main, "notify_saved_messages", AsyncMock(side_effect=lambda txt: alerts.append(txt)))

    status = await main.check_and_enforce_daily_drawdown()
    assert status == "OK"
    assert alerts == []

    sig_after = main.state_mgr.get_signal(50101)
    assert sig_after["status"] == "ACTIVE"
    assert sig_after["market_tickets"] == [1001, 1002]
    assert sig_after["pending_tickets"] == [1003, 1004]


# ---------------------------------------------------------------------------
# 3. Heavy Open Lots Uncapped With Default Config
# ---------------------------------------------------------------------------

def test_heavy_open_lots_uncapped_with_default_config(monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 20.0
    ex._dry_run_ticket_lots = {701: 0.50}  # 25x over a £20 account's 0.02 High Risk guide!

    allowed_lot, reason = ex.check_and_cap_order_lot(0.50, symbol="XAUUSD.s")
    assert allowed_lot == 0.50
    assert reason is None

    lots, warn = ex.allocate_4layer_lots("XAUUSD.s", base_lots=[0.10, 0.10, 0.10, 0.20])
    assert lots == [0.10, 0.10, 0.10, 0.20]
    assert warn is None
    assert ex.last_allocation_warning is None

    # Risk threshold alerts stay silent when lot cap is OFF
    assert ex.evaluate_risk_threshold_alerts(0.0, 0.50) == []

    # Full 4-layer market entry executes uncapped
    mk, pd, layers = ex.execute_4layer_market("XAUUSD.s", "BUY", sl=3875.0, final_tp=3904.5, raw_tp1=3905.0)
    assert len(mk) == 4
    assert pd == []
    assert [layers[k]["lot"] for k in ("L1", "L2", "L3", "L4")] == [0.01, 0.01, 0.01, 0.02]


# ---------------------------------------------------------------------------
# 4. Startup Validation Passes With Default 0.0 Limits & Rejects Invalid Cases
# ---------------------------------------------------------------------------

def test_startup_checks_allows_zero_limits_and_validates_ordering(tmp_path):
    # Default config with 0.0 limits passes cleanly
    res_default = startup_checks.validate_environment(check_packages=False)
    assert res_default["ok"] is True
    assert res_default["errors"] == []

    def _make_cfg(**overrides):
        base = {k: getattr(config, k) for k in dir(config) if k.isupper()}
        base.update(
            LOG_FILE_PATH=tmp_path / "copier.log",
            STATE_FILE_PATH=tmp_path / "state.json",
            TRADES_LEDGER_PATH=tmp_path / "ledger.jsonl",
            DAILY_LOSS_PAUSE_PCT=0.0,
            EMERGENCY_CUTOFF_LOSS_PCT=0.0,
            FLOAT_LOSS_FORCE_CLOSE_PCT=0.0,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    # Single-enabled pause or cutoff passes
    assert startup_checks.validate_environment(
        check_packages=False, cfg=_make_cfg(DAILY_LOSS_PAUSE_PCT=50.0, EMERGENCY_CUTOFF_LOSS_PCT=0.0)
    )["ok"] is True
    assert startup_checks.validate_environment(
        check_packages=False, cfg=_make_cfg(DAILY_LOSS_PAUSE_PCT=0.0, EMERGENCY_CUTOFF_LOSS_PCT=90.0)
    )["ok"] is True

    # Invalid pause >= cutoff when both > 0 still fails
    res_order = startup_checks.validate_environment(
        check_packages=False, cfg=_make_cfg(DAILY_LOSS_PAUSE_PCT=90.0, EMERGENCY_CUTOFF_LOSS_PCT=50.0)
    )
    assert res_order["ok"] is False
    assert any("DAILY_LOSS_PAUSE_PCT" in e for e in res_order["errors"])

    # Out-of-range float cap fails
    res_bad_fc = startup_checks.validate_environment(
        check_packages=False, cfg=_make_cfg(FLOAT_LOSS_FORCE_CLOSE_PCT=150.0)
    )
    assert res_bad_fc["ok"] is False
    assert any("FLOAT_LOSS_FORCE_CLOSE_PCT" in e for e in res_bad_fc["errors"])


# ---------------------------------------------------------------------------
# 5. Saved Messages Manual Controls (/floatcap, /losslimit, /lotcap, dailyloss)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_saved_messages_floatcap_toggle_on_and_off(tmp_path, monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    ex = _reset_main_for_test(tmp_path, "floatcap_cmd", balance=200.0)
    monkeypatch.setattr(
        ex,
        "get_account_financials",
        lambda: {"balance": 200.0, "credit": 0.0, "equity": 200.0, "profit": 0.0, "currency": "GBP", "formatted": "Bal: 200"},
    )
    monkeypatch.setattr(main, "notify_saved_messages", AsyncMock())

    # Query status while OFF
    evt_q = MockTelethonEvent(msg_id=60001, text="/floatcap", chat_id="me")
    await main.handle_saved_messages_command(evt_q)
    assert any("Unlimited (OFF)" in r for r in evt_q.replies)

    # Enable at 60%
    evt_on = MockTelethonEvent(msg_id=60002, text="/floatcap 60", chat_id="me")
    await main.handle_saved_messages_command(evt_on)
    assert config.FLOAT_LOSS_FORCE_CLOSE_PCT == 60.0
    assert any("60%" in r and "-120.00 GBP" in r for r in evt_on.replies)

    # Floating loss at -60% (-£120 on £200) triggers float_force_close
    ex._dry_run_floating_pnl = -120.0
    dd_on = ex.get_daily_drawdown_status()
    assert dd_on["float_force_close_triggered"] is True
    assert await main.check_and_enforce_daily_drawdown() == "FLOAT_FORCE_CLOSE"

    # Turn back OFF
    evt_off = MockTelethonEvent(msg_id=60003, text="/floatcap off", chat_id="me")
    await main.handle_saved_messages_command(evt_off)
    assert config.FLOAT_LOSS_FORCE_CLOSE_PCT == 0.0
    assert any("Unlimited (OFF)" in r for r in evt_off.replies)

    dd_off = ex.get_daily_drawdown_status()
    assert dd_off["float_force_close_triggered"] is False
    assert await main.check_and_enforce_daily_drawdown() == "OK"

    # Invalid input rejected without mutating config
    evt_bad = MockTelethonEvent(msg_id=60004, text="/floatcap 150", chat_id="me")
    await main.handle_saved_messages_command(evt_bad)
    assert config.FLOAT_LOSS_FORCE_CLOSE_PCT == 0.0
    assert any("Invalid value" in r for r in evt_bad.replies)


@pytest.mark.asyncio
async def test_saved_messages_losslimit_toggle_on_and_off(tmp_path, monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    ex = _reset_main_for_test(tmp_path, "losslimit_cmd", balance=100.0)
    monkeypatch.setattr(
        ex,
        "get_account_financials",
        lambda: {"balance": 100.0, "credit": 0.0, "equity": 100.0, "profit": 0.0, "currency": "GBP", "formatted": "Bal: 100"},
    )

    # Query status while OFF
    evt_q = MockTelethonEvent(msg_id=60101, text="/losslimit", chat_id="me")
    await main.handle_saved_messages_command(evt_q)
    assert any("Unlimited (OFF)" in r for r in evt_q.replies)

    # Enable 50% pause and 90% cutoff
    evt_on = MockTelethonEvent(msg_id=60102, text="/losslimit 50 90", chat_id="me")
    await main.handle_saved_messages_command(evt_on)
    assert config.DAILY_LOSS_PAUSE_PCT == 50.0
    assert config.EMERGENCY_CUTOFF_LOSS_PCT == 90.0
    assert any("50%" in r and "90%" in r for r in evt_on.replies)

    # At -55% loss: pause triggers, emergency cutoff does not, preflight blocks
    ex._dry_run_floating_pnl = -55.0
    dd_55 = ex.get_daily_drawdown_status()
    assert dd_55["pause_new_signals"] is True
    assert dd_55["emergency_cutoff_triggered"] is False
    ok_pre, msg_pre = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3905.0, sl=3875.0)
    assert ok_pre is False
    assert "50% Daily Loss Limit Reached" in msg_pre

    # At -92% loss: emergency cutoff triggers
    ex._dry_run_floating_pnl = -92.0
    dd_92 = ex.get_daily_drawdown_status()
    assert dd_92["emergency_cutoff_triggered"] is True

    # Invalid ordering (cutoff <= pause) rejected without mutating live limits
    evt_bad = MockTelethonEvent(msg_id=60103, text="/losslimit 90 50", chat_id="me")
    await main.handle_saved_messages_command(evt_bad)
    assert config.DAILY_LOSS_PAUSE_PCT == 50.0
    assert config.EMERGENCY_CUTOFF_LOSS_PCT == 90.0
    assert any("Invalid value" in r for r in evt_bad.replies)

    # Disable both via `/losslimit off`
    evt_off = MockTelethonEvent(msg_id=60104, text="/losslimit off", chat_id="me")
    await main.handle_saved_messages_command(evt_off)
    assert config.DAILY_LOSS_PAUSE_PCT == 0.0
    assert config.EMERGENCY_CUTOFF_LOSS_PCT == 0.0
    assert any("Unlimited (OFF)" in r for r in evt_off.replies)

    dd_off = ex.get_daily_drawdown_status()
    assert dd_off["pause_new_signals"] is False
    assert dd_off["emergency_cutoff_triggered"] is False
    ok_after, _ = ex.check_preflight("XAUUSD.s", "BUY", raw_tp1=3905.0, sl=3875.0)
    assert ok_after is True


@pytest.mark.asyncio
async def test_saved_messages_lotcap_toggle_on_and_off(tmp_path, monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    ex = _reset_main_for_test(tmp_path, "lotcap_cmd", balance=50.0)
    ex._dry_run_ticket_lots = {801: 0.05}  # At £50 High Risk cap (0.05 lots)

    # Initially OFF: uncapped
    assert ex.check_and_cap_order_lot(0.05, symbol="XAUUSD.s") == (0.05, None)

    evt_q = MockTelethonEvent(msg_id=60201, text="/lotcap", chat_id="me")
    await main.handle_saved_messages_command(evt_q)
    assert any("Unlimited (OFF)" in r for r in evt_q.replies)

    # Enable via `/lotcap on` -> blocks excess lots
    evt_on = MockTelethonEvent(msg_id=60202, text="/lotcap on", chat_id="me")
    await main.handle_saved_messages_command(evt_on)
    assert config.ENFORCE_HIGH_RISK_LOT_CAP is True
    assert any("Enabled → `ON`" in r for r in evt_on.replies)

    lot_blocked, err_reason = ex.check_and_cap_order_lot(0.01, symbol="XAUUSD.s")
    assert lot_blocked == 0.0
    assert err_reason is not None and "High Risk Lot Cap Reached" in err_reason

    lots_alloc, alloc_warn = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_alloc == [0.0, 0.0, 0.0, 0.0]
    assert alloc_warn is not None and "High Risk Lot Cap Reached" in alloc_warn

    # Disable via `/lotcap off` -> returns to unlimited
    evt_off = MockTelethonEvent(msg_id=60203, text="/lotcap off", chat_id="me")
    await main.handle_saved_messages_command(evt_off)
    assert config.ENFORCE_HIGH_RISK_LOT_CAP is False
    assert any("Unlimited (OFF)" in r for r in evt_off.replies)

    assert ex.check_and_cap_order_lot(0.50, symbol="XAUUSD.s") == (0.50, None)
    assert ex.allocate_4layer_lots("XAUUSD.s") == ([0.01, 0.01, 0.01, 0.02], None)


@pytest.mark.asyncio
async def test_saved_messages_dailyloss_manual_toggle_still_works(tmp_path, monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    _reset_main_for_test(tmp_path, "dailyloss_cmd", balance=100.0)

    # Enable currency daily realized loss cap: `dailyloss 25`
    evt_on = MockTelethonEvent(msg_id=60301, text="dailyloss 25", chat_id="me")
    await main.handle_saved_messages_command(evt_on)
    dl_cfg = main.state_mgr.get_daily_loss_config()
    assert dl_cfg["enabled"] is True
    assert dl_cfg["limit_amount"] == 25.0
    assert any("Daily Loss Limit Activated" in r and "25.00" in r for r in evt_on.replies)

    # Disable via `dailyloss off`
    evt_off = MockTelethonEvent(msg_id=60302, text="dailyloss off", chat_id="me")
    await main.handle_saved_messages_command(evt_off)
    dl_cfg_off = main.state_mgr.get_daily_loss_config()
    assert dl_cfg_off["enabled"] is False
    assert dl_cfg_off["limit_amount"] == 0.0
    assert any("Daily Loss Limit Disabled" in r and "Unlimited" in r for r in evt_off.replies)


@pytest.mark.asyncio
async def test_status_health_and_risk_controls_formatting(tmp_path, monkeypatch):
    _pin_unlimited_defaults(monkeypatch)
    _reset_main_for_test(tmp_path, "status_cmd", balance=100.0)

    # /status when all caps OFF shows Unlimited (OFF) and lists new commands
    evt_status = MockTelethonEvent(msg_id=60401, text="/status", chat_id="me")
    await main.handle_saved_messages_command(evt_status)
    status_txt = evt_status.replies[0]
    assert "Unlimited (OFF)" in status_txt
    assert "/lotcap" in status_txt
    assert "/losslimit" in status_txt
    assert "/floatcap" in status_txt
    assert "dailyloss" in status_txt

    # /health when all caps OFF shows Unlimited (OFF)
    evt_health = MockTelethonEvent(msg_id=60402, text="/health", chat_id="me")
    await main.handle_saved_messages_command(evt_health)
    health_txt = evt_health.replies[0]
    assert "Unlimited (OFF)" in health_txt
    assert "/lotcap" in health_txt
    assert "/losslimit" in health_txt
    assert "/floatcap" in health_txt

    # Direct unit test of risk_controls helpers (1-arg /losslimit, off tokens, format_limits_status_lines)
    fake_cfg = SimpleNamespace(
        ENFORCE_HIGH_RISK_LOT_CAP=False,
        DAILY_LOSS_PAUSE_PCT=0.0,
        EMERGENCY_CUTOFF_LOSS_PCT=0.0,
        FLOAT_LOSS_FORCE_CLOSE_PCT=0.0,
    )
    changed, _ = risk_controls.apply_loss_limit_command(fake_cfg, ["50", "90"], 200.0, "GBP")
    assert changed is True
    assert fake_cfg.DAILY_LOSS_PAUSE_PCT == 50.0
    assert fake_cfg.EMERGENCY_CUTOFF_LOSS_PCT == 90.0

    # 1-arg update where new pause >= old cutoff resets cutoff to 0.0
    changed2, _ = risk_controls.apply_loss_limit_command(fake_cfg, ["95"], 200.0, "GBP")
    assert changed2 is True
    assert fake_cfg.DAILY_LOSS_PAUSE_PCT == 95.0
    assert fake_cfg.EMERGENCY_CUTOFF_LOSS_PCT == 0.0

    # format_limits_status_lines reflects active limits cleanly
    fake_cfg.ENFORCE_HIGH_RISK_LOT_CAP = True
    fake_cfg.FLOAT_LOSS_FORCE_CLOSE_PCT = 60.0
    lines = risk_controls.format_limits_status_lines(
        fake_cfg,
        {
            "currency": "GBP",
            "total_daily_loss": 10.0,
            "loss_pct": 5.0,
            "pause_limit_amount": 190.0,
            "emergency_cutoff_amount": 0.0,
            "floating_loss": 10.0,
            "float_cap_limit": 120.0,
            "pause_new_signals": False,
            "emergency_cutoff_triggered": False,
            "float_force_close_triggered": False,
        },
        {
            "open_lots": 0.02,
            "pending_lots": 0.04,
            "low_risk_lots": 0.02,
            "mid_risk_lots": 0.10,
            "high_risk_lots": 0.20,
        },
    )
    assert "0.02 / 0.20 Max" in lines["active_lots_label"]
    assert "95% Pause" in lines["drawdown_guard_line"]
    assert "Cut-Off: `OFF`" in lines["drawdown_guard_line"]
    assert "60%" in lines["float_cap_line"]


def test_render_terminal_status_reflects_lot_cap_mode(monkeypatch, caplog):
    _pin_unlimited_defaults(monkeypatch)
    monkeypatch.setattr(main, "_last_terminal_snapshot", None)
    monkeypatch.setattr(main, "_last_hud_state", None)
    st = {
        "balance": 20.0,
        "credit": 0.0,
        "equity": 20.0,
        "profit": 0.0,
        "currency": "GBP",
        "open_lots": 0.05,
        "pending_lots": 0.04,
        "low_risk_lots": 0.01,
        "mid_risk_lots": 0.01,
        "high_risk_lots": 0.02,
    }

    with caplog.at_level("INFO", logger="copier"):
        main._render_terminal_status(st, force_print=True)
    assert "Active Lots: 0.05 (Unlimited)" in caplog.text
    assert "0.05 (Unlim)" in main._current_hud_line

    caplog.clear()
    monkeypatch.setattr(config, "ENFORCE_HIGH_RISK_LOT_CAP", True)
    with caplog.at_level("INFO", logger="copier"):
        main._render_terminal_status(st, force_print=True)
    assert "Active Lots: 0.05/0.02 Max" in caplog.text
    assert "0.05/0.02" in main._current_hud_line

