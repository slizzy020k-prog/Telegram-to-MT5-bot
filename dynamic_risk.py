"""Volatility-scaled StopLoss/TakeProfit and optional dynamic ATR trailing stop."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

VALID_ATR_TIMEFRAMES: Tuple[str, ...] = ("M1", "M5", "M15", "H1")
VALID_DYNAMIC_SLTP_MODES: Tuple[str, ...] = ("ADAPTIVE", "FALLBACK_ONLY", "PURE_ATR")

_ON_TOKENS = frozenset({"on", "true", "1", "enable", "enabled"})
_OFF_TOKENS = frozenset({"off", "false", "0", "disable", "disabled", "none"})
_MODE_ALIASES: Dict[str, str] = {
    "adaptive": "ADAPTIVE",
    "scale": "ADAPTIVE",
    "fallback": "FALLBACK_ONLY",
    "fallback_only": "FALLBACK_ONLY",
    "pure": "PURE_ATR",
    "pure_atr": "PURE_ATR",
}

_DYNAMIC_RISK_USAGE = (
    "⚠️ **Invalid syntax.** Usage: `/dynamicrisk <on|off|adaptive|fallback|pure|sl_mult tp_mult [M1|M5|M15|H1]>`\n"
    "Examples: `/dynamicrisk 2.5 1.5 M5`, `/dynamicrisk pure`, or `/dynamicrisk off`"
)
_ATR_TRAIL_USAGE = (
    "⚠️ **Invalid syntax.** Usage: `/atrtrail <on|off|tp on|tp off|mult [activation_mult] [M1|M5|M15|H1]>`\n"
    "Examples: `/atrtrail on`, `/atrtrail tp on`, `/atrtrail 1.8 1.0 M5`, or `/atrtrail off`"
)


@dataclass(frozen=True)
class DynamicRiskPolicy:
    """Immutable snapshot of volatility-scaled SL/TP and Primary Dynamic ATR Engine settings."""

    sltp_enabled: bool = True
    sltp_mode: str = "ADAPTIVE"
    atr_timeframe: str = "M5"
    atr_period: int = 14
    atr_baseline_dollars: float = 4.00
    sl_atr_mult: float = 2.50
    tp_atr_mult: float = 1.50
    tp_step_atr_mult: float = 1.50
    sl_min_dollars: float = 4.50
    sl_max_dollars: float = 35.00
    tp_min_dollars: float = 2.50
    tp_max_dollars: float = 50.00
    scale_min: float = 0.75
    scale_max: float = 1.75
    trail_enabled: bool = True
    trail_timeframe: str = "M5"
    trail_atr_mult: float = 1.40
    trail_extended_mfe_dollars: float = 10.00
    trail_extended_atr_mult: float = 1.00
    trail_homerun_mfe_dollars: float = 18.00
    trail_homerun_atr_mult: float = 0.80
    trail_activation_atr_mult: float = 1.00
    trail_min_dollars: float = 2.50
    trail_max_dollars: float = 12.00
    trail_step_dollars: float = 0.25
    be_activation_atr_mult: float = 0.75
    be_min_mfe_dollars: float = 3.50
    be_lock_offset_dollars: float = 0.50
    tp_trail_enabled: bool = True
    tp_lift_atr_mult: float = 0.50
    tp_lift_progress_pct: float = 0.75
    tp1_lift_min_progress_pct: float = 0.50


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _clamp(val: float, low: float, high: float) -> float:
    lo, hi = (low, high) if low <= high else (high, low)
    return max(lo, min(hi, val))


def _norm_choice(val: Any, default: str, choices: Tuple[str, ...]) -> str:
    txt = str(val or default).strip().upper()
    return txt if txt in choices else default


def read_dynamic_risk_policy(cfg_mod: Any) -> DynamicRiskPolicy:
    """Read dynamic ATR SL/TP and Primary Dynamic ATR Engine settings from config.

    The progressive trail ladder is normalised rather than rejected: if a user configures an
    inverted pair (EXTENDED_MFE > HOMERUN_MFE) the two thresholds are reordered so that
    trail_extended_mfe_dollars <= trail_homerun_mfe_dollars always holds. This mirrors
    config.validate_dynamic_risk_pairs(), which swaps the same pair at import time, while
    startup_checks._validate_dynamic_risk_config treats any residual inversion as a fatal
    startup error -- so an inverted ladder never reaches the engine.
    """
    sl_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_SL_MIN_DOLLARS", 4.50), 4.50))
    sl_hi = max(sl_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_SL_MAX_DOLLARS", 35.00), 35.00))
    tp_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_TP_MIN_DOLLARS", 2.50), 2.50))
    tp_hi = max(tp_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_TP_MAX_DOLLARS", 50.00), 50.00))
    sc_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_SLTP_SCALE_MIN", 0.75), 0.75))
    sc_hi = max(sc_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_SLTP_SCALE_MAX", 1.75), 1.75))
    tr_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50), 2.50))
    tr_hi = max(tr_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00), 12.00))
    raw_per = _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_PERIOD", 14), 14.0)
    ext_mfe_raw = max(0.50, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", 10.00), 10.00))
    home_mfe_raw = max(0.50, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS", 18.00), 18.00))
    ext_mfe = min(ext_mfe_raw, home_mfe_raw)
    home_mfe = max(ext_mfe_raw, home_mfe_raw)
    ext_mult = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_EXTENDED_MULT", 1.00), 1.00))
    home_mult = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_HOMERUN_MULT", 0.80), 0.80))
    tp1_prog = _clamp(_safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT", 0.50), 0.50), 0.05, 0.99)
    return DynamicRiskPolicy(
        sltp_enabled=bool(getattr(cfg_mod, "DYNAMIC_ATR_SLTP_ENABLED", True)),
        sltp_mode=_norm_choice(getattr(cfg_mod, "DYNAMIC_SLTP_MODE", "ADAPTIVE"), "ADAPTIVE", VALID_DYNAMIC_SLTP_MODES),
        atr_timeframe=_norm_choice(getattr(cfg_mod, "DYNAMIC_ATR_TIMEFRAME", "M5"), "M5", VALID_ATR_TIMEFRAMES),
        atr_period=max(2, min(200, int(raw_per))),
        atr_baseline_dollars=max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_BASELINE_DOLLARS", 4.00), 4.00)),
        sl_atr_mult=max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_SL_ATR_MULT", 2.50), 2.50)),
        tp_atr_mult=max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_TP_ATR_MULT", 1.50), 1.50)),
        tp_step_atr_mult=max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_TP_STEP_ATR_MULT", 1.50), 1.50)),
        sl_min_dollars=sl_lo,
        sl_max_dollars=sl_hi,
        tp_min_dollars=tp_lo,
        tp_max_dollars=tp_hi,
        scale_min=sc_lo,
        scale_max=sc_hi,
        trail_enabled=bool(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", True)),
        trail_timeframe=_norm_choice(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_TIMEFRAME", "M5"), "M5", VALID_ATR_TIMEFRAMES),
        trail_atr_mult=max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MULT", 1.40), 1.40)),
        trail_extended_mfe_dollars=ext_mfe,
        trail_extended_atr_mult=ext_mult,
        trail_homerun_mfe_dollars=home_mfe,
        trail_homerun_atr_mult=home_mult,
        trail_activation_atr_mult=max(0.0, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00), 1.00)),
        trail_min_dollars=tr_lo,
        trail_max_dollars=tr_hi,
        trail_step_dollars=max(0.05, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25), 0.25)),
        be_activation_atr_mult=max(0.0, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_BE_ACTIVATION_MULT", 0.75), 0.75)),
        be_min_mfe_dollars=max(0.0, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS", 3.50), 3.50)),
        be_lock_offset_dollars=max(0.0, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 0.50), 0.50)),
        tp_trail_enabled=bool(getattr(cfg_mod, "DYNAMIC_ATR_TP_TRAIL_ENABLED", True)),
        tp_lift_atr_mult=max(0.05, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TP_LIFT_ATR_MULT", 0.50), 0.50)),
        tp_lift_progress_pct=_clamp(_safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT", 0.75), 0.75), 0.10, 0.99),
        tp1_lift_min_progress_pct=tp1_prog,
    )


def compute_volatility_ratio(atr: float, policy: DynamicRiskPolicy) -> float:
    """Return clamped ATR / baseline scaling ratio (1.0 when ATR or baseline is invalid)."""
    atr_f = _safe_float(atr, 0.0)
    if atr_f <= 0.0 or policy.atr_baseline_dollars <= 0.0:
        return 1.0
    return round(_clamp(atr_f / policy.atr_baseline_dollars, policy.scale_min, policy.scale_max), 4)


def compute_atr_fallback_sl_distance(atr: float, policy: DynamicRiskPolicy) -> Optional[float]:
    """Return clamped ATR fallback SL distance in price dollars, or None if unavailable."""
    atr_f = _safe_float(atr, 0.0)
    if not policy.sltp_enabled or atr_f <= 0.0:
        return None
    return round(_clamp(atr_f * policy.sl_atr_mult, policy.sl_min_dollars, policy.sl_max_dollars), 4)


def compute_atr_fallback_tp_distance(atr: float, policy: DynamicRiskPolicy) -> Optional[float]:
    """Return clamped ATR fallback TP1 distance in price dollars, or None if unavailable."""
    atr_f = _safe_float(atr, 0.0)
    if not policy.sltp_enabled or atr_f <= 0.0:
        return None
    return round(_clamp(atr_f * policy.tp_atr_mult, policy.tp_min_dollars, policy.tp_max_dollars), 4)


def _extract_tp_map(raw_tp1: Optional[float], tp_levels: Optional[Dict[str, float]], digits: int) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if isinstance(tp_levels, dict):
        for idx in range(1, 6):
            key = f"TP{idx}"
            val = tp_levels.get(key)
            if val is not None and _safe_float(val, 0.0) > 0.0:
                out[key] = round(float(val), digits)
    if "TP1" not in out and raw_tp1 is not None and _safe_float(raw_tp1, 0.0) > 0.0:
        out["TP1"] = round(float(raw_tp1), digits)
    return out


def _scale_sl_price(
    sign: float, sl_anchor: float, sl: Optional[float], vol_ratio: float, atr_sl_dist: float, policy: DynamicRiskPolicy, digits: int
) -> Optional[float]:
    if sl is None or _safe_float(sl, 0.0) <= 0.0 or policy.sltp_mode == "PURE_ATR":
        return round(sl_anchor - sign * atr_sl_dist, digits)
    sl_f = round(float(sl), digits)
    if policy.sltp_mode != "ADAPTIVE":
        return sl_f
    orig_dist = sign * (sl_anchor - sl_f)
    if orig_dist <= 0.0:
        return round(sl_anchor - sign * atr_sl_dist, digits)
    scaled_dist = _clamp(orig_dist * vol_ratio, policy.sl_min_dollars, policy.sl_max_dollars)
    return round(sl_anchor - sign * scaled_dist, digits)


def _scale_tp_map(
    sign: float, tp_anchor: float, orig_map: Dict[str, float], vol_ratio: float, atr_tp1_dist: float, atr_step_dist: float, policy: DynamicRiskPolicy, digits: int, compute_missing_tp: bool
) -> Dict[str, float]:
    min_tick = 10.0 ** (-max(0, min(6, digits)))
    if policy.sltp_mode == "PURE_ATR" or (not orig_map and compute_missing_tp):
        built: Dict[str, float] = {"TP1": round(tp_anchor + sign * atr_tp1_dist, digits)}
        step = max(min_tick, atr_step_dist)
        for idx in range(2, 6):
            prev_p = built[f"TP{idx - 1}"]
            cand = round(prev_p + sign * step, digits)
            if sign * (cand - prev_p) < min_tick - 1e-12:
                cand = round(prev_p + sign * min_tick, digits)
            built[f"TP{idx}"] = cand
        return built
    if not orig_map or policy.sltp_mode != "ADAPTIVE":
        return dict(orig_map)
    orig_tp1 = orig_map.get("TP1")
    if orig_tp1 is None:
        return dict(orig_map)
    orig_tp1_dist = sign * (orig_tp1 - tp_anchor)
    if orig_tp1_dist <= 0.0:
        return dict(orig_map)
    scaled_tp1_dist = _clamp(orig_tp1_dist * vol_ratio, policy.tp_min_dollars, policy.tp_max_dollars)
    scaled: Dict[str, float] = {"TP1": round(tp_anchor + sign * scaled_tp1_dist, digits)}
    prev_orig, prev_new = orig_tp1, scaled["TP1"]
    for idx in range(2, 6):
        key = f"TP{idx}"
        if key not in orig_map:
            continue
        cur_orig = orig_map[key]
        delta = max(min_tick, sign * (cur_orig - prev_orig) * vol_ratio)
        cand = round(prev_new + sign * delta, digits)
        if sign * (cand - prev_new) < min_tick - 1e-12:
            cand = round(prev_new + sign * min_tick, digits)
        scaled[key] = cand
        prev_orig, prev_new = cur_orig, cand
    return scaled


def scale_sl_tp_by_atr(
    direction: str,
    sl_anchor: float,
    tp_anchor: float,
    atr: float,
    policy: DynamicRiskPolicy,
    digits: int = 2,
    sl: Optional[float] = None,
    raw_tp1: Optional[float] = None,
    tp_levels: Optional[Dict[str, float]] = None,
    final_tp: Optional[float] = None,
    front_run_offset: float = 0.50,
    compute_missing_tp: bool = False,
) -> Dict[str, Any]:
    """Scale or compute SL and TP1..TP5 from live ATR according to DynamicRiskPolicy."""
    orig_map = _extract_tp_map(raw_tp1, tp_levels, digits)
    atr_f = _safe_float(atr, 0.0)
    if not policy.sltp_enabled or atr_f <= 0.0 or sl_anchor <= 0.0 or tp_anchor <= 0.0:
        return {"applied": False, "mode": policy.sltp_mode, "timeframe": policy.atr_timeframe, "atr": round(atr_f, 4), "vol_ratio": 1.0, "sl": sl, "raw_tp1": raw_tp1, "tp_levels": orig_map, "final_tp": final_tp, "tp_step_dollars": None}
    sign = 1.0 if (direction or "BUY").strip().upper() == "BUY" else -1.0
    vol_ratio = compute_volatility_ratio(atr_f, policy)
    atr_sl_dist = _clamp(atr_f * policy.sl_atr_mult, policy.sl_min_dollars, policy.sl_max_dollars)
    atr_tp1_dist = _clamp(atr_f * policy.tp_atr_mult, policy.tp_min_dollars, policy.tp_max_dollars)
    atr_step_dist = _clamp(atr_f * policy.tp_step_atr_mult, policy.tp_min_dollars, policy.tp_max_dollars)
    new_sl = _scale_sl_price(sign, sl_anchor, sl, vol_ratio, atr_sl_dist, policy, digits)
    new_tp_map = _scale_tp_map(sign, tp_anchor, orig_map, vol_ratio, atr_tp1_dist, atr_step_dist, policy, digits, compute_missing_tp)
    new_tp1 = new_tp_map.get("TP1")
    new_final_tp = final_tp
    if new_tp1 is not None:
        if policy.sltp_mode == "FALLBACK_ONLY" and orig_map.get("TP1") is not None and final_tp is not None:
            new_final_tp = round(float(final_tp), digits)
        else:
            tp1_dist = abs(new_tp1 - tp_anchor)
            fr = max(0.0, float(front_run_offset))
            new_final_tp = round(new_tp1 - sign * fr, digits) if tp1_dist > fr else new_tp1
    return {"applied": True, "mode": policy.sltp_mode, "timeframe": policy.atr_timeframe, "atr": round(atr_f, 4), "vol_ratio": vol_ratio, "sl": new_sl, "raw_tp1": new_tp1, "tp_levels": new_tp_map, "final_tp": new_final_tp, "tp_step_dollars": round(atr_step_dist, digits)}


def _normalize_milestones(cleared_milestones: Optional[Sequence[str]]) -> Tuple[List[str], Set[str]]:
    cleared: List[str] = []
    seen: Set[str] = set()
    for item in (cleared_milestones or ()):
        tok = str(item or "").strip().upper()
        if tok and tok not in seen:
            seen.add(tok)
            cleared.append(tok)
    return cleared, seen


def _advance_milestones(
    sign: float,
    live_price: float,
    tp_map: Dict[str, float],
    max_stage: int,
    cleared: List[str],
    seen: Set[str],
) -> List[str]:
    newly_cleared: List[str] = []
    for m in range(1, max_stage):
        m_key = f"TP{m}"
        m_tp = tp_map.get(m_key)
        if m_tp is None or m_tp <= 0.0:
            break
        if m_key in seen:
            continue
        if sign * (live_price - m_tp) >= -1e-9:
            seen.add(m_key)
            cleared.append(m_key)
            newly_cleared.append(m_key)
        else:
            break
    return newly_cleared


def _parse_highest_cleared_stage(cleared_milestones: Optional[Sequence[str]]) -> int:
    highest = 0
    for item in (cleared_milestones or ()):
        tok = str(item or "").strip().upper()
        if tok.startswith("TP") and len(tok) == 3 and tok[2].isdigit():
            stage = int(tok[2])
            if 1 <= stage <= 5:
                highest = max(highest, stage)
    return highest


def _compute_milestone_floor(
    sign: float,
    entry_price: float,
    cleared_milestones: Optional[Sequence[str]],
    tp_levels: Optional[Dict[str, float]],
    lock_offset: float,
    digits: int,
) -> Optional[float]:
    highest = _parse_highest_cleared_stage(cleared_milestones)
    if highest <= 0:
        return None
    be_floor = round(entry_price + sign * max(0.0, lock_offset), digits)
    if highest == 1:
        return be_floor
    if isinstance(tp_levels, dict):
        prev_tp = _safe_float(tp_levels.get(f"TP{highest - 1}"), 0.0)
        if prev_tp > 0.0:
            prev_r = round(prev_tp, digits)
            return max(prev_r, be_floor) if sign > 0 else min(prev_r, be_floor)
    return be_floor


def compute_dynamic_tp_step(
    direction: str,
    entry_price: float,
    live_price: float,
    peak_price: Optional[float],
    atr: float,
    tp_levels: Optional[Dict[str, float]],
    current_stage_idx: int,
    max_stage: int,
    cleared_milestones: Optional[Sequence[str]],
    policy: DynamicRiskPolicy,
    digits: int = 2,
    stops_buffer: float = 0.0,
    be_locked: bool = False,
) -> Dict[str, Any]:
    """Compute momentum-gated Dynamic Trailing TP step (`TP1 -> TP2 -> ... -> max_stage`)."""
    sign = 1.0 if (direction or "BUY").strip().upper() == "BUY" else -1.0
    orig_stage_idx = max(1, min(5, int(current_stage_idx or 1)))
    eff_max_stage = max(1, min(5, int(max_stage or 1)))
    tp_map = _extract_tp_map(None, tp_levels, digits)
    cleared, seen = _normalize_milestones(cleared_milestones)
    if entry_price <= 0.0 or live_price <= 0.0 or not tp_map:
        return {
            "should_lift": False,
            "target_stage_idx": orig_stage_idx,
            "target_stage_name": f"TP{orig_stage_idx}",
            "target_tp": tp_map.get(f"TP{orig_stage_idx}"),
            "cleared_milestones": cleared,
            "newly_cleared": [],
            "momentum_ok": False,
        }

    newly_cleared = _advance_milestones(sign, live_price, tp_map, eff_max_stage, cleared, seen)
    atr_f = max(0.0, _safe_float(atr, 0.0))
    peak_in = _safe_float(peak_price, 0.0)
    eff_peak = (max(peak_in, live_price) if sign > 0 else min(peak_in, live_price)) if peak_in > 0.0 else live_price
    pullback = max(0.0, sign * (eff_peak - live_price))
    momentum_ok = (pullback <= (0.25 * atr_f) + 1e-9) if atr_f > 0.0 else False

    buf = max(0.0, _safe_float(stops_buffer, 0.0))
    k = orig_stage_idx
    target_stage_idx = orig_stage_idx
    target_tp = tp_map.get(f"TP{orig_stage_idx}")

    while k < eff_max_stage and policy.tp_trail_enabled and atr_f > 0.0:
        cur_key = f"TP{k}"
        next_idx = k + 1
        next_tp = tp_map.get(f"TP{next_idx}")
        if next_tp is None or next_tp <= 0.0:
            break
        anchor = entry_price if k == 1 else (tp_map.get(f"TP{k - 1}") or entry_price)
        cleared_cur = cur_key in seen
        can_advance = cleared_cur
        if not can_advance:
            cur_tp = tp_map.get(cur_key)
            if cur_tp is None or cur_tp <= 0.0:
                break
            past_anchor = sign * (live_price - anchor) > 1e-9
            dist_to_target = sign * (cur_tp - live_price)
            leg_span = sign * (cur_tp - anchor)
            progress = (sign * (live_price - anchor) / leg_span) if leg_span > 1e-9 else 0.0
            atr_proximity = dist_to_target <= (policy.tp_lift_atr_mult * atr_f) + 1e-9
            progress_hit = leg_span > 1e-9 and progress >= policy.tp_lift_progress_pct - 1e-9
            if k == 1:
                atr_proximity_allowed = bool(
                    be_locked or (leg_span > 1e-9 and progress >= policy.tp1_lift_min_progress_pct - 1e-9)
                )
                near_target = (atr_proximity and atr_proximity_allowed) or progress_hit
            else:
                near_target = atr_proximity or progress_hit
            can_advance = bool(past_anchor and momentum_ok and near_target)
        if not can_advance:
            break
        if not ((sign * (next_tp - live_price) >= buf - 1e-9) or (next_idx < eff_max_stage)):
            break
        k = next_idx
        target_stage_idx = k
        target_tp = next_tp

    should_lift = (
        target_stage_idx > orig_stage_idx
        and target_tp is not None
        and (sign * (target_tp - live_price) >= buf - 1e-9)
    )
    if not should_lift:
        target_stage_idx = orig_stage_idx
        target_tp = tp_map.get(f"TP{orig_stage_idx}")
    return {
        "should_lift": should_lift,
        "target_stage_idx": target_stage_idx,
        "target_stage_name": f"TP{target_stage_idx}",
        "target_tp": target_tp,
        "cleared_milestones": cleared,
        "newly_cleared": newly_cleared,
        "momentum_ok": momentum_ok,
    }


def select_progressive_trail_mult(mfe_dollars: float, policy: DynamicRiskPolicy) -> Tuple[float, str]:
    """Select the progressive ATR trailing multiplier and zone for a given MFE (open profit in $).

    Zones, keyed on MFE measured from the peak:
        STANDARD -> MFE <  trail_extended_mfe_dollars -> trail_atr_mult
        EXTENDED -> MFE >= trail_extended_mfe_dollars -> trail_extended_atr_mult
        HOME_RUN -> MFE >= trail_homerun_mfe_dollars -> trail_homerun_atr_mult

    The zone thresholds are inclusive at the tabulated boundaries ($10.00 and $18.00 by default).

    Forward-only invariant: MFE is measured from a monotonically non-decreasing peak, so the zone
    can only advance STANDARD -> EXTENDED -> HOME_RUN. The min(...) guard keeps the effective
    multiplier non-increasing even when a user configures an aggressive base multiplier, so
    trail_dist is non-increasing and atr_sl = peak - sign * trail_dist can only ever move
    forward. The trail therefore never loosens a stop.
    """
    mfe_f = max(0.0, _safe_float(mfe_dollars, 0.0))
    if mfe_f + 1e-9 >= policy.trail_homerun_mfe_dollars:
        return (min(policy.trail_atr_mult, policy.trail_extended_atr_mult, policy.trail_homerun_atr_mult), "HOME_RUN")
    if mfe_f + 1e-9 >= policy.trail_extended_mfe_dollars:
        return (min(policy.trail_atr_mult, policy.trail_extended_atr_mult), "EXTENDED")
    return (policy.trail_atr_mult, "STANDARD")


def compute_dynamic_atr_trail_stop(
    direction: str,
    entry_price: float,
    live_price: float,
    prev_peak: Optional[float],
    current_sl: Optional[float],
    atr: float,
    policy: DynamicRiskPolicy,
    digits: int = 2,
    stops_buffer: float = 0.0,
    cleared_milestones: Optional[Sequence[str]] = None,
    tp_levels: Optional[Dict[str, float]] = None,
    apply_milestone_floor: bool = True,
    *,
    r_lock_floor: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute forward-only BE Lock, Dynamic ATR Trail SL, and Milestone Floor for an open position.

    `r_lock_floor` is an optional extra floor contributed by the R-relative profit
    lock (`risk_lifecycle.compute_r_lock_floor`). It joins the same candidate list
    as the milestone floor, the ATR trail and the BE lock, so the picker keeps the
    tightest stop and the forward-only invariant is preserved. Passing `None`
    (the default) leaves every existing caller byte-for-byte unchanged.

    The returned `"r_lock_floor"` key means **the R floor that won the picker**:
    it is the rounded floor only when `stop_source == "R_LOCK"`, and `None`
    otherwise (the floor was rejected as invalid, or another candidate was
    tighter). A consumer can therefore treat `res["r_lock_floor"] is not None`
    as exactly "the R lock is the active stop".

    `"r_lock_floor"` is the **pre-clamp** floor, i.e. the price the R lock
    itself produced. `"candidate_sl"` is the **post-clamp** effective stop, i.e.
    the price after the broker minimum-distance clamp was applied. The two can
    differ; a consumer must take the price from `"candidate_sl"` and use
    `"r_lock_floor"` only as the provenance signal.
    """
    atr_f = _safe_float(atr, 0.0)
    if not policy.trail_enabled or atr_f <= 0.0 or entry_price <= 0.0 or live_price <= 0.0:
        return {
            "should_update": False,
            "armed": False,
            "be_armed": False,
            "trail_armed": False,
            "should_sweep_pending": False,
            "peak_price": prev_peak,
            "trail_dist": 0.0,
            "candidate_sl": None,
            "be_sl": None,
            "atr_sl": None,
            "milestone_floor": None,
            "r_lock_floor": None,
            "stop_source": None,
            "atr": round(atr_f, 4),
            "mfe_dollars": 0.0,
            # Trail disabled or ATR invalid: no zone is in effect, so report the base ladder.
            "trail_mult": round(policy.trail_atr_mult, 4),
            "trail_zone": "STANDARD",
        }

    sign = 1.0 if (direction or "BUY").strip().upper() == "BUY" else -1.0
    prev_f = _safe_float(prev_peak, 0.0)
    peak_px = (max(prev_f, live_price) if sign > 0 else min(prev_f, live_price)) if prev_f > 0.0 else live_price
    mfe = sign * (peak_px - entry_price)

    be_threshold = max(max(0.0, policy.be_activation_atr_mult) * atr_f, max(0.0, policy.be_min_mfe_dollars))
    be_armed = mfe + 1e-9 >= be_threshold
    be_sl = round(entry_price + sign * max(0.0, policy.be_lock_offset_dollars), digits) if be_armed else None
    should_sweep_pending = be_armed

    eff_mult, trail_zone = select_progressive_trail_mult(max(0.0, mfe), policy)
    trail_dist = round(_clamp(atr_f * eff_mult, policy.trail_min_dollars, policy.trail_max_dollars), digits)
    trail_armed = mfe + 1e-9 >= max(0.0, policy.trail_activation_atr_mult * atr_f)
    atr_sl = round(peak_px - sign * trail_dist, digits) if trail_armed else None

    milestone_floor = (
        _compute_milestone_floor(sign, entry_price, cleared_milestones, tp_levels, policy.be_lock_offset_dollars, digits)
        if apply_milestone_floor
        else None
    )

    # Validate the R floor ONCE, defensively, and reuse the validated value in
    # the candidate list and in every return path. The candidate guard must
    # never raise on a value the echo would happily `float()`.
    r_lock_val = _safe_float(r_lock_floor, 0.0)
    r_lock_candidate: Optional[float] = round(r_lock_val, digits) if r_lock_val > 0.0 else None

    candidates: List[Tuple[int, str, float]] = []
    if r_lock_candidate is not None:
        candidates.append((0, "R_LOCK", r_lock_candidate))
    if milestone_floor is not None:
        candidates.append((1, "MILESTONE_FLOOR", milestone_floor))
    if atr_sl is not None:
        candidates.append((2, "ATR_TRAIL", atr_sl))
    if be_sl is not None:
        candidates.append((3, "BE_LOCK", be_sl))

    armed = bool(candidates)
    if not armed:
        return {
            "should_update": False,
            "armed": False,
            "be_armed": False,
            "trail_armed": False,
            "should_sweep_pending": False,
            "peak_price": round(peak_px, digits),
            "trail_dist": trail_dist,
            "candidate_sl": None,
            "be_sl": None,
            "atr_sl": None,
            "milestone_floor": None,
            "r_lock_floor": None,
            "stop_source": None,
            "atr": round(atr_f, 4),
            "mfe_dollars": round(max(0.0, mfe), 4),
            "trail_mult": round(eff_mult, 4),
            "trail_zone": trail_zone,
        }

    # Tie-break priority: R_LOCK > MILESTONE_FLOOR > ATR_TRAIL > BE_LOCK.
    # The picker always keeps the tightest stop, so a floor can only tighten it.
    _, stop_source, raw_sl = min(
        candidates,
        key=lambda item: (-round(item[2], digits) if sign > 0 else round(item[2], digits), item[0]),
    )
    # The R floor is "in effect" only when it won the picker.
    active_r_lock = r_lock_candidate if stop_source == "R_LOCK" else None
    buf = max(0.0, _safe_float(stops_buffer, 0.0))
    if buf > 0.0:
        raw_sl = min(raw_sl, live_price - buf) if sign > 0 else max(raw_sl, live_price + buf)
    cand_sl = round(raw_sl, digits)
    cur_f = _safe_float(current_sl, 0.0)
    should_upd = True if cur_f <= 0.0 else ((sign * (cand_sl - cur_f)) >= max(1e-6, policy.trail_step_dollars - 1e-9))

    return {
        "should_update": should_upd,
        "armed": True,
        "be_armed": be_armed,
        "trail_armed": trail_armed,
        "should_sweep_pending": should_sweep_pending,
        "peak_price": round(peak_px, digits),
        "trail_dist": trail_dist,
        "candidate_sl": cand_sl,
        "be_sl": be_sl,
        "atr_sl": atr_sl,
        "milestone_floor": milestone_floor,
        "r_lock_floor": active_r_lock,
        "stop_source": stop_source,
        "atr": round(atr_f, 4),
        "mfe_dollars": round(max(0.0, mfe), 4),
        "trail_mult": round(eff_mult, 4),
        "trail_zone": trail_zone,
    }


def apply_dynamic_risk_command(cfg_mod: Any, args: Sequence[str], live_atr: float = 0.0) -> Tuple[bool, str]:
    """Query or configure ATR-scaled SL/TP via Saved Messages (`/dynamicrisk`)."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    pol = read_dynamic_risk_policy(cfg_mod)
    atr_f = max(0.0, _safe_float(live_atr, 0.0))
    if not cleaned:
        badge = f"🟢 ON (`{pol.sltp_mode}`)" if pol.sltp_enabled else "⚪ OFF"
        atr_txt = f"${atr_f:.2f} (Ratio `{compute_volatility_ratio(atr_f, pol):.2f}x`)" if atr_f > 0.0 else "N/A"
        return False, (
            f"📐 **Dynamic ATR SL/TP:** {badge}\n• Timeframe: `{pol.atr_timeframe}` (ATR({pol.atr_period}) = `{atr_txt}` | Base `${pol.atr_baseline_dollars:.2f}`)\n"
            f"• Multipliers: SL `{pol.sl_atr_mult:.2f}x` (`${pol.sl_min_dollars:.2f}–${pol.sl_max_dollars:.2f}`), TP `{pol.tp_atr_mult:.2f}x` (`${pol.tp_min_dollars:.2f}–${pol.tp_max_dollars:.2f}`)\n"
            "• Controls: `/dynamicrisk on|off`, `/dynamicrisk adaptive|fallback|pure`, `/dynamicrisk <sl_mult> <tp_mult> [M1|M5|M15|H1]`"
        )
    tok = cleaned[0].lower()
    if len(cleaned) == 1 and tok in _OFF_TOKENS:
        setattr(cfg_mod, "DYNAMIC_ATR_SLTP_ENABLED", False)
        return True, "⚪ **Dynamic ATR SL/TP Disabled** — signals use literal fixed SL/TP."
    if len(cleaned) == 1 and tok in _ON_TOKENS:
        setattr(cfg_mod, "DYNAMIC_ATR_SLTP_ENABLED", True)
        return True, f"🟢 **Dynamic ATR SL/TP Enabled** — Mode `{pol.sltp_mode}` (`{pol.atr_timeframe}` ATR({pol.atr_period}))."
    if len(cleaned) == 1 and tok in _MODE_ALIASES:
        mode = _MODE_ALIASES[tok]
        setattr(cfg_mod, "DYNAMIC_ATR_SLTP_ENABLED", True)
        setattr(cfg_mod, "DYNAMIC_SLTP_MODE", mode)
        return True, f"📐 **Dynamic ATR SL/TP Mode Updated → `{mode}`** (`{pol.atr_timeframe}` ATR({pol.atr_period}))."
    if len(cleaned) in (2, 3):
        sl_m, tp_m = _safe_float(cleaned[0], -1.0), _safe_float(cleaned[1], -1.0)
        tf = cleaned[2].upper() if len(cleaned) == 3 else pol.atr_timeframe
        if not (0.20 <= sl_m <= 20.0 and 0.20 <= tp_m <= 20.0 and tf in VALID_ATR_TIMEFRAMES):
            return False, _DYNAMIC_RISK_USAGE
        setattr(cfg_mod, "DYNAMIC_ATR_SLTP_ENABLED", True)
        setattr(cfg_mod, "DYNAMIC_SL_ATR_MULT", round(sl_m, 2))
        setattr(cfg_mod, "DYNAMIC_TP_ATR_MULT", round(tp_m, 2))
        setattr(cfg_mod, "DYNAMIC_ATR_TIMEFRAME", tf)
        return True, f"📐 **Dynamic ATR SL/TP Updated → `{pol.sltp_mode}` (`{tf}`)**\n• SL Multiplier: `{sl_m:.2f}x ATR` | TP Multiplier: `{tp_m:.2f}x ATR`"
    return False, _DYNAMIC_RISK_USAGE


def apply_atr_trail_command(cfg_mod: Any, args: Sequence[str], live_atr: float = 0.0) -> Tuple[bool, str]:
    """Query or configure the Primary Dynamic ATR Trailing Engine via Saved Messages (`/atrtrail`)."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    pol = read_dynamic_risk_policy(cfg_mod)
    atr_f = max(0.0, _safe_float(live_atr, 0.0))
    if not cleaned:
        badge = "🟢 ON (Primary Engine)" if pol.trail_enabled else "⚪ OFF"
        tp_badge = "🟢 ON" if pol.tp_trail_enabled else "⚪ OFF"
        atr_txt = f"${atr_f:.2f} → Trail `${_clamp(atr_f * pol.trail_atr_mult, pol.trail_min_dollars, pol.trail_max_dollars):.2f}`" if atr_f > 0.0 else "N/A"
        return False, (
            f"🎯 **Dynamic ATR Trailing Engine:** {badge}\n"
            f"• Timeframe: `{pol.trail_timeframe}` | SL Trail: `{pol.trail_atr_mult:.2f}x -> {pol.trail_extended_atr_mult:.2f}x @ ${pol.trail_extended_mfe_dollars:.0f} -> {pol.trail_homerun_atr_mult:.2f}x @ ${pol.trail_homerun_mfe_dollars:.0f}` (Arm `{pol.trail_activation_atr_mult:.2f}x` | Bounds `${pol.trail_min_dollars:.2f}–${pol.trail_max_dollars:.2f}` | Step `${pol.trail_step_dollars:.2f}`)\n"
            f"• BE Lock: `max({pol.be_activation_atr_mult:.2f}x ATR, ${pol.be_min_mfe_dollars:.2f})` → `Entry ± ${pol.be_lock_offset_dollars:.2f}` | TP Trail: {tp_badge} (`≤{pol.tp_lift_atr_mult:.2f}x ATR` or `≥{pol.tp_lift_progress_pct * 100:.0f}%` [TP1 `≥{pol.tp1_lift_min_progress_pct * 100:.0f}%`]) | Live: `{atr_txt}`\n"
            "• Controls: `/atrtrail on|off`, `/atrtrail tp on|off`, or `/atrtrail <mult> [activation_mult] [M1|M5|M15|H1]`"
        )
    tok = cleaned[0].lower()
    if len(cleaned) == 1 and tok in _OFF_TOKENS:
        setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", False)
        return True, "⚪ **Dynamic ATR Trailing Engine Disabled (OFF)**."
    if len(cleaned) == 1 and tok in _ON_TOKENS:
        setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", True)
        return True, (
            f"🎯 **Dynamic ATR Trailing Engine Enabled → `ON`** (`{pol.trail_atr_mult:.2f}x -> {pol.trail_extended_atr_mult:.2f}x @ ${pol.trail_extended_mfe_dollars:.0f} -> {pol.trail_homerun_atr_mult:.2f}x @ ${pol.trail_homerun_mfe_dollars:.0f} {pol.trail_timeframe} ATR`, "
            f"Bounds `${pol.trail_min_dollars:.2f}–${pol.trail_max_dollars:.2f}`, "
            f"BE `≥max({pol.be_activation_atr_mult:.2f}x, ${pol.be_min_mfe_dollars:.2f})`, "
            f"TP Trail `{'ON' if pol.tp_trail_enabled else 'OFF'}`)."
        )
    if len(cleaned) == 2 and tok == "tp":
        sub = cleaned[1].lower()
        if sub in _ON_TOKENS:
            setattr(cfg_mod, "DYNAMIC_ATR_TP_TRAIL_ENABLED", True)
            return True, f"🎯 **Dynamic ATR TP Trail Enabled → `ON`** (`≤{pol.tp_lift_atr_mult:.2f}x ATR` or `≥{pol.tp_lift_progress_pct * 100:.0f}%` [TP1 `≥{pol.tp1_lift_min_progress_pct * 100:.0f}%`])."
        if sub in _OFF_TOKENS:
            setattr(cfg_mod, "DYNAMIC_ATR_TP_TRAIL_ENABLED", False)
            return True, "⚪ **Dynamic ATR TP Trail Disabled (OFF)** — broker TPs stay fixed unless cleared."
        return False, _ATR_TRAIL_USAGE
    mult = _safe_float(cleaned[0], -1.0)
    if not (0.20 <= mult <= 20.0) or len(cleaned) > 3:
        return False, _ATR_TRAIL_USAGE
    act, tf = pol.trail_activation_atr_mult, pol.trail_timeframe
    for extra in cleaned[1:]:
        if extra.upper() in VALID_ATR_TIMEFRAMES:
            tf = extra.upper()
        elif 0.0 <= _safe_float(extra, -1.0) <= 20.0:
            act = _safe_float(extra, act)
        else:
            return False, _ATR_TRAIL_USAGE
    setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", True)
    setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MULT", round(mult, 2))
    setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", round(act, 2))
    setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_TIMEFRAME", tf)
    return True, (
        f"🎯 **Dynamic ATR Trailing Engine Enabled → `ON` (`{tf}`)**\n"
        f"• Trail: `{mult:.2f}x -> {pol.trail_extended_atr_mult:.2f}x @ ${pol.trail_extended_mfe_dollars:.0f} -> {pol.trail_homerun_atr_mult:.2f}x @ ${pol.trail_homerun_mfe_dollars:.0f} ATR` | Activation: `{act:.2f}x ATR` | Bounds `${pol.trail_min_dollars:.2f}–${pol.trail_max_dollars:.2f}`\n"
        f"• BE Lock: `max({pol.be_activation_atr_mult:.2f}x, ${pol.be_min_mfe_dollars:.2f})` → `±${pol.be_lock_offset_dollars:.2f}` | TP Trail: `{'ON' if pol.tp_trail_enabled else 'OFF'}`"
    )


def format_dynamic_risk_status_line(cfg_mod: Any, live_sltp_atr: float = 0.0, live_trail_atr: float = 0.0) -> str:
    """Format concise Dynamic Risk status line for `/status` and `/health`."""
    pol = read_dynamic_risk_policy(cfg_mod)
    sltp_s = (
        f"🟢 `{pol.sltp_mode}` (`{pol.atr_timeframe}`" + (f" ATR=${live_sltp_atr:.2f}" if live_sltp_atr > 0.0 else "") + ")"
        if pol.sltp_enabled else "⚪ OFF"
    )
    tp_tag = "+TP" if pol.tp_trail_enabled else ""
    trail_s = (
        f"🟢 ON (`{pol.trail_atr_mult:.2f}x -> {pol.trail_extended_atr_mult:.2f}x @ ${pol.trail_extended_mfe_dollars:.0f} -> {pol.trail_homerun_atr_mult:.2f}x @ ${pol.trail_homerun_mfe_dollars:.0f} {pol.trail_timeframe}` `${pol.trail_min_dollars:.2f}-${pol.trail_max_dollars:.2f}` BE≥`${pol.be_min_mfe_dollars:.2f}`{tp_tag}"
        + (f" ATR=${live_trail_atr:.2f}" if live_trail_atr > 0.0 else "")
        + ")"
        if pol.trail_enabled else "⚪ OFF"
    )
    return f"• Dynamic ATR Risk (`/dynamicrisk`, `/atrtrail`): SL/TP={sltp_s} | ATR Trail={trail_s}"
