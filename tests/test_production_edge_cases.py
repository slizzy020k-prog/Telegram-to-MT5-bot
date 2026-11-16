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
        assert sig_pending["status"] == "PENDING_DELAY"
        assert sig_pending["market_tickets"] == []

        await asyncio.sleep(0.12)
        sig_active = main.state_mgr.get_signal(9801)
        assert sig_active["status"] == "ACTIVE"
        assert len(sig_active["market_tickets"]) == 1
        assert sig_active["direction"] == "SELL"

        # 2. Sell now WITH zone edit within timer -> cancels timer & executes zone limits
        evt_sell2 = MockTelethonEvent(msg_id=9803, text="Sell now")
        await main.handle_new_message(evt_sell2)
        assert main.state_mgr.get_signal(9803)["status"] == "PENDING_DELAY"

        evt_edit2 = MockTelethonEvent(msg_id=9803, text="Sell 3905-3910\nTP 3880\nSL 3920")
        await main.handle_message_edited(evt_edit2)
        await asyncio.sleep(0.10)
        sig_zone = main.state_mgr.get_signal(9803)
        assert sig_zone["status"] == "ACTIVE"
        assert sig_zone["is_zone"] is True
        assert len(sig_zone["pending_tickets"]) == 3

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
        assert main.state_mgr.get_signal(9951)["status"] == "PENDING_DELAY"

        await main.handle_new_message(MockTelethonEvent(msg_id=9952, text="BE", reply_to_msg_id=9951))
        assert main.state_mgr.get_signal(9951)["status"] == "PENDING_DELAY"

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
async def test_professional_risk_guide_thresholds_and_hard_cap(tmp_path):
    """Verify exact professional risk guide table, threshold crossing alerts, and High Risk hard lot cap."""
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

    # 2. Enforce High Risk Lot Cap on a £50 account (High Risk = 0.05 lots)
    main.state_mgr = StateManager(tmp_path / "risk_cap_state.json")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()
    main.executor._dry_run_capital = 50.0

    alerts = []
    orig_notify = main.notify_saved_messages

    async def cap_notify(txt):
        alerts.append(txt)

    main.notify_saved_messages = cap_notify
    try:
        # Signal 1: 3-layer zone (0.01 + 0.01 + 0.02 = 0.04 lots) -> passes Low (0.01) & Mid (0.02)
        evt1 = MockTelethonEvent(msg_id=9961, text="Buy 3895-3887\nTP 3905\nSL 3875")
        await main.handle_new_message(evt1)
        assert main.executor.get_total_active_lots()["total_lots"] == 0.04
        assert any("Passed LOW RISK" in a and "Passed MID RISK" in a for a in alerts)

        # Signal 2: 3-layer zone when only 0.01 lot remains under 0.05 High Risk Cap
        evt2 = MockTelethonEvent(msg_id=9962, text="Buy 3895-3887\nTP 3905\nSL 3875")
        await main.handle_new_message(evt2)
        assert main.executor.get_total_active_lots()["total_lots"] == 0.05
        assert any("MAX HIGH RISK CAP REACHED" in a for a in alerts)
        sig2 = main.state_mgr.get_signal(9962)
        assert len(sig2["market_tickets"]) + len(sig2["pending_tickets"]) == 1

        # Signal 3: 0.00 lots available -> blocked completely by High Risk Cap
        evt3 = MockTelethonEvent(msg_id=9963, text="Buy 3895-3887\nTP 3905\nSL 3875")
        await main.handle_new_message(evt3)
        assert main.state_mgr.get_signal(9963) is None
        assert any("High Risk Lot Cap Reached" in a for a in alerts)
    finally:
        main.notify_saved_messages = orig_notify


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

    # 2. Provider replies "Tp1 was hit on this" to an active signal -> scales TP1 & activates runner
    fresh_evt = MockTelethonEvent(msg_id=9972, text="Buy 3895-3887\nTP 3905\nTP2 3915\nSL 3875")
    await main.handle_new_message(fresh_evt)
    sig = main.state_mgr.get_signal(9972)
    # Simulate all 3 layers filled
    all_tkts = sig["market_tickets"] + sig["pending_tickets"]
    main.state_mgr.update_signal(9972, {"market_tickets": all_tkts, "pending_tickets": []})

    reply_evt = MockTelethonEvent(msg_id=9973, text="Tp1 was hit on this", reply_to_msg_id=9972)
    await main.handle_new_message(reply_evt)
    sig_after = main.state_mgr.get_signal(9972)
    assert sig_after["tp1_reached"] is True
    assert sig_after["status"] == "RUNNER_ACTIVE"
    assert sig_after["runner_layer"] == "BOTTOM"


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

    # Open an initial signal (3 layers) while loss is 0%
    evt1 = MockTelethonEvent(msg_id=9981, text="Buy 3895-3887\nTP 3905\nSL 3875")
    await main.handle_new_message(evt1)
    sig1 = main.state_mgr.get_signal(9981)
    assert sig1["status"] == "ACTIVE"
    open_mk = list(sig1["market_tickets"])
    open_pd = list(sig1["pending_tickets"])
    assert len(open_mk) + len(open_pd) == 3

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

    # Open an active trade (3 layers)
    sig_evt = MockTelethonEvent(msg_id=9991, text="Buy 3895-3887\nTP 3905\nSL 3875")
    await main.handle_new_message(sig_evt)
    sig = main.state_mgr.get_signal(9991)
    assert sig["status"] == "ACTIVE"
    assert len(sig["market_tickets"]) + len(sig["pending_tickets"]) == 3

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

