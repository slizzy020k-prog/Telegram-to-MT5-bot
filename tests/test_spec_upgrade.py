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
