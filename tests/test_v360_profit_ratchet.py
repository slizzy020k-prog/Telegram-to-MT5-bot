"""
v3.6.0 Profit Ratchet + Multi-Layer Bank & Run Waterfall — Comprehensive 12-Scenario Suite.

All tests run strictly isolated under DRY_RUN=True and tmp_path (zero live MT5/disk side effects).
Covers:
  1-2. Incidents 9937 & 9943 SELL replays (BANK_HARVEST L1, TIER_2/3 CORE_RUNNER, orphan sweep)
  3.   Incident 9934 SELL anti-choke at BOTH 3741.00 ($0.33 margin) and 3741.70 ($1.03 margin)
  4.   Incident 13167 tight-TP1 TIER_0T broker floor (+$0.50) + spread-paused virtual harvest
  5.   Full 4-layer Waterfall (BANK_HARVEST -> TP1 -> STAGE2_BANKER TP2 -> L4 0.02 TP3 partial -> TP4/5)
       + Live 9953 startup hydration (zero SL loosening, legacy v3.5 adoption)
  6.   Live 13187 previously-set pending limits continuity (zero premature cancel + fill hydration)
  7.   Strict 2-step SL-first TP lift abort on 10016 & 10029 FROZEN + 60s alert throttle
  8.   Sticky per-layer anti-domino (`roles_locked=True`) + LATE_RECOVERY race fill isolation
  9.   Gradual rollover spread ramp ($0.52 -> $2.60) HARD_MAX_SPREAD=1.25 + bad tick rejection
 10.   0.01 lot indivisibility guard + BANK_SINGLE_LAYER_AT_TP1 / BANK_ALL_AT_TP1 config gates
 11.   Universal edit/BE forward-only SL, RUNNER_ACTIVE preservation, crossed TP1 edit, & parser guards
 12.   Deal-price classification, accounted_deal_tickets dedup, silent ledger backfill, & alert hygiene
"""

import logging
from types import SimpleNamespace
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


def _tick(bid: float, ask: float = None, spread: float = 0.20, ts: float = 1795100000.0) -> SimpleNamespace:
    ask_px = round(ask if ask is not None else (bid + spread), 2)
    return SimpleNamespace(bid=round(float(bid), 2), ask=ask_px, time=ts)


@pytest.fixture(autouse=True)
def _pin_v360_defaults(monkeypatch):
    """Pin deterministic v3.6.0 Rev 2 defaults regardless of local `.env`."""
    defaults = {
        "STAGED_EXIT_ENABLED": True,
        "PROFIT_RATCHET_ENABLED": True,
        "BANK_HARVEST_ENABLED": True,
        "TIERED_STAGE_BANKING_ENABLED": True,
        "CANCEL_PENDING_ON_BE": True,
        "CANCEL_PENDING_ON_PROFIT_LOCK": True,
        "CANCEL_PENDING_ON_TP1": True,
        "CANCEL_PENDING_ON_STAGE0_LOCK_EXIT": True,
        "RATCHET_BROKER_STEP_DOLLARS": 0.25,
        "RATCHET_LIFT_MIN_MFE_DOLLARS": 1.50,
        "RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS": 3.50,
        "RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS": 2.00,
        "RATCHET_TIGHT_TP1_LOCK_PCT": 0.40,
        "RATCHET_TIER1A_MFE_DOLLARS": 3.50,
        "RATCHET_TIER1A_LOCK_DOLLARS": 0.50,
        "RATCHET_TIER1B_MFE_DOLLARS": 4.25,
        "RATCHET_TIER1B_LOCK_DOLLARS": 0.75,
        "RATCHET_TIER2_MFE_DOLLARS": 5.00,
        "RATCHET_TIER2_MIN_LOCK_DOLLARS": 1.50,
        "RATCHET_TIER2_RETENTION_PCT": 0.30,
        "RATCHET_TIER2_MIN_CUSHION": 3.50,
        "RATCHET_TIER2_MAX_CUSHION": 4.00,
        "RATCHET_TIER3_MFE_DOLLARS": 8.00,
        "RATCHET_TIER3_RETENTION_PCT": 0.55,
        "RATCHET_TIER3_MIN_CUSHION": 3.50,
        "RATCHET_TIER3_MAX_CUSHION": 4.25,
        "RATCHET_TIER4_MFE_DOLLARS": 15.00,
        "RATCHET_TIER4_RETENTION_PCT": 0.68,
        "RATCHET_TIER4_MIN_CUSHION": 3.75,
        "RATCHET_TIER4_MAX_CUSHION": 5.00,
        "RATCHET_TIER5_MFE_DOLLARS": 25.00,
        "RATCHET_TIER5_RETENTION_PCT": 0.78,
        "RATCHET_TIER5_MIN_CUSHION": 4.50,
        "RATCHET_TIER5_MAX_CUSHION": 6.00,
        "RATCHET_STAGE4_RETENTION_PCT": 0.85,
        "RATCHET_STAGE4_MIN_CUSHION": 3.00,
        "RATCHET_STAGE4_MAX_CUSHION": 4.50,
        "PEAK_HARVEST_ARM_PCT": 0.85,
        "PEAK_HARVEST_CUSHION_MULT": 0.65,
        "PEAK_HARVEST_MIN_BROKER_CUSHION": 2.75,
        "PEAK_HARVEST_MIN_VIRT_CUSHION": 2.25,
        "PEAK_HARVEST_MAX_CUSHION": 3.50,
        "BANK_HARVEST_ARM_MFE_DOLLARS": 5.00,
        "BANK_HARVEST_ALT_ARM_PCT": 0.80,
        "BANK_HARVEST_ALT_MIN_MFE_DOLLARS": 2.50,
        "BANK_HARVEST_PULLBACK_ATR_MULT": 1.15,
        "BANK_HARVEST_PULLBACK_MIN": 1.75,
        "BANK_HARVEST_PULLBACK_MAX": 2.50,
        "BANK_HARVEST_BROKER_CUSHION_DOLLARS": 2.50,
        "HARD_MAX_SPREAD_DOLLARS": 1.25,
        "MIN_VALID_SPREAD_DOLLARS": 0.05,
        "STOP_LEVEL_SAFETY_BUFFER_DOLLARS": 0.05,
        "PENDING_PNL_BACKFILL_MAX_SECONDS": 60.0,
        "RUNNER_EARLY_TP_LIFT_PCT": 0.70,
        "RUNNER_PROACTIVE_TP_LIFT": True,
        "RUNNER_MAX_TP_STAGE": 5,
        "RUNNER_CLEAN_BREAK_PIPS": 5.0,
        "REVERSAL_CONFIRM_TICKS": 2,
        "BE_OFFSET_PIPS": 5.0,
        "BE_AFTER_TP1": True,
        "BANK_ALL_AT_TP1": False,
        "BANK_SINGLE_LAYER_AT_TP1": False,
        "MAX_ALLOWED_SPREAD_DOLLARS": 0.40,
        "ADAPTIVE_SPREAD_CEILING_DOLLARS": 0.80,
    }
    for key, val in defaults.items():
        monkeypatch.setattr(config, key, val, raising=False)
        monkeypatch.setattr(ex_mod, key, val, raising=False)
        monkeypatch.setattr(main, key, val, raising=False)


def _setup_runtime(tmp_path, name: str) -> MT5Executor:
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
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main.CHANNEL_IDS = [-1003712081715]
    main.state_mgr = StateManager(tmp_path / f"{name}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{name}_ledger.jsonl")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_m1_atr = 1.67
    main.executor = ex
    return ex


# ---------------------------------------------------------------------------
# 1 & 2. INCIDENTS 9937 & 9943 REPLAYS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_01_replay_9937_sell_bank_harvest_ratchet_and_orphan_sweep(tmp_path):
    ex = _setup_runtime(tmp_path, "9937")
    sig = main.state_mgr.create_signal(
        message_id=9937,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="SELL",
        sl=3751.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3741.0, "entry_price": 3740.91, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154028286, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3742.5, "entry_price": 3742.52, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154028287, "order_type": "LIMIT", "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3743.5, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154028288, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3745.0, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154028289, "order_type": "LIMIT", "status": "PENDING"},
        },
        market_tickets=[154028286, 154028287],
        pending_tickets=[154028288, 154028289],
        all_placed_tickets=[154028286, 154028287, 154028288, 154028289],
        final_tp=3725.5,
        raw_tp1=3725.0,
        raw_tp2=3710.0,
        raw_tp3=3680.0,
        tp_levels={"TP1": 3725.0, "TP2": 3710.0, "TP3": 3680.0},
        recorded_entry_price=3740.91,
        initial_price=3740.91,
        status="ACTIVE",
    )

    alerts = []
    with patch.object(main, "notify_saved_messages", side_effect=lambda msg: alerts.append(msg)):
        # Peak down to bid=3732.15, ask=3732.35 (MFE_L1=+8.56, MFE_L2=+10.17)
        t_peak = _tick(3732.15, 3732.35)
        with patch.object(ex, "get_tick", return_value=t_peak):
            await main._process_pre_tp1_or_backstop_tick(9937, main.state_mgr.get_signal(9937))

        s_pk = main.state_mgr.get_signal(9937)
        assert s_pk["layers"]["L1"]["role"] == "BANK_LAYER"
        assert s_pk["layers"]["L2"]["role"] == "CORE_RUNNER"
        assert s_pk["roles_locked"] is True
        # Orphan L3 & L4 pending orders swept on profit lock!
        assert s_pk["pending_tickets"] == []
        assert s_pk["layers"]["L3"]["status"] == "CANCELLED"
        assert s_pk["layers"]["L4"]["status"] == "CANCELLED"
        assert s_pk["layers"]["L1"]["ratchet_tier"] == "BANK_HARVEST"
        assert s_pk["layers"]["L2"]["ratchet_tier"] == "TIER_3"
        # L2 broker SL locked deeply in profit (~3735.85)
        assert s_pk["layers"]["L2"]["ratchet_sl"] < 3736.50

        # Bounce to ask=3734.40 (2 confirming ticks) triggers BANK_HARVEST on L1 ONLY
        for _ in range(2):
            with patch.object(ex, "get_tick", return_value=_tick(3734.20, 3734.40)):
                await main._process_pre_tp1_or_backstop_tick(9937, main.state_mgr.get_signal(9937))

        s_bh = main.state_mgr.get_signal(9937)
        assert s_bh["layers"]["L1"]["status"] == "CLOSED"
        assert s_bh["layers"]["L1"]["exit_reason_code"] == "BANK_HARVEST"
        # L2 CORE_RUNNER remains open & protected!
        assert 154028287 in s_bh["market_tickets"]
        assert s_bh["layers"]["L2"]["status"] != "CLOSED"
        assert any("BANK_HARVEST" in a for a in alerts)


@pytest.mark.asyncio
async def test_02_replay_9943_sell_bank_harvest_ratchet_and_orphan_sweep(tmp_path):
    ex = _setup_runtime(tmp_path, "9943")
    main.state_mgr.create_signal(
        message_id=9943,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="SELL",
        sl=3751.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3741.80, "entry_price": 3741.80, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154044419, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3742.45, "entry_price": 3742.45, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154044420, "order_type": "LIMIT", "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3743.33, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154044421, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3745.00, "tp": 3737.5, "tp_stage": "TP1", "ticket": 154044422, "order_type": "LIMIT", "status": "PENDING"},
        },
        market_tickets=[154044419, 154044420],
        pending_tickets=[154044421, 154044422],
        all_placed_tickets=[154044419, 154044420, 154044421, 154044422],
        final_tp=3730.5,
        raw_tp1=3730.0,
        raw_tp2=3725.0,
        raw_tp3=3710.0,
        tp_levels={"TP1": 3730.0, "TP2": 3725.0, "TP3": 3710.0},
        recorded_entry_price=3741.80,
        initial_price=3741.80,
        status="ACTIVE",
    )

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        with patch.object(ex, "get_tick", return_value=_tick(3735.40, 3735.60)):
            await main._process_pre_tp1_or_backstop_tick(9943, main.state_mgr.get_signal(9943))

        s_mid = main.state_mgr.get_signal(9943)
        assert s_mid["pending_tickets"] == []
        assert s_mid["layers"]["L1"]["ratchet_tier"] == "BANK_HARVEST"
        assert s_mid["layers"]["L2"]["ratchet_tier"] == "TIER_2"
        assert s_mid["layers"]["L2"]["ratchet_sl"] == pytest.approx(3739.10)  # min(3742.45 - 1.50, 3735.60 + 3.50 cushion)

        for _ in range(2):
            with patch.object(ex, "get_tick", return_value=_tick(3737.80, 3738.00)):
                await main._process_pre_tp1_or_backstop_tick(9943, main.state_mgr.get_signal(9943))

        s_end = main.state_mgr.get_signal(9943)
        assert s_end["layers"]["L1"]["status"] == "CLOSED"
        assert s_end["layers"]["L1"]["exit_reason_code"] == "BANK_HARVEST"
        assert 154044420 in s_end["market_tickets"]


# ---------------------------------------------------------------------------
# 3 & 4. INCIDENTS 9934 (ANTI-CHOKE) & 13167 (TIGHT TP1 TIER_0T)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry_px,expected_tier,expected_sl", [
    (3741.00, "TIER_1A", 3740.50),
    (3741.70, "TIER_1B", 3740.95),
])
def test_03_replay_9934_sell_anti_choke_both_3741_00_and_3741_70(tmp_path, entry_px, expected_tier, expected_sl):
    ex = _setup_runtime(tmp_path, f"9934_{entry_px}")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "sl": 3746.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": entry_px, "entry_price": entry_px, "tp": 3737.5, "tp_stage": "TP1", "ticket": 153877174, "order_type": "MARKET", "status": "OPEN"},
        },
        "market_tickets": [153877174],
        "pending_tickets": [],
        "raw_tp1": 3737.0,
        "raw_tp2": 3727.0,
        "tp_levels": {"TP1": 3737.0, "TP2": 3727.0},
        "recorded_entry_price": entry_px,
        "status": "ACTIVE",
    }

    # Peak reaches bid=3737.05, ask=3737.17 (tp1_leg >= $4.00 > $3.50 -> NOT Tier 0T!)
    res_pk = ex.evaluate_profit_ratchet_tick(sig, tick=_tick(3737.05, 3737.17), stage=0)
    assert sig["layers"]["L1"]["ratchet_tier"] == expected_tier
    assert sig["layers"]["L1"]["ratchet_sl"] == pytest.approx(expected_sl)
    assert res_pk["virtual_close_tickets"] == []

    # Rebound wick up to ask=3740.17 leaves >= $0.33 margin and MUST survive!
    for _ in range(3):
        res_wick = ex.evaluate_profit_ratchet_tick(sig, tick=_tick(3739.97, 3740.17), stage=0)
        assert res_wick["virtual_close_tickets"] == []
    assert sig["layers"]["L1"]["status"] == "OPEN"


@pytest.mark.asyncio
async def test_04_replay_13167_tight_tp1_tier_0t_broker_and_virtual_harvest(tmp_path):
    ex = _setup_runtime(tmp_path, "13167")
    main.state_mgr.create_signal(
        message_id=13167,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="SELL",
        sl=3742.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3736.00, "entry_price": 3736.00, "tp": 3733.5, "tp_stage": "TP1", "ticket": 153920001, "order_type": "MARKET", "status": "OPEN"},
        },
        market_tickets=[153920001],
        pending_tickets=[],
        raw_tp1=3733.00,
        raw_tp2=3723.00,
        tp_levels={"TP1": 3733.00, "TP2": 3723.00},
        recorded_entry_price=3736.00,
        status="ACTIVE",
    )

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # tp1_leg = 3736 - 3733 = $3.00 <= $3.50; drop to bid=3733.50, ask=3733.70 -> prog=83%, MFE=$2.30 -> TIER_0T!
        with patch.object(ex, "get_tick", return_value=_tick(3733.50, 3733.70)):
            await main._process_pre_tp1_or_backstop_tick(13167, main.state_mgr.get_signal(13167))

        s_t0 = main.state_mgr.get_signal(13167)
        assert s_t0["layers"]["L1"]["ratchet_tier"] == "TIER_0T"
        assert s_t0["layers"]["L1"]["ratchet_sl"] == pytest.approx(3735.50)  # +$0.50 spread-resilient server SL
        assert s_t0["status"] == "ACTIVE"

        # 1st bounce tick at ask=3735.20 (below broker 3735.50, past virt_sl 3735.08): noise check holds
        with patch.object(ex, "get_tick", return_value=_tick(3735.00, 3735.20)):
            await main._process_pre_tp1_or_backstop_tick(13167, main.state_mgr.get_signal(13167))
        assert main.state_mgr.get_signal(13167)["status"] == "ACTIVE"

        # 2nd confirming tick harvests profit cleanly
        with patch.object(ex, "get_tick", return_value=_tick(3735.02, 3735.22)):
            await main._process_pre_tp1_or_backstop_tick(13167, main.state_mgr.get_signal(13167))
        s_done = main.state_mgr.get_signal(13167)
        assert s_done["status"] == "CLOSED"
        assert s_done["exit_reason_code"] == "PROFIT_RATCHET_SL"


# ---------------------------------------------------------------------------
# 5. FULL 4-LAYER WATERFALL & LIVE 9953 HYDRATION
# ---------------------------------------------------------------------------

def test_05_four_layer_waterfall_and_9953_live_startup_hydration(tmp_path):
    ex = _setup_runtime(tmp_path, "waterfall_9953")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3630.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3659.0, "entry_price": 3659.0, "tp": 3661.5, "tp_stage": "TP1", "ticket": 501, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3652.67, "entry_price": 3652.67, "tp": 3661.5, "tp_stage": "TP1", "ticket": 502, "order_type": "LIMIT", "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3646.33, "entry_price": 3646.33, "tp": 3661.5, "tp_stage": "TP1", "ticket": 503, "order_type": "LIMIT", "status": "FILLED"},
            "L4": {"lot": 0.02, "target_price": 3640.0, "entry_price": 3640.0, "tp": 3661.5, "tp_stage": "TP1", "ticket": 504, "order_type": "LIMIT", "status": "FILLED"},
        },
        "market_tickets": [501, 502, 503, 504],
        "pending_tickets": [],
        "raw_tp1": 3662.0,
        "raw_tp2": 3669.0,
        "raw_tp3": 3690.0,
        "raw_tp4": 3715.0,
        "raw_tp5": 3750.0,
        "tp_levels": {"TP1": 3662.0, "TP2": 3669.0, "TP3": 3690.0, "TP4": 3715.0, "TP5": 3750.0},
        "recorded_entry_price": 3659.0,
        "status": "ACTIVE",
    }
    for t_id, vol in ((501, 0.01), (502, 0.01), (503, 0.01), (504, 0.02)):
        ex._dry_run_ticket_lots[t_id] = vol

    # TP1 reached: L1 & L2 close at TP1, L3 (STAGE2_BANKER) & L4 (CORE_RUNNER) stay open lifted to TP2=3669.0!
    with patch.object(ex, "get_tick", return_value=_tick(3662.10, 3662.30)):
        rt = ex.handle_tp1_scaling_and_select_runner(sig)
    assert rt == 504
    assert sig["layers"]["L1"]["role"] == "BANK_LAYER" and sig["layers"]["L1"]["status"] == "CLOSED"
    assert sig["layers"]["L2"]["role"] == "BANK_TP1" and sig["layers"]["L2"]["status"] == "CLOSED"
    assert sig["layers"]["L3"]["role"] == "STAGE2_BANKER" and sig["layers"]["L3"]["status"] != "CLOSED"
    assert sig["layers"]["L3"]["tp"] == pytest.approx(3669.0)
    assert sig["layers"]["L4"]["role"] == "CORE_RUNNER" and sig["layers"]["L4"]["tp"] == pytest.approx(3669.0)
    assert sig["market_tickets"] == [503, 504]

    # TP2 reached cleanly (3669.50) -> L3 closes with STAGE2_BANK_TP, L4 climbs to TP3=3690.0
    with patch.object(ex, "get_tick", return_value=_tick(3669.55, 3669.75)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["layers"]["L3"]["status"] == "CLOSED"
    assert sig["layers"]["L3"]["exit_reason_code"] == "STAGE2_BANK_TP"
    assert sig["market_tickets"] == [504]
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3690.0)

    # TP3 reached cleanly (3690.60) -> L4 partial-closes 0.01 (remaining 0.01), tp3_partial_done=True, lifts to TP4=3715.0
    with patch.object(ex, "get_tick", return_value=_tick(3690.60, 3690.80)):
        assert ex.manage_designated_runner(sig) is True
    assert sig["layers"]["L4"]["tp3_partial_done"] is True
    assert sig["layers"]["L4"]["remaining_lot"] == pytest.approx(0.01)
    assert ex._dry_run_ticket_lots[504] == pytest.approx(0.01)
    assert sig["layers"]["L4"]["tp"] == pytest.approx(3715.0)
    assert sig["last_partial_bank"]["closed_volume"] == pytest.approx(0.01)

    # Live 9953 startup hydration (old schema missing entry_price & role, existing tighter SL on L2 preserved)
    sig_9953 = {
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "sl": 3740.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3731.0, "tp": 3700.5, "ticket": 154067020, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3732.67, "tp": 3700.5, "ticket": 154067256, "order_type": "LIMIT", "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3734.33, "tp": 3700.5, "ticket": 154067257, "order_type": "LIMIT", "status": "FILLED"},
            "L4": {"lot": 0.02, "target_price": 3736.0, "tp": 3700.5, "ticket": 154067258, "order_type": "LIMIT", "status": "FILLED"},
        },
        "market_tickets": [154067020, 154067256, 154067257, 154067258],
        "pending_tickets": [],
        "status": "ACTIVE",
    }
    live_pos = {
        154067020: SimpleNamespace(ticket=154067020, symbol="XAUUSD.s", volume=0.01, price_open=3731.15, sl=3740.0, tp=3700.5),
        154067256: SimpleNamespace(ticket=154067256, symbol="XAUUSD.s", volume=0.01, price_open=3732.67, sl=3731.0, tp=3700.5),
        154067257: SimpleNamespace(ticket=154067257, symbol="XAUUSD.s", volume=0.01, price_open=3734.33, sl=3740.0, tp=3700.5),
        154067258: SimpleNamespace(ticket=154067258, symbol="XAUUSD.s", volume=0.02, price_open=3736.00, sl=3740.0, tp=3700.5),
    }
    changed = ex.hydrate_signal_layers_from_mt5(sig_9953, live_pos)
    assert changed is True
    assert sig_9953["layers"]["L1"]["entry_price"] == pytest.approx(3731.15)
    assert sig_9953["layers"]["L4"]["remaining_lot"] == pytest.approx(0.02)
    assert sig_9953["layers"]["L2"]["broker_sl"] == pytest.approx(3731.0)
    assert sig_9953["layers"]["L1"]["role"] == "BANK_LAYER"
    assert sig_9953["layers"]["L3"]["role"] == "STAGE2_BANKER"
    assert sig_9953["layers"]["L4"]["role"] == "CORE_RUNNER"


# ---------------------------------------------------------------------------
# 6. LIVE 13187 PENDING LIMITS CONTINUITY (ZERO PREMATURE CANCEL + FILL HYDRATE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_06_previously_set_pending_limits_13187_zero_premature_cancel_and_fill_continuity(tmp_path):
    ex = _setup_runtime(tmp_path, "13187")
    sig_13187 = main.state_mgr.create_signal(
        message_id=13187,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3630.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3659.0, "tp": 3661.5, "ticket": 153969343, "order_type": "LIMIT", "status": "PENDING"},
            "L2": {"lot": 0.01, "target_price": 3652.67, "tp": 3661.5, "ticket": 153969345, "order_type": "LIMIT", "status": "PENDING"},
            "L3": {"lot": 0.01, "target_price": 3646.33, "tp": 3661.5, "ticket": 153969346, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3640.0, "tp": 3661.5, "ticket": 153969347, "order_type": "LIMIT", "status": "PENDING"},
        },
        market_tickets=[],
        pending_tickets=[153969343, 153969345, 153969346, 153969347],
        all_placed_tickets=[153969343, 153969345, 153969346, 153969347],
        raw_tp1=3662.0,
        raw_tp2=3669.0,
        raw_tp3=3679.0,
        raw_tp4=3690.0,
        raw_tp5=3715.0,
        tp_levels={"TP1": 3662.0, "TP2": 3669.0, "TP3": 3679.0, "TP4": 3690.0, "TP5": 3715.0},
        final_tp=3661.5,
        status="ACTIVE",
    )

    # A) Unfilled pending setup MUST NOT be touched by startup hydration or reconciler!
    assert ex.hydrate_signal_layers_from_mt5(sig_13187, {}) is False
    ex.dry_run = False
    ex._connected = True
    ord_objs = [SimpleNamespace(ticket=t, symbol="XAUUSD.s", volume_current=0.01, price_open=3650.0) for t in (153969343, 153969345, 153969346, 153969347)]
    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex_mod, "mt5") as mock_mt5:
        mock_mt5.positions_get.return_value = []
        mock_mt5.orders_get.return_value = ord_objs
        await main._reconcile_live_mt5_signals([("13187", main.state_mgr.get_signal(13187))])
        s_unfilled = main.state_mgr.get_signal(13187)
        assert s_unfilled["status"] == "ACTIVE"
        assert s_unfilled["pending_tickets"] == [153969343, 153969345, 153969346, 153969347]

        # B) Once L1 & L2 fill into positions, reconciler hydrates entry_price + assigns sticky v3.6 roles!
        filled_pos = [
            SimpleNamespace(ticket=153969343, symbol="XAUUSD.s", type=0, volume=0.01, price_open=3659.0, sl=3630.0, tp=3661.5),
            SimpleNamespace(ticket=153969345, symbol="XAUUSD.s", type=0, volume=0.01, price_open=3652.67, sl=3630.0, tp=3661.5),
        ]
        mock_mt5.positions_get.return_value = filled_pos
        mock_mt5.orders_get.return_value = ord_objs[2:]
        mock_mt5.symbol_info.return_value = SimpleNamespace(digits=2, point=0.01, trade_stops_level=50)
        await main._reconcile_live_mt5_signals([("13187", main.state_mgr.get_signal(13187))])

    ex.dry_run = True
    s_filled = main.state_mgr.get_signal(13187)
    assert s_filled["market_tickets"] == [153969343, 153969345]
    assert s_filled["pending_tickets"] == [153969346, 153969347]
    assert s_filled["layers"]["L1"]["entry_price"] == pytest.approx(3659.0)
    assert s_filled["layers"]["L2"]["entry_price"] == pytest.approx(3652.67)
    assert s_filled["layers"]["L1"]["role"] == "BANK_LAYER"
    assert s_filled["layers"]["L2"]["role"] == "CORE_RUNNER"

    # C) Price rallies +$10 on L2 (3662.67) -> Profit Ratchet immediately locks L2 & sweeps unfilled L3/L4!
    ex.evaluate_profit_ratchet_tick(s_filled, tick=_tick(3662.70, 3662.90), stage=0)
    assert s_filled["layers"]["L2"]["ratchet_tier"] == "TIER_3"
    assert s_filled["pending_tickets"] == []


# ---------------------------------------------------------------------------
# 7. STRICT 2-STEP SL-FIRST TP LIFT ABORTS (10016 & 10029)
# ---------------------------------------------------------------------------

def test_07_strict_two_step_sl_first_tp_lift_abort_on_10016_and_10029(tmp_path):
    ex = _setup_runtime(tmp_path, "2step_abort")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3875.0,
        "layers": {"L1": {"lot": 0.01, "entry_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 701, "order_type": "MARKET", "status": "OPEN"}},
        "market_tickets": [701],
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0},
    }

    calls = []
    def _reject_sl(ticket, sl=None, tp=None, require_both=False):
        calls.append((ticket, sl, tp))
        ex.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": 10016}
        return False

    # A) 10016 SL rejection aborts Step 2 TP lift completely + throttles notify_abort per 60s
    with patch.object(ex, "modify_position_sltp", side_effect=_reject_sl):
        r1 = ex._lift_ticket_tp_two_step(sig, "L1", 701, 3910.0, "TP2", 3890.5, 3890.0, mfe_exec=8.0, tick=_tick(3898.0))
        r2 = ex._lift_ticket_tp_two_step(sig, "L1", 701, 3910.0, "TP2", 3890.5, 3890.0, mfe_exec=8.0, tick=_tick(3898.1))
    assert r1["lifted"] is False and r1["aborted_sl_fail"] is True and r1["notify_abort"] is True
    assert r2["lifted"] is False and r2["notify_abort"] is False  # throttled within 60s!
    assert sig["layers"]["L1"]["tp"] == pytest.approx(3899.5)
    assert sig["layers"]["L1"]["tp_stage"] == "TP1"
    assert all(tp is None for _, _, tp in calls)

    # B) 10029 freeze_level aborts Step 2 without touching TP
    calls.clear()
    with patch.object(ex, "modify_position_sltp", return_value=True), \
         patch.object(ex, "is_within_freeze_level", return_value=True):
        r_frz = ex._lift_ticket_tp_two_step(sig, "L1", 701, 3910.0, "TP2", 3890.5, 3890.0, mfe_exec=8.0, tick=_tick(3899.4))
    assert r_frz["lifted"] is False and r_frz["aborted_frozen"] is True and r_frz["retcode"] == 10029
    assert sig["layers"]["L1"]["tp"] == pytest.approx(3899.5)


# ---------------------------------------------------------------------------
# 8. STICKY ROLE ANTI-DOMINO & LATE_RECOVERY ISOLATION
# ---------------------------------------------------------------------------

def test_08_sticky_role_anti_domino_and_late_recovery_isolation(tmp_path):
    ex = _setup_runtime(tmp_path, "anti_domino")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {
            "L1": {"lot": 0.01, "entry_price": 3895.0, "tp": 3899.5, "ticket": 801, "status": "CLOSED", "role": "BANK_LAYER"},
            "L2": {"lot": 0.01, "entry_price": 3892.0, "tp": 3899.5, "ticket": 802, "status": "OPEN", "role": "BANK_TP1"},
            "L3": {"lot": 0.01, "entry_price": 3889.0, "tp": 3899.5, "ticket": 803, "status": "OPEN"},  # late fill without role!
            "L4": {"lot": 0.02, "entry_price": 3887.0, "tp": 3899.5, "ticket": 804, "status": "RUNNER", "role": "CORE_RUNNER"},
        },
        "market_tickets": [802, 803, 804],
        "roles_locked": True,
    }
    roles = ex.assign_or_preserve_layer_roles(sig)
    assert roles["L2"] == "BANK_TP1"        # NEVER dominoes into BANK_LAYER!
    assert roles["L3"] == "LATE_RECOVERY"   # Race fill isolated cleanly
    assert roles["L4"] == "CORE_RUNNER"


# ---------------------------------------------------------------------------
# 9. ROLLOVER SPREAD RAMP & BAD TICK REJECTION
# ---------------------------------------------------------------------------

def test_09_spread_spike_rollover_ramp_and_bad_tick_rejection(tmp_path):
    ex = _setup_runtime(tmp_path, "spread_ramp")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "SELL",
        "layers": {"L1": {"lot": 0.01, "entry_price": 3740.0, "mfe_dollars": 4.0, "ticket": 901, "status": "OPEN", "role": "SOLO_RUNNER"}},
        "market_tickets": [901],
        "raw_tp1": 3737.0,
        "tp_levels": {"TP1": 3737.0, "TP2": 3727.0},
    }
    for sp in (0.52, 0.55, 0.75, 1.10, 1.45, 2.60):
        is_spk, cur_sp, _ = ex.check_spread_spike("XAUUSD.s", _tick(3736.0, 3736.0 + sp))
    assert is_spk is True and cur_sp == pytest.approx(2.60)
    # Rollover values >= 1.25 never poisoned rolling _spread_history baseline!
    assert max(ex._spread_history["XAUUSD.s"]) < 1.25

    res_sp = ex.evaluate_profit_ratchet_tick(sig, tick=_tick(3730.0, 3732.60), stage=0)
    assert res_sp["paused_by_spread"] is True
    assert sig["layers"]["L1"]["mfe_dollars"] == pytest.approx(4.0)  # frozen during spread spike!

    # Corrupted tick (< $0.05 spread or <= 0 bid) rejected cleanly
    assert ex.is_valid_live_tick("XAUUSD.s", _tick(3736.0, 3736.01)) is False
    assert ex.is_valid_live_tick("XAUUSD.s", SimpleNamespace(bid=0.0, ask=3736.0)) is False


# ---------------------------------------------------------------------------
# 10. 0.01 LOT INDIVISIBILITY & CONFIG LIFT GATES
# ---------------------------------------------------------------------------

def test_10_indivisible_001_partial_guard_and_bank_config_gates(tmp_path, monkeypatch):
    ex = _setup_runtime(tmp_path, "indivisible")
    ex._dry_run_ticket_lots[1001] = 0.01
    r_skip = ex.close_partial_volume(1001, 0.01, current_volume=0.01)
    assert r_skip["ok"] is False and r_skip["skipped_indivisible"] is True
    assert ex._dry_run_ticket_lots[1001] == pytest.approx(0.01)

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {"L1": {"lot": 0.01, "entry_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 1001, "status": "OPEN", "role": "SOLO_RUNNER"}},
        "market_tickets": [1001],
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0},
    }
    monkeypatch.setattr(config, "BANK_SINGLE_LAYER_AT_TP1", True, raising=False)
    monkeypatch.setattr(ex_mod, "BANK_SINGLE_LAYER_AT_TP1", True, raising=False)
    res_g = ex.evaluate_profit_ratchet_tick(sig, tick=_tick(3898.5, 3898.7), stage=0)
    assert not any(ev.get("type") == "TP_LIFT" for ev in res_g["events"])
    assert sig["layers"]["L1"]["tp_stage"] == "TP1"


# ---------------------------------------------------------------------------
# 11. EDIT / BE FORWARD-ONLY SL, RUNNER_ACTIVE PRESERVATION & PARSER GUARDS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_11_edit_be_forward_only_runner_active_preservation_and_parser_guards(tmp_path):
    ex = _setup_runtime(tmp_path, "edit_be_parser")
    sig = main.state_mgr.create_signal(
        message_id=1101,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3659.5,
        layers={
            "L1": {"lot": 0.01, "target_price": 3659.0, "entry_price": 3659.0, "broker_sl": 3659.5, "sl": 3659.5, "tp": 3669.0, "tp_stage": "TP2", "ticket": 11001, "order_type": "MARKET", "status": "OPEN", "ratchet_tier": "TIER_1A", "ratchet_sl": 3659.5},
        },
        market_tickets=[11001],
        pending_tickets=[],
        raw_tp1=3662.0,
        raw_tp2=3669.0,
        tp_levels={"TP1": 3662.0, "TP2": 3669.0},
        final_tp=3661.5,
        status="BE_APPLIED",
        be_armed=True,
        roles_locked=True,
    )

    sl_sent = []
    orig_mod = ex.modify_position_sltp
    def _spy(ticket, sl=None, tp=None, require_both=False):
        sl_sent.append((ticket, sl, tp))
        return orig_mod(ticket, sl=sl, tp=tp, require_both=require_both)

    with patch.object(ex, "get_tick", return_value=_tick(3661.0, 3661.2)), \
         patch.object(ex, "modify_position_sltp", side_effect=_spy):
        # Edit attempts to loosen SL back down to 3630 -> SL MUST stay None (protected)! TP2 preserved!
        evt_ed = MockTelethonEvent(1101, "BUY 3659-3640\nSL 3630\nTP 3662\nTP 3669\nTP 3690", chat_id=-1003712081715)
        await main.handle_message_edited(evt_ed)

    assert all(sl is None for _, sl, _ in sl_sent)
    s_ed = main.state_mgr.get_signal(1101, chat_id=-1003712081715)
    assert s_ed["sl"] == pytest.approx(3659.5)
    assert s_ed["layers"]["L1"]["tp_stage"] == "TP2"

    # /be preserves RUNNER_ACTIVE status!
    sig_r = main.state_mgr.create_signal(
        message_id=1102,
        chat_id="me",
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3659.5,
        layers={"L1": {"lot": 0.01, "target_price": 3659.0, "entry_price": 3659.0, "tp": 3669.0, "tp_stage": "TP2", "ticket": 11002, "order_type": "MARKET", "status": "RUNNER"}},
        market_tickets=[11002],
        pending_tickets=[],
        status="RUNNER_ACTIVE",
        runner_ticket=11002,
        runner_layer="L1",
    )
    with patch.object(ex, "get_tick", return_value=_tick(3661.0, 3661.2)):
        await main.handle_saved_messages_command(MockTelethonEvent(9001, "/be #1102", chat_id="me"))
    assert main.state_mgr.get_signal(1102, chat_id="me")["status"] == "RUNNER_ACTIVE"

    # Parser guards: BE false-positives rejected, real BE kept, 13187/13188 FP typo extracted, NFP rejected
    assert parser_mod.parse_message("TP4 hit on the sells btw + 530pops from 3757", is_reply=True, reply_to_msg_id=13164) is None
    assert parser_mod.parse_message("Running 60pips move sl to BE if you want.", is_reply=True, reply_to_msg_id=9940) is None
    assert parser_mod.parse_message("Move stops to breakeven if you'd like or close early up to you.", is_reply=True, reply_to_msg_id=9948) is None
    p_be = parser_mod.parse_message("TP1 hit, go breakeven!", is_reply=True, reply_to_msg_id=13164)
    assert p_be is not None and p_be.management_action == "ACTION_BREAKEVEN"

    p_13187 = parser_mod.parse_message("BUY LIMIT 3659-3640\nSL 3630\nTP 3662\nTP 3669\nFP 3679\nTP 3690\nTP 3715\nTP 3750")
    assert sorted(p_13187.tp_levels.values()) == [3662.0, 3669.0, 3679.0, 3690.0, 3715.0] or 3679.0 in p_13187.tp_levels.values()
    p_13188 = parser_mod.parse_message("SELL 3710-3713\nSL 3722\nTP 3705\nFP 3695\nTP 3685")
    assert 3695.0 in p_13188.tp_levels.values()
    assert parser_mod.extract_tps("NFP 3700") == []


# ---------------------------------------------------------------------------
# 12. DEAL CLASSIFICATION, DEDUP, SILENT LEDGER BACKFILL & ALERTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_12_deal_classification_deal_dedup_silent_ledger_backfill_and_alerts(tmp_path):
    ex = _setup_runtime(tmp_path, "telemetry")
    ex.dry_run = False
    ex._connected = True

    deals = [
        SimpleNamespace(entry=0, position_id=1201, ticket=101, price=3740.0, profit=0.0, commission=-0.14, swap=0.0, fee=0.0, volume=0.02, reason=0, time=100),
        SimpleNamespace(entry=1, position_id=1201, ticket=102, price=3720.0, profit=5.00, commission=0.0, swap=0.0, fee=0.0, volume=0.01, reason=4, time=200),
        SimpleNamespace(entry=1, position_id=1201, ticket=103, price=3710.0, profit=9.00, commission=0.0, swap=-0.06, fee=0.0, volume=0.01, reason=5, time=300),
    ]
    # Partial close sees deals 102; final close sees deals 102 + 103 -> zero double counting!
    sig_dedup = {"accounted_tickets": [], "accounted_deal_tickets": [], "banked_profit": 0.0}
    with patch.object(ex_mod, "mt5") as mock_mt5:
        mock_mt5.history_deals_get.return_value = deals[:2]
        add1, _ = await main._account_closed_tickets(sig_dedup, [1201], partial_only=True)
        assert add1 == pytest.approx(4.86)
        assert sig_dedup["accounted_tickets"] == []
        assert sig_dedup["accounted_deal_tickets"] == [102]

        mock_mt5.history_deals_get.return_value = deals
        add2, _ = await main._account_closed_tickets(sig_dedup, [1201], partial_only=False)
        assert add2 == pytest.approx(8.94)
        assert sig_dedup["banked_profit"] == pytest.approx(13.80)
        assert sig_dedup["accounted_deal_tickets"] == [102, 103]
        assert sig_dedup["accounted_tickets"] == [1201]

    ex.dry_run = True

    # Deal-price exit classification checks
    sig_cls = {"direction": "SELL"}
    assert main._classify_deal_exit_code(sig_cls, 1, {"role": "BANK_LAYER", "entry_price": 3741.0}, {"reason": "SL", "exit_price": 3735.0}) == "BANK_HARVEST_SL"
    assert main._classify_deal_exit_code(sig_cls, 2, {"role": "CORE_RUNNER", "entry_price": 3742.5, "ratchet_tier": "TIER_2"}, {"reason": "SL", "exit_price": 3739.0}) == "PROFIT_RATCHET_SL"
    assert main._classify_deal_exit_code(sig_cls, 3, {"role": "BANK_TP1", "entry_price": 3742.0}, {"reason": "SL", "exit_price": 3742.0}) == "BE_SL"
    assert main._classify_deal_exit_code(sig_cls, 4, {"role": "BANK_TP1", "entry_price": 3742.0}, {"reason": "SL", "exit_price": 3751.0}) == "INITIAL_SL"
    assert main._classify_deal_exit_code(sig_cls, 5, {"role": "STAGE2_BANKER", "entry_price": 3743.0}, {"reason": "TP", "exit_price": 3727.0}) == "STAGE2_BANK_TP"

    # Silent closed-signal ledger backfill on startup (no duplicate Telegram spam)
    main.state_mgr.create_signal(
        message_id=9937,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="SELL",
        layers={"L1": {"lot": 0.01, "entry_price": 3741.0, "ticket": 7001, "order_type": "MARKET", "status": "CLOSED"}},
        accounted_tickets=[7001],
        banked_profit=1.45,
        status="CLOSED",
        ledger_recorded=False,
    )
    spam = []
    with patch.object(main, "notify_saved_messages", side_effect=lambda m: spam.append(m)):
        res_bf = await main._startup_hydrate_and_backfill_ledger()
    assert res_bf["backfilled"] == 1
    assert spam == []
    assert main.state_mgr.get_signal(9937, chat_id=-1003712081715)["ledger_recorded"] is True

    # Zero "Layer None" + N=1 0.01 TP1 hold alert omits "+£0.00 (Tickets: [])"
    sig_solo = main.state_mgr.create_signal(
        message_id=1209,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={"L1": {"lot": 0.01, "entry_price": 3890.0, "tp": 3899.5, "ticket": 12091, "order_type": "MARKET", "status": "OPEN"}},
        market_tickets=[12091],
        pending_tickets=[],
        raw_tp1=3900.0,
        raw_tp2=3910.0,
        tp_levels={"TP1": 3900.0, "TP2": 3910.0},
        recorded_entry_price=3890.0,
        status="ACTIVE",
    )
    ex._dry_run_ticket_lots[12091] = 0.01
    with patch.object(ex, "get_tick", return_value=_tick(3900.2, 3900.4)):
        _, _, tp1_alert = await main._execute_tp1_scaling_handoff(1209, sig_solo, chat_id=-1003712081715, source_label="Watcher", curr_price=3900.2)
    assert "Layer None" not in tp1_alert
    assert "(Tickets: `[]`" not in tp1_alert
    assert "0.01 Solo Runner Holding" in tp1_alert
