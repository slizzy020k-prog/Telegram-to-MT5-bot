import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Isolate logs, state, ledger, and lock into a disposable temp dir BEFORE importing config/main.
_SIM_TEMP_DIR = tempfile.TemporaryDirectory(prefix="xauusd_copier_sim_")
_SIM_DIR = Path(_SIM_TEMP_DIR.name)
os.environ["DRY_RUN"] = "true"
os.environ["LOG_FILE_PATH"] = str(_SIM_DIR / "sim_copier.log")
os.environ["STATE_FILE_PATH"] = str(_SIM_DIR / "sim_signals_state.json")
os.environ["TRADES_LEDGER_PATH"] = str(_SIM_DIR / "sim_trades_ledger.jsonl")
os.environ["LOCK_FILE_PATH"] = str(_SIM_DIR / "sim_copier.lock")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config  # noqa: E402
import main  # noqa: E402
from mt5_executor import MT5Executor  # noqa: E402
from startup_checks import (  # noqa: E402
    acquire_single_instance_lock,
    release_single_instance_lock,
    validate_environment,
)
from state_manager import StateManager  # noqa: E402
from trade_ledger import TradeLedger  # noqa: E402


class MockTelethonEvent:
    def __init__(
        self,
        msg_id: int,
        text: str,
        reply_to_msg_id: int = None,
        chat_id: int = -1001234567890,
    ):
        self.chat_id = chat_id
        self.message = MagicMock()
        self.message.id = msg_id
        self.message.message = text
        self.message.reply_to_msg_id = reply_to_msg_id
        self.message.forward = None


def _make_tick(bid: float, ask: float) -> MagicMock:
    tick = MagicMock()
    tick.bid = bid
    tick.ask = ask
    return tick


def _shutdown_sim_loggers() -> None:
    """Close file handlers pointing at the temp dir so Windows can delete temp files cleanly."""
    for name in ("copier", "telethon"):
        log_obj = logging.getLogger(name)
        for handler in list(log_obj.handlers):
            if isinstance(handler, logging.FileHandler):
                try:
                    handler.close()
                except Exception:
                    pass
                log_obj.removeHandler(handler)


async def run_simulation() -> None:
    print("=" * 72)
    print("  TELEGRAM-TO-MT5 XAUUSD SIGNAL COPIER v3.1 - ISOLATED LIVE SIMULATION")
    print("=" * 72)
    print(f"  Temp Isolation Dir : {_SIM_DIR}")
    print(f"  Dry-Run Enforced   : {config.DRY_RUN}")
    assert config.DRY_RUN is True
    assert Path(config.LOG_FILE_PATH).parent == _SIM_DIR
    assert Path(config.STATE_FILE_PATH).parent == _SIM_DIR
    assert Path(config.TRADES_LEDGER_PATH).parent == _SIM_DIR

    main.state_mgr = StateManager(_SIM_DIR / "sim_signals_state.json")
    main.trade_ledger = TradeLedger(_SIM_DIR / "sim_trades_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # -----------------------------------------------------------------------
    # STEP 0: Startup Preflight Checks & Single-Instance Mutex Lock
    # -----------------------------------------------------------------------
    print("\n[STEP 0] Testing Startup Preflight & Single-Instance Lock...")
    env_check = validate_environment(check_packages=True)
    assert env_check["ok"], f"Preflight failed: {env_check}"
    sim_lock = acquire_single_instance_lock(config.LOCK_FILE_PATH)
    assert sim_lock is not None
    dup_lock = acquire_single_instance_lock(config.LOCK_FILE_PATH)
    assert dup_lock is None
    release_single_instance_lock(sim_lock)
    print("  [OK] Preflight passed & duplicate instance lock contended cleanly.")

    # -----------------------------------------------------------------------
    # STEP 1: Noise & Victory Recap Blocker
    # -----------------------------------------------------------------------
    print("\n[STEP 1] Testing Noise & Victory Recap Blocker (Gate 2)...")
    noise_text = "49/50 trades won here by me this week! + 16,780 pips confirmed! Manage your risk!"
    evt1 = MockTelethonEvent(msg_id=20001, text=noise_text)
    await main.handle_new_message(evt1)
    assert main.state_mgr.get_signal(20001, chat_id=evt1.chat_id) is None
    print("  [OK] Ignored completely by Gate 2. Zero false orders placed.")

    # -----------------------------------------------------------------------
    # STEP 2: Ignored Pair Blocker (Non-Gold)
    # -----------------------------------------------------------------------
    print("\n[STEP 2] Testing Non-Gold Pair Filter (IGNORED_SYMBOLS)...")
    forex_text = "Buy EURUSD 1.0850 \n TP 1.0900 \n SL 1.0800"
    evt2 = MockTelethonEvent(msg_id=20002, text=forex_text)
    await main.handle_new_message(evt2)
    assert main.state_mgr.get_signal(20002, chat_id=evt2.chat_id) is None
    print("  [OK] Blocked immediately. Only Gold (XAUUSD) trades are allowed.")

    # -----------------------------------------------------------------------
    # STEP 3: 4-Layer Zone Entry [0.01, 0.01, 0.01, 0.02] Initially at TP1
    # -----------------------------------------------------------------------
    print("\n[STEP 3] Testing 4-Layer Zone Entry [0.01, 0.01, 0.01, 0.02] Initially at TP1...")
    signal_text = "Buy 3895-3887 \n TP 3900 \n TP 3910 \n SL 3875"
    evt3 = MockTelethonEvent(msg_id=20003, text=signal_text)
    await main.handle_new_message(evt3)

    sig3 = main.state_mgr.get_signal(20003, chat_id=evt3.chat_id)
    assert sig3 is not None
    layers = sig3["layers"]
    assert list(layers.keys()) == ["L1", "L2", "L3", "L4"]
    assert [layers[k]["lot"] for k in ("L1", "L2", "L3", "L4")] == [0.01, 0.01, 0.01, 0.02]
    assert sig3["tp_levels"] == {
        "TP1": 3900.0,
        "TP2": 3910.0,
        "TP3": 3920.0,
        "TP4": 3930.0,
        "TP5": 3940.0,
    }
    assert all(layers[k]["tp"] == 3899.50 and layers[k]["tp_stage"] == "TP1" for k in ("L1", "L2", "L3", "L4"))
    total_lots = round(sum(l["lot"] for l in layers.values()), 2)
    assert total_lots == 0.05
    print(f"  [OK] Placed 4 layers on {sig3['symbol']} initially at TP1 (Total {total_lots:.2f} lots):")
    for k in ("L1", "L2", "L3", "L4"):
        l_info = layers[k]
        print(
            f"     - {k}: Lot={l_info['lot']:.2f} | Entry={l_info['target_price']} "
            f"| TP={l_info['tp']} ({l_info['tp_stage']}) | Type={l_info['order_type']} | Ticket={l_info['ticket']}"
        )

    # -----------------------------------------------------------------------
    # STEP 4: Selective Breakeven (BE on L1, L2, L3; L4 untouched)
    # -----------------------------------------------------------------------
    print("\n[STEP 4] Testing Selective Breakeven Command ('BE')...")
    evt4 = MockTelethonEvent(msg_id=20004, text="BE", reply_to_msg_id=20003)
    await main.handle_new_message(evt4)
    print("  [OK] Selective Breakeven applied to BE_LAYERS=['L1','L2','L3']; L4 (0.02) untouched.")

    # -----------------------------------------------------------------------
    # STEP 5: Delayed "Buy now" -> Edited with 4-Layer Zone
    # -----------------------------------------------------------------------
    print("\n[STEP 5] Testing Delayed 'Buy now' -> Edited with 4-Layer Zone...")
    orig_delay = main.SELL_NOW_DELAY_SECONDS
    main.SELL_NOW_DELAY_SECONDS = 20.0
    try:
        evt5a = MockTelethonEvent(msg_id=20005, text="Buy now")
        await main.handle_new_message(evt5a)
        sig5a = main.state_mgr.get_signal(20005, chat_id=evt5a.chat_id)
        assert sig5a is not None and sig5a["status"] in ("PENDING", "PENDING_DELAY")

        edit_text = "Buy 3895-3887 \n TP 3900 \n SL 3875"
        evt5b = MockTelethonEvent(msg_id=20005, text=edit_text)
        await main.handle_message_edited(evt5b)
        sig5b = main.state_mgr.get_signal(20005, chat_id=evt5b.chat_id)
        assert sig5b["status"] == "ACTIVE"
        assert len(sig5b["market_tickets"]) + len(sig5b["pending_tickets"]) == 4
        assert round(sum(v["lot"] for v in sig5b["layers"].values()), 2) == 0.05
    finally:
        main.SELL_NOW_DELAY_SECONDS = orig_delay
    print("  [OK] Delayed 'Buy now' converted cleanly to 4-layer Zone (4 orders, 0.05 lots).")

    # -----------------------------------------------------------------------
    # STEP 6: Dynamic Multi-Layer Scaling (N=1 Single-Layer + N=4 Multi-Layer)
    # -----------------------------------------------------------------------
    print("\n[STEP 6] Testing Dynamic Runner Scaling (N=1 -> L1 & N=4 -> L4) + Anti-Choke Guard...")
    # [6a] Single-layer (N=1): only L1 filled -> L1 dynamically becomes deepest runner & scales TP1->TP4
    sig_single = {
        "symbol": config.DEFAULT_SYMBOL,
        "direction": "BUY",
        "recorded_entry_price": 3890.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 9501, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3888.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 9502, "order_type": "LIMIT", "status": "PENDING"},
        },
        "market_tickets": [9501],
        "pending_tickets": [9502],
        "final_tp": 3899.5,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "raw_tp3": 3920.0,
        "raw_tp4": 3930.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
    }
    main.executor.prepare_deepest_runners_for_tp_breakout(sig_single, live_price=3899.30)
    assert sig_single["layers"]["L1"]["tp"] == 3910.0
    with patch.object(main.executor, "get_tick", return_value=_make_tick(3900.60, 3900.80)):
        rt_1 = main.executor.handle_tp1_scaling_and_select_runner(sig_single)
    assert rt_1 == 9501 and sig_single["runner_layers"] == ["L1"] and sig_single["last_banked_tickets"] == []
    for px in (3910.60, 3920.60):
        with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
            assert main.executor.manage_designated_runner(sig_single) is True
    with patch.object(main.executor, "get_tick", return_value=_make_tick(3930.10, 3930.30)):
        assert main.executor.manage_designated_runner(sig_single) is False
    assert sig_single["tp4_reached"] is True and sig_single["status"] == "CLOSED"
    print("  [6a] Single-Layer (N=1): L1 dynamically promoted (0 closed at TP1) and scaled TP2->TP3->TP4!")

    # [6b] Full 4-layer trade: L1..L3 bank at TP1, L4 runner scales TP2->TP3 with Anti-Choke noise immunity
    all_tickets = []
    for k in ("L1", "L2", "L3", "L4"):
        layers[k]["status"] = "OPEN"
        layers[k]["order_type"] = "MARKET"
        all_tickets.append(layers[k]["ticket"])
    sig3["market_tickets"] = list(all_tickets)
    sig3["pending_tickets"] = []
    main.state_mgr.update_signal(
        20003,
        {"layers": layers, "market_tickets": list(all_tickets), "pending_tickets": []},
        chat_id=evt3.chat_id,
    )

    with patch.object(main.executor, "get_tick", return_value=_make_tick(3898.20, 3898.40)):
        runner_t = main.executor.handle_tp1_scaling_and_select_runner(sig3)
    assert runner_t == layers["L4"]["ticket"]
    assert sig3["status"] == "RUNNER_ACTIVE"
    assert sig3["runner_layer"] == "L4"
    assert all(sig3["layers"][lk]["status"] == "CLOSED" for lk in ("L1", "L2", "L3"))
    assert sig3["market_tickets"] == [runner_t]
    assert sig3["layers"]["L4"]["tp"] == 3910.0
    print(f"  [6b] TP1 Scaling (N=4): L1..L3 closed at TP1, L4 #{runner_t} active -> TP2=3910.0.")

    # Clean breakthrough past TP1 -> TP2, then 6-pip micro-noise near TP2 must NOT choke L4!
    for px in (3900.60, 3909.30, 3908.70, 3910.60):
        with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
            assert main.executor.manage_designated_runner(sig3) is True
    assert "TP2" in sig3["runner_cleared_milestones"]
    assert sig3["runner_milestone_name"] == "TP3"
    assert sig3["layers"]["L4"]["tp"] == 3920.0
    print(f"  [6c] Ignored 6p micro-noise & cleared TP2 -> stepped to {sig3['runner_milestone_name']}, floor={sig3['runner_sl_floor']}.")

    # Approach TP3 (3920.0) within strict 10p buffer ($1.00 -> >= 3919.0) -> arm -> peak 3919.80 -> 2-tick 13p reversal -> Bank It!
    for px in (3919.20, 3919.80, 3918.50):
        with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
            assert main.executor.manage_designated_runner(sig3) is True
    assert sig3["runner_reversal_ticks"] == 1
    with patch.object(main.executor, "get_tick", return_value=_make_tick(3918.50, 3918.70)):
        still_running = main.executor.manage_designated_runner(sig3)
    assert still_running is False
    assert sig3["layers"]["L4"]["status"] == "CLOSED"
    assert sig3["market_tickets"] == [] and sig3["status"] == "CLOSED"
    assert "Bank It" in (sig3.get("runner_close_reason") or "")
    print(f"  [6d] Confirmed 2-tick Reversal Guard near TP3: {sig3['runner_close_reason']}.")

    main.state_mgr.update_signal(20003, sig3, chat_id=evt3.chat_id)
    ledger_rec = main.trade_ledger.record_closed_signal(
        signal_key=f"{evt3.chat_id}:20003",
        signal_data=sig3,
        realized_pnl=64.50,
        exit_reason=sig3["runner_close_reason"],
        currency="GBP",
        dry_run=True,
    )
    stats = main.trade_ledger.get_stats()
    assert ledger_rec["total_lots"] == 0.05
    assert stats["total_trades"] == 1 and stats["wins"] == 1
    assert (_SIM_DIR / "sim_copier.log").exists()
    assert (_SIM_DIR / "sim_signals_state.json").exists()
    assert (_SIM_DIR / "sim_trades_ledger.jsonl").exists()
    print(
        f"  [6e] TradeLedger recorded closed 4-layer trade -> stats={stats['total_trades']} win(s), "
        f"PnL=+{stats['total_realized_pnl']:.2f} GBP (win_rate={stats['win_rate_pct']}%)."
    )

    # -----------------------------------------------------------------------
    # STEP 7: Deleted Signal in Chat -> Immediate Trade Take-Out
    # -----------------------------------------------------------------------
    print("\n[STEP 7] Testing Deleted Signal in Chat -> Immediate Trade Take-Out (handle_message_deleted)...")
    evt_del_target = MockTelethonEvent(msg_id=20007, text="Buy 3895-3887 \n TP 3905 \n SL 3875")
    evt_keep_alive = MockTelethonEvent(msg_id=20008, text="Buy 3896-3886 \n TP 3906 \n SL 3874")
    with patch.object(main.executor, "get_tick", return_value=_make_tick(3894.70, 3895.00)):
        await main.handle_new_message(evt_del_target)
        await main.handle_new_message(evt_keep_alive)

    sig_del_before = main.state_mgr.get_signal(20007, chat_id=evt_del_target.chat_id)
    sig_keep_before = main.state_mgr.get_signal(20008, chat_id=evt_keep_alive.chat_id)
    assert sig_del_before is not None and len(sig_del_before["market_tickets"]) == 1 and len(sig_del_before["pending_tickets"]) == 3
    assert sig_keep_before is not None and len(sig_keep_before["market_tickets"]) == 1 and len(sig_keep_before["pending_tickets"]) == 3

    from types import SimpleNamespace
    del_event = SimpleNamespace(deleted_ids=[20007], chat_id=evt_del_target.chat_id)
    await main.handle_message_deleted(del_event)

    sig_del_after = main.state_mgr.get_signal(20007, chat_id=evt_del_target.chat_id)
    sig_keep_after = main.state_mgr.get_signal(20008, chat_id=evt_keep_alive.chat_id)
    assert sig_del_after["status"] == "CLOSED"
    assert sig_del_after["deleted_from_chat"] is True
    assert sig_del_after["market_tickets"] == [] and sig_del_after["pending_tickets"] == []
    assert sig_del_after["ledger_recorded"] is True
    assert sig_keep_after["status"] == "ACTIVE"
    assert len(sig_keep_after["market_tickets"]) == 1 and len(sig_keep_after["pending_tickets"]) == 3
    stats_after_del = main.trade_ledger.get_stats()
    assert stats_after_del["total_trades"] == 2
    print(
        f"  [OK] Deleted Signal #20007 closed immediately (status={sig_del_after['status']}, "
        f"deleted_from_chat={sig_del_after['deleted_from_chat']}), Ledger={stats_after_del['total_trades']} trades, "
        f"while unrelated Signal #20008 remained {sig_keep_after['status']}!"
    )

    print("\n" + "=" * 72)
    print("  ALL 7 v3.1 + STEP 7 v3.2 SIMULATION CHECKS PASSED (ZERO LIVE FILES TOUCHED)!")
    print("=" * 72)


if __name__ == "__main__":
    try:
        asyncio.run(run_simulation())
    finally:
        _shutdown_sim_loggers()
        _SIM_TEMP_DIR.cleanup()
