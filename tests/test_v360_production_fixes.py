"""
Comprehensive v3.6.0 Production Fixes & Calibration Regression Suite:
1. Fix A — 50% Daily Loss Limit Auto-Reset & Unpause with Unfilled Pending Limits (Msg 13187):
   - When 0 open market positions remain after a realized daily loss (-28.98 GBP / 51.8%),
     `check_and_enforce_daily_drawdown()` and `check_preflight` automatically update the
     balance baseline (`26.93 GBP`) and unpause new signals while keeping Msg 13187's 4
     unfilled pending BUY LIMIT orders (`#153969343, #153969345, #153969346, #153969347`)
     completely untouched.
2. Fix B1 & B2 — Single-Price Limit Consolidation (`SINGLE_PRICE_CONSOLIDATE_ORDERS=True`)
   & Duplicate-Price Pending Limit Guard (`has_duplicate_pending_order`):
   - Single-price limit/stop entries (e.g. Msg 9957 `Sell limit xauusd 3755`) place 1
     consolidated order (`L1`) at `3755.0` instead of 4 stacked limits at the exact same price.
   - If the provider later edits the single-price message into a 4-layer zone (e.g. Msg 13167
     `BUY XAUUSD 3750` -> `BUY XAUUSD 3750-3740`), `zone_completed=False` allows L2..L4
     expansion across the zone tiers.
   - Duplicate pending limit/stop orders at the same price within tolerance (`$0.50` / `$0.25`)
     are blocked across identical or repeated signals.
3. Fix B3 — `ACTION_CANCEL_PENDING` Parser & Handler (`DELETE SELL LIMIT` Msg 9960 /
   `Delete buy limit for now everyone` Msg 9910):
   - Cancels only pending limit/stop orders matching `cancel_direction` (`SELL` or `BUY`),
     never closing open market positions.
   - Preserves bare `"delete limit"` -> `ACTION_FULL_CLOSE` backwards compatibility.
4. Fix B4 — Gate 5 Wrong-Side / Out-of-Range TP Edit Sanity (`Msg 9919` `SELL 3728-3732 TP 4320`):
   - Strips invalid explicit TPs on edits (`tp_valid=False`, `invalid_explicit_tps=[4320.0]`),
     preventing 5x MT5 `retcode=10016 (Invalid stops)` errors while still applying valid SL updates.
5. Fix C — Ledger `deal_ticket` PnL Accounting (`Msg 9934` `+£1.16` vs `£0.00` bug) &
   `TIER_0T` Peak MFE Pullback Persistence (`Msg 13167`).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import main
import mt5_executor as ex_mod
from mt5_executor import MT5Executor
from parser import parse_message
from state_manager import StateManager
from trade_ledger import TradeLedger


class FakeMsg:
    def __init__(self, msg_id: int, text: str, reply_to_msg_id=None):
        self.id = msg_id
        self.message = text
        self.raw_text = text
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to = None
        self.forward = None
        self.fwd_from = None
        self.out = False
        self.date = None
        self.edit_date = None


class FakeEvent:
    def __init__(self, msg_id: int, text: str, chat_id=-1004369388188, reply_to=None):
        self.message = FakeMsg(msg_id, text, reply_to_msg_id=reply_to)
        self.chat_id = chat_id
        self.is_private = chat_id == "me"
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, txt: str):
        self.replies.append(txt)


def _setup_test_env(tmp_path, tag: str) -> MT5Executor:
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    if hasattr(main, "_recent_message_texts"):
        main._recent_message_texts.clear()
    if hasattr(main, "_recent_management_commands"):
        main._recent_management_commands.clear()
    main._deleted_messages.clear()
    main._signal_locks.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False
    main._daily_win_terminate_requested = False
    main._daily_loss_terminate_requested = False
    main._daily_loss_locked = False
    main._daily_50pct_alert_date = None
    main.CHANNEL_IDS = [-1003712081715, -1004369388188]
    main.SINGLE_PRICE_CONSOLIDATE_ORDERS = True
    config.SINGLE_PRICE_CONSOLIDATE_ORDERS = True
    ex_mod.SINGLE_PRICE_CONSOLIDATE_ORDERS = True
    main.state_mgr = StateManager(tmp_path / f"{tag}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{tag}_ledger.jsonl")
    ex = MT5Executor(dry_run=True)
    ex.connect()
    main.executor = ex
    return ex


@pytest.mark.asyncio
async def test_fix_a_daily_loss_unpause_with_unfilled_pending_limits_13187(tmp_path):
    """
    Reproduce live state after Msg 9953 SL (-28.98 GBP / 51.8% from 55.91 -> 26.93 GBP):
    - 0 open market positions on MT5
    - Msg 13187 has 4 unfilled pending BUY LIMIT orders (#153969343, #153969345, #153969346, #153969347)
    - Verify check_and_enforce_daily_drawdown() & handle_new_message automatically reset
      daily_start_balance to 26.93 GBP, unpause new signals, and keep all 4 pending orders of 13187 intact!
    """
    ex = _setup_test_env(tmp_path, "fix_a_13187")
    today_str = ex.get_server_date_str()

    # Seed Msg 13187 with 4 unfilled pending BUY LIMITs and 0 open market positions
    for t_id, px, lot in (
        (153969343, 3659.00, 0.01),
        (153969345, 3652.67, 0.01),
        (153969346, 3646.33, 0.01),
        (153969347, 3640.00, 0.02),
    ):
        ex._dry_run_pending_ticket_lots[t_id] = lot
        ex._dry_run_pending_orders[t_id] = {
            "symbol": "XAUUSD.s",
            "direction": "BUY",
            "price": px,
            "order_type": "LIMIT",
            "lot": lot,
        }

    main.state_mgr.create_signal(
        message_id=13187,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3630.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3659.00, "tp": 3661.5, "ticket": 153969343, "order_type": "LIMIT", "status": "PENDING"},
            "L2": {"lot": 0.01, "target_price": 3652.67, "tp": 3661.5, "ticket": 153969345, "order_type": "LIMIT", "status": "PENDING"},
            "L3": {"lot": 0.01, "target_price": 3646.33, "tp": 3661.5, "ticket": 153969346, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3640.00, "tp": 3661.5, "ticket": 153969347, "order_type": "LIMIT", "status": "PENDING"},
        },
        market_tickets=[],
        pending_tickets=[153969343, 153969345, 153969346, 153969347],
        raw_tp1=3662.0,
        final_tp=3661.5,
        status="ACTIVE",
    )

    # Simulate balance after realized loss: ref_balance=55.91, current balance=26.93, realized_today=-28.98 (-51.8%)
    ex._daily_drawdown_ref_balance = 55.91
    ex._daily_loss_baseline_realized = 0.0
    ex._dry_run_daily_realized_pnl = -28.98
    ex._dry_run_floating_pnl = 0.0
    ex._dry_run_capital = 26.93

    with patch.object(ex, "get_account_financials", return_value={
        "balance": 26.93, "equity": 26.93, "credit": 12.00, "profit": 0.0,
        "margin": 0.0, "free_margin": 38.93, "currency": "GBP", "formatted": "Bal: 26.93",
    }), patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Before check_and_enforce_daily_drawdown, get_daily_drawdown_status shows pause_new_signals=True
        dd_before = ex.get_daily_drawdown_status()
        assert dd_before["pause_new_signals"] is True

        # Now send a brand-new signal while Msg 13187 has 4 pending BUY LIMITs and 0 open market positions:
        # handle_new_message must auto-run check_and_enforce_daily_drawdown, reset baseline to 26.93,
        # unpause new signals, preserve all 4 pending orders of 13187, and execute the new signal!
        tick_now = SimpleNamespace(bid=3738.00, ask=3738.30, time=1700000000)
        with patch.object(ex, "get_tick", return_value=tick_now):
            await main.handle_new_message(
                FakeEvent(9965, "Sell limit xauusd 3745\nTp 3735\nSl 3755", chat_id=-1004369388188)
            )

        dd_after = ex.get_daily_drawdown_status()
        assert dd_after["pause_new_signals"] is False
        assert ex._daily_drawdown_ref_balance == pytest.approx(26.93)
        assert ex._daily_loss_baseline_realized == pytest.approx(-28.98)

        # Msg 13187's 4 pending BUY LIMIT orders MUST still be intact!
        s_13187 = main.state_mgr.get_signal(13187, chat_id=-1003712081715)
        assert s_13187["status"] == "ACTIVE"
        assert s_13187["pending_tickets"] == [153969343, 153969345, 153969346, 153969347]
        for t_id in (153969343, 153969345, 153969346, 153969347):
            assert t_id in ex._dry_run_pending_ticket_lots

        # And the new signal 9965 executed cleanly!
        s_9965 = main.state_mgr.get_signal(9965, chat_id=-1004369388188)
        assert s_9965 is not None and s_9965["status"] == "ACTIVE"
        assert len(s_9965["pending_tickets"]) == 1


@pytest.mark.asyncio
async def test_fix_b1_b2_single_price_consolidation_zone_expansion_and_duplicate_guard(tmp_path):
    """
    Verify:
    1. Single-price limit signal (Msg 9957 `Sell limit xauusd 3755`) places 1 consolidated
       pending order at 3755.0 (not 4 identical stacked orders).
    2. Duplicate single-price signal at 3755.0 (or within $0.50 tolerance) is blocked from
       stacking duplicate pending limit orders at the same price.
    3. Editing a 1-order consolidated single-price signal into a 4-layer zone (e.g. Msg 13167
       `BUY XAUUSD 3750` -> `BUY XAUUSD 3750-3740`) expands L2..L4 across the zone tiers.
    """
    ex = _setup_test_env(tmp_path, "fix_b1_b2")
    tick_now = SimpleNamespace(bid=3740.00, ask=3740.30, time=1700000100)

    with patch.object(ex, "get_tick", return_value=tick_now), \
         patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # 1. Msg 9957: "Sell limit xauusd 3755\nTp 3745\nSl 3765" -> places 1 order at 3755.0
        await main.handle_new_message(
            FakeEvent(9957, "Sell limit xauusd 3755\nTp 3745\nSl 3765", chat_id=-1004369388188)
        )
        s_9957 = main.state_mgr.get_signal(9957, chat_id=-1004369388188)
        assert s_9957 is not None
        assert len(s_9957["pending_tickets"]) == 1
        assert list(s_9957["layers"].keys()) == ["L1"]
        assert s_9957["zone_completed"] is False

        # 2. Another signal at the same price 3755.0 (or 3755.20 within $0.50 tolerance) is blocked!
        mk_dup, pd_dup, layers_dup = ex.execute_4layer_single_price(
            symbol="XAUUSD.s",
            direction="SELL",
            price=3755.20,
            sl=3765.0,
            final_tp=3745.5,
            raw_tp1=3745.0,
            order_intent="LIMIT",
        )
        assert mk_dup == [] and pd_dup == [] and layers_dup == {}
        assert "Duplicate" in (ex.last_error or "")

        # 3. Single-price BUY LIMIT at 3730.0 edited into a 4-layer zone 3730-3721 expands L2..L4!
        await main.handle_new_message(
            FakeEvent(13167, "BUY XAUUSD 3730\nTP1 3735\nTP2 3742\nSL 3715", chat_id=-1003712081715)
        )
        s_init = main.state_mgr.get_signal(13167, chat_id=-1003712081715)
        assert s_init is not None
        assert len(s_init["pending_tickets"]) == 1
        assert s_init["zone_completed"] is False

        await main.handle_message_edited(
            FakeEvent(13167, "BUY XAUUSD 3730-3721\nTP1 3735\nTP2 3742\nSL 3715", chat_id=-1003712081715)
        )
        s_exp = main.state_mgr.get_signal(13167, chat_id=-1003712081715)
        assert len(s_exp["pending_tickets"]) == 4
        assert set(s_exp["layers"].keys()) == {"L1", "L2", "L3", "L4"}
        assert s_exp["zone_completed"] is True


@pytest.mark.asyncio
async def test_fix_b3_delete_sell_limit_and_delete_buy_limit_cancel_pending_only(tmp_path):
    """
    Verify:
    - Msg 9960 (`DELETE SELL LIMIT`) and Msg 9910 (`Delete buy limit for now everyone`)
      parse as `ACTION_CANCEL_PENDING` with `cancel_direction="SELL"` / `"BUY"`.
    - Bare `"delete limit"` still parses as `ACTION_FULL_CLOSE`.
    - Executing `DELETE SELL LIMIT` cancels pending SELL limit orders while preserving
      any open market positions and any BUY pending orders.
    """
    p_sell = parse_message("DELETE SELL LIMIT", message_id=9960)
    assert p_sell is not None
    assert p_sell.management_action == "ACTION_CANCEL_PENDING"
    assert p_sell.cancel_direction == "SELL"

    p_buy = parse_message("Delete buy limit for now everyone", message_id=9910)
    assert p_buy is not None
    assert p_buy.management_action == "ACTION_CANCEL_PENDING"
    assert p_buy.cancel_direction == "BUY"

    p_bare = parse_message("delete limit", message_id=9911)
    assert p_bare is not None
    assert p_bare.management_action == "ACTION_FULL_CLOSE"

    p_lads = parse_message("Delete limit lads left without us", message_id=10006)
    assert p_lads is not None
    assert p_lads.management_action == "ACTION_CANCEL_PENDING"
    assert p_lads.cancel_direction is None

    ex = _setup_test_env(tmp_path, "fix_b3")
    tick_now = SimpleNamespace(bid=3740.00, ask=3740.30, time=1700000200)

    with patch.object(ex, "get_tick", return_value=tick_now), \
         patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Place pending BUY limit (Msg 9901 @ 3725) and pending SELL limit (Msg 9957 @ 3755)
        await main.handle_new_message(
            FakeEvent(9901, "Buy limit xauusd 3725\nTp 3735\nSl 3715", chat_id=-1004369388188)
        )
        await main.handle_new_message(
            FakeEvent(9957, "Sell limit xauusd 3755\nTp 3745\nSl 3765", chat_id=-1004369388188)
        )
        assert len(main.state_mgr.get_signal(9901, chat_id=-1004369388188)["pending_tickets"]) == 1
        assert len(main.state_mgr.get_signal(9957, chat_id=-1004369388188)["pending_tickets"]) == 1

        # Standalone "DELETE SELL LIMIT" (Msg 9960) must cancel 9957 (SELL) and leave 9901 (BUY) untouched!
        await main.handle_new_message(
            FakeEvent(9960, "DELETE SELL LIMIT", chat_id=-1004369388188)
        )
        s_sell_after = main.state_mgr.get_signal(9957, chat_id=-1004369388188)
        s_buy_after = main.state_mgr.get_signal(9901, chat_id=-1004369388188)
        assert s_sell_after["pending_tickets"] == []
        assert s_sell_after["status"] in ("CANCELLED", "CLOSED")
        assert len(s_buy_after["pending_tickets"]) == 1
        assert s_buy_after["status"] == "ACTIVE"

        # Standalone "Delete buy limit for now everyone" (Msg 9910) cancels 9901 (BUY)!
        await main.handle_new_message(
            FakeEvent(9910, "Delete buy limit for now everyone", chat_id=-1004369388188)
        )
        s_buy_final = main.state_mgr.get_signal(9901, chat_id=-1004369388188)
        assert s_buy_final["pending_tickets"] == []
        assert s_buy_final["status"] in ("CANCELLED", "CLOSED")

        # 3. Cross-channel isolation: "Delete limit lads left without us" OR "close all" in Channel A (-1004369388188)
        #    MUST NEVER cancel Channel B's (-1003712081715) pending BUY LIMITs (Msg 13187)!
        await main.handle_new_message(
            FakeEvent(13187, "BUY LIMIT 3659-3640\nSL 3630\nTP 3662\nTP 3669", chat_id=-1003712081715)
        )
        await main.handle_new_message(
            FakeEvent(10003, "Sell limit xauusd 3755\nTp 3745\nSl 3765", chat_id=-1004369388188)
        )
        assert len(main.state_mgr.get_signal(13187, chat_id=-1003712081715)["pending_tickets"]) == 4
        assert len(main.state_mgr.get_signal(10003, chat_id=-1004369388188)["pending_tickets"]) == 1

        await main.handle_new_message(
            FakeEvent(10006, "Delete limit lads left without us", chat_id=-1004369388188)
        )
        assert main.state_mgr.get_signal(10003, chat_id=-1004369388188)["pending_tickets"] == []
        s_13187_after = main.state_mgr.get_signal(13187, chat_id=-1003712081715)
        assert s_13187_after["status"] == "ACTIVE"
        assert len(s_13187_after["pending_tickets"]) == 4


@pytest.mark.asyncio
async def test_fix_b4_gate5_wrong_side_tp_edit_sanity_msg_9919(tmp_path):
    """
    Reproduce Msg 9919 live bug:
    - Initial message: unpriced "Sell xauusd now" at ~3729.10 (fallback TP1=3724.10).
    - Edited 63s later to: "Sell xauusd 3728-3732\\n\\nTp 4320\\n\\nSl 3736" where TP 4320
      is a typo for 3720 ($590 above SELL entry, wrong side of direction!).
    - Gate 5 must strip TP 4320 (`tp_valid=False`, `invalid_explicit_tps=[4320.0]`, `tp_levels={}`),
      so `handle_message_edited` updates SL to 3736.0 without attempting to push TP=4320.5 to MT5!
    """
    raw_edit = "Sell xauusd 3728-3732\n\nTp 4320\n\nSl 3736"
    parsed = parse_message(raw_edit, message_id=9919, is_edit=True, live_price=3729.10)
    assert parsed is not None
    assert parsed.direction == "SELL"
    assert parsed.sl == pytest.approx(3736.0)
    assert parsed.tp_levels == {}
    assert parsed.raw_tp1 is None
    assert parsed.final_tp is None
    assert parsed.tp_valid is False
    assert parsed.invalid_explicit_tps == [4320.0]

    ex = _setup_test_env(tmp_path, "fix_b4_9919")
    tick_now = SimpleNamespace(bid=3729.10, ask=3729.35, time=1700000300)

    with patch.object(ex, "get_tick", return_value=tick_now), \
         patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Seed active SELL signal 9919 with fallback TP 3724.60
        main.state_mgr.create_signal(
            message_id=9919,
            chat_id=-1004369388188,
            symbol="XAUUSD.s",
            direction="SELL",
            sl=None,
            layers={
                "L1": {"lot": 0.01, "target_price": 3729.10, "entry_price": 3729.10, "tp": 3724.60, "tp_stage": "TP1", "ticket": 153835461, "order_type": "MARKET", "status": "OPEN"},
            },
            market_tickets=[153835461],
            pending_tickets=[],
            raw_tp1=3724.10,
            final_tp=3724.60,
            fallback_tp_applied=True,
            recorded_entry_price=3729.10,
            initial_price=3729.10,
            status="ACTIVE",
        )
        ex._dry_run_ticket_lots[153835461] = 0.01

        sent_tps = []
        orig_mod = ex.modify_position_sltp

        def _spy_mod(ticket, sl=None, tp=None, require_both=False):
            sent_tps.append(tp)
            return orig_mod(ticket, sl=sl, tp=tp, require_both=require_both)

        with patch.object(ex, "modify_position_sltp", side_effect=_spy_mod):
            await main.handle_message_edited(
                FakeEvent(9919, raw_edit, chat_id=-1004369388188)
            )

        # Verify TP 4320 / 4320.5 was NEVER sent to MT5, while SL 3736.0 was applied!
        assert all(tp is None or tp < 3728.0 for tp in sent_tps)
        s_9919 = main.state_mgr.get_signal(9919, chat_id=-1004369388188)
        assert s_9919["sl"] == pytest.approx(3736.0)


@pytest.mark.asyncio
async def test_fix_d_limit_entry_front_run_offset_050_msg_13191(tmp_path, monkeypatch):
    """
    Reproduce Msg 13191 ('BUY 3710-3700 SL 3685 TP 3713 TP 3720 ...'):
    - Raw zone tiers are {L1: 3710.0, L2: 3706.67, L3: 3703.33, L4: 3700.0}.
    - Live market dipped to bid=3710.44 / ask=3710.76 (44 points above 3710.00) and then rallied to TP1=3713.0.
    - With LIMIT_ENTRY_FRONT_RUN_OFFSET=0.50 (+$0.50 on BUY LIMIT, -$0.50 on SELL LIMIT),
      the 4 BUY LIMIT layers are placed +0.50 higher at {L1: 3710.5, L2: 3707.17, L3: 3703.83, L4: 3700.5},
      and SELL LIMIT layers are placed -0.50 lower, so L1 @ 3710.50 activates cleanly instead of missing!
    """
    ex = _setup_test_env(tmp_path, "fix_d_13191")
    monkeypatch.setattr(config, "LIMIT_ENTRY_FRONT_RUN_OFFSET", 0.50)
    monkeypatch.setattr(ex_mod, "LIMIT_ENTRY_FRONT_RUN_OFFSET", 0.50)
    monkeypatch.setattr(main, "LIMIT_ENTRY_FRONT_RUN_OFFSET", 0.50)

    tick_now = SimpleNamespace(bid=3715.88, ask=3716.40, time=1700000400)
    with patch.object(ex, "get_tick", return_value=tick_now), \
         patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # 1. BUY Zone 3710-3700 (Msg 13191) -> shifted +0.50 higher to [3710.50, 3707.17, 3703.83, 3700.50]
        await main.handle_new_message(
            FakeEvent(
                13191,
                "BUY 3710-3700\nSL 3685\nTP 3713\nTP 3720\nFP 3723\nTP 3730\nTP 3740",
                chat_id=-1003712081715,
            )
        )
        s_buy = main.state_mgr.get_signal(13191, chat_id=-1003712081715)
        assert s_buy is not None
        assert len(s_buy["pending_tickets"]) == 4
        assert s_buy["layers"]["L1"]["target_price"] == pytest.approx(3710.50)
        assert s_buy["layers"]["L2"]["target_price"] == pytest.approx(3707.17)
        assert s_buy["layers"]["L3"]["target_price"] == pytest.approx(3703.83)
        assert s_buy["layers"]["L4"]["target_price"] == pytest.approx(3700.50)

        # 2. SELL Zone 3720-3730 -> shifted -0.50 lower to [3719.50, 3722.83, 3726.17, 3729.50]
        await main.handle_new_message(
            FakeEvent(
                13192,
                "SELL 3720-3730\nSL 3745\nTP 3715\nTP 3705",
                chat_id=-1003712081715,
            )
        )
        s_sell = main.state_mgr.get_signal(13192, chat_id=-1003712081715)
        assert s_sell is not None
        assert len(s_sell["pending_tickets"]) == 4
        assert s_sell["layers"]["L1"]["target_price"] == pytest.approx(3719.50)
        assert s_sell["layers"]["L2"]["target_price"] == pytest.approx(3722.83)
        assert s_sell["layers"]["L3"]["target_price"] == pytest.approx(3726.17)
        assert s_sell["layers"]["L4"]["target_price"] == pytest.approx(3729.50)

        # 3. Single-price BUY LIMIT 3705 -> shifted +0.50 higher to 3705.50
        await main.handle_new_message(
            FakeEvent(
                13195,
                "BUY LIMIT 3705\nSL 3690\nTP 3715",
                chat_id=-1003712081715,
            )
        )
        s_single = main.state_mgr.get_signal(13195, chat_id=-1003712081715)
        assert s_single is not None
        assert s_single["layers"]["L1"]["target_price"] == pytest.approx(3705.50)


@pytest.mark.asyncio
async def test_fix_e_deep_layer_uses_own_tp1_leg_and_preserves_l3_l4_pendings_msg_13199(tmp_path):
    """
    Reproduce Msg 13199 ('BUY LIMIT 3700-3690 SL 3675 TP 3703 TP 3710 ...'):
    - L1 filled at 3700.50, L2 filled at 3697.17, L3 (3693.83) and L4 (3690.50) pending.
    - When price bounces from 3697.17 to 3699.82:
      * L1 is still underwater (-$0.68/oz, below 3700.50).
      * L2 has MFE = +$2.65/oz, which is only 45.5% of L2's own $5.83 leg to TP1 (3703.0 - 3697.17),
        and L2's own tp1_leg ($5.83) is > $3.50 (RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS).
      * Therefore, L2 MUST NOT trigger TIER_0T (which only applies when a layer's OWN tp1_leg <= $3.50),
        L2's SL must stay at 3675.0 (not choked at 3697.67), and L3/L4 pending limits MUST NOT be cancelled!
    """
    ex = _setup_test_env(tmp_path, "fix_e_13199")
    main.state_mgr.create_signal(
        message_id=13199,
        chat_id=-1003712081715,
        symbol="XAUUSD.s",
        direction="BUY",
        sl=3675.0,
        layers={
            "L1": {"lot": 0.01, "target_price": 3700.50, "entry_price": 3700.50, "tp": 3702.5, "tp_stage": "TP1", "ticket": 154321761, "order_type": "LIMIT", "status": "FILLED"},
            "L2": {"lot": 0.01, "target_price": 3697.17, "entry_price": 3697.17, "tp": 3702.5, "tp_stage": "TP1", "ticket": 154321762, "order_type": "LIMIT", "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3693.83, "tp": 3702.5, "tp_stage": "TP1", "ticket": 154321763, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3690.50, "tp": 3702.5, "tp_stage": "TP1", "ticket": 154321764, "order_type": "LIMIT", "status": "PENDING"},
        },
        market_tickets=[154321761, 154321762],
        pending_tickets=[154321763, 154321764],
        all_placed_tickets=[154321761, 154321762, 154321763, 154321764],
        raw_tp1=3703.0,
        raw_tp2=3710.0,
        raw_tp3=3713.0,
        tp_levels={"TP1": 3703.0, "TP2": 3710.0, "TP3": 3713.0},
        final_tp=3702.5,
        recorded_entry_price=3700.50,
        initial_price=3700.50,
        status="ACTIVE",
    )
    for t_id, lot in ((154321761, 0.01), (154321762, 0.01)):
        ex._dry_run_ticket_lots[t_id] = lot
    for t_id, lot in ((154321763, 0.01), (154321764, 0.02)):
        ex._dry_run_pending_ticket_lots[t_id] = lot

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock):
        # Bounce to bid=3699.82, ask=3700.14 (L2 MFE = +$2.65/oz, still below L1=3700.50)
        with patch.object(ex, "get_tick", return_value=SimpleNamespace(bid=3699.82, ask=3700.14, time=1700000500)):
            await main._process_pre_tp1_or_backstop_tick(13199, main.state_mgr.get_signal(13199, chat_id=-1003712081715))

        s_after = main.state_mgr.get_signal(13199, chat_id=-1003712081715)
        assert s_after["layers"]["L2"].get("ratchet_tier") is None
        assert s_after["layers"]["L2"]["status"] == "FILLED"
        # L3 and L4 pending limits MUST still be alive!
        assert s_after["pending_tickets"] == [154321763, 154321764]
        assert s_after["layers"]["L3"]["status"] == "PENDING"
        assert s_after["layers"]["L4"]["status"] == "PENDING"


