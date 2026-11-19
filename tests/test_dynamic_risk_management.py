"""Tests for Feature 4: Dynamic Risk Management (ATR-scaled SL/TP & optional ATR trailing stop)."""

from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import dynamic_risk
import main
import mt5_executor
import startup_checks
from mt5_executor import MT5Executor
from parser import ZoneInfo
from state_manager import StateManager
from tests.test_copier_scenarios import MockTelethonEvent
from trade_ledger import TradeLedger


def _pin_dynamic_defaults(monkeypatch) -> None:
    """Restore default Dynamic Risk config values across tests."""
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
    monkeypatch.setattr(config, "DYNAMIC_TP_MIN_DOLLARS", 2.00)
    monkeypatch.setattr(config, "DYNAMIC_TP_MAX_DOLLARS", 50.00)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_SCALE_MIN", 0.75)
    monkeypatch.setattr(config, "DYNAMIC_SLTP_SCALE_MAX", 1.75)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_TIMEFRAME", "M5")
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 1.50)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25)
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


# ---------------------------------------------------------------------------
# 4. Optional Dynamic ATR Trailing Stop
# ---------------------------------------------------------------------------

def test_optional_atr_trailing_stop_off_by_default_and_dynamic_ratchet_when_enabled(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_atr = 2.00

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3885.0,
        "market_tickets": [101],
        "layers": {"L1": {"ticket": 101, "status": "OPEN", "entry_price": 3895.0, "sl": 3885.0}},
    }

    # OFF by default -> complete no-op
    assert config.DYNAMIC_ATR_TRAIL_ENABLED is False
    res_off = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3905.0, ask=3905.3))
    assert res_off == {"enabled": False, "updated_tickets": []}

    # Enable dynamic ATR trail (mult=1.5, activation=1.0x ATR, min=1.50, max=12.0, step=0.25)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_ENABLED", True)

    # Not armed yet when MFE ($1.50) < activation (1.0 * $2.00 = $2.00)
    res_unarmed = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3896.5, ask=3896.8))
    assert res_unarmed["updated_tickets"] == []

    # Armed at bid=3900.0 (MFE=$5.00 >= $2.00): ATR=2.0 -> trail_dist = $3.00 -> SL = 3897.00
    res_arm = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3900.0, ask=3900.3))
    assert res_arm["newly_armed"] is True
    assert res_arm["updated_tickets"] == [(101, "L1", 3897.0, 3.0, 2.0)]
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3897.0)

    # Advance to peak 3902.0 with higher volatility ATR=5.00 (trail_dist widens to $7.50)
    # Candidate SL = 3902.0 - 7.50 = 3894.50 < existing SL 3897.00 => NEVER loosens SL!
    ex._dry_run_atr = 5.00
    res_widen = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3902.0, ask=3902.3))
    assert res_widen["updated_tickets"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3897.0)
    assert sig["layers"]["L1"]["dynamic_atr_trail_peak"] == pytest.approx(3902.0)

    # Volatility contracts ATR=1.00 -> raw 1.5*1=1.50 (at min=1.50) -> cand = 3902.0 - 1.50 = 3900.50
    ex._dry_run_atr = 1.00
    res_tighten = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3902.0, ask=3902.3))
    assert res_tighten["updated_tickets"] == [(101, "L1", 3900.5, 1.5, 1.0)]
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3900.5)

    # Step-size filter: +0.10 move < trail_step_dollars ($0.25) is skipped
    res_small = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3902.10, ask=3902.40))
    assert res_small["updated_tickets"] == []
    assert sig["layers"]["L1"]["broker_sl"] == pytest.approx(3900.5)

    # Stops-level clamping via compute_dynamic_atr_trail_stop: cand SL clamped at least stops_buffer away from live price
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
        stops_buffer=2.00,  # live=3901 - 2.00 = 3899.00 < raw (3905 - 1.5 = 3903.5)
    )
    assert clamped["should_update"] is True
    assert clamped["candidate_sl"] == pytest.approx(3899.00)

    # Spread spike pauses trailing stop updates cleanly
    res_spike = ex.evaluate_dynamic_atr_trailing_stop(sig, tick=SimpleNamespace(bid=3906.0, ask=3909.0))
    assert res_spike["paused_by_spread"] is True
    assert res_spike["updated_tickets"] == []


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


# ---------------------------------------------------------------------------
# 5. Saved Messages Commands, /status & /health, and Startup Validation
# ---------------------------------------------------------------------------

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


def test_startup_checks_dynamic_risk_validation(tmp_path):
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

    for bool_key in (
        "BANK_HARVEST_ENABLED",
        "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED",
        "PROFIT_RATCHET_ENABLED",
        "DYNAMIC_ATR_SLTP_ENABLED",
        "DYNAMIC_ATR_TRAIL_ENABLED",
    ):
        res_bad_bool = startup_checks.validate_environment(
            check_packages=False, cfg=_cfg_with(**{bool_key: "yes"})
        )
        assert res_bad_bool["ok"] is False
        assert any(bool_key in e for e in res_bad_bool["errors"])


def test_production_default_invariants_dynamic_risk_and_no_virtual_exits(monkeypatch):
    clean_keys = (
        "DYNAMIC_ATR_SLTP_ENABLED",
        "DYNAMIC_SLTP_MODE",
        "DYNAMIC_SL_MIN_DOLLARS",
        "GOLD_REVERSAL_GUARD_ENABLED",
        "BANK_HARVEST_ENABLED",
        "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED",
        "PROFIT_RATCHET_ENABLED",
    )
    for key in clean_keys:
        monkeypatch.delenv(key, raising=False)
    with patch("dotenv.load_dotenv", return_value=False):
        raw_cfg = runpy.run_path(str(Path(config.__file__).resolve()))

    assert raw_cfg["DYNAMIC_ATR_SLTP_ENABLED"] is True
    assert raw_cfg["DYNAMIC_SLTP_MODE"] == "ADAPTIVE"
    assert raw_cfg["DYNAMIC_SL_MIN_DOLLARS"] == pytest.approx(4.50)
    assert raw_cfg["GOLD_REVERSAL_GUARD_ENABLED"] is False
    assert raw_cfg["BANK_HARVEST_ENABLED"] is False
    assert raw_cfg["PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED"] is False
    assert raw_cfg["PROFIT_RATCHET_ENABLED"] is True

    assert dynamic_risk.DynamicRiskPolicy().sl_min_dollars == pytest.approx(4.50)
    assert dynamic_risk.read_dynamic_risk_policy(SimpleNamespace()).sl_min_dollars == pytest.approx(4.50)


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


@pytest.mark.asyncio
async def test_pre_tp1_disables_virtual_closes_while_preserving_broker_sl_ratchet(tmp_path, monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    pins = {
        "PROFIT_RATCHET_ENABLED": True,
        "STAGED_EXIT_ENABLED": True,
        "GOLD_REVERSAL_GUARD_ENABLED": False,
        "BANK_HARVEST_ENABLED": False,
        "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED": False,
        "REVERSAL_CONFIRM_TICKS": 2,
        "RUNNER_PROACTIVE_TP_LIFT": True,
        "HARD_MAX_SPREAD_DOLLARS": 1.25,
        "MIN_VALID_SPREAD_DOLLARS": 0.05,
    }
    for k, v in pins.items():
        monkeypatch.setattr(config, k, v, raising=False)
        monkeypatch.setattr(mt5_executor, k, v, raising=False)
        monkeypatch.setattr(main, k, v, raising=False)

    ex = _reset_main_for_dyn_test(tmp_path, "no_virt_pre_tp1")
    main.state_mgr.create_signal(
        message_id=95001,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3890.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 95101, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 95102, "order_type": "MARKET", "status": "OPEN"},
            "L3": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 95103, "order_type": "MARKET", "status": "OPEN"},
            "L4": {"lot": 0.02, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3909.5, "tp_stage": "TP1", "ticket": 95104, "order_type": "MARKET", "status": "OPEN"},
        },
        market_tickets=[95101, 95102, 95103, 95104],
        pending_tickets=[],
        raw_tp1=3910.0,
        raw_tp2=3920.0,
        raw_tp3=3930.0,
        tp_levels={"TP1": 3910.0, "TP2": 3920.0, "TP3": 3930.0},
        final_tp=3909.5,
        recorded_entry_price=3900.0,
        initial_price=3900.0,
        status="ACTIVE",
    )
    for tk_id, lot in ((95101, 0.01), (95102, 0.01), (95103, 0.01), (95104, 0.02)):
        ex._dry_run_ticket_lots[tk_id] = lot

    sl_updates = []
    orig_mod = ex.modify_position_sltp

    def _spy_mod(ticket, sl=None, tp=None, require_both=False):
        if sl is not None:
            sl_updates.append((ticket, sl))
        return orig_mod(ticket, sl=sl, tp=tp, require_both=require_both)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), \
         patch.object(ex, "get_m1_atr", return_value=1.0), \
         patch.object(ex, "modify_position_sltp", side_effect=_spy_mod), \
         patch.object(ex, "close_market_position", wraps=ex.close_market_position) as spy_close:
        # 1) +$3.60 MFE arms TIER_1A (+$0.50 broker SL = 3900.50)
        with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3903.60, ask=3903.80, time=1795200001.0)):
            await main._process_pre_tp1_or_backstop_tick(95001, main.state_mgr.get_signal(95001, chat_id=-1003712081715))
        s_t1a = main.state_mgr.get_signal(95001, chat_id=-1003712081715)
        assert s_t1a["layers"]["L1"]["ratchet_tier"] == "TIER_1A"
        assert s_t1a["layers"]["L1"]["ratchet_sl"] == pytest.approx(3900.50)
        assert any(sl == pytest.approx(3900.50) for _, sl in sl_updates)

        # 2) +$6.00 MFE (>= $5.00 BANK_HARVEST threshold), then -$2.50 wick pullback (3903.50) for 2 ticks
        with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3906.00, ask=3906.20, time=1795200002.0)):
            await main._process_pre_tp1_or_backstop_tick(95001, main.state_mgr.get_signal(95001, chat_id=-1003712081715))
        for idx in range(2):
            with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3903.50, ask=3903.70, time=1795200003.0 + idx)):
                await main._process_pre_tp1_or_backstop_tick(95001, main.state_mgr.get_signal(95001, chat_id=-1003712081715))

        s_after = main.state_mgr.get_signal(95001, chat_id=-1003712081715)
        assert spy_close.call_count == 0
        assert s_after["status"] == "ACTIVE"
        assert s_after["market_tickets"] == [95101, 95102, 95103, 95104]
        assert s_after["layers"]["L1"]["ratchet_tier"] == "TIER_1B"
        assert s_after["layers"]["L1"]["ratchet_sl"] == pytest.approx(3900.75)
        assert s_after["layers"]["L4"]["ratchet_tier"] == "TIER_2"
        assert s_after["layers"]["L4"]["ratchet_sl"] == pytest.approx(3902.50)

        # 3) Tight TP1 ($3.00 leg -> TIER_0T) also keeps broker +$0.50 SL without virtual market-closing
        main.state_mgr.create_signal(
            message_id=95002,
            chat_id=-1003712081715,
            symbol="XAUUSD.s",
            direction="BUY",
            sl=3892.0,
            layers={"L1": {"lot": 0.01, "target_price": 3900.0, "entry_price": 3900.0, "tp": 3902.5, "tp_stage": "TP1", "ticket": 95201, "order_type": "MARKET", "status": "OPEN"}},
            market_tickets=[95201],
            pending_tickets=[],
            raw_tp1=3903.0,
            raw_tp2=3913.0,
            tp_levels={"TP1": 3903.0, "TP2": 3913.0},
            final_tp=3902.5,
            recorded_entry_price=3900.0,
            initial_price=3900.0,
            status="ACTIVE",
        )
        ex._dry_run_ticket_lots[95201] = 0.01
        with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3902.50, ask=3902.70, time=1795200010.0)):
            await main._process_pre_tp1_or_backstop_tick(95002, main.state_mgr.get_signal(95002, chat_id=-1003712081715))
        for idx in range(2):
            with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3900.90, ask=3901.10, time=1795200011.0 + idx)):
                await main._process_pre_tp1_or_backstop_tick(95002, main.state_mgr.get_signal(95002, chat_id=-1003712081715))
        s_t0 = main.state_mgr.get_signal(95002, chat_id=-1003712081715)
        assert spy_close.call_count == 0
        assert s_t0["status"] == "ACTIVE"
        assert s_t0["layers"]["L1"]["ratchet_tier"] == "TIER_0T"
        assert s_t0["layers"]["L1"]["ratchet_sl"] == pytest.approx(3900.50)


def test_post_tp1_runner_disables_near_target_squeeze_virtual_close(monkeypatch):
    _pin_dynamic_defaults(monkeypatch)
    pins = {
        "PROFIT_RATCHET_ENABLED": True,
        "STAGED_EXIT_ENABLED": True,
        "GOLD_REVERSAL_GUARD_ENABLED": False,
        "BANK_HARVEST_ENABLED": False,
        "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED": False,
        "REVERSAL_CONFIRM_TICKS": 2,
        "HARD_MAX_SPREAD_DOLLARS": 1.25,
        "MIN_VALID_SPREAD_DOLLARS": 0.05,
    }
    for k, v in pins.items():
        monkeypatch.setattr(config, k, v, raising=False)
        monkeypatch.setattr(mt5_executor, k, v, raising=False)

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_ticket_lots[95304] = 0.02

    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "sl": 3880.0,
        "layers": {
            "L4": {
                "lot": 0.02,
                "target_price": 3890.0,
                "entry_price": 3890.0,
                "tp": 3920.0,
                "tp_stage": "TP3",
                "ticket": 95304,
                "order_type": "MARKET",
                "status": "RUNNER",
                "role": "CORE_RUNNER",
                "mfe_dollars": 29.0,
                "mfe_peak_price": 3919.0,
            }
        },
        "market_tickets": [95304],
        "runner_layers": ["L4"],
        "runner_tickets": [95304],
        "runner_layer": "L4",
        "runner_ticket": 95304,
        "runner_milestone_idx": 3,
        "runner_milestone_name": "TP3",
        "runner_stage": 2,
        "runner_cleared_milestones": ["TP1", "TP2"],
        "runner_trail_peak": 3919.0,
        "runner_peak_price": 3919.0,
        "runner_trail_stop": 3913.0,
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "raw_tp3": 3920.0,
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0},
        "recorded_entry_price": 3890.0,
        "tp1_reached": True,
        "tp2_reached": True,
        "status": "RUNNER_ACTIVE",
    }

    with patch.object(ex, "get_m1_atr", return_value=1.0), \
         patch.object(ex, "close_market_position", wraps=ex.close_market_position) as spy_close:
        # Peak at 3919.00 (90% of TP2->TP3 leg) sets is_squeeze=True; virt_sl=3916.75 > broker ratchet SL=3916.25
        with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3919.00, ask=3919.20, time=1795200100.0)):
            assert ex.manage_designated_runner(sig) is True

        # Pullback to 3916.50 (below virt_sl 3916.75, above broker SL 3916.25) for 3 ticks must NOT virtual-close
        for idx in range(3):
            with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3916.50, ask=3916.70, time=1795200101.0 + idx)):
                assert ex.manage_designated_runner(sig) is True

        assert spy_close.call_count == 0
        assert sig["status"] == "RUNNER_ACTIVE"
        assert sig["layers"]["L4"]["ratchet_sl"] >= 3916.00
