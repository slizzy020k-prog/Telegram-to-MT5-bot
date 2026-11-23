#!/usr/bin/env python3
"""
Deterministic 12-Trade Nov 17-19 XAUUSD.s M1-Bar A/B Replay Proof
=================================================================

Compares:
  - Arm A (`4544f7f` v3.5.2 baseline): executed inside an isolated temporary
    git worktree at commit `4544f7f` in a dedicated Python subprocess.
  - Arm B (`v3.6.0` calibrated working tree): executed in a separate Python
    subprocess against the working repository with calibrated v3.6.0 defaults.

Both arms run strictly in `DRY_RUN=true` with isolated temporary runtime paths
(`STATE_FILE_PATH`, `LOG_FILE_PATH`, `TRADES_LEDGER_PATH`, `LOCK_FILE_PATH`)
set in `os.environ` prior to importing `config` or `main`.

MT5 usage is strictly read-only (`mt5.copy_rates_range` only; zero orders).
The historical M1 tape defaults to the canonical pinned fixture
(`tests/fixtures/tape_2026-11-17_19.json`, validated on load); live fetch
requires the explicit `--live-bars` flag, and `--refresh-tape <new-file>`
re-fetches to a NEW file for human-reviewed promotion.
Multi-layer trades initialize all filled MT5 layers (`L1..Lk`) at `open_utc`
using exact MT5 fill prices, with legacy `TOP -> L1` normalized so all 12 trades
exercise the 4-layer / Profit Ratchet FSM uniformly.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "4544f7f"
SYMBOL = "XAUUSD.s"
GBP_PER_PT_001_LOT = 0.7589
MT5_SERVER_OFFSET_SEC = 7200  # MT5 UTC+2 -> UTC/GMT
ATR_PERIOD = 14
WARMUP_START_MT5 = datetime(2026, 11, 17, 0, 0)
WARMUP_END_MT5 = datetime(2026, 11, 19, 13, 0)
CHAT_ID = -1001234567890

# Canonical pinned historical tape (Phase 1.1). Default replay input; live
# MT5 fetch requires the explicit --live-bars flag, refresh requires
# --refresh-tape <new-file>.
PINNED_TAPE_PATH = REPO_ROOT / "tests" / "fixtures" / "tape_2026-11-17_19.json"
TAPE_SCHEMA = "mt5-copier-replay-tape/1"

# Production-engine replay. Arms B and C drive the real ATR trail engine; Arm A
# stays the frozen 4544f7f reference and is deliberately NOT fed the engine ATR.
_PRODUCTION_ENGINE_ARM = False
R_LOCK_WIDE_R_THRESHOLD = 5.0  # R >= $5/oz counts as a "wide stop" for precedence
_PICKER_TRACE: Dict[str, Any] = {}


def _reset_picker_trace(msg_id: str) -> None:
    global _PICKER_TRACE
    _PICKER_TRACE = {
        "msg_id": msg_id,
        "calls": 0,
        "sources": {},
        "r_lock_wins": 0,
        "r_lock_wide_wins": 0,
        "r_lock_max_r": 0.0,
        "r_lock_first_mfe": None,
        # Phase 2 lock_fraction instrumentation (REPORTING ONLY; never alters a
        # picker decision). f_k == 0 wins are BE-equivalents (floor == entry).
        "r_lock_fraction_zero_wins": 0,
        "r_lock_fraction_pos_wins": 0,
        "r_lock_winning_r_min": None,
        "r_lock_winning_r_max": 0.0,
        "r_lock_max_trail_dist_at_win": 0.0,
        "r_lock_win_details": [],
        "max_trail_dist": 0.0,
        "r_values_seen": set(),
    }


def _install_picker_trace(mt5_executor_mod: Any) -> None:
    """Record every production picker decision so R-lock precedence is evidenced."""
    orig = mt5_executor_mod.compute_dynamic_atr_trail_stop

    def _traced(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        res = orig(*args, **kwargs)
        st = _PICKER_TRACE
        if st:
            st["calls"] += 1
            src = str(res.get("stop_source") or "")
            st["sources"][src] = st["sources"].get(src, 0) + 1
            # Per-call trail distance + per-layer R, for the structural wide-stop
            # check (trail_dist vs (t_k - f_k) * R). Reporting only.
            td = float(res.get("trail_dist") or 0.0)
            if td > st["max_trail_dist"]:
                st["max_trail_dist"] = td
            ir = kwargs.get("initial_r")
            ir_ok = isinstance(ir, (int, float)) and not isinstance(ir, bool) and float(ir) > 0.0
            if ir_ok:
                st["r_values_seen"].add(round(float(ir), 4))
            if src == "R_LOCK":
                st["r_lock_wins"] += 1
                if ir_ok:
                    if float(ir) > st["r_lock_max_r"]:
                        st["r_lock_max_r"] = float(ir)
                    if float(ir) >= R_LOCK_WIDE_R_THRESHOLD:
                        st["r_lock_wide_wins"] += 1
                if st["r_lock_first_mfe"] is None:
                    st["r_lock_first_mfe"] = float(res.get("mfe_dollars") or 0.0)
                # Recover the winning tier's lock_fraction f_k from the winning
                # floor: floor = entry + sign * f_k * R  =>  f_k = sign*(floor-entry)/R.
                entry = kwargs.get("entry_price")
                floor = res.get("r_lock_floor")
                if (
                    ir_ok
                    and isinstance(entry, (int, float)) and not isinstance(entry, bool)
                    and isinstance(floor, (int, float)) and not isinstance(floor, bool)
                ):
                    sign = 1.0 if str(kwargs.get("direction") or "BUY").strip().upper() == "BUY" else -1.0
                    frac = round(sign * (float(floor) - float(entry)) / float(ir), 4)
                    mfe = float(res.get("mfe_dollars") or 0.0)
                    st["r_lock_win_details"].append({
                        "fraction": frac,
                        "r": round(float(ir), 4),
                        "trail_dist": round(td, 4),
                        "mfe": round(mfe, 4),
                        "zone": str(res.get("trail_zone") or ""),
                    })
                    if frac <= 1e-9:
                        st["r_lock_fraction_zero_wins"] += 1
                    else:
                        st["r_lock_fraction_pos_wins"] += 1
                    if st["r_lock_winning_r_min"] is None or float(ir) < st["r_lock_winning_r_min"]:
                        st["r_lock_winning_r_min"] = float(ir)
                    if float(ir) > st["r_lock_winning_r_max"]:
                        st["r_lock_winning_r_max"] = float(ir)
                    if td > st["r_lock_max_trail_dist_at_win"]:
                        st["r_lock_max_trail_dist_at_win"] = td
        return res

    mt5_executor_mod.compute_dynamic_atr_trail_stop = _traced


def _rlock_snap_tier(fraction: float, tiers: Any, r: float, mfe: float) -> Tuple[Optional[float], float]:
    """Snap a recovered lock_fraction to its configured tier (t_k, f_k).

    The winning floor is rounded to broker digits, so recovering f_k from
    (floor - entry) / R carries up to half-a-cent/R of discretisation error
    (e.g. 0.2495 instead of 0.25, or -0.0 instead of 0.0). Snap to the nearest
    configured tier fraction within that tolerance so the histogram groups by
    true tier. Returns (threshold, snapped_fraction); threshold is None when no
    tier matches (e.g. tiers unavailable on the frozen Arm A engine).
    """
    frac = 0.0 if abs(fraction) <= 1e-9 else float(fraction)
    tol = (0.005 / r + 1e-6) if r > 0.0 else 1e-6
    best: Optional[Tuple[float, float]] = None
    best_dist = tol
    for (t, f) in (tiers or []):
        dist = abs(float(f) - frac)
        if dist <= best_dist:
            best_dist = dist
            best = (float(t), float(f))
    if best is None:
        return (None, frac)
    return (best[0], best[1])


def _enrich_rlock_trace(st: Dict[str, Any], tiers: Any) -> None:
    """Add t_k / margin to each win and compute the structural wide-stop view.

    Mutates the trace dict in place; deterministic and reporting-only. The
    structural test: R_LOCK can only win a profit tier (f_k > 0) while
    trail_dist > (t_k - f_k) * R. If even the LARGEST trail distance ever
    observed is below the EASIEST profit-tier margin on the widest layer, the
    wide stop is structurally unreachable by the R lock (not a fraction problem).
    """
    if not st:
        return
    for d in st.get("r_lock_win_details") or []:
        t, f_snap = _rlock_snap_tier(d["fraction"], tiers, d["r"], d["mfe"])
        d["fraction"] = f_snap
        d["threshold"] = t
        if t is not None:
            margin = round((t - f_snap) * d["r"], 4)
            d["margin_needed"] = margin
            d["trail_gt_margin"] = bool(d["trail_dist"] > margin + 1e-9)
        else:
            d["margin_needed"] = None
            d["trail_gt_margin"] = None
    r_vals = sorted(st.get("r_values_seen") or [])
    widest_r = r_vals[-1] if r_vals else 0.0
    profit_margins = [(float(t) - float(f)) * widest_r for (t, f) in (tiers or []) if float(f) > 0.0]
    min_margin = round(min(profit_margins), 4) if profit_margins else None
    max_td = round(float(st.get("max_trail_dist") or 0.0), 4)
    st["r_lock_widest_r"] = round(widest_r, 4)
    st["r_lock_min_profit_margin"] = min_margin
    st["r_lock_structural_blocked"] = bool(min_margin is not None and max_td < min_margin - 1e-9)


# ---------------------------------------------------------------------------
# T3-tight comparison telemetry (opt-in per worker run; REPORTING ONLY).
# Enabled exclusively for the new "--arm tight" / "--arm t3tight" modes, so
# every pre-existing arm's output stays byte-identical. Never alters a picker
# decision: it wraps the (already-traced) production picker and only records
# WHEN protection first armed on the simulated replay clock.
# ---------------------------------------------------------------------------
_PROTECTION_TELEMETRY = False
_EXIT_BINDING_TELEMETRY = False
_PROTECTION_TRACE: Dict[str, Any] = {}
# Round-turn slippage estimate for the cost-adjusted metric view ($/oz per side).
# Spread is already modeled per-bar from the tape (bid/ask); this adds an explicit
# slippage allowance on top. Reporting constant only - never fed to the engine.
SLIPPAGE_ESTIMATE_DOLLARS_PER_SIDE = 0.10
# Commission estimate for the cost-adjusted view ($/oz round turn). Documented
# assumption: $7 per 1.00 lot round turn = $0.07/oz (1 lot = 100 oz). Reporting
# constant only - never fed to the engine.
COMMISSION_ESTIMATE_DOLLARS_PER_OZ_ROUND_TURN = 0.07


def _reset_protection_trace(msg_id: str) -> None:
    global _PROTECTION_TRACE
    _PROTECTION_TRACE = {
        "msg_id": msg_id,
        "first_protect_utc": None,
        "first_protect_source": None,
        "first_be_arm_utc": None,
        "first_trail_arm_utc": None,
        "first_mfe_lock_utc": None,
        "first_auto_be_utc": None,
        "in_profit_candidate_calls": 0,
        "max_mfe_lock_tier": 0.0,
        "max_mfe_lock_pct": 0.0,
        "max_mfe_lock_profit": 0.0,
        # Per-layer last / last-applied production picker snapshots (exit binding
        # evidence). Keyed by rounded layer entry price; layers sharing an entry
        # share a slot (their picker math is identical).
        "picker_by_layer": {},
    }


def _install_protection_trace(mt5_executor_mod: Any) -> None:
    """Record first-protection timing around the production picker (reporting only)."""
    orig = mt5_executor_mod.compute_dynamic_atr_trail_stop

    def _traced(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        res = orig(*args, **kwargs)
        st = _PROTECTION_TRACE
        if st:
            now_iso = _REPLAY_CLOCK.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            be_armed = bool(res.get("be_armed"))
            trail_armed = bool(res.get("trail_armed"))
            lock_armed = res.get("mfe_lock_sl") is not None
            auto_be_armed = bool(res.get("auto_be_armed"))
            if be_armed and st["first_be_arm_utc"] is None:
                st["first_be_arm_utc"] = now_iso
            if trail_armed and st["first_trail_arm_utc"] is None:
                st["first_trail_arm_utc"] = now_iso
            if lock_armed and st["first_mfe_lock_utc"] is None:
                st["first_mfe_lock_utc"] = now_iso
            if auto_be_armed and st["first_auto_be_utc"] is None:
                st["first_auto_be_utc"] = now_iso
            if (be_armed or trail_armed or lock_armed or auto_be_armed) and st["first_protect_utc"] is None:
                st["first_protect_utc"] = now_iso
                st["first_protect_source"] = (
                    "BE_LOCK" if be_armed else (
                        "ATR_TRAIL" if trail_armed else ("MFE_LOCK" if lock_armed else "AUTO_BE")
                    )
                )
            for key, field in (
                ("max_mfe_lock_tier", "mfe_lock_tier"),
                ("max_mfe_lock_pct", "mfe_lock_fraction"),
                ("max_mfe_lock_profit", "mfe_lock_profit"),
            ):
                v = float(res.get(field) or 0.0)
                if v > st[key]:
                    st[key] = v
            entry = kwargs.get("entry_price")
            cand = res.get("candidate_sl")
            if isinstance(entry, (int, float)) and isinstance(cand, (int, float)):
                sign = 1.0 if str(kwargs.get("direction") or "BUY").strip().upper() == "BUY" else -1.0
                if sign * (float(cand) - float(entry)) > 1e-9:
                    st["in_profit_candidate_calls"] += 1
            if isinstance(entry, (int, float)):
                slim = {k: res.get(k) for k in (
                    "stop_source", "candidate_sl", "atr_sl", "mfe_lock_sl", "r_lock_floor",
                    "be_sl", "auto_be_sl", "milestone_floor", "mfe_lock_tier",
                    "mfe_lock_fraction", "mfe_lock_profit", "mfe_dollars", "trail_dist",
                    "atr", "should_update",
                )}
                slot = st["picker_by_layer"].setdefault(str(round(float(entry), 4)), {"last": None, "last_applied": None})
                slot["last"] = slim
                if res.get("should_update"):
                    slot["last_applied"] = slim
        return res

    mt5_executor_mod.compute_dynamic_atr_trail_stop = _traced


# ---------------------------------------------------------------------------
# RESEARCH ONLY - wick-aware / close-confirmed MFE activation (wick-aware brief).
# Scripts-only simulation: the production engine is UNCHANGED; this gate wraps
# the production picker for the research arms and, until a tier is confirmed,
# suppresses the MFE_LOCK candidate and re-picks the tightest OTHER candidate
# (same picker rule: max BUY / min SELL, same tie-break priorities, same
# stops-buffer clamp, same forward-only should_update guard). Confirmation uses
# bar CLOSE prices (close-based MFE/ATR); the production peak/trail MFE keeps
# tracking wicks, and MFE stays independent of TP moves. Once confirmed a tier
# stays confirmed (hysteresis / never-loosen). The production pct/profit ratchet
# state is left untouched, so an allowed lock can never be looser than the
# established ratchet.
# ---------------------------------------------------------------------------
_WICK_GATE_MODE: Optional[str] = None
_WICK_TRACKER: Optional["_WickConfirmationTracker"] = None


def _parse_wick_tiers(raw: str) -> Tuple[Tuple[float, float], ...]:
    pairs: Dict[float, float] = {}
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        left, _, right = token.partition(":")
        try:
            thr, pct = float(left), float(right)
        except ValueError:
            continue
        if thr >= 0.0 and 0.0 <= pct <= 1.0:
            pairs[thr] = pct
    return tuple(sorted(pairs.items()))


class _WickConfirmationTracker:
    """Per-layer close-confirmation / N-consecutive persistence state.

    mode "close": a tier confirms when ONE bar closes with close-based
    MFE/ATR >= threshold. mode "persist2"/"persist3": N consecutive closes.
    Confirmed tiers never regress. Ratio uses the bounded trail ATR exactly
    like the production ladder; MFE for confirmation is close-based
    (sign * (close - entry)), NOT the wick peak.
    """

    def __init__(self, mode: str, tiers: Tuple[Tuple[float, float], ...],
                 atr_min: float = 1.0, atr_max: float = 12.0) -> None:
        self.mode = mode
        self.n_required = 1 if mode == "close" else int(mode.replace("persist", ""))
        self.tiers = tiers
        self.atr_min = max(0.01, float(atr_min))
        self.atr_max = max(self.atr_min, float(atr_max))
        self.layers: Dict[Tuple[float, float], Dict[str, Any]] = {}
        self.suppressed_calls = 0

    def reset(self) -> None:
        self.layers = {}
        self.suppressed_calls = 0

    def register(self, entry: float, sign: float) -> Tuple[float, float]:
        key = (round(float(entry), 4), float(sign))
        if key not in self.layers:
            self.layers[key] = {"entry": float(entry), "sign": float(sign),
                                "consec": {thr: 0 for thr, _ in self.tiers},
                                "confirmed": set()}
        return key

    def finalize_bar(self, close_price: float, atr: float) -> None:
        atr_bound = max(self.atr_min, min(self.atr_max, float(atr)))
        for key, st in self.layers.items():
            mfe_close = max(0.0, st["sign"] * (float(close_price) - st["entry"]))
            ratio = mfe_close / atr_bound if atr_bound > 0.0 else 0.0
            for thr, _pct in self.tiers:
                if ratio + 1e-9 >= thr:
                    st["consec"][thr] += 1
                    if st["consec"][thr] >= self.n_required:
                        st["confirmed"].add(thr)
                else:
                    st["consec"][thr] = 0

    def tier_confirmed(self, key: Tuple[float, float], thr: float) -> bool:
        st = self.layers.get(key)
        return bool(st and thr in st["confirmed"])

    def max_confirmed_tier(self) -> float:
        return max((max(st["confirmed"]) for st in self.layers.values() if st["confirmed"]), default=0.0)


def _wick_effective_tier(fraction: float, tiers: Tuple[Tuple[float, float], ...]) -> Optional[float]:
    """Map the picker's effective (hysteresis-aware) pct back to its tier threshold."""
    if fraction <= 0.0:
        return None
    for thr, pct in tiers:
        if abs(pct - fraction) < 1e-6:
            return thr
    best = None
    for thr, pct in tiers:
        if pct <= fraction + 1e-9:
            best = thr
    return best


def _apply_wick_gate(res: Dict[str, Any], kwargs: Dict[str, Any],
                     tracker: _WickConfirmationTracker) -> Dict[str, Any]:
    """Suppress an unconfirmed MFE_LOCK and re-pick the tightest other candidate."""
    if res.get("mfe_lock_sl") is None:
        return res
    entry = kwargs.get("entry_price")
    if not isinstance(entry, (int, float)):
        return res
    sign = 1.0 if str(kwargs.get("direction") or "BUY").strip().upper() == "BUY" else -1.0
    key = tracker.register(float(entry), sign)
    tier = _wick_effective_tier(float(res.get("mfe_lock_fraction") or 0.0), tracker.tiers)
    if tier is None or tracker.tier_confirmed(key, tier):
        return res
    # Suppress MFE_LOCK; replicate the production picker over the OTHER candidates.
    cands: List[Tuple[int, str, float]] = []
    for idx, name, field in (
        (0, "R_LOCK", "r_lock_floor"),
        (1, "MILESTONE_FLOOR", "milestone_floor"),
        (2, "ATR_TRAIL", "atr_sl"),
        (4, "BE_LOCK", "be_sl"),
        (5, "AUTO_BE", "auto_be_sl"),
    ):
        val = res.get(field)
        if isinstance(val, (int, float)) and float(val) > 0.0:
            cands.append((idx, name, float(val)))
    tracker.suppressed_calls += 1
    out = dict(res)
    out["mfe_lock_sl"] = None
    out["wick_gate_suppressed"] = True
    if not cands:
        out.update({"armed": False, "candidate_sl": None, "stop_source": None, "should_update": False})
        return out
    _, src, raw_sl = min(cands, key=lambda it: (-it[2] if sign > 0 else it[2], it[0]))
    live = float(kwargs.get("live_price") or 0.0)
    buf = max(0.0, float(kwargs.get("stops_buffer") or 0.0))
    if buf > 0.0 and live > 0.0:
        raw_sl = min(raw_sl, live - buf) if sign > 0 else max(raw_sl, live + buf)
    cand_sl = round(raw_sl, 2)
    policy = kwargs.get("policy")
    step = float(getattr(policy, "trail_step_dollars", 0.25) or 0.25)
    cur = float(kwargs.get("current_sl") or 0.0)
    should_upd = True if cur <= 0.0 else ((sign * (cand_sl - cur)) >= max(1e-6, step - 1e-9))
    out.update({"armed": True, "candidate_sl": cand_sl, "stop_source": src, "should_update": should_upd})
    return out


def _install_wick_gate(mt5_executor_mod: Any, mode: str, tiers_str: str, cfg_mod: Any) -> None:
    """Install the research gate INNERMOST so all telemetry sees the gated result."""
    global _WICK_TRACKER
    tiers = _parse_wick_tiers(tiers_str)
    tracker = _WickConfirmationTracker(
        mode, tiers,
        atr_min=float(getattr(cfg_mod, "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS", 1.0)),
        atr_max=float(getattr(cfg_mod, "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS", 12.0)),
    )
    _WICK_TRACKER = tracker
    orig = mt5_executor_mod.compute_dynamic_atr_trail_stop

    def _gated(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        res = orig(*args, **kwargs)
        return _apply_wick_gate(res, kwargs, tracker)

    mt5_executor_mod.compute_dynamic_atr_trail_stop = _gated


# ---------------------------------------------------------------------------
# RESEARCH ONLY - ATR-scaled interim protection floor (interim-floor brief).
# AUGMENTS (never suppresses) the production picker: the immediate MFE ladder,
# T3 trail, R-lock, BE/auto-BE and milestone floors are all UNCHANGED and still
# proposed every tick. The floor is ONE EXTRA tightest-stop candidate that binds
# ONLY when tighter than every production candidate, and the forward-only
# should_update guard guarantees an applied SL never loosens. MFE activation is
# NOT delayed; MFE/peak keep tracking wicks independent of TP moves.
#   atr mode: floor = entry + sign*k*ATR, armed once MFE >= k*ATR (peak reached
#             the floor level, so the floor is never above the reached peak).
#   mfe mode: floor = entry + sign*f*MFE, armed once MFE/bounded_ATR >= 0.20
#             (early fraction-of-MFE floor, ahead of the ladder's 0.40 ratio).
# ---------------------------------------------------------------------------
_INTERIM_FLOOR_SPEC: Optional[str] = None
_INTERIM_FLOOR_STATS: Dict[str, Any] = {}
_INTERIM_FLOOR_EARLY_RATIO = 0.20


def _reset_interim_floor_stats() -> None:
    global _INTERIM_FLOOR_STATS
    _INTERIM_FLOOR_STATS = {"armed_calls": 0, "binds": 0, "max_floor_profit": 0.0}


def _apply_interim_floor(res: Dict[str, Any], kwargs: Dict[str, Any], spec: str,
                         stats: Dict[str, Any]) -> Dict[str, Any]:
    """Add the interim floor as an extra candidate; re-pick the tightest stop."""
    entry = kwargs.get("entry_price")
    atr = float(res.get("atr") or 0.0)
    mfe = float(res.get("mfe_dollars") or 0.0)
    if not isinstance(entry, (int, float)) or atr <= 0.0 or mfe <= 0.0:
        return res
    sign = 1.0 if str(kwargs.get("direction") or "BUY").strip().upper() == "BUY" else -1.0
    mode, strength = spec[0], float(spec[1:]) / 100.0
    if mode == "k":
        lvl = strength * atr
        armed = mfe + 1e-9 >= lvl
    else:  # "f": early fraction-of-MFE floor
        atr_bound = max(1.0, min(12.0, atr))
        armed = (mfe / atr_bound) + 1e-9 >= _INTERIM_FLOOR_EARLY_RATIO
        lvl = strength * mfe
    if not armed or lvl <= 0.0:
        return res
    floor_sl = round(float(entry) + sign * lvl, 2)
    stats["armed_calls"] = int(stats.get("armed_calls", 0)) + 1
    if lvl > float(stats.get("max_floor_profit", 0.0)):
        stats["max_floor_profit"] = lvl
    cands: List[Tuple[int, str, float]] = []
    for idx, name, field in (
        (0, "R_LOCK", "r_lock_floor"),
        (1, "MILESTONE_FLOOR", "milestone_floor"),
        (2, "ATR_TRAIL", "atr_sl"),
        (3, "MFE_LOCK", "mfe_lock_sl"),
        (4, "BE_LOCK", "be_sl"),
        (5, "AUTO_BE", "auto_be_sl"),
    ):
        val = res.get(field)
        if isinstance(val, (int, float)) and float(val) > 0.0:
            cands.append((idx, name, float(val)))
    cands.append((6, "INTERIM_FLOOR", floor_sl))
    _, src, raw_sl = min(cands, key=lambda it: (-it[2] if sign > 0 else it[2], it[0]))
    live = float(kwargs.get("live_price") or 0.0)
    buf = max(0.0, float(kwargs.get("stops_buffer") or 0.0))
    if buf > 0.0 and live > 0.0:
        raw_sl = min(raw_sl, live - buf) if sign > 0 else max(raw_sl, live + buf)
    cand_sl = round(raw_sl, 2)
    policy = kwargs.get("policy")
    step = float(getattr(policy, "trail_step_dollars", 0.25) or 0.25)
    cur = float(kwargs.get("current_sl") or 0.0)
    should_upd = True if cur <= 0.0 else ((sign * (cand_sl - cur)) >= max(1e-6, step - 1e-9))
    if src == "INTERIM_FLOOR" and should_upd:
        stats["binds"] = int(stats.get("binds", 0)) + 1
    out = dict(res)
    out.update({
        "armed": True,
        "candidate_sl": cand_sl,
        "stop_source": src,
        "should_update": should_upd,
        "interim_floor_sl": floor_sl,
        "interim_floor_armed": True,
    })
    return out


def _install_interim_floor(mt5_executor_mod: Any, spec: str) -> None:
    """Install INNERMOST so picker/protection telemetry sees the floor-augmented result."""
    global _INTERIM_FLOOR_STATS
    _reset_interim_floor_stats()
    orig = mt5_executor_mod.compute_dynamic_atr_trail_stop

    def _floored(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        res = orig(*args, **kwargs)
        return _apply_interim_floor(res, kwargs, spec, _INTERIM_FLOOR_STATS)

    mt5_executor_mod.compute_dynamic_atr_trail_stop = _floored


# Canonical DYNAMIC_ATR_* trail + BE knobs pinned for BOTH arms so the replay
# is independent of the ambient .env (Phase 1.1, Hole 2). Values equal the
# shipped v3.9.0 production defaults; pinning makes them immune to .env drift.
_DYNAMIC_ATR_PINNED_PARAMS: Dict[str, Any] = {
    "DYNAMIC_ATR_SLTP_ENABLED": True,
    "DYNAMIC_SLTP_MODE": "ADAPTIVE",
    "DYNAMIC_ATR_TRAIL_ENABLED": True,
    "DYNAMIC_ATR_TRAIL_TIMEFRAME": "M5",
    "DYNAMIC_ATR_TRAIL_MULT": 1.40,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS": 10.00,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MULT": 1.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS": 18.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MULT": 0.80,
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 1.00,
    "DYNAMIC_ATR_TRAIL_MIN_DOLLARS": 2.50,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 8.00,
    "DYNAMIC_ATR_TRAIL_STEP_DOLLARS": 0.25,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS": 2.20,
    "DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS": 5.50,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS": 2.00,
    "DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS": 4.00,
    "DYNAMIC_ATR_BE_ACTIVATION_MULT": 0.75,
    "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS": 3.50,
    "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS": 0.50,
}

# Shipped defaults for the opt-in T3 protection knobs (5ac5fe2 / 5f47b48 /
# 09aae20). The live .env may ACTIVATE these; every engine arm must see the
# shipped default for any knob its own param set does not explicitly pin, so
# all pre-existing arms stay byte-identical regardless of ambient activation
# (same .env-independence philosophy as _DYNAMIC_ATR_PINNED_PARAMS).
_OPT_IN_PROTECTION_SHIPPED_DEFAULTS: Dict[str, Any] = {
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": 0.0,
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": 0.0,
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": False,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "0.40:0.45,0.60:0.55,0.80:0.65,1.00:0.70,1.25:0.75,1.50:0.80,2.00:0.85",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": 0.60,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS": 1.00,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS": 12.00,
    "DYNAMIC_ATR_RELATIVE_SCALE_MULT": 1.02,
    "DYNAMIC_ATR_AUTO_BE_ENABLED": False,
    "DYNAMIC_ATR_AUTO_BE_PIPS": 30.0,
    "DYNAMIC_ATR_AUTO_BE_PIP_SIZE_DOLLARS": 0.0,
    "DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT": 1.00,
}

# Calibrated v3.6.0 parameters (M1 Swarm Consensus + 9 canonical Rev 2 defaults)
ARM_B_CALIBRATED_PARAMS: Dict[str, Any] = {
    "PROFIT_RATCHET_ENABLED": True,
    "BANK_HARVEST_ENABLED": True,
    "TIERED_STAGE_BANKING_ENABLED": True,
    "STAGE1_ATR_ACTIVATION_PCT": 0.50,
    "TP1_TRAIL_ATR_MULT": 1.55,
    "TP1_TRAIL_MIN_DOLLARS": 2.50,
    "ARM_THRESHOLD_PCT": 0.90,
    "GIVEBACK_PCT": 0.30,
    "REVERSAL_CONFIRM_TICKS": 2,
    "RUNNER_REVERSAL_CONFIRM_TICKS": 2,
    "MAX_ALLOWED_SPREAD_DOLLARS": 0.45,
    "RATCHET_TIER2_MIN_LOCK_DOLLARS": 1.50,
    "RATCHET_TIER3_RETENTION_PCT": 0.55,
    "RATCHET_TIER4_RETENTION_PCT": 0.68,
    "RATCHET_TIER4_MIN_CUSHION": 3.75,
    "RATCHET_TIER5_RETENTION_PCT": 0.78,
    "RATCHET_STAGE4_MIN_CUSHION": 3.00,
    "RATCHET_STAGE4_MAX_CUSHION": 4.50,
    "PEAK_HARVEST_MIN_VIRT_CUSHION": 2.25,
    "BANK_HARVEST_ALT_ARM_PCT": 0.80,
    # Arm B is the no-R-lock baseline: pin it explicitly so an R_LOCK_ENABLED=true
    # in the ambient .env cannot contaminate the A/B/C comparison.
    "R_LOCK_ENABLED": False,
    **_DYNAMIC_ATR_PINNED_PARAMS,
}

# Arm C params: Arm B plus the R-relative profit floor (R lock only). The same
# DYNAMIC_ATR_* trail + BE pins apply so Arm C is equally .env-independent.
ARM_C_CALIBRATED_PARAMS: Dict[str, Any] = {
    **_DYNAMIC_ATR_PINNED_PARAMS,
    "R_LOCK_ENABLED": True,
}

# EXPERIMENTAL - Phase 2 decision evidence ONLY. NOT production, NOT shipped,
# never a default arm, and never written to config.py / .env. A deliberately
# re-tiered (lower-threshold) R-lock ladder used to TEST the audit hypothesis
# that re-tiering cannot engage wide stops. Run explicitly via "--arm cx"
# (which also runs Arm C for a before/after on the pinned tape). The easiest
# profit-tier margin here is (0.75 - 0.25) * R = 0.5R, vs 0.75R for the shipped
# ladder; any layer with 0.5R above the $8 trail clamp ceiling (R > $16) stays
# structurally unreachable regardless of tiering.
ARM_CX_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_C_CALIBRATED_PARAMS,
    "R_LOCK_TIERS": "0.5:0.00,0.75:0.25,1.0:0.50,1.25:0.75",
}

# EXPERIMENTAL - Phase 3+4 trailing A/B decision evidence ONLY. NOT production,
# NOT shipped, never a default arm, and never written to config.py / .env.
# All four arms are Arm B (R-lock OFF; Phase 2 proved R-lock irrelevant to
# gate_3) plus ONE deliberate trail-knob delta, so every difference vs the
# pinned baseline B is attributable to the named knob. Run explicitly via
# "--arm trailing" (runs A + B + T1..T4 on the identical pinned tape) or a
# single "--arm t1|t2|t3|t4".
#   H1 (t1): arm the ATR trail earlier on high-ATR trades (9934 never armed
#            at 1.00x because M5 ATR > MFE 6.34).
#   H2 (t2): tighten the STANDARD-zone trail distance clamp $8.00 -> $5.50
#            (reduce stop-distance surrender on 13153/13167/9934).
#   H1+H2 (t3): both together (audit hypothesis: H1 alone is insufficient
#            because an $8 trail is looser than BE at MFE 6.34).
#   t4: intermediate STANDARD clamp $6.50 to map the trade-off.
ARM_T1_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_B_CALIBRATED_PARAMS,
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70,
}
ARM_T2_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_B_CALIBRATED_PARAMS,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50,
}
ARM_T3_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_B_CALIBRATED_PARAMS,
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT": 0.70,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 5.50,
}
ARM_T4_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_B_CALIBRATED_PARAMS,
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS": 6.50,
}

# EXPERIMENTAL - T3-upgraded validation arm (t3-atr-relative definitive spec).
# T3 (TRAILING_PROFILE=t3 base: activation 0.70x + STANDARD clamp $5.50) PLUS the
# opt-in ATR-relative upgrade shipped in engine commit 5f47b48: ATR-relative MFE
# protection ladder (ratio:pct, hysteresis, bounded ATR), 2% Relative ATR, and
# automatic 30-pip BE (verified gold pip $0.10 -> $3.00). SUPERSEDES the earlier
# fractional/absolute-dollar MFE-lock arm (5ac5fe2). Replay arm-param override
# ONLY: never written to config.py / .env, never a default arm. The canonical
# pinned fixture and the existing arms (a/b/c/cx/t1..t4) are deliberately untouched.
ARM_T3TIGHT_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_T3_EXPERIMENTAL_PARAMS,
    "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS": 4.00,
    "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS": 3.00,
    "DYNAMIC_ATR_TRAIL_DISTANCE_TIERS": "6:3.50,10:3.00,15:2.75,20:2.50",
    "DYNAMIC_ATR_MFE_LOCK_ENABLED": True,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": "0.40:0.45,0.60:0.55,0.80:0.65,1.00:0.70,1.25:0.75,1.50:0.80,2.00:0.85",
    "DYNAMIC_ATR_MFE_LOCK_COST_FLOOR_DOLLARS": 0.60,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MIN_DOLLARS": 1.00,
    "DYNAMIC_ATR_MFE_LOCK_ATR_MAX_DOLLARS": 12.00,
    "DYNAMIC_ATR_RELATIVE_SCALE_MULT": 1.02,
    "DYNAMIC_ATR_AUTO_BE_ENABLED": True,
    "DYNAMIC_ATR_AUTO_BE_PIPS": 30.0,
    "DYNAMIC_ATR_AUTO_BE_PIP_SIZE_DOLLARS": 0.0,
}

# Side-by-side comparison registry for "--arm tight" (T3 vs T3-tight). Kept
# SEPARATE from TRAILING_EXPERIMENT_ARMS so "--arm trailing" stays byte-identical.
TIGHT_COMPARISON_ARMS: Dict[str, Dict[str, Any]] = {
    "t3": {
        "params": ARM_T3_EXPERIMENTAL_PARAMS,
        "label": "T3: activation 0.70x + STANDARD clamp $5.50",
    },
    "t3tight": {
        "params": ARM_T3TIGHT_EXPERIMENTAL_PARAMS,
        "label": "T3-upgraded: ATR-relative MFE ladder + 2% Relative ATR + auto 30-pip BE (5f47b48)",
    },
}

# EXPERIMENTAL - 5% trail-distance scale backtest (engine commit 09aae20).
# BOTH arms are the CURRENT upgraded ATR-relative config (192d4f7 'upgraded'
# arm); the ONLY delta is DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT, applied by the
# production engine AFTER the zone clamp + distance-tier cap (widens the ATR
# trail distance ~5% even when a cap binds; MFE ladder / activation / auto-BE /
# initial SL untouched; never-loosen preserved). s100 pins scale 1.0 explicitly
# so the comparison is hermetic vs ambient .env. Replay arm-param overrides ONLY.
ARM_T3TIGHT_SCALE100_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_T3TIGHT_EXPERIMENTAL_PARAMS,
    "DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT": 1.00,
}
ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS: Dict[str, Any] = {
    **ARM_T3TIGHT_EXPERIMENTAL_PARAMS,
    "DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT": 1.05,
}

# Side-by-side registry for "--arm scale" (upgraded scale 1.00 vs 1.05). Kept
# SEPARATE from TIGHT_COMPARISON_ARMS so "--arm tight" stays byte-identical.
SCALE_COMPARISON_ARMS: Dict[str, Dict[str, Any]] = {
    "s100": {
        "params": ARM_T3TIGHT_SCALE100_EXPERIMENTAL_PARAMS,
        "label": "Upgraded ATR-relative config, trail distance scale 1.00",
    },
    "s105": {
        "params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
        "label": "Upgraded ATR-relative config, trail distance scale 1.05 (+5% retracement room, 09aae20)",
    },
}

# RESEARCH - binding-ladder backtest (binding-ladder-research-brief). Exit
# binding telemetry on the upgraded+1.05 arm showed MFE_LOCK binds at 14/16
# premature layer exits; the CLIPPING tiers are 0.40 (pct 0.45, 12 exits),
# 0.60 (pct 0.55, 1 exit) and 1.00 (pct 0.70, 1 exit). Candidates reduce ONLY
# those tiers' protected-% by 2 / 3 percentage points; activation ratios and
# every other knob (auto 30-pip BE, ceilings, distance tiers, 2% relative ATR,
# 1.05 trail scale, initial SL) stay UNCHANGED. Research only - NOT adopted.
LADDER_CURRENT_TIERS = "0.40:0.45,0.60:0.55,0.80:0.65,1.00:0.70,1.25:0.75,1.50:0.80,2.00:0.85"
LADDER_MINUS2PP_TIERS = "0.40:0.43,0.60:0.53,0.80:0.65,1.00:0.68,1.25:0.75,1.50:0.80,2.00:0.85"
LADDER_MINUS3PP_TIERS = "0.40:0.42,0.60:0.52,0.80:0.65,1.00:0.67,1.25:0.75,1.50:0.80,2.00:0.85"
ARM_LADDER_MINUS2PP_PARAMS: Dict[str, Any] = {
    **ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": LADDER_MINUS2PP_TIERS,
}
ARM_LADDER_MINUS3PP_PARAMS: Dict[str, Any] = {
    **ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
    "DYNAMIC_ATR_MFE_LOCK_TIERS": LADDER_MINUS3PP_TIERS,
}
LADDER_RESEARCH_ARMS: Dict[str, Dict[str, Any]] = {
    "s105": {
        "params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
        "label": "Current ladder (upgraded ATR-relative + 1.05 trail scale)",
    },
    "l2pp": {
        "params": ARM_LADDER_MINUS2PP_PARAMS,
        "label": "Candidate -2pp on clipping tiers 0.40/0.60/1.00 (ratios unchanged)",
    },
    "l3pp": {
        "params": ARM_LADDER_MINUS3PP_PARAMS,
        "label": "Candidate -3pp on clipping tiers 0.40/0.60/1.00 (ratios unchanged)",
    },
}

# RESEARCH - wick-aware activation arms (wick-aware-research-brief). Params are
# IDENTICAL to the current upgraded + 1.05 config (s105); the ONLY difference is
# the scripts-only activation gate applied around the production picker:
#   wc  = A: candle-CLOSE confirmation (close-based MFE/ATR >= tier at a bar close)
#   wp2 = B: N=2 consecutive closes satisfy the tier threshold
#   wp3 = B: N=3 consecutive closes
# Baseline C = s105 (immediate production activation). NOT adopted; research only.
WICK_RESEARCH_ARMS: Dict[str, Dict[str, Any]] = {
    "wc": {
        "params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
        "gate": "close",
        "label": "A: candle-close confirmed MFE activation (close-based ratio)",
    },
    "wp2": {
        "params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
        "gate": "persist2",
        "label": "B: N=2 consecutive close persistence before MFE activation",
    },
    "wp3": {
        "params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS,
        "gate": "persist3",
        "label": "B: N=3 consecutive close persistence before MFE activation",
    },
}

# RESEARCH - ATR-scaled interim fallback floor arms (interim-floor brief).
# Params IDENTICAL to the current upgraded + 1.05 config (s105); the immediate
# MFE ladder is UNCHANGED (no suppression/delay). The ONLY addition is a
# scripts-only extra tightest-stop candidate:
#   ifkXX: floor = entry + sign*0.XX*ATR, armed once MFE >= 0.XX*ATR
#   iffXX: floor = entry + sign*0.XX*MFE, armed once MFE/ATR >= 0.20
# Baseline = s105. NOT adopted; research only.
FLOOR_RESEARCH_ARMS: Dict[str, Dict[str, Any]] = {
    "ifk25": {"params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "floor": "k25",
              "label": "Interim floor k=0.25 x ATR (arms at MFE >= 0.25 ATR)"},
    "ifk35": {"params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "floor": "k35",
              "label": "Interim floor k=0.35 x ATR (arms at MFE >= 0.35 ATR)"},
    "ifk50": {"params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "floor": "k50",
              "label": "Interim floor k=0.50 x ATR (arms at MFE >= 0.50 ATR)"},
    "iff30": {"params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "floor": "f30",
              "label": "Early fraction floor f=0.30 x MFE (arms at ratio >= 0.20)"},
    "iff40": {"params": ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "floor": "f40",
              "label": "Early fraction floor f=0.40 x MFE (arms at ratio >= 0.20)"},
}

# Registry of the experimental trailing arms (order = report order).
TRAILING_EXPERIMENT_ARMS: Dict[str, Dict[str, Any]] = {
    "t1": {
        "params": ARM_T1_EXPERIMENTAL_PARAMS,
        "label": "H1: activation 1.00x -> 0.70x ATR",
    },
    "t2": {
        "params": ARM_T2_EXPERIMENTAL_PARAMS,
        "label": "H2: STANDARD trail clamp $8.00 -> $5.50",
    },
    "t3": {
        "params": ARM_T3_EXPERIMENTAL_PARAMS,
        "label": "H1+H2: activation 0.70x + clamp $5.50",
    },
    "t4": {
        "params": ARM_T4_EXPERIMENTAL_PARAMS,
        "label": "Intermediate: STANDARD trail clamp $8.00 -> $6.50",
    },
}

# Arm modes that drive the production ATR engine on the simulated replay
# clock (Arm A stays the frozen 4544f7f reference with the wall clock).
_ENGINE_ARM_MODES: Tuple[str, ...] = ("b", "c", "cx") + tuple(TRAILING_EXPERIMENT_ARMS) + ("t3tight", "s100", "s105", "l2pp", "l3pp", "wc", "wp2", "wp3", "ifk25", "ifk35", "ifk50", "iff30", "iff40")

# Exact 12 filled Nov 17-19 XAUUSD.s trades (verified against MT5 deals, signals_state.json, copier.log)
TRADE_CATALOG: List[Dict[str, Any]] = [
    {
        "msg_id": "12973",
        "dir": "SELL",
        "open_utc": "2026-11-17T02:36:32Z",
        "close_utc": "2026-11-17T02:44:35Z",
        "layers": [("L1", 0.01, 3865.00)],  # normalized TOP -> L1
        "sl": 3890.0,
        "tp_levels": {"TP1": 3862.0, "TP2": 3852.0, "TP3": 3845.0, "TP4": 3835.0, "TP5": 3825.0},
        "actual_mt5_gbp": 0.92,
        "actual_exit_reason": "TP1 SHORTFALL (Peak 3862.88, Close 3863.79)",
    },
    {
        "msg_id": "9699",
        "dir": "BUY",
        "open_utc": "2026-11-17T11:13:03Z",
        "close_utc": "2026-11-17T11:44:25Z",
        "layers": [("L1", 0.01, 3858.00)],  # normalized TOP -> L1
        "sl": 3850.0,
        "tp_levels": {"TP1": 3865.0, "TP2": 3880.0, "TP3": 3900.0, "TP4": 3910.0, "TP5": 3920.0},
        "actual_mt5_gbp": 3.80,
        "actual_exit_reason": "TP1 SHORTFALL (Peak 3863.82, Close 3863.01; BE 9702)",
    },
    {
        "msg_id": "9705",
        "dir": "BUY",
        "open_utc": "2026-11-17T13:38:40Z",
        "close_utc": "2026-11-17T13:46:59Z",
        "layers": [("L1", 0.01, 3844.00)],  # normalized TOP -> L1
        "sl": 3820.0,
        "tp_levels": {"TP1": 3849.0, "TP2": 3857.0, "TP3": 3870.0, "TP4": 3880.0, "TP5": 3890.0},
        "actual_mt5_gbp": 0.85,
        "actual_exit_reason": "TG Copier Close @ 3845.12 on Msg 9712 TP1 reply",
    },
    {
        "msg_id": "13030",
        "dir": "SELL",
        "open_utc": "2026-11-17T15:42:55Z",
        "close_utc": "2026-11-17T15:44:31Z",
        "layers": [("L1", 0.01, 3885.00), ("L2", 0.01, 3888.33)],
        "sl": 3910.0,
        "tp_levels": {"TP1": 3883.0, "TP2": 3875.0, "TP3": 3873.0, "TP4": 3865.0, "TP5": 3855.0},
        "actual_mt5_gbp": 4.65,
        "actual_exit_reason": "Broker Take Profit Hit (TP1 @ 3883.50/3883.70)",
    },
    {
        "msg_id": "13078",
        "dir": "BUY",
        "open_utc": "2026-11-18T00:23:35Z",
        "close_utc": "2026-11-18T01:10:44Z",
        "layers": [("L1", 0.01, 3833.54), ("L2", 0.01, 3831.33), ("L3", 0.01, 3825.67)],
        "sl": 3813.0,
        "tp_levels": {"TP1": 3840.0, "TP2": 3847.0, "TP3": 3850.0, "TP4": 3857.0, "TP5": 3867.0},
        "actual_mt5_gbp": -23.24,
        "actual_exit_reason": "MT5 Stop-Out after SL 3813",
    },
    {
        "msg_id": "13153",
        "dir": "BUY",
        "open_utc": "2026-11-18T17:52:42Z",
        "close_utc": "2026-11-18T17:53:53Z",
        "layers": [("L1", 0.01, 3730.00)],
        "sl": 3705.0,
        "tp_levels": {"TP1": 3733.0, "TP2": 3740.0, "TP3": 3743.0, "TP4": 3750.0, "TP5": 3760.0},
        "actual_mt5_gbp": 0.44,
        "actual_exit_reason": "GOLD_GUARD (Peak 3732.07, Close 3730.58)",
    },
    {
        "msg_id": "9905",
        "dir": "SELL",
        "open_utc": "2026-11-18T18:22:06Z",
        "close_utc": "2026-11-18T18:23:42Z",
        "layers": [("L1", 0.01, 3739.18), ("L2", 0.01, 3739.18), ("L3", 0.01, 3739.17), ("L4", 0.02, 3739.20)],
        "sl": 3760.0,
        "tp_levels": {"TP1": 3733.0, "TP2": 3725.0, "TP3": 3715.0, "TP4": 3705.0, "TP5": 3695.0},
        "actual_mt5_gbp": -0.05,
        "actual_exit_reason": "Manual close from MT5 app",
    },
    {
        "msg_id": "9934",
        "dir": "SELL",
        "open_utc": "2026-11-18T23:39:39Z",
        "close_utc": "2026-11-19T00:03:35Z",
        "layers": [("L1", 0.01, 3741.70)],
        "sl": 3767.0,
        "tp_levels": {"TP1": 3737.0, "TP2": 3730.0, "TP3": 3715.0, "TP4": 3700.0, "TP5": 3650.0},
        "actual_mt5_gbp": 1.16,
        "actual_exit_reason": "ATR_TRAIL Stg1 (Peak 3737.67, Close 3740.17; ledger £0.00 bug)",
    },
    {
        "msg_id": "13167",
        "dir": "BUY",
        "open_utc": "2026-11-19T01:02:08Z",
        "close_utc": "2026-11-19T01:02:53Z",
        "layers": [("L1", 0.01, 3750.00)],
        "sl": 3725.0,
        "tp_levels": {"TP1": 3753.0, "TP2": 3760.0, "TP3": 3765.0, "TP4": 3770.0, "TP5": 3780.0},
        "actual_mt5_gbp": 0.85,
        "actual_exit_reason": "GOLD_GUARD (Peak 3752.64, Close 3751.12)",
    },
    {
        "msg_id": "9937",
        "dir": "SELL",
        "open_utc": "2026-11-19T06:19:10Z",
        "close_utc": "2026-11-19T07:02:30Z",
        "layers": [("L1", 0.01, 3737.43), ("L2", 0.01, 3740.26)],
        "sl": 3750.0,
        "tp_levels": {"TP1": 3725.0, "TP2": 3710.0, "TP3": 3680.0, "TP4": 3670.0, "TP5": 3660.0},
        "actual_mt5_gbp": 0.56,
        "actual_exit_reason": "BROKER_SL BE (3738.48 after Msg 9939)",
    },
    {
        "msg_id": "9943",
        "dir": "SELL",
        "open_utc": "2026-11-19T07:02:34Z",
        "close_utc": "2026-11-19T07:17:58Z",
        "layers": [("L1", 0.01, 3739.78), ("L2", 0.01, 3739.76)],
        "sl": 3755.0,
        "tp_levels": {"TP1": 3730.0, "TP2": 3725.0, "TP3": 3710.0, "TP4": 3680.0, "TP5": 3670.0},
        "actual_mt5_gbp": 0.45,
        "actual_exit_reason": "BROKER_SL BE (3739.47 after Msg 9947)",
    },
    {
        "msg_id": "9953",
        "dir": "SELL",
        "open_utc": "2026-11-19T07:52:46Z",
        "close_utc": "2026-11-19T08:12:43Z",
        "layers": [("L1", 0.01, 3730.86), ("L2", 0.01, 3732.67), ("L3", 0.01, 3734.33), ("L4", 0.02, 3736.00)],
        "sl": 3740.0,
        "tp_levels": {"TP1": 3700.0, "TP2": 3680.0, "TP3": 3650.0, "TP4": 3640.0, "TP5": 3630.0},
        "actual_mt5_gbp": -25.36,
        "actual_exit_reason": "BROKER_SL (SL=3740.0, all 4 layers)",
    },
]


def _parse_utc(ts_str: str) -> datetime:
    s = ts_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_iso(epoch_sec: int) -> str:
    return datetime.fromtimestamp(int(epoch_sec), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_rollover_utc(iso_str: Optional[str]) -> bool:
    """True if UTC timestamp falls inside rollover window 21:55-23:05 GMT."""
    if not iso_str:
        return False
    dt = _parse_utc(iso_str)
    mins = dt.hour * 60 + dt.minute
    return (21 * 60 + 55) <= mins <= (23 * 60 + 5)


def _compute_rolling_atr(bars: List[Dict[str, Any]], period: int = ATR_PERIOD) -> None:
    """
    Populate `bar['atr14']` on each bar using the exact closed-bar SMA-TR formula
    from `mt5_executor.py:930-949` (`eff_period + 1` preceding closed bars).
    """
    n = len(bars)
    for i in range(n):
        if i < 2:
            bars[i]["atr14"] = 0.0
            continue
        start_idx = max(0, i - (period + 1))
        window = bars[start_idx:i]
        if len(window) < 2:
            bars[i]["atr14"] = 0.0
            continue
        trs: List[float] = []
        prev_close = float(window[0]["close"])
        for j in range(1, len(window)):
            b = window[j]
            h = float(b["high"])
            l = float(b["low"])
            c = float(b["close"])
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            if tr >= 0.0:
                trs.append(tr)
            prev_close = c
        bars[i]["atr14"] = round(sum(trs) / len(trs), 4) if trs else 0.0


def _compute_m5_atr_by_m1(bars: List[Dict[str, Any]], period: int = ATR_PERIOD) -> List[float]:
    """Closed-bar M5 ATR aligned to each M1 bar, for the production ATR engine.

    The production trail engine reads get_atr(symbol, trail_timeframe=M5, ...),
    which in dry-run returns _dry_run_atr. The replay tape is M1, so aggregate the
    M1 bars into M5 buckets, run the same closed-bar SMA-TR formula on the M5 series,
    and give each M1 bar the ATR of the last COMPLETED M5 bar (closed-bar semantics).
    Without this the engine sees ATR=0 and returns before ever calling the picker.
    """
    buckets: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    for b in bars:
        key = (int(b["time_utc"]) // 300) * 300
        agg = buckets.get(key)
        if agg is None:
            buckets[key] = {
                "time_utc": key,
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
            }
            order.append(key)
        else:
            agg["high"] = max(agg["high"], float(b["high"]))
            agg["low"] = min(agg["low"], float(b["low"]))
            agg["close"] = float(b["close"])
    m5_bars = [buckets[k] for k in order]
    _compute_rolling_atr(m5_bars, period)
    atr_by_bucket = {int(mb["time_utc"]): float(mb.get("atr14") or 0.0) for mb in m5_bars}
    out: List[float] = []
    for b in bars:
        key = (int(b["time_utc"]) // 300) * 300
        out.append(atr_by_bucket.get(key - 300, 0.0))
    return out


def _bars_sha256(bars: List[Dict[str, Any]]) -> str:
    """Canonical hash of a bars array (order-sensitive, key-sorted compact JSON)."""
    canonical = json.dumps(bars, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_pinned_tape(payload: Dict[str, Any], path: Path) -> None:
    """Strict load-time validation of a canonical tape fixture.

    Checks bar count, UTC range, symbol, sha256 of the bars array, and the
    exact gap structure. Fails loudly on ANY mismatch; never substitutes.
    """
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        raise RuntimeError(f"Pinned tape validation failed ({path}): bars array missing or empty")
    if payload.get("bar_count") != len(bars):
        raise RuntimeError(
            f"Pinned tape validation failed ({path}): bar_count {payload.get('bar_count')} != actual {len(bars)}"
        )
    first_iso = _utc_iso(bars[0]["time_utc"])
    last_iso = _utc_iso(bars[-1]["time_utc"])
    if first_iso != payload.get("first_utc") or last_iso != payload.get("last_utc"):
        raise RuntimeError(
            f"Pinned tape validation failed ({path}): range {first_iso} -> {last_iso} "
            f"!= pinned {payload.get('first_utc')} -> {payload.get('last_utc')}"
        )
    if payload.get("symbol") != SYMBOL:
        raise RuntimeError(
            f"Pinned tape validation failed ({path}): symbol {payload.get('symbol')!r} != {SYMBOL!r}"
        )
    actual_sha = _bars_sha256(bars)
    if actual_sha != payload.get("bars_sha256"):
        raise RuntimeError(
            f"Pinned tape validation failed ({path}): bars sha256 {actual_sha} "
            f"!= pinned {payload.get('bars_sha256')}"
        )
    ts = [int(b["time_utc"]) for b in bars]
    diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    if any(d <= 0 or d % 60 != 0 for d in diffs):
        raise RuntimeError(
            f"Pinned tape validation failed ({path}): timestamps not strictly increasing minute-aligned M1 bars"
        )
    gaps_meta = payload.get("gaps") or {}
    actual_gaps = [{"after_index": i, "gap_sec": d} for i, d in enumerate(diffs) if d != 60]
    if actual_gaps != list(gaps_meta.get("positions") or []):
        raise RuntimeError(
            f"Pinned tape validation failed ({path}): gap structure {actual_gaps} "
            f"!= pinned {gaps_meta.get('positions')}"
        )
    if len(actual_gaps) != gaps_meta.get("count") or (max(diffs) if diffs else 0) != gaps_meta.get("max_gap_sec"):
        raise RuntimeError(f"Pinned tape validation failed ({path}): gap count/max mismatch")


def load_tape(bars_json_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    """Load a tape file; return (raw_bars, metadata, pinned).

    Schema-tagged canonical fixtures are validated strictly and fail loudly.
    Legacy tapes (no schema) load unvalidated but are marked unpinned so the
    report never claims reproducibility for them.
    """
    payload = json.loads(bars_json_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("schema") == TAPE_SCHEMA:
        _validate_pinned_tape(payload, bars_json_path)
        return payload["bars"], payload, True
    raw_bars = payload["bars"] if isinstance(payload, dict) else payload
    meta = {
        "count": len(raw_bars),
        "first_utc": _utc_iso(raw_bars[0]["time_utc"]) if raw_bars else None,
        "last_utc": _utc_iso(raw_bars[-1]["time_utc"]) if raw_bars else None,
    }
    return raw_bars, meta, False


def fetch_m1_bars(
    bars_json_path: Optional[Path] = None,
    save_bars_path: Optional[Path] = None,
    spread_override: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Load M1 bars from JSON or fetch read-only from MT5 (`copy_rates_range`),
    normalize server timestamps (`-7200s` -> UTC), compute spread and ATR(14).
    """
    bars: List[Dict[str, Any]] = []
    if bars_json_path is not None:
        raw_bars, _tape_meta, _tape_pinned = load_tape(bars_json_path)
        for b in raw_bars:
            bars.append(
                {
                    "time_utc": int(b["time_utc"]),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "spread_pts": int(b.get("spread_pts", 32)),
                    "spread_dollars": round(
                        float(spread_override)
                        if spread_override is not None
                        else max(0.05, float(b.get("spread_dollars", int(b.get("spread_pts", 32)) * 0.01))),
                        4,
                    ),
                }
            )
    else:
        import MetaTrader5 as mt5

        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        try:
            rates = mt5.copy_rates_range(
                SYMBOL,
                mt5.TIMEFRAME_M1,
                WARMUP_START_MT5,
                WARMUP_END_MT5,
            )
            if rates is None or len(rates) == 0:
                raise RuntimeError(f"mt5.copy_rates_range returned no bars: {mt5.last_error()}")
            for r in rates:
                t_utc = int(r["time"]) - MT5_SERVER_OFFSET_SEC
                sp_pts = int(r["spread"])
                sp_dlr = (
                    round(float(spread_override), 4)
                    if spread_override is not None
                    else round(max(0.05, sp_pts * 0.01), 4)
                )
                bars.append(
                    {
                        "time_utc": t_utc,
                        "open": round(float(r["open"]), 2),
                        "high": round(float(r["high"]), 2),
                        "low": round(float(r["low"]), 2),
                        "close": round(float(r["close"]), 2),
                        "spread_pts": sp_pts,
                        "spread_dollars": sp_dlr,
                    }
                )
        finally:
            mt5.shutdown()

    bars.sort(key=lambda x: x["time_utc"])
    _compute_rolling_atr(bars, period=ATR_PERIOD)

    if save_bars_path is not None:
        save_bars_path.parent.mkdir(parents=True, exist_ok=True)
        out_payload = {
            "symbol": SYMBOL,
            "timeframe": "M1",
            "server_offset_sec": MT5_SERVER_OFFSET_SEC,
            "atr_period": ATR_PERIOD,
            "bar_count": len(bars),
            "first_utc": _utc_iso(bars[0]["time_utc"]) if bars else None,
            "last_utc": _utc_iso(bars[-1]["time_utc"]) if bars else None,
            "bars": bars,
        }
        save_bars_path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")

    return bars


def _write_tape_fixture(
    path: Path,
    bars: List[Dict[str, Any]],
    fetched_utc: str,
    source: str,
    canonical: bool = False,
) -> Dict[str, Any]:
    """Write a schema-tagged tape fixture (metadata + bars) for review/promotion."""
    ts = [int(b["time_utc"]) for b in bars]
    diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    gaps = [{"after_index": i, "gap_sec": d} for i, d in enumerate(diffs) if d != 60]
    meta: Dict[str, Any] = {
        "schema": TAPE_SCHEMA,
        "canonical": canonical,
        "symbol": SYMBOL,
        "timeframe": "M1",
        "atr_period": ATR_PERIOD,
        "server_offset_sec": MT5_SERVER_OFFSET_SEC,
        "gbp_rate_per_pt_001_lot": GBP_PER_PT_001_LOT,
        "bar_count": len(bars),
        "first_utc": _utc_iso(ts[0]) if ts else None,
        "last_utc": _utc_iso(ts[-1]) if ts else None,
        "bars_sha256": _bars_sha256(bars),
        "bars_sha256_algorithm": "sha256(utf8(json.dumps(bars, sort_keys=True, separators=(',',':'))))",
        "fetched_utc": fetched_utc,
        "source": source,
        "spread_policy": "per-bar broker spread_pts; spread_dollars = max(0.05, spread_pts * 0.01) unless --spread-override",
        "gaps": {"count": len(gaps), "max_gap_sec": max(diffs) if diffs else 0, "positions": gaps},
    }
    payload = dict(meta)
    payload["bars"] = bars
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return meta


def _refresh_tape(target: Path, spread_override: Optional[float]) -> int:
    """Explicit live re-fetch to a NEW tape file + diff summary for human review.

    Never overwrites the canonical fixture in place; promotion is a separate,
    deliberate human-reviewed step.
    """
    resolved = target.resolve()
    if resolved == PINNED_TAPE_PATH.resolve():
        raise RuntimeError("--refresh-tape must never overwrite the canonical fixture in place; choose a NEW file")
    if resolved.exists():
        raise RuntimeError(f"--refresh-tape target already exists: {resolved}; choose a NEW file")
    new_bars = fetch_m1_bars(bars_json_path=None, spread_override=spread_override)
    fetched_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = _write_tape_fixture(
        resolved,
        new_bars,
        fetched_utc=fetched_utc,
        source=(
            f"live MT5 copy_rates_range refresh {WARMUP_START_MT5.isoformat()} -> "
            f"{WARMUP_END_MT5.isoformat()} (server time)"
        ),
        canonical=False,
    )
    print(f"Refreshed tape written to NEW file: {resolved}")
    print(f"  bars={meta['bar_count']} range={meta['first_utc']} -> {meta['last_utc']}")
    print(f"  sha256={meta['bars_sha256']}")
    try:
        old_bars, old_meta, _pinned = load_tape(PINNED_TAPE_PATH)
    except Exception as exc:
        print(f"  canonical fixture unavailable for diff ({exc}); review the new file manually")
        return 0
    print("Diff summary vs canonical fixture (human review required before promotion):")
    print(f"  bar_count: canonical={len(old_bars)} new={len(new_bars)}")
    print(
        f"  range: canonical={old_meta.get('first_utc')} -> {old_meta.get('last_utc')} "
        f"| new={meta['first_utc']} -> {meta['last_utc']}"
    )
    print(f"  sha256 identical: {old_meta.get('bars_sha256') == meta['bars_sha256']}")
    print(
        f"  gaps: canonical={len((old_meta.get('gaps') or {}).get('positions') or [])} "
        f"new={meta['gaps']['count']}"
    )
    n = min(len(old_bars), len(new_bars))
    first_diff = next((i for i in range(n) if old_bars[i] != new_bars[i]), None)
    diff_count = sum(1 for i in range(n) if old_bars[i] != new_bars[i]) + abs(len(old_bars) - len(new_bars))
    print(f"  differing bars: {diff_count} (first divergent index: {first_diff})")
    print("Promotion is manual: review the diff, then replace the canonical fixture deliberately.")
    return 0


def _find_entry_bar_index(bars: List[Dict[str, Any]], open_utc: str) -> int:
    """Return index of the M1 bar containing `open_utc` (`time_utc <= open_ts < time_utc + 60`)."""
    open_ts = int(_parse_utc(open_utc).timestamp())
    for idx, b in enumerate(bars):
        bt = int(b["time_utc"])
        if bt <= open_ts < bt + 60:
            return idx
    for idx, b in enumerate(bars):
        if int(b["time_utc"]) >= open_ts:
            return idx
    raise RuntimeError(f"No M1 bar found for open_utc={open_utc}")


def _compute_potential_mfe_before_sl(
    trade: Dict[str, Any],
    bars: List[Dict[str, Any]],
    idx0: int,
) -> float:
    """Measure potential favorable excursion ($/oz from avg entry) before initial SL is hit."""
    is_buy = trade["dir"] == "BUY"
    sl = float(trade["sl"])
    total_lot = sum(l[1] for l in trade["layers"])
    avg_entry = sum(l[1] * l[2] for l in trade["layers"]) / total_lot
    max_fav = 0.0
    for b in bars[idx0 + 1 :]:
        h = float(b["high"])
        l = float(b["low"])
        sp = float(b["spread_dollars"])
        if is_buy:
            fav = h - avg_entry
            if fav > max_fav:
                max_fav = fav
            if l <= sl + 1e-9:
                break
        else:
            fav = avg_entry - (l + sp)
            if fav > max_fav:
                max_fav = fav
            if (h + sp) >= sl - 1e-9:
                break
    return round(max(0.0, max_fav), 2)


def _bar_tick_sequence(bar: Dict[str, Any]) -> List[Tuple[float, float]]:
    """
    Deterministic intra-minute tick path:
      - Bull (`C >= O`): O -> L -> H -> C
      - Bear (`C < O`):  O -> H -> L -> C
    Duplicated 2x per point so `REVERSAL_CONFIRM_TICKS=2` triggers deterministically.
    Returns list of `(bid, ask)` tuples.
    """
    o = float(bar["open"])
    h = float(bar["high"])
    l = float(bar["low"])
    c = float(bar["close"])
    sp = float(bar["spread_dollars"])
    pts = [o, l, h, c] if c >= o else [o, h, l, c]
    ticks: List[Tuple[float, float]] = []
    for p in pts:
        bid = round(p, 2)
        ask = round(p + sp, 2)
        ticks.append((bid, ask))
        ticks.append((bid, ask))
    return ticks


# ---------------------------------------------------------------------------
# ISOLATED WORKER IMPLEMENTATION (executed inside subprocess per Arm)
# ---------------------------------------------------------------------------

def _shutdown_copier_loggers() -> None:
    for name in ("copier", "telethon"):
        log_obj = logging.getLogger(name)
        for h in list(log_obj.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    h.close()
                except Exception:
                    pass
                log_obj.removeHandler(h)


class BrokerSpy:
    """
    Simulates real MT5 broker SL/TP state, partial closes, full closes, and
    deal-level GBP PnL (`pts * (lot / 0.01) * 0.7589`) on top of `DRY_RUN=true`.
    """

    def __init__(self, executor: Any, is_buy: bool) -> None:
        self.executor = executor
        self.is_buy = is_buy
        self.positions: Dict[int, Dict[str, Any]] = {}
        self.closed_events: List[Dict[str, Any]] = []
        self.partial_deals_by_ticket: Dict[int, List[Dict[str, Any]]] = {}
        self.closed_profit_by_ticket: Dict[int, float] = {}
        self.last_exit_price_by_ticket: Dict[int, float] = {}
        self.last_close_reason_by_ticket: Dict[int, str] = {}
        self.current_tick: Optional[SimpleNamespace] = None
        self.current_utc: str = ""
        self._deal_seq: int = 900000
        # Stop-modification telemetry (reporting only; surfaced opt-in).
        self.modify_attempts: int = 0
        self.modify_failures: int = 0

    def register_position(
        self,
        ticket: int,
        layer_key: str,
        lot: float,
        entry_price: float,
        sl: Optional[float],
        tp: Optional[float],
    ) -> None:
        self.positions[int(ticket)] = {
            "ticket": int(ticket),
            "layer": layer_key,
            "lot": round(float(lot), 4),
            "initial_lot": round(float(lot), 4),
            "entry_price": float(entry_price),
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
            "open": True,
        }

    def _next_deal_id(self) -> int:
        self._deal_seq += 1
        return self._deal_seq

    def _calc_gbp(self, entry_px: float, exit_px: float, closed_lot: float) -> Tuple[float, float]:
        pts = (exit_px - entry_px) if self.is_buy else (entry_px - exit_px)
        gbp = pts * (closed_lot / 0.01) * GBP_PER_PT_001_LOT
        return round(pts, 4), round(gbp, 2)

    def _current_exec_exit_price(self) -> float:
        assert self.current_tick is not None
        return float(self.current_tick.bid) if self.is_buy else float(self.current_tick.ask)

    def record_close(
        self,
        ticket: int,
        closed_lot: float,
        exit_price: float,
        reason: str,
        is_partial: bool = False,
    ) -> float:
        t_int = int(ticket)
        pos = self.positions.get(t_int)
        if not pos or not pos["open"] or pos["lot"] <= 0.0:
            return 0.0
        eff_lot = round(min(float(closed_lot), pos["lot"]), 4)
        pts, gbp = self._calc_gbp(pos["entry_price"], exit_price, eff_lot)
        did = self._next_deal_id()
        deal_rec = {
            "deal": did,
            "deal_ticket": did,
            "profit": gbp,
            "volume": eff_lot,
            "price": round(exit_price, 4),
            "reason": reason,
        }
        self.partial_deals_by_ticket.setdefault(t_int, []).append(deal_rec)
        self.closed_profit_by_ticket[t_int] = round(self.closed_profit_by_ticket.get(t_int, 0.0) + gbp, 2)
        self.last_exit_price_by_ticket[t_int] = round(exit_price, 4)
        self.last_close_reason_by_ticket[t_int] = reason
        self.closed_events.append(
            {
                "ticket": t_int,
                "layer": pos["layer"],
                "lot": eff_lot,
                "entry_price": pos["entry_price"],
                "exit_price": round(exit_price, 4),
                "pts": pts,
                "gbp": gbp,
                "reason": reason,
                "is_partial": is_partial,
                "exit_utc": self.current_utc,
            }
        )
        rem = round(pos["lot"] - eff_lot, 4)
        pos["lot"] = rem
        if rem <= 1e-6:
            pos["lot"] = 0.0
            pos["open"] = False
            self.executor._dry_run_ticket_lots.pop(t_int, None)
            self.executor._bot_closed_tickets.add(t_int)
        else:
            self.executor._dry_run_ticket_lots[t_int] = rem
        self.executor._cached_pos_profits[t_int] = self.closed_profit_by_ticket[t_int]
        return gbp

    def total_gbp(self) -> float:
        return round(sum(e["gbp"] for e in self.closed_events), 2)

    def total_pts_lot(self) -> float:
        return round(sum(e["pts"] * e["lot"] for e in self.closed_events), 6)

    def last_exit_price(self) -> Optional[float]:
        return self.closed_events[-1]["exit_price"] if self.closed_events else None

    def last_exit_utc(self) -> Optional[str]:
        return self.closed_events[-1]["exit_utc"] if self.closed_events else None


def _install_broker_spy(executor: Any, spy: BrokerSpy) -> None:
    orig_modify = executor.modify_position_sltp
    orig_close = executor.close_market_position
    orig_partial = getattr(executor, "close_partial_volume", None)

    def _spy_modify_position_sltp(
        ticket: int, sl: Optional[float] = None, tp: Optional[float] = None, **kwargs: Any
    ) -> bool:
        spy.modify_attempts += 1
        ok = orig_modify(ticket, sl, tp, **kwargs)
        if not ok:
            spy.modify_failures += 1
        if ok:
            pos = spy.positions.get(int(ticket))
            if pos and pos["open"]:
                if sl is not None and float(sl) > 0.0:
                    pos["sl"] = float(sl)
                if tp is not None and float(tp) > 0.0:
                    pos["tp"] = float(tp)
        return ok

    def _spy_close_market_position(ticket: int) -> bool:
        t_int = int(ticket)
        pos = spy.positions.get(t_int)
        rem_lot = pos["lot"] if (pos and pos["open"]) else 0.0
        ok = orig_close(ticket)
        if ok and rem_lot > 0.0:
            exit_px = spy._current_exec_exit_price()
            spy.record_close(t_int, rem_lot, exit_px, reason="MANUAL/BOT", is_partial=False)
        return ok

    def _spy_close_partial_volume(
        ticket: Optional[int],
        close_volume: float,
        current_volume: Optional[float] = None,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        res = orig_partial(
            ticket,
            close_volume,
            current_volume=current_volume,
            symbol=symbol,
            direction=direction,
        )
        if isinstance(res, dict) and res.get("ok") and ticket:
            c_vol = float(res.get("closed_volume") or close_volume)
            exit_px = spy._current_exec_exit_price()
            spy.record_close(int(ticket), c_vol, exit_px, reason="MANUAL/BOT", is_partial=True)
        return res

    def _spy_get_position_close_info(ticket: Optional[int]) -> Dict[str, Any]:
        if not ticket:
            return {"profit": 0.0, "reason": "UNKNOWN"}
        t_int = int(ticket)
        deals = list(spy.partial_deals_by_ticket.get(t_int) or [])
        total_p = round(spy.closed_profit_by_ticket.get(t_int, 0.0), 2)
        reason = spy.last_close_reason_by_ticket.get(t_int, "MANUAL/BOT")
        exit_px = spy.last_exit_price_by_ticket.get(t_int)
        pos = spy.positions.get(t_int)
        entry_px = pos["entry_price"] if pos else None
        rich = {
            "profit": total_p,
            "reason": reason,
            "deals_settled": True,
            "exit_price": exit_px,
            "entry_price": entry_px,
            "exit_deal_tickets": [d["deal_ticket"] for d in deals],
            "exit_deals": deals,
        }
        if hasattr(executor, "_last_close_details") and isinstance(executor._last_close_details, dict):
            executor._last_close_details[t_int] = rich
        return {"profit": total_p, "reason": reason, "exit_deals": deals}

    executor.modify_position_sltp = _spy_modify_position_sltp
    executor.close_market_position = _spy_close_market_position
    if orig_partial is not None:
        executor.close_partial_volume = _spy_close_partial_volume
    executor.get_position_close_info = _spy_get_position_close_info


def _derive_max_stage(sig: Dict[str, Any], main_mod: Any) -> int:
    stage = 0
    if sig.get("tp1_reached"):
        stage = max(stage, 1)
    if sig.get("tp2_reached"):
        stage = max(stage, 2)
    if sig.get("tp3_reached"):
        stage = max(stage, 3)
    if sig.get("tp4_reached"):
        stage = max(stage, 4)
    if sig.get("tp5_reached"):
        stage = max(stage, 5)
    for ms in (sig.get("runner_cleared_milestones") or []):
        s_ms = str(ms).strip().upper()
        if s_ms.startswith("TP") and s_ms[2:].isdigit():
            stage = max(stage, int(s_ms[2:]))
    if hasattr(main_mod, "_get_signal_stage"):
        try:
            stage = max(stage, int(main_mod._get_signal_stage(sig)))
        except Exception:
            pass
    es = sig.get("exit_stage")
    if isinstance(es, int) and not isinstance(es, bool):
        stage = max(stage, es)
    return stage


def _build_signal_record(
    main_mod: Any,
    trade: Dict[str, Any],
    base_ticket: int,
    spy: BrokerSpy,
) -> Dict[str, Any]:
    is_buy = trade["dir"] == "BUY"
    raw_tp1 = float(trade["tp_levels"]["TP1"])
    final_tp = round(raw_tp1 - 0.50, 2) if is_buy else round(raw_tp1 + 0.50, 2)
    sl = float(trade["sl"])

    layers: Dict[str, Dict[str, Any]] = {}
    market_tickets: List[int] = []
    for idx, (lk, lot, fill_px) in enumerate(trade["layers"]):
        tkt = base_ticket + idx
        market_tickets.append(tkt)
        layers[lk] = {
            "lot": float(lot),
            "target_price": float(fill_px),
            "fill_price": float(fill_px),
            "entry_price": float(fill_px),
            "sl": sl,
            # The live path captures the stop the order was placed with as
            # `initial_sl` (mt5_executor); the R-relative profit floor reads
            # it to compute R. Without it the R lock is silently skipped and
            # Arm C would not exercise the feature at all.
            "initial_sl": sl,
            "tp": final_tp,
            "tp_stage": "TP1",
            "ticket": tkt,
            "order_type": "MARKET",
            "status": "OPEN",
        }
        main_mod.executor._dry_run_ticket_lots[tkt] = float(lot)
        spy.register_position(tkt, lk, float(lot), float(fill_px), sl=sl, tp=final_tp)

    tp_levels = dict(trade["tp_levels"])
    sig = main_mod.state_mgr.create_signal(
        message_id=trade["msg_id"],
        symbol=SYMBOL,
        direction=trade["dir"],
        layers=layers,
        zone=None,
        market_tickets=market_tickets,
        pending_tickets=[],
        zone_completed=True,
        sl=sl,
        raw_tp1=raw_tp1,
        raw_tp2=tp_levels.get("TP2"),
        raw_tp3=tp_levels.get("TP3"),
        raw_tp4=tp_levels.get("TP4"),
        raw_tp5=tp_levels.get("TP5"),
        tp_levels=tp_levels,
        final_tp=final_tp,
        max_orders=len(trade["layers"]),
        is_zone=len(trade["layers"]) > 1,
        recorded_entry_price=float(trade["layers"][0][2]),
        initial_price=float(trade["layers"][0][2]),
        chat_id=CHAT_ID,
    )
    sig["status"] = "ACTIVE"
    sig["broker_sl"] = sl
    sig["runner_max_tp_stage"] = 5
    sig["timestamp"] = trade["open_utc"]
    main_mod.state_mgr.update_signal(trade["msg_id"], sig, chat_id=CHAT_ID)
    return main_mod.state_mgr.get_signal(trade["msg_id"], chat_id=CHAT_ID)


async def _simulate_broker_stops(
    main_mod: Any,
    sig: Dict[str, Any],
    spy: BrokerSpy,
    tick: SimpleNamespace,
    check_sl: bool,
    check_tp: bool,
) -> Optional[str]:
    is_buy = spy.is_buy
    exec_px = float(tick.bid) if is_buy else float(tick.ask)
    layers = sig.get("layers") or {}
    closed_any: List[Tuple[int, str, float, str]] = []

    for tkt, pos in list(spy.positions.items()):
        if not pos["open"] or pos["lot"] <= 0.0:
            continue
        if check_sl and pos["sl"] is not None and pos["sl"] > 0.0:
            sl_px = float(pos["sl"])
            hit_sl = (exec_px <= sl_px + 1e-9) if is_buy else (exec_px >= sl_px - 1e-9)
            if hit_sl:
                in_profit = (sl_px > pos["entry_price"] + 1e-6) if is_buy else (sl_px < pos["entry_price"] - 1e-6)
                lv_obj = layers.get(pos["layer"]) if isinstance(layers, dict) else None
                if not in_profit:
                    reason = "BROKER_SL"
                elif isinstance(lv_obj, dict) and lv_obj.get("ratchet_tier"):
                    reason = "PROFIT_RATCHET_SL"
                elif sig.get("tp1_reached"):
                    src = str(sig.get("active_stop_source") or "").strip().upper()
                    reason = f"{src}_SL" if src else "RUNNER_TRAIL_SL"
                elif sig.get("stage0_profit_lock_active"):
                    reason = "STAGE0_PROFIT_LOCK_SL"
                else:
                    reason = "PROFIT_LOCK_SL"
                spy.record_close(tkt, pos["lot"], sl_px, reason="SL", is_partial=False)
                closed_any.append((tkt, pos["layer"], sl_px, reason))
                continue
        if check_tp and pos["tp"] is not None and pos["tp"] > 0.0:
            tp_px = float(pos["tp"])
            hit_tp = (exec_px >= tp_px - 1e-9) if is_buy else (exec_px <= tp_px + 1e-9)
            if hit_tp:
                spy.record_close(tkt, pos["lot"], tp_px, reason="TP", is_partial=False)
                closed_any.append((tkt, pos["layer"], tp_px, "BROKER_TP"))

    if not closed_any:
        return None

    closed_tkts = [c[0] for c in closed_any]
    closed_set = set(closed_tkts)
    for tkt, lk, exit_px, reason_code in closed_any:
        lv = layers.get(lk)
        if isinstance(lv, dict):
            lv["status"] = "CLOSED"
            lv["exit_price"] = round(exit_px, 4)
            lv["exit_reason_code"] = reason_code

    sig["market_tickets"] = [t for t in (sig.get("market_tickets") or []) if int(t) not in closed_set]
    if sig.get("runner_tickets"):
        sig["runner_tickets"] = [t for t in sig["runner_tickets"] if int(t) not in closed_set]
        sig["runner_ticket"] = sig["runner_tickets"][-1] if sig["runner_tickets"] else None

    await main_mod._account_closed_tickets(sig, closed_tkts)

    if not sig.get("market_tickets"):
        last_px = closed_any[-1][2]
        last_reason = closed_any[-1][3]
        sig["status"] = "CLOSED"
        sig["exit_price"] = round(last_px, 4)
        sig["exit_reason_code"] = last_reason
        sig["runner_close_reason"] = last_reason
        main_mod.state_mgr.update_signal(sig.get("msg_id") or closed_any[-1][0], sig, chat_id=CHAT_ID)
        return last_reason
    return None


def _pin_arm_b_params(config_mod: Any, executor_mod: Any, main_mod: Any) -> None:
    """Explicitly pin and verify calibrated v3.6.0 parameters across config, executor, and main."""
    for key, val in ARM_B_CALIBRATED_PARAMS.items():
        setattr(config_mod, key, val)
        if hasattr(executor_mod, key):
            setattr(executor_mod, key, val)
        if hasattr(main_mod, key):
            setattr(main_mod, key, val)
    if hasattr(config_mod, "validate_ratchet_cushion_pairs"):
        config_mod.validate_ratchet_cushion_pairs()
    if hasattr(main_mod, "_MAIN_CFG_SNAPSHOT") and isinstance(main_mod._MAIN_CFG_SNAPSHOT, dict):
        for k in list(main_mod._MAIN_CFG_SNAPSHOT.keys()):
            if k in ARM_B_CALIBRATED_PARAMS:
                main_mod._MAIN_CFG_SNAPSHOT[k] = ARM_B_CALIBRATED_PARAMS[k]

    for key, expected in ARM_B_CALIBRATED_PARAMS.items():
        actual = getattr(config_mod, key)
        if isinstance(expected, float):
            if abs(float(actual) - expected) > 1e-9:
                raise RuntimeError(f"Arm B calibration mismatch for {key}: {actual} != {expected}")
        elif actual != expected:
            raise RuntimeError(f"Arm B calibration mismatch for {key}: {actual!r} != {expected!r}")


def _pin_arm_c_params(config_mod: Any, executor_mod: Any, main_mod: Any) -> None:
    """Explicitly pin and verify the Arm C R-lock parameters across config, executor, and main."""
    targets = (config_mod, executor_mod, main_mod)
    for key, expected in ARM_C_CALIBRATED_PARAMS.items():
        for mod in targets:
            if hasattr(mod, key):
                setattr(mod, key, expected)
        actual = getattr(config_mod, key, None)
        if isinstance(expected, float):
            if abs(float(actual or 0.0) - expected) > 1e-9:
                raise RuntimeError(f"Arm C calibration mismatch for {key}: {actual} != {expected}")
        elif actual != expected:
            raise RuntimeError(f"Arm C calibration mismatch for {key}: {actual!r} != {expected!r}")
    if hasattr(main_mod, "_MAIN_CFG_SNAPSHOT") and isinstance(main_mod._MAIN_CFG_SNAPSHOT, dict):
        for k in list(main_mod._MAIN_CFG_SNAPSHOT.keys()):
            if k in ARM_C_CALIBRATED_PARAMS:
                main_mod._MAIN_CFG_SNAPSHOT[k] = ARM_C_CALIBRATED_PARAMS[k]


def _pin_arm_cx_params(config_mod: Any, executor_mod: Any, main_mod: Any) -> None:
    """EXPERIMENTAL ONLY: pin the re-tiered R-lock ladder override (arm cx).

    Applied AFTER the Arm B + Arm C pins so the only delta vs Arm C is
    R_LOCK_TIERS. Never touches production defaults; replay arm-param override.
    """
    targets = (config_mod, executor_mod, main_mod)
    for key, expected in ARM_CX_EXPERIMENTAL_PARAMS.items():
        for mod in targets:
            if hasattr(mod, key):
                setattr(mod, key, expected)
        actual = getattr(config_mod, key, None)
        if actual != expected:
            raise RuntimeError(f"Arm CX experimental calibration mismatch for {key}: {actual!r} != {expected!r}")
    if hasattr(main_mod, "_MAIN_CFG_SNAPSHOT") and isinstance(main_mod._MAIN_CFG_SNAPSHOT, dict):
        for k in list(main_mod._MAIN_CFG_SNAPSHOT.keys()):
            if k in ARM_CX_EXPERIMENTAL_PARAMS:
                main_mod._MAIN_CFG_SNAPSHOT[k] = ARM_CX_EXPERIMENTAL_PARAMS[k]


def _pin_arm_experimental_params(
    params: Dict[str, Any],
    config_mod: Any,
    executor_mod: Any,
    main_mod: Any,
    label: str,
) -> None:
    """EXPERIMENTAL ONLY: pin a replay arm-param override set (arms t1..t4).

    Applied AFTER the Arm B pins so the only delta vs the pinned baseline B
    is the named experimental knob(s). Never touches production defaults;
    this is a replay-only override mechanism, clearly labelled experimental.
    """
    targets = (config_mod, executor_mod, main_mod)
    for key, expected in params.items():
        for mod in targets:
            if hasattr(mod, key):
                setattr(mod, key, expected)
        actual = getattr(config_mod, key, None)
        if isinstance(expected, float):
            if abs(float(actual or 0.0) - expected) > 1e-9:
                raise RuntimeError(f"{label} experimental calibration mismatch for {key}: {actual} != {expected}")
        elif actual != expected:
            raise RuntimeError(f"{label} experimental calibration mismatch for {key}: {actual!r} != {expected!r}")
    if hasattr(main_mod, "_MAIN_CFG_SNAPSHOT") and isinstance(main_mod._MAIN_CFG_SNAPSHOT, dict):
        for k in list(main_mod._MAIN_CFG_SNAPSHOT.keys()):
            if k in params:
                main_mod._MAIN_CFG_SNAPSHOT[k] = params[k]


class _ReplayClock:
    """The simulated UTC clock of a replay worker.

    The trade-age guard reads the wall clock (datetime.now(timezone.utc)),
    but the replay simulates the RECORDED Nov 17-19 week. Without a simulated
    clock every recorded trade would look days old and the guard would flatten
    it on its very first tick, which is not a replay of the recorded week at
    all. The clock therefore follows the M1 bars, and Arm A keeps the wall
    clock because it is the frozen historical baseline.
    """

    def __init__(self) -> None:
        self._now: Optional[datetime] = None

    def set_epoch(self, epoch_sec: int) -> None:
        """Move the simulated clock to the given UTC epoch second."""
        self._now = datetime.fromtimestamp(int(epoch_sec), tz=timezone.utc)

    def now(self, tz: Optional[Any] = None) -> datetime:
        """Return the simulated time (the wall clock when no bar was seen yet)."""
        base = self._now or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        if tz is None:
            return base.replace(tzinfo=None)
        return base.astimezone(tz)


_REPLAY_CLOCK = _ReplayClock()


def _install_replay_clock(main_mod: Any) -> None:
    """Make main read the simulated replay clock instead of the wall clock."""
    real_datetime = datetime

    class _SimulatedDatetime(real_datetime):
        @classmethod
        def now(cls, tz: Optional[Any] = None) -> datetime:
            return _REPLAY_CLOCK.now(tz)

    main_mod.datetime = _SimulatedDatetime


async def _run_single_trade_worker(
    main_mod: Any,
    state_cls: Any,
    ledger_cls: Any,
    exec_cls: Any,
    runtime_dir: Path,
    trade: Dict[str, Any],
    bars: List[Dict[str, Any]],
    trade_idx: int,
) -> Dict[str, Any]:
    msg_id = trade["msg_id"]
    is_buy = trade["dir"] == "BUY"
    total_lot = round(sum(l[1] for l in trade["layers"]), 4)
    avg_entry = round(sum(l[1] * l[2] for l in trade["layers"]) / total_lot, 4)
    _reset_picker_trace(msg_id)
    if _PROTECTION_TELEMETRY:
        _reset_protection_trace(msg_id)
    if _WICK_TRACKER is not None:
        _WICK_TRACKER.reset()
    if _INTERIM_FLOOR_SPEC:
        _reset_interim_floor_stats()
    m5_atr_series = _compute_m5_atr_by_m1(bars) if _PRODUCTION_ENGINE_ARM else None

    # Fresh isolated runtime state per trade
    state_path = runtime_dir / f"state_{msg_id}.json"
    ledger_path = runtime_dir / f"ledger_{msg_id}.jsonl"
    if state_path.exists():
        state_path.unlink()
    if ledger_path.exists():
        ledger_path.unlink()

    main_mod.state_mgr = state_cls(state_path)
    main_mod.trade_ledger = ledger_cls(ledger_path)
    main_mod.executor = exec_cls(dry_run=True)
    main_mod.executor.connect()
    if hasattr(main_mod, "_runner_save_throttle_ts"):
        main_mod._runner_save_throttle_ts.clear()

    spy = BrokerSpy(main_mod.executor, is_buy=is_buy)
    _install_broker_spy(main_mod.executor, spy)

    idx0 = _find_entry_bar_index(bars, trade["open_utc"])
    pot_mfe = _compute_potential_mfe_before_sl(trade, bars, idx0)

    # The simulated clock starts at the entry bar and follows the M1 bars,
    # so the trade-age guard measures the age of the RECORDED trade.
    _REPLAY_CLOCK.set_epoch(int(bars[idx0]["time_utc"]))

    sig = _build_signal_record(main_mod, trade, base_ticket=100000 + trade_idx * 100, spy=spy)
    sig["msg_id"] = msg_id

    peak_mfe = 0.0
    max_stage = 0
    exit_reason: Optional[str] = None
    exit_utc: Optional[str] = None
    error_msg: Optional[str] = None

    # Optional scripted explicit Telegram BE command (attachment section 9).
    # Drives the SAME production path the Telegram handler uses
    # (mt5_executor.apply_signal_breakeven) at the first bar >= be_command_utc.
    be_cmd_ts: Optional[int] = None
    if trade.get("be_command_utc"):
        be_cmd_ts = int(_parse_utc(trade["be_command_utc"]).timestamp())
    be_cmd_fired = False
    explicit_be_info: Optional[Dict[str, Any]] = None

    try:
        for b_idx in range(idx0 + 1, len(bars)):
            bar = bars[b_idx]
            prev_bar = bars[b_idx - 1]
            # RESEARCH: the previous bar has now CLOSED - evaluate close-based
            # confirmation/persistence for every registered layer before any
            # tick of the new bar can bind the ladder lock.
            if _WICK_TRACKER is not None:
                _WICK_TRACKER.finalize_bar(
                    float(prev_bar["close"]),
                    float(m5_atr_series[b_idx - 1]) if m5_atr_series is not None
                    else float(prev_bar.get("atr14") or 0.0),
                )
            bar_utc = _utc_iso(bar["time_utc"])
            _REPLAY_CLOCK.set_epoch(int(bar["time_utc"]))
            main_mod.executor._dry_run_m1_atr = float(bar.get("atr14") or 0.0)
            if m5_atr_series is not None:
                main_mod.executor._dry_run_atr = m5_atr_series[b_idx]
            main_mod.executor._dry_run_last_m1_candle = {
                "open": float(prev_bar["open"]),
                "high": float(prev_bar["high"]),
                "low": float(prev_bar["low"]),
                "close": float(prev_bar["close"]),
                "time": float(prev_bar["time_utc"]),
            }

            for bid, ask in _bar_tick_sequence(bar):
                tick = SimpleNamespace(bid=bid, ask=ask, time=bar["time_utc"])
                spy.current_tick = tick
                spy.current_utc = bar_utc
                main_mod.executor.get_tick = lambda _sym, _t=tick: _t

                if be_cmd_ts is not None and not be_cmd_fired and int(bar["time_utc"]) >= be_cmd_ts:
                    be_cmd_fired = True
                    _cmd_txt = trade.get("be_command_text") or "Set breakeven"
                    try:
                        _be_res = main_mod.executor.apply_signal_breakeven(sig)
                        explicit_be_info = {
                            "command": _cmd_txt,
                            "utc": bar_utc,
                            "modified_tickets": len(_be_res.get("modified_tickets") or []),
                            "deferred_tickets": len(_be_res.get("deferred_tickets") or []),
                        }
                    except Exception as exc:
                        explicit_be_info = {"command": _cmd_txt, "utc": bar_utc, "error": f"{type(exc).__name__}: {exc}"}

                exec_px = bid if is_buy else ask
                cur_fav = (exec_px - avg_entry) if is_buy else (avg_entry - exec_px)
                if cur_fav > peak_mfe:
                    peak_mfe = cur_fav

                # 1) Check broker SL before FSM tick
                sl_closed_reason = await _simulate_broker_stops(
                    main_mod, sig, spy, tick, check_sl=True, check_tp=False
                )
                if sl_closed_reason is not None:
                    exit_reason = sl_closed_reason
                    exit_utc = bar_utc
                    break

                # 2) Drive real main.py FSM tick handler
                if sig.get("status") == "RUNNER_ACTIVE" or (sig.get("tp1_reached") and sig.get("runner_ticket")):
                    await main_mod._process_runner_active_tick(msg_id, sig)
                else:
                    await main_mod._process_pre_tp1_or_backstop_tick(msg_id, sig)

                max_stage = max(max_stage, _derive_max_stage(sig, main_mod))

                # 3) Check if FSM closed all positions
                if sig.get("status") == "CLOSED" or not sig.get("market_tickets"):
                    exit_reason = str(
                        sig.get("exit_reason_code")
                        or sig.get("runner_close_reason")
                        or "FSM_CLOSED"
                    )
                    exit_utc = bar_utc
                    break

                # 4) Check broker TP after FSM tick (allows TP1->TP2 handoff or lift on crossing tick)
                tp_closed_reason = await _simulate_broker_stops(
                    main_mod, sig, spy, tick, check_sl=False, check_tp=True
                )
                if tp_closed_reason is not None:
                    exit_reason = tp_closed_reason
                    exit_utc = bar_utc
                    break

            if exit_reason is not None:
                break

        # Mark-to-market fallback if data ends while still open
        if exit_reason is None and sig.get("market_tickets"):
            last_bar = bars[-1]
            last_utc = _utc_iso(last_bar["time_utc"])
            last_tick = SimpleNamespace(
                bid=float(last_bar["close"]),
                ask=round(float(last_bar["close"]) + float(last_bar["spread_dollars"]), 2),
                time=last_bar["time_utc"],
            )
            spy.current_tick = last_tick
            spy.current_utc = last_utc
            exit_px = float(last_tick.bid) if is_buy else float(last_tick.ask)
            for tkt, pos in list(spy.positions.items()):
                if pos["open"] and pos["lot"] > 0.0:
                    spy.record_close(tkt, pos["lot"], exit_px, reason="EOD_MTM", is_partial=False)
            sig["market_tickets"] = []
            sig["status"] = "CLOSED"
            exit_reason = "EOD_MTM"
            exit_utc = last_utc
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

    # Phase 2: label each R_LOCK win with its tier threshold/margin and compute
    # the structural wide-stop view using the EFFECTIVE lifecycle tiers. Pure
    # reporting; the frozen Arm A executor has no lifecycle policy (guarded).
    try:
        _eff_tiers = tuple(main_mod.executor._get_lifecycle_policy().r_lock_tiers)
    except Exception:
        _eff_tiers = ()
    _enrich_rlock_trace(_PICKER_TRACE, _eff_tiers)

    # Exit binding evidence (research): for every closed layer, attribute the SL
    # that was actually hit to the production picker component that proposed it
    # (last APPLIED picker snapshot), and record the active MFE tier/pct plus
    # every component's proposed SL at that point. FSM-other stops (legacy
    # ratchet / stage-0 lock / manual BE paths that bypass the ATR picker) are
    # labelled, never guessed.
    exit_binding: List[Dict[str, Any]] = []
    if _PROTECTION_TELEMETRY and _EXIT_BINDING_TELEMETRY:
        _slots = _PROTECTION_TRACE.get("picker_by_layer") or {}
        for ev in spy.closed_events:
            _slot = _slots.get(str(round(float(ev["entry_price"]), 4))) or {}
            _applied = _slot.get("last_applied") or _slot.get("last") or {}
            _sl_hit = float(ev["exit_price"]) if str(ev.get("reason")) == "SL" else None
            _comps = {k: _applied.get(k) for k in (
                "atr_sl", "mfe_lock_sl", "r_lock_floor", "be_sl", "auto_be_sl", "milestone_floor"
            )}
            _binding = None
            if _sl_hit is not None:
                for _name, _val in _comps.items():
                    if isinstance(_val, (int, float)) and abs(float(_val) - _sl_hit) < 0.011:
                        _binding = _name
                        break
                if _binding is None and isinstance(_applied.get("candidate_sl"), (int, float)):
                    if abs(float(_applied["candidate_sl"]) - _sl_hit) < 0.011:
                        _binding = str(_applied.get("stop_source") or "UNKNOWN")
                if _binding is None:
                    _binding = "FSM_OTHER_OR_UNTRACKED"
            exit_binding.append({
                "layer": ev["layer"],
                "entry_price": ev["entry_price"],
                "exit_price": ev["exit_price"],
                "exit_reason_code": ev["reason"],
                "sl_hit": _sl_hit,
                "binding_component": _binding,
                "stop_source_last_applied": _applied.get("stop_source"),
                "active_mfe_tier": _applied.get("mfe_lock_tier"),
                "active_mfe_pct": _applied.get("mfe_lock_fraction"),
                "mfe_at_exit": _applied.get("mfe_dollars"),
                "component_sls": _comps,
                "candidate_sl": _applied.get("candidate_sl"),
            })

    net_gbp = spy.total_gbp()
    pts_lot = spy.total_pts_lot()
    realized_pts_per_oz = round(pts_lot / total_lot, 2) if total_lot > 0.0 else 0.0
    mfe_capture_pct = round((realized_pts_per_oz / pot_mfe) * 100.0, 1) if pot_mfe > 0.0 else None
    final_exit_px = spy.last_exit_price()
    final_exit_utc = exit_utc or spy.last_exit_utc()
    rollover_flag = _is_rollover_utc(final_exit_utc)

    res = {
        "msg_id": msg_id,
        "dir": trade["dir"],
        "open_utc": trade["open_utc"],
        "actual_close_utc": trade["close_utc"],
        "total_lot": total_lot,
        "avg_entry": avg_entry,
        "initial_r_per_oz": round(abs(avg_entry - float(trade["sl"])), 4),
        "sl": trade["sl"],
        "tp1": trade["tp_levels"]["TP1"],
        "actual_mt5_gbp": trade["actual_mt5_gbp"],
        "actual_exit_reason": trade["actual_exit_reason"],
        "sim_gbp": net_gbp,
        "sim_pts_lot": pts_lot,
        "realized_pts_per_oz": realized_pts_per_oz,
        "peak_mfe_dollars": round(peak_mfe, 2),
        "potential_mfe_before_sl": pot_mfe,
        "mfe_capture_pct": mfe_capture_pct,
        "max_stage": max_stage,
        "exit_price": final_exit_px,
        "exit_reason": exit_reason or "UNKNOWN",
        "exit_utc": final_exit_utc,
        "rollover_flag": rollover_flag,
        "banked_profit_tracked": round(float(sig.get("banked_profit") or 0.0), 2),
        "ratchet_tier_final": (
            (sig.get("layers") or {}).get("L1", {}).get("ratchet_tier")
            or next(
                (
                    lv.get("ratchet_tier")
                    for lv in (sig.get("layers") or {}).values()
                    if isinstance(lv, dict) and lv.get("ratchet_tier")
                ),
                None,
            )
        ),
        "error": error_msg,
        "engine_picker_calls": int(_PICKER_TRACE.get("calls", 0)),
        "stop_source_counts": dict(_PICKER_TRACE.get("sources", {})),
        "r_lock_wins": int(_PICKER_TRACE.get("r_lock_wins", 0)),
        "r_lock_wide_wins": int(_PICKER_TRACE.get("r_lock_wide_wins", 0)),
        "r_lock_max_r": round(float(_PICKER_TRACE.get("r_lock_max_r", 0.0)), 4),
        "r_lock_first_mfe": _PICKER_TRACE.get("r_lock_first_mfe"),
        "peak_mfe_r_multiple": (
            round(peak_mfe / abs(avg_entry - float(trade["sl"])), 4)
            if abs(avg_entry - float(trade["sl"])) > 1e-9 else None
        ),
        "r_lock_fraction_zero_wins": int(_PICKER_TRACE.get("r_lock_fraction_zero_wins", 0)),
        "r_lock_fraction_pos_wins": int(_PICKER_TRACE.get("r_lock_fraction_pos_wins", 0)),
        "r_lock_be_equivalent_wins": int(_PICKER_TRACE.get("r_lock_fraction_zero_wins", 0)),
        "r_lock_winning_r_min": _PICKER_TRACE.get("r_lock_winning_r_min"),
        "r_lock_winning_r_max": round(float(_PICKER_TRACE.get("r_lock_winning_r_max", 0.0)), 4),
        "r_lock_max_trail_dist_at_win": round(float(_PICKER_TRACE.get("r_lock_max_trail_dist_at_win", 0.0)), 4),
        "max_trail_dist_observed": round(float(_PICKER_TRACE.get("max_trail_dist", 0.0)), 4),
        "r_values_seen": sorted(_PICKER_TRACE.get("r_values_seen") or []),
        "r_lock_widest_r": float(_PICKER_TRACE.get("r_lock_widest_r", 0.0)),
        "r_lock_min_profit_margin": _PICKER_TRACE.get("r_lock_min_profit_margin"),
        "r_lock_structural_blocked": bool(_PICKER_TRACE.get("r_lock_structural_blocked", False)),
        "r_lock_win_details": list(_PICKER_TRACE.get("r_lock_win_details") or []),
    }
    if _PROTECTION_TELEMETRY:
        pt = _PROTECTION_TRACE
        tt_min: Optional[float] = None
        if pt.get("first_protect_utc"):
            try:
                tt_min = round(
                    (_parse_utc(pt["first_protect_utc"]) - _parse_utc(trade["open_utc"])).total_seconds() / 60.0,
                    2,
                )
            except Exception:
                tt_min = None
        res.update(
            {
                "first_protect_utc": pt.get("first_protect_utc"),
                "first_protect_source": pt.get("first_protect_source"),
                "first_be_arm_utc": pt.get("first_be_arm_utc"),
                "first_trail_arm_utc": pt.get("first_trail_arm_utc"),
                "first_mfe_lock_utc": pt.get("first_mfe_lock_utc"),
                "first_auto_be_utc": pt.get("first_auto_be_utc"),
                "in_profit_candidate_calls": int(pt.get("in_profit_candidate_calls", 0)),
                "time_to_first_protection_min": tt_min,
                "max_mfe_lock_tier": round(float(pt.get("max_mfe_lock_tier", 0.0)), 4),
                "max_mfe_lock_pct": round(float(pt.get("max_mfe_lock_pct", 0.0)), 4),
                "max_mfe_lock_profit": round(float(pt.get("max_mfe_lock_profit", 0.0)), 4),
                "stop_modify_attempts": int(spy.modify_attempts),
                "stop_modify_failures": int(spy.modify_failures),
                "explicit_be": explicit_be_info,
            }
        )
        if _EXIT_BINDING_TELEMETRY:
            res["exit_binding"] = exit_binding
    if _INTERIM_FLOOR_SPEC:
        res["interim_floor"] = {
            "spec": _INTERIM_FLOOR_SPEC,
            "armed_calls": int(_INTERIM_FLOOR_STATS.get("armed_calls", 0)),
            "binds": int(_INTERIM_FLOOR_STATS.get("binds", 0)),
            "max_floor_profit": round(float(_INTERIM_FLOOR_STATS.get("max_floor_profit", 0.0)), 4),
        }
    if _WICK_GATE_MODE and _WICK_TRACKER is not None:
        res["wick_gate"] = {
            "mode": _WICK_GATE_MODE,
            "suppressed_calls": int(_WICK_TRACKER.suppressed_calls),
            "max_confirmed_tier": round(float(_WICK_TRACKER.max_confirmed_tier()), 4),
        }
    return res


def _run_worker_main(
    arm_mode: str,
    bars_path: Path,
    out_path: Path,
    catalog_path: Optional[Path] = None,
    protection_telemetry: bool = False,
    exit_binding: bool = False,
    wick_gate: Optional[str] = None,
    interim_floor: Optional[str] = None,
) -> int:
    runtime_tmp = tempfile.TemporaryDirectory(prefix=f"ab_replay_{arm_mode}_")
    try:
        rdir = Path(runtime_tmp.name)
        os.environ["DRY_RUN"] = "true"
        os.environ["STAGED_EXIT_ENABLED"] = "true"
        os.environ["NOTIFY_SAVED_MESSAGES"] = "false"
        os.environ["STATE_FILE_PATH"] = str(rdir / "signals_state.json")
        os.environ["LOG_FILE_PATH"] = str(rdir / "copier.log")
        os.environ["TRADES_LEDGER_PATH"] = str(rdir / "trades_ledger.jsonl")
        os.environ["LOCK_FILE_PATH"] = str(rdir / "copier.lock")

        if arm_mode in _ENGINE_ARM_MODES:
            for k, v in ARM_B_CALIBRATED_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
            # Neutralize ambient .env activation of the opt-in protection knobs
            # BEFORE config import; arm-specific overrides below win.
            for k, v in _OPT_IN_PROTECTION_SHIPPED_DEFAULTS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        if arm_mode in ("c", "cx"):
            for k, v in ARM_C_CALIBRATED_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        if arm_mode == "cx":
            # EXPERIMENTAL replay-only override; never a production default.
            for k, v in ARM_CX_EXPERIMENTAL_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        if arm_mode in TRAILING_EXPERIMENT_ARMS:
            # EXPERIMENTAL replay-only trailing override; never a production default.
            for k, v in TRAILING_EXPERIMENT_ARMS[arm_mode]["params"].items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        elif arm_mode == "t3tight":
            # EXPERIMENTAL replay-only T3-tight override; never a production default.
            for k, v in ARM_T3TIGHT_EXPERIMENTAL_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        elif arm_mode in SCALE_COMPARISON_ARMS or arm_mode in LADDER_RESEARCH_ARMS or arm_mode in WICK_RESEARCH_ARMS or arm_mode in FLOOR_RESEARCH_ARMS:
            # EXPERIMENTAL replay-only scale/ladder override; never a production default.
            _reg = SCALE_COMPARISON_ARMS.get(arm_mode) or LADDER_RESEARCH_ARMS.get(arm_mode) or WICK_RESEARCH_ARMS.get(arm_mode) or FLOOR_RESEARCH_ARMS[arm_mode]
            for k, v in _reg["params"].items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)

        import config  # noqa: E402
        import main  # noqa: E402
        import mt5_executor  # noqa: E402
        from state_manager import StateManager  # noqa: E402
        from trade_ledger import TradeLedger  # noqa: E402

        assert config.DRY_RUN is True, "DRY_RUN must be True in replay worker"
        assert Path(config.STATE_FILE_PATH).parent == rdir, "STATE_FILE_PATH isolation failed"

        if arm_mode == "a":
            if hasattr(config, "PROFIT_RATCHET_ENABLED"):
                raise RuntimeError("Arm A contamination: PROFIT_RATCHET_ENABLED found in 4544f7f worktree!")
        elif arm_mode == "b":
            _pin_arm_b_params(config, mt5_executor, main)
        elif arm_mode == "c":
            _pin_arm_b_params(config, mt5_executor, main)
            _pin_arm_c_params(config, mt5_executor, main)
        elif arm_mode == "cx":  # EXPERIMENTAL re-tier override layered on top of Arm C
            _pin_arm_b_params(config, mt5_executor, main)
            _pin_arm_c_params(config, mt5_executor, main)
            _pin_arm_cx_params(config, mt5_executor, main)
        else:  # t1..t4 / t3tight / s100 / s105: EXPERIMENTAL override on Arm B
            _pin_arm_b_params(config, mt5_executor, main)
            if arm_mode in TRAILING_EXPERIMENT_ARMS:
                _arm_params = TRAILING_EXPERIMENT_ARMS[arm_mode]["params"]
            elif arm_mode in TIGHT_COMPARISON_ARMS:
                _arm_params = TIGHT_COMPARISON_ARMS[arm_mode]["params"]
            else:
                _reg = SCALE_COMPARISON_ARMS.get(arm_mode) or LADDER_RESEARCH_ARMS.get(arm_mode) or WICK_RESEARCH_ARMS.get(arm_mode) or FLOOR_RESEARCH_ARMS[arm_mode]
                _arm_params = _reg["params"]
            _pin_arm_experimental_params(
                _arm_params, config, mt5_executor, main, f"Arm {arm_mode.upper()}",
            )

        # Hermetic opt-in isolation (module level): pin shipped defaults for any
        # protection knob the arm's own param set does not explicitly carry, so
        # ambient .env activation can never leak into an existing arm.
        if arm_mode in _ENGINE_ARM_MODES:
            if arm_mode == "b":
                _pinned_keys = set(ARM_B_CALIBRATED_PARAMS)
            elif arm_mode in ("c", "cx"):
                _pinned_keys = set(ARM_B_CALIBRATED_PARAMS) | set(ARM_C_CALIBRATED_PARAMS)
                if arm_mode == "cx":
                    _pinned_keys |= set(ARM_CX_EXPERIMENTAL_PARAMS)
            elif arm_mode in TRAILING_EXPERIMENT_ARMS:
                _pinned_keys = set(TRAILING_EXPERIMENT_ARMS[arm_mode]["params"])
            elif arm_mode in TIGHT_COMPARISON_ARMS:
                _pinned_keys = set(TIGHT_COMPARISON_ARMS[arm_mode]["params"])
            else:
                _reg = SCALE_COMPARISON_ARMS.get(arm_mode) or LADDER_RESEARCH_ARMS.get(arm_mode) or WICK_RESEARCH_ARMS.get(arm_mode) or FLOOR_RESEARCH_ARMS[arm_mode]
                _pinned_keys = set(_reg["params"])
            for _k, _v in _OPT_IN_PROTECTION_SHIPPED_DEFAULTS.items():
                if _k in _pinned_keys:
                    continue
                for _mod in (config, mt5_executor, main):
                    if hasattr(_mod, _k):
                        setattr(_mod, _k, _v)
                if hasattr(main, "_MAIN_CFG_SNAPSHOT") and isinstance(main._MAIN_CFG_SNAPSHOT, dict):
                    if _k in main._MAIN_CFG_SNAPSHOT:
                        main._MAIN_CFG_SNAPSHOT[_k] = _v

        # Arms B and C run on the simulated replay clock: the trade-age guard
        # reads the wall clock, and the replay simulates the RECORDED week.
        if arm_mode in _ENGINE_ARM_MODES:
            _install_replay_clock(main)

        # Arms B and C drive the production ATR trail engine: feed it the M5 ATR it
        # actually reads and trace every picker decision. Arm A is the frozen 4544f7f
        # reference and is deliberately left untouched.
        globals()["_PRODUCTION_ENGINE_ARM"] = arm_mode in _ENGINE_ARM_MODES
        globals()["_INTERIM_FLOOR_SPEC"] = interim_floor
        if interim_floor:
            # RESEARCH floor installed INNERMOST (augmentation, not suppression).
            _install_interim_floor(mt5_executor, interim_floor)
        globals()["_WICK_GATE_MODE"] = wick_gate
        if wick_gate:
            # RESEARCH gate installed INNERMOST: picker/protection telemetry and
            # the executor all see the gated (post-suppression) result.
            _install_wick_gate(
                mt5_executor, wick_gate,
                str(getattr(config, "DYNAMIC_ATR_MFE_LOCK_TIERS", "")), config,
            )
        if _PRODUCTION_ENGINE_ARM:
            _install_picker_trace(mt5_executor)
        globals()["_PROTECTION_TELEMETRY"] = bool(protection_telemetry)
        globals()["_EXIT_BINDING_TELEMETRY"] = bool(exit_binding)
        if _PROTECTION_TELEMETRY:
            _install_protection_trace(mt5_executor)

        async def _no_notify(*_args: Any, **_kwargs: Any) -> None:
            return None

        main.notify_saved_messages = _no_notify

        bars_payload = json.loads(bars_path.read_text(encoding="utf-8"))
        bars = bars_payload["bars"] if isinstance(bars_payload, dict) else bars_payload

        # Optional signal-set override (Nov-23 / synthetic scenarios). The default
        # catalog stays the canonical 12-trade Nov 17-19 list, byte-identical.
        catalog: List[Dict[str, Any]] = TRADE_CATALOG
        catalog_sha: Optional[str] = None
        if catalog_path is not None:
            cat_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog = cat_payload["trades"] if isinstance(cat_payload, dict) else cat_payload
            if not isinstance(catalog, list) or not catalog:
                raise RuntimeError(f"Signal catalog empty or invalid: {catalog_path}")
            for t in catalog:
                for key in ("msg_id", "dir", "open_utc", "layers", "sl", "tp_levels"):
                    if key not in t:
                        raise RuntimeError(f"Signal catalog entry missing {key!r}: {t.get('msg_id')}")
            catalog_sha = hashlib.sha256(
                json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

        # Arm params snapshot: record the exact strategy parameters + config
        # used for this run so every output JSON is self-describing.
        if arm_mode == "a":
            arm_params_snapshot: Dict[str, Any] = {
                "arm": "a",
                "baseline_commit": BASELINE_COMMIT,
                "pinned_params": {},
                "config_effective": {
                    k: getattr(config, k, None)
                    for k in sorted(set(ARM_B_CALIBRATED_PARAMS) | set(ARM_C_CALIBRATED_PARAMS))
                },
            }
        else:
            effective = dict(ARM_B_CALIBRATED_PARAMS)
            if arm_mode in ("c", "cx"):
                effective.update(ARM_C_CALIBRATED_PARAMS)
            if arm_mode == "cx":
                effective.update(ARM_CX_EXPERIMENTAL_PARAMS)
            if arm_mode in TRAILING_EXPERIMENT_ARMS:
                effective.update(TRAILING_EXPERIMENT_ARMS[arm_mode]["params"])
            elif arm_mode == "t3tight":
                effective.update(ARM_T3TIGHT_EXPERIMENTAL_PARAMS)
            elif arm_mode in SCALE_COMPARISON_ARMS or arm_mode in LADDER_RESEARCH_ARMS or arm_mode in WICK_RESEARCH_ARMS or arm_mode in FLOOR_RESEARCH_ARMS:
                _reg = SCALE_COMPARISON_ARMS.get(arm_mode) or LADDER_RESEARCH_ARMS.get(arm_mode) or WICK_RESEARCH_ARMS.get(arm_mode) or FLOOR_RESEARCH_ARMS[arm_mode]
                effective.update(_reg["params"])
            arm_params_snapshot = {
                "arm": arm_mode,
                "pinned_params": effective,
                "config_verified": {k: getattr(config, k) for k in effective},
            }
            if arm_mode == "cx":
                arm_params_snapshot["experimental"] = True
                arm_params_snapshot["experimental_note"] = (
                    "EXPERIMENTAL re-tiered R_LOCK_TIERS override (replay arm-param only; "
                    "not production, not shipped). Compared against Arm C on the pinned tape."
                )
            if arm_mode in TRAILING_EXPERIMENT_ARMS:
                arm_params_snapshot["experimental"] = True
                arm_params_snapshot["experimental_note"] = (
                    "EXPERIMENTAL trailing A/B arm-param override (replay arm-param only; "
                    "not production, not shipped). Compared against pinned baseline Arm B "
                    "on the identical pinned tape. Delta vs B: "
                    + ", ".join(
                        f"{k}={v}"
                        for k, v in TRAILING_EXPERIMENT_ARMS[arm_mode]["params"].items()
                        if ARM_B_CALIBRATED_PARAMS.get(k) != v
                    )
                )
            elif arm_mode == "t3tight":
                arm_params_snapshot["experimental"] = True
                arm_params_snapshot["experimental_note"] = (
                    "EXPERIMENTAL T3-tight arm-param override (replay arm-param only; not "
                    "production, not shipped). T3 plus the six opt-in 5ac5fe2 protection "
                    "knobs. Delta vs T3: "
                    + ", ".join(
                        f"{k}={v}"
                        for k, v in ARM_T3TIGHT_EXPERIMENTAL_PARAMS.items()
                        if ARM_T3_EXPERIMENTAL_PARAMS.get(k) != v
                    )
                )
            elif arm_mode in SCALE_COMPARISON_ARMS or arm_mode in LADDER_RESEARCH_ARMS or arm_mode in WICK_RESEARCH_ARMS or arm_mode in FLOOR_RESEARCH_ARMS:
                arm_params_snapshot["experimental"] = True
                if arm_mode in FLOOR_RESEARCH_ARMS:
                    arm_params_snapshot["experimental_note"] = (
                        "RESEARCH interim-floor arm (scripts-only extra candidate; NOT adopted, "
                        "production engine + immediate MFE ladder unchanged). Params identical to "
                        "upgraded + 1.05; floor=" + str(FLOOR_RESEARCH_ARMS[arm_mode]["floor"]) + "."
                    )
                elif arm_mode in WICK_RESEARCH_ARMS:
                    arm_params_snapshot["experimental_note"] = (
                        "RESEARCH wick-aware activation arm (scripts-only gate; NOT adopted, "
                        "production engine unchanged). Params identical to upgraded + 1.05; "
                        "gate=" + str(WICK_RESEARCH_ARMS[arm_mode]["gate"]) + "."
                    )
                elif arm_mode in ("l2pp", "l3pp"):
                    arm_params_snapshot["experimental_note"] = (
                        "RESEARCH ladder candidate (replay arm-param only; NOT adopted). "
                        "Upgraded ATR-relative config + 1.05 trail scale with clipping-tier "
                        "protected-% reduced " + ("2" if arm_mode == "l2pp" else "3") +
                        "pp on ratios 0.40/0.60/1.00; activation ratios and all other knobs unchanged."
                    )
                else:
                    _reg = SCALE_COMPARISON_ARMS.get(arm_mode) or LADDER_RESEARCH_ARMS.get(arm_mode) or WICK_RESEARCH_ARMS.get(arm_mode) or FLOOR_RESEARCH_ARMS[arm_mode]
                    arm_params_snapshot["experimental_note"] = (
                        "EXPERIMENTAL distance-scale arm-param override (replay arm-param only; "
                        "not production, not shipped). Upgraded ATR-relative config (192d4f7) "
                        "with DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT="
                        + str(_reg["params"]["DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT"])
                        + " (09aae20). Delta vs scale 1.00: DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT only."
                    )
        if wick_gate:
            arm_params_snapshot["wick_gate"] = wick_gate
        if interim_floor:
            arm_params_snapshot["interim_floor"] = interim_floor
        arm_params_snapshot["gbp_rate_per_pt_001_lot"] = GBP_PER_PT_001_LOT
        arm_params_snapshot["server_offset_sec"] = MT5_SERVER_OFFSET_SEC

        async def _run_all() -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for idx, trade in enumerate(catalog):
                res = await _run_single_trade_worker(
                    main,
                    StateManager,
                    TradeLedger,
                    mt5_executor.MT5Executor,
                    rdir,
                    trade,
                    bars,
                    idx,
                )
                out.append(res)
            return out

        results = asyncio.run(_run_all())
        agg = _summarize_arm(results, arm_mode)
        payload = {
            "arm": arm_mode,
            "code_root": str(Path(main.__file__).resolve().parent),
            "dry_run": bool(config.DRY_RUN),
            "arm_params": arm_params_snapshot,
            "bars_sha256": _bars_sha256(bars),
            "results": results,
            "summary": agg,
        }
        if catalog_sha is not None:
            payload["catalog_sha256"] = catalog_sha
        if protection_telemetry:
            payload["protection_telemetry"] = True
            payload["slippage_estimate_dollars_per_side"] = SLIPPAGE_ESTIMATE_DOLLARS_PER_SIDE
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0
    finally:
        _shutdown_copier_loggers()
        try:
            runtime_tmp.cleanup()
        except Exception:
            pass


def _summarize_arm(results: List[Dict[str, Any]], arm_mode: str) -> Dict[str, Any]:
    net_gbp = round(sum(float(r["sim_gbp"]) for r in results), 2)
    net_pts_lot = round(sum(float(r["sim_pts_lot"]) for r in results), 6)
    tail_ids = {"13078", "9953"}
    tail_gbp = round(sum(float(r["sim_gbp"]) for r in results if r["msg_id"] in tail_ids), 2)
    trio_ids = {"13153", "9934", "13167"}
    trio_gbp = round(sum(float(r["sim_gbp"]) for r in results if r["msg_id"] in trio_ids), 2)
    trio_pts_lot = round(sum(float(r["sim_pts_lot"]) for r in results if r["msg_id"] in trio_ids), 6)
    r_13153 = next((r for r in results if r["msg_id"] == "13153"), {})
    r_9934 = next((r for r in results if r["msg_id"] == "9934"), {})
    rollover_trades = [r["msg_id"] for r in results if r.get("rollover_flag")]
    unflagged_rollover = [
        r["msg_id"] for r in results if _is_rollover_utc(r.get("exit_utc")) and not r.get("rollover_flag")
    ]
    errors = [{"msg_id": r["msg_id"], "error": r["error"]} for r in results if r.get("error")]
    avg_mfe_cap_vals = [float(r["mfe_capture_pct"]) for r in results if r.get("mfe_capture_pct") is not None and float(r["sim_gbp"]) > 0]
    avg_win_mfe_cap = round(sum(avg_mfe_cap_vals) / len(avg_mfe_cap_vals), 1) if avg_mfe_cap_vals else 0.0

    # Phase 2 lock_fraction aggregates (reporting only).
    frac_zero = sum(int(r.get("r_lock_fraction_zero_wins", 0)) for r in results)
    frac_pos = sum(int(r.get("r_lock_fraction_pos_wins", 0)) for r in results)
    win_details = [d for r in results for d in (r.get("r_lock_win_details") or [])]
    win_r_vals = sorted({round(float(d["r"]), 4) for d in win_details})
    frac_dist: Dict[str, int] = {}
    for d in win_details:
        key = str(d["fraction"])
        frac_dist[key] = frac_dist.get(key, 0) + 1
    max_td_win = max([float(r.get("r_lock_max_trail_dist_at_win", 0.0)) for r in results] or [0.0])
    max_td_obs = max([float(r.get("max_trail_dist_observed", 0.0)) for r in results] or [0.0])
    trio_ids = {"13153", "9934", "13167"}
    trio_peak_r = {
        r["msg_id"]: float(r.get("peak_mfe_r_multiple") or 0.0)
        for r in results if r["msg_id"] in trio_ids
    }
    trio_reach_half = any(v >= 0.5 for v in trio_peak_r.values())
    struct_blocked_wide = [
        r["msg_id"] for r in results
        if r.get("r_lock_structural_blocked") and float(r.get("r_lock_widest_r", 0.0)) >= R_LOCK_WIDE_R_THRESHOLD
    ]

    return {
        "arm": arm_mode,
        "trade_count": len(results),
        "net_gbp": net_gbp,
        "net_pts_lot": net_pts_lot,
        "actual_live_gbp": round(sum(float(r["actual_mt5_gbp"]) for r in results), 2),
        "tail_loss_gbp_13078_9953": tail_gbp,
        "choke_trio_gbp_13153_9934_13167": trio_gbp,
        "choke_trio_pts_lot": trio_pts_lot,
        "trade_13153_max_stage": int(r_13153.get("max_stage") or 0),
        "trade_9934_banked_tracked": float(r_9934.get("banked_profit_tracked") or 0.0),
        "avg_winner_mfe_capture_pct": avg_win_mfe_cap,
        "rollover_exits": rollover_trades,
        "unflagged_rollover_exits": unflagged_rollover,
        "error_count": len(errors),
        "errors": errors,
        "engine_picker_calls": sum(int(r.get("engine_picker_calls", 0)) for r in results),
        "r_lock_wins": sum(int(r.get("r_lock_wins", 0)) for r in results),
        "r_lock_wide_wins": sum(int(r.get("r_lock_wide_wins", 0)) for r in results),
        "r_lock_max_r": round(max([float(r.get("r_lock_max_r", 0.0)) for r in results] or [0.0]), 4),
        "r_lock_trades": [r["msg_id"] for r in results if int(r.get("r_lock_wins", 0)) > 0],
        "r_lock_fraction_zero_wins": frac_zero,
        "r_lock_fraction_pos_wins": frac_pos,
        "r_lock_be_equivalent_wins": frac_zero,
        "r_lock_winning_r_min": min(win_r_vals) if win_r_vals else None,
        "r_lock_winning_r_max": max(win_r_vals) if win_r_vals else 0.0,
        "r_lock_winning_r_values": win_r_vals,
        "r_lock_fraction_distribution": frac_dist,
        "r_lock_max_trail_dist_at_win": round(max_td_win, 4),
        "max_trail_dist_observed": round(max_td_obs, 4),
        "trio_peak_r_multiples": trio_peak_r,
        "trio_peaks_reach_half_r": bool(trio_reach_half),
        "r_lock_structural_blocked_wide_trades": struct_blocked_wide,
    }


def _classify_exit_reason(reason: Optional[str]) -> str:
    """Bucket a replay exit reason for the trailing-experiment metric set."""
    txt = str(reason or "").strip().upper()
    if txt == "BROKER_SL":
        return "initial_stop_loss"
    if txt.endswith("_SL"):
        return "protected_stop"  # ATR_TRAIL_SL / BE_LOCK_SL / MILESTONE_FLOOR_SL / R_LOCK_SL / ratchet / profit locks
    if "BROKER_TP" in txt or txt.startswith("TP"):
        return "tp_family"
    return "other"


def _trailing_experiment_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Phase 3+4 full metric set for one arm on the pinned tape.

    Pure reporting over the existing per-trade result rows (no worker change,
    so default replay behaviour is untouched). Definitions:
      * win/loss: sign of simulated net GBP per trade (0 = flat/BE).
      * max drawdown: peak-to-trough of the cumulative realized-GBP equity
        curve in trade-close (catalog) order; NOT intra-trade tick drawdown.
      * avg R/trade: realized $/oz divided by the trade's initial risk $/oz.
      * profit surrendered after peak: max(0, peak MFE $/oz - max(realized
        $/oz, 0)), lot-weighted to GBP (same avg-entry convention as
        mfe_capture_pct; multi-layer partial fills make this an approximation,
        per the audit). Flooring realized at 0 keeps the initial risk of a
        stop-loss trade out of 'surrendered profit'.
      * premature trailing-stop exit: a protected-stop exit (any *_SL except
        the initial BROKER_SL) that realized < 50% of the trade's peak MFE.
      * MAE: NOT measured - the replay worker does not track adverse excursion.
    """
    n = len(results)
    wins = [r for r in results if float(r["sim_gbp"]) > 0.0]
    losses = [r for r in results if float(r["sim_gbp"]) < 0.0]
    flats = n - len(wins) - len(losses)
    gross_profit = round(sum(float(r["sim_gbp"]) for r in wins), 2)
    gross_loss = round(sum(float(r["sim_gbp"]) for r in losses), 2)
    net = round(sum(float(r["sim_gbp"]) for r in results), 2)
    profit_factor = round(gross_profit / abs(gross_loss), 3) if gross_loss < 0.0 else None

    equity = 0.0
    eq_peak = 0.0
    max_dd = 0.0
    for r in results:
        equity += float(r["sim_gbp"])
        eq_peak = max(eq_peak, equity)
        max_dd = max(max_dd, eq_peak - equity)

    r_mults = [
        float(r["realized_pts_per_oz"]) / float(r["initial_r_per_oz"])
        for r in results if float(r.get("initial_r_per_oz") or 0.0) > 0.0
    ]
    caps_all = [float(r["mfe_capture_pct"]) for r in results if r.get("mfe_capture_pct") is not None]
    caps_win = [float(r["mfe_capture_pct"]) for r in wins if r.get("mfe_capture_pct") is not None]

    surrender_gbp = 0.0
    per_trade_surrender: Dict[str, Any] = {}
    for r in results:
        giveback_usd = max(0.0, float(r["peak_mfe_dollars"]) - max(float(r["realized_pts_per_oz"]), 0.0))
        giveback_gbp = giveback_usd * (float(r["total_lot"]) / 0.01) * GBP_PER_PT_001_LOT
        surrender_gbp += giveback_gbp
        per_trade_surrender[r["msg_id"]] = {
            "peak_mfe_usd_per_oz": round(float(r["peak_mfe_dollars"]), 2),
            "realized_usd_per_oz": round(float(r["realized_pts_per_oz"]), 2),
            "giveback_usd_per_oz": round(giveback_usd, 2),
            "giveback_gbp": round(giveback_gbp, 2),
        }

    exit_buckets: Dict[str, int] = {}
    exit_reason_dist: Dict[str, int] = {}
    be_closures: List[str] = []
    atr_trail_exits: List[str] = []
    premature: List[str] = []
    holding_minutes: List[float] = []
    stage_dist: Dict[str, int] = {}
    for r in results:
        reason = str(r.get("exit_reason") or "UNKNOWN")
        bucket = _classify_exit_reason(reason)
        exit_buckets[bucket] = exit_buckets.get(bucket, 0) + 1
        exit_reason_dist[reason] = exit_reason_dist.get(reason, 0) + 1
        if "BE_LOCK" in reason.upper():
            be_closures.append(r["msg_id"])
        if "ATR_TRAIL" in reason.upper():
            atr_trail_exits.append(r["msg_id"])
        if (
            bucket == "protected_stop"
            and float(r["peak_mfe_dollars"]) > 0.0
            and float(r["realized_pts_per_oz"]) < 0.5 * float(r["peak_mfe_dollars"])
        ):
            premature.append(r["msg_id"])
        stage_key = f"TP{int(r.get('max_stage') or 0)}" if int(r.get("max_stage") or 0) > 0 else "none"
        stage_dist[stage_key] = stage_dist.get(stage_key, 0) + 1
        try:
            t_open = _parse_utc(str(r["open_utc"]))
            t_exit = _parse_utc(str(r.get("exit_utc") or r["open_utc"]))
            holding_minutes.append((t_exit - t_open).total_seconds() / 60.0)
        except Exception:
            pass

    atr_trail_engaged = [r["msg_id"] for r in results if int((r.get("stop_source_counts") or {}).get("ATR_TRAIL", 0)) > 0]
    be_lock_engaged = [r["msg_id"] for r in results if int((r.get("stop_source_counts") or {}).get("BE_LOCK", 0)) > 0]
    r_lock_trades = [r["msg_id"] for r in results if int(r.get("r_lock_wins", 0)) > 0]

    def _trade_row(mid: str) -> Dict[str, Any]:
        r = next((x for x in results if x["msg_id"] == mid), None)
        if r is None:
            return {}
        return {
            "sim_gbp": float(r["sim_gbp"]),
            "sim_pts_lot": float(r["sim_pts_lot"]),
            "mfe_capture_pct": r.get("mfe_capture_pct"),
            "peak_mfe_usd_per_oz": round(float(r["peak_mfe_dollars"]), 2),
            "realized_usd_per_oz": round(float(r["realized_pts_per_oz"]), 2),
            "giveback_usd_per_oz": per_trade_surrender.get(mid, {}).get("giveback_usd_per_oz"),
            "max_stage": int(r.get("max_stage") or 0),
            "exit_reason": r.get("exit_reason"),
            "exit_price": r.get("exit_price"),
            "exit_utc": r.get("exit_utc"),
            "atr_trail_picker_wins": int((r.get("stop_source_counts") or {}).get("ATR_TRAIL", 0)),
            "stop_source_counts": dict(r.get("stop_source_counts") or {}),
        }

    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats_be": flats,
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "gross_profit_gbp": gross_profit,
        "gross_loss_gbp": gross_loss,
        "net_profit_gbp": net,
        "profit_factor": profit_factor,
        "max_drawdown_gbp": round(max_dd, 2),
        "max_drawdown_basis": "cumulative realized-GBP equity curve, trade-close order (not intra-trade)",
        "avg_r_per_trade": round(sum(r_mults) / len(r_mults), 4) if r_mults else None,
        "avg_mfe_capture_pct_all": round(sum(caps_all) / len(caps_all), 1) if caps_all else None,
        "avg_mfe_capture_pct_winners": round(sum(caps_win) / len(caps_win), 1) if caps_win else None,
        "mae": "not measured (replay worker does not track adverse excursion)",
        "avg_realized_profit_gbp": round(net / n, 4) if n else 0.0,
        "avg_winner_gbp": round(gross_profit / len(wins), 4) if wins else 0.0,
        "profit_surrendered_after_peak_gbp": round(surrender_gbp, 2),
        "per_trade_surrender": per_trade_surrender,
        "stop_loss_frequency": exit_buckets.get("initial_stop_loss", 0),
        "protected_stop_exits": exit_buckets.get("protected_stop", 0),
        "exit_bucket_distribution": exit_buckets,
        "exit_reason_distribution": exit_reason_dist,
        "tp_stage_distribution": stage_dist,
        "r_lock_engagement": {"picker_wins": sum(int(r.get("r_lock_wins", 0)) for r in results), "trades": r_lock_trades},
        "atr_trail_engagement": {"trades": atr_trail_engaged, "picker_wins": sum(int((r.get("stop_source_counts") or {}).get("ATR_TRAIL", 0)) for r in results)},
        "be_lock_engagement_trades": be_lock_engaged,
        "be_closure_frequency": len(be_closures),
        "be_closure_trades": be_closures,
        "atr_trail_exit_trades": atr_trail_exits,
        "premature_trailing_exit_frequency": len(premature),
        "premature_trailing_exit_trades": premature,
        "premature_definition": "protected-stop exit (*_SL except initial BROKER_SL) realizing < 50% of peak MFE",
        "avg_holding_time_min": round(sum(holding_minutes) / len(holding_minutes), 2) if holding_minutes else None,
        "choke_trio_detail": {mid: _trade_row(mid) for mid in ("13153", "9934", "13167")},
        "trade_9934_exit": _trade_row("9934"),
        "winners_non_regression": {mid: _trade_row(mid) for mid in ("9905", "9705")},
    }


def _tight_comparison_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Full brief metric set for one arm of the T3 vs T3-tight comparison.

    Pure reporting over the per-trade result rows (no engine change). Where
    definitions overlap _trailing_experiment_metrics they are identical:
    win/loss = sign of simulated net GBP; giveback floors realized at 0;
    premature = protected-stop exit (*_SL except initial BROKER_SL) realizing
    < 50% of peak MFE. Additions required by the replay-validation brief:
      * avg losing trade,
      * avg time to first protection (opt-in picker telemetry; minutes from
        open_utc to the first BE/trail/MFE-lock arm on the replay clock),
      * spread+slippage-adjusted view: spread is already modeled per-bar from
        the tape bid/ask; slippage adds a flat round-turn allowance of
        2 * SLIPPAGE_ESTIMATE_DOLLARS_PER_SIDE $/oz on top,
      * TP1-TP4 completion rates (fraction of trades reaching max_stage >= k).
    """
    n = len(results)
    wins = [r for r in results if float(r["sim_gbp"]) > 0.0]
    losses = [r for r in results if float(r["sim_gbp"]) < 0.0]
    flats = n - len(wins) - len(losses)
    gross_profit = round(sum(float(r["sim_gbp"]) for r in wins), 2)
    gross_loss = round(sum(float(r["sim_gbp"]) for r in losses), 2)
    net = round(sum(float(r["sim_gbp"]) for r in results), 2)
    profit_factor = round(gross_profit / abs(gross_loss), 3) if gross_loss < 0.0 else None

    # Max drawdown: peak-to-trough of the cumulative realized-GBP equity curve
    # in trade-close (catalog) order; NOT intra-trade tick drawdown.
    equity = 0.0
    eq_peak = 0.0
    max_dd = 0.0
    for r in results:
        equity += float(r["sim_gbp"])
        eq_peak = max(eq_peak, equity)
        max_dd = max(max_dd, eq_peak - equity)

    def _lot_gbp(r: Dict[str, Any], usd_per_oz: float) -> float:
        return usd_per_oz * (float(r["total_lot"]) / 0.01) * GBP_PER_PT_001_LOT

    peak_mfe_gbp = round(sum(_lot_gbp(r, float(r["peak_mfe_dollars"])) for r in results), 2)
    captured_gbp = round(sum(_lot_gbp(r, float(r["realized_pts_per_oz"])) for r in results), 2)
    giveback_gbp = round(
        sum(_lot_gbp(r, max(0.0, float(r["peak_mfe_dollars"]) - max(float(r["realized_pts_per_oz"]), 0.0))) for r in results),
        2,
    )
    caps_all = [float(r["mfe_capture_pct"]) for r in results if r.get("mfe_capture_pct") is not None]
    caps_win = [float(r["mfe_capture_pct"]) for r in wins if r.get("mfe_capture_pct") is not None]

    premature: List[str] = []
    exit_buckets: Dict[str, int] = {}
    exit_reason_dist: Dict[str, int] = {}
    for r in results:
        reason = str(r.get("exit_reason") or "UNKNOWN")
        bucket = _classify_exit_reason(reason)
        exit_buckets[bucket] = exit_buckets.get(bucket, 0) + 1
        exit_reason_dist[reason] = exit_reason_dist.get(reason, 0) + 1
        if (
            bucket == "protected_stop"
            and float(r["peak_mfe_dollars"]) > 0.0
            and float(r["realized_pts_per_oz"]) < 0.5 * float(r["peak_mfe_dollars"])
        ):
            premature.append(r["msg_id"])

    tt_vals = [
        float(r["time_to_first_protection_min"])
        for r in results
        if r.get("time_to_first_protection_min") is not None
    ]
    never_protected = [r["msg_id"] for r in results if r.get("first_protect_utc") is None]

    slip = SLIPPAGE_ESTIMATE_DOLLARS_PER_SIDE
    comm = COMMISSION_ESTIMATE_DOLLARS_PER_OZ_ROUND_TURN
    adj_rows = [
        round(float(r["sim_gbp"]) - (2.0 * slip + comm) * (float(r["total_lot"]) / 0.01) * GBP_PER_PT_001_LOT, 2)
        for r in results
    ]
    adj_net = round(sum(adj_rows), 2)
    adj_wins = [v for v in adj_rows if v > 0.0]
    adj_losses = [v for v in adj_rows if v < 0.0]
    adj_gp = round(sum(adj_wins), 2)
    adj_gl = round(sum(adj_losses), 2)
    adj_pf = round(adj_gp / abs(adj_gl), 3) if adj_gl < 0.0 else None

    # Break-even frequency (attachment section 9): trades where the engine BE
    # lock / auto-BE ever armed (telemetry), and trades whose EXIT was a
    # BE-family stop (BE_LOCK_SL / AUTO_BE_SL exit reason).
    be_armed_trades = [r["msg_id"] for r in results if r.get("first_be_arm_utc")]
    auto_be_armed_trades = [r["msg_id"] for r in results if r.get("first_auto_be_utc")]
    mfe_lock_armed_trades = [r["msg_id"] for r in results if r.get("first_mfe_lock_utc")]
    be_family_exits = [
        r["msg_id"] for r in results
        if "BE_LOCK" in str(r.get("exit_reason") or "").upper()
        or "AUTO_BE" in str(r.get("exit_reason") or "").upper()
    ]
    stop_mod_attempts = sum(int(r.get("stop_modify_attempts") or 0) for r in results)
    stop_mod_failures = sum(int(r.get("stop_modify_failures") or 0) for r in results)
    explicit_be_events = [r.get("explicit_be") for r in results if r.get("explicit_be")]

    tp_completion = {
        f"TP{k}": round(sum(1 for r in results if int(r.get("max_stage") or 0) >= k) / n, 4) if n else 0.0
        for k in range(1, 5)
    }
    errors = [{"msg_id": r["msg_id"], "error": r["error"]} for r in results if r.get("error")]

    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats_be": flats,
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "gross_profit_gbp": gross_profit,
        "gross_loss_gbp": gross_loss,
        "net_profit_gbp": net,
        "net_expectancy_gbp": round(net / n, 4) if n else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown_gbp": round(max_dd, 2),
        "max_drawdown_basis": "cumulative realized-GBP equity curve, trade-close order (not intra-trade)",
        "avg_winner_gbp": round(gross_profit / len(wins), 4) if wins else 0.0,
        "avg_loser_gbp": round(gross_loss / len(losses), 4) if losses else 0.0,
        "peak_mfe_gbp": peak_mfe_gbp,
        "mfe_captured_gbp": captured_gbp,
        "mfe_given_back_gbp": giveback_gbp,
        "avg_mfe_capture_pct_all": round(sum(caps_all) / len(caps_all), 1) if caps_all else None,
        "avg_mfe_capture_pct_winners": round(sum(caps_win) / len(caps_win), 1) if caps_win else None,
        "premature_stop_outs": len(premature),
        "premature_stop_out_trades": premature,
        "premature_definition": "protected-stop exit (*_SL except initial BROKER_SL) realizing < 50% of peak MFE",
        "be_armed_trades": be_armed_trades,
        "be_armed_frequency": len(be_armed_trades),
        "auto_be_armed_trades": auto_be_armed_trades,
        "auto_be_armed_frequency": len(auto_be_armed_trades),
        "mfe_lock_armed_trades": mfe_lock_armed_trades,
        "be_family_exit_trades": be_family_exits,
        "be_family_exits": len(be_family_exits),
        "stop_modify_attempts": stop_mod_attempts,
        "stop_modify_failures": stop_mod_failures,
        "explicit_be_events": explicit_be_events,
        "avg_time_to_first_protection_min": round(sum(tt_vals) / len(tt_vals), 2) if tt_vals else None,
        "trades_protected": len(tt_vals),
        "never_protected_trades": never_protected,
        "slippage_estimate_dollars_per_side": slip,
        "commission_estimate_dollars_per_oz_round_turn": comm,
        "cost_adjusted": {
            "basis": "tape per-bar spread in bid/ask + round-turn slippage (2 x per-side) + commission estimate ($/oz round turn)",
            "net_profit_gbp": adj_net,
            "net_expectancy_gbp": round(adj_net / n, 4) if n else 0.0,
            "profit_factor": adj_pf,
            "wins": len(adj_wins),
            "losses": len(adj_losses),
        },
        "tp_completion_rates": tp_completion,
        "exit_bucket_distribution": exit_buckets,
        "exit_reason_distribution": exit_reason_dist,
        "initial_stop_loss_exits": exit_buckets.get("initial_stop_loss", 0),
        "protected_stop_exits": exit_buckets.get("protected_stop", 0),
        "error_count": len(errors),
        "errors": errors,
    }


def _evaluate_trailing_gates(
    arm_a: Dict[str, Any],
    arm_b: Dict[str, Any],
    variants: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """g1-g5 per experimental variant, same formulas as the shipped gates.

    g1-g4 substitute the variant for Arm B against the frozen Arm A reference;
    g5 compares the variant against the pinned baseline B (net + tail no worse,
    zero errors). Winner non-regression (9905/9705) is reported vs baseline B;
    'material' degradation = capture drop > 5.0 percentage points or GBP drop
    > 0.10 on either winner. The gate_3 thresholds are used AS-IS (the
    0.090 pts*lot == GBP 6.83 vs GBP 6.125 inconsistency is a finding, not
    something to 'fix' here).
    """
    sa = arm_a["summary"]
    sb = arm_b["summary"]
    by_id_b = {r["msg_id"]: r for r in arm_b["results"]}
    out: Dict[str, Any] = {}
    for name, payload in variants.items():
        sv = payload["summary"]
        g1 = sv["net_gbp"] > sa["net_gbp"]
        g2 = sv["tail_loss_gbp_13078_9953"] > sa["tail_loss_gbp_13078_9953"]
        g3 = (
            sv["choke_trio_gbp_13153_9934_13167"] > 2.45 * 2.5
            and sv["choke_trio_pts_lot"] >= 0.090
            and sv["trade_13153_max_stage"] >= 2
        )
        g4 = (
            sa["error_count"] == 0
            and sv["error_count"] == 0
            and sv["trade_9934_banked_tracked"] > 0.0
            and len(sa["unflagged_rollover_exits"]) == 0
            and len(sv["unflagged_rollover_exits"]) == 0
        )
        g5 = (
            sv["net_gbp"] >= sb["net_gbp"]
            and sv["tail_loss_gbp_13078_9953"] >= sb["tail_loss_gbp_13078_9953"]
            and sv["error_count"] == 0
        )
        winners: Dict[str, Any] = {}
        for mid in ("9905", "9705"):
            rv = next((r for r in payload["results"] if r["msg_id"] == mid), {})
            rb = by_id_b.get(mid, {})
            cap_b = rb.get("mfe_capture_pct")
            cap_v = rv.get("mfe_capture_pct")
            gbp_b = float(rb.get("sim_gbp") or 0.0)
            gbp_v = float(rv.get("sim_gbp") or 0.0)
            cap_drop = (float(cap_b) - float(cap_v)) if (cap_b is not None and cap_v is not None) else None
            material = bool((cap_drop is not None and cap_drop > 5.0) or (gbp_b - gbp_v) > 0.10)
            winners[mid] = {
                "capture_baseline_pct": cap_b,
                "capture_variant_pct": cap_v,
                "capture_drop_pp": round(cap_drop, 1) if cap_drop is not None else None,
                "gbp_baseline": gbp_b,
                "gbp_variant": gbp_v,
                "material_degradation": material,
            }
        out[name] = {
            "gate_1_net_profit": g1,
            "gate_2_tail_protection": g2,
            "gate_3_mfe_capture_and_13153_stage2": g3,
            "gate_3_legs": {
                "trio_gbp": sv["choke_trio_gbp_13153_9934_13167"],
                "trio_gbp_threshold": 2.45 * 2.5,
                "trio_pts_lot": sv["choke_trio_pts_lot"],
                "trio_pts_lot_threshold": 0.090,
                "trade_13153_max_stage": sv["trade_13153_max_stage"],
            },
            "gate_4_zero_regression": g4,
            "g5_vs_baseline_b": g5,
            "all_gates_passed": bool(g1 and g2 and g3 and g4 and g5),
            "winner_non_regression": winners,
            "winner_non_regression_ok": all(not w["material_degradation"] for w in winners.values()),
        }
    return out


def _run_arm_subprocess(
    arm_mode: str,
    code_root: Path,
    bars_path: Path,
    out_path: Path,
    catalog_path: Optional[Path] = None,
    protection_telemetry: bool = False,
    exit_binding: bool = False,
    wick_gate: Optional[str] = None,
    interim_floor: Optional[str] = None,
) -> Dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(code_root)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--arm-mode",
        arm_mode,
        "--worker-bars",
        str(bars_path),
        "--worker-out",
        str(out_path),
    ]
    if catalog_path is not None:
        cmd += ["--worker-catalog", str(catalog_path)]
    if protection_telemetry:
        cmd += ["--worker-protection-telemetry"]
    if exit_binding:
        cmd += ["--worker-exit-binding"]
    if wick_gate:
        cmd += ["--worker-wick-gate", wick_gate]
    if interim_floor:
        cmd += ["--worker-interim-floor", interim_floor]
    proc = subprocess.run(
        cmd, cwd=str(code_root), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Arm {arm_mode.upper()} worker failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(out_path.read_text(encoding="utf-8"))


def _run_arm_a_isolated(bars_path: Path, out_path: Path) -> Dict[str, Any]:
    wt_parent = Path(tempfile.mkdtemp(prefix="ab_replay_wt_4544f7f_"))
    wt_dir = wt_parent / "wt"
    try:
        add_res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "add", "--detach", str(wt_dir), BASELINE_COMMIT],
            capture_output=True,
            text=True,
        )
        if add_res.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {add_res.stderr.strip()}")
        return _run_arm_subprocess("a", wt_dir, bars_path, out_path)
    finally:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(wt_dir)],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "prune"],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(wt_parent, ignore_errors=True)


def _evaluate_gates(
    arm_a: Optional[Dict[str, Any]],
    arm_b: Optional[Dict[str, Any]],
    arm_c: Optional[Dict[str, Any]] = None,
    arm_other: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Arm C is compared against ARM B (not against Arm A): the R lock is an
    # increment on Arm B, and the gate is absent when Arm C was not run.
    if arm_c and arm_b:
        sc = arm_c["summary"]
        sb2 = arm_b["summary"]
        # tail_loss_gbp_13078_9953 is a SIGNED PnL sum (negative = a loss). Better
        # tail protection means a LESS negative value, exactly as gate_2 compares it
        # (sb.tail > sa.tail). So "C's tail loss no worse than B's" is sc.tail >= sb.tail.
        g5 = (
            sc["net_gbp"] >= sb2["net_gbp"]
            and sc["tail_loss_gbp_13078_9953"] >= sb2["tail_loss_gbp_13078_9953"]
            and sc["error_count"] == 0
        )
    else:
        g5 = None

    if arm_a and arm_b:
        sa = arm_a["summary"]
        sb = arm_b["summary"]
        g1 = sb["net_gbp"] > sa["net_gbp"]
        g2 = sb["tail_loss_gbp_13078_9953"] > sa["tail_loss_gbp_13078_9953"]
        g3 = (
            sb["choke_trio_gbp_13153_9934_13167"] > 2.45 * 2.5
            and sb["choke_trio_pts_lot"] >= 0.090
            and sb["trade_13153_max_stage"] >= 2
        )
        g4 = (
            sa["error_count"] == 0
            and sb["error_count"] == 0
            and sb["trade_9934_banked_tracked"] > 0.0
            and len(sa["unflagged_rollover_exits"]) == 0
            and len(sb["unflagged_rollover_exits"]) == 0
        )
        return {
            "mode": "both",
            "gate_1_net_profit": g1,
            "gate_2_tail_protection": g2,
            "gate_3_mfe_capture_and_13153_stage2": g3,
            "gate_4_zero_regression": g4,
            "g5": g5,
            "all_gates_passed": bool(g1 and g2 and g3 and g4 and g5 is not False),
        }

    single = arm_a or arm_b or arm_c or arm_other
    assert single is not None
    ss = single["summary"]
    g4_single = ss["error_count"] == 0 and len(ss["unflagged_rollover_exits"]) == 0
    return {
        "mode": f"single_{single['arm']}",
        "gate_4_zero_regression": g4_single,
        "g5": g5,
        "all_gates_passed": bool(g4_single and g5 is not False),
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    bars_meta = report["bars_meta"]
    arm_a = report.get("arm_a")
    arm_b = report.get("arm_b")
    arm_c = report.get("arm_c")
    gates = report["gates"]

    lines: List[str] = [
        "# 12-Trade XAUUSD.s M1-Bar A/B Replay Proof",
        "",
        f"- **Generated UTC:** `{report['generated_utc']}`",
        f"- **Bars Loaded:** `{bars_meta['count']}` M1 bars (`{bars_meta['first_utc']}` -> `{bars_meta['last_utc']}`, spread_override=`{bars_meta['spread_override']}`)",
        f"- **GBP Conversion Rate:** `£{GBP_PER_PT_001_LOT:.4f}` per `($1.00/oz × 0.01 lot)`",
        f"- **Rollover Window Flagged:** `21:55–23:05 UTC`",
        "",
    ]

    if arm_a and arm_b:
        sa = arm_a["summary"]
        sb = arm_b["summary"]
        by_id_a = {r["msg_id"]: r for r in arm_a["results"]}
        by_id_b = {r["msg_id"]: r for r in arm_b["results"]}
        lines += [
            "## Aggregate A/B Comparison",
            "",
            "| Metric | Actual Live MT5 | Arm A (`4544f7f`) | Arm B (`v3.6.0` Calibrated) | Delta (B - A) |",
            "|---|---:|---:|---:|---:|",
            f"| **Net PnL (GBP)** | `£{sa['actual_live_gbp']:+.2f}` | `£{sa['net_gbp']:+.2f}` | `£{sb['net_gbp']:+.2f}` | `£{sb['net_gbp'] - sa['net_gbp']:+.2f}` |",
            f"| **Tail Loss (`13078+9953`)** | `-£48.60` | `£{sa['tail_loss_gbp_13078_9953']:+.2f}` | `£{sb['tail_loss_gbp_13078_9953']:+.2f}` | `£{sb['tail_loss_gbp_13078_9953'] - sa['tail_loss_gbp_13078_9953']:+.2f}` |",
            f"| **Choke Trio (`13153+9934+13167`)** | `+£2.45` | `£{sa['choke_trio_gbp_13153_9934_13167']:+.2f}` | `£{sb['choke_trio_gbp_13153_9934_13167']:+.2f}` | `£{sb['choke_trio_gbp_13153_9934_13167'] - sa['choke_trio_gbp_13153_9934_13167']:+.2f}` |",
            f"| **`13153` Max Stage** | `0` | `{sa['trade_13153_max_stage']}` | `{sb['trade_13153_max_stage']}` | `+{sb['trade_13153_max_stage'] - sa['trade_13153_max_stage']}` |",
            f"| **Rollover Exits (`21:55-23:05Z`)** | `0` | `{len(sa['rollover_exits'])}` | `{len(sb['rollover_exits'])}` | `0` |",
            "",
            "## Per-Trade A/B Table",
            "",
            "| Msg ID | Dir | Entry | Pot. MFE | Live (£) | Arm A (£) | Arm A Exit (Stage) | Arm B (£) | Arm B Exit (Stage) | Arm B MFE Cap % | Rollover |",
            "|---|---|---:|---:|---:|---:|---|---:|---|---:|---|",
        ]
        for t in TRADE_CATALOG:
            mid = t["msg_id"]
            ra = by_id_a[mid]
            rb = by_id_b[mid]
            ro = "ROLLOVER" if (ra["rollover_flag"] or rb["rollover_flag"]) else "OK"
            cap_b = f"{rb['mfe_capture_pct']:.1f}%" if rb["mfe_capture_pct"] is not None else "N/A"
            lines.append(
                f"| `{mid}` | {ra['dir']} | `{ra['avg_entry']:.2f}` | `${ra['potential_mfe_before_sl']:.2f}` | "
                f"`£{ra['actual_mt5_gbp']:+.2f}` | `£{ra['sim_gbp']:+.2f}` | `{ra['exit_reason']}` (S{ra['max_stage']}) | "
                f"`£{rb['sim_gbp']:+.2f}` | `{rb['exit_reason']}` (S{rb['max_stage']}) | `{cap_b}` | `{ro}` |"
            )
    else:
        single = arm_a or arm_b or arm_c or next(
            (report.get(f"arm_{m}") for m in list(TRAILING_EXPERIMENT_ARMS) + ["t3tight", "s100", "s105", "l2pp", "l3pp", "wc", "wp2", "wp3"] + list(FLOOR_RESEARCH_ARMS) if report.get(f"arm_{m}")), None
        )
        assert single is not None
        ss = single["summary"]
        label = {
            "a": "Arm A (`4544f7f`)",
            "b": "Arm B (`v3.6.0` Calibrated)",
            "c": "Arm C (R Lock Only)",
            **{m: f"EXPERIMENTAL Arm {m.upper()} ({TRAILING_EXPERIMENT_ARMS[m]['label']})" for m in TRAILING_EXPERIMENT_ARMS},
            "t3tight": f"EXPERIMENTAL Arm T3TIGHT ({TIGHT_COMPARISON_ARMS['t3tight']['label']})",
            "s100": f"EXPERIMENTAL Arm S100 ({SCALE_COMPARISON_ARMS['s100']['label']})",
            "s105": f"EXPERIMENTAL Arm S105 ({SCALE_COMPARISON_ARMS['s105']['label']})",
            "l2pp": f"RESEARCH Arm L2PP ({LADDER_RESEARCH_ARMS['l2pp']['label']})",
            "l3pp": f"RESEARCH Arm L3PP ({LADDER_RESEARCH_ARMS['l3pp']['label']})",
            "wc": f"RESEARCH Arm WC ({WICK_RESEARCH_ARMS['wc']['label']})",
            "wp2": f"RESEARCH Arm WP2 ({WICK_RESEARCH_ARMS['wp2']['label']})",
            "wp3": f"RESEARCH Arm WP3 ({WICK_RESEARCH_ARMS['wp3']['label']})",
            **{m: f"RESEARCH Arm {m.upper()} ({FLOOR_RESEARCH_ARMS[m]['label']})" for m in FLOOR_RESEARCH_ARMS},
        }.get(single["arm"], "Arm " + str(single["arm"]))
        lines += [
            f"## {label} Summary",
            "",
            f"- **Net Simulated PnL:** `£{ss['net_gbp']:+.2f}` (pts*lot=`{ss['net_pts_lot']:+.4f}`) vs Actual Live `£{ss['actual_live_gbp']:+.2f}`",
            f"- **Tail Loss (`13078+9953`):** `£{ss['tail_loss_gbp_13078_9953']:+.2f}`",
            f"- **Choke Trio (`13153+9934+13167`):** `£{ss['choke_trio_gbp_13153_9934_13167']:+.2f}` (pts*lot=`{ss['choke_trio_pts_lot']:+.4f}`)",
            f"- **`13153` Max Stage:** `{ss['trade_13153_max_stage']}`",
            f"- **Rollover Exits (`21:55-23:05Z`):** `{ss['rollover_exits'] or 'None'}` | Errors: `{ss['error_count']}`",
            "",
            "| Msg ID | Dir | Lot | Avg Entry | Peak MFE | Pot. MFE | Live (£) | Sim (£) | Exit Price | Exit Reason | Stage | MFE Cap % | Rollover |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|",
        ]
        for r in single["results"]:
            ro = "ROLLOVER" if r["rollover_flag"] else "OK"
            cap = f"{r['mfe_capture_pct']:.1f}%" if r["mfe_capture_pct"] is not None else "N/A"
            epx = f"{r['exit_price']:.2f}" if r["exit_price"] is not None else "N/A"
            lines.append(
                f"| `{r['msg_id']}` | {r['dir']} | `{r['total_lot']:.2f}` | `{r['avg_entry']:.2f}` | "
                f"`${r['peak_mfe_dollars']:.2f}` | `${r['potential_mfe_before_sl']:.2f}` | "
                f"`£{r['actual_mt5_gbp']:+.2f}` | `£{r['sim_gbp']:+.2f}` | `{epx}` | "
                f"`{r['exit_reason']}` | `S{r['max_stage']}` | `{cap}` | `{ro}` |"
            )

    if arm_c:
        sc = arm_c["summary"]
        sb_c = arm_b["summary"] if arm_b else None
        net_b = f"`£{sb_c['net_gbp']}`" if sb_c else "`n/a`"
        tail_b = f"`£{sb_c['tail_loss_gbp_13078_9953']}`" if sb_c else "`n/a`"
        lines += [
            "",
            "### Arm C (R Lock Only)",
            "",
            f"- **Net PnL:** `£{sc['net_gbp']}` (Arm B: {net_b})",
            f"- **Tail Loss (13078 + 9953):** `£{sc['tail_loss_gbp_13078_9953']}` "
            f"(Arm B: {tail_b})",
            f"- **Trade Count:** `{sc['trade_count']}` | Errors: `{sc['error_count']}`",
            f"- **Production ATR engine picker calls:** `{sc.get('engine_picker_calls', 0)}`",
            f"- **R-lock precedence:** `{sc.get('r_lock_wins', 0)}` picker wins "
            f"(`{sc.get('r_lock_wide_wins', 0)}` on wide stops R>=$5), "
            f"max R=`{sc.get('r_lock_max_r', 0.0)}`, "
            f"trades: `{', '.join(sc.get('r_lock_trades', [])) or 'none'}`",
        ]
        rlock_rows = [r for r in arm_c["results"] if int(r.get("r_lock_wins", 0)) > 0]
        if rlock_rows:
            lines += [
                "",
                "| Msg ID | Initial R ($/oz) | R-lock wins | Wide (R>=$5) wins | f=0 (BE-equiv) | f>0 | Winning R range |",
                "|---|---:|---:|---:|---:|---:|---|",
            ]
            for r in rlock_rows:
                rmin = r.get("r_lock_winning_r_min")
                rmax = r.get("r_lock_winning_r_max", 0.0)
                rrange = f"${rmin:.2f} - ${rmax:.2f}" if rmin is not None else "n/a"
                lines.append(
                    f"| `{r['msg_id']}` | `{r.get('initial_r_per_oz', 0.0)}` | "
                    f"`{r.get('r_lock_wins', 0)}` | `{r.get('r_lock_wide_wins', 0)}` | "
                    f"`{r.get('r_lock_fraction_zero_wins', 0)}` | `{r.get('r_lock_fraction_pos_wins', 0)}` | "
                    f"`{rrange}` |"
                )
        # Phase 2 lock_fraction distribution + structural wide-stop evidence.
        lines += [
            "",
            "#### R-lock lock_fraction distribution (Arm C)",
            "",
            f"- **Wins at fraction 0 (BE-equivalents):** `{sc.get('r_lock_fraction_zero_wins', 0)}`",
            f"- **Wins at fraction > 0:** `{sc.get('r_lock_fraction_pos_wins', 0)}`",
            f"- **Winning R range ($/oz):** `{sc.get('r_lock_winning_r_min')}` .. `{sc.get('r_lock_winning_r_max', 0.0)}`",
            f"- **Fraction histogram:** `{sc.get('r_lock_fraction_distribution', {})}`",
            f"- **Max trail_dist at a win:** `${sc.get('r_lock_max_trail_dist_at_win', 0.0)}` | "
            f"max trail_dist observed (any call): `${sc.get('max_trail_dist_observed', 0.0)}`",
            "",
            "#### Structural wide-stop check (trail_dist vs (t_k - f_k)*R)",
            "",
            "| Msg ID | Widest layer R | Easiest profit margin (t-f)*R | Max trail_dist | Structurally blocked |",
            "|---|---:|---:|---:|---|",
        ]
        for r in arm_c["results"]:
            if float(r.get("r_lock_widest_r", 0.0)) <= 0.0:
                continue
            margin = r.get("r_lock_min_profit_margin")
            mtxt = f"${margin:.2f}" if margin is not None else "n/a"
            blocked = "YES" if r.get("r_lock_structural_blocked") else "no"
            lines.append(
                f"| `{r['msg_id']}` | `${r.get('r_lock_widest_r', 0.0):.2f}` | `{mtxt}` | "
                f"`${r.get('max_trail_dist_observed', 0.0):.2f}` | `{blocked}` |"
            )
        lines += [
            "",
            f"- **Trio peaks reach 0.5R?** `{sc.get('trio_peaks_reach_half_r', False)}` "
            f"(peak/R multiples: `{sc.get('trio_peak_r_multiples', {})}`)",
            f"- **Wide trades structurally blocked:** `{', '.join(sc.get('r_lock_structural_blocked_wide_trades', [])) or 'none'}`",
        ]

    arm_cx = report.get("arm_cx")
    if arm_cx and arm_c:
        scx = arm_cx["summary"]
        sc_c = arm_c["summary"]
        lines += [
            "",
            "### EXPERIMENTAL Arm CX (re-tiered R-lock) vs Arm C - NOT shipped",
            "",
            f"- **Override (replay arm-param only):** `R_LOCK_TIERS = {ARM_CX_EXPERIMENTAL_PARAMS['R_LOCK_TIERS']}`",
            "",
            "| Metric | Arm C (shipped tiers) | Arm CX (experimental) | Delta (CX - C) |",
            "|---|---:|---:|---:|",
            f"| Net PnL (GBP) | `£{sc_c['net_gbp']:+.2f}` | `£{scx['net_gbp']:+.2f}` | `£{scx['net_gbp'] - sc_c['net_gbp']:+.2f}` |",
            f"| Tail (13078+9953) | `£{sc_c['tail_loss_gbp_13078_9953']:+.2f}` | `£{scx['tail_loss_gbp_13078_9953']:+.2f}` | `£{scx['tail_loss_gbp_13078_9953'] - sc_c['tail_loss_gbp_13078_9953']:+.2f}` |",
            f"| R-lock wins | `{sc_c.get('r_lock_wins', 0)}` | `{scx.get('r_lock_wins', 0)}` | `{scx.get('r_lock_wins', 0) - sc_c.get('r_lock_wins', 0):+d}` |",
            f"| Wide (R>=$5) wins | `{sc_c.get('r_lock_wide_wins', 0)}` | `{scx.get('r_lock_wide_wins', 0)}` | `{scx.get('r_lock_wide_wins', 0) - sc_c.get('r_lock_wide_wins', 0):+d}` |",
            f"| Fraction>0 wins | `{sc_c.get('r_lock_fraction_pos_wins', 0)}` | `{scx.get('r_lock_fraction_pos_wins', 0)}` | `{scx.get('r_lock_fraction_pos_wins', 0) - sc_c.get('r_lock_fraction_pos_wins', 0):+d}` |",
            f"| Structurally blocked wide trades | `{len(sc_c.get('r_lock_structural_blocked_wide_trades', []))}` | `{len(scx.get('r_lock_structural_blocked_wide_trades', []))}` | - |",
            "",
            "Re-tiering lowers the easiest profit margin from 0.75R to 0.5R, but any layer with "
            "0.5R above the $8 trail clamp ceiling (R > $16) remains structurally unreachable. "
            "This arm is experimental evidence only; production R_LOCK_TIERS is unchanged.",
        ]

    trailing_experiments = report.get("trailing_experiments")
    trailing_gates = report.get("trailing_gates")
    if trailing_experiments and arm_b:
        bm = trailing_experiments["baseline_metrics"]
        arm_keys = list(trailing_experiments["arms"].keys())
        cols = ["B (baseline)"] + [m.upper() for m in arm_keys]
        lines += [
            "",
            "## EXPERIMENTAL Phase 3+4 Trailing A/B (pinned tape) - NOT shipped",
            "",
            f"- {trailing_experiments['note']}",
            "",
            "| Arm | Override vs baseline B |",
            "|---|---|",
            "| B | none (pinned baseline; R-lock OFF) |",
        ]
        for m in arm_keys:
            ov = trailing_experiments["arms"][m]["overrides_vs_baseline_b"]
            lines.append(f"| {m.upper()} | `{ov}` ({trailing_experiments['arms'][m]['label']}) |")

        def _mrow(label: str, key: str, fmt: str = "{}") -> str:
            cells = [fmt.format(bm.get(key))]
            for m in arm_keys:
                cells.append(fmt.format(trailing_experiments["arms"][m]["metrics"].get(key)))
            return f"| {label} | " + " | ".join(f"`{c}`" for c in cells) + " |"

        lines += [
            "",
            "| Metric | " + " | ".join(cols) + " |",
            "|---|" + "---:|" * len(cols),
            _mrow("Total trades", "total_trades"),
            "| Wins / Losses / Flats | " + " | ".join(
                f"`{x['wins']}/{x['losses']}/{x['flats_be']}`"
                for x in [bm] + [trailing_experiments["arms"][m]["metrics"] for m in arm_keys]
            ) + " |",
            _mrow("Win rate %", "win_rate_pct"),
            _mrow("Gross profit (GBP)", "gross_profit_gbp", "{:+.2f}"),
            _mrow("Gross loss (GBP)", "gross_loss_gbp", "{:+.2f}"),
            _mrow("Net profit (GBP)", "net_profit_gbp", "{:+.2f}"),
            _mrow("Profit factor", "profit_factor"),
            _mrow("Max drawdown (GBP, equity curve)", "max_drawdown_gbp", "{:.2f}"),
            _mrow("Avg R/trade", "avg_r_per_trade"),
            _mrow("Avg MFE capture % (all)", "avg_mfe_capture_pct_all"),
            _mrow("Avg MFE capture % (winners)", "avg_mfe_capture_pct_winners"),
            _mrow("Avg realised profit (GBP)", "avg_realized_profit_gbp", "{:+.4f}"),
            _mrow("Surrendered after peak (GBP)", "profit_surrendered_after_peak_gbp", "{:.2f}"),
            _mrow("Initial stop-loss exits", "stop_loss_frequency"),
            _mrow("BE-closure frequency", "be_closure_frequency"),
            _mrow("Premature trailing exits (<50% peak)", "premature_trailing_exit_frequency"),
            _mrow("Avg holding time (min)", "avg_holding_time_min"),
            "",
            f"- MAE: {bm['mae']}",
            f"- Premature definition: {bm['premature_definition']}",
            f"- TP stage distribution B: `{bm['tp_stage_distribution']}`",
        ]
        for m in arm_keys:
            mm = trailing_experiments["arms"][m]["metrics"]
            lines.append(f"- TP stage distribution {m.upper()}: `{mm['tp_stage_distribution']}` | "
                         f"ATR-trail engaged trades: `{mm['atr_trail_engagement']['trades']}` | "
                         f"R-lock wins: `{mm['r_lock_engagement']['picker_wins']}` | "
                         f"exit reasons: `{mm['exit_reason_distribution']}`")

        lines += [
            "",
            "### gate_3 legs + choke-trio giveback per arm",
            "",
            "| Arm | Trio GBP (>6.125) | Trio pts*lot (>=0.090) | 13153 stage (>=2) | 13153 giveback $/oz | 9934 giveback $/oz | 13167 giveback $/oz | 9934 exit |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        sb = arm_b["summary"]
        trio_b = bm["choke_trio_detail"]
        lines.append(
            f"| B | `{sb['choke_trio_gbp_13153_9934_13167']:+.2f}` | `{sb['choke_trio_pts_lot']:.4f}` | "
            f"`{sb['trade_13153_max_stage']}` | `{trio_b['13153'].get('giveback_usd_per_oz')}` | "
            f"`{trio_b['9934'].get('giveback_usd_per_oz')}` | `{trio_b['13167'].get('giveback_usd_per_oz')}` | "
            f"`{trio_b['9934'].get('exit_reason')} @ {trio_b['9934'].get('exit_price')}` |"
        )
        for m in arm_keys:
            payload = report.get(f"arm_{m}")
            sv = payload["summary"]
            mm = trailing_experiments["arms"][m]["metrics"]
            trio = mm["choke_trio_detail"]
            lines.append(
                f"| {m.upper()} | `{sv['choke_trio_gbp_13153_9934_13167']:+.2f}` | `{sv['choke_trio_pts_lot']:.4f}` | "
                f"`{sv['trade_13153_max_stage']}` | `{trio['13153'].get('giveback_usd_per_oz')}` | "
                f"`{trio['9934'].get('giveback_usd_per_oz')}` | `{trio['13167'].get('giveback_usd_per_oz')}` | "
                f"`{trio['9934'].get('exit_reason')} @ {trio['9934'].get('exit_price')}` |"
            )

        lines += [
            "",
            "### Winner non-regression (capture % / GBP, vs baseline B)",
            "",
            "| Arm | 9905 capture | 9905 GBP | 9705 capture | 9705 GBP |",
            "|---|---:|---:|---:|---:|",
            f"| B | `{bm['winners_non_regression']['9905'].get('mfe_capture_pct')}` | `{bm['winners_non_regression']['9905'].get('sim_gbp'):+.2f}` | "
            f"`{bm['winners_non_regression']['9705'].get('mfe_capture_pct')}` | `{bm['winners_non_regression']['9705'].get('sim_gbp'):+.2f}` |",
        ]
        for m in arm_keys:
            mm = trailing_experiments["arms"][m]["metrics"]
            w = mm["winners_non_regression"]
            lines.append(
                f"| {m.upper()} | `{w['9905'].get('mfe_capture_pct')}` | `{w['9905'].get('sim_gbp'):+.2f}` | "
                f"`{w['9705'].get('mfe_capture_pct')}` | `{w['9705'].get('sim_gbp'):+.2f}` |"
            )

        if trailing_gates:
            lines += [
                "",
                "### Gates g1-g5 per experimental variant (g1-g4 vs Arm A; g5 vs baseline B)",
                "",
                "| Arm | g1 net | g2 tail | g3 trio | g4 zero-regression | g5 vs B | All | Winners OK |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for m in arm_keys:
                tg = trailing_gates.get(m, {})
                lines.append(
                    f"| {m.upper()} | `{tg.get('gate_1_net_profit')}` | `{tg.get('gate_2_tail_protection')}` | "
                    f"`{tg.get('gate_3_mfe_capture_and_13153_stage2')}` | `{tg.get('gate_4_zero_regression')}` | "
                    f"`{tg.get('g5_vs_baseline_b')}` | `{tg.get('all_gates_passed')}` | `{tg.get('winner_non_regression_ok')}` |"
                )
            lines += [
                "",
                "Finding (not 'fixed'): gate_3's 0.090 pts*lot leg == GBP 6.83 at the pinned rate, "
                "stricter than its GBP 6.125 leg; thresholds are inconsistent by design or by accident "
                "and need an explicit owner decision.",
            ]

    for _comp_key, _lk, _rk, _ll, _rl, _dl, _title, _dkey in (
        ("tight_comparison", "t3", "t3tight", "T3", "T3-tight", "tight - T3",
         "## T3 vs T3-tight (production engine replay) - EXPERIMENTAL, NOT shipped", "delta_tight_minus_t3"),
        ("scale_comparison", "s100", "s105", "Scale 1.00", "Scale 1.05", "1.05 - 1.00",
         "## Upgraded trail-distance scale 1.00 vs 1.05 (production engine replay) - EXPERIMENTAL, NOT shipped",
         "delta_s105_minus_s100"),
        ("ladder_minus2pp_comparison", "s105", "l2pp", "Current", "-2pp", "-2pp - current",
         "## RESEARCH ladder -2pp (clipping tiers) vs current - NOT adopted", "delta_l2pp_minus_current"),
        ("ladder_minus3pp_comparison", "s105", "l3pp", "Current", "-3pp", "-3pp - current",
         "## RESEARCH ladder -3pp (clipping tiers) vs current - NOT adopted", "delta_l3pp_minus_current"),
        ("wick_close_comparison", "s105", "wc", "C baseline", "A close", "A - C",
         "## RESEARCH wick-aware A (candle-close confirmation) vs C baseline - NOT adopted", "delta_wc_minus_current"),
        ("wick_persist2_comparison", "s105", "wp2", "C baseline", "B N=2", "B2 - C",
         "## RESEARCH wick-aware B (N=2 persistence) vs C baseline - NOT adopted", "delta_wp2_minus_current"),
        ("wick_persist3_comparison", "s105", "wp3", "C baseline", "B N=3", "B3 - C",
         "## RESEARCH wick-aware B (N=3 persistence) vs C baseline - NOT adopted", "delta_wp3_minus_current"),
    ):
        tight = report.get(_comp_key)
        if not tight:
            continue
        mt3 = tight["arms"][_lk]["metrics"]
        mtt = tight["arms"][_rk]["metrics"]
        d = tight[_dkey]
        lines += [
            "",
            _title,
            "",
            "- {}".format(tight["note"]),
            "- Slippage allowance: `${:.2f}/oz per side` on top of the tape's per-bar spread.".format(tight["slippage_estimate_dollars_per_side"]),
            "- Commission estimate: `${:.2f}/oz round turn` ($7 per 1.00 lot RT; documented assumption).".format(tight["commission_estimate_dollars_per_oz_round_turn"]),
            "",
            "| Metric | {} | {} | Delta ({}) |".format(_ll, _rl, _dl),
            "|---|---:|---:|---:|",
        ]

        def _fmt(v: Any, fmt: str) -> str:
            if v is None:
                return "n/a"
            try:
                return fmt.format(v)
            except (TypeError, ValueError):
                return str(v)

        def _row(label: str, a: Any, b: Any, delta: Any = None, fmt: str = "{}") -> None:
            dv = delta if delta is not None else (
                round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else "-"
            )
            lines.append("| {} | `{}` | `{}` | `{}` |".format(
                label, _fmt(a, fmt), _fmt(b, fmt), _fmt(dv, fmt) if isinstance(dv, (int, float)) else dv))

        _row("Net profit (GBP)", mt3["net_profit_gbp"], mtt["net_profit_gbp"], d["net_profit_gbp"], "{:+.2f}")
        _row("Net expectancy (GBP/trade)", mt3["net_expectancy_gbp"], mtt["net_expectancy_gbp"], d["net_expectancy_gbp"], "{:+.4f}")
        _row("Profit factor", mt3["profit_factor"], mtt["profit_factor"])
        _row("Wins / losses / flats", "{}/{}/{}".format(mt3["wins"], mt3["losses"], mt3["flats_be"]), "{}/{}/{}".format(mtt["wins"], mtt["losses"], mtt["flats_be"]), "-")
        _row("Avg winner (GBP)", mt3["avg_winner_gbp"], mtt["avg_winner_gbp"], fmt="{:+.4f}")
        _row("Avg loser (GBP)", mt3["avg_loser_gbp"], mtt["avg_loser_gbp"], fmt="{:+.4f}")
        _row("Peak MFE (GBP, lot-wtd)", mt3["peak_mfe_gbp"], mtt["peak_mfe_gbp"], fmt="{:.2f}")
        _row("MFE captured (GBP)", mt3["mfe_captured_gbp"], mtt["mfe_captured_gbp"], fmt="{:+.2f}")
        _row("MFE given back (GBP)", mt3["mfe_given_back_gbp"], mtt["mfe_given_back_gbp"], d["mfe_given_back_gbp"], "{:+.2f}")
        _row("Avg MFE capture % (all)", mt3["avg_mfe_capture_pct_all"], mtt["avg_mfe_capture_pct_all"])
        _row("Premature stop-outs (<50% peak)", mt3["premature_stop_outs"], mtt["premature_stop_outs"], d["premature_stop_outs"])
        _row("Avg time to first protection (min)", mt3["avg_time_to_first_protection_min"], mtt["avg_time_to_first_protection_min"])
        _row("Trades never protected", len(mt3["never_protected_trades"]), len(mtt["never_protected_trades"]))
        _row("Cost-adj net (GBP)", mt3["cost_adjusted"]["net_profit_gbp"], mtt["cost_adjusted"]["net_profit_gbp"], d["cost_adjusted_net_profit_gbp"], "{:+.2f}")
        _row("Cost-adj expectancy (GBP)", mt3["cost_adjusted"]["net_expectancy_gbp"], mtt["cost_adjusted"]["net_expectancy_gbp"], fmt="{:+.4f}")
        _row("Cost-adj profit factor", mt3["cost_adjusted"]["profit_factor"], mtt["cost_adjusted"]["profit_factor"])
        for k in range(1, 5):
            _row("TP{} completion rate".format(k), mt3["tp_completion_rates"]["TP{}".format(k)], mtt["tp_completion_rates"]["TP{}".format(k)], fmt="{:.2%}")
        _row("Initial stop-loss exits", mt3["initial_stop_loss_exits"], mtt["initial_stop_loss_exits"])
        _row("Protected-stop exits", mt3["protected_stop_exits"], mtt["protected_stop_exits"])
        _row("Max drawdown (GBP, equity curve)", mt3["max_drawdown_gbp"], mtt["max_drawdown_gbp"], fmt="{:.2f}")
        _row("BE armed trades", mt3["be_armed_frequency"], mtt["be_armed_frequency"])
        _row("Auto-BE armed trades", mt3["auto_be_armed_frequency"], mtt["auto_be_armed_frequency"])
        _row("BE-family exits", mt3["be_family_exits"], mtt["be_family_exits"])
        _row("Stop-mod attempts", mt3["stop_modify_attempts"], mtt["stop_modify_attempts"])
        _row("Stop-mod failures", mt3["stop_modify_failures"], mtt["stop_modify_failures"])
        _row("Errors", mt3["error_count"], mtt["error_count"])
        lines += [
            "",
            "- Premature definition: {}".format(mt3["premature_definition"]),
            "- Never protected {}: `{}` | {}: `{}`".format(_ll, mt3["never_protected_trades"] or "none", _rl, mtt["never_protected_trades"] or "none"),
            "- Premature trades {}: `{}` | {}: `{}`".format(_ll, mt3["premature_stop_out_trades"] or "none", _rl, mtt["premature_stop_out_trades"] or "none"),
            "- Exit reasons {}: `{}`".format(_ll, mt3["exit_reason_distribution"]),
            "- Exit reasons {}: `{}`".format(_rl, mtt["exit_reason_distribution"]),
            "",
            "### Per-trade side by side",
            "",
            "| Msg | Dir | Peak MFE $/oz | {} GBP | {} exit (stage) | {} protect (min) | {} GBP | {} exit (stage) | {} protect (min) |".format(_ll, _ll, _ll, _rl, _rl, _rl),
            "|---|---|---:|---:|---|---:|---:|---|---:|",
        ]
        for row in tight["per_trade"]:
            a, b = row[_lk], row[_rk]
            ta = a["time_to_first_protection_min"]
            tb = b["time_to_first_protection_min"]
            lines.append(
                "| `{}` | {} | `{:.2f}` | `{:+.2f}` | `{}` (S{}) | `{}` | `{:+.2f}` | `{}` (S{}) | `{}` |".format(
                    row["msg_id"], row["dir"], a["peak_mfe_dollars"], a["sim_gbp"], a["exit_reason"], a["max_stage"],
                    ta if ta is not None else "never", b["sim_gbp"], b["exit_reason"], b["max_stage"],
                    tb if tb is not None else "never",
                )
            )

    for _cand, _comp in (report.get("floor_comparisons") or {}).items():
        _fl = FLOOR_RESEARCH_ARMS[_cand]["floor"]
        _ll, _rl = "C baseline", "floor " + _fl
        ma = _comp["arms"]["s105"]["metrics"]
        mb = _comp["arms"][_cand]["metrics"]
        d = _comp["delta_" + _cand + "_minus_current"]
        lines += [
            "",
            "## RESEARCH interim floor " + _fl + " vs C baseline - NOT adopted",
            "",
            "- {}".format(_comp["note"]),
            "",
            "| Metric | {} | {} | Delta |".format(_ll, _rl),
            "|---|---:|---:|---:|",
        ]
        def _ffmt(v: Any, fmt: str) -> str:
            if v is None:
                return "n/a"
            try:
                return fmt.format(v)
            except (TypeError, ValueError):
                return str(v)
        def _frow(label: str, a: Any, b: Any, delta: Any = None, fmt: str = "{}") -> None:
            dv = delta if delta is not None else (
                round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else "-"
            )
            lines.append("| {} | `{}` | `{}` | `{}` |".format(
                label, _ffmt(a, fmt), _ffmt(b, fmt), _ffmt(dv, fmt) if isinstance(dv, (int, float)) else dv))
        _frow("Premature stop-outs", ma["premature_stop_outs"], mb["premature_stop_outs"], d["premature_stop_outs"])
        _frow("Net profit (GBP)", ma["net_profit_gbp"], mb["net_profit_gbp"], d["net_profit_gbp"], "{:+.2f}")
        _frow("Net expectancy (GBP/trade)", ma["net_expectancy_gbp"], mb["net_expectancy_gbp"], d["net_expectancy_gbp"], "{:+.4f}")
        _frow("MFE given back (GBP)", ma["mfe_given_back_gbp"], mb["mfe_given_back_gbp"], d["mfe_given_back_gbp"], "{:+.2f}")
        _frow("Profit factor", ma["profit_factor"], mb["profit_factor"])
        _frow("Max drawdown (GBP)", ma["max_drawdown_gbp"], mb["max_drawdown_gbp"], fmt="{:.2f}")
        _frow("Worst trade (GBP)", _comp["arms"]["s105"]["tail"]["worst_trade_gbp"], _comp["arms"][_cand]["tail"]["worst_trade_gbp"], fmt="{:+.2f}")
        _frow("Avg winner (GBP)", ma["avg_winner_gbp"], mb["avg_winner_gbp"], fmt="{:+.4f}")
        _frow("Avg loser (GBP)", ma["avg_loser_gbp"], mb["avg_loser_gbp"], fmt="{:+.4f}")
        for k in range(1, 5):
            _frow("TP{} completion".format(k), ma["tp_completion_rates"]["TP{}".format(k)], mb["tp_completion_rates"]["TP{}".format(k)], fmt="{:.2%}")
        _frow("Cost-adj net (GBP)", ma["cost_adjusted"]["net_profit_gbp"], mb["cost_adjusted"]["net_profit_gbp"], d["cost_adjusted_net_profit_gbp"], "{:+.2f}")
        _frow("Stop-mod failures", ma["stop_modify_failures"], mb["stop_modify_failures"])
        _frow("Errors", ma["error_count"], mb["error_count"])
        lines += [
            "",
            "- Premature trades C: `{}` | floor: `{}`".format(ma["premature_stop_out_trades"] or "none", mb["premature_stop_out_trades"] or "none"),
        ]

    lines += ["", "## Gates Evaluation", ""]
    for k, v in gates.items():
        lines.append(f"- **{k}:** `{v}`")
    lines.append("")
    return "\n".join(lines)


def _build_side_by_side_comparison(
    arms_payloads: Dict[str, Dict[str, Any]],
    registry: Dict[str, Dict[str, Any]],
    left: str,
    right: str,
    base_params: Dict[str, Any],
    overrides_key: str,
    delta_key: str,
    note: str,
) -> Dict[str, Any]:
    """Side-by-side comparison payload for two engine-driven arms (pure reporting).

    Shared by --arm tight (t3 vs t3tight) and --arm scale (s100 vs s105); the
    structure/keys are identical so existing tight reports stay byte-identical.
    """
    def _side_row(r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "sim_gbp": float(r["sim_gbp"]),
            "exit_reason": r.get("exit_reason"),
            "max_stage": int(r.get("max_stage") or 0),
            "peak_mfe_dollars": round(float(r["peak_mfe_dollars"]), 2),
            "realized_usd_per_oz": round(float(r["realized_pts_per_oz"]), 2),
            "time_to_first_protection_min": r.get("time_to_first_protection_min"),
            "first_protect_source": r.get("first_protect_source"),
        }
    _l_rows = {r["msg_id"]: r for r in arms_payloads[left]["results"]}
    _r_rows = {r["msg_id"]: r for r in arms_payloads[right]["results"]}
    _m_l = _tight_comparison_metrics(arms_payloads[left]["results"])
    _m_r = _tight_comparison_metrics(arms_payloads[right]["results"])
    return {
        "experimental": True,
        "note": note,
        "slippage_estimate_dollars_per_side": SLIPPAGE_ESTIMATE_DOLLARS_PER_SIDE,
        "commission_estimate_dollars_per_oz_round_turn": COMMISSION_ESTIMATE_DOLLARS_PER_OZ_ROUND_TURN,
        "arms": {
            m: {
                "label": registry[m]["label"],
                "params": registry[m]["params"],
                overrides_key: {
                    k: v for k, v in registry[m]["params"].items()
                    if base_params.get(k) != v
                },
                "metrics": _m_l if m == left else _m_r,
            }
            for m in (left, right)
        },
        "per_trade": [
            {"msg_id": mid, "dir": _l_rows[mid]["dir"], left: _side_row(_l_rows[mid]),
             right: _side_row(_r_rows[mid])}
            for mid in _l_rows
        ],
        delta_key: {
            "net_profit_gbp": round(_m_r["net_profit_gbp"] - _m_l["net_profit_gbp"], 2),
            "net_expectancy_gbp": round(_m_r["net_expectancy_gbp"] - _m_l["net_expectancy_gbp"], 4),
            "mfe_given_back_gbp": round(_m_r["mfe_given_back_gbp"] - _m_l["mfe_given_back_gbp"], 2),
            "premature_stop_outs": _m_r["premature_stop_outs"] - _m_l["premature_stop_outs"],
            "cost_adjusted_net_profit_gbp": round(
                _m_r["cost_adjusted"]["net_profit_gbp"] - _m_l["cost_adjusted"]["net_profit_gbp"], 2
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="12-Trade XAUUSD.s M1-Bar A/B Replay Proof")
    parser.add_argument(
        "--arm",
        choices=("a", "b", "c", "cx", "t1", "t2", "t3", "t4", "t3tight", "s100", "s105", "l2pp", "l3pp", "wc", "wp2", "wp3", "ifk25", "ifk35", "ifk50", "iff30", "iff40", "both", "trailing", "tight", "scale", "ladder", "wick", "floor"),
        default="both",
        help="'trailing' runs the EXPERIMENTAL Phase 3+4 A/B (A + B + T1..T4) on the pinned tape. "
        "'tight' runs the EXPERIMENTAL T3 vs T3-tight comparison (optionally on a custom tape "
        "via --bars-json + --signals).",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    parser.add_argument("--save-bars", type=Path, default=None)
    parser.add_argument(
        "--bars-json", type=Path, default=PINNED_TAPE_PATH,
        help="Tape JSON to replay (default: canonical pinned fixture). Schema-tagged fixtures are validated on load.",
    )
    parser.add_argument(
        "--live-bars", action="store_true",
        help="Opt INTO live MT5 copy_rates_range fetch (non-reproducible). Cannot combine with an explicit --bars-json.",
    )
    parser.add_argument(
        "--refresh-tape", type=Path, default=None,
        help="Re-fetch live bars to a NEW tape file (never the canonical fixture) and print a diff summary for human review.",
    )
    parser.add_argument("--spread-override", type=float, default=None)
    parser.add_argument(
        "--signals", type=Path, default=None,
        help="Signal-set JSON (catalog) for --arm tight/t3tight, e.g. tests/fixtures/signals_2026-11-23.json. "
        "Default: the canonical 12-trade Nov 17-19 catalog.",
    )
    # Internal worker args
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--arm-mode", choices=("a", "b", "c", "cx", "t1", "t2", "t3", "t4", "t3tight", "s100", "s105", "l2pp", "l3pp", "wc", "wp2", "wp3", "ifk25", "ifk35", "ifk50", "iff30", "iff40"), default="a", help=argparse.SUPPRESS)
    parser.add_argument("--worker-bars", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-catalog", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-protection-telemetry", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-exit-binding", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-wick-gate", choices=("close", "persist2", "persist3"), default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-interim-floor", choices=("k25", "k35", "k50", "f30", "f40"), default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        assert args.worker_bars is not None and args.worker_out is not None
        return _run_worker_main(
            args.arm_mode, args.worker_bars, args.worker_out,
            catalog_path=args.worker_catalog,
            protection_telemetry=args.worker_protection_telemetry,
            exit_binding=args.worker_exit_binding,
            wick_gate=args.worker_wick_gate,
            interim_floor=args.worker_interim_floor,
        )

    if args.refresh_tape is not None:
        return _refresh_tape(args.refresh_tape, args.spread_override)

    if args.signals is not None and args.arm not in ("tight", "t3tight", "scale", "s100", "s105", "ladder", "l2pp", "l3pp", "wick", "wc", "wp2", "wp3", "floor", "ifk25", "ifk35", "ifk50", "iff30", "iff40"):
        parser.error("--signals is only supported with the research/comparison arm modes")

    bars_json_resolved = args.bars_json.resolve() if args.bars_json is not None else None
    if args.live_bars and bars_json_resolved != PINNED_TAPE_PATH.resolve():
        parser.error("--live-bars cannot be combined with an explicit --bars-json")
    use_live = args.live_bars
    bars_source = (
        "live" if use_live
        else ("pinned_fixture" if bars_json_resolved == PINNED_TAPE_PATH.resolve() else "custom_json")
    )

    bars = fetch_m1_bars(
        bars_json_path=None if use_live else args.bars_json,
        save_bars_path=args.save_bars,
        spread_override=args.spread_override,
    )

    with tempfile.TemporaryDirectory(prefix="ab_replay_io_") as io_tmp:
        io_dir = Path(io_tmp)
        bars_file = io_dir / "bars.json"
        bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

        arm_a_res: Optional[Dict[str, Any]] = None
        arm_b_res: Optional[Dict[str, Any]] = None
        arm_c_res: Optional[Dict[str, Any]] = None
        arm_cx_res: Optional[Dict[str, Any]] = None
        arm_t_res: Dict[str, Optional[Dict[str, Any]]] = {m: None for m in TRAILING_EXPERIMENT_ARMS}
        arm_t3tight_res: Optional[Dict[str, Any]] = None
        tight_arms: Dict[str, Dict[str, Any]] = {}
        scale_arms: Dict[str, Dict[str, Any]] = {}
        scale_single_res: Dict[str, Dict[str, Any]] = {}
        ladder_arms: Dict[str, Dict[str, Any]] = {}
        ladder_single_res: Dict[str, Dict[str, Any]] = {}
        wick_arms: Dict[str, Dict[str, Any]] = {}
        wick_single_res: Dict[str, Dict[str, Any]] = {}
        floor_arms: Dict[str, Dict[str, Any]] = {}
        floor_single_res: Dict[str, Dict[str, Any]] = {}

        if args.arm in ("a", "both", "trailing"):
            arm_a_res = _run_arm_a_isolated(bars_file, io_dir / "arm_a.json")
        if args.arm in ("b", "both", "trailing"):
            arm_b_res = _run_arm_subprocess("b", REPO_ROOT, bars_file, io_dir / "arm_b.json")
        if args.arm in ("c", "both", "cx"):
            arm_c_res = _run_arm_subprocess("c", REPO_ROOT, bars_file, io_dir / "arm_c.json")
        if args.arm == "cx":
            # EXPERIMENTAL re-tier comparison; runs Arm C too for before/after.
            arm_cx_res = _run_arm_subprocess("cx", REPO_ROOT, bars_file, io_dir / "arm_cx.json")
        if args.arm == "trailing":
            # EXPERIMENTAL Phase 3+4 trailing A/B; runs A + B too for gates.
            for mode in TRAILING_EXPERIMENT_ARMS:
                arm_t_res[mode] = _run_arm_subprocess(mode, REPO_ROOT, bars_file, io_dir / f"arm_{mode}.json")
        elif args.arm in TRAILING_EXPERIMENT_ARMS:
            arm_t_res[args.arm] = _run_arm_subprocess(args.arm, REPO_ROOT, bars_file, io_dir / f"arm_{args.arm}.json")
        if args.arm == "tight":
            # EXPERIMENTAL T3 vs T3-tight on the identical tape + catalog, with
            # opt-in protection telemetry. Existing arms/modes are untouched.
            for mode in ("t3", "t3tight"):
                tight_arms[mode] = _run_arm_subprocess(
                    mode, REPO_ROOT, bars_file, io_dir / f"arm_{mode}.json",
                    catalog_path=args.signals, protection_telemetry=True,
                )
        elif args.arm == "t3tight":
            arm_t3tight_res = _run_arm_subprocess(
                "t3tight", REPO_ROOT, bars_file, io_dir / "arm_t3tight.json",
                catalog_path=args.signals, protection_telemetry=True,
            )
        if args.arm == "scale":
            # EXPERIMENTAL upgraded-config distance scale 1.00 vs 1.05 on the
            # identical tape + catalog. Existing arms/modes are untouched.
            for mode in ("s100", "s105"):
                scale_arms[mode] = _run_arm_subprocess(
                    mode, REPO_ROOT, bars_file, io_dir / f"arm_{mode}.json",
                    catalog_path=args.signals, protection_telemetry=True,
                )
        elif args.arm in ("s100", "s105"):
            scale_single_res[args.arm] = _run_arm_subprocess(
                args.arm, REPO_ROOT, bars_file, io_dir / f"arm_{args.arm}.json",
                catalog_path=args.signals, protection_telemetry=True,
            )
        if args.arm == "ladder":
            # RESEARCH: current ladder vs -2pp / -3pp clipping-tier candidates
            # on the identical tape + catalog (not adopted; controller decides).
            for mode in ("s105", "l2pp", "l3pp"):
                ladder_arms[mode] = _run_arm_subprocess(
                    mode, REPO_ROOT, bars_file, io_dir / f"arm_{mode}.json",
                    catalog_path=args.signals, protection_telemetry=True, exit_binding=True,
                )
        elif args.arm in ("l2pp", "l3pp"):
            ladder_single_res[args.arm] = _run_arm_subprocess(
                args.arm, REPO_ROOT, bars_file, io_dir / f"arm_{args.arm}.json",
                catalog_path=args.signals, protection_telemetry=True, exit_binding=True,
            )
        if args.arm == "wick":
            # RESEARCH: A (close) / B(N=2) / B(N=3) vs C (s105 immediate baseline).
            for mode in ("s105", "wc", "wp2", "wp3"):
                wick_arms[mode] = _run_arm_subprocess(
                    mode, REPO_ROOT, bars_file, io_dir / f"arm_{mode}.json",
                    catalog_path=args.signals, protection_telemetry=True,
                    wick_gate=WICK_RESEARCH_ARMS[mode]["gate"] if mode in WICK_RESEARCH_ARMS else None,
                )
        elif args.arm in ("wc", "wp2", "wp3"):
            wick_single_res[args.arm] = _run_arm_subprocess(
                args.arm, REPO_ROOT, bars_file, io_dir / f"arm_{args.arm}.json",
                catalog_path=args.signals, protection_telemetry=True,
                wick_gate=WICK_RESEARCH_ARMS[args.arm]["gate"],
            )
        if args.arm == "floor":
            # RESEARCH: interim-floor strength sweep vs s105 baseline (immediate
            # ladder unchanged; floor is an extra tightest-stop candidate).
            for mode in ("s105",) + tuple(FLOOR_RESEARCH_ARMS):
                floor_arms[mode] = _run_arm_subprocess(
                    mode, REPO_ROOT, bars_file, io_dir / f"arm_{mode}.json",
                    catalog_path=args.signals, protection_telemetry=True,
                    interim_floor=FLOOR_RESEARCH_ARMS[mode]["floor"] if mode in FLOOR_RESEARCH_ARMS else None,
                )
        elif args.arm in FLOOR_RESEARCH_ARMS:
            floor_single_res[args.arm] = _run_arm_subprocess(
                args.arm, REPO_ROOT, bars_file, io_dir / f"arm_{args.arm}.json",
                catalog_path=args.signals, protection_telemetry=True,
                interim_floor=FLOOR_RESEARCH_ARMS[args.arm]["floor"],
            )

    if args.arm == "tight":
        _err_t3 = int(tight_arms["t3"]["summary"]["error_count"])
        _err_tt = int(tight_arms["t3tight"]["summary"]["error_count"])
        gates = {
            "mode": "tight_comparison",
            "t3_error_count": _err_t3,
            "t3tight_error_count": _err_tt,
            "all_gates_passed": bool(_err_t3 == 0 and _err_tt == 0),
        }
    elif args.arm == "scale":
        _err_s100 = int(scale_arms["s100"]["summary"]["error_count"])
        _err_s105 = int(scale_arms["s105"]["summary"]["error_count"])
        gates = {
            "mode": "scale_comparison",
            "s100_error_count": _err_s100,
            "s105_error_count": _err_s105,
            "all_gates_passed": bool(_err_s100 == 0 and _err_s105 == 0),
        }
    elif args.arm == "ladder":
        _errs = {m: int(ladder_arms[m]["summary"]["error_count"]) for m in ("s105", "l2pp", "l3pp")}
        gates = {
            "mode": "ladder_research",
            "error_counts": _errs,
            "all_gates_passed": all(v == 0 for v in _errs.values()),
        }
    elif args.arm == "wick":
        _errs = {m: int(wick_arms[m]["summary"]["error_count"]) for m in ("s105", "wc", "wp2", "wp3")}
        gates = {
            "mode": "wick_research",
            "error_counts": _errs,
            "all_gates_passed": all(v == 0 for v in _errs.values()),
        }
    elif args.arm == "floor":
        _errs = {m: int(floor_arms[m]["summary"]["error_count"]) for m in floor_arms}
        gates = {
            "mode": "floor_research",
            "error_counts": _errs,
            "all_gates_passed": all(v == 0 for v in _errs.values()),
        }
    else:
        gates = _evaluate_gates(
            arm_a_res, arm_b_res, arm_c_res,
            arm_other=arm_t3tight_res
            or next((scale_single_res[m] for m in ("s100", "s105") if scale_single_res.get(m)), None)
            or next((ladder_single_res[m] for m in ("l2pp", "l3pp") if ladder_single_res.get(m)), None)
            or next((wick_single_res[m] for m in ("wc", "wp2", "wp3") if wick_single_res.get(m)), None)
            or next((floor_single_res[m] for m in FLOOR_RESEARCH_ARMS if floor_single_res.get(m)), None)
            or next((arm_t_res[m] for m in TRAILING_EXPERIMENT_ARMS if arm_t_res[m]), None),
        )

    # EXPERIMENTAL Phase 3+4 trailing A/B evidence (replay arm-param overrides
    # only; production defaults untouched). Present only in --arm trailing mode.
    trailing_experiments: Optional[Dict[str, Any]] = None
    trailing_gates: Optional[Dict[str, Any]] = None
    if args.arm == "trailing" and arm_a_res and arm_b_res:
        variants = {m: arm_t_res[m] for m in TRAILING_EXPERIMENT_ARMS if arm_t_res[m]}
        trailing_experiments = {
            "experimental": True,
            "note": (
                "EXPERIMENTAL trailing A/B arm-param overrides on the identical pinned tape "
                "(same bars, spread, GBP rate 0.7589, offset 7200, execution rules). "
                "NOT production, NOT shipped; promotion requires explicit approval."
            ),
            "baseline_arm": "b",
            "baseline_metrics": _trailing_experiment_metrics(arm_b_res["results"]),
            "arms": {
                m: {
                    "label": TRAILING_EXPERIMENT_ARMS[m]["label"],
                    "overrides_vs_baseline_b": {
                        k: v for k, v in TRAILING_EXPERIMENT_ARMS[m]["params"].items()
                        if ARM_B_CALIBRATED_PARAMS.get(k) != v
                    },
                    "metrics": _trailing_experiment_metrics(payload["results"]),
                }
                for m, payload in variants.items()
            },
        }
        trailing_gates = _evaluate_trailing_gates(arm_a_res, arm_b_res, variants)
    tight_comparison: Optional[Dict[str, Any]] = None
    if args.arm == "tight":
        tight_comparison = _build_side_by_side_comparison(
            tight_arms, TIGHT_COMPARISON_ARMS, "t3", "t3tight",
            ARM_T3_EXPERIMENTAL_PARAMS, "overrides_vs_t3", "delta_tight_minus_t3",
            note=(
                "EXPERIMENTAL T3 vs T3-tight comparison driving the PRODUCTION engine "
                "(dynamic_risk.compute_dynamic_atr_trail_stop via mt5_executor.evaluate_dynamic_atr_engine) "
                "on the identical tape + signal set. T3-tight = T3 plus the six opt-in 5ac5fe2 knobs. "
                "Replay arm-param overrides only; not production, not shipped."
            ),
        )
    scale_comparison: Optional[Dict[str, Any]] = None
    if args.arm == "scale":
        scale_comparison = _build_side_by_side_comparison(
            scale_arms, SCALE_COMPARISON_ARMS, "s100", "s105",
            ARM_T3TIGHT_SCALE100_EXPERIMENTAL_PARAMS, "overrides_vs_s100", "delta_s105_minus_s100",
            note=(
                "EXPERIMENTAL upgraded-config trail-distance scale 1.00 vs 1.05 comparison driving the "
                "PRODUCTION engine (dynamic_risk.compute_dynamic_atr_trail_stop via "
                "mt5_executor.evaluate_dynamic_atr_engine) on the identical tape + signal set. "
                "Only delta: DYNAMIC_ATR_TRAIL_DISTANCE_SCALE_MULT (09aae20), applied AFTER the zone "
                "clamp + distance-tier cap (trail distance only; MFE ladder / activation / auto-BE / "
                "initial SL unchanged). Replay arm-param overrides only; not production, not shipped."
            ),
        )
    ladder_minus2pp_comparison: Optional[Dict[str, Any]] = None
    ladder_minus3pp_comparison: Optional[Dict[str, Any]] = None
    binding_evidence: Optional[Dict[str, Any]] = None
    if args.arm == "ladder":
        _ladder_note = (
            "RESEARCH binding-ladder backtest driving the PRODUCTION engine on the identical tape + "
            "signal set. Current = upgraded ATR-relative config + 1.05 trail scale. Candidates reduce "
            "ONLY the clipping tiers' protected-% (0.40/0.60/1.00, identified by exit binding telemetry) "
            "by 2 / 3 percentage points; activation ratios and all other knobs unchanged. NOT adopted; "
            "the controller decides from this evidence."
        )
        ladder_minus2pp_comparison = _build_side_by_side_comparison(
            ladder_arms, LADDER_RESEARCH_ARMS, "s105", "l2pp",
            ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "overrides_vs_current", "delta_l2pp_minus_current",
            note=_ladder_note,
        )
        ladder_minus3pp_comparison = _build_side_by_side_comparison(
            ladder_arms, LADDER_RESEARCH_ARMS, "s105", "l3pp",
            ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "overrides_vs_current", "delta_l3pp_minus_current",
            note=_ladder_note,
        )
        # Tail / worst-trade evidence per arm (research metric addition).
        for _comp in (ladder_minus2pp_comparison, ladder_minus3pp_comparison):
            for _m in _comp["arms"]:
                _rows = sorted(ladder_arms[_m]["results"], key=lambda r: float(r["sim_gbp"]))
                _comp["arms"][_m]["tail"] = {
                    "worst_trade_gbp": round(float(_rows[0]["sim_gbp"]), 2) if _rows else 0.0,
                    "worst_trade_msg": _rows[0]["msg_id"] if _rows else None,
                    "worst_3_trades_gbp": round(sum(float(r["sim_gbp"]) for r in _rows[:3]), 2),
                }
        binding_evidence = {
            m: [
                {"msg_id": r["msg_id"], "exit_reason": r.get("exit_reason"),
                 "peak_mfe_dollars": r.get("peak_mfe_dollars"),
                 "realized_pts_per_oz": r.get("realized_pts_per_oz"),
                 "exit_binding": r.get("exit_binding") or []}
                for r in ladder_arms[m]["results"]
            ]
            for m in ("s105", "l2pp", "l3pp")
        }
    wick_close_comparison: Optional[Dict[str, Any]] = None
    wick_persist2_comparison: Optional[Dict[str, Any]] = None
    wick_persist3_comparison: Optional[Dict[str, Any]] = None
    if args.arm == "wick":
        _wick_note = (
            "RESEARCH wick-aware activation backtest driving the PRODUCTION engine (unchanged) with a "
            "scripts-only gate: A = candle-close confirmation, B = N-consecutive-close persistence "
            "(N=2 / N=3), C = s105 immediate baseline. Params identical across arms (upgraded ladder + "
            "1.05 trail scale); confirmation uses close-based MFE/ATR while peak/trail MFE keeps tracking "
            "wicks. NOT adopted; the controller waits for user approval."
        )
        wick_close_comparison = _build_side_by_side_comparison(
            wick_arms, {**LADDER_RESEARCH_ARMS, **WICK_RESEARCH_ARMS}, "s105", "wc",
            ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "overrides_vs_current", "delta_wc_minus_current",
            note=_wick_note,
        )
        wick_persist2_comparison = _build_side_by_side_comparison(
            wick_arms, {**LADDER_RESEARCH_ARMS, **WICK_RESEARCH_ARMS}, "s105", "wp2",
            ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "overrides_vs_current", "delta_wp2_minus_current",
            note=_wick_note,
        )
        wick_persist3_comparison = _build_side_by_side_comparison(
            wick_arms, {**LADDER_RESEARCH_ARMS, **WICK_RESEARCH_ARMS}, "s105", "wp3",
            ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "overrides_vs_current", "delta_wp3_minus_current",
            note=_wick_note,
        )
        for _comp, _cand in ((wick_close_comparison, "wc"), (wick_persist2_comparison, "wp2"), (wick_persist3_comparison, "wp3")):
            for _m in _comp["arms"]:
                _rows = sorted(wick_arms[_m]["results"], key=lambda r: float(r["sim_gbp"]))
                _comp["arms"][_m]["tail"] = {
                    "worst_trade_gbp": round(float(_rows[0]["sim_gbp"]), 2) if _rows else 0.0,
                    "worst_trade_msg": _rows[0]["msg_id"] if _rows else None,
                    "worst_3_trades_gbp": round(sum(float(r["sim_gbp"]) for r in _rows[:3]), 2),
                }
                _comp["arms"][_m]["wick_gate"] = WICK_RESEARCH_ARMS[_m]["gate"] if _m in WICK_RESEARCH_ARMS else None
    floor_comparisons: Dict[str, Dict[str, Any]] = {}
    if args.arm == "floor":
        _floor_note = (
            "RESEARCH interim-floor strength sweep driving the PRODUCTION engine (unchanged, immediate MFE "
            "ladder) with a scripts-only EXTRA tightest-stop candidate: entry + sign*k*ATR (armed at "
            "MFE >= k*ATR) or entry + sign*f*MFE (armed at MFE/ATR >= 0.20). The floor binds ONLY when "
            "tighter than every production candidate; forward-only never-loosen preserved; 2% relative "
            "ATR + 1.05 trail scale unchanged. NOT adopted; controller waits for user approval."
        )
        _floor_registry = {**LADDER_RESEARCH_ARMS, **FLOOR_RESEARCH_ARMS}
        for _cand in FLOOR_RESEARCH_ARMS:
            _comp = _build_side_by_side_comparison(
                floor_arms, _floor_registry, "s105", _cand,
                ARM_T3TIGHT_SCALE105_EXPERIMENTAL_PARAMS, "overrides_vs_current",
                "delta_" + _cand + "_minus_current", note=_floor_note,
            )
            for _m in _comp["arms"]:
                _rows = sorted(floor_arms[_m]["results"], key=lambda r: float(r["sim_gbp"]))
                _comp["arms"][_m]["tail"] = {
                    "worst_trade_gbp": round(float(_rows[0]["sim_gbp"]), 2) if _rows else 0.0,
                    "worst_trade_msg": _rows[0]["msg_id"] if _rows else None,
                    "worst_3_trades_gbp": round(sum(float(r["sim_gbp"]) for r in _rows[:3]), 2),
                }
                _comp["arms"][_m]["interim_floor"] = FLOOR_RESEARCH_ARMS[_m]["floor"] if _m in FLOOR_RESEARCH_ARMS else None
            floor_comparisons[_cand] = _comp
    signals_meta = None
    if args.signals is not None:
        _sp = json.loads(args.signals.read_text(encoding="utf-8"))
        _tr = _sp["trades"] if isinstance(_sp, dict) else _sp
        signals_meta = {k: v for k, v in _sp.items() if k != "trades"} if isinstance(_sp, dict) else {}
        signals_meta["trade_count"] = len(_tr)
        signals_meta["catalog_sha256"] = hashlib.sha256(
            json.dumps(_tr, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    fixture_meta = None
    if not use_live and args.bars_json is not None:
        _fb, fixture_payload, _fp = load_tape(args.bars_json)
        if _fp:
            fixture_meta = {k: v for k, v in fixture_payload.items() if k != "bars"}
    report: Dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": SYMBOL,
        "gbp_rate_per_pt_001_lot": GBP_PER_PT_001_LOT,
        "bars_meta": {
            "count": len(bars),
            "first_utc": _utc_iso(bars[0]["time_utc"]) if bars else None,
            "last_utc": _utc_iso(bars[-1]["time_utc"]) if bars else None,
            "spread_override": args.spread_override,
            "source": bars_source,
            "path": None if use_live else str(args.bars_json),
            "replay_bars_sha256": _bars_sha256(bars),
        },
        "fixture_meta": fixture_meta,
        "arm_a": arm_a_res,
        "arm_b": arm_b_res,
        "arm_c": arm_c_res,
        "arm_cx": arm_cx_res,
        "arm_t1": arm_t_res["t1"],
        "arm_t2": arm_t_res["t2"],
        "arm_t3": arm_t_res["t3"] or tight_arms.get("t3"),
        "arm_t4": arm_t_res["t4"],
        "arm_t3tight": arm_t3tight_res or tight_arms.get("t3tight"),
        "arm_s100": scale_arms.get("s100") or scale_single_res.get("s100"),
        "arm_s105": scale_arms.get("s105") or scale_single_res.get("s105") or ladder_arms.get("s105") or wick_arms.get("s105") or floor_arms.get("s105"),
        "arm_l2pp": ladder_arms.get("l2pp") or ladder_single_res.get("l2pp"),
        "arm_l3pp": ladder_arms.get("l3pp") or ladder_single_res.get("l3pp"),
        "ladder_minus2pp_comparison": ladder_minus2pp_comparison,
        "ladder_minus3pp_comparison": ladder_minus3pp_comparison,
        "binding_evidence": binding_evidence,
        "arm_wc": wick_arms.get("wc") or wick_single_res.get("wc"),
        "arm_wp2": wick_arms.get("wp2") or wick_single_res.get("wp2"),
        "arm_wp3": wick_arms.get("wp3") or wick_single_res.get("wp3"),
        "wick_close_comparison": wick_close_comparison,
        "wick_persist2_comparison": wick_persist2_comparison,
        "wick_persist3_comparison": wick_persist3_comparison,
        "arm_ifk25": floor_arms.get("ifk25") or floor_single_res.get("ifk25"),
        "arm_ifk35": floor_arms.get("ifk35") or floor_single_res.get("ifk35"),
        "arm_ifk50": floor_arms.get("ifk50") or floor_single_res.get("ifk50"),
        "arm_iff30": floor_arms.get("iff30") or floor_single_res.get("iff30"),
        "arm_iff40": floor_arms.get("iff40") or floor_single_res.get("iff40"),
        "floor_comparisons": floor_comparisons or None,
        "tight_comparison": tight_comparison,
        "scale_comparison": scale_comparison,
        "signals_meta": signals_meta,
        "trailing_experiments": trailing_experiments,
        "trailing_gates": trailing_gates,
        "gates": gates,
    }
    md_text = _render_markdown(report)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.md_out is not None:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(md_text, encoding="utf-8")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(md_text)
    return 0 if gates.get("all_gates_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
