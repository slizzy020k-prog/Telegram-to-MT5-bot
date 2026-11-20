"""
Production QA Suite for Late Message Edits (3+ Minutes / Arbitrary Elapsed Time):
1. Real-world Signal #9710 regression: Unpriced 'Buy xauusd now' -> 45s synthetic fallback TP1
   (plus shortfall arming) -> edited 3.5+ minutes later with Zone + TPs + SL + 'High risk .'
   ('Manage your risk', 'VIP', etc.):
   - Gate 2 allows real trade signals/edits with risk disclaimers while still blocking pure risk tables.
   - Additional layers (L2, L3, L4) are placed across the edited zone up to 4 layers ([0.01, 0.01, 0.01, 0.02]).
   - Synthetic fallback TP1..TP5 is discarded and replaced by the real edited TPs, fallback_tp_applied=False,
     shortfall_armed reset, and SL + Broker TP1 updated across all open & pending MT5 orders.
2. Single-price signal ('SELL XAUUSD 3860') edited 4+ minutes later into a Zone ('SELL XAUUSD 3860-3864'):
   - Unfilled pending orders re-tier cleanly across L1..L4 zone levels with updated SL & TP1.
   - Race safety: if a pending order filled at the exact moment of cancel, it promotes to market_tickets
     and updates SL/TP without duplicating orders past 4.
3. Untracked / placeholder message edited > 3 minutes later (e.g. 5m, 25m, 90m):
   - Accepted via MAX_NEW_EDIT_AGE_SECONDS=3600 and fresh Telethon msg.edit_date check.
   - Emoji reactions / view updates on old unedited Telethon messages (edit_date=None) and 2h stale
     untracked messages remain blocked.
   - Rapid <5s text edit on a filtered placeholder is NOT swallowed by deduplication.
4. Independent SL vs TP fallback in MT5Executor.modify_position_sltp & modify_pending_order_sltp:
   - When combined (sl, tp) modification fails with MT5 retcode 10016 ('Invalid stops') on one leg,
     the valid leg (TP or SL) still applies independently on MT5.
5. Multi-channel supergroup ID variant normalization (-4369388188 <-> -1004369388188) + Offline
   Reconnect Catch-Up of missed edits on active signals whose msg_id <= last_seen_id.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
import mt5_executor as ex_mod
from mt5_executor import MT5Executor
from parser import parse_message
from state_manager import StateManager
from trade_ledger import TradeLedger


class FakeMsg:
    def __init__(
        self,
        msg_id: int,
        text: str,
        reply_to_msg_id=None,
        date: datetime | None = None,
        edit_date: datetime | None = None,
    ):
        self.id = msg_id
        self.message = text
        self.raw_text = text
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to = None
        self.forward = None
        self.fwd_from = None
        self.out = False
        self.date = date or datetime.now(timezone.utc)
        self.edit_date = edit_date


class FakeEvent:
    def __init__(
        self,
        msg_id: int,
        text: str,
        chat_id=-1004369388188,
        reply_to=None,
        date: datetime | None = None,
        edit_date: datetime | None = None,
    ):
        self.message = FakeMsg(msg_id, text, reply_to_msg_id=reply_to, date=date, edit_date=edit_date)
        self.chat_id = chat_id
        self.is_private = chat_id == "me"
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, txt: str):
        self.replies.append(txt)


def _reset_runtime(tmp_path, tag: str = "late_edit"):
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    main._recent_message_texts.clear()
    main._deleted_messages.clear()
    main._signal_locks.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main.CLOSE_ON_SIGNAL_DELETED = True
    main.CHANNEL_IDS = [-4369388188, -1004369388188]
    import config
    config.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    ex_mod.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    main.SINGLE_PRICE_CONSOLIDATE_ORDERS = False
    main.state_mgr = StateManager(str(tmp_path / f"{tag}_state.json"))
    main.trade_ledger = TradeLedger(str(tmp_path / f"{tag}_ledger.jsonl"))


# ---------------------------------------------------------------------------
# 1. GATE 2 RISK DISCLAIMER VS PURE RISK TABLE FILTERING
# ---------------------------------------------------------------------------

def test_gate2_allows_signals_and_edits_with_risk_disclaimers_but_blocks_noise():
    # Exact Signal #9710 edits from live Telegram channel -1004369388188
    edit_now_with_risk = (
        "Buy xauusd now\n\n"
        "Tp 3864\nTp 3872\nTp 3885\nTp 3900\nTp 3950\nTp 4000\n\n"
        "Sl 3830\n\nHigh risk ."
    )
    p1 = parse_message(edit_now_with_risk, message_id=9710, is_edit=True, live_price=3858.56)
    assert p1 is not None
    assert p1.event_type == "NEW_SIGNAL"
    assert p1.direction == "BUY"
    assert p1.raw_tp1 == 3864.0
    assert p1.final_tp == 3863.50
    assert p1.sl == 3830.0 and p1.sl_valid is True

    edit_zone_with_risk = (
        "Buy xauusd 3858-3850\n\n"
        "Tp 3864\nTp 3872\nTp 3885\nTp 3900\nTp 3950\nTp 4000\n\n"
        "Sl 3830\n\nHigh risk . Manage your risk! Protect your capital (VIP)"
    )
    p2 = parse_message(edit_zone_with_risk, message_id=9710, is_edit=True, live_price=3858.56)
    assert p2 is not None
    assert p2.is_zone is True
    assert p2.zone.get_tiers("BUY") == {"L1": 3858.0, "L2": 3855.33, "L3": 3852.67, "L4": 3850.0}
    assert p2.sl == 3830.0 and p2.sl_valid is True
    assert p2.tp_levels == {
        "TP1": 3864.0,
        "TP2": 3872.0,
        "TP3": 3885.0,
        "TP4": 3900.0,
        "TP5": 3950.0,
    }

    # New signal with 'Low risk' / 'Mid risk' / 'VIP' disclaimer must also parse cleanly
    p3 = parse_message("VIP Sell XAUUSD 3900-3906\nTP 3890\nSL 3915\nLow risk setup")
    assert p3 is not None and p3.direction == "SELL" and p3.is_zone is True and p3.sl == 3915.0

    # Active-trade target adjustment reply with 'Manage your risk' must also pass Gate 2
    p4 = parse_message("SL 3885\nTP2 3918\nManage your risk", is_reply=True, reply_to_msg_id=9710)
    assert p4 is not None and p4.management_action == "ACTION_ADJUST_TARGETS"

    # Pure standalone risk/promo/chatter posts MUST still be rejected!
    assert parse_message("HIGH RISK SMALL LOTS") is None
    assert parse_message("Manage your risk lads") is None
    assert parse_message("£50 account High Risk : 0.05 Mid Risk : 0.02 Low Risk : 0.01") is None
    assert parse_message("Manage your risk guys! Account £50 : 0.01 lot") is None
    assert parse_message("How to place a buy limit") is None
    assert parse_message("Join VIP today!") is None


# ---------------------------------------------------------------------------
# 2. LATE EDIT AFTER 3.5+ MINUTES: ADDING ZONE LAYERS + REPLACING FALLBACK TP1 & SL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_late_edit_after_3_minutes_adds_layers_and_replaces_fallback_tp_and_sl(tmp_path):
    _reset_runtime(tmp_path, "scen_9710")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Simulate an active trade opened 4 minutes ago with 1 initial layer ("TOP" = 0.01 lot),
        # where 45s synthetic fallback TP1 (3863.25) already attached AND shortfall_armed fired near 3862.5!
        four_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()
        main.state_mgr.create_signal(
            message_id=9710,
            chat_id=-1004369388188,
            symbol="XAUUSD.s",
            direction="BUY",
            layers={
                "TOP": {
                    "lot": 0.01,
                    "target_price": None,
                    "tp": 3862.75,
                    "tp_stage": "TP1",
                    "ticket": 5001,
                    "order_type": "MARKET",
                    "status": "OPEN",
                }
            },
            market_tickets=[5001],
            pending_tickets=[],
            zone=None,
            zone_completed=False,
            is_zone=False,
            max_orders=1,
            sl=None,
            raw_tp1=3863.25,
            raw_tp2=3873.25,
            raw_tp3=3883.25,
            raw_tp4=3893.25,
            raw_tp5=3903.25,
            tp_levels={
                "TP1": 3863.25,
                "TP2": 3873.25,
                "TP3": 3883.25,
                "TP4": 3893.25,
                "TP5": 3903.25,
            },
            final_tp=3862.75,
            fallback_tp_applied=True,
            shortfall_armed=True,
            shortfall_reversal_ticks=1,
            peak_price=3862.60,
            initial_price=3858.56,
            recorded_entry_price=3858.56,
            raw_text="Buy xauusd now",
            timestamp=four_min_ago,
        )

        # Live price at 4 minutes is 3856.80/3857.00 (inside top of 3858-3850 zone: L1 already open,
        # L2=3855.33, L3=3852.67, L4=3850.00 should place as 3 pending BUY LIMIT orders!)
        tick_edit = SimpleNamespace(bid=3856.80, ask=3857.00, time=1700000300)
        mod_pos_calls = []
        orig_mod_pos = main.executor.modify_position_sltp

        def _spy_mod_pos(ticket, sl=None, tp=None):
            mod_pos_calls.append((ticket, sl, tp))
            return orig_mod_pos(ticket=ticket, sl=sl, tp=tp)

        late_edit_text = (
            "Buy xauusd 3858-3850\n\n"
            "Tp 3864\nTp 3872\nTp 3885\nTp 3900\nTp 3950\nTp 4000\n\n"
            "Sl 3830\n\nHigh risk ."
        )
        # Deliver edit using the bare -4369388188 variant to verify cross-variant lookup too!
        evt_edit = FakeEvent(
            9710,
            late_edit_text,
            chat_id=-4369388188,
            date=datetime.now(timezone.utc) - timedelta(minutes=4),
            edit_date=datetime.now(timezone.utc),
        )

        with patch.object(main.executor, "get_tick", return_value=tick_edit), patch.object(
            main.executor, "modify_position_sltp", side_effect=_spy_mod_pos
        ):
            await main.handle_message_edited(evt_edit)

        sig = main.state_mgr.get_signal(9710, chat_id=-1004369388188)
        assert sig is not None
        # 1) "TOP" promoted to "L1", and L2, L3, L4 added across 3858-3850 -> 4 total orders ([0.01, 0.01, 0.01, 0.02])!
        assert set(sig["layers"].keys()) == {"L1", "L2", "L3", "L4"}
        assert [sig["layers"][f"L{i}"]["lot"] for i in range(1, 5)] == [0.01, 0.01, 0.01, 0.02]
        assert len(sig["market_tickets"]) == 1
        assert len(sig["pending_tickets"]) == 3
        assert sig["layers"]["L2"]["target_price"] == pytest.approx(3855.33)
        assert sig["layers"]["L3"]["target_price"] == pytest.approx(3852.67)
        assert sig["layers"]["L4"]["target_price"] == pytest.approx(3850.00)

        # 2) Synthetic fallback TP1 (3863.25) wiped and replaced by real TPs (3864..3950) + SL=3830.0!
        assert sig["fallback_tp_applied"] is False
        assert sig["sl"] == pytest.approx(3830.0)
        assert sig["raw_tp1"] == pytest.approx(3864.0)
        assert sig["final_tp"] == pytest.approx(3863.50)
        assert sig["tp_levels"] == {
            "TP1": 3864.0,
            "TP2": 3872.0,
            "TP3": 3885.0,
            "TP4": 3900.0,
            "TP5": 3950.0,
        }
        assert all(sig["layers"][f"L{i}"]["tp"] == pytest.approx(3863.50) for i in range(1, 5))
        assert (5001, 3830.0, 3863.50) in mod_pos_calls

        # 3) Pure Dynamic ATR Engine: zero virtual closes on ticks!
        with patch.object(main.executor, "get_tick", return_value=tick_edit):
            await main._process_pre_tp1_or_backstop_tick("9710", main.state_mgr.get_signal(9710, chat_id=-1004369388188))
        sig_after_tick = main.state_mgr.get_signal(9710, chat_id=-1004369388188)
        assert sig_after_tick["status"] == "ACTIVE"
        assert len(sig_after_tick["market_tickets"]) == 1
        assert len(sig_after_tick["pending_tickets"]) == 3


# ---------------------------------------------------------------------------
# 3. SINGLE-PRICE SIGNAL EDITED 4+ MINUTES LATER INTO A 4-LAYER ZONE (PENDING RE-TIERING + FILL RACE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_price_pending_retiered_into_4layer_zone_after_4_minutes_and_fill_race(tmp_path):
    _reset_runtime(tmp_path, "retier")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Step 1: Provider posts priced single-price signal "SELL XAUUSD 3860\nTP 3855\nSL 3870" while live bid is 3858.0 -> 4 pending SELL LIMITs at 3860.0
        t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
        tick_init = SimpleNamespace(bid=3858.00, ask=3858.20, time=1700000000)
        with patch.object(main.executor, "get_tick", return_value=tick_init):
            await main.handle_new_message(
                FakeEvent(9742, "SELL XAUUSD 3860\nTP 3855\nSL 3870", chat_id=-1004369388188, date=t0)
            )

        sig0 = main.state_mgr.get_signal(9742, chat_id=-1004369388188)
        assert sig0 is not None
        assert len(sig0["market_tickets"]) == 0
        assert len(sig0["pending_tickets"]) == 4
        orig_l1_ticket = sig0["layers"]["L1"]["ticket"]
        orig_l2_ticket = sig0["layers"]["L2"]["ticket"]
        assert all(sig0["layers"][f"L{i}"]["target_price"] == 3860.0 for i in range(1, 5))

        # Simulate BOTH fill-race paths simultaneously:
        # - L1 (unchanged 3860.0 target): already filled on MT5 -> modify_pending_order_sltp returns False,
        #   so modify_position_sltp updates it and promotes L1 into market_tickets!
        # - L2 (retiering 3860.0 -> 3861.33): filled right before cancel_pending_order -> cancel returns False,
        #   so L2 promotes to market_tickets without placing a duplicate 5th order!
        # - L3 (3862.67) and L4 (3864.00): cancelled cleanly and re-placed at their new zone limit tiers!
        orig_cancel = main.executor.cancel_pending_order
        orig_mod_pend = main.executor.modify_pending_order_sltp
        orig_mod_pend_price = main.executor.modify_pending_order_price_sltp

        def _cancel_with_l2_fill_race(ticket):
            if ticket == orig_l2_ticket:
                return False
            return orig_cancel(ticket)

        def _mod_pend_with_l1_filled(ticket, sl=None, tp=None):
            if ticket == orig_l1_ticket:
                return False
            return orig_mod_pend(ticket=ticket, sl=sl, tp=tp)

        def _mod_pend_price_with_l2_fill_race(ticket, price, sl=None, tp=None):
            if ticket == orig_l2_ticket:
                return False
            return orig_mod_pend_price(ticket, price, sl=sl, tp=tp)

        tick_4m_later = SimpleNamespace(bid=3860.80, ask=3861.00, time=1700000240)
        edit_zone_text = (
            "SELL XAUUSD 3860-3864\n\n"
            "TP 3855\nTP 3850\nTP 3840\nTP 3800\n\n"
            "SL 3870"
        )
        evt_edit = FakeEvent(
            9742,
            edit_zone_text,
            chat_id=-1004369388188,
            date=t0,
            edit_date=datetime.now(timezone.utc),
        )

        with patch.object(main.executor, "get_tick", return_value=tick_4m_later), patch.object(
            main.executor, "cancel_pending_order", side_effect=_cancel_with_l2_fill_race
        ), patch.object(
            main.executor, "modify_pending_order_sltp", side_effect=_mod_pend_with_l1_filled
        ), patch.object(
            main.executor, "modify_pending_order_price_sltp", side_effect=_mod_pend_price_with_l2_fill_race
        ):
            await main.handle_message_edited(evt_edit)

        sig1 = main.state_mgr.get_signal(9742, chat_id=-1004369388188)
        # Total orders strictly 4 (no duplicates!): L1 & L2 promoted to market, L3 & L4 re-tiered pending limits!
        assert len(sig1["market_tickets"]) + len(sig1["pending_tickets"]) == 4
        assert orig_l1_ticket in sig1["market_tickets"]
        assert orig_l2_ticket in sig1["market_tickets"]
        assert sig1["layers"]["L1"]["status"] == "OPEN"
        assert sig1["layers"]["L2"]["status"] == "OPEN"
        assert sig1["layers"]["L3"]["target_price"] == pytest.approx(3862.67)
        assert sig1["layers"]["L3"]["status"] == "PENDING"
        assert sig1["layers"]["L4"]["target_price"] == pytest.approx(3864.00)
        assert sig1["layers"]["L4"]["status"] == "PENDING"
        assert sig1["sl"] == pytest.approx(3870.0)
        assert sig1["raw_tp1"] == pytest.approx(3855.0)
        assert sig1["final_tp"] == pytest.approx(3855.50)
        assert all(sig1["layers"][f"L{i}"]["tp"] == pytest.approx(3855.50) for i in range(1, 5))


# ---------------------------------------------------------------------------
# 4. UNTRACKED PLACEHOLDER EDITED 5m / 90m LATER VS EMOJI REACTIONS & <5s RAPID EDIT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_untracked_late_edits_accepted_while_reactions_blocked_and_rapid_edit_not_deduped(tmp_path):
    _reset_runtime(tmp_path, "untracked_age")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    now_utc = datetime.now(timezone.utc)
    tick = SimpleNamespace(bid=3855.00, ask=3855.20, time=1700000500)

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), patch.object(
        main.executor, "get_tick", return_value=tick
    ):
        # Case A: Placeholder "HIGH RISK SMALL LOTS" posted 6 minutes ago (rejected by Gate 2),
        # then edited right now into a 4-layer signal -> MUST execute all 4 layers!
        t_6m_ago = now_utc - timedelta(minutes=6)
        await main.handle_new_message(FakeEvent(8801, "HIGH RISK SMALL LOTS", date=t_6m_ago))
        assert main.state_mgr.get_signal(8801, chat_id=-1004369388188) is None

        await main.handle_message_edited(
            FakeEvent(
                8801,
                "Buy XAUUSD 3854-3848\nTP 3862\nSL 3840\nHigh risk",
                date=t_6m_ago,
                edit_date=now_utc,
            )
        )
        sig_a = main.state_mgr.get_signal(8801, chat_id=-1004369388188)
        assert sig_a is not None
        assert len(sig_a["market_tickets"]) + len(sig_a["pending_tickets"]) == 4
        assert sig_a["sl"] == 3840.0 and sig_a["raw_tp1"] == 3862.0

        # Case B: Placeholder posted 25 minutes ago (< 3600s), genuinely edited right now (edit_date = now_utc) -> ACCEPTED!
        t_25m_ago = now_utc - timedelta(minutes=25)
        await main.handle_message_edited(
            FakeEvent(
                8802,
                "Buy limit XAUUSD 3845\nTP 3860\nSL 3835",
                date=t_25m_ago,
                edit_date=now_utc,
            )
        )
        sig_b = main.state_mgr.get_signal(8802, chat_id=-1004369388188)
        assert sig_b is not None and len(sig_b["pending_tickets"]) == 4

        # Case B2: Untracked message from 6 hours ago (> 3600s), edited right now (real-world Msg 9857) -> IGNORED!
        t_6h_ago = now_utc - timedelta(hours=6)
        await main.handle_message_edited(
            FakeEvent(
                8805,
                "Sell xauusd 3776-3785\nTp 3771\nSl 3790",
                date=t_6h_ago,
                edit_date=now_utc,
            )
        )
        assert main.state_mgr.get_signal(8805, chat_id=-1004369388188) is None

        # Case C: Old message from 90m ago where edit_date was ALSO 85m ago (subscriber clicked emoji reaction now) -> IGNORED!
        t_90m_ago = now_utc - timedelta(minutes=90)
        await main.handle_message_edited(
            FakeEvent(
                8803,
                "Buy limit XAUUSD 3845\nTP 3860\nSL 3835",
                date=t_90m_ago,
                edit_date=now_utc - timedelta(minutes=85),
            )
        )
        assert main.state_mgr.get_signal(8803, chat_id=-1004369388188) is None

        # Case D: Rapid <5s edit after initial placeholder ("Standby gold" -> edited 1s later into real signal)
        # Must NOT be swallowed by DEDUPLICATION_WINDOW_SECONDS!
        await main.handle_new_message(FakeEvent(8804, "Standby gold incoming", date=now_utc))
        assert main.state_mgr.get_signal(8804, chat_id=-1004369388188) is None
        await main.handle_message_edited(
            FakeEvent(8804, "Sell limit XAUUSD 3865\nTP 3855\nSL 3875", date=now_utc, edit_date=now_utc)
        )
        sig_d = main.state_mgr.get_signal(8804, chat_id=-1004369388188)
        assert sig_d is not None and len(sig_d["pending_tickets"]) == 4


# ---------------------------------------------------------------------------
# 5. INDEPENDENT SL / TP MODIFICATION FALLBACK ON MT5 10016 INVALID STOPS
# ---------------------------------------------------------------------------

def test_modify_position_and_pending_independent_sl_tp_fallback_on_invalid_stops():
    ex = MT5Executor(dry_run=False)
    ex._connected = True

    fake_pos = SimpleNamespace(ticket=70001, symbol="XAUUSD.s", sl=3843.56, tp=3862.75)
    fake_ord = SimpleNamespace(
        ticket=70002, symbol="XAUUSD.s", price_open=3855.0, sl=3843.56, tp=3862.75, type_time=0, type_filling=2
    )
    fake_sym = SimpleNamespace(digits=2, point=0.01, trade_stops_level=50)

    pos_requests = []

    def _mock_order_send_pos(req):
        pos_requests.append(dict(req))
        # Combined (sl=3857.90, tp=3870.0) fails because SL is inside stops_level,
        # TP-only (sl=3843.56, tp=3870.0) succeeds, SL-only fails!
        if req["sl"] == 3857.90:
            return SimpleNamespace(retcode=10016, comment="Invalid stops")
        return SimpleNamespace(retcode=10009, comment="Request executed")

    with patch.object(ex, "_ensure_connected", return_value=True), patch.object(
        ex, "get_symbol_info", return_value=fake_sym
    ), patch.object(ex_mod.mt5, "positions_get", return_value=[fake_pos]), patch.object(
        ex_mod.mt5, "order_send", side_effect=_mock_order_send_pos
    ):
        ok_pos = ex.modify_position_sltp(70001, sl=3857.90, tp=3870.0)

    assert ok_pos is True
    assert any(r["sl"] == 3843.56 and r["tp"] == 3870.0 for r in pos_requests)

    ord_requests = []

    def _mock_order_send_ord(req):
        ord_requests.append(dict(req))
        # Combined fails on TP, SL-only (sl=3830.0, tp=3862.75) succeeds!
        if req["tp"] == 3855.10:
            return SimpleNamespace(retcode=10016, comment="Invalid stops")
        return SimpleNamespace(retcode=10009, comment="Request executed")

    with patch.object(ex, "_ensure_connected", return_value=True), patch.object(
        ex, "get_symbol_info", return_value=fake_sym
    ), patch.object(ex_mod.mt5, "orders_get", return_value=[fake_ord]), patch.object(
        ex_mod.mt5, "order_send", side_effect=_mock_order_send_ord
    ):
        ok_ord = ex.modify_pending_order_sltp(70002, sl=3830.0, tp=3855.10)

    assert ok_ord is True
    assert any(r["sl"] == 3830.0 and r["tp"] == 3862.75 for r in ord_requests)


# ---------------------------------------------------------------------------
# 6. OFFLINE RECONNECT CATCH-UP CATCHES MISSED EDITS ON OLDER ACTIVE SIGNALS (msg_id <= watermark)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_catchup_applies_missed_edit_on_older_active_signal_below_watermark(tmp_path):
    _reset_runtime(tmp_path, "catchup_edit")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    # Active signal 950 was created BEFORE watermark advanced to 960
    main.state_mgr.create_signal(
        message_id=950,
        chat_id=-1004369388188,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={"L1": {"lot": 0.01, "target_price": 3858.0, "tp": None, "tp_stage": "TP1", "ticket": 8001, "order_type": "MARKET", "status": "OPEN"}},
        market_tickets=[8001],
        pending_tickets=[],
        zone_completed=False,
        is_zone=False,
        sl=None,
        raw_tp1=None,
        final_tp=None,
        raw_text="Buy xauusd now",
    )
    main.state_mgr.set_catchup_watermark(-1004369388188, 960)

    edited_msg_950 = SimpleNamespace(
        id=950,
        message="Buy xauusd 3858-3850\nTP 3866\nTP 3875\nSL 3840\nHigh risk",
        date=datetime.now(timezone.utc) - timedelta(minutes=10),
        edit_date=datetime.now(timezone.utc) - timedelta(seconds=15),
        reply_to_msg_id=None,
        reply_to=None,
        forward=None,
    )

    async def _fake_get_messages(chat_id, limit=None, min_id=0, ids=None):
        if ids == [950]:
            return [edited_msg_950]
        return []

    tick = SimpleNamespace(bid=3857.00, ask=3857.20, time=1700000900)
    fake_client = SimpleNamespace(get_messages=_fake_get_messages)

    with patch.object(main, "telethon_client", fake_client), patch.object(
        main, "notify_saved_messages", new_callable=AsyncMock
    ), patch.object(main.executor, "get_tick", return_value=tick):
        summary = await main.run_reconnect_catchup(reason="reconnect")

    assert summary["edits"] == 1
    assert summary["channels"] == 1  # Deduplicated [-4369388188, -1004369388188] -> 1 channel fetch!
    sig = main.state_mgr.get_signal(950, chat_id=-1004369388188)
    assert sig["sl"] == 3840.0
    assert sig["raw_tp1"] == 3866.0
    assert len(sig["market_tickets"]) + len(sig["pending_tickets"]) == 4
