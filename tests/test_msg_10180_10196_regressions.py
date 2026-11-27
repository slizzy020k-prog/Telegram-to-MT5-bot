"""
Regression suite for the Msg 10180 / Msg 10196 production audit fixes.

Msg 10180 (state-machine / role integrity):
  * stub -> zone edit must apply ADAPTIVE ATR scaling and persist the snapshot
  * the deepest (L4) fill must be promoted to CORE_RUNNER, never L1
  * a pre-BE TP1->TP2 lift must NOT lock roles (forward-only guard stays open)
  * the TP1 -> TP2 ATR-proximity lift needs >=50 % leg progress unless be_locked
  * the monotonic TP sanitizer must drop the 3437 typo and keep TP1..TPn contiguous

Msg 10196 (chatter guard + progressive ATR trail + BE protection):
  * future-tense chatter ("gonna ... soon") must never reach the order path
  * the progressive trail ladder must tighten STANDARD -> EXTENDED -> HOME_RUN
  * the ATR trail ratchet must never move the SL backwards
  * "close worst / hold layers" must not overwrite a BANK_TP1 target with TP3
  * "close worst / hold layers" must skip a layer inside the broker stops level

All tests are deterministic: no wall-clock, no network, no live MT5 connection.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import config
import dynamic_risk
import main
import mt5_executor as ex_mod
import parser
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

CHAT_ID = -1004369388188


def _make_tick(bid: float, ask: float, ts: float = 1794886034.0) -> SimpleNamespace:
    return SimpleNamespace(bid=bid, ask=ask, time=ts)


def _sym_info(digits: int = 2, point: float = 0.01, stops_level: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        digits=digits,
        point=point,
        spread=20,
        trade_mode=4,
        filling_mode=2,
        visible=True,
        trade_stops_level=stops_level,
        trade_freeze_level=0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )


def _reset_runtime(tmp_path, tag: str) -> None:
    for k in list(main._pending_sell_now_tasks.keys()):
        main._cancel_delayed_sell_now(k)
    main._pending_task_chats.clear()
    main._recent_messages.clear()
    main._recent_message_texts.clear()
    main._deleted_messages.clear()
    main._signal_locks.clear()
    main._runner_save_throttle_ts.clear()
    main.TRADING_HALTED = False
    main.CLOSE_ON_SIGNAL_DELETED = True
    main.state_mgr = StateManager(str(tmp_path / f"{tag}_state.json"))
    main.trade_ledger = TradeLedger(str(tmp_path / f"{tag}_ledger.jsonl"))


class FakeMsg:
    def __init__(self, msg_id, text, reply_to_msg_id=None, date=None, edit_date=None):
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
    def __init__(self, msg_id, text, chat_id=CHAT_ID, reply_to=None, date=None, edit_date=None):
        self.message = FakeMsg(msg_id, text, reply_to_msg_id=reply_to, date=date, edit_date=edit_date)
        self.chat_id = chat_id
        self.is_private = False
        self.is_reply = reply_to is not None
        self.replies = []

    async def reply(self, txt: str):
        self.replies.append(txt)


def _pin_progressive_trail_policy(monkeypatch) -> None:
    """Pin the spec ladder (1.40 / 1.00 @ $10 / 0.80 @ $18) with the v3.9.0 per-stage clamps."""
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MULT", 1.40, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", 10.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MULT", 1.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS", 18.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MULT", 0.80, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50, raising=False)
    # v3.9.0: Stage A cap 12.00 -> 8.00; EXTENDED/HOME_RUN own tighter clamps.
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 8.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS", 2.20, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS", 5.50, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS", 2.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS", 4.00, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25, raising=False)


# ---------------------------------------------------------------------------
# Msg 10180 - 1. stub -> zone edit applies ADAPTIVE ATR scaling
# ---------------------------------------------------------------------------

async def _drive_stub_to_zone_edit(tmp_path, monkeypatch, msg_id=9401):
    """
    Set up a bare "Buy xauusd now" stub signal, edit it into a full zone
    (SL + TP1..TP5) and run the real edit handler.

    Returns (signal, scale_calls) where scale_calls records every
    apply_dynamic_atr_sl_tp invocation made by the edit path.
    """
    monkeypatch.setattr(config, "DYNAMIC_SLTP_MODE", "ADAPTIVE", raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_SLTP_ENABLED", True, raising=False)

    _reset_runtime(tmp_path, "stub_zone")
    main.executor = MT5Executor(dry_run=True)
    main.executor.connect()

    stub_text = "Buy xauusd now"
    zone_text = (
        "Buy xauusd 3895-3887\n\n"
        "Sl 3880\n"
        "Tp 3900\nTp 3910\nTp 3920\nTp 3930\nTp 3940"
    )

    # The stub: an instant-now market entry with the synthetic fallback TP ladder that
    # the copier attaches to an unpriced "now" signal (no zone, no dynamic-risk snapshot).
    main.state_mgr.create_signal(
        message_id=msg_id,
        chat_id=CHAT_ID,
        symbol="XAUUSD.s",
        direction="BUY",
        layers={
            "L1": {
                "lot": 0.01,
                "target_price": None,
                "tp": 3899.50,
                "tp_stage": "TP1",
                "ticket": 4001,
                "order_type": "MARKET",
                "status": "OPEN",
            }
        },
        market_tickets=[4001],
        pending_tickets=[],
        zone=None,
        zone_completed=False,
        is_zone=False,
        is_instant_now=True,
        max_orders=1,
        sl=None,
        raw_tp1=3900.0,
        raw_tp2=3910.0,
        raw_tp3=3920.0,
        raw_tp4=3930.0,
        raw_tp5=3940.0,
        tp_levels={"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
        final_tp=3899.50,
        fallback_tp_applied=True,
        initial_price=3891.00,
        recorded_entry_price=3891.00,
        raw_text=stub_text,
        timestamp=(datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),
    )

    live_tick = _make_tick(3892.00, 3892.20)
    scale_calls = []
    orig_scale = main.executor.apply_dynamic_atr_sl_tp

    def _spy_scale(*args, **kwargs):
        scale_calls.append(kwargs)
        return orig_scale(*args, **kwargs)

    evt_edit = FakeEvent(
        msg_id,
        zone_text,
        chat_id=CHAT_ID,
        date=datetime.now(timezone.utc) - timedelta(minutes=4),
        edit_date=datetime.now(timezone.utc),
    )

    with patch.object(main, "notify_saved_messages", new_callable=AsyncMock), patch.object(
        main.executor, "get_tick", return_value=live_tick
    ), patch.object(main.executor, "get_atr", return_value=8.00), patch.object(
        main.executor, "apply_dynamic_atr_sl_tp", side_effect=_spy_scale
    ), patch.object(main.executor, "modify_position_sltp", return_value=True), patch.object(
        main.executor, "place_limit_order", return_value=4102
    ):
        await main.handle_message_edited(evt_edit)

    sig = main.state_mgr.get_signal(msg_id, chat_id=CHAT_ID)
    assert sig is not None, "the edited stub signal must still exist"
    return sig, scale_calls, zone_text


def _assert_stub_edit_scaled_like_a_fresh_signal(sig, scale_calls, zone_text):
    """Shared assertions for the stub -> zone edit scaling behaviour."""

    # 1) The edit was routed through the ATR scaler exactly once ...
    assert len(scale_calls) == 1, f"expected one apply_dynamic_atr_sl_tp call, got {len(scale_calls)}"
    # 2) ... with usable anchors ...
    assert scale_calls[0]["sl_anchor"] > 0.0 and scale_calls[0]["tp_anchor"] > 0.0
    dr = sig.get("dynamic_risk")
    assert isinstance(dr, dict) and dr.get("applied") is True, f"dynamic_risk not applied: {dr}"
    assert dr.get("mode") == "ADAPTIVE", f"expected ADAPTIVE mode, got {dr.get('mode')}"
    # 3) ... and the scaled snapshot is persisted on the signal.
    assert sig.get("edit_dynamic_risk_scaled") is True

    # 4) Equivalence: the anchors used by the edit are exactly the anchors the fresh
    #    zone path uses, and therefore the scaled levels are identical.
    parsed = parser.parse_message(zone_text, is_edit=True, live_price=3892.00)
    assert parsed is not None and parsed.zone is not None
    fresh_sl_anchor, fresh_tp_anchor = main.executor.compute_zone_atr_anchors(
        symbol="XAUUSD.s",
        direction="BUY",
        zone=parsed.zone,
        final_tp=parsed.final_tp,
        raw_tp1=parsed.raw_tp1,
        pip_size=0.10,
        digits=2,
    )
    assert scale_calls[0]["sl_anchor"] == pytest.approx(fresh_sl_anchor)
    assert scale_calls[0]["tp_anchor"] == pytest.approx(fresh_tp_anchor)

    with patch.object(main.executor, "get_atr", return_value=8.00):
        fresh_snap = main.executor.apply_dynamic_atr_sl_tp(
            symbol="XAUUSD.s",
            direction="BUY",
            sl_anchor=fresh_sl_anchor,
            tp_anchor=fresh_tp_anchor,
            sl=parsed.sl,
            raw_tp1=parsed.raw_tp1,
            tp_levels=parsed.tp_levels,
            final_tp=parsed.final_tp,
            pip_size=0.10,
            digits=2,
        )
    assert fresh_snap["applied"] is True
    # The edit must land on exactly the same scaled levels as a fresh signal.
    assert sig["sl"] == pytest.approx(fresh_snap["sl"])
    assert sig["raw_tp1"] == pytest.approx(fresh_snap["raw_tp1"])
    assert sig["final_tp"] == pytest.approx(fresh_snap["final_tp"])
    # Sanity: the scaling really did move the levels (ATR 8.00 -> vol_ratio 1.75).
    assert fresh_snap["vol_ratio"] == pytest.approx(1.75)
    assert fresh_snap["raw_tp1"] != pytest.approx(parsed.raw_tp1)


@pytest.mark.asyncio
async def test_stub_edit_to_zone_applies_adaptive_atr_scaling(tmp_path, monkeypatch):
    """
    A bare "Buy xauusd now" stub edited into a full zone (SL + TP1..TP5) must be routed
    through apply_dynamic_atr_sl_tp in ADAPTIVE mode, and the edit must scale the
    levels EXACTLY like an identical fresh zone signal.

    Equivalence anchor: MT5Executor.compute_zone_atr_anchors is the single source of
    truth for the zone ATR anchors (shared with execute_4layer_zone), so the edit path
    and the fresh-signal path must agree on (sl_anchor, tp_anchor) and therefore on the
    scaled SL / TP1 / final TP.

    Regression guard: this test also fails loudly (NameError inside the edit handler) if
    main.py ever stops importing TP1_CLOSE_PROXIMITY_PIPS again - see
    test_main_imports_tp1_close_proximity_pips.
    """
    sig, scale_calls, zone_text = await _drive_stub_to_zone_edit(tmp_path, monkeypatch)
    _assert_stub_edit_scaled_like_a_fresh_signal(sig, scale_calls, zone_text)


def test_main_imports_tp1_close_proximity_pips():
    """
    Regression guard for the production bug found by this suite: the stub -> zone edit
    path recomputes the proximity-close price from TP1_CLOSE_PROXIMITY_PIPS
    (main.py, in the Upgrade-4 ADAPTIVE scaling block), but main.py originally never
    imported that constant, so ANY edit that actually applied ATR scaling died with
    NameError before the scaled levels were persisted.

    The constant lives in config.py; main.py must import it.
    """
    assert hasattr(main, "TP1_CLOSE_PROXIMITY_PIPS"), (
        "main.py must import TP1_CLOSE_PROXIMITY_PIPS: the stub->zone edit path uses it "
        "to recompute the scaled proximity-close price, and an undefined name there "
        "aborts the whole edit with NameError"
    )
    assert main.TP1_CLOSE_PROXIMITY_PIPS == config.TP1_CLOSE_PROXIMITY_PIPS


# ---------------------------------------------------------------------------
# Msg 10180 - 2. the deepest fill becomes CORE_RUNNER (never L1)
# ---------------------------------------------------------------------------

def test_l4_deep_fill_is_promoted_to_core_runner_not_l1():
    """With L1..L4 all filled, CORE_RUNNER is the deepest/best fill (L4), not L1."""
    ex = MT5Executor(dry_run=True)
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "ACTIVE",
        "layers": {
            "L1": {"lot": 0.01, "target_price": 3895.00, "tp": 3899.50, "tp_stage": "TP1", "ticket": 101, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3892.33, "tp": 3899.50, "tp_stage": "TP1", "ticket": 102, "order_type": "LIMIT", "status": "OPEN"},
            "L3": {"lot": 0.01, "target_price": 3889.67, "tp": 3899.50, "tp_stage": "TP1", "ticket": 103, "order_type": "LIMIT", "status": "OPEN"},
            "L4": {"lot": 0.02, "target_price": 3887.00, "tp": 3899.50, "tp_stage": "TP1", "ticket": 104, "order_type": "LIMIT", "status": "FILLED"},
        },
        "market_tickets": [101, 102, 103, 104],
        "pending_tickets": [],
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
    }
    roles = ex.assign_or_preserve_layer_roles(sig)

    assert roles["L4"] == "CORE_RUNNER", f"deepest fill must be CORE_RUNNER, got {roles}"
    assert roles["L1"] != "CORE_RUNNER"
    assert sig["runner_layers"] == ["L4"], f"runner must be L4, got {sig.get('runner_layers')}"
    assert "LATE_RECOVERY" not in roles.values(), f"no layer may be demoted to LATE_RECOVERY: {roles}"


# ---------------------------------------------------------------------------
# Msg 10180 - 3. a premature TP lift must not lock the roles
# ---------------------------------------------------------------------------

def test_premature_tp_lift_does_not_lock_roles():
    """
    A TP1 -> TP2 lift that happens BEFORE the BE lock must not set roles_locked,
    so a later "Extend SL" edit is not blocked by the forward-only SL guard.
    """
    ex = MT5Executor(dry_run=True)
    sig = {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "ACTIVE",
        "layers": {
            # L1 was lifted to TP2 by ATR proximity while TP1 has NOT been cleared and
            # no BE lock has been applied.
            # The layers carry production-like roles on purpose: the auto-lock guard in
            # assign_or_preserve_layer_roles only fires for a layer whose tp_stage is not
            # TP1 *and* whose role is not empty, so a role-less fixture would let this
            # test pass on the broken (pre-fix) code as well.
            "L1": {"lot": 0.01, "target_price": 3895.00, "tp": 3910.00, "tp_stage": "TP2", "role": "CORE_RUNNER", "ticket": 101, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "target_price": 3892.33, "tp": 3899.50, "tp_stage": "TP1", "role": "BANK_TP1", "ticket": 102, "order_type": "LIMIT", "status": "PENDING"},
            "L3": {"lot": 0.01, "target_price": 3889.67, "tp": 3899.50, "tp_stage": "TP1", "role": "BANK_TP1", "ticket": 103, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "target_price": 3887.00, "tp": 3899.50, "tp_stage": "TP1", "role": "BANK_TP1", "ticket": 104, "order_type": "LIMIT", "status": "PENDING"},
        },
        "market_tickets": [101],
        "pending_tickets": [102, 103, 104],
        "tp_levels": {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
    }

    ex.assign_or_preserve_layer_roles(sig)
    assert sig.get("roles_locked") in (None, False), (
        f"a pre-BE TP lift must not lock roles, got roles_locked={sig.get('roles_locked')}"
    )

    # The forward-only SL guard must therefore still accept an "Extend SL" edit.
    candidate_sl = 3880.0
    resolved = main._resolve_edit_layer_sl(sig, sig["layers"]["L1"], candidate_sl, "BUY")
    assert resolved == pytest.approx(candidate_sl), (
        "a pre-BE TP lift must not block a later SL edit"
    )

    # Control: once the roles ARE locked (BE lock / TP1 cleared), the same edit IS blocked.
    locked_sig = dict(sig)
    locked_sig["roles_locked"] = True
    locked_sig["layers"] = {"L1": dict(sig["layers"]["L1"], sl=3890.00)}
    assert main._resolve_edit_layer_sl(locked_sig, locked_sig["layers"]["L1"], candidate_sl, "BUY") is None


# ---------------------------------------------------------------------------
# Msg 10180 - 4. TP1 -> TP2 lift needs 50 % leg progress unless be_locked
# ---------------------------------------------------------------------------

def test_tp1_lift_requires_50pct_progress_or_be_locked(monkeypatch):
    """compute_dynamic_tp_step refuses the TP1 -> TP2 ATR-proximity lift below 50 % progress."""
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT", 0.50, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP_LIFT_ATR_MULT", 0.50, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT", 0.75, raising=False)
    policy = dynamic_risk.read_dynamic_risk_policy(config)

    tp_levels = {"TP1": 110.0, "TP2": 120.0, "TP3": 130.0}

    def _step(live_price, be_locked):
        return dynamic_risk.compute_dynamic_tp_step(
            direction="BUY",
            entry_price=100.0,
            live_price=live_price,
            peak_price=live_price,
            atr=12.0,
            tp_levels=tp_levels,
            current_stage_idx=1,
            max_stage=3,
            cleared_milestones=[],
            policy=policy,
            digits=2,
            stops_buffer=0.0,
            be_locked=be_locked,
        )

    # 40 % of the TP1 leg (entry 100 -> TP1 110); price 104 is 6.0 away from TP1 which is
    # inside the ATR-proximity window (0.50 x ATR 12.0 = 6.0).
    below = _step(104.0, False)
    assert below["should_lift"] is False, f"TP1 lift must be refused below 50 % progress: {below}"
    assert below["target_stage_name"] == "TP1"

    # Same situation but the layer is BE-locked -> the lift is allowed.
    allowed_be = _step(104.0, True)
    assert allowed_be["should_lift"] is True, f"BE-locked layer may lift: {allowed_be}"
    assert allowed_be["target_stage_name"] == "TP2"
    assert allowed_be["target_tp"] == pytest.approx(120.0)

    # Exactly 50 % of the leg -> allowed without a BE lock (threshold is inclusive).
    at_threshold = _step(105.0, False)
    assert at_threshold["should_lift"] is True, f"50 % progress must lift: {at_threshold}"
    assert at_threshold["target_stage_name"] == "TP2"


# ---------------------------------------------------------------------------
# Msg 10180 - 5. monotonic TP sanitizer drops the 3437 typo
# ---------------------------------------------------------------------------

def test_monotonic_sanitizer_drops_3437_typo():
    """SELL [3568, 3555, 3437, 3520, 3500] -> kept [3568, 3555, 3520, 3500], dropped [3437]."""
    kept, dropped = parser.sanitize_monotonic_tps([3568.0, 3555.0, 3437.0, 3520.0, 3500.0], direction="SELL")
    assert kept == [3568.0, 3555.0, 3520.0, 3500.0]
    assert dropped == [3437.0]

    labeled = {"TP1": 3568.0, "TP2": 3555.0, "TP3": 3437.0, "TP4": 3520.0, "TP5": 3500.0}
    clean, dropped_lab = parser._sanitize_labeled_tps_monotonic(labeled, "SELL")
    assert dropped_lab == [3437.0]
    # Strictly ordered and contiguously labelled TP1..TP4.
    assert clean == {"TP1": 3568.0, "TP2": 3555.0, "TP3": 3520.0, "TP4": 3500.0}
    assert list(clean.keys()) == ["TP1", "TP2", "TP3", "TP4"]
    prices = [clean[k] for k in sorted(clean, key=lambda s: int(s[2:]))]
    assert prices == sorted(prices, reverse=True), "a SELL ladder must stay strictly descending"

    # The stage-preserving view keeps the provider stage identity (a TP2 stays a TP2).
    stages, _ = parser._sanitize_labeled_tps_monotonic_stages({"TP2": 3900.0, "TP4": 3880.0}, "SELL")
    assert stages == {"TP2": 3900.0, "TP4": 3880.0}


# ---------------------------------------------------------------------------
# Msg 10196 - 6. future-tense chatter is ignored
# ---------------------------------------------------------------------------

def test_future_tense_gonna_chatter_is_ignored():
    """Future-tense chatter must never be classified as a trade-management action."""
    chatter = "I'm gonna go breakeven early and close worst soon"
    assert parser.parse_message(chatter) is None
    assert parser.gate_3_classify_trade_management(chatter, is_reply=True) is None

    # Same management wording, but without the future-tense token -> still a command.
    cmd = "close worst entries now and hold layers"
    action = parser.parse_message(cmd)
    assert action is not None, "the plain command must still parse"
    assert action.management_action == "ACTION_CLOSE_WORST_HOLD_LAYERS"
    assert parser.gate_3_classify_trade_management(cmd, is_reply=True).management_action == (
        "ACTION_CLOSE_WORST_HOLD_LAYERS"
    )

    # The very same command prefixed with a future-tense token is dropped.
    assert parser.parse_message("I'm gonna close worst entries now and hold layers") is None


# ---------------------------------------------------------------------------
# Msg 10196 - 7. progressive trail zones
# ---------------------------------------------------------------------------

def test_progressive_trail_tightening_zones(monkeypatch):
    """MFE < $10 -> 1.40x STANDARD; $10..$18 -> 1.00x EXTENDED; >= $18 -> 0.80x HOME_RUN."""
    _pin_progressive_trail_policy(monkeypatch)
    policy = dynamic_risk.read_dynamic_risk_policy(config)

    assert dynamic_risk.select_progressive_trail_mult(0.00, policy) == (1.40, "STANDARD")
    assert dynamic_risk.select_progressive_trail_mult(9.99, policy) == (1.40, "STANDARD")
    assert dynamic_risk.select_progressive_trail_mult(10.00, policy) == (1.00, "EXTENDED")
    assert dynamic_risk.select_progressive_trail_mult(17.99, policy) == (1.00, "EXTENDED")
    assert dynamic_risk.select_progressive_trail_mult(18.00, policy) == (0.80, "HOME_RUN")
    assert dynamic_risk.select_progressive_trail_mult(100.00, policy) == (0.80, "HOME_RUN")

    def _trail(entry, mfe, atr):
        peak = entry + mfe
        return dynamic_risk.compute_dynamic_atr_trail_stop(
            direction="BUY",
            entry_price=entry,
            live_price=peak,
            prev_peak=peak,
            current_sl=0.0,
            atr=atr,
            policy=policy,
            digits=2,
        )

    # M5 ATR 4.00 -> trail_dist = 4.00 x zone multiplier.
    std = _trail(3850.0, 5.00, 4.00)
    assert std["trail_zone"] == "STANDARD" and std["trail_mult"] == pytest.approx(1.40)
    assert std["trail_dist"] == pytest.approx(5.60)

    ext = _trail(3850.0, 12.00, 4.00)
    assert ext["trail_zone"] == "EXTENDED" and ext["trail_mult"] == pytest.approx(1.00)
    assert ext["trail_dist"] == pytest.approx(4.00)

    home = _trail(3850.0, 20.00, 4.00)
    assert home["trail_zone"] == "HOME_RUN" and home["trail_mult"] == pytest.approx(0.80)
    assert home["trail_dist"] == pytest.approx(3.20)

    # ATR-relative stages: at ATR 0.50 the thresholds are min($10, 2.5 x ATR) = $1.25
    # and min($18, 4.0 x ATR) = $2.00, so even a $5.00 MFE is already HOME_RUN in a
    # calm session. Each zone still honours its OWN floor:
    # STANDARD $2.50, EXTENDED $2.20, HOME_RUN $2.00.
    for mfe, zone, mult, floor in ((5.00, "HOME_RUN", 0.80, 2.00), (12.00, "HOME_RUN", 0.80, 2.00), (20.00, "HOME_RUN", 0.80, 2.00)):
        res = _trail(3850.0, mfe, 0.50)
        assert res["trail_zone"] == zone
        assert res["trail_mult"] == pytest.approx(mult)
        assert res["trail_dist"] == pytest.approx(floor), f"{zone} must honour the ${floor:.2f} floor"


# ---------------------------------------------------------------------------
# Msg 10196 - 8. the trail never moves the SL backwards
# ---------------------------------------------------------------------------

def test_progressive_trail_never_moves_sl_backwards(monkeypatch):
    """The ATR trail ratchet is monotonic forward across all three zone transitions."""
    _pin_progressive_trail_policy(monkeypatch)
    policy = dynamic_risk.read_dynamic_risk_policy(config)

    entry = 3850.0
    atr = 4.00
    prev_sl = None
    prev_mult = None
    # The trail only arms once MFE >= trail_activation_mult (1.00) x ATR (4.00) = $4.00.
    for step in range(16, 301):
        mfe = step * 0.25
        peak = entry + mfe
        res = dynamic_risk.compute_dynamic_atr_trail_stop(
            direction="BUY",
            entry_price=entry,
            live_price=peak,
            prev_peak=peak,
            current_sl=0.0,
            atr=atr,
            policy=policy,
            digits=2,
        )
        assert res["trail_armed"] is True
        assert res["atr_sl"] is not None
        if prev_sl is not None:
            assert res["atr_sl"] >= prev_sl - 1e-9, (
                f"ATR trail moved backwards at MFE={mfe}: {res['atr_sl']} < {prev_sl}"
            )
            assert res["trail_mult"] <= prev_mult + 1e-9, (
                f"zone multiplier increased at MFE={mfe}: {res['trail_mult']} > {prev_mult}"
            )
        prev_sl = res["atr_sl"]
        prev_mult = res["trail_mult"]

    # Explicitly pin the three zone transitions on the BUY side.
    at_standard = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY", entry_price=entry, live_price=3859.99, prev_peak=3859.99,
        current_sl=0.0, atr=atr, policy=policy, digits=2,
    )
    at_extended = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY", entry_price=entry, live_price=3860.00, prev_peak=3860.00,
        current_sl=0.0, atr=atr, policy=policy, digits=2,
    )
    at_homerun = dynamic_risk.compute_dynamic_atr_trail_stop(
        direction="BUY", entry_price=entry, live_price=3868.00, prev_peak=3868.00,
        current_sl=0.0, atr=atr, policy=policy, digits=2,
    )
    assert at_standard["trail_zone"] == "STANDARD"
    assert at_extended["trail_zone"] == "EXTENDED"
    assert at_homerun["trail_zone"] == "HOME_RUN"
    assert at_standard["atr_sl"] < at_extended["atr_sl"] < at_homerun["atr_sl"]


# ---------------------------------------------------------------------------
# Msg 10196 - 9/10. "close worst / hold layers" BE protection
# ---------------------------------------------------------------------------

def _mt5_mocks(positions, tick, sym_info=None):
    sym_obj = sym_info or _sym_info()

    def _positions_get(*args, **kwargs):
        ticket = kwargs.get("ticket")
        if ticket is not None:
            return [p for p in positions if p.ticket == ticket]
        return list(positions)

    return [
        patch.object(ex_mod.mt5, "symbol_info", return_value=sym_obj),
        patch.object(ex_mod.mt5, "symbol_select", return_value=True),
        patch.object(ex_mod.mt5, "symbol_info_tick", return_value=tick),
        patch.object(ex_mod.mt5, "positions_get", side_effect=_positions_get),
        patch.object(ex_mod.mt5, "last_error", return_value=(1, "OK")),
    ]


def _position(ticket, price_open, profit, sl=0.0, tp=0.0, price_current=0.0, pos_type=0):
    return SimpleNamespace(
        ticket=ticket,
        price_open=price_open,
        price_current=price_current or price_open,
        profit=profit,
        sl=sl,
        tp=tp,
        type=pos_type,
        volume=0.01,
        symbol="XAUUSD.s",
    )


def test_close_worst_hold_layers_does_not_overwrite_bank_tp1_with_tp3():
    """
    "close worst / hold layers" may only move the worst entry to BE; it must never
    push that (BANK_TP1) layer broker TP forward to TP3.
    """
    ex = MT5Executor(dry_run=True)
    ex.dry_run = False

    tick = _make_tick(3900.00, 3900.20)
    positions = [
        _position(701, 3895.00, profit=25.0, sl=3890.00, tp=3904.50),
        _position(702, 3892.00, profit=10.0, sl=3887.00, tp=3901.50),
        # Worst entry: its broker TP is still the BANK_TP1 target (TP1 = 3904.50).
        _position(703, 3890.00, profit=-20.0, sl=3885.00, tp=3904.50),
    ]

    modify_calls = []

    def _spy_modify(ticket, sl=None, tp=None):
        modify_calls.append((ticket, sl, tp))
        return True

    mocks = _mt5_mocks(positions, tick)
    with patch.object(ex, "modify_position_sltp", side_effect=_spy_modify):
        for p in mocks:
            p.start()
        try:
            result = ex.apply_breakeven_worst_hold_layers(
                market_tickets=[701, 702, 703],
                symbol="XAUUSD.s",
                direction="BUY",
                be_offset_pips=5.0,
                raw_tp3=3920.0,
            )
        finally:
            for p in mocks:
                p.stop()

    assert result["be_tickets"] == [703]
    assert result["held_tickets"] == [701, 702]
    assert result["failed_be_tickets"] == []

    assert len(modify_calls) == 1
    ticket, sl, tp = modify_calls[0]
    assert ticket == 703
    # The SL moved to breakeven ...
    assert sl == pytest.approx(3890.50)
    # ... but the TP is the untouched BANK_TP1 target, NOT the TP3 that was offered.
    assert tp == pytest.approx(3904.50)
    assert tp != pytest.approx(3920.0)


def test_close_worst_hold_layers_rejects_underwater_be_move():
    """
    A worst entry whose BE stop sits inside the broker stops level is skipped, the
    reason is reported, and no modify_position_sltp call is made for it.
    """
    ex = MT5Executor(dry_run=True)
    ex.dry_run = False

    # Live bid 3900.50; the worst entry opened at 3900.40 -> BE SL 3900.90 is above the
    # market and only 0.40 away, i.e. inside trade_stops_level (50 pts * 0.01 = 0.50).
    tick = _make_tick(3900.50, 3900.70)
    positions = [
        _position(801, 3895.00, profit=25.0, sl=3890.00, tp=3904.50),
        _position(802, 3892.00, profit=10.0, sl=3887.00, tp=3901.50),
        _position(803, 3900.40, profit=-20.0, sl=3895.00, tp=3904.50),
    ]

    modify_calls = []
    mocks = _mt5_mocks(positions, tick)
    with patch.object(ex, "modify_position_sltp", side_effect=lambda **kw: modify_calls.append(kw) or True):
        for p in mocks:
            p.start()
        try:
            result = ex.apply_breakeven_worst_hold_layers(
                market_tickets=[801, 802, 803],
                symbol="XAUUSD.s",
                direction="BUY",
                be_offset_pips=5.0,
                raw_tp3=3920.0,
            )
        finally:
            for p in mocks:
                p.stop()

    assert result["be_tickets"] == []
    assert result["held_tickets"] == [801, 802]
    assert len(result["failed_be_tickets"]) == 1
    failed_ticket, reason = result["failed_be_tickets"][0]
    assert failed_ticket == 803
    assert "stops_level" in reason, f"the rejection must carry a reason: {reason!r}"
    # No broker call for the skipped layer.
    assert modify_calls == [], f"no SL/TP modification may be sent for a rejected layer: {modify_calls}"


# ---------------------------------------------------------------------------
# Fix wave - I-1: an explicit TP edit on a live signal must never be discarded
# ---------------------------------------------------------------------------

def test_explicit_tp_edit_on_live_signal_is_not_discarded():
    """
    A TP edit on a running signal is the provider's current intent and must be honoured.
    The strict-monotonicity enforcement may only displace the stale pre-existing stage
    that conflicts with the edit - never the edit itself.
    """
    existing = {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}
    step = 100.0  # pips; 100 pips * 0.10 pip size = 10.00 per stage
    cases = [
        # (explicit edit, expected merged ladder)
        (
            {"TP3": 3950.0},
            {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3950.0, "TP4": 3960.0, "TP5": 3970.0},
        ),
        (
            {"TP1": 3960.0},
            {"TP1": 3960.0, "TP2": 3970.0, "TP3": 3980.0, "TP4": 3990.0, "TP5": 4000.0},
        ),
        (
            {"TP2": 3950.0},
            {"TP1": 3900.0, "TP2": 3950.0, "TP3": 3960.0, "TP4": 3970.0, "TP5": 3980.0},
        ),
        (
            {"TP5": 3960.0},
            {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3960.0},
        ),
        (
            {"TP1": 3850.0},
            {"TP1": 3850.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0},
        ),
    ]
    for edit, expected in cases:
        merged = parser.merge_tp_levels_for_active_signal(
            dict(existing),
            edit,
            direction="BUY",
            pip_size=0.10,
            step_pips=[step, step, step, step],
        )
        assert merged == expected, (
            f"explicit TP edit {edit} was not honoured: got {merged}, expected {expected}"
        )
        prices = [merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged]
        assert prices == sorted(prices), (
            f"the merged TP ladder must stay strictly ordered: {merged}"
        )

    # The same five edits mirrored onto a SELL ladder (the ladder descends).
    sell_existing = {
        "TP1": 3900.0, "TP2": 3890.0, "TP3": 3880.0, "TP4": 3870.0, "TP5": 3860.0,
    }
    sell_cases = [
        (
            {"TP3": 3850.0},
            {"TP1": 3900.0, "TP2": 3890.0, "TP3": 3850.0, "TP4": 3840.0, "TP5": 3830.0},
        ),
        (
            {"TP1": 3840.0},
            {"TP1": 3840.0, "TP2": 3830.0, "TP3": 3820.0, "TP4": 3810.0, "TP5": 3800.0},
        ),
        (
            {"TP2": 3850.0},
            {"TP1": 3900.0, "TP2": 3850.0, "TP3": 3840.0, "TP4": 3830.0, "TP5": 3820.0},
        ),
        (
            {"TP5": 3840.0},
            {"TP1": 3900.0, "TP2": 3890.0, "TP3": 3880.0, "TP4": 3870.0, "TP5": 3840.0},
        ),
        (
            {"TP1": 3960.0},
            {"TP1": 3960.0, "TP2": 3890.0, "TP3": 3880.0, "TP4": 3870.0, "TP5": 3860.0},
        ),
    ]
    for edit, expected in sell_cases:
        merged = parser.merge_tp_levels_for_active_signal(
            dict(sell_existing),
            edit,
            direction="SELL",
            pip_size=0.10,
            step_pips=[step, step, step, step],
        )
        assert merged == expected, (
            f"explicit SELL TP edit {edit} was not honoured: got {merged}, "
            f"expected {expected}"
        )
        prices = [merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged]
        assert prices == sorted(prices, reverse=True), (
            f"the merged SELL TP ladder must stay strictly ordered: {merged}"
        )


# ---------------------------------------------------------------------------
# Fix wave - M-1: the stage-preserving view must not relabel TP2 onto TP1
# ---------------------------------------------------------------------------

def test_stage_preserving_view_never_relabels_tp2_onto_tp1():
    """
    The stage-preserving view keeps the provider's original stage labels, so a TP1 that
    the sanitizer drops can never shift the original TP2 onto TP1.
    """
    stages, dropped = parser._sanitize_labeled_tps_monotonic_stages(
        {"TP1": 4000.0, "TP2": 3900.0, "TP3": 3910.0, "TP4": 3950.0},
        "BUY",
    )
    assert "TP1" not in stages, f"TP1 must not be re-used for another stage: {stages}"
    assert stages == {"TP2": 3900.0, "TP3": 3910.0, "TP4": 3950.0}
    assert dropped == [4000.0]

# ---------------------------------------------------------------------------
# Fix wave round 2 - an explicit TP edit may never rewrite an UNEDITED stage
# ---------------------------------------------------------------------------

def test_out_of_order_explicit_tp_edit_is_dropped(caplog):
    """
    An explicit edit that can only be honoured by pushing an UNEDITED stage backwards
    is an out-of-order provider typo: it must be dropped with a WARNING that names the
    stage and its price, leaving the live ladder untouched.
    """
    existing = {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}
    step = [100.0, 100.0, 100.0, 100.0]
    cases = [
        ({"TP3": 3895.0}, "TP3", 3895.0),   # below TP1 - would rewrite TP1/TP2 downwards
        ({"TP2": 3899.0}, "TP2", 3899.0),   # below TP1 - would rewrite TP1 downwards
    ]
    for edit, stage, price in cases:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="copier.parser"):
            merged = parser.merge_tp_levels_for_active_signal(
                dict(existing),
                edit,
                direction="BUY",
                pip_size=0.10,
                step_pips=step,
            )
        assert merged == existing, (
            f"the out-of-order edit {edit} must not rewrite the live ladder: got {merged}"
        )
        warnings = [
            rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
        ]
        assert warnings, f"dropping {edit} must be logged"
        assert any(
            stage in message and f"{price:.2f}" in message for message in warnings
        ), f"the warning must name the dropped stage and its price: {warnings}"


def test_single_explicit_edit_never_rewrites_an_unedited_stage_backwards(caplog):
    """
    Property test over 10 000 randomised single-edit merges.

    P1 - no UNEDITED stage ever moves against the trade direction: a stage the
         provider did not explicitly edit keeps a value >= its pre-existing value on a
         BUY (<= on a SELL). Only the explicitly edited stage is exempt.
    P2 - the merged ladder is strictly ordered, or the edit was dropped with a
         WARNING and the pre-existing ladder was kept unchanged.
    """
    rng = random.Random(20251120)
    step = [100.0, 100.0, 100.0, 100.0]

    with caplog.at_level(logging.WARNING, logger="copier.parser"):
        for case in range(10000):
            direction = "BUY" if rng.random() < 0.5 else "SELL"
            sign = 1.0 if direction == "BUY" else -1.0
            n_stages = rng.randint(1, 5)
            base_price = rng.uniform(3000.0, 4000.0)
            gap = rng.uniform(1.0, 60.0)
            existing = {
                f"TP{i + 1}": round(base_price + sign * i * gap, 2) for i in range(n_stages)
            }
            stage = rng.randint(1, 5)
            low = min(existing.values())
            high = max(existing.values())
            mode = rng.random()
            if mode < 0.34:
                # below the ladder range (against the trade direction)
                price = round(
                    (low - rng.uniform(0.1, 120.0))
                    if direction == "BUY"
                    else (high + rng.uniform(0.1, 120.0)),
                    2,
                )
            elif mode < 0.67:
                # inside the ladder range
                price = round(rng.uniform(low, high), 2)
            else:
                # above the ladder range
                price = round(
                    (high + rng.uniform(0.1, 120.0))
                    if direction == "BUY"
                    else (low - rng.uniform(0.1, 120.0)),
                    2,
                )
            edit = {f"TP{stage}": price}

            caplog.clear()
            merged = parser.merge_tp_levels_for_active_signal(
                dict(existing),
                edit,
                direction=direction,
                pip_size=0.10,
                step_pips=step,
            )
            context = (
                f"case {case}: direction={direction} existing={existing} "
                f"edit={edit} merged={merged}"
            )

            # P1 - no unedited stage moves against the trade direction.
            for i in range(1, 6):
                key = f"TP{i}"
                if i == stage:
                    continue  # the explicitly edited stage is exempt
                previous = existing.get(key)
                if previous is None:
                    continue
                current = merged.get(key)
                if current is None:
                    continue
                if direction == "BUY":
                    assert current >= previous - 1e-9, (
                        f"P1 violated: the unedited stage {key} moved backwards from "
                        f"{previous} to {current} ({context})"
                    )
                else:
                    assert current <= previous + 1e-9, (
                        f"P1 violated: the unedited stage {key} moved backwards from "
                        f"{previous} to {current} ({context})"
                    )

            # P2 - the merged ladder is strictly ordered, or the edit was dropped.
            prices = [merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged]
            ordered = (
                all(b > a for a, b in zip(prices, prices[1:]))
                if direction == "BUY"
                else all(b < a for a, b in zip(prices, prices[1:]))
            )
            honoured = merged.get(f"TP{stage}") == price
            if not ordered:
                baseline = parser.merge_tp_levels_for_active_signal(
                    dict(existing),
                    None,
                    direction=direction,
                    pip_size=0.10,
                    step_pips=step,
                )
                assert merged == baseline, (
                    f"P2 violated: the ladder is not ordered and the edit was not "
                    f"dropped ({context})"
                )
                assert not honoured, (
                    f"P2 violated: the edit was honoured but the ladder is not ordered "
                    f"({context})"
                )
            if not honoured:
                # every drop must be logged, naming the stage and its price
                warnings = [
                    rec.getMessage()
                    for rec in caplog.records
                    if rec.levelno >= logging.WARNING
                ]
                assert any(
                    f"TP{stage}" in message and f"{price:.2f}" in message
                    for message in warnings
                ), (
                    f"the dropped edit must be logged with its stage and price: "
                    f"{warnings} ({context})"
                )


def test_conflicting_explicit_tp_edits_keep_at_most_one(caplog):
    """
    Two explicit edits that contradict each other cannot both survive: at most one is
    honoured, the other is dropped with a WARNING instead of rewriting the ladder.
    """
    existing = {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}
    step = [100.0, 100.0, 100.0, 100.0]
    cases = [
        {"TP2": 3950.0, "TP3": 3900.0},   # the second edit sits below the first
        {"TP3": 3950.0, "TP2": 3960.0},   # the second edit sits above the first
    ]
    for edit in cases:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="copier.parser"):
            merged = parser.merge_tp_levels_for_active_signal(
                dict(existing),
                edit,
                direction="BUY",
                pip_size=0.10,
                step_pips=step,
            )
        prices = [merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged]
        assert prices == sorted(prices), (
            f"the merged TP ladder must stay strictly ordered: {merged}"
        )
        honoured = [k for k, v in edit.items() if merged.get(k) == v]
        assert len(honoured) <= 1, (
            f"at most one of two conflicting edits may survive: {edit} -> {merged}"
        )
        warnings = [
            rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
        ]
        assert warnings, f"a conflicting edit must be logged: {edit}"



# ---------------------------------------------------------------------------
# Fix wave round 3 - an unlabeled TP edit may never rewrite a live stage backwards
# ---------------------------------------------------------------------------

def test_measured_unlabeled_tp_edit_never_rewrites_a_live_stage_backwards(caplog):
    """
    The measured case: a live ladder TP1 3518.47 / TP2 3536.26 / TP3 3554.06 (BUY)
    and an unlabeled edit [3577.20, 3494.47, 3544.83].

    The merge used to rewrite TP1 downwards to 3484.47 (-34.00), which can put a
    broker take-profit below the entry price and close the live position at a loss.
    The merged ladder must not move any live stage against the trade direction and
    must stay strictly ordered; every price that cannot be placed that way is
    dropped with a WARNING that names it.
    """
    existing = {"TP1": 3518.47, "TP2": 3536.26, "TP3": 3554.06}
    submitted = [3577.20, 3494.47, 3544.83]
    step = [100.0, 100.0, 100.0, 100.0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="copier.parser"):
        merged = parser.merge_tp_levels_for_active_signal(
            dict(existing),
            None,
            submitted,
            direction="BUY",
            pip_size=0.10,
            step_pips=step,
        )

    # No live stage may be rewritten against the trade direction.
    for key, previous in existing.items():
        current = merged.get(key)
        assert current is not None, f"the stage {key} must survive: {merged}"
        assert current >= previous - 1e-9, (
            f"the stage {key} was rewritten backwards from {previous} to {current}: "
            f"{merged}"
        )

    # The merged ladder must stay strictly ordered.
    ladder = [merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged]
    assert ladder == sorted(ladder), f"the merged ladder must stay ordered: {merged}"

    # Every price that could not be placed is dropped with a WARNING naming it.
    warnings = [
        rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
    ]
    assert warnings, f"a dropped price must be logged: {submitted} -> {merged}"
    for idx, price in enumerate(submitted):
        stage = f"TP{idx + 1}"
        honoured = merged.get(stage) is not None and abs(merged[stage] - price) <= 1e-9
        if not honoured:
            assert any(f"{price:.2f}" in message for message in warnings), (
                f"the dropped price {price:.2f} must be logged: {warnings}"
            )


def test_unlabelled_tp_edit_never_rewrites_an_unedited_stage_backwards(caplog):
    """
    Property test over 10 000 randomised unlabeled merges.

    P1 - no stage that does not carry one of the submitted prices may move against
         the trade direction: it keeps a value >= its pre-existing value on a BUY
         (<= on a SELL).
    P2 - the merged ladder is strictly ordered, or a submitted price was dropped
         with a WARNING and the pre-existing ladder was kept unchanged.

    An unlabeled price is also never allowed to push a live take-profit backwards:
    the stage a submitted price lands on is the assignment this module makes (the
    consecutive free stages, from the bottom), so honouring such a price would
    loosen the broker take-profit.
    """
    rng = random.Random(20251121)
    step = [100.0, 100.0, 100.0, 100.0]

    with caplog.at_level(logging.WARNING, logger="copier.parser"):
        for case in range(10000):
            direction = "BUY" if rng.random() < 0.5 else "SELL"
            sign = 1.0 if direction == "BUY" else -1.0
            n_stages = rng.randint(1, 5)
            base_price = rng.uniform(3000.0, 4000.0)
            gap = rng.uniform(1.0, 60.0)
            existing = {
                f"TP{i + 1}": round(base_price + sign * i * gap, 2) for i in range(n_stages)
            }
            low = min(existing.values())
            high = max(existing.values())
            n_prices = rng.randint(1, 3)
            submitted = []
            for _ in range(n_prices):
                mode = rng.random()
                if mode < 0.34:
                    # below the ladder range (against the trade direction)
                    price = round(
                        (low - rng.uniform(0.1, 120.0))
                        if direction == "BUY"
                        else (high + rng.uniform(0.1, 120.0)),
                        2,
                    )
                elif mode < 0.67:
                    # inside the ladder range
                    price = round(rng.uniform(low, high), 2)
                else:
                    # above the ladder range
                    price = round(
                        (high + rng.uniform(0.1, 120.0))
                        if direction == "BUY"
                        else (low - rng.uniform(0.1, 120.0)),
                        2,
                    )
                submitted.append(price)

            caplog.clear()
            merged = parser.merge_tp_levels_for_active_signal(
                dict(existing),
                None,
                submitted,
                direction=direction,
                pip_size=0.10,
                step_pips=step,
            )
            context = (
                f"case {case}: direction={direction} existing={existing} "
                f"submitted={submitted} merged={merged}"
            )

            # P1 - a stage that carries none of the submitted prices is not
            # provider-authored and must not move against the trade direction.
            for i in range(1, 6):
                key = f"TP{i}"
                previous = existing.get(key)
                if previous is None:
                    continue
                current = merged.get(key)
                if current is None:
                    continue
                if any(abs(current - price) <= 1e-9 for price in submitted):
                    continue  # the stage carries a submitted price
                if direction == "BUY":
                    assert current >= previous - 1e-9, (
                        f"P1 violated: the stage {key} moved backwards from "
                        f"{previous} to {current} ({context})"
                    )
                else:
                    assert current <= previous + 1e-9, (
                        f"P1 violated: the stage {key} moved backwards from "
                        f"{previous} to {current} ({context})"
                    )

            # A live take-profit is never loosened: an unlabeled price is only
            # honoured when it does not push the live stage backwards.
            for i in range(1, 6):
                key = f"TP{i}"
                previous = existing.get(key)
                if previous is None:
                    continue
                current = merged.get(key)
                if current is None:
                    continue
                if direction == "BUY":
                    assert current >= previous - 1e-9, (
                        f"a live take-profit was loosened: the stage {key} moved "
                        f"backwards from {previous} to {current} ({context})"
                    )
                else:
                    assert current <= previous + 1e-9, (
                        f"a live take-profit was loosened: the stage {key} moved "
                        f"backwards from {previous} to {current} ({context})"
                    )

            # P2 - the merged ladder is strictly ordered, or a price was dropped
            # with a WARNING and the pre-existing ladder was kept unchanged.
            ladder = [merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged]
            ordered = (
                all(b > a for a, b in zip(ladder, ladder[1:]))
                if direction == "BUY"
                else all(b < a for a, b in zip(ladder, ladder[1:]))
            )
            warnings = [
                rec.getMessage()
                for rec in caplog.records
                if rec.levelno >= logging.WARNING
            ]
            if not ordered:
                baseline = parser.merge_tp_levels_for_active_signal(
                    dict(existing),
                    None,
                    direction=direction,
                    pip_size=0.10,
                    step_pips=step,
                )
                assert merged == baseline, (
                    f"P2 violated: the ladder is not ordered and the prices were not "
                    f"dropped ({context})"
                )
                assert warnings, (
                    f"P2 violated: the ladder is not ordered and nothing was logged "
                    f"({context})"
                )

            # Every price that did not survive must be logged with its price.
            for idx, price in enumerate(submitted):
                stage = f"TP{idx + 1}"
                honoured = (
                    merged.get(stage) is not None
                    and abs(merged[stage] - price) <= 1e-9
                )
                if not honoured:
                    assert any(f"{price:.2f}" in message for message in warnings), (
                        f"the dropped price {price:.2f} must be logged with its "
                        f"price: {warnings} ({context})"
                    )


# ---------------------------------------------------------------------------
# Qwen-review bypass fixes: every live TP-edit path must use the protected merge
# ---------------------------------------------------------------------------

def _live_sig_for_adjust(ladder):
    return {
        "symbol": "XAUUSD.s",
        "direction": "BUY",
        "status": "RUNNER_ACTIVE",
        "tp_levels": dict(ladder),
        "raw_tp1": ladder.get("TP1"),
        "raw_tp2": ladder.get("TP2"),
        "raw_tp3": ladder.get("TP3"),
        "market_tickets": [],
        "pending_tickets": [],
        "layers": {},
    }


def test_adjust_active_signal_sl_tp_never_loosens_a_live_tp_unlabelled():
    """
    Qwen CRITICAL 1: adjust_active_signal_sl_tp used to run its own inline TP merge that
    bypassed the protected parser merge, so an unlabelled price could push a live
    take-profit backwards. It must now route through the protected merge: the offending
    prices are dropped, no live stage moves backwards, and the ladder stays ordered.
    """
    ex = MT5Executor(dry_run=True)
    ex.connect()
    live = {"TP1": 3518.47, "TP2": 3536.26, "TP3": 3554.06}
    sig = _live_sig_for_adjust(live)
    ex.adjust_active_signal_sl_tp(sig, unlabeled_tps=[3577.20, 3494.47, 3544.83])
    out = sig["tp_levels"]
    for k, v in live.items():
        assert out[k] >= v - 1e-9, f"live stage {k} was loosened: {v} -> {out[k]}"
    vals = [out[f"TP{i}"] for i in range(1, 6)]
    assert all(vals[i] < vals[i + 1] for i in range(4)), f"ladder not ordered: {out}"


def test_adjust_active_signal_sl_tp_labelled_edit_stays_ordered():
    """A labelled TP edit through the executor must keep the ladder strictly ordered."""
    ex = MT5Executor(dry_run=True)
    ex.connect()
    live = {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0, "TP4": 3930.0, "TP5": 3940.0}
    sig = _live_sig_for_adjust(live)
    # TP3 = 3895 sits below TP2 and cannot be placed without breaking order: it must be
    # dropped and the live ladder kept, never persisted as a non-monotonic ladder.
    ex.adjust_active_signal_sl_tp(sig, labeled_tps={"TP3": 3895.0})
    out = sig["tp_levels"]
    vals = [out[f"TP{i}"] for i in range(1, 6)]
    assert all(vals[i] < vals[i + 1] for i in range(4)), f"ladder not ordered: {out}"
    for k, v in live.items():
        assert out[k] >= v - 1e-9, f"live stage {k} was loosened: {v} -> {out[k]}"


def test_no_edit_merge_never_moves_a_live_stage_backwards():
    """
    Qwen MAJOR 3: a merge with NO edit must never touch the live ladder. On a malformed
    pre-existing ladder the old DP sanitizer could re-derive a stage backwards (loosening
    a live take-profit). The no-edit path must return the ladder unchanged.
    """
    malformed = {"TP1": 3750.38, "TP2": 3791.28, "TP3": 3755.61}
    out = parser.merge_tp_levels_for_active_signal(
        dict(malformed), None, None, direction="BUY", pip_size=0.10
    )
    assert out == malformed, f"no-edit merge must not touch the live ladder: {out}"
    # And the same for a well-formed ladder, both directions.
    for direction, ladder in (
        ("BUY", {"TP1": 3900.0, "TP2": 3910.0, "TP3": 3920.0}),
        ("SELL", {"TP1": 3920.0, "TP2": 3910.0, "TP3": 3900.0}),
    ):
        out = parser.merge_tp_levels_for_active_signal(
            dict(ladder), None, None, direction=direction, pip_size=0.10
        )
        assert out == ladder, f"no-edit merge changed a well-formed ladder: {out}"


def test_all_rejected_edits_on_malformed_ladder_keep_the_live_ladder():
    """
    Qwen scoped re-review residual MAJOR: on a malformed pre-existing ladder, an edit whose
    every submitted price/label is rejected must still keep the live ladder unchanged. The
    final merge must not re-run the DP sanitizer with no accepted edits and thereby push a
    live take-profit backwards.
    """
    malformed = {"TP1": 3500.0, "TP2": 3600.0, "TP3": 3550.0}
    for kwargs in (
        {"unlabeled_tps": [3400.0]},
        {"labeled_tps": {"TP2": 3400.0}},
    ):
        out = parser.merge_tp_levels_for_active_signal(
            dict(malformed), direction="BUY", pip_size=0.10, **kwargs
        )
        assert out == malformed, (
            f"all-rejected edits must keep the live ladder unchanged, got {out} for {kwargs}"
        )
        for k, v in malformed.items():
            assert out[k] >= v - 1e-9, f"live stage {k} was loosened: {v} -> {out[k]}"
