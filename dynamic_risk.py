"""Volatility-scaled StopLoss/TakeProfit and optional dynamic ATR trailing stop."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Optional, Sequence, Tuple

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
    "⚠️ **Invalid syntax.** Usage: `/atrtrail <on|off|mult [activation_mult] [M1|M5|M15|H1]>`\n"
    "Examples: `/atrtrail on`, `/atrtrail 1.8 1.0 M5`, or `/atrtrail off`"
)


@dataclass(frozen=True)
class DynamicRiskPolicy:
    """Immutable snapshot of volatility-scaled SL/TP and dynamic ATR trailing settings."""

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
    tp_min_dollars: float = 2.00
    tp_max_dollars: float = 50.00
    scale_min: float = 0.75
    scale_max: float = 1.75
    trail_enabled: bool = False
    trail_timeframe: str = "M5"
    trail_atr_mult: float = 1.50
    trail_activation_atr_mult: float = 1.00
    trail_min_dollars: float = 1.50
    trail_max_dollars: float = 12.00
    trail_step_dollars: float = 0.25


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
    """Read dynamic ATR SL/TP and optional trailing-stop settings from config."""
    sl_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_SL_MIN_DOLLARS", 4.50), 4.50))
    sl_hi = max(sl_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_SL_MAX_DOLLARS", 35.00), 35.00))
    tp_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_TP_MIN_DOLLARS", 2.00), 2.00))
    tp_hi = max(tp_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_TP_MAX_DOLLARS", 50.00), 50.00))
    sc_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_SLTP_SCALE_MIN", 0.75), 0.75))
    sc_hi = max(sc_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_SLTP_SCALE_MAX", 1.75), 1.75))
    tr_lo = max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 1.50), 1.50))
    tr_hi = max(tr_lo, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00), 12.00))
    raw_per = _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_PERIOD", 14), 14.0)
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
        trail_enabled=bool(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", False)),
        trail_timeframe=_norm_choice(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_TIMEFRAME", "M5"), "M5", VALID_ATR_TIMEFRAMES),
        trail_atr_mult=max(0.10, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_MULT", 1.50), 1.50)),
        trail_activation_atr_mult=max(0.0, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00), 1.00)),
        trail_min_dollars=tr_lo,
        trail_max_dollars=tr_hi,
        trail_step_dollars=max(0.05, _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25), 0.25)),
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
        return sl_f
    scaled_dist = _clamp(orig_dist * vol_ratio, policy.sl_min_dollars, policy.sl_max_dollars)
    return round(sl_anchor - sign * scaled_dist, digits)


def _scale_tp_map(
    sign: float, tp_anchor: float, orig_map: Dict[str, float], vol_ratio: float, atr_tp1_dist: float, atr_step_dist: float, policy: DynamicRiskPolicy, digits: int, compute_missing_tp: bool
) -> Dict[str, float]:
    if policy.sltp_mode == "PURE_ATR" or (not orig_map and compute_missing_tp):
        built: Dict[str, float] = {"TP1": round(tp_anchor + sign * atr_tp1_dist, digits)}
        for idx in range(2, 6):
            built[f"TP{idx}"] = round(built[f"TP{idx - 1}"] + sign * atr_step_dist, digits)
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
    min_tick = 10.0 ** (-max(0, min(6, digits)))
    prev_orig, prev_new = orig_tp1, scaled["TP1"]
    for idx in range(2, 6):
        key = f"TP{idx}"
        if key not in orig_map:
            continue
        cur_orig = orig_map[key]
        delta = max(min_tick, sign * (cur_orig - prev_orig) * vol_ratio)
        scaled[key] = round(prev_new + sign * delta, digits)
        prev_orig, prev_new = cur_orig, scaled[key]
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
) -> Dict[str, Any]:
    """Compute forward-only dynamic ATR trailing SL for a single open position."""
    atr_f = _safe_float(atr, 0.0)
    if not policy.trail_enabled or atr_f <= 0.0 or entry_price <= 0.0 or live_price <= 0.0:
        return {"should_update": False, "armed": False, "peak_price": prev_peak, "trail_dist": 0.0, "candidate_sl": None, "atr": round(atr_f, 4), "mfe_dollars": 0.0}
    sign = 1.0 if (direction or "BUY").strip().upper() == "BUY" else -1.0
    trail_dist = round(_clamp(atr_f * policy.trail_atr_mult, policy.trail_min_dollars, policy.trail_max_dollars), digits)
    prev_f = _safe_float(prev_peak, 0.0)
    peak_px = (max(prev_f, live_price) if sign > 0 else min(prev_f, live_price)) if prev_f > 0.0 else live_price
    mfe = sign * (peak_px - entry_price)
    if mfe + 1e-9 < max(0.0, policy.trail_activation_atr_mult * atr_f):
        return {"should_update": False, "armed": False, "peak_price": round(peak_px, digits), "trail_dist": trail_dist, "candidate_sl": None, "atr": round(atr_f, 4), "mfe_dollars": round(max(0.0, mfe), 4)}
    raw_sl = peak_px - sign * trail_dist
    buf = max(0.0, _safe_float(stops_buffer, 0.0))
    if buf > 0.0:
        raw_sl = min(raw_sl, live_price - buf) if sign > 0 else max(raw_sl, live_price + buf)
    cand_sl = round(raw_sl, digits)
    cur_f = _safe_float(current_sl, 0.0)
    should_upd = True if cur_f <= 0.0 else ((sign * (cand_sl - cur_f)) >= max(1e-6, policy.trail_step_dollars - 1e-9))
    return {"should_update": should_upd, "armed": True, "peak_price": round(peak_px, digits), "trail_dist": trail_dist, "candidate_sl": cand_sl, "atr": round(atr_f, 4), "mfe_dollars": round(max(0.0, mfe), 4)}


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
    """Query or toggle optional dynamic ATR trailing stop via Saved Messages (`/atrtrail`)."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    pol = read_dynamic_risk_policy(cfg_mod)
    atr_f = max(0.0, _safe_float(live_atr, 0.0))
    if not cleaned:
        badge = "🟢 ON" if pol.trail_enabled else "⚪ OFF (Optional)"
        atr_txt = f"${atr_f:.2f} → Trail `${_clamp(atr_f * pol.trail_atr_mult, pol.trail_min_dollars, pol.trail_max_dollars):.2f}`" if atr_f > 0.0 else "N/A"
        return False, (
            f"🎯 **Dynamic ATR Trailing Stop:** {badge}\n• Timeframe: `{pol.trail_timeframe}` | Mult: `{pol.trail_atr_mult:.2f}x ATR` | Arm: `{pol.trail_activation_atr_mult:.2f}x ATR`\n"
            f"• Bounds: `${pol.trail_min_dollars:.2f}–${pol.trail_max_dollars:.2f}` (Step `${pol.trail_step_dollars:.2f}`) | Live: `{atr_txt}`\n"
            "• Controls: `/atrtrail on|off` or `/atrtrail <mult> [activation_mult] [M1|M5|M15|H1]`"
        )
    tok = cleaned[0].lower()
    if len(cleaned) == 1 and tok in _OFF_TOKENS:
        setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", False)
        return True, "⚪ **Dynamic ATR Trailing Stop Disabled (OFF)**."
    if len(cleaned) == 1 and tok in _ON_TOKENS:
        setattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ENABLED", True)
        return True, f"🎯 **Dynamic ATR Trailing Stop Enabled → `ON`** (`{pol.trail_atr_mult:.2f}x {pol.trail_timeframe} ATR`, Arm `{pol.trail_activation_atr_mult:.2f}x`)."
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
    return True, f"🎯 **Dynamic ATR Trailing Stop Enabled → `ON` (`{tf}`)**\n• Trail: `{mult:.2f}x ATR` | Activation: `{act:.2f}x ATR` | Bounds `${pol.trail_min_dollars:.2f}–${pol.trail_max_dollars:.2f}`"


def format_dynamic_risk_status_line(cfg_mod: Any, live_sltp_atr: float = 0.0, live_trail_atr: float = 0.0) -> str:
    """Format concise Dynamic Risk status line for `/status` and `/health`."""
    pol = read_dynamic_risk_policy(cfg_mod)
    sltp_s = (
        f"🟢 `{pol.sltp_mode}` (`{pol.atr_timeframe}`" + (f" ATR=${live_sltp_atr:.2f}" if live_sltp_atr > 0.0 else "") + ")"
        if pol.sltp_enabled else "⚪ OFF"
    )
    trail_s = (
        f"🟢 ON (`{pol.trail_atr_mult:.2f}x {pol.trail_timeframe}`" + (f" ATR=${live_trail_atr:.2f}" if live_trail_atr > 0.0 else "") + ")"
        if pol.trail_enabled else "⚪ OFF"
    )
    return f"• Dynamic ATR Risk (`/dynamicrisk`, `/atrtrail`): SL/TP={sltp_s} | ATR Trail={trail_s}"
