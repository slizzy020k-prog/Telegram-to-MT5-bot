import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import MetaTrader5 as mt5
import pytest

import config
import main
import mt5_executor as ex_mod
from mt5_executor import MT5Executor, normalize_lot_to_symbol, scale_layer_lots
from parser import ZoneInfo, compute_zone_4tiers, derive_tp_levels, parse_message
from state_manager import StateManager
from trade_ledger import TradeLedger


def _make_tick(bid: float, ask: float, ts: float = 1794886034.0) -> SimpleNamespace:
    return SimpleNamespace(bid=bid, ask=ask, time=ts)


@pytest.fixture(autouse=True)
def _pin_v31_v35_baseline(monkeypatch):
    """Pin Tiered Stage Banking off for legacy v3.1-v3.5 tests (L1-L3 bank at TP1, L4 runner)."""
    monkeypatch.setattr(config, "TIERED_STAGE_BANKING_ENABLED", False, raising=False)
    monkeypatch.setattr(ex_mod, "TIERED_STAGE_BANKING_ENABLED", False, raising=False)
    monkeypatch.setattr(main, "TIERED_STAGE_BANKING_ENABLED", False, raising=False)


def _make_sym_info(
    digits: int = 2,
    point: float = 0.01,
    vol_min: float = 0.01,
    vol_max: float = 100.0,
    vol_step: float = 0.01,
    stops_level: int = 50,
    filling_mode: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        digits=digits,
        point=point,
        spread=20,
        trade_mode=4,
        filling_mode=filling_mode,
        visible=True,
        trade_stops_level=stops_level,
        volume_min=vol_min,
        volume_max=vol_max,
        volume_step=vol_step,
    )


def _build_mock_mt5(tick_ref: dict, sym_info: SimpleNamespace = None, start_ticket: int = 4000):
    sym_obj = sym_info or _make_sym_info()
    counter = {"n": start_ticket}
    sent_requests = []
    positions = []
    orders = []
    candles = {"m1": None}

    def _order_send(req):
        sent_requests.append(dict(req))
        counter["n"] += 1
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=counter["n"], comment="Mock OK")

    def _positions_get(*args, **kwargs):
        t = kwargs.get("ticket")
        if t is not None:
            return [p for p in positions if p.ticket == t]
        return list(positions)

    def _orders_get(*args, **kwargs):
        t = kwargs.get("ticket")
        if t is not None:
            return [o for o in orders if o.ticket == t]
        return list(orders)

    def _copy_rates(*args, **kwargs):
        return candles["m1"]

    patches = [
        patch.object(ex_mod.mt5, "initialize", return_value=True),
        patch.object(ex_mod.mt5, "shutdown", return_value=True),
        patch.object(ex_mod.mt5, "terminal_info", return_value=SimpleNamespace(connected=True, trade_allowed=True)),
        patch.object(ex_mod.mt5, "account_info", return_value=SimpleNamespace(
            login=36393319, server="PUPrime-Live 6", balance=500.0, credit=0.0,
            equity=500.0, profit=0.0, margin=0.0, margin_free=500.0, currency="GBP",
        )),
        patch.object(ex_mod.mt5, "symbol_info", return_value=sym_obj),
        patch.object(ex_mod.mt5, "symbol_select", return_value=True),
        patch.object(ex_mod.mt5, "symbol_info_tick", side_effect=lambda s: tick_ref["tick"]),
        patch.object(ex_mod.mt5, "order_send", side_effect=_order_send),
        patch.object(ex_mod.mt5, "positions_get", side_effect=_positions_get),
        patch.object(ex_mod.mt5, "orders_get", side_effect=_orders_get),
        patch.object(ex_mod.mt5, "copy_rates_from_pos", side_effect=_copy_rates),
        patch.object(ex_mod.mt5, "last_error", return_value=(1, "OK")),
    ]
    return {
        "patches": patches,
        "sent_requests": sent_requests,
        "positions": positions,
        "orders": orders,
        "candles": candles,
    }


# ---------------------------------------------------------------------------
# 1. BROKER VOLUME SAFETY, PROPORTIONAL SCALING & ATOMIC BUDGET CAP
# ---------------------------------------------------------------------------

def test_normalize_lot_to_symbol_and_scale_layer_lots():
    sym_std = _make_sym_info(vol_min=0.01, vol_max=100.0, vol_step=0.01)
    assert normalize_lot_to_symbol(0.0, sym_std) == 0.0
    assert normalize_lot_to_symbol(-0.05, sym_std) == 0.0
    assert normalize_lot_to_symbol(float("nan"), sym_std) == 0.0
    assert normalize_lot_to_symbol(0.004, sym_std) == 0.01  # clamped up to volume_min
    assert normalize_lot_to_symbol(0.019, sym_std) == 0.01  # floored to step (never rounds up!)

    sym_tight = _make_sym_info(vol_min=0.02, vol_max=0.05, vol_step=0.02)
    assert normalize_lot_to_symbol(0.01, sym_tight) == 0.02
    assert normalize_lot_to_symbol(0.039, sym_tight) == 0.02
    assert normalize_lot_to_symbol(0.10, sym_tight) == 0.04  # step=0.02 inside <= 0.05 max

    # Proportional scaling [0.01, 0.01, 0.01, 0.02]
    assert scale_layer_lots([0.01, 0.01, 0.01, 0.02], 2.0, sym_std) == [0.02, 0.02, 0.02, 0.04]
    assert scale_layer_lots([0.01, 0.01, 0.01, 0.02], 3.0, sym_std) == [0.03, 0.03, 0.03, 0.06]
    # Invalid base / multiplier safe fallback
    assert scale_layer_lots([0.01, -1.0, 0.01, 0.02], -5.0, sym_std) == [0.01, 0.01, 0.01, 0.02]


def test_allocate_4layer_lots_atomic_budget_cap(monkeypatch):
    monkeypatch.setattr(config, "ENFORCE_HIGH_RISK_LOT_CAP", True)
    ex = MT5Executor(dry_run=True)
    ex.connect()

    # £100 capital -> high_risk_lots=0.10 -> full [0.01, 0.01, 0.01, 0.02] fits
    ex._dry_run_capital = 100.0
    lots_ok, warn_ok = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_ok == [0.01, 0.01, 0.01, 0.02]
    assert warn_ok is None

    # Already committed 0.06 lots -> 0.04 remaining -> deterministic cap prioritizing L4 runner
    ex._dry_run_ticket_lots = {1: 0.06}
    lots_cap4, warn_cap4 = ex.allocate_4layer_lots("XAUUSD.s")
    assert sum(lots_cap4) == pytest.approx(0.04)
    assert lots_cap4 == [0.01, 0.01, 0.01, 0.01]
    assert warn_cap4 is not None and "High-Risk Cap applied" in warn_cap4

    # 0.03 active open remaining -> execute_4layer_market keeps L1, L2, L4 (0.03 total) while L3 is skipped;
    # meanwhile pending limit zone layers keep full pending lots uncapped.
    ex._dry_run_ticket_lots = {1: 0.07}
    lots_cap3, warn_cap3 = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_cap3 == [0.01, 0.01, 0.0, 0.01]
    assert warn_cap3 is not None

    mk_m, pd_m, layers_m = ex.execute_4layer_market(
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3875.0,
        final_tp=3899.5,
        raw_tp1=3900.0,
    )
    assert len(mk_m) == 3 and pd_m == []
    assert set(layers_m.keys()) == {"L1", "L2", "L4"}
    assert round(sum(v["lot"] for v in layers_m.values()), 2) == 0.03

    ex._dry_run_ticket_lots = {1: 0.07}
    mk_z, pd_z, layers_z = ex.execute_4layer_zone(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=ZoneInfo(3895.0, 3891.0, 3887.0, "BUY"),
        sl=3875.0,
        final_tp=3899.5,
        raw_tp1=3900.0,
    )
    assert len(mk_z) == 1 and len(pd_z) == 3
    assert set(layers_z.keys()) == {"L1", "L2", "L3", "L4"}
    assert round(sum(v["lot"] for v in layers_z.values()), 2) == 0.05

    # 0.00 remaining -> all zeroed, 0 orders placed
    ex._dry_run_ticket_lots = {1: 0.10}
    lots_zero, warn_zero = ex.allocate_4layer_lots("XAUUSD.s")
    assert lots_zero == [0.0, 0.0, 0.0, 0.0]
    assert "High Risk Lot Cap Reached" in warn_zero
    mk0, pd0, layers0 = ex.execute_4layer_market("XAUUSD.s", "BUY", sl=3875.0, final_tp=3899.5, raw_tp1=3900.0)
    assert mk0 == [] and pd0 == [] and layers0 == {}


# ---------------------------------------------------------------------------
# 2. 4-LAYER ENTRY SPLITTING: ZONE, SINGLE-PRICE (LIMIT/STOP), MARKET NOW
# ---------------------------------------------------------------------------

def test_4layer_zone_splitting_buy_sell_and_cross_boundary():
    # BUY zone: 3895 -> 3887, live ask=3891.00 (crosses L1=3895 and L2=3892.33 -> 2 MARKET, 2 LIMIT)
    tick_ref = {"tick": _make_tick(3890.70, 3891.00)}
    env = _build_mock_mt5(tick_ref)
    for p in env["patches"]:
        p.start()
    try:
        ex = MT5Executor(dry_run=False)
        assert ex.connect() is True

        parsed = parse_message("Buy 3895-3887\nTP1 3900\nTP2 3910\nTP3 3920\nTP4 3930\nTP5 3945\nSL 3875")
        mk, pd, layers = ex.execute_4layer_zone(
            symbol=parsed.symbol,
            direction=parsed.direction,
            zone=parsed.zone,
            sl=parsed.sl,
            final_tp=parsed.final_tp,
            tp_levels=parsed.tp_levels,
        )
        assert len(mk) == 2 and len(pd) == 2
        assert layers["L1"]["target_price"] == 3895.00 and layers["L1"]["order_type"] == "MARKET" and layers["L1"]["tp"] == 3899.50
        assert layers["L2"]["target_price"] == 3892.33 and layers["L2"]["order_type"] == "MARKET" and layers["L2"]["tp"] == 3899.50
        assert layers["L3"]["target_price"] == 3889.67 and layers["L3"]["order_type"] == "LIMIT" and layers["L3"]["tp"] == 3899.50
        assert layers["L4"]["target_price"] == 3887.00 and layers["L4"]["order_type"] == "LIMIT" and layers["L4"]["tp"] == 3899.50
        assert all(layers[k]["tp_stage"] == "TP1" for k in ("L1", "L2", "L3", "L4"))
        assert [layers[k]["lot"] for k in ("L1", "L2", "L3", "L4")] == [0.01, 0.01, 0.01, 0.02]

        # SELL zone: 3910-3920, live bid=3905.00 -> all 4 SELL LIMITs from bottom->top, all initially targeting TP1 (3900.5)
        env["sent_requests"].clear()
        tick_ref["tick"] = _make_tick(3905.00, 3905.40)
        parsed_s = parse_message("Sell 3910-3920\nTP1 3900\nSL 3930")
        mk_s, pd_s, layers_s = ex.execute_4layer_zone(
            symbol=parsed_s.symbol,
            direction=parsed_s.direction,
            zone=parsed_s.zone,
            sl=parsed_s.sl,
            final_tp=parsed_s.final_tp,
            tp_levels=parsed_s.tp_levels,
        )
        assert mk_s == [] and len(pd_s) == 4
        assert [layers_s[k]["target_price"] for k in ("L1", "L2", "L3", "L4")] == [3910.0, 3913.33, 3916.67, 3920.0]
        assert all(layers_s[k]["tp"] == 3900.5 and layers_s[k]["tp_stage"] == "TP1" for k in ("L1", "L2", "L3", "L4"))
    finally:
        for p in reversed(env["patches"]):
            p.stop()


def test_4layer_single_price_limit_and_stop_and_10016_fallback():
    import config
    orig_cons = getattr(config, "SINGLE_PRICE_CONSOLIDATE_ORDERS", True)
    config.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    ex_mod.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    tick_ref = {"tick": _make_tick(3895.00, 3895.50)}
    env = _build_mock_mt5(tick_ref, start_ticket=6000)
    for p in env["patches"]:
        p.start()
    try:
        ex = MT5Executor(dry_run=False)
        ex.connect()

        # 1. BUY STOP @ 3905.00 (above ask 3895.50) -> 4 ORDER_TYPE_BUY_STOP orders
        p_bstop = parse_message("Buy stop 3905\nTP 3915\nSL 3890")
        assert p_bstop.is_stop_order is True and p_bstop.order_intent == "STOP"
        ok, msg = ex.check_preflight(
            p_bstop.symbol, p_bstop.direction, raw_tp1=p_bstop.raw_tp1, sl=p_bstop.sl,
            single_price=p_bstop.single_price, is_stop_order=True, order_intent="STOP",
        )
        assert ok is True
        mk, pd, layers = ex.execute_4layer_single_price(
            p_bstop.symbol, p_bstop.direction, p_bstop.single_price, sl=p_bstop.sl,
            final_tp=p_bstop.final_tp, tp_levels=p_bstop.tp_levels, is_stop_order=True, order_intent="STOP",
        )
        assert mk == [] and len(pd) == 4
        assert all(r["type"] == mt5.ORDER_TYPE_BUY_STOP and r["price"] == 3905.0 for r in env["sent_requests"])
        assert [layers[k]["lot"] for k in ("L1", "L2", "L3", "L4")] == [0.01, 0.01, 0.01, 0.02]

        # 2. SELL STOP @ 3885.00 (below bid 3895.00) -> 4 ORDER_TYPE_SELL_STOP orders
        env["sent_requests"].clear()
        p_sstop = parse_message("Sell stop 3885\nTP 3875\nSL 3900")
        mk2, pd2, layers2 = ex.execute_4layer_single_price(
            p_sstop.symbol, p_sstop.direction, p_sstop.single_price, sl=p_sstop.sl,
            final_tp=p_sstop.final_tp, tp_levels=p_sstop.tp_levels, is_stop_order=True, order_intent="STOP",
        )
        assert mk2 == [] and len(pd2) == 4
        assert all(r["type"] == mt5.ORDER_TYPE_SELL_STOP and r["price"] == 3885.0 for r in env["sent_requests"])

        # 3. Already-crossed BUY STOP (ask 3906.0 >= 3905.0) -> 4 MARKET orders
        env["sent_requests"].clear()
        tick_ref["tick"] = _make_tick(3905.50, 3906.00)
        mk3, pd3, layers3 = ex.execute_4layer_single_price(
            "XAUUSD.s", "BUY", 3905.0, sl=3890.0, final_tp=3914.5,
            tp_levels=derive_tp_levels([3915.0], "BUY"), is_stop_order=True, order_intent="STOP",
        )
        assert len(mk3) == 4 and pd3 == []
        assert all(layers3[k]["order_type"] == "MARKET" for k in ("L1", "L2", "L3", "L4"))

        # 4. Single-Price LIMIT with 10016 Invalid stops fallback -> Market orders
        env["sent_requests"].clear()
        tick_ref["tick"] = _make_tick(3895.00, 3895.50)
        orig_send = ex_mod.mt5.order_send

        def _fail_10016(req):
            if req.get("action") == mt5.TRADE_ACTION_PENDING:
                return SimpleNamespace(retcode=10016, order=0, comment="Invalid stops")
            return orig_send(req)

        with patch.object(ex_mod.mt5, "order_send", side_effect=_fail_10016):
            mk4, pd4, layers4 = ex.execute_4layer_single_price(
                "XAUUSD.s", "BUY", 3895.40, sl=3880.0, final_tp=3904.5,
                tp_levels=derive_tp_levels([3905.0], "BUY"), order_intent="LIMIT",
            )
            assert len(mk4) == 4 and pd4 == []
            assert all(layers4[k]["order_type"] == "MARKET" for k in ("L1", "L2", "L3", "L4"))
    finally:
        config.SINGLE_PRICE_CONSOLIDATE_ORDERS = orig_cons
        ex_mod.SINGLE_PRICE_CONSOLIDATE_ORDERS = orig_cons
        for p in reversed(env["patches"]):
            p.stop()


@pytest.mark.asyncio
async def test_unpriced_market_now_fallback_tp_initializes_all_layers_at_tp1(tmp_path):
    """Verify v3.1 Initial TP1 policy: fallback TP1 sets all 4 open layers initially to final_tp (TP1) while storing TP1..TP5."""
    main.state_mgr = StateManager(tmp_path / "fallback_tp_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "fallback_tp_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    sig = main.state_mgr.create_signal(
        message_id=4401,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {"lot": 0.01, "ticket": 111, "order_type": "MARKET", "status": "OPEN", "tp": None},
            "L2": {"lot": 0.01, "ticket": 112, "order_type": "MARKET", "status": "OPEN", "tp": None},
            "L3": {"lot": 0.01, "ticket": 113, "order_type": "MARKET", "status": "OPEN", "tp": None},
            "L4": {"lot": 0.02, "ticket": 114, "order_type": "MARKET", "status": "OPEN", "tp": None},
        },
        market_tickets=[111, 112, 113, 114],
        pending_tickets=[],
        sl=3880.0,
        raw_tp1=None,
        final_tp=None,
        timestamp=old_ts,
    )

    mod_calls = []
    with patch.object(main.executor, "get_tick", return_value=_make_tick(3895.0, 3895.5)), \
         patch.object(main.executor, "modify_position_sltp", side_effect=lambda ticket, sl=None, tp=None: mod_calls.append((ticket, tp)) or True):
        await main._process_pre_tp1_or_backstop_tick("4401", sig)

    updated = main.state_mgr.get_signal(4401)
    assert updated["fallback_tp_applied"] is True
    assert updated["raw_tp1"] == 3900.0
    assert updated["tp_levels"] == {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}
    assert dict(mod_calls) == {111: 3899.5, 112: 3899.5, 113: 3899.5, 114: 3899.5}
    assert all(updated["layers"][k]["tp"] == 3899.5 and updated["layers"][k]["tp_stage"] == "TP1" for k in ("L1", "L2", "L3", "L4"))


def test_dynamic_pip_size_gold_and_forex():
    tick_ref = {"tick": _make_tick(1.0850, 1.0852)}
    env = _build_mock_mt5(tick_ref, sym_info=_make_sym_info(digits=5, point=0.00001))
    for p in env["patches"]:
        p.start()
    try:
        ex = MT5Executor(dry_run=False)
        ex.connect()
        assert ex.get_pip_size("XAUUSD.s") == 0.10
        assert ex.get_pip_size("EURUSD") == pytest.approx(0.0001)
    finally:
        for p in reversed(env["patches"]):
            p.stop()

    env_jpy = _build_mock_mt5(tick_ref, sym_info=_make_sym_info(digits=3, point=0.001))
    for p in env_jpy["patches"]:
        p.start()
    try:
        ex = MT5Executor(dry_run=False)
        ex.connect()
        assert ex.get_pip_size("USDJPY") == pytest.approx(0.01)
    finally:
        for p in reversed(env_jpy["patches"]):
            p.stop()


# ---------------------------------------------------------------------------
# 3. TP1-TP5 DERIVATION, BUY/SELL STOP PARSING & CONFIG VALIDATION
# ---------------------------------------------------------------------------

def test_derive_tp_levels_and_parser_tp1_to_tp5():
    assert derive_tp_levels([], "BUY") == {}
    assert derive_tp_levels([3900.0], "BUY") == {
        "TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0
    }
    assert derive_tp_levels([3880.0], "SELL") == {
        "TP1": 3880.0, "TP2": 3870.0, "TP3": 3860.0, "TP4": 3850.0, "TP5": 3840.0
    }
    assert derive_tp_levels([3900.0, 3915.0], "BUY", step_pips=[50, 80, 120, 150]) == {
        "TP1": 3900.0, "TP2": 3915.0, "TP3": 3923.0, "TP4": 3935.0, "TP5": 3950.0
    }
    assert derive_tp_levels([1.0850], "BUY", pip_size=0.0001, step_pips=[20, 40, 60, 80]) == {
        "TP1": 1.0850, "TP2": 1.0870, "TP3": 1.0910, "TP4": 1.0970, "TP5": 1.1050
    }

    parsed_2 = parse_message("Buy 3895-3887\nTP1 3902\nTP2 3914\nSL 3875")
    assert parsed_2.explicit_tps == [3902.0, 3914.0]
    assert parsed_2.tp_levels == {"TP1": 3902.0, "TP2": 3914.0, "TP3": 3924.0, "TP4": 3934.0, "TP5": 3944.0}
    assert parsed_2.raw_tp5 == 3944.0

    parsed_7 = parse_message("Buy 3895-3887\nTP 3900\nTP 3910\nTP 3925\nTP 3940\nTP 3960\nTP 3980\nTP 4000\nSL 3875")
    assert parsed_7.tp_levels == {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3925.0, "TP4": 3940.0, "TP5": 3960.0}


def test_parser_buy_stop_sell_stop_and_sl_disambiguation():
    p1 = parse_message("Buy xauusd stop 3905\nTP 3915")
    assert p1 is not None
    assert p1.is_stop_order is True and p1.is_limit_order is False and p1.order_intent == "STOP"
    assert p1.single_price == 3905.0
    assert p1.sl is None and p1.sl_valid is False  # 'Buy xauusd stop 3905' NOT treated as SL!

    p2 = parse_message("Sell stop 3885\nStop 3900\nTP 3870")
    assert p2 is not None
    assert p2.is_stop_order is True and p2.single_price == 3885.0
    assert p2.sl == 3900.0 and p2.sl_valid is True

    tiers_buy = compute_zone_4tiers(3895.0, 3887.0, "BUY")
    tiers_sell = compute_zone_4tiers(3920.0, 3910.0, "SELL")
    assert tiers_buy == {"L1": 3895.0, "L2": 3892.33, "L3": 3889.67, "L4": 3887.0}
    assert tiers_sell == {"L1": 3910.0, "L2": 3913.33, "L3": 3916.67, "L4": 3920.0}


def test_config_validation_safe_fallbacks(monkeypatch):
    try:
        monkeypatch.setenv("BASE_LAYER_LOTS", "[0.01, -0.02, 0.01]")
        monkeypatch.setenv("DYNAMIC_SLTP_MODE", "INVALID_MODE")
        monkeypatch.setenv("RISK_LOT_MULTIPLIER", "-3")
        importlib.reload(config)
        assert config.BASE_LAYER_LOTS == [0.01, 0.01, 0.01, 0.02]
        assert config.DYNAMIC_SLTP_MODE == "ADAPTIVE"
        assert config.RISK_LOT_MULTIPLIER == 1.0

        monkeypatch.setenv("BASE_LAYER_LOTS", "0.02, 0.02, 0.02, 0.04")
        monkeypatch.setenv("DYNAMIC_SLTP_MODE", "pure_atr")
        importlib.reload(config)
        assert config.BASE_LAYER_LOTS == [0.02, 0.02, 0.02, 0.04]
        assert config.DYNAMIC_SLTP_MODE == "PURE_ATR"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ---------------------------------------------------------------------------
# 4. LAYER 4 ACTIVE RUNNER FSM & MULTI-LAYER COEXISTENCE
# ---------------------------------------------------------------------------

def _make_4layer_sig(direction: str = "BUY") -> dict:
    is_buy = direction == "BUY"
    tp1 = 3900.0 if is_buy else 3880.0
    final_tp = 3899.5 if is_buy else 3880.5
    tps = derive_tp_levels([tp1], direction=direction)
    return {
        "symbol": "XAUUSD.s",
        "direction": direction,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3895.0 if is_buy else 3885.0, "tp": final_tp, "tp_stage": "TP1", "ticket": 101, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3892.33 if is_buy else 3887.67, "tp": final_tp, "tp_stage": "TP1", "ticket": 102, "order_type": "LIMIT", "status": "OPEN"},
            "L3": {"lot": 0.01, "target_price": 3889.67 if is_buy else 3890.33, "tp": final_tp, "tp_stage": "TP1", "ticket": 103, "order_type": "LIMIT", "status": "OPEN"},
            "L4": {"lot": 0.02, "target_price": 3887.0 if is_buy else 3893.0, "tp": final_tp, "tp_stage": "TP1", "ticket": 104, "order_type": "LIMIT", "status": "OPEN"},
        },
        "market_tickets": [101, 102, 103, 104],
        "pending_tickets": [],
        "final_tp": final_tp,
        "raw_tp1": tps["TP1"],
        "raw_tp2": tps["TP2"],
        "raw_tp3": tps["TP3"],
        "raw_tp4": tps["TP4"],
        "raw_tp5": tps["TP5"],
        "tp_levels": tps,
        "tp_stage": "TP1",
    }


def test_tp1_scaling_4layer_dynamic_selection_and_early_floor_fix(monkeypatch):
    ex = MT5Executor(dry_run=True)
    ex.connect()

    # Case A: All 4 open + early/proximity TP1 trigger at 3898.20 -> closes L1..L3, keeps L4 at TP2, uses BE floor (never premature 3899.50)
    sig = _make_4layer_sig("BUY")
    with patch.object(ex, "get_tick", return_value=_make_tick(3898.20, 3898.50)):
        rt = ex.handle_tp1_scaling_and_select_runner(sig)
    assert rt == 104
    assert sig["runner_layer"] == "L4"
    assert sig["runner_layers"] == ["L4"]
    assert sig["runner_tickets"] == [104]
    assert sig["status"] == "RUNNER_ACTIVE"
    assert sig["layers"]["L1"]["status"] == "CLOSED"
    assert sig["layers"]["L2"]["status"] == "CLOSED"
    assert sig["layers"]["L3"]["status"] == "CLOSED"
    assert sig["layers"]["L4"]["tp"] == 3910.0 and sig["layers"]["L4"]["tp_stage"] == "TP2"
    assert sig["market_tickets"] == [104]
    assert sig["runner_sl_floor"] == pytest.approx(3887.50)
    with patch.object(ex, "get_tick", return_value=_make_tick(3898.30, 3898.60)):
        assert ex.manage_designated_runner(sig) is True
        assert sig["runner_ticket"] == 104

    # Case B: L1 + L2 + L3 open, L4 pending -> L1 & L2 close at TP1, L3 becomes runner with TP stepped to TP2
    sig3 = _make_4layer_sig("BUY")
    sig3["market_tickets"] = [101, 102, 103]
    sig3["pending_tickets"] = [104]
    sig3["layers"]["L4"]["status"] = "PENDING"
    mod_tps = []
    with patch.object(ex, "get_tick", return_value=_make_tick(3900.20, 3900.50)), \
         patch.object(ex, "modify_position_sltp", side_effect=lambda ticket, sl=None, tp=None: mod_tps.append((ticket, sl, tp)) or True):
        rt3 = ex.handle_tp1_scaling_and_select_runner(sig3)
    assert rt3 == 103
    assert sig3["runner_layer"] == "L3"
    assert sig3["market_tickets"] == [103]
    assert sig3["pending_tickets"] == []
    assert sig3["layers"]["L4"]["status"] == "CANCELLED"
    assert any(t == 103 and tp == 3910.0 for t, _, tp in mod_tps)

    # Case C: CANCEL_PENDING_ON_TP1=False keeps L3, L4 pending while L1 closes and L2 scales to TP2
    sig_keep = _make_4layer_sig("BUY")
    sig_keep["market_tickets"] = [101, 102]
    sig_keep["pending_tickets"] = [103, 104]
    sig_keep["layers"]["L3"]["status"] = "PENDING"
    sig_keep["layers"]["L4"]["status"] = "PENDING"
    monkeypatch.setattr(ex_mod, "CANCEL_PENDING_ON_TP1", False)
    with patch.object(ex, "get_tick", return_value=_make_tick(3900.20, 3900.50)):
        rt_k = ex.handle_tp1_scaling_and_select_runner(sig_keep)
    assert rt_k == 102
    assert sig_keep["runner_layer"] == "L2"
    assert sig_keep["pending_tickets"] == [103, 104]

    # Case D: Legacy TOP/MID/BOTTOM record with tp_levels still uses legacy 3-layer scaling
    legacy_sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "layers": {
            "TOP": {"lot": 0.01, "ticket": 201, "status": "OPEN"},
            "MID": {"lot": 0.01, "ticket": 202, "status": "OPEN"},
            "BOTTOM": {"lot": 0.02, "ticket": 203, "status": "OPEN"},
        },
        "market_tickets": [201, 202, 203],
        "pending_tickets": [],
        "raw_tp1": 3900.0,
        "raw_tp2": 3910.0,
        "tp_levels": derive_tp_levels([3900.0], "BUY"),
    }
    rt_leg = ex.handle_tp1_scaling_and_select_runner(legacy_sig)
    assert rt_leg == 203 and legacy_sig["runner_layer"] == "BOTTOM"
    assert legacy_sig["market_tickets"] == [203]


@pytest.mark.parametrize("target_idx,approach_px,peak_px,pullback_px", [
    (1, 3899.20, 3899.80, 3898.50),  # Tight 10p zone near TP1=3900, 13p pullback
    (2, 3909.20, 3909.80, 3908.50),  # Tight 10p zone near TP2=3910, 13p pullback
    (3, 3919.20, 3919.80, 3918.50),  # Tight 10p zone near TP3=3920, 13p pullback
    (4, 3929.20, 3929.80, 3928.50),  # Tight 10p zone near TP4=3930, 13p pullback
])
def test_runner_fsm_bank_it_near_tp1_tp2_tp3_tp4_pips_mode(monkeypatch, target_idx, approach_px, peak_px, pullback_px):
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 5)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 5)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    sig = _make_4layer_sig("BUY")
    with patch.object(ex, "get_atr", return_value=4.00):
        with patch.object(ex, "get_tick", return_value=_make_tick(3898.10, 3898.30)):
            ex.handle_tp1_scaling_and_select_runner(sig)

        # Clear milestones up to target_idx - 1 cleanly
        for m in range(1, target_idx):
            brk = sig["tp_levels"][f"TP{m}"] + 0.60
            with patch.object(ex, "get_tick", return_value=_make_tick(brk, brk + 0.20)):
                assert ex.manage_designated_runner(sig) is True

        spy_close = MagicMock()
        ex.close_market_position = spy_close

        # Dynamic ATR Engine lifts TP when approaching target within 0.50 * ATR
        with patch.object(ex, "get_tick", return_value=_make_tick(approach_px, approach_px + 0.20)):
            assert ex.manage_designated_runner(sig) is True
        with patch.object(ex, "get_tick", return_value=_make_tick(peak_px, peak_px + 0.20)):
            assert ex.manage_designated_runner(sig) is True
        # Mid-candle pullbacks trigger ZERO virtual market closes; broker SL/TP handles protection
        with patch.object(ex, "get_tick", return_value=_make_tick(pullback_px, pullback_px + 0.20)):
            assert ex.manage_designated_runner(sig) is True

        assert spy_close.called is False
        assert sig["status"] == "RUNNER_ACTIVE"
        assert sig["layers"]["L4"]["status"] == "RUNNER"


def test_runner_fsm_pct_both_modes_and_m1_candle_confirm(monkeypatch):
    """Verify Pure Dynamic ATR momentum-gating: pullbacks > 0.25 * ATR halt TP lift, with zero virtual closes."""
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 4)
    ex = MT5Executor(dry_run=True)
    ex.connect()

    sig = _make_4layer_sig("BUY")
    with patch.object(ex, "get_atr", return_value=4.00):
        with patch.object(ex, "get_tick", return_value=_make_tick(3900.20, 3900.40)):
            ex.handle_tp1_scaling_and_select_runner(sig)
        with patch.object(ex, "get_tick", return_value=_make_tick(3910.60, 3910.80)):
            ex.manage_designated_runner(sig)
        assert sig["layers"]["L4"]["tp"] == 3920.0

        spy_close = MagicMock()
        ex.close_market_position = spy_close

        # Surge toward TP3 to 3919.50 (within 0.50 * ATR of 3920) -> lifts TP to TP4 (3930.0)
        with patch.object(ex, "get_tick", return_value=_make_tick(3919.50, 3919.70)):
            assert ex.manage_designated_runner(sig) is True
        assert sig["layers"]["L4"]["tp"] == 3930.0

        # Pullback > 0.25 * ATR (1.50 pullback to 3918.00 from peak 3919.50) -> momentum lost, zero virtual closes
        with patch.object(ex, "get_tick", return_value=_make_tick(3918.00, 3918.20)):
            assert ex.manage_designated_runner(sig) is True
        assert spy_close.called is False
        assert sig["status"] == "RUNNER_ACTIVE"


def test_runner_fsm_clean_breakthroughs_tp1_to_tp4_and_optional_tp5(monkeypatch):
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 4)
    ex = MT5Executor(dry_run=True)
    ex.connect()

    # 1. Default RUNNER_MAX_TP_STAGE=4 BUY clean stepped run: TP1(3900) -> TP2(3910) -> TP3(3920) -> TP4(3930)
    sig = _make_4layer_sig("BUY")
    mod_tps = []
    with patch.object(ex, "get_atr", return_value=4.00):
        with patch.object(ex, "get_tick", return_value=_make_tick(3900.10, 3900.30)):
            ex.handle_tp1_scaling_and_select_runner(sig)
        assert sig["runner_milestone_idx"] == 2
        assert sig["layers"]["L4"]["tp"] == 3910.0  # stepped initially to TP2!

        with patch.object(ex, "modify_position_sltp", side_effect=lambda ticket, sl=None, tp=None: mod_tps.append((ticket, sl, tp)) or True):
            # Clean break past TP1 (3900.60) -> clears TP1, steps to TP2=3910.0
            with patch.object(ex, "get_tick", return_value=_make_tick(3900.60, 3900.80)):
                assert ex.manage_designated_runner(sig) is True
            assert sig["runner_cleared_milestones"] == ["TP1"]
            floor_tp1 = sig["runner_sl_floor"]

            # Pullback after clean break above floor must NOT fire any virtual close
            with patch.object(ex, "get_tick", return_value=_make_tick(3899.70, 3899.90)):
                assert ex.manage_designated_runner(sig) is True

            # Clean break past TP2 (3910.60) -> steps to TP3=3920.0, floor ratchets up
            with patch.object(ex, "get_tick", return_value=_make_tick(3910.60, 3910.80)):
                assert ex.manage_designated_runner(sig) is True
            assert sig["runner_milestone_idx"] == 3
            assert sig["layers"]["L4"]["tp"] == 3920.0
            assert sig["runner_sl_floor"] > floor_tp1

            # Clean break past TP3 (3920.60) -> steps to TP4=3930.0
            with patch.object(ex, "get_tick", return_value=_make_tick(3920.60, 3920.80)):
                assert ex.manage_designated_runner(sig) is True
            assert sig["runner_milestone_idx"] == 4
            assert sig["layers"]["L4"]["tp"] == 3930.0

            # Reach TP4 ceiling (3930.10)
            with patch.object(ex, "get_tick", return_value=_make_tick(3930.10, 3930.30)):
                ex.manage_designated_runner(sig)
            assert sig["runner_cleared_milestones"] == ["TP1", "TP2", "TP3"]
            assert ex.get_runner_fsm_stage(sig) == 3

        # When broker executes TP at ceiling, position disappears and finalizes
        with patch.object(ex, "get_position_close_info", return_value={"reason": "TP"}), patch("mt5_executor.mt5.positions_get", return_value=None):
            ex.dry_run = False
            assert ex.manage_designated_runner(sig) is False
            ex.dry_run = True
        assert sig["exit_reason_code"] == "TP_CEILING"

    # 2. SELL clean run with optional RUNNER_MAX_TP_STAGE=5 -> reaches TP5 (3840.0)
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 5)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 5)
    sig_s = _make_4layer_sig("SELL")
    with patch.object(ex, "get_atr", return_value=4.00):
        with patch.object(ex, "get_tick", return_value=_make_tick(3879.70, 3879.90)):
            ex.handle_tp1_scaling_and_select_runner(sig_s)
        for px in (3879.40, 3869.40, 3859.40, 3849.40):
            with patch.object(ex, "get_tick", return_value=_make_tick(px - 0.2, px)):
                assert ex.manage_designated_runner(sig_s) is True
        assert sig_s["runner_milestone_idx"] == 5
        with patch.object(ex, "get_tick", return_value=_make_tick(3839.70, 3839.90)):
            ex.manage_designated_runner(sig_s)
        assert ex.get_runner_fsm_stage(sig_s) == 4
        with patch.object(ex, "get_position_close_info", return_value={"reason": "TP"}), patch("mt5_executor.mt5.positions_get", return_value=None):
            ex.dry_run = False
            assert ex.manage_designated_runner(sig_s) is False
            ex.dry_run = True
        assert sig_s["exit_reason_code"] == "TP_CEILING"


# ---------------------------------------------------------------------------
# 5. V3.1 SIMULATION SCENARIOS: SINGLE-LAYER, MULTI-LAYER, STRICT/ANTI-CHOKE
# ---------------------------------------------------------------------------

def test_v31_single_layer_dynamically_becomes_runner_and_scales_tp1_to_tp4(monkeypatch):
    """
    Scenario 1 (1 active layer): L1 starts at TP1 (3899.5), enters tight TP1 zone so
    prepare_deepest_runners_for_tp_breakout proactively lifts L1 -> TP2 (preventing permanent
    broker TP1 closure), handles TP1 breakout with 0 closed layers, and steps TP2 -> TP3 -> TP4!
    """
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 4)
    ex = MT5Executor(dry_run=True)
    ex.connect()
    tps = derive_tp_levels([3900.0, 3910.0, 3920.0, 3930.0], direction="BUY")
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "recorded_entry_price": 3890.0,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 9101, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3888.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 9102, "order_type": "LIMIT", "status": "PENDING"},
        },
        "market_tickets": [9101],
        "pending_tickets": [9102],
        "final_tp": 3899.5,
        "raw_tp1": tps["TP1"],
        "raw_tp2": tps["TP2"],
        "raw_tp3": tps["TP3"],
        "raw_tp4": tps["TP4"],
        "raw_tp5": tps["TP5"],
        "tp_levels": tps,
    }

    with patch.object(ex, "get_atr", return_value=4.00):
        # Midrange (3895.0) must NOT lift TP prematurely
        prep_mid = ex.prepare_deepest_runners_for_tp_breakout(sig, live_price=3895.0)
        assert prep_mid["runner_layers"] == ["L1"]
        assert prep_mid["early_layers"] == []
        assert prep_mid["lifted_tickets"] == []
        assert sig["layers"]["L1"]["tp"] == 3899.5

        # Entering tight TP1 proximity zone (3899.20, within 0.5*ATR of 3900.0) lifts L1 to TP2
        prep_near = ex.prepare_deepest_runners_for_tp_breakout(sig, live_price=3899.20)
        assert prep_near["lifted_tickets"] == [9101]
        assert sig["layers"]["L1"]["tp"] == 3910.0
        assert sig["layers"]["L1"]["tp_stage"] == "TP2"

        # Strong breakout through TP1 (3900.60) -> cancels L2 pending, closes 0 open layers, promotes L1 to RUNNER!
        with patch.object(ex, "get_tick", return_value=_make_tick(3900.60, 3900.80)):
            rt = ex.handle_tp1_scaling_and_select_runner(sig)
        assert rt == 9101
        assert sig["last_banked_tickets"] == []
        assert sig["runner_layers"] == ["L1"]
        assert sig["runner_tickets"] == [9101]
        assert sig["market_tickets"] == [9101]
        assert sig["pending_tickets"] == []
        assert sig["status"] == "RUNNER_ACTIVE"
        assert sig["layers"]["L1"]["status"] == "RUNNER"
        assert sig["runner_milestone_idx"] == 2
        # Msg 10196 progressive-trail change: entry 3890.00, live/peak 3900.60 -> MFE $10.60,
        # which sits in the EXTENDED zone (MFE >= $10.00) where the ATR trail multiplier
        # tightens from the STANDARD 1.40x to 1.00x. trail_dist = M5 ATR 4.00 x 1.00 = 4.00,
        # so the runner SL floor is 3900.60 - 4.00 = 3896.60 (the old expectation 3894.60
        # came from the pre-Upgrade-5 single 1.50x multiplier). The new value is TIGHTER
        # and is the specified behaviour.
        assert sig["runner_sl_floor"] == pytest.approx(3896.60)

        # Stepped breakout progression: TP2 (3910.60) -> TP3 (3920.60) -> TP4 (3930.10)
        with patch.object(ex, "get_tick", return_value=_make_tick(3910.60, 3910.80)):
            assert ex.manage_designated_runner(sig) is True
            assert sig["layers"]["L1"]["tp"] == 3920.0 and sig["layers"]["L1"]["tp_stage"] == "TP3"

        with patch.object(ex, "get_tick", return_value=_make_tick(3920.60, 3920.80)):
            assert ex.manage_designated_runner(sig) is True
            assert sig["layers"]["L1"]["tp"] == 3930.0 and sig["layers"]["L1"]["tp_stage"] == "TP4"

        with patch.object(ex, "get_tick", return_value=_make_tick(3930.10, 3930.30)):
            ex.manage_designated_runner(sig)

        with patch.object(ex, "get_position_close_info", return_value={"reason": "TP"}), patch("mt5_executor.mt5.positions_get", return_value=None):
            ex.dry_run = False
            assert ex.manage_designated_runner(sig) is False
            ex.dry_run = True
        assert sig["status"] == "CLOSED"
        assert sig["exit_reason_code"] == "TP_CEILING"


def test_v31_multilayer_2_3_4_and_dual_runner_scaling_and_late_fill_reset(monkeypatch):
    """
    Scenario 2 (2 to 4 active layers + Tiered Stage Banking + late limit role protection):
    - 2 open layers -> L1 banks at TP1, L2 scales TP2..TP4.
    - Late fill isolation: L1 pre-lifted near TP1 locks roles; subsequent L4 fill safely gets LATE_RECOVERY with TP1.
    - 4 open layers with Tiered Stage Banking -> L1..L2 bank at TP1, L3 becomes STAGE2_BANKER capped at TP2, L4 CORE_RUNNER scales TP2..TP4.
    """
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 4)
    ex = MT5Executor(dry_run=True)
    ex.connect()

    # Check 2 active layers
    sig2 = _make_4layer_sig("BUY")
    sig2["market_tickets"] = [101, 102]
    sig2["pending_tickets"] = [103, 104]
    sig2["layers"]["L3"]["status"] = "PENDING"
    sig2["layers"]["L4"]["status"] = "PENDING"
    with patch.object(ex, "get_atr", return_value=4.00):
        with patch.object(ex, "get_tick", return_value=_make_tick(3900.60, 3900.80)):
            rt2 = ex.handle_tp1_scaling_and_select_runner(sig2)
    assert rt2 == 102 and sig2["runner_layers"] == ["L2"] and sig2["last_banked_tickets"] == [101]

    # Check late fill role protection: L1 lifted near TP1, then L4 fills -> L1 keeps TP2 runner role, L4 gets LATE_RECOVERY
    sig_late = _make_4layer_sig("BUY")
    sig_late["market_tickets"] = [101]
    sig_late["pending_tickets"] = [104]
    sig_late["layers"]["L2"]["status"] = "CANCELLED"
    sig_late["layers"]["L3"]["status"] = "CANCELLED"
    sig_late["layers"]["L4"]["status"] = "PENDING"
    with patch.object(ex, "get_atr", return_value=4.00):
        ex.prepare_deepest_runners_for_tp_breakout(sig_late, live_price=3899.30)
        assert sig_late["layers"]["L1"]["tp"] == 3910.0  # L1 lifted
        sig_late["layers"]["L4"]["status"] = "FILLED"
        sig_late["market_tickets"] = [101, 104]
        sig_late["pending_tickets"] = []
        prep2 = ex.prepare_deepest_runners_for_tp_breakout(sig_late, live_price=3899.40)
        assert prep2["runner_layers"] == ["L1"]
        assert sig_late["layers"]["L1"]["tp"] == 3910.0
        assert sig_late["layers"]["L4"]["role"] == "LATE_RECOVERY"
        assert sig_late["layers"]["L4"]["tp"] == 3899.5

    # Check 4 active layers with Tiered Stage Banking
    monkeypatch.setattr(config, "TIERED_STAGE_BANKING_ENABLED", True)
    monkeypatch.setattr(ex_mod, "TIERED_STAGE_BANKING_ENABLED", True)
    sig4 = _make_4layer_sig("BUY")
    with patch.object(ex, "get_atr", return_value=4.00):
        with patch.object(ex, "get_tick", return_value=_make_tick(3900.60, 3900.80)):
            ex.handle_tp1_scaling_and_select_runner(sig4)
        assert sig4["runner_layers"] == ["L4"]
        assert sig4["last_banked_tickets"] == [101, 102]
        assert sig4["layers"]["L1"]["status"] == "CLOSED" and sig4["layers"]["L2"]["status"] == "CLOSED"
        assert sig4["layers"]["L3"]["role"] == "STAGE2_BANKER" and sig4["layers"]["L3"]["tp"] == 3910.0
        assert sig4["layers"]["L4"]["role"] == "CORE_RUNNER" and sig4["layers"]["L4"]["tp"] == 3910.0

        with patch.object(ex, "get_tick", return_value=_make_tick(3910.60, 3910.80)):
            assert ex.manage_designated_runner(sig4) is True
        assert sig4["layers"]["L3"]["tp"] == 3910.0 and sig4["layers"]["L4"]["tp"] == 3920.0

        with patch.object(ex, "get_tick", return_value=_make_tick(3920.60, 3920.80)):
            assert ex.manage_designated_runner(sig4) is True
        assert sig4["layers"]["L4"]["tp"] == 3930.0

        with patch.object(ex, "get_tick", return_value=_make_tick(3930.10, 3930.30)):
            ex.manage_designated_runner(sig4)
        with patch.object(ex, "get_position_close_info", return_value={"reason": "TP"}), patch("mt5_executor.mt5.positions_get", return_value=None):
            ex.dry_run = False
            assert ex.manage_designated_runner(sig4) is False
            ex.dry_run = True
        assert sig4["exit_reason_code"] == "TP_CEILING"


@pytest.mark.asyncio
async def test_v31_strict_activation_zone_and_near_miss_tp1_reversal_banks_profit(tmp_path, monkeypatch):
    """
    Scenario 3 (Pure Dynamic ATR BE Lock, Trailed SL & Zero Early Virtual Closes):
    - Mid-range drops stay ACTIVE without premature closes.
    - Near-miss surge within a hair of TP1 (3899.75 vs TP1=3900.0) activates BE lock, trails broker SL,
      sweeps pending orders, and mid-candle pullbacks trigger ZERO close_market_position calls!
    """
    main.state_mgr = StateManager(tmp_path / "strict_rev_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "strict_rev_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Pin the STANDARD-zone ATR trail multiplier to the specified 1.40 (not the .env
    # override) so the trailed-SL expectation below is anchored to the spec.
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.40, raising=False)

    alerts = []
    orig_notify = main.notify_saved_messages
    main.notify_saved_messages = AsyncMock(side_effect=lambda msg: alerts.append(msg))
    try:
        with patch.object(main.executor, "get_atr", return_value=4.00):
            # 1. Tight 25-pip TP leg (3890 -> 3892.5): mid-range drop (3891.20, 3889.80) stays ACTIVE
            main.state_mgr.create_signal(
                message_id=9301,
                symbol="XAUUSD.s",
                direction="BUY",
                layers={"L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3892.0, "tp_stage": "TP1", "ticket": 301, "order_type": "MARKET", "status": "OPEN"}},
                market_tickets=[301],
                pending_tickets=[],
                raw_tp1=3892.5,
                final_tp=3892.0,
                initial_price=3890.0,
            )
            for px in (3891.20, 3889.80):
                with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
                    await main._process_pre_tp1_or_backstop_tick("9301", dict(main.state_mgr.get_signal(9301)))
            assert main.state_mgr.get_signal(9301)["status"] == "ACTIVE"

            # 2. Normal 100-pip leg (3890 -> 3900): mid-range drop at 3895 -> 3893.2 stays ACTIVE
            sig = main.state_mgr.create_signal(
                message_id=9302,
                symbol="XAUUSD.s",
                direction="BUY",
                layers={
                    "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 401, "order_type": "MARKET", "status": "OPEN"},
                    "L2": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 402, "order_type": "MARKET", "status": "OPEN"},
                },
                market_tickets=[401, 402],
                pending_tickets=[403],
                raw_tp1=3900.0,
                final_tp=3899.5,
                initial_price=3890.0,
            )
            for px in (3895.00, 3893.20):
                with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
                    await main._process_pre_tp1_or_backstop_tick("9302", dict(main.state_mgr.get_signal(9302)))
            assert main.state_mgr.get_signal(9302)["status"] == "ACTIVE"

            spy_close = MagicMock()
            main.executor.close_market_position = spy_close

            # Surge to 3899.75 ($9.75 profit, 97.5% of TP1!) -> activates BE lock & trails SL to 3894.15, sweeps pending
            for px in (3899.75, 3898.45, 3898.40):
                with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
                    await main._process_pre_tp1_or_backstop_tick("9302", dict(main.state_mgr.get_signal(9302)))
            cur_sig = main.state_mgr.get_signal(9302)
            assert cur_sig["status"] == "ACTIVE"
            assert cur_sig["pending_tickets"] == []
            assert cur_sig["layers"]["L1"]["be_locked"] is True
            # Msg 10196: peak 3899.75 - entry 3890.00 = MFE $9.75, which is BELOW the $10.00
            # EXTENDED boundary, so this is the STANDARD zone. The STANDARD zone uses
            # 1.40 x M5 ATR (floor $2.50), i.e. trail_dist = 4.00 x 1.40 = 5.60, so the
            # trailed SL is 3899.75 - 5.60 = 3894.15. The previous expectation 3893.75
            # came from the .env override DYNAMIC_ATR_TRAIL_MULT=1.50 (4.00 x 1.50 = 6.00);
            # the multiplier is now pinned to the specified 1.40 below so the number is
            # anchored to the spec rather than to a value that happened to be in .env.
            assert cur_sig["layers"]["L1"]["sl"] == pytest.approx(3894.15)

            # Mid-candle pullback to 3896.70 (above SL) triggers ZERO virtual market closes!
            with patch.object(main.executor, "get_tick", return_value=_make_tick(3896.70, 3896.90)):
                await main._process_pre_tp1_or_backstop_tick("9302", dict(main.state_mgr.get_signal(9302)))
            assert spy_close.called is False
            assert main.state_mgr.get_signal(9302)["status"] == "ACTIVE"

            # When broker executes exit, ledger records clean trade
            await main._record_signal_to_ledger("9302", cur_sig, exit_reason="Dynamic ATR Trailing SL")
            main.state_mgr.update_signal("9302", {"status": "CLOSED", "market_tickets": []})
            assert main.state_mgr.get_signal(9302)["status"] == "CLOSED"
            assert len(main.trade_ledger.read_trades()) == 1
    finally:
        main.notify_saved_messages = orig_notify


@pytest.mark.asyncio
async def test_v31_anti_choke_noise_immunity_and_full_tp1_to_tp4_breakout(tmp_path, monkeypatch):
    """
    Scenario 4 (Anti-Choke Noise Immunity & Pure Dynamic ATR Stepped Progression):
    - Trade enters at 3890 with 4 layers at TP1 (3899.5), approaches TP1 (3899.40), suffers healthy
      micro-pullbacks (7 pips) + isolated noise spike that recovers -> ZERO virtual closes!
    - Then breaks TP1 -> early layers bank TP1 -> L4 steps through TP2 & TP3 with noise immunity -> reaches TP4!
    """
    monkeypatch.setattr(config, "RUNNER_MAX_TP_STAGE", 4)
    monkeypatch.setattr(ex_mod, "RUNNER_MAX_TP_STAGE", 4)
    main.state_mgr = StateManager(tmp_path / "antichoke_state.json")
    main.trade_ledger = TradeLedger(tmp_path / "antichoke_ledger.jsonl")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    orig_notify = main.notify_saved_messages
    main.notify_saved_messages = AsyncMock()
    try:
        tps = derive_tp_levels([3900.0, 3910.0, 3920.0, 3930.0], direction="BUY")
        main.state_mgr.create_signal(
            message_id=9401,
            symbol="XAUUSD.s",
            direction="BUY",
            layers={
                "L1": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 501, "order_type": "MARKET", "status": "OPEN"},
                "L2": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 502, "order_type": "MARKET", "status": "OPEN"},
                "L3": {"lot": 0.01, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 503, "order_type": "MARKET", "status": "OPEN"},
                "L4": {"lot": 0.02, "target_price": 3890.0, "tp": 3899.5, "tp_stage": "TP1", "ticket": 504, "order_type": "MARKET", "status": "OPEN"},
            },
            market_tickets=[501, 502, 503, 504],
            pending_tickets=[],
            raw_tp1=tps["TP1"],
            raw_tp2=tps["TP2"],
            raw_tp3=tps["TP3"],
            raw_tp4=tps["TP4"],
            raw_tp5=tps["TP5"],
            tp_levels=tps,
            final_tp=3899.5,
            initial_price=3890.0,
        )

        with patch.object(main.executor, "get_atr", return_value=4.00):
            # Pre-TP1 tight zone + noisy dips: pre-lifts L4 to TP2, locks BE, stays ACTIVE
            for px in (3899.40, 3898.70, 3898.10, 3899.10):
                cur = main.state_mgr.get_signal(9401)
                with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
                    await main._process_pre_tp1_or_backstop_tick("9401", cur)
            pre_s = main.state_mgr.get_signal(9401)
            assert pre_s["status"] == "ACTIVE"
            assert pre_s["layers"]["L1"]["be_locked"] is True
            assert pre_s["layers"]["L4"]["tp"] == 3910.0  # L4 already pre-lifted to TP2 before TP1!

            # Break TP1 (3900.60): execute TP1 scaling handoff
            with patch.object(main.executor, "get_tick", return_value=_make_tick(3900.60, 3900.80)):
                cur_sig = dict(main.state_mgr.get_signal(9401))
                main.executor.handle_tp1_scaling_and_select_runner(cur_sig)
                main.state_mgr.update_signal(9401, cur_sig)
            mid_s = main.state_mgr.get_signal(9401)
            assert mid_s["status"] == "RUNNER_ACTIVE"
            assert mid_s["runner_layer"] == "L4"
            assert mid_s["runner_milestone_idx"] == 2

            # Approach TP2 & TP3 with healthy noise + single jitter spike -> steps through to TP4
            path_prices = [
                3909.30, 3908.70, 3908.00, 3909.00,
                3910.60,
                3919.40, 3918.70,
                3920.60,
                3930.10,
            ]
            for px in path_prices:
                cur = main.state_mgr.get_signal(9401)
                with patch.object(main.executor, "get_tick", return_value=_make_tick(px, px + 0.2)):
                    await main._process_runner_active_tick("9401", cur)

            step_s = main.state_mgr.get_signal(9401)
            assert step_s["runner_cleared_milestones"] == ["TP1", "TP2", "TP3"]

            # Broker executes TP at TP4 ceiling -> finalizes cleanly to TradeLedger
            with patch.object(main.executor, "get_position_close_info", return_value={"reason": "TP"}), \
                 patch("mt5_executor.mt5.positions_get", return_value=None):
                main.executor.dry_run = False
                cur_end = dict(main.state_mgr.get_signal(9401))
                with patch.object(main.executor, "get_tick", return_value=_make_tick(3930.10, 3930.30)):
                    await main._process_runner_active_tick("9401", cur_end)
                main.executor.dry_run = True

            final_s = main.state_mgr.get_signal(9401)
            assert final_s["status"] == "CLOSED"
            assert final_s["exit_reason_code"] == "TP_CEILING"
            assert len(main.trade_ledger.read_trades()) == 1
    finally:
        main.notify_saved_messages = orig_notify
