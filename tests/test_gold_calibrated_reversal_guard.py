"""
Automated verification suite for Gold (XAUUSD) Calibrated Peak-Profit Giveback Guard (v3.4):
1. Single-layer trade (N=1): Layer 1 dynamically becomes deepest runner, disables tight TP1 Shortfall,
   lifts broker TP early at 70%, arms at 85% of TP1, and scales cleanly through TP2-TP4.
2. Multi-layer trade (N=4): Layers 1-3 respect calibrated TP1 rules (including layer-separated $2.00
   TP1 Shortfall banking ONLY L1-L3) while Layer 4 runs with the Gold Reversal Guard to TP2-TP4.
3. Near-miss reversal near TP1: Price reaches 90% of TP1 and drops >=30% (and >=$1.50 / 0.35*M1_ATR_14),
   confirming immediate market banking & TradeLedger recording.
4. Gold wick ($1.00 noise) & Spread Spike Filter (both $0.40 ECN & $0.80 adaptive PU Prime $0.52 baseline):
   confirming bot holds through noise/spread spikes and rides winners to TP1-TP4.
5. Atomic Exit Lock (`is_closing = True`): thread-safe duplicate-close prevention under concurrent ticks.
"""

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5
import pytest

import config
import main
import mt5_executor as ex_mod
from mt5_executor import MT5Executor
from parser import derive_tp_levels
from state_manager import StateManager
from trade_ledger import TradeLedger


def _tick(bid: float, ask: float = None, spread: float = 0.20) -> SimpleNamespace:
    return SimpleNamespace(bid=round(bid, 2), ask=round(ask if ask is not None else (bid + spread), 2), time=1794900000.0)


@pytest.mark.asyncio
async def test_1_single_layer_dynamically_becomes_runner_disables_tp1_shortfall_arms_85pct_and_scales_tp2_to_tp4(tmp_path, monkeypatch):
    """
    Scenario 1 (Single-layer trade, 1 active layer):
    - Confirm Layer 1 dynamically becomes the deepest runner and disables the tight TP1 Shortfall rule.
    - Confirm Reversal Guard stays OFF below 85%, lifts broker TP early at 70% so TP1-$0.50 never caps L1,
      arms at >=85% of TP1, keeps `reversal_guard_armed = True` across TP1 breakout, and scales TP2->TP3->TP4.
    """
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    main.state_mgr = StateManager(tmp_path / "s1_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "s1_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    tps = derive_tp_levels([3900.0, 3910.0, 3920.0, 3930.0], direction="BUY")
    main.state_mgr.create_signal(
        message_id=7101,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 1101, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3887.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 1102, "order_type": "LIMIT", "status": "PENDING"},
        },
        market_tickets=[1101],
        pending_tickets=[1102],
        raw_tp1=tps["TP1"],
        raw_tp2=tps["TP2"],
        raw_tp3=tps["TP3"],
        raw_tp4=tps["TP4"],
        raw_tp5=tps["TP5"],
        tp_levels=tps,
        final_tp=3899.5,
        recorded_entry_price=3890.0,
        initial_price=3890.0,
    )

    with patch.object(main, "notify_saved_messages", new=AsyncMock()):
        # 1) Mid-range (60% of TP1 = 3896.0) + $2.50 pullback -> completely unarmed, stays ACTIVE
        for px in (3896.00, 3893.50):
            with patch.object(main.executor, "get_tick", return_value=_tick(px)):
                await main._process_pre_tp1_or_backstop_tick("7101", main.state_mgr.get_signal(7101))
        s_mid = main.state_mgr.get_signal(7101)
        assert s_mid["reversal_guard_armed"] is False
        assert s_mid["shortfall_armed"] is False
        assert s_mid["layers"]["L1"]["tp"] == 3899.5
        assert s_mid["status"] == "ACTIVE"

        # 2) 75% of TP1 (3897.50 >= 70% lift threshold, < 85% arm threshold):
        #    Broker TP lifts to TP2 (3910.0) while Reversal Guard remains OFF!
        with patch.object(main.executor, "get_tick", return_value=_tick(3897.50)):
            await main._process_pre_tp1_or_backstop_tick("7101", main.state_mgr.get_signal(7101))
        s_75 = main.state_mgr.get_signal(7101)
        assert s_75["layers"]["L1"]["tp"] == 3910.0
        assert s_75["layers"]["L1"]["tp_stage"] == "TP2"
        assert s_75["reversal_guard_armed"] is False
        assert s_75["shortfall_armed"] is False

        # 3) 88% of TP1 (3898.80 >= 85%) and 95% (3899.50, inside 8p of TP1):
        #    Reversal Guard arms, while tight TP1 Shortfall stays DISABLED on single-layer runner L1!
        for px in (3898.80, 3899.50):
            with patch.object(main.executor, "get_tick", return_value=_tick(px)):
                await main._process_pre_tp1_or_backstop_tick("7101", main.state_mgr.get_signal(7101))
        s_88 = main.state_mgr.get_signal(7101)
        assert s_88["reversal_guard_armed"] is True
        assert s_88["shortfall_armed"] is False  # Runner override confirmed!
        assert s_88["peak_profit"] == pytest.approx(9.50)

        # 4) Breakout past TP1 (3900.60) -> 0 open layers closed, L1 becomes RUNNER_ACTIVE, reversal_guard_armed stays True!
        with patch.object(main.executor, "get_tick", return_value=_tick(3900.60)):
            await main._process_pre_tp1_or_backstop_tick("7101", main.state_mgr.get_signal(7101))
        s_tp1 = main.state_mgr.get_signal(7101)
        assert s_tp1["status"] == "RUNNER_ACTIVE"
        assert s_tp1["runner_layers"] == ["L1"]
        assert s_tp1["market_tickets"] == [1101]
        assert s_tp1["pending_tickets"] == []
        assert s_tp1["reversal_guard_armed"] is True
        assert s_tp1["peak_profit"] == pytest.approx(10.60)

        # 5) Scale dynamically through TP2 (3910.60) -> TP3 (3920.60) -> TP4 ceiling (3930.10)!
        with patch.object(main.executor, "get_tick", return_value=_tick(3910.60)):
            await main._process_runner_active_tick("7101", main.state_mgr.get_signal(7101))
        s_tp2 = main.state_mgr.get_signal(7101)
        assert s_tp2["layers"]["L1"]["tp"] == 3920.0
        assert s_tp2["runner_sl_floor"] >= 3900.0  # Dynamic TP1 trail / milestone floor
        assert s_tp2["reversal_guard_armed"] is True
        assert s_tp2["peak_profit"] == pytest.approx(20.60)

        with patch.object(main.executor, "get_tick", return_value=_tick(3920.60)):
            await main._process_runner_active_tick("7101", main.state_mgr.get_signal(7101))
        s_tp3 = main.state_mgr.get_signal(7101)
        assert s_tp3["layers"]["L1"]["tp"] == 3930.0
        assert s_tp3["runner_sl_floor"] >= 3910.0  # Dynamic TP1 trail / milestone floor at TP2+

        with patch.object(main.executor, "get_tick", return_value=_tick(3930.10)):
            await main._process_runner_active_tick("7101", main.state_mgr.get_signal(7101))
        s_end = main.state_mgr.get_signal(7101)
        assert s_end["status"] == "CLOSED"
        assert s_end["tp4_reached"] is True
        assert s_end["runner_cleared_milestones"] == ["TP1", "TP2", "TP3", "TP4"]


@pytest.mark.asyncio
async def test_2_multilayer_4_active_layers_layer_separation_and_l4_scaling_to_tp4(tmp_path, monkeypatch):
    """
    Scenario 2 (Multi-layer trade, 4 active layers):
    - Part A: L1-L3 keep TP1 front-run (3899.5) and bank at TP1 while L4 (0.02) scales to TP2-TP4
      under the Gold Reversal Guard (surviving $2.00 pullbacks near TP3 that would choke a 15p trail!).
    - Part B: Pre-TP1 $2.10 pullback (3 ticks) triggers TP1 Shortfall ONLY on base layers L1-L3,
      sparing L4 (since $2.10 < 30% of $9.50 = $2.85) so L4 holds and rides the recovery to TP4!
    """
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    monkeypatch.setattr(config, "BE_OFFSET_PIPS", 5.0)
    monkeypatch.setattr(main, "BE_OFFSET_PIPS", 5.0)
    main.state_mgr = StateManager(tmp_path / "s2_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "s2_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    tps = derive_tp_levels([3900.0, 3910.0, 3920.0, 3930.0], direction="BUY")

    def _seed_4layer(mid: int, base_tkt: int):
        main.state_mgr.create_signal(
            message_id=mid,
            symbol="XAUUSD.s",
            direction="BUY",
            layers={
                "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": base_tkt + 1, "status": "OPEN"},
                "L2": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": base_tkt + 2, "status": "OPEN"},
                "L3": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": base_tkt + 3, "status": "OPEN"},
                "L4": {"lot": 0.02, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": base_tkt + 4, "status": "OPEN"},
            },
            market_tickets=[base_tkt + 1, base_tkt + 2, base_tkt + 3, base_tkt + 4],
            pending_tickets=[],
            raw_tp1=tps["TP1"],
            raw_tp2=tps["TP2"],
            raw_tp3=tps["TP3"],
            raw_tp4=tps["TP4"],
            raw_tp5=tps["TP5"],
            tp_levels=tps,
            final_tp=3899.5,
            recorded_entry_price=3890.0,
            initial_price=3890.0,
        )

    with patch.object(main, "notify_saved_messages", new=AsyncMock()):
        # --- Part A: L1-L3 bank at TP1, L4 runs with Gold Reversal Guard through $2.00 pullback to TP4 ---
        _seed_4layer(7201, 2100)
        with patch.object(main.executor, "get_tick", return_value=_tick(3898.00)):
            await main._process_pre_tp1_or_backstop_tick("7201", main.state_mgr.get_signal(7201))
        s_pre = main.state_mgr.get_signal(7201)
        assert all(s_pre["layers"][k]["tp"] == 3899.5 for k in ("L1", "L2", "L3"))
        assert s_pre["layers"]["L4"]["tp"] == 3910.0  # Only L4 lifted early at >=70%!

        with patch.object(main.executor, "get_tick", return_value=_tick(3900.60)):
            await main._process_pre_tp1_or_backstop_tick("7201", main.state_mgr.get_signal(7201))
        s_r = main.state_mgr.get_signal(7201)
        assert s_r["status"] == "RUNNER_ACTIVE"
        assert s_r["market_tickets"] == [2104]
        assert all(s_r["layers"][k]["status"] == "CLOSED" for k in ("L1", "L2", "L3"))
        assert s_r["layers"]["L4"]["status"] == "RUNNER"

        # Break TP2 (3910.60), climb to 3919.00, dip $1.80 to 3917.20 (< $2.50 trail dist) -> L4 HOLDS & hits TP4!
        for px in (3910.60, 3919.00, 3917.20, 3920.60, 3930.10):
            with patch.object(main.executor, "get_tick", return_value=_tick(px)):
                await main._process_runner_active_tick("7201", main.state_mgr.get_signal(7201))
        s_done = main.state_mgr.get_signal(7201)
        assert s_done["status"] == "CLOSED"
        assert s_done["tp4_reached"] is True

        # --- Part B: Layer-Separated Pre-TP1 Shortfall banks ONLY L1-L3, sparing L4! ---
        _seed_4layer(7202, 2200)
        # Peak at 3899.50 ($9.50 profit -> L4 30% giveback = $2.85; Base L1-L3 Shortfall = $2.00 x 3 ticks)
        for px in (3899.50, 3897.40, 3897.40, 3897.40):
            with patch.object(main.executor, "get_tick", return_value=_tick(px)):
                await main._process_pre_tp1_or_backstop_tick("7202", main.state_mgr.get_signal(7202))
        s_sep = main.state_mgr.get_signal(7202)
        assert s_sep["status"] == "BE_APPLIED"
        assert s_sep["market_tickets"] == [2204]  # L4 SPARED!
        assert all(s_sep["layers"][k]["status"] == "CLOSED" for k in ("L1", "L2", "L3"))
        assert s_sep["layers"]["L4"]["status"] == "OPEN"
        assert s_sep["layers"]["L4"]["be_sl"] == 3890.5

        # L4 recovers and rides all the way to TP4!
        with patch.object(main.executor, "get_tick", return_value=_tick(3900.60)):
            await main._process_pre_tp1_or_backstop_tick("7202", main.state_mgr.get_signal(7202))
        for px in (3910.60, 3920.60, 3930.10):
            with patch.object(main.executor, "get_tick", return_value=_tick(px)):
                await main._process_runner_active_tick("7202", main.state_mgr.get_signal(7202))
        assert main.state_mgr.get_signal(7202)["tp4_reached"] is True


@pytest.mark.asyncio
async def test_3_near_miss_reversal_at_90pct_tp1_banks_profit_and_respects_m1_atr_floor(tmp_path):
    """
    Scenario 3 (Near-miss reversal near TP1):
    - Price reaches 90% of TP1 (3899.00 on 3890->3900, peak_profit=$9.00) and drops by >=30%
      ($2.80 = 31.1% >= 30% AND >= $1.50 Gold noise floor) -> bot immediately banks remaining profit!
    - Also verifies dynamic `max(1.50, 0.35 * M1_ATR_14)` noise floor when M1 ATR(14) is elevated.
    """
    main.state_mgr = StateManager(tmp_path / "s3_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "s3_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    tps = derive_tp_levels([3900.0], direction="BUY")
    main.state_mgr.create_signal(
        message_id=7301,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 3101, "status": "OPEN"},
        },
        market_tickets=[3101],
        pending_tickets=[],
        raw_tp1=tps["TP1"],
        tp_levels=tps,
        final_tp=3899.5,
        recorded_entry_price=3890.0,
        initial_price=3890.0,
    )

    alerts = []
    with patch.object(main, "notify_saved_messages", new=AsyncMock(side_effect=lambda m: alerts.append(m))):
        # Reach 90% of TP1 (3899.00) -> armed!
        with patch.object(main.executor, "get_tick", return_value=_tick(3899.00)):
            await main._process_pre_tp1_or_backstop_tick("7301", main.state_mgr.get_signal(7301))
        s_arm = main.state_mgr.get_signal(7301)
        assert s_arm["reversal_guard_armed"] is True
        assert s_arm["peak_profit"] == pytest.approx(9.00)

        # Drop 25% ($2.25 to 3896.75 < 30%) -> holds!
        with patch.object(main.executor, "get_tick", return_value=_tick(3896.75)):
            await main._process_pre_tp1_or_backstop_tick("7301", main.state_mgr.get_signal(7301))
        assert main.state_mgr.get_signal(7301)["status"] == "ACTIVE"

        # Drop 31.1% ($2.80 to 3896.20 >= 30% AND >= $1.50) -> BANKS IMMEDIATELY!
        with patch.object(main.executor, "get_tick", return_value=_tick(3896.20)):
            await main._process_pre_tp1_or_backstop_tick("7301", main.state_mgr.get_signal(7301))
        s_closed = main.state_mgr.get_signal(7301)
        assert s_closed["status"] == "CLOSED"
        assert s_closed["market_tickets"] == []
        assert "Gold Reversal Guard" in s_closed["runner_close_reason"]
        assert len(main.trade_ledger.read_trades()) == 1
        assert any("Gold Calibrated Reversal Guard Banked" in a for a in alerts)

    # Verify M1_ATR_14 dynamic floor: M1_ATR_14 = $6.00 -> floor = max(1.50, 0.35 * 6.00) = $2.10
    main.executor._dry_run_m1_atr = 6.00
    sig_atr = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {"L1": {"lot": 0.01, "target_price": 3894.0, "ticket": 3201, "status": "OPEN"}},
        "market_tickets": [3201],
        "raw_tp1": 3900.0,
        "recorded_entry_price": 3894.0,
    }
    res_arm = main.executor.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3899.40))  # 90% ($5.40 profit)
    assert res_arm["armed"] is True and res_arm["noise_floor_dollars"] == pytest.approx(2.10)
    # $1.80 drop is 33.3% (>=30%) and >=$1.50, BUT < $2.10 ATR floor -> holds!
    res_hold = main.executor.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3897.60))
    assert res_hold["triggered"] is False
    # $2.20 drop (>= $2.10 ATR floor and >= 30%) -> triggers!
    res_trig = main.executor.evaluate_gold_reversal_guard(sig_atr, tick=_tick(3897.20))
    assert res_trig["triggered"] is True


@pytest.mark.asyncio
async def test_4_gold_wick_1_dollar_and_spread_spike_hold_through_tp1_to_tp4_and_puprime_baseline(tmp_path, monkeypatch):
    """
    Scenario 4 (Gold wick & spread spike test):
    - Price reaches 90% of TP1 (3899.00), dips by $1.00 (normal Gold noise) -> holds!
    - Experiences a brief spread spike ($0.65 > $0.40 max spread while bid dips -$3.50) -> pauses & holds!
    - Continues cleanly through TP1 -> TP2 -> TP3 -> TP4!
    - Also verifies PU Prime $0.52 normal spread baseline adapts to $0.80 ceiling without blocking exits.
    """
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    main.state_mgr = StateManager(tmp_path / "s4_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "s4_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    tps = derive_tp_levels([3900.0, 3910.0, 3920.0, 3930.0], direction="BUY")
    main.state_mgr.create_signal(
        message_id=7401,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 4101, "status": "OPEN"},
        },
        market_tickets=[4101],
        pending_tickets=[],
        raw_tp1=tps["TP1"],
        raw_tp2=tps["TP2"],
        raw_tp3=tps["TP3"],
        raw_tp4=tps["TP4"],
        raw_tp5=tps["TP5"],
        tp_levels=tps,
        final_tp=3899.5,
        recorded_entry_price=3890.0,
        initial_price=3890.0,
    )

    with patch.object(main, "notify_saved_messages", new=AsyncMock()):
        # 1) Reach 90% of TP1 (3899.00, normal $0.20 spread) -> arms!
        with patch.object(main.executor, "get_tick", return_value=_tick(3899.00, spread=0.20)):
            await main._process_pre_tp1_or_backstop_tick("7401", main.state_mgr.get_signal(7401))
        assert main.state_mgr.get_signal(7401)["reversal_guard_armed"] is True

        # 2) Dip by $1.00 (normal Gold noise to 3898.00) -> MUST HOLD!
        with patch.object(main.executor, "get_tick", return_value=_tick(3898.00, spread=0.20)):
            await main._process_pre_tp1_or_backstop_tick("7401", main.state_mgr.get_signal(7401))
        assert main.state_mgr.get_signal(7401)["status"] == "ACTIVE"

        # 3) Momentary spread spike ($0.65 > $0.40) with -$2.90 bid drop (< $3.50 hard ceiling) -> PAUSED, MUST HOLD!
        with patch.object(main.executor, "get_tick", return_value=_tick(3896.10, spread=0.65)):
            await main._process_pre_tp1_or_backstop_tick("7401", main.state_mgr.get_signal(7401))
        s_spike = main.state_mgr.get_signal(7401)
        assert s_spike["status"] == "ACTIVE"
        assert s_spike["spread_spike_paused"] is True

        # 4) Spread normalizes & trade rallies through TP1 -> TP2 -> TP3 -> TP4!
        with patch.object(main.executor, "get_tick", return_value=_tick(3900.60, spread=0.20)):
            await main._process_pre_tp1_or_backstop_tick("7401", main.state_mgr.get_signal(7401))
        assert main.state_mgr.get_signal(7401)["status"] == "RUNNER_ACTIVE"

        # Also test a momentary spread spike at TP3 (3919.50 -> artificial bid 3917.50 (-$2.00 < $2.50 trail) with $0.70 spread) -> holds!
        for tk in (
            _tick(3910.60, spread=0.20),
            _tick(3919.50, spread=0.20),
            _tick(3917.50, spread=0.70),  # spread spike paused (< $2.50 trail stop & < $3.50 hard ceiling)!
            _tick(3920.60, spread=0.20),
            _tick(3930.10, spread=0.20),
        ):
            with patch.object(main.executor, "get_tick", return_value=tk):
                await main._process_runner_active_tick("7401", main.state_mgr.get_signal(7401))

        s_final = main.state_mgr.get_signal(7401)
        assert s_final["status"] == "CLOSED"
        assert s_final["tp4_reached"] is True

    # 5) Live PU Prime $0.52 normal spread adaptation check
    ex_live = MT5Executor(dry_run=False)
    is_sp_normal, sp_val, eff_ceil = ex_live.check_spread_spike("XAUUSD.s", _tick(3895.00, spread=0.52))
    assert is_sp_normal is False and sp_val == pytest.approx(0.52) and eff_ceil == pytest.approx(0.80)
    is_sp_news, _, _ = ex_live.check_spread_spike("XAUUSD.s", _tick(3895.00, spread=0.95))
    assert is_sp_news is True


def test_5_atomic_exit_lock_prevents_duplicate_close_orders_on_rapid_concurrent_ticks():
    """Verify `is_closing = True` atomic lock prevents concurrent threads from sending duplicate close orders."""
    ex = MT5Executor(dry_run=False)
    ex._connected = True
    send_count = {"n": 0}

    def slow_send(req):
        time.sleep(0.05)
        send_count["n"] += 1
        return True, SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=9999)

    fake_pos = [SimpleNamespace(ticket=5555, symbol="XAUUSD.s", volume=0.02, type=0, profit=12.0, swap=0.0)]
    with patch.object(ex_mod.mt5, "terminal_info", return_value=SimpleNamespace(connected=True, trade_allowed=True)), \
         patch.object(ex_mod.mt5, "positions_get", return_value=fake_pos), \
         patch.object(ex, "get_tick", return_value=_tick(3905.00, spread=0.20)), \
         patch.object(ex, "send_order_with_filling_fallback", side_effect=slow_send):

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(ex.close_market_position(5555)))
            for _ in range(5)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    # Exactly ONE close order sent; the 4 concurrent overlapping calls were blocked by `is_closing`!
    assert send_count["n"] == 1
    assert results.count(True) == 1
    assert results.count(False) == 4
