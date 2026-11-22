"""
Production tests for v3.3.3 Fast Per-Channel Standalone Breakeven Across All Layers (L1-L4):
1. Standalone non-reply breakeven recognition (short & >10-word natural commands, +pips recaps, TP1+BE combos)
   with strict false-positive immunity against casual English "be" chatter & new-signal posts.
2. Strict per-channel trade isolation: standalone breakeven in Channel A ONLY protects Channel A's trade
   (never crossing over to Channel B or sweeping unrelated channels) and moves ALL 4 layers (L1, L2, L3, L4).
3. Smart target selection within a channel: prioritizes open filled positions over unfilled pending limits,
   and handles replies to intermediate commentary messages in the same channel.
4. Instant 0ms two-tier stop-level fallback (+2.0p -> +0.0p exact entry) + forward-only SL ratchet + RUNNER_ACTIVE preservation.
5. Sub-second fill-race promotion + 0.5s armed auto-BE sweep when an upper layer is temporarily underwater.
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config
import main
import mt5_executor as mt5_exec_mod
from mt5_executor import MT5Executor
from parser import parse_message
from state_manager import StateManager


class DummyChannelMsg:
    def __init__(self, msg_id: int, text: str, reply_to_msg_id=None):
        self.id = msg_id
        self.message = text
        self.text = text
        self.raw_text = text
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to = SimpleNamespace(reply_to_msg_id=reply_to_msg_id) if reply_to_msg_id is not None else None
        self.forward = None
        self.date = None
        self.edit_date = None


class DummyChannelEvent:
    def __init__(self, msg_id: int, chat_id: int, text: str, reply_to_msg_id=None):
        self.id = msg_id
        self.chat_id = chat_id
        self.is_reply = reply_to_msg_id is not None
        self.message = DummyChannelMsg(msg_id, text, reply_to_msg_id=reply_to_msg_id)


def test_default_be_layers_includes_all_four_layers(shipped_config):
    """Verify default BE_LAYERS covers all 4 layers including L4 (0.02 lot)."""
    assert shipped_config.BE_LAYERS == ["L1", "L2", "L3", "L4"]


def test_standalone_channel_breakeven_phrases_and_false_positive_guards():
    """Verify non-reply standalone breakeven prompts match while English 'be' chatter does not."""
    valid_standalone_be_prompts = [
        "BE",
        "be!",
        "be now",
        "Go breakeven!",
        "breakeven guys",
        "break even your entries",
        "set breakeven on entries",
        "move sl to entry",
        "sl at entry now",
        "risk free now!",
        "+55 pips running 🔥 go breakeven now",
        "TP1 hit, go breakeven!",
        "Everyone please move sl to entry right now and hold your layers risk free from here",
        "I’m holding sells with break even set , some partials closed",
    ]
    for prompt in valid_standalone_be_prompts:
        res = parse_message(prompt, message_id=900, is_reply=False, reply_to_msg_id=None)
        assert res is not None, f"Expected ACTION_BREAKEVEN for standalone prompt: {prompt!r}"
        assert res.event_type == "TRADE_MANAGEMENT", f"Wrong event_type for {prompt!r}: {res.event_type}"
        assert res.management_action == "ACTION_BREAKEVEN", f"Wrong action for {prompt!r}: {res.management_action}"

    non_be_messages = [
        "guys be careful with CPI news in 5 minutes",
        "we will be ready for New York open soon",
        "let it be for now guys wait for confirmation",
        "Last night our entry was risk free +120 pips boom",
    ]
    for chatter in non_be_messages:
        res = parse_message(chatter, message_id=901, is_reply=False, reply_to_msg_id=None)
        assert res is None or res.management_action != "ACTION_BREAKEVEN", (
            f"Casual/recap text {chatter!r} should NOT trigger ACTION_BREAKEVEN!"
        )

    # New signal containing "be careful" must parse as NEW_SIGNAL, never ACTION_BREAKEVEN
    sig_res = parse_message(
        "Sell XAUUSD 3900-3905\nTP 3890\nSL 3915\nbe careful high risk",
        message_id=902,
        is_reply=False,
        reply_to_msg_id=None,
    )
    assert sig_res is not None
    assert sig_res.event_type == "NEW_SIGNAL"
    assert sig_res.direction == "SELL"


@pytest.mark.asyncio
async def test_strict_per_channel_breakeven_isolation_and_all_4_layers():
    """
    Standalone 'Go breakeven!' in Channel A must:
    1. Execute with ZERO pre-parse `get_tick` MT5 IPC delay.
    2. Move ALL 4 layers (L1, L2, L3, L4 including 0.02 L4) on Channel A's trade to BE.
    3. Leave Channel B's active trade 100% untouched.
    4. Ignore standalone 'breakeven' from Channel C (which has no active trade) without leaking to A or B.
    """
    chan_a = -1004369388188
    chan_a_raw = -4369388188  # tests -100 <-> - normalization too
    chan_b = -1003712081715
    chan_c = -1009999999999

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(Path(tmp) / "signals_state.json")
        ex = MT5Executor(dry_run=True)

        # Active 4-layer BUY trade in Channel A
        sm.create_signal(
            message_id=101,
            chat_id=chan_a,
            symbol="XAUUSD",
            direction="BUY",
            entry_type="ZONE",
            layers={
                "L1": {"lot": 0.01, "target_price": 3895.0, "tp": 3904.5, "tp_stage": "TP1", "ticket": 1001, "order_type": "MARKET", "status": "OPEN"},
                "L2": {"lot": 0.01, "target_price": 3892.0, "tp": 3904.5, "tp_stage": "TP1", "ticket": 1002, "order_type": "MARKET", "status": "FILLED"},
                "L3": {"lot": 0.01, "target_price": 3889.0, "tp": 3904.5, "tp_stage": "TP1", "ticket": 1003, "order_type": "MARKET", "status": "FILLED"},
                "L4": {"lot": 0.02, "target_price": 3886.0, "tp": 3904.5, "tp_stage": "TP1", "ticket": 1004, "order_type": "MARKET", "status": "FILLED"},
            },
            market_tickets=[1001, 1002, 1003, 1004],
            pending_tickets=[],
            sl=3875.0,
            raw_tp1=3905.0,
            final_tp=3904.5,
        )

        # Active 4-layer SELL trade in Channel B
        sm.create_signal(
            message_id=202,
            chat_id=chan_b,
            symbol="XAUUSD",
            direction="SELL",
            entry_type="ZONE",
            layers={
                "L1": {"lot": 0.01, "target_price": 3910.0, "tp": 3900.5, "tp_stage": "TP1", "ticket": 2001, "order_type": "MARKET", "status": "OPEN"},
                "L2": {"lot": 0.01, "target_price": 3913.0, "tp": 3900.5, "tp_stage": "TP1", "ticket": 2002, "order_type": "MARKET", "status": "FILLED"},
                "L3": {"lot": 0.01, "target_price": 3916.0, "tp": 3900.5, "tp_stage": "TP1", "ticket": 2003, "order_type": "MARKET", "status": "FILLED"},
                "L4": {"lot": 0.02, "target_price": 3919.0, "tp": 3900.5, "tp_stage": "TP1", "ticket": 2004, "order_type": "MARKET", "status": "FILLED"},
            },
            market_tickets=[2001, 2002, 2003, 2004],
            pending_tickets=[],
            sl=3930.0,
            raw_tp1=3900.0,
            final_tp=3900.5,
        )

        with patch.object(main, "state_mgr", sm), \
             patch.object(main, "executor", ex), \
             patch.object(main, "BE_LAYERS", ["L1", "L2", "L3", "L4"]), \
             patch.object(main, "BE_OFFSET_PIPS", 5.0), \
             patch.object(config, "BE_OFFSET_PIPS", 5.0), \
             patch.object(main, "CHANNEL_IDS", [chan_a, chan_a_raw, chan_b, chan_c]), \
             patch.object(main, "notify_saved_messages", new=AsyncMock()):

            # 1) Standalone "breakeven" in Channel C (no trade in Channel C) -> MUST NOT touch A or B!
            evt_c = DummyChannelEvent(msg_id=303, chat_id=chan_c, text="break even your entries", reply_to_msg_id=None)
            await main.handle_new_message(evt_c)

            sig_a_before = sm.get_signal(101, chat_id=chan_a)
            sig_b_before = sm.get_signal(202, chat_id=chan_b)
            assert sig_a_before["status"] == "ACTIVE" and not sig_a_before.get("be_done_tickets")
            assert sig_b_before["status"] == "ACTIVE" and not sig_b_before.get("be_done_tickets")

            # 2) Standalone "Go breakeven!" in Channel A (no reply_to_msg_id, raw chat ID form)
            with patch.object(ex, "get_tick") as mock_get_tick:
                evt_a = DummyChannelEvent(msg_id=105, chat_id=chan_a_raw, text="Go breakeven!", reply_to_msg_id=None)
                await main.handle_new_message(evt_a)
                # Zero pre-parse get_tick IPC call on trade management commands!
                mock_get_tick.assert_not_called()

            sig_a_after = sm.get_signal(101, chat_id=chan_a)
            sig_b_after = sm.get_signal(202, chat_id=chan_b)

            # All 4 layers (including L4 0.02 lot) of Channel A moved to BE!
            assert sig_a_after["status"] == "BE_APPLIED"
            assert sig_a_after["be_armed"] is True
            assert sig_a_after["be_done_tickets"] == [1001, 1002, 1003, 1004]
            assert sig_a_after["layers"]["L1"]["be_sl"] == 3895.5
            assert sig_a_after["layers"]["L2"]["be_sl"] == 3892.5
            assert sig_a_after["layers"]["L3"]["be_sl"] == 3889.5
            assert sig_a_after["layers"]["L4"]["be_sl"] == 3886.5

            # Channel B trade remains 100% untouched!
            assert sig_b_after["status"] == "ACTIVE"
            assert not sig_b_after.get("be_done_tickets")
            assert "be_sl" not in sig_b_after["layers"]["L1"]


@pytest.mark.asyncio
async def test_channel_prefers_open_trade_over_unfilled_pending_and_handles_commentary_reply():
    """
    If Channel A has both an open filled trade (#501) and a newer unfilled pending setup (#502),
    a standalone breakeven (or a reply to an intermediate commentary message #555) in Channel A
    protects the open filled trade (#501) across all 4 layers.
    """
    chan_a = -1004369388188
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(Path(tmp) / "signals_state.json")
        ex = MT5Executor(dry_run=True)

        sm.create_signal(
            message_id=501,
            chat_id=chan_a,
            symbol="XAUUSD",
            direction="BUY",
            entry_type="ZONE",
            layers={
                "L1": {"lot": 0.01, "target_price": 3880.0, "tp": 3890.0, "tp_stage": "TP1", "ticket": 5001, "status": "OPEN"},
                "L2": {"lot": 0.01, "target_price": 3878.0, "tp": 3890.0, "tp_stage": "TP1", "ticket": 5002, "status": "FILLED"},
                "L3": {"lot": 0.01, "target_price": 3876.0, "tp": 3890.0, "tp_stage": "TP1", "ticket": 5003, "status": "FILLED"},
                "L4": {"lot": 0.02, "target_price": 3874.0, "tp": 3890.0, "tp_stage": "TP1", "ticket": 5004, "status": "FILLED"},
            },
            market_tickets=[5001, 5002, 5003, 5004],
            pending_tickets=[],
            sl=3865.0,
        )
        # Newer unfilled pending limit in the same channel
        sm.create_signal(
            message_id=502,
            chat_id=chan_a,
            symbol="XAUUSD",
            direction="SELL",
            entry_type="ZONE",
            layers={
                "L1": {"lot": 0.01, "target_price": 3920.0, "tp": 3910.0, "tp_stage": "TP1", "ticket": 6001, "status": "PENDING"},
            },
            market_tickets=[],
            pending_tickets=[6001],
            sl=3935.0,
        )

        with patch.object(main, "state_mgr", sm), \
             patch.object(main, "executor", ex), \
             patch.object(main, "BE_LAYERS", ["L1", "L2", "L3", "L4"]), \
             patch.object(main, "CHANNEL_IDS", [chan_a]), \
             patch.object(main, "notify_saved_messages", new=AsyncMock()):

            # Reply to commentary message #555 (not a signal ID) saying "+45 pips go breakeven!"
            evt = DummyChannelEvent(msg_id=556, chat_id=chan_a, text="+45 pips go breakeven!", reply_to_msg_id=555)
            await main.handle_new_message(evt)

            sig_open = sm.get_signal(501, chat_id=chan_a)
            sig_pend = sm.get_signal(502, chat_id=chan_a)
            assert sig_open["status"] == "BE_APPLIED"
            assert sig_open["be_done_tickets"] == [5001, 5002, 5003, 5004]
            assert sig_pend["status"] == "ACTIVE"
            assert not sig_pend.get("be_done_tickets")


def test_live_mt5_zero_ms_exact_entry_fallback_and_forward_only_runner_ratchet():
    """
    Verify live MT5 `apply_signal_breakeven`:
    - L1 (#7001): +2.0p (3895.20) rejected by stops_level -> 0ms fallback to exact entry 3895.00 succeeds!
    - L2 (#7002) & L3 (#7003): +2.0p succeeds immediately.
    - L4 (#7004): already trailed above entry at 3897.50 -> forward-only ratchet NEVER loosens SL!
    """
    ex = MT5Executor(dry_run=False)
    live_positions = {
        7001: SimpleNamespace(ticket=7001, price_open=3895.00, sl=3875.00, tp=3904.50, type=0),
        7002: SimpleNamespace(ticket=7002, price_open=3892.00, sl=3875.00, tp=3904.50, type=0),
        7003: SimpleNamespace(ticket=7003, price_open=3889.00, sl=3875.00, tp=3904.50, type=0),
        7004: SimpleNamespace(ticket=7004, price_open=3886.00, sl=3897.50, tp=3915.00, type=0),
    }
    modify_calls = []

    def fake_modify(ticket, sl=None, tp=None):
        modify_calls.append((ticket, sl, tp))
        # Simulate L1 being +1.5p in profit: 3895.20 fails stops_level, exact 3895.00 succeeds!
        if ticket == 7001 and sl == 3895.20:
            return False
        live_positions[ticket].sl = sl
        return True

    fake_mt5 = MagicMock()
    fake_mt5.ORDER_TYPE_BUY = 0
    fake_mt5.positions_get.side_effect = lambda ticket=None: [live_positions[ticket]] if ticket in live_positions else []

    sig = {
        "symbol": "XAUUSD",
        "direction": "BUY",
        "status": "RUNNER_ACTIVE",
        "runner_sl_floor": 3897.50,
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3895.0, "ticket": 7001, "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3892.0, "ticket": 7002, "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3889.0, "ticket": 7003, "status": "FILLED"},
            "L4": {"lot": 0.02, "target_price": 3886.0, "ticket": 7004, "status": "FILLED"},
        },
        "market_tickets": [7001, 7002, 7003, 7004],
        "pending_tickets": [],
        "runner_tickets": [7004],
        "runner_ticket": 7004,
    }

    with patch.object(mt5_exec_mod, "mt5", fake_mt5), \
         patch.object(ex, "get_symbol_info", return_value=SimpleNamespace(digits=2, point=0.01)), \
         patch.object(ex, "modify_position_sltp", side_effect=fake_modify):
        res = ex.apply_signal_breakeven(sig, be_layers=["L1", "L2", "L3", "L4"], be_offset_pips=2.0)

    assert res["modified_tickets"] == [7001, 7002, 7003, 7004]
    assert res["deferred_tickets"] == []
    # L1 tried 3895.20 then immediately landed exact 3895.00
    assert (7001, 3895.20, 3904.50) in modify_calls
    assert (7001, 3895.00, 3904.50) in modify_calls
    assert res["applied_sls"][7001] == 3895.00
    assert res["applied_sls"][7002] == 3892.20
    assert res["applied_sls"][7003] == 3889.20
    # L4 kept its tighter trailed SL 3897.50 without any loosening modify call!
    assert res["applied_sls"][7004] == 3897.50
    assert not any(t == 7004 for t, _, _ in modify_calls)
    assert sig["runner_sl_floor"] == 3897.50


def test_subsecond_fill_race_promotion_and_armed_auto_be_retry():
    """
    When 'Go breakeven!' arrives while L1 (#8001 at 3895.0) is underwater at 3893.5,
    and L4 (#8004 at 3886.0) just filled milliseconds ago (still in `pending_tickets`):
    1. Call 1 promotes L4 to `market_tickets`, protects L2/L3/L4 immediately, and defers L1.
    2. Call 2 (0.5s watcher sweep once price rallies past 3895.2) snaps L1 to BE automatically!
    """
    ex = MT5Executor(dry_run=False)
    curr_bid = {"val": 3893.50}
    live_positions = {
        8001: SimpleNamespace(ticket=8001, price_open=3895.00, sl=3875.00, tp=3904.50, type=0),
        8002: SimpleNamespace(ticket=8002, price_open=3892.00, sl=3875.00, tp=3904.50, type=0),
        8003: SimpleNamespace(ticket=8003, price_open=3889.00, sl=3875.00, tp=3904.50, type=0),
        8004: SimpleNamespace(ticket=8004, price_open=3886.00, sl=3875.00, tp=3904.50, type=0),
    }

    def fake_modify(ticket, sl=None, tp=None):
        # Broker BUY SL rule: SL must be strictly below current Bid
        if sl >= curr_bid["val"]:
            return False
        live_positions[ticket].sl = sl
        return True

    fake_mt5 = MagicMock()
    fake_mt5.ORDER_TYPE_BUY = 0
    fake_mt5.positions_get.side_effect = lambda ticket=None: [live_positions[ticket]] if ticket in live_positions else []

    sig = {
        "symbol": "XAUUSD",
        "direction": "BUY",
        "status": "ACTIVE",
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3895.0, "ticket": 8001, "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3892.0, "ticket": 8002, "status": "FILLED"},
            "L3": {"lot": 0.01, "target_price": 3889.0, "ticket": 8003, "status": "FILLED"},
            "L4": {"lot": 0.02, "target_price": 3886.0, "ticket": 8004, "status": "PENDING"},
        },
        "market_tickets": [8001, 8002, 8003],
        "pending_tickets": [8004],  # Filled on MT5 milliseconds ago before reconciler!
    }

    with patch.object(mt5_exec_mod, "mt5", fake_mt5), \
         patch.object(ex, "get_symbol_info", return_value=SimpleNamespace(digits=2, point=0.01)), \
         patch.object(ex, "modify_position_sltp", side_effect=fake_modify):

        # Pass 1 at Bid=3893.50: L2, L3, L4 protected immediately; L4 promoted; L1 deferred!
        res1 = ex.apply_signal_breakeven(sig, be_layers=["L1", "L2", "L3", "L4"], be_offset_pips=2.0)
        assert res1["promoted_tickets"] == [8004]
        assert sig["pending_tickets"] == []
        assert sig["market_tickets"] == [8001, 8002, 8003, 8004]
        assert sig["layers"]["L4"]["status"] == "FILLED"
        assert res1["modified_tickets"] == [8002, 8003, 8004]
        assert res1["deferred_tickets"] == [8001]

        # Pass 2 on 0.5s watcher tick once Bid rallies to 3895.60: L1 snaps to BE automatically!
        curr_bid["val"] = 3895.60
        res2 = ex.apply_signal_breakeven(sig, be_layers=["L1", "L2", "L3", "L4"], be_offset_pips=2.0)
        assert res2["modified_tickets"] == [8001]
        assert res2["deferred_tickets"] == []
        assert sig["be_done_tickets"] == [8001, 8002, 8003, 8004]
        assert live_positions[8001].sl == 3895.20
