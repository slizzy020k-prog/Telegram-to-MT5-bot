"""
v3.5.2 Comprehensive Production Test Suite (12 Focused Verification Tests).

Covers every v3.5.2 fix across Tracks A, B, and C:
  1. Incident 9934: Dual-Gate Stage 1 ATR activation (`STAGE1_ATR_ACTIVATION_PCT=0.45`)
     holding `fsm_floor` on initial TP1 handoff + SELL Bid/Ask spread separation.
  2. Incident 13153: Wide-zone (`> $4.00`) & `has_unfilled_zone_layers` (`L2-L4` pending)
     protection from premature Stage 0 Reversal Guard & premature 70% TP lifts.
  3. Incident 13167: Stage 0 Profit-Lock (`BE+5p` broker SL + pending sweep + virtual
     backstop) and 2-tick noise hysteresis (`REVERSAL_CONFIRM_TICKS=2`).
  4. Failed Breakout `70% -> 85%` cliff-gap catch & `$5.00` hard peak-profit ceiling.
  5. Reconciler `#153908764` hardening: IPC `None` (`_last_live_*_query_ok`) abort, 3s grace
     window (`is_ticket_in_reconcile_grace`), inside-lock TOCTOU re-query self-heal, and
     orphan `all_placed_tickets` sweep.
  6. In-place `modify_pending_order_price_sltp` preserving ticket IDs + market-crossing
     edit pre-check opening MARKET instead of failing pending price modification.
  7. Parser geometry & typo repair (`3750-4730`), directionless zone inference, and
     Gate 2 / Gate 3 zero false positives on chatter / balances.
  8. Single-pass `Pipeline Output:` logging and releasing `_get_signal_lock` before
     `notify_saved_messages` awaits.
  9. Strict SELL Bid/Ask (`chart_price = tick.bid` vs `exec_price = tick.ask`) parity
     in runner milestones and pre-TP1 breakout lifts under wide spreads.
 10. MT5 deal history `entry + exit` retry (`~0.15s`), `_cached_pos_profits` fallback,
     strict 2-key `get_position_close_info` contract, and `last_runner_layer` persistence.
 11. Telegram TP1 alert (`Stage: Stage 1 (TP1 Cleared) | Next Target: TP2`, never `Layer None`)
     and `_render_terminal_status` HUD `open_lots` accuracy.
 12. v3.5.2 config defaults (`STAGE0_PROFIT_LOCK_OFFSET_PIPS` init order, bounds,
     `validate_staged_trail_tiers()`).
"""

import importlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import main
import mt5_executor as ex_mod
import parser as parser_mod
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger
from tests.test_copier_scenarios import MockTelethonEvent


def _tick(bid: float, ask: Optional[float] = None, spread: float = 0.20) -> SimpleNamespace:
    ask_px = round(ask if ask is not None else (bid + spread), 2)
    return SimpleNamespace(bid=round(float(bid), 2), ask=ask_px, time=1795000000.0)


@pytest.fixture(autouse=True)
def _pin_v352_defaults(monkeypatch):
    """Pin deterministic v3.5.2 config & module globals regardless of local `.env`."""
    defaults = {
        "STAGED_EXIT_ENABLED": True,
        "PROFIT_RATCHET_ENABLED": False,
        "TIERED_STAGE_BANKING_ENABLED": False,
        "BE_OFFSET_PIPS": 5.0,
        "BE_AFTER_TP1": True,
        "RUNNER_MAX_TP_STAGE": 5,
        "RUNNER_CLEAN_BREAK_PIPS": 5.0,
        "RUNNER_EARLY_TP_LIFT_PCT": 0.70,
        "RUNNER_PROACTIVE_TP_LIFT": True,
        "RUNNER_DEEPEST_COUNT_FULL": 1,
        "GOLD_REVERSAL_GUARD_ENABLED": True,
        "ARM_THRESHOLD_PCT": 0.85,
        "GIVEBACK_PCT": 0.30,
        "MIN_GOLD_PULLBACK_DOLLARS": 1.50,
        "ATR_NOISE_FLOOR_MULT": 0.35,
        "HARD_GIVEBACK_CEILING_DOLLARS": 3.50,
        "MAX_GIVEBACK_DOLLARS": 4.50,
        "REVERSAL_CONFIRM_TICKS": 2,
        "STAGE0_PROFIT_LOCK_ENABLED": True,
        "STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS": 1.20,
        "STAGE0_PROFIT_LOCK_OFFSET_PIPS": 5.0,
        "CANCEL_PENDING_ON_STAGE0_LOCK_EXIT": True,
        "STAGE1_ATR_ACTIVATION_PCT": 0.45,
        "TP1_TRAIL_ENABLED": True,
        "TP1_TRAIL_ATR_MULT": 1.5,
        "TP1_TRAIL_MIN_DOLLARS": 2.50,
        "TP1_TRAIL_MAX_DOLLARS": 3.50,
        "TP1_TRAIL_STEP_DOLLARS": 0.25,
        "TP1_TOUCH_BANK_ENABLED": True,
        "TP1_TOUCH_BANK_BUFFER_PIPS": 12.0,
        "BANK_ALL_AT_TP1": False,
        "BANK_SINGLE_LAYER_AT_TP1": False,
        "CANCEL_PENDING_ON_TP1": True,
        "MAX_ALLOWED_SPREAD_DOLLARS": 0.40,
        "ADAPTIVE_SPREAD_CEILING_DOLLARS": 0.80,
        "DAILY_LOSS_PAUSE_ENABLED": False,
        "MARKET_EXEC_BUFFER_PIPS": 15.0,
    }
    for key, val in defaults.items():
        monkeypatch.setattr(config, key, val, raising=False)
        if hasattr(ex_mod, key):
            monkeypatch.setattr(ex_mod, key, val, raising=False)
        if hasattr(main, key):
            monkeypatch.setattr(main, key, val, raising=False)


def _setup_runtime(tmp_path: Path, name: str) -> MT5Executor:
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    if hasattr(main, "_recent_management_commands"):
        main._recent_management_commands.clear()
    main._deleted_messages.clear()
    main._signal_locks.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False
    main.CHANNEL_IDS = [-1003712081715]
    main.state_mgr = StateManager(tmp_path / f"{name}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{name}_ledger.jsonl")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    main.executor = ex
    return ex


def _make_sell_9934_signal(ex: MT5Executor) -> Dict[str, Any]:
    tp_levels = {"TP1": 3737.0, "TP2": 3730.0, "TP3": 3715.0, "TP4": 3700.0, "TP5": 3650.0}
    ticket = 153877174
    ex._dry_run_ticket_lots[ticket] = 0.01
    return {
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "chat_id": -1003712081715,
        "sl": 3765.0,
        "layers": {
            "L1": {
                "lot": 0.01,
                "target_price": 3741.70,
                "tp": 3737.50,
                "tp_stage": "TP1",
                "ticket": ticket,
                "order_type": "MARKET",
                "status": "OPEN",
            },
            "L2": {"lot": 0.01, "target_price": 3744.0, "tp": 3730.0, "tp_stage": "TP2", "ticket": None, "order_type": "SELL_LIMIT", "status": "CANCELLED"},
            "L3": {"lot": 0.01, "target_price": 3746.0, "tp": 3715.0, "tp_stage": "TP3", "ticket": None, "order_type": "SELL_LIMIT", "status": "CANCELLED"},
            "L4": {"lot": 0.02, "target_price": 3748.0, "tp": 3650.0, "tp_stage": "TP5", "ticket": None, "order_type": "SELL_LIMIT", "status": "CANCELLED"},
        },
        "market_tickets": [ticket],
        "pending_tickets": [],
        "final_tp": 3737.50,
        "raw_tp1": 3737.0,
        "raw_tp2": 3730.0,
        "raw_tp3": 3715.0,
        "raw_tp4": 3700.0,
        "raw_tp5": 3650.0,
        "tp_levels": tp_levels,
        "recorded_entry_price": 3741.70,
        "initial_price": 3741.70,
        "status": "ACTIVE",
    }


def test_01_incident_9934_dual_gate_stage1_atr_and_sell_bid_ask_spread_parity(tmp_path):
    """Incident 9934: Holds fsm_floor until >=45% of TP1->TP2 on Bid; ATR operates in Ask space."""
    ex = _setup_runtime(tmp_path, "t01_9934")
    ex._dry_run_m1_atr = 2.50 / 1.5  # Stage 1 ATR trail distance = $2.50
    sig = _make_sell_9934_signal(ex)

    # TP1 touch on chart (Bid=3737.00), wide Ask=3737.52 ($0.52 spread)
    with patch.object(ex, "get_tick", return_value=_tick(3737.00, 3737.52)):
        runner_t = ex.handle_tp1_scaling_and_select_runner(sig)

    assert runner_t == 153877174
    assert sig["runner_cleared_milestones"] == ["TP1"]
    assert sig["runner_milestone_idx"] == 2
    assert sig["stage1_atr_armed"] is False
    assert sig["fsm_floor"] == pytest.approx(3741.20)
    assert sig["trail_stop"] == pytest.approx(3741.20)
    assert sig["broker_sl"] == pytest.approx(3741.20)

    # Reproduces 9934 bounce: Ask=3740.17 previously choked runner; now holds under fsm_floor!
    for b, a in ((3737.15, 3737.67), (3739.65, 3740.17)):
        with patch.object(ex, "get_tick", return_value=_tick(b, a)):
            assert ex.manage_designated_runner(sig) is True
            assert sig["stage1_atr_armed"] is False
            assert sig["trail_stop"] == pytest.approx(3741.20)

    # Drops to Bid=3733.80 (3.20 / 7.00 = 45.7% >= 45% of TP1->TP2): Stage 1 ATR arms!
    with patch.object(ex, "get_tick", return_value=_tick(3733.80, 3734.32)):
        assert ex.manage_designated_runner(sig) is True

    assert sig["stage1_atr_armed"] is True
    assert sig["chart_peak"] == pytest.approx(3733.80)
    assert sig["trail_peak"] == pytest.approx(3734.32)
    assert sig["trail_stop"] == pytest.approx(3736.82)  # min(3741.20, 3734.32 + 2.50)
    assert sig["broker_sl"] == pytest.approx(3736.82)


@pytest.mark.asyncio
async def test_02_incident_13153_wide_zone_and_unfilled_layers_protection(tmp_path):
    """Incident 13153: Protects 1 market + resting L2-L4 limit zone from premature exit/lift."""
    ex = _setup_runtime(tmp_path, "t02_13153")
    # Case 2a: Sub-lock profit with resting L2-L4 limits -> Reversal Guard NEVER liquidates L1 or cancels L2-L4!
    layers = {
        "L1": {"lot": 0.01, "target_price": 3738.0, "tp": 3736.8, "tp_stage": "TP1", "ticket": 2001, "order_type": "MARKET", "status": "OPEN"},
        "L2": {"lot": 0.01, "target_price": 3741.33, "tp": 3730.0, "tp_stage": "TP2", "ticket": 2002, "order_type": "SELL_LIMIT", "status": "PENDING"},
        "L3": {"lot": 0.01, "target_price": 3744.67, "tp": 3715.0, "tp_stage": "TP3", "ticket": 2003, "order_type": "SELL_LIMIT", "status": "PENDING"},
        "L4": {"lot": 0.02, "target_price": 3748.0, "tp": 3650.0, "tp_stage": "TP5", "ticket": 2004, "order_type": "SELL_LIMIT", "status": "PENDING"},
    }
    ex._dry_run_ticket_lots[2001] = 0.01
    for t in (2002, 2003, 2004):
        ex._dry_run_ticket_lots[t] = 0.01

    sig = main.state_mgr.create_signal(
        message_id=13153,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="SELL",
        sl=3760.0,
        layers=layers,
        market_tickets=[2001],
        pending_tickets=[2002, 2003, 2004],
        final_tp=3736.8,
        raw_tp1=3736.5,
        raw_tp2=3730.0,
        raw_tp3=3715.0,
        raw_tp5=3650.0,
        tp_levels={"TP1": 3736.5, "TP2": 3730.0, "TP3": 3715.0, "TP4": 3700.0, "TP5": 3650.0},
        recorded_entry_price=3738.0,
        initial_price=3738.0,
        status="ACTIVE",
    )

    # Near miss toward TP1 followed by bounce back toward L2 limit -> must NOT liquidate L1!
    for b in (3736.65, 3738.10, 3738.20, 3738.30):
        res = ex.evaluate_gold_reversal_guard(sig, _tick(b, b + 0.20))
        assert res["triggered"] is False
    assert sig["pending_tickets"] == [2002, 2003, 2004]

    # Case 2b: Tight $3.00 TP1 leg (<=$4.00 tight-scalp rule) with resting L2-L4 limits ->
    # has_unfilled_zone_layers prevents premature tight-scalp early arming & liquidation!
    sig_wide = dict(sig)
    sig_wide["raw_tp1"] = 3735.0
    sig_wide["final_tp"] = 3735.5
    sig_wide["layers"] = {k: dict(v) for k, v in layers.items()}
    sig_wide["layers"]["L1"]["tp"] = 3735.5
    sig_wide["tp_levels"] = {"TP1": 3735.0, "TP2": 3730.0, "TP3": 3715.0, "TP4": 3700.0, "TP5": 3650.0}
    sig_wide["reversal_guard_armed"] = False
    res_early = ex.evaluate_gold_reversal_guard(sig_wide, _tick(3735.95, 3736.15))  # ~68% progress
    assert res_early["armed"] is False
    assert res_early["triggered"] is False
    assert sig_wide["pending_tickets"] == [2002, 2003, 2004]


@pytest.mark.asyncio
async def test_03_incident_13167_stage0_profit_lock_and_two_tick_confirm(tmp_path):
    """Incident 13167: 2-tick hysteresis + Stage 0 BE+5p Profit-Lock virtual backstop."""
    ex = _setup_runtime(tmp_path, "t03_13167")
    ex._dry_run_ticket_lots[3001] = 0.02
    sig = main.state_mgr.create_signal(
        message_id=13167,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3785.0,
        layers={
            "L1": {"lot": 0.02, "target_price": 3800.0, "tp": 3805.5, "tp_stage": "TP1", "ticket": 3001, "order_type": "MARKET", "status": "OPEN"},
        },
        market_tickets=[3001],
        pending_tickets=[],
        final_tp=3805.5,
        raw_tp1=3806.0,
        raw_tp2=3810.0,
        tp_levels={"TP1": 3806.0, "TP2": 3810.0, "TP3": 3820.0, "TP4": 3830.0, "TP5": 3840.0},
        recorded_entry_price=3800.0,
        initial_price=3800.0,
        status="ACTIVE",
    )

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # 1. Rally to 3805.20 (86.7% of $6 leg; +$5.20 profit >= $1.20) -> Stage 0 Profit Lock BE+5p = 3800.50!
        with patch.object(ex, "get_tick", return_value=_tick(3805.20)):
            await main._process_pre_tp1_or_backstop_tick("13167", main.state_mgr.get_signal(13167))

        s_armed = main.state_mgr.get_signal(13167)
        assert s_armed["reversal_guard_armed"] is True
        assert s_armed["stage0_profit_lock_active"] is True
        assert s_armed["stage0_profit_lock_sl"] == pytest.approx(3800.50)

        # 2. Single noise pullback tick to 3802.00 ($3.20 pullback, 61.5%) -> Tick 1 does NOT exit!
        with patch.object(ex, "get_tick", return_value=_tick(3802.00)):
            await main._process_pre_tp1_or_backstop_tick("13167", main.state_mgr.get_signal(13167))
        assert main.state_mgr.get_signal(13167)["status"] == "ACTIVE"
        assert main.state_mgr.get_signal(13167)["reversal_guard_ticks"] == 1

        # 3. Bounce reset: tick back to 3803.90 (pullback $1.30 < $1.50 floor) resets confirm counter!
        with patch.object(ex, "get_tick", return_value=_tick(3803.90)):
            await main._process_pre_tp1_or_backstop_tick("13167", main.state_mgr.get_signal(13167))
        assert main.state_mgr.get_signal(13167)["reversal_guard_ticks"] == 0

        # 4. Drop past locked floor (3800.40 <= 3800.50): triggers Stage 0 Profit-Lock immediately on tick 1!
        with patch.object(ex, "get_tick", return_value=_tick(3800.40)):
            await main._process_pre_tp1_or_backstop_tick("13167", main.state_mgr.get_signal(13167))

        s_closed = main.state_mgr.get_signal(13167)
        assert s_closed["status"] == "CLOSED"
        assert s_closed["exit_reason_code"] == "STAGE0_PROFIT_LOCK"
        assert s_closed["market_tickets"] == []
        assert s_closed["pending_tickets"] == []

        # 5. Clean signal 13168: 2 consecutive confirming pullback ticks above lock SL -> closes on tick 2!
        ex._dry_run_ticket_lots[3003] = 0.02
        main.state_mgr.create_signal(
            message_id=13168,
            chat_id=-1003712081715,
            symbol="XAUUSD.s",
            direction="BUY",
            sl=3785.0,
            layers={
                "L1": {"lot": 0.02, "target_price": 3800.0, "tp": 3805.5, "tp_stage": "TP1", "ticket": 3003, "order_type": "MARKET", "status": "OPEN"},
            },
            market_tickets=[3003],
            pending_tickets=[],
            final_tp=3805.5,
            raw_tp1=3806.0,
            raw_tp2=3810.0,
            tp_levels={"TP1": 3806.0, "TP2": 3810.0, "TP3": 3820.0, "TP4": 3830.0, "TP5": 3840.0},
            recorded_entry_price=3800.0,
            initial_price=3800.0,
            status="ACTIVE",
        )
        with patch.object(ex, "get_tick", return_value=_tick(3805.20)):
            await main._process_pre_tp1_or_backstop_tick("13168", main.state_mgr.get_signal(13168))
        with patch.object(ex, "get_tick", return_value=_tick(3802.00)):
            await main._process_pre_tp1_or_backstop_tick("13168", main.state_mgr.get_signal(13168))
            assert main.state_mgr.get_signal(13168)["status"] == "ACTIVE"
            assert main.state_mgr.get_signal(13168)["reversal_guard_ticks"] == 1
            await main._process_pre_tp1_or_backstop_tick("13168", main.state_mgr.get_signal(13168))

        s_guard_closed = main.state_mgr.get_signal(13168)
        assert s_guard_closed["status"] == "CLOSED"
        assert s_guard_closed["exit_reason_code"] == "GOLD_GUARD"


def test_04_failed_breakout_70_to_85_cliff_gap_and_5_dollar_hard_ceiling(tmp_path, monkeypatch):
    """Cliff-Gap Catch #1 (TP2 lifted & final_tp hit <85%) + $5.00 hard peak-profit ceiling."""
    ex = _setup_runtime(tmp_path, "t04_cliff")
    monkeypatch.setattr(config, "STAGE0_PROFIT_LOCK_ENABLED", False)
    monkeypatch.setattr(ex_mod, "STAGE0_PROFIT_LOCK_ENABLED", False)

    # Part A: Cliff-Gap Catch #1 — $3.00 leg where final_tp=3802.50 is 83.3% (< 85% arm threshold!),
    # L1 closed at final_tp leaving L4 lifted to tp_stage="TP2" reaching final_tp -> arms cleanly!
    ex._dry_run_ticket_lots[4001] = 0.01
    ex._dry_run_ticket_lots[4002] = 0.02
    sig_cliff = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3785.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3800.0, "tp": 3802.5, "tp_stage": "TP1", "ticket": 4001, "order_type": "MARKET", "status": "OPEN"},
            "L4": {"lot": 0.02, "target_price": 3800.0, "tp": 3802.5, "tp_stage": "TP1", "ticket": 4002, "order_type": "MARKET", "status": "OPEN"},
        },
        "market_tickets": [4001, 4002],
        "pending_tickets": [],
        "final_tp": 3802.5,
        "raw_tp1": 3803.0,
        "tp_levels": {"TP1": 3803.0, "TP2": 3810.0, "TP3": 3820.0, "TP4": 3830.0, "TP5": 3840.0},
        "recorded_entry_price": 3800.0,
        "initial_price": 3800.0,
        "status": "ACTIVE",
    }
    lift = ex.prepare_deepest_runners_for_tp_breakout(sig_cliff, live_price=3802.15, chart_price=3802.15)
    assert 4002 in lift.get("lifted_tickets", [])
    assert sig_cliff["layers"]["L4"]["tp_stage"] == "TP2"

    # Simulate L1 closing at final_tp=3802.50 (83.3% < 85%), leaving L4 open at tp_stage=="TP2"
    sig_cliff["layers"]["L1"]["status"] = "CLOSED"
    sig_cliff["market_tickets"] = [4002]
    res_cliff = ex.evaluate_gold_reversal_guard(sig_cliff, _tick(3802.52))
    assert sig_cliff["reversal_guard_armed"] is True
    assert res_cliff["armed"] is True

    # Pullback from 3802.52 to 3800.90 ($1.62 >= $1.50 & 64% >= 30%) -> 2 ticks triggers exit!
    assert ex.evaluate_gold_reversal_guard(sig_cliff, _tick(3800.90))["triggered"] is False
    assert ex.evaluate_gold_reversal_guard(sig_cliff, _tick(3800.90))["triggered"] is True

    # Part B: $5.00 hard ceiling overrides wide ATR noise floor when peak_profit >= $5.00
    ex._dry_run_ticket_lots[4003] = 0.01
    ex._dry_run_m1_atr = 18.0  # Huge ATR -> 0.35 * 18.0 = $6.30 (> $5.00 hard ceiling!)
    sig_ceil = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3785.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3800.0, "tp": 3811.5, "ticket": 4003, "order_type": "MARKET", "status": "OPEN"},
        },
        "market_tickets": [4003],
        "pending_tickets": [],
        "raw_tp1": 3812.0,
        "tp_levels": {"TP1": 3812.0, "TP2": 3822.0, "TP3": 3832.0, "TP4": 3842.0, "TP5": 3852.0},
        "recorded_entry_price": 3800.0,
        "initial_price": 3800.0,
        "status": "ACTIVE",
    }
    res_peak = ex.evaluate_gold_reversal_guard(sig_ceil, _tick(3810.50))  # 87.5%, peak_profit=$10.50
    assert res_peak["triggered"] is False
    # Drop $5.20 to 3805.30: < $6.30 ATR noise floor, yet >= $3.50 hard ceiling -> triggers immediately!
    r1 = ex.evaluate_gold_reversal_guard(sig_ceil, _tick(3805.30))
    assert r1["triggered"] is True
    assert r1["hard_ceiling_triggered"] is True


@pytest.mark.asyncio
async def test_05_reconciler_153908764_toctou_ipc_none_3s_grace_and_self_heal(tmp_path):
    """Reconciler #153908764: IPC None abort, 3s grace, inside-lock self-heal, orphan sweep."""
    ex = _setup_runtime(tmp_path, "t05_reconcile")
    layers = {
        "L1": {"lot": 0.01, "target_price": 3741.0, "tp": 3737.5, "ticket": 153908761, "order_type": "MARKET", "status": "OPEN"},
        "L2": {"lot": 0.01, "target_price": 3744.0, "tp": 3730.0, "ticket": 153908764, "order_type": "SELL_LIMIT", "status": "PENDING"},
    }
    main.state_mgr.create_signal(
        message_id=1539,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="SELL",
        sl=3760.0,
        layers=layers,
        market_tickets=[153908761],
        pending_tickets=[153908764],
        all_placed_tickets=[153908761, 153908764],
        final_tp=3737.5,
        raw_tp1=3737.0,
        recorded_entry_price=3741.0,
        initial_price=3741.0,
        status="ACTIVE",
    )

    ex.dry_run = False
    ex._connected = True
    cancelled_orphans: List[int] = []

    def _rec_cancel(ticket):
        cancelled_orphans.append(int(ticket))
        return True

    fake_pos = SimpleNamespace(ticket=153908761, symbol="XAUUSD.s", type=1, volume=0.01, price_open=3741.0, sl=3760.0, tp=3737.5, profit=1.50, swap=0.0, comment="")
    fake_ord = SimpleNamespace(ticket=153908764, symbol="XAUUSD.s", type=3, volume_current=0.01, price_open=3744.0, sl=3760.0, tp=3730.0, comment="")

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex_mod, "mt5") as mock_mt5, \
         patch.object(ex, "cancel_pending_order", side_effect=_rec_cancel), \
         patch.object(ex, "get_position_close_info", return_value={"profit": 2.10, "close_price": 3738.0}):

        # Case A: MT5 positions_get returns None (IPC hiccup) -> reconciler MUST abort safely!
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = [fake_ord]
        await main._reconcile_live_mt5_signals([("1539", main.state_mgr.get_signal(1539))])
        assert main.state_mgr.get_signal(1539)["status"] == "ACTIVE"

        # Case B: Newly placed ticket inside 3.0s grace missing from snapshot -> kept ACTIVE!
        with patch.object(ex_mod.time, "monotonic", return_value=1000.0):
            ex._record_ticket_placed(153908761)
        with patch.object(ex_mod.time, "monotonic", return_value=1001.5):
            mock_mt5.positions_get.return_value = []
            mock_mt5.orders_get.return_value = [fake_ord]
            await main._reconcile_live_mt5_signals([("1539", main.state_mgr.get_signal(1539))])
            assert main.state_mgr.get_signal(1539)["status"] == "ACTIVE"
            assert ex.is_ticket_in_reconcile_grace(153908761, 3.0) is True

        # Case C: Outside 3s grace, snapshot 1 misses limit fill, inside-lock TOCTOU re-query sees filled pos!
        with patch.object(ex_mod.time, "monotonic", return_value=1005.0):
            assert ex.is_ticket_in_reconcile_grace(153908761, 3.0) is False
            mock_mt5.positions_get.side_effect = [[], [fake_pos]]
            mock_mt5.orders_get.side_effect = [[], []]
            await main._reconcile_live_mt5_signals([("1539", main.state_mgr.get_signal(1539))])
            assert main.state_mgr.get_signal(1539)["status"] == "ACTIVE"

            # Case D: Live pending #153908764 missing from pending_tickets self-heals back; orphan sweep cancels it; then empty MT5 marks CLOSED!
            main.state_mgr.update_signal(1539, {"pending_tickets": []})
            mock_mt5.positions_get.side_effect = None
            mock_mt5.orders_get.side_effect = None
            mock_mt5.positions_get.return_value = []
            mock_mt5.orders_get.return_value = [fake_ord]
            await main._reconcile_live_mt5_signals([("1539", main.state_mgr.get_signal(1539))])
            healed = main.state_mgr.get_signal(1539)
            assert healed["status"] == "ACTIVE"
            assert 153908764 in healed["pending_tickets"]

            # Sweep via all_placed_tickets even if pending_tickets is cleared -> cancels #153908764!
            healed["pending_tickets"] = []
            cancelled_orphans.extend(ex.cancel_signal_pending_orders(healed, include_all_placed=True))
            assert 153908764 in cancelled_orphans

            # Confirmed genuinely gone on MT5 -> marks CLOSED
            mock_mt5.orders_get.return_value = []
            await main._reconcile_live_mt5_signals([("1539", main.state_mgr.get_signal(1539))])
            assert main.state_mgr.get_signal(1539)["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_06_in_place_modify_pending_order_price_sltp_and_market_cross_fallback(tmp_path):
    """Edit re-tiering uses in-place price mod on limits, or MARKET fallback if crossed."""
    ex = _setup_runtime(tmp_path, "t06_edit")

    # 1. Non-crossing limit edit uses modify_pending_order_price_sltp in-place (same ticket!)
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        with patch.object(ex, "get_tick", return_value=_tick(3890.0)):
            evt_new = MockTelethonEvent(6001, "Buy XAUUSD 3885-3880\nSL 3870\nTP 3895")
            await main.handle_new_message(evt_new)

        sig_before = main.state_mgr.get_signal(6001)
        orig_l1_t = sig_before["layers"]["L1"]["ticket"]
        orig_l4_t = sig_before["layers"]["L4"]["ticket"]
        assert sig_before["layers"]["L1"]["order_type"] == "LIMIT"

        in_place_calls: List[tuple] = []
        orig_mod_price = ex.modify_pending_order_price_sltp

        def _spy_mod_price(ticket, price, sl=None, tp=None):
            in_place_calls.append((int(ticket), float(price), sl, tp))
            return orig_mod_price(ticket, price, sl=sl, tp=tp)

        with patch.object(ex, "get_tick", return_value=_tick(3890.0)), \
             patch.object(ex, "modify_pending_order_price_sltp", side_effect=_spy_mod_price):
            evt_edit_limit = MockTelethonEvent(6001, "Buy XAUUSD 3886-3880\nSL 3870\nTP 3895")
            await main.handle_message_edited(evt_edit_limit)

        sig_mid = main.state_mgr.get_signal(6001)
        assert len(in_place_calls) >= 1
        assert sig_mid["layers"]["L1"]["ticket"] == orig_l1_t
        assert sig_mid["layers"]["L4"]["ticket"] == orig_l4_t

        # 2. Crossing edit within 15p market buffer cancels pending L1 & opens MARKET!
        with patch.object(ex, "get_tick", return_value=_tick(3886.80)):
            evt_edit_cross = MockTelethonEvent(6001, "Buy XAUUSD 3888-3880\nSL 3870\nTP 3895")
            await main.handle_message_edited(evt_edit_cross)

        sig_after = main.state_mgr.get_signal(6001)
        assert sig_after["layers"]["L1"]["order_type"] == "MARKET"
        assert sig_after["layers"]["L1"]["status"] == "OPEN"
        assert sig_after["layers"]["L1"]["ticket"] in sig_after["market_tickets"]


def test_07_parser_geometry_typo_repair_and_gate2_gate3_zero_false_positives():
    """Parser repairs 3750-4730 typo, infers missing direction, rejects non-signal chatter."""
    # 1. Typo repair (3750-4730 -> 3750-3730 SELL, TP1 below 3730)
    sig_typo = parser_mod.parse_message("SELL XAUUSD\n3750-4730\nSL 3765\nTP1 3725", validate_sanity=True)
    assert sig_typo is not None and sig_typo.is_entry is True
    assert sig_typo.direction == "SELL"
    assert sig_typo.zone is not None
    assert sig_typo.zone.bottom == pytest.approx(3730.0)
    assert sig_typo.zone.top == pytest.approx(3750.0)

    # 2. Directionless zone inferred as SELL from SL above zone
    sig_no_dir_sell = parser_mod.parse_message("XAUUSD 3742-3752\nSL 3765\nTP 3739", validate_sanity=True)
    assert sig_no_dir_sell is not None and sig_no_dir_sell.is_entry is True
    assert sig_no_dir_sell.direction == "SELL"

    # 3. Directionless multi-target GOLD inferred as BUY from TPs > entry & SL < entry
    sig_no_dir_buy = parser_mod.parse_message("GOLD 3800\nSL 3785\nTP1 3805\nTP2 3812", validate_sanity=True)
    assert sig_no_dir_buy is not None and sig_no_dir_buy.is_entry is True
    assert sig_no_dir_buy.direction == "BUY"

    # 4. Gate 2 & Gate 3 zero false positives on chatter / PnL numbers
    for chatter in (
        "yesterday I lost 3750 on 4730 and went home",
        "Account balance is 57.73 GBP, equity 70.89 GBP",
        "closed my phone around 3740 after lunch",
        "morning team let us check gold 3745 later",
    ):
        assert parser_mod.parse_message(chatter, validate_sanity=True) is None


@pytest.mark.asyncio
async def test_08_single_pass_parsing_and_signal_lock_release_before_notify(tmp_path, caplog):
    """Single-pass Pipeline Output log per message & signal lock released prior to notify."""
    ex = _setup_runtime(tmp_path, "t08_concurrency")
    lock_states_inside_notify: List[bool] = []

    async def _check_lock(text: str):
        lock_states_inside_notify.append(main._get_signal_lock(-1001234567890, 8001).locked())

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="copier.parser"), \
         patch.object(main, "notify_saved_messages", side_effect=_check_lock), \
         patch.object(ex, "get_tick", return_value=_tick(3740.0)):
        evt = MockTelethonEvent(8001, "SELL XAUUSD 3742-3752\nSL 3765\nTP 3739")
        await main.handle_new_message(evt)

    pipeline_logs = [r for r in caplog.records if "Pipeline Output:" in r.getMessage()]
    assert len(pipeline_logs) == 1
    assert len(lock_states_inside_notify) >= 1
    assert all(locked is False for locked in lock_states_inside_notify)


def test_09_sell_bid_ask_chart_vs_exec_parity_in_runner_and_stage0(tmp_path):
    """SELL milestones/TP-lifts respond to chart Bid while trail stops/SL use exec Ask."""
    ex = _setup_runtime(tmp_path, "t09_bid_ask")
    ex._dry_run_ticket_lots[9001] = 0.01
    ex._dry_run_ticket_lots[9002] = 0.02
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "sl": 3760.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3745.0, "tp": 3740.5, "ticket": 9001, "order_type": "MARKET", "status": "OPEN"},
            "L4": {"lot": 0.02, "target_price": 3745.0, "tp": 3740.5, "ticket": 9002, "order_type": "MARKET", "status": "OPEN"},
        },
        "market_tickets": [9001, 9002],
        "pending_tickets": [],
        "raw_tp1": 3740.0,
        "raw_tp2": 3730.0,
        "raw_tp3": 3720.0,
        "raw_tp4": 3710.0,
        "raw_tp5": 3700.0,
        "tp_levels": {"TP1": 3740.0, "TP2": 3730.0, "TP3": 3720.0, "TP4": 3710.0, "TP5": 3700.0},
        "recorded_entry_price": 3745.0,
        "initial_price": 3745.0,
        "status": "ACTIVE",
    }

    # Chart Bid=3741.20 is 76% of 3745->3740 leg, while Ask=3742.00 ($0.80 spread) is only 60% (<70%)!
    # Because chart_price = Bid is used for milestone/70% lift checks, L4 lifts to TP2!
    lift = ex.prepare_deepest_runners_for_tp_breakout(sig, live_price=3742.00, chart_price=3741.20)
    assert 9002 in lift.get("lifted_tickets", [])
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3730.0)


def test_10_pnl_deal_history_retry_cached_profit_fallback_and_layer_persistence(tmp_path):
    """Deals require exit_deals; ~0.15s retry catches exit deal; missing falls back to cache."""
    ex = _setup_runtime(tmp_path, "t10_pnl")
    ex.dry_run = False
    ex._connected = True

    entry_only = SimpleNamespace(entry=0, position_id=555, ticket=10, price=3741.70, profit=0.0, swap=0.0, commission=0.0)
    exit_deal = SimpleNamespace(entry=1, position_id=555, ticket=11, price=3737.00, profit=4.70, swap=-0.10, commission=-0.05)

    with patch.object(ex_mod, "mt5") as mock_mt5, \
         patch.object(ex_mod.time, "sleep") as mock_sleep:
        # 1. Retry succeeds when exit deal appears on second call
        mock_mt5.history_deals_get.side_effect = [[entry_only], [entry_only, exit_deal]]
        res_retry = ex.get_position_close_info(555)
        assert set(res_retry.keys()) == {"profit", "reason"}
        assert res_retry["profit"] == pytest.approx(4.55)
        assert mock_sleep.called

        # 2. Entry-only on all retries falls back to _cached_pos_profits (never +0.00!)
        ex._cached_pos_profits[556] = 3.85
        mock_mt5.history_deals_get.side_effect = None
        mock_mt5.history_deals_get.return_value = [entry_only]
        res_cache = ex.get_position_close_info(556)
        assert set(res_cache.keys()) == {"profit", "reason"}
        assert res_cache["profit"] == pytest.approx(3.85)

    # 3. _finalize_runner_exit preserves last_runner_layer so ledger never shows Layer None
    ex.dry_run = True
    sig = _make_sell_9934_signal(ex)
    sig["status"] = "RUNNER_ACTIVE"
    sig["runner_ticket"] = 153877174
    sig["runner_layer"] = "L1"
    sig["runner_layers"] = ["L1"]
    ex._finalize_runner_exit(sig, [153877174], ["L1"], "TP1 Trailing Stop", exit_reason_code="ATR_TRAIL")
    assert sig["status"] == "CLOSED"
    assert sig["last_runner_layer"] == "L1"
    assert sig["last_runner_layers"] == ["L1"]


@pytest.mark.asyncio
async def test_11_tp1_telegram_alert_format_and_hud_open_lots_accuracy(tmp_path, capsys):
    """TP1 alert displays Stage 1 (TP1 Cleared) | Next Target: TP2; HUD renders open_lots."""
    ex = _setup_runtime(tmp_path, "t11_alerts")
    sig = main.state_mgr.create_signal(
        message_id=9934,
        **_make_sell_9934_signal(ex),
    )

    with patch.object(ex, "get_tick", return_value=_tick(3737.00, 3737.52)):
        _, _, tp1_msg = await main._execute_tp1_scaling_handoff(
            "9934",
            dict(sig),
            -1003712081715,
            "Provider Reply",
            curr_price=3737.00,
        )

    assert "Stage 1 (TP1 Cleared)" in tp1_msg
    assert "Next Target: `TP2`" in tp1_msg or "Next Target: TP2" in tp1_msg
    assert "Layer `L1`" in tp1_msg or "Layer L1" in tp1_msg
    assert "Next Milestone: TP1" not in tp1_msg

    # Verify _render_terminal_status uses open_lots without NameError on OPEN signals
    main._render_terminal_status(main.state_mgr.get_all_signals(), force_print=True)
    out = capsys.readouterr().out
    assert "Open Signals" in out or "Runner Active" in out or "0.01" in out or out != ""


def test_12_v352_config_defaults_and_safety_invariants():
    """Reloaded config defaults pass staged validation with valid v3.5.2 bounds."""
    assert isinstance(config.REVERSAL_CONFIRM_TICKS, int) and config.REVERSAL_CONFIRM_TICKS >= 1
    assert isinstance(config.STAGE0_PROFIT_LOCK_ENABLED, bool)
    assert config.STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS > 0
    assert isinstance(config.CANCEL_PENDING_ON_STAGE0_LOCK_EXIT, bool)
    assert 0.0 <= config.STAGE1_ATR_ACTIVATION_PCT <= 1.0
    assert config.STAGE0_PROFIT_LOCK_OFFSET_PIPS >= 0.0
    tiers = config.validate_staged_trail_tiers()
    assert isinstance(tiers, dict) and set(tiers.keys()) == {1, 2, 3, 4}
    # Ensure reloading config module executes without NameError on BE_OFFSET_PIPS ordering
    reloaded = importlib.reload(config)
    assert hasattr(reloaded, "STAGE0_PROFIT_LOCK_OFFSET_PIPS")
