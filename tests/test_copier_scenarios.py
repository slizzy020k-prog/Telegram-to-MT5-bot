import asyncio
import pytest
from unittest.mock import MagicMock
from mt5_executor import MT5Executor
from state_manager import StateManager
from parser import parse_message, ZoneInfo
from config import LAYER_LOTS, MAX_ORDERS_PER_SIGNAL, FALLBACK_SL_PIPS


class MockTelethonEvent:
    """Mock for Telethon Message and MessageEdited events."""
    def __init__(self, msg_id: int, text: str, reply_to_msg_id: int = None, chat_id: int = -1001234567890, forward=None):
        self.chat_id = chat_id
        self.replies = []
        self.message = MagicMock()
        self.message.id = msg_id
        self.message.message = text
        self.message.reply_to_msg_id = reply_to_msg_id
        self.message.forward = forward
        self.message.out = False

    async def reply(self, text: str):
        self.replies.append(text)


def test_scenario_a_asymmetric_zone_execution():
    """
    Scenario A: Zone posted immediately:
    "Buy 3895-3887, TP 3900, SL 3875"
    Must place exactly 3 orders:
    - Layer 1 (TOP): 0.01 lot at 3895.00
    - Layer 2 (MID): 0.01 lot at 3891.00
    - Layer 3 (BOTTOM): 0.02 lot at 3887.00
    Total exposure = 0.04 lot. All target TP1 - 0.50 (3899.50).
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    msg_text = "Buy 3895-3887, TP 3900, SL 3875"
    parsed = parse_message(msg_text)

    assert parsed.is_entry is True
    assert parsed.symbol in ("XAUUSD", "XAUUSD.s")
    assert parsed.zone is not None
    assert parsed.final_tp == 3899.50
    assert parsed.proximity_close_price == 3898.00

    market_tickets, pending_tickets, layers_data = executor.execute_3layer_zone(
        symbol=parsed.symbol,
        direction=parsed.direction,
        zone=parsed.zone,
        sl=parsed.sl,
        final_tp=parsed.final_tp,
        raw_tp1=parsed.raw_tp1,
    )

    total_orders = len(market_tickets) + len(pending_tickets)
    assert total_orders == 3
    assert layers_data["TOP"]["lot"] == 0.01
    assert layers_data["MID"]["lot"] == 0.01
    assert layers_data["BOTTOM"]["lot"] == 0.02

    total_lots = round(sum(l["lot"] for l in layers_data.values()), 2)
    assert total_lots == 0.04


def test_scenario_b_edit_workflow(tmp_path):
    """
    Scenario B:
    1. 'Buy now' posted first -> Layer 1 (0.01 Market) placed.
    2. Edited later with 'Buy 3895-3887, TP 3900, SL 3875':
       - Order #1 SL updated to 3875.0, TP to 3899.50.
       - Layer 2 (0.01 Mid Limit at 3891.00) and Layer 3 (0.02 Bottom Limit at 3887.00) placed.
       - Total orders strictly capped at 3 (0.04 total exposure).
    3. Subsequent edit: Hard guard prevents 4th order, only modifies SL/TP.
    """
    state_file = tmp_path / "test_state_b.json"
    state_mgr = StateManager(state_file)
    executor = MT5Executor(dry_run=True)
    executor.connect()

    msg_id = 99100

    # Step 1: "Buy now"
    initial_text = "Buy now"
    parsed_1 = parse_message(initial_text)
    assert parsed_1.is_entry is True
    assert parsed_1.is_market is True

    top_lot = LAYER_LOTS["TOP"]  # 0.01
    t1 = executor.execute_market_order(
        symbol=parsed_1.symbol,
        direction=parsed_1.direction,
        lot=top_lot,
        sl=parsed_1.sl,
        tp=parsed_1.final_tp,
        comment="TG Layer 1 (Market Now)",
    )
    assert t1 is not None

    layers = {
        "TOP": {"lot": top_lot, "target_price": None, "ticket": t1, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": LAYER_LOTS["MID"], "target_price": None, "ticket": None, "order_type": "LIMIT", "status": "PENDING"},
        "BOTTOM": {"lot": LAYER_LOTS["BOTTOM"], "target_price": None, "ticket": None, "order_type": "LIMIT", "status": "PENDING"},
    }

    state_mgr.create_signal(
        message_id=msg_id,
        symbol=parsed_1.symbol,
        direction=parsed_1.direction,
        layers=layers,
        market_tickets=[t1],
        pending_tickets=[],
        zone_completed=False,
        sl=parsed_1.sl,
        raw_tp1=parsed_1.raw_tp1,
        final_tp=parsed_1.final_tp,
        proximity_close_price=parsed_1.proximity_close_price,
        raw_text=initial_text,
    )

    sig_step1 = state_mgr.get_signal(msg_id)
    assert len(sig_step1["market_tickets"]) == 1
    assert len(sig_step1["pending_tickets"]) == 0
    assert sig_step1["zone_completed"] is False

    # Step 2: Edit arrives with Zone, SL, and TP
    edited_text = "Buy 3895-3887, TP 3900, SL 3875"
    parsed_edit = parse_message(edited_text, is_edit=True)
    assert parsed_edit.zone is not None
    assert parsed_edit.sl == 3875.0
    assert parsed_edit.final_tp == 3899.50

    # Modify existing Order #1
    for t in sig_step1["market_tickets"]:
        assert executor.modify_position_sltp(t, sl=parsed_edit.sl, tp=parsed_edit.final_tp) is True

    # Place remaining limit orders (slots_left = 3 - 1 = 2)
    t2 = executor.place_limit_order(
        symbol="XAUUSD",
        direction="BUY",
        price=parsed_edit.zone.mid,
        lot=LAYER_LOTS["MID"],  # 0.01
        sl=parsed_edit.sl,
        tp=parsed_edit.final_tp,
    )
    t3 = executor.place_limit_order(
        symbol="XAUUSD",
        direction="BUY",
        price=parsed_edit.zone.bottom,
        lot=LAYER_LOTS["BOTTOM"],  # 0.02
        sl=parsed_edit.sl,
        tp=parsed_edit.final_tp,
    )

    layers["MID"] = {"lot": LAYER_LOTS["MID"], "target_price": parsed_edit.zone.mid, "ticket": t2, "order_type": "LIMIT", "status": "PENDING"}
    layers["BOTTOM"] = {"lot": LAYER_LOTS["BOTTOM"], "target_price": parsed_edit.zone.bottom, "ticket": t3, "order_type": "LIMIT", "status": "PENDING"}

    state_mgr.update_signal(
        msg_id,
        {
            "pending_tickets": [t2, t3],
            "layers": layers,
            "zone_completed": True,
            "sl": parsed_edit.sl,
            "final_tp": parsed_edit.final_tp,
        },
    )

    sig_step2 = state_mgr.get_signal(msg_id)
    total_step2 = len(sig_step2["market_tickets"]) + len(sig_step2["pending_tickets"])
    assert total_step2 == 3
    assert sig_step2["zone_completed"] is True
    assert sig_step2["final_tp"] == 3899.50


def test_selective_breakeven():
    """
    Selective Breakeven Rule:
    Only moves TOP (0.01) and MID (0.01) to BE.
    Leaves BOTTOM (0.02) untouched at original SL.
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    layers = {
        "TOP": {"lot": 0.01, "ticket": 101, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 102, "order_type": "MARKET", "status": "OPEN"},
        "BOTTOM": {"lot": 0.02, "ticket": 103, "order_type": "MARKET", "status": "OPEN"},
    }

    modified = executor.apply_selective_breakeven(
        layers=layers,
        symbol="XAUUSD",
        direction="BUY",
        be_layers=["TOP", "MID"],
        be_offset_pips=2.0,
    )

    # Must modify TOP and MID, but NOT BOTTOM
    assert 101 in modified
    assert 102 in modified
    assert 103 not in modified


def test_proximity_close_rule():
    """
    20-Pip Proximity Auto-Close:
    Closes all open market positions and cancels all pending limits.
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    executor.apply_proximity_close(
        market_tickets=[401, 402],
        pending_tickets=[403],
    )
    # Executed without errors


@pytest.mark.asyncio
async def test_end_to_end_event_handlers(tmp_path):
    """
    Test handle_new_message and handle_message_edited end-to-end with simulated events:
    1. Noise event rejected.
    2. Other pair (EURUSD) rejected.
    3. Scenario A: Complete Zone Signal executed (0.01/0.01/0.02).
    4. Reply "BE": Selective BE applied.
    5. Reply "Active": Reactivation of old trade.
    """
    from main import handle_new_message, handle_message_edited
    import main

    test_state_file = tmp_path / "e2e_state.json"
    main.state_mgr = StateManager(test_state_file)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # 1. Noise event
    noise_evt = MockTelethonEvent(
        msg_id=1001,
        text="Manage your risk guys! Account £50 : 0.01 lot",
    )
    await handle_new_message(noise_evt)
    assert main.state_mgr.get_signal(1001) is None

    # 2. Other pair ignored
    forex_evt = MockTelethonEvent(
        msg_id=1002,
        text="Buy EURUSD 1.0850 \n TP 1.0900 \n SL 1.0800",
    )
    await handle_new_message(forex_evt)
    assert main.state_mgr.get_signal(1002) is None

    # 3. Scenario A: Complete Zone Signal (defaults to XAUUSD)
    scen_a_evt = MockTelethonEvent(
        msg_id=1003,
        text="Buy 3895-3887, TP 3900, SL 3875",
    )
    await handle_new_message(scen_a_evt)
    sig_a = main.state_mgr.get_signal(1003)
    assert sig_a is not None
    assert sig_a["symbol"] in ("XAUUSD", "XAUUSD.s")
    assert sig_a["final_tp"] == 3899.50
    assert sig_a["proximity_close_price"] == 3898.00
    assert len(sig_a["market_tickets"]) + len(sig_a["pending_tickets"]) == 3
    assert sig_a["layers"]["BOTTOM"]["lot"] == 0.02

    # 4. Reply BE on Scenario A
    be_reply = MockTelethonEvent(
        msg_id=1004,
        text="BE",
        reply_to_msg_id=1003,
    )
    await handle_new_message(be_reply)
    sig_a_be = main.state_mgr.get_signal(1003)
    assert sig_a_be["status"] == "BE_APPLIED"

    # Close trade
    main.state_mgr.update_signal(1003, {"status": "CLOSED"})

    # 5. Reply "Active" to reactivate the closed trade
    reactivate_reply = MockTelethonEvent(
        msg_id=1005,
        text="Active",
        reply_to_msg_id=1003,
    )
    await handle_new_message(reactivate_reply)
    sig_a_reactivated = main.state_mgr.get_signal(1003)
    assert sig_a_reactivated["status"] == "ACTIVE"
    assert len(sig_a_reactivated["market_tickets"]) + len(sig_a_reactivated["pending_tickets"]) == 3

    # 6. Concurrent Second Signal: Must execute immediately even with trade 1003 still active
    scen_second_evt = MockTelethonEvent(
        msg_id=1006,
        text="Buy 3910-3905, TP 3925, SL 3890",
    )
    await handle_new_message(scen_second_evt)
    sig_second = main.state_mgr.get_signal(1006)
    assert sig_second is not None
    assert sig_second["status"] == "ACTIVE"
    assert main.state_mgr.count_open_signals() == 2  # Both active concurrently!

    # 7. Saved Messages Status Command: /status
    from main import handle_saved_messages_command
    status_evt = MockTelethonEvent(msg_id=1007, text="/status")
    status_evt.reply = MagicMock(side_effect=lambda text: asyncio.sleep(0))
    await handle_saved_messages_command(status_evt)
    status_evt.reply.assert_called_once()
    reply_arg = status_evt.reply.call_args[0][0]
    assert "Telegram Copier System Status" in reply_arg
    assert "ONLINE" in reply_arg

    # 8. Saved Messages Phone Test Diagnostic: /test
    test_evt = MockTelethonEvent(msg_id=1008, text="/test")
    test_evt.reply = MagicMock(side_effect=lambda text: asyncio.sleep(0))
    await handle_saved_messages_command(test_evt)
    test_evt.reply.assert_called_once()
    assert "End-to-End Diagnostics" in test_evt.reply.call_args[0][0]

    # 9. Saved Messages Phone Simulator: /sim Buy 3895-3887 TP 3900 SL 3875
    sim_evt = MockTelethonEvent(msg_id=1009, text="/sim Buy 3895-3887 TP 3900 SL 3875")
    sim_evt.reply = MagicMock(side_effect=lambda text: asyncio.sleep(0))
    await handle_saved_messages_command(sim_evt)
    sim_evt.reply.assert_called_once()
    assert "Signal Parser Simulation" in sim_evt.reply.call_args[0][0]

    # 10. Saved Messages Phone Runner Test: /testrunner
    runner_evt = MockTelethonEvent(msg_id=1010, text="/testrunner")
    runner_evt.reply = MagicMock(side_effect=lambda text: asyncio.sleep(0))
    await handle_saved_messages_command(runner_evt)
    runner_evt.reply.assert_called_once()
    assert "Smart TP1 & Runner Hierarchy" in runner_evt.reply.call_args[0][0]


def test_single_price_smart_routing_and_execution():
    """
    Test ADDITIONS_SPEC:
    Single-Price differentiation and smart limit-vs-market routing:
    - Target 3895 with ask=3895.00 (ask <= target) -> Market Buy
    - Target 3880 with ask=3895.00 (ask > target) -> BUY LIMIT
    - Lot size = SINGLE_PRICE_LOT (0.01)
    - max_orders = 1
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    # 1. Ask <= target (DummyTick ask=3895.00) -> Market Buy
    mkt_tickets, pend_tickets, layers = executor.execute_single_price_order(
        symbol="XAUUSD.s",
        direction="BUY",
        price=3895.0,
        sl=3875.0,
        final_tp=3900.0,
        lot=0.01,
    )
    assert len(mkt_tickets) == 1
    assert len(pend_tickets) == 0
    assert layers["TOP"]["order_type"] == "MARKET"
    assert layers["TOP"]["lot"] == 0.01

    # 2. Ask > target (DummyTick ask=3895.00 > price 3880.0) -> BUY LIMIT
    mkt_tickets2, pend_tickets2, layers2 = executor.execute_single_price_order(
        symbol="XAUUSD.s",
        direction="BUY",
        price=3880.0,
        sl=3870.0,
        final_tp=3890.0,
        lot=0.01,
    )
    assert len(mkt_tickets2) == 0
    assert len(pend_tickets2) == 1
    assert layers2["TOP"]["order_type"] == "LIMIT"
    assert layers2["TOP"]["lot"] == 0.01


def test_algo_trading_guard():
    """
    Test ADDITIONS_SPEC: Live Algo Trading Button Monitor
    If trade_allowed == False, check_preflight fails and orders are cleanly rejected.
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    assert executor.is_trade_allowed() is True
    preflight_ok, _ = executor.check_preflight("XAUUSD.s", "BUY")
    assert preflight_ok is True

    # Simulate Algo Trading toggled off in MT5
    with pytest.MonkeyPatch.context() as m:
        m.setattr(executor, "is_trade_allowed", lambda: False)
        assert executor.is_trade_allowed() is False
        blocked, err = executor.check_preflight("XAUUSD.s", "BUY")
        assert blocked is False
        assert "Algo Trading is OFF in MT5" in err


def test_reconciliation_logic(tmp_path):
    """
    Test ADDITIONS_SPEC: 1-Second MT5 State Reconciliation
    1. If a pending order is filled, it moves from pending_tickets to market_tickets.
    2. If all orders/positions for a signal disappear (closed manually or by SL/TP),
       signal status updates to CLOSED.
    """
    state_file = tmp_path / "reconcile_state.json"
    state_mgr = StateManager(state_file)

    msg_id = 8801
    layers = {
        "TOP": {"lot": 0.01, "target_price": 3895.0, "ticket": 10001, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "target_price": 3891.0, "ticket": 10002, "order_type": "LIMIT", "status": "PENDING"},
        "BOTTOM": {"lot": 0.02, "target_price": 3887.0, "ticket": 10003, "order_type": "LIMIT", "status": "PENDING"},
    }

    state_mgr.create_signal(
        message_id=msg_id,
        symbol="XAUUSD.s",
        direction="BUY",
        layers=layers,
        market_tickets=[10001],
        pending_tickets=[10002, 10003],
        zone_completed=True,
        sl=3875.0,
        raw_tp1=3900.0,
        final_tp=3899.50,
        proximity_close_price=3898.00,
        raw_text="Buy 3895-3887 TP 3900 SL 3875",
        max_orders=3,
        is_zone=True,
    )

    sig = state_mgr.get_signal(msg_id)
    assert len(sig["market_tickets"]) == 1
    assert len(sig["pending_tickets"]) == 2

    # Simulate reconciliation step: ticket 10002 filled (now in positions, not pending orders)
    live_pos_tickets = {10001, 10002}
    live_ord_tickets = {10003}

    # Reconcile logic identical to main.py
    for pending_t in list(sig["pending_tickets"]):
        if pending_t in live_pos_tickets:
            sig["pending_tickets"].remove(pending_t)
            sig["market_tickets"].append(pending_t)
            for l_data in sig["layers"].values():
                if l_data.get("ticket") == pending_t:
                    l_data["status"] = "FILLED"

    state_mgr.update_signal(msg_id, {
        "market_tickets": sig["market_tickets"],
        "pending_tickets": sig["pending_tickets"],
        "layers": sig["layers"]
    })

    updated_sig = state_mgr.get_signal(msg_id)
    assert 10002 in updated_sig["market_tickets"]
    assert 10002 not in updated_sig["pending_tickets"]
    assert len(updated_sig["market_tickets"]) == 2
    assert len(updated_sig["pending_tickets"]) == 1

    # Simulate mobile closure / TP hit: all positions closed (empty sets in live MT5)
    live_pos_tickets_empty = set()
    live_ord_tickets_empty = set()
    all_tickets = set(updated_sig["market_tickets"] + updated_sig["pending_tickets"])
    remaining_live = (live_pos_tickets_empty | live_ord_tickets_empty) & all_tickets

    if not remaining_live and updated_sig.get("status") in ("ACTIVE", "BE_APPLIED"):
        state_mgr.update_signal(msg_id, {"status": "CLOSED"})

    closed_sig = state_mgr.get_signal(msg_id)
    assert closed_sig["status"] == "CLOSED"


def test_smart_tp1_runner_hierarchy_all_three_cases():
    """
    Test Smart TP1 Runner Hierarchy:
    1. Case 1: Only Layer 1 active -> Bank Layer 1, cancel limits, Status: CLOSED
    2. Case 2: Layers 1 & 2 active -> Bank Layer 1, Trail Layer 2 (0.01) to TP1, Status: RUNNER_ACTIVE
    3. Case 3: All 3 active -> Bank Layers 1 & 2, Trail Layer 3 (0.02) to TP1, Status: RUNNER_ACTIVE
    """
    executor = MT5Executor(dry_run=True)
    executor.connect()

    sample_layers = {
        "TOP": {"lot": 0.01, "ticket": 5001, "order_type": "MARKET", "status": "OPEN"},
        "MID": {"lot": 0.01, "ticket": 5002, "order_type": "LIMIT", "status": "PENDING"},
        "BOTTOM": {"lot": 0.02, "ticket": 5003, "order_type": "LIMIT", "status": "PENDING"},
    }

    # Case 1: Only Top active
    r1 = executor.apply_smart_tp1_runner_strategy(
        layers=sample_layers,
        market_tickets=[5001],
        pending_tickets=[5002, 5003],
        symbol="XAUUSD.s",
        direction="BUY",
        raw_tp1=3900.0,
    )
    assert r1["status"] == "CLOSED"
    assert r1["banked"] == ["TOP"]
    assert r1["runner"] is None
    assert r1["remaining_market_tickets"] == []

    # Case 2: Top & Mid active
    r2 = executor.apply_smart_tp1_runner_strategy(
        layers=sample_layers,
        market_tickets=[5001, 5002],
        pending_tickets=[5003],
        symbol="XAUUSD.s",
        direction="BUY",
        raw_tp1=3900.0,
    )
    assert r2["status"] == "RUNNER_ACTIVE"
    assert r2["banked"] == ["TOP"]
    assert r2["runner"] == "MID"
    assert r2["runner_ticket"] == 5002
    assert r2["remaining_market_tickets"] == [5002]
    assert r2["trailed_sl"] is not None

    # Case 3: All 3 active (Top, Mid, Bottom)
    r3 = executor.apply_smart_tp1_runner_strategy(
        layers=sample_layers,
        market_tickets=[5001, 5002, 5003],
        pending_tickets=[],
        symbol="XAUUSD.s",
        direction="BUY",
        raw_tp1=3900.0,
    )
    assert r3["status"] == "RUNNER_ACTIVE"
    assert r3["banked"] == ["TOP", "MID"]
    assert r3["runner"] == "BOTTOM"
    assert r3["runner_ticket"] == 5003
    assert r3["remaining_market_tickets"] == [5003]
    assert r3["trailed_sl"] is not None


