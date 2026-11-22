"""Phase 1.4 - MT5 startup session preflight (read-only IPC, fully mocked).

Covers validate_mt5_session (connectivity / server / account / permissions / symbol)
and the main.py hook (_run_mt5_session_preflight + reconnect re-verification).
NO live MT5 calls: the executor's mt5 module is replaced with a controlled fake.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
import mt5_executor
import startup_checks
from mt5_executor import MT5Executor


def _fake_mt5(
    connected=True,
    trade_allowed=True,
    login=12345,
    server="Broker-Live",
    symbol_info=True,
    trade_mode=4,
    volume_min=0.01,
    volume_step=0.01,
    terminal_none=False,
    account_none=False,
):
    mt5 = MagicMock(name="fake_mt5")
    mt5.SYMBOL_TRADE_MODE_FULL = 4
    mt5.terminal_info.return_value = (
        None if terminal_none else SimpleNamespace(connected=connected, trade_allowed=trade_allowed)
    )
    mt5.account_info.return_value = (
        None if account_none else SimpleNamespace(login=login, server=server, balance=1000.0, currency="GBP")
    )
    mt5.symbol_info.return_value = (
        SimpleNamespace(
            volume_min=volume_min,
            volume_step=volume_step,
            volume_max=100.0,
            trade_mode=trade_mode,
            digits=2,
            point=0.01,
            visible=True,
        )
        if symbol_info
        else None
    )
    return mt5


def _cfg(**over):
    base = dict(DRY_RUN=False, MT5_LOGIN=12345, MT5_SERVER="Broker-Live", DEFAULT_SYMBOL="XAUUSD.s")
    base.update(over)
    return SimpleNamespace(**base)


def _executor(monkeypatch, fake):
    monkeypatch.setattr(mt5_executor, "mt5", fake)
    ex = MT5Executor(dry_run=False)
    ex._connected = True
    return ex


# ---------------------------------------------------------------------------
# validate_mt5_session - mock matrix
# ---------------------------------------------------------------------------

def test_ok_connected_matching_account_trade_allowed(monkeypatch):
    fake = _fake_mt5()
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is True
    assert problems == []


def test_wrong_login_hard_fail(monkeypatch):
    fake = _fake_mt5(login=99999)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg(MT5_LOGIN=12345))
    assert ok is False
    assert any("login mismatch" in p for p in problems)


def test_wrong_server_hard_fail(monkeypatch):
    fake = _fake_mt5(server="Other-Server")
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg(MT5_SERVER="Broker-Live"))
    assert ok is False
    assert any("server mismatch" in p for p in problems)


def test_trade_allowed_false_hard_fail(monkeypatch):
    fake = _fake_mt5(trade_allowed=False)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is False
    assert any("trade_allowed" in p for p in problems)


def test_terminal_disconnected_hard_fail(monkeypatch):
    fake = _fake_mt5(connected=False)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is False
    assert any("not connected" in p for p in problems)


def test_terminal_info_none_hard_fail(monkeypatch):
    fake = _fake_mt5(terminal_none=True)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is False
    assert any("terminal_info() unavailable" in p for p in problems)


def test_account_info_none_hard_fail(monkeypatch):
    fake = _fake_mt5(account_none=True)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is False
    assert any("no account" in p for p in problems)


def test_mt5_login_unset_live_hard_fail(monkeypatch):
    fake = _fake_mt5()
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg(MT5_LOGIN=None))
    assert ok is False
    assert any("identity unverified" in p for p in problems)


def test_mt5_login_unset_explicit_opt_in_ok(monkeypatch):
    fake = _fake_mt5()
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(
        ex, _cfg(MT5_LOGIN=None, MT5_PREFLIGHT_ALLOW_UNVERIFIED_LOGIN=True)
    )
    assert ok is True
    assert problems == []


def test_dry_run_cfg_bypasses_live_checks(monkeypatch):
    fake = _fake_mt5(connected=False, trade_allowed=False, login=1, server="Wrong")
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg(DRY_RUN=True))
    assert ok is True
    assert problems == []
    fake.terminal_info.assert_not_called()
    fake.account_info.assert_not_called()


def test_dry_run_executor_bypasses_live_checks(monkeypatch):
    fake = _fake_mt5(connected=False)
    monkeypatch.setattr(mt5_executor, "mt5", fake)
    ex = MT5Executor(dry_run=True)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg(DRY_RUN=False))
    assert ok is True
    assert problems == []
    fake.terminal_info.assert_not_called()


def test_symbol_trade_mode_not_full_warns_but_ok(monkeypatch, caplog):
    fake = _fake_mt5(trade_mode=0)
    ex = _executor(monkeypatch, fake)
    with caplog.at_level(logging.WARNING, logger="copier.startup"):
        ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is True
    assert problems == []
    assert any("not FULL" in r.message for r in caplog.records)


def test_symbol_missing_hard_fail(monkeypatch):
    fake = _fake_mt5(symbol_info=False)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is False
    assert any("not found on broker" in p for p in problems)


def test_symbol_insane_volume_hard_fail(monkeypatch):
    fake = _fake_mt5(volume_min=0.0, volume_step=0.0)
    ex = _executor(monkeypatch, fake)
    ok, problems = startup_checks.validate_mt5_session(ex, _cfg())
    assert ok is False
    assert any("volume_min" in p for p in problems)
    assert any("volume_step" in p for p in problems)


def test_no_login_or_order_send_ever_attempted(monkeypatch):
    fake = _fake_mt5(login=99999)
    ex = _executor(monkeypatch, fake)
    startup_checks.validate_mt5_session(ex, _cfg(MT5_LOGIN=12345))
    fake.login.assert_not_called()
    fake.initialize.assert_not_called()
    fake.order_send.assert_not_called()
    fake.symbol_select.assert_not_called()


# ---------------------------------------------------------------------------
# main.py hook - TRADING_HALTED on hard failure (notify mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hook_hard_failure_sets_trading_halted_and_alerts(monkeypatch):
    monkeypatch.setattr(main, "TRADING_HALTED", False)
    monkeypatch.setattr(main, "executor", MT5Executor(dry_run=False))
    cap = AsyncMock()
    monkeypatch.setattr(main, "notify_saved_messages", cap)
    monkeypatch.setattr(
        main, "validate_mt5_session", MagicMock(return_value=(False, ["MT5 account login mismatch: terminal=99999"]))
    )
    ok = await main._run_mt5_session_preflight()
    assert ok is False
    assert main.TRADING_HALTED is True
    assert main.session_trading_blocked() is True
    cap.assert_awaited_once()
    text = cap.await_args.args[0]
    assert "Trading HALTED" in text
    assert "login mismatch" in text


@pytest.mark.asyncio
async def test_hook_ok_does_not_halt(monkeypatch):
    monkeypatch.setattr(main, "TRADING_HALTED", False)
    monkeypatch.setattr(main, "executor", MT5Executor(dry_run=False))
    cap = AsyncMock()
    monkeypatch.setattr(main, "notify_saved_messages", cap)
    monkeypatch.setattr(main, "validate_mt5_session", MagicMock(return_value=(True, [])))
    ok = await main._run_mt5_session_preflight()
    assert ok is True
    assert main.TRADING_HALTED is False
    cap.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_reruns_preflight_and_halts_on_mismatch(monkeypatch):
    monkeypatch.setattr(main, "TRADING_HALTED", False)
    monkeypatch.setattr(main, "_mt5_connected_state", False)
    monkeypatch.setattr(main, "_mt5_last_disconnect_at", None)
    ex = MT5Executor(dry_run=False)
    ex._connected = True
    monkeypatch.setattr(ex, "is_connected", lambda: True)
    monkeypatch.setattr(main, "executor", ex)
    alerts = []

    async def _cap(txt):
        alerts.append(txt)

    monkeypatch.setattr(main, "notify_saved_messages", _cap)
    monkeypatch.setattr(
        main, "validate_mt5_session", MagicMock(return_value=(False, ["MT5 server mismatch"]))
    )
    await main._handle_mt5_connection_transition(True)
    assert main.TRADING_HALTED is True
    assert any("Connection Recovered" in a for a in alerts)
    assert any("Trading HALTED" in a for a in alerts)


@pytest.mark.asyncio
async def test_reconnect_skips_preflight_without_live_session(monkeypatch):
    monkeypatch.setattr(main, "TRADING_HALTED", False)
    monkeypatch.setattr(main, "_mt5_connected_state", False)
    monkeypatch.setattr(main, "_mt5_last_disconnect_at", None)
    ex = MT5Executor(dry_run=False)
    ex._connected = False
    monkeypatch.setattr(main, "executor", ex)
    alerts = []

    async def _cap(txt):
        alerts.append(txt)

    monkeypatch.setattr(main, "notify_saved_messages", _cap)
    spy = MagicMock(return_value=(False, ["should not run"]))
    monkeypatch.setattr(main, "validate_mt5_session", spy)
    await main._handle_mt5_connection_transition(True)
    spy.assert_not_called()
    assert main.TRADING_HALTED is False
    assert len(alerts) == 1
    assert "Connection Recovered" in alerts[0]
