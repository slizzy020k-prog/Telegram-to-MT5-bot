import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import MetaTrader5 as mt5
import pytest

import config
import main
import mt5_executor as ex_mod
import startup_checks
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger


def _make_event(
    msg_id: int,
    text: str,
    reply_to_msg_id=None,
    chat_id=-1001234567890,
    msg_date=None,
):
    evt = MagicMock()
    evt.chat_id = chat_id
    evt.is_reply = reply_to_msg_id is not None
    evt.replies = []
    evt.message = MagicMock()
    evt.message.id = msg_id
    evt.message.message = text
    evt.message.reply_to_msg_id = reply_to_msg_id
    evt.message.reply_to = None
    evt.message.forward = None
    evt.message.out = False
    evt.message.date = msg_date or datetime.now(timezone.utc)

    async def _reply(msg_txt: str):
        evt.replies.append(msg_txt)

    evt.reply = _reply
    return evt


def _reset_runtime(tmp_path, name: str = "v3_ops"):
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main._daily_loss_lock_notified_date = None
    main._awaiting_dailywin_input = False
    main._awaiting_dailyloss_input = False
    main._daily_50pct_alert_date = None
    main._mt5_connected_state = None
    main._mt5_last_disconnect_at = None
    main._telegram_reconnect_count = 0
    main.state_mgr = StateManager(tmp_path / f"{name}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{name}_ledger.jsonl")
    config.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    ex_mod.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    main.SINGLE_PRICE_CONSOLIDATE_ORDERS = False


# ---------------------------------------------------------------------------
# 1. ISOLATION SAFETY & NON-BLOCKING ASYNCIO OFFLOAD
# ---------------------------------------------------------------------------

def test_pytest_isolation_safety():
    """Pytest MUST enforce DRY_RUN=True and redirect log/state/ledger/lock away from live files."""
    assert config.DRY_RUN is True
    live_dir = config.BASE_DIR.resolve()
    assert Path(config.LOG_FILE_PATH).resolve() != (live_dir / "copier.log")
    assert Path(config.STATE_FILE_PATH).resolve() != (live_dir / "signals_state.json")
    assert Path(config.TRADES_LEDGER_PATH).resolve() != (live_dir / "trades_ledger.jsonl")
    assert Path(config.LOCK_FILE_PATH).resolve() != (live_dir / "copier.lock")


@pytest.mark.asyncio
async def test_mt5_call_offloads_to_worker_thread_without_blocking_loop():
    main_tid = threading.get_ident()
    seen_tids = []
    loop_ticks = []

    def _blocking_ipc(val: int) -> int:
        seen_tids.append(threading.get_ident())
        time.sleep(0.05)
        return val * 2

    async def _ticker():
        for i in range(3):
            loop_ticks.append(i)
            await asyncio.sleep(0.01)

    res, _ = await asyncio.gather(main._mt5_call(_blocking_ipc, 21), _ticker())
    assert res == 42
    assert len(seen_tids) == 1
    assert seen_tids[0] != main_tid
    assert len(loop_ticks) == 3


@pytest.mark.asyncio
async def test_duplicate_message_and_concurrent_edit_lock_serialization(tmp_path):
    """Per-(chat_id, msg_id) asyncio.Lock prevents duplicate orders during concurrent message/edit arrival."""
    _reset_runtime(tmp_path, "lock_race")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_staged = main.STAGED_MARKET_ENTRY
    main.SELL_NOW_DELAY_SECONDS = 0.0
    main.STAGED_MARKET_ENTRY = False  # test expects 4 market layers at once
    call_count = {"n": 0}
    orig_exec_market = main.executor.execute_4layer_market

    def _slow_market(*args, **kwargs):
        call_count["n"] += 1
        time.sleep(0.04)
        return orig_exec_market(*args, **kwargs)

    try:
        with patch.object(main.executor, "execute_4layer_market", side_effect=_slow_market):
            evt1 = _make_event(7101, "Buy now\nTP 3905\nSL 3880")
            evt1_dup = _make_event(7101, "Buy now\nTP 3905\nSL 3880")
            evt1_edit = _make_event(7101, "Buy now\nTP 3908\nSL 3875")

            await asyncio.gather(
                main.handle_new_message(evt1),
                main.handle_new_message(evt1_dup),
                main.handle_message_edited(evt1_edit),
            )

        assert call_count["n"] == 1
        sig = main.state_mgr.get_signal(7101, chat_id=-1001234567890)
        assert sig is not None
        assert len(sig["market_tickets"]) == 4
        assert sig["sl"] == 3875.0
        assert sig["raw_tp1"] == 3908.0
        assert all(sig["layers"][k]["tp"] == 3907.50 for k in ("L1", "L2", "L3", "L4"))
        assert all(sig["layers"][k]["tp_stage"] == "TP1" for k in ("L1", "L2", "L3", "L4"))
        assert sig["tp_levels"]["TP5"] == 3948.0

        # Verify #34 fix: edit on a signal with raw_text=None never raises AttributeError
        main.state_mgr.update_signal(7101, {"raw_text": None}, chat_id=-1001234567890)
        await main.handle_message_edited(_make_event(7101, "Buy now\nTP 3910\nSL 3876"))
        sig2 = main.state_mgr.get_signal(7101, chat_id=-1001234567890)
        assert sig2["sl"] == 3876.0
        assert sig2["raw_tp1"] == 3910.0
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        main.STAGED_MARKET_ENTRY = orig_staged


# ---------------------------------------------------------------------------
# 2. STARTUP PREFLIGHT CHECKS & SINGLE-INSTANCE MUTEX CONTENTION
# ---------------------------------------------------------------------------

def test_startup_checks_validation_and_single_instance_lock_contention(tmp_path):
    # 1. Valid environment passes
    res_ok = startup_checks.validate_environment(check_packages=True)
    assert res_ok["ok"] is True
    assert res_ok["errors"] == []

    # 2. Invalid config invariants fail cleanly
    bad_cfg = SimpleNamespace(
        TELEGRAM_API_ID=123456,
        TELEGRAM_API_HASH="abc123",
        CHANNEL_IDS=[],
        BASE_LAYER_LOTS=[0.01, -0.02],
        RISK_LOT_MULTIPLIER=0.0,
        LAYER_LOTS={},
        MAX_ORDERS_PER_SIGNAL=0,
        BE_LAYERS=["L1"],
        DEFAULT_SYMBOL="",
        RUNNER_PROXIMITY_MODE="BOGUS",
        FALLBACK_SL_PIPS=500.0,
        MAX_SL_PIPS=300.0,
        DAILY_LOSS_PAUSE_PCT=90.0,
        EMERGENCY_CUTOFF_LOSS_PCT=50.0,
        LOG_FILE_PATH=tmp_path,  # directory instead of file!
        STATE_FILE_PATH=tmp_path / "ok_state.json",
        TRADES_LEDGER_PATH=tmp_path / "ok_ledger.jsonl",
    )
    res_bad = startup_checks.validate_environment(check_packages=False, cfg=bad_cfg)
    assert res_bad["ok"] is False
    assert any("CHANNEL_IDS" in e for e in res_bad["errors"])
    assert any("BASE_LAYER_LOTS" in e for e in res_bad["errors"])
    assert any("FALLBACK_SL_PIPS" in e for e in res_bad["errors"])
    assert any("DAILY_LOSS_PAUSE_PCT" in e for e in res_bad["errors"])
    assert any("directory" in e for e in res_bad["errors"])

    # 3. Same-process & cross-process lock contention
    lock_file = tmp_path / "instance_test.lock"
    h1 = startup_checks.acquire_single_instance_lock(lock_file)
    assert h1 is not None and h1.acquired is True

    # Same-process duplicate fails
    assert startup_checks.acquire_single_instance_lock(lock_file) is None

    # Cross-process duplicate fails
    probe_code = (
        "import sys; from startup_checks import acquire_single_instance_lock; "
        f"h = acquire_single_instance_lock(r'{lock_file}'); "
        "sys.exit(90 if h is None else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(config.BASE_DIR),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 90

    # Release and re-acquire cleanly
    startup_checks.release_single_instance_lock(h1)
    assert h1.acquired is False
    h2 = startup_checks.acquire_single_instance_lock(lock_file)
    assert h2 is not None
    startup_checks.release_single_instance_lock(h2)


@pytest.mark.asyncio
async def test_main_exits_90_on_duplicate_instance_lock(tmp_path):
    _reset_runtime(tmp_path, "dup_exit")
    with patch.object(main, "validate_environment", return_value={"ok": True, "errors": [], "warnings": []}), \
         patch.object(main, "acquire_single_instance_lock", return_value=None):
        with pytest.raises(SystemExit) as exc:
            await main.main()
        assert exc.value.code == startup_checks.DUPLICATE_INSTANCE_EXIT_CODE == 90


# ---------------------------------------------------------------------------
# 3. BOUNDED RECONNECT CATCH-UP + MT5 DISCONNECT/RECOVERY ALERTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_reconnect_catchup_dedup_edits_and_stale_guards(tmp_path, monkeypatch):
    _reset_runtime(tmp_path, "catchup")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    chat_id = -100777888
    monkeypatch.setattr(main, "CHANNEL_IDS", [chat_id])
    monkeypatch.setattr(main, "CATCHUP_WINDOW_SECONDS", 1800)
    monkeypatch.setattr(main, "CATCHUP_MAX_MESSAGES", 25)
    monkeypatch.setattr(main, "CATCHUP_MARKET_MAX_AGE_SECONDS", 60)
    monkeypatch.setattr(main, "CATCHUP_MARKET_MAX_SLIPPAGE_PIPS", 15.0)

    # Pre-seed watermark at msg_id=100 and an existing signal at 100
    main.state_mgr.create_signal(
        message_id=100,
        chat_id=chat_id,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={"L1": {"lot": 0.01, "ticket": 9001, "status": "OPEN"}},
        market_tickets=[9001],
        pending_tickets=[],
        sl=3875.0,
        raw_tp1=3900.0,
        final_tp=3899.5,
        raw_text="Buy now\nTP 3900\nSL 3875",
    )
    main.state_mgr.set_catchup_watermark(chat_id, 100)

    now_utc = datetime.now(timezone.utc)
    fake_msgs = [
        # 1. Edit on existing signal 100 -> must replay edit (TP -> 3905, SL -> 3872)
        SimpleNamespace(id=100, message="Buy now\nTP 3905\nSL 3872", date=now_utc - timedelta(seconds=10), reply_to_msg_id=None, reply_to=None, forward=None),
        # 2. Below watermark (id=99) -> must be ignored
        SimpleNamespace(id=99, message="Buy limit 3880\nTP 3900\nSL 3865", date=now_utc - timedelta(seconds=5), reply_to_msg_id=None, reply_to=None, forward=None),
        # 3. Outside CATCHUP_WINDOW_SECONDS (age=3600s) -> skipped
        SimpleNamespace(id=101, message="Buy limit 3882\nTP 3900\nSL 3865", date=now_utc - timedelta(seconds=3600), reply_to_msg_id=None, reply_to=None, forward=None),
        # 4. Stale "Buy now" (age=120s > 60s) -> rejected stale
        SimpleNamespace(id=102, message="Buy now\nTP 3905\nSL 3880", date=now_utc - timedelta(seconds=120), reply_to_msg_id=None, reply_to=None, forward=None),
        # 5. Slipped BUY LIMIT (price=3899 vs live ask=3895 -> 40 pips > 15 pips max slippage) -> rejected stale
        SimpleNamespace(id=103, message="Buy limit 3899\nTP 3910\nSL 3880", date=now_utc - timedelta(seconds=5), reply_to_msg_id=None, reply_to=None, forward=None),
        # 6. TP1 already reached on market (crossed BUY LIMIT 3896 with TP1=3892 <= live 3895) -> rejected stale
        SimpleNamespace(id=104, message="Buy limit 3896\nTP 3892\nSL 3870", date=now_utc - timedelta(seconds=5), reply_to_msg_id=None, reply_to=None, forward=None),
        # 7. Fresh valid pending BUY LIMIT -> replayed & placed (4 orders)!
        SimpleNamespace(id=105, message="Buy limit 3880\nTP 3900\nSL 3865", date=now_utc - timedelta(seconds=5), reply_to_msg_id=None, reply_to=None, forward=None),
    ]

    mock_tg = MagicMock()
    mock_tg.get_messages = AsyncMock(return_value=fake_msgs)
    mock_tg.send_message = AsyncMock()
    orig_tg = main.telethon_client
    main.telethon_client = mock_tg
    try:
        summary = await main.run_reconnect_catchup(reason="test_reconnect")
    finally:
        main.telethon_client = orig_tg

    assert summary["edits"] == 1
    assert summary["replayed"] == 1
    assert summary["skipped_stale"] == 3
    assert main.state_mgr.get_signal(100, chat_id=chat_id)["raw_tp1"] == 3905.0
    assert main.state_mgr.get_signal(99, chat_id=chat_id) is None
    assert main.state_mgr.get_signal(101, chat_id=chat_id) is None
    assert main.state_mgr.get_signal(102, chat_id=chat_id) is None
    assert main.state_mgr.get_signal(103, chat_id=chat_id) is None
    assert main.state_mgr.get_signal(104, chat_id=chat_id) is None
    s105 = main.state_mgr.get_signal(105, chat_id=chat_id)
    assert s105 is not None and len(s105["pending_tickets"]) == 4
    assert main.state_mgr.get_last_seen_msg_id(chat_id) == 105


@pytest.mark.asyncio
async def test_mt5_disconnect_and_recovery_one_shot_alerts(tmp_path):
    _reset_runtime(tmp_path, "mt5_alerts")
    main.executor = MT5Executor(dry_run=False)
    alerts = []
    orig_notify = main.notify_saved_messages

    async def _cap(txt: str):
        alerts.append(txt)

    main.notify_saved_messages = _cap
    try:
        await main._handle_mt5_connection_transition(True)   # Initial online
        await main._handle_mt5_connection_transition(False)  # 1st disconnect -> alert
        await main._handle_mt5_connection_transition(False)  # Still down -> NO repeat
        await main._handle_mt5_connection_transition(False)  # Still down -> NO repeat
        await main._handle_mt5_connection_transition(True)   # Recovered -> alert
        await main._handle_mt5_connection_transition(True)   # Still online -> NO repeat

        assert len(alerts) == 2
        assert "MT5 Connection Lost" in alerts[0]
        assert "MT5 Connection Recovered" in alerts[1]
    finally:
        main.notify_saved_messages = orig_notify


# ---------------------------------------------------------------------------
# 4. RUNNER_ACTIVE BROKER CLOSE UNIFIED ACCOUNTING, LEDGER & STATE MANAGER
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_active_broker_close_unified_accounting_and_ledger(tmp_path):
    _reset_runtime(tmp_path, "unified_close")
    main.executor = MT5Executor(dry_run=False)
    main.executor._connected = True

    chat_id = -100555666
    msg_id = 8801
    main.state_mgr.create_signal(
        message_id=msg_id,
        chat_id=chat_id,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "ticket": 701, "order_type": "MARKET", "status": "CLOSED"},
            "L2": {"lot": 0.01, "ticket": 702, "order_type": "LIMIT", "status": "OPEN"},
            "L3": {"lot": 0.01, "ticket": 703, "order_type": "LIMIT", "status": "OPEN"},
            "L4": {"lot": 0.02, "ticket": 704, "order_type": "LIMIT", "status": "RUNNER"},
        },
        market_tickets=[702, 703, 704],
        pending_tickets=[],
        accounted_tickets=[701],
        banked_profit=1.25,
        runner_layer="L4",
        runner_ticket=704,
        runner_cleared_milestones=["TP1", "TP2"],
        tp1_reached=True,
        status="RUNNER_ACTIVE",
    )

    alerts = []
    orig_notify = main.notify_saved_messages

    async def _cap(txt: str):
        alerts.append(txt)

    main.notify_saved_messages = _cap
    try:
        # Phase 1: Broker closes runner #704 (+12.50 GBP) while L2(#702) & L3(#703) stay open
        open_pos = [SimpleNamespace(ticket=702), SimpleNamespace(ticket=703)]
        close_map = {
            704: {"profit": 12.50, "reason": "TP"},
            702: {"profit": 4.00, "reason": "TP"},
            703: {"profit": 6.00, "reason": "TP"},
        }

        with patch.object(main.executor, "get_live_positions", side_effect=lambda *a: open_pos), \
             patch.object(main.executor, "get_live_orders", return_value=[]), \
             patch.object(main.executor, "get_position_close_info", side_effect=lambda t: close_map[t]), \
             patch.object(main.executor, "get_account_financials", return_value={
                 "balance": 200.0, "credit": 0.0, "equity": 200.0, "currency": "GBP", "formatted": "Balance: 200.00 GBP"
             }):
            await main._reconcile_live_mt5_signals(main.state_mgr.get_active_signals())

            s_mid = main.state_mgr.get_signal(msg_id, chat_id=chat_id)
            assert s_mid["status"] == "BE_APPLIED"
            assert s_mid["runner_ticket"] is None
            assert s_mid["market_tickets"] == [702, 703]
            assert s_mid["banked_profit"] == 13.75  # 1.25 + 12.50
            assert main.trade_ledger.read_trades() == []  # Still open positions -> not finalized yet!
            assert any(
                "Runner Closed by Broker & Unified Summary" in a
                and "+£12.50 GBP" in a
                and "+£1.25 GBP" in a
                and "+£13.75 GBP" in a
                for a in alerts
            )

            # Phase 2: L2 (#702) and L3 (#703) close at TP2/TP3 -> signal CLOSED + single ledger record!
            open_pos.clear()
            await main._reconcile_live_mt5_signals(main.state_mgr.get_active_signals())

        s_done = main.state_mgr.get_signal(msg_id, chat_id=chat_id)
        assert s_done["status"] == "CLOSED"
        assert s_done["banked_profit"] == 23.75  # 1.25 + 12.50 + 4.00 + 6.00
        assert set(s_done["accounted_tickets"]) == {701, 702, 703, 704}
        assert s_done["ledger_recorded"] is True

        trades = main.trade_ledger.read_trades()
        assert len(trades) == 1
        assert trades[0]["realized_pnl"] == 23.75
        assert trades[0]["total_lots"] == 0.05
        assert trades[0]["highest_milestone_cleared"] == "TP2"
        assert set(trades[0]["tickets"]) == {701, 702, 703, 704}

        # Idempotent: calling _record_signal_to_ledger again does not duplicate
        assert await main._record_signal_to_ledger(msg_id, s_done, "Repeat") is None
        assert len(main.trade_ledger.read_trades()) == 1
    finally:
        main.notify_saved_messages = orig_notify


def test_trade_ledger_stats_and_state_manager_dirty_checking(tmp_path):
    # 1. TradeLedger JSONL + corrupt line resilience + stats
    ledger_path = tmp_path / "trades.jsonl"
    ledger = TradeLedger(ledger_path)
    ledger.append_trade({"signal_key": "1", "realized_pnl": 10.50, "closed_at": "2026-11-17T10:00:00+00:00", "exit_reason": "TP5"})
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write("{corrupt-json-line\n")
    ledger.append_trade({"signal_key": "2", "realized_pnl": -4.00, "closed_at": "2026-11-17T11:00:00+00:00", "exit_reason": "SL"})

    trades = ledger.read_trades()
    assert len(trades) == 2
    stats = ledger.get_stats(server_date="2026-11-17")
    assert stats["total_trades"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert stats["win_rate_pct"] == 50.0
    assert stats["total_realized_pnl"] == 6.50
    assert stats["today_trades"] == 2
    assert stats["today_realized_pnl"] == 6.50
    assert stats["best_trade_pnl"] == 10.50 and stats["worst_trade_pnl"] == -4.00

    # 2. StateManager dirty-checked writes & monotonic watermarks
    mgr = StateManager(tmp_path / "dirty_state.json")
    mgr.create_signal(message_id=55, chat_id=-100999, symbol="XAUUSD.s", direction="BUY")
    saves_after_create = mgr.save_count

    # Updating identical values performs ZERO disk writes
    mgr.update_signal(55, {"direction": "BUY"}, chat_id=-100999)
    mgr.set_tp1_reached(55, False, chat_id=-100999)
    assert mgr.save_count == saves_after_create

    # Monotonic watermarks
    mgr.set_catchup_watermark(-100999, 500)
    saves_wm = mgr.save_count
    mgr.set_catchup_watermark(-100999, 450)  # Older ID must not regress or write
    assert mgr.get_last_seen_msg_id(-100999) == 500
    assert mgr.save_count == saves_wm


# ---------------------------------------------------------------------------
# 5. SINGLE-CALL CLOSE INFO, PNL CACHE TTL, /HEALTH & BUY STOP LIFECYCLE
# ---------------------------------------------------------------------------

def test_executor_single_deal_query_and_pnl_cache_ttl():
    ex = MT5Executor(dry_run=False)
    ex._connected = True
    ex._server_offset_cache = 7200

    # 1. Single history_deals_get call in get_position_close_info
    deals_for_pos = [
        SimpleNamespace(entry=0, type=0, profit=0.0, commission=-0.10, swap=0.0, fee=0.0, reason=0),
        SimpleNamespace(entry=1, type=1, profit=15.00, commission=-0.10, swap=-0.20, fee=0.0, reason=5),
    ]
    with patch.object(ex_mod.mt5, "history_deals_get", return_value=deals_for_pos) as spy_deals:
        info = ex.get_position_close_info(8888)
        assert spy_deals.call_count == 1
        assert info == {"profit": 14.6, "reason": "TP"}

    # 2. Realized today PnL cache hits within TTL and resets on invalidate_realized_pnl_cache()
    now_srv = time.time() + 7200
    tick = SimpleNamespace(bid=3895.0, ask=3895.5, time=now_srv)
    day_deals = [SimpleNamespace(entry=1, type=1, time=now_srv - 5, profit=20.0, commission=0.0, swap=0.0, fee=0.0)]
    with patch.object(ex_mod.mt5, "symbol_info_tick", return_value=tick), \
         patch.object(ex_mod.mt5, "history_deals_get", return_value=day_deals) as spy_pnl:
        assert ex.get_realized_today_pnl() == 20.0
        assert ex.get_realized_today_pnl() == 20.0
        assert spy_pnl.call_count == 1  # Cached!

        ex.invalidate_realized_pnl_cache()
        day_deals.append(SimpleNamespace(entry=1, type=1, time=now_srv - 2, profit=5.0, commission=0.0, swap=0.0, fee=0.0))
        assert ex.get_realized_today_pnl() == 25.0
        assert spy_pnl.call_count == 2


@pytest.mark.asyncio
async def test_health_command_and_buy_stop_full_lifecycle(tmp_path):
    _reset_runtime(tmp_path, "health_and_stop")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main._telegram_reconnect_count = 2

    fake_tg = MagicMock()
    fake_tg.is_connected.return_value = True
    orig_tg = main.telethon_client
    main.telethon_client = fake_tg
    main.trade_ledger.append_trade({"signal_key": "seed:1", "realized_pnl": 18.50, "exit_reason": "TP5"})

    try:
        # 1. Place BUY STOP signal through handle_new_message
        await main.handle_new_message(_make_event(9201, "Buy stop 3905\nTP 3915\nSL 3890"))
        sig = main.state_mgr.get_signal(9201, chat_id=-1001234567890)
        assert sig is not None
        assert sig["is_stop_order"] is True
        assert sig["order_intent"] == "STOP"
        assert len(sig["pending_tickets"]) == 4

        # 2. Verify /health command reports all v3.0 telemetry
        health_evt = _make_event(9202, "/health", chat_id="me")
        await main.handle_saved_messages_command(health_evt)
        assert len(health_evt.replies) == 1
        rep = health_evt.replies[0]
        assert "XAUUSD Copier v3.0 Health Telemetry" in rep
        assert "Ping:" in rep and "ms" in rep
        assert "Reconnects: `2`" in rep
        assert "Active Signals (1)" in rep
        assert "Ledger Summary" in rep
        assert "`1` trades (`1W/0L`)" in rep
        assert "Win Rate: `100.0%`" in rep
    finally:
        main.telethon_client = orig_tg
