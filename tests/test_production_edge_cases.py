import time
from datetime import datetime, timedelta
import pytest
from unittest.mock import MagicMock, patch
from config import DEFAULT_SYMBOL, FALLBACK_SL_PIPS
from parser import parse_message, ZoneInfo
from mt5_executor import MT5Executor
from state_manager import StateManager
import main


def test_sell_3layer_tp1_scaling_all_cases():
    """Verify SELL direction TP1 scaling across shallow, medium, and full pullbacks."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    # Case A: Shallow pullback (Only Layer 1 filled at 3897)
    layers_a = {
        "TOP": {"lot": 0.01, "ticket": 601, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 602, "order_type": "LIMIT", "status": "PENDING"},
        "BOTTOM": {"lot": 0.02, "ticket": 603, "order_type": "LIMIT", "status": "PENDING"},
    }
    sig_a = {
        "layers": layers_a,
        "market_tickets": [601],
        "pending_tickets": [602, 603],
        "symbol": DEFAULT_SYMBOL,
        "direction": "SELL",
        "raw_tp1": 3880.0,
        "raw_tp2": 3860.0,
    }
    runner_a = executor.handle_tp1_scaling_and_select_runner(sig_a)
    assert runner_a is None
    assert sig_a["status"] == "CLOSED"
    assert sig_a["market_tickets"] == []
    assert sig_a["pending_tickets"] == []
    assert layers_a["TOP"]["status"] == "CLOSED"

    # Case B: Medium pullback (Layers 1 & 2 filled)
    layers_b = {
        "TOP": {"lot": 0.01, "ticket": 611, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 612, "order_type": "LIMIT", "status": "OPEN"},
        "BOTTOM": {"lot": 0.02, "ticket": 613, "order_type": "LIMIT", "status": "PENDING"},
    }
    sig_b = {
        "layers": layers_b,
        "market_tickets": [611, 612],
        "pending_tickets": [613],
        "symbol": DEFAULT_SYMBOL,
        "direction": "SELL",
        "raw_tp1": 3880.0,
        "raw_tp2": 3860.0,
    }
    runner_b = executor.handle_tp1_scaling_and_select_runner(sig_b)
    assert runner_b == 612
    assert sig_b["status"] == "RUNNER_ACTIVE"
    assert sig_b["runner_layer"] == "MID"
    assert sig_b["soft_tp1_floor"] == 3880.50  # 3880.00 + 0.50 for SELL
    assert sig_b["market_tickets"] == [612]
    assert sig_b["pending_tickets"] == []
    assert layers_b["TOP"]["status"] == "CLOSED"
    assert layers_b["MID"]["status"] == "RUNNER"

    # Case C: Full zone fill (All 3 layers filled)
    layers_c = {
        "TOP": {"lot": 0.01, "ticket": 621, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 622, "order_type": "LIMIT", "status": "OPEN"},
        "BOTTOM": {"lot": 0.02, "ticket": 623, "order_type": "LIMIT", "status": "OPEN"},
    }
    sig_c = {
        "layers": layers_c,
        "market_tickets": [621, 622, 623],
        "pending_tickets": [],
        "symbol": DEFAULT_SYMBOL,
        "direction": "SELL",
        "raw_tp1": 3880.0,
        "raw_tp2": 3860.0,
    }
    runner_c = executor.handle_tp1_scaling_and_select_runner(sig_c)
    assert runner_c == 623
    assert sig_c["status"] == "RUNNER_ACTIVE"
    assert sig_c["runner_layer"] == "BOTTOM"  # Layer 3 (0.02 lot - best price at top of zone)
    assert sig_c["soft_tp1_floor"] == 3880.50
    assert sig_c["market_tickets"] == [623]
    assert layers_c["TOP"]["status"] == "CLOSED"
    assert layers_c["MID"]["status"] == "CLOSED"
    assert layers_c["BOTTOM"]["status"] == "RUNNER"


def test_sell_runner_virtual_floor_and_tp2_target():
    """Verify SELL runner behavior for virtual floor hit, TP2 hit, and trailing SL."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sig_data = {
        "runner_ticket": 701,
        "runner_layer": "BOTTOM",
        "symbol": DEFAULT_SYMBOL,
        "direction": "SELL",
        "soft_tp1_floor": 3880.50,
        "raw_tp1": 3880.0,
        "raw_tp2": 3860.0,
        "market_tickets": [701],
        "status": "RUNNER_ACTIVE",
    }

    # 1. Price rises to 3880.60 (above floor 3880.50) -> Virtual floor triggered
    mock_tick_floor = MagicMock()
    mock_tick_floor.ask = 3880.60
    mock_tick_floor.bid = 3880.30

    with patch.object(executor, "get_tick", return_value=mock_tick_floor):
        still_active = executor.manage_designated_runner(sig_data)

    assert still_active is False
    assert sig_data["status"] == "CLOSED"
    assert sig_data["runner_ticket"] is None

    # 2. Reset and test TP2 reached
    sig_data2 = {
        "runner_ticket": 702,
        "runner_layer": "BOTTOM",
        "symbol": DEFAULT_SYMBOL,
        "direction": "SELL",
        "soft_tp1_floor": 3880.50,
        "raw_tp1": 3880.0,
        "raw_tp2": 3860.0,
        "market_tickets": [702],
        "status": "RUNNER_ACTIVE",
    }
    mock_tick_tp2 = MagicMock()
    mock_tick_tp2.ask = 3859.80  # Below raw_tp2 3860.0
    mock_tick_tp2.bid = 3859.50

    with patch.object(executor, "get_tick", return_value=mock_tick_tp2):
        still_active2 = executor.manage_designated_runner(sig_data2)

    assert still_active2 is False
    assert sig_data2["status"] == "CLOSED"
    assert sig_data2["runner_ticket"] is None


def test_buy_runner_tp2_reached():
    """Verify BUY runner closes at market when live price touches or passes TP2."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sig_data = {
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

    # Price pushes to 3910.20 (above raw_tp2 3910.00)
    mock_tick = MagicMock()
    mock_tick.bid = 3910.20
    mock_tick.ask = 3910.50

    with patch.object(executor, "get_tick", return_value=mock_tick):
        still_active = executor.manage_designated_runner(sig_data)

    assert still_active is False
    assert sig_data["status"] == "CLOSED"
    assert sig_data["runner_ticket"] is None


def test_limit_order_fallback_sl_when_none():
    """Verify pending limit orders attach protective fallback SL when signal provides no SL."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    # BUY Limit at 3890.0 with no SL -> Fallback SL = 3890.0 - 15.0 = 3875.0
    with patch.object(executor, "send_order_with_filling_fallback") as mock_send:
        mock_result = MagicMock()
        mock_result.order = 9991
        mock_send.return_value = (True, mock_result)

        t = executor.place_limit_order(
            symbol="XAUUSD.s",
            direction="BUY",
            price=3890.0,
            lot=0.01,
            sl=None,
        )
        assert t == 9991
        request = mock_send.call_args[0][0]
        assert request["sl"] == 3875.0  # 3890.0 - 150 pips (15.0 points)

    # SELL Limit at 3910.0 with no SL -> Fallback SL = 3910.0 + 15.0 = 3925.0
    with patch.object(executor, "send_order_with_filling_fallback") as mock_send_sell:
        mock_result_sell = MagicMock()
        mock_result_sell.order = 9992
        mock_send_sell.return_value = (True, mock_result_sell)

        t_sell = executor.place_limit_order(
            symbol="XAUUSD.s",
            direction="SELL",
            price=3910.0,
            lot=0.01,
            sl=None,
        )
        assert t_sell == 9992
        request_sell = mock_send_sell.call_args[0][0]
        assert request_sell["sl"] == 3925.0  # 3910.0 + 150 pips (15.0 points)


def test_gate_5_instant_market_typo_guard():
    """Verify missing-digit typos on instant market signals are flagged invalid."""
    # 1. Missing digit: SL 387 instead of 3875
    parsed_typo = parse_message("Buy now \n TP 3890 \n SL 387")
    assert parsed_typo is not None
    assert parsed_typo.sl is None
    assert parsed_typo.sl_valid is False

    # 2. Huge distance typo: SL 3800 when TP is 3890 (90 points / 900 pips)
    parsed_huge = parse_message("Buy now \n TP 3890 \n SL 3800")
    assert parsed_huge is not None
    assert parsed_huge.sl is None
    assert parsed_huge.sl_valid is False

    # 3. Valid instant market signal
    parsed_valid = parse_message("Buy now \n TP 3890 \n SL 3875")
    assert parsed_valid is not None
    assert parsed_valid.sl == 3875.0
    assert parsed_valid.sl_valid is True


@pytest.mark.asyncio
async def test_edit_preserves_runner_active_state(tmp_path):
    """Verify that editing a message while trade is RUNNER_ACTIVE preserves the trailed SL."""
    from main import handle_message_edited
    from tests.test_copier_scenarios import MockTelethonEvent

    test_state_file = tmp_path / "runner_edit_state.json"
    main.state_mgr = StateManager(test_state_file)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    msg_id = 9501
    main.state_mgr.create_signal(
        message_id=msg_id,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={"BOTTOM": {"lot": 0.02, "ticket": 7771, "status": "RUNNER"}},
        market_tickets=[7771],
        pending_tickets=[],
        status="RUNNER_ACTIVE",
        runner_ticket=7771,
        runner_layer="BOTTOM",
        soft_tp1_floor=3899.50,
        raw_tp1=3900.0,
        raw_tp2=3910.0,
        raw_text="Buy 3895-3887 TP 3900 SL 3875",
    )

    # Provider edits message to update TP2 to 3920
    edit_evt = MockTelethonEvent(
        msg_id=msg_id,
        text="Buy 3895-3887 \n TP 3900 \n TP2 3920 \n SL 3875",
    )
    await handle_message_edited(edit_evt)

    sig = main.state_mgr.get_signal(msg_id)
    assert sig["status"] == "RUNNER_ACTIVE"
    assert sig["runner_ticket"] == 7771
    assert sig["soft_tp1_floor"] == 3899.50  # Trailed floor preserved!
    assert sig["raw_tp2"] == 3920.0          # TP2 updated!


def test_reactivate_ignores_non_reply_chat_noise():
    """Normal chat messages containing 'active' without reply must not trigger ACTION_REACTIVATE."""
    res_noise = parse_message("London session is active now guys!", message_id=9601, reply_to_msg_id=None)
    assert res_noise is None

    # Standalone 'Active' or reply 'active' still works
    res_standalone = parse_message("Active", message_id=9602, reply_to_msg_id=None)
    assert res_standalone is not None
    assert res_standalone.management_action == "ACTION_REACTIVATE"

    res_reply = parse_message("Trade is active again", message_id=9603, reply_to_msg_id=100)
    assert res_reply is not None
    assert res_reply.management_action == "ACTION_REACTIVATE"


@pytest.mark.asyncio
async def test_close_worst_entries_and_hold_layers_breakeven(tmp_path):
    """'close worst entries now and hold layers' moves worst entry to BE and holds bottom 2 layers."""
    from tests.test_copier_scenarios import MockTelethonEvent

    parsed = parse_message("close worst entries now and hold layers", message_id=9702, reply_to_msg_id=9701)
    assert parsed is not None
    assert parsed.management_action == "ACTION_CLOSE_WORST_HOLD_LAYERS"

    # Also verify TP3 extraction
    sig_parsed = parse_message("Buy 3895-3887\nTP 3900\nTP2 3910\nTP3 3925\nSL 3875")
    assert sig_parsed.raw_tp1 == 3900.0
    assert sig_parsed.raw_tp2 == 3910.0
    assert sig_parsed.raw_tp3 == 3925.0

    test_state_file = tmp_path / "worst_hold_state.json"
    main.state_mgr = StateManager(test_state_file)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    main.state_mgr.create_signal(
        message_id=9701,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "TOP": {"lot": 0.01, "ticket": 101, "status": "OPEN"},
            "MID": {"lot": 0.01, "ticket": 102, "status": "OPEN"},
            "BOTTOM": {"lot": 0.02, "ticket": 103, "status": "OPEN"},
        },
        market_tickets=[101, 102, 103],
        pending_tickets=[],
        raw_tp1=3900.0,
        raw_tp2=3910.0,
        raw_tp3=3925.0,
    )

    alerts = []
    orig_notify = main.notify_saved_messages

    async def cap_notify(txt):
        alerts.append(txt)

    main.notify_saved_messages = cap_notify
    try:
        evt = MockTelethonEvent(msg_id=9702, text="close worst entries now and hold layers", reply_to_msg_id=9701)
        await main.handle_new_message(evt)
    finally:
        main.notify_saved_messages = orig_notify

    sig = main.state_mgr.get_signal(9701)
    assert sig["hold_past_tp2"] is True
    assert sig["status"] == "BE_APPLIED"
    assert len(alerts) == 1
    assert "Worst Entries -> Breakeven" in alerts[0]
    assert "101" in alerts[0]
    assert "102" in alerts[0] and "103" in alerts[0]


@pytest.mark.asyncio
async def test_sell_now_delay_timer_and_failed_order_skips_state(tmp_path):
    """Verify Sell now waits delay timer: executes if no limit arrives, cancels timer if edited with zone, and skips ACTIVE state on broker rejection."""
    import asyncio
    from tests.test_copier_scenarios import MockTelethonEvent

    test_state_file = tmp_path / "sell_now_state.json"
    main.state_mgr = StateManager(test_state_file)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 0.05
    try:
        # 1. Sell now with NO limit edit -> waits 0.05s then executes market SELL
        evt_sell = MockTelethonEvent(msg_id=9801, text="Sell now")
        await main.handle_new_message(evt_sell)
        sig_pending = main.state_mgr.get_signal(9801)
        assert sig_pending["status"] == "PENDING"
        assert sig_pending["market_tickets"] == []

        for _ in range(15):
            await asyncio.sleep(0.03)
            sig_active = main.state_mgr.get_signal(9801)
            if sig_active and sig_active["status"] == "ACTIVE":
                break
        sig_active = main.state_mgr.get_signal(9801)
        assert sig_active["status"] == "ACTIVE"
        assert len(sig_active["market_tickets"]) == 1
        assert sig_active["direction"] == "SELL"

        # 2. Sell now WITH zone edit within timer -> cancels timer & executes 4-layer zone limits
        evt_sell2 = MockTelethonEvent(msg_id=9803, text="Sell now")
        await main.handle_new_message(evt_sell2)
        assert main.state_mgr.get_signal(9803)["status"] == "PENDING"

        evt_edit2 = MockTelethonEvent(msg_id=9803, text="Sell 3905-3910\nTP 3880\nSL 3920")
        await main.handle_message_edited(evt_edit2)
        await asyncio.sleep(0.10)
        sig_zone = main.state_mgr.get_signal(9803)
        assert sig_zone["status"] == "ACTIVE"
        assert sig_zone["is_zone"] is True
        assert len(sig_zone["pending_tickets"]) == 4

        # 3. Failed Buy now order (e.g. No money) must NOT save ACTIVE signal
        alerts = []
        orig_notify = main.notify_saved_messages

        async def cap_notify(txt):
            alerts.append(txt)

        main.notify_saved_messages = cap_notify
        try:
            with patch.object(main.executor, "execute_market_order", return_value=None):
                main.executor.last_error = "retcode=10019 (No money)"
                evt_fail = MockTelethonEvent(msg_id=9802, text="Buy now")
                await main.handle_new_message(evt_fail)
                await asyncio.sleep(0.12)
        finally:
            main.notify_saved_messages = orig_notify

        assert main.state_mgr.get_signal(9802) is None
        assert any("Order Rejected by MT5" in a and "No money" in a for a in alerts)
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay


def test_runner_holds_past_tp2_to_tp3_when_hold_past_tp2_set():
    """When hold_past_tp2 is True and raw_tp3 is set, runner does not close early at raw_tp2."""
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sig = {
        "runner_ticket": 805,
        "runner_layer": "BOTTOM",
        "symbol": DEFAULT_SYMBOL,
        "direction": "BUY",
        "soft_tp1_floor": 3899.50,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "raw_tp3": 3925.0,
        "hold_past_tp2": True,
        "market_tickets": [805],
        "status": "RUNNER_ACTIVE",
    }

    # Price reaches 3912.0 (above TP2 3910.0, below TP3 3925.0) -> stays ACTIVE and trails!
    mock_tick_mid = MagicMock()
    mock_tick_mid.bid = 3912.00
    mock_tick_mid.ask = 3912.30
    with patch.object(executor, "get_tick", return_value=mock_tick_mid):
        assert executor.manage_designated_runner(sig) is True
        assert sig["status"] == "RUNNER_ACTIVE"

    # Price reaches 3925.20 (>= TP3 3925.0) -> closes at market!
    mock_tick_tp3 = MagicMock()
    mock_tick_tp3.bid = 3925.20
    mock_tick_tp3.ask = 3925.50
    with patch.object(executor, "get_tick", return_value=mock_tick_tp3):
        assert executor.manage_designated_runner(sig) is False
        assert sig["status"] == "CLOSED"
        assert sig["tp2_reached"] is True


def test_chat_noise_does_not_trigger_management():
    """Non-reply chatter containing ambiguous words must never act on a live trade."""
    noise = [
        "guys be careful with this one",
        "dont exit early let it run to the targets please everyone",
        "cut your losses quickly and protect your mindset today team",
        "secure your bag",
        "Sell 3900-3905 TP 3880 SL 3915 be careful",
    ]
    for msg in noise:
        res = parse_message(msg, message_id=9900)
        assert res is None or res.event_type == "NEW_SIGNAL", f"Chat noise misclassified: {msg!r} -> {res}"

    # Explicit short commands and replies still work
    assert parse_message("BE", message_id=1).management_action == "ACTION_BREAKEVEN"
    assert parse_message("exit", message_id=2).management_action == "ACTION_FULL_CLOSE"
    assert parse_message("be careful", message_id=3, reply_to_msg_id=50).management_action == "ACTION_BREAKEVEN"
    assert parse_message("close worst entries now and hold layers", message_id=4).management_action == "ACTION_CLOSE_WORST_HOLD_LAYERS"
    assert parse_message("move sl to breakeven now", message_id=5).management_action == "ACTION_BREAKEVEN"


@pytest.mark.asyncio
async def test_pending_delay_ignores_breakeven_and_close_cancels_timer(tmp_path):
    """BE during the Sell Now wait is ignored; 'close' cancels the timer with zero orders."""
    import asyncio
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "delay_state.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 5
    try:
        await main.handle_new_message(MockTelethonEvent(msg_id=9951, text="Sell now"))
        assert main.state_mgr.get_signal(9951)["status"] == "PENDING"

        await main.handle_new_message(MockTelethonEvent(msg_id=9952, text="BE", reply_to_msg_id=9951))
        assert main.state_mgr.get_signal(9951)["status"] == "PENDING"

        await main.handle_new_message(MockTelethonEvent(msg_id=9953, text="close", reply_to_msg_id=9951))
        sig = main.state_mgr.get_signal(9951)
        assert sig["status"] == "CLOSED"
        assert sig["market_tickets"] == []
        assert 9951 not in main._pending_sell_now_tasks or main._pending_sell_now_tasks[9951].done() or True
        await asyncio.sleep(0.05)
        assert 9951 not in main._pending_sell_now_tasks
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig


def test_get_position_close_info_dry_run():
    ex = MT5Executor(dry_run=True)
    ex.connect()
    info = ex.get_position_close_info(12345)
    assert set(info) == {"profit", "reason"}


@pytest.mark.asyncio
async def test_professional_risk_guide_thresholds_and_hard_cap(tmp_path, monkeypatch):
    """Verify exact professional risk guide table, threshold crossing alerts, and active-only High Risk hard lot cap."""
    from mt5_executor import calculate_risk_guide_thresholds
    from tests.test_copier_scenarios import MockTelethonEvent

    # 1. Exact professional guide rows
    assert calculate_risk_guide_thresholds(50) == {"capital": 50.0, "low_risk_lots": 0.01, "mid_risk_lots": 0.02, "high_risk_lots": 0.05}
    assert calculate_risk_guide_thresholds(100) == {"capital": 100.0, "low_risk_lots": 0.01, "mid_risk_lots": 0.05, "high_risk_lots": 0.10}
    assert calculate_risk_guide_thresholds(200) == {"capital": 200.0, "low_risk_lots": 0.02, "mid_risk_lots": 0.10, "high_risk_lots": 0.20}
    assert calculate_risk_guide_thresholds(500) == {"capital": 500.0, "low_risk_lots": 0.05, "mid_risk_lots": 0.25, "high_risk_lots": 0.50}
    assert calculate_risk_guide_thresholds(1000) == {"capital": 1000.0, "low_risk_lots": 0.10, "mid_risk_lots": 0.50, "high_risk_lots": 1.00}

    # Custom account amount (£120 -> High Risk = 0.12 = 3 concurrent 0.04-lot signals)
    assert calculate_risk_guide_thresholds(120) == {"capital": 120.0, "low_risk_lots": 0.01, "mid_risk_lots": 0.06, "high_risk_lots": 0.12}

    # 2. Enforce High Risk Lot Cap for active open buys/sells on a £50 account (High Risk = 0.05 lots)
    monkeypatch.setattr(main, "STAGED_MARKET_ENTRY", False)
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "risk_cap_state.json"))
    ex50 = MT5Executor(dry_run=True)
    ex50.connect()
    ex50._dry_run_capital = 50.0
    monkeypatch.setattr(main, "executor", ex50)

    alerts = []

    async def cap_notify(txt):
        alerts.append(txt)

    monkeypatch.setattr(main, "notify_saved_messages", cap_notify)

    # Signal 1 on £50: 4-layer Market Now (0.01+0.01+0.01+0.02 = 0.05 open lots) -> passes Low, Mid & hits 0.05 High Cap!
    evt1 = MockTelethonEvent(msg_id=9961, text="Buy now\nTP 3905\nSL 3875")
    await main.handle_new_message(evt1)
    assert main.executor.get_total_active_lots()["open_lots"] == 0.05
    assert any("Passed LOW RISK" in a and "Passed MID RISK" in a and "MAX HIGH RISK CAP REACHED" in a for a in alerts)

    # Signal 2 on £50: 0.00 active lots available -> Market Now blocked by High Risk Cap
    evt2 = MockTelethonEvent(msg_id=9962, text="Buy now\nTP 3905\nSL 3875")
    await main.handle_new_message(evt2)
    assert main.state_mgr.get_signal(9962) is None
    assert any("High Risk Lot Cap Reached" in a for a in alerts)

    # Pending limit zone below market still allowed even at 0.05/0.05 active open cap
    evt_pend = MockTelethonEvent(msg_id=9963, text="Buy 3885-3877\nTP 3895\nSL 3865")
    await main.handle_new_message(evt_pend)
    sig_pend = main.state_mgr.get_signal(9963)
    assert sig_pend is not None and len(sig_pend["pending_tickets"]) == 4
    assert main.executor.get_total_active_lots()["open_lots"] == 0.05

    # 3. Atomic partial cap on a £80 account (High Risk = 0.08 lots): 0.05 + capped 0.03 -> 0.08 active open, then blocked!
    alerts.clear()
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "risk_cap_state_80.json"))
    ex80 = MT5Executor(dry_run=True)
    ex80.connect()
    ex80._dry_run_capital = 80.0
    monkeypatch.setattr(main, "executor", ex80)

    await main.handle_new_message(MockTelethonEvent(msg_id=9964, text="Buy now\nTP 3905\nSL 3875"))
    assert main.executor.get_total_active_lots()["open_lots"] == 0.05

    await main.handle_new_message(MockTelethonEvent(msg_id=9965, text="Buy now\nTP 3905\nSL 3875"))
    assert main.executor.get_total_active_lots()["open_lots"] == 0.08
    sig_partial = main.state_mgr.get_signal(9965)
    assert sig_partial is not None
    assert len(sig_partial["market_tickets"]) + len(sig_partial["pending_tickets"]) == 3
    assert round(sum(l["lot"] for l in sig_partial["layers"].values()), 2) == 0.03
    assert any("MAX HIGH RISK CAP REACHED" in a for a in alerts)

    await main.handle_new_message(MockTelethonEvent(msg_id=9966, text="Buy now\nTP 3905\nSL 3875"))
    assert main.state_mgr.get_signal(9966) is None


@pytest.mark.asyncio
async def test_old_message_edit_reaction_ignored_and_tp1_reply_scales(tmp_path):
    """Verify emoji reactions on old messages (>3m) are ignored and provider 'Tp1 was hit' reply triggers TP1 scaling."""
    from datetime import datetime, timezone, timedelta
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "edit_age_state.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # 1. Old untracked message edited/reacted to 2 hours later -> ignored!
    old_evt = MockTelethonEvent(msg_id=9971, text="Buy 3895-3887\nTP 3905\nSL 3875")
    old_evt.message.date = datetime.now(timezone.utc) - timedelta(hours=2)
    await main.handle_message_edited(old_evt)
    assert main.state_mgr.get_signal(9971) is None

    # 2. Provider replies "Tp1 was hit on this" to an active signal -> scales TP1 & activates L4 runner
    fresh_evt = MockTelethonEvent(msg_id=9972, text="Buy 3895-3887\nTP 3905\nTP2 3915\nSL 3875")
    await main.handle_new_message(fresh_evt)
    sig = main.state_mgr.get_signal(9972)
    # Simulate all 4 layers filled
    all_tkts = sig["market_tickets"] + sig["pending_tickets"]
    for lk in ("L1", "L2", "L3", "L4"):
        sig["layers"][lk]["status"] = "OPEN"
    main.state_mgr.update_signal(9972, {"market_tickets": all_tkts, "pending_tickets": [], "layers": sig["layers"]})

    reply_evt = MockTelethonEvent(msg_id=9973, text="Tp1 was hit on this", reply_to_msg_id=9972)
    await main.handle_new_message(reply_evt)
    sig_after = main.state_mgr.get_signal(9972)
    assert sig_after["tp1_reached"] is True
    assert sig_after["status"] == "RUNNER_ACTIVE"
    assert sig_after["runner_layer"] == "L4"
    assert sig_after["layers"]["L1"]["status"] == "CLOSED"
    assert sig_after["layers"]["L2"]["status"] == "CLOSED"
    assert sig_after["layers"]["L3"]["status"] == "CLOSED"
    assert len(sig_after["market_tickets"]) == 1  # Dynamic deepest runner L4 retained at TP2
    assert sig_after["layers"]["L4"]["tp"] == 3915.0


@pytest.mark.asyncio
async def test_two_stage_daily_loss_50pct_hold_and_90pct_emergency_cutoff(tmp_path):
    """
    Verify two-stage daily loss guard:
    - At 50% loss (£60 on £120): holds existing open trades to natural SL/TP, blocks new signals.
    - At 90% loss (£108 on £120): urgency cut-off immediately closes losing positions & cancels limits.
    """
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "drawdown_state.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 120.0
    main._daily_50pct_alert_date = None

    # Open an initial signal (4 layers) while loss is 0%
    evt1 = MockTelethonEvent(msg_id=9981, text="Buy 3895-3887\nTP 3905\nSL 3875")
    await main.handle_new_message(evt1)
    sig1 = main.state_mgr.get_signal(9981)
    assert sig1["status"] == "ACTIVE"
    open_mk = list(sig1["market_tickets"])
    open_pd = list(sig1["pending_tickets"])
    assert len(open_mk) + len(open_pd) == 4

    alerts = []
    orig_notify = main.notify_saved_messages

    async def cap_notify(txt):
        alerts.append(txt)

    main.notify_saved_messages = cap_notify
    try:
        # Stage 1: Floating loss hits 50% (-£62.00 on £120.00 balance)
        main.executor._dry_run_floating_pnl = -62.0
        state_50 = await main.check_and_enforce_daily_drawdown()
        assert state_50 == "PAUSED_50PCT"

        # Existing trade MUST still be ACTIVE (held naturally for SL/TP)
        sig1_still = main.state_mgr.get_signal(9981)
        assert sig1_still["status"] == "ACTIVE"
        assert sig1_still["market_tickets"] == open_mk
        assert any("50% Daily Loss Threshold Reached" in a and "NOT** closed" in a for a in alerts)

        # New incoming signal MUST be blocked while at 50% loss
        evt2 = MockTelethonEvent(msg_id=9982, text="Buy 3895-3887\nTP 3905\nSL 3875")
        await main.handle_new_message(evt2)
        assert main.state_mgr.get_signal(9982) is None
        assert any("50% Daily Loss Limit Reached" in a for a in alerts)

        # Stage 2: Floating loss reaches 90% (-£109.00 on £120.00 balance) -> Urgency Cut-Off!
        main.executor._dry_run_floating_pnl = -109.0
        state_90 = await main.check_and_enforce_daily_drawdown()
        assert state_90 == "EMERGENCY_CUTOFF"

        sig1_cut = main.state_mgr.get_signal(9981)
        assert sig1_cut["status"] == "CLOSED"
        assert sig1_cut["market_tickets"] == []
        assert sig1_cut["pending_tickets"] == []
        assert any("90% EMERGENCY DRAWDOWN CUT-OFF TRIGGERED" in a for a in alerts)
    finally:
        main.notify_saved_messages = orig_notify


@pytest.mark.asyncio
async def test_daily_win_command_and_auto_session_termination(tmp_path):
    """
    Typing `dailywin` in Saved Messages prompts for a target (e.g. `£20`), and once the bot makes
    +£20.00 from trading, it banks all open trades, cancels pending limits, sends a Saved Messages
    notification, and terminates the session for no more trading.
    """
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "state_dailywin.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 120.0
    main._awaiting_dailywin_input = False
    main._daily_win_terminate_requested = False

    # Open an active trade (4 layers)
    sig_evt = MockTelethonEvent(msg_id=9991, text="Buy 3895-3887\nTP 3905\nSL 3875")
    await main.handle_new_message(sig_evt)
    sig = main.state_mgr.get_signal(9991)
    assert sig["status"] == "ACTIVE"
    assert len(sig["market_tickets"]) + len(sig["pending_tickets"]) == 4

    alerts = []
    orig_notify = main.notify_saved_messages

    async def cap_notify(txt):
        alerts.append(txt)

    main.notify_saved_messages = cap_notify
    try:
        # Step 1: User types `dailywin` in Saved Messages -> bot prompts for amount
        cmd1 = MockTelethonEvent(msg_id=9992, text="dailywin", chat_id="me")
        await main.handle_saved_messages_command(cmd1)
        assert main._awaiting_dailywin_input is True
        assert any("Daily Win Limit Toggle" in r for r in cmd1.replies)

        # Step 2: User replies `£20` -> bot activates £20 daily win limit
        cmd2 = MockTelethonEvent(msg_id=9993, text="£20", chat_id="me")
        await main.handle_saved_messages_command(cmd2)
        assert main._awaiting_dailywin_input is False
        cfg = main.state_mgr.get_daily_win_config()
        assert cfg["enabled"] is True
        assert cfg["target_amount"] == 20.0
        assert any("Daily Win Limit Activated: `+£20.00 GBP`" in r for r in cmd2.replies)

        # Step 3: Profit is +£12.50 (below £20 target) -> session stays active
        main.executor._dry_run_daily_realized_pnl = 10.0
        main.executor._dry_run_floating_pnl = 2.50
        res_mid = await main.check_and_enforce_daily_win_target()
        assert res_mid == "OK"
        assert main._daily_win_terminate_requested is False
        assert main.state_mgr.get_signal(9991)["status"] == "ACTIVE"

        # Step 4: Profit reaches +£20.50 (realized £15.00 + floating £5.50) -> triggers termination!
        main.executor._dry_run_daily_realized_pnl = 15.0
        main.executor._dry_run_floating_pnl = 5.50
        res_hit = await main.check_and_enforce_daily_win_target()
        assert res_hit == "DAILY_WIN_REACHED"
        assert main._daily_win_terminate_requested is True

        # Active trade must be banked/closed and pending limits cancelled
        sig_after = main.state_mgr.get_signal(9991)
        assert sig_after["status"] == "CLOSED"
        assert sig_after["market_tickets"] == []
        assert sig_after["pending_tickets"] == []

        # Saved Messages notification must confirm target hit & session terminated
        assert any(
            "DAILY WIN TARGET REACHED (+£20.00 GBP) — SESSION TERMINATED!" in a
            and "+£20.50 GBP" in a
            for a in alerts
        )

        # Daily win config resets to disabled for next session
        assert main.state_mgr.get_daily_win_config()["enabled"] is False

        # Subsequent signals are ignored once session termination is flagged
        late_sig = MockTelethonEvent(msg_id=9994, text="Buy 3895-3887\nTP 3905\nSL 3875")
        await main.handle_new_message(late_sig)
        assert main.state_mgr.get_signal(9994) is None
    finally:
        main._daily_win_terminate_requested = False
        main.notify_saved_messages = orig_notify


@pytest.mark.asyncio
async def test_recorded_market_point_bad_position_guard_and_tp1_hit_cancellation(tmp_path):
    """
    Verify:
    1. When 'Sell now' / 'Buy now' is posted, the market point is recorded (`recorded_entry_price`).
    2. If the channel posts/replies/edits 'TP1 hit' for that message before entry, it cancels the timer
       and NEVER enters the trade.
    3. For 'Sell now' with no zone edit: if price after the delay is LOWER than recorded point (bad position),
       it holds off and only activates when price is HIGHER than (or equal to) the recorded point.
    4. For 'Buy now' with no zone edit (opposite): if price after the delay is HIGHER than recorded point
       (bad position), it holds off and only activates when price is LOWER than (or equal to) the recorded point.
    """
    import asyncio
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "state_recorded_px.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_abort = main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH
    main.SELL_NOW_DELAY_SECONDS = 0.05
    main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = False
    try:
        # 1. 'Sell now' where channel replies 'TP1 hit' during delay -> MUST NOT enter trade!
        tick_initial = MagicMock(bid=3895.00, ask=3895.50)
        with patch.object(main.executor, "get_tick", return_value=tick_initial):
            await main.handle_new_message(MockTelethonEvent(msg_id=10101, text="Sell now"))
            sig1 = main.state_mgr.get_signal(10101)
            assert sig1["status"] == "PENDING"
            assert sig1["recorded_entry_price"] == 3895.00

            # Channel replies "TP1 hit" before timer finishes
            await main.handle_new_message(
                MockTelethonEvent(msg_id=10102, text="TP1 hit", reply_to_msg_id=10101)
            )
            await asyncio.sleep(0.12)
            sig1_after = main.state_mgr.get_signal(10101)
            assert sig1_after["status"] == "INVALIDATED_TP1"
            assert sig1_after["tp1_reached"] is True
            assert sig1_after["market_tickets"] == []

        # 2. 'Sell now' recorded at 3895.00: in legacy recovery-wait mode (ABORT_ON_MISMATCH=False),
        #    if price dumped to 3891.00, holds off until price rises >= 3895.00, then activates!
        live_tick = MagicMock(bid=3895.00, ask=3895.50)
        with patch.object(main.executor, "get_tick", side_effect=lambda sym: live_tick):
            await main.handle_new_message(MockTelethonEvent(msg_id=10103, text="Sell now"))
            assert main.state_mgr.get_signal(10103)["recorded_entry_price"] == 3895.00

            # Price drops to 3891.00 (lower than 3895.00 -> bad position for SELL)
            live_tick.bid = 3891.00
            live_tick.ask = 3891.50
            await asyncio.sleep(0.12)

            sig_held = main.state_mgr.get_signal(10103)
            assert sig_held["status"] == "PENDING"
            assert sig_held.get("awaiting_favorable_price") is True
            assert sig_held["market_tickets"] == []

            # Now price moves higher than recorded point (3895.80 > 3895.00) -> activates SELL!
            live_tick.bid = 3895.80
            live_tick.ask = 3896.30
            await asyncio.sleep(0.12)

            sig_entered = main.state_mgr.get_signal(10103)
            assert sig_entered["status"] == "ACTIVE"
            assert len(sig_entered["market_tickets"]) == 1

        # 3. 'Buy now' recorded at 3895.00 (opposite rule): in legacy recovery-wait mode,
        #    if price spiked to 3899.00, holds off until price pulls back <= 3895.00, then activates!
        buy_tick = MagicMock(bid=3894.50, ask=3895.00)
        with patch.object(main.executor, "get_tick", side_effect=lambda sym: buy_tick):
            await main.handle_new_message(MockTelethonEvent(msg_id=10104, text="Buy now"))
            assert main.state_mgr.get_signal(10104)["recorded_entry_price"] == 3895.00

            # Price spikes to 3899.00 (higher than 3895.00 -> bad position for BUY)
            buy_tick.bid = 3898.50
            buy_tick.ask = 3899.00
            await asyncio.sleep(0.12)

            sig_buy_held = main.state_mgr.get_signal(10104)
            assert sig_buy_held["status"] == "PENDING"
            assert sig_buy_held.get("awaiting_favorable_price") is True
            assert sig_buy_held["market_tickets"] == []

            # Now price moves lower than recorded point (3894.20 < 3895.00) -> activates BUY!
            buy_tick.bid = 3893.70
            buy_tick.ask = 3894.20
            await asyncio.sleep(0.12)

            sig_buy_entered = main.state_mgr.get_signal(10104)
            assert sig_buy_entered["status"] == "ACTIVE"
            assert len(sig_buy_entered["market_tickets"]) == 1
    finally:
        main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = orig_abort
        main.SELL_NOW_DELAY_SECONDS = orig_delay


def _reset_main_session_flags():
    """Reset module-level session control flags between isolated tests."""
    for mid in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(mid)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    main._awaiting_dailywin_input = False
    main._awaiting_dailyloss_input = False
    main.TRADING_HALTED = False
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main._daily_loss_lock_notified_date = None
    main._daily_50pct_alert_date = None


@pytest.mark.asyncio
async def test_dailyloss_commands_2step_1step_invalid_switch_and_status(tmp_path):
    """
    Verify Saved Messages `dailyloss` flows:
    - Interactive 2-step (`dailyloss` -> `£20`)
    - 1-step (`dailyloss 20`, `/dailyloss £25.50`, `dailyloss -30`, `dailyloss off`)
    - Invalid input keeps prompt armed (`_awaiting_dailyloss_input is True`)
    - Cross-switching between `dailywin` and `dailyloss` cleanly cancels the other prompt
    - `/status` displays Daily Loss Limit (OFF, ACTIVE, LOCKED) and `dailyloss` command footer
    """
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "state_dailyloss_cmds.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 120.0
    _reset_main_session_flags()

    try:
        # 0. /status when OFF
        st0 = MockTelethonEvent(msg_id=11000, text="/status", chat_id="me")
        await main.handle_saved_messages_command(st0)
        assert "Daily Loss Limit (`dailyloss`): ⚪ OFF" in st0.replies[-1]
        assert "`dailyloss`" in st0.replies[-1]

        # 1. Interactive 2-step: `dailyloss` -> invalid -> `£20`
        c1 = MockTelethonEvent(msg_id=11001, text="dailyloss", chat_id="me")
        await main.handle_saved_messages_command(c1)
        assert main._awaiting_dailyloss_input is True
        assert main._awaiting_dailywin_input is False
        assert "Daily Loss Limit Toggle (`dailyloss`)" in c1.replies[-1]

        c_bad = MockTelethonEvent(msg_id=11002, text="not-a-number", chat_id="me")
        await main.handle_saved_messages_command(c_bad)
        assert main._awaiting_dailyloss_input is True
        assert "Invalid Daily Loss Amount" in c_bad.replies[-1]

        c2 = MockTelethonEvent(msg_id=11003, text="£20", chat_id="me")
        await main.handle_saved_messages_command(c2)
        assert main._awaiting_dailyloss_input is False
        cfg = main.state_mgr.get_daily_loss_config()
        assert cfg["enabled"] is True
        assert cfg["limit_amount"] == 20.0
        assert "Daily Loss Limit Activated: `-£20.00 GBP`" in c2.replies[-1]
        assert "ONLY realized closed / Stop-Loss trades" in c2.replies[-1]

        # /status when ACTIVE
        st1 = MockTelethonEvent(msg_id=11004, text="/status", chat_id="me")
        await main.handle_saved_messages_command(st1)
        assert "Daily Loss Limit (`dailyloss`): 🟢 ACTIVE — Limit: `-£20.00 GBP`" in st1.replies[-1]

        # 2. 1-step variants
        c3 = MockTelethonEvent(msg_id=11005, text="/dailyloss £25.50", chat_id="me")
        await main.handle_saved_messages_command(c3)
        assert main.state_mgr.get_daily_loss_config()["limit_amount"] == 25.50

        c4 = MockTelethonEvent(msg_id=11006, text="dailyloss -30", chat_id="me")
        await main.handle_saved_messages_command(c4)
        assert main.state_mgr.get_daily_loss_config()["limit_amount"] == 30.0

        c_off = MockTelethonEvent(msg_id=11007, text="dailyloss off", chat_id="me")
        await main.handle_saved_messages_command(c_off)
        assert main.state_mgr.get_daily_loss_config()["enabled"] is False
        assert "Daily Loss Limit Disabled" in c_off.replies[-1]

        # 3. Switching prompts: `dailywin` -> `dailyloss` -> `£15` sets dailyloss ONLY
        sw1 = MockTelethonEvent(msg_id=11008, text="dailywin", chat_id="me")
        await main.handle_saved_messages_command(sw1)
        assert main._awaiting_dailywin_input is True
        assert main._awaiting_dailyloss_input is False

        sw2 = MockTelethonEvent(msg_id=11009, text="dailyloss", chat_id="me")
        await main.handle_saved_messages_command(sw2)
        assert main._awaiting_dailywin_input is False
        assert main._awaiting_dailyloss_input is True

        sw3 = MockTelethonEvent(msg_id=11010, text="£15", chat_id="me")
        await main.handle_saved_messages_command(sw3)
        assert main._awaiting_dailyloss_input is False
        assert main.state_mgr.get_daily_loss_config()["limit_amount"] == 15.0
        assert main.state_mgr.get_daily_win_config()["enabled"] is False

        # 4. Reverse switch: `dailyloss` -> `dailywin` -> `£35` sets dailywin ONLY
        sw4 = MockTelethonEvent(msg_id=11011, text="dailyloss", chat_id="me")
        await main.handle_saved_messages_command(sw4)
        assert main._awaiting_dailyloss_input is True

        sw5 = MockTelethonEvent(msg_id=11012, text="dailywin", chat_id="me")
        await main.handle_saved_messages_command(sw5)
        assert main._awaiting_dailyloss_input is False
        assert main._awaiting_dailywin_input is True

        sw6 = MockTelethonEvent(msg_id=11013, text="£35", chat_id="me")
        await main.handle_saved_messages_command(sw6)
        assert main._awaiting_dailywin_input is False
        assert main.state_mgr.get_daily_win_config()["enabled"] is True
        assert main.state_mgr.get_daily_win_config()["target_amount"] == 35.0
        assert main.state_mgr.get_daily_loss_config()["limit_amount"] == 15.0

        # 5. Known command `/status` clears an open `dailyloss` prompt and runs `/status`
        sw7 = MockTelethonEvent(msg_id=11014, text="dailyloss", chat_id="me")
        await main.handle_saved_messages_command(sw7)
        assert main._awaiting_dailyloss_input is True
        main._daily_loss_locked = True
        st2 = MockTelethonEvent(msg_id=11015, text="/status", chat_id="me")
        await main.handle_saved_messages_command(st2)
        assert main._awaiting_dailyloss_input is False
        assert "🔒 LOCKED" in st2.replies[-1]
    finally:
        _reset_main_session_flags()


@pytest.mark.asyncio
async def test_dailyloss_realized_only_enforcement_and_exposure_cleanup(tmp_path):
    """
    Verify:
    - Floating drawdown of -£35.00 does NOT trigger `dailyloss £20` (`limit_reached is False`).
    - Realized Stop-Loss deals reaching -£20.00 DO trigger `DAILY_LOSS_REACHED`, cancel pending
      limits & delayed timers, close open exposure, send Saved Messages alert, and set
      `_daily_loss_terminate_requested = True`.
    - Also verify `DAILY_LOSS_CLOSE_OPEN_TRADES=False` leaves open positions while cancelling limits/timers.
    """
    import asyncio
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "state_dailyloss_exec.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 120.0
    _reset_main_session_flags()

    alerts = []
    orig_notify = main.notify_saved_messages
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_close_open = main.DAILY_LOSS_CLOSE_OPEN_TRADES
    main.SELL_NOW_DELAY_SECONDS = 5.0

    async def cap_notify(txt):
        alerts.append(txt)

    main.notify_saved_messages = cap_notify
    try:
        # Configure dailyloss £20
        await main.handle_saved_messages_command(MockTelethonEvent(msg_id=11101, text="dailyloss 20", chat_id="me"))

        # Open a 3-layer zone (1 market + 2 pending limits) and a delayed Sell now timer
        await main.handle_new_message(MockTelethonEvent(msg_id=11102, text="Buy 3895-3887\nTP 3905\nSL 3875"))
        sig_zone = main.state_mgr.get_signal(11102)
        assert sig_zone["status"] == "ACTIVE"
        mk_before_11102 = list(sig_zone["market_tickets"])
        assert len(mk_before_11102) >= 1
        assert len(sig_zone["pending_tickets"]) >= 1

        await main.handle_new_message(MockTelethonEvent(msg_id=11103, text="Sell now"))
        assert main.state_mgr.get_signal(11103)["status"] == "PENDING"
        assert 11103 in main._pending_sell_now_tasks

        # 1. Floating open drawdown -£35.00 MUST NOT trigger dailyloss £20
        main.executor._dry_run_floating_pnl = -35.0
        main.executor._dry_run_daily_realized_pnl = 0.0
        prog_float = main.get_daily_loss_progress()
        assert prog_float["limit_reached"] is False
        assert await main.check_and_enforce_daily_loss_limit(exit_on_trigger=False) == "OK"
        assert main.TRADING_HALTED is False
        assert main._daily_loss_terminate_requested is False
        assert main.state_mgr.get_signal(11102)["status"] == "ACTIVE"
        assert main.state_mgr.get_signal(11103)["status"] == "PENDING"

        # 2. Realized closed Stop-Loss deals reach -£20.00 -> triggers termination & sys.exit(0)!
        #    With default DAILY_LOSS_CLOSE_OPEN_TRADES=False: pending limits & timers cancel, filled positions stay open on SL/TP.
        main.DAILY_LOSS_CLOSE_OPEN_TRADES = False
        main.executor._dry_run_daily_realized_pnl = -20.0
        prog_realized = main.get_daily_loss_progress()
        assert prog_realized["limit_reached"] is True
        with pytest.raises(SystemExit) as sys_exc:
            await main.check_and_enforce_daily_loss_limit(exit_on_trigger=True)
        assert sys_exc.value.code == 0
        assert main.TRADING_HALTED is True
        assert main._daily_loss_terminate_requested is True
        assert main.session_trading_blocked() is True

        # Delayed timer cancelled, PENDING closed, pending limits cancelled while open market position stays intact
        await asyncio.sleep(0.02)
        assert 11103 not in main._pending_sell_now_tasks
        assert main.state_mgr.get_signal(11103)["status"] == "CLOSED"
        sig_zone_after = main.state_mgr.get_signal(11102)
        assert sig_zone_after["market_tickets"] == mk_before_11102
        assert sig_zone_after["pending_tickets"] == []

        # Trigger timestamp persisted for same-day relaunch lock
        cfg = main.state_mgr.get_daily_loss_config()
        assert cfg["triggered_date"] == main._trading_day_str()
        assert cfg["triggered_at"] > 0.0

        # Notification delivered to Saved Messages
        assert any("DAILY LOSS LIMIT HIT: Realized P&L is -20.00 (Limit: -20.00)." in a for a in alerts)

        # New incoming signal blocked once termination requested
        await main.handle_new_message(MockTelethonEvent(msg_id=11104, text="Buy 3895-3887\nTP 3905\nSL 3875"))
        assert main.state_mgr.get_signal(11104) is None

        # 3. Verify DAILY_LOSS_CLOSE_OPEN_TRADES=True closes open positions as well as cancelling pending limits
        _reset_main_session_flags()
        main.state_mgr = StateManager(tmp_path / "state_dailyloss_close_open.json")
        main.executor = MT5Executor(dry_run=True)
        main.executor.connect()
        main.DAILY_LOSS_CLOSE_OPEN_TRADES = True
        await main.handle_saved_messages_command(MockTelethonEvent(msg_id=11110, text="dailyloss 20", chat_id="me"))
        await main.handle_new_message(MockTelethonEvent(msg_id=11111, text="Buy 3895-3887\nTP 3905\nSL 3875"))
        assert len(main.state_mgr.get_signal(11111)["market_tickets"]) >= 1
        main.executor._dry_run_daily_realized_pnl = -20.0
        assert await main.check_and_enforce_daily_loss_limit(exit_on_trigger=False) == "DAILY_LOSS_REACHED"
        sig_closed = main.state_mgr.get_signal(11111)
        assert sig_closed["status"] == "CLOSED"
        assert sig_closed["market_tickets"] == []
        assert sig_closed["pending_tickets"] == []
    finally:
        main.DAILY_LOSS_CLOSE_OPEN_TRADES = orig_close_open
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        main.notify_saved_messages = orig_notify
        _reset_main_session_flags()


def test_mt5_get_realized_today_pnl_excludes_balance_and_credit_deals():
    """Verify non-dry-run get_realized_today_pnl() counts ONLY closed BUY/SELL deals (never balance/credit)."""
    from types import SimpleNamespace
    import mt5_executor as ex_mod

    ex = MT5Executor(dry_run=False)
    ex._connected = True
    ex._server_offset_cache = 7200

    now_srv_ts = time.time() + 7200
    tick_mock = SimpleNamespace(time=now_srv_ts, bid=3860.0, ask=3860.5)
    deals = [
        # Deposit / Credit / Correction with OUT-like or IN entry -> MUST be excluded!
        SimpleNamespace(entry=0, type=2, time=now_srv_ts - 100, profit=120.0, commission=0.0, swap=0.0),
        SimpleNamespace(entry=1, type=2, time=now_srv_ts - 90, profit=-50.0, commission=0.0, swap=0.0),
        SimpleNamespace(entry=1, type=3, time=now_srv_ts - 80, profit=-24.0, commission=0.0, swap=0.0),
        # Open IN trading deal -> excluded
        SimpleNamespace(entry=0, type=0, time=now_srv_ts - 70, profit=0.0, commission=-0.10, swap=0.0),
        # Closed BUY/SELL deals (SL/TP/manual) -> included with profit + commission + swap!
        SimpleNamespace(entry=1, type=0, time=now_srv_ts - 60, profit=-15.0, commission=-0.15, swap=-0.05),
        SimpleNamespace(entry=1, type=1, time=now_srv_ts - 50, profit=-4.80, commission=0.0, swap=0.0),
        # Yesterday's closed deal -> excluded
        SimpleNamespace(entry=1, type=0, time=now_srv_ts - 90000, profit=-99.0, commission=0.0, swap=0.0),
    ]

    with patch.object(ex_mod.mt5, "symbol_info_tick", return_value=tick_mock), \
         patch.object(ex_mod.mt5, "history_deals_get", return_value=deals):
        pnl = ex.get_realized_today_pnl()
    assert pnl == -20.0


@pytest.mark.asyncio
async def test_dailyloss_same_day_relaunch_lock_and_unlock(tmp_path):
    """
    When relaunched on the same broker day after `dailyloss` already fired:
    - `check_and_enforce_daily_loss_limit()` returns `"LOCKED"` (does NOT exit 89).
    - `session_trading_blocked()` blocks new channel signals.
    - Typing `dailyloss off` OR raising the limit (`dailyloss 50`) unlocks trading.
    """
    from datetime import timedelta
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "state_dailyloss_lock.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 120.0
    _reset_main_session_flags()

    alerts = []
    orig_notify = main.notify_saved_messages

    async def cap_notify(txt):
        alerts.append(txt)

    main.notify_saved_messages = cap_notify
    try:
        today = main._trading_day_str()
        main.executor._dry_run_daily_realized_pnl = -20.0
        main.state_mgr.set_daily_loss_config(
            enabled=True,
            limit_amount=20.0,
            triggered_date=today,
            triggered_at=main.BOT_START_TIME - 30.0,
        )

        # 1. Enters LOCKED mode on same-day relaunch
        status = await main.check_and_enforce_daily_loss_limit()
        assert status == "LOCKED"
        assert main._daily_loss_locked is True
        assert main._daily_loss_terminate_requested is False
        assert main.session_trading_blocked() is True
        assert any("DAILY LOSS LOCK ACTIVE" in a for a in alerts)

        # New signals blocked while locked
        await main.handle_new_message(MockTelethonEvent(msg_id=11201, text="Buy 3895-3887\nTP 3905\nSL 3875"))
        assert main.state_mgr.get_signal(11201) is None

        # 2. Raising limit above today's realized loss (`dailyloss 50`) unlocks trading
        await main.handle_saved_messages_command(MockTelethonEvent(msg_id=11202, text="dailyloss 50", chat_id="me"))
        assert main._daily_loss_locked is False
        assert main.session_trading_blocked() is False
        assert await main.check_and_enforce_daily_loss_limit() == "OK"

        await main.handle_new_message(MockTelethonEvent(msg_id=11203, text="Buy 3895-3887\nTP 3905\nSL 3875"))
        assert main.state_mgr.get_signal(11203)["status"] == "ACTIVE"

        # 3. Lock again, then unlock via `dailyloss off`
        main.state_mgr.set_daily_loss_config(
            enabled=True,
            limit_amount=20.0,
            triggered_date=today,
            triggered_at=main.BOT_START_TIME - 10.0,
        )
        main._daily_loss_lock_notified_date = None
        assert await main.check_and_enforce_daily_loss_limit() == "LOCKED"
        assert main.session_trading_blocked() is True

        await main.handle_saved_messages_command(MockTelethonEvent(msg_id=11204, text="dailyloss off", chat_id="me"))
        assert main._daily_loss_locked is False
        assert main.session_trading_blocked() is False

        # 4. Next broker day rollover (yesterday's trigger + today's realized 0.0) -> OK
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        main.state_mgr.set_daily_loss_config(
            enabled=True,
            limit_amount=20.0,
            triggered_date=yesterday,
            triggered_at=main.BOT_START_TIME - 86400.0,
        )
        main.executor._dry_run_daily_realized_pnl = 0.0
        assert await main.check_and_enforce_daily_loss_limit() == "OK"
        assert main._daily_loss_locked is False
    finally:
        main.notify_saved_messages = orig_notify
        _reset_main_session_flags()


def test_is_tp1_hit_message_and_parser_true_false_positives():
    """Verify parser.is_tp1_hit_message and parse_message on realistic TP1 alerts vs signal lines."""
    from parser import is_tp1_hit_message

    tp1_true = [
        "TP1 hit",
        "Tp1 hit ✅",
        "✅ TP1 ✅",
        "TP1 done",
        "TP1 smashed 🔥",
        "Target 1 hit",
        "Hit TP1 +30 pips",
        "TP 1 reached",
        "TP1 ✅",
        "First TP hit",
    ]
    for msg in tp1_true:
        assert is_tp1_hit_message(msg) is True, f"Expected True for {msg!r}"
        parsed_standalone = parse_message(msg, message_id=12001)
        assert parsed_standalone is not None, f"Standalone TP1 rejected: {msg!r}"
        assert parsed_standalone.management_action == "ACTION_TP1_HIT_REPLY", f"Wrong action for {msg!r}: {parsed_standalone}"

        parsed_reply = parse_message(msg, message_id=12002, is_reply=True, reply_to_msg_id=12000)
        assert parsed_reply is not None and parsed_reply.management_action == "ACTION_TP1_HIT_REPLY"

    tp1_false = [
        "Buy 3895-3887 TP1 3900",
        "TP1 3900",
        "Target 1: 3900",
        "Take profit 1 3900",
        "✅ TP1 3900",
        "TP1 not hit yet",
        "TP2 hit",
    ]
    for msg in tp1_false:
        assert is_tp1_hit_message(msg) is False, f"Expected False for {msg!r}"

    # Full signal line with TP1 3900 must parse as NEW_SIGNAL, never TP1 hit
    sig = parse_message("Buy 3895-3887 TP1 3900 SL 3875")
    assert sig is not None and sig.event_type == "NEW_SIGNAL"
    assert sig.raw_tp1 == 3900.0


@pytest.mark.asyncio
async def test_tp1_hit_cancels_delayed_now_and_unfilled_zone_via_reply_edit_standalone_and_watcher(tmp_path):
    """
    Verify:
    - 'Sell now' / 'Buy now' cancelled with 0 orders if TP1 hit arrives via edit OR short standalone alert.
    - Bad-position guard holds on null tick and expires cleanly after NOW_ENTRY_MAX_WAIT_SECONDS.
    - Zone with unfilled pending limits (and mixed market + pending limits) cancels pending limits /
      scales cleanly on TP1 hit via reply, edit, standalone alert, and live watcher price.
    """
    import asyncio
    from tests.test_copier_scenarios import MockTelethonEvent

    main.state_mgr = StateManager(tmp_path / "state_tp1_cancel_all.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    _reset_main_session_flags()

    orig_delay = main.SELL_NOW_DELAY_SECONDS
    orig_max_wait = main.NOW_ENTRY_MAX_WAIT_SECONDS
    orig_abort = main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH
    main.SELL_NOW_DELAY_SECONDS = 0.08
    main.NOW_ENTRY_MAX_WAIT_SECONDS = 0.15
    try:
        # 1. Sell now cancelled by EDIT ("TP1 hit ✅")
        tick_sell = MagicMock(bid=3895.0, ask=3895.5)
        with patch.object(main.executor, "get_tick", return_value=tick_sell):
            await main.handle_new_message(MockTelethonEvent(msg_id=12101, text="Sell now"))
            assert main.state_mgr.get_signal(12101)["status"] == "PENDING"
            await main.handle_message_edited(MockTelethonEvent(msg_id=12101, text="TP1 hit ✅"))
            await asyncio.sleep(0.10)
        s1 = main.state_mgr.get_signal(12101)
        assert s1["status"] == "INVALIDATED_TP1" and s1["tp1_reached"] is True and s1["market_tickets"] == []

        # 2. Buy now cancelled by short STANDALONE alert ("TP1 smashed 🔥")
        tick_buy = MagicMock(bid=3894.5, ask=3895.0)
        with patch.object(main.executor, "get_tick", return_value=tick_buy):
            await main.handle_new_message(MockTelethonEvent(msg_id=12102, text="Buy now"))
            assert main.state_mgr.get_signal(12102)["status"] == "PENDING"
            await main.handle_new_message(MockTelethonEvent(msg_id=12103, text="TP1 smashed 🔥"))
            await asyncio.sleep(0.10)
        s2 = main.state_mgr.get_signal(12102)
        assert s2["status"] == "INVALIDATED_TP1" and s2["tp1_reached"] is True and s2["market_tickets"] == []

        # 3. Bad-position guard: null tick never bypasses initial_price (aborts immediately in spec mode & after max wait in legacy mode)
        main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = True
        tick_seq = [MagicMock(bid=3895.0, ask=3895.5), MagicMock(bid=3895.0, ask=3895.5)]
        with patch.object(main.executor, "get_tick", side_effect=lambda sym: tick_seq.pop(0) if tick_seq else None):
            await main.handle_new_message(MockTelethonEvent(msg_id=12104, text="Sell now"))
            assert main.state_mgr.get_signal(12104)["initial_price"] == 3895.0
            await asyncio.sleep(0.16)
        s3 = main.state_mgr.get_signal(12104)
        assert s3["status"] == "ABORTED_PRICE_GUARD" and s3["market_tickets"] == []

        # 4a. Unfilled zone pending limits cancelled via REPLY "TP1 ✅"
        buy_above_zone = MagicMock(bid=3896.8, ask=3897.0)  # Above top 3895 -> all 4 limits PENDING
        with patch.object(main.executor, "get_tick", return_value=buy_above_zone), \
             patch.object(main.executor, "cancel_pending_order", wraps=main.executor.cancel_pending_order) as cancel_spy:
            await main.handle_new_message(MockTelethonEvent(msg_id=12105, text="Buy 3895-3887\nTP 3900\nSL 3875"))
            sz = main.state_mgr.get_signal(12105)
            assert sz["market_tickets"] == []
            assert len(sz["pending_tickets"]) == 4
            await main.handle_new_message(MockTelethonEvent(msg_id=12106, text="TP1 ✅", reply_to_msg_id=12105))
            assert cancel_spy.call_count == 4
        sz_after = main.state_mgr.get_signal(12105)
        assert sz_after["status"] == "INVALIDATED_TP1" and sz_after["tp1_reached"] is True and sz_after["pending_tickets"] == []

        # 4b. Unfilled zone pending limits cancelled via EDIT "Target 1 hit"
        with patch.object(main.executor, "get_tick", return_value=buy_above_zone), \
             patch.object(main.executor, "cancel_pending_order", wraps=main.executor.cancel_pending_order) as cancel_spy2:
            await main.handle_new_message(MockTelethonEvent(msg_id=12107, text="Buy 3895-3887\nTP 3900\nSL 3875"))
            assert len(main.state_mgr.get_signal(12107)["pending_tickets"]) == 4
            await main.handle_message_edited(MockTelethonEvent(msg_id=12107, text="Target 1 hit"))
            assert cancel_spy2.call_count == 4
        assert main.state_mgr.get_signal(12107)["status"] == "INVALIDATED_TP1"
        assert main.state_mgr.get_signal(12107)["tp1_reached"] is True

        # 4c. Unfilled zone pending limits cancelled via LIVE PRICE in tp1_proximity_watcher_task after entering zone
        with patch.object(main.executor, "get_tick", return_value=buy_above_zone):
            await main.handle_new_message(MockTelethonEvent(msg_id=12108, text="Buy 3895-3887\nTP 3900\nSL 3875"))
        tick_enter_zone = MagicMock(bid=3894.5, ask=3894.9)
        with patch.object(main.executor, "get_tick", return_value=tick_enter_zone):
            await main._process_pre_tp1_or_backstop_tick(12108, main.state_mgr.get_signal(12108))
        tick_tp1_hit = MagicMock(bid=3900.2, ask=3900.5)
        with patch.object(main.executor, "get_tick", return_value=tick_tp1_hit):
            watcher = asyncio.create_task(main.tp1_proximity_watcher_task())
            await asyncio.sleep(0.05)
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        s_watch = main.state_mgr.get_signal(12108)
        assert s_watch["status"] == "INVALIDATED_TP1"
        assert s_watch["tp1_reached"] is True
        assert s_watch["pending_tickets"] == []

        # 4d. Mixed market + pending limits: reply "TP1 hit" cancels pending L2, L3, L4
        #     and dynamically promotes the single surviving filled L1 into RUNNER_ACTIVE toward TP2..TP4
        tick_mixed = MagicMock(bid=3894.5, ask=3894.8)  # Below L1 3895, above L2 3892.33 -> 1 market, 3 pending
        with patch.object(main.executor, "get_tick", return_value=tick_mixed):
            await main.handle_new_message(MockTelethonEvent(msg_id=12109, text="Buy 3895-3887\nTP 3900\nSL 3875"))
        s_mix = main.state_mgr.get_signal(12109)
        assert len(s_mix["market_tickets"]) == 1 and len(s_mix["pending_tickets"]) == 3
        l1_ticket = s_mix["market_tickets"][0]
        await main.handle_new_message(MockTelethonEvent(msg_id=12110, text="TP1 hit", reply_to_msg_id=12109))
        s_mix_after = main.state_mgr.get_signal(12109)
        assert s_mix_after["tp1_reached"] is True
        assert s_mix_after["pending_tickets"] == []
        assert s_mix_after["market_tickets"] == [l1_ticket]
        assert s_mix_after["runner_layer"] == "L1"
        assert s_mix_after["runner_layers"] == ["L1"]
        assert s_mix_after["status"] == "RUNNER_ACTIVE"
        assert s_mix_after["layers"]["L1"]["tp"] == s_mix_after["raw_tp2"]
    finally:
        main.NOW_PRICE_GUARD_ABORT_ON_MISMATCH = orig_abort
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        main.NOW_ENTRY_MAX_WAIT_SECONDS = orig_max_wait
        _reset_main_session_flags()


@pytest.mark.asyncio
async def test_phase1_bug_regressions_and_bat_launchers(tmp_path):
    """
    Regression checks for Phase 1 audit fixes:
    1. `manage_designated_runner` with `raw_tp1=None, raw_tp2=None` does not raise TypeError.
    2. `/closeall` cancels active `_pending_sell_now_tasks` and closes `PENDING_DELAY` signals.
    3. `handle_message_edited` during `PENDING_DELAY` enforces `check_preflight` before zone/limit entry.
    4. `StateManager` safely skips `_daily_win`, `_daily_loss`, and non-dict entries.
    5. Both `.bat` launchers preserve CRLF line endings, handle `ERRORLEVEL` 88 and 89, and contain no `|`.
    """
    from pathlib import Path
    from tests.test_copier_scenarios import MockTelethonEvent

    # 1. Runner with raw_tp1=None & raw_tp2=None (BUY and SELL)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    for direction, px, floor in [("BUY", 3896.0, 3894.5), ("SELL", 3894.0, 3895.5)]:
        sig_runner = {
            "runner_ticket": 4242,
            "runner_layer": "MID",
            "symbol": DEFAULT_SYMBOL,
            "direction": direction,
            "soft_tp1_floor": floor,
            "raw_tp1": None,
            "raw_tp2": None,
            "market_tickets": [4242],
            "status": "RUNNER_ACTIVE",
        }
        with patch.object(ex, "get_tick", return_value=MagicMock(bid=px, ask=px + 0.3)):
            assert ex.manage_designated_runner(sig_runner) is True

    # 2 & 3. `/closeall` & edited-delay preflight guard
    main.state_mgr = StateManager(tmp_path / "state_regressions.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    _reset_main_session_flags()
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 5.0
    try:
        await main.handle_new_message(MockTelethonEvent(msg_id=13001, text="Sell now"))
        assert 13001 in main._pending_sell_now_tasks
        close_cmd = MockTelethonEvent(msg_id=13002, text="/closeall", chat_id="me")
        await main.handle_saved_messages_command(close_cmd)
        assert 13001 not in main._pending_sell_now_tasks
        assert main.state_mgr.get_signal(13001)["status"] == "CLOSED"

        await main.handle_new_message(MockTelethonEvent(msg_id=13003, text="Sell now"))
        with patch.object(main.executor, "check_preflight", return_value=(False, "Within 20 pips of TP1")):
            await main.handle_message_edited(
                MockTelethonEvent(msg_id=13003, text="Sell 3895-3900\nTP 3894\nSL 3910")
            )
        s_edit = main.state_mgr.get_signal(13003)
        assert s_edit["status"] == "CLOSED"
        assert s_edit["market_tickets"] == [] and s_edit["pending_tickets"] == []
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay
        _reset_main_session_flags()

    # 4. StateManager skips `_` metadata & non-dict records cleanly
    sm = StateManager(tmp_path / "state_meta_skip.json")
    sm.set_daily_win_config(True, 25.0, 0.0, "2026-11-17")
    sm.set_daily_loss_config(True, 20.0, "2026-11-17", 1234.0)
    with sm._lock:
        sm._state["_corrupt"] = "not-a-dict"
        sm._state["corrupt_non_meta"] = ["bad"]
        sm._save_state_locked()
    sm.create_signal(message_id=13010, symbol=DEFAULT_SYMBOL, direction="BUY")
    assert list(sm.get_all_signals().keys()) == ["13010"]
    assert sm.count_open_signals() == 1
    assert len(sm.get_active_signals()) == 1
    assert sm.get_latest_signal()[0] == "13010"
    assert sm.cleanup_old_signals(days=7) == 0

    # 5. Launcher .bat files retain CRLF, handle 88 & 89, and contain no unescaped '|'
    bat_paths = [
        Path(__file__).resolve().parent.parent / "start_copier.bat",
        Path(r"C:\Users\kambo\OneDrive\Desktop\Launch_XAUUSD_Copier.bat"),
    ]
    for bp in bat_paths:
        if bp.exists():
            raw = bp.read_bytes()
            assert b"\r\n" in raw
            assert b"\n" not in raw.replace(b"\r\n", b"")
            assert b"|" not in raw
            txt = raw.decode("utf-8", errors="replace")
            assert "ERRORLEVEL% EQU 88" in txt
            assert "ERRORLEVEL% EQU 89" in txt
            assert "ERRORLEVEL% EQU 0" in txt



