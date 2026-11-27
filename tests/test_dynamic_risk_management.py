"""Tests for Feature 4: Dynamic Risk Management (Pure Dynamic ATR Engine)."""

from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import dynamic_risk
import main
import mt5_executor
import parser as parser_mod
import startup_checks
from mt5_executor import MT5Executor
from parser import ZoneInfo
from state_manager import StateManager
from tests.test_copier_scenarios import MockTelethonEvent
from trade_ledger import TradeLedger


def _pin_dynamic_defaults(monkeypatch) -> None:
    """Restore default Pure Dynamic ATR config values across tests."""
    monkeypatch.setattr(config, "DYNAMIC_ATR_SLTP_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_MODE", "ADAPTIVE")
    monkeypatch.setattr(config, "DYNAMIC_ATR_TIMEFRAME", "M5")
    monkeypatch.setattr(config, "DYNAMIC_ATR_PERIOD", 14)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BASELINE_DOLLARS", 4.00)
    monkeypatch.setattr(config, "DYNAMIC_SL_ATR_MULT", 2.50)
    monkeypatch.setattr(config, "DYNAMIC_TP_ATR_MULT", 1.50)
    monkeypatch.setattr(config, "DYNAMIC_TP_STEP_ATR_MULT", 1.50)
    monkeypatch.setattr(config, "DYNAMIC_SL_MIN_DOLLARS", 4.50)
    monkeypatch.setattr(config, "DYNAMIC_SL_MAX_DOLLARS", 35.00)
    monkeypatch.setattr(config, "DYNAMIC_TP_MIN_DOLLARS", 2.50)
    monkeypatch.setattr(config, "DYNAMIC_TP_MAX_DOLLARS", 50.00)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_SCALE_MIN", 0.75)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_SCALE_MAX", 1.75)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_TIMEFRAME", "M5")
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.40)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", 10.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MULT", 1.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS", 18.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MULT", 0.80)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 8.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS", 2.20)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS", 5.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS", 2.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS", 4.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_ACTIVATION_MULT", 0.75)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS", 3.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 0.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP_TRAIL_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP_LIFT_ATR_MULT", 0.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT", 0.75)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT", 0.50)
    monkeypatch.setattr(config, "SINGLE_PRICE_CONSOLIDATE_ORDERS", True)
    monkeypatch.setattr(mt5_executor, "SINGLE_PRICE_CONSOLIDATE_ORDERS", True)
    monkeypatch.setattr(main, "SINGLE_PRICE_CONSOLIDATE_ORDERS", True)


def _reset_main_for_dyn_test(tmp_path, tag: str) -> MT5Executor:
    main.state_mgr = StateManager(tmp_path / f"{tag}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{tag}_ledger.jsonl")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 200.0
    main.executor = ex
    main.TRADING_HALTED = False
    return ex


def _tick(bid: float, ask: float = None, time: float = 1700000000.0) -> SimpleNamespace:
    return SimpleNamespace(bid=bid, ask=ask if ask is not None else bid + 0.20, time=time)


# ---------------------------------------------------------------------------
# 1. Adaptive ATR SL/TP Scaling (BUY, SELL & Zone Anchor Safety)
# ---------------------------------------------------------------------------

def test_adaptive_scaling_widens_high_vol_and_tightens_low_vol_preserving_monotonic_tps(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    pol = dynamic_risk.read_dynamic_risk_policy(config)

    # High volatility: ATR=$6.00 vs baseline $4.00 => 1.50x ratio
    res_high = dynamic_risk.scale_sl_tp_by_atr(
        direction="BUY",
        sl_anchor=3890.0,
        tp_anchor=3890.0,
        atr=6.00,
        policy=pol,
        sl=3880.0,
        tp_levels={"TP1": 3894.0, "TP2": 3900.0, "TP3": 3910.0, "TP4": 3920.0},
        final_tp=3893.5,
        front_run_offset=0.50,
    )
    assert res_high["applied"] is True
    assert res_high["vol_ratio"] == pytest.approx(1.50)
    assert res_high["sl"] == pytest.approx(3875.0)  # 3890 - (10 * 1.5)
    tps = res_high["tp_levels"]
    assert tps["TP1"] == pytest.approx(3896.0)  # 3890 + (4 * 1.5)
    assert tps["TP2"] == pytest.approx(3905.0)  # 3896 + (6 * 1.5)
    assert tps["TP3"] == pytest.approx(3920.0)  # 3905 + (10 * 1.5)
    assert tps["TP4"] == pytest.approx(3935.0)  # 3920 + (10 * 1.5)
    assert res_high["final_tp"] == pytest.approx(3895.5)

    # Low volatility: ATR=$2.00 => raw 0.50x clamped to scale_min=0.75x (SELL direction)
    res_low = dynamic_risk.scale_sl_tp_by_atr(
        direction="SELL",
        sl_anchor=3900.0,
        tp_anchor=3900.0,
        atr=2.00,
        policy=pol,
        sl=3912.0,
        tp_levels={"TP1": 3896.0, "TP2": 3888.0, "TP3": 3880.0},
        final_tp=3896.5,
        front_run_offset=0.50,
    )
    assert res_low["vol_ratio"] == pytest.approx(0.75)
    assert res_low["sl"] == pytest.approx(3909.0)  # 3900 + (12 * 0.75)
    s_tps = res_low["tp_levels"]
    assert s_tps["TP1"] == pytest.approx(3897.0)  # 3900 - (4 * 0.75)
    assert s_tps["TP2"] == pytest.approx(3891.0)  # 3897 - (8 * 0.75)
    assert s_tps["TP3"] == pytest.approx(3885.0)  # 3891 - (8 * 0.75)
    assert s_tps["TP1"] > s_tps["TP2"] > s_tps["TP3"]
    assert res_low["final_tp"] == pytest.approx(3897.5)


def test_zone_sl_anchor_never_moves_inside_entry_zone(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_atr = 2.00  # scale_min = 0.75x

    # BUY zone 3659-3640 | SL 3630 -> SL anchor is zone bottom (3640), so 3640 - (10 * 0.75) = 3632.50 < 3640!
    zone = ZoneInfo(top=3659.0, mid=3649.5, bottom=3640.0, direction="BUY")
    mk, pd, layers = ex.execute_4layer_zone(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=zone,
        sl=3630.0,
        final_tp=3661.5,
        raw_tp1=3662.0,
        raw_tp2=3670.0,
    )
    assert mk == [] and len(pd) == 4
    snap = ex.last_dynamic_risk_snapshot
    assert snap is not None and snap["applied"] is True
    assert snap["sl"] == pytest.approx(3632.50)
    assert snap["sl"] < 3640.0  # Strictly outside [3640, 3659] entry zone
    assert layers["L1"]["tp"] == pytest.approx(snap["final_tp"])


# ---------------------------------------------------------------------------
# 2. FALLBACK_ONLY, PURE_ATR, Disabled & ATR=0 Behavior
# ---------------------------------------------------------------------------

def test_fallback_only_preserves_explicit_and_scales_missing_sl_and_tp1(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_MODE", "FALLBACK_ONLY")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_atr = 6.00  # SL dist = 6.0 * 2.5 = $15.00

    # Explicit SL/TP untouched in FALLBACK_ONLY
    mk, _, layers = ex.execute_4layer_single_price(
        symbol="XAUUSD.s",
        direction="BUY",
        price=3895.0,
        sl=3882.0,
        final_tp=3904.5,
        raw_tp1=3905.0,
        order_intent="MARKET",
    )
    assert len(mk) == 1
    assert ex.last_dynamic_risk_snapshot["sl"] == pytest.approx(3882.0)
    assert ex.last_dynamic_risk_snapshot["raw_tp1"] == pytest.approx(3905.0)
    assert layers["L1"]["tp"] == pytest.approx(3904.5)

    # Missing SL (`sl=None`) uses ATR fallback distance ($15.00 instead of 150 pips)
    ex._dry_run_atr = 8.00  # 8.0 * 2.5 = $20.00 fallback SL distance
    fb_sl = ex._compute_fallback_sl(True, 3895.0, None, 0.10, 2, "BUY", "XAUUSD.s", "Market")
    assert fb_sl == pytest.approx(3875.0)  # 3895 - 20


@pytest.mark.asyncio
async def test_unpriced_market_stub_attaches_atr_fallback_tp1(tmp_path, monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_MODE", "FALLBACK_ONLY")
    ex = _reset_main_for_dyn_test(tmp_path, "fb_tp1")
    ex._dry_run_atr = 6.00  # ATR fallback TP1 = 6.0 * 1.5 = $9.00

    main.state_mgr.create_signal(
        message_id=77001,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={"L1": {"lot": 0.01, "ticket": 9001, "order_type": "MARKET", "status": "OPEN", "entry_price": 3894.8}},
        market_tickets=[9001],
        pending_tickets=[],
        sl=3880.0,
        raw_tp1=None,
        timestamp="2020-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(main, "notify_saved_messages", AsyncMock())
    sig = main.state_mgr.get_signal(77001)
    await main._process_pre_tp1_or_backstop_tick("77001", sig)

    updated = main.state_mgr.get_signal(77001)
    assert updated["fallback_tp_applied"] is True
    assert updated["raw_tp1"] == pytest.approx(3894.80 + 9.00)  # DummyTick bid = 3894.80


def test_pure_atr_and_disabled_modes(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_MODE", "PURE_ATR")
    pol_pure = dynamic_risk.read_dynamic_risk_policy(config)

    # PURE_ATR ignores explicit wide/tight SL & TPs, uses atr * mults clamped to bounds
    res_pure = dynamic_risk.scale_sl_tp_by_atr(
        direction="BUY",
        sl_anchor=3895.0,
        tp_anchor=3895.0,
        atr=5.00,
        policy=pol_pure,
        sl=3850.0,
        tp_levels={"TP1": 3897.0, "TP2": 3950.0},
        front_run_offset=0.50,
    )
    assert res_pure["sl"] == pytest.approx(3895.0 - 12.50)  # 5 * 2.5
    assert res_pure["tp_levels"]["TP1"] == pytest.approx(3895.0 + 7.50)  # 5 * 1.5
    assert res_pure["tp_levels"]["TP2"] == pytest.approx(3910.0)
    assert res_pure["tp_levels"]["TP5"] == pytest.approx(3932.5)
    assert res_pure["final_tp"] == pytest.approx(3902.0)

    # Disabled flag or atr == 0.0 -> no changes
    monkeypatch.setattr(config, "DYNAMIC_ATR_SLTP_ENABLED", False)
    pol_off = dynamic_risk.read_dynamic_risk_policy(config)
    res_off = dynamic_risk.scale_sl_tp_by_atr(
        direction="BUY",
        sl_anchor=3895.0,
        tp_anchor=3895.0,
        atr=6.00,
        policy=pol_off,
        sl=3885.0,
        raw_tp1=3900.0,
        final_tp=3899.5,
    )
    assert res_off["applied"] is False
    assert res_off["sl"] == 3885.0
    assert res_off["final_tp"] == 3899.5

    res_zero_atr = dynamic_risk.scale_sl_tp_by_atr(
        direction="BUY",
        sl_anchor=3895.0,
        tp_anchor=3895.0,
        atr=0.0,
        policy=pol_pure,
        sl=3885.0,
        raw_tp1=3900.0,
        final_tp=3899.5,
    )
    assert res_zero_atr["applied"] is False


# ---------------------------------------------------------------------------
# 3. Single-Price & Market Entry Snapshot Wiring + Multi-Timeframe get_atr
# ---------------------------------------------------------------------------

def test_execute_4layer_single_price_and_market_no_double_scale_and_mtf_atr(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_atr = 6.00  # 1.5x vol ratio

    mk_sp, _, l_sp = ex.execute_4layer_single_price(
        symbol="XAUUSD.s",
        direction="BUY",
        price=3895.0,
        sl=3885.0,
        final_tp=3902.5,
        raw_tp1=3903.0,
        order_intent="MARKET",
    )
    assert len(mk_sp) == 1
    assert ex.last_dynamic_risk_snapshot["sl"] == pytest.approx(3880.0)  # 3895 - 10 * 1.5
    assert l_sp["L1"]["tp"] == pytest.approx(3906.5)  # TP1 3895 + 8*1.5=3907 - 0.50

    mk_m, _, l_m = ex.execute_4layer_market(
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3885.0,
        final_tp=3902.5,
        raw_tp1=3903.0,
    )
    assert len(mk_m) == 4
    assert ex.last_dynamic_risk_snapshot["sl"] == pytest.approx(3880.0)
    assert l_m["L1"]["tp"] == pytest.approx(3906.5)

    # Dry-run M1 vs M5 isolation: _dry_run_m1_atr does not leak into M5
    ex._dry_run_atr = None
    ex._dry_run_m1_atr = 2.25
    assert ex.get_m1_atr("XAUUSD.s") == pytest.approx(2.25)
    assert ex.get_atr("XAUUSD.s", timeframe="M5") == 0.0

    # Live MT5 multi-timeframe copy_rates_from_pos call
    live_ex = MT5Executor(dry_run=False)
    live_ex._connected = True
    fake_rates = [
        {"high": 3902.0, "low": 3898.0, "close": 3900.0},
        {"high": 3905.0, "low": 3899.0, "close": 3904.0},
        {"high": 3910.0, "low": 3904.0, "close": 3908.0},
    ]
    mock_mt5 = MagicMock()
    mock_mt5.TIMEFRAME_M15 = 15
    mock_mt5.symbol_info.return_value = SimpleNamespace(point=0.01)
    mock_mt5.copy_rates_from_pos.return_value = fake_rates
    monkeypatch.setattr(mt5_executor, "mt5", mock_mt5)
    atr_m15 = live_ex.get_atr("XAUUSD.s", timeframe="M15", period=14)
    assert atr_m15 == pytest.approx(6.0)
    mock_mt5.copy_rates_from_pos.assert_called_once()


def test_quiet_session_sl_floor_clamped_to_450_adaptive_and_pure_atr(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    pol_ad = dynamic_risk.read_dynamic_risk_policy(config)

    # ADAPTIVE: $5.00 SL distance * 0.75 quiet-session scale = $3.75 -> clamped up to $4.50
    res_buy = dynamic_risk.scale_sl_tp_by_atr(
        direction="BUY",
        sl_anchor=3900.0,
        tp_anchor=3900.0,
        atr=2.00,
        policy=pol_ad,
        sl=3895.0,
        tp_levels={"TP1": 3910.0},
    )
    assert res_buy["vol_ratio"] == pytest.approx(0.75)
    assert res_buy["sl"] == pytest.approx(3895.50)

    res_sell = dynamic_risk.scale_sl_tp_by_atr(
        direction="SELL",
        sl_anchor=3900.0,
        tp_anchor=3900.0,
        atr=2.00,
        policy=pol_ad,
        sl=3905.0,
        tp_levels={"TP1": 3890.0},
    )
    assert res_sell["sl"] == pytest.approx(3904.50)

    # PURE_ATR / fallback: ATR=$1.20 * 2.50 = $3.00 -> clamped up to $4.50
    monkeypatch.setattr(config, "DYNAMIC_SLTP_MODE", "PURE_ATR")
    pol_pure = dynamic_risk.read_dynamic_risk_policy(config)
    assert dynamic_risk.compute_atr_fallback_sl_distance(1.20, pol_pure) == pytest.approx(4.50)
    res_pure = dynamic_risk.scale_sl_tp_by_atr(
        direction="BUY",
        sl_anchor=3900.0,
        tp_anchor=3900.0,
        atr=1.20,
        policy=pol_pure,
        sl=3890.0,
        tp_levels={"TP1": 3910.0},
    )
    assert res_pure["sl"] == pytest.approx(3895.50)


# ---------------------------------------------------------------------------
# 4. Decommission & Production Defaults Verification
# ---------------------------------------------------------------------------

def test_decommission_and_pure_dynamic_atr_defaults(monkeypatch, shipped_config):
    """Verify that all legacy virtual exit mechanisms have been decommissioned and pure defaults are active."""
    deleted_symbols = [
        "GOLD_REVERSAL_GUARD_ENABLED",
        "STAGE0_PROFIT_LOCK_ENABLED",
        "CANCEL_PENDING_ON_STAGE0_LOCK_EXIT",
        "PROFIT_RATCHET_ENABLED",
        "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED",
        "BANK_HARVEST_ENABLED",
        "STAGED_EXIT_ENABLED",
        "SHORTFALL_TRIGGER_DISTANCE",
        "SHORTFALL_REVERSAL_TICKS",
        "RUNNER_PROXIMITY_THRESHOLD_PIPS",
        "RUNNER_REVERSAL_CONFIRM_TICKS",
        "RUNNER_EARLY_TP_LIFT_PCT",
        "RUNNER_PROACTIVE_TP_LIFT",
        "RUNNER_CONTINUOUS_TRAIL_ENABLED",
        "RUNNER_CLEAN_BREAK_PIPS",
        "evaluate_gold_reversal_guard",
        "evaluate_profit_ratchet_tick",
    ]
    for mod in (config, mt5_executor, main):
        for sym in deleted_symbols:
            assert not hasattr(mod, sym), f"{mod.__name__} must NOT export decommissioned symbol {sym}!"

    # Invariants in config
    assert config.DYNAMIC_ATR_SLTP_ENABLED is True
    assert config.DYNAMIC_SLTP_MODE == "ADAPTIVE"
    assert config.DYNAMIC_SL_MIN_DOLLARS == pytest.approx(4.50)
    assert config.DYNAMIC_SL_MAX_DOLLARS == pytest.approx(35.00)
    assert config.DYNAMIC_TP_MIN_DOLLARS == pytest.approx(2.50)
    assert config.DYNAMIC_TP_MAX_DOLLARS == pytest.approx(50.00)
    assert config.DYNAMIC_ATR_TRAIL_ENABLED is True
    assert config.DYNAMIC_ATR_TRAIL_MIN_DOLLARS == pytest.approx(2.50)
    assert config.DYNAMIC_ATR_TRAIL_MAX_DOLLARS == pytest.approx(8.00)  # v3.9.0 Stage-A cap
    assert config.DYNAMIC_ATR_TRAIL_STEP_DOLLARS == pytest.approx(0.25)
    assert config.DYNAMIC_ATR_BE_ACTIVATION_MULT == pytest.approx(0.50)
    assert config.DYNAMIC_ATR_BE_MIN_MFE_DOLLARS == pytest.approx(0.60)
    assert config.DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS == pytest.approx(1.20)
    assert config.DYNAMIC_ATR_TP_TRAIL_ENABLED is True
    assert config.DYNAMIC_ATR_TP_LIFT_ATR_MULT == pytest.approx(0.50)
    assert config.DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT == pytest.approx(0.75)

    # Invariants in DynamicRiskPolicy
    pol = dynamic_risk.DynamicRiskPolicy()
    assert pol.sltp_enabled is True
    assert pol.sltp_mode == "ADAPTIVE"
    assert pol.sl_min_dollars == pytest.approx(4.50)
    assert pol.sl_max_dollars == pytest.approx(35.00)
    assert pol.tp_min_dollars == pytest.approx(2.50)
    assert pol.tp_max_dollars == pytest.approx(50.00)
    assert pol.trail_enabled is True
    assert pol.trail_min_dollars == pytest.approx(2.50)
    # v3.9.0: Stage A trail max default tightened 12.00 -> 8.00 (per-stage clamps).
    assert pol.trail_max_dollars == pytest.approx(8.00)
    assert pol.trail_step_dollars == pytest.approx(0.25)
    assert pol.be_activation_atr_mult == pytest.approx(0.50)
    assert pol.be_min_mfe_dollars == pytest.approx(0.60)
    assert pol.be_lock_offset_dollars == pytest.approx(1.20)
    assert pol.tp_trail_enabled is True
    assert pol.tp_lift_atr_mult == pytest.approx(0.50)
    assert pol.tp_lift_progress_pct == pytest.approx(0.75)


def test_startup_checks_dynamic_risk_validation(tmp_path):
    """Validate that startup_checks validates all pure dynamic ATR settings properly."""
    assert startup_checks.validate_environment(check_packages=False, cfg=config)["ok"] is True

    def _cfg_with(**overrides):
        base = {k: getattr(config, k) for k in dir(config) if k.isupper()}
        base.update(
            LOG_FILE_PATH=tmp_path / "copier.log",
            STATE_FILE_PATH=tmp_path / "state.json",
            TRADES_LEDGER_PATH=tmp_path / "ledger.jsonl",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    res_bad_mode = startup_checks.validate_environment(
        check_packages=False, cfg=_cfg_with(DYNAMIC_SLTP_MODE="INVALID")
    )
    assert res_bad_mode["ok"] is False
    assert any("DYNAMIC_SLTP_MODE" in e for e in res_bad_mode["errors"])

    res_bad_tf = startup_checks.validate_environment(
        check_packages=False, cfg=_cfg_with(DYNAMIC_ATR_TRAIL_TIMEFRAME="D1")
    )
    assert res_bad_tf["ok"] is False
    assert any("DYNAMIC_ATR_TRAIL_TIMEFRAME" in e for e in res_bad_tf["errors"])

    res_inverted = startup_checks.validate_environment(
        check_packages=False, cfg=_cfg_with(DYNAMIC_SL_MIN_DOLLARS=40.0, DYNAMIC_SL_MAX_DOLLARS=10.0)
    )
    assert res_inverted["ok"] is False
    assert any("DYNAMIC_SL_MIN_DOLLARS" in e for e in res_inverted["errors"])

    res_bad_period = startup_checks.validate_environment(
        check_packages=False, cfg=_cfg_with(DYNAMIC_ATR_PERIOD=1)
    )
    assert res_bad_period["ok"] is False
    assert any("DYNAMIC_ATR_PERIOD" in e for e in res_bad_period["errors"])

    for bool_key in (
        "DYNAMIC_ATR_SLTP_ENABLED",
        "DYNAMIC_ATR_TRAIL_ENABLED",
        "DYNAMIC_ATR_TP_TRAIL_ENABLED",
    ):
        res_bad_bool = startup_checks.validate_environment(
            check_packages=False, cfg=_cfg_with(**{bool_key: "yes"})
        )
        assert res_bad_bool["ok"] is False
        assert any(bool_key in e for e in res_bad_bool["errors"])


# ---------------------------------------------------------------------------
# 5. Dynamic Trailing SL, BE Lock & Pending Sweep
# ---------------------------------------------------------------------------

def test_dynamic_atr_engine_disabled_and_be_lock_and_trailing_sl(monkeypatch):
    """Test evaluate_dynamic_atr_engine: BE lock, forward-only SL trail, pending sweep, clamps."""
    _pin_dynamic_defaults(monkeypatch)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_atr = 2.00

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3885.0,
        "market_tickets": [101],
        "pending_tickets": [102],
        "layers": {
            "L1": {"ticket": 101, "status": "OPEN", "entry_price": 3895.0, "sl": 3885.0},
            "L2": {"ticket": 102, "status": "PENDING", "target_price": 3890.0, "sl": 3885.0},
        },
    }

    # 1) When trail_enabled is False -> disabled
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", False)
    res_off = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3905.0, 3905.3))
    assert res_off["enabled"] is False
    assert res_off["updated_tickets"] == []

    # 2) Re-enable trail
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)

    # 3) Low MFE ($1.50) < BE threshold (max(0.75*2, 3.50) = $3.50) and < trail activation (1.0*2.0 = $2.00)
    res_low = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3896.5, 3896.8))
    assert res_low["newly_armed"] is False
    assert res_low["newly_be_locked"] == []
    assert res_low["pending_swept"] == []

    # 4) Advance to bid=3898.8 (MFE = $3.80 >= $3.50 BE threshold; ATR trail activation = $2.00 also armed)
    # BE lock sets SL = 3895.0 + 0.50 = 3895.50.
    # ATR trail dist = clamp(2.0 * 1.40, 2.50, 8.00) = $2.80 -> candidate ATR SL = 3898.8 - 2.80 = 3896.00.
    # Max(BE lock 3895.50, ATR SL 3896.00) = 3896.00. Pending limit 102 is swept!
    res_be = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3898.8, 3899.0))
    assert any(item[0] == 101 for item in res_be["newly_be_locked"])
    assert 102 in res_be["pending_swept"]
    assert sig["layers"]["L1"]["be_locked"] is True
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3896.00)
    assert sig["pending_tickets"] == []

    # 5) Advance to peak 3902.0 with higher volatility ATR=10.00 (trail_dist clamps to $8.00)
    # Candidate SL = 3902.0 - 8.00 = 3894.00 < existing SL 3896.00 => forward-only NEVER loosens SL!
    ex._dry_run_atr = 10.00
    res_widen = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3902.0, 3902.3))
    assert res_widen["updated_tickets"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3896.00)
    assert sig["layers"]["L1"]["dynamic_atr_trail_peak"] == pytest.approx(3902.0)

    # 6) Volatility contracts ATR=1.00 -> trail_dist clamped to min $2.50
    # The breakeven lock (entry + $1.20) is now the TIGHTEST candidate, so the stop
    # ratchets to the breakeven lock rather than to the ATR trail.
    ex._dry_run_atr = 1.00
    res_tighten = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3902.0, 3902.3))
    assert len(res_tighten["updated_tickets"]) == 1
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3900.00)

    # 7) Small movement < step filter ($0.25) is ignored
    res_small = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3902.10, 3902.30))
    assert res_small["updated_tickets"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3900.00)

    # 8) Stops-level clamping via compute_dynamic_atr_trail_stop
    pol = dynamic_risk.read_dynamic_risk_policy(config)
    clamped = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY",
        entry_price=3895.0,
        live_price=3901.0,
        prev_peak=3905.0,
        current_sl=3897.0,
        atr=1.0,
        policy=pol,
        digits=2,
        stops_buffer=2.00,  # live=3901 - 2.00 = 3899.00 < raw (3905 - 2.20 = 3902.80, v3.9.0 Stage-B floor)
    )
    assert clamped["should_update"] is True
    assert clamped["candidate_sl"] == pytest.approx(3899.00)

    # 9) Spread spike pauses dynamic ATR engine cleanly
    res_spike = ex.evaluate_dynamic_atr_engine(sig, tick=SimpleNamespace(bid=3906.0, ask=3909.0, time=1700000005.0))
    assert res_spike["paused_by_spread"] is True
    assert res_spike["updated_tickets"] == []


# ---------------------------------------------------------------------------
# 6. Dynamic Trailing TP Progression (BUY & SELL)
# ---------------------------------------------------------------------------

def test_dynamic_trailing_tp_progression_buy_and_sell(monkeypatch):
    """Test momentum-gated Dynamic Trailing TP steps TP1 -> TP2 -> TP3 -> TP4 -> TP5."""
    _pin_dynamic_defaults(monkeypatch)
    pol = dynamic_risk.read_dynamic_risk_policy(config)

    # 1) BUY Signal: Entry 3900.0, TP1=3910.0, TP2=3920.0, TP3=3930.0, TP4=3940.0, TP5=3950.0. ATR=4.0
    # At live_price = 3908.0, dist_to_target = 3910 - 3908 = 2.0 <= 0.50 * 4.0 = 2.0 (momentum_ok: pullback 0.0 <= 1.0)
    res_tp1_buy = dynamic_risk.compute_dynamic_tp_step(
        direction="BUY",
        entry_price=3900.0,
        live_price=3908.0,
        peak_price=3908.0,
        atr=4.0,
        tp_levels={"TP1": 3910.0, "TP2": 3920.0, "TP3": 3930.0, "TP4": 3940.0, "TP5": 3950.0},
        current_stage_idx=1,
        max_stage=5,
        cleared_milestones=[],
        policy=pol,
    )
    assert res_tp1_buy["should_lift"] is True
    assert res_tp1_buy["target_stage_idx"] == 2
    assert res_tp1_buy["target_stage_name"] == "TP2"
    assert res_tp1_buy["target_tp"] == pytest.approx(3920.0)

    # 2) SELL Signal: Entry 3900.0, TP1=3890.0, TP2=3880.0, TP3=3870.0. ATR=4.0
    # At live_price = 3891.8, dist_to_target = 3891.8 - 3890.0 = 1.8 <= 2.0 (pullback 0.0 <= 1.0)
    res_tp1_sell = dynamic_risk.compute_dynamic_tp_step(
        direction="SELL",
        entry_price=3900.0,
        live_price=3891.8,
        peak_price=3891.8,
        atr=4.0,
        tp_levels={"TP1": 3890.0, "TP2": 3880.0, "TP3": 3870.0},
        current_stage_idx=1,
        max_stage=3,
        cleared_milestones=[],
        policy=pol,
    )
    assert res_tp1_sell["should_lift"] is True
    assert res_tp1_sell["target_stage_idx"] == 2
    assert res_tp1_sell["target_tp"] == pytest.approx(3880.0)

    # 3) Pullback > 0.25 * ATR (pullback $1.50 > 0.25 * $4.00 = $1.00) -> momentum_ok is False -> NO LIFT!
    res_pullback = dynamic_risk.compute_dynamic_tp_step(
        direction="BUY",
        entry_price=3900.0,
        live_price=3907.5,
        peak_price=3909.0,  # pullback = 3909.0 - 3907.5 = 1.50 > 1.00
        atr=4.0,
        tp_levels={"TP1": 3910.0, "TP2": 3920.0, "TP3": 3930.0},
        current_stage_idx=1,
        max_stage=3,
        cleared_milestones=[],
        policy=pol,
    )
    assert res_pullback["momentum_ok"] is False
    assert res_pullback["should_lift"] is False

    # 4) Caps at max_stage (e.g. 3-TP signal caps at TP3)
    res_max = dynamic_risk.compute_dynamic_tp_step(
        direction="BUY",
        entry_price=3900.0,
        live_price=3929.0,
        peak_price=3929.0,
        atr=4.0,
        tp_levels={"TP1": 3910.0, "TP2": 3920.0, "TP3": 3930.0},
        current_stage_idx=3,
        max_stage=3,
        cleared_milestones=["TP1", "TP2"],
        policy=pol,
    )
    assert res_max["should_lift"] is False
    assert res_max["target_stage_idx"] == 3


# ---------------------------------------------------------------------------
# 7. Zero Early Virtual Closes on Mid-Candle Pullback Wicks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_early_virtual_closes_on_pullback_wicks(tmp_path, monkeypatch):
    """Verify that pre-TP1 wicks, post-TP1 $4 pullbacks, and post-TP2 $5 wicks trigger ZERO market closes."""
    _pin_dynamic_defaults(monkeypatch)
    ex = _reset_main_for_dyn_test(tmp_path, "zero_virt")
    ex._dry_run_atr = 4.00

    main.state_mgr.create_signal(
        message_id=98001,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3885.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 98101, "order_type": "MARKET", "status": "OPEN", "role": "BANK_LAYER"},
            "L2": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 98102, "order_type": "MARKET", "status": "OPEN", "role": "BANK_TP1"},
            "L3": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 98103, "order_type": "MARKET", "status": "OPEN", "role": "STAGE2_BANKER"},
            "L4": {"lot": 0.02, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 98104, "order_type": "MARKET", "status": "OPEN", "role": "CORE_RUNNER"},
        },
        market_tickets=[98101, 98102, 98103, 98104],
        pending_tickets=[],
        raw_tp1=3910.0,
        raw_tp2=3920.0,
        raw_tp3=3930.0,
        tp_levels={"TP1": 3910.0, "TP2": 3920.0, "TP3": 3930.0},
        final_tp=3909.5,
        recorded_entry_price=3900.0,
        status="ACTIVE",
    )
    for tk, lot in ((98101, 0.01), (98102, 0.01), (98103, 0.01), (98104, 0.02)):
        ex._dry_run_ticket_lots[tk] = lot

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex, "close_market_position", wraps=ex.close_market_position) as spy_close:

        # 1) Pre-TP1 near-miss: price reaches 3908.5 (+$8.50), then pulls back to 3905.0 (-$3.50 wick)
        with patch.object(ex, "get_tick", return_value=_tick(3908.5, 3908.7)):
            await main._process_pre_tp1_or_backstop_tick(98001, main.state_mgr.get_signal(98001, chat_id=-1003712081715))
        for _ in range(3):
            with patch.object(ex, "get_tick", return_value=_tick(3905.0, 3905.2)):
                await main._process_pre_tp1_or_backstop_tick(98001, main.state_mgr.get_signal(98001, chat_id=-1003712081715))
        assert spy_close.call_count == 0
        sig_curr = main.state_mgr.get_signal(98001, chat_id=-1003712081715)
        assert sig_curr["status"] == "ACTIVE"
        assert sig_curr["market_tickets"] == [98101, 98102, 98103, 98104]

        # 2) Touch TP1 -> status becomes RUNNER_ACTIVE, BE locked at 3900.50
        with patch.object(ex, "get_tick", return_value=_tick(3910.2, 3910.4)):
            await main._process_pre_tp1_or_backstop_tick(98001, main.state_mgr.get_signal(98001, chat_id=-1003712081715))

        # 3) Post-TP1 pullback of $4.00 (from 3910.2 to 3906.2, well above BE SL 3900.50)
        for _ in range(3):
            with patch.object(ex, "get_tick", return_value=_tick(3906.2, 3906.4)):
                await main._process_runner_active_tick(98001, main.state_mgr.get_signal(98001, chat_id=-1003712081715))
        assert spy_close.call_count == 0
        sig_p1 = main.state_mgr.get_signal(98001, chat_id=-1003712081715)
        assert sig_p1["status"] == "RUNNER_ACTIVE"

        # 4) Advance to TP2 (3920.5), then post-TP2 $5 wick pullback (to 3915.5, above milestone floor TP1 3910.0)
        with patch.object(ex, "get_tick", return_value=_tick(3920.5, 3920.7)):
            await main._process_runner_active_tick(98001, main.state_mgr.get_signal(98001, chat_id=-1003712081715))
        for _ in range(3):
            with patch.object(ex, "get_tick", return_value=_tick(3915.5, 3915.7)):
                await main._process_runner_active_tick(98001, main.state_mgr.get_signal(98001, chat_id=-1003712081715))
        assert spy_close.call_count == 0
        sig_p2 = main.state_mgr.get_signal(98001, chat_id=-1003712081715)
        assert sig_p2["status"] == "RUNNER_ACTIVE"


# ---------------------------------------------------------------------------
# 8. Migrated Infrastructure Tests (Startup Hydration, Roles, Spread Ramp)
# ---------------------------------------------------------------------------

def test_migrated_startup_hydration_and_role_preservation(tmp_path):
    """(Migrated from test_05) Startup hydration preserves tighter broker SL and assigns roles cleanly."""
    ex = _setup_runtime(tmp_path, "waterfall_hydration")
    sig = {
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
    changed = ex.hydrate_signal_layers_from_mt5(sig, live_pos)
    assert changed is True
    assert sig["layers"]["L1"]["entry_price"] == pytest.approx(3731.15)
    assert sig["layers"]["L4"]["remaining_lot"] == pytest.approx(0.02)
    assert sig["layers"]["L2"]["broker_sl"] == pytest.approx(3731.0)
    assert sig["layers"]["L1"]["role"] in ("BANK_TP1", "BANK_LAYER")
    assert sig["layers"]["L3"]["role"] == "STAGE2_BANKER"
    assert sig["layers"]["L4"]["role"] == "CORE_RUNNER"


def test_migrated_sticky_role_anti_domino_and_late_recovery_isolation(tmp_path):
    """(Migrated from test_08) Sticky roles prevent domino mutation and isolate late fills."""
    ex = _setup_runtime(tmp_path, "anti_domino")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {
            "L1": {"lot": 0.01, "entry_price": 3895.0, "tp": 3899.5, "ticket": 801, "status": "CLOSED", "role": "BANK_LAYER"},
            "L2": {"lot": 0.01, "entry_price": 3892.0, "tp": 3899.5, "ticket": 802, "status": "OPEN", "role": "BANK_TP1"},
            "L3": {"lot": 0.01, "entry_price": 3889.0, "tp": 3899.5, "ticket": 803, "status": "OPEN"},
            "L4": {"lot": 0.02, "entry_price": 3887.0, "tp": 3899.5, "ticket": 804, "status": "RUNNER", "role": "CORE_RUNNER"},
        },
        "market_tickets": [802, 803, 804],
        "roles_locked": True,
    }
    roles = ex.assign_or_preserve_layer_roles(sig)
    assert roles["L2"] == "BANK_TP1"
    assert roles["L3"] == "LATE_RECOVERY"
    assert roles["L4"] == "CORE_RUNNER"


def test_migrated_spread_spike_rollover_ramp_and_bad_tick_rejection(tmp_path):
    """(Migrated from test_09) Rollover spread spikes do not poison history; bad ticks rejected."""
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
    assert max(ex._spread_history["XAUUSD.s"]) < 1.25

    res_sp = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3730.0, 3732.60))
    assert res_sp["paused_by_spread"] is True

    # Corrupted tick rejected cleanly
    assert ex.is_valid_live_tick("XAUUSD.s", _tick(3736.0, 3736.01)) is False
    assert ex.is_valid_live_tick("XAUUSD.s", SimpleNamespace(bid=0.0, ask=3736.0, time=1700000000.0)) is False


def test_migrated_stops_and_freeze_level_rejection_two_step(tmp_path):
    """(Migrated from test_07) Retcode 10016 / 10029 stops/freeze aborts modification without touching broker SL/TP."""
    ex = _setup_runtime(tmp_path, "2step_abort")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3875.0,
        "layers": {"L1": {"lot": 0.01, "entry_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 701, "order_type": "MARKET", "status": "OPEN", "role": "CORE_RUNNER"}},
        "market_tickets": [701],
        "runner_tickets": [701],
        "runner_layers": ["L1"],
        "runner_ticket": 701,
        "runner_layer": "L1",
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0},
    }

    # A) 10016 SL rejection from modify_position_sltp -> aborted, ticket not updated
    def _reject_sl(ticket, sl=None, tp=None, require_both=False):
        ex.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": 10016}
        return False

    with patch.object(ex, "modify_position_sltp", side_effect=_reject_sl):
        r1 = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3898.0, 3898.2))
    assert r1["updated_tickets"] == []
    assert r1["tp_lifted_tickets"] == []

    # B) 10029 freeze_level aborts without touching modify_position_sltp
    with patch.object(ex, "is_within_freeze_level", return_value=True), \
         patch.object(ex, "modify_position_sltp", wraps=ex.modify_position_sltp) as spy_mod:
        r_frz = ex.evaluate_dynamic_atr_engine(sig, tick=_tick(3898.0, 3898.2))
    assert spy_mod.call_count == 0
    assert r_frz["updated_tickets"] == []
    assert r_frz["tp_lifted_tickets"] == []


@pytest.mark.asyncio
async def test_migrated_universal_forward_only_edit_sl_guard(tmp_path):
    """(Migrated from test_11) Edited message attempting to loosen SL is rejected; /be preserves RUNNER_ACTIVE."""
    ex = _setup_runtime(tmp_path, "edit_be_parser")
    sig = main.state_mgr.create_signal(
        message_id=1101,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3659.5,
        layers={
            "L1": {"lot": 0.01, "target_price": 3659.0, "entry_price": 3659.0, "broker_sl": 3659.5, "sl": 3659.5, "tp": 3669.0, "tp_stage": "TP2", "ticket": 11001, "order_type": "MARKET", "status": "OPEN", "dynamic_atr_trail_sl": 3659.5},
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
        evt_ed = MockTelethonEvent(1101, "BUY 3659-3640\nSL 3630\nTP 3662\nTP 3669\nTP 3690", chat_id=-1003712081715)
        await main.handle_message_edited(evt_ed)

    assert all(sl is None for _, sl, _ in sl_sent)
    s_ed = main.state_mgr.get_signal(1101, chat_id=-1003712081715)
    assert s_ed["sl"] == pytest.approx(3659.5)
    assert s_ed["layers"]["L1"]["tp_stage"] == "TP2"

    # /be preserves RUNNER_ACTIVE status
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

    # Parser guards
    assert parser_mod.parse_message("TP4 hit on the sells btw + 530pops from 3757", is_reply=True, reply_to_msg_id=13164) is None
    assert parser_mod.parse_message("Running 60pips move sl to BE if you want.", is_reply=True, reply_to_msg_id=9940) is None
    p_be = parser_mod.parse_message("TP1 hit, go breakeven!", is_reply=True, reply_to_msg_id=13164)
    assert p_be is not None and p_be.management_action == "ACTION_BREAKEVEN"


@pytest.mark.asyncio
async def test_migrated_deal_classification_deal_dedup_and_silent_ledger_backfill(tmp_path):
    """(Migrated from test_12) Deal dedup, exit classification, and silent ledger backfill."""
    ex = _setup_runtime(tmp_path, "telemetry")
    ex.dry_run = False
    ex._connected = True

    deals = [
        SimpleNamespace(entry=0, position_id=1201, ticket=101, price=3740.0, profit=0.0, commission=-0.14, swap=0.0, fee=0.0, volume=0.02, reason=0, time=100),
        SimpleNamespace(entry=1, position_id=1201, ticket=102, price=3720.0, profit=5.00, commission=0.0, swap=0.0, fee=0.0, volume=0.01, reason=4, time=200),
        SimpleNamespace(entry=1, position_id=1201, ticket=103, price=3710.0, profit=9.00, commission=0.0, swap=-0.06, fee=0.0, volume=0.01, reason=5, time=300),
    ]
    sig_dedup = {"accounted_tickets": [], "accounted_deal_tickets": [], "banked_profit": 0.0}
    with patch.object(mt5_executor, "mt5") as mock_mt5:
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

    # Deal-price exit classification
    sig_cls = {"direction": "SELL"}
    assert main._classify_deal_exit_code(sig_cls, 1, {"role": "BANK_LAYER", "entry_price": 3741.0}, {"reason": "SL", "exit_price": 3735.0}) == "DYNAMIC_ATR_TRAIL_SL"
    assert main._classify_deal_exit_code(sig_cls, 2, {"role": "CORE_RUNNER", "entry_price": 3742.5, "dynamic_atr_trail_sl": 3739.0}, {"reason": "SL", "exit_price": 3739.0}) == "DYNAMIC_ATR_TRAIL_SL"
    assert main._classify_deal_exit_code(sig_cls, 3, {"role": "BANK_TP1", "entry_price": 3742.0}, {"reason": "SL", "exit_price": 3742.0}) == "BE_SL"
    assert main._classify_deal_exit_code(sig_cls, 4, {"role": "BANK_TP1", "entry_price": 3742.0}, {"reason": "SL", "exit_price": 3751.0}) == "INITIAL_SL"
    assert main._classify_deal_exit_code(sig_cls, 5, {"role": "STAGE2_BANKER", "entry_price": 3743.0}, {"reason": "TP", "exit_price": 3727.0}) == "STAGE2_BANK_TP"

    # Silent ledger backfill on startup
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


def _setup_runtime(tmp_path, tag: str) -> MT5Executor:
    return _reset_main_for_dyn_test(tmp_path, tag)


# ---------------------------------------------------------------------------
# 9. Saved Messages Commands & Status Updates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_new_message_persists_scaled_sl_tp_and_health_display(tmp_path, monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    ex = _reset_main_for_dyn_test(tmp_path, "msg_dyn")
    ex._dry_run_atr = 6.00  # 1.50x volatility scaling
    monkeypatch.setattr(main, "notify_saved_messages", AsyncMock())
    monkeypatch.setattr(main, "_update_live_terminal_status_async", AsyncMock())

    evt = MockTelethonEvent(
        msg_id=89001,
        text="BUY LIMIT 3890-3880\nSL 3870\nTP 3894\nTP 3900",
        chat_id=-1003712081715,
    )
    await main.handle_new_message(evt)
    saved = main.state_mgr.get_signal(89001, chat_id=-1003712081715)
    assert saved is not None
    # Zone bottom = 3880 -> SL dist = 10 * 1.5 = 15 -> SL = 3865.0; Zone top L1 = 3890 -> TP1 dist = 4 * 1.5 = 6 -> 3896.0
    assert saved["sl"] == pytest.approx(3865.0)
    assert saved["raw_tp1"] == pytest.approx(3896.0)
    assert saved["final_tp"] == pytest.approx(3895.5)
    assert saved.get("dynamic_risk", {}).get("applied") is True

    evt_h = MockTelethonEvent(msg_id=89002, text="/health", chat_id="me")
    await main.handle_saved_messages_command(evt_h)
    assert "Dynamic ATR Risk" in evt_h.replies[0]
    assert "$6.00" in evt_h.replies[0]


@pytest.mark.asyncio
async def test_saved_messages_dynamic_risk_and_atr_trail_commands(tmp_path, monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    ex = _reset_main_for_dyn_test(tmp_path, "saved_dyn")
    ex._dry_run_atr = 5.00

    evt_q = MockTelethonEvent(msg_id=88001, text="/dynamicrisk", chat_id="me")
    await main.handle_saved_messages_command(evt_q)
    assert any("ADAPTIVE" in r and "$5.00" in r for r in evt_q.replies)

    evt_tune = MockTelethonEvent(msg_id=88002, text="/dynamicrisk 2.0 1.25 M15", chat_id="me")
    await main.handle_saved_messages_command(evt_tune)
    assert config.DYNAMIC_SL_ATR_MULT == 2.0
    assert config.DYNAMIC_TP_ATR_MULT == 1.25
    assert config.DYNAMIC_ATR_TIMEFRAME == "M15"

    evt_pure = MockTelethonEvent(msg_id=88003, text="/dynamicrisk pure", chat_id="me")
    await main.handle_saved_messages_command(evt_pure)
    assert config.DYNAMIC_SLTP_MODE == "PURE_ATR"

    evt_off = MockTelethonEvent(msg_id=88004, text="/dynamicrisk off", chat_id="me")
    await main.handle_saved_messages_command(evt_off)
    assert config.DYNAMIC_ATR_SLTP_ENABLED is False

    evt_tr_on = MockTelethonEvent(msg_id=88005, text="/atrtrail 1.8 0.5 M5", chat_id="me")
    await main.handle_saved_messages_command(evt_tr_on)
    assert config.DYNAMIC_ATR_TRAIL_ENABLED is True
    assert config.DYNAMIC_ATR_TRAIL_MULT == 1.8
    assert config.DYNAMIC_ATR_TRAIL_ACTIVATION_MULT == 0.5
    assert config.DYNAMIC_ATR_TRAIL_TIMEFRAME == "M5"

    evt_st = MockTelethonEvent(msg_id=88006, text="/status", chat_id="me")
    await main.handle_saved_messages_command(evt_st)
    assert "/dynamicrisk" in evt_st.replies[0]
    assert "/atrtrail" in evt_st.replies[0]
    assert "ATR Trail=🟢 ON" in evt_st.replies[0]

    evt_tr_off = MockTelethonEvent(msg_id=88007, text="/atrtrail off", chat_id="me")
    await main.handle_saved_messages_command(evt_tr_off)
    assert config.DYNAMIC_ATR_TRAIL_ENABLED is False
