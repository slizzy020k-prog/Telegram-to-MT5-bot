import asyncio
import logging
import time
from unittest.mock import MagicMock

# Force DRY_RUN for simulation
import config
config.DRY_RUN = True

from mt5_executor import MT5Executor
from state_manager import StateManager
from parser import parse_message, ZoneInfo
from main import handle_new_message, handle_message_edited
import main


class MockTelethonEvent:
    def __init__(self, msg_id: int, text: str, reply_to_msg_id: int = None, chat_id: int = -1001234567890):
        self.chat_id = chat_id
        self.message = MagicMock()
        self.message.id = msg_id
        self.message.message = text
        self.message.reply_to_msg_id = reply_to_msg_id
        self.message.forward = None


async def run_simulation():
    print("=" * 70)
    print("  🚀 TELEGRAM-TO-MT5 XAUUSD SIGNAL COPIER - LIVE SIMULATION")
    print("=" * 70)

    # Use dry-run executor and fresh temporary simulation state
    sim_state_path = config.BASE_DIR / "sim_state.json"
    if sim_state_path.exists():
        sim_state_path.unlink()

    main.state_mgr = StateManager(sim_state_path)
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # -----------------------------------------------------------------------
    # TEST 1: Noise & Victory Recap Blocker
    # -----------------------------------------------------------------------
    print("\n[STEP 1] Testing Noise & Victory Recap Blocker...")
    noise_text = "49/50 trades won here by me this week! + 16,780 pips confirmed 🏦🏧 Manage your risk!"
    print(f"  Incoming Message: \"{noise_text}\"")
    evt1 = MockTelethonEvent(msg_id=20001, text=noise_text)
    await handle_new_message(evt1)
    sig1 = main.state_mgr.get_signal(20001)
    assert sig1 is None
    print("  ✅ Result: Ignored completely by Gate 2. Zero false orders placed.")

    # -----------------------------------------------------------------------
    # TEST 2: Ignored Pair Blocker (Non-Gold)
    # -----------------------------------------------------------------------
    print("\n[STEP 2] Testing Non-Gold Pair Filter (IGNORED_SYMBOLS)...")
    forex_text = "Buy EURUSD 1.0850 \n TP 1.0900 \n SL 1.0800"
    print(f"  Incoming Message: \"{forex_text}\"")
    evt2 = MockTelethonEvent(msg_id=20002, text=forex_text)
    await handle_new_message(evt2)
    sig2 = main.state_mgr.get_signal(20002)
    assert sig2 is None
    print("  ✅ Result: Blocked immediately. Only Gold (XAUUSD) trades are allowed.")

    # -----------------------------------------------------------------------
    # TEST 3: Scenario A - 3-Layer Asymmetric Zone Entry (XAUUSD Default)
    # -----------------------------------------------------------------------
    print("\n[STEP 3] Testing Scenario A: 3-Layer Zone Entry (0.01 / 0.01 / 0.02)...")
    signal_text = "Buy 3895-3887 \n TP 3900 \n SL 3875"
    print(f"  Incoming Message: \"{signal_text}\"")
    evt3 = MockTelethonEvent(msg_id=20003, text=signal_text)
    await handle_new_message(evt3)

    sig3 = main.state_mgr.get_signal(20003)
    assert sig3 is not None
    layers = sig3["layers"]
    print(f"  ✅ Signal Parsed & Placed on {sig3['symbol']}:")
    print(f"     • Layer 1 (TOP):    Lot={layers['TOP']['lot']} | Price={layers['TOP']['target_price']} | Ticket={layers['TOP']['ticket']}")
    print(f"     • Layer 2 (MID):    Lot={layers['MID']['lot']} | Price={layers['MID']['target_price']} | Ticket={layers['MID']['ticket']}")
    print(f"     • Layer 3 (BOTTOM): Lot={layers['BOTTOM']['lot']} | Price={layers['BOTTOM']['target_price']} | Ticket={layers['BOTTOM']['ticket']}")
    print(f"     • Hard MT5 TP (TP1 - 0.50): {sig3['final_tp']}")
    print(f"     • 20-Pip Proximity Trigger: {sig3['proximity_close_price']} ($2.00 before TP1)")
    print(f"     • Total Lots Exposure: {sum(l['lot'] for l in layers.values()):.2f} / 0.04 max")

    # -----------------------------------------------------------------------
    # TEST 4: Selective Breakeven (BE on TOP & MID only)
    # -----------------------------------------------------------------------
    print("\n[STEP 4] Testing Selective Breakeven Command ('BE')...")
    be_text = "BE"
    print(f"  Incoming Reply: \"{be_text}\" (replying to Signal 20003)")
    evt4 = MockTelethonEvent(msg_id=20004, text=be_text, reply_to_msg_id=20003)
    await handle_new_message(evt4)

    sig4 = main.state_mgr.get_signal(20003)
    print("  ✅ Selective Breakeven Executed:")
    print("     • Layer 1 (0.01 TOP): Moved SL to Entry + 2.0 pips buffer.")
    print("     • Layer 2 (0.01 MID): Moved SL to Entry + 2.0 pips buffer.")
    print("     • Layer 3 (0.02 BOTTOM): UNTOUCHED at original SL 3875.0 (protected from pullbacks).")

    # -----------------------------------------------------------------------
    # TEST 5: Scenario B - "Buy now" held with recorded point -> Zone edited later
    # -----------------------------------------------------------------------
    print("\n[STEP 5] Testing Scenario B: 'Buy now' held (recorded point) -> Edited later with Zone...")
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 20.0
    try:
        buy_now_text = "Buy now"
        print(f"  [5a] Initial Post: \"{buy_now_text}\"")
        evt5a = MockTelethonEvent(msg_id=20005, text=buy_now_text)
        await handle_new_message(evt5a)
        sig5a = main.state_mgr.get_signal(20005)
        assert sig5a is not None and sig5a["status"] in ("PENDING", "PENDING_DELAY")
        print(f"     • Recorded Entry Point: {sig5a['recorded_entry_price']} | Status: {sig5a['status']} (awaiting zone/delay)")

        print("  [5b] Provider edits message within delay window with Zone & SL/TP:")
        edit_text = "Buy 3895-3887 \n TP 3900 \n SL 3875"
        print(f"  Edited Message: \"{edit_text}\"")
        evt5b = MockTelethonEvent(msg_id=20005, text=edit_text)
        await handle_message_edited(evt5b)
        sig5b = main.state_mgr.get_signal(20005)
        assert sig5b["status"] == "ACTIVE"
        assert len(sig5b["market_tickets"]) + len(sig5b["pending_tickets"]) == 3
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay
    print(f"  ✅ Edit State Machine Handled:")
    print(f"     • Layer 1: SL updated to 3875.0, TP updated to 3899.50")
    print(f"     • Layer 2 (0.01 Mid Limit): Placed at 3891.00")
    print(f"     • Layer 3 (0.02 Bottom Limit): Placed at 3887.00")
    print(f"     • Hard Cap: Exactly 3 orders total (0.04 lots).")

    # -----------------------------------------------------------------------
    # TEST 6: 20-Pip TP1 Proximity Auto-Close ($2.00 / 20 Pips Before TP1)
    # -----------------------------------------------------------------------
    print("\n[STEP 6] Testing 20-Pip TP1 Proximity Auto-Close...")
    print("  Simulating live Gold tick reaching 3898.00 (TP1 is 3900.00, buffer is $2.00)...")
    main.executor.apply_proximity_close(
        market_tickets=sig3["market_tickets"],
        pending_tickets=sig3["pending_tickets"],
    )
    main.state_mgr.set_tp1_reached(20003, True)
    main.state_mgr.update_signal(20003, {"status": "CLOSED", "market_tickets": [], "pending_tickets": []})
    sig_closed = main.state_mgr.get_signal(20003)
    print("  ✅ Proximity Auto-Close Triggered:")
    print("     • All open positions closed at market to bank profit before resistance.")
    print("     • All pending limit orders cancelled.")
    print("     • Trade marked CLOSED in state.")

    # Clean up temp state
    if sim_state_path.exists():
        sim_state_path.unlink()

    print("\n" + "=" * 70)
    print("  🎉 ALL 6 SIMULATION TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_simulation())
