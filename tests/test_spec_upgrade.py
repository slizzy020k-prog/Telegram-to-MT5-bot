"""
Spec Section 4 Compliance Test Suite (Updated Technical Specification):
- Test 1: Sell Now Price Guard (favorable executes, unfavorable aborts immediately)
- Test 2: Buy Now Price Guard (favorable executes, unfavorable aborts immediately)
- Test 3: TP1 Invalidation via Reply (mid-delay & unfilled zone limits -> TRADE_ACTION_REMOVE, active positions untouched)
- Test 4: TP1 Invalidation via Live Market Price (mid-delay & unfilled zone limits -> TRADE_ACTION_REMOVE)
- Test 5: Daily Realized Loss Limit (/set_daily_loss, /status_daily_loss, floating ignored, realized breach -> cancel pending, shutdown, disconnect, sys.exit(0))
- Test 6: Multi-channel (chat_id, message_id) collision isolation
"""
import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import MetaTrader5 as mt5

import main
import mt5_executor as ex_mod
from config import DEFAULT_SYMBOL, MAGIC_NUMBER
from mt5_executor import MT5Executor
from state_manager import StateManager


def _make_event(msg_id: int, text: str, reply_to_msg_id=None, chat_id=-1001234567890):
    evt = MagicMock()
    evt.chat_id = chat_id
    evt.is_reply = reply_to_msg_id is not None
    evt.replies = []
    evt.message = MagicMock()
    evt.message.id = msg_id
    evt.message.message = text
    evt.message.reply_to_msg_id = reply_to_msg_id
    evt.message.forward = None
    evt.message.out = False

    async def _reply(msg_txt: str):
        evt.replies.append(msg_txt)

    evt.reply = _reply
    return evt


def _build_mock_mt5_env(tick_ref: dict, order_counter_start: int = 5000):
    """Build deterministic mocked MetaTrader5 API calls for live-mode executor tests."""
    counter = {"n": order_counter_start}
    sent_requests = []
    open_positions = []
    pending_orders = []
    history_deals = []

    def _symbol_info(sym):
        return SimpleNamespace(
            digits=2,
            point=0.01,
            spread=20,
            trade_mode=4,
            filling_mode=2,
            visible=True,
            trade_stops_level=50,
        )

    def _symbol_info_tick(sym):
        return tick_ref["tick"]

    def _order_send(req):
        sent_requests.append(dict(req))
        counter["n"] += 1
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=counter["n"], comment="Mock done")

    def _positions_get(*args, **kwargs):
        ticket = kwargs.get("ticket")
        if ticket is not None:
            return [p for p in open_positions if p.ticket == ticket]
        return list(open_positions)

    def _orders_get(*args, **kwargs):
        ticket = kwargs.get("ticket")
        if ticket is not None:
            return [o for o in pending_orders if o.ticket == ticket]
        return list(pending_orders)

    def _history_deals_get(*args, **kwargs):
        return list(history_deals)

    patches = [
        patch.object(ex_mod.mt5, "initialize", return_value=True),
        patch.object(ex_mod.mt5, "shutdown", return_value=True),
        patch.object(ex_mod.mt5, "terminal_info", return_value=SimpleNamespace(connected=True, trade_allowed=True)),
        patch.object(ex_mod.mt5, "account_info", return_value=SimpleNamespace(
            login=36393319, server="PUPrime-Live 6", balance=500.0, credit=0.0,
            equity=500.0, profit=0.0, margin=0.0, margin_free=500.0, currency="GBP",
        )),
        patch.object(ex_mod.mt5, "symbol_info", side_effect=_symbol_info),
        patch.object(ex_mod.mt5, "symbol_select", return_value=True),
        patch.object(ex_mod.mt5, "symbol_info_tick", side_effect=_symbol_info_tick),
        patch.object(ex_mod.mt5, "order_send", side_effect=_order_send),
        patch.object(ex_mod.mt5, "positions_get", side_effect=_positions_get),
        patch.object(ex_mod.mt5, "orders_get", side_effect=_orders_get),
        patch.object(ex_mod.mt5, "history_deals_get", side_effect=_history_deals_get),
        patch.object(ex_mod.mt5, "last_error", return_value=(1, "OK")),
    ]
    return {
        "patches": patches,
        "sent_requests": sent_requests,
        "open_positions": open_positions,
        "pending_orders": pending_orders,
        "history_deals": history_deals,
    }


def _reset_main(tmp_path, filename: str):
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    main.TRADING_HALTED = False
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main._daily_loss_lock_notified_date = None
    main._awaiting_dailywin_input = False
    main._awaiting_dailyloss_input = False
    main._daily_50pct_alert_date = None
    main.state_mgr = StateManager(tmp_path / filename)


@pytest.mark.asyncio
async def test_spec_1_sell_now_price_guard_mocked_mt5_and_telethon(tmp_path, caplog):
    """
    Spec Section 4 Test 1 (Sell Now Price Guard):
    initial_price = 2650.00 -> post-delay bid=2651.20 executes + logs EXECUTED,
    whereas post-delay bid=2648.50 aborts immediately + logs ABORTED.
    """
    _reset_main(tmp_path, "spec1.json")
    now_srv = time.time() + 7200
    tick_ref = {"tick": SimpleNamespace(bid=2650.00, ask=2650.50, time=now_srv)}
    env = _build_mock_mt5_env(tick_ref)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()
    mock_tg.disconnect = AsyncMock()

    orig_tg = main.telethon_client
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_poll = main.DELAYED_POLL_INTERVAL_SECONDS
    orig_abort = main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH
    main.telethon_client = mock_tg
    main.SELL_NOW_DELAY_SECONDS = 0.06
    main.DELAYED_POLL_INTERVAL_SECONDS = 0.02
    main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = True

    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        assert main.executor.connect() is True
        caplog.set_level(logging.INFO, logger="copier")

        # 1A. Favorable move: 2650.00 -> 2651.20 -> EXECUTED
        await main.handle_new_message(_make_event(101, "Sell now"))
        sig_init = main.state_mgr.get_signal(101)
        assert sig_init["status"] == "PENDING"
        assert sig_init["initial_price"] == 2650.00

        tick_ref["tick"] = SimpleNamespace(bid=2651.20, ask=2651.70, time=now_srv)
        await asyncio.sleep(0.12)

        sig_exec = main.state_mgr.get_signal(101)
        assert sig_exec["status"] == "ACTIVE"
        assert len(sig_exec["market_tickets"]) == 1
        assert any(r.get("action") == mt5.TRADE_ACTION_DEAL and r.get("type") == mt5.ORDER_TYPE_SELL for r in env["sent_requests"])
        assert any("[PRICE GUARD]" in rec.message and "SELL" in rec.message and "2650.0" in rec.message and "2651.2" in rec.message and "Action: EXECUTED" in rec.message for rec in caplog.records)

        # 1B. Unfavorable move: 2650.00 -> 2648.50 -> ABORTED immediately
        env["sent_requests"].clear()
        caplog.clear()
        tick_ref["tick"] = SimpleNamespace(bid=2650.00, ask=2650.50, time=now_srv)
        await main.handle_new_message(_make_event(102, "Sell now"))
        assert main.state_mgr.get_signal(102)["initial_price"] == 2650.00

        tick_ref["tick"] = SimpleNamespace(bid=2648.50, ask=2649.00, time=now_srv)
        await asyncio.sleep(0.12)

        sig_abort = main.state_mgr.get_signal(102)
        assert sig_abort["status"] == "ABORTED_PRICE_GUARD"
        assert sig_abort["market_tickets"] == []
        assert env["sent_requests"] == []
        assert any("[PRICE GUARD]" in rec.message and "SELL" in rec.message and "2650.0" in rec.message and "2648.5" in rec.message and "Action: ABORTED" in rec.message for rec in caplog.records)
    finally:
        for p in reversed(env["patches"]):
            p.stop()
        main.telethon_client = orig_tg
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        main.DELAYED_POLL_INTERVAL_SECONDS = orig_poll
        main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = orig_abort
        _reset_main(tmp_path, "spec1_cleanup.json")


@pytest.mark.asyncio
async def test_spec_2_buy_now_price_guard_mocked_mt5_and_telethon(tmp_path, caplog):
    """
    Spec Section 4 Test 2 (Buy Now Price Guard):
    initial_price = 2650.00 -> post-delay ask=2649.10 executes + logs EXECUTED,
    whereas post-delay ask=2652.00 aborts immediately + logs ABORTED.
    """
    _reset_main(tmp_path, "spec2.json")
    now_srv = time.time() + 7200
    tick_ref = {"tick": SimpleNamespace(bid=2649.50, ask=2650.00, time=now_srv)}
    env = _build_mock_mt5_env(tick_ref)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()
    mock_tg.disconnect = AsyncMock()

    orig_tg = main.telethon_client
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_poll = main.DELAYED_POLL_INTERVAL_SECONDS
    orig_abort = main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH
    main.telethon_client = mock_tg
    main.SELL_NOW_DELAY_SECONDS = 0.06
    main.DELAYED_POLL_INTERVAL_SECONDS = 0.02
    main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = True

    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        assert main.executor.connect() is True
        caplog.set_level(logging.INFO, logger="copier")

        # 2A. Favorable move: 2650.00 -> 2649.10 -> EXECUTED
        await main.handle_new_message(_make_event(201, "Buy now"))
        assert main.state_mgr.get_signal(201)["initial_price"] == 2650.00

        tick_ref["tick"] = SimpleNamespace(bid=2648.60, ask=2649.10, time=now_srv)
        await asyncio.sleep(0.12)

        sig_exec = main.state_mgr.get_signal(201)
        assert sig_exec["status"] == "ACTIVE"
        assert len(sig_exec["market_tickets"]) == 1
        assert any(r.get("action") == mt5.TRADE_ACTION_DEAL and r.get("type") == mt5.ORDER_TYPE_BUY for r in env["sent_requests"])
        assert any("[PRICE GUARD]" in rec.message and "BUY" in rec.message and "2650.0" in rec.message and "2649.1" in rec.message and "Action: EXECUTED" in rec.message for rec in caplog.records)

        # 2B. Unfavorable move: 2650.00 -> 2652.00 -> ABORTED immediately
        env["sent_requests"].clear()
        caplog.clear()
        tick_ref["tick"] = SimpleNamespace(bid=2649.50, ask=2650.00, time=now_srv)
        await main.handle_new_message(_make_event(202, "Buy now"))
        assert main.state_mgr.get_signal(202)["initial_price"] == 2650.00

        tick_ref["tick"] = SimpleNamespace(bid=2651.50, ask=2652.00, time=now_srv)
        await asyncio.sleep(0.12)

        sig_abort = main.state_mgr.get_signal(202)
        assert sig_abort["status"] == "ABORTED_PRICE_GUARD"
        assert sig_abort["market_tickets"] == []
        assert env["sent_requests"] == []
        assert any("[PRICE GUARD]" in rec.message and "BUY" in rec.message and "2650.0" in rec.message and "2652.0" in rec.message and "Action: ABORTED" in rec.message for rec in caplog.records)
    finally:
        for p in reversed(env["patches"]):
            p.stop()
        main.telethon_client = orig_tg
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        main.DELAYED_POLL_INTERVAL_SECONDS = orig_poll
        main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = orig_abort
        _reset_main(tmp_path, "spec2_cleanup.json")


@pytest.mark.asyncio
async def test_spec_3_tp1_invalidation_via_reply_removes_pending_without_closing_filled(tmp_path, caplog):
    """
    Spec Section 4 Test 3 (TP1 via Reply):
    - Reply "TP1 hit" mid-delay -> aborts delayed trade (INVALIDATED_TP1), logs [TP1 INVALIDATED], 0 orders.
    - Reply "TP1 hit" while unfilled zone limit order is active -> removes pending MT5 limit orders via
      mt5.TRADE_ACTION_REMOVE and logs [TP1 INVALIDATED].
    - Already-filled active positions on MT5 are NEVER closed by invalidate_signal_tp1.
    """
    _reset_main(tmp_path, "spec3.json")
    now_srv = time.time() + 7200
    tick_ref = {"tick": SimpleNamespace(bid=2652.00, ask=2652.50, time=now_srv)}
    env = _build_mock_mt5_env(tick_ref, order_counter_start=7000)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()
    mock_tg.disconnect = AsyncMock()

    orig_tg = main.telethon_client
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.telethon_client = mock_tg
    main.SELL_NOW_DELAY_SECONDS = 2.0

    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        assert main.executor.connect() is True
        caplog.set_level(logging.INFO, logger="copier")

        # 3A. Mid-delay reply "TP1 hit"
        await main.handle_new_message(_make_event(301, "Buy now"))
        assert main.state_mgr.get_signal(301)["status"] == "PENDING"
        await main.handle_new_message(_make_event(302, "TP1 hit", reply_to_msg_id=301))
        await asyncio.sleep(0.05)

        s_delay = main.state_mgr.get_signal(301)
        assert s_delay["status"] == "INVALIDATED_TP1"
        assert s_delay["tp1_reached"] is True
        assert 301 not in main._pending_sell_now_tasks
        assert env["sent_requests"] == []
        assert any("[TP1 INVALIDATED]" in rec.message and "Reply" in rec.message for rec in caplog.records)

        # 3B. Unfilled zone limit orders active while another filled position exists
        caplog.clear()
        filled_pos = SimpleNamespace(ticket=7777, symbol=DEFAULT_SYMBOL, volume=0.01, type=mt5.ORDER_TYPE_BUY, magic=MAGIC_NUMBER, profit=5.0, swap=0.0)
        env["open_positions"].append(filled_pos)

        await main.handle_new_message(_make_event(303, "Buy 2650-2645\nTP1 2658\nSL 2635"))
        sz = main.state_mgr.get_signal(303)
        assert sz["market_tickets"] == []
        pending_tkts = list(sz["pending_tickets"])
        assert len(pending_tkts) == 3

        env["sent_requests"].clear()
        await main.handle_new_message(_make_event(304, "TP1 hit", reply_to_msg_id=303))

        sz_after = main.state_mgr.get_signal(303)
        assert sz_after["status"] == "INVALIDATED_TP1"
        assert sz_after["tp1_reached"] is True
        assert sz_after["pending_tickets"] == []

        remove_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_REMOVE]
        deal_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_DEAL]
        assert {r["order"] for r in remove_reqs} == set(pending_tkts)
        assert deal_reqs == []  # Never closed already-filled position #7777!
        assert any("[TP1 INVALIDATED]" in rec.message and "Reply" in rec.message for rec in caplog.records)

        # 3C. Also verify invalidate_signal_tp1 directly leaves market_tickets untouched
        main.state_mgr.create_signal(
            message_id=305,
            symbol=DEFAULT_SYMBOL,
            direction="BUY",
            market_tickets=[7777],
            pending_tickets=[7999],
            layers={
                "TOP": {"ticket": 7777, "status": "OPEN"},
                "MID": {"ticket": 7999, "status": "PENDING"},
            },
        )
        env["sent_requests"].clear()
        await main.invalidate_signal_tp1(305, reason="Reply")
        assert [r["order"] for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_REMOVE] == [7999]
        assert [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_DEAL] == []
    finally:
        for p in reversed(env["patches"]):
            p.stop()
        main.telethon_client = orig_tg
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        _reset_main(tmp_path, "spec3_cleanup.json")


@pytest.mark.asyncio
async def test_spec_4_tp1_invalidation_via_live_market_price_mid_delay_and_zone(tmp_path, caplog):
    """
    Spec Section 4 Test 4 (TP1 via Live Market Price):
    - Signal 'BUY NOW TP1 2655 SL 2640' at 2650.00; during delay, tick hits 2655.20 ->
      aborts entry immediately (INVALIDATED_TP1) with 0 orders.
    - Unfilled zone limits active when tick reaches TP1 -> watcher removes pending limits via TRADE_ACTION_REMOVE.
    """
    _reset_main(tmp_path, "spec4.json")
    now_srv = time.time() + 7200
    tick_ref = {"tick": SimpleNamespace(bid=2649.50, ask=2650.00, time=now_srv)}
    env = _build_mock_mt5_env(tick_ref, order_counter_start=8000)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()
    mock_tg.disconnect = AsyncMock()

    orig_tg = main.telethon_client
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_poll = main.DELAYED_POLL_INTERVAL_SECONDS
    main.telethon_client = mock_tg
    main.SELL_NOW_DELAY_SECONDS = 0.5
    main.DELAYED_POLL_INTERVAL_SECONDS = 0.02

    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        assert main.executor.connect() is True
        caplog.set_level(logging.INFO, logger="copier")

        # 4A. BUY NOW TP1 2655 SL 2640 invalidated mid-delay when live tick reaches 2655.20
        await main.handle_new_message(_make_event(401, "BUY NOW TP1 2655 SL 2640"))
        sig_init = main.state_mgr.get_signal(401)
        assert sig_init["status"] == "PENDING"
        assert sig_init["tp1_price"] == 2655.0
        assert sig_init["sl"] == 2640.0

        tick_ref["tick"] = SimpleNamespace(bid=2655.20, ask=2655.50, time=now_srv)
        await asyncio.sleep(0.10)

        sig_inv = main.state_mgr.get_signal(401)
        assert sig_inv["status"] == "INVALIDATED_TP1"
        assert sig_inv["tp1_reached"] is True
        assert sig_inv["market_tickets"] == []
        assert [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_DEAL] == []
        assert any("[TP1 INVALIDATED]" in rec.message and "Market Price Reached TP1" in rec.message for rec in caplog.records)

        # 4B. Unfilled zone pending limits removed via TRADE_ACTION_REMOVE when live tick reaches TP1
        caplog.clear()
        tick_ref["tick"] = SimpleNamespace(bid=2649.50, ask=2650.00, time=now_srv)
        await main.handle_new_message(_make_event(402, "Buy 2645-2640\nTP1 2655\nSL 2630"))
        sz = main.state_mgr.get_signal(402)
        pending_tkts = list(sz["pending_tickets"])
        assert len(pending_tkts) == 3
        for pt in pending_tkts:
            env["pending_orders"].append(SimpleNamespace(ticket=pt, symbol=DEFAULT_SYMBOL, volume_current=0.01, magic=MAGIC_NUMBER))

        env["sent_requests"].clear()
        tick_ref["tick"] = SimpleNamespace(bid=2655.20, ask=2655.50, time=now_srv)
        watcher = asyncio.create_task(main.tp1_proximity_watcher_task())
        await asyncio.sleep(0.05)
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

        sz_after = main.state_mgr.get_signal(402)
        assert sz_after["status"] == "INVALIDATED_TP1"
        assert sz_after["pending_tickets"] == []
        remove_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_REMOVE]
        assert {r["order"] for r in remove_reqs} == set(pending_tkts)
    finally:
        for p in reversed(env["patches"]):
            p.stop()
        main.telethon_client = orig_tg
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        main.DELAYED_POLL_INTERVAL_SECONDS = orig_poll
        _reset_main(tmp_path, "spec4_cleanup.json")


@pytest.mark.asyncio
async def test_spec_5_daily_realized_loss_limit_mocked_mt5_and_telethon(tmp_path):
    """
    Spec Section 4 Test 5 (Daily Realized Loss Limit):
    - /set_daily_loss 100 and /status_daily_loss in Saved Messages ("me")
    - Floating open trade at -$300 (history_deals_get = []) -> bot stays running (TRADING_HALTED is False)
    - Closed deal at -$105 net (DEAL_ENTRY_OUT, DEAL_TYPE_SELL) -> sends exact alert to "me",
      cancels pending orders via TRADE_ACTION_REMOVE, calls mt5.shutdown() & telethon.disconnect(),
      and executes sys.exit(0).
    """
    _reset_main(tmp_path, "spec5.json")
    now_srv = time.time() + 7200
    tick_ref = {"tick": SimpleNamespace(bid=2650.00, ask=2650.50, time=now_srv)}
    env = _build_mock_mt5_env(tick_ref, order_counter_start=9000)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()
    mock_tg.disconnect = AsyncMock()

    orig_tg = main.telethon_client
    orig_close_open = main.DAILY_LOSS_CLOSE_OPEN_TRADES
    main.telethon_client = mock_tg
    main.DAILY_LOSS_CLOSE_OPEN_TRADES = False

    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        assert main.executor.connect() is True

        # 5A. /set_daily_loss 100 & /status_daily_loss
        cmd_set = _make_event(501, "/set_daily_loss 100", chat_id="me")
        await main.handle_saved_messages_command(cmd_set)
        assert any("Daily realized loss limit set to -100.00 GBP." in r for r in cmd_set.replies)
        assert main.state_mgr.get_daily_loss_config()["daily_loss_limit"] == 100.0

        cmd_status = _make_event(502, "/status_daily_loss", chat_id="me")
        await main.handle_saved_messages_command(cmd_status)
        assert any("-100.00 GBP" in r and "0.00 GBP" in r for r in cmd_status.replies)

        # 5B. Mock open trade at -$300 floating loss (no closed deals) -> MUST NOT trigger halt
        env["open_positions"].append(
            SimpleNamespace(ticket=9002, symbol=DEFAULT_SYMBOL, volume=0.02, type=mt5.ORDER_TYPE_BUY, magic=MAGIC_NUMBER, profit=-300.0, swap=0.0)
        )
        env["pending_orders"].append(
            SimpleNamespace(ticket=9001, symbol=DEFAULT_SYMBOL, volume_current=0.01, magic=MAGIC_NUMBER)
        )
        env["history_deals"].clear()

        res_float = await main.check_and_enforce_daily_loss_limit(exit_on_trigger=True)
        assert res_float == "OK"
        assert main.TRADING_HALTED is False
        assert main.session_trading_blocked() is False

        # 5C. Closed deal hits -$105 net (plus balance/credit decoy deals) -> HALT + CANCEL PENDING + SHUTDOWN + EXIT(0)
        env["history_deals"].extend([
            SimpleNamespace(entry=1, type=2, time=now_srv - 30, profit=-500.0, commission=0.0, swap=0.0, fee=0.0),
            SimpleNamespace(entry=1, type=3, time=now_srv - 25, profit=-200.0, commission=0.0, swap=0.0, fee=0.0),
            SimpleNamespace(entry=1, type=mt5.DEAL_TYPE_SELL, time=now_srv - 10, profit=-104.0, commission=-0.50, swap=-0.50, fee=0.0),
        ])

        with pytest.raises(SystemExit) as exit_info:
            await main.check_and_enforce_daily_loss_limit(exit_on_trigger=True)
        assert exit_info.value.code == 0
        assert main.TRADING_HALTED is True
        assert main.session_trading_blocked() is True

        # Pending order #9001 cancelled via TRADE_ACTION_REMOVE; open position #9002 left on broker SL/TP
        remove_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_REMOVE]
        deal_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_DEAL]
        assert [r["order"] for r in remove_reqs] == [9001]
        assert deal_reqs == []

        # Saved Messages alert contains exact spec headline
        sent_texts = [call.args[1] for call in mock_tg.send_message.await_args_list if call.args[0] == "me"]
        assert any(
            "🛑 DAILY LOSS LIMIT HIT: Realized P&L is -105.00 (Limit: -100.00). Pending orders canceled. Terminating bot session."
            in t
            for t in sent_texts
        )

        # Telethon disconnect() and MT5 shutdown() called
        mock_tg.disconnect.assert_awaited()
        ex_mod.mt5.shutdown.assert_called()
    finally:
        for p in reversed(env["patches"]):
            p.stop()
        main.telethon_client = orig_tg
        main.DAILY_LOSS_CLOSE_OPEN_TRADES = orig_close_open
        _reset_main(tmp_path, "spec5_cleanup.json")


@pytest.mark.asyncio
async def test_spec_6_multichannel_chat_id_message_id_collision_isolation(tmp_path):
    """Two channels posting the same message_id concurrently do not collide in state or delayed tasks."""
    _reset_main(tmp_path, "spec6.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 5.0
    try:
        await main.handle_new_message(_make_event(500, "Sell now", chat_id=-100111))
        await main.handle_new_message(_make_event(500, "Buy now", chat_id=-100222))

        s1 = main.state_mgr.get_signal(500, chat_id=-100111)
        s2 = main.state_mgr.get_signal(500, chat_id=-100222)
        assert s1["direction"] == "SELL" and s1["status"] == "PENDING"
        assert s2["direction"] == "BUY" and s2["status"] == "PENDING"

        # Invalidating channel -100222's signal #500 leaves channel -100111's signal #500 PENDING
        await main.handle_new_message(_make_event(501, "TP1 hit", reply_to_msg_id=500, chat_id=-100222))
        assert main.state_mgr.get_signal(500, chat_id=-100222)["status"] == "INVALIDATED_TP1"
        assert main.state_mgr.get_signal(500, chat_id=-100111)["status"] == "PENDING"
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        _reset_main(tmp_path, "spec6_cleanup.json")


@pytest.mark.asyncio
async def test_spec_7_zero_delay_immediate_now_execution(tmp_path):
    """When SELL_NOW_DELAY_SECONDS=0, Buy/Sell now execute immediately with 0 pending timer."""
    _reset_main(tmp_path, "spec7.json")
    tick_ref = {"tick": SimpleNamespace(bid=3850.20, ask=3850.70, time=1794886034)}
    env = _build_mock_mt5_env(tick_ref, order_counter_start=7000)

    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 0.0
    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        main.executor.connect()

        # 1. Immediate Buy now -> enters Market BUY on arrival without PENDING timer
        await main.handle_new_message(_make_event(701, "Buy xauusd now\nSL 3835\nTP 3860"))
        assert main._pending_sell_now_tasks == {}
        s_buy = main.state_mgr.get_signal(701)
        assert s_buy is not None
        assert s_buy["status"] == "ACTIVE"
        assert len(s_buy["market_tickets"]) == 1
        assert s_buy["pending_tickets"] == []

        # 2. Immediate Sell now -> enters Market SELL immediately
        await main.handle_new_message(_make_event(702, "Sell now"))
        assert main._pending_sell_now_tasks == {}
        s_sell = main.state_mgr.get_signal(702)
        assert s_sell is not None
        assert s_sell["status"] == "ACTIVE"
        assert len(s_sell["market_tickets"]) == 1

        # 3. Follow-up zone edit seamlessly places Layers 2 & 3 limits
        await main.handle_message_edited(_make_event(702, "Sell 3850-3855\nSL 3865\nTP 3840"))
        s_edit = main.state_mgr.get_signal(702)
        assert len(s_edit["market_tickets"]) + len(s_edit["pending_tickets"]) == 3
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        for p in reversed(env["patches"]):
            p.stop()
        _reset_main(tmp_path, "spec7_cleanup.json")


def test_spec_8_buy_sell_limit_variants_and_tp1_extraction():
    """Verify all Buy/Sell Limit (singular/plural/spaced/multiline) and TP1 patterns."""
    from parser import parse_message, is_tp1_hit_message, extract_tps

    # 1. Channel signal style: double spaces + BUY LIMIT zone + multi-TP
    p1 = parse_message(
        "BUY LIMIT  3840-3830\nSL 3820 \nTP 3843\nTP 3850\nTP 3853",
        message_id=12972,
        live_price=3850.0,
    )
    assert p1 is not None and p1.is_zone is True and p1.direction == "BUY"
    assert p1.entry_tiers == {"top": 3840.0, "mid": 3835.0, "bottom": 3830.0}
    assert p1.raw_tp1 == 3843.0 and p1.final_tp == 3842.5 and p1.sl == 3820.0

    # 2. SELL LIMIT zone
    p2 = parse_message("SELL LIMIT  3860-3870\nSL 3880\nTP 3857\nTP 3850", message_id=12973, live_price=3850.0)
    assert p2 is not None and p2.is_zone is True and p2.direction == "SELL"
    assert p2.entry_tiers == {"top": 3870.0, "mid": 3865.0, "bottom": 3860.0}
    assert p2.raw_tp1 == 3857.0 and p2.final_tp == 3857.5 and p2.sl == 3880.0

    # 3. Symbol between Buy/Sell and limit
    p3 = parse_message("Buy xauusd limit 3840\nSL 3820\nTP 3850", message_id=12974, live_price=3850.0)
    assert p3 is not None and p3.single_price == 3840.0 and p3.is_instant_now is False and p3.direction == "BUY"

    p4 = parse_message("Sell xauusd limit at 3860\nSL 3875\nTP 3850", message_id=12975, live_price=3850.0)
    assert p4 is not None and p4.single_price == 3860.0 and p4.is_instant_now is False and p4.direction == "SELL"

    # 4. Plural limits with & / newline + "TP 1 3843" spaced TP1
    p5 = parse_message("Buy limits 3840 & 3830\nSL 3820\nTP 1 3843", message_id=12976, live_price=3850.0)
    assert p5 is not None and p5.is_zone is True
    assert p5.entry_tiers == {"top": 3840.0, "mid": 3835.0, "bottom": 3830.0}
    assert p5.raw_tp1 == 3843.0

    p6 = parse_message("Sell limits:\n3860\n3870\nSL 3880\nTP 1 3850", message_id=12977, live_price=3845.0)
    assert p6 is not None and p6.is_zone is True and p6.direction == "SELL"
    assert p6.entry_tiers == {"top": 3870.0, "mid": 3865.0, "bottom": 3860.0}
    assert p6.raw_tp1 == 3850.0

    # 5. "TP1💥50 PIPs" triggers TP1 hit without being misparsed as price 50
    assert is_tp1_hit_message("TP1💥50 PIPs") is True
    assert extract_tps("TP1💥50 PIPs") == []
    assert is_tp1_hit_message("TP3💥380 PIPs") is False
    assert is_tp1_hit_message("TP1 🎯 3850") is False
    tp1_mgmt = parse_message("TP1💥50 PIPs", message_id=9688, is_reply=True, reply_to_msg_id=9687)
    assert tp1_mgmt is not None and tp1_mgmt.management_action == "ACTION_TP1_HIT_REPLY"


@pytest.mark.asyncio
async def test_spec_9_buy_limit_and_sell_limit_live_activation_and_stops_fallback(tmp_path):
    """
    Verify BUY LIMIT and SELL LIMIT with TP1 between limit & market:
    - check_preflight PASSES (not falsely rejected for 'within 20 pips of TP1')
    - All 3 LIMIT orders place cleanly on MT5
    - tp1_proximity_watcher_task does NOT delete them immediately when price starts past TP1
    - Once price crosses toward entry zone and bounces back to TP1 without fill, watcher invalidates
    - Retcode 10016 (Invalid stops) right at entry boundary falls back to Market entry instead of dropping
    """
    _reset_main(tmp_path, "spec9.json")
    tick_ref = {"tick": SimpleNamespace(bid=3850.23, ask=3850.75, time=1794886034)}
    env = _build_mock_mt5_env(tick_ref, order_counter_start=9100)

    for p in env["patches"]:
        p.start()
    try:
        main.executor = MT5Executor(dry_run=False)
        main.executor.connect()

        # 1. BUY LIMIT 3840-3830, SL 3820, TP 3843 while live ask = 3850.75 (> TP1 3843)
        await main.handle_new_message(
            _make_event(9101, "BUY LIMIT  3840-3830\nSL 3820 \nTP 3843\nTP 3850", chat_id=-1003712081715)
        )
        s_buy = main.state_mgr.get_signal(9101, chat_id=-1003712081715)
        assert s_buy is not None
        assert s_buy["status"] == "ACTIVE"
        assert len(s_buy["pending_tickets"]) == 3
        assert s_buy["tp1_guard_armed"] is False

        buy_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_PENDING and r.get("type") == mt5.ORDER_TYPE_BUY_LIMIT]
        assert sorted(r["price"] for r in buy_reqs) == [3830.0, 3835.0, 3840.0]

        for pt in s_buy["pending_tickets"]:
            env["pending_orders"].append(SimpleNamespace(ticket=pt, symbol=DEFAULT_SYMBOL, volume_current=0.01, magic=MAGIC_NUMBER))

        # 2. SELL LIMIT 3860-3870, SL 3880, TP 3857 while live bid = 3850.23 (< TP1 3857)
        await main.handle_new_message(
            _make_event(9102, "SELL LIMIT 3860-3870\nSL 3880\nTP 3857\nTP 3850", chat_id=-1004369388188)
        )
        s_sell = main.state_mgr.get_signal(9102, chat_id=-1004369388188)
        assert s_sell is not None
        assert s_sell["status"] == "ACTIVE"
        assert len(s_sell["pending_tickets"]) == 3
        assert s_sell["tp1_guard_armed"] is False

        sell_reqs = [r for r in env["sent_requests"] if r.get("action") == mt5.TRADE_ACTION_PENDING and r.get("type") == mt5.ORDER_TYPE_SELL_LIMIT]
        assert sorted(r["price"] for r in sell_reqs) == [3860.0, 3865.0, 3870.0]

        for pt in s_sell["pending_tickets"]:
            env["pending_orders"].append(SimpleNamespace(ticket=pt, symbol=DEFAULT_SYMBOL, volume_current=0.01, magic=MAGIC_NUMBER))

        # 3. Single-price plural "Sell limits 3862"
        await main.handle_new_message(
            _make_event(9103, "Sell limits 3862\nSL 3875\nTP 3855", chat_id=-1004369388188)
        )
        s_single_sell = main.state_mgr.get_signal(9103, chat_id=-1004369388188)
        assert s_single_sell is not None and len(s_single_sell["pending_tickets"]) == 1
        for pt in s_single_sell["pending_tickets"]:
            env["pending_orders"].append(SimpleNamespace(ticket=pt, symbol=DEFAULT_SYMBOL, volume_current=0.01, magic=MAGIC_NUMBER))

        # 4. Run watcher while tick stays at 3850.23/3850.75 -> NONE of the BUY/SELL limits get falsely killed!
        watcher = asyncio.create_task(main.tp1_proximity_watcher_task())
        try:
            await asyncio.sleep(0.15)
            assert main.state_mgr.get_signal(9101, chat_id=-1003712081715)["status"] == "ACTIVE"
            assert main.state_mgr.get_signal(9102, chat_id=-1004369388188)["status"] == "ACTIVE"
            assert main.state_mgr.get_signal(9103, chat_id=-1004369388188)["status"] == "ACTIVE"

            # Now dip to 3841.0 (below 3843 TP1, near 3840 entry) -> arms BUY LIMIT guard, then bounce to 3843.2 -> invalidates 9101!
            tick_ref["tick"] = SimpleNamespace(bid=3840.6, ask=3841.1, time=1794886035)
            await asyncio.sleep(1.1)
            assert main.state_mgr.get_signal(9101, chat_id=-1003712081715)["tp1_guard_armed"] is True

            tick_ref["tick"] = SimpleNamespace(bid=3843.1, ask=3843.6, time=1794886036)
            await asyncio.sleep(1.1)
            assert main.state_mgr.get_signal(9101, chat_id=-1003712081715)["status"] == "INVALIDATED_TP1"
            assert main.state_mgr.get_signal(9102, chat_id=-1004369388188)["status"] == "ACTIVE"
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

        # 5. Stops-level (10016 Invalid stops) boundary fallback on limit -> Market order
        tick_ref["tick"] = SimpleNamespace(bid=3850.20, ask=3850.75, time=1794886037)
        orig_order_send = ex_mod.mt5.order_send
        call_idx = {"n": 0}

        def _stops_fail_then_market(req):
            call_idx["n"] += 1
            if req.get("action") == mt5.TRADE_ACTION_PENDING:
                return SimpleNamespace(retcode=10016, order=0, comment="Invalid stops")
            return orig_order_send(req)

        with patch.object(ex_mod.mt5, "order_send", side_effect=_stops_fail_then_market):
            mk, pd, layers = main.executor.execute_single_price_order(
                symbol=DEFAULT_SYMBOL,
                direction="BUY",
                price=3850.60,
                lot=0.01,
                sl=3835.0,
                final_tp=3860.0,
            )
            assert len(mk) == 1 and pd == []
            assert layers["TOP"]["order_type"] == "MARKET"
    finally:
        for p in reversed(env["patches"]):
            p.stop()
        _reset_main(tmp_path, "spec9_cleanup.json")


@pytest.mark.asyncio
async def test_spec_10_idle_mt5_skip_keepawake_and_keepalive_recovery(tmp_path):
    """
    Verify uptime & low-overhead fixes:
    - Disabled dailywin / dailyloss skip expensive history_deals_get calls completely.
    - Windows keep-awake and console QuickEdit helpers execute safely without errors.
    - Keepalive failure triggers clean disconnect -> connect socket recovery.
    """
    _reset_main(tmp_path, "spec10.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig_tg = main.telethon_client
    orig_keepalive = main.TELEGRAM_KEEPALIVE_SECONDS
    try:
        with patch.object(main.executor, "get_realized_today_pnl", wraps=main.executor.get_realized_today_pnl) as spy_realized, \
             patch.object(main.executor, "get_daily_drawdown_status", wraps=main.executor.get_daily_drawdown_status) as spy_dd:
            assert await main.check_and_enforce_daily_win_target() == "OK"
            assert await main.check_and_enforce_daily_loss_limit(exit_on_trigger=False) == "OK"
            assert spy_realized.call_count == 0
            assert spy_dd.call_count == 0

        # Power / console helpers must never raise
        main._set_windows_keep_awake(True)
        main._set_windows_keep_awake(False)
        main._disable_windows_console_quickedit()

        # Keepalive failure triggers disconnect + connect recovery
        mock_tg = MagicMock()
        mock_tg.is_connected.return_value = True
        mock_tg.side_effect = TimeoutError("dead socket")
        mock_tg.disconnect = AsyncMock(return_value=None)
        mock_tg.connect = AsyncMock(return_value=None)
        main.telethon_client = mock_tg

        await main._run_telegram_keepalive_check(time.time())

        assert mock_tg.disconnect.await_count == 1
        assert mock_tg.connect.await_count == 1
    finally:
        main.TELEGRAM_KEEPALIVE_SECONDS = orig_keepalive
        main.telethon_client = orig_tg
        _reset_main(tmp_path, "spec10_cleanup.json")
