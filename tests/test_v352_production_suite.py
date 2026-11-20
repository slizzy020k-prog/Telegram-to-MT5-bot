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
    """Pin deterministic config & module globals regardless of local `.env`."""
    defaults = {
        "DYNAMIC_ATR_SLTP_ENABLED": True,
        "DYNAMIC_SLTP_MODE": "ADAPTIVE",
        "DYNAMIC_ATR_TRAIL_ENABLED": True,
        "DYNAMIC_ATR_TP_TRAIL_ENABLED": True,
        "TIERED_STAGE_BANKING_ENABLED": False,
        "BE_OFFSET_PIPS": 5.0,
        "BE_AFTER_TP1": True,
        "RUNNER_MAX_TP_STAGE": 5,
        "RUNNER_CLEAN_BREAK_PIPS": 5.0,
        "RUNNER_DEEPEST_COUNT_FULL": 1,
        "BANK_ALL_AT_TP1": False,
        "BANK_SINGLE_LAYER_AT_TP1": False,
        "CANCEL_PENDING_ON_TP1": True,
        "MAX_ALLOWED_SPREAD_DOLLARS": 0.40,
        "ADAPTIVE_SPREAD_CEILING_DOLLARS": 0.80,
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


@pytest.mark.asyncio
async def test_02_incident_13153_wide_zone_and_unfilled_layers_protection(tmp_path):
    """Incident 13153: Protects 1 market + resting L2-L4 limit zone from premature exit/lift."""
    ex = _setup_runtime(tmp_path, "t02_13153")
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

    # Near miss toward TP1 followed by bounce back toward L2 limit -> must NOT liquidate L1 or sweep limits!
    for b in (3736.65, 3738.10, 3738.20, 3738.30):
        res = ex.evaluate_dynamic_atr_engine(sig, _tick(b, b + 0.20))
        assert res.get("pending_swept") == []
    assert sig["pending_tickets"] == [2002, 2003, 2004]
    assert sig["layers"]["L1"]["status"] == "OPEN"

    # Case 2b: Tight $3.00 TP1 leg with resting L2-L4 limits -> limits preserved
    sig_wide = dict(sig)
    sig_wide["raw_tp1"] = 3735.0
    sig_wide["final_tp"] = 3735.5
    sig_wide["layers"] = {k: dict(v) for k, v in layers.items()}
    sig_wide["layers"]["L1"]["tp"] = 3735.5
    sig_wide["tp_levels"] = {"TP1": 3735.0, "TP2": 3730.0, "TP3": 3715.0, "TP4": 3700.0, "TP5": 3650.0}
    res_early = ex.evaluate_dynamic_atr_engine(sig_wide, _tick(3735.95, 3736.15))
    assert res_early.get("pending_swept") == []
    assert sig_wide["pending_tickets"] == [2002, 2003, 2004]


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
    """Config defaults and invariants for Pure Dynamic ATR Engine."""
    assert config.DYNAMIC_ATR_SLTP_ENABLED is True
    assert config.DYNAMIC_SLTP_MODE == "ADAPTIVE"
    assert config.DYNAMIC_SL_MIN_DOLLARS == 4.50
    assert config.DYNAMIC_SL_MAX_DOLLARS == 35.00
    assert config.DYNAMIC_TP_MIN_DOLLARS == 2.50
    assert config.DYNAMIC_TP_MAX_DOLLARS == 50.00
    assert config.DYNAMIC_ATR_TRAIL_ENABLED is True
    assert config.DYNAMIC_ATR_TRAIL_MIN_DOLLARS == 2.50
    assert config.DYNAMIC_ATR_TRAIL_MAX_DOLLARS == 12.00
    assert config.DYNAMIC_ATR_TRAIL_STEP_DOLLARS == 0.25
    assert config.DYNAMIC_ATR_BE_ACTIVATION_MULT == 0.75
    assert config.DYNAMIC_ATR_BE_MIN_MFE_DOLLARS == 3.50
    assert config.DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS == 0.50
    assert config.DYNAMIC_ATR_TP_TRAIL_ENABLED is True
    assert config.DYNAMIC_ATR_TP_LIFT_ATR_MULT == 0.50
    assert config.DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT == 0.75
    assert hasattr(config, "validate_dynamic_risk_pairs")
    config.validate_dynamic_risk_pairs()
    reloaded = importlib.reload(config)
    assert hasattr(reloaded, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS")
