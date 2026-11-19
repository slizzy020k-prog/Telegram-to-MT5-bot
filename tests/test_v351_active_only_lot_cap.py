"""v3.5.1 regression tests: High-Risk Lot Cap applies to active open buys/sells only, not pending limits/stops."""

from types import SimpleNamespace

import MetaTrader5 as mt5
import pytest

import config
import main
import mt5_executor as ex_mod
from mt5_executor import MT5Executor, calculate_risk_guide_thresholds
from parser import ZoneInfo
from state_manager import StateManager
from tests.test_copier_scenarios import MockTelethonEvent
from tests.test_v3_4layer_and_runner import _make_sym_info, _make_tick
from trade_ledger import TradeLedger


def _make_account_info(balance: float = 57.73, credit: float = 0.0, equity: float = None, currency: str = "GBP"):
    eq = balance if equity is None else equity
    return SimpleNamespace(
        login=36393319,
        server="PUPrime-Live 6",
        balance=balance,
        credit=credit,
        equity=eq,
        profit=round(eq - balance, 2),
        margin=0.0,
        margin_free=balance,
        currency=currency,
    )


def _patch_live_mt5(
    monkeypatch,
    tick_ref: dict,
    account: SimpleNamespace,
    positions_list: list,
    orders_list: list,
    sym_info: SimpleNamespace = None,
    start_ticket: int = 153880000,
):
    """Patch ex_mod.mt5 functions onto MT5Executor(dry_run=False) for deterministic live-path testing."""
    sym_obj = sym_info or _make_sym_info(stops_level=20)
    counter = {"n": start_ticket}
    sent_requests = []

    def _order_send(req):
        sent_requests.append(dict(req))
        counter["n"] += 1
        return SimpleNamespace(retcode=getattr(mt5, "TRADE_RETCODE_DONE", 10009), order=counter["n"], comment="Mock OK")

    def _positions_get(*args, **kwargs):
        t = kwargs.get("ticket")
        if t is not None:
            return [p for p in positions_list if p.ticket == t]
        return list(positions_list)

    def _orders_get(*args, **kwargs):
        t = kwargs.get("ticket")
        if t is not None:
            return [o for o in orders_list if o.ticket == t]
        return list(orders_list)

    monkeypatch.setattr(ex_mod.mt5, "initialize", lambda: True)
    monkeypatch.setattr(ex_mod.mt5, "shutdown", lambda: True)
    monkeypatch.setattr(ex_mod.mt5, "terminal_info", lambda: SimpleNamespace(connected=True, trade_allowed=True))
    monkeypatch.setattr(ex_mod.mt5, "account_info", lambda: account)
    monkeypatch.setattr(ex_mod.mt5, "symbol_info", lambda sym: sym_obj)
    monkeypatch.setattr(ex_mod.mt5, "symbol_select", lambda sym, flag=True: True)
    monkeypatch.setattr(ex_mod.mt5, "symbol_info_tick", lambda sym: tick_ref["tick"])
    monkeypatch.setattr(ex_mod.mt5, "order_send", _order_send)
    monkeypatch.setattr(ex_mod.mt5, "positions_get", _positions_get)
    monkeypatch.setattr(ex_mod.mt5, "orders_get", _orders_get)
    monkeypatch.setattr(ex_mod.mt5, "copy_rates_from_pos", lambda *args, **kwargs: None)
    monkeypatch.setattr(ex_mod.mt5, "last_error", lambda: (1, "OK"))

    ex = MT5Executor(dry_run=False)
    ex._connected = True
    ex._symbol_cache = {"XAUUSD.s": "XAUUSD.s", "XAUUSD": "XAUUSD.s"}
    return ex, sent_requests


@pytest.mark.asyncio
async def test_v351_signal_13164_reproducer_pending_limits_allowed(tmp_path, monkeypatch):
    """
    Exact live Signal 13164 reproducer:
    57.73 GBP account holding 0.01 open SELL + 0.04 pending SELL LIMITs (Signal 9934).
    Incoming SELL 3742-3752 with bid=3739.78 places 4 pending SELL LIMIT orders uncapped.
    """
    tick_ref = {"tick": _make_tick(3739.78, 3740.30)}
    magic = config.MAGIC_NUMBER

    positions_list = [
        SimpleNamespace(ticket=153877174, magic=magic, volume=0.01, symbol="XAUUSD.s", type=1, price_open=3741.7),
    ]
    orders_list = [
        SimpleNamespace(ticket=153878899, magic=magic, volume_current=0.01, symbol="XAUUSD.s"),
        SimpleNamespace(ticket=153878900, magic=magic, volume_current=0.01, symbol="XAUUSD.s"),
        SimpleNamespace(ticket=153878903, magic=magic, volume_current=0.02, symbol="XAUUSD.s"),
    ]
    account = _make_account_info(balance=57.73, currency="GBP")
    ex, sent_requests = _patch_live_mt5(monkeypatch, tick_ref, account, positions_list, orders_list)

    risk_st = ex.get_risk_exposure_status()
    assert risk_st["open_lots"] == 0.01
    assert risk_st["pending_lots"] == 0.04
    assert risk_st["total_lots"] == 0.05
    assert risk_st["high_risk_lots"] == 0.05
    assert risk_st["available_lots"] == 0.04
    assert risk_st["risk_tier"] == "LOW_RISK_SAFE"

    monkeypatch.setattr(main, "executor", ex)
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "sig13164_state.json"))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / "sig13164_ledger.jsonl"))
    monkeypatch.setattr(main, "CHANNEL_IDS", [-1001234567890])

    alerts = []

    async def _cap(msg):
        alerts.append(msg)

    monkeypatch.setattr(main, "notify_saved_messages", _cap)

    sig_text = (
        "SELL XAUUSD\n"
        "Zone: 3742 - 3752\n"
        "SL 3765\n"
        "TP1 3739\n"
        "TP2 3732\n"
        "TP3 3729\n"
        "TP4 3722\n"
        "TP5 3710\n"
    )
    await main.handle_new_message(MockTelethonEvent(msg_id=13164, text=sig_text, chat_id=-1001234567890))

    sig = main.state_mgr.get_signal(13164, chat_id=-1001234567890)
    assert sig is not None
    assert sig["market_tickets"] == []
    assert len(sig["pending_tickets"]) == 4
    assert set(sig["layers"].keys()) == {"L1", "L2", "L3", "L4"}
    assert round(sum(v["lot"] for v in sig["layers"].values()), 2) == 0.05

    pending_action = getattr(mt5, "TRADE_ACTION_PENDING", 5)
    sell_limit_type = getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3)
    assert len(sent_requests) == 4
    for req in sent_requests:
        assert req["action"] == pending_action
        assert req["type"] == sell_limit_type
    assert not any("High Risk Lot Cap Reached" in a for a in alerts)


def test_v351_pending_allowed_while_market_blocked_at_full_cap(monkeypatch):
    """At 0.05/0.05 active open lots on £50, pending limits/stops succeed while market & stops-fallback block."""
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 50.0
    ex._dry_run_ticket_lots = {101: 0.05}

    st = ex.get_risk_exposure_status()
    assert st["open_lots"] == 0.05
    assert st["available_lots"] == 0.00
    assert st["risk_tier"] == "HIGH_CAP_REACHED"

    # Pending limit & stop placements succeed uncapped
    t_lim = ex.place_limit_order("XAUUSD.s", "BUY", 3880.0, lot=0.02, sl=3870.0, tp=3890.0)
    t_stp = ex.place_stop_order("XAUUSD.s", "BUY", 3910.0, lot=0.02, sl=3900.0, tp=3920.0)
    assert t_lim is not None
    assert t_stp is not None

    mk_z, pd_z, layers_z = ex.execute_4layer_zone(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=ZoneInfo(3885.0, 3881.0, 3877.0, "BUY"),
        sl=3865.0,
        final_tp=3894.5,
        raw_tp1=3895.0,
    )
    assert mk_z == []
    assert len(pd_z) == 4
    assert round(sum(v["lot"] for v in layers_z.values()), 2) == 0.05

    lots_now = ex.get_total_active_lots()
    assert lots_now["open_lots"] == 0.05
    assert lots_now["pending_lots"] == 0.09
    assert ex.get_open_market_tickets() == [101]

    # Cancelling one pending order removes from _dry_run_pending_ticket_lots only
    assert ex.cancel_pending_order(t_lim) is True
    assert ex.get_total_active_lots()["pending_lots"] == 0.07
    assert ex.get_total_active_lots()["open_lots"] == 0.05

    # Active market buys/sells are strictly blocked
    assert ex.execute_market_order("XAUUSD.s", "BUY", lot=0.01, sl=3870.0, tp=3900.0) is None
    assert "High Risk Lot Cap Reached (0.05/0.05 active lots" in (ex.last_error or "")

    mk_m, pd_m, layers_m = ex.execute_4layer_market("XAUUSD.s", "BUY", sl=3870.0, final_tp=3899.5, raw_tp1=3900.0)
    assert mk_m == [] and pd_m == [] and layers_m == {}
    assert "High Risk Lot Cap Reached (0.05/0.05 active lots" in (ex.last_error or "")

    # Live _place_pending_with_stops_fallback: if pending fails (invalid stops) and price crosses into market fallback,
    # execute_market_order still enforces the 0.05 active open lot cap!
    tick_ref = {"tick": _make_tick(3894.80, 3895.00)}
    magic = config.MAGIC_NUMBER
    positions_list = [
        SimpleNamespace(ticket=801, magic=magic, volume=0.05, symbol="XAUUSD.s", type=0, price_open=3890.0),
    ]
    account = _make_account_info(balance=50.0, currency="GBP")
    live_ex, sent_requests = _patch_live_mt5(
        monkeypatch, tick_ref, account, positions_list, [], sym_info=_make_sym_info(stops_level=50), start_ticket=5000
    )

    fail_pending_result = SimpleNamespace(retcode=10016, order=0, comment="Invalid stops")
    monkeypatch.setattr(
        ex_mod.mt5, "order_send", lambda req: (sent_requests.append(dict(req)), fail_pending_result)[1]
    )

    fb_ticket, fb_kind = live_ex._place_pending_with_stops_fallback(
        symbol="XAUUSD.s",
        direction="BUY",
        target_price=3894.5,
        lot=0.01,
        sl=3870.0,
        tp=3905.0,
        pending_comment="TG L1 (Limit)",
        market_comment="TG L1 (Market)",
        pending_kind="LIMIT",
    )
    assert fb_ticket is None and fb_kind == "FAILED"
    assert "High Risk Lot Cap Reached (0.05/0.05 active lots" in (live_ex.last_error or "")
    deal_action = getattr(mt5, "TRADE_ACTION_DEAL", 1)
    assert not any(r.get("action") == deal_action for r in sent_requests)


def test_v351_mixed_zone_skips_market_layers_retains_pending_at_cap():
    """Mixed zones skip/cap only the layers crossing at market and preserve full lots on pending layers."""
    # 1. Full cap (0.05/0.05 on £50): L1=3895 crosses market (ask=3895.00) -> skipped; L2..L4 limits kept
    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 50.0
    ex._dry_run_ticket_lots = {101: 0.05}

    mk, pd, layers = ex.execute_4layer_zone(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=ZoneInfo(3895.0, 3891.0, 3887.0, "BUY"),
        sl=3875.0,
        final_tp=3904.5,
        raw_tp1=3905.0,
    )
    assert mk == []
    assert len(pd) == 3
    assert set(layers.keys()) == {"L2", "L3", "L4"}
    assert round(sum(v["lot"] for v in layers.values()), 2) == 0.04
    assert "High Risk Lot Cap Reached" in (ex.last_allocation_warning or "")
    assert ex.get_total_active_lots() == {"open_lots": 0.05, "pending_lots": 0.04, "total_lots": 0.09}

    # 2. Partial market budget (0.04/0.05 -> 0.01 avail): L1=3898 & L2=3895 are market, L3=3892 & L4=3889 are limits.
    # Only 1 market layer (L1=0.01) fits in avail=0.01; L2 is skipped (0.0); L3 & L4 limits keep 0.01 & 0.02!
    ex2 = MT5Executor(dry_run=True)
    ex2.connect()
    ex2._dry_run_capital = 50.0
    ex2._dry_run_ticket_lots = {201: 0.04}

    mk2, pd2, layers2 = ex2.execute_4layer_zone(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=ZoneInfo(3898.0, 3893.5, 3889.0, "BUY"),
        sl=3875.0,
        final_tp=3904.5,
        raw_tp1=3905.0,
    )
    assert len(mk2) == 1
    assert len(pd2) == 2
    assert set(layers2.keys()) == {"L1", "L3", "L4"}
    assert layers2["L1"]["lot"] == 0.01
    assert layers2["L3"]["lot"] == 0.01
    assert layers2["L4"]["lot"] == 0.02
    assert ex2.get_total_active_lots() == {"open_lots": 0.05, "pending_lots": 0.03, "total_lots": 0.08}

    # 3. Single-price: below-market LIMIT succeeds at 0.05/0.05; at-market AUTO blocked
    ex._dry_run_pending_ticket_lots.clear()
    mk_s1, pd_s1, layers_s1 = ex.execute_4layer_single_price(
        symbol="XAUUSD.s", direction="BUY", price=3880.0, sl=3865.0, final_tp=3894.5, raw_tp1=3895.0, order_intent="AUTO"
    )
    assert mk_s1 == [] and len(pd_s1) == 4 and set(layers_s1.keys()) == {"L1", "L2", "L3", "L4"}

    mk_s2, pd_s2, layers_s2 = ex.execute_4layer_single_price(
        symbol="XAUUSD.s", direction="BUY", price=3895.0, sl=3875.0, final_tp=3904.5, raw_tp1=3905.0, order_intent="AUTO"
    )
    assert mk_s2 == [] and pd_s2 == [] and layers_s2 == {}


@pytest.mark.asyncio
async def test_v351_active_market_cap_end_to_end_50_and_80(tmp_path, monkeypatch):
    """End-to-end handle_new_message enforces 0.05 (£50) & 0.08 (£80) on active buys/sells while allowing pending zones."""
    monkeypatch.setattr(main, "STAGED_MARKET_ENTRY", False)
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(main, "CHANNEL_IDS", [-1001234567890])

    # Part A: £50 account
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "e2e_50_state.json"))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / "e2e_50_ledger.jsonl"))
    ex50 = MT5Executor(dry_run=True)
    ex50.connect()
    ex50._dry_run_capital = 50.0
    monkeypatch.setattr(main, "executor", ex50)

    alerts = []

    async def _cap(txt):
        alerts.append(txt)

    monkeypatch.setattr(main, "notify_saved_messages", _cap)

    await main.handle_new_message(MockTelethonEvent(msg_id=2001, text="Buy now\nSL 3875\nTP 3905", chat_id=-1001234567890))
    assert ex50.get_total_active_lots() == {"open_lots": 0.05, "pending_lots": 0.0, "total_lots": 0.05}
    assert any("MAX HIGH RISK CAP REACHED" in a for a in alerts)

    await main.handle_new_message(MockTelethonEvent(msg_id=2002, text="Buy now\nSL 3875\nTP 3905", chat_id=-1001234567890))
    assert main.state_mgr.get_signal(2002, chat_id=-1001234567890) is None
    assert any("High Risk Lot Cap Reached" in a for a in alerts)

    # Pure pending zone still executes cleanly at 0.05/0.05 active cap
    await main.handle_new_message(MockTelethonEvent(msg_id=2003, text="Buy 3885-3877\nTP 3895\nSL 3865", chat_id=-1001234567890))
    sig_pend = main.state_mgr.get_signal(2003, chat_id=-1001234567890)
    assert sig_pend is not None and len(sig_pend["pending_tickets"]) == 4
    assert ex50.get_total_active_lots()["open_lots"] == 0.05
    assert ex50.get_total_active_lots()["pending_lots"] == 0.05

    # Part B: £80 account (high_risk_lots = 0.08): 0.05 + 0.03 = 0.08 open, 3rd market blocked, pending still passes
    alerts.clear()
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "e2e_80_state.json"))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / "e2e_80_ledger.jsonl"))
    ex80 = MT5Executor(dry_run=True)
    ex80.connect()
    ex80._dry_run_capital = 80.0
    monkeypatch.setattr(main, "executor", ex80)

    await main.handle_new_message(MockTelethonEvent(msg_id=2011, text="Buy now\nSL 3875\nTP 3905", chat_id=-1001234567890))
    assert ex80.get_total_active_lots()["open_lots"] == 0.05

    await main.handle_new_message(MockTelethonEvent(msg_id=2012, text="Buy now\nSL 3875\nTP 3905", chat_id=-1001234567890))
    assert ex80.get_total_active_lots()["open_lots"] == 0.08
    sig80 = main.state_mgr.get_signal(2012, chat_id=-1001234567890)
    assert sig80 is not None and len(sig80["market_tickets"]) == 3
    assert round(sum(l["lot"] for l in sig80["layers"].values()), 2) == 0.03

    await main.handle_new_message(MockTelethonEvent(msg_id=2013, text="Buy now\nSL 3875\nTP 3905", chat_id=-1001234567890))
    assert main.state_mgr.get_signal(2013, chat_id=-1001234567890) is None

    await main.handle_new_message(MockTelethonEvent(msg_id=2014, text="Buy 3885-3877\nTP 3895\nSL 3865", chat_id=-1001234567890))
    sig80_pend = main.state_mgr.get_signal(2014, chat_id=-1001234567890)
    assert sig80_pend is not None and len(sig80_pend["pending_tickets"]) == 4


def test_v351_live_mode_positions_get_and_orders_get_separation(monkeypatch):
    """Live MT5 positions_get vs orders_get: ignores unrelated magic and counts only active positions toward cap."""
    tick_ref = {"tick": _make_tick(3739.78, 3740.30)}
    magic = config.MAGIC_NUMBER
    account = _make_account_info(balance=57.73, currency="GBP")

    positions_list = [
        SimpleNamespace(ticket=11, magic=magic, volume=0.05, symbol="XAUUSD.s", type=1, price_open=3741.7),
        SimpleNamespace(ticket=99, magic=999999, volume=0.10, symbol="XAUUSD.s", type=0, price_open=3735.0),
    ]
    orders_list = [
        SimpleNamespace(ticket=21, magic=magic, volume_current=0.04, symbol="XAUUSD.s"),
        SimpleNamespace(ticket=98, magic=888888, volume_current=0.20, symbol="XAUUSD.s"),
    ]
    ex, _ = _patch_live_mt5(monkeypatch, tick_ref, account, positions_list, orders_list, start_ticket=91000)

    lots = ex.get_total_active_lots()
    assert lots == {"open_lots": 0.05, "pending_lots": 0.04, "total_lots": 0.09}
    assert ex.get_open_market_tickets() == [11]

    st = ex.get_risk_exposure_status()
    assert st["available_lots"] == 0.00
    assert st["risk_tier"] == "HIGH_CAP_REACHED"

    # Market order blocked when open_lots == 0.05
    assert ex.execute_market_order("XAUUSD.s", "SELL", lot=0.01, sl=3765.0, tp=3730.0) is None
    assert "High Risk Lot Cap Reached (0.05/0.05 active lots" in (ex.last_error or "")

    # Pending limit order still succeeds even at 0.05 open + 0.04 pending
    t_pend = ex.place_limit_order("XAUUSD.s", "SELL", 3745.0, lot=0.02, sl=3765.0, tp=3730.0)
    assert t_pend == 91001


@pytest.mark.asyncio
async def test_v351_late_edit_zone_expansion_keeps_pending_limits_at_cap(tmp_path, monkeypatch):
    """Editing an existing staged signal with a zone while active lots are at 0.05/0.05 still places pending L2..L4."""
    monkeypatch.setattr(main, "STAGED_MARKET_ENTRY", True)
    monkeypatch.setattr(main, "SELL_NOW_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(main, "CHANNEL_IDS", [-1001234567890])
    monkeypatch.setattr(main, "state_mgr", StateManager(tmp_path / "edit_cap_state.json"))
    monkeypatch.setattr(main, "trade_ledger", TradeLedger(tmp_path / "edit_cap_ledger.jsonl"))

    ex = MT5Executor(dry_run=True)
    ex.connect()
    ex._dry_run_capital = 50.0
    ex._dry_run_ticket_lots = {901: 0.04}  # 0.04 already open -> 0.01 room left for L1
    monkeypatch.setattr(main, "executor", ex)

    alerts = []

    async def _cap(txt):
        alerts.append(txt)

    monkeypatch.setattr(main, "notify_saved_messages", _cap)

    # Initial staged Buy now -> opens L1 (0.01 market), bringing active open exposure to 0.05/0.05
    await main.handle_new_message(MockTelethonEvent(msg_id=3001, text="Buy now", chat_id=-1001234567890))
    sig0 = main.state_mgr.get_signal(3001, chat_id=-1001234567890)
    assert sig0 is not None and len(sig0["market_tickets"]) == 1
    assert ex.get_total_active_lots()["open_lots"] == 0.05

    # Now edit msg 3001 to add zone 3895-3887 while active cap is 0.05/0.05 -> L2..L4 limits must still place
    edit_evt = MockTelethonEvent(msg_id=3001, text="Buy 3895-3887\nTP 3905\nSL 3875", chat_id=-1001234567890)
    await main.handle_message_edited(edit_evt)

    sig1 = main.state_mgr.get_signal(3001, chat_id=-1001234567890)
    assert sig1 is not None
    assert len(sig1["market_tickets"]) == 1
    assert len(sig1["pending_tickets"]) == 3
    assert set(sig1["layers"].keys()) == {"L1", "L2", "L3", "L4"}
    assert ex.get_total_active_lots() == {"open_lots": 0.05, "pending_lots": 0.04, "total_lots": 0.09}
