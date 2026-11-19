import copy
import logging
import math
import numbers
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple

import MetaTrader5 as mt5

import config as _config
from config import (
    ADAPTIVE_SPREAD_CEILING_DOLLARS,
    ADAPTIVE_SPREAD_MULTIPLIER,
    ALLOW_BELOW_ZONE_WICK,
    ARM_THRESHOLD_PCT,
    ATR_NOISE_FLOOR_MULT,
    AUTO_ADJUST_LOTS_TO_BALANCE,
    BANK_HARVEST_ALT_ARM_PCT,
    BANK_HARVEST_ALT_MIN_MFE_DOLLARS,
    BANK_HARVEST_ARM_MFE_DOLLARS,
    BANK_HARVEST_BROKER_CUSHION_DOLLARS,
    BANK_HARVEST_ENABLED,
    BANK_HARVEST_PULLBACK_ATR_MULT,
    BANK_HARVEST_PULLBACK_MAX,
    BANK_HARVEST_PULLBACK_MIN,
    BASE_LAYER_LOTS,
    BE_AFTER_TP1,
    BE_LAYERS,
    BE_OFFSET_PIPS,
    CANCEL_PENDING_ON_BE,
    CANCEL_PENDING_ON_PROFIT_LOCK,
    CANCEL_PENDING_ON_STAGE0_LOCK_EXIT,
    CANCEL_PENDING_ON_TP1,
    DAILY_LOSS_PAUSE_PCT,
    DAILY_PNL_CACHE_TTL_SECONDS,
    DEFAULT_SYMBOL,
    DEFAULT_TP_STEP_PIPS,
    DYNAMIC_ATR_BASELINE_DOLLARS,
    DYNAMIC_ATR_PERIOD,
    DYNAMIC_ATR_SLTP_ENABLED,
    DYNAMIC_ATR_TIMEFRAME,
    DYNAMIC_ATR_TRAIL_ACTIVATION_MULT,
    DYNAMIC_ATR_TRAIL_ENABLED,
    DYNAMIC_ATR_TRAIL_MAX_DOLLARS,
    DYNAMIC_ATR_TRAIL_MIN_DOLLARS,
    DYNAMIC_ATR_TRAIL_MULT,
    DYNAMIC_ATR_TRAIL_STEP_DOLLARS,
    DYNAMIC_ATR_TRAIL_TIMEFRAME,
    DYNAMIC_SL_ATR_MULT,
    DYNAMIC_SL_MAX_DOLLARS,
    DYNAMIC_SL_MIN_DOLLARS,
    DYNAMIC_SLTP_MODE,
    DYNAMIC_SLTP_SCALE_MAX,
    DYNAMIC_SLTP_SCALE_MIN,
    DYNAMIC_TP_ATR_MULT,
    DYNAMIC_TP_MAX_DOLLARS,
    DYNAMIC_TP_MIN_DOLLARS,
    DYNAMIC_TP_STEP_ATR_MULT,
    EMERGENCY_CUTOFF_LOSS_PCT,
    FLOAT_LOSS_FORCE_CLOSE_PCT,
    ENFORCE_HIGH_RISK_LOT_CAP,
    FALLBACK_SL_PIPS,
    GIVEBACK_PCT,
    GOLD_REVERSAL_GUARD_ENABLED,
    HARD_GIVEBACK_CEILING_DOLLARS,
    HARD_MAX_SPREAD_DOLLARS,
    LAYER_LOTS,
    LAYER_NAMES,
    LEGACY_LAYER_MAP,
    M1_ATR_PERIOD,
    MAGIC_NUMBER,
    MAX_ALLOWED_SPREAD_DOLLARS,
    MAX_GIVEBACK_DOLLARS,
    MAX_ORDERS_PER_SIGNAL,
    MAX_TP_DISTANCE_DOLLARS,
    MIN_GOLD_PULLBACK_DOLLARS,
    MIN_SL_BUFFER_PIPS,
    MIN_VALID_SPREAD_DOLLARS,
    MT5_LOGIN,
    MT5_PASSWORD,
    MT5_PATH,
    MT5_SERVER,
    NO_TP2_SURGE_ARM_PIPS,
    PEAK_HARVEST_ARM_PCT,
    PEAK_HARVEST_CUSHION_MULT,
    PEAK_HARVEST_MAX_CUSHION,
    PEAK_HARVEST_MIN_BROKER_CUSHION,
    PEAK_HARVEST_MIN_VIRT_CUSHION,
    PENDING_DUPLICATE_ORDER_TOLERANCE,
    PENDING_DUPLICATE_PRICE_TOLERANCE,
    PROFIT_RATCHET_ENABLED,
    PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED,
    RATCHET_BROKER_STEP_DOLLARS,
    RATCHET_LIFT_MIN_MFE_DOLLARS,
    RATCHET_STAGE4_MAX_CUSHION,
    RATCHET_STAGE4_MIN_CUSHION,
    RATCHET_STAGE4_RETENTION_PCT,
    RATCHET_TIER1A_LOCK_DOLLARS,
    RATCHET_TIER1A_MFE_DOLLARS,
    RATCHET_TIER1B_LOCK_DOLLARS,
    RATCHET_TIER1B_MFE_DOLLARS,
    RATCHET_TIER2_MAX_CUSHION,
    RATCHET_TIER2_MIN_CUSHION,
    RATCHET_TIER2_MIN_LOCK_DOLLARS,
    RATCHET_TIER2_MFE_DOLLARS,
    RATCHET_TIER2_RETENTION_PCT,
    RATCHET_TIER3_MAX_CUSHION,
    RATCHET_TIER3_MIN_CUSHION,
    RATCHET_TIER3_MFE_DOLLARS,
    RATCHET_TIER3_RETENTION_PCT,
    RATCHET_TIER4_MAX_CUSHION,
    RATCHET_TIER4_MIN_CUSHION,
    RATCHET_TIER4_MFE_DOLLARS,
    RATCHET_TIER4_RETENTION_PCT,
    RATCHET_TIER5_MAX_CUSHION,
    RATCHET_TIER5_MIN_CUSHION,
    RATCHET_TIER5_MFE_DOLLARS,
    RATCHET_TIER5_RETENTION_PCT,
    RATCHET_TIGHT_TP1_LOCK_PCT,
    RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS,
    RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS,
    RISK_LOT_MULTIPLIER,
    RUNNER_CLEAN_BREAK_PIPS,
    RUNNER_CONTINUOUS_TRAIL_ENABLED,
    RUNNER_DEEPEST_COUNT_FULL,
    RUNNER_EARLY_TP_LIFT_PCT,
    RUNNER_MAX_MIDRANGE_RATIO,
    RUNNER_MAX_TP_STAGE,
    RUNNER_PROACTIVE_TP_LIFT,
    RUNNER_PROXIMITY_BUFFER_PCT,
    RUNNER_PROXIMITY_BUFFER_PIPS,
    RUNNER_PROXIMITY_MODE,
    RUNNER_REVERSAL_CANDLE_CONFIRM,
    RUNNER_REVERSAL_CONFIRM_TICKS,
    RUNNER_REVERSAL_PULLBACK_PCT,
    RUNNER_REVERSAL_PULLBACK_PIPS,
    SHORTFALL_ARM_PIPS,
    SHORTFALL_CONFIRM_TICKS,
    SHORTFALL_PULLBACK_PIPS,
    SINGLE_PRICE_CONSOLIDATE_ORDERS,
    STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS,
    STAGE0_PROFIT_LOCK_ENABLED,
    STAGE0_PROFIT_LOCK_OFFSET_PIPS,
    STAGE1_ATR_ACTIVATION_PCT,
    STAGED_EXIT_ENABLED,
    STOP_LEVEL_SAFETY_BUFFER_DOLLARS,
    TIERED_STAGE_BANKING_ENABLED,
    TP1_CLOSE_PROXIMITY_PIPS,
    TP1_LOCK_BUFFER,
    TP1_TOUCH_BANK_BUFFER_PIPS,
    TP1_TOUCH_BANK_ENABLED,
    TP1_TRAIL_ATR_MULT,
    TP1_TRAIL_ENABLED,
    TP1_TRAIL_MAX_DOLLARS,
    TP1_TRAIL_MIN_DOLLARS,
    TP1_TRAIL_STEP_DOLLARS,
    TP2_SHORTFALL_ARM_PIPS,
    TP2_SHORTFALL_PULLBACK_PIPS,
    TP2_STEP_PIPS,
    TP3_STEP_PIPS,
    TP4_STEP_PIPS,
    TP5_STEP_PIPS,
    TP_FRONT_RUN_OFFSET,
    LIMIT_ENTRY_FRONT_RUN_OFFSET,
    TRAIL_DISTANCE_PIPS,
    TRAIL_STEP_PIPS,
    TRAIL_TP2_ATR_MULT,
    TRAIL_TP2_MAX,
    TRAIL_TP2_MIN,
    TRAIL_TP3_ATR_MULT,
    TRAIL_TP3_MAX,
    TRAIL_TP3_MIN,
    TRAIL_TP4_ATR_MULT,
    TRAIL_TP4_MAX,
    TRAIL_TP4_MIN,
)
from parser import ZoneInfo, compute_zone_4tiers, derive_tp_levels
from dynamic_risk import (
    DynamicRiskPolicy,
    compute_atr_fallback_sl_distance,
    compute_dynamic_atr_trail_stop,
    read_dynamic_risk_policy,
    scale_sl_tp_by_atr,
)

logger = logging.getLogger("copier.mt5")

_DEFAULT_BASE_LOTS: List[float] = [0.01, 0.01, 0.01, 0.02]
_V3_TO_LEGACY_LAYER: Dict[str, str] = {"L1": "TOP", "L2": "MID", "L4": "BOTTOM"}

# Snapshot import-time config bindings so tests monkeypatching either `mt5_executor.<NAME>`
# or `config.<NAME>` take effect cleanly.
_CFG_IMPORT_SNAPSHOT: Dict[str, Any] = {
    name: copy.deepcopy(globals()[name])
    for name in (
        "BASE_LAYER_LOTS",
        "RISK_LOT_MULTIPLIER",
        "LAYER_LOTS",
        "MAX_ORDERS_PER_SIGNAL",
        "MAGIC_NUMBER",
        "BE_LAYERS",
        "BE_OFFSET_PIPS",
        "BE_AFTER_TP1",
        "CANCEL_PENDING_ON_TP1",
        "FALLBACK_SL_PIPS",
        "TP_FRONT_RUN_OFFSET",
        "LIMIT_ENTRY_FRONT_RUN_OFFSET",
        "TP1_CLOSE_PROXIMITY_PIPS",
        "TP1_LOCK_BUFFER",
        "TP1_TOUCH_BANK_ENABLED",
        "TP1_TOUCH_BANK_BUFFER_PIPS",
        "STAGED_EXIT_ENABLED",
        "TP1_TRAIL_ENABLED",
        "TP1_TRAIL_ATR_MULT",
        "TP1_TRAIL_MIN_DOLLARS",
        "TP1_TRAIL_MAX_DOLLARS",
        "TP1_TRAIL_STEP_DOLLARS",
        "TRAIL_TP2_ATR_MULT",
        "TRAIL_TP2_MIN",
        "TRAIL_TP2_MAX",
        "TRAIL_TP3_ATR_MULT",
        "TRAIL_TP3_MIN",
        "TRAIL_TP3_MAX",
        "TRAIL_TP4_ATR_MULT",
        "TRAIL_TP4_MIN",
        "TRAIL_TP4_MAX",
        "DEFAULT_TP_STEP_PIPS",
        "TP2_STEP_PIPS",
        "TP3_STEP_PIPS",
        "TP4_STEP_PIPS",
        "TP5_STEP_PIPS",
        "TRAIL_DISTANCE_PIPS",
        "TRAIL_STEP_PIPS",
        "TP2_SHORTFALL_ARM_PIPS",
        "TP2_SHORTFALL_PULLBACK_PIPS",
        "NO_TP2_SURGE_ARM_PIPS",
        "SHORTFALL_ARM_PIPS",
        "SHORTFALL_PULLBACK_PIPS",
        "SHORTFALL_CONFIRM_TICKS",
        "GOLD_REVERSAL_GUARD_ENABLED",
        "ARM_THRESHOLD_PCT",
        "GIVEBACK_PCT",
        "MIN_GOLD_PULLBACK_DOLLARS",
        "ATR_NOISE_FLOOR_MULT",
        "M1_ATR_PERIOD",
        "MAX_GIVEBACK_DOLLARS",
        "HARD_GIVEBACK_CEILING_DOLLARS",
        "MAX_ALLOWED_SPREAD_DOLLARS",
        "ADAPTIVE_SPREAD_CEILING_DOLLARS",
        "ADAPTIVE_SPREAD_MULTIPLIER",
        "RUNNER_EARLY_TP_LIFT_PCT",
        "RUNNER_CONTINUOUS_TRAIL_ENABLED",
        "RUNNER_DEEPEST_COUNT_FULL",
        "RUNNER_MAX_TP_STAGE",
        "RUNNER_PROACTIVE_TP_LIFT",
        "RUNNER_PROXIMITY_MODE",
        "RUNNER_PROXIMITY_BUFFER_PIPS",
        "RUNNER_PROXIMITY_BUFFER_PCT",
        "RUNNER_MAX_MIDRANGE_RATIO",
        "RUNNER_REVERSAL_PULLBACK_PIPS",
        "RUNNER_REVERSAL_PULLBACK_PCT",
        "RUNNER_REVERSAL_CONFIRM_TICKS",
        "RUNNER_REVERSAL_CANDLE_CONFIRM",
        "RUNNER_CLEAN_BREAK_PIPS",
        "MIN_SL_BUFFER_PIPS",
        "ENFORCE_HIGH_RISK_LOT_CAP",
        "DAILY_LOSS_PAUSE_PCT",
        "EMERGENCY_CUTOFF_LOSS_PCT",
        "FLOAT_LOSS_FORCE_CLOSE_PCT",
        "DEFAULT_SYMBOL",
        "DAILY_PNL_CACHE_TTL_SECONDS",
        "PROFIT_RATCHET_ENABLED",
        "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED",
        "RATCHET_BROKER_STEP_DOLLARS",
        "STOP_LEVEL_SAFETY_BUFFER_DOLLARS",
        "HARD_MAX_SPREAD_DOLLARS",
        "MIN_VALID_SPREAD_DOLLARS",
        "RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS",
        "RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS",
        "RATCHET_TIGHT_TP1_LOCK_PCT",
        "RATCHET_LIFT_MIN_MFE_DOLLARS",
        "BANK_HARVEST_ENABLED",
        "TIERED_STAGE_BANKING_ENABLED",
        "CANCEL_PENDING_ON_BE",
        "CANCEL_PENDING_ON_PROFIT_LOCK",
        "SINGLE_PRICE_CONSOLIDATE_ORDERS",
        "PENDING_DUPLICATE_PRICE_TOLERANCE",
        "PENDING_DUPLICATE_ORDER_TOLERANCE",
        "MAX_TP_DISTANCE_DOLLARS",
        "DYNAMIC_ATR_SLTP_ENABLED",
        "DYNAMIC_SLTP_MODE",
        "DYNAMIC_ATR_TIMEFRAME",
        "DYNAMIC_ATR_PERIOD",
        "DYNAMIC_ATR_BASELINE_DOLLARS",
        "DYNAMIC_SL_ATR_MULT",
        "DYNAMIC_TP_ATR_MULT",
        "DYNAMIC_TP_STEP_ATR_MULT",
        "DYNAMIC_SL_MIN_DOLLARS",
        "DYNAMIC_SL_MAX_DOLLARS",
        "DYNAMIC_TP_MIN_DOLLARS",
        "DYNAMIC_TP_MAX_DOLLARS",
        "DYNAMIC_SLTP_SCALE_MIN",
        "DYNAMIC_SLTP_SCALE_MAX",
        "DYNAMIC_ATR_TRAIL_ENABLED",
        "DYNAMIC_ATR_TRAIL_TIMEFRAME",
        "DYNAMIC_ATR_TRAIL_MULT",
        "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT",
        "DYNAMIC_ATR_TRAIL_MIN_DOLLARS",
        "DYNAMIC_ATR_TRAIL_MAX_DOLLARS",
        "DYNAMIC_ATR_TRAIL_STEP_DOLLARS",
    )
}


def _cfg(name: str, fallback: Any = None) -> Any:
    """Resolve runtime setting, respecting monkeypatches on either mt5_executor or config."""
    mod_val = globals().get(name, fallback)
    if name in _CFG_IMPORT_SNAPSHOT and mod_val != _CFG_IMPORT_SNAPSHOT[name]:
        return mod_val
    return getattr(_config, name, mod_val)


def _num(val: Any, default: float = 0.0) -> float:
    """Return float(val) when val is a concrete finite real number (ignoring bool and MagicMock)."""
    if isinstance(val, numbers.Real) and not isinstance(val, bool):
        fval = float(val)
        if math.isfinite(fval):
            return fval
    return default


def _int_num(val: Any, default: int = 0) -> int:
    """Return int(val) when val is a concrete integer (ignoring bool and MagicMock)."""
    if isinstance(val, numbers.Integral) and not isinstance(val, bool):
        return int(val)
    return default


def _mt5_const(name: str, default: int) -> int:
    """Safely read an integer MT5 constant, falling back if mt5 is a MagicMock."""
    return _int_num(getattr(mt5, name, default), default)


def _decimals_for_step(step: float) -> int:
    """Count decimal places represented by a broker volume step."""
    if step <= 0.0 or not math.isfinite(step):
        return 2
    formatted = f"{step:.8f}".rstrip("0").rstrip(".")
    if "." not in formatted:
        return 0
    return min(8, len(formatted.split(".")[1]))


def _extract_symbol_volume_limits(sym_info: Any = None) -> Tuple[float, float, float, int]:
    """Extract safe (volume_min, volume_max, volume_step, step_decimals) from MT5 symbol info."""
    vol_min = _num(getattr(sym_info, "volume_min", 0.01), 0.01) if sym_info is not None else 0.01
    vol_max = _num(getattr(sym_info, "volume_max", 100.0), 100.0) if sym_info is not None else 100.0
    vol_step = _num(getattr(sym_info, "volume_step", 0.01), 0.01) if sym_info is not None else 0.01

    if vol_min <= 0.0:
        vol_min = 0.01
    if vol_step <= 0.0:
        vol_step = 0.01
    if vol_max < vol_min:
        vol_max = vol_min
    return vol_min, vol_max, vol_step, _decimals_for_step(vol_step)


def normalize_lot_to_symbol(lot: float, sym_info: Any = None) -> float:
    """
    Validate and floor-align a lot size to broker symbol volume constraints
    (volume_min, volume_max, volume_step). Returns 0.0 when lot <= 0 or invalid.
    """
    if lot is None or isinstance(lot, bool) or not isinstance(lot, numbers.Real):
        return 0.0
    val = float(lot)
    if not math.isfinite(val) or val <= 0.0:
        return 0.0

    vol_min, vol_max, vol_step, dec = _extract_symbol_volume_limits(sym_info)
    if val < vol_min:
        val = vol_min
    if val > vol_max:
        val = vol_max

    eps = max(1e-12, vol_step * 1e-7)
    floored = math.floor((val + eps) / vol_step) * vol_step
    rounded = round(floored, dec)
    if rounded < vol_min:
        rounded = round(vol_min, dec)
    if rounded > vol_max:
        # Step down from vol_max to stay inside [vol_min, vol_max]
        max_floored = math.floor((vol_max + eps) / vol_step) * vol_step
        rounded = round(max(vol_min, min(vol_max, max_floored)), dec)
    return float(rounded)


def scale_layer_lots(
    base_lots: Sequence[float] = BASE_LAYER_LOTS,
    multiplier: float = 1.0,
    sym_info: Any = None,
) -> List[float]:
    """
    Proportionally scale the 4-layer lot vector and normalize each layer against broker limits.
    Falls back to [0.01, 0.01, 0.01, 0.02] and multiplier=1.0 on invalid inputs.
    """
    valid_base: Optional[List[float]] = None
    if isinstance(base_lots, (list, tuple)) and len(base_lots) == 4:
        parsed = [_num(v, 0.0) for v in base_lots]
        if all(v > 0.0 for v in parsed):
            valid_base = parsed
    if valid_base is None:
        valid_base = list(_DEFAULT_BASE_LOTS)

    mult = _num(multiplier, 0.0)
    if mult <= 0.0:
        mult = 1.0

    return [normalize_lot_to_symbol(b * mult, sym_info) for b in valid_base]


def calculate_risk_guide_thresholds(capital: float) -> Dict[str, float]:
    """
    Professional Risk Guide Calculator for any custom account balance:
      £50   -> Low: 0.01 | Mid: 0.02 | High: 0.05
      £100  -> Low: 0.01 | Mid: 0.05 | High: 0.10
      £200  -> Low: 0.02 | Mid: 0.10 | High: 0.20
      £500  -> Low: 0.05 | Mid: 0.25 | High: 0.50
      £1000 -> Low: 0.10 | Mid: 0.50 | High: 1.00
    """
    cap = max(0.0, float(capital or 0.0))
    low_risk = max(0.01, round(math.floor((cap + 1e-9) / 100.0) * 0.01, 2))
    mid_risk = max(0.01, round(math.floor((cap + 1e-9) / 20.0) * 0.01, 2))
    high_risk = max(0.01, round(math.floor((cap + 1e-9) / 10.0) * 0.01, 2))
    return {
        "capital": round(cap, 2),
        "low_risk_lots": low_risk,
        "mid_risk_lots": mid_risk,
        "high_risk_lots": high_risk,
    }


def compute_dynamic_layer_lots_for_balance(capital: float, risk_mode: str = "high") -> List[float]:
    """
    Compute 4-layer lot vector [L1, L2, L3, L4] scaled to live account balance
    following the user's Professional Risk Guide:
      £50   account -> High: 0.05 | Mid: 0.02 | Low: 0.01 -> [0.01, 0.01, 0.01, 0.02]
      £100  account -> High: 0.10 | Mid: 0.05 | Low: 0.01 -> [0.02, 0.02, 0.02, 0.04]
      £200  account -> High: 0.20 | Mid: 0.10 | Low: 0.02 -> [0.04, 0.04, 0.04, 0.08]
      £500  account -> High: 0.50 | Mid: 0.25 | Low: 0.05 -> [0.10, 0.10, 0.10, 0.20]
      £1000 account -> High: 1.00 | Mid: 0.50 | Low: 0.10 -> [0.20, 0.20, 0.20, 0.40]

    Splits the target lot total into 4 layers with the canonical 1:1:1:2 ratio:
      L1 = 20%
      L2 = 20%
      L3 = 20%
      L4 = 40% (Runner)
    """
    thresholds = calculate_risk_guide_thresholds(capital)
    mode = (risk_mode or "high").strip().lower()
    if mode == "low":
        target = thresholds["low_risk_lots"]
    elif mode == "mid":
        target = thresholds["mid_risk_lots"]
    else:
        target = thresholds["high_risk_lots"]

    # When target lot total <= 0.05 (e.g. <= £50 account), return base [0.01, 0.01, 0.01, 0.02]
    if target <= 0.05 + 1e-9:
        return [0.01, 0.01, 0.01, 0.02]

    l1 = max(0.01, round(target * 0.20, 2))
    l2 = max(0.01, round(target * 0.20, 2))
    l3 = max(0.01, round(target * 0.20, 2))
    l4 = max(0.01, round(target - (l1 + l2 + l3), 2))
    return [l1, l2, l3, l4]


def _is_legacy_3layer_dict(layers: Any) -> bool:
    """Return True if a layers dict uses TOP/MID/BOTTOM without canonical L1..L4 keys."""
    if not isinstance(layers, dict) or not layers:
        return False
    has_legacy = any(k in layers for k in LEGACY_LAYER_MAP)
    has_v3 = any(k in layers for k in LAYER_NAMES)
    return has_legacy and not has_v3


def select_dynamic_runners(
    layers: Optional[Dict[str, Any]],
    market_tickets: Optional[Sequence[int]],
    full_runner_count: Optional[int] = None,
) -> Tuple[List[str], List[str]]:
    """
    Dynamically partition currently open layers (shallow-to-deep L1..L4) into
    (runner_layers, early_layers):
      - n == 0: ([], [])
      - n == 1: ([only_layer], [])  -- 0 layers closed at TP1; single layer scales TP1->TP4
      - n == 2: ([deepest], [L1])
      - n == 3: ([deepest], [L1, L2])
      - n >= 4: (open_layers[-count:], open_layers[:-count]) where count in {1, 2}
    """
    if not isinstance(layers, dict) or not layers:
        return [], []

    active_set = {
        _int_num(t, 0)
        for t in (market_tickets or [])
        if t is not None and _int_num(t, 0) > 0
    }
    if not active_set:
        return [], []

    ordered_keys: Sequence[str] = (
        LAYER_NAMES
        if any(k in layers for k in LAYER_NAMES)
        else ("TOP", "MID", "BOTTOM")
    )
    open_layers: List[str] = []
    for lk in ordered_keys:
        l_info = layers.get(lk)
        if not isinstance(l_info, dict):
            continue
        lt = _int_num(l_info.get("ticket"), 0)
        st = str(l_info.get("status") or "").strip().upper()
        if lt > 0 and lt in active_set and st not in ("CLOSED", "CANCELLED", "PENDING"):
            open_layers.append(lk)

    n = len(open_layers)
    if n == 0:
        return [], []
    if n == 1:
        return [open_layers[0]], []
    if n in (2, 3):
        return [open_layers[-1]], open_layers[:-1]

    raw_cnt = (
        _int_num(full_runner_count, 0)
        if full_runner_count is not None
        else _int_num(_cfg("RUNNER_DEEPEST_COUNT_FULL", RUNNER_DEEPEST_COUNT_FULL), 1)
    )
    count = max(1, min(2, raw_cnt if raw_cnt > 0 else 1))
    return open_layers[-count:], open_layers[:-count]


def _is_legacy_runner_signal(signal_data: Dict[str, Any]) -> bool:
    """Detect whether a signal record should execute legacy 3-layer runner logic vs v3 4-layer FSM."""
    layers = signal_data.get("layers") or {}
    has_v3_layers = any(k in layers for k in LAYER_NAMES)
    has_legacy_layers = any(k in layers for k in LEGACY_LAYER_MAP)
    runner_layer = str(signal_data.get("runner_layer") or "").strip().upper()
    if has_v3_layers or runner_layer in LAYER_NAMES:
        return False
    if has_legacy_layers:
        return True
    if runner_layer in LEGACY_LAYER_MAP:
        has_v3_tp = bool(signal_data.get("raw_tp4") is not None or signal_data.get("raw_tp5") is not None)
        return not has_v3_tp
    has_v3_markers = bool(
        signal_data.get("tp_levels")
        or signal_data.get("raw_tp4") is not None
        or signal_data.get("raw_tp5") is not None
        or "runner_milestone_idx" in signal_data
    )
    return not has_v3_markers


def _get_runner_fsm_stage(signal_data: Optional[Dict[str, Any]]) -> int:
    """
    Derive canonical v3.5.0 runner stage (0..4) purely from existing persisted fields:
      0 -> Pre-TP1 (all layers open, or pending/invalidated/aborted)
      1 -> TP1 scaling handoff (L1-L3 banked, runner active) or TP1 clean break
      2 -> TP2 clean break
      3 -> TP3 clean break
      4 -> TP4 clean break (TP5 mode) or TP4/TP5 ceiling
    """
    if not isinstance(signal_data, dict):
        return 0
    status = str(signal_data.get("status") or "").strip().upper()
    if status in ("PENDING", "PENDING_DELAY", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"):
        return 0
    cleared_stage = max(
        (
            int(s[2:])
            for x in (signal_data.get("runner_cleared_milestones") or [])
            if (s := str(x).strip().upper()).startswith("TP") and s[2:].isdigit()
        ),
        default=0,
    )
    raw_idx = signal_data.get("runner_milestone_idx")
    idx_stage = (
        (int(raw_idx) - 1)
        if isinstance(raw_idx, numbers.Integral) and not isinstance(raw_idx, bool) and int(raw_idx) > 1
        else 0
    )
    in_runner_phase = bool(
        signal_data.get("tp1_reached")
        or status == "RUNNER_ACTIVE"
        or cleared_stage >= 1
        or idx_stage >= 1
    )
    if not in_runner_phase:
        return 0
    return min(4, max(1, cleared_stage, idx_stage))


class MT5Executor:
    """
    MetaTrader 5 trade executor supporting v3.0 4-layer execution (L1..L4 -> TP1..TP5),
    broker volume scaling/caps, explicit MARKET/LIMIT/STOP routing, Layer 4 Active Runner FSM
    ("Bank It" Proximity Reversal Guard), and full legacy 3-layer compatibility.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._connected = False
        self._mt5_lock = threading.RLock()
        self._symbol_cache: Dict[str, str] = {}
        self.last_error: Optional[str] = None
        self.last_allocation_warning: Optional[str] = None
        self._dry_run_capital: Optional[float] = None
        self._dry_run_ticket_lots: Dict[int, float] = {}
        self._dry_run_pending_ticket_lots: Dict[int, float] = {}
        self._dry_run_pending_orders: Dict[int, Dict[str, Any]] = {}
        self._dry_run_daily_realized_pnl: float = 0.0
        self._dry_run_floating_pnl: float = 0.0
        self._dry_run_last_m1_candle: Optional[Dict[str, float]] = None
        self._dry_run_m1_atr: Optional[float] = None
        self._dry_run_atr: Optional[float] = None
        self.last_dynamic_risk_snapshot: Optional[Dict[str, Any]] = None
        self._cached_pos_profits: Dict[int, float] = {}
        self._bot_closed_tickets: Set[int] = set()
        self._ticket_placed_monotonic: Dict[int, float] = {}
        self._last_live_query_ok: bool = True
        self._last_live_pos_query_ok: bool = True
        self._last_live_ord_query_ok: bool = True
        self._atr_cache: Dict[Tuple[Any, ...], Tuple[float, float]] = {}
        self._server_offset_cache: Optional[int] = None
        self._server_offset_cache_ts: float = 0.0
        self._last_preflight_ctx: Optional[Dict[str, Any]] = None
        self._realized_pnl_cache: Optional[Tuple[float, float, str]] = None
        self._closing_lock = threading.Lock()
        self._closing_tickets: Set[int] = set()
        self._spread_history: Dict[str, Deque[float]] = {}
        self._daily_loss_baseline_realized: float = 0.0
        self._daily_drawdown_ref_balance: Optional[float] = None
        self._daily_drawdown_baseline_date: Optional[str] = None
        self._last_fill_prices: Dict[int, float] = {}
        self.last_modify_result: Dict[str, Any] = {}
        self._lift_abort_warn_ts: Dict[str, float] = {}
        self._last_close_details: Dict[int, Dict[str, Any]] = {}

    def _record_ticket_placed(self, ticket: int) -> None:
        """Record monotonic placement timestamp for MT5 IPC visibility grace windows."""
        t_int = _int_num(ticket, 0)
        if t_int <= 0:
            return
        now_m = time.monotonic()
        self._ticket_placed_monotonic[t_int] = now_m
        if len(self._ticket_placed_monotonic) > 512:
            cutoff = now_m - 300.0
            self._ticket_placed_monotonic = {
                k: ts for k, ts in self._ticket_placed_monotonic.items() if ts >= cutoff
            }

    def is_ticket_in_reconcile_grace(self, ticket: Any, grace_seconds: float = 3.0) -> bool:
        """Return True when a ticket was placed less than `grace_seconds` ago."""
        t_int = _int_num(ticket, 0)
        if t_int <= 0:
            return False
        ts = getattr(self, "_ticket_placed_monotonic", {}).get(t_int)
        if ts is None:
            return False
        return (time.monotonic() - ts) < max(0.0, float(grace_seconds))

    def connect(self) -> bool:
        """Initialize connection to MetaTrader 5 terminal."""
        if self.dry_run:
            logger.info("MT5Executor running in DRY-RUN mode.")
            self._connected = True
            self._atr_cache.clear()
            self._server_offset_cache_ts = 0.0
            self._last_preflight_ctx = None
            return True

        with self._mt5_lock:
            init_kwargs = {}
            if MT5_PATH:
                init_kwargs["path"] = MT5_PATH

            if not mt5.initialize(**init_kwargs):
                err = mt5.last_error()
                logger.error(f"MT5 initialize() failed: {err}")
                return False

            if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
                if not mt5.login(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
                    err = mt5.last_error()
                    logger.error(f"MT5 login failed: {err}")
                    return False
                logger.info(f"MT5 logged in successfully to account {MT5_LOGIN} ({MT5_SERVER})")
            else:
                term_info = mt5.terminal_info()
                acc_info = mt5.account_info()
                if acc_info:
                    logger.info(
                        f"Connected to MT5 active account: {getattr(acc_info, 'login', 'N/A')} "
                        f"on {getattr(acc_info, 'server', 'N/A')} "
                        f"(Balance: {_num(getattr(acc_info, 'balance', 0.0))} {getattr(acc_info, 'currency', '')})"
                    )
                else:
                    logger.info(f"Connected to MT5 terminal: {term_info}")

            self._connected = True
            self._atr_cache.clear()
            self._server_offset_cache_ts = 0.0
            self._last_preflight_ctx = None
            self._last_live_query_ok = True
            self._last_live_pos_query_ok = True
            self._last_live_ord_query_ok = True
            return True

    def shutdown(self) -> None:
        """Shutdown MT5 connection."""
        with self._mt5_lock:
            if not self.dry_run and self._connected:
                mt5.shutdown()
                self._connected = False
                self._realized_pnl_cache = None
                self._atr_cache.clear()
                self._server_offset_cache_ts = 0.0
                self._last_preflight_ctx = None
                self._last_live_query_ok = False
                self._last_live_pos_query_ok = False
                self._last_live_ord_query_ok = False
                logger.info("MT5 connection shut down.")

    def is_connected(self) -> bool:
        """Check if MT5 is initialized and connected to the trade server."""
        if self.dry_run:
            return True
        if not self._connected:
            return False
        try:
            with self._mt5_lock:
                term_info = mt5.terminal_info()
            return term_info is not None and bool(getattr(term_info, "connected", False))
        except Exception:
            return False

    def is_trade_allowed(self) -> bool:
        """Verify if MT5 Algo Trading is enabled in terminal (trade_allowed == True)."""
        if self.dry_run:
            return True
        if not self._connected:
            return False
        try:
            with self._mt5_lock:
                term = mt5.terminal_info()
            return term is not None and bool(getattr(term, "trade_allowed", False))
        except Exception:
            return False

    def get_health_status(self) -> Tuple[bool, bool]:
        """Return (connected, trade_allowed) using a single mt5.terminal_info() IPC call."""
        if self.dry_run:
            return True, True
        if not self._connected:
            return False, False
        try:
            with self._mt5_lock:
                term = mt5.terminal_info()
            if term is None:
                return False, False
            return bool(getattr(term, "connected", False)), bool(getattr(term, "trade_allowed", False))
        except Exception:
            return False, False

    def _ensure_connected(self) -> bool:
        """Ensure MT5 connection is live; reconnect if dropped."""
        if self.dry_run:
            return True
        with self._mt5_lock:
            if not self._connected:
                logger.info("MT5 not connected; attempting initial connection...")
                return self.connect()
            try:
                term_info = mt5.terminal_info()
                if term_info is None or not getattr(term_info, "connected", False):
                    logger.warning("MT5 connection lost; attempting reconnection...")
                    self._connected = False
                    return self.connect()
                return True
            except Exception as e:
                logger.warning(f"MT5 health check error ({e}); attempting reconnection...")
                self._connected = False
                return self.connect()

    def resolve_symbol(self, symbol: str) -> str:
        """
        Auto-resolves broker symbol suffixes (e.g. XAUUSD -> XAUUSD.s).
        Result is cached per session.
        """
        default_sym = _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        symbol = symbol or default_sym
        if self.dry_run or not self._connected:
            return symbol

        with self._mt5_lock:
            if symbol in self._symbol_cache:
                return self._symbol_cache[symbol]

            resolved = symbol
            if mt5.symbol_info(symbol) is not None:
                resolved = symbol
            else:
                for suffix in [".s", ".m", ".pro", ".ecn", "_i", "m", ".raw", ".c"]:
                    candidate = f"{symbol}{suffix}"
                    if mt5.symbol_info(candidate) is not None:
                        resolved = candidate
                        break
                else:
                    if symbol.upper() in ("GOLD", "XAUUSD"):
                        for cand in ["XAUUSD.s", "XAUUSD.m", "XAUUSD", "GOLD", "GOLD.s", "GOLD.m"]:
                            if mt5.symbol_info(cand) is not None:
                                resolved = cand
                                break

            self._symbol_cache[symbol] = resolved
            return resolved

    def ensure_symbol(self, symbol: str) -> bool:
        """Ensure symbol exists and is selected in Market Watch."""
        if self.dry_run:
            return True
        if not symbol:
            return False

        with self._mt5_lock:
            resolved = self.resolve_symbol(symbol)
            info = mt5.symbol_info(resolved)
            if info is None:
                logger.error(f"Symbol {symbol} (resolved: {resolved}) not found on broker.")
                return False
            if getattr(info, "visible", True) is False:
                if not mt5.symbol_select(resolved, True):
                    logger.error(f"Failed to select symbol {resolved} in Market Watch.")
                    return False
            return True

    def get_symbol_info(self, symbol: str):
        """Get MT5 symbol information (thread-safe)."""
        if self.dry_run:
            class DummySymbolInfo:
                digits = 2
                point = 0.01
                spread = 15
                trade_mode = 4  # Full access
                filling_mode = 2  # IOC
                visible = True
                trade_stops_level = 50
                volume_min = 0.01
                volume_max = 100.0
                volume_step = 0.01
            return DummySymbolInfo()
        with self._mt5_lock:
            resolved = self.resolve_symbol(symbol)
            return mt5.symbol_info(resolved)

    def get_tick(self, symbol: str):
        """Get latest tick for symbol (thread-safe)."""
        if self.dry_run:
            class DummyTick:
                ask = 3895.00
                bid = 3894.80
            return DummyTick()
        with self._mt5_lock:
            resolved = self.resolve_symbol(symbol)
            return mt5.symbol_info_tick(resolved)

    def get_pip_size(self, symbol: str) -> float:
        """
        Calculate pip value in price points dynamically:
        - Gold (XAUUSD / GOLD): 1 pip = 0.10 ($0.10)
        - Forex 3/5 digit: point * 10
        - Forex 2/4 digit: point
        """
        upper = (symbol or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)).upper()
        if "GOLD" in upper or "XAU" in upper:
            return 0.10

        info = self.get_symbol_info(symbol)
        if not info:
            return 0.0001

        point = _num(getattr(info, "point", 0.0001), 0.0001) or 0.0001
        if _int_num(getattr(info, "digits", 5), 5) in (3, 5):
            return point * 10.0
        return point

    def get_last_closed_m1_candle(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        Return the latest closed M1 candle {"open", "high", "low", "close", "time"}
        from MT5 (bar index 1, avoiding the still-forming bar 0).
        """
        if self.dry_run:
            return copy.deepcopy(self._dry_run_last_m1_candle) if self._dry_run_last_m1_candle else None
        if not self._connected:
            return None

        try:
            timeframe_m1 = _mt5_const("TIMEFRAME_M1", 1)
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol)
                rates = mt5.copy_rates_from_pos(resolved, timeframe_m1, 1, 1)
            if rates is None or len(rates) == 0:
                return None
            bar = rates[-1]

            def _bar_field(key: str) -> Optional[float]:
                if isinstance(bar, dict):
                    val = bar.get(key)
                else:
                    try:
                        val = bar[key]
                    except Exception:
                        val = getattr(bar, key, None)
                if isinstance(val, numbers.Real) and not isinstance(val, bool):
                    fval = float(val)
                    return fval if math.isfinite(fval) else None
                return None

            o = _bar_field("open")
            c = _bar_field("close")
            if o is None or c is None:
                return None
            h = _bar_field("high")
            l = _bar_field("low")
            t = _bar_field("time")
            return {
                "open": o,
                "high": h if h is not None else max(o, c),
                "low": l if l is not None else min(o, c),
                "close": c,
                "time": t if t is not None else 0.0,
            }
        except Exception as e:
            logger.debug(f"Could not fetch closed M1 candle for {symbol}: {e}")
            return None

    _ATR_TIMEFRAME_MAP: Dict[str, Tuple[str, int]] = {
        "M1": ("TIMEFRAME_M1", 1),
        "M5": ("TIMEFRAME_M5", 5),
        "M15": ("TIMEFRAME_M15", 15),
        "H1": ("TIMEFRAME_H1", 16385),
    }

    def get_atr(
        self,
        symbol: str,
        timeframe: Optional[str] = None,
        period: Optional[int] = None,
    ) -> float:
        """
        Compute closed-bar ATR(period) for the requested timeframe (M1/M5/M15/H1).
        In dry_run, returns `_dry_run_atr` when set, or `_dry_run_m1_atr` when timeframe == 'M1'.
        """
        tf = str(timeframe or _cfg("DYNAMIC_ATR_TIMEFRAME", DYNAMIC_ATR_TIMEFRAME) or "M5").strip().upper()
        if tf not in self._ATR_TIMEFRAME_MAP:
            tf = "M5"
        default_per = (
            _cfg("M1_ATR_PERIOD", M1_ATR_PERIOD)
            if tf == "M1"
            else _cfg("DYNAMIC_ATR_PERIOD", DYNAMIC_ATR_PERIOD)
        )
        eff_period = max(2, _int_num(period if period is not None else default_per, 14))

        if self.dry_run:
            if tf == "M1" and self._dry_run_m1_atr is not None and self._dry_run_m1_atr > 0:
                return float(self._dry_run_m1_atr)
            if self._dry_run_atr is not None and self._dry_run_atr > 0:
                return float(self._dry_run_atr)
            return 0.0
        if not self._connected:
            return 0.0

        sym_key = (symbol or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)).strip().upper()
        cache_key = (sym_key, eff_period) if tf == "M1" else (sym_key, tf, eff_period)
        atr_cache = getattr(self, "_atr_cache", None)
        if atr_cache is not None:
            cached = atr_cache.get(cache_key)
            if cached is not None and (time.monotonic() - cached[0]) < 60.0:
                return cached[1]

        tf_const_name, tf_fallback = self._ATR_TIMEFRAME_MAP[tf]
        try:
            mt5_tf = _mt5_const(tf_const_name, tf_fallback)
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol)
                rates = mt5.copy_rates_from_pos(resolved, mt5_tf, 1, eff_period + 1)
            if rates is None or len(rates) < 2:
                return 0.0

            def _field(bar_obj: Any, key: str) -> Optional[float]:
                if isinstance(bar_obj, dict):
                    val = bar_obj.get(key)
                else:
                    try:
                        val = bar_obj[key]
                    except Exception:
                        val = getattr(bar_obj, key, None)
                if isinstance(val, numbers.Real) and not isinstance(val, bool):
                    fv = float(val)
                    return fv if math.isfinite(fv) else None
                return None

            trs: List[float] = []
            prev_close = _field(rates[0], "close")
            for idx in range(1, len(rates)):
                bar = rates[idx]
                h, l, c = _field(bar, "high"), _field(bar, "low"), _field(bar, "close")
                if h is None or l is None or c is None:
                    continue
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close)) if prev_close is not None else (h - l)
                if tr >= 0.0:
                    trs.append(tr)
                prev_close = c

            if not trs:
                return 0.0
            atr_val = round(sum(trs) / len(trs), 4)
            if atr_val > 0.0 and atr_cache is not None:
                atr_cache[cache_key] = (time.monotonic(), atr_val)
            return atr_val
        except Exception as e:
            logger.debug(f"Could not compute {tf} ATR({eff_period}) for {symbol}: {e}")
            return 0.0

    def get_m1_atr(self, symbol: str, period: Optional[int] = None) -> float:
        """
        Compute the closed-bar M1 ATR(period) in price dollars/points (default period=14).
        Returns `self._dry_run_m1_atr` in dry-run when set, or 0.0 safely when bars are unavailable.
        """
        eff_period = max(2, _int_num(period if period is not None else _cfg("M1_ATR_PERIOD", M1_ATR_PERIOD), 14))
        return self.get_atr(symbol, timeframe="M1", period=eff_period)

    def is_valid_live_tick(self, symbol: str, tick: Any) -> bool:
        """
        Validate tick bid/ask sanity and guard against live corrupt zero-spread ticks
        (< MIN_VALID_SPREAD_DOLLARS). Synthetic zero-spread ticks in dry_run remain allowed.
        """
        if tick is None:
            return False
        bid = _num(getattr(tick, "bid", 0.0), 0.0)
        ask = _num(getattr(tick, "ask", 0.0), 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return False
        spread = ask - bid
        min_valid = float(_cfg("MIN_VALID_SPREAD_DOLLARS", MIN_VALID_SPREAD_DOLLARS))
        if self.dry_run:
            if spread > 1e-9 and spread + 1e-9 < min_valid:
                return False
        else:
            if spread + 1e-9 < min_valid:
                return False
        return True

    def is_within_freeze_level(
        self, symbol: str, target_price: Optional[float], tick: Any = None, sym_info: Any = None
    ) -> bool:
        """
        Return True when `target_price` sits inside broker `trade_freeze_level * point`
        of current market bid/ask in live mode (always False in dry_run or when freeze == 0).
        """
        if self.dry_run or target_price is None or float(target_price) <= 0.0:
            return False
        info = sym_info if sym_info is not None else self.get_symbol_info(symbol)
        if info is None:
            return False
        freeze_lvl = _num(getattr(info, "trade_freeze_level", 0.0), 0.0)
        if freeze_lvl <= 0.0:
            return False
        pt = _num(getattr(info, "point", 0.01), 0.01) or 0.01
        freeze_dist = freeze_lvl * pt
        t = tick if tick is not None else self.get_tick(symbol)
        if t is None:
            return False
        bid = _num(getattr(t, "bid", 0.0), 0.0)
        ask = _num(getattr(t, "ask", 0.0), 0.0)
        tp_f = float(target_price)
        if bid > 0.0 and abs(tp_f - bid) <= freeze_dist + 1e-9:
            return True
        if ask > 0.0 and abs(tp_f - ask) <= freeze_dist + 1e-9:
            return True
        return False

    def check_spread_spike(self, symbol: str, tick: Any) -> Tuple[bool, float, float]:
        """
        Adaptive Spread Spike Filter with absolute HARD_MAX_SPREAD_DOLLARS ($1.25) cap:
        - Returns `(is_spike, spread_dollars, max_allowed_spread_dollars)`.
        - Caps eff_max at HARD_MAX_SPREAD_DOLLARS so slow rollover ramps ($0.52 -> $2.60)
          cannot poison the rolling median history.
        - Corrupt zero-spread live ticks (< MIN_VALID_SPREAD_DOLLARS) do not pollute history.
        """
        cfg_max = float(_cfg("MAX_ALLOWED_SPREAD_DOLLARS", MAX_ALLOWED_SPREAD_DOLLARS))
        adapt_ceil = float(_cfg("ADAPTIVE_SPREAD_CEILING_DOLLARS", ADAPTIVE_SPREAD_CEILING_DOLLARS))
        hard_max = float(_cfg("HARD_MAX_SPREAD_DOLLARS", HARD_MAX_SPREAD_DOLLARS))
        min_valid = float(_cfg("MIN_VALID_SPREAD_DOLLARS", MIN_VALID_SPREAD_DOLLARS))
        mult = float(_cfg("ADAPTIVE_SPREAD_MULTIPLIER", ADAPTIVE_SPREAD_MULTIPLIER))
        if tick is None:
            return False, 0.0, min(hard_max, cfg_max)

        bid = _num(getattr(tick, "bid", 0.0), 0.0)
        ask = _num(getattr(tick, "ask", 0.0), 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return False, 0.0, min(hard_max, cfg_max)

        spread_dollars = round(ask - bid, 4)
        if not self.dry_run and spread_dollars + 1e-9 < min_valid:
            return True, spread_dollars, min(hard_max, cfg_max)

        sym_key = (symbol or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)).strip()

        with self._closing_lock:
            hist = self._spread_history.setdefault(sym_key, deque(maxlen=30))
            if hist:
                sorted_h = sorted(hist)
                med = sorted_h[len(sorted_h) // 2]
                if med > cfg_max + 1e-6:
                    eff_max = max(adapt_ceil, round(mult * med, 4))
                else:
                    eff_max = max(cfg_max, round(mult * med, 4))
            else:
                if spread_dollars <= min(hard_max, adapt_ceil) + 1e-6:
                    eff_max = max(adapt_ceil, round(mult * spread_dollars, 4)) if spread_dollars > cfg_max + 1e-6 else cfg_max
                else:
                    eff_max = cfg_max

            eff_max = round(min(hard_max, eff_max), 4)
            is_spike = spread_dollars > eff_max + 1e-6
            if not is_spike:
                hist.append(spread_dollars)

        return is_spike, spread_dollars, eff_max

    def try_begin_signal_close(self, signal_data: Dict[str, Any]) -> bool:
        """Atomically acquire `signal_data['is_closing'] = True` to prevent overlapping close triggers."""
        with self._closing_lock:
            if signal_data.get("is_closing"):
                return False
            signal_data["is_closing"] = True
            return True

    def end_signal_close(self, signal_data: Dict[str, Any]) -> None:
        """Release `signal_data['is_closing'] = False` after atomic close sequence completes."""
        with self._closing_lock:
            signal_data["is_closing"] = False

    def check_preflight(
        self,
        symbol: str,
        direction: str,
        raw_tp1: Optional[float] = None,
        sl: Optional[float] = None,
        zone: Any = None,
        single_price: Optional[float] = None,
        is_stop_order: bool = False,
        order_intent: str = "AUTO",
    ) -> Tuple[bool, str]:
        """
        Pre-Flight Safety Checks:
        1. MT5 connection live (reconnects if dropped).
        2. Algo Trading enabled & Daily Drawdown below 50% pause threshold.
        3. Trade mode full (market open) & valid live tick quote.
        4. Safe Price Window Check:
           - Immediate market / active entries: not within proximity buffer of TP1 or < MIN_SL_BUFFER_PIPS from SL.
           - Pending LIMIT or STOP setups away from live market validate safely without blocking on trigger side.
        """
        self._last_preflight_ctx = None
        direction_norm = (direction or "BUY").strip().upper()
        if direction_norm not in ("BUY", "SELL"):
            return False, f"Invalid trade direction: {direction}"

        if not self.dry_run and not self._ensure_connected():
            return False, "MT5 terminal not connected"

        if not self.is_trade_allowed():
            return False, "Algo Trading is OFF in MT5 (trade_allowed=False)"

        dd = self.get_daily_drawdown_status()
        if dd.get("pause_new_signals"):
            pause_pct = _cfg("DAILY_LOSS_PAUSE_PCT", DAILY_LOSS_PAUSE_PCT)
            return (
                False,
                f"{int(pause_pct)}% Daily Loss Limit Reached "
                f"(-{dd['total_daily_loss']:.2f} {dd['currency']} / {dd['loss_pct']:.1f}% of "
                f"{dd['reference_balance']:.2f} {dd['currency']} balance) — "
                f"Holding existing open trades to natural SL/TP; new signals paused.",
            )

        if self.dry_run:
            return True, "DRY_RUN: Pre-flight OK"

        resolved = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved):
            return False, f"Symbol {resolved} not accessible"

        info = self.get_symbol_info(resolved)
        if not info:
            return False, f"Failed to retrieve symbol info for {resolved}"

        raw_trade_mode = getattr(info, "trade_mode", None)
        full_mode = _mt5_const("SYMBOL_TRADE_MODE_FULL", 4)
        if isinstance(raw_trade_mode, int) and not isinstance(raw_trade_mode, bool) and raw_trade_mode != full_mode:
            return False, f"Trade mode is not FULL (mode={raw_trade_mode}) - market may be closed"

        tick = self.get_tick(resolved)
        if not tick:
            return False, f"No tick available for {resolved}"

        ask = getattr(tick, "ask", None)
        bid = getattr(tick, "bid", None)
        if not isinstance(ask, (int, float)) or not isinstance(bid, (int, float)) or ask <= 0 or bid <= 0:
            return False, f"Invalid/empty tick quote for {resolved} (ask={ask}, bid={bid})"

        point = _num(getattr(info, "point", 0.01), 0.01) or 0.01
        digits = _int_num(getattr(info, "digits", 2), 2)
        pip_size = self.get_pip_size(resolved)
        spread_points = round((ask - bid) / point, 1)
        logger.info(f"Pre-flight check: Current spread on {resolved} is {spread_points} points.")

        def _stash_ctx() -> None:
            self._last_preflight_ctx = {
                "symbol": resolved,
                "raw_symbol": symbol,
                "direction": direction_norm,
                "sym_info": info,
                "tick": tick,
                "digits": digits,
                "pip_size": pip_size,
                "ts": time.monotonic(),
            }

        intent = (order_intent or "AUTO").strip().upper()
        if is_stop_order and intent == "AUTO":
            intent = "STOP"

        if intent == "STOP" and isinstance(single_price, (int, float)) and not isinstance(single_price, bool):
            stop_px = float(single_price)
            is_pending_stop = (direction_norm == "BUY" and ask < stop_px) or (direction_norm == "SELL" and bid > stop_px)
            if is_pending_stop:
                logger.info(
                    f"Pre-flight OK for pending {direction_norm} STOP @ {stop_px} "
                    f"(live bid={bid}, ask={ask}, TP1={raw_tp1}, SL={sl})."
                )
                _stash_ctx()
                return True, "Pre-flight checks passed"

        is_pending_limit = False
        limit_ref = None
        if intent != "STOP":
            if zone is not None:
                limit_ref = getattr(zone, "top", None) if direction_norm == "BUY" else getattr(zone, "bottom", None)
            elif single_price is not None:
                limit_ref = single_price

        if isinstance(limit_ref, (int, float)) and not isinstance(limit_ref, bool):
            if direction_norm == "BUY" and ask > float(limit_ref):
                is_pending_limit = True
            elif direction_norm == "SELL" and bid < float(limit_ref):
                is_pending_limit = True

        if is_pending_limit:
            logger.info(
                f"Pre-flight OK for pending {direction_norm} LIMIT @ {limit_ref} "
                f"(live bid={bid}, ask={ask}, TP1={raw_tp1}, SL={sl})."
            )
            _stash_ctx()
            return True, "Pre-flight checks passed"

        tp1_prox_pips = _cfg("TP1_CLOSE_PROXIMITY_PIPS", TP1_CLOSE_PROXIMITY_PIPS)
        min_sl_pips = _cfg("MIN_SL_BUFFER_PIPS", MIN_SL_BUFFER_PIPS)
        tp1_buf = tp1_prox_pips * pip_size
        sl_buf = min_sl_pips * pip_size

        if direction_norm == "BUY":
            if raw_tp1 is not None and ask >= (raw_tp1 - tp1_buf):
                return False, f"Price {ask} is within {tp1_prox_pips} pips of TP1 ({raw_tp1}) - R:R expired"
            if sl is not None and ask <= (sl + sl_buf):
                return False, f"Price {ask} is too close to SL ({sl}) (< {min_sl_pips} pips)"
        else:
            if raw_tp1 is not None and bid <= (raw_tp1 + tp1_buf):
                return False, f"Price {bid} is within {tp1_prox_pips} pips of TP1 ({raw_tp1}) - R:R expired"
            if sl is not None and bid >= (sl - sl_buf):
                return False, f"Price {bid} is too close to SL ({sl}) (< {min_sl_pips} pips)"

        _stash_ctx()
        return True, "Pre-flight checks passed"

    def get_filling_modes(self, symbol: str) -> List[int]:
        """Determine supported filling modes for symbol."""
        ioc = _mt5_const("ORDER_FILLING_IOC", 1)
        fok = _mt5_const("ORDER_FILLING_FOK", 0)
        ret = _mt5_const("ORDER_FILLING_RETURN", 2)
        if self.dry_run:
            return [ioc]

        info = self.get_symbol_info(symbol)
        if not info:
            return [ret]

        filling_mask = _int_num(getattr(info, "filling_mode", 0), 0)
        modes = []
        if filling_mask & 2 != 0:
            modes.append(ioc)
        if filling_mask & 1 != 0:
            modes.append(fok)
        modes.append(ret)
        return modes

    def send_order_with_filling_fallback(self, request: dict) -> Tuple[bool, Any]:
        """Send MT5 order, retrying alternative filling modes on invalid fill error (thread-safe)."""
        done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
        placed_code = _mt5_const("TRADE_RETCODE_PLACED", 10008)
        invalid_fill_code = _mt5_const("TRADE_RETCODE_INVALID_FILL", 10030)

        if self.dry_run:
            dummy_ticket = random.randint(100000, 999999)
            logger.info(f"[DRY-RUN] Order sent successfully: ticket={dummy_ticket}, request={request}")
            class DummyResult:
                retcode = done_code
                order = dummy_ticket
                comment = "Dry run order"
            return True, DummyResult()

        with self._mt5_lock:
            if not self._ensure_connected():
                self.last_error = "MT5 terminal is not connected"
                logger.error("Cannot send order: MT5 terminal is not connected.")
                return False, None

            self.last_error = None
            symbol = request.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
            filling_modes = self.get_filling_modes(symbol)
            success_retcodes = (done_code, placed_code)

            for fill_mode in filling_modes:
                req_copy = request.copy()
                req_copy["type_filling"] = fill_mode
                result = mt5.order_send(req_copy)

                if result is None:
                    err = mt5.last_error()
                    self.last_error = f"order_send returned None: {err}"
                    logger.warning(f"order_send returned None (fill={fill_mode}): {err}")
                    continue

                if result.retcode in success_retcodes:
                    self.last_error = None
                    return True, result

                if result.retcode == invalid_fill_code:
                    self.last_error = f"retcode={result.retcode} ({getattr(result, 'comment', '')})"
                    logger.debug(f"Invalid filling mode {fill_mode}, attempting next mode...")
                    continue
                else:
                    self.last_error = f"retcode={result.retcode} ({getattr(result, 'comment', '')})"
                    logger.error(
                        f"Order failed: retcode={result.retcode}, comment='{getattr(result, 'comment', '')}', request={req_copy}"
                    )
                    return False, result

            return False, None

    @staticmethod
    def _extract_order_ticket(result: Any) -> Optional[int]:
        """Extract positive integer ticket from OrderSendResult, guarding unconfigured MagicMock attributes."""
        raw_order = getattr(result, "order", None)
        t = _int_num(raw_order, 0)
        if t > 0:
            return t
        raw_deal = getattr(result, "deal", None)
        d = _int_num(raw_deal, 0)
        if d > 0:
            return d
        if raw_order is not None and not isinstance(raw_order, (int, float, bool)):
            return (id(result) % 900000) + 100000
        return None

    def _get_dynamic_risk_policy(self) -> DynamicRiskPolicy:
        """Resolve DynamicRiskPolicy respecting monkeypatches on either config or mt5_executor."""
        proxy = SimpleNamespace(
            DYNAMIC_ATR_SLTP_ENABLED=_cfg("DYNAMIC_ATR_SLTP_ENABLED", DYNAMIC_ATR_SLTP_ENABLED),
            DYNAMIC_SLTP_MODE=_cfg("DYNAMIC_SLTP_MODE", DYNAMIC_SLTP_MODE),
            DYNAMIC_ATR_TIMEFRAME=_cfg("DYNAMIC_ATR_TIMEFRAME", DYNAMIC_ATR_TIMEFRAME),
            DYNAMIC_ATR_PERIOD=_cfg("DYNAMIC_ATR_PERIOD", DYNAMIC_ATR_PERIOD),
            DYNAMIC_ATR_BASELINE_DOLLARS=_cfg("DYNAMIC_ATR_BASELINE_DOLLARS", DYNAMIC_ATR_BASELINE_DOLLARS),
            DYNAMIC_SL_ATR_MULT=_cfg("DYNAMIC_SL_ATR_MULT", DYNAMIC_SL_ATR_MULT),
            DYNAMIC_TP_ATR_MULT=_cfg("DYNAMIC_TP_ATR_MULT", DYNAMIC_TP_ATR_MULT),
            DYNAMIC_TP_STEP_ATR_MULT=_cfg("DYNAMIC_TP_STEP_ATR_MULT", DYNAMIC_TP_STEP_ATR_MULT),
            DYNAMIC_SL_MIN_DOLLARS=_cfg("DYNAMIC_SL_MIN_DOLLARS", DYNAMIC_SL_MIN_DOLLARS),
            DYNAMIC_SL_MAX_DOLLARS=_cfg("DYNAMIC_SL_MAX_DOLLARS", DYNAMIC_SL_MAX_DOLLARS),
            DYNAMIC_TP_MIN_DOLLARS=_cfg("DYNAMIC_TP_MIN_DOLLARS", DYNAMIC_TP_MIN_DOLLARS),
            DYNAMIC_TP_MAX_DOLLARS=_cfg("DYNAMIC_TP_MAX_DOLLARS", DYNAMIC_TP_MAX_DOLLARS),
            DYNAMIC_SLTP_SCALE_MIN=_cfg("DYNAMIC_SLTP_SCALE_MIN", DYNAMIC_SLTP_SCALE_MIN),
            DYNAMIC_SLTP_SCALE_MAX=_cfg("DYNAMIC_SLTP_SCALE_MAX", DYNAMIC_SLTP_SCALE_MAX),
            DYNAMIC_ATR_TRAIL_ENABLED=_cfg("DYNAMIC_ATR_TRAIL_ENABLED", DYNAMIC_ATR_TRAIL_ENABLED),
            DYNAMIC_ATR_TRAIL_TIMEFRAME=_cfg("DYNAMIC_ATR_TRAIL_TIMEFRAME", DYNAMIC_ATR_TRAIL_TIMEFRAME),
            DYNAMIC_ATR_TRAIL_MULT=_cfg("DYNAMIC_ATR_TRAIL_MULT", DYNAMIC_ATR_TRAIL_MULT),
            DYNAMIC_ATR_TRAIL_ACTIVATION_MULT=_cfg("DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", DYNAMIC_ATR_TRAIL_ACTIVATION_MULT),
            DYNAMIC_ATR_TRAIL_MIN_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_MIN_DOLLARS", DYNAMIC_ATR_TRAIL_MIN_DOLLARS),
            DYNAMIC_ATR_TRAIL_MAX_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_MAX_DOLLARS", DYNAMIC_ATR_TRAIL_MAX_DOLLARS),
            DYNAMIC_ATR_TRAIL_STEP_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_STEP_DOLLARS", DYNAMIC_ATR_TRAIL_STEP_DOLLARS),
        )
        return read_dynamic_risk_policy(proxy)

    def apply_dynamic_atr_sl_tp(
        self,
        symbol: str,
        direction: str,
        sl_anchor: float,
        tp_anchor: float,
        sl: Optional[float],
        raw_tp1: Optional[float],
        tp_levels: Optional[Dict[str, float]],
        final_tp: Optional[float],
        pip_size: float,
        digits: int,
    ) -> Dict[str, Any]:
        """Compute ATR-scaled SL/TP levels, cache the snapshot, and log when applied."""
        policy = self._get_dynamic_risk_policy()
        atr = self.get_atr(symbol, policy.atr_timeframe, policy.atr_period)
        fr_off = float(_cfg("TP_FRONT_RUN_OFFSET", TP_FRONT_RUN_OFFSET)) * (pip_size / 0.10 if pip_size > 0 else 1.0)
        snap = scale_sl_tp_by_atr(
            direction=direction,
            sl_anchor=sl_anchor,
            tp_anchor=tp_anchor,
            atr=atr,
            policy=policy,
            digits=digits,
            sl=sl,
            raw_tp1=raw_tp1,
            tp_levels=tp_levels,
            final_tp=final_tp,
            front_run_offset=fr_off,
        )
        self.last_dynamic_risk_snapshot = snap
        if snap.get("applied"):
            logger.info(
                f"[DYNAMIC RISK] {direction} {symbol} ({snap['mode']} {snap['timeframe']} "
                f"ATR=${snap['atr']:.2f}, Ratio={snap['vol_ratio']:.2f}x): "
                f"SL {sl} -> {snap['sl']} | TP1 {raw_tp1} -> {snap['raw_tp1']}"
            )
        return snap

    def _compute_fallback_sl(
        self,
        is_buy: bool,
        ref_price: float,
        sl: Optional[float],
        pip_size: float,
        digits: int,
        direction_label: str,
        resolved_sym: str,
        kind_label: str,
    ) -> float:
        """Normalize explicit SL or compute ATR-scaled / FALLBACK_SL_PIPS stop loss away from ref_price."""
        if sl is None or sl == 0.0:
            policy = self._get_dynamic_risk_policy()
            if policy.sltp_enabled:
                atr = self.get_atr(resolved_sym, policy.atr_timeframe, policy.atr_period)
                atr_dist = compute_atr_fallback_sl_distance(atr, policy)
                if atr_dist is not None and atr_dist > 0.0:
                    final_sl = round(ref_price - atr_dist, digits) if is_buy else round(ref_price + atr_dist, digits)
                    logger.info(
                        f"Attached ATR fallback SL ({policy.atr_timeframe} ATR=${atr:.2f}, "
                        f"dist=${atr_dist:.2f}): {final_sl} on {kind_label} {direction_label} {resolved_sym}"
                    )
                    return final_sl
            fb_pips = _cfg("FALLBACK_SL_PIPS", FALLBACK_SL_PIPS)
            offset = fb_pips * pip_size
            final_sl = round(ref_price - offset, digits) if is_buy else round(ref_price + offset, digits)
            logger.info(
                f"Attached fallback SL ({fb_pips} pips): {final_sl} on {kind_label} {direction_label} {resolved_sym}"
            )
            return final_sl
        return round(float(sl), digits)

    def _prepare_symbol_order_context(
        self,
        symbol: str,
        direction: str,
        lot: float,
        caller_label: str,
        enforce_risk_cap: bool = True,
    ) -> Optional[Tuple[str, str, Any, int, float, float]]:
        """Validate direction/lot/connection/symbol and return (direction_norm, resolved_sym, sym_info, digits, pip_size, normalized_lot)."""
        self.last_error = None
        direction_norm = (direction or "").strip().upper()
        if direction_norm not in ("BUY", "SELL"):
            self.last_error = f"Invalid direction: {direction}"
            logger.error(f"{caller_label} rejected invalid direction: {direction!r}")
            return None

        if lot is None or float(lot) <= 0.0:
            self.last_error = f"Invalid lot size: {lot}"
            logger.error(f"{caller_label} rejected invalid lot size: {lot!r}")
            return None

        if not self._ensure_connected():
            self.last_error = "MT5 terminal is not connected"
            logger.error(f"Cannot execute {caller_label}: MT5 terminal is not connected.")
            return None

        resolved_sym = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved_sym):
            self.last_error = f"Symbol {resolved_sym} not accessible"
            return None

        sym_info = self.get_symbol_info(resolved_sym)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(resolved_sym)

        norm_lot = normalize_lot_to_symbol(float(lot), sym_info)
        if enforce_risk_cap:
            allowed_lot, block_reason = self.check_and_cap_order_lot(norm_lot, symbol=resolved_sym)
            if allowed_lot <= 0.0:
                self.last_error = block_reason or "High Risk Lot Cap reached"
                logger.warning(f"{caller_label} {direction_norm} blocked by Risk Guide Cap: {self.last_error}")
                return None
        else:
            allowed_lot = norm_lot

        final_lot = normalize_lot_to_symbol(allowed_lot, sym_info)
        if final_lot <= 0.0:
            self.last_error = "Normalized lot size below broker minimum"
            return None

        return direction_norm, resolved_sym, sym_info, digits, pip_size, final_lot

    def execute_market_order(
        self,
        symbol: str,
        direction: str,
        lot: float = 0.01,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "TG Layer Market",
        enforce_risk_cap: bool = True,
    ) -> Optional[int]:
        """Execute an immediate Market Buy or Sell order."""
        ctx = self._prepare_symbol_order_context(
            symbol, direction, lot, "execute_market_order", enforce_risk_cap=enforce_risk_cap
        )
        if ctx is None:
            return None
        direction_norm, resolved_sym, _, digits, pip_size, final_lot = ctx

        tick = self.get_tick(resolved_sym)
        if not tick:
            self.last_error = f"No tick available for {resolved_sym}"
            logger.error(f"Cannot get tick for {resolved_sym}")
            return None

        is_buy = direction_norm == "BUY"
        order_type = _mt5_const("ORDER_TYPE_BUY", 0) if is_buy else _mt5_const("ORDER_TYPE_SELL", 1)
        raw_entry = getattr(tick, "ask", None) if is_buy else getattr(tick, "bid", None)
        if not isinstance(raw_entry, (int, float)) or isinstance(raw_entry, bool) or raw_entry <= 0:
            self.last_error = f"Invalid tick price ({raw_entry}) for {resolved_sym}"
            logger.error(self.last_error)
            return None
        entry_price = round(float(raw_entry), digits)

        final_sl = self._compute_fallback_sl(
            is_buy, entry_price, sl, pip_size, digits, direction_norm, resolved_sym, "Market"
        )
        final_tp = round(float(tp), digits) if tp is not None else 0.0
        if final_tp > 0.0:
            if is_buy and final_tp <= entry_price:
                logger.warning(
                    f"Market BUY @ {entry_price} rejected invalid TP {final_tp} <= entry. Dropping TP to 0.0"
                )
                final_tp = 0.0
            elif not is_buy and final_tp >= entry_price:
                logger.warning(
                    f"Market SELL @ {entry_price} rejected invalid TP {final_tp} >= entry. Dropping TP to 0.0"
                )
                final_tp = 0.0

        request = {
            "action": _mt5_const("TRADE_ACTION_DEAL", 1),
            "symbol": resolved_sym,
            "volume": final_lot,
            "type": order_type,
            "price": entry_price,
            "sl": final_sl,
            "tp": final_tp,
            "deviation": 20,
            "magic": _cfg("MAGIC_NUMBER", MAGIC_NUMBER),
            "comment": str(comment or "")[:31],
            "type_time": _mt5_const("ORDER_TIME_GTC", 0),
        }

        success, result = self.send_order_with_filling_fallback(request)
        if success and result:
            ticket = self._extract_order_ticket(result)
            if not ticket:
                self.last_error = "Order succeeded but MT5 returned no ticket"
                logger.error(f"Market {direction_norm} order returned invalid ticket: {ticket}")
                return None
            if self.dry_run:
                self._dry_run_ticket_lots[ticket] = final_lot
            fill_px = _num(getattr(result, "price", 0.0), 0.0) or float(entry_price)
            self._last_fill_prices[ticket] = round(fill_px, digits)
            if len(self._last_fill_prices) > 512:
                self._last_fill_prices = dict(list(self._last_fill_prices.items())[-256:])
            self._record_ticket_placed(ticket)
            logger.info(
                f"Opened Market {direction_norm} Order #{ticket} | Symbol={resolved_sym}, "
                f"Lot={final_lot}, Price={fill_px}, SL={final_sl}, TP={final_tp}"
            )
            return ticket
        return None

    @staticmethod
    def _normalize_sym_base(symbol: Optional[str]) -> str:
        s = (symbol or "").strip().upper()
        if "." in s:
            s = s.split(".", 1)[0]
        if "_" in s:
            s = s.split("_", 1)[0]
        return s

    def has_duplicate_pending_order(
        self,
        symbol: str,
        direction: str,
        price: float,
        tolerance: Optional[float] = None,
        exclude_tickets: Optional[Sequence[int]] = None,
    ) -> Tuple[bool, Optional[int], Optional[float]]:
        """
        Check whether an unfilled pending LIMIT/STOP order already exists for the same
        symbol + direction within `tolerance` price points (default PENDING_DUPLICATE_ORDER_TOLERANCE).
        Returns (is_duplicate, matching_ticket, matching_price).
        """
        try:
            target_px = float(price)
        except (TypeError, ValueError):
            return False, None, None
        if not math.isfinite(target_px) or target_px <= 0.0:
            return False, None, None

        dir_norm = (direction or "").strip().upper()
        if dir_norm not in ("BUY", "SELL"):
            return False, None, None

        tol = max(
            0.0,
            float(
                tolerance
                if tolerance is not None
                else _cfg("PENDING_DUPLICATE_ORDER_TOLERANCE", PENDING_DUPLICATE_ORDER_TOLERANCE)
            ),
        )
        excl: Set[int] = {
            _int_num(t, 0) for t in (exclude_tickets or []) if _int_num(t, 0) > 0
        }
        sym_resolved = self.resolve_symbol(symbol)
        target_base = self._normalize_sym_base(sym_resolved or symbol)

        if self.dry_run or not self._connected:
            dry_orders = getattr(self, "_dry_run_pending_orders", None)
            if not isinstance(dry_orders, dict):
                return False, None, None
            active_lots = getattr(self, "_dry_run_pending_ticket_lots", {})
            for stale_t in list(dry_orders.keys()):
                if stale_t not in active_lots:
                    dry_orders.pop(stale_t, None)
            for t_k, rec in dry_orders.items():
                if not isinstance(rec, dict):
                    continue
                t_int = _int_num(t_k, 0)
                if t_int <= 0 or t_int in excl:
                    continue
                if self._normalize_sym_base(rec.get("symbol")) != target_base:
                    continue
                if str(rec.get("direction") or "").strip().upper() != dir_norm:
                    continue
                o_px = _num(rec.get("price"), 0.0)
                if o_px > 0.0 and abs(o_px - target_px) <= tol + 1e-6:
                    return True, t_int, o_px
            return False, None, None

        buy_types = (
            _mt5_const("ORDER_TYPE_BUY_LIMIT", 2),
            _mt5_const("ORDER_TYPE_BUY_STOP", 4),
        )
        sell_types = (
            _mt5_const("ORDER_TYPE_SELL_LIMIT", 3),
            _mt5_const("ORDER_TYPE_SELL_STOP", 5),
        )
        wanted_types = buy_types if dir_norm == "BUY" else sell_types

        live_orders = self.get_live_orders(sym_resolved)
        for ord_obj in live_orders:
            t_int = _int_num(getattr(ord_obj, "ticket", 0), 0)
            if t_int <= 0 or t_int in excl:
                continue
            o_type = _int_num(getattr(ord_obj, "type", -1), -1)
            if o_type not in wanted_types:
                continue
            o_sym = getattr(ord_obj, "symbol", None)
            if isinstance(o_sym, str) and o_sym.strip():
                if self._normalize_sym_base(o_sym) != target_base:
                    continue
            o_px = _num(getattr(ord_obj, "price_open", 0.0), 0.0)
            if o_px > 0.0 and abs(o_px - target_px) <= tol + 1e-6:
                return True, t_int, o_px

        return False, None, None

    def _place_pending_order(
        self,
        symbol: str,
        direction: str,
        price: float,
        lot: float,
        sl: Optional[float],
        tp: Optional[float],
        comment: str,
        pending_kind: str,
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Optional[int]:
        """Shared pending LIMIT / STOP placement helper."""
        self.last_error = None
        kind_upper = pending_kind.strip().upper()
        caller_label = "place_stop_order" if kind_upper == "STOP" else "place_limit_order"
        if price is None or float(price) <= 0.0:
            self.last_error = f"Invalid {kind_upper.lower()} price: {price}"
            logger.error(f"{caller_label} rejected invalid price: {price!r}")
            return None

        ctx = self._prepare_symbol_order_context(
            symbol, direction, lot, caller_label, enforce_risk_cap=False
        )
        if ctx is None:
            return None
        direction_norm, resolved_sym, _, digits, pip_size, final_lot = ctx

        is_buy = direction_norm == "BUY"
        if kind_upper == "STOP":
            order_type = _mt5_const("ORDER_TYPE_BUY_STOP", 4) if is_buy else _mt5_const("ORDER_TYPE_SELL_STOP", 5)
        else:
            order_type = _mt5_const("ORDER_TYPE_BUY_LIMIT", 2) if is_buy else _mt5_const("ORDER_TYPE_SELL_LIMIT", 3)

        target_price = round(float(price), digits)
        if not allow_duplicate_price:
            is_dup, dup_t, dup_px = self.has_duplicate_pending_order(
                resolved_sym,
                direction_norm,
                target_price,
                exclude_tickets=exclude_tickets,
            )
            if is_dup:
                self.last_error = (
                    f"Duplicate pending {direction_norm} order @ {target_price} "
                    f"(matches #{dup_t} @ {dup_px})"
                )
                logger.warning(f"{caller_label} blocked: {self.last_error}")
                return None

        final_sl = self._compute_fallback_sl(
            is_buy, target_price, sl, pip_size, digits, direction_norm, resolved_sym, kind_upper.title()
        )
        final_tp = round(float(tp), digits) if tp is not None else 0.0
        if final_tp > 0.0:
            if is_buy and final_tp <= target_price:
                logger.warning(
                    f"Pending {kind_upper} BUY @ {target_price} rejected invalid TP {final_tp} <= entry. Dropping TP to 0.0"
                )
                final_tp = 0.0
            elif not is_buy and final_tp >= target_price:
                logger.warning(
                    f"Pending {kind_upper} SELL @ {target_price} rejected invalid TP {final_tp} >= entry. Dropping TP to 0.0"
                )
                final_tp = 0.0

        request = {
            "action": _mt5_const("TRADE_ACTION_PENDING", 5),
            "symbol": resolved_sym,
            "volume": final_lot,
            "type": order_type,
            "price": target_price,
            "sl": final_sl,
            "tp": final_tp,
            "deviation": 20,
            "magic": _cfg("MAGIC_NUMBER", MAGIC_NUMBER),
            "comment": str(comment or "")[:31],
            "type_time": _mt5_const("ORDER_TIME_GTC", 0),
        }

        success, result = self.send_order_with_filling_fallback(request)
        if success and result:
            ticket = self._extract_order_ticket(result)
            if not ticket:
                self.last_error = "Pending order succeeded but MT5 returned no ticket"
                logger.error(f"{kind_upper.title()} {direction_norm} order returned invalid ticket: {ticket}")
                return None
            if self.dry_run:
                self._dry_run_pending_ticket_lots[ticket] = final_lot
                if not isinstance(getattr(self, "_dry_run_pending_orders", None), dict):
                    self._dry_run_pending_orders = {}
                self._dry_run_pending_orders[ticket] = {
                    "ticket": ticket,
                    "symbol": resolved_sym,
                    "direction": direction_norm,
                    "price": target_price,
                    "lot": final_lot,
                    "sl": final_sl,
                    "tp": final_tp,
                    "kind": kind_upper,
                }
            self._record_ticket_placed(ticket)
            logger.info(
                f"Placed {kind_upper.title()} {direction_norm} Order #{ticket} | Symbol={resolved_sym}, "
                f"TargetPrice={target_price}, Lot={final_lot}, SL={final_sl}, TP={final_tp}"
            )
            return ticket
        return None

    def place_limit_order(
        self,
        symbol: str,
        direction: str,
        price: float,
        lot: float = 0.01,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "TG Layer Limit",
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Optional[int]:
        """Place a BUY LIMIT or SELL LIMIT order at the specified price."""
        return self._place_pending_order(
            symbol=symbol,
            direction=direction,
            price=price,
            lot=lot,
            sl=sl,
            tp=tp,
            comment=comment,
            pending_kind="LIMIT",
            exclude_tickets=exclude_tickets,
            allow_duplicate_price=allow_duplicate_price,
        )

    def place_stop_order(
        self,
        symbol: str,
        direction: str,
        price: float,
        lot: float = 0.01,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "TG Layer Stop",
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Optional[int]:
        """Place a BUY STOP or SELL STOP breakout order at the specified price."""
        return self._place_pending_order(
            symbol=symbol,
            direction=direction,
            price=price,
            lot=lot,
            sl=sl,
            tp=tp,
            comment=comment,
            pending_kind="STOP",
            exclude_tickets=exclude_tickets,
            allow_duplicate_price=allow_duplicate_price,
        )

    def _place_pending_with_stops_fallback(
        self,
        symbol: str,
        direction: str,
        target_price: float,
        lot: float,
        sl: Optional[float],
        tp: Optional[float],
        pending_comment: str,
        market_comment: str,
        pending_kind: str = "LIMIT",
        enforce_risk_cap: bool = True,
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Tuple[Optional[int], str]:
        """
        Place a pending LIMIT or STOP order; if broker rejects because live price is touching
        or inside trade_stops_level of target_price, fall back to immediate Market entry.
        Duplicate-blocked pending orders NEVER fall back to Market entry.
        """
        kind_upper = pending_kind.strip().upper()
        if kind_upper == "STOP":
            t = self.place_stop_order(
                symbol=symbol, direction=direction, price=target_price, lot=lot,
                sl=sl, tp=tp, comment=pending_comment,
                exclude_tickets=exclude_tickets,
                allow_duplicate_price=allow_duplicate_price,
            )
        else:
            t = self.place_limit_order(
                symbol=symbol, direction=direction, price=target_price, lot=lot,
                sl=sl, tp=tp, comment=pending_comment,
                exclude_tickets=exclude_tickets,
                allow_duplicate_price=allow_duplicate_price,
            )
        if t:
            return t, kind_upper

        if isinstance(self.last_error, str) and self.last_error.startswith("Duplicate pending "):
            return None, "FAILED"

        if self.dry_run:
            return None, "FAILED"

        fresh_tick = self.get_tick(symbol)
        sym_info = self.get_symbol_info(symbol)
        point = _num(getattr(sym_info, "point", 0.01), 0.01) or 0.01
        stops_lvl = _int_num(getattr(sym_info, "trade_stops_level", 0), 0)
        pip_size = self.get_pip_size(symbol)
        tolerance = max(stops_lvl * point, 2.5 * pip_size)

        if fresh_tick:
            is_buy = direction.upper() == "BUY"
            ask = getattr(fresh_tick, "ask", None)
            bid = getattr(fresh_tick, "bid", None)
            if kind_upper == "STOP":
                near_or_crossed = (
                    (is_buy and isinstance(ask, (int, float)) and ask >= target_price - tolerance)
                    or (not is_buy and isinstance(bid, (int, float)) and bid <= target_price + tolerance)
                )
            else:
                near_or_crossed = (
                    (is_buy and isinstance(ask, (int, float)) and ask <= target_price + tolerance)
                    or (not is_buy and isinstance(bid, (int, float)) and bid >= target_price - tolerance)
                )
            if near_or_crossed:
                logger.info(
                    f"{kind_upper.title()} {direction} @ {target_price} within stops/crossed "
                    f"({ask if is_buy else bid}); falling back to Market entry."
                )
                mt = self.execute_market_order(
                    symbol=symbol,
                    direction=direction,
                    lot=lot,
                    sl=sl,
                    tp=tp,
                    comment=market_comment,
                    enforce_risk_cap=enforce_risk_cap,
                )
                if mt:
                    return mt, "MARKET"
        return None, "FAILED"

    def _place_limit_with_stops_fallback(
        self,
        symbol: str,
        direction: str,
        target_price: float,
        lot: float,
        sl: Optional[float],
        tp: Optional[float],
        limit_comment: str,
        market_comment: str,
    ) -> Tuple[Optional[int], str]:
        """Backward-compatible wrapper for LIMIT pending placement with stops fallback."""
        return self._place_pending_with_stops_fallback(
            symbol=symbol,
            direction=direction,
            target_price=target_price,
            lot=lot,
            sl=sl,
            tp=tp,
            pending_comment=limit_comment,
            market_comment=market_comment,
            pending_kind="LIMIT",
        )

    def _layer_will_execute_at_market(
        self,
        symbol: str,
        direction: str,
        target_price: Optional[float],
        tick: Any,
        order_intent: str = "AUTO",
        allow_market_buffer: bool = True,
    ) -> bool:
        """Return True when the given layer will execute immediately at market rather than as a pending order."""
        intent = (order_intent or "AUTO").strip().upper()
        is_buy = (direction or "BUY").strip().upper() == "BUY"

        if intent == "MARKET" or target_price is None:
            return True
        if tick is None:
            return False

        if intent == "STOP":
            return (tick.ask >= target_price) if is_buy else (tick.bid <= target_price)

        # AUTO / LIMIT routing
        if intent == "AUTO" and allow_market_buffer:
            pip_size = self.get_pip_size(symbol)
            auto_market_buffer = float(_cfg("AUTO_MARKET_SLIPPAGE_PIPS", 15.0)) * pip_size
            if is_buy:
                return tick.ask <= target_price + auto_market_buffer
            return tick.bid >= target_price - auto_market_buffer

        return (tick.ask <= target_price) if is_buy else (tick.bid >= target_price)

    def _execute_layer_price(
        self,
        symbol: str,
        direction: str,
        target_price: Optional[float],
        lot: float,
        sl: Optional[float],
        tp: Optional[float],
        tick: Any,
        market_comment: str,
        limit_comment: str,
        order_intent: str = "AUTO",
        stop_comment: Optional[str] = None,
        allow_market_buffer: bool = True,
        enforce_risk_cap: bool = True,
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Tuple[Optional[int], str]:
        """
        Route a single layer to MARKET, LIMIT, or STOP based on explicit order_intent
        and current bid/ask vs target_price.
        """
        intent = (order_intent or "AUTO").strip().upper()
        if self._layer_will_execute_at_market(
            symbol=symbol,
            direction=direction,
            target_price=target_price,
            tick=tick,
            order_intent=intent,
            allow_market_buffer=allow_market_buffer,
        ):
            t = self.execute_market_order(
                symbol=symbol,
                direction=direction,
                lot=lot,
                sl=sl,
                tp=tp,
                comment=market_comment,
                enforce_risk_cap=enforce_risk_cap,
            )
            return (t, "MARKET") if t else (None, "FAILED")

        pending_kind = "STOP" if intent == "STOP" else "LIMIT"
        pending_comment = (
            (stop_comment or limit_comment.replace("Limit", "Stop"))
            if pending_kind == "STOP"
            else limit_comment
        )
        return self._place_pending_with_stops_fallback(
            symbol=symbol,
            direction=direction,
            target_price=target_price,
            lot=lot,
            sl=sl,
            tp=tp,
            pending_comment=pending_comment,
            market_comment=market_comment,
            pending_kind=pending_kind,
            enforce_risk_cap=enforce_risk_cap,
            exclude_tickets=exclude_tickets,
            allow_duplicate_price=allow_duplicate_price,
        )

    def allocate_4layer_lots(
        self,
        symbol: str,
        base_lots: Optional[Sequence[float]] = None,
        multiplier: Optional[float] = None,
        market_mask: Optional[Sequence[bool]] = None,
    ) -> Tuple[List[float], Optional[str]]:
        """
        Atomically compute 4-layer lot sizes [L1, L2, L3, L4] scaled by multiplier and
        normalized to symbol volume constraints.

        Only layers marked True in ``market_mask`` (default all 4) compete for the active open
        position ``available_lots`` cap; pending limit/stop layers (False) retain full scaled lots.
        """
        sym_info = self.get_symbol_info(symbol)
        auto_adj = bool(_cfg("AUTO_ADJUST_LOTS_TO_BALANCE", AUTO_ADJUST_LOTS_TO_BALANCE))
        if base_lots is None and auto_adj and not (self.dry_run and self._dry_run_capital is None):
            status = self.get_risk_exposure_status()
            cap = float(status.get("capital", 0.0))
            eff_base = compute_dynamic_layer_lots_for_balance(cap)
        else:
            eff_base = base_lots if base_lots is not None else _cfg("BASE_LAYER_LOTS", BASE_LAYER_LOTS)
        eff_mult = multiplier if multiplier is not None else _cfg("RISK_LOT_MULTIPLIER", RISK_LOT_MULTIPLIER)
        if (
            isinstance(base_lots, (list, tuple))
            and len(base_lots) == 4
            and all(_num(v, -1.0) >= 0.0 for v in base_lots)
            and any(_num(v, 0.0) > 0.0 for v in base_lots)
            and any(_num(v, 0.0) == 0.0 for v in base_lots)
        ):
            cfg_fallback = list(_cfg("BASE_LAYER_LOTS", BASE_LAYER_LOTS))
            while len(cfg_fallback) < 4:
                cfg_fallback.append(0.01)
            filled_base = [
                _num(base_lots[i], 0.0) if _num(base_lots[i], 0.0) > 0.0 else float(cfg_fallback[i])
                for i in range(4)
            ]
            full_scaled = scale_layer_lots(filled_base, eff_mult, sym_info)
            scaled = [full_scaled[i] if _num(base_lots[i], 0.0) > 0.0 else 0.0 for i in range(4)]
        else:
            scaled = scale_layer_lots(eff_base, eff_mult, sym_info)

        enforce_cap = _cfg("ENFORCE_HIGH_RISK_LOT_CAP", ENFORCE_HIGH_RISK_LOT_CAP)
        if not enforce_cap or (self.dry_run and self._dry_run_capital is None):
            self.last_allocation_warning = None
            return scaled, None

        if market_mask is None:
            is_market = [True, True, True, True]
        else:
            is_market = [bool(x) for x in list(market_mask)[:4]]
            while len(is_market) < 4:
                is_market.append(True)

        market_requested = round(sum(scaled[i] for i in range(4) if is_market[i]), 6)
        if market_requested <= 0.0:
            self.last_allocation_warning = None
            return scaled, None

        vol_min, vol_max, vol_step, dec = _extract_symbol_volume_limits(sym_info)
        status = self.get_risk_exposure_status()
        avail = float(status.get("available_lots", 0.0))
        high_cap = float(status.get("high_risk_lots", 0.0))
        open_lots = float(status.get("open_lots", 0.0))
        cap = float(status.get("capital", 0.0))
        curr = status.get("currency", "GBP")

        if market_requested <= avail + 1e-9:
            self.last_allocation_warning = None
            return scaled, None

        if avail < vol_min - 1e-9:
            alloc = [0.0 if is_market[i] else scaled[i] for i in range(4)]
            base_msg = (
                f"High Risk Lot Cap Reached ({open_lots:.2f}/{high_cap:.2f} active lots "
                f"for {cap:.2f} {curr} account)"
            )
            warn = (
                f"{base_msg} — market layer(s) skipped; pending limit/stop layer(s) retained."
                if any(alloc)
                else base_msg
            )
            self.last_allocation_warning = warn
            return alloc, warn

        ratio = avail / market_requested if market_requested > 0 else 0.0
        eps = max(1e-12, vol_step * 1e-7)
        alloc: List[float] = []
        for i in range(4):
            if not is_market[i]:
                alloc.append(scaled[i])
                continue
            s = scaled[i]
            raw_p = s * ratio
            floored = round(math.floor((raw_p + eps) / vol_step) * vol_step, dec)
            if floored < vol_min - 1e-9:
                alloc.append(0.0)
            else:
                alloc.append(round(min(floored, s, vol_max), dec))

        alloc_market = round(sum(alloc[i] for i in range(4) if is_market[i]), 6)
        rem = round(avail - alloc_market, 6)

        # Pass 1: activate zeroed market layers in L1 -> L2 -> L3 -> L4 order when budget allows vol_min
        min_norm = normalize_lot_to_symbol(vol_min, sym_info)
        for i in range(4):
            if is_market[i] and alloc[i] == 0.0 and rem >= min_norm - 1e-9 and min_norm <= scaled[i] + 1e-9:
                alloc[i] = min_norm
                rem = round(rem - min_norm, 6)

        # Pass 2: top up remaining step budget in L4 -> L3 -> L2 -> L1 order across market layers
        while rem >= vol_step - 1e-9:
            progressed = False
            for i in (3, 2, 1, 0):
                if (
                    is_market[i]
                    and alloc[i] > 0.0
                    and rem >= vol_step - 1e-9
                    and (alloc[i] + vol_step) <= min(scaled[i], vol_max) + 1e-9
                ):
                    alloc[i] = round(alloc[i] + vol_step, dec)
                    rem = round(rem - vol_step, 6)
                    progressed = True
            if not progressed:
                break

        new_alloc_market = sum(alloc[i] for i in range(4) if is_market[i])
        warn = (
            f"High-Risk Cap applied: allocated {new_alloc_market:.2f}/{market_requested:.2f} active lots "
            f"(avail={avail:.2f})"
        )
        self.last_allocation_warning = warn
        return alloc, warn

    def _resolve_4layer_tps(
        self,
        direction: str,
        pip_size: float,
        digits: int,
        final_tp: Optional[float] = None,
        raw_tp1: Optional[float] = None,
        raw_tp2: Optional[float] = None,
        raw_tp3: Optional[float] = None,
        raw_tp4: Optional[float] = None,
        raw_tp5: Optional[float] = None,
        tp_levels: Optional[Dict[str, float]] = None,
        step_scale: float = 1.0,
    ) -> Tuple[Dict[str, Optional[float]], Dict[str, float]]:
        """
        Resolve per-layer TP targets (L1->final_tp, L2->TP2, L3->TP3, L4->TP5) and full TP1..TP5 map.
        Derives missing TPs deterministically using dynamic pip_size when TP1 is known.
        """
        merged: Dict[str, float] = {}
        if isinstance(tp_levels, dict):
            for k, v in tp_levels.items():
                if v is not None:
                    merged[str(k).upper()] = round(float(v), digits)

        explicit_args = {
            "TP1": raw_tp1,
            "TP2": raw_tp2,
            "TP3": raw_tp3,
            "TP4": raw_tp4,
            "TP5": raw_tp5,
        }
        for k, v in explicit_args.items():
            if v is not None:
                merged[k] = round(float(v), digits)

        seed_tps: List[float] = []
        for idx in range(1, 6):
            val = merged.get(f"TP{idx}")
            if val is not None:
                seed_tps.append(val)
            else:
                break

        if not seed_tps and final_tp is not None:
            seed_tps.append(round(float(final_tp), digits))

        if seed_tps:
            sc = step_scale if (isinstance(step_scale, (int, float)) and step_scale > 0.0) else 1.0
            steps = [
                round(float(_cfg("TP2_STEP_PIPS", TP2_STEP_PIPS)) * sc, 4),
                round(float(_cfg("TP3_STEP_PIPS", TP3_STEP_PIPS)) * sc, 4),
                round(float(_cfg("TP4_STEP_PIPS", TP4_STEP_PIPS)) * sc, 4),
                round(float(_cfg("TP5_STEP_PIPS", TP5_STEP_PIPS)) * sc, 4),
            ]
            derived = derive_tp_levels(seed_tps, direction=direction, pip_size=pip_size, step_pips=steps)
            for k, v in derived.items():
                merged.setdefault(k, round(float(v), digits))

        eff_final_tp = round(float(final_tp), digits) if final_tp is not None else None
        if eff_final_tp is None and merged.get("TP1") is not None:
            offset = float(_cfg("TP_FRONT_RUN_OFFSET", TP_FRONT_RUN_OFFSET)) * (pip_size / 0.10 if pip_size > 0 else 1.0)
            is_buy = (direction or "BUY").strip().upper() == "BUY"
            eff_final_tp = round(merged["TP1"] - offset, digits) if is_buy else round(merged["TP1"] + offset, digits)

        layer_tps: Dict[str, Optional[float]] = {
            "L1": eff_final_tp,
            "L2": eff_final_tp,
            "L3": eff_final_tp,
            "L4": eff_final_tp,
        }
        return layer_tps, merged

    def _validate_entry_prerequisites(
        self,
        symbol: str,
        direction: str,
        caller_label: str,
    ) -> Optional[Tuple[str, str, Any, Any, int, float]]:
        """Validate direction, MT5 connection, symbol availability, and tick quote for multi-layer execution."""
        direction_norm = (direction or "BUY").strip().upper()
        if direction_norm not in ("BUY", "SELL"):
            self.last_error = f"Invalid {caller_label} direction ({direction})"
            return None

        preflight_ctx = getattr(self, "_last_preflight_ctx", None)
        if not self.dry_run and self._connected and isinstance(preflight_ctx, dict):
            age = time.monotonic() - _num(preflight_ctx.get("ts"), 0.0)
            ctx_sym = preflight_ctx.get("symbol")
            ctx_raw = preflight_ctx.get("raw_symbol")
            ctx_tick = preflight_ctx.get("tick")
            ctx_info = preflight_ctx.get("sym_info")
            if (
                age < 1.0
                and preflight_ctx.get("direction") == direction_norm
                and (symbol == ctx_raw or symbol == ctx_sym or self.resolve_symbol(symbol) == ctx_sym)
                and ctx_tick is not None
                and ctx_info is not None
            ):
                ask = getattr(ctx_tick, "ask", None)
                bid = getattr(ctx_tick, "bid", None)
                if isinstance(ask, (int, float)) and isinstance(bid, (int, float)) and ask > 0 and bid > 0:
                    self._last_preflight_ctx = None
                    return (
                        direction_norm,
                        str(ctx_sym),
                        ctx_tick,
                        ctx_info,
                        _int_num(preflight_ctx.get("digits"), 2),
                        float(preflight_ctx.get("pip_size") or self.get_pip_size(str(ctx_sym))),
                    )
        self._last_preflight_ctx = None

        if not self._ensure_connected():
            self.last_error = "MT5 terminal is not connected"
            return None

        resolved_sym = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved_sym):
            self.last_error = f"Symbol {resolved_sym} not accessible"
            return None

        tick = self.get_tick(resolved_sym)
        if not tick:
            self.last_error = f"No tick available for {resolved_sym}"
            logger.error(f"Cannot get tick for {resolved_sym}")
            return None

        ask = getattr(tick, "ask", None)
        bid = getattr(tick, "bid", None)
        if not isinstance(ask, (int, float)) or not isinstance(bid, (int, float)) or ask <= 0 or bid <= 0:
            self.last_error = f"Invalid tick quote for {resolved_sym} (ask={ask}, bid={bid})"
            logger.error(self.last_error)
            return None

        sym_info = self.get_symbol_info(resolved_sym)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(resolved_sym)
        return direction_norm, resolved_sym, tick, sym_info, digits, pip_size

    def _execute_4layer_specs(
        self,
        symbol: str,
        direction: str,
        layer_specs: List[Tuple[str, Optional[float], float, Optional[float]]],
        sl: Optional[float],
        tick: Any,
        order_intent: str = "AUTO",
        entry_label: str = "4Layer",
        enforce_risk_cap: bool = True,
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """Shared execution loop for L1..L4 layers across Zone, Single-Price, and Market entries."""
        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}
        eff_exclude: List[int] = [_int_num(x, 0) for x in (exclude_tickets or []) if _int_num(x, 0) > 0]

        for l_name, target_px, l_lot, l_tp in layer_specs:
            if l_lot <= 0.0:
                logger.info(f"Skipping {l_name} ({entry_label}): allocated lot is 0.0 (cap enforced).")
                continue
            allow_mkt_buf = (l_name in ("L1", "TOP") or entry_label == "SinglePrice")
            t, o_type = self._execute_layer_price(
                symbol=symbol,
                direction=direction,
                target_price=target_px,
                lot=l_lot,
                sl=sl,
                tp=l_tp,
                tick=tick,
                market_comment=f"TG {l_name} (Market)",
                limit_comment=f"TG {l_name} (Limit)",
                order_intent=order_intent,
                stop_comment=f"TG {l_name} (Stop)",
                allow_market_buffer=allow_mkt_buf,
                enforce_risk_cap=enforce_risk_cap,
                exclude_tickets=eff_exclude,
                allow_duplicate_price=allow_duplicate_price,
            )
            if t:
                layer_entry_px: Optional[float] = None
                if o_type == "MARKET":
                    market_tickets.append(t)
                    status = "OPEN"
                    layer_entry_px = self._last_fill_prices.get(t, target_px)
                else:
                    pending_tickets.append(t)
                    eff_exclude.append(t)
                    status = "PENDING"
                layer_dict: Dict[str, Any] = {
                    "lot": l_lot,
                    "remaining_lot": l_lot,
                    "target_price": target_px,
                    "tp": l_tp,
                    "tp_stage": "TP1",
                    "ticket": t,
                    "order_type": o_type,
                    "status": status,
                }
                if layer_entry_px is not None:
                    layer_dict["entry_price"] = layer_entry_px
                    layer_dict["entry"] = layer_entry_px
                layers_data[l_name] = layer_dict

        return market_tickets, pending_tickets, layers_data

    def _apply_limit_entry_front_run(
        self,
        symbol: str,
        direction: str,
        target_price: float,
        final_tp: Optional[float],
        raw_tp1: Optional[float],
        pip_size: float,
        digits: int,
    ) -> float:
        """
        Shift pending LIMIT target_price toward market by LIMIT_ENTRY_FRONT_RUN_OFFSET
        (+$0.50 on BUY LIMIT, -$0.50 on SELL LIMIT on Gold) so round-number bounces
        (e.g. Msg 13191 dipping to 3710.44 bid above a 3710.00 BUY zone) still fill L1,
        provided at least $1.00 (10 pips) of profit room remains to TP1/final_tp.
        """
        raw_off = float(_cfg("LIMIT_ENTRY_FRONT_RUN_OFFSET", LIMIT_ENTRY_FRONT_RUN_OFFSET))
        if raw_off <= 0.0:
            return round(float(target_price), digits)
        scale = (pip_size / 0.10) if pip_size > 0 else 1.0
        off = raw_off * scale
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        shifted = round(float(target_price) + (off if is_buy else -off), digits)
        tp_bound = final_tp if (final_tp is not None and float(final_tp) > 0.0) else raw_tp1
        if tp_bound is not None and float(tp_bound) > 0.0:
            min_room = 10.0 * (pip_size if pip_size > 0 else 0.10)
            if is_buy and shifted >= float(tp_bound) - min_room + 1e-9:
                return round(float(target_price), digits)
            if not is_buy and shifted <= float(tp_bound) + min_room - 1e-9:
                return round(float(target_price), digits)
        return shifted

    def _prepare_dynamic_sltp_inputs(
        self,
        symbol: str,
        direction: str,
        sl_anchor: float,
        tp_anchor: float,
        sl: Optional[float],
        final_tp: Optional[float],
        raw_tp1: Optional[float],
        raw_tp2: Optional[float],
        raw_tp3: Optional[float],
        raw_tp4: Optional[float],
        raw_tp5: Optional[float],
        tp_levels: Optional[Dict[str, float]],
        pip_size: float,
        digits: int,
    ) -> Tuple[Optional[float], Dict[str, Optional[float]], Dict[str, float]]:
        """Apply ATR-scaled SL/TP once before resolving L1..L4 layer targets."""
        self.last_dynamic_risk_snapshot = None
        input_map: Dict[str, float] = {}
        if isinstance(tp_levels, dict):
            for k, v in tp_levels.items():
                if v is not None:
                    input_map[str(k).upper()] = round(float(v), digits)
        for idx, val in enumerate((raw_tp1, raw_tp2, raw_tp3, raw_tp4, raw_tp5), start=1):
            if val is not None:
                input_map[f"TP{idx}"] = round(float(val), digits)

        snap = self.apply_dynamic_atr_sl_tp(
            symbol=symbol,
            direction=direction,
            sl_anchor=sl_anchor,
            tp_anchor=tp_anchor,
            sl=sl,
            raw_tp1=input_map.get("TP1"),
            tp_levels=input_map or None,
            final_tp=final_tp,
            pip_size=pip_size,
            digits=digits,
        )
        eff_sl = sl
        eff_tp_map = dict(input_map)
        eff_final_tp = final_tp
        step_scale = 1.0
        if snap.get("applied"):
            eff_sl = snap.get("sl")
            eff_tp_map = dict(snap.get("tp_levels") or {})
            eff_final_tp = snap.get("final_tp")
            step_scale = float(snap.get("vol_ratio") or 1.0)

        layer_tps, merged_tps = self._resolve_4layer_tps(
            direction=direction,
            pip_size=pip_size,
            digits=digits,
            final_tp=eff_final_tp,
            raw_tp1=eff_tp_map.get("TP1"),
            raw_tp2=eff_tp_map.get("TP2"),
            raw_tp3=eff_tp_map.get("TP3"),
            raw_tp4=eff_tp_map.get("TP4"),
            raw_tp5=eff_tp_map.get("TP5"),
            tp_levels=eff_tp_map or None,
            step_scale=step_scale,
        )
        if snap.get("applied") and isinstance(self.last_dynamic_risk_snapshot, dict):
            self.last_dynamic_risk_snapshot["tp_levels"] = dict(merged_tps)
            self.last_dynamic_risk_snapshot["raw_tp1"] = merged_tps.get("TP1", snap.get("raw_tp1"))
            self.last_dynamic_risk_snapshot["final_tp"] = layer_tps.get("L1")
        return eff_sl, layer_tps, merged_tps

    def execute_4layer_zone(
        self,
        symbol: str,
        direction: str,
        zone: ZoneInfo,
        sl: Optional[float],
        final_tp: Optional[float],
        raw_tp1: Optional[float] = None,
        raw_tp2: Optional[float] = None,
        raw_tp3: Optional[float] = None,
        raw_tp4: Optional[float] = None,
        raw_tp5: Optional[float] = None,
        tp_levels: Optional[Dict[str, float]] = None,
        base_lots: Optional[Sequence[float]] = None,
        multiplier: Optional[float] = None,
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Split a Zone entry into 4 layers across [0%, 33.3%, 66.7%, 100%] (L1..L4 -> TP1/TP2/TP3/TP5).
        Returns (market_tickets, pending_tickets, layers_data).
        """
        self.last_dynamic_risk_snapshot = None
        if zone is None:
            self.last_error = "Invalid 4-layer zone parameters (zone=None)"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer zone")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        if hasattr(zone, "get_tiers"):
            raw_tiers = zone.get_tiers(direction_norm)
        elif isinstance(zone, dict):
            raw_tiers = compute_zone_4tiers(zone["top"], zone["bottom"], direction_norm, digits=digits)
        else:
            raw_tiers = compute_zone_4tiers(zone.top, zone.bottom, direction_norm, digits=digits)
        tiers = {
            k: self._apply_limit_entry_front_run(
                symbol=resolved_sym,
                direction=direction_norm,
                target_price=float(v),
                final_tp=final_tp,
                raw_tp1=raw_tp1 or (tp_levels or {}).get("TP1"),
                pip_size=pip_size,
                digits=digits,
            )
            for k, v in raw_tiers.items()
        }

        market_mask = [
            self._layer_will_execute_at_market(
                symbol=resolved_sym,
                direction=direction_norm,
                target_price=tiers[l_name],
                tick=tick,
                order_intent="AUTO",
                allow_market_buffer=(l_name in ("L1", "TOP")),
            )
            for l_name in LAYER_NAMES
        ]
        alloc_lots, warn = self.allocate_4layer_lots(
            resolved_sym, base_lots=base_lots, multiplier=multiplier, market_mask=market_mask
        )
        if sum(alloc_lots) <= 0.0:
            self.last_error = warn or "High Risk Lot Cap reached"
            return [], [], {}

        sl_anchor = min(tiers.values()) if direction_norm == "BUY" else max(tiers.values())
        sl, layer_tps, _ = self._prepare_dynamic_sltp_inputs(
            symbol=resolved_sym,
            direction=direction_norm,
            sl_anchor=sl_anchor,
            tp_anchor=tiers["L1"],
            sl=sl,
            final_tp=final_tp,
            raw_tp1=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels,
            pip_size=pip_size,
            digits=digits,
        )

        logger.info(
            f"Executing 4-Layer Zone for {direction_norm} {resolved_sym}: "
            f"Tiers={tiers}, Lots={alloc_lots}, TPs={layer_tps} | SL={sl}"
        )

        layer_specs = [
            (l_name, tiers[l_name], alloc_lots[idx], layer_tps[l_name])
            for idx, l_name in enumerate(LAYER_NAMES)
        ]
        return self._execute_4layer_specs(
            symbol=resolved_sym,
            direction=direction_norm,
            layer_specs=layer_specs,
            sl=sl,
            tick=tick,
            order_intent="AUTO",
            entry_label="Zone",
            enforce_risk_cap=False,
            exclude_tickets=exclude_tickets,
            allow_duplicate_price=allow_duplicate_price,
        )

    def _resolve_active_layer_subset(
        self,
        base_lots: Optional[Sequence[float]] = None,
        max_layers: Optional[int] = None,
        layers_subset: Optional[Sequence[str]] = None,
    ) -> Tuple[List[str], Optional[List[float]]]:
        """Resolve active layer names (e.g. ['L1'] for stubs) and masked base_lots vector."""
        if layers_subset is not None:
            wanted = {str(k).strip().upper() for k in layers_subset if k}
            active_names = [l_name for l_name in LAYER_NAMES if l_name in wanted]
        elif max_layers is not None:
            k = max(1, min(len(LAYER_NAMES), int(max_layers)))
            active_names = list(LAYER_NAMES[:k])
        else:
            active_names = list(LAYER_NAMES)

        if len(active_names) >= len(LAYER_NAMES):
            return active_names, (list(base_lots) if base_lots is not None else None)

        cfg_base = list(base_lots) if base_lots is not None else list(_cfg("BASE_LAYER_LOTS", BASE_LAYER_LOTS))
        while len(cfg_base) < len(LAYER_NAMES):
            cfg_base.append(0.01)
        masked_lots = [
            float(cfg_base[idx]) if l_name in active_names else 0.0
            for idx, l_name in enumerate(LAYER_NAMES)
        ]
        return active_names, masked_lots

    def execute_4layer_single_price(
        self,
        symbol: str,
        direction: str,
        price: float,
        sl: Optional[float] = None,
        final_tp: Optional[float] = None,
        raw_tp1: Optional[float] = None,
        raw_tp2: Optional[float] = None,
        raw_tp3: Optional[float] = None,
        raw_tp4: Optional[float] = None,
        raw_tp5: Optional[float] = None,
        tp_levels: Optional[Dict[str, float]] = None,
        order_intent: str = "AUTO",
        is_stop_order: bool = False,
        base_lots: Optional[Sequence[float]] = None,
        multiplier: Optional[float] = None,
        max_layers: Optional[int] = None,
        layers_subset: Optional[Sequence[str]] = None,
        exclude_tickets: Optional[Sequence[int]] = None,
        allow_duplicate_price: bool = False,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Execute a Single-Price entry (LIMIT, STOP, or smart AUTO).
        When SINGLE_PRICE_CONSOLIDATE_ORDERS is True and max_layers/layers_subset are omitted,
        consolidates to 1 clean order (L1) rather than stacking 4 identical orders at the same price.
        Returns (market_tickets, pending_tickets, layers_data).
        """
        self.last_dynamic_risk_snapshot = None
        if price is None or float(price) <= 0.0:
            self.last_error = f"Invalid single-price parameter (price={price})"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer single-price")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        eff_max_layers = max_layers
        if (
            eff_max_layers is None
            and layers_subset is None
            and bool(_cfg("SINGLE_PRICE_CONSOLIDATE_ORDERS", SINGLE_PRICE_CONSOLIDATE_ORDERS))
        ):
            eff_max_layers = 1

        active_names, eff_base_lots = self._resolve_active_layer_subset(
            base_lots=base_lots,
            max_layers=eff_max_layers,
            layers_subset=layers_subset,
        )
        eff_intent = (order_intent or "AUTO").strip().upper()
        if is_stop_order and eff_intent == "AUTO":
            eff_intent = "STOP"

        target_px = round(float(price), digits)
        if eff_intent != "STOP" and not is_stop_order:
            target_px = self._apply_limit_entry_front_run(
                symbol=resolved_sym,
                direction=direction_norm,
                target_price=target_px,
                final_tp=final_tp,
                raw_tp1=raw_tp1 or (tp_levels or {}).get("TP1"),
                pip_size=pip_size,
                digits=digits,
            )
        market_mask = [
            (l_name in active_names)
            and self._layer_will_execute_at_market(
                symbol=resolved_sym,
                direction=direction_norm,
                target_price=target_px,
                tick=tick,
                order_intent=eff_intent,
                allow_market_buffer=(l_name in ("L1", "TOP")),
            )
            for l_name in LAYER_NAMES
        ]
        alloc_lots, warn = self.allocate_4layer_lots(
            resolved_sym, base_lots=eff_base_lots, multiplier=multiplier, market_mask=market_mask
        )
        if sum(alloc_lots) <= 0.0:
            self.last_error = warn or "High Risk Lot Cap reached"
            return [], [], {}
        sl, layer_tps, _ = self._prepare_dynamic_sltp_inputs(
            symbol=resolved_sym,
            direction=direction_norm,
            sl_anchor=target_px,
            tp_anchor=target_px,
            sl=sl,
            final_tp=final_tp,
            raw_tp1=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels,
            pip_size=pip_size,
            digits=digits,
        )

        logger.info(
            f"Executing 4-Layer Single-Price ({eff_intent}) for {direction_norm} {resolved_sym}: "
            f"Price={target_px}, ActiveLayers={active_names}, Lots={alloc_lots}, TPs={layer_tps} | SL={sl}"
        )

        layer_specs = [
            (l_name, target_px, alloc_lots[idx], layer_tps[l_name])
            for idx, l_name in enumerate(LAYER_NAMES)
            if l_name in active_names
        ]
        return self._execute_4layer_specs(
            symbol=resolved_sym,
            direction=direction_norm,
            layer_specs=layer_specs,
            sl=sl,
            tick=tick,
            order_intent=eff_intent,
            entry_label=f"Single-{eff_intent}",
            enforce_risk_cap=False,
            exclude_tickets=exclude_tickets,
            allow_duplicate_price=allow_duplicate_price,
        )

    def execute_4layer_market(
        self,
        symbol: str,
        direction: str,
        sl: Optional[float] = None,
        final_tp: Optional[float] = None,
        raw_tp1: Optional[float] = None,
        raw_tp2: Optional[float] = None,
        raw_tp3: Optional[float] = None,
        raw_tp4: Optional[float] = None,
        raw_tp5: Optional[float] = None,
        tp_levels: Optional[Dict[str, float]] = None,
        base_lots: Optional[Sequence[float]] = None,
        multiplier: Optional[float] = None,
        max_layers: Optional[int] = None,
        layers_subset: Optional[Sequence[str]] = None,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Split an immediate Market Now entry into 4 market orders (L1..L4 -> TP1/TP2/TP3/TP5),
        or a restricted layer subset (e.g. max_layers=1 -> L1 only for unpriced stubs).
        Returns (market_tickets, pending_tickets, layers_data).
        """
        self.last_dynamic_risk_snapshot = None
        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer market")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        active_names, eff_base_lots = self._resolve_active_layer_subset(
            base_lots=base_lots,
            max_layers=max_layers,
            layers_subset=layers_subset,
        )
        alloc_lots, warn = self.allocate_4layer_lots(resolved_sym, base_lots=eff_base_lots, multiplier=multiplier)
        if sum(alloc_lots) <= 0.0:
            self.last_error = warn or "High Risk Lot Cap reached"
            return [], [], {}

        raw_entry = tick.ask if direction_norm == "BUY" else tick.bid
        entry_px = round(float(raw_entry), digits)

        sl, layer_tps, _ = self._prepare_dynamic_sltp_inputs(
            symbol=resolved_sym,
            direction=direction_norm,
            sl_anchor=entry_px,
            tp_anchor=entry_px,
            sl=sl,
            final_tp=final_tp,
            raw_tp1=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels,
            pip_size=pip_size,
            digits=digits,
        )

        logger.info(
            f"Executing 4-Layer Market for {direction_norm} {resolved_sym}: "
            f"Entry={entry_px}, ActiveLayers={active_names}, Lots={alloc_lots}, TPs={layer_tps} | SL={sl}"
        )

        layer_specs = [
            (l_name, entry_px, alloc_lots[idx], layer_tps[l_name])
            for idx, l_name in enumerate(LAYER_NAMES)
            if l_name in active_names
        ]
        return self._execute_4layer_specs(
            symbol=resolved_sym,
            direction=direction_norm,
            layer_specs=layer_specs,
            sl=sl,
            tick=tick,
            order_intent="MARKET",
            entry_label="Market",
            enforce_risk_cap=False,
        )

    def execute_single_price_order(
        self,
        symbol: str,
        direction: str,
        price: float,
        lot: float = 0.01,
        sl: Optional[float] = None,
        final_tp: Optional[float] = None,
        raw_tp1: Optional[float] = None,
        order_intent: str = "AUTO",
        is_stop_order: bool = False,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Legacy single-order placement at the specified single price using Smart Routing.
        Returns (market_tickets, pending_tickets, layer_dict).
        """
        if price is None or float(price) <= 0.0:
            self.last_error = f"Invalid single-price order parameters (direction={direction}, price={price})"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "single-price")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, _, _ = prereq

        eff_intent = (order_intent or "AUTO").strip().upper()
        if is_stop_order and eff_intent == "AUTO":
            eff_intent = "STOP"

        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"Executing Single-Price Order ({eff_intent}) for {direction_norm} {resolved_sym}: "
            f"Price={price}, Lot={lot}, TP={final_tp}, SL={sl}"
        )

        t1, o_type = self._execute_layer_price(
            symbol=resolved_sym,
            direction=direction_norm,
            target_price=price,
            lot=lot,
            sl=sl,
            tp=final_tp,
            tick=tick,
            market_comment="TG Single (Market)",
            limit_comment="TG Single (Limit)",
            order_intent=eff_intent,
            stop_comment="TG Single (Stop)",
        )
        if t1:
            if o_type == "MARKET":
                market_tickets.append(t1)
                layers_data["TOP"] = {
                    "lot": lot,
                    "target_price": price,
                    "tp": final_tp,
                    "tp_stage": "TP1",
                    "ticket": t1,
                    "order_type": "MARKET",
                    "status": "OPEN",
                }
            else:
                pending_tickets.append(t1)
                layers_data["TOP"] = {
                    "lot": lot,
                    "target_price": price,
                    "tp": final_tp,
                    "tp_stage": "TP1",
                    "ticket": t1,
                    "order_type": o_type,
                    "status": "PENDING",
                }

        return market_tickets, pending_tickets, layers_data

    def execute_3layer_zone(
        self,
        symbol: str,
        direction: str,
        zone: ZoneInfo,
        sl: Optional[float],
        final_tp: Optional[float],
        raw_tp1: Optional[float] = None,
        raw_tp2: Optional[float] = None,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Legacy 3-Layer Zone Placement (TOP/MID/BOTTOM).
        Retained for backward compatibility with direct legacy callers.
        """
        if zone is None:
            self.last_error = f"Invalid 3-layer zone parameters (direction={direction}, zone={zone})"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "3-layer zone")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        cfg_lots = _cfg("LAYER_LOTS", LAYER_LOTS)
        top_lot = cfg_lots.get("TOP", cfg_lots.get("L1", 0.01))
        mid_lot = cfg_lots.get("MID", cfg_lots.get("L2", 0.01))
        bottom_lot = cfg_lots.get("BOTTOM", cfg_lots.get("L4", 0.02))

        top_price = zone.top
        mid_price = zone.mid
        bottom_price = zone.bottom

        top_tp = final_tp
        step_dist = float(_cfg("DEFAULT_TP_STEP_PIPS", DEFAULT_TP_STEP_PIPS)) * pip_size
        if raw_tp2:
            runner_tp = round(raw_tp2, digits)
        elif raw_tp1:
            runner_tp = round(raw_tp1 + step_dist, digits) if direction_norm == "BUY" else round(raw_tp1 - step_dist, digits)
        else:
            runner_tp = final_tp

        mid_tp = runner_tp
        bottom_tp = runner_tp

        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"Executing 3-Layer Zone for {direction_norm} {resolved_sym}: "
            f"Top={top_price} ({top_lot}, TP={top_tp}), Mid={mid_price} ({mid_lot}, TP={mid_tp}), "
            f"Bottom={bottom_price} ({bottom_lot}, TP={bottom_tp}) | SL={sl}"
        )

        if direction_norm == "BUY":
            layer_specs = [
                ("TOP", top_price, top_lot, top_tp, "TG Layer 1 (Top Market)", "TG Layer 1 (Top Limit)"),
                ("MID", mid_price, mid_lot, mid_tp, "TG Layer 2 (Mid Market)", "TG Layer 2 (Mid Limit)"),
                ("BOTTOM", bottom_price, bottom_lot, bottom_tp, "TG Layer 3 (Bottom Market)", "TG Layer 3 (Bottom Limit)"),
            ]
        else:
            layer_specs = [
                ("TOP", bottom_price, top_lot, top_tp, "TG Layer 1 (Bottom Market)", "TG Layer 1 (Bottom Limit)"),
                ("MID", mid_price, mid_lot, mid_tp, "TG Layer 2 (Mid Market)", "TG Layer 2 (Mid Limit)"),
                ("BOTTOM", top_price, bottom_lot, bottom_tp, "TG Layer 3 (Top Market)", "TG Layer 3 (Top Limit)"),
            ]

        for l_name, target_px, l_lot, l_tp, mk_comment, lim_comment in layer_specs:
            t, o_type = self._execute_layer_price(
                symbol=resolved_sym,
                direction=direction_norm,
                target_price=target_px,
                lot=l_lot,
                sl=sl,
                tp=l_tp,
                tick=tick,
                market_comment=mk_comment,
                limit_comment=lim_comment,
            )
            if t:
                if o_type == "MARKET":
                    market_tickets.append(t)
                    layers_data[l_name] = {
                        "lot": l_lot, "target_price": target_px, "ticket": t, "order_type": "MARKET", "status": "OPEN",
                    }
                else:
                    pending_tickets.append(t)
                    layers_data[l_name] = {
                        "lot": l_lot, "target_price": target_px, "ticket": t, "order_type": "LIMIT", "status": "PENDING",
                    }

        return market_tickets, pending_tickets, layers_data

    # Compatibility alias for 3tier
    execute_3tier_zone = execute_3layer_zone

    def modify_position_sltp(
        self,
        ticket: int,
        sl: Optional[float],
        tp: Optional[float],
        require_both: bool = False,
    ) -> bool:
        """
        Modify SL and TP of an open MT5 market position (thread-safe).
        When `require_both=True`, independent single-leg fallback is disabled so a
        failed SL write never silently applies a TP lift.
        """
        self.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": None}
        if not ticket:
            return False
        if self.dry_run:
            logger.info(f"[DRY-RUN] Modified Position #{ticket}: SL={sl}, TP={tp}")
            self.last_modify_result = {
                "ok": True,
                "sl_ok": sl is not None,
                "tp_ok": tp is not None,
                "retcode": 10009,
            }
            return True

        with self._mt5_lock:
            if not self._ensure_connected():
                logger.error(f"Cannot modify position #{ticket}: MT5 is not connected.")
                return False

            pos = mt5.positions_get(ticket=ticket)
            if not pos:
                logger.warning(f"Position #{ticket} not found for SL/TP modification.")
                return False
            p = pos[0]

            raw_sym = getattr(p, "symbol", None)
            p_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
            sym_info = self.get_symbol_info(p_symbol)
            digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

            cur_sl = round(_num(getattr(p, "sl", 0.0), 0.0), digits)
            cur_tp = round(_num(getattr(p, "tp", 0.0), 0.0), digits)
            new_sl = round(float(sl), digits) if sl is not None else cur_sl
            new_tp = round(float(tp), digits) if tp is not None else cur_tp

            sl_changed = abs(new_sl - cur_sl) > 1e-6
            tp_changed = abs(new_tp - cur_tp) > 1e-6
            if not sl_changed and not tp_changed:
                logger.debug(f"Position #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No-op).")
                self.last_modify_result = {"ok": True, "sl_ok": True, "tp_ok": True, "retcode": 10025}
                return True

            if sym_info is not None and _num(getattr(sym_info, "trade_freeze_level", 0.0), 0.0) > 0.0:
                tick_f = self.get_tick(p_symbol)
                frozen_sl = sl_changed and (
                    (cur_sl > 0.0 and self.is_within_freeze_level(p_symbol, cur_sl, tick_f, sym_info))
                    or self.is_within_freeze_level(p_symbol, new_sl, tick_f, sym_info)
                )
                frozen_tp = tp_changed and (
                    (cur_tp > 0.0 and self.is_within_freeze_level(p_symbol, cur_tp, tick_f, sym_info))
                    or self.is_within_freeze_level(p_symbol, new_tp, tick_f, sym_info)
                )
                if frozen_sl or frozen_tp:
                    self.last_error = "10029 TRADE_RETCODE_FROZEN"
                    self.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": 10029}
                    logger.debug(f"Position #{ticket} SL/TP modify skipped: within broker trade_freeze_level.")
                    return False

            request = {
                "action": _mt5_const("TRADE_ACTION_SLTP", 6),
                "position": ticket,
                "symbol": p_symbol,
                "sl": new_sl,
                "tp": new_tp,
            }

            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            no_changes_code = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)
            frozen_code = _mt5_const("TRADE_RETCODE_FROZEN", 10029)

            def _is_mod_ok(res: Any) -> bool:
                if not res:
                    return False
                if res.retcode in (done_code, no_changes_code):
                    return True
                return str(getattr(res, "comment", "")).strip().lower() == "no changes"

            result = mt5.order_send(request)
            retcode = getattr(result, "retcode", None) if result else None
            if result and result.retcode == done_code:
                logger.info(f"Updated SL/TP on Position #{ticket}: SL={new_sl}, TP={new_tp}")
                self.last_modify_result = {"ok": True, "sl_ok": True, "tp_ok": True, "retcode": retcode}
                return True
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            if _is_mod_ok(result):
                logger.debug(f"Position #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
                self.last_modify_result = {"ok": True, "sl_ok": True, "tp_ok": True, "retcode": retcode}
                return True

            if retcode == frozen_code:
                self.last_error = str(comment or "10029 Frozen")
                self.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": frozen_code}
                logger.warning(f"Position #{ticket} SL/TP modify frozen by broker (10029): {comment}")
                return False

            # Independent leg fallback ONLY when not require_both and both legs were requested
            if (not require_both) and sl is not None and tp is not None:
                tp_applied = False
                sl_applied = False
                eff_tp = cur_tp
                if tp_changed:
                    res_tp = mt5.order_send({
                        "action": _mt5_const("TRADE_ACTION_SLTP", 6),
                        "position": ticket,
                        "symbol": p_symbol,
                        "sl": cur_sl,
                        "tp": new_tp,
                    })
                    if _is_mod_ok(res_tp):
                        tp_applied = True
                        eff_tp = new_tp
                        logger.info(
                            f"Updated TP independently on Position #{ticket}: TP={new_tp} "
                            f"(combined SL/TP rejected: {comment})"
                        )
                if sl_changed:
                    res_sl = mt5.order_send({
                        "action": _mt5_const("TRADE_ACTION_SLTP", 6),
                        "position": ticket,
                        "symbol": p_symbol,
                        "sl": new_sl,
                        "tp": eff_tp,
                    })
                    if _is_mod_ok(res_sl):
                        sl_applied = True
                        logger.info(
                            f"Updated SL independently on Position #{ticket}: SL={new_sl} "
                            f"(combined SL/TP rejected: {comment})"
                        )
                if tp_applied or sl_applied:
                    self.last_modify_result = {
                        "ok": True,
                        "sl_ok": sl_applied,
                        "tp_ok": tp_applied,
                        "retcode": retcode,
                    }
                    return True

            self.last_error = str(comment)
            self.last_modify_result = {"ok": False, "sl_ok": False, "tp_ok": False, "retcode": retcode}
            logger.error(f"Failed to update SL/TP on Position #{ticket}: {comment}")
            return False

    def modify_pending_order_sltp(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        """Modify SL and TP of a pending MT5 order (thread-safe)."""
        if not ticket:
            return False
        if self.dry_run:
            dry_orders = getattr(self, "_dry_run_pending_orders", None)
            if isinstance(dry_orders, dict) and ticket in dry_orders:
                rec = dry_orders[ticket]
                ord_px = _num(rec.get("price"), 0.0)
                rec_dir = str(rec.get("direction") or "").strip().upper()
                rec_sym = str(rec.get("symbol") or "").upper()
                if sl is not None:
                    rec["sl"] = round(float(sl), 2)
                if tp is not None:
                    cand_tp = round(float(tp), 2)
                    max_tp_dist = float(_cfg("MAX_TP_DISTANCE_DOLLARS", MAX_TP_DISTANCE_DOLLARS))
                    tp_bad = False
                    if ord_px > 0.0 and cand_tp > 0.0:
                        if rec_dir == "BUY" and cand_tp <= ord_px:
                            tp_bad = True
                        elif rec_dir == "SELL" and cand_tp >= ord_px:
                            tp_bad = True
                        elif ("XAU" in rec_sym or "GOLD" in rec_sym) and abs(cand_tp - ord_px) > max_tp_dist:
                            tp_bad = True
                    if not tp_bad:
                        rec["tp"] = cand_tp
                    else:
                        logger.warning(
                            f"[DRY-RUN] Pending Order #{ticket} ({rec_dir} @ {ord_px}) rejected invalid TP {cand_tp}."
                        )
            logger.info(f"[DRY-RUN] Modified Pending Order #{ticket}: SL={sl}, TP={tp}")
            return True

        with self._mt5_lock:
            if not self._ensure_connected():
                logger.error(f"Cannot modify pending order #{ticket}: MT5 is not connected.")
                return False

            orders = mt5.orders_get(ticket=ticket)
            if not orders:
                logger.warning(f"Pending order #{ticket} not found for modification.")
                return False
            order = orders[0]

            raw_sym = getattr(order, "symbol", None)
            o_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
            sym_info = self.get_symbol_info(o_symbol)
            digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

            cur_sl = round(_num(getattr(order, "sl", 0.0), 0.0), digits)
            cur_tp = round(_num(getattr(order, "tp", 0.0), 0.0), digits)
            new_sl = round(float(sl), digits) if sl is not None else cur_sl
            new_tp = round(float(tp), digits) if tp is not None else cur_tp

            ord_price = _num(getattr(order, "price_open", 0.0), 0.0)
            o_type = _int_num(getattr(order, "type", -1), -1)
            buy_limit = _mt5_const("ORDER_TYPE_BUY_LIMIT", 2)
            sell_limit = _mt5_const("ORDER_TYPE_SELL_LIMIT", 3)
            buy_stop = _mt5_const("ORDER_TYPE_BUY_STOP", 4)
            sell_stop = _mt5_const("ORDER_TYPE_SELL_STOP", 5)
            if ord_price > 0.0 and new_tp > 0.0:
                max_tp_dist = float(_cfg("MAX_TP_DISTANCE_DOLLARS", MAX_TP_DISTANCE_DOLLARS))
                is_gold = "XAU" in o_symbol.upper() or "GOLD" in o_symbol.upper()
                tp_bad = False
                if o_type in (buy_limit, buy_stop) and new_tp <= ord_price:
                    tp_bad = True
                elif o_type in (sell_limit, sell_stop) and new_tp >= ord_price:
                    tp_bad = True
                elif is_gold and abs(new_tp - ord_price) > max_tp_dist:
                    tp_bad = True
                if tp_bad:
                    logger.warning(
                        f"Pending Order #{ticket} ({o_symbol}, type={o_type}, price={ord_price}) "
                        f"rejected invalid TP {new_tp}; preserving cur_tp={cur_tp}."
                    )
                    cur_tp_bad = (
                        (o_type in (buy_limit, buy_stop) and cur_tp > 0.0 and cur_tp <= ord_price)
                        or (o_type in (sell_limit, sell_stop) and cur_tp > 0.0 and cur_tp >= ord_price)
                    )
                    new_tp = 0.0 if cur_tp_bad else cur_tp

            if abs(new_sl - cur_sl) <= 1e-6 and abs(new_tp - cur_tp) <= 1e-6:
                logger.debug(f"Pending Order #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No-op).")
                return True
            ord_time = _int_num(getattr(order, "type_time", 0), _mt5_const("ORDER_TIME_GTC", 0))
            ord_fill = _int_num(getattr(order, "type_filling", 0), _mt5_const("ORDER_FILLING_RETURN", 2))

            request = {
                "action": _mt5_const("TRADE_ACTION_MODIFY", 7),
                "order": ticket,
                "price": ord_price,
                "sl": new_sl,
                "tp": new_tp,
                "type_time": ord_time,
                "type_filling": ord_fill,
            }

            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            no_changes_code = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)

            def _is_ord_ok(res: Any) -> bool:
                if not res:
                    return False
                if res.retcode in (done_code, no_changes_code):
                    return True
                return str(getattr(res, "comment", "")).strip().lower() == "no changes"

            result = mt5.order_send(request)
            if result and result.retcode == done_code:
                logger.info(f"Updated SL/TP on Pending Order #{ticket}: SL={new_sl}, TP={new_tp}")
                return True
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            if _is_ord_ok(result):
                logger.debug(f"Pending Order #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
                return True

            if sl is not None and tp is not None:
                tp_applied = False
                sl_applied = False
                eff_tp = cur_tp
                if abs(new_tp - cur_tp) > 1e-6:
                    res_tp = mt5.order_send({
                        "action": _mt5_const("TRADE_ACTION_MODIFY", 7),
                        "order": ticket,
                        "price": ord_price,
                        "sl": cur_sl,
                        "tp": new_tp,
                        "type_time": ord_time,
                        "type_filling": ord_fill,
                    })
                    if _is_ord_ok(res_tp):
                        tp_applied = True
                        eff_tp = new_tp
                        logger.info(
                            f"Updated TP independently on Pending Order #{ticket}: TP={new_tp} "
                            f"(combined SL/TP rejected: {comment})"
                        )
                if abs(new_sl - cur_sl) > 1e-6:
                    res_sl = mt5.order_send({
                        "action": _mt5_const("TRADE_ACTION_MODIFY", 7),
                        "order": ticket,
                        "price": ord_price,
                        "sl": new_sl,
                        "tp": eff_tp,
                        "type_time": ord_time,
                        "type_filling": ord_fill,
                    })
                    if _is_ord_ok(res_sl):
                        sl_applied = True
                        logger.info(
                            f"Updated SL independently on Pending Order #{ticket}: SL={new_sl} "
                            f"(combined SL/TP rejected: {comment})"
                        )
                if tp_applied or sl_applied:
                    return True

            self.last_error = str(comment)
            logger.error(f"Failed to update SL/TP on Pending Order #{ticket}: {comment}")
            return False

    def modify_pending_order_price_sltp(
        self,
        ticket: int,
        price: float,
        sl: Optional[float],
        tp: Optional[float],
    ) -> bool:
        """
        Modify price, SL, and TP of an unfilled pending order in-place via TRADE_ACTION_MODIFY.
        Pre-validates live tick + order type + trade_stops_level, and NEVER falls back to cur_price.
        """
        t_int = _int_num(ticket, 0)
        if t_int <= 0 or price is None or float(price) <= 0.0:
            return False
        if self.dry_run:
            dry_orders = getattr(self, "_dry_run_pending_orders", None)
            if isinstance(dry_orders, dict) and t_int in dry_orders:
                rec = dry_orders[t_int]
                rec["price"] = round(float(price), 2)
                if sl is not None:
                    rec["sl"] = round(float(sl), 2)
                if tp is not None:
                    rec["tp"] = round(float(tp), 2)
            logger.info(f"[DRY-RUN] Modified Pending Order #{t_int}: Price={price}, SL={sl}, TP={tp}")
            return True

        with self._mt5_lock:
            if not self._ensure_connected():
                logger.error(f"Cannot modify pending order #{t_int}: MT5 is not connected.")
                return False

            orders = mt5.orders_get(ticket=t_int)
            if not orders:
                logger.debug(f"Pending order #{t_int} not found for in-place price/SL/TP modify.")
                return False
            order = orders[0]

            raw_sym = getattr(order, "symbol", None)
            o_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
            sym_info = self.get_symbol_info(o_symbol)
            digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
            point = _num(getattr(sym_info, "point", 0.01), 0.01) or 0.01
            stops_lvl = _int_num(getattr(sym_info, "trade_stops_level", 0), 0) if sym_info else 0
            min_dist = max(0.0, float(stops_lvl) * point)

            cur_price = round(_num(getattr(order, "price_open", 0.0), 0.0), digits)
            cur_sl = round(_num(getattr(order, "sl", 0.0), 0.0), digits)
            cur_tp = round(_num(getattr(order, "tp", 0.0), 0.0), digits)
            new_price = round(float(price), digits)
            new_sl = round(float(sl), digits) if sl is not None else cur_sl
            new_tp = round(float(tp), digits) if tp is not None else cur_tp

            if abs(new_price - cur_price) <= 1e-6 and abs(new_sl - cur_sl) <= 1e-6 and abs(new_tp - cur_tp) <= 1e-6:
                logger.debug(f"Pending Order #{t_int} already at Price={new_price}, SL={new_sl}, TP={new_tp} (No-op).")
                return True

            o_type = _int_num(getattr(order, "type", -1), -1)
            buy_limit = _mt5_const("ORDER_TYPE_BUY_LIMIT", 2)
            sell_limit = _mt5_const("ORDER_TYPE_SELL_LIMIT", 3)
            buy_stop = _mt5_const("ORDER_TYPE_BUY_STOP", 4)
            sell_stop = _mt5_const("ORDER_TYPE_SELL_STOP", 5)

            tick = mt5.symbol_info_tick(o_symbol)
            if tick is not None:
                ask = getattr(tick, "ask", None)
                bid = getattr(tick, "bid", None)
                if o_type == buy_limit and isinstance(ask, (int, float)) and ask > 0 and new_price >= ask - min_dist:
                    return False
                if o_type == sell_limit and isinstance(bid, (int, float)) and bid > 0 and new_price <= bid + min_dist:
                    return False
                if o_type == buy_stop and isinstance(ask, (int, float)) and ask > 0 and new_price <= ask + min_dist:
                    return False
                if o_type == sell_stop and isinstance(bid, (int, float)) and bid > 0 and new_price >= bid - min_dist:
                    return False

            if o_type in (buy_limit, buy_stop):
                if new_sl > 0.0 and new_sl >= new_price - min_dist:
                    return False
                if new_tp > 0.0 and new_tp <= new_price + min_dist:
                    return False
            elif o_type in (sell_limit, sell_stop):
                if new_sl > 0.0 and new_sl <= new_price + min_dist:
                    return False
                if new_tp > 0.0 and new_tp >= new_price - min_dist:
                    return False

            ord_time = _int_num(getattr(order, "type_time", 0), _mt5_const("ORDER_TIME_GTC", 0))
            ord_fill = _int_num(getattr(order, "type_filling", 0), _mt5_const("ORDER_FILLING_RETURN", 2))

            request = {
                "action": _mt5_const("TRADE_ACTION_MODIFY", 7),
                "order": t_int,
                "price": new_price,
                "sl": new_sl,
                "tp": new_tp,
                "type_time": ord_time,
                "type_filling": ord_fill,
            }
            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            no_changes_code = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)
            result = mt5.order_send(request)
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            if result and (result.retcode in (done_code, no_changes_code) or str(comment).strip().lower() == "no changes"):
                logger.info(
                    f"Modified Pending Order #{t_int} in-place: Price={new_price}, SL={new_sl}, TP={new_tp}"
                )
                return True

            self.last_error = str(comment)
            logger.info(f"In-place modify failed for Pending Order #{t_int} (Price={new_price}): {comment}")
            return False

    def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel an unfilled pending limit/stop order (thread-safe)."""
        if not ticket:
            return False
        if self.dry_run:
            self._dry_run_pending_ticket_lots.pop(ticket, None)
            if isinstance(getattr(self, "_dry_run_pending_orders", None), dict):
                self._dry_run_pending_orders.pop(ticket, None)
            self._dry_run_ticket_lots.pop(ticket, None)
            logger.info(f"[DRY-RUN] Cancelled Pending Order #{ticket}")
            return True

        with self._mt5_lock:
            if not self._ensure_connected():
                logger.error(f"Cannot cancel pending order #{ticket}: MT5 is not connected.")
                return False

            try:
                filled_pos = mt5.positions_get(ticket=ticket)
            except Exception:
                filled_pos = None
            if isinstance(filled_pos, (list, tuple)) and len(filled_pos) > 0:
                logger.info(f"Pending order #{ticket} already filled into an open position; skipping cancel.")
                return False

            request = {
                "action": _mt5_const("TRADE_ACTION_REMOVE", 8),
                "order": ticket,
            }

            result = mt5.order_send(request)
            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            if result and result.retcode == done_code:
                if isinstance(getattr(self, "_dry_run_pending_orders", None), dict):
                    self._dry_run_pending_orders.pop(ticket, None)
                logger.info(f"Successfully cancelled Pending Order #{ticket}")
                return True
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            logger.info(f"Order #{ticket} not cancelled (already filled or removed): {comment}")
            return False

    def close_market_position(self, ticket: int) -> bool:
        """
        Close an open MT5 position completely at current market price (thread-safe).
        Guarded by an atomic in-flight `_closing_tickets` lock (`is_closing = True`) so overlapping
        rules or rapid ticks never send duplicate market-close orders for the same ticket.
        """
        t_int = _int_num(ticket, 0)
        if t_int <= 0:
            return False

        with self._closing_lock:
            if t_int in self._closing_tickets:
                logger.warning(
                    f"Atomic exit lock: duplicate concurrent close skipped for Ticket #{t_int} (is_closing=True)."
                )
                return False
            self._closing_tickets.add(t_int)

        try:
            if self.dry_run:
                self._dry_run_ticket_lots.pop(ticket, None)
                logger.info(f"[DRY-RUN] Closed Market Position #{ticket}")
                self._cached_pos_profits[t_int] = 1.25
                self._bot_closed_tickets.add(t_int)
                self.invalidate_realized_pnl_cache()
                return True

            with self._mt5_lock:
                if not self._ensure_connected():
                    logger.error(f"Cannot close position #{ticket}: MT5 is not connected.")
                    return False

                pos = mt5.positions_get(ticket=ticket)
                if not pos:
                    logger.warning(f"Position #{ticket} not found to close (already closed).")
                    return False
                p = pos[0]
                self._cached_pos_profits[t_int] = round(
                    _num(getattr(p, "profit", 0.0)) + _num(getattr(p, "swap", 0.0)),
                    2,
                )

                raw_sym = getattr(p, "symbol", None)
                p_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
                tick = self.get_tick(p_symbol)
                if not tick:
                    return False

                buy_order_type = _mt5_const("ORDER_TYPE_BUY", 0)
                sell_order_type = _mt5_const("ORDER_TYPE_SELL", 1)
                is_buy_pos = _int_num(getattr(p, "type", buy_order_type), buy_order_type) == buy_order_type
                close_type = sell_order_type if is_buy_pos else buy_order_type
                close_price = getattr(tick, "bid", None) if is_buy_pos else getattr(tick, "ask", None)
                if not isinstance(close_price, (int, float)) or isinstance(close_price, bool) or close_price <= 0:
                    logger.warning(f"Invalid close tick price ({close_price}) for #{ticket} ({p_symbol})")
                    return False

                vol = _num(getattr(p, "volume", 0.01), 0.01) or 0.01
                request = {
                    "action": _mt5_const("TRADE_ACTION_DEAL", 1),
                    "position": ticket,
                    "symbol": p_symbol,
                    "volume": vol,
                    "type": close_type,
                    "price": close_price,
                    "deviation": 20,
                    "magic": _cfg("MAGIC_NUMBER", MAGIC_NUMBER),
                    "comment": "TG Copier Close",
                    "type_time": _mt5_const("ORDER_TIME_GTC", 0),
                }

                success, _ = self.send_order_with_filling_fallback(request)
                if success:
                    self._bot_closed_tickets.add(t_int)
                    if len(self._bot_closed_tickets) > 2000:
                        self._bot_closed_tickets = set(list(self._bot_closed_tickets)[-500:])
                    self.invalidate_realized_pnl_cache()
                    logger.info(f"Closed Position #{ticket} ({p_symbol} {vol} lots at {close_price})")
                    return True
                return False
        finally:
            with self._closing_lock:
                self._closing_tickets.discard(t_int)

    def close_partial_volume(
        self,
        ticket: Optional[int],
        close_volume: float,
        current_volume: Optional[float] = None,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Safely close a partial volume of an open position without ever violating
        broker volume_min or turning an indivisible 0.01 lot into a full close (FM-4).
        """
        t_int = _int_num(ticket, 0)
        if t_int <= 0 or close_volume is None or float(close_volume) <= 0.0:
            return {"ok": False, "skipped_indivisible": False, "reason": "invalid_args"}

        with self._closing_lock:
            if t_int in self._closing_tickets:
                return {"ok": False, "skipped_indivisible": False, "reason": "already_closing"}
            self._closing_tickets.add(t_int)

        try:
            eff_sym = symbol or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
            sym_info = self.get_symbol_info(eff_sym)
            vol_min, _, vol_step, dec = _extract_symbol_volume_limits(sym_info)

            if self.dry_run:
                cur_vol = _num(
                    current_volume if current_volume is not None else self._dry_run_ticket_lots.get(t_int),
                    0.0,
                )
                if cur_vol <= 0.0:
                    cur_vol = round(float(close_volume) * 2.0, dec)
                if cur_vol + 1e-9 < 2.0 * vol_min:
                    return {"ok": False, "skipped_indivisible": True, "remaining_volume": round(cur_vol, dec)}
                eps = max(1e-12, vol_step * 1e-7)
                c_vol = round(math.floor((float(close_volume) + eps) / vol_step) * vol_step, dec)
                rem = round(cur_vol - c_vol, dec)
                if c_vol + 1e-9 < vol_min or rem + 1e-9 < vol_min:
                    return {"ok": False, "skipped_indivisible": True, "remaining_volume": round(cur_vol, dec)}
                if t_int in self._dry_run_ticket_lots:
                    self._dry_run_ticket_lots[t_int] = rem
                self.invalidate_realized_pnl_cache()
                logger.info(f"[DRY-RUN] Partial close Position #{t_int}: closed {c_vol} lots, remaining={rem}")
                return {
                    "ok": True,
                    "dry_run": True,
                    "closed_volume": c_vol,
                    "remaining_volume": rem,
                    "retcode": 10009,
                }

            with self._mt5_lock:
                if not self._ensure_connected():
                    return {"ok": False, "skipped_indivisible": False, "reason": "disconnected"}
                pos = mt5.positions_get(ticket=t_int)
                if not pos:
                    return {"ok": False, "skipped_indivisible": False, "reason": "not_found"}
                p = pos[0]
                raw_sym = getattr(p, "symbol", None)
                p_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else eff_sym
                sym_info = self.get_symbol_info(p_symbol)
                vol_min, _, vol_step, dec = _extract_symbol_volume_limits(sym_info)
                cur_vol = _num(getattr(p, "volume", 0.0), 0.0)
                if cur_vol + 1e-9 < 2.0 * vol_min:
                    logger.info(
                        f"Skipping partial close on #{t_int}: volume {cur_vol} < 2 * volume_min ({2*vol_min:.2f})."
                    )
                    return {"ok": False, "skipped_indivisible": True, "remaining_volume": round(cur_vol, dec)}

                eps = max(1e-12, vol_step * 1e-7)
                c_vol = round(math.floor((float(close_volume) + eps) / vol_step) * vol_step, dec)
                rem = round(cur_vol - c_vol, dec)
                if c_vol + 1e-9 < vol_min or rem + 1e-9 < vol_min:
                    logger.info(
                        f"Skipping partial close on #{t_int}: close={c_vol}, remaining={rem}, volume_min={vol_min}."
                    )
                    return {"ok": False, "skipped_indivisible": True, "remaining_volume": round(cur_vol, dec)}

                tick = self.get_tick(p_symbol)
                if not tick:
                    return {"ok": False, "skipped_indivisible": False, "reason": "no_tick"}

                buy_order_type = _mt5_const("ORDER_TYPE_BUY", 0)
                sell_order_type = _mt5_const("ORDER_TYPE_SELL", 1)
                is_buy_pos = _int_num(getattr(p, "type", buy_order_type), buy_order_type) == buy_order_type
                close_type = sell_order_type if is_buy_pos else buy_order_type
                close_price = getattr(tick, "bid", None) if is_buy_pos else getattr(tick, "ask", None)
                if not isinstance(close_price, (int, float)) or isinstance(close_price, bool) or close_price <= 0:
                    return {"ok": False, "skipped_indivisible": False, "reason": "bad_price"}

                if self.is_within_freeze_level(p_symbol, float(close_price), tick, sym_info):
                    return {
                        "ok": False,
                        "skipped_indivisible": False,
                        "remaining_volume": round(cur_vol, dec),
                        "retcode": _mt5_const("TRADE_RETCODE_FROZEN", 10029),
                        "reason": "frozen",
                    }

                request = {
                    "action": _mt5_const("TRADE_ACTION_DEAL", 1),
                    "position": t_int,
                    "symbol": p_symbol,
                    "volume": c_vol,
                    "type": close_type,
                    "price": float(close_price),
                    "deviation": 20,
                    "magic": _cfg("MAGIC_NUMBER", MAGIC_NUMBER),
                    "comment": "TG Copier Partial",
                    "type_time": _mt5_const("ORDER_TIME_GTC", 0),
                }
                success, res = self.send_order_with_filling_fallback(request)
                retcode = getattr(res, "retcode", None) if res else None
                if success and res and retcode == _mt5_const("TRADE_RETCODE_DONE", 10009):
                    self.invalidate_realized_pnl_cache()
                    deal_id = _int_num(getattr(res, "deal", 0), 0)
                    deal_px = _num(getattr(res, "price", 0.0), 0.0) or float(close_price)
                    logger.info(
                        f"Partial closed #{t_int} ({p_symbol}): {c_vol} lots @ {deal_px}, remaining={rem}"
                    )
                    return {
                        "ok": True,
                        "skipped_indivisible": False,
                        "closed_volume": c_vol,
                        "remaining_volume": rem,
                        "deal_ticket": deal_id or None,
                        "price": deal_px,
                        "retcode": retcode,
                    }
                return {
                    "ok": False,
                    "skipped_indivisible": False,
                    "remaining_volume": round(cur_vol, dec),
                    "retcode": retcode,
                    "reason": self.last_error or "partial_close_failed",
                }
        finally:
            with self._closing_lock:
                self._closing_tickets.discard(t_int)

    def _compute_close_info_from_deals(self, ticket: int, deals: Any) -> Dict[str, Any]:
        """
        Compute rich deal-history attribution for a closed/partially-closed position:
        {profit, gross_profit, commission, swap, fee, reason, exit_price, entry_price,
         closed_volume, close_time, exit_deal_tickets, exit_deals, deals_settled}.
        """
        t_int = _int_num(ticket, 0)
        reason = "UNKNOWN"
        if deals:
            in_entry = _mt5_const("DEAL_ENTRY_IN", 0)
            out_entries = {
                _mt5_const("DEAL_ENTRY_OUT", 1),
                _mt5_const("DEAL_ENTRY_INOUT", 2),
                _mt5_const("DEAL_ENTRY_OUT_BY", 3),
            }
            in_deals = [d for d in deals if _int_num(getattr(d, "entry", -1), -1) == in_entry]
            exit_deals_raw = [d for d in deals if _int_num(getattr(d, "entry", -1), -1) in out_entries]
            if exit_deals_raw:
                total_gross = sum(_num(getattr(d, "profit", 0.0)) for d in deals)
                total_comm = sum(_num(getattr(d, "commission", 0.0)) for d in deals)
                total_swap = sum(_num(getattr(d, "swap", 0.0)) for d in deals)
                total_fee = sum(_num(getattr(d, "fee", 0.0)) for d in deals)
                net_profit = total_gross + total_comm + total_swap + total_fee
                profit = round(net_profit, 2)

                sl_const = _mt5_const("DEAL_REASON_SL", 4)
                tp_const = _mt5_const("DEAL_REASON_TP", 5)

                exit_deals_info: List[Dict[str, Any]] = []
                total_exit_vol = 0.0
                weighted_exit_px = 0.0
                exit_deal_tickets: List[int] = []
                for ed in exit_deals_raw:
                    dtkt = _int_num(getattr(ed, "ticket", 0), 0)
                    if dtkt > 0:
                        exit_deal_tickets.append(dtkt)
                    ev = max(0.0, _num(getattr(ed, "volume", 0.0), 0.0))
                    epx = _num(getattr(ed, "price", 0.0), 0.0)
                    if ev > 0.0 and epx > 0.0:
                        weighted_exit_px += ev * epx
                        total_exit_vol += ev
                    ed_gross = _num(getattr(ed, "profit", 0.0))
                    ed_comm = _num(getattr(ed, "commission", 0.0))
                    ed_swap = _num(getattr(ed, "swap", 0.0))
                    ed_fee = _num(getattr(ed, "fee", 0.0))
                    ed_net = round(ed_gross + ed_comm + ed_swap + ed_fee, 2)
                    ed_raw_r = getattr(ed, "reason", None)
                    ed_reason = "SL" if ed_raw_r == sl_const else ("TP" if ed_raw_r == tp_const else "MANUAL/BOT")
                    exit_deals_info.append({
                        "deal_ticket": dtkt or None,
                        "profit": ed_net,
                        "gross_profit": round(ed_gross, 2),
                        "commission": round(ed_comm, 2),
                        "swap": round(ed_swap, 2),
                        "fee": round(ed_fee, 2),
                        "price": round(epx, 4) if epx > 0.0 else None,
                        "volume": round(ev, 4) if ev > 0.0 else None,
                        "reason": ed_reason,
                        "time": _int_num(getattr(ed, "time", 0), 0) or None,
                    })

                if exit_deals_info and in_deals:
                    in_costs = sum(
                        _num(getattr(id_d, "profit", 0.0))
                        + _num(getattr(id_d, "commission", 0.0))
                        + _num(getattr(id_d, "swap", 0.0))
                        + _num(getattr(id_d, "fee", 0.0))
                        for id_d in in_deals
                    )
                    if abs(in_costs) > 1e-9:
                        exit_deals_info[0]["profit"] = round(exit_deals_info[0]["profit"] + in_costs, 2)

                d = exit_deals_raw[-1]
                d_reason = getattr(d, "reason", None)
                if d_reason == sl_const:
                    reason = "SL"
                elif d_reason == tp_const:
                    reason = "TP"
                else:
                    reason = "MANUAL/BOT"

                last_epx = _num(getattr(d, "price", 0.0), 0.0)
                exit_price = (
                    round(weighted_exit_px / total_exit_vol, 4)
                    if total_exit_vol > 0.0 and weighted_exit_px > 0.0
                    else (round(last_epx, 4) if last_epx > 0.0 else None)
                )
                entry_price: Optional[float] = None
                if in_deals:
                    in_px = _num(getattr(in_deals[-1], "price", 0.0), 0.0)
                    if in_px > 0.0:
                        entry_price = round(in_px, 4)

                return {
                    "profit": profit,
                    "gross_profit": round(total_gross, 2),
                    "commission": round(total_comm, 2),
                    "swap": round(total_swap, 2),
                    "fee": round(total_fee, 2),
                    "reason": reason,
                    "exit_price": exit_price,
                    "entry_price": entry_price,
                    "closed_volume": round(total_exit_vol, 4) if total_exit_vol > 0.0 else None,
                    "close_time": _int_num(getattr(d, "time", 0), 0) or None,
                    "exit_deal_tickets": exit_deal_tickets,
                    "exit_deals": exit_deals_info,
                    "deals_settled": True,
                }

        fallback_reason = "MANUAL/BOT" if t_int in getattr(self, "_bot_closed_tickets", set()) else "UNKNOWN"
        return {
            "profit": self._cached_pos_profits.get(t_int, 0.0),
            "gross_profit": self._cached_pos_profits.get(t_int, 0.0),
            "commission": 0.0,
            "swap": 0.0,
            "fee": 0.0,
            "reason": fallback_reason,
            "exit_price": None,
            "entry_price": None,
            "closed_volume": None,
            "close_time": None,
            "exit_deal_tickets": [],
            "exit_deals": [],
            "deals_settled": False,
        }

    def get_position_close_info(self, ticket: Optional[int]) -> Dict[str, Any]:
        """
        Return `{"profit": float, "reason": str}` (preserving the strict 2-key contract)
        while populating full deal-level attribution (`exit_price`, `entry_price`, `exit_deals`,
        `deals_settled`, etc.) in `self._last_close_details[ticket]`.
        """
        t_int = _int_num(ticket, 0)
        if t_int <= 0:
            return {"profit": 0.0, "reason": "UNKNOWN"}

        if self.dry_run or not self._connected:
            rich = {
                "profit": self._cached_pos_profits.get(t_int, 1.25),
                "reason": "UNKNOWN",
                "deals_settled": True,
                "exit_price": None,
                "entry_price": None,
                "exit_deal_tickets": [],
                "exit_deals": [],
            }
            self._last_close_details[t_int] = rich
            return {"profit": rich["profit"], "reason": rich["reason"]}

        out_entries = {
            _mt5_const("DEAL_ENTRY_OUT", 1),
            _mt5_const("DEAL_ENTRY_INOUT", 2),
            _mt5_const("DEAL_ENTRY_OUT_BY", 3),
        }

        try:
            with self._mt5_lock:
                deals = mt5.history_deals_get(position=t_int)
            has_exit = bool(deals and any(_int_num(getattr(d, "entry", -1), -1) in out_entries for d in deals))
            if not has_exit:
                for delay in (0.15, 0.30, 0.50, 0.75):
                    time.sleep(delay)
                    with self._mt5_lock:
                        deals = mt5.history_deals_get(position=t_int)
                    has_exit = bool(
                        deals and any(_int_num(getattr(d, "entry", -1), -1) in out_entries for d in deals)
                    )
                    if has_exit:
                        break
                if not has_exit:
                    with self._mt5_lock:
                        now_utc = datetime.now(timezone.utc)
                        win_deals = mt5.history_deals_get(now_utc - timedelta(hours=1), now_utc + timedelta(minutes=2))
                    if win_deals:
                        matched = [
                            d for d in win_deals
                            if _int_num(getattr(d, "position_id", -1), -1) == t_int
                        ]
                        if matched and any(_int_num(getattr(d, "entry", -1), -1) in out_entries for d in matched):
                            deals = matched
                            has_exit = True
                if not has_exit:
                    logger.warning(
                        f"history_deals_get(position={t_int}) returned no exit deals after retries; "
                        f"using cached profit fallback ({self._cached_pos_profits.get(t_int, 0.0):.2f})."
                    )
            rich = self._compute_close_info_from_deals(t_int, deals)
            self._last_close_details[t_int] = rich
            return {"profit": rich["profit"], "reason": rich["reason"]}
        except Exception as e:
            logger.warning(f"Could not determine close info for #{t_int}: {e}")
            fallback_reason = "MANUAL/BOT" if t_int in getattr(self, "_bot_closed_tickets", set()) else "UNKNOWN"
            rich = {
                "profit": self._cached_pos_profits.get(t_int, 0.0),
                "reason": fallback_reason,
                "deals_settled": False,
                "exit_price": None,
                "entry_price": None,
                "exit_deal_tickets": [],
                "exit_deals": [],
            }
            self._last_close_details[t_int] = rich
            return {"profit": rich["profit"], "reason": rich["reason"]}

    def get_position_close_details(self, ticket: Optional[int]) -> Dict[str, Any]:
        """Query `get_position_close_info` and return the full deal-level metadata dictionary."""
        t_int = _int_num(ticket, 0)
        res = self.get_position_close_info(ticket)
        if t_int > 0 and isinstance(getattr(self, "_last_close_details", None), dict) and t_int in self._last_close_details:
            merged = dict(self._last_close_details[t_int])
            if isinstance(res, dict):
                merged["profit"] = res.get("profit", merged.get("profit", 0.0))
                merged["reason"] = res.get("reason", merged.get("reason", "UNKNOWN"))
            return merged
        return dict(res) if isinstance(res, dict) else {"profit": 0.0, "reason": "UNKNOWN"}

    def get_position_closed_profit(self, ticket: Optional[int]) -> float:
        """
        Retrieve realized net profit for a closed position ticket from MT5 history deals.
        Returns profit in account currency (e.g. GBP).
        """
        if not ticket:
            return 0.0
        if self.dry_run or not self._connected:
            return self._cached_pos_profits.get(ticket, 1.25)
        return self.get_position_close_info(ticket)["profit"]

    def get_account_financials(self) -> Dict[str, Any]:
        """Retrieve current live balance, credit, equity, and currency from MT5 (thread-safe)."""
        if self.dry_run or not self._connected:
            bal = float(self._dry_run_capital) if self._dry_run_capital is not None else 11.03
            cred = 7.03
            eq = round(bal + cred, 2)
            return {
                "balance": bal,
                "credit": cred,
                "equity": eq,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": eq,
                "currency": "GBP",
                "formatted": (
                    "💳 **Account Update:**\n"
                    f"• Balance: `{bal:.2f} GBP`\n"
                    f"• Credit: `{cred:.2f} GBP`\n"
                    f"• Equity: `{eq:.2f} GBP`"
                ),
            }
        try:
            with self._mt5_lock:
                acc = mt5.account_info()
            if acc:
                bal = round(_num(getattr(acc, "balance", 0.0), 0.0), 2)
                cred = round(_num(getattr(acc, "credit", 0.0), 0.0), 2)
                eq = round(_num(getattr(acc, "equity", 0.0), 0.0), 2)
                pnl = round(_num(getattr(acc, "profit", 0.0), 0.0), 2)
                mgn = round(_num(getattr(acc, "margin", 0.0), 0.0), 2)
                free_mgn = round(_num(getattr(acc, "margin_free", eq), eq), 2)
                raw_curr = getattr(acc, "currency", None)
                curr = raw_curr if isinstance(raw_curr, str) and raw_curr else "GBP"
                return {
                    "balance": bal,
                    "credit": cred,
                    "equity": eq,
                    "profit": pnl,
                    "margin": mgn,
                    "margin_free": free_mgn,
                    "currency": curr,
                    "formatted": (
                        f"💳 **Account Update:**\n"
                        f"• Balance: `{bal:.2f} {curr}`\n"
                        f"• Credit: `{cred:.2f} {curr}`\n"
                        f"• Equity: `{eq:.2f} {curr}`"
                    ),
                }
        except Exception as e:
            logger.warning(f"Could not fetch account financials: {e}")
        return {
            "balance": 0.0,
            "credit": 0.0,
            "equity": 0.0,
            "profit": 0.0,
            "margin": 0.0,
            "margin_free": 0.0,
            "currency": "GBP",
            "formatted": "• Financials unavailable",
        }

    def calculate_risk_guide_thresholds(self, capital: float) -> Dict[str, float]:
        """Delegate to module-level professional risk guide calculator."""
        return calculate_risk_guide_thresholds(capital)

    def get_total_active_lots(self) -> Dict[str, float]:
        """Return current bot open market lots, pending limit/stop lots, and total committed lots."""
        if self.dry_run or not self._connected:
            open_lots = round(sum(self._dry_run_ticket_lots.values()), 2)
            pending_lots = round(sum(self._dry_run_pending_ticket_lots.values()), 2)
            return {
                "open_lots": open_lots,
                "pending_lots": pending_lots,
                "total_lots": round(open_lots + pending_lots, 2),
            }

        magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
        try:
            with self._mt5_lock:
                positions = mt5.positions_get() or []
                orders = mt5.orders_get() or []
            open_lots = round(
                sum(_num(getattr(p, "volume", 0.0)) for p in positions if _int_num(getattr(p, "magic", 0), 0) == magic),
                2,
            )
            pending_lots = round(
                sum(_num(getattr(o, "volume_current", 0.0)) for o in orders if _int_num(getattr(o, "magic", 0), 0) == magic),
                2,
            )
            return {
                "open_lots": open_lots,
                "pending_lots": pending_lots,
                "total_lots": round(open_lots + pending_lots, 2),
            }
        except Exception as e:
            logger.warning(f"Could not query active lots: {e}")
            return {"open_lots": 0.0, "pending_lots": 0.0, "total_lots": 0.0}

    def get_open_market_tickets(self) -> List[int]:
        """Return list of open market position tickets tracked by this bot."""
        if self.dry_run or not self._connected:
            return list(self._dry_run_ticket_lots.keys())
        magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
        try:
            with self._mt5_lock:
                positions = mt5.positions_get() or []
            return [
                _int_num(getattr(p, "ticket", 0), 0)
                for p in positions
                if _int_num(getattr(p, "magic", 0), 0) == magic and _int_num(getattr(p, "ticket", 0), 0) > 0
            ]
        except Exception as e:
            logger.warning(f"Could not query open positions: {e}")
            return []

    def get_risk_exposure_status(self) -> Dict[str, Any]:
        """
        Compute live account financials, active lot exposure, and Professional Risk Guide
        thresholds (Low, Mid, and High Risk Cap).
        """
        fin = self.get_account_financials()
        if self.dry_run or not self._connected:
            cap = self._dry_run_capital if self._dry_run_capital is not None else 200.0
        else:
            bal = fin.get("balance", 0.0)
            cred = fin.get("credit", 0.0)
            cap = bal if bal >= 30.0 else (bal + cred)

        thresholds = calculate_risk_guide_thresholds(cap)
        lots = self.get_total_active_lots()
        open_lots = lots["open_lots"]
        low_l = thresholds["low_risk_lots"]
        mid_l = thresholds["mid_risk_lots"]
        high_l = thresholds["high_risk_lots"]

        if open_lots >= high_l - 1e-9:
            tier = "HIGH_CAP_REACHED"
        elif open_lots > mid_l + 1e-9:
            tier = "ABOVE_MID_RISK"
        elif open_lots > low_l + 1e-9:
            tier = "ABOVE_LOW_RISK"
        else:
            tier = "LOW_RISK_SAFE"

        return {
            **fin,
            **thresholds,
            **lots,
            "risk_tier": tier,
            "available_lots": max(0.0, round(high_l - open_lots, 2)),
        }

    def check_and_cap_order_lot(
        self,
        requested_lot: float,
        symbol: Optional[str] = None,
    ) -> Tuple[float, Optional[str]]:
        """
        Enforces the Professional Risk Guide High-Risk Cap for active open market positions:
        Never allows open active buy/sell lots to exceed high_risk_lots.
        Pending limit/stop orders are uncapped at placement time.
        """
        if requested_lot is None or float(requested_lot) <= 0.0:
            return 0.0, f"Invalid lot size: {requested_lot}"

        sym_info = self.get_symbol_info(symbol) if symbol else None
        vol_min, _, vol_step, dec = _extract_symbol_volume_limits(sym_info)
        norm_req = normalize_lot_to_symbol(float(requested_lot), sym_info) if sym_info else round(float(requested_lot), 2)

        if not _cfg("ENFORCE_HIGH_RISK_LOT_CAP", ENFORCE_HIGH_RISK_LOT_CAP):
            return norm_req, None

        if self.dry_run and self._dry_run_capital is None:
            return norm_req, None

        status = self.get_risk_exposure_status()
        avail = status["available_lots"]
        high_cap = status["high_risk_lots"]
        open_lots = status["open_lots"]
        cap = status["capital"]
        curr = status.get("currency", "GBP")

        if avail < vol_min - 1e-9:
            reason = (
                f"High Risk Lot Cap Reached ({open_lots:.2f}/{high_cap:.2f} active lots "
                f"for {cap:.2f} {curr} account)"
            )
            return 0.0, reason

        allowed_raw = min(norm_req, avail)
        eps = max(1e-12, vol_step * 1e-7)
        allowed = round(math.floor((allowed_raw + eps) / vol_step) * vol_step, dec)
        if allowed < vol_min:
            reason = (
                f"High Risk Lot Cap Reached ({open_lots:.2f}/{high_cap:.2f} active lots "
                f"for {cap:.2f} {curr} account)"
            )
            return 0.0, reason

        return allowed, None

    def evaluate_risk_threshold_alerts(self, prev_lots: float, new_lots: float) -> List[str]:
        """
        Check which Risk Guide thresholds (Low Risk, Mid Risk, High Risk Cap) were crossed
        as total exposure increased from prev_lots to new_lots.
        Returns no alerts when ENFORCE_HIGH_RISK_LOT_CAP is disabled (unlimited mode).
        """
        if not _cfg("ENFORCE_HIGH_RISK_LOT_CAP", ENFORCE_HIGH_RISK_LOT_CAP):
            return []
        status = self.get_risk_exposure_status()
        low_l = status["low_risk_lots"]
        mid_l = status["mid_risk_lots"]
        high_l = status["high_risk_lots"]
        cap = status["capital"]
        curr = status.get("currency", "GBP")

        alerts: List[str] = []
        if prev_lots <= low_l + 1e-9 and new_lots > low_l + 1e-9:
            alerts.append(
                f"📊 **Risk Guide Alert — Passed LOW RISK (`{low_l:.2f}` lots):** "
                f"Active exposure is now `{new_lots:.2f}` lots on `{cap:.2f} {curr}` account "
                f"(Mid Risk: `{mid_l:.2f}` | High Cap: `{high_l:.2f}`)."
            )
        if prev_lots <= mid_l + 1e-9 and new_lots > mid_l + 1e-9:
            alerts.append(
                f"⚠️ **Risk Guide Alert — Passed MID RISK (`{mid_l:.2f}` lots):** "
                f"Active exposure is now `{new_lots:.2f}` lots (entering High Risk zone; "
                f"hard cap is `{high_l:.2f}` lots)."
            )
        if prev_lots < high_l - 1e-9 and new_lots >= high_l - 1e-9:
            alerts.append(
                f"🚨 **MAX HIGH RISK CAP REACHED (`{new_lots:.2f} / {high_l:.2f}` lots):** "
                f"Capital protection active for `{cap:.2f} {curr}` account — no additional lots "
                f"will open until existing trades bank or close!"
            )
        return alerts

    def get_server_time_offset_seconds(self) -> int:
        """
        Broker server clock offset vs UTC (e.g. PU Prime = GMT+2 -> 7200s), derived from the
        latest tick timestamp (cached for 300s). Stale ticks fall back to the last good cached offset.
        """
        cached = getattr(self, "_server_offset_cache", None)
        cached_ts = _num(getattr(self, "_server_offset_cache_ts", 0.0), 0.0)
        if self.dry_run or not self._connected:
            return 0
        if cached is not None and cached_ts > 0.0 and (time.monotonic() - cached_ts) < 300.0:
            return cached
        try:
            with self._mt5_lock:
                tick = mt5.symbol_info_tick(self.resolve_symbol(_cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)))
            tick_time = _num(getattr(tick, "time", 0.0), 0.0) if tick else 0.0
            if tick_time > 0.0:
                diff = tick_time - time.time()
                rounded = int(round(diff / 1800.0) * 1800)
                if -12 * 3600 <= rounded <= 14 * 3600 and abs(diff - rounded) <= 600:
                    self._server_offset_cache = rounded
                    self._server_offset_cache_ts = time.monotonic()
                    return rounded
        except Exception as e:
            logger.debug(f"Could not derive server time offset: {e}")
        return cached if cached is not None else 0

    def get_server_date_str(self) -> str:
        """Current broker-server trading day as YYYY-MM-DD (local date in dry-run)."""
        if self.dry_run or not self._connected:
            return datetime.now().strftime("%Y-%m-%d")
        offset = self.get_server_time_offset_seconds()
        return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).strftime("%Y-%m-%d")

    def invalidate_realized_pnl_cache(self) -> None:
        """Explicitly invalidate the short-TTL realized today PnL cache."""
        with self._mt5_lock:
            self._realized_pnl_cache = None

    def get_realized_today_pnl(self) -> float:
        """
        Net REALIZED P&L for the current broker-server trading day (closed trading deals only:
        profit + swap + commission + fee on OUT / INOUT / OUT_BY deals with type DEAL_TYPE_BUY or
        DEAL_TYPE_SELL). Cached for DAILY_PNL_CACHE_TTL_SECONDS and invalidated on closes.
        """
        if self.dry_run or not self._connected:
            return round(float(self._dry_run_daily_realized_pnl), 2)

        ttl = float(_cfg("DAILY_PNL_CACHE_TTL_SECONDS", DAILY_PNL_CACHE_TTL_SECONDS))
        try:
            offset = self.get_server_time_offset_seconds()
            srv_now = datetime.fromtimestamp(time.time() + offset, tz=timezone.utc)
            srv_date = srv_now.strftime("%Y-%m-%d")
            day_start = srv_now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

            with self._mt5_lock:
                if ttl > 0.0 and self._realized_pnl_cache is not None:
                    c_val, c_ts, c_date = self._realized_pnl_cache
                    if c_date == srv_date and (time.monotonic() - c_ts) < ttl:
                        return c_val

                deals = mt5.history_deals_get(day_start, day_end + timedelta(hours=1))
                if deals is None:
                    deals = mt5.history_deals_get(day_start, day_end)
                if deals is None:
                    logger.debug(f"history_deals_get returned None: {mt5.last_error()}")
                    return 0.0

                out_entries = {
                    _mt5_const("DEAL_ENTRY_OUT", 1),
                    _mt5_const("DEAL_ENTRY_INOUT", 2),
                    _mt5_const("DEAL_ENTRY_OUT_BY", 3),
                }
                trade_deal_types = {
                    _mt5_const("DEAL_TYPE_BUY", 0),
                    _mt5_const("DEAL_TYPE_SELL", 1),
                }
                day_start_ts = day_start.timestamp()
                day_end_ts = day_end.timestamp()
                realized = 0.0
                for d in deals:
                    if _int_num(getattr(d, "entry", -1), -1) not in out_entries:
                        continue
                    if _int_num(getattr(d, "type", -1), -1) not in trade_deal_types:
                        continue
                    raw_time = getattr(d, "time", None)
                    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
                        d_ts = float(raw_time)
                        if d_ts < day_start_ts or d_ts >= day_end_ts:
                            continue
                    realized += (
                        _num(getattr(d, "profit", 0.0))
                        + _num(getattr(d, "swap", 0.0))
                        + _num(getattr(d, "commission", 0.0))
                        + _num(getattr(d, "fee", 0.0))
                    )
                rounded = round(realized, 2)
                if ttl > 0.0:
                    self._realized_pnl_cache = (rounded, time.monotonic(), srv_date)
                return rounded
        except Exception as e:
            logger.debug(f"Could not compute realized today P&L: {e}")
            return 0.0

    def cancel_all_pending_orders(
        self,
        tracked_pending_tickets: Optional[List[int]] = None,
    ) -> List[int]:
        """Thread-safely cancel every pending MT5 order (TRADE_ACTION_REMOVE)."""
        tickets_to_cancel: List[int] = []
        for t in (tracked_pending_tickets or []):
            t_int = _int_num(t, 0)
            if t_int and t_int not in tickets_to_cancel:
                tickets_to_cancel.append(t_int)

        if not self.dry_run and self._connected:
            try:
                with self._mt5_lock:
                    orders = mt5.orders_get() or []
                for o in orders:
                    o_ticket = _int_num(getattr(o, "ticket", None), 0)
                    if o_ticket and o_ticket not in tickets_to_cancel:
                        tickets_to_cancel.append(o_ticket)
            except Exception as e:
                logger.warning(f"cancel_all_pending_orders: could not list live pending orders: {e}")

        cancelled: List[int] = []
        for t in tickets_to_cancel:
            if self.cancel_pending_order(t):
                cancelled.append(t)
        return cancelled

    def close_all_bot_exposure(
        self,
        tracked_market_tickets: Optional[List[int]] = None,
        tracked_pending_tickets: Optional[List[int]] = None,
    ) -> Dict[str, List[int]]:
        """Close every open bot position (MAGIC_NUMBER) and cancel every bot pending order."""
        closed: List[int] = []
        cancelled: List[int] = []
        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        for t in (tracked_market_tickets or []):
            t_int = _int_num(t, 0)
            if t_int and t_int not in market_tickets:
                market_tickets.append(t_int)
        for t in (tracked_pending_tickets or []):
            t_int = _int_num(t, 0)
            if t_int and t_int not in pending_tickets:
                pending_tickets.append(t_int)

        magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
        if not self.dry_run and self._connected:
            try:
                with self._mt5_lock:
                    positions = mt5.positions_get() or []
                    orders = mt5.orders_get() or []
                for p in positions:
                    p_ticket = _int_num(getattr(p, "ticket", None), 0)
                    if p_ticket and _int_num(getattr(p, "magic", 0), 0) == magic and p_ticket not in market_tickets:
                        market_tickets.append(p_ticket)
                for o in orders:
                    o_ticket = _int_num(getattr(o, "ticket", None), 0)
                    if o_ticket and _int_num(getattr(o, "magic", 0), 0) == magic and o_ticket not in pending_tickets:
                        pending_tickets.append(o_ticket)
            except Exception as e:
                logger.warning(f"close_all_bot_exposure: could not list live bot exposure: {e}")

        for t in market_tickets:
            if self.close_market_position(t):
                closed.append(t)
        market_set = set(market_tickets)
        for t in pending_tickets:
            if t in market_set:
                continue
            if self.cancel_pending_order(t):
                cancelled.append(t)
        if closed:
            self.invalidate_realized_pnl_cache()
        return {"closed_tickets": closed, "cancelled_orders": cancelled}

    def get_daily_drawdown_status(self) -> Dict[str, Any]:
        """
        Two-Stage Daily Loss & Account Drawdown Monitor:
        - Stage 1 (DAILY_LOSS_PAUSE_PCT = 50%): Pause NEW signals, keep existing trades running naturally to SL/TP.
        - Stage 2 (EMERGENCY_CUTOFF_LOSS_PCT = 90%): Urgency cut-off immediately closes losing trades.
        """
        fin = self.get_account_financials()
        curr = fin.get("currency", "GBP")

        if self.dry_run or not self._connected:
            curr_bal = float(self._dry_run_capital) if self._dry_run_capital is not None else 200.0
            realized_today = float(self._dry_run_daily_realized_pnl or 0.0)
            floating_pnl = float(self._dry_run_floating_pnl or 0.0)
        else:
            bal = float(fin.get("balance") or 0.0)
            cred = float(fin.get("credit") or 0.0)
            curr_bal = bal if bal >= 30.0 else round(bal + cred, 2)
            realized_today = self.get_realized_today_pnl()
            floating_pnl = 0.0
            magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
            try:
                with self._mt5_lock:
                    positions = mt5.positions_get() or []
                for p in positions:
                    if _int_num(getattr(p, "magic", 0), 0) != magic:
                        continue
                    floating_pnl += (
                        _num(getattr(p, "profit", 0.0))
                        + _num(getattr(p, "swap", 0.0))
                    )
            except Exception as e:
                logger.debug(f"Could not compute floating P&L: {e}")

        realized_today = round(realized_today, 2)
        floating_pnl = round(floating_pnl, 2)

        # Rollover check: If date changes, reset baseline to 0.0
        srv_date = None
        if not self.dry_run and self._connected:
            try:
                offset = self.get_server_time_offset_seconds()
                srv_now = datetime.fromtimestamp(time.time() + offset, tz=timezone.utc)
                srv_date = srv_now.strftime("%Y-%m-%d")
            except Exception:
                srv_date = None
        if srv_date and srv_date != getattr(self, "_daily_drawdown_baseline_date", None):
            self._daily_loss_baseline_realized = 0.0
            self._daily_drawdown_ref_balance = None
            self._daily_drawdown_baseline_date = srv_date

        # Active daily realized loss relative to the baseline (updated when previous losses are realized & passed)
        base_realized = float(getattr(self, "_daily_loss_baseline_realized", 0.0) or 0.0)
        active_realized_loss = max(0.0, round(-(realized_today - base_realized), 2))
        floating_loss = max(0.0, round(-floating_pnl, 2))
        total_daily_loss = round(active_realized_loss + floating_loss, 2)

        base_ref = getattr(self, "_daily_drawdown_ref_balance", None)
        if base_ref is not None and base_ref > 0:
            ref_bal = round(max(curr_bal, base_ref), 2)
        else:
            ref_bal = max(0.0, curr_bal, round(curr_bal + active_realized_loss, 2))

        pause_pct = float(_cfg("DAILY_LOSS_PAUSE_PCT", DAILY_LOSS_PAUSE_PCT) or 0.0)
        cutoff_pct = float(_cfg("EMERGENCY_CUTOFF_LOSS_PCT", EMERGENCY_CUTOFF_LOSS_PCT) or 0.0)
        float_cap_pct = float(_cfg("FLOAT_LOSS_FORCE_CLOSE_PCT", FLOAT_LOSS_FORCE_CLOSE_PCT) or 0.0)

        pause_limit = round(ref_bal * (pause_pct / 100.0), 2) if pause_pct > 0.0 else 0.0
        cutoff_limit_ref = round(ref_bal * (cutoff_pct / 100.0), 2) if cutoff_pct > 0.0 else 0.0
        cutoff_limit_curr = round(max(0.0, curr_bal) * (cutoff_pct / 100.0), 2) if cutoff_pct > 0.0 else 0.0
        float_cap_limit = round(max(0.0, curr_bal) * (float_cap_pct / 100.0), 2) if float_cap_pct > 0.0 else 0.0

        loss_pct = round((total_daily_loss / ref_bal) * 100.0, 1) if ref_bal > 0 else 0.0
        floating_loss_pct = round((floating_loss / curr_bal) * 100.0, 1) if curr_bal > 0 else 0.0

        pause_new = bool(
            pause_pct > 0.0
            and ref_bal > 0
            and pause_limit > 0.0
            and total_daily_loss >= pause_limit - 1e-9
        )
        emergency_cutoff = bool(
            cutoff_pct > 0.0
            and (
                (ref_bal > 0 and cutoff_limit_ref > 0.0 and total_daily_loss >= cutoff_limit_ref - 1e-9)
                or (curr_bal > 0 and cutoff_limit_curr > 0.0 and floating_loss >= cutoff_limit_curr - 1e-9)
            )
        )
        float_force_close = bool(
            float_cap_pct > 0.0
            and curr_bal > 0
            and float_cap_limit > 0.0
            and floating_loss >= float_cap_limit - 1e-9
        )

        return {
            "currency": curr,
            "current_balance": round(curr_bal, 2),
            "reference_balance": round(ref_bal, 2),
            "realized_today_pnl": realized_today,
            "floating_pnl": floating_pnl,
            "floating_loss": floating_loss,
            "total_daily_loss": total_daily_loss,
            "loss_pct": max(loss_pct, floating_loss_pct),
            "pause_pct": pause_pct,
            "cutoff_pct": cutoff_pct,
            "pause_limit_amount": pause_limit,
            "emergency_cutoff_amount": min(cutoff_limit_ref, cutoff_limit_curr) if cutoff_limit_curr > 0 else cutoff_limit_ref,
            "float_cap_pct": float_cap_pct,
            "float_cap_limit": float_cap_limit,
            "float_force_close_triggered": float_force_close,
            "pause_new_signals": pause_new,
            "emergency_cutoff_triggered": emergency_cutoff,
        }

    def update_daily_loss_baseline(self, new_ref_balance: Optional[float] = None) -> Dict[str, Any]:
        """
        Update the daily loss baseline so that previously realized losses (that have passed)
        are acknowledged as realized and archived, resetting active drawdown tracking to 0% against
        the current account balance.
        """
        fin = self.get_account_financials()
        if self.dry_run or not self._connected:
            curr_bal = float(self._dry_run_capital) if self._dry_run_capital is not None else float(fin.get("balance") or 0.0)
        else:
            bal = float(fin.get("balance") or 0.0)
            cred = float(fin.get("credit") or 0.0)
            curr_bal = bal if bal >= 30.0 else round(bal + cred, 2)
        realized_today = float(
            self.get_realized_today_pnl()
            if (not self.dry_run and self._connected)
            else (self._dry_run_daily_realized_pnl or 0.0)
        )

        self._daily_loss_baseline_realized = round(realized_today, 2)
        self._daily_drawdown_ref_balance = round(new_ref_balance if new_ref_balance is not None else curr_bal, 2)
        if not self.dry_run and self._connected:
            try:
                offset = self.get_server_time_offset_seconds()
                srv_now = datetime.fromtimestamp(time.time() + offset, tz=timezone.utc)
                self._daily_drawdown_baseline_date = srv_now.strftime("%Y-%m-%d")
            except Exception:
                pass
        logger.info(
            f"[copier.mt5] Daily loss baseline updated: baseline_realized={self._daily_loss_baseline_realized:.2f}, "
            f"ref_balance={self._daily_drawdown_ref_balance:.2f} {fin.get('currency', 'GBP')}"
        )
        return {
            "baseline_realized": self._daily_loss_baseline_realized,
            "ref_balance": self._daily_drawdown_ref_balance,
            "current_balance": curr_bal,
        }

    def emergency_cutoff_losing_trades(
        self,
        tracked_market_tickets: Optional[List[int]] = None,
        tracked_pending_tickets: Optional[List[int]] = None,
    ) -> Dict[str, List[int]]:
        """
        90% Emergency Drawdown Cut-Off:
        Immediately closes all losing open positions at market and cancels all pending orders.
        """
        closed_tickets: List[int] = []
        cancelled_orders: List[int] = []

        if self.dry_run or not self._connected:
            for t in list(tracked_market_tickets or list(self._dry_run_ticket_lots.keys())):
                if self.close_market_position(t):
                    closed_tickets.append(t)
            for pt in list(tracked_pending_tickets or list(self._dry_run_pending_ticket_lots.keys())):
                if self.cancel_pending_order(pt):
                    cancelled_orders.append(pt)
            if closed_tickets:
                self.invalidate_realized_pnl_cache()
            return {"closed_tickets": closed_tickets, "cancelled_orders": cancelled_orders}

        magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
        try:
            with self._mt5_lock:
                positions = mt5.positions_get() or []
                orders = mt5.orders_get() or []
            for p in positions:
                p_ticket = _int_num(getattr(p, "ticket", None), 0)
                if not p_ticket:
                    continue
                if _int_num(getattr(p, "magic", 0), 0) == magic or (tracked_market_tickets and p_ticket in tracked_market_tickets):
                    if _num(getattr(p, "profit", 0.0)) < 0.0 or tracked_market_tickets:
                        if self.close_market_position(p_ticket):
                            closed_tickets.append(p_ticket)

            for o in orders:
                o_ticket = _int_num(getattr(o, "ticket", None), 0)
                if not o_ticket:
                    continue
                if _int_num(getattr(o, "magic", 0), 0) == magic or (tracked_pending_tickets and o_ticket in tracked_pending_tickets):
                    if self.cancel_pending_order(o_ticket):
                        cancelled_orders.append(o_ticket)
        except Exception as e:
            logger.error(f"Error during 90% emergency cut-off: {e}")

        if closed_tickets:
            self.invalidate_realized_pnl_cache()
        return {"closed_tickets": closed_tickets, "cancelled_orders": cancelled_orders}

    def _move_ticket_to_be(
        self,
        ticket: int,
        offset_value: float,
        digits: int,
        direction: str,
        fallback_entry: Optional[float] = None,
    ) -> Optional[float]:
        """
        Move an open position's SL to entry +/- offset_value while keeping its TP intact.
        - Forward-only ratchet: never loosens an SL that is already at or tighter than BE.
        - Instant 0ms stop-level fallback: if broker rejects `entry +/- offset_value` with
          `10016 Invalid stops` because live price is narrowly inside the commission buffer,
          immediately falls back to exact `entry` (`0.0p`) in the same call.
        """
        if self.dry_run:
            self.modify_position_sltp(ticket=ticket, sl=None, tp=None)
            if fallback_entry is not None:
                is_buy = (direction or "BUY").strip().upper() == "BUY"
                return round(float(fallback_entry) + offset_value, digits) if is_buy else round(float(fallback_entry) - offset_value, digits)
            return None

        buy_order_type = _mt5_const("ORDER_TYPE_BUY", 0)
        with self._mt5_lock:
            pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return None
        p = pos[0]
        price_open = _num(getattr(p, "price_open", 0.0), 0.0)
        p_tp = _num(getattr(p, "tp", 0.0), 0.0)
        cur_sl = _num(getattr(p, "sl", 0.0), 0.0)
        is_buy = _int_num(getattr(p, "type", buy_order_type), buy_order_type) == buy_order_type
        new_sl = round(price_open + offset_value, digits) if is_buy else round(price_open - offset_value, digits)

        # Forward-only SL ratchet: never loosen a position whose SL is already at or past BE
        if is_buy and cur_sl > 0.0 and cur_sl >= new_sl - 1e-6:
            return cur_sl
        if not is_buy and cur_sl > 0.0 and cur_sl <= new_sl + 1e-6:
            return cur_sl

        if self.modify_position_sltp(ticket=ticket, sl=new_sl, tp=p_tp):
            return new_sl

        # Instant 0ms fallback to exact entry (0.0p) when +offset_value is inside broker stops_level
        if offset_value > 0.0 and price_open > 0.0:
            exact_sl = round(price_open, digits)
            if is_buy and cur_sl > 0.0 and cur_sl >= exact_sl - 1e-6:
                return cur_sl
            if not is_buy and cur_sl > 0.0 and cur_sl <= exact_sl + 1e-6:
                return cur_sl
            if exact_sl != new_sl and self.modify_position_sltp(ticket=ticket, sl=exact_sl, tp=p_tp):
                logger.info(
                    f"Ticket #{ticket} moved to exact-entry BE {exact_sl} "
                    f"(0.0p instant fallback; +{offset_value:.2f} buffer rejected by stops_level)"
                )
                return exact_sl

        return None

    def _resolve_be_layer_names(
        self,
        layers: Dict[str, Dict[str, Any]],
        eff_be_layers: List[str],
    ) -> List[str]:
        """Resolve configured BE layer names across both v3.0 4-layer and legacy 3-layer dicts."""
        is_legacy = _is_legacy_3layer_dict(layers)
        resolved_names: List[str] = []
        for raw_name in eff_be_layers:
            up = str(raw_name).strip().upper()
            if is_legacy:
                if up in layers and up not in resolved_names:
                    resolved_names.append(up)
                elif up in ("L1", "L2"):
                    mapped = _V3_TO_LEGACY_LAYER[up]
                    if mapped in layers and mapped not in resolved_names:
                        resolved_names.append(mapped)
                elif up == "L4" and "BOTTOM" in layers and "BOTTOM" not in resolved_names:
                    resolved_names.append("BOTTOM")
                # Note: L3 alone does not map to BOTTOM so explicit ["L1","L2","L3"] keeps legacy BOTTOM untouched.
            else:
                if up in layers and up not in resolved_names:
                    resolved_names.append(up)
                elif up in LEGACY_LAYER_MAP and LEGACY_LAYER_MAP[up] in layers:
                    mapped_v3 = LEGACY_LAYER_MAP[up]
                    if mapped_v3 not in resolved_names:
                        resolved_names.append(mapped_v3)
        return resolved_names

    def apply_selective_breakeven(
        self,
        layers: Dict[str, Dict[str, Any]],
        symbol: str,
        direction: str,
        be_layers: Optional[List[str]] = None,
        be_offset_pips: Optional[float] = None,
    ) -> List[int]:
        """
        Breakeven across configured layers:
        - Default `BE_LAYERS` (`["L1", "L2", "L3", "L4"]`): moves ALL 4 layers (including L4 0.02) to BE.
        - Explicit `be_layers` subset (e.g. `["TOP", "MID"]`): moves only the requested layers.
        Pending orders remain active. Returns list of tickets modified to BE.
        """
        pip_size = self.get_pip_size(symbol)
        eff_offset_pips = float(be_offset_pips) if be_offset_pips is not None else float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS))
        offset_value = eff_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

        eff_be_layers = list(be_layers) if be_layers is not None else list(_cfg("BE_LAYERS", BE_LAYERS))
        resolved_names = self._resolve_be_layer_names(layers, eff_be_layers)

        modified_tickets: List[int] = []
        seen_tickets = set()

        for layer_name in resolved_names:
            layer = layers.get(layer_name)
            if not isinstance(layer, dict):
                continue
            ticket = layer.get("ticket")
            if not ticket or ticket in seen_tickets:
                continue
            seen_tickets.add(ticket)

            if self.dry_run:
                logger.info(
                    f"[DRY-RUN] Moved Layer {layer_name} (Ticket #{ticket}) to Breakeven (+{eff_offset_pips} pips offset)"
                )
                modified_tickets.append(ticket)
                continue

            new_sl = self._move_ticket_to_be(ticket, offset_value, digits, direction)
            if new_sl is not None:
                modified_tickets.append(ticket)
                layer["be_sl"] = new_sl
                logger.info(
                    f"Layer {layer_name} (Ticket #{ticket}) moved to BE: NewSL={new_sl} (+{eff_offset_pips} pips target)"
                )

        return modified_tickets

    def apply_signal_breakeven(
        self,
        signal_data: Dict[str, Any],
        be_layers: Optional[List[str]] = None,
        be_offset_pips: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Fast, all-layer signal breakeven with live fill-race promotion & deferred retry tracking:
        - Moves all open target layers (default L1..L4) + any open market/runner tickets on this signal to BE.
        - Checks live `mt5.positions_get(ticket=t)` even for tickets still in `pending_tickets` in case a
          limit filled milliseconds before the 1s reconciler ran, promoting filled tickets immediately.
        - Never cancels unfilled pending orders.
        - Never loosens an already-trailed runner SL (forward-only ratchet).
        - Tracks `be_done_tickets` and `deferred_tickets` (open positions temporarily underwater / inside
          broker `stops_level`) so the 0.5s watcher can snap deferred tickets to BE automatically.
        """
        symbol = signal_data.get("symbol", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        layers = signal_data.get("layers") or {}

        pip_size = self.get_pip_size(symbol)
        eff_offset_pips = float(be_offset_pips) if be_offset_pips is not None else float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS))
        offset_value = eff_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

        eff_be_layers = list(be_layers) if be_layers is not None else list(_cfg("BE_LAYERS", BE_LAYERS))
        upper_be_set = {str(x).strip().upper() for x in eff_be_layers}
        all_layers_mode = {"L1", "L2", "L3", "L4"}.issubset(upper_be_set) or {"TOP", "MID", "BOTTOM"}.issubset(upper_be_set)

        resolved_names = self._resolve_be_layer_names(layers, eff_be_layers)
        candidates: List[Tuple[int, Optional[str]]] = []
        seen_tickets = set()

        for layer_name in resolved_names:
            layer = layers.get(layer_name)
            if not isinstance(layer, dict):
                continue
            if str(layer.get("status") or "").upper() in ("CLOSED", "CANCELLED"):
                continue
            ticket = layer.get("ticket")
            if ticket and ticket not in seen_tickets:
                seen_tickets.add(ticket)
                candidates.append((int(ticket), layer_name))

        if all_layers_mode:
            ticket_to_layer = {
                int(ld["ticket"]): lk
                for lk, ld in layers.items()
                if isinstance(ld, dict) and ld.get("ticket") is not None
            }
            extra_pool = list(signal_data.get("market_tickets") or []) + list(signal_data.get("runner_tickets") or [])
            if signal_data.get("runner_ticket") is not None:
                extra_pool.append(signal_data["runner_ticket"])
            for raw_t in extra_pool:
                if raw_t is None:
                    continue
                t_int = int(raw_t)
                if t_int not in seen_tickets:
                    seen_tickets.add(t_int)
                    candidates.append((t_int, ticket_to_layer.get(t_int)))

        mk_tickets = [int(t) for t in (signal_data.get("market_tickets") or []) if t is not None]
        pd_tickets = [int(t) for t in (signal_data.get("pending_tickets") or []) if t is not None]
        be_done_set = {int(t) for t in (signal_data.get("be_done_tickets") or []) if t is not None}

        modified_tickets: List[int] = []
        deferred_tickets: List[int] = []
        promoted_tickets: List[int] = []
        applied_sls: Dict[int, float] = {}

        if self.dry_run:
            has_ticket_lists = bool(mk_tickets or pd_tickets)
            for ticket, layer_name in candidates:
                if has_ticket_lists and ticket in pd_tickets and ticket not in mk_tickets:
                    continue
                layer_obj = layers.get(layer_name) if layer_name else None
                entry_ref = 0.0
                if isinstance(layer_obj, dict):
                    for ek in ("entry_price", "entry", "price_open", "target_price"):
                        if _num(layer_obj.get(ek), 0.0) > 0.0:
                            entry_ref = _num(layer_obj.get(ek), 0.0)
                            break
                if entry_ref <= 0.0:
                    entry_ref = _num(signal_data.get("recorded_entry_price"), 0.0)
                if entry_ref > 0.0:
                    calc_sl = round(entry_ref + offset_value, digits) if is_buy else round(entry_ref - offset_value, digits)
                    if isinstance(layer_obj, dict):
                        for stop_k in ("be_sl", "sl", "broker_sl", "ratchet_sl"):
                            prev_v = _num(layer_obj.get(stop_k), 0.0)
                            if prev_v > 0.0:
                                if is_buy and prev_v > calc_sl:
                                    calc_sl = prev_v
                                elif not is_buy and prev_v < calc_sl:
                                    # Only tighten against previous green / BE / ratchet stops (never clamp above wide red SL)
                                    if stop_k != "sl" or prev_v <= entry_ref + 1e-9:
                                        calc_sl = prev_v
                        s0_sl = _num(signal_data.get("stage0_profit_lock_sl"), 0.0)
                        if s0_sl > 0.0:
                            if is_buy and s0_sl >= entry_ref - 1e-9 and s0_sl > calc_sl:
                                calc_sl = s0_sl
                            elif not is_buy and s0_sl <= entry_ref + 1e-9 and s0_sl < calc_sl:
                                calc_sl = s0_sl
                        layer_obj["be_sl"] = calc_sl
                        layer_obj["sl"] = calc_sl
                        layer_obj["broker_sl"] = calc_sl
                    applied_sls[ticket] = calc_sl
                if ticket not in be_done_set:
                    logger.info(
                        f"[DRY-RUN] Moved Layer {layer_name or 'OPEN'} (Ticket #{ticket}) to Breakeven (+{eff_offset_pips} pips offset)"
                    )
                    modified_tickets.append(ticket)
                    be_done_set.add(ticket)

            if applied_sls:
                best_be_sl = max(applied_sls.values()) if is_buy else min(applied_sls.values())
                prev_bsl = _num(signal_data.get("broker_sl"), 0.0)
                if prev_bsl <= 0.0 or (is_buy and best_be_sl > prev_bsl) or (not is_buy and best_be_sl < prev_bsl):
                    signal_data["broker_sl"] = best_be_sl
                if signal_data.get("status") == "RUNNER_ACTIVE":
                    cur_floor = signal_data.get("runner_sl_floor")
                    if cur_floor is None or (is_buy and best_be_sl > float(cur_floor)) or (not is_buy and best_be_sl < float(cur_floor)):
                        signal_data["runner_sl_floor"] = best_be_sl

            cancelled_pending: List[int] = []
            if (
                bool(_cfg("CANCEL_PENDING_ON_BE", CANCEL_PENDING_ON_BE))
                and pd_tickets
                and (mk_tickets or modified_tickets or be_done_set)
            ):
                cancelled_pending = self.cancel_signal_pending_orders(signal_data)
                cancel_set = set(cancelled_pending) | set(pd_tickets)
                signal_data["pending_tickets"] = []
                for l_val in layers.values():
                    if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                        if _int_num(l_val.get("ticket"), 0) in cancel_set or _int_num(l_val.get("ticket"), 0) > 0:
                            l_val["status"] = "CANCELLED"

            signal_data["be_done_tickets"] = sorted(be_done_set)
            signal_data["be_deferred_tickets"] = []
            return {
                "modified_tickets": modified_tickets,
                "deferred_tickets": [],
                "promoted_tickets": [],
                "cancelled_pending_tickets": cancelled_pending,
                "be_done_tickets": sorted(be_done_set),
                "applied_sls": applied_sls,
            }

        buy_order_type = _mt5_const("ORDER_TYPE_BUY", 0)
        for ticket, layer_name in candidates:
            with self._mt5_lock:
                pos = mt5.positions_get(ticket=ticket)
            if not pos:
                # Unfilled pending order or already closed on broker; leave pending orders untouched
                continue

            # Catch sub-second fill race if a pending limit filled before 1s reconciler ran
            p0 = pos[0]
            p_open = _num(getattr(p0, "price_open", 0.0), 0.0)
            p_vol = _num(getattr(p0, "volume", 0.0), 0.0)
            if ticket in pd_tickets:
                pd_tickets = [t for t in pd_tickets if t != ticket]
                if ticket not in mk_tickets:
                    mk_tickets.append(ticket)
                if layer_name and isinstance(layers.get(layer_name), dict):
                    layers[layer_name]["status"] = "FILLED"
                    if p_open > 0.0:
                        layers[layer_name]["entry_price"] = round(p_open, digits)
                        layers[layer_name]["entry"] = round(p_open, digits)
                    if p_vol > 0.0:
                        layers[layer_name]["remaining_lot"] = p_vol
                promoted_tickets.append(ticket)
                logger.info(
                    f"Instant BE fill-race catch: Pending #{ticket} ({layer_name}) already filled on MT5; promoting & protecting now."
                )

            p_sl = _num(getattr(p0, "sl", 0.0), 0.0)
            pos_is_buy = _int_num(getattr(p0, "type", buy_order_type), buy_order_type) == buy_order_type
            if p_open > 0.0 and layer_name and isinstance(layers.get(layer_name), dict):
                if layers[layer_name].get("entry_price") is None:
                    layers[layer_name]["entry_price"] = round(p_open, digits)
                    layers[layer_name]["entry"] = round(p_open, digits)
                if layers[layer_name].get("remaining_lot") is None and p_vol > 0.0:
                    layers[layer_name]["remaining_lot"] = p_vol
            if p_open > 0.0 and signal_data.get("recorded_entry_price") is None:
                signal_data["recorded_entry_price"] = round(p_open, digits)
                signal_data["initial_price"] = round(p_open, digits)

            if ticket in be_done_set and p_open > 0.0:
                if (pos_is_buy and p_sl >= p_open - 1e-6) or (not pos_is_buy and p_sl > 0.0 and p_sl <= p_open + 1e-6):
                    applied_sls[ticket] = p_sl
                    if layer_name and isinstance(layers.get(layer_name), dict):
                        layers[layer_name]["sl"] = p_sl
                        layers[layer_name]["broker_sl"] = p_sl
                    continue

            new_sl = self._move_ticket_to_be(ticket, offset_value, digits, direction)
            if new_sl is not None:
                modified_tickets.append(ticket)
                be_done_set.add(ticket)
                applied_sls[ticket] = new_sl
                if layer_name and isinstance(layers.get(layer_name), dict):
                    layers[layer_name]["be_sl"] = new_sl
                    layers[layer_name]["sl"] = new_sl
                    layers[layer_name]["broker_sl"] = new_sl
                logger.info(
                    f"Layer {layer_name or 'OPEN'} (Ticket #{ticket}) moved to BE: NewSL={new_sl}"
                )
            else:
                deferred_tickets.append(ticket)
                logger.warning(
                    f"Layer {layer_name or 'OPEN'} (Ticket #{ticket}) open at {p_open} but inside stops_level / underwater; "
                    f"armed for 0.5s auto-BE retry."
                )

        if promoted_tickets:
            signal_data["market_tickets"] = mk_tickets
            signal_data["pending_tickets"] = pd_tickets

        if applied_sls:
            best_be_sl = max(applied_sls.values()) if is_buy else min(applied_sls.values())
            prev_bsl = _num(signal_data.get("broker_sl"), 0.0)
            if prev_bsl <= 0.0 or (is_buy and best_be_sl > prev_bsl) or (not is_buy and best_be_sl < prev_bsl):
                signal_data["broker_sl"] = best_be_sl

            if signal_data.get("status") == "RUNNER_ACTIVE":
                cur_floor = signal_data.get("runner_sl_floor")
                if cur_floor is None:
                    signal_data["runner_sl_floor"] = best_be_sl
                else:
                    cur_floor_f = float(cur_floor)
                    if (is_buy and best_be_sl > cur_floor_f) or (not is_buy and best_be_sl < cur_floor_f):
                        signal_data["runner_sl_floor"] = best_be_sl
                cur_ts = signal_data.get("trail_stop")
                if isinstance(cur_ts, numbers.Real) and not isinstance(cur_ts, bool) and float(cur_ts) > 0.0:
                    if (is_buy and best_be_sl > float(cur_ts)) or (not is_buy and best_be_sl < float(cur_ts)):
                        signal_data["trail_stop"] = best_be_sl

        # Strictly AFTER fill-race promotion: sweep leftover unfilled pending orders when CANCEL_PENDING_ON_BE=True
        cancelled_pending = []
        if (
            bool(_cfg("CANCEL_PENDING_ON_BE", CANCEL_PENDING_ON_BE))
            and pd_tickets
            and (mk_tickets or modified_tickets or be_done_set)
        ):
            cancelled_pending = self.cancel_signal_pending_orders(signal_data)
            signal_data["pending_tickets"] = []
            for l_val in layers.values():
                if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                    l_val["status"] = "CANCELLED"

        signal_data["be_done_tickets"] = sorted(be_done_set)
        signal_data["be_deferred_tickets"] = deferred_tickets
        return {
            "modified_tickets": modified_tickets,
            "deferred_tickets": deferred_tickets,
            "promoted_tickets": promoted_tickets,
            "cancelled_pending_tickets": cancelled_pending,
            "be_done_tickets": sorted(be_done_set),
            "applied_sls": applied_sls,
        }

    def apply_breakeven_worst_hold_layers(
        self,
        market_tickets: List[int],
        symbol: str,
        direction: str,
        be_offset_pips: float = BE_OFFSET_PIPS,
        raw_tp3: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        "close worst entries now and hold layers":
        Moves worst-performing position(s) to Breakeven (entry ± offset) and leaves the
        top 2 most profitable / deepest layers untouched to run.
        """
        pip_size = self.get_pip_size(symbol)
        offset_value = be_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        buy_order_type = _mt5_const("ORDER_TYPE_BUY", 0)

        be_tickets: List[int] = []
        held_tickets: List[int] = []
        failed_be_tickets: List[Tuple[int, str]] = []

        if self.dry_run:
            if len(market_tickets) > 2:
                worst = market_tickets[:-2]
                held_tickets = list(market_tickets[-2:])
                for t in worst:
                    logger.info(f"[DRY-RUN] Moved worst entry Ticket #{t} to Breakeven (+{be_offset_pips} pips)")
                    be_tickets.append(t)
            else:
                held_tickets = list(market_tickets)
                logger.info(f"[DRY-RUN] <= 2 open orders ({held_tickets}); holding all layers to run.")
            return {
                "be_tickets": be_tickets,
                "held_tickets": held_tickets,
                "failed_be_tickets": failed_be_tickets,
            }

        active_positions = []
        with self._mt5_lock:
            for ticket in market_tickets:
                pos = mt5.positions_get(ticket=ticket)
                if pos:
                    active_positions.append(pos[0])

        if not active_positions:
            return {"be_tickets": [], "held_tickets": [], "failed_be_tickets": []}

        active_positions.sort(key=lambda p: _num(getattr(p, "profit", 0.0)), reverse=True)
        held_positions = active_positions[:2]
        worst_positions = active_positions[2:]

        held_tickets = [_int_num(getattr(p, "ticket", 0), 0) for p in held_positions]

        if raw_tp3:
            for hp in held_positions:
                hp_ticket = _int_num(getattr(hp, "ticket", 0), 0)
                is_buy = _int_num(getattr(hp, "type", buy_order_type), buy_order_type) == buy_order_type
                hp_tp = _num(getattr(hp, "tp", 0.0), 0.0)
                hp_sl = _num(getattr(hp, "sl", 0.0), 0.0)
                should_extend = (
                    hp_tp == 0.0
                    or (is_buy and raw_tp3 > hp_tp)
                    or (not is_buy and raw_tp3 < hp_tp)
                )
                if should_extend and hp_ticket:
                    self.modify_position_sltp(ticket=hp_ticket, sl=hp_sl, tp=round(raw_tp3, digits))

        for wp in worst_positions:
            wp_ticket = _int_num(getattr(wp, "ticket", 0), 0)
            if not wp_ticket:
                continue
            price_open = _num(getattr(wp, "price_open", 0.0), 0.0)
            wp_sl = _num(getattr(wp, "sl", 0.0), 0.0)
            wp_tp = _num(getattr(wp, "tp", 0.0), 0.0)
            wp_profit = _num(getattr(wp, "profit", 0.0), 0.0)
            wp_is_buy = _int_num(getattr(wp, "type", buy_order_type), buy_order_type) == buy_order_type
            if wp_is_buy:
                new_sl = round(price_open + offset_value, digits)
            else:
                new_sl = round(price_open - offset_value, digits)

            already_better = wp_sl > 0.0 and (
                (wp_is_buy and wp_sl >= new_sl - 1e-6)
                or (not wp_is_buy and wp_sl <= new_sl + 1e-6)
            )
            if already_better:
                be_tickets.append(wp_ticket)
                continue

            if self.modify_position_sltp(ticket=wp_ticket, sl=new_sl, tp=wp_tp):
                be_tickets.append(wp_ticket)
                logger.info(
                    f"Worst entry Ticket #{wp_ticket} (profit={wp_profit:.2f}) moved to BE: "
                    f"Open={price_open}, NewSL={new_sl} | Holding top 2 layers {held_tickets}"
                )
            else:
                err = self.last_error or "Broker rejected BE SL"
                failed_be_tickets.append((wp_ticket, err))
                logger.warning(f"Could not move worst entry #{wp_ticket} to BE ({err})")

        return {
            "be_tickets": be_tickets,
            "held_tickets": held_tickets,
            "failed_be_tickets": failed_be_tickets,
        }

    def apply_breakeven(
        self,
        market_tickets: List[int],
        pending_tickets: List[int],
        symbol: str,
        direction: str,
        be_offset_pips: Optional[float] = None,
        layers: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> bool:
        """Apply selective breakeven if layers provided; otherwise move all market tickets to BE."""
        eff_offset = float(be_offset_pips) if be_offset_pips is not None else float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS))
        if layers:
            self.apply_selective_breakeven(
                layers=layers,
                symbol=symbol,
                direction=direction,
                be_layers=None,
                be_offset_pips=eff_offset,
            )
            return True

        pip_size = self.get_pip_size(symbol)
        offset_value = eff_offset * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

        for ticket in market_tickets:
            if self.dry_run:
                logger.info(f"[DRY-RUN] Moved SL to Breakeven for Position #{ticket}")
                continue
            self._move_ticket_to_be(ticket, offset_value, digits, direction)

        return True

    def get_live_positions(self, symbol: Optional[str] = None) -> List[Any]:
        """Thread-safe reader returning all open MT5 positions (optionally filtered by symbol)."""
        if self.dry_run or not self._connected:
            self._last_live_query_ok = False
            self._last_live_pos_query_ok = False
            return []
        try:
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol) if symbol else None
                positions = mt5.positions_get(symbol=resolved) if resolved else mt5.positions_get()
            if positions is None:
                self._last_live_query_ok = False
                self._last_live_pos_query_ok = False
                return []
            self._last_live_query_ok = True
            self._last_live_pos_query_ok = True
            pos_list = list(positions)
            for p in pos_list:
                pt = _int_num(getattr(p, "ticket", 0), 0)
                if pt > 0:
                    self._cached_pos_profits[pt] = round(
                        _num(getattr(p, "profit", 0.0), 0.0) + _num(getattr(p, "swap", 0.0), 0.0),
                        2,
                    )
            return pos_list
        except Exception as e:
            logger.debug(f"get_live_positions error: {e}")
            self._last_live_query_ok = False
            self._last_live_pos_query_ok = False
            return []

    def get_live_orders(self, symbol: Optional[str] = None) -> List[Any]:
        """Thread-safe reader returning all pending MT5 orders (optionally filtered by symbol)."""
        if self.dry_run or not self._connected:
            self._last_live_query_ok = False
            self._last_live_ord_query_ok = False
            return []
        try:
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol) if symbol else None
                orders = mt5.orders_get(symbol=resolved) if resolved else mt5.orders_get()
            if orders is None:
                self._last_live_query_ok = False
                self._last_live_ord_query_ok = False
                return []
            self._last_live_query_ok = True
            self._last_live_ord_query_ok = True
            return list(orders)
        except Exception as e:
            logger.debug(f"get_live_orders error: {e}")
            self._last_live_query_ok = False
            self._last_live_ord_query_ok = False
            return []

    def cancel_signal_pending_orders(
        self,
        signal_data: Dict[str, Any],
        include_all_placed: bool = True,
    ) -> List[int]:
        """
        Sweep all pending LIMIT/STOP orders belonging to `signal_data`, including any orphans in
        `mt5_order_tickets`, `all_placed_tickets`, or `layers[*].ticket`.
        """
        if not isinstance(signal_data, dict):
            return []

        market_set: Set[int] = {
            _int_num(x, 0)
            for x in (
                list(signal_data.get("market_tickets") or [])
                + list(signal_data.get("runner_tickets") or [])
                + ([signal_data.get("runner_ticket")] if signal_data.get("runner_ticket") else [])
                + list(signal_data.get("accounted_tickets") or [])
                + list(signal_data.get("closed_tickets") or [])
            )
            if _int_num(x, 0) > 0
        }
        layers = signal_data.get("layers")
        if isinstance(layers, dict):
            for l_info in layers.values():
                if not isinstance(l_info, dict):
                    continue
                lt = _int_num(l_info.get("ticket"), 0)
                st = str(l_info.get("status") or "").strip().upper()
                if lt > 0 and st in ("OPEN", "FILLED", "RUNNER", "CLOSED"):
                    market_set.add(lt)

        live_ord_ids: Set[int] = set()
        live_orders_checked = False
        if not self.dry_run and self._connected:
            try:
                live_orders = self.get_live_orders()
                if self._last_live_ord_query_ok:
                    live_orders_checked = True
                    live_ord_ids = {
                        _int_num(getattr(o, "ticket", 0), 0)
                        for o in live_orders
                        if _int_num(getattr(o, "ticket", 0), 0) > 0
                    }
            except Exception:
                live_ord_ids = set()
                live_orders_checked = False

        layer_status_by_ticket: Dict[int, str] = {}
        if isinstance(layers, dict):
            for l_info in layers.values():
                if isinstance(l_info, dict):
                    lt = _int_num(l_info.get("ticket"), 0)
                    st = str(l_info.get("status") or "").strip().upper()
                    if lt > 0 and st:
                        layer_status_by_ticket[lt] = st

        candidates: List[int] = []
        seen: Set[int] = set()

        def _add_candidate(val: Any) -> None:
            t_int = _int_num(val, 0)
            if t_int > 0 and t_int not in market_set and t_int not in seen:
                seen.add(t_int)
                candidates.append(t_int)

        for pt in (signal_data.get("pending_tickets") or []):
            _add_candidate(pt)
        if isinstance(layers, dict):
            for l_info in layers.values():
                if not isinstance(l_info, dict):
                    continue
                lt = _int_num(l_info.get("ticket"), 0)
                st = str(l_info.get("status") or "").strip().upper()
                if lt > 0 and (
                    st == "PENDING"
                    or (live_orders_checked and lt in live_ord_ids and st not in ("OPEN", "FILLED", "RUNNER", "CLOSED"))
                ):
                    _add_candidate(lt)
        if include_all_placed:
            for key in ("all_placed_tickets", "mt5_order_tickets"):
                for t in (signal_data.get(key) or []):
                    t_int = _int_num(t, 0)
                    if t_int <= 0 or t_int in market_set:
                        continue
                    if live_orders_checked:
                        if t_int in live_ord_ids:
                            _add_candidate(t_int)
                    else:
                        if layer_status_by_ticket.get(t_int) not in ("CLOSED", "CANCELLED"):
                            _add_candidate(t_int)

        cancelled: List[int] = []
        cancelled_set: Set[int] = set()
        for t in candidates:
            if self.cancel_pending_order(t):
                cancelled.append(t)
                cancelled_set.add(t)

        if cancelled_set or live_orders_checked:
            signal_data["pending_tickets"] = [
                pt for pt in (signal_data.get("pending_tickets") or [])
                if _int_num(pt, 0) > 0
                and _int_num(pt, 0) not in cancelled_set
                and (not live_orders_checked or _int_num(pt, 0) in live_ord_ids)
            ]
            if cancelled_set and isinstance(layers, dict):
                for l_info in layers.values():
                    if isinstance(l_info, dict) and _int_num(l_info.get("ticket"), 0) in cancelled_set:
                        st = str(l_info.get("status") or "").strip().upper()
                        if st not in ("OPEN", "FILLED", "RUNNER", "CLOSED"):
                            l_info["status"] = "CANCELLED"

        return cancelled

    def get_bot_positions(self, symbol: Optional[str] = None) -> List[Any]:
        """Retrieve all active MT5 market positions placed by this bot (magic == MAGIC_NUMBER)."""
        magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
        return [p for p in self.get_live_positions(symbol) if _int_num(getattr(p, "magic", 0), 0) == magic]

    def get_bot_pending_orders(self, symbol: Optional[str] = None) -> List[Any]:
        """Retrieve all unfilled MT5 pending orders placed by this bot (magic == MAGIC_NUMBER)."""
        magic = _cfg("MAGIC_NUMBER", MAGIC_NUMBER)
        return [o for o in self.get_live_orders(symbol) if _int_num(getattr(o, "magic", 0), 0) == magic]

    def apply_proximity_close(
        self,
        market_tickets: List[int],
        pending_tickets: List[int],
        symbol: Optional[str] = None,
        sweep_all_magic: bool = False,
    ) -> None:
        """
        Close all recorded market positions and cancel all recorded pending orders for the target signal.
        Never sweeps unrelated signals' orders from other channels unless `sweep_all_magic=True`.
        """
        closed_set = set()
        for ticket in list(market_tickets):
            if self.close_market_position(ticket):
                closed_set.add(ticket)

        for ticket in list(pending_tickets):
            if not self.cancel_pending_order(ticket):
                # Fill-race fallback: if pending order filled right before cancel, close the filled position
                if not self.dry_run and self._connected:
                    try:
                        with self._mt5_lock:
                            filled_p = mt5.positions_get(ticket=ticket)
                    except Exception:
                        filled_p = None
                    if filled_p and self.close_market_position(ticket):
                        closed_set.add(ticket)

        if sweep_all_magic and not self.dry_run and self._connected:
            for p in self.get_bot_positions(symbol):
                p_t = _int_num(getattr(p, "ticket", 0), 0)
                if p_t and p_t not in closed_set:
                    logger.info(f"Safety sweep: Closing unrecorded filled bot position #{p_t} ({getattr(p, 'symbol', '')})")
                    self.close_market_position(p_t)

            for o in self.get_bot_pending_orders(symbol):
                o_t = _int_num(getattr(o, "ticket", 0), 0)
                if o_t:
                    logger.info(f"Safety sweep: Cancelling remaining bot pending order #{o_t} ({getattr(o, 'symbol', '')})")
                    self.cancel_pending_order(o_t)

        self.invalidate_realized_pnl_cache()
        logger.info(
            f"Proximity Auto-Close executed: Closed {len(market_tickets)} market positions, "
            f"Cancelled {len(pending_tickets)} pending orders."
        )

    apply_full_close = apply_proximity_close

    def close_signal_exposure(self, signal_data: Dict[str, Any]) -> Dict[str, List[int]]:
        """
        Surgically close all open market positions and cancel all unfilled pending orders
        belonging ONLY to `signal_data` (never sweeping unrelated active signals by MAGIC_NUMBER).
        Guards against pending-to-market fill races by checking live MT5 positions first.
        """
        if not isinstance(signal_data, dict):
            return {"closed_tickets": [], "cancelled_orders": []}

        accounted_set = {
            _int_num(x, 0)
            for x in (signal_data.get("accounted_tickets") or [])
            if _int_num(x, 0) > 0
        }

        all_signal_tickets: List[int] = []
        all_set = set()
        known_market_list: List[int] = []
        known_market_set = set()
        known_pending_list: List[int] = []
        known_pending_set = set()

        def _add_all(val: Any) -> int:
            t_int = _int_num(val, 0)
            if t_int > 0 and t_int not in accounted_set and t_int not in all_set:
                all_set.add(t_int)
                all_signal_tickets.append(t_int)
            return t_int if (t_int > 0 and t_int not in accounted_set) else 0

        def _add_market(val: Any) -> None:
            t_int = _add_all(val)
            if t_int > 0 and t_int not in known_market_set:
                known_market_set.add(t_int)
                known_market_list.append(t_int)

        def _add_pending(val: Any) -> None:
            t_int = _add_all(val)
            if t_int > 0 and t_int not in known_pending_set:
                known_pending_set.add(t_int)
                known_pending_list.append(t_int)

        for t in (signal_data.get("market_tickets") or []):
            _add_market(t)
        for t in (signal_data.get("runner_tickets") or []):
            _add_market(t)
        if signal_data.get("runner_ticket") is not None:
            _add_market(signal_data.get("runner_ticket"))

        for t in (signal_data.get("pending_tickets") or []):
            _add_pending(t)

        for t in (signal_data.get("mt5_order_tickets") or []):
            _add_all(t)
        for t in (signal_data.get("all_placed_tickets") or []):
            _add_all(t)

        layers = signal_data.get("layers")
        if isinstance(layers, dict):
            for l_info in layers.values():
                if not isinstance(l_info, dict):
                    continue
                lt = l_info.get("ticket")
                st = str(l_info.get("status") or "").strip().upper()
                if st in ("OPEN", "FILLED", "RUNNER"):
                    _add_market(lt)
                elif st == "PENDING":
                    _add_pending(lt)
                else:
                    _add_all(lt)

        closed_tickets: List[int] = []
        cancelled_orders: List[int] = []

        if self.dry_run or not self._ensure_connected():
            for t in known_market_list:
                if self.close_market_position(t):
                    closed_tickets.append(t)
            closed_set = set(closed_tickets)
            for t in known_pending_list:
                if t not in closed_set and t not in known_market_set:
                    if self.cancel_pending_order(t):
                        cancelled_orders.append(t)
        else:
            live_pos_ids = set()
            live_ord_ids = set()
            try:
                with self._mt5_lock:
                    positions = mt5.positions_get() or []
                    orders = mt5.orders_get() or []
                live_pos_ids = {
                    _int_num(getattr(p, "ticket", 0), 0)
                    for p in positions
                    if _int_num(getattr(p, "ticket", 0), 0) > 0
                }
                live_ord_ids = {
                    _int_num(getattr(o, "ticket", 0), 0)
                    for o in orders
                    if _int_num(getattr(o, "ticket", 0), 0) > 0
                }
            except Exception as e:
                logger.warning(f"close_signal_exposure: could not query live positions/orders: {e}")

            for t in all_signal_tickets:
                if t in live_pos_ids:
                    if self.close_market_position(t):
                        closed_tickets.append(t)
                elif t in live_ord_ids:
                    if self.cancel_pending_order(t):
                        cancelled_orders.append(t)
                    else:
                        recheck_pos = None
                        try:
                            with self._mt5_lock:
                                recheck_pos = mt5.positions_get(ticket=t)
                        except Exception:
                            recheck_pos = None
                        if recheck_pos and self.close_market_position(t):
                            closed_tickets.append(t)
                else:
                    if t in known_market_set:
                        if self.close_market_position(t):
                            closed_tickets.append(t)
                    elif t in known_pending_set:
                        if self.cancel_pending_order(t):
                            cancelled_orders.append(t)

        if closed_tickets:
            self.invalidate_realized_pnl_cache()

        return {"closed_tickets": closed_tickets, "cancelled_orders": cancelled_orders}

    def _compute_initial_tp1_floor(
        self,
        signal_data: Dict[str, Any],
        symbol: str,
        direction: str,
        pip_size: float,
        digits: int,
    ) -> float:
        """Compute initial TP1 protection floor using dynamic pip_size scaling."""
        raw_tp1 = signal_data.get("raw_tp1")
        raw_lock = float(_cfg("TP1_LOCK_BUFFER", TP1_LOCK_BUFFER))
        lock_buf = raw_lock * (pip_size / 0.10) if pip_size > 0 else raw_lock
        is_buy = direction == "BUY"

        if raw_tp1 is not None:
            return round(float(raw_tp1) - lock_buf, digits) if is_buy else round(float(raw_tp1) + lock_buf, digits)

        tick = self.get_tick(symbol)
        raw_px = None
        if tick is not None:
            raw_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        if not isinstance(raw_px, (int, float)) or isinstance(raw_px, bool) or raw_px <= 0:
            raw_px = signal_data.get("recorded_entry_price")
        if not isinstance(raw_px, (int, float)) or isinstance(raw_px, bool) or raw_px <= 0:
            raw_px = 3895.0
        return round(float(raw_px) - lock_buf, digits) if is_buy else round(float(raw_px) + lock_buf, digits)

    def _compute_tp1_touch_floor(
        self,
        raw_tp1: float,
        direction: str,
        pip_size: float,
        digits: int,
    ) -> float:
        """Compute TP1-touch bank protection floor (TP1 - $1.20 BUY / TP1 + $1.20 SELL by default)."""
        raw_buf_pips = float(_cfg("TP1_TOUCH_BANK_BUFFER_PIPS", TP1_TOUCH_BANK_BUFFER_PIPS))
        touch_buf = raw_buf_pips * (pip_size if pip_size > 0 else 0.10)
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        return round(float(raw_tp1) - touch_buf, digits) if is_buy else round(float(raw_tp1) + touch_buf, digits)

    def _clamp_sl_for_broker_stops(
        self,
        desired_sl: float,
        direction: str,
        tick: Any,
        sym_info: Any,
        digits: int,
    ) -> float:
        """Clamp SL distance to at least broker trade_stops_level + STOP_LEVEL_SAFETY_BUFFER_DOLLARS."""
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
        raw_dist = max(0.0, stops_lvl * (pt or 0.01))
        safety_buf = float(_cfg("STOP_LEVEL_SAFETY_BUFFER_DOLLARS", STOP_LEVEL_SAFETY_BUFFER_DOLLARS)) if (not self.dry_run or raw_dist > 0.0) else 0.0
        min_stop_dist = raw_dist + max(0.0, safety_buf) if raw_dist > 0.0 else 0.0
        target_sl = round(float(desired_sl), digits)
        if tick is None or min_stop_dist <= 0.0:
            return target_sl
        ref_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        if not isinstance(ref_px, numbers.Real) or isinstance(ref_px, bool) or float(ref_px) <= 0.0:
            return target_sl
        ref_f = float(ref_px)
        if is_buy:
            return min(target_sl, round(ref_f - min_stop_dist, digits))
        return max(target_sl, round(ref_f + min_stop_dist, digits))

    def _apply_tp1_touch_protection(
        self,
        signal_data: Dict[str, Any],
        symbol: str,
        direction: str,
        raw_tp1: float,
        pip_size: float,
        digits: int,
        tick: Any = None,
        sym_info: Any = None,
        skip_tickets: Sequence[int] = (),
        preferred_floor: Optional[float] = None,
    ) -> Optional[float]:
        """
        Persist tp1_touched=True, ratchet runner virtual floor to at least TP1 - buffer (BUY)
        or TP1 + buffer (SELL), and apply hard broker SL (clamped to stops level) across
        ALL remaining open layers (runner + any remaining L2/L3 positions).
        """
        if not bool(_cfg("TP1_TOUCH_BANK_ENABLED", TP1_TOUCH_BANK_ENABLED)):
            return None

        is_buy = (direction or "BUY").strip().upper() == "BUY"
        touch_floor = self._compute_tp1_touch_floor(float(raw_tp1), direction, pip_size, digits)
        signal_data["tp1_touched"] = True
        signal_data["tp1_touch_floor"] = touch_floor

        cands = [touch_floor]
        for prev_key in ("runner_sl_floor", "soft_tp1_floor"):
            pv = signal_data.get(prev_key)
            if isinstance(pv, numbers.Real) and not isinstance(pv, bool) and float(pv) > 0.0:
                cands.append(float(pv))
        if isinstance(preferred_floor, numbers.Real) and not isinstance(preferred_floor, bool) and float(preferred_floor) > 0.0:
            cands.append(float(preferred_floor))
        eff_floor = round(max(cands) if is_buy else min(cands), digits)

        signal_data["runner_sl_floor"] = eff_floor
        signal_data["soft_tp1_floor"] = eff_floor

        if sym_info is None:
            sym_info = self.get_symbol_info(symbol)
        if tick is None:
            tick = self.get_tick(symbol)
        desired_broker_sl = self._clamp_sl_for_broker_stops(eff_floor, direction, tick, sym_info, digits)

        layers = signal_data.get("layers") or {}
        ticket_to_layer: Dict[int, Dict[str, Any]] = {}
        for l_val in layers.values():
            if isinstance(l_val, dict):
                lt = _int_num(l_val.get("ticket"), 0)
                if lt > 0:
                    ticket_to_layer[lt] = l_val

        skip_set = {_int_num(t, 0) for t in skip_tickets if _int_num(t, 0) > 0}
        raw_rts = signal_data.get("runner_tickets") or (
            [signal_data.get("runner_ticket")] if signal_data.get("runner_ticket") else []
        )
        runner_tkts = [_int_num(t, 0) for t in raw_rts if _int_num(t, 0) > 0]
        runner_set = set(runner_tkts)
        all_open = list(
            dict.fromkeys(
                t
                for t in (runner_tkts + [_int_num(x, 0) for x in (signal_data.get("market_tickets") or [])])
                if t > 0 and t not in skip_set
            )
        )

        for tkt in all_open:
            l_info = ticket_to_layer.get(tkt)
            cur_sl = 0.0
            if not self.dry_run:
                with self._mt5_lock:
                    pos_res = mt5.positions_get(ticket=tkt)
                if pos_res:
                    cur_sl = _num(getattr(pos_res[0], "sl", 0.0), 0.0)
            if cur_sl <= 0.0 and l_info is not None:
                for sl_k in ("sl", "be_sl"):
                    sv = _num(l_info.get(sl_k), 0.0)
                    if sv > 0.0:
                        cur_sl = sv
                        break

            # Forward-only ratchet: never loosen an already tighter broker SL
            if cur_sl > 0.0:
                if (is_buy and desired_broker_sl <= cur_sl + 1e-6) or (not is_buy and desired_broker_sl >= cur_sl - 1e-6):
                    continue

            if self.modify_position_sltp(tkt, sl=desired_broker_sl, tp=None):
                if l_info is not None:
                    l_info["sl"] = desired_broker_sl
                if tkt in runner_set:
                    signal_data["broker_sl"] = desired_broker_sl

        return eff_floor

    @staticmethod
    def get_runner_fsm_stage(signal_data: Optional[Dict[str, Any]]) -> int:
        """Return canonical v3.5.0 runner stage (0..4) for signal_data."""
        return _get_runner_fsm_stage(signal_data)

    def _get_staged_trail_tier(self, stage: int) -> Tuple[float, float, float]:
        """
        Return normalized (atr_mult, min_dollars, max_dollars) for stage (1..4),
        reading dynamic _cfg(...) keys and swapping (min_d, max_d) if min_d > max_d.
        """
        eff_stage = max(1, min(4, _int_num(stage, 1)))
        if eff_stage == 1:
            atr_mult = float(_cfg("TP1_TRAIL_ATR_MULT", TP1_TRAIL_ATR_MULT))
            min_d = float(_cfg("TP1_TRAIL_MIN_DOLLARS", TP1_TRAIL_MIN_DOLLARS))
            max_d = float(_cfg("TP1_TRAIL_MAX_DOLLARS", TP1_TRAIL_MAX_DOLLARS))
        elif eff_stage == 2:
            atr_mult = float(_cfg("TRAIL_TP2_ATR_MULT", TRAIL_TP2_ATR_MULT))
            min_d = float(_cfg("TRAIL_TP2_MIN", TRAIL_TP2_MIN))
            max_d = float(_cfg("TRAIL_TP2_MAX", TRAIL_TP2_MAX))
        elif eff_stage == 3:
            atr_mult = float(_cfg("TRAIL_TP3_ATR_MULT", TRAIL_TP3_ATR_MULT))
            min_d = float(_cfg("TRAIL_TP3_MIN", TRAIL_TP3_MIN))
            max_d = float(_cfg("TRAIL_TP3_MAX", TRAIL_TP3_MAX))
        else:
            atr_mult = float(_cfg("TRAIL_TP4_ATR_MULT", TRAIL_TP4_ATR_MULT))
            min_d = float(_cfg("TRAIL_TP4_MIN", TRAIL_TP4_MIN))
            max_d = float(_cfg("TRAIL_TP4_MAX", TRAIL_TP4_MAX))
        if min_d > max_d:
            min_d, max_d = max_d, min_d
        return atr_mult, min_d, max_d

    def _compute_staged_trail_dist(
        self,
        symbol: str,
        stage: int,
        digits: int,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Compute M1-ATR-calibrated staged trailing stop distance for `stage` (1..4)
        and return (trail_dist, trail_tier_used_dict).
        Falls back to midpoint (min_d + max_d) / 2 when M1 ATR(14) <= 0.
        """
        eff_stage = max(1, min(4, _int_num(stage, 1)))
        atr_mult, min_d, max_d = self._get_staged_trail_tier(eff_stage)
        m1_atr = self.get_m1_atr(symbol)
        if isinstance(m1_atr, numbers.Real) and not isinstance(m1_atr, bool) and math.isfinite(float(m1_atr)) and float(m1_atr) > 0.0:
            atr_val = float(m1_atr)
            trail_dist = round(max(min_d, min(max_d, atr_mult * atr_val)), digits)
        else:
            atr_val = 0.0
            trail_dist = round((min_d + max_d) / 2.0, digits)
        tier_used: Dict[str, Any] = {
            "stage": eff_stage,
            "atr_mult": round(float(atr_mult), 4),
            "min_dollars": round(float(min_d), 4),
            "max_dollars": round(float(max_d), 4),
            "m1_atr_14": round(float(atr_val), 4),
            "trail_dist": float(trail_dist),
        }
        return trail_dist, tier_used

    def _compute_staged_fsm_floor(
        self,
        signal_data: Dict[str, Any],
        stage: int,
        tp_levels: Dict[str, float],
        direction: str,
        pip_size: float,
        digits: int,
        runner_entry: Optional[float] = None,
    ) -> Optional[float]:
        """
        Compute PURE structural stage floor for Stage 1..4:
          Stage 1: entry +/- BE_OFFSET_PIPS * pip_size
          Stage 2: TP1
          Stage 3: TP2
          Stage 4: TP3
        Ratcheted ONLY against prior `signal_data.get("fsm_floor")` (NEVER reading
        `runner_sl_floor` or `trail_stop` so ATR stops never pollute `fsm_floor`).
        """
        if stage <= 0:
            return None
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        eff_entry: Optional[float] = None
        if runner_entry is not None and isinstance(runner_entry, numbers.Real) and not isinstance(runner_entry, bool) and float(runner_entry) > 0.0:
            eff_entry = float(runner_entry)
        else:
            layers = signal_data.get("layers") or {}
            raw_rls = signal_data.get("runner_layers")
            rl_key = (
                str(raw_rls[-1])
                if (isinstance(raw_rls, list) and raw_rls)
                else signal_data.get("runner_layer")
            )
            if rl_key and isinstance(layers.get(rl_key), dict):
                tp_px = layers[rl_key].get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0.0:
                    eff_entry = float(tp_px)
            if eff_entry is None:
                eff_entry = self._resolve_signal_primary_entry(signal_data)

        be_offset = float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS)) * (pip_size if pip_size > 0 else 0.10)
        cands: List[float] = []
        if eff_entry is not None and eff_entry > 0.0:
            be_floor = round(eff_entry + be_offset, digits) if is_buy else round(eff_entry - be_offset, digits)
            cands.append(be_floor)

        if stage >= 2:
            tp1_v = tp_levels.get("TP1") if isinstance(tp_levels, dict) else None
            if tp1_v is None:
                tp1_v = signal_data.get("raw_tp1")
            if isinstance(tp1_v, numbers.Real) and not isinstance(tp1_v, bool) and float(tp1_v) > 0.0:
                cands.append(round(float(tp1_v), digits))
        if stage >= 3:
            tp2_v = tp_levels.get("TP2") if isinstance(tp_levels, dict) else None
            if tp2_v is None:
                tp2_v = signal_data.get("raw_tp2")
            if isinstance(tp2_v, numbers.Real) and not isinstance(tp2_v, bool) and float(tp2_v) > 0.0:
                cands.append(round(float(tp2_v), digits))
        if stage >= 4:
            tp3_v = tp_levels.get("TP3") if isinstance(tp_levels, dict) else None
            if tp3_v is None:
                tp3_v = signal_data.get("raw_tp3")
            if isinstance(tp3_v, numbers.Real) and not isinstance(tp3_v, bool) and float(tp3_v) > 0.0:
                cands.append(round(float(tp3_v), digits))

        prev_fsm = signal_data.get("fsm_floor")
        if isinstance(prev_fsm, numbers.Real) and not isinstance(prev_fsm, bool) and float(prev_fsm) > 0.0:
            cands.append(round(float(prev_fsm), digits))

        if not cands:
            return None
        return round(max(cands) if is_buy else min(cands), digits)

    def _populate_exit_telemetry(
        self,
        signal_data: Dict[str, Any],
        exit_reason_code: Optional[str],
        exit_price: Optional[float] = None,
        exit_stage: Optional[int] = None,
        runner_entry: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Populate v3.5.0 structured exit telemetry fields on `signal_data` and emit
        a structured `[EXIT TELEMETRY]` log entry.
        """
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

        if exit_stage is not None and isinstance(exit_stage, numbers.Integral) and not isinstance(exit_stage, bool):
            stage_val = max(0, min(4, int(exit_stage)))
        else:
            stage_val = self.get_runner_fsm_stage(signal_data)

        eff_exit_price: Optional[float] = None
        if exit_price is not None and isinstance(exit_price, numbers.Real) and not isinstance(exit_price, bool) and float(exit_price) > 0.0:
            eff_exit_price = round(float(exit_price), 4)
        else:
            prev_ep = signal_data.get("exit_price")
            if isinstance(prev_ep, numbers.Real) and not isinstance(prev_ep, bool) and float(prev_ep) > 0.0:
                eff_exit_price = round(float(prev_ep), 4)

        peak_px: Optional[float] = None
        for pk_key in ("trail_peak", "runner_peak_price", "peak_price"):
            pv = signal_data.get(pk_key)
            if isinstance(pv, numbers.Real) and not isinstance(pv, bool) and float(pv) > 0.0:
                peak_px = round(float(pv), digits)
                break
        if peak_px is None and eff_exit_price is not None:
            peak_px = round(float(eff_exit_price), digits)
        elif peak_px is not None and eff_exit_price is not None and exit_reason_code == "TP_CEILING":
            peak_px = round(max(peak_px, float(eff_exit_price)) if is_buy else min(peak_px, float(eff_exit_price)), digits)

        ref_entry: Optional[float] = None
        if runner_entry is not None and isinstance(runner_entry, numbers.Real) and not isinstance(runner_entry, bool) and float(runner_entry) > 0.0:
            ref_entry = float(runner_entry)
        else:
            layers = signal_data.get("layers") or {}
            raw_rls = signal_data.get("runner_layers")
            rl_key = (
                str(raw_rls[-1])
                if (isinstance(raw_rls, list) and raw_rls)
                else signal_data.get("runner_layer")
            )
            if rl_key and isinstance(layers.get(rl_key), dict):
                tp_px = layers[rl_key].get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0.0:
                    ref_entry = float(tp_px)
            if ref_entry is None:
                ref_entry = self._resolve_signal_primary_entry(signal_data)

        peak_profit_per_oz: Optional[float] = None
        if peak_px is not None and ref_entry is not None and ref_entry > 0.0:
            peak_profit_per_oz = round(
                max(0.0, (peak_px - ref_entry) if is_buy else (ref_entry - peak_px)),
                4,
            )
        else:
            raw_pp = signal_data.get("peak_profit")
            if isinstance(raw_pp, numbers.Real) and not isinstance(raw_pp, bool) and float(raw_pp) >= 0.0:
                peak_profit_per_oz = round(float(raw_pp), 4)

        mfe: Dict[str, Optional[float]] = {
            "peak_price": peak_px,
            "peak_profit_per_oz": peak_profit_per_oz,
        }

        if exit_reason_code == "TP_CEILING":
            giveback: Optional[float] = 0.0
        elif peak_px is not None and eff_exit_price is not None:
            giveback = round(
                max(0.0, (peak_px - eff_exit_price) if is_buy else (eff_exit_price - peak_px)),
                4,
            )
        else:
            giveback = None

        if stage_val == 0:
            tier_used: Optional[Dict[str, Any]] = None
        else:
            cur_tier = signal_data.get("trail_tier_used")
            if isinstance(cur_tier, dict):
                tier_used = dict(cur_tier)
            else:
                _, tier_used = self._compute_staged_trail_dist(symbol, stage_val, digits)

        if exit_reason_code is not None:
            signal_data["exit_reason_code"] = str(exit_reason_code)
        signal_data["exit_stage"] = stage_val
        signal_data["exit_price"] = eff_exit_price
        signal_data["max_favorable_excursion"] = mfe
        signal_data["giveback_from_peak"] = giveback
        signal_data["trail_tier_used"] = tier_used

        logger.info(
            f"[EXIT TELEMETRY] symbol={symbol} direction={direction} "
            f"exit_reason_code={signal_data.get('exit_reason_code')} "
            f"exit_stage={stage_val} exit_price={eff_exit_price} "
            f"mfe={mfe} giveback_from_peak={giveback} trail_tier_used={tier_used}"
        )
        return {
            "exit_reason_code": signal_data.get("exit_reason_code"),
            "exit_stage": stage_val,
            "exit_price": eff_exit_price,
            "max_favorable_excursion": mfe,
            "giveback_from_peak": giveback,
            "trail_tier_used": tier_used,
        }

    def _compute_tp1_trail_dist(self, symbol: str, digits: int) -> float:
        """
        Compute M1-ATR-calibrated TP1 trailing stop distance clamped to
        [TP1_TRAIL_MIN_DOLLARS, TP1_TRAIL_MAX_DOLLARS]. Falls back to midpoint ($2.50)
        when M1 ATR(14) is unavailable.
        """
        atr_mult = float(_cfg("TP1_TRAIL_ATR_MULT", TP1_TRAIL_ATR_MULT))
        min_d = float(_cfg("TP1_TRAIL_MIN_DOLLARS", TP1_TRAIL_MIN_DOLLARS))
        max_d = max(min_d, float(_cfg("TP1_TRAIL_MAX_DOLLARS", TP1_TRAIL_MAX_DOLLARS)))
        m1_atr = self.get_m1_atr(symbol)
        if not isinstance(m1_atr, numbers.Real) or isinstance(m1_atr, bool) or float(m1_atr) <= 0.0:
            return round((min_d + max_d) / 2.0, digits)
        raw = atr_mult * float(m1_atr)
        return round(max(min_d, min(max_d, raw)), digits)

    def _update_tp1_trail_state(
        self,
        signal_data: Dict[str, Any],
        symbol: str,
        direction: str,
        live_price: float,
        pip_size: float,
        digits: int,
        runner_entry_fallback: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """
        Update dynamic ATR TP1 trailing state (`trail_peak`, `trail_dist`, `trail_stop`)
        with forward-only ratcheting and a breakeven (+BE_OFFSET) floor from primary entry.
        """
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        trail_dist = self._compute_tp1_trail_dist(symbol, digits)

        prev_peak = signal_data.get("trail_peak")
        if isinstance(prev_peak, numbers.Real) and not isinstance(prev_peak, bool) and float(prev_peak) > 0.0:
            trail_peak = round(
                max(float(prev_peak), float(live_price)) if is_buy else min(float(prev_peak), float(live_price)),
                digits,
            )
        else:
            trail_peak = round(float(live_price), digits)

        cand_stop = round((trail_peak - trail_dist) if is_buy else (trail_peak + trail_dist), digits)

        primary_entry = self._resolve_signal_primary_entry(signal_data)
        if primary_entry is None and runner_entry_fallback is not None and float(runner_entry_fallback) > 0.0:
            primary_entry = float(runner_entry_fallback)
        be_offset = float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS)) * (pip_size if pip_size > 0 else 0.10)
        be_floor: Optional[float] = None
        if primary_entry is not None and float(primary_entry) > 0.0:
            be_floor = (
                round(float(primary_entry) + be_offset, digits)
                if is_buy
                else round(float(primary_entry) - be_offset, digits)
            )

        cands = [cand_stop]
        if be_floor is not None:
            cands.append(be_floor)
        for prev_key in ("trail_stop", "runner_sl_floor", "soft_tp1_floor"):
            pv = signal_data.get(prev_key)
            if isinstance(pv, numbers.Real) and not isinstance(pv, bool) and float(pv) > 0.0:
                cands.append(float(pv))

        trail_stop = round(max(cands) if is_buy else min(cands), digits)

        signal_data["tp1_touched"] = True
        signal_data["trail_dist"] = trail_dist
        signal_data["trail_peak"] = trail_peak
        signal_data["trail_stop"] = trail_stop
        signal_data["runner_sl_floor"] = trail_stop
        signal_data["soft_tp1_floor"] = trail_stop
        return trail_peak, trail_dist, trail_stop

    def _sync_one_ticket_tp1_trail_sl(
        self,
        signal_data: Dict[str, Any],
        ticket: int,
        l_info: Optional[Dict[str, Any]],
        is_runner: bool,
        is_buy: bool,
        desired_broker_sl: float,
        step_dollars: float,
        extra_tp: Optional[float] = None,
        extra_tp_stage: Optional[str] = None,
        force_sl_sync: bool = False,
        fallback_sl: Optional[float] = None,
        tick: Any = None,
        sym_info: Any = None,
        digits: int = 2,
    ) -> bool:
        """
        Apply TP1 trailing broker SL on a single open ticket forward-only, throttling
        repeat updates to >= TP1_TRAIL_STEP_DOLLARS (unless `force_sl_sync=True`)
        while letting explicit TP steps pass. On broker rejection of `desired_broker_sl`,
        retries with clamped `fallback_sl` (e.g. `fsm_floor`) if strictly better than `cur_sl`.
        """
        cur_sl = 0.0
        if not self.dry_run:
            with self._mt5_lock:
                pos_res = mt5.positions_get(ticket=ticket)
            if pos_res:
                cur_sl = _num(getattr(pos_res[0], "sl", 0.0), 0.0)
        if cur_sl <= 0.0 and l_info is not None:
            for sl_k in ("sl", "be_sl"):
                sv = _num(l_info.get(sl_k), 0.0)
                if sv > 0.0:
                    cur_sl = sv
                    break
        if cur_sl <= 0.0 and is_runner:
            sv = _num(signal_data.get("broker_sl"), 0.0)
            if sv > 0.0:
                cur_sl = sv

        tp1_sl_applied = set(_int_num(x, 0) for x in (signal_data.get("_tp1_trail_sl_tickets") or []) if _int_num(x, 0) > 0)
        already_protected = ticket in tp1_sl_applied

        if cur_sl > 0.0:
            is_better = (desired_broker_sl > cur_sl + 1e-9) if is_buy else (desired_broker_sl < cur_sl - 1e-9)
            improvement = (desired_broker_sl - cur_sl) if is_buy else (cur_sl - desired_broker_sl)
        else:
            is_better = True
            improvement = float("inf")

        should_update_sl = is_better and (
            bool(force_sl_sync)
            or (not already_protected)
            or (cur_sl <= 0.0)
            or (improvement + 1e-9 >= step_dollars)
        )

        already_at_tp = (
            extra_tp is not None
            and l_info is not None
            and abs(_num(l_info.get("tp"), 0.0) - extra_tp) < 1e-6
            and (extra_tp_stage is None or l_info.get("tp_stage") == extra_tp_stage)
        )
        tp_arg = extra_tp if (extra_tp is not None and not already_at_tp) else None

        if not should_update_sl:
            if tp_arg is not None:
                if self.modify_position_sltp(ticket, sl=None, tp=tp_arg) and l_info is not None:
                    l_info["tp"] = tp_arg
                    if extra_tp_stage is not None:
                        l_info["tp_stage"] = extra_tp_stage
                return True
            return False

        # Send desired SL (and optional TP step together in one IPC call); fall back to fallback_sl or independent TP if SL rejected
        mod_ok = self.modify_position_sltp(ticket, sl=desired_broker_sl, tp=extra_tp)
        if mod_ok:
            tp1_sl_applied.add(ticket)
            signal_data["_tp1_trail_sl_tickets"] = sorted(tp1_sl_applied)
            if l_info is not None:
                l_info["sl"] = desired_broker_sl
                if extra_tp is not None:
                    l_info["tp"] = extra_tp
                    if extra_tp_stage is not None:
                        l_info["tp_stage"] = extra_tp_stage
            if is_runner:
                signal_data["broker_sl"] = desired_broker_sl
            return True

        if fallback_sl is not None and isinstance(fallback_sl, numbers.Real) and not isinstance(fallback_sl, bool) and float(fallback_sl) > 0.0:
            symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
            eff_sym_info = sym_info if sym_info is not None else self.get_symbol_info(symbol)
            eff_tick = tick if tick is not None else self.get_tick(symbol)
            eff_digits = _int_num(getattr(eff_sym_info, "digits", digits), digits) if eff_sym_info else digits
            clamped_fb = self._clamp_sl_for_broker_stops(
                float(fallback_sl),
                "BUY" if is_buy else "SELL",
                eff_tick,
                eff_sym_info,
                eff_digits,
            )
            fb_better = (cur_sl <= 0.0) or (
                (clamped_fb > cur_sl + 1e-9) if is_buy else (clamped_fb < cur_sl - 1e-9)
            )
            if fb_better and abs(clamped_fb - desired_broker_sl) > 1e-9:
                if self.modify_position_sltp(ticket, sl=clamped_fb, tp=extra_tp):
                    tp1_sl_applied.add(ticket)
                    signal_data["_tp1_trail_sl_tickets"] = sorted(tp1_sl_applied)
                    if l_info is not None:
                        l_info["sl"] = clamped_fb
                        if extra_tp is not None:
                            l_info["tp"] = extra_tp
                            if extra_tp_stage is not None:
                                l_info["tp_stage"] = extra_tp_stage
                    if is_runner:
                        signal_data["broker_sl"] = clamped_fb
                    return True

        if tp_arg is not None:
            if self.modify_position_sltp(ticket, sl=None, tp=tp_arg) and l_info is not None:
                l_info["tp"] = tp_arg
                if extra_tp_stage is not None:
                    l_info["tp_stage"] = extra_tp_stage
        return False

    def _sync_tp1_trail_broker_sl(
        self,
        signal_data: Dict[str, Any],
        symbol: str,
        direction: str,
        desired_sl: float,
        digits: int,
        tick: Any = None,
        sym_info: Any = None,
        skip_tickets: Sequence[int] = (),
        runner_tp: Optional[float] = None,
        runner_stage: Optional[str] = None,
        force_sl_sync: bool = False,
        fallback_sl: Optional[float] = None,
    ) -> int:
        """
        Sync hard broker SL across ALL remaining open layers (runner + non-runner L2/L3)
        subject to stops-level clamping, forward-only ratchet, and TP1_TRAIL_STEP_DOLLARS
        (bypassed when `force_sl_sync=True`).
        """
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        step_dollars = max(0.0, float(_cfg("TP1_TRAIL_STEP_DOLLARS", TP1_TRAIL_STEP_DOLLARS)))

        if sym_info is None:
            sym_info = self.get_symbol_info(symbol)
        if tick is None:
            tick = self.get_tick(symbol)
        desired_broker_sl = self._clamp_sl_for_broker_stops(desired_sl, direction, tick, sym_info, digits)
        clamped_fb_sl: Optional[float] = None
        if fallback_sl is not None and isinstance(fallback_sl, numbers.Real) and not isinstance(fallback_sl, bool) and float(fallback_sl) > 0.0:
            clamped_fb_sl = self._clamp_sl_for_broker_stops(float(fallback_sl), direction, tick, sym_info, digits)

        layers = signal_data.get("layers") or {}
        ticket_to_layer: Dict[int, Dict[str, Any]] = {}
        for l_val in layers.values():
            if isinstance(l_val, dict):
                lt = _int_num(l_val.get("ticket"), 0)
                if lt > 0:
                    ticket_to_layer[lt] = l_val

        skip_set = {_int_num(t, 0) for t in skip_tickets if _int_num(t, 0) > 0}
        raw_rts = signal_data.get("runner_tickets") or (
            [signal_data.get("runner_ticket")] if signal_data.get("runner_ticket") else []
        )
        runner_tkts = [_int_num(t, 0) for t in raw_rts if _int_num(t, 0) > 0]
        runner_set = set(runner_tkts)
        all_open = list(
            dict.fromkeys(
                t
                for t in (runner_tkts + [_int_num(x, 0) for x in (signal_data.get("market_tickets") or [])])
                if t > 0 and t not in skip_set
            )
        )

        modified_cnt = 0
        tp_rounded = round(float(runner_tp), digits) if runner_tp is not None else None
        for tkt in all_open:
            is_r = tkt in runner_set
            if self._sync_one_ticket_tp1_trail_sl(
                signal_data=signal_data,
                ticket=tkt,
                l_info=ticket_to_layer.get(tkt),
                is_runner=is_r,
                is_buy=is_buy,
                desired_broker_sl=desired_broker_sl,
                step_dollars=step_dollars,
                extra_tp=tp_rounded if is_r else None,
                extra_tp_stage=runner_stage if is_r else None,
                force_sl_sync=force_sl_sync,
                fallback_sl=clamped_fb_sl,
                tick=tick,
                sym_info=sym_info,
                digits=digits,
            ):
                modified_cnt += 1
        return modified_cnt

    def _handle_tp1_scaling_legacy(
        self,
        signal_data: Dict[str, Any],
        symbol: str,
        direction: str,
        floor_price: float,
    ) -> Optional[int]:
        """Legacy 3-layer TP1 scaling (TOP/MID close -> single BOTTOM/MID runner)."""
        layers = signal_data.get("layers") or {}
        raw_tp1 = signal_data.get("raw_tp1")
        market_tickets = list(signal_data.get("market_tickets") or [])
        active_set = set(market_tickets)

        self.cancel_signal_pending_orders(signal_data)
        signal_data["pending_tickets"] = []

        top_ticket = (layers.get("TOP") or {}).get("ticket") if layers else None
        mid_ticket = (layers.get("MID") or {}).get("ticket") if layers else None
        bot_ticket = (layers.get("BOTTOM") or {}).get("ticket") if layers else None

        top_open = top_ticket is not None and top_ticket in active_set
        mid_open = mid_ticket is not None and mid_ticket in active_set
        bot_open = bot_ticket is not None and bot_ticket in active_set

        banked_tickets: List[int] = []
        if top_open and top_ticket:
            self.close_market_position(top_ticket)
            banked_tickets.append(top_ticket)
            if isinstance(layers.get("TOP"), dict):
                layers["TOP"]["status"] = "CLOSED"

        runner_ticket = None
        runner_layer = None

        if bot_open and bot_ticket:
            if mid_open and mid_ticket:
                self.close_market_position(mid_ticket)
                banked_tickets.append(mid_ticket)
                if isinstance(layers.get("MID"), dict):
                    layers["MID"]["status"] = "CLOSED"
            runner_ticket = bot_ticket
            runner_layer = "BOTTOM"
            if isinstance(layers.get("BOTTOM"), dict):
                layers["BOTTOM"]["status"] = "RUNNER"
        elif mid_open and mid_ticket:
            runner_ticket = mid_ticket
            runner_layer = "MID"
            if isinstance(layers.get("MID"), dict):
                layers["MID"]["status"] = "RUNNER"

        for mt in market_tickets:
            if mt and mt != runner_ticket and mt not in banked_tickets:
                self.close_market_position(mt)
                banked_tickets.append(mt)

        signal_data["last_banked_tickets"] = banked_tickets
        signal_data["runner_layer"] = runner_layer
        signal_data["runner_ticket"] = runner_ticket
        signal_data["soft_tp1_floor"] = floor_price
        signal_data["runner_sl_floor"] = floor_price
        signal_data["runner_peak_price"] = raw_tp1
        signal_data["tp2_shortfall_armed"] = False
        signal_data["tp1_reached"] = True
        signal_data["market_tickets"] = [runner_ticket] if runner_ticket else []
        signal_data["status"] = "RUNNER_ACTIVE" if runner_ticket else "CLOSED"

        if runner_ticket:
            self.modify_position_sltp(runner_ticket, sl=floor_price, tp=None)

        return runner_ticket

    @staticmethod
    def _is_valid_tp(v: Any) -> bool:
        return (
            isinstance(v, numbers.Real)
            and not isinstance(v, bool)
            and math.isfinite(float(v))
            and float(v) > 0.0
        )

    def _resolve_effective_max_stage(
        self,
        configured_max_stage: int,
        tp_levels: Optional[Dict[str, Any]],
    ) -> int:
        """
        Cap configured_max_stage against the highest valid TP stage (TP1..TP5) in tp_levels
        so signals defining only TP1..TP4 (or setting higher TPs to None) cap cleanly at that stage.
        """
        cfg_cap = max(1, min(5, _int_num(configured_max_stage, 5)))
        if not isinstance(tp_levels, dict) or not tp_levels:
            return cfg_cap
        highest_valid_tp = max(
            (i for i in range(1, 6) if self._is_valid_tp(tp_levels.get(f"TP{i}"))),
            default=0,
        )
        if highest_valid_tp > 0:
            return max(1, min(5, cfg_cap, highest_valid_tp))
        return cfg_cap

    def _ensure_signal_tp_levels(
        self,
        signal_data: Dict[str, Any],
        direction: str,
        pip_size: float,
        digits: int,
    ) -> Dict[str, float]:
        """Build/sync TP1..TP5 dictionary on signal_data for Active Runner FSM."""
        tp_map = signal_data.get("tp_levels")
        merged: Dict[str, float] = {}
        explicit_none_keys: Set[str] = set()
        has_explicit_multi_tp = isinstance(tp_map, dict) and len(tp_map) >= 2
        if isinstance(tp_map, dict):
            for k, v in tp_map.items():
                up_k = str(k).strip().upper()
                if v is None:
                    explicit_none_keys.add(up_k)
                elif self._is_valid_tp(v):
                    merged[up_k] = round(float(v), digits)
        for i in range(1, 6):
            k = f"TP{i}"
            if k in explicit_none_keys:
                continue
            val = signal_data.get(f"raw_tp{i}")
            if self._is_valid_tp(val) and k not in merged:
                merged[k] = round(float(val), digits)

        seed: List[float] = []
        for i in range(1, 6):
            v = merged.get(f"TP{i}")
            if v is not None:
                seed.append(v)
            else:
                break

        if seed and len(merged) < 5 and not has_explicit_multi_tp:
            steps = [
                float(_cfg("TP2_STEP_PIPS", TP2_STEP_PIPS)),
                float(_cfg("TP3_STEP_PIPS", TP3_STEP_PIPS)),
                float(_cfg("TP4_STEP_PIPS", TP4_STEP_PIPS)),
                float(_cfg("TP5_STEP_PIPS", TP5_STEP_PIPS)),
            ]
            derived = derive_tp_levels(seed, direction=direction, pip_size=pip_size, step_pips=steps)
            for k, v in derived.items():
                if k not in explicit_none_keys and self._is_valid_tp(v):
                    merged.setdefault(k, round(float(v), digits))

        if merged:
            signal_data["tp_levels"] = merged
            for i in range(1, 6):
                k = f"TP{i}"
                if k in merged and signal_data.get(f"raw_tp{i}") is None and k not in explicit_none_keys:
                    signal_data[f"raw_tp{i}"] = merged[k]
        return merged

    @staticmethod
    def select_dynamic_runners(
        layers: Optional[Dict[str, Any]],
        market_tickets: Optional[Sequence[int]],
        full_runner_count: Optional[int] = None,
    ) -> Tuple[List[str], List[str]]:
        """Delegate to module-level dynamic runner selector."""
        return select_dynamic_runners(layers, market_tickets, full_runner_count=full_runner_count)

    def _resolve_milestone_anchor(
        self,
        signal_data: Dict[str, Any],
        tp_levels: Dict[str, float],
        k: int,
        fallback_entry: Optional[float] = None,
    ) -> Optional[float]:
        """
        Resolve anchor price for midrange proximity clamping at milestone TPk:
        - k == 1: entry reference price (recorded_entry_price / initial_price / open layer target_price)
        - k >= 2: TP{k-1} (falling back to entry reference price)
        """
        if k >= 2:
            prev_tp = tp_levels.get(f"TP{k - 1}")
            if prev_tp is not None and float(prev_tp) > 0:
                return float(prev_tp)

        for key in ("recorded_entry_price", "initial_price"):
            val = signal_data.get(key)
            if isinstance(val, numbers.Real) and not isinstance(val, bool) and float(val) > 0:
                return float(val)

        if fallback_entry is not None and float(fallback_entry) > 0:
            return float(fallback_entry)

        layers = signal_data.get("layers") or {}
        for lk in ("L4", "L3", "L2", "L1", "BOTTOM", "MID", "TOP"):
            l_info = layers.get(lk)
            if isinstance(l_info, dict):
                tp_px = l_info.get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                    return float(tp_px)
        return None

    def _ordered_open_layer_keys(self, signal_data: Dict[str, Any]) -> List[str]:
        """Return currently open filled layer keys ordered shallow -> deep."""
        layers = signal_data.get("layers")
        if not isinstance(layers, dict) or not layers:
            return []
        mkt_set = {_int_num(t, 0) for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) > 0}
        ordered = (
            LAYER_NAMES
            if any(k in layers for k in LAYER_NAMES)
            else ("TOP", "MID", "BOTTOM")
        )
        open_keys: List[str] = []
        for lk in ordered:
            lv = layers.get(lk)
            if not isinstance(lv, dict):
                continue
            lt = _int_num(lv.get("ticket"), 0)
            st = str(lv.get("status") or "").strip().upper()
            if lt > 0 and (lt in mkt_set or not mkt_set) and st in ("OPEN", "FILLED", "RUNNER"):
                open_keys.append(lk)
            elif lt > 0 and mkt_set and lt in mkt_set and st not in ("CLOSED", "CANCELLED", "PENDING"):
                open_keys.append(lk)
        return open_keys

    def assign_or_preserve_layer_roles(self, signal_data: Dict[str, Any]) -> Dict[str, str]:
        """
        Assign sticky v3.6 roles across open market layers (`SOLO_RUNNER`, `BANK_LAYER`,
        `BANK_TP1`, `STAGE2_BANKER`, `CORE_RUNNER`) or assign `LATE_RECOVERY` to late race fills
        once `roles_locked=True` so existing roles/runners can never suffer downward dominoes.
        """
        layers = signal_data.get("layers")
        if not isinstance(layers, dict) or not layers:
            return {}
        open_keys = self._ordered_open_layer_keys(signal_data)
        if not open_keys:
            return {}

        roles_locked = bool(signal_data.get("roles_locked"))
        if not roles_locked:
            # Also auto-lock if a previous v3.6 action already set stage0/runner lift state
            if any(
                isinstance(layers.get(lk), dict)
                and str(layers[lk].get("tp_stage") or "TP1").strip().upper() != "TP1"
                and str(layers[lk].get("role") or "").strip().upper() != ""
                for lk in open_keys
            ):
                roles_locked = True
                signal_data["roles_locked"] = True

        tiered_on = bool(_cfg("TIERED_STAGE_BANKING_ENABLED", TIERED_STAGE_BANKING_ENABLED))
        assigned: Dict[str, str] = {}
        if roles_locked:
            for lk in open_keys:
                lv = layers[lk]
                r = str(lv.get("role") or "").strip().upper()
                if not r:
                    lv["role"] = "LATE_RECOVERY"
                    r = "LATE_RECOVERY"
                assigned[lk] = r
        else:
            n = len(open_keys)
            if n == 1:
                assigned[open_keys[0]] = "SOLO_RUNNER"
            elif n == 2:
                assigned[open_keys[0]] = "BANK_LAYER"
                assigned[open_keys[1]] = "CORE_RUNNER"
                signal_data["bank_harvest_layer"] = open_keys[0]
            elif n == 3:
                assigned[open_keys[0]] = "BANK_LAYER"
                assigned[open_keys[1]] = "BANK_TP1"
                assigned[open_keys[2]] = "CORE_RUNNER"
                signal_data["bank_harvest_layer"] = open_keys[0]
            else:
                assigned[open_keys[0]] = "BANK_LAYER"
                for mid_k in open_keys[1:-2]:
                    assigned[mid_k] = "BANK_TP1"
                assigned[open_keys[-2]] = "STAGE2_BANKER" if tiered_on else "BANK_TP1"
                assigned[open_keys[-1]] = "CORE_RUNNER"
                signal_data["bank_harvest_layer"] = open_keys[0]
            for lk, role_val in assigned.items():
                layers[lk]["role"] = role_val

        r_layers = [lk for lk in open_keys if assigned.get(lk) in ("SOLO_RUNNER", "CORE_RUNNER")]
        if not r_layers and open_keys:
            # Edge case: CORE_RUNNER already closed while LATE_RECOVERY/STAGE2_BANKER remains open
            non_bank = [lk for lk in open_keys if assigned.get(lk) not in ("BANK_LAYER", "BANK_TP1")]
            r_layers = non_bank[-1:] if non_bank else [open_keys[-1]]
        r_tickets = [_int_num(layers[lk].get("ticket"), 0) for lk in r_layers if _int_num(layers[lk].get("ticket"), 0) > 0]
        if r_layers and not _is_legacy_3layer_dict(layers):
            signal_data["runner_layers"] = list(r_layers)
            signal_data["runner_tickets"] = list(r_tickets)
            signal_data["runner_layer"] = r_layers[-1]
            signal_data["runner_ticket"] = r_tickets[-1] if r_tickets else None
            signal_data["last_runner_layers"] = list(r_layers)
            signal_data["last_runner_layer"] = r_layers[-1]
        return assigned

    def hydrate_signal_layers_from_mt5(
        self,
        signal_data: Dict[str, Any],
        live_positions_by_ticket: Optional[Dict[int, Any]] = None,
    ) -> bool:
        """
        Idempotently hydrate active or recently filled layers (including pre-v3.6 live positions
        and previously set limits when filled) from live MT5 positions (`price_open`, `volume`,
        `sl`, `tp`). Never cancels unfilled limits and never loosens existing broker SLs.
        """
        if not isinstance(signal_data, dict):
            return False
        layers = signal_data.get("layers")
        if not isinstance(layers, dict) or not layers:
            return False

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

        pos_map = live_positions_by_ticket
        if pos_map is None and not self.dry_run and self._connected:
            with self._mt5_lock:
                raw_pos = mt5.positions_get()
            if raw_pos is not None:
                pos_map = {
                    _int_num(getattr(p, "ticket", 0), 0): p
                    for p in raw_pos
                    if _int_num(getattr(p, "ticket", 0), 0) > 0
                }
        pos_map = pos_map or {}

        changed = False
        ordered = LAYER_NAMES if any(k in layers for k in LAYER_NAMES) else ("TOP", "MID", "BOTTOM")
        first_open_entry: Optional[float] = None
        any_promoted_mkt = False

        for lk in ordered:
            lv = layers.get(lk)
            if not isinstance(lv, dict):
                continue
            lt = _int_num(lv.get("ticket"), 0)
            p = pos_map.get(lt) if lt > 0 else None
            if p is not None:
                p_open = _num(getattr(p, "price_open", 0.0), 0.0)
                p_vol = _num(getattr(p, "volume", 0.0), 0.0)
                p_sl = _num(getattr(p, "sl", 0.0), 0.0)
                p_tp = _num(getattr(p, "tp", 0.0), 0.0)
                st_u = str(lv.get("status") or "").strip().upper()
                if st_u == "PENDING":
                    lv["status"] = "FILLED"
                    changed = True
                if p_open > 0.0 and (_num(lv.get("entry_price"), 0.0) <= 0.0 or _num(lv.get("entry"), 0.0) <= 0.0):
                    ep = round(p_open, digits)
                    lv["entry_price"] = ep
                    lv["entry"] = ep
                    changed = True
                if p_vol > 0.0 and (_num(lv.get("remaining_lot"), 0.0) <= 0.0 or p_vol < _num(lv.get("remaining_lot"), 0.0) - 1e-6):
                    lv["remaining_lot"] = round(p_vol, 4)
                    changed = True
                if p_sl > 0.0:
                    r_psl = round(p_sl, digits)
                    if lv.get("broker_sl") != r_psl:
                        lv["broker_sl"] = r_psl
                        changed = True
                    cur_entry = _num(lv.get("entry_price") or lv.get("target_price"), 0.0)
                    if cur_entry > 0.0:
                        is_green = (r_psl >= cur_entry - 1e-6) if is_buy else (r_psl <= cur_entry + 1e-6)
                        if is_green:
                            prev_rsl = _num(lv.get("ratchet_sl"), 0.0)
                            if prev_rsl <= 0.0 or (is_buy and r_psl > prev_rsl + 1e-6) or (not is_buy and r_psl < prev_rsl - 1e-6):
                                lv["ratchet_sl"] = r_psl
                                changed = True
                if p_tp > 0.0 and _num(lv.get("tp"), 0.0) <= 0.0:
                    lv["tp"] = round(p_tp, digits)
                    changed = True

                mkt_list = list(signal_data.get("market_tickets") or [])
                if lt not in mkt_list:
                    mkt_list.append(lt)
                    signal_data["market_tickets"] = mkt_list
                    any_promoted_mkt = True
                    changed = True
                pd_list = list(signal_data.get("pending_tickets") or [])
                if lt in pd_list:
                    signal_data["pending_tickets"] = [t for t in pd_list if t != lt]
                    changed = True

            # Capture earliest open entry for recorded_entry_price backfill
            st_now = str(lv.get("status") or "").strip().upper()
            if first_open_entry is None and st_now in ("OPEN", "FILLED", "RUNNER"):
                cand_e = _num(lv.get("entry_price") or lv.get("entry"), 0.0)
                if cand_e > 0.0:
                    first_open_entry = cand_e

        if first_open_entry is not None and _num(signal_data.get("recorded_entry_price"), 0.0) <= 0.0:
            signal_data["recorded_entry_price"] = round(first_open_entry, digits)
            signal_data["initial_price"] = round(first_open_entry, digits)
            changed = True

        open_keys = self._ordered_open_layer_keys(signal_data)
        if open_keys and (
            any_promoted_mkt
            or any(not str((layers.get(lk) or {}).get("role") or "").strip() for lk in open_keys)
        ):
            self.assign_or_preserve_layer_roles(signal_data)
            changed = True

        return changed

    @staticmethod
    def compute_ticket_ratchet_levels(
        direction: str,
        entry_price: float,
        mfe_exec: float,
        tp1_leg: Optional[float],
        prog_to_tp1: float,
        stage: int,
        leg_prog: float,
        leg_span: Optional[float],
        atr_val: float,
        role: str,
        is_solo: bool = False,
        lift_active: bool = False,
        digits: int = 2,
    ) -> Dict[str, Any]:
        """
        Pure Rev 2 v3.6 Profit Ratchet calculation for a single filled ticket.
        Computes monotonic broker SL lock ($L_broker) and spread-paused virtual lock ($L_virt).
        """
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        sign = 1.0 if is_buy else -1.0
        role_u = str(role or "CORE_RUNNER").strip().upper()
        atr = float(atr_val) if atr_val and float(atr_val) > 0.0 else 1.67

        out: Dict[str, Any] = {
            "tier": None,
            "l_broker": None,
            "l_virt": None,
            "broker_sl": None,
            "virt_sl": None,
            "is_squeeze": False,
            "arm_bank_harvest": False,
            "harvest_virt_pb": None,
        }
        if entry_price <= 0.0 or mfe_exec <= 0.0:
            return out

        peak_prog = max(
            float(prog_to_tp1 or 0.0),
            (float(mfe_exec) / float(tp1_leg)) if (tp1_leg is not None and tp1_leg > 1e-9) else 0.0,
        )

        def _finalize(tier_name: str, l_brk: float, l_vrt: float, is_sq: bool = False, arm_bh: bool = False, bh_pb: Optional[float] = None) -> Dict[str, Any]:
            l_brk_r = max(0.0, round(l_brk, 6))
            l_vrt_r = max(l_brk_r, round(l_vrt, 6))
            return {
                "tier": tier_name,
                "l_broker": l_brk_r,
                "l_virt": l_vrt_r,
                "broker_sl": round(entry_price + sign * l_brk_r, digits),
                "virt_sl": round(entry_price + sign * l_vrt_r, digits),
                "is_squeeze": is_sq,
                "arm_bank_harvest": arm_bh,
                "harvest_virt_pb": bh_pb,
            }

        if role_u == "LATE_RECOVERY":
            t1_mfe = float(_cfg("RATCHET_TIER1A_MFE_DOLLARS", RATCHET_TIER1A_MFE_DOLLARS))
            t1_lock = float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS))
            if mfe_exec + 1e-9 >= t1_mfe:
                return _finalize("TIER_1A", t1_lock, t1_lock)
            return out

        if role_u == "BANK_LAYER":
            bh_on = bool(_cfg("BANK_HARVEST_ENABLED", BANK_HARVEST_ENABLED))
            bh_mfe = float(_cfg("BANK_HARVEST_ARM_MFE_DOLLARS", BANK_HARVEST_ARM_MFE_DOLLARS))
            bh_alt_pct = float(_cfg("BANK_HARVEST_ALT_ARM_PCT", BANK_HARVEST_ALT_ARM_PCT))
            bh_alt_min = float(_cfg("BANK_HARVEST_ALT_MIN_MFE_DOLLARS", BANK_HARVEST_ALT_MIN_MFE_DOLLARS))
            arm_bh = bh_on and (
                (mfe_exec + 1e-9 >= bh_mfe)
                or (peak_prog + 1e-9 >= bh_alt_pct and mfe_exec + 1e-9 >= bh_alt_min)
            )
            if arm_bh:
                pb_mult = float(_cfg("BANK_HARVEST_PULLBACK_ATR_MULT", BANK_HARVEST_PULLBACK_ATR_MULT))
                pb_min = float(_cfg("BANK_HARVEST_PULLBACK_MIN", BANK_HARVEST_PULLBACK_MIN))
                pb_max = float(_cfg("BANK_HARVEST_PULLBACK_MAX", BANK_HARVEST_PULLBACK_MAX))
                brk_cush = float(_cfg("BANK_HARVEST_BROKER_CUSHION_DOLLARS", BANK_HARVEST_BROKER_CUSHION_DOLLARS))
                t1_lock = float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS))
                virt_pb = round(max(pb_min, min(pb_max, pb_mult * atr)), 4)
                l_brk = max(t1_lock, mfe_exec - brk_cush)
                l_vrt = max(l_brk, mfe_exec - virt_pb)
                return _finalize("BANK_HARVEST", l_brk, l_vrt, arm_bh=True, bh_pb=virt_pb)
            # Below BANK_HARVEST arming threshold, still lock Tier 1a/1b green broker SL at $3.50/$4.25
            t1b_mfe = float(_cfg("RATCHET_TIER1B_MFE_DOLLARS", RATCHET_TIER1B_MFE_DOLLARS))
            t1b_lock = float(_cfg("RATCHET_TIER1B_LOCK_DOLLARS", RATCHET_TIER1B_LOCK_DOLLARS))
            t1a_mfe = float(_cfg("RATCHET_TIER1A_MFE_DOLLARS", RATCHET_TIER1A_MFE_DOLLARS))
            t1a_lock = float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS))
            if mfe_exec + 1e-9 >= t1b_mfe:
                return _finalize("TIER_1B", t1b_lock, t1b_lock)
            if mfe_exec + 1e-9 >= t1a_mfe:
                return _finalize("TIER_1A", t1a_lock, t1a_lock)
            return out

        # CORE_RUNNER, SOLO_RUNNER, STAGE2_BANKER (and BANK_TP1 falls through to Tier 1a/1b/2+ if lifted or waiting)
        t5_mfe = float(_cfg("RATCHET_TIER5_MFE_DOLLARS", RATCHET_TIER5_MFE_DOLLARS))
        t4_mfe = float(_cfg("RATCHET_TIER4_MFE_DOLLARS", RATCHET_TIER4_MFE_DOLLARS))
        t3_mfe = float(_cfg("RATCHET_TIER3_MFE_DOLLARS", RATCHET_TIER3_MFE_DOLLARS))
        t2_mfe = float(_cfg("RATCHET_TIER2_MFE_DOLLARS", RATCHET_TIER2_MFE_DOLLARS))
        t1b_mfe = float(_cfg("RATCHET_TIER1B_MFE_DOLLARS", RATCHET_TIER1B_MFE_DOLLARS))
        t1a_mfe = float(_cfg("RATCHET_TIER1A_MFE_DOLLARS", RATCHET_TIER1A_MFE_DOLLARS))
        t1a_lock = float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS))
        t1b_lock = float(_cfg("RATCHET_TIER1B_LOCK_DOLLARS", RATCHET_TIER1B_LOCK_DOLLARS))

        tight_max_leg = float(_cfg("RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS", RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS))
        tight_min_mfe = float(_cfg("RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS", RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS))
        tight_lock_pct = float(_cfg("RATCHET_TIGHT_TP1_LOCK_PCT", RATCHET_TIGHT_TP1_LOCK_PCT))
        lift_min_mfe = float(_cfg("RATCHET_LIFT_MIN_MFE_DOLLARS", RATCHET_LIFT_MIN_MFE_DOLLARS))
        lift_pct = float(_cfg("RUNNER_EARLY_TP_LIFT_PCT", RUNNER_EARLY_TP_LIFT_PCT))

        is_tight_0t = bool(
            stage == 0
            and tp1_leg is not None
            and 0.0 < tp1_leg <= tight_max_leg + 1e-9
            and peak_prog + 1e-9 >= max(lift_pct, 0.76)
            and mfe_exec + 1e-9 >= tight_min_mfe
            and mfe_exec + 1e-9 < t1a_mfe
        )

        tier_name: Optional[str] = None
        l_broker = 0.0
        l_virt = 0.0
        cush = 3.50

        if mfe_exec + 1e-9 >= t5_mfe:
            c5 = max(float(_cfg("RATCHET_TIER5_MIN_CUSHION", RATCHET_TIER5_MIN_CUSHION)), min(float(_cfg("RATCHET_TIER5_MAX_CUSHION", RATCHET_TIER5_MAX_CUSHION)), 2.50 * atr))
            ret5 = float(_cfg("RATCHET_TIER5_RETENTION_PCT", RATCHET_TIER5_RETENTION_PCT))
            l_broker = l_virt = max(ret5 * mfe_exec, mfe_exec - c5)
            tier_name, cush = "TIER_5", c5
        elif mfe_exec + 1e-9 >= t4_mfe:
            c4 = max(float(_cfg("RATCHET_TIER4_MIN_CUSHION", RATCHET_TIER4_MIN_CUSHION)), min(float(_cfg("RATCHET_TIER4_MAX_CUSHION", RATCHET_TIER4_MAX_CUSHION)), 2.25 * atr))
            ret4 = float(_cfg("RATCHET_TIER4_RETENTION_PCT", RATCHET_TIER4_RETENTION_PCT))
            l_broker = l_virt = max(ret4 * mfe_exec, mfe_exec - c4)
            tier_name, cush = "TIER_4", c4
        elif mfe_exec + 1e-9 >= t3_mfe:
            c3 = max(float(_cfg("RATCHET_TIER3_MIN_CUSHION", RATCHET_TIER3_MIN_CUSHION)), min(float(_cfg("RATCHET_TIER3_MAX_CUSHION", RATCHET_TIER3_MAX_CUSHION)), 2.00 * atr))
            ret3 = float(_cfg("RATCHET_TIER3_RETENTION_PCT", RATCHET_TIER3_RETENTION_PCT))
            l_broker = l_virt = max(ret3 * mfe_exec, mfe_exec - c3)
            tier_name, cush = "TIER_3", c3
        elif mfe_exec + 1e-9 >= t2_mfe:
            c2 = max(float(_cfg("RATCHET_TIER2_MIN_CUSHION", RATCHET_TIER2_MIN_CUSHION)), min(float(_cfg("RATCHET_TIER2_MAX_CUSHION", RATCHET_TIER2_MAX_CUSHION)), 2.00 * atr))
            ret2 = float(_cfg("RATCHET_TIER2_RETENTION_PCT", RATCHET_TIER2_RETENTION_PCT))
            min_l2 = float(_cfg("RATCHET_TIER2_MIN_LOCK_DOLLARS", RATCHET_TIER2_MIN_LOCK_DOLLARS))
            l_broker = l_virt = max(min_l2, ret2 * mfe_exec, mfe_exec - c2)
            tier_name, cush = "TIER_2", c2
        elif is_tight_0t:
            tier_name = "TIER_0T"
            l_broker = t1a_lock
            l_virt = max(t1a_lock, tight_lock_pct * mfe_exec)
            cush = max(0.50, mfe_exec - l_virt)
        elif mfe_exec + 1e-9 >= t1b_mfe:
            tier_name = "TIER_1B"
            l_broker = l_virt = t1b_lock
            cush = max(1.0, mfe_exec - t1b_lock)
        elif (mfe_exec + 1e-9 >= t1a_mfe) or (lift_active and mfe_exec + 1e-9 >= lift_min_mfe):
            tier_name = "TIER_1A"
            l_broker = l_virt = t1a_lock
            cush = max(1.0, mfe_exec - t1a_lock)

        if stage == 4 and role_u in ("CORE_RUNNER", "SOLO_RUNNER") and mfe_exec > 0.0:
            cs4 = max(float(_cfg("RATCHET_STAGE4_MIN_CUSHION", RATCHET_STAGE4_MIN_CUSHION)), min(float(_cfg("RATCHET_STAGE4_MAX_CUSHION", RATCHET_STAGE4_MAX_CUSHION)), 1.75 * atr))
            ret_s4 = float(_cfg("RATCHET_STAGE4_RETENTION_PCT", RATCHET_STAGE4_RETENTION_PCT))
            l_s4 = max(ret_s4 * mfe_exec, mfe_exec - cs4)
            if l_s4 > l_broker:
                l_broker = l_virt = l_s4
                tier_name, cush = "STAGE4_SQUEEZE", cs4

        is_sq = False
        if tier_name in ("TIER_2", "TIER_3", "TIER_4", "TIER_5", "STAGE4_SQUEEZE") and role_u in ("CORE_RUNNER", "SOLO_RUNNER"):
            pk_arm = float(_cfg("PEAK_HARVEST_ARM_PCT", PEAK_HARVEST_ARM_PCT))
            sq_mult = float(_cfg("PEAK_HARVEST_CUSHION_MULT", PEAK_HARVEST_CUSHION_MULT))
            sq_min_brk = float(_cfg("PEAK_HARVEST_MIN_BROKER_CUSHION", PEAK_HARVEST_MIN_BROKER_CUSHION))
            sq_min_vrt = float(_cfg("PEAK_HARVEST_MIN_VIRT_CUSHION", PEAK_HARVEST_MIN_VIRT_CUSHION))
            sq_max = float(_cfg("PEAK_HARVEST_MAX_CUSHION", PEAK_HARVEST_MAX_CUSHION))
            if stage >= 2 and leg_span is not None and leg_span + 1e-9 >= 5.00 and leg_prog + 1e-9 >= pk_arm:
                c_sq_b = max(sq_min_brk, min(sq_max, sq_mult * cush))
                c_sq_v = max(sq_min_vrt, min(sq_max, sq_mult * cush))
                l_broker = max(l_broker, mfe_exec - c_sq_b)
                l_virt = max(l_broker, l_virt, mfe_exec - c_sq_v)
                is_sq = True
            elif stage == 0 and is_solo and tp1_leg is not None and tp1_leg + 1e-9 >= 10.00 and peak_prog + 1e-9 >= pk_arm:
                c_sq_b = max(sq_min_brk, min(sq_max, 0.75 * cush))
                c_sq_v = max(max(sq_min_vrt, 2.50), min(sq_max, 0.75 * cush))
                l_broker = max(l_broker, mfe_exec - c_sq_b)
                l_virt = max(l_broker, l_virt, mfe_exec - c_sq_v)
                is_sq = True

        if tier_name is None:
            return out
        return _finalize(tier_name, l_broker, l_virt, is_sq=is_sq)

    def _sync_ticket_ratchet_sl(
        self,
        signal_data: Dict[str, Any],
        layer_key: Optional[str],
        ticket: int,
        desired_broker_sl: float,
        step_dollars: Optional[float] = None,
        force_sync: bool = False,
        tick: Any = None,
        sym_info: Any = None,
        digits: int = 2,
        floor_lock_sl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Per-ticket monotonic broker SL synchronization (FM-8: never broadcasts across layers).
        Clamps to stops_level + safety buffer, rejects non-green clamping, checks `>= step_dollars`
        improvement unless `force_sync=True`, and updates only `layers[layer_key]` (plus
        signal_data["broker_sl"] for CORE_RUNNER/SOLO_RUNNER).
        """
        t_int = _int_num(ticket, 0)
        if t_int <= 0 or desired_broker_sl <= 0.0:
            return {"synced": False, "sl": None, "rejected": False, "retcode": None}

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        if sym_info is None:
            sym_info = self.get_symbol_info(symbol)
            digits = _int_num(getattr(sym_info, "digits", digits), digits) if sym_info else digits
        if tick is None:
            tick = self.get_tick(symbol)
        eff_step = float(
            _cfg("RATCHET_BROKER_STEP_DOLLARS", RATCHET_BROKER_STEP_DOLLARS)
            if step_dollars is None
            else step_dollars
        )

        layers = signal_data.get("layers") or {}
        l_info = layers.get(layer_key) if (layer_key and isinstance(layers.get(layer_key), dict)) else None
        entry_px = self._resolve_layer_entry(signal_data, layer_key, l_info) or 0.0

        cur_sl = 0.0
        if not self.dry_run:
            with self._mt5_lock:
                pos_res = mt5.positions_get(ticket=t_int)
            if pos_res:
                cur_sl = round(_num(getattr(pos_res[0], "sl", 0.0), 0.0), digits)
                p_open = _num(getattr(pos_res[0], "price_open", 0.0), 0.0)
                if p_open > 0.0 and entry_px <= 0.0:
                    entry_px = round(p_open, digits)
        if l_info is not None:
            for k_sl in ("ratchet_sl", "broker_sl", "be_sl"):
                sv = _num(l_info.get(k_sl), 0.0)
                if sv > 0.0:
                    if cur_sl <= 0.0 or (is_buy and sv > cur_sl) or (not is_buy and sv < cur_sl):
                        cur_sl = round(sv, digits)

        target_sl = round(float(desired_broker_sl), digits)
        if cur_sl > 0.0:
            # Maintain strict monotonicity even against existing tighter manual/BE/ratchet SL
            cur_is_green = (cur_sl >= entry_px - 1e-6) if (entry_px > 0 and is_buy) else (cur_sl <= entry_px + 1e-6 if entry_px > 0 else True)
            if cur_is_green:
                target_sl = round(max(target_sl, cur_sl) if is_buy else min(target_sl, cur_sl), digits)

        clamped_sl = self._clamp_sl_for_broker_stops(target_sl, direction, tick, sym_info, digits)
        if floor_lock_sl is not None and floor_lock_sl > 0.0:
            # Do not replace desired SL if clamping pushed it worse than desired minimum lock; try fallback
            fallback_clamped = self._clamp_sl_for_broker_stops(float(floor_lock_sl), direction, tick, sym_info, digits)
            if (is_buy and clamped_sl < float(floor_lock_sl) - 1e-6) or (not is_buy and clamped_sl > float(floor_lock_sl) + 1e-6):
                clamped_sl = fallback_clamped

        # Strictly green guard when entry known
        if entry_px > 0.0:
            if (is_buy and clamped_sl <= entry_px + 1e-6) or (not is_buy and clamped_sl >= entry_px - 1e-6):
                return {"synced": False, "sl": None, "rejected": True, "retcode": 10016}

        already_better = cur_sl > 0.0 and (
            (is_buy and cur_sl >= clamped_sl - 1e-9) or (not is_buy and cur_sl <= clamped_sl + 1e-9)
        )
        if already_better:
            if l_info is not None:
                l_info["broker_sl"] = cur_sl
                l_info["ratchet_sl"] = cur_sl
            return {"synced": True, "sl": cur_sl, "rejected": False, "retcode": 10025}

        improvement = (clamped_sl - cur_sl) if (cur_sl > 0.0 and is_buy) else ((cur_sl - clamped_sl) if cur_sl > 0.0 else float("inf"))
        cur_is_protected = cur_sl > 0.0 and (entry_px <= 0.0 or ((cur_sl >= entry_px - 1e-6) if is_buy else (cur_sl <= entry_px + 1e-6)))
        if (not force_sync) and cur_is_protected and (improvement + 1e-9 < eff_step):
            return {"synced": False, "sl": cur_sl, "rejected": False, "retcode": None}

        ok = self.modify_position_sltp(t_int, sl=clamped_sl, tp=None)
        retcode = (getattr(self, "last_modify_result", {}) or {}).get("retcode")
        if ok:
            if l_info is not None:
                l_info["broker_sl"] = clamped_sl
                l_info["ratchet_sl"] = clamped_sl
                l_info["sl"] = clamped_sl
            role_u = str((l_info or {}).get("role") or "").strip().upper()
            if role_u in ("CORE_RUNNER", "SOLO_RUNNER", "") or t_int == _int_num(signal_data.get("runner_ticket"), 0):
                prev_bsl = _num(signal_data.get("broker_sl"), 0.0)
                if prev_bsl <= 0.0 or (is_buy and clamped_sl > prev_bsl + 1e-9) or (not is_buy and clamped_sl < prev_bsl - 1e-9):
                    signal_data["broker_sl"] = clamped_sl
            return {"synced": True, "sl": clamped_sl, "rejected": False, "retcode": retcode or 10009}
        return {"synced": False, "sl": None, "rejected": True, "retcode": retcode}

    def _lift_ticket_tp_two_step(
        self,
        signal_data: Dict[str, Any],
        layer_key: Optional[str],
        ticket: int,
        next_tp: float,
        next_tp_stage: str,
        desired_broker_sl: float,
        entry_price: float,
        mfe_exec: Optional[float] = None,
        tick: Any = None,
        sym_info: Any = None,
        digits: int = 2,
    ) -> Dict[str, Any]:
        """
        Strict 2-Step SL-First TP Lift (`FM-1`, `FM-6`):
          1) Enforce minimum clearance gate & strictly green broker SL write (`tp=None`).
          2) If Step 1 fails or rejects (`10016`/`10029`), ABORT Step 2 and keep TP1/TPk intact.
          3) Step 2 writes TP only (`sl=None`) outside `trade_freeze_level`.
        """
        t_int = _int_num(ticket, 0)
        if t_int <= 0 or next_tp is None or float(next_tp) <= 0.0:
            return {"lifted": False, "aborted_clearance": True}

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        if sym_info is None:
            sym_info = self.get_symbol_info(symbol)
            digits = _int_num(getattr(sym_info, "digits", digits), digits) if sym_info else digits
        if tick is None:
            tick = self.get_tick(symbol)

        layers = signal_data.get("layers") or {}
        l_info = layers.get(layer_key) if (layer_key and isinstance(layers.get(layer_key), dict)) else None
        target_tp = round(float(next_tp), digits)
        if l_info is not None and abs(_num(l_info.get("tp"), 0.0) - target_tp) < 1e-6 and l_info.get("tp_stage") == next_tp_stage:
            return {"lifted": False, "already_at_tp": True}

        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) or 0.01
        raw_stop_dist = max(0.0, stops_lvl * pt)
        safety_buf = float(_cfg("STOP_LEVEL_SAFETY_BUFFER_DOLLARS", STOP_LEVEL_SAFETY_BUFFER_DOLLARS)) if (not self.dry_run or raw_stop_dist > 0.0) else 0.0
        min_stop_dist = raw_stop_dist + (max(0.0, safety_buf) if raw_stop_dist > 0.0 else 0.0)

        min_mfe_cfg = float(_cfg("RATCHET_LIFT_MIN_MFE_DOLLARS", RATCHET_LIFT_MIN_MFE_DOLLARS))
        if mfe_exec is not None and entry_price > 0.0:
            l_req = abs(float(desired_broker_sl) - float(entry_price))
            req_mfe = max(min_mfe_cfg, (l_req + min_stop_dist + 0.10) if not self.dry_run else min_mfe_cfg)
            if float(mfe_exec) + 1e-9 < req_mfe:
                return {"lifted": False, "aborted_clearance": True, "req_mfe": round(req_mfe, 2)}

        # Step 1: Strictly green broker SL FIRST (never combined sl+tp call!)
        sl_res = self._sync_ticket_ratchet_sl(
            signal_data=signal_data,
            layer_key=layer_key,
            ticket=t_int,
            desired_broker_sl=desired_broker_sl,
            force_sync=True,
            tick=tick,
            sym_info=sym_info,
            digits=digits,
        )
        if not sl_res.get("synced"):
            warn_key = f"{t_int}:{next_tp_stage}"
            now_m = time.monotonic()
            throttled = (now_m - self._lift_abort_warn_ts.get(warn_key, 0.0)) < 60.0
            if not throttled:
                self._lift_abort_warn_ts[warn_key] = now_m
                logger.warning(
                    f"2-Step TP Lift ABORTED on #{t_int} ({layer_key}): Broker SL sync rejected "
                    f"(desired={desired_broker_sl}, retcode={sl_res.get('retcode')}); keeping current TP intact."
                )
            return {
                "lifted": False,
                "aborted_sl_fail": True,
                "retcode": sl_res.get("retcode"),
                "notify_abort": not throttled,
            }

        # Step 2: Broker TP ONLY (check freeze_level first)
        cur_tp = _num((l_info or {}).get("tp"), 0.0)
        if self.is_within_freeze_level(symbol, cur_tp, tick, sym_info) or self.is_within_freeze_level(symbol, target_tp, tick, sym_info):
            return {"lifted": False, "aborted_frozen": True, "retcode": 10029}

        tp_ok = self.modify_position_sltp(t_int, sl=None, tp=target_tp)
        if not tp_ok:
            retcode = (getattr(self, "last_modify_result", {}) or {}).get("retcode")
            return {"lifted": False, "aborted_tp_fail": True, "retcode": retcode}

        if l_info is not None:
            l_info["tp"] = target_tp
            l_info["tp_stage"] = next_tp_stage
        logger.info(
            f"2-Step Lift #{t_int} ({layer_key}): Broker SL locked @ {sl_res.get('sl')} -> TP lifted to {next_tp_stage}={target_tp}"
        )
        return {"lifted": True, "sl": sl_res.get("sl"), "tp": target_tp, "tp_stage": next_tp_stage}

    def evaluate_profit_ratchet_tick(
        self,
        signal_data: Dict[str, Any],
        tick: Any = None,
        stage: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        v3.6 Continuous Per-Ticket Progressive Profit Ratchet + BANK_HARVEST + 2-Step TP Lift.
        Runs on every 1s tick across active open positions in Stage 0 and Stages 1-4.
        """
        result: Dict[str, Any] = {
            "paused_by_spread": False,
            "events": [],
            "harvest_close_tickets": [],
            "virtual_close_tickets": [],
            "pending_swept": [],
            "lift_aborts": [],
        }
        if not isinstance(signal_data, dict):
            return result
        if not bool(_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED)):
            return result
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        if tick is None:
            tick = self.get_tick(symbol)
        if tick is None or not self.is_valid_live_tick(symbol, tick):
            result["paused_by_spread"] = True
            signal_data["spread_spike_paused"] = True
            return result

        is_spike, spread_dollars, eff_max_spread = self.check_spread_spike(symbol, tick)
        signal_data["spread_spike_paused"] = is_spike
        if is_spike:
            result["paused_by_spread"] = True
            return result

        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        exec_price = float(tick.bid) if is_buy else float(tick.ask)
        raw_chart = getattr(tick, "bid", None)
        chart_price = float(raw_chart) if isinstance(raw_chart, numbers.Real) and not isinstance(raw_chart, bool) and float(raw_chart) > 0 else exec_price

        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(symbol)
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        raw_tp1 = signal_data.get("raw_tp1") or tp_levels.get("TP1")

        open_keys = self._ordered_open_layer_keys(signal_data)
        if not open_keys:
            return result
        roles = self.assign_or_preserve_layer_roles(signal_data)
        layers = signal_data.get("layers") or {}

        eff_stage = stage if stage is not None else self.get_runner_fsm_stage(signal_data)
        sig_entry = self._resolve_signal_primary_entry(signal_data)
        tp1_leg: Optional[float] = None
        prog_to_tp1 = 0.0
        if raw_tp1 is not None and sig_entry is not None and sig_entry > 0.0:
            tp1_leg = (float(raw_tp1) - sig_entry) if is_buy else (sig_entry - float(raw_tp1))
            if tp1_leg > 1e-9:
                prog_to_tp1 = ((chart_price - sig_entry) / tp1_leg) if is_buy else ((sig_entry - chart_price) / tp1_leg)

        configured_max_stage = max(1, min(5, _int_num(signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE), 5)))
        max_stage = self._resolve_effective_max_stage(configured_max_stage, tp_levels)
        signal_data["effective_max_stage"] = max_stage

        leg_prog = 0.0
        leg_span: Optional[float] = None
        if eff_stage >= 1 and eff_stage < max_stage:
            prev_tp_k = tp_levels.get(f"TP{eff_stage}")
            next_tp_k = tp_levels.get(f"TP{eff_stage + 1}")
            if prev_tp_k is not None and next_tp_k is not None:
                leg_span = (float(next_tp_k) - float(prev_tp_k)) if is_buy else (float(prev_tp_k) - float(next_tp_k))
                if leg_span > 1e-9:
                    leg_prog = ((chart_price - float(prev_tp_k)) / leg_span) if is_buy else ((float(prev_tp_k) - chart_price) / leg_span)

        atr_val = self.get_m1_atr(symbol)
        is_solo = len(open_keys) == 1
        confirm_req = max(1, _int_num(_cfg("REVERSAL_CONFIRM_TICKS", 2), 2))
        proactive = bool(_cfg("RUNNER_PROACTIVE_TP_LIFT", RUNNER_PROACTIVE_TP_LIFT)) and eff_stage == 0 and max_stage >= 2
        bank_all_tp1 = bool(_cfg("BANK_ALL_AT_TP1", False)) or (max_stage <= 1)
        bank_single_tp1 = bool(_cfg("BANK_SINGLE_LAYER_AT_TP1", False))
        lift_pct = float(_cfg("RUNNER_EARLY_TP_LIFT_PCT", RUNNER_EARLY_TP_LIFT_PCT))

        should_lift_stage0 = False
        if proactive and not bank_all_tp1 and not (bank_single_tp1 and is_solo) and raw_tp1 is not None and tp_levels.get("TP2") is not None:
            if prog_to_tp1 + 1e-9 >= lift_pct:
                should_lift_stage0 = True
            else:
                anchor = self._resolve_milestone_anchor(signal_data, tp_levels, 1)
                eff_buf, _ = self._compute_proximity_and_pullback(float(raw_tp1), chart_price, pip_size, anchor=anchor)
                dist_t = (float(raw_tp1) - chart_price) if is_buy else (chart_price - float(raw_tp1))
                if dist_t <= eff_buf + 1e-9:
                    should_lift_stage0 = True

        lock_roles_now = False
        sweep_pendings_now = False

        for lk in open_keys:
            lv = layers[lk]
            lt = _int_num(lv.get("ticket"), 0)
            if lt <= 0:
                continue
            role = roles.get(lk, "CORE_RUNNER")
            entry_px = self._resolve_layer_entry(signal_data, lk, lv)
            if entry_px is None or entry_px <= 0.0:
                continue

            cur_prof = round((exec_price - entry_px) if is_buy else (entry_px - exec_price), 6)
            prev_mfe = _num(lv.get("mfe_dollars"), 0.0)
            new_mfe = round(max(prev_mfe, cur_prof, 0.0), 6)
            prev_pk = lv.get("mfe_peak_price")
            if cur_prof > prev_mfe + 1e-9 or prev_pk is None:
                pk_px = round(exec_price, digits) if cur_prof > 0.0 else (round(float(prev_pk), digits) if prev_pk is not None else round(entry_px, digits))
                if prev_pk is None or (is_buy and exec_price > float(prev_pk)) or (not is_buy and exec_price < float(prev_pk)):
                    lv["mfe_peak_price"] = round(exec_price, digits)
                elif prev_pk is not None:
                    lv["mfe_peak_price"] = round(float(prev_pk), digits)
            if cur_prof > prev_mfe + 1e-9:
                lv["bank_harvest_ticks"] = 0
                lv["virt_ratchet_ticks"] = 0
            lv["mfe_dollars"] = new_mfe

            if role in ("CORE_RUNNER", "SOLO_RUNNER"):
                sig_prev_pp = _num(signal_data.get("peak_profit"), 0.0)
                if new_mfe > sig_prev_pp:
                    signal_data["peak_profit"] = new_mfe
                    signal_data["reversal_guard_peak_profit"] = new_mfe
                if lv.get("mfe_peak_price") is not None:
                    prev_rpk = signal_data.get("runner_peak_price")
                    lpk = float(lv["mfe_peak_price"])
                    if prev_rpk is None or (is_buy and lpk > float(prev_rpk)) or (not is_buy and lpk < float(prev_rpk)):
                        signal_data["runner_peak_price"] = lpk

            can_lift_role = (
                should_lift_stage0
                and role in ("CORE_RUNNER", "SOLO_RUNNER", "STAGE2_BANKER")
                and str(lv.get("tp_stage") or "TP1").strip().upper() == "TP1"
            )
            layer_tp1_leg: Optional[float] = tp1_leg
            layer_prog_to_tp1: float = prog_to_tp1
            if raw_tp1 is not None and entry_px > 0.0:
                layer_tp1_leg = (float(raw_tp1) - entry_px) if is_buy else (entry_px - float(raw_tp1))
                if layer_tp1_leg > 1e-9:
                    layer_prog_to_tp1 = ((chart_price - entry_px) / layer_tp1_leg) if is_buy else ((entry_px - chart_price) / layer_tp1_leg)
                else:
                    layer_prog_to_tp1 = 0.0

            r_res = self.compute_ticket_ratchet_levels(
                direction=direction,
                entry_price=entry_px,
                mfe_exec=new_mfe,
                tp1_leg=layer_tp1_leg,
                prog_to_tp1=layer_prog_to_tp1,
                stage=eff_stage,
                leg_prog=leg_prog,
                leg_span=leg_span,
                atr_val=atr_val,
                role=role,
                is_solo=is_solo,
                lift_active=can_lift_role,
                digits=digits,
            )

            new_tier = r_res.get("tier")
            prev_tier = lv.get("ratchet_tier")
            tier_jumped = bool(new_tier and new_tier != prev_tier)
            was_squeeze = bool(lv.get("squeeze_active"))
            now_squeeze = bool(r_res.get("is_squeeze"))
            if now_squeeze:
                lv["squeeze_active"] = True

            if new_tier is not None:
                lock_roles_now = True
                # Only sweep unfilled deep zone limits when price is above L1 entry (in profit on L1) or MFE >= $5.00
                if (
                    new_mfe + 1e-9 >= float(_cfg("RATCHET_TIER2_MFE_DOLLARS", RATCHET_TIER2_MFE_DOLLARS))
                    or (sig_entry is not None and ((chart_price >= sig_entry) if is_buy else (chart_price <= sig_entry)))
                ):
                    sweep_pendings_now = True
                lv["ratchet_tier"] = new_tier
                if r_res.get("arm_bank_harvest") and not lv.get("bank_harvest_armed"):
                    lv["bank_harvest_armed"] = True

            # Pre-TP1 2-Step SL-First TP Lift for CORE_RUNNER, SOLO_RUNNER, and N=4 STAGE2_BANKER (-> TP2)
            tp2_val = tp_levels.get("TP2")
            lifted_this_tick = False
            if can_lift_role and tp2_val is not None:
                min_lift_sl = round(entry_px + float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS)), digits) if is_buy else round(entry_px - float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS)), digits)
                lift_sl = r_res.get("broker_sl") or min_lift_sl
                if is_buy:
                    lift_sl = max(lift_sl, min_lift_sl)
                else:
                    lift_sl = min(lift_sl, min_lift_sl)
                old_tp_val = lv.get("tp")
                lift_res = self._lift_ticket_tp_two_step(
                    signal_data=signal_data,
                    layer_key=lk,
                    ticket=lt,
                    next_tp=float(tp2_val),
                    next_tp_stage="TP2",
                    desired_broker_sl=lift_sl,
                    entry_price=entry_px,
                    mfe_exec=new_mfe,
                    tick=tick,
                    sym_info=sym_info,
                    digits=digits,
                )
                if lift_res.get("lifted"):
                    lifted_this_tick = True
                    lock_roles_now = True
                    sweep_pendings_now = True
                    if role in ("CORE_RUNNER", "SOLO_RUNNER"):
                        signal_data["tp_stage"] = "TP2"
                    result["events"].append({
                        "type": "TP_LIFT",
                        "layer": lk,
                        "role": role,
                        "ticket": lt,
                        "entry_price": entry_px,
                        "peak_price": lv.get("mfe_peak_price"),
                        "mfe_dollars": new_mfe,
                        "tier": new_tier or "TIER_1A",
                        "broker_sl": lift_res.get("sl"),
                        "old_tp": old_tp_val,
                        "new_tp": lift_res.get("tp"),
                        "new_tp_stage": "TP2",
                    })
                elif lift_res.get("aborted_sl_fail") and lift_res.get("notify_abort"):
                    result["lift_aborts"].append({
                        "layer": lk,
                        "ticket": lt,
                        "desired_sl": lift_sl,
                        "retcode": lift_res.get("retcode"),
                    })

            # Continuous broker SL ratchet progression
            broker_sl_target = r_res.get("broker_sl")
            if broker_sl_target is not None:
                sync_res = self._sync_ticket_ratchet_sl(
                    signal_data=signal_data,
                    layer_key=lk,
                    ticket=lt,
                    desired_broker_sl=float(broker_sl_target),
                    force_sync=bool(tier_jumped or (now_squeeze and not was_squeeze)),
                    tick=tick,
                    sym_info=sym_info,
                    digits=digits,
                )
                if eff_stage == 0 and sync_res.get("synced") and role in ("CORE_RUNNER", "SOLO_RUNNER", "BANK_LAYER"):
                    signal_data["stage0_profit_lock_active"] = True
                    signal_data["stage0_profit_lock_sl"] = sync_res.get("sl") or broker_sl_target
                    signal_data["stage0_broker_sl_synced"] = True

                # One-shot Tier Jump / Squeeze alert (only when not already emitting TP_LIFT on same tick)
                if (tier_jumped or (now_squeeze and not was_squeeze)) and not lifted_this_tick and lv.get("last_alerted_tier") != (f"{new_tier}_SQ" if now_squeeze else new_tier):
                    lv["last_alerted_tier"] = f"{new_tier}_SQ" if now_squeeze else new_tier
                    result["events"].append({
                        "type": "SQUEEZE" if (now_squeeze and not was_squeeze) else "TIER_JUMP",
                        "layer": lk,
                        "role": role,
                        "ticket": lt,
                        "entry_price": entry_px,
                        "peak_price": lv.get("mfe_peak_price"),
                        "mfe_dollars": new_mfe,
                        "tier": new_tier,
                        "broker_sl": sync_res.get("sl") or lv.get("ratchet_sl") or broker_sl_target,
                        "virt_sl": r_res.get("virt_sl"),
                    })

            # Virtual Spread-Paused Harvest Checks (2 confirming ticks)
            virt_sl = r_res.get("virt_sl")
            if virt_sl is not None:
                if role == "BANK_LAYER" and r_res.get("arm_bank_harvest"):
                    bh_hit = (exec_price <= float(virt_sl) + 1e-9) if is_buy else (exec_price >= float(virt_sl) - 1e-9)
                    if bh_hit:
                        bh_t = _int_num(lv.get("bank_harvest_ticks"), 0) + 1
                        lv["bank_harvest_ticks"] = bh_t
                        if bh_t >= confirm_req:
                            result["harvest_close_tickets"].append({
                                "layer": lk,
                                "ticket": lt,
                                "entry_price": entry_px,
                                "peak_price": lv.get("mfe_peak_price"),
                                "mfe_dollars": new_mfe,
                                "virt_sl": float(virt_sl),
                                "exec_price": exec_price,
                            })
                    else:
                        lv["bank_harvest_ticks"] = 0
                elif (
                    role in ("CORE_RUNNER", "SOLO_RUNNER")
                    and (new_tier == "TIER_0T" or now_squeeze)
                    and bool(_cfg("PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED", PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED))
                ):
                    v_hit = (exec_price <= float(virt_sl) + 1e-9) if is_buy else (exec_price >= float(virt_sl) - 1e-9)
                    if v_hit:
                        vr_t = _int_num(lv.get("virt_ratchet_ticks"), 0) + 1
                        lv["virt_ratchet_ticks"] = vr_t
                        if vr_t >= confirm_req:
                            result["virtual_close_tickets"].append({
                                "layer": lk,
                                "ticket": lt,
                                "role": role,
                                "tier": "SQUEEZE" if now_squeeze else "TIER_0T",
                                "entry_price": entry_px,
                                "peak_price": lv.get("mfe_peak_price"),
                                "mfe_dollars": new_mfe,
                                "virt_sl": float(virt_sl),
                                "exec_price": exec_price,
                            })
                    else:
                        lv["virt_ratchet_ticks"] = 0

        if lock_roles_now:
            signal_data["roles_locked"] = True
        if sweep_pendings_now and bool(_cfg("CANCEL_PENDING_ON_PROFIT_LOCK", CANCEL_PENDING_ON_PROFIT_LOCK)) and signal_data.get("pending_tickets"):
            swept = self.cancel_signal_pending_orders(signal_data)
            signal_data["pending_tickets"] = []
            for l_val in layers.values():
                if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                    l_val["status"] = "CANCELLED"
            result["pending_swept"] = swept

        return result

    def prepare_deepest_runners_for_tp_breakout(
        self,
        signal_data: Dict[str, Any],
        live_price: Optional[float] = None,
        chart_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Pre-TP1 Proactive Runner Breakout Preparation:
        1. Assigns/preserves sticky layer roles when PROFIT_RATCHET_ENABLED=True.
        2. Resets any early_layers back to final_tp unless roles are locked as STAGE2_BANKER/CORE_RUNNER.
        3. Honors BANK_ALL_AT_TP1 / BANK_SINGLE_LAYER_AT_TP1 and executes strict 2-step SL-first TP2 lift.
        """
        layers = signal_data.get("layers") or {}
        market_tickets = list(signal_data.get("market_tickets") or [])
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(symbol)

        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        raw_tp1 = signal_data.get("raw_tp1") or tp_levels.get("TP1")
        final_tp = signal_data.get("final_tp") or signal_data.get("hard_broker_tp")
        if final_tp is None and raw_tp1 is not None:
            offset = float(_cfg("TP_FRONT_RUN_OFFSET", TP_FRONT_RUN_OFFSET)) * (pip_size / 0.10 if pip_size > 0 else 1.0)
            final_tp = round(float(raw_tp1) - offset, digits) if is_buy else round(float(raw_tp1) + offset, digits)

        ratchet_on = bool(_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED))
        tiered_on = bool(_cfg("TIERED_STAGE_BANKING_ENABLED", TIERED_STAGE_BANKING_ENABLED))
        if ratchet_on and not _is_legacy_3layer_dict(layers) and market_tickets:
            roles = self.assign_or_preserve_layer_roles(signal_data)
            open_keys = self._ordered_open_layer_keys(signal_data)
            runner_layers = [lk for lk in open_keys if roles.get(lk) in ("CORE_RUNNER", "SOLO_RUNNER")]
            stage2_layers = [lk for lk in open_keys if roles.get(lk) == "STAGE2_BANKER"] if tiered_on else []
            early_layers = [lk for lk in open_keys if lk not in runner_layers and lk not in stage2_layers]
        else:
            full_cnt = _int_num(_cfg("RUNNER_DEEPEST_COUNT_FULL", RUNNER_DEEPEST_COUNT_FULL), 1)
            runner_layers, early_layers = select_dynamic_runners(layers, market_tickets, full_runner_count=full_cnt)
            stage2_layers = []

        runner_tickets = [
            _int_num(layers[lk].get("ticket"), 0)
            for lk in runner_layers
            if isinstance(layers.get(lk), dict) and _int_num(layers[lk].get("ticket"), 0) > 0
        ]

        signal_data["runner_layers"] = list(runner_layers)
        signal_data["runner_tickets"] = list(runner_tickets)
        signal_data["runner_layer"] = runner_layers[-1] if runner_layers else None
        signal_data["runner_ticket"] = runner_tickets[-1] if runner_tickets else None
        if runner_layers:
            signal_data["last_runner_layers"] = list(runner_layers)
            signal_data["last_runner_layer"] = runner_layers[-1]

        reset_tickets: List[int] = []
        if final_tp is not None and not signal_data.get("roles_locked"):
            target_tp1 = round(float(final_tp), digits)
            for lk in early_layers:
                l_info = layers.get(lk)
                if not isinstance(l_info, dict):
                    continue
                lt = _int_num(l_info.get("ticket"), 0)
                cur_tp = _num(l_info.get("tp"), 0.0)
                if lt > 0 and (abs(cur_tp - target_tp1) > 1e-6 or l_info.get("tp_stage") != "TP1"):
                    if self.modify_position_sltp(lt, sl=None, tp=target_tp1):
                        l_info["tp"] = target_tp1
                        l_info["tp_stage"] = "TP1"
                        reset_tickets.append(lt)

        tick_obj = None
        if live_price is None:
            tick_obj = self.get_tick(symbol)
            if tick_obj is not None:
                cand_px = getattr(tick_obj, "bid", None) if is_buy else getattr(tick_obj, "ask", None)
                if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                    live_price = float(cand_px)
                if chart_price is None:
                    cand_chart = getattr(tick_obj, "bid", None)
                    if isinstance(cand_chart, (int, float)) and not isinstance(cand_chart, bool) and cand_chart > 0:
                        chart_price = float(cand_chart)
        else:
            b_px = float(chart_price if chart_price is not None else live_price)
            a_px = round(b_px + 0.20, digits) if is_buy else float(live_price)
            tick_obj = SimpleNamespace(bid=b_px, ask=a_px)
        eval_price = chart_price if chart_price is not None else live_price

        lifted_tickets: List[int] = []
        proactive = bool(_cfg("RUNNER_PROACTIVE_TP_LIFT", RUNNER_PROACTIVE_TP_LIFT))
        configured_max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    5,
                ),
            ),
        )
        max_stage = self._resolve_effective_max_stage(configured_max_stage, tp_levels)
        signal_data["runner_max_tp_stage"] = configured_max_stage
        signal_data["effective_max_stage"] = max_stage
        bank_all_tp1 = bool(_cfg("BANK_ALL_AT_TP1", False)) or (max_stage <= 1)
        bank_single_tp1 = bool(_cfg("BANK_SINGLE_LAYER_AT_TP1", False))
        if bank_all_tp1 or (bank_single_tp1 and len(market_tickets) == 1):
            proactive = False
        tp2 = tp_levels.get("TP2")

        if proactive and max_stage >= 2 and raw_tp1 is not None and tp2 is not None and eval_price is not None and runner_layers:
            anchor = self._resolve_milestone_anchor(signal_data, tp_levels, 1)
            eff_buf, _ = self._compute_proximity_and_pullback(
                float(raw_tp1), float(eval_price), pip_size, anchor=anchor
            )
            dist_to_tp1 = (float(raw_tp1) - float(eval_price)) if is_buy else (float(eval_price) - float(raw_tp1))
            should_lift_early = dist_to_tp1 <= eff_buf + 1e-9

            if bool(_cfg("GOLD_REVERSAL_GUARD_ENABLED", GOLD_REVERSAL_GUARD_ENABLED)):
                sig_entry = self._resolve_signal_primary_entry(signal_data)
                if sig_entry is not None and sig_entry > 0:
                    leg_span = (float(raw_tp1) - sig_entry) if is_buy else (sig_entry - float(raw_tp1))
                    if leg_span > 1e-9:
                        prog_pct = ((float(eval_price) - sig_entry) / leg_span) if is_buy else ((sig_entry - float(eval_price)) / leg_span)
                        lift_pct = float(_cfg("RUNNER_EARLY_TP_LIFT_PCT", RUNNER_EARLY_TP_LIFT_PCT))
                        if prog_pct + 1e-9 >= lift_pct:
                            should_lift_early = True

            if should_lift_early:
                target_tp2 = round(float(tp2), digits)
                lift_targets = list(runner_layers) + list(stage2_layers)
                for lk in lift_targets:
                    l_info = layers.get(lk)
                    if not isinstance(l_info, dict):
                        continue
                    lt = _int_num(l_info.get("ticket"), 0)
                    cur_tp = _num(l_info.get("tp"), 0.0)
                    if lt > 0 and (abs(cur_tp - target_tp2) > 1e-6 or l_info.get("tp_stage") != "TP2"):
                        l_entry = self._resolve_layer_entry(signal_data, lk, l_info) or 0.0
                        if ratchet_on and l_entry > 0.0 and live_price is not None:
                            l_mfe = max(0.0, _num(l_info.get("mfe_dollars"), 0.0), (live_price - l_entry) if is_buy else (l_entry - live_price))
                            t1a_off = float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS))
                            lift_sl = round((l_entry + t1a_off) if is_buy else (l_entry - t1a_off), digits)
                            l_res = self._lift_ticket_tp_two_step(
                                signal_data=signal_data,
                                layer_key=lk,
                                ticket=lt,
                                next_tp=target_tp2,
                                next_tp_stage="TP2",
                                desired_broker_sl=lift_sl,
                                entry_price=l_entry,
                                mfe_exec=l_mfe,
                                tick=tick_obj,
                                sym_info=sym_info,
                                digits=digits,
                            )
                            if l_res.get("lifted"):
                                lifted_tickets.append(lt)
                        elif self.modify_position_sltp(lt, sl=None, tp=target_tp2):
                            l_info["tp"] = target_tp2
                            l_info["tp_stage"] = "TP2"
                            lifted_tickets.append(lt)
                if lifted_tickets or all(
                    isinstance(layers.get(lk), dict) and layers[lk].get("tp_stage") == "TP2"
                    for lk in runner_layers
                ):
                    signal_data["tp_stage"] = "TP2"
                    if ratchet_on and lifted_tickets:
                        signal_data["roles_locked"] = True

        return {
            "runner_layers": runner_layers,
            "early_layers": early_layers,
            "runner_tickets": runner_tickets,
            "lifted_tickets": lifted_tickets,
            "reset_tickets": reset_tickets,
        }

    def _resolve_layer_entry(
        self,
        signal_data: Dict[str, Any],
        layer_key: Optional[str],
        l_obj: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """
        Resolve actual fill price of a specific layer, preferring:
          entry_price / entry / price_open -> target_price -> recorded_entry_price -> L1 market/signal entry.
        """
        layers = signal_data.get("layers") or {}
        if l_obj is None and layer_key and isinstance(layers.get(layer_key), dict):
            l_obj = layers[layer_key]
        if isinstance(l_obj, dict):
            for ek in ("entry_price", "entry", "price_open"):
                val = l_obj.get(ek)
                if isinstance(val, numbers.Real) and not isinstance(val, bool) and float(val) > 0.0:
                    return float(val)
            rec_entry = signal_data.get("recorded_entry_price")
            if (
                layer_key in ("L1", "TOP")
                and str(l_obj.get("order_type") or "").strip().upper() == "MARKET"
                and isinstance(rec_entry, numbers.Real)
                and not isinstance(rec_entry, bool)
                and float(rec_entry) > 0.0
            ):
                return float(rec_entry)
            tp_px = l_obj.get("target_price")
            if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0.0:
                return float(tp_px)
        return self._resolve_signal_primary_entry(signal_data)

    def _resolve_signal_primary_entry(self, signal_data: Dict[str, Any]) -> Optional[float]:
        """Resolve primary active/recorded signal entry price for measuring % progress toward TP1."""
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        layers = signal_data.get("layers") or {}
        market_set = {_int_num(t, 0) for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) > 0}

        # Prefer actual fill price on currently open shallowest layer first
        for lk in ("L1", "TOP", "L2", "MID", "L3", "L4", "BOTTOM"):
            l_obj = layers.get(lk)
            if not isinstance(l_obj, dict):
                continue
            lt = _int_num(l_obj.get("ticket"), 0)
            st = str(l_obj.get("status") or "").strip().upper()
            is_open_layer = (lt in market_set) or (st in ("OPEN", "FILLED", "RUNNER"))
            if is_open_layer:
                for ek in ("entry_price", "entry", "price_open"):
                    ev = l_obj.get(ek)
                    if isinstance(ev, numbers.Real) and not isinstance(ev, bool) and float(ev) > 0:
                        return float(ev)

        rec_entry = signal_data.get("recorded_entry_price")
        if isinstance(rec_entry, numbers.Real) and not isinstance(rec_entry, bool) and float(rec_entry) > 0:
            for lk in ("L1", "TOP"):
                l_obj = layers.get(lk)
                if isinstance(l_obj, dict) and str(l_obj.get("order_type") or "").strip().upper() == "MARKET":
                    return float(rec_entry)

        # Open layer target_price before closed L1 target_price
        for lk in ("L1", "TOP", "L2", "MID", "L3", "L4", "BOTTOM"):
            l_obj = layers.get(lk)
            if not isinstance(l_obj, dict):
                continue
            lt = _int_num(l_obj.get("ticket"), 0)
            st = str(l_obj.get("status") or "").strip().upper()
            if (lt in market_set) or (st in ("OPEN", "FILLED", "RUNNER")):
                tp_px = l_obj.get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                    return float(tp_px)

        for key in ("recorded_entry_price", "initial_price"):
            val = signal_data.get(key)
            if isinstance(val, numbers.Real) and not isinstance(val, bool) and float(val) > 0:
                return float(val)
        for lk in ("L1", "TOP"):
            l_obj = layers.get(lk)
            if isinstance(l_obj, dict):
                tp_px = l_obj.get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                    return float(tp_px)
        z = signal_data.get("zone")
        if isinstance(z, dict):
            zv = z.get("top") if direction == "BUY" else z.get("bottom")
            if isinstance(zv, numbers.Real) and not isinstance(zv, bool) and float(zv) > 0:
                return float(zv)
        for lk in ("L2", "L3", "L4", "MID", "BOTTOM"):
            l_obj = layers.get(lk)
            if isinstance(l_obj, dict):
                tp_px = l_obj.get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                    return float(tp_px)
        return None

    def _apply_stage0_profit_lock(
        self,
        signal_data: Dict[str, Any],
        symbol: str,
        direction: str,
        runner_entry: float,
        pip_size: float,
        digits: int,
        tick: Any,
        sym_info: Any,
        has_unfilled_zone_layers: bool = False,
    ) -> float:
        """
        Lock open Stage 0 runner position(s) at Entry +/- STAGE0_PROFIT_LOCK_OFFSET_PIPS (BE + 5p)
        on broker SL + virtual state, and sweep pending L2..L4 limits on single-layer unfilled zones
        so flash crashes cannot turn a near-TP1 winner red or leave unfilled limits exposed.
        """
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        eff_pip = pip_size if pip_size > 0 else 0.10
        lock_offset = float(_cfg("STAGE0_PROFIT_LOCK_OFFSET_PIPS", STAGE0_PROFIT_LOCK_OFFSET_PIPS)) * eff_pip
        lock_sl = round((runner_entry + lock_offset) if is_buy else (runner_entry - lock_offset), digits)
        clamped_lock_sl = self._clamp_sl_for_broker_stops(lock_sl, direction, tick, sym_info, digits)
        is_green_sl = (
            ((clamped_lock_sl > runner_entry + 1e-9) if is_buy else (clamped_lock_sl < runner_entry - 1e-9))
            if lock_offset > 0.0
            else True
        )

        layers = signal_data.get("layers") or {}
        ticket_to_layer: Dict[int, Dict[str, Any]] = {}
        for l_val in layers.values():
            if isinstance(l_val, dict):
                lt = _int_num(l_val.get("ticket"), 0)
                if lt > 0:
                    ticket_to_layer[lt] = l_val

        open_tickets = list(
            dict.fromkeys(
                t
                for t in (
                    [_int_num(x, 0) for x in (signal_data.get("market_tickets") or [])]
                    + [_int_num(x, 0) for x in (signal_data.get("runner_tickets") or [])]
                )
                if t > 0
            )
        )
        any_sl_synced = False
        for tkt in open_tickets:
            l_info = ticket_to_layer.get(tkt)
            cur_sl = 0.0
            if not self.dry_run:
                with self._mt5_lock:
                    pos_res = mt5.positions_get(ticket=tkt)
                if pos_res:
                    cur_sl = _num(getattr(pos_res[0], "sl", 0.0), 0.0)
            if cur_sl <= 0.0 and l_info is not None:
                cur_sl = _num(l_info.get("sl") or l_info.get("be_sl"), 0.0)

            already_better = cur_sl > 0.0 and (
                (is_buy and cur_sl >= clamped_lock_sl - 1e-9)
                or (not is_buy and cur_sl <= clamped_lock_sl + 1e-9)
            )
            if already_better:
                any_sl_synced = True
                continue
            if not is_green_sl:
                continue
            if self.modify_position_sltp(tkt, sl=clamped_lock_sl, tp=None):
                any_sl_synced = True
                if l_info is not None:
                    l_info["sl"] = clamped_lock_sl
                prev_bsl = _num(signal_data.get("broker_sl"), 0.0)
                if (
                    prev_bsl <= 0.0
                    or (is_buy and clamped_lock_sl > prev_bsl + 1e-9)
                    or (not is_buy and clamped_lock_sl < prev_bsl - 1e-9)
                ):
                    signal_data["broker_sl"] = clamped_lock_sl

        signal_data["stage0_profit_lock_active"] = True
        signal_data["stage0_profit_lock_sl"] = lock_sl
        signal_data["stage0_broker_sl_synced"] = bool(any_sl_synced or not open_tickets)

        if has_unfilled_zone_layers:
            signal_data["stage0_zone_protected"] = True
            if (
                (self.dry_run or signal_data["stage0_broker_sl_synced"])
                and bool(_cfg("CANCEL_PENDING_ON_STAGE0_LOCK_EXIT", CANCEL_PENDING_ON_STAGE0_LOCK_EXIT))
            ):
                self.cancel_signal_pending_orders(signal_data)
                signal_data["pending_tickets"] = []
                for l_val in layers.values():
                    if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                        l_val["status"] = "CANCELLED"

        logger.info(
            f"Stage 0 Profit-Lock ARMED (lock_sl={lock_sl}, broker_sl={clamped_lock_sl if is_green_sl else None}, "
            f"synced={signal_data['stage0_broker_sl_synced']}, entry={runner_entry}, zone_protected={has_unfilled_zone_layers})"
        )
        return lock_sl

    def evaluate_gold_reversal_guard(
        self,
        signal_data: Dict[str, Any],
        tick: Any = None,
        runner_entry_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Gold (XAUUSD) Calibrated Peak-Profit Giveback Guard + Stage 0 Profit-Lock:
        1. Chart-price arming (`ARM_THRESHOLD_PCT = 0.85`, or `0.80` on true tight scalps,
           plus Cliff-Gap Catch #1 when runner `tp_stage == "TP2"` has reached `final_tp`).
        2. Stage 0 Profit-Lock: upon arming in profit >= STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS,
           locks hard broker SL at `Entry +/- STAGE0_PROFIT_LOCK_OFFSET_PIPS` (+5p),
           cancels unfilled L2..L4 pending limits on single-layer zones, and guarantees green exits.
        3. Wide-Zone Protection: unfilled-zone setups & true wide zones are immune to tight scalp
           calibration and premature Stage 0 $1.50/30% market-kills while protected by Stage 0 lock.
        4. Confirmation ticks (`REVERSAL_CONFIRM_TICKS = 2`) and Adaptive Spread Spike Filter.
        """
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"

        staged_exit_on = bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))
        if staged_exit_on and isinstance(signal_data, dict) and self.get_runner_fsm_stage(signal_data) >= 1:
            signal_data["reversal_guard_armed"] = False
            return {
                "armed": False,
                "triggered": False,
                "stage0_lock_triggered": False,
                "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                "hard_ceiling_triggered": False,
                "paused_by_spread": False,
                "disabled_post_tp1": True,
                "spread_dollars": 0.0,
                "max_allowed_spread": float(_cfg("MAX_ALLOWED_SPREAD_DOLLARS", MAX_ALLOWED_SPREAD_DOLLARS)),
                "peak_profit": _num(signal_data.get("peak_profit", 0.0), 0.0),
                "current_profit": 0.0,
                "pullback_dollars": 0.0,
                "drawdown_from_peak": 0.0,
                "noise_floor_dollars": float(_cfg("MIN_GOLD_PULLBACK_DOLLARS", MIN_GOLD_PULLBACK_DOLLARS)),
                "m1_atr_14": 0.0,
                "peak_price": signal_data.get("trail_peak") or signal_data.get("runner_peak_price"),
                "live_price": None,
            }

        if tick is None:
            tick = self.get_tick(symbol)
        if tick is None:
            return {
                "armed": bool(signal_data.get("reversal_guard_armed", False)),
                "triggered": False,
                "stage0_lock_triggered": False,
                "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                "paused_by_spread": False,
            }

        raw_exec = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        if not isinstance(raw_exec, numbers.Real) or isinstance(raw_exec, bool) or float(raw_exec) <= 0:
            return {
                "armed": bool(signal_data.get("reversal_guard_armed", False)),
                "triggered": False,
                "stage0_lock_triggered": False,
                "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                "paused_by_spread": False,
            }
        exec_price = float(raw_exec)
        raw_chart = getattr(tick, "bid", None)
        chart_price = (
            float(raw_chart)
            if isinstance(raw_chart, numbers.Real) and not isinstance(raw_chart, bool) and float(raw_chart) > 0
            else exec_price
        )

        is_spike, spread_dollars, eff_max_spread = self.check_spread_spike(symbol, tick)
        signal_data["spread_spike_paused"] = is_spike

        layers = signal_data.get("layers") or {}
        market_tickets = list(signal_data.get("market_tickets") or [])
        pending_tickets = list(signal_data.get("pending_tickets") or [])
        has_pending_layer = any(
            isinstance(lv, dict) and str(lv.get("status") or "").strip().upper() == "PENDING"
            for lv in layers.values()
        )
        has_unfilled_zone_layers = (len(market_tickets) == 1) and (bool(pending_tickets) or has_pending_layer)

        raw_rls = signal_data.get("runner_layers")
        if isinstance(raw_rls, list) and raw_rls:
            runner_layers = [str(x) for x in raw_rls if x]
        else:
            full_cnt = _int_num(_cfg("RUNNER_DEEPEST_COUNT_FULL", RUNNER_DEEPEST_COUNT_FULL), 1)
            runner_layers, _ = select_dynamic_runners(layers, market_tickets, full_runner_count=full_cnt)

        primary_rl = runner_layers[-1] if runner_layers else signal_data.get("runner_layer")
        sig_entry = self._resolve_signal_primary_entry(signal_data)

        runner_entry: Optional[float] = None
        if runner_entry_override is not None and float(runner_entry_override) > 0:
            runner_entry = float(runner_entry_override)
        elif primary_rl and isinstance(layers.get(primary_rl), dict):
            l_obj = layers[primary_rl]
            rec_entry = signal_data.get("recorded_entry_price")
            if (
                primary_rl in ("L1", "TOP")
                and str(l_obj.get("order_type") or "").strip().upper() == "MARKET"
                and isinstance(rec_entry, numbers.Real)
                and not isinstance(rec_entry, bool)
                and float(rec_entry) > 0
            ):
                runner_entry = float(rec_entry)
            else:
                tp_px = l_obj.get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                    runner_entry = float(tp_px)
        if runner_entry is None:
            runner_entry = sig_entry
        if sig_entry is None:
            sig_entry = runner_entry

        if runner_entry is None or runner_entry <= 0:
            return {
                "armed": bool(signal_data.get("reversal_guard_armed", False)),
                "triggered": False,
                "stage0_lock_triggered": False,
                "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                "paused_by_spread": is_spike,
            }

        pip_size = self.get_pip_size(symbol)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        raw_tp1 = signal_data.get("raw_tp1") or tp_levels.get("TP1")

        zone_mid_leg: Optional[float] = None
        zone_obj = signal_data.get("zone")
        if raw_tp1 is not None and isinstance(zone_obj, dict):
            z_mid = zone_obj.get("mid")
            if not (isinstance(z_mid, numbers.Real) and not isinstance(z_mid, bool) and float(z_mid) > 0):
                z_top = zone_obj.get("top")
                z_bot = zone_obj.get("bottom")
                if (
                    isinstance(z_top, numbers.Real) and not isinstance(z_top, bool)
                    and isinstance(z_bot, numbers.Real) and not isinstance(z_bot, bool)
                ):
                    z_mid = (float(z_top) + float(z_bot)) / 2.0
            if isinstance(z_mid, numbers.Real) and not isinstance(z_mid, bool) and float(z_mid) > 0:
                zone_mid_leg = (float(raw_tp1) - float(z_mid)) if is_buy else (float(z_mid) - float(raw_tp1))

        tp1_leg = (
            ((float(raw_tp1) - sig_entry) if is_buy else (sig_entry - float(raw_tp1)))
            if (raw_tp1 is not None and sig_entry is not None)
            else None
        )
        is_tight_scalp = bool(
            tp1_leg is not None
            and 0.0 < tp1_leg <= 4.0
            and not has_unfilled_zone_layers
            and not bool(signal_data.get("stage0_zone_protected"))
            and (zone_mid_leg is None or zone_mid_leg <= 4.0)
        )

        current_profit = round((exec_price - runner_entry) if is_buy else (runner_entry - exec_price), 6)

        # Re-anchor stored peak_profit cleanly if deepest runner layer changed (e.g. deeper limit filled)
        prev_r_entry = signal_data.get("reversal_guard_runner_entry")
        prev_peak_px = signal_data.get("runner_peak_price")
        prev_peak_profit = _num(
            signal_data.get("peak_profit", signal_data.get("reversal_guard_peak_profit", 0.0)),
            0.0,
        )
        if prev_r_entry is not None and abs(float(prev_r_entry) - runner_entry) > 1e-6 and prev_peak_px is not None:
            prev_peak_profit = round((float(prev_peak_px) - runner_entry) if is_buy else (runner_entry - float(prev_peak_px)), 6)
        signal_data["reversal_guard_runner_entry"] = runner_entry

        arm_pct = float(_cfg("ARM_THRESHOLD_PCT", ARM_THRESHOLD_PCT))
        armed = bool(signal_data.get("reversal_guard_armed", False))

        # Track live & peak profit every tick in dollars
        tick_peak_profit = max(0.0, prev_peak_profit, current_profit)
        if prev_peak_px is None:
            tick_peak_px = exec_price if current_profit > 0 else prev_peak_px
        else:
            tick_peak_px = max(float(prev_peak_px), exec_price) if is_buy else min(float(prev_peak_px), exec_price)

        if not armed:
            if (not staged_exit_on) and (signal_data.get("tp1_reached") or signal_data.get("tp1_touched")):
                armed = True
            elif tp1_leg is not None and tp1_leg > 1e-9:
                eff_arm_pct = min(arm_pct, 0.80) if is_tight_scalp else arm_pct
                sig_progress = ((chart_price - sig_entry) / tp1_leg) if is_buy else ((sig_entry - chart_price) / tp1_leg)
                if sig_progress + 1e-9 >= eff_arm_pct and current_profit > 0:
                    armed = True

            # Cliff-Gap Catch #1: if 70% lift already promoted the runner to TP2 and chart_price reached final_tp, arm immediately
            if not armed and current_profit > 0:
                runner_on_tp2 = (str(signal_data.get("tp_stage") or "").strip().upper() == "TP2") or any(
                    isinstance(layers.get(rk), dict) and str(layers[rk].get("tp_stage") or "").strip().upper() == "TP2"
                    for rk in runner_layers
                )
                f_tp = signal_data.get("final_tp") or signal_data.get("hard_broker_tp")
                if runner_on_tp2 and isinstance(f_tp, numbers.Real) and not isinstance(f_tp, bool) and float(f_tp) > 0:
                    f_tp_val = float(f_tp)
                    if (is_buy and chart_price >= f_tp_val - 1e-9) or (not is_buy and chart_price <= f_tp_val + 1e-9):
                        armed = True

            if armed:
                signal_data["reversal_guard_armed"] = True
                peak_profit = tick_peak_profit
                peak_px = tick_peak_px if tick_peak_px is not None else exec_price
                signal_data["peak_profit"] = round(peak_profit, 6)
                signal_data["reversal_guard_peak_profit"] = round(peak_profit, 6)
                signal_data["runner_peak_price"] = round(peak_px, digits)
                signal_data["runner_reversal_ticks"] = 0
                logger.info(
                    f"Gold Reversal Guard ARMED (exec={exec_price}, chart={chart_price}, "
                    f"peak_profit=${peak_profit:.2f}, runner_entry={runner_entry}, arm_pct={arm_pct*100:.0f}%)"
                )
            else:
                signal_data["reversal_guard_armed"] = False
                signal_data["peak_profit"] = round(tick_peak_profit, 6)
                signal_data["reversal_guard_peak_profit"] = round(tick_peak_profit, 6)
                if tick_peak_px is not None:
                    signal_data["runner_peak_price"] = round(tick_peak_px, digits)
                return {
                    "armed": False,
                    "triggered": False,
                    "stage0_lock_triggered": False,
                    "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                    "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                    "hard_ceiling_triggered": False,
                    "paused_by_spread": is_spike,
                    "spread_dollars": spread_dollars,
                    "max_allowed_spread": eff_max_spread,
                    "peak_profit": tick_peak_profit,
                    "current_profit": current_profit,
                    "pullback_dollars": 0.0,
                    "drawdown_from_peak": 0.0,
                    "noise_floor_dollars": float(_cfg("MIN_GOLD_PULLBACK_DOLLARS", MIN_GOLD_PULLBACK_DOLLARS)),
                    "m1_atr_14": 0.0,
                    "peak_price": tick_peak_px,
                    "live_price": exec_price,
                }

        # Armed: continuously update peak_profit & runner_peak_price as runner climbs TP1 -> TP2 -> TP3 -> TP4
        peak_profit = tick_peak_profit
        peak_px = tick_peak_px if tick_peak_px is not None else exec_price

        if current_profit > prev_peak_profit + 1e-9:
            signal_data["runner_reversal_ticks"] = 0

        signal_data["peak_profit"] = round(peak_profit, 6)
        signal_data["reversal_guard_peak_profit"] = round(peak_profit, 6)
        signal_data["runner_peak_price"] = round(peak_px, digits)

        pullback_dollars = max(0.0, round(peak_profit - current_profit, 6))
        drawdown_from_peak = (pullback_dollars / peak_profit) if peak_profit > 1e-9 else 0.0
        signal_data["drawdown_from_peak"] = round(drawdown_from_peak, 6)

        if not signal_data.get("tp1_reached"):
            stage0_lock_on = bool(_cfg("STAGE0_PROFIT_LOCK_ENABLED", STAGE0_PROFIT_LOCK_ENABLED))
            min_lock_profit = float(_cfg("STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS", STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS))
            if (
                stage0_lock_on
                and (not signal_data.get("stage0_profit_lock_active") or not signal_data.get("stage0_broker_sl_synced", True))
                and current_profit + 1e-9 >= min_lock_profit
            ):
                self._apply_stage0_profit_lock(
                    signal_data=signal_data,
                    symbol=symbol,
                    direction=direction,
                    runner_entry=runner_entry,
                    pip_size=pip_size,
                    digits=digits,
                    tick=tick,
                    sym_info=sym_info,
                    has_unfilled_zone_layers=has_unfilled_zone_layers,
                )

            if signal_data.get("stage0_profit_lock_active"):
                s0_sl = signal_data.get("stage0_profit_lock_sl")
                if isinstance(s0_sl, numbers.Real) and not isinstance(s0_sl, bool) and float(s0_sl) > 0:
                    s0_sl_f = float(s0_sl)
                    s0_hit = (exec_price <= s0_sl_f + 1e-9) if is_buy else (exec_price >= s0_sl_f - 1e-9)
                    if s0_hit and not is_spike:
                        logger.info(
                            f"Stage 0 Profit-Lock TRIGGERED (exec={exec_price}, lock_sl={s0_sl_f}, "
                            f"peak=${peak_profit:.2f}, live=${current_profit:.2f})"
                        )
                        return {
                            "armed": True,
                            "triggered": True,
                            "stage0_lock_triggered": True,
                            "stage0_profit_lock_active": True,
                            "stage0_profit_lock_sl": s0_sl_f,
                            "hard_ceiling_triggered": False,
                            "paused_by_spread": False,
                            "spread_dollars": spread_dollars,
                            "max_allowed_spread": eff_max_spread,
                            "peak_profit": peak_profit,
                            "current_profit": current_profit,
                            "pullback_dollars": pullback_dollars,
                            "drawdown_from_peak": drawdown_from_peak,
                            "noise_floor_dollars": float(_cfg("MIN_GOLD_PULLBACK_DOLLARS", MIN_GOLD_PULLBACK_DOLLARS)),
                            "m1_atr_14": 0.0,
                            "peak_price": peak_px,
                            "live_price": exec_price,
                        }

        m1_atr_14 = self.get_m1_atr(symbol)
        min_pb_dollars = float(_cfg("MIN_GOLD_PULLBACK_DOLLARS", MIN_GOLD_PULLBACK_DOLLARS))
        atr_mult = float(_cfg("ATR_NOISE_FLOOR_MULT", ATR_NOISE_FLOOR_MULT))
        noise_floor_dollars = round(max(min_pb_dollars, atr_mult * m1_atr_14), 4)
        if is_tight_scalp and tp1_leg is not None and not signal_data.get("tp2_reached"):
            noise_floor_dollars = min(noise_floor_dollars, max(1.65, round(0.60 * tp1_leg, 4)))

        giveback_pct = float(_cfg("GIVEBACK_PCT", GIVEBACK_PCT))
        max_gb_dollars = float(_cfg("MAX_GIVEBACK_DOLLARS", MAX_GIVEBACK_DOLLARS))
        hard_ceil_cfg = float(_cfg("HARD_GIVEBACK_CEILING_DOLLARS", HARD_GIVEBACK_CEILING_DOLLARS))
        positive_caps = [v for v in (max_gb_dollars, hard_ceil_cfg) if v > 0.0]
        hard_ceiling_dollars = min(positive_caps) if positive_caps else 0.0
        zone_protected = bool(has_unfilled_zone_layers or signal_data.get("stage0_zone_protected"))
        min_peak_for_ceiling = 5.0 if zone_protected else 0.0
        hard_ceiling_breached = (
            peak_profit >= min_peak_for_ceiling
            and peak_profit > 0.0
            and hard_ceiling_dollars > 0.0
            and pullback_dollars + 1e-9 >= hard_ceiling_dollars
        )

        # Hard giveback ceiling overrides 30% rule, noise floor, and spread-spike pause
        if hard_ceiling_breached:
            logger.info(
                f"Gold Reversal Guard HARD CEILING TRIGGERED "
                f"(peak=${peak_profit:.2f}, live=${current_profit:.2f}, "
                f"giveback=${pullback_dollars:.2f} >= ceiling=${hard_ceiling_dollars:.2f}, spread=${spread_dollars:.2f})"
            )
            return {
                "armed": True,
                "triggered": True,
                "stage0_lock_triggered": False,
                "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                "hard_ceiling_triggered": True,
                "paused_by_spread": False,
                "spread_dollars": spread_dollars,
                "max_allowed_spread": eff_max_spread,
                "peak_profit": peak_profit,
                "current_profit": current_profit,
                "pullback_dollars": pullback_dollars,
                "drawdown_from_peak": drawdown_from_peak,
                "noise_floor_dollars": noise_floor_dollars,
                "m1_atr_14": m1_atr_14,
                "peak_price": peak_px,
                "live_price": exec_price,
            }

        if is_spike:
            logger.info(
                f"Gold Reversal Guard PAUSED due to spread spike (${spread_dollars:.2f} > ${eff_max_spread:.2f} max)"
            )
            return {
                "armed": True,
                "triggered": False,
                "stage0_lock_triggered": False,
                "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
                "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
                "hard_ceiling_triggered": False,
                "paused_by_spread": True,
                "spread_dollars": spread_dollars,
                "max_allowed_spread": eff_max_spread,
                "peak_profit": peak_profit,
                "current_profit": current_profit,
                "pullback_dollars": pullback_dollars,
                "drawdown_from_peak": drawdown_from_peak,
                "noise_floor_dollars": noise_floor_dollars,
                "m1_atr_14": m1_atr_14,
                "peak_price": peak_px,
                "live_price": exec_price,
            }

        cand_trigger = False
        tp1_trail_on = bool(_cfg("TP1_TRAIL_ENABLED", TP1_TRAIL_ENABLED))
        if signal_data.get("tp1_reached") or signal_data.get("tp1_touched"):
            if tp1_trail_on:
                # v3.4.2: Post-TP1 exits are governed by the dynamic ATR trailing stop (+ $3.50 hard ceiling backstop).
                cand_trigger = False
            elif hard_ceiling_dollars > 0.0 or max_gb_dollars > 0.0:
                eff_cap = hard_ceiling_dollars if hard_ceiling_dollars > 0.0 else max_gb_dollars
                pct_req = giveback_pct * peak_profit
                eff_req_dollars = max(noise_floor_dollars, min(eff_cap, pct_req))
                cand_trigger = (pullback_dollars + 1e-9 >= eff_req_dollars) and (peak_profit > 0.0)
            else:
                cand_trigger = False
        else:
            # Pre-TP1: suppress Stage 0 $1.50/30% market-kill on unfilled-zone / Stage 0 zone-protected signals
            if zone_protected:
                cand_trigger = False
            else:
                cand_trigger = (
                    peak_profit > 0.0
                    and drawdown_from_peak + 1e-9 >= giveback_pct
                    and pullback_dollars + 1e-9 >= noise_floor_dollars
                )

        confirm_req = max(1, _int_num(_cfg("REVERSAL_CONFIRM_TICKS", 2), 2))
        if cand_trigger:
            rev_ticks = _int_num(signal_data.get("reversal_guard_ticks", 0), 0) + 1
            signal_data["reversal_guard_ticks"] = rev_ticks
            triggered = rev_ticks >= confirm_req
        else:
            signal_data["reversal_guard_ticks"] = 0
            triggered = False

        return {
            "armed": True,
            "triggered": triggered,
            "stage0_lock_triggered": False,
            "stage0_profit_lock_active": bool(signal_data.get("stage0_profit_lock_active", False)),
            "stage0_profit_lock_sl": signal_data.get("stage0_profit_lock_sl"),
            "hard_ceiling_triggered": False,
            "paused_by_spread": False,
            "spread_dollars": spread_dollars,
            "max_allowed_spread": eff_max_spread,
            "peak_profit": peak_profit,
            "current_profit": current_profit,
            "pullback_dollars": pullback_dollars,
            "drawdown_from_peak": drawdown_from_peak,
            "noise_floor_dollars": noise_floor_dollars,
            "m1_atr_14": m1_atr_14,
            "peak_price": peak_px,
            "live_price": exec_price,
        }

    def _eval_stage1_atr_armed(
        self,
        signal_data: Dict[str, Any],
        stage: int,
        tp_levels: Dict[str, float],
        direction: str,
        chart_peak: Optional[float],
        atr_stop: float,
    ) -> bool:
        """
        Dual-Gate Stage 1 ATR Activation:
        Holds pure `fsm_floor` (BE + 5p) on initial TP1 handoff and arms Stage 1 ATR trailing ONLY when:
        - `stage >= 2` (or already armed / missing TP1..TP2 / threshold <= 0), OR
        - Gate A: `chart_peak` progresses >= STAGE1_ATR_ACTIVATION_PCT (default 0.45) of TP1 -> TP2, OR
        - Gate B: `atr_stop` naturally crosses past `TP1` into pure TP1-plus profit protection.
        """
        if stage >= 2 or bool(signal_data.get("stage1_atr_armed")):
            signal_data["stage1_atr_armed"] = True
            return True

        act_pct = float(_cfg("STAGE1_ATR_ACTIVATION_PCT", STAGE1_ATR_ACTIVATION_PCT))
        tp1_raw = tp_levels.get("TP1") if isinstance(tp_levels, dict) else None
        if tp1_raw is None:
            tp1_raw = signal_data.get("raw_tp1")
        tp2_raw = tp_levels.get("TP2") if isinstance(tp_levels, dict) else None
        if tp2_raw is None:
            tp2_raw = signal_data.get("raw_tp2")

        if act_pct <= 0.0 or tp1_raw is None or tp2_raw is None:
            signal_data["stage1_atr_armed"] = True
            return True

        is_buy = (direction or "BUY").strip().upper() == "BUY"
        tp1_f = float(tp1_raw)
        tp2_f = float(tp2_raw)
        leg = (tp2_f - tp1_f) if is_buy else (tp1_f - tp2_f)
        if leg <= 1e-9:
            signal_data["stage1_atr_armed"] = True
            return True

        gate_a = False
        if chart_peak is not None and isinstance(chart_peak, numbers.Real) and not isinstance(chart_peak, bool):
            prog = ((float(chart_peak) - tp1_f) / leg) if is_buy else ((tp1_f - float(chart_peak)) / leg)
            gate_a = prog + 1e-9 >= act_pct

        gate_b = (atr_stop >= tp1_f - 1e-9) if is_buy else (atr_stop <= tp1_f + 1e-9)
        armed = bool(gate_a or gate_b)
        signal_data["stage1_atr_armed"] = armed
        return armed

    def handle_tp1_scaling_and_select_runner(self, signal_data: Dict[str, Any]) -> Optional[int]:
        """
        v3.1 Dynamic Multi-Layer TP1 Scaling (with legacy 3-layer fallback):
        1. 4-Layer (L1..L4):
           - Partitions open layers via select_dynamic_runners(layers, market_tickets, RUNNER_DEEPEST_COUNT_FULL):
             * 1 open layer -> 0 layers closed at TP1; single layer scales TP1 -> TP2 -> TP3 -> TP4!
             * 2..3 open layers -> deepest 1 layer scales; earlier layers bank at TP1.
             * 4 open layers -> deepest RUNNER_DEEPEST_COUNT_FULL (1 or 2) layers scale; earlier layers bank at TP1.
           - Cancels unfilled pending_tickets when CANCEL_PENDING_ON_TP1=True.
           - Closes early_layers (if still open) and lifts runner_tickets to TP2 (tp_stage="TP2").
           - Applies BE / TP1 floor protection across runner_tickets and initializes stepped scaling FSM.
        2. Legacy 3-Layer (TOP/MID/BOTTOM): preserves legacy TOP/MID close & single runner behavior.
        """
        layers = signal_data.get("layers") or {}
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(symbol)

        floor_price = self._compute_initial_tp1_floor(signal_data, symbol, direction, pip_size, digits)

        if _is_legacy_3layer_dict(layers):
            return self._handle_tp1_scaling_legacy(signal_data, symbol, direction, floor_price)

        is_buy = direction == "BUY"
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        raw_tp1 = signal_data.get("raw_tp1") or tp_levels.get("TP1")

        signal_data["stage0_profit_lock_active"] = False
        signal_data["stage0_zone_protected"] = False

        market_tickets = list(signal_data.get("market_tickets") or [])
        ratchet_on = bool(_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED))
        tiered_on = bool(_cfg("TIERED_STAGE_BANKING_ENABLED", TIERED_STAGE_BANKING_ENABLED))

        # 1. Pending order cancellation policy
        if _cfg("CANCEL_PENDING_ON_TP1", CANCEL_PENDING_ON_TP1):
            self.cancel_signal_pending_orders(signal_data)
            signal_data["pending_tickets"] = []
            for l_val in layers.values():
                if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                    l_val["status"] = "CANCELLED"

        roles: Dict[str, str] = {}
        if ratchet_on:
            roles = self.assign_or_preserve_layer_roles(signal_data)

        # 2. Dynamic runner vs early layer selection
        full_cnt = _int_num(_cfg("RUNNER_DEEPEST_COUNT_FULL", RUNNER_DEEPEST_COUNT_FULL), 1)
        runner_layers, early_layers = select_dynamic_runners(layers, market_tickets, full_runner_count=full_cnt)

        if not runner_layers and not early_layers and market_tickets:
            if len(market_tickets) == 1:
                runner_tickets = [market_tickets[0]]
                early_tickets: List[int] = []
            else:
                runner_tickets = [market_tickets[-1]]
                early_tickets = list(market_tickets[:-1])
        else:
            early_tickets = [
                _int_num(layers[lk].get("ticket"), 0)
                for lk in early_layers
                if isinstance(layers.get(lk), dict) and _int_num(layers[lk].get("ticket"), 0) in market_tickets
            ]
            runner_tickets = [
                _int_num(layers[lk].get("ticket"), 0)
                for lk in runner_layers
                if isinstance(layers.get(lk), dict) and _int_num(layers[lk].get("ticket"), 0) in market_tickets
            ]

        configured_max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    5,
                ),
            ),
        )
        max_stage = self._resolve_effective_max_stage(configured_max_stage, tp_levels)
        bank_all_tp1 = bool(_cfg("BANK_ALL_AT_TP1", False)) or (max_stage <= 1)
        bank_single_tp1 = bool(_cfg("BANK_SINGLE_LAYER_AT_TP1", False))

        if bank_all_tp1:
            early_tickets = list(dict.fromkeys(early_tickets + runner_tickets + market_tickets))
            early_layers = list(dict.fromkeys(early_layers + runner_layers + list(layers.keys())))
            runner_tickets = []
            runner_layers = []
        elif bank_single_tp1 and len(market_tickets) == 1 and not early_tickets:
            early_tickets = list(market_tickets)
            early_layers = list(runner_layers) if runner_layers else (list(layers.keys()) if layers else ["L1"])
            runner_tickets = []
            runner_layers = []
        elif max_stage <= 1:
            early_tickets = list(dict.fromkeys(early_tickets + runner_tickets))
            early_layers = list(dict.fromkeys(early_layers + runner_layers))
            runner_tickets = []
            runner_layers = []

        # v3.6 Tiered Multi-Layer Stage Banking: keep STAGE2_BANKER open past TP1 to aim for TP2
        stage2_bankers: List[Tuple[str, int]] = []
        if ratchet_on and tiered_on and not bank_all_tp1 and not (bank_single_tp1 and len(market_tickets) == 1) and max_stage >= 2:
            s2_ticket_set: Set[int] = set()
            filtered_early_layers: List[str] = []
            for lk in early_layers:
                lv = layers.get(lk)
                if isinstance(lv, dict) and str(roles.get(lk) or lv.get("role") or "").strip().upper() == "STAGE2_BANKER":
                    lt = _int_num(lv.get("ticket"), 0)
                    if lt > 0 and lt in market_tickets:
                        s2_ticket_set.add(lt)
                        stage2_bankers.append((lk, lt))
                        continue
                filtered_early_layers.append(lk)
            early_layers = filtered_early_layers
            early_tickets = [et for et in early_tickets if et not in s2_ticket_set]

        # v3.6 N=1 SOLO_RUNNER partial close at TP1 when volume >= 2 * volume_min (never force-close 0.01)
        if ratchet_on and len(runner_layers) == 1 and not early_layers and not bank_all_tp1 and not bank_single_tp1 and max_stage >= 2:
            solo_lk = runner_layers[0]
            solo_info = layers.get(solo_lk) if isinstance(layers.get(solo_lk), dict) else None
            solo_tk = _int_num(runner_tickets[0], 0) if runner_tickets else 0
            if (
                solo_info is not None
                and str(roles.get(solo_lk) or solo_info.get("role") or "").strip().upper() == "SOLO_RUNNER"
                and not solo_info.get("solo_partial_done")
                and solo_tk > 0
            ):
                vol_min, _, vol_step, dec = _extract_symbol_volume_limits(sym_info)
                cur_vol = _num(solo_info.get("remaining_lot") or solo_info.get("lot"), 0.0)
                if cur_vol <= 0.0:
                    if self.dry_run:
                        cur_vol = _num(self._dry_run_ticket_lots.get(solo_tk), 0.0)
                    else:
                        with self._mt5_lock:
                            s_pos = mt5.positions_get(ticket=solo_tk)
                        if s_pos:
                            cur_vol = _num(getattr(s_pos[0], "volume", 0.0), 0.0)
                if cur_vol + 1e-9 >= 2.0 * vol_min:
                    partial_vol = max(vol_min, round(cur_vol / 2.0, dec))
                    p_res = self.close_partial_volume(
                        solo_tk,
                        partial_vol,
                        current_volume=cur_vol,
                        symbol=symbol,
                        direction=direction,
                    )
                    if p_res.get("ok"):
                        solo_info["solo_partial_done"] = True
                        solo_info["remaining_lot"] = p_res.get("remaining_volume")
                        signal_data["last_partial_bank"] = {
                            "layer": solo_lk,
                            "ticket": solo_tk,
                            "role": "SOLO_RUNNER",
                            "closed_volume": p_res.get("closed_volume"),
                            "remaining_volume": p_res.get("remaining_volume"),
                            "milestone": "TP1",
                        }

        # 3. Close early_layers at TP1 (FM-10: verify close success or position absence before marking CLOSED)
        banked_tickets: List[int] = []
        for lk in early_layers:
            l_info = layers.get(lk)
            if isinstance(l_info, dict):
                lt = _int_num(l_info.get("ticket"), 0)
                if lt > 0 and lt in market_tickets and lt not in banked_tickets:
                    closed_ok = bool(self.close_market_position(lt))
                    if not closed_ok and not self.dry_run:
                        with self._mt5_lock:
                            still_open = bool(mt5.positions_get(ticket=lt))
                        closed_ok = not still_open
                    if closed_ok:
                        banked_tickets.append(lt)
                        l_info["status"] = "CLOSED"
                        l_info.setdefault("exit_reason_code", "TP1_BANK")
                    else:
                        logger.warning(f"TP1 early close incomplete for {lk} #{lt}; keeping open in market_tickets.")
                elif str(l_info.get("status") or "").strip().upper() == "CLOSED" or lt <= 0:
                    l_info["status"] = "CLOSED"
        for et in early_tickets:
            if et > 0 and et not in banked_tickets:
                closed_ok = bool(self.close_market_position(et))
                if not closed_ok and not self.dry_run:
                    with self._mt5_lock:
                        still_open = bool(mt5.positions_get(ticket=et))
                    closed_ok = not still_open
                if closed_ok:
                    banked_tickets.append(et)

        if ratchet_on and (banked_tickets or runner_tickets or stage2_bankers):
            signal_data["roles_locked"] = True

        surviving_tickets = [mt for mt in market_tickets if mt and mt not in banked_tickets]
        runner_ticket: Optional[int] = runner_tickets[-1] if runner_tickets else None
        runner_layer: Optional[str] = runner_layers[-1] if runner_layers else None

        # 4. Fetch live price before setting floor so proximity/early TP1 scaling never arms a premature virtual floor
        tick = self.get_tick(symbol)
        live_price: Optional[float] = None
        chart_price: Optional[float] = None
        if tick is not None:
            cand_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
            if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                live_price = float(cand_px)
            cand_chart = getattr(tick, "bid", None)
            if isinstance(cand_chart, (int, float)) and not isinstance(cand_chart, bool) and cand_chart > 0:
                chart_price = float(cand_chart)
        eval_chart_px = chart_price if chart_price is not None else live_price

        clean_break_dist = float(_cfg("RUNNER_CLEAN_BREAK_PIPS", RUNNER_CLEAN_BREAK_PIPS)) * pip_size
        already_clean_tp1 = False
        if raw_tp1 is not None and eval_chart_px is not None:
            if is_buy:
                already_clean_tp1 = eval_chart_px >= round(float(raw_tp1) + clean_break_dist, digits)
            else:
                already_clean_tp1 = eval_chart_px <= round(float(raw_tp1) - clean_break_dist, digits)

        staged_exit_on = bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))
        if staged_exit_on:
            milestone_idx = 2 if max_stage >= 2 else 1
            cleared = ["TP1"]
        elif already_clean_tp1 and max_stage >= 2:
            milestone_idx = 2
            cleared = ["TP1"]
        else:
            milestone_idx = 1
            cleared = []

        # 5. Lock runner SL floor & step all runner_tickets' broker TP to TP2
        be_after_tp1 = bool(_cfg("BE_AFTER_TP1", BE_AFTER_TP1))
        offset_value = float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS)) * pip_size
        tp1_floor_price = floor_price
        next_stage_idx = min(max_stage, max(2, milestone_idx)) if max_stage >= 2 else 1
        next_stage_key = f"TP{next_stage_idx}"
        next_stage_tp = tp_levels.get(next_stage_key)
        tp1_trail_on = bool(_cfg("TP1_TRAIL_ENABLED", TP1_TRAIL_ENABLED))
        tp1_touch_enabled = bool(_cfg("TP1_TOUCH_BANK_ENABLED", TP1_TOUCH_BANK_ENABLED))
        touched_now = bool(
            raw_tp1 is not None
            and eval_chart_px is not None
            and ((eval_chart_px >= float(raw_tp1)) if is_buy else (eval_chart_px <= float(raw_tp1)))
        )
        tp1_touched = bool(
            raw_tp1 is not None
            and (signal_data.get("tp1_touched") or touched_now)
            and (tp1_trail_on or tp1_touch_enabled)
        )
        trail_step_dollars = max(0.0, float(_cfg("TP1_TRAIL_STEP_DOLLARS", TP1_TRAIL_STEP_DOLLARS)))

        if staged_exit_on:
            sl_applied_tickets: Set[int] = set()
            signal_data["runner_layers"] = list(runner_layers)
            signal_data["runner_tickets"] = list(runner_tickets)
            signal_data["runner_layer"] = runner_layer
            signal_data["runner_ticket"] = runner_ticket
            if runner_layers:
                signal_data["last_runner_layers"] = list(runner_layers)
                signal_data["last_runner_layer"] = runner_layer
            signal_data["runner_milestone_idx"] = milestone_idx
            signal_data["runner_milestone_name"] = f"TP{milestone_idx}"
            signal_data["runner_cleared_milestones"] = list(cleared)
            signal_data["tp1_reached"] = True
            signal_data["reversal_guard_armed"] = False

            if runner_tickets:
                primary_info = layers.get(runner_layer) if (runner_layer and isinstance(layers.get(runner_layer), dict)) else {}
                r_open: Optional[float] = None
                if not self.dry_run and runner_ticket:
                    with self._mt5_lock:
                        r_pos = mt5.positions_get(ticket=runner_ticket)
                    if r_pos:
                        val = _num(getattr(r_pos[0], "price_open", 0.0), 0.0)
                        if val > 0:
                            r_open = val
                if (
                    r_open is None
                    and runner_layer in ("L1", "TOP")
                    and isinstance(primary_info, dict)
                    and str(primary_info.get("order_type") or "").strip().upper() == "MARKET"
                    and signal_data.get("recorded_entry_price")
                ):
                    r_open = float(signal_data["recorded_entry_price"])
                if r_open is None and isinstance(primary_info, dict) and primary_info.get("target_price"):
                    r_open = float(primary_info["target_price"])
                if r_open is None and signal_data.get("recorded_entry_price"):
                    r_open = float(signal_data["recorded_entry_price"])
                if r_open is None:
                    r_open = self._resolve_signal_primary_entry(signal_data)

                stage = self.get_runner_fsm_stage(signal_data)
                fsm_floor = self._compute_staged_fsm_floor(
                    signal_data,
                    stage,
                    tp_levels,
                    direction,
                    pip_size,
                    digits,
                    runner_entry=r_open,
                )

                if live_price is not None:
                    seed_px = live_price
                elif raw_tp1 is not None:
                    seed_px = float(raw_tp1)
                    if not is_buy and sym_info is not None:
                        spr_pts = _num(getattr(sym_info, "spread", 0.0), 0.0)
                        pt_sz = _num(getattr(sym_info, "point", 0.01), 0.01) or 0.01
                        if spr_pts > 0.0:
                            seed_px = round(seed_px + spr_pts * pt_sz, digits)
                else:
                    seed_px = float(r_open) if r_open is not None else 0.0

                chart_seed_px = (
                    eval_chart_px
                    if eval_chart_px is not None
                    else (float(raw_tp1) if raw_tp1 is not None else seed_px)
                )
                prev_cp = signal_data.get("chart_peak")
                if isinstance(prev_cp, numbers.Real) and not isinstance(prev_cp, bool) and float(prev_cp) > 0.0 and chart_seed_px > 0.0:
                    chart_peak = round(
                        max(float(prev_cp), float(chart_seed_px)) if is_buy else min(float(prev_cp), float(chart_seed_px)),
                        digits,
                    )
                else:
                    chart_peak = round(float(chart_seed_px), digits)

                prev_tp = signal_data.get("trail_peak")
                if isinstance(prev_tp, numbers.Real) and not isinstance(prev_tp, bool) and float(prev_tp) > 0.0 and seed_px > 0.0:
                    trail_peak = round(
                        max(float(prev_tp), float(seed_px)) if is_buy else min(float(prev_tp), float(seed_px)),
                        digits,
                    )
                else:
                    trail_peak = round(float(seed_px), digits)

                trail_dist, tier_used = self._compute_staged_trail_dist(symbol, stage, digits)
                atr_stop = round((trail_peak - trail_dist) if is_buy else (trail_peak + trail_dist), digits)
                s1_atr_armed = self._eval_stage1_atr_armed(
                    signal_data,
                    stage,
                    tp_levels,
                    direction,
                    chart_peak,
                    atr_stop,
                )

                stop_cands: List[float] = []
                if s1_atr_armed or fsm_floor is None:
                    stop_cands.append(atr_stop)
                if fsm_floor is not None:
                    stop_cands.append(fsm_floor)
                prev_ts = signal_data.get("trail_stop")
                if isinstance(prev_ts, numbers.Real) and not isinstance(prev_ts, bool) and float(prev_ts) > 0.0:
                    stop_cands.append(round(float(prev_ts), digits))
                if ratchet_on:
                    s0_sl = signal_data.get("stage0_profit_lock_sl")
                    if isinstance(s0_sl, numbers.Real) and not isinstance(s0_sl, bool) and float(s0_sl) > 0.0:
                        stop_cands.append(round(float(s0_sl), digits))
                    for lk_r in runner_layers:
                        lv_r = layers.get(lk_r)
                        if isinstance(lv_r, dict):
                            for k_r in ("ratchet_sl", "broker_sl", "sl"):
                                r_v = lv_r.get(k_r)
                                if isinstance(r_v, numbers.Real) and not isinstance(r_v, bool) and float(r_v) > 0.0:
                                    r_ent = self._resolve_layer_entry(signal_data, lk_r, lv_r) or r_open
                                    if r_ent and ((is_buy and float(r_v) >= float(r_ent) - 1e-6) or (not is_buy and float(r_v) <= float(r_ent) + 1e-6)):
                                        stop_cands.append(round(float(r_v), digits))
                    if r_open is not None and r_open > 0.0 and trail_peak > 0.0:
                        pk_mfe = round(max(0.0, (trail_peak - r_open) if is_buy else (r_open - trail_peak)), 6)
                        tp1_leg_s = (float(raw_tp1) - r_open) if (raw_tp1 is not None and is_buy) else ((r_open - float(raw_tp1)) if raw_tp1 is not None else None)
                        p_role = (layers.get(runner_layer) or {}).get("role") or ("SOLO_RUNNER" if len(surviving_tickets) == 1 else "CORE_RUNNER")
                        r_calc = self.compute_ticket_ratchet_levels(
                            direction=direction,
                            entry_price=r_open,
                            mfe_exec=pk_mfe,
                            tp1_leg=tp1_leg_s,
                            prog_to_tp1=1.0,
                            stage=stage,
                            leg_prog=0.0,
                            leg_span=None,
                            atr_val=self.get_m1_atr(symbol),
                            role=str(p_role),
                            is_solo=(len(surviving_tickets) == 1),
                            lift_active=True,
                            digits=digits,
                        )
                        if r_calc.get("broker_sl") is not None:
                            stop_cands.append(round(float(r_calc["broker_sl"]), digits))
                        if r_calc.get("tier") and isinstance(layers.get(runner_layer), dict):
                            layers[runner_layer]["ratchet_tier"] = r_calc["tier"]
                trail_stop = round(max(stop_cands) if is_buy else min(stop_cands), digits)

                active_stop_source = (
                    "FSM_FLOOR"
                    if (
                        fsm_floor is not None
                        and (
                            (is_buy and fsm_floor >= trail_stop - 1e-9)
                            or (not is_buy and fsm_floor <= trail_stop + 1e-9)
                        )
                    )
                    else "ATR_TRAIL"
                )

                signal_data["tp1_touched"] = True
                signal_data["fsm_floor"] = fsm_floor
                signal_data["chart_peak"] = chart_peak
                signal_data["trail_peak"] = trail_peak
                signal_data["runner_peak_price"] = trail_peak
                signal_data["trail_dist"] = trail_dist
                signal_data["trail_tier_used"] = tier_used
                signal_data["atr_stop"] = atr_stop
                signal_data["trail_stop"] = trail_stop
                signal_data["active_stop_source"] = active_stop_source
                floor_price = trail_stop
                new_peak_px: Optional[float] = trail_peak

                if r_open is not None and r_open > 0 and trail_peak > 0:
                    pk_prof = round(max(0.0, (trail_peak - r_open) if is_buy else (r_open - trail_peak)), 6)
                    prev_pk_prof = _num(signal_data.get("peak_profit", 0.0), 0.0)
                    signal_data["peak_profit"] = round(max(pk_prof, prev_pk_prof), 6)
                    signal_data["reversal_guard_peak_profit"] = signal_data["peak_profit"]
                    signal_data["reversal_guard_runner_entry"] = r_open

                target_tp_val = round(float(next_stage_tp), digits) if next_stage_tp is not None else None
                clamped_trail_sl = self._clamp_sl_for_broker_stops(trail_stop, direction, tick, sym_info, digits)
                clamped_fb_sl = (
                    self._clamp_sl_for_broker_stops(fsm_floor, direction, tick, sym_info, digits)
                    if fsm_floor is not None
                    else None
                )
                any_tp_ready = bool(target_tp_val is None)
                if ratchet_on:
                    # First ensure N=4 STAGE2_BANKER is green-protected & lifted to TP2 via 2-step SL-first lift
                    tp2_raw = tp_levels.get("TP2")
                    tp2_target = round(float(tp2_raw), digits) if tp2_raw is not None else None
                    for s2_lk, s2_tk in stage2_bankers:
                        s2_info = layers.get(s2_lk) if isinstance(layers.get(s2_lk), dict) else None
                        s2_entry = self._resolve_layer_entry(signal_data, s2_lk, s2_info) or r_open or 0.0
                        s2_exec = live_price if live_price is not None else (float(raw_tp1) if raw_tp1 is not None else s2_entry)
                        s2_mfe = round(max(_num((s2_info or {}).get("mfe_dollars"), 0.0), (s2_exec - s2_entry) if is_buy else (s2_entry - s2_exec), 0.0), 6)
                        if s2_info is not None:
                            s2_info["mfe_dollars"] = s2_mfe
                            if live_price is not None:
                                prev_pk2 = s2_info.get("mfe_peak_price")
                                if prev_pk2 is None or (is_buy and live_price > float(prev_pk2)) or (not is_buy and live_price < float(prev_pk2)):
                                    s2_info["mfe_peak_price"] = round(live_price, digits)
                        s2_rat = self.compute_ticket_ratchet_levels(
                            direction=direction,
                            entry_price=s2_entry,
                            mfe_exec=s2_mfe,
                            tp1_leg=None,
                            prog_to_tp1=1.0,
                            stage=stage,
                            leg_prog=0.0,
                            leg_span=None,
                            atr_val=self.get_m1_atr(symbol),
                            role="STAGE2_BANKER",
                            is_solo=False,
                            lift_active=True,
                            digits=digits,
                        )
                        t1a_lock = float(_cfg("RATCHET_TIER1A_LOCK_DOLLARS", RATCHET_TIER1A_LOCK_DOLLARS))
                        min_s2_sl = round((s2_entry + t1a_lock) if is_buy else (s2_entry - t1a_lock), digits) if s2_entry > 0 else clamped_trail_sl
                        s2_sl = s2_rat.get("broker_sl") or min_s2_sl
                        if s2_rat.get("tier") and s2_info is not None:
                            s2_info["ratchet_tier"] = s2_rat["tier"]
                        if tp2_target is not None:
                            s2_lift = self._lift_ticket_tp_two_step(
                                signal_data=signal_data,
                                layer_key=s2_lk,
                                ticket=s2_tk,
                                next_tp=tp2_target,
                                next_tp_stage="TP2",
                                desired_broker_sl=float(s2_sl),
                                entry_price=s2_entry,
                                mfe_exec=None,
                                tick=tick,
                                sym_info=sym_info,
                                digits=digits,
                            )
                            if s2_lift.get("lifted"):
                                sl_applied_tickets.add(s2_tk)
                            else:
                                s2_sync = self._sync_ticket_ratchet_sl(
                                    signal_data=signal_data,
                                    layer_key=s2_lk,
                                    ticket=s2_tk,
                                    desired_broker_sl=float(s2_sl),
                                    force_sync=True,
                                    tick=tick,
                                    sym_info=sym_info,
                                    digits=digits,
                                )
                                if s2_sync.get("synced"):
                                    sl_applied_tickets.add(s2_tk)
                        else:
                            s2_sync = self._sync_ticket_ratchet_sl(
                                signal_data=signal_data,
                                layer_key=s2_lk,
                                ticket=s2_tk,
                                desired_broker_sl=float(s2_sl),
                                force_sync=True,
                                tick=tick,
                                sym_info=sym_info,
                                digits=digits,
                            )
                            if s2_sync.get("synced"):
                                sl_applied_tickets.add(s2_tk)

                    # Strict 2-Step SL-First TP Lift per runner ticket
                    for idx_r, rt in enumerate(runner_tickets):
                        lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                        l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                        l_entry = self._resolve_layer_entry(signal_data, lk, l_info) or r_open or 0.0
                        if target_tp_val is not None:
                            lift_res = self._lift_ticket_tp_two_step(
                                signal_data=signal_data,
                                layer_key=lk,
                                ticket=rt,
                                next_tp=target_tp_val,
                                next_tp_stage=next_stage_key,
                                desired_broker_sl=float(clamped_trail_sl),
                                entry_price=l_entry,
                                mfe_exec=None,
                                tick=tick,
                                sym_info=sym_info,
                                digits=digits,
                            )
                            if lift_res.get("lifted"):
                                sl_applied_tickets.add(rt)
                                any_tp_ready = True
                                if lift_res.get("sl") is not None:
                                    signal_data["broker_sl"] = lift_res["sl"]
                            else:
                                if lift_res.get("already_at_tp"):
                                    any_tp_ready = True
                                sl_only = self._sync_ticket_ratchet_sl(
                                    signal_data=signal_data,
                                    layer_key=lk,
                                    ticket=rt,
                                    desired_broker_sl=float(clamped_trail_sl),
                                    force_sync=True,
                                    tick=tick,
                                    sym_info=sym_info,
                                    digits=digits,
                                    floor_lock_sl=clamped_fb_sl,
                                )
                                if sl_only.get("synced"):
                                    sl_applied_tickets.add(rt)
                        else:
                            sl_only = self._sync_ticket_ratchet_sl(
                                signal_data=signal_data,
                                layer_key=lk,
                                ticket=rt,
                                desired_broker_sl=float(clamped_trail_sl),
                                force_sync=True,
                                tick=tick,
                                sym_info=sym_info,
                                digits=digits,
                                floor_lock_sl=clamped_fb_sl,
                            )
                            if sl_only.get("synced"):
                                sl_applied_tickets.add(rt)
                        if l_info is not None:
                            l_info["status"] = "RUNNER"
                else:
                    any_tp_ready = True
                    for idx_r, rt in enumerate(runner_tickets):
                        lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                        l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                        if self._sync_one_ticket_tp1_trail_sl(
                            signal_data=signal_data,
                            ticket=rt,
                            l_info=l_info,
                            is_runner=True,
                            is_buy=is_buy,
                            desired_broker_sl=clamped_trail_sl,
                            step_dollars=trail_step_dollars,
                            extra_tp=target_tp_val,
                            extra_tp_stage=next_stage_key if target_tp_val is not None else None,
                            force_sl_sync=True,
                            fallback_sl=clamped_fb_sl,
                            tick=tick,
                            sym_info=sym_info,
                            digits=digits,
                        ):
                            sl_applied_tickets.add(rt)
                        if l_info is not None:
                            l_info["status"] = "RUNNER"
            else:
                r_open = None
                any_tp_ready = True
                prev_peak_px = signal_data.get("runner_peak_price")
                cand_peak = live_price if live_price is not None else raw_tp1
                if prev_peak_px is not None and cand_peak is not None:
                    new_peak_px = max(float(prev_peak_px), float(cand_peak)) if is_buy else min(float(prev_peak_px), float(cand_peak))
                else:
                    new_peak_px = cand_peak if cand_peak is not None else prev_peak_px
                floor_price = None

            signal_data["last_banked_tickets"] = banked_tickets
            signal_data["tp_stage"] = (next_stage_key if (runner_tickets and any_tp_ready) else "TP1")
            signal_data["runner_max_tp_stage"] = configured_max_stage
            signal_data["effective_max_stage"] = max_stage
            signal_data["soft_tp1_floor"] = floor_price
            signal_data["runner_sl_floor"] = floor_price
            signal_data["runner_proximity_armed"] = False
            signal_data["runner_reversal_ticks"] = 0
            signal_data["shortfall_reversal_ticks"] = 0
            signal_data["tp2_shortfall_armed"] = False
            signal_data["runner_peak_price"] = new_peak_px
            signal_data["market_tickets"] = surviving_tickets

            if (not ratchet_on) and runner_tickets and floor_price is not None:
                self._sync_tp1_trail_broker_sl(
                    signal_data,
                    symbol,
                    direction,
                    float(floor_price),
                    digits,
                    tick=tick,
                    sym_info=sym_info,
                    skip_tickets=sorted(sl_applied_tickets),
                    force_sl_sync=True,
                    fallback_sl=signal_data.get("fsm_floor"),
                )
        else:
            if runner_tickets:
                primary_info = layers.get(runner_layer) if (runner_layer and isinstance(layers.get(runner_layer), dict)) else {}
                r_open = None
                if not self.dry_run and runner_ticket:
                    with self._mt5_lock:
                        r_pos = mt5.positions_get(ticket=runner_ticket)
                    if r_pos:
                        val = _num(getattr(r_pos[0], "price_open", 0.0), 0.0)
                        if val > 0:
                            r_open = val
                if r_open is None and isinstance(primary_info, dict) and primary_info.get("target_price"):
                    r_open = float(primary_info["target_price"])
                if r_open is None and signal_data.get("recorded_entry_price"):
                    r_open = float(signal_data["recorded_entry_price"])

                runner_be_sl: Optional[float] = None
                if r_open is not None and r_open > 0:
                    runner_be_sl = round(r_open + offset_value, digits) if is_buy else round(r_open - offset_value, digits)

                gold_guard_on = bool(_cfg("GOLD_REVERSAL_GUARD_ENABLED", GOLD_REVERSAL_GUARD_ENABLED))
                past_tp1_floor = True
                if live_price is not None:
                    past_tp1_floor = (live_price >= tp1_floor_price) if is_buy else (live_price <= tp1_floor_price)

                sl_applied_tickets = set()
                target_tp_val = round(float(next_stage_tp), digits) if next_stage_tp is not None else None
                eff_touch_floor: Optional[float] = None

                if tp1_trail_on and tp1_touched:
                    # v3.4.2: Remove TP1 - $0.50 clean-break choke; initialize dynamic ATR trailing stop instead
                    use_tp1_lock = False
                    touch_px = live_price if live_price is not None else (signal_data.get("tp1_touch_price") or float(raw_tp1))
                    _, _, trail_stop_val = self._update_tp1_trail_state(
                        signal_data,
                        symbol,
                        direction,
                        float(touch_px),
                        pip_size,
                        digits,
                        runner_entry_fallback=r_open,
                    )
                    floor_price = trail_stop_val
                    clamped_trail_sl = self._clamp_sl_for_broker_stops(floor_price, direction, tick, sym_info, digits)
                    for idx_r, rt in enumerate(runner_tickets):
                        lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                        l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                        if self._sync_one_ticket_tp1_trail_sl(
                            signal_data=signal_data,
                            ticket=rt,
                            l_info=l_info,
                            is_runner=True,
                            is_buy=is_buy,
                            desired_broker_sl=clamped_trail_sl,
                            step_dollars=trail_step_dollars,
                            extra_tp=target_tp_val,
                            extra_tp_stage=next_stage_key if target_tp_val is not None else None,
                        ):
                            sl_applied_tickets.add(rt)
                        if l_info is not None:
                            l_info["status"] = "RUNNER"
                else:
                    if tp1_touched and tp1_touch_enabled and not tp1_trail_on:
                        eff_touch_floor = self._compute_tp1_touch_floor(float(raw_tp1), direction, pip_size, digits)
                        signal_data["tp1_touched"] = True
                        signal_data["tp1_touch_floor"] = eff_touch_floor

                    # Legacy non-trail mode: on clean break (+5p) lock TP1-0.50; on TP1 touch fallback lock TP1-$1.20
                    use_tp1_lock = (not tp1_trail_on) and past_tp1_floor and (already_clean_tp1 or not gold_guard_on)

                    if use_tp1_lock:
                        cands = [tp1_floor_price]
                        if eff_touch_floor is not None:
                            cands.append(eff_touch_floor)
                        if be_after_tp1 and runner_be_sl is not None:
                            cands.append(runner_be_sl)
                        floor_price = round(max(cands) if is_buy else min(cands), digits)
                    elif eff_touch_floor is not None:
                        cands = [eff_touch_floor]
                        if be_after_tp1 and runner_be_sl is not None:
                            cands.append(runner_be_sl)
                        floor_price = round(max(cands) if is_buy else min(cands), digits)
                    else:
                        floor_price = runner_be_sl if (be_after_tp1 and runner_be_sl is not None) else None

                    for idx_r, rt in enumerate(runner_tickets):
                        lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                        l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                        t_be_sl = runner_be_sl
                        if l_info and l_info.get("target_price"):
                            t_open = float(l_info["target_price"])
                            t_be_sl = round(t_open + offset_value, digits) if is_buy else round(t_open - offset_value, digits)

                        if use_tp1_lock or eff_touch_floor is not None:
                            sl_cands = [floor_price] if floor_price is not None else []
                            if be_after_tp1 and t_be_sl is not None:
                                sl_cands.append(t_be_sl)
                            desired_sl = round(max(sl_cands) if is_buy else min(sl_cands), digits) if sl_cands else None
                        else:
                            desired_sl = t_be_sl if be_after_tp1 else None

                        t_sl = (
                            self._clamp_sl_for_broker_stops(desired_sl, direction, tick, sym_info, digits)
                            if desired_sl is not None
                            else None
                        )
                        if t_sl is not None:
                            if self.modify_position_sltp(rt, sl=t_sl, tp=target_tp_val):
                                sl_applied_tickets.add(rt)
                                signal_data["broker_sl"] = t_sl
                                if l_info is not None:
                                    l_info["sl"] = t_sl
                            elif be_after_tp1 and t_be_sl is not None and t_be_sl != t_sl:
                                if self.modify_position_sltp(rt, sl=t_be_sl, tp=target_tp_val):
                                    sl_applied_tickets.add(rt)
                                    if l_info is not None:
                                        l_info["sl"] = t_be_sl
                                elif target_tp_val is not None:
                                    self.modify_position_sltp(rt, sl=None, tp=target_tp_val)
                            elif target_tp_val is not None:
                                self.modify_position_sltp(rt, sl=None, tp=target_tp_val)
                        elif target_tp_val is not None:
                            self.modify_position_sltp(rt, sl=None, tp=target_tp_val)

                        if l_info is not None:
                            l_info["status"] = "RUNNER"
                            if target_tp_val is not None:
                                l_info["tp"] = target_tp_val
                                l_info["tp_stage"] = next_stage_key
            else:
                r_open = None
                sl_applied_tickets = set()
                if tp1_trail_on and tp1_touched and raw_tp1 is not None:
                    touch_px = live_price if live_price is not None else (signal_data.get("tp1_touch_price") or float(raw_tp1))
                    _, _, trail_stop_val = self._update_tp1_trail_state(
                        signal_data,
                        symbol,
                        direction,
                        float(touch_px),
                        pip_size,
                        digits,
                    )
                    floor_price = trail_stop_val if surviving_tickets else None
                else:
                    eff_touch_floor = (
                        self._compute_tp1_touch_floor(float(raw_tp1), direction, pip_size, digits)
                        if (tp1_touched and tp1_touch_enabled and not tp1_trail_on)
                        else None
                    )
                    floor_price = eff_touch_floor if surviving_tickets else None

            # Scaling Continuity: carry forward peak_price & peak_profit and keep reversal_guard_armed = True
            prev_peak_px = signal_data.get("runner_peak_price")
            cand_peak = live_price if live_price is not None else raw_tp1
            if prev_peak_px is not None and cand_peak is not None:
                new_peak_px = max(float(prev_peak_px), float(cand_peak)) if is_buy else min(float(prev_peak_px), float(cand_peak))
            else:
                new_peak_px = cand_peak if cand_peak is not None else prev_peak_px

            if runner_tickets and bool(_cfg("GOLD_REVERSAL_GUARD_ENABLED", GOLD_REVERSAL_GUARD_ENABLED)):
                signal_data["reversal_guard_armed"] = True
                if r_open is not None and r_open > 0 and new_peak_px is not None:
                    pk_prof = round((float(new_peak_px) - r_open) if is_buy else (r_open - float(new_peak_px)), 6)
                    prev_pk_prof = _num(signal_data.get("peak_profit", 0.0), 0.0)
                    if signal_data.get("reversal_guard_runner_entry") not in (None, r_open):
                        prev_pk_prof = pk_prof
                    best_prof = max(0.0, pk_prof, prev_pk_prof)
                    signal_data["peak_profit"] = round(best_prof, 6)
                    signal_data["reversal_guard_peak_profit"] = round(best_prof, 6)
                    signal_data["reversal_guard_runner_entry"] = r_open

            signal_data["last_banked_tickets"] = banked_tickets
            signal_data["runner_layers"] = list(runner_layers)
            signal_data["runner_tickets"] = list(runner_tickets)
            signal_data["runner_layer"] = runner_layer
            signal_data["runner_ticket"] = runner_ticket
            if runner_layers:
                signal_data["last_runner_layers"] = list(runner_layers)
                signal_data["last_runner_layer"] = runner_layer
            signal_data["tp_stage"] = next_stage_key if runner_tickets else "TP1"
            signal_data["runner_max_tp_stage"] = configured_max_stage
            signal_data["effective_max_stage"] = max_stage
            signal_data["soft_tp1_floor"] = floor_price
            signal_data["runner_sl_floor"] = floor_price
            signal_data["runner_milestone_idx"] = milestone_idx
            signal_data["runner_milestone_name"] = f"TP{milestone_idx}"
            signal_data["runner_cleared_milestones"] = cleared
            signal_data["runner_proximity_armed"] = False
            signal_data["runner_reversal_ticks"] = 0
            signal_data["shortfall_reversal_ticks"] = 0
            signal_data["tp2_shortfall_armed"] = False
            signal_data["runner_peak_price"] = new_peak_px
            signal_data["tp1_reached"] = True
            signal_data["market_tickets"] = surviving_tickets

            if tp1_touched and raw_tp1 is not None:
                if tp1_trail_on and floor_price is not None:
                    self._sync_tp1_trail_broker_sl(
                        signal_data,
                        symbol,
                        direction,
                        float(floor_price),
                        digits,
                        tick=tick,
                        sym_info=sym_info,
                        skip_tickets=sorted(sl_applied_tickets),
                    )
                elif tp1_touch_enabled and not tp1_trail_on:
                    self._apply_tp1_touch_protection(
                        signal_data,
                        symbol,
                        direction,
                        float(raw_tp1),
                        pip_size,
                        digits,
                        tick=tick,
                        sym_info=sym_info,
                        skip_tickets=sorted(sl_applied_tickets),
                        preferred_floor=floor_price,
                    )

        if runner_tickets:
            signal_data["status"] = "RUNNER_ACTIVE"
        elif surviving_tickets:
            signal_data["status"] = "BE_APPLIED" if be_after_tp1 else "ACTIVE"
        elif signal_data.get("pending_tickets"):
            signal_data["status"] = "ACTIVE"
        else:
            signal_data["status"] = "CLOSED"

        return runner_ticket

    def _compute_proximity_and_pullback(
        self,
        target: float,
        peak: float,
        pip_size: float,
        anchor: Optional[float] = None,
    ) -> Tuple[float, float]:
        """
        Compute effective (arm_buffer, reversal_pullback) for PIPS, PCT, or BOTH modes,
        clamping arm_buffer against |target - anchor| * RUNNER_MAX_MIDRANGE_RATIO so short
        legs never arm mid-range.
        """
        mode = str(_cfg("RUNNER_PROXIMITY_MODE", RUNNER_PROXIMITY_MODE) or "PIPS").strip().upper()
        buf_pips = float(_cfg("RUNNER_PROXIMITY_BUFFER_PIPS", RUNNER_PROXIMITY_BUFFER_PIPS)) * pip_size
        buf_pct = abs(float(target)) * (float(_cfg("RUNNER_PROXIMITY_BUFFER_PCT", RUNNER_PROXIMITY_BUFFER_PCT)) / 100.0)

        pb_pips = float(_cfg("RUNNER_REVERSAL_PULLBACK_PIPS", RUNNER_REVERSAL_PULLBACK_PIPS)) * pip_size
        pb_pct = abs(float(peak)) * (float(_cfg("RUNNER_REVERSAL_PULLBACK_PCT", RUNNER_REVERSAL_PULLBACK_PCT)) / 100.0)

        if mode == "PCT":
            raw_buf, eff_pb = buf_pct, pb_pct
        elif mode == "BOTH":
            raw_buf, eff_pb = min(buf_pips, buf_pct), max(pb_pips, pb_pct)
        else:
            raw_buf, eff_pb = buf_pips, pb_pips

        if anchor is not None and float(anchor) > 0:
            leg_dist = abs(float(target) - float(anchor))
            if leg_dist > 0:
                max_ratio = float(_cfg("RUNNER_MAX_MIDRANGE_RATIO", RUNNER_MAX_MIDRANGE_RATIO))
                raw_buf = min(raw_buf, leg_dist * max_ratio)

        return raw_buf, eff_pb

    def _step_runner_broker_tp_and_sl(
        self,
        signal_data: Dict[str, Any],
        runner_tickets: Sequence[int],
        runner_layers: Sequence[str],
        new_sl: Optional[float],
        new_tp: Optional[float],
        new_stage: str,
        digits: int,
    ) -> None:
        """Step broker TP (and optionally ratchet SL) across all active runner_tickets."""
        layers = signal_data.get("layers") or {}
        tp_rounded = round(float(new_tp), digits) if new_tp is not None else None
        sl_rounded = round(float(new_sl), digits) if new_sl is not None else None

        for idx_r, rt in enumerate(runner_tickets):
            lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
            l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None

            already_at_tp = (
                l_info is not None
                and tp_rounded is not None
                and abs(_num(l_info.get("tp"), 0.0) - tp_rounded) < 1e-6
                and l_info.get("tp_stage") == new_stage
            )
            if sl_rounded is None and already_at_tp:
                continue

            if not self.modify_position_sltp(rt, sl=sl_rounded, tp=tp_rounded):
                if sl_rounded is not None and tp_rounded is not None and not already_at_tp:
                    self.modify_position_sltp(rt, sl=None, tp=tp_rounded)

            if l_info is not None and tp_rounded is not None:
                l_info["tp"] = tp_rounded
                l_info["tp_stage"] = new_stage

        if tp_rounded is not None:
            signal_data["tp_stage"] = new_stage

    def _close_reached_layer_targets(
        self,
        signal_data: Dict[str, Any],
        tp_levels: Dict[str, float],
        is_buy: bool,
        live_price: float,
        runner_tickets: Sequence[int],
    ) -> List[int]:
        """
        Milestone Auto-Close Backstop for non-runner L2 (TP2) and L3 (TP3) positions
        if present in market_tickets alongside runner_tickets.
        """
        layers = signal_data.get("layers") or {}
        market_tickets = list(signal_data.get("market_tickets") or [])
        runner_set = set(runner_tickets)
        closed_now: List[int] = []

        # v3.6 STAGE2_BANKER layers always bank at TP2; fallback legacy mapping handles L2->TP2, L3->TP3
        targets_to_check: List[Tuple[str, str, Optional[str]]] = []
        handled_layers: Set[str] = set()
        for l_name, l_info in layers.items():
            if not isinstance(l_info, dict):
                continue
            role_u = str(l_info.get("role") or "").strip().upper()
            if role_u == "STAGE2_BANKER":
                targets_to_check.append((l_name, "TP2", "STAGE2_BANK_TP"))
                handled_layers.add(l_name)
        for l_name, tp_key in (("L2", "TP2"), ("L3", "TP3")):
            if l_name in handled_layers:
                continue
            targets_to_check.append((l_name, tp_key, None))

        for l_name, tp_key, exit_code in targets_to_check:
            l_info = layers.get(l_name)
            if not isinstance(l_info, dict):
                continue
            lt = _int_num(l_info.get("ticket"), 0)
            tp_val = tp_levels.get(tp_key)
            if not lt or lt in runner_set or lt not in market_tickets or tp_val is None:
                continue
            hit = (live_price >= float(tp_val)) if is_buy else (live_price <= float(tp_val))
            if hit:
                closed_ok = bool(self.close_market_position(lt))
                if not closed_ok and not self.dry_run:
                    with self._mt5_lock:
                        still_open = bool(mt5.positions_get(ticket=lt))
                    closed_ok = not still_open
                if closed_ok:
                    l_info["status"] = "CLOSED"
                    if exit_code:
                        l_info["exit_reason_code"] = exit_code
                    market_tickets = [t for t in market_tickets if t != lt]
                    closed_now.append(lt)
                    logger.info(f"Backstop auto-closed {l_name} #{lt} at {tp_key}={tp_val} (live={live_price})")
                else:
                    logger.warning(f"Backstop close incomplete for {l_name} #{lt} at {tp_key}={tp_val}; keeping open.")

        signal_data["market_tickets"] = market_tickets
        signal_data["last_backstop_closed_tickets"] = closed_now
        return closed_now

    def _finalize_runner_exit(
        self,
        signal_data: Dict[str, Any],
        runner_tickets: Sequence[int],
        runner_layers: Sequence[str],
        reason: str,
        close_position: bool = True,
        ceiling_stage: Optional[int] = None,
        close_all_market_tickets: bool = False,
        exit_reason_code: Optional[str] = None,
        exit_price: Optional[float] = None,
        exit_stage: Optional[int] = None,
    ) -> bool:
        """
        Atomically close active runner_tickets (and optionally any remaining non-runner open
        market_tickets when banking on TP1-Touch Fallback Bank or v3.5.0 Staged Exit) under
        `is_closing = True` lock.
        """
        if not self.try_begin_signal_close(signal_data):
            logger.warning(f"Atomic exit lock: duplicate _finalize_runner_exit skipped ({reason}).")
            return False

        try:
            r_tickets = [_int_num(t, 0) for t in runner_tickets if _int_num(t, 0) > 0]
            all_mkt = [_int_num(t, 0) for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) > 0]
            if close_all_market_tickets:
                to_close = list(dict.fromkeys(r_tickets + all_mkt))
            else:
                to_close = list(r_tickets)

            already_closed_once = set(signal_data.get("_closed_tickets_once") or [])
            actually_closed: List[int] = []
            if close_position:
                for rt in to_close:
                    if rt in already_closed_once:
                        actually_closed.append(rt)
                        continue
                    closed_ok = bool(self.close_market_position(rt))
                    if not closed_ok and not self.dry_run:
                        with self._mt5_lock:
                            still_open = bool(mt5.positions_get(ticket=rt))
                        closed_ok = not still_open
                    if closed_ok:
                        already_closed_once.add(rt)
                        actually_closed.append(rt)
                signal_data["_closed_tickets_once"] = sorted(already_closed_once)
            else:
                actually_closed = list(to_close)
                self.invalidate_realized_pnl_cache()

            if ceiling_stage is not None:
                c_key = f"TP{ceiling_stage}"
                signal_data[f"tp{ceiling_stage}_reached"] = True
                if ceiling_stage >= 2:
                    signal_data["tp2_reached"] = True
                signal_data["tp_stage"] = c_key
                cleared = list(signal_data.get("runner_cleared_milestones") or [])
                if c_key not in cleared:
                    cleared.append(c_key)
                signal_data["runner_cleared_milestones"] = cleared

            if exit_reason_code is not None or exit_price is not None or exit_stage is not None:
                self._populate_exit_telemetry(
                    signal_data,
                    exit_reason_code=exit_reason_code,
                    exit_price=exit_price,
                    exit_stage=exit_stage,
                )

            close_set = set(actually_closed)
            layers = signal_data.get("layers") or {}
            for lk in runner_layers:
                if lk and isinstance(layers.get(lk), dict):
                    lt_r = _int_num(layers[lk].get("ticket"), 0)
                    if lt_r <= 0 or lt_r in close_set or not close_position:
                        layers[lk]["status"] = "CLOSED"
            if close_all_market_tickets:
                for l_val in layers.values():
                    if isinstance(l_val, dict):
                        lt = _int_num(l_val.get("ticket"), 0)
                        if (lt > 0 and lt in close_set) or (lt <= 0 and str(l_val.get("status") or "").upper() in ("OPEN", "FILLED", "RUNNER")):
                            l_val["status"] = "CLOSED"

            remaining_mkt = [t for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) not in close_set]
            remaining_runners = [t for t in r_tickets if t not in close_set] if close_position else []
            signal_data["market_tickets"] = remaining_mkt
            signal_data["last_runner_tickets"] = list(actually_closed or to_close)
            signal_data["last_runner_ticket"] = (actually_closed or to_close)[-1] if (actually_closed or to_close) else None
            r_layers_list = [str(lk) for lk in runner_layers if lk]
            if r_layers_list:
                signal_data["last_runner_layers"] = list(r_layers_list)
                signal_data["last_runner_layer"] = r_layers_list[-1]
            signal_data["runner_close_reason"] = reason
            if not remaining_runners:
                signal_data["runner_tickets"] = []
                signal_data["runner_ticket"] = None
                signal_data["runner_layers"] = []
                signal_data["runner_layer"] = None
                signal_data["runner_proximity_armed"] = False
                signal_data["runner_reversal_ticks"] = 0
                signal_data["tp2_shortfall_armed"] = False

            if (not remaining_mkt) or (bool(_cfg("CANCEL_PENDING_ON_TP1", CANCEL_PENDING_ON_TP1)) and signal_data.get("tp1_reached")):
                self.cancel_signal_pending_orders(signal_data)
                signal_data["pending_tickets"] = []
                for l_val in layers.values():
                    if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                        l_val["status"] = "CANCELLED"

            has_open_or_pending = bool(remaining_mkt or signal_data.get("pending_tickets"))
            signal_data["status"] = "BE_APPLIED" if has_open_or_pending else "CLOSED"
            return bool(remaining_runners)
        finally:
            self.end_signal_close(signal_data)

    def _manage_4layer_runner(self, signal_data: Dict[str, Any]) -> bool:
        """
        v3.1 Dynamic Stepped Momentum Scaling FSM (TP1 -> TP2 -> TP3 -> TP4, up to RUNNER_MAX_TP_STAGE):
        1. Virtual Floor protection across all active runner_tickets.
        2. Backstop auto-close for any non-runner open L2/L3.
        3. Ceiling exit at TP{RUNNER_MAX_TP_STAGE} (default TP4, or TP5 when configured).
        4. Clean breakthrough progression (TP1..TP{max_stage-1}): steps broker TP upward, disarms guard, ratchets floor.
        5. Strict Proximity Reversal Guard ("Bank It") with midrange clamp, proactive TP{k+1} broker TP lift,
           sustained RUNNER_REVERSAL_CONFIRM_TICKS counter, and optional M1 closed candle confirmation.
        6. Continuous one-way broker SL trailing across all runner_tickets.
        """
        raw_rts = signal_data.get("runner_tickets")
        if isinstance(raw_rts, list) and raw_rts:
            runner_tickets = [_int_num(t, 0) for t in raw_rts if _int_num(t, 0) > 0]
        else:
            rt_single = _int_num(signal_data.get("runner_ticket"), 0)
            runner_tickets = [rt_single] if rt_single > 0 else []
        if not runner_tickets:
            return False

        raw_rls = signal_data.get("runner_layers")
        if isinstance(raw_rls, list) and raw_rls:
            runner_layers = [str(x) for x in raw_rls if x]
        else:
            rl_single = signal_data.get("runner_layer")
            runner_layers = [str(rl_single)] if rl_single else []

        signal_data["runner_tickets"] = list(runner_tickets)
        signal_data["runner_ticket"] = runner_tickets[-1]
        signal_data["runner_layers"] = list(runner_layers)
        signal_data["runner_layer"] = runner_layers[-1] if runner_layers else None
        signal_data["last_runner_tickets"] = list(runner_tickets)
        signal_data["last_runner_ticket"] = runner_tickets[-1]
        if runner_layers:
            signal_data["last_runner_layers"] = list(runner_layers)
            signal_data["last_runner_layer"] = runner_layers[-1]
        signal_data["last_backstop_closed_tickets"] = []
        signal_data["last_closed_sub_runner_tickets"] = []
        signal_data.setdefault("runner_proximity_armed", False)
        signal_data.setdefault("runner_reversal_ticks", 0)

        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        primary_layer = runner_layers[-1] if runner_layers else None
        primary_ticket = runner_tickets[-1]
        layers = signal_data.get("layers") or {}

        if self.dry_run:
            p_symbol = symbol
            sym_info = self.get_symbol_info(p_symbol)
            tick = self.get_tick(p_symbol)
            if not tick:
                return True
            digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
            pos_sl = float(
                signal_data.get("broker_sl")
                or signal_data.get("runner_sl_floor")
                or signal_data.get("soft_tp1_floor")
                or 0.0
            )
            stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
            pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
            min_stop_dist = stops_lvl * (pt or 0.01)
            runner_entry = None
            if primary_layer and isinstance(layers.get(primary_layer), dict):
                runner_entry = layers[primary_layer].get("target_price")
            if runner_entry is None:
                runner_entry = signal_data.get("recorded_entry_price")
        else:
            live_positions_map: Dict[int, Any] = {}
            with self._mt5_lock:
                for rt in runner_tickets:
                    pos_res = mt5.positions_get(ticket=rt)
                    if pos_res:
                        live_positions_map[rt] = pos_res[0]
                        self._cached_pos_profits[_int_num(rt, 0)] = round(
                            _num(getattr(pos_res[0], "profit", 0.0), 0.0)
                            + _num(getattr(pos_res[0], "swap", 0.0), 0.0),
                            2,
                        )

            if not live_positions_map:
                staged_on_close = bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))
                close_info = self.get_position_close_info(runner_tickets[-1])
                raw_rc = str(close_info.get("reason", "UNKNOWN")).strip().upper()
                reason_label_map = {
                    "TP": "Broker Take Profit Hit",
                    "SL": "Trailed Stop Loss Hit (Locked in Gain)",
                    "MANUAL/BOT": "Closed Manually / from MT5",
                }
                code_map = {
                    "TP": "TP_CEILING",
                    "SL": "BROKER_SL",
                    "MANUAL/BOT": "MANUAL",
                }
                ext_reason = reason_label_map.get(raw_rc, "Trailed Stop Loss Hit (Locked in Gain)")
                ext_code = code_map.get(raw_rc, "BROKER_SL")
                bsl = _num(
                    signal_data.get("broker_sl")
                    or signal_data.get("trail_stop")
                    or signal_data.get("runner_sl_floor")
                    or signal_data.get("soft_tp1_floor"),
                    0.0,
                )
                return self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason=ext_reason,
                    close_position=False,
                    exit_reason_code=ext_code if staged_on_close else None,
                    exit_price=bsl if (staged_on_close and ext_code == "BROKER_SL" and bsl > 0.0) else None,
                    exit_stage=self.get_runner_fsm_stage(signal_data) if staged_on_close else None,
                )

            if len(live_positions_map) < len(runner_tickets):
                closed_sub = [rt for rt in runner_tickets if rt not in live_positions_map]
                signal_data["last_closed_sub_runner_tickets"] = list(closed_sub)
                for idx_r, rt in enumerate(list(runner_tickets)):
                    if rt in closed_sub:
                        lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                        if lk and isinstance(layers.get(lk), dict):
                            layers[lk]["status"] = "CLOSED"
                runner_layers = [
                    runner_layers[idx_r]
                    for idx_r, rt in enumerate(runner_tickets)
                    if rt in live_positions_map and idx_r < len(runner_layers)
                ]
                runner_tickets = [rt for rt in runner_tickets if rt in live_positions_map]
                signal_data["market_tickets"] = [
                    t for t in (signal_data.get("market_tickets") or []) if t not in closed_sub
                ]
                signal_data["runner_tickets"] = list(runner_tickets)
                signal_data["runner_ticket"] = runner_tickets[-1]
                signal_data["runner_layers"] = list(runner_layers)
                signal_data["runner_layer"] = runner_layers[-1] if runner_layers else None
                primary_ticket = runner_tickets[-1]
                primary_layer = runner_layers[-1] if runner_layers else None

            pos = live_positions_map[primary_ticket]
            pos_sl = _num(getattr(pos, "sl", 0.0), 0.0)
            runner_entry = _num(getattr(pos, "price_open", 0.0), 0.0) or None
            raw_sym = getattr(pos, "symbol", None)
            p_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else symbol
            sym_info = self.get_symbol_info(p_symbol)
            tick = self.get_tick(p_symbol)
            if not sym_info or not tick:
                return True
            digits = _int_num(getattr(sym_info, "digits", 2), 2)
            stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0)
            pt = _num(getattr(sym_info, "point", 0.01), 0.01) or 0.01
            min_stop_dist = stops_lvl * pt

        raw_live = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        if not isinstance(raw_live, (int, float)) or isinstance(raw_live, bool) or raw_live <= 0:
            return True
        live_price = float(raw_live)
        raw_chart = getattr(tick, "bid", None)
        chart_price = (
            float(raw_chart)
            if isinstance(raw_chart, (int, float)) and not isinstance(raw_chart, bool) and raw_chart > 0
            else live_price
        )

        pip_size = self.get_pip_size(p_symbol)
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        configured_max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    5,
                ),
            ),
        )
        max_stage = self._resolve_effective_max_stage(configured_max_stage, tp_levels)
        signal_data["runner_max_tp_stage"] = configured_max_stage
        signal_data["effective_max_stage"] = max_stage

        if bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED)):
            # ===================================================================
            # v3.5.2 STAGED GOLD RUNNER EXIT ARCHITECTURE (11-Step Tick Order)
            # ===================================================================
            # (1) Read live price + spread spike check; hard-disable Stage 0 Gold Guard
            is_spike, spread_dollars, eff_max_spread = self.check_spread_spike(p_symbol, tick)
            signal_data["spread_spike_paused"] = is_spike
            signal_data["tp1_reached"] = True
            signal_data["tp1_touched"] = True
            signal_data["reversal_guard_armed"] = False

            # (2) Update canonical trail_peak (exec space) and chart_peak (chart bid space)
            prev_peak = signal_data.get("trail_peak")
            if not (isinstance(prev_peak, numbers.Real) and not isinstance(prev_peak, bool) and float(prev_peak) > 0.0):
                prev_peak = signal_data.get("runner_peak_price")
            if isinstance(prev_peak, numbers.Real) and not isinstance(prev_peak, bool) and float(prev_peak) > 0.0:
                trail_peak = round(
                    max(float(prev_peak), live_price) if is_buy else min(float(prev_peak), live_price),
                    digits,
                )
            else:
                trail_peak = round(live_price, digits)
            signal_data["trail_peak"] = trail_peak
            signal_data["runner_peak_price"] = trail_peak

            prev_cp = signal_data.get("chart_peak")
            if isinstance(prev_cp, numbers.Real) and not isinstance(prev_cp, bool) and float(prev_cp) > 0.0:
                chart_peak = round(
                    max(float(prev_cp), chart_price) if is_buy else min(float(prev_cp), chart_price),
                    digits,
                )
            else:
                chart_peak = round(chart_price, digits)
            signal_data["chart_peak"] = chart_peak

            ref_entry = (
                float(runner_entry)
                if (runner_entry is not None and isinstance(runner_entry, numbers.Real) and not isinstance(runner_entry, bool) and float(runner_entry) > 0.0)
                else self._resolve_signal_primary_entry(signal_data)
            )
            if ref_entry is not None and ref_entry > 0.0:
                pk_prof = round(max(0.0, (trail_peak - ref_entry) if is_buy else (ref_entry - trail_peak)), 6)
                prev_pk_prof = _num(signal_data.get("peak_profit", 0.0), 0.0)
                signal_data["peak_profit"] = round(max(pk_prof, prev_pk_prof), 6)
                signal_data["reversal_guard_peak_profit"] = signal_data["peak_profit"]

            # (3) Ceiling check at TP{max_stage} -> close runner + ALL open layers
            ceiling_key = f"TP{max_stage}"
            ceiling_tp = tp_levels.get(ceiling_key)
            if ceiling_tp is not None:
                ceiling_val = float(ceiling_tp)
                if (is_buy and live_price >= ceiling_val) or (not is_buy and live_price <= ceiling_val):
                    exit_stage_val = min(4, max_stage)
                    _, tier_snap = self._compute_staged_trail_dist(p_symbol, exit_stage_val, digits)
                    signal_data["trail_tier_used"] = tier_snap
                    logger.info(
                        f"Runner {runner_tickets}: Reached {ceiling_key} ceiling ({ceiling_val})! "
                        f"Closing runner + all open layers at market."
                    )
                    return self._finalize_runner_exit(
                        signal_data,
                        runner_tickets,
                        runner_layers,
                        reason=f"Target {ceiling_key} Reached ({ceiling_val})",
                        ceiling_stage=max_stage,
                        close_all_market_tickets=True,
                        exit_reason_code="TP_CEILING",
                        exit_price=live_price,
                        exit_stage=exit_stage_val,
                    )

            # (4) Non-runner L2 (TP2) & L3 (TP3) backstop auto-close
            self._close_reached_layer_targets(signal_data, tp_levels, is_buy, chart_price, runner_tickets)

            # (5) Sequential while loop clean-break progression (evaluated in chart_price bid space)
            prev_stage = self.get_runner_fsm_stage(signal_data)
            k = max(1, min(max_stage, _int_num(signal_data.get("runner_milestone_idx", 1), 1)))
            cleared = list(signal_data.get("runner_cleared_milestones") or [])
            clean_break_dist = float(_cfg("RUNNER_CLEAN_BREAK_PIPS", RUNNER_CLEAN_BREAK_PIPS)) * pip_size
            stepped_milestone_tp: Optional[float] = None
            stepped_milestone_stage: Optional[str] = None
            milestone_advanced = False

            while k < max_stage:
                target = tp_levels.get(f"TP{k}")
                if target is None:
                    break
                target_f = float(target)
                clean_level = round(target_f + clean_break_dist, digits) if is_buy else round(target_f - clean_break_dist, digits)
                broke_clean = (chart_price >= clean_level) if is_buy else (chart_price <= clean_level)
                if not broke_clean:
                    break

                m_name = f"TP{k}"
                if m_name not in cleared:
                    cleared.append(m_name)
                if k >= 2:
                    signal_data[f"tp{k}_reached"] = True

                signal_data["runner_proximity_armed"] = False
                signal_data["runner_reversal_ticks"] = 0
                signal_data["tp2_shortfall_armed"] = False

                k += 1
                milestone_advanced = True
                step_stage = f"TP{k}"
                step_tp = tp_levels.get(step_stage)
                stepped_milestone_tp = float(step_tp) if step_tp is not None else None
                stepped_milestone_stage = step_stage
                signal_data["tp_stage"] = step_stage
                signal_data["runner_milestone_idx"] = k
                signal_data["runner_milestone_name"] = step_stage
                signal_data["runner_cleared_milestones"] = list(cleared)

                iter_stage = self.get_runner_fsm_stage(signal_data)
                iter_floor = self._compute_staged_fsm_floor(
                    signal_data,
                    iter_stage,
                    tp_levels,
                    direction,
                    pip_size,
                    digits,
                    runner_entry=runner_entry,
                )
                if iter_floor is not None:
                    signal_data["fsm_floor"] = iter_floor

                logger.info(
                    f"Runner {runner_tickets}: Clean break past {m_name} ({target_f}); "
                    f"stepped to {step_stage}={step_tp}, stage={iter_stage}, fsm_floor={signal_data.get('fsm_floor')}"
                )

            signal_data["runner_milestone_idx"] = k
            signal_data["runner_milestone_name"] = f"TP{k}"
            signal_data["runner_cleared_milestones"] = cleared

            stage = self.get_runner_fsm_stage(signal_data)
            fsm_floor = self._compute_staged_fsm_floor(
                signal_data,
                stage,
                tp_levels,
                direction,
                pip_size,
                digits,
                runner_entry=runner_entry,
            )
            if fsm_floor is not None:
                signal_data["fsm_floor"] = fsm_floor
            stage_changed = bool(stage != prev_stage or milestone_advanced)
            ratchet_on = bool(_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED))
            ratchet_map: Dict[str, Dict[str, Any]] = {}
            tier_jump_map: Dict[str, Tuple[bool, bool]] = {}
            atr_val_cur = self.get_m1_atr(p_symbol)
            confirm_req = max(1, _int_num(_cfg("REVERSAL_CONFIRM_TICKS", 2), 2))
            if ratchet_on:
                signal_data["_last_ratchet_events"] = []
                signal_data["_last_lift_aborts"] = []
                roles_map = self.assign_or_preserve_layer_roles(signal_data)

                # TP3 Partial Bank on CORE_RUNNER (0.02 -> 0.01, never force-close 0.01)
                if ("TP3" in cleared or k >= 4) and not is_spike:
                    vol_min, _, _, dec = _extract_symbol_volume_limits(sym_info)
                    for idx_r, rt in enumerate(runner_tickets):
                        lk_r = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                        lv_r = layers.get(lk_r) if (lk_r and isinstance(layers.get(lk_r), dict)) else None
                        if lv_r is None:
                            continue
                        r_role = str(lv_r.get("role") or roles_map.get(lk_r or "", "CORE_RUNNER")).strip().upper()
                        if r_role == "CORE_RUNNER" and not lv_r.get("tp3_partial_done"):
                            cur_vol = _num(lv_r.get("remaining_lot") or lv_r.get("lot"), 0.0)
                            if cur_vol <= 0.0:
                                if self.dry_run:
                                    cur_vol = _num(self._dry_run_ticket_lots.get(rt), 0.0)
                                else:
                                    p_obj = live_positions_map.get(rt)
                                    if p_obj is not None:
                                        cur_vol = _num(getattr(p_obj, "volume", 0.0), 0.0)
                            if cur_vol + 1e-9 >= 2.0 * vol_min:
                                partial_vol = max(vol_min, round(cur_vol / 2.0, dec))
                                p_res = self.close_partial_volume(
                                    rt,
                                    partial_vol,
                                    current_volume=cur_vol,
                                    symbol=p_symbol,
                                    direction=direction,
                                )
                                if p_res.get("ok"):
                                    lv_r["tp3_partial_done"] = True
                                    lv_r["remaining_lot"] = p_res.get("remaining_volume")
                                    signal_data["last_partial_bank"] = {
                                        "layer": lk_r,
                                        "ticket": rt,
                                        "role": "CORE_RUNNER",
                                        "closed_volume": p_res.get("closed_volume"),
                                        "remaining_volume": p_res.get("remaining_volume"),
                                        "milestone": "TP3",
                                    }

                # Per-ticket MFE & Ratchet computation for all open layers
                leg_prog_cur = 0.0
                leg_span_cur: Optional[float] = None
                if 1 <= stage < max_stage:
                    p_tp = tp_levels.get(f"TP{stage}")
                    n_tp = tp_levels.get(f"TP{stage + 1}")
                    if p_tp is not None and n_tp is not None:
                        leg_span_cur = (float(n_tp) - float(p_tp)) if is_buy else (float(p_tp) - float(n_tp))
                        if leg_span_cur > 1e-9:
                            leg_prog_cur = ((chart_price - float(p_tp)) / leg_span_cur) if is_buy else ((float(p_tp) - chart_price) / leg_span_cur)

                open_keys_r = self._ordered_open_layer_keys(signal_data)
                raw_tp1_r = signal_data.get("raw_tp1") or tp_levels.get("TP1")
                is_solo_r = len(open_keys_r) == 1
                for lk_o in open_keys_r:
                    lv_o = layers.get(lk_o)
                    if not isinstance(lv_o, dict):
                        continue
                    ent_o = self._resolve_layer_entry(signal_data, lk_o, lv_o) or ref_entry or 0.0
                    if ent_o <= 0.0:
                        continue
                    cur_prof_o = round((live_price - ent_o) if is_buy else (ent_o - live_price), 6)
                    prev_mfe_o = _num(lv_o.get("mfe_dollars"), 0.0)
                    if not is_spike:
                        new_mfe_o = round(max(prev_mfe_o, cur_prof_o, 0.0), 6)
                        if cur_prof_o > prev_mfe_o + 1e-9:
                            lv_o["virt_ratchet_ticks"] = 0
                        prev_pk_o = lv_o.get("mfe_peak_price")
                        if prev_pk_o is None or (is_buy and live_price > float(prev_pk_o)) or (not is_buy and live_price < float(prev_pk_o)):
                            lv_o["mfe_peak_price"] = round(live_price, digits)
                        lv_o["mfe_dollars"] = new_mfe_o
                    else:
                        new_mfe_o = prev_mfe_o
                    tp1_leg_o = ((float(raw_tp1_r) - ent_o) if is_buy else (ent_o - float(raw_tp1_r))) if raw_tp1_r is not None else None
                    prog_tp1_o = (((chart_price - ent_o) / tp1_leg_o) if is_buy else ((ent_o - chart_price) / tp1_leg_o)) if (tp1_leg_o and tp1_leg_o > 1e-9) else 1.0
                    role_o = str(lv_o.get("role") or roles_map.get(lk_o, "CORE_RUNNER")).strip().upper()
                    r_res_o = self.compute_ticket_ratchet_levels(
                        direction=direction,
                        entry_price=ent_o,
                        mfe_exec=new_mfe_o,
                        tp1_leg=tp1_leg_o,
                        prog_to_tp1=prog_tp1_o,
                        stage=stage,
                        leg_prog=leg_prog_cur,
                        leg_span=leg_span_cur,
                        atr_val=atr_val_cur,
                        role=role_o,
                        is_solo=is_solo_r,
                        lift_active=False,
                        digits=digits,
                    )
                    ratchet_map[lk_o] = r_res_o
                    new_tier_o = r_res_o.get("tier")
                    prev_tier_o = lv_o.get("ratchet_tier")
                    was_sq_o = bool(lv_o.get("squeeze_active"))
                    now_sq_o = bool(r_res_o.get("is_squeeze"))
                    tier_jumped_o = bool(new_tier_o and new_tier_o != prev_tier_o)
                    sq_started_o = bool(now_sq_o and not was_sq_o)
                    if new_tier_o:
                        lv_o["ratchet_tier"] = new_tier_o
                    if now_sq_o:
                        lv_o["squeeze_active"] = True
                    tier_jump_map[lk_o] = (tier_jumped_o, sq_started_o)

            # (6) Proactive broker TP lift at >= RUNNER_EARLY_TP_LIFT_PCT (70%) of leg OR proximity buffer (chart space)
            cur_target = tp_levels.get(f"TP{k}")
            proactive_lift_stage: Optional[str] = None
            if cur_target is not None and bool(_cfg("RUNNER_PROACTIVE_TP_LIFT", RUNNER_PROACTIVE_TP_LIFT)) and k < max_stage:
                cur_target_f = float(cur_target)
                anchor = self._resolve_milestone_anchor(signal_data, tp_levels, k, fallback_entry=runner_entry)
                lift_now = False
                if anchor is not None and float(anchor) > 0:
                    leg_span = (cur_target_f - float(anchor)) if is_buy else (float(anchor) - cur_target_f)
                    if leg_span > 1e-9:
                        prog = ((chart_price - float(anchor)) / leg_span) if is_buy else ((float(anchor) - chart_price) / leg_span)
                        if prog + 1e-9 >= float(_cfg("RUNNER_EARLY_TP_LIFT_PCT", RUNNER_EARLY_TP_LIFT_PCT)):
                            lift_now = True
                if not lift_now:
                    eff_buf, _ = self._compute_proximity_and_pullback(cur_target_f, chart_price, pip_size, anchor=anchor)
                    dist_t = (cur_target_f - chart_price) if is_buy else (chart_price - cur_target_f)
                    if dist_t <= eff_buf + 1e-9:
                        lift_now = True
                if lift_now:
                    lift_idx = min(max_stage, k + 1)
                    lift_stage = f"TP{lift_idx}"
                    lift_tp = tp_levels.get(lift_stage)
                    if lift_tp is not None:
                        lift_tp_rounded = round(float(lift_tp), digits)
                        needs_tp_lift = False
                        for lk in runner_layers:
                            l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                            if l_info is None or abs(_num(l_info.get("tp"), 0.0) - lift_tp_rounded) >= 1e-6 or l_info.get("tp_stage") != lift_stage:
                                needs_tp_lift = True
                                break
                        if needs_tp_lift or not runner_layers:
                            stepped_milestone_tp = float(lift_tp)
                            stepped_milestone_stage = lift_stage
                        proactive_lift_stage = lift_stage
                        if not ratchet_on:
                            signal_data["tp_stage"] = lift_stage

            # (7) Stage tier (mult, min_d, max_d) -> trail_dist, trail_tier_used, atr_stop + Dual-Gate Stage 1 ATR check
            trail_dist, tier_used = self._compute_staged_trail_dist(p_symbol, stage, digits)
            atr_stop = round((trail_peak - trail_dist) if is_buy else (trail_peak + trail_dist), digits)
            signal_data["trail_dist"] = trail_dist
            signal_data["trail_tier_used"] = tier_used
            signal_data["atr_stop"] = atr_stop

            was_s1_armed = bool(signal_data.get("stage1_atr_armed"))
            s1_atr_armed = self._eval_stage1_atr_armed(
                signal_data,
                stage,
                tp_levels,
                direction,
                chart_peak,
                atr_stop,
            )
            just_armed_s1 = (not was_s1_armed) and s1_atr_armed

            # (8) Protective selection + monotonic trail_stop ratchet (never loosens on stage handoff or ATR expansion)
            prev_trail_stop: Optional[float] = None
            for prev_key in ("trail_stop", "runner_sl_floor", "soft_tp1_floor"):
                pv = signal_data.get(prev_key)
                if isinstance(pv, numbers.Real) and not isinstance(pv, bool) and float(pv) > 0.0:
                    prev_trail_stop = round(float(pv), digits)
                    break

            stop_cands: List[float] = []
            if s1_atr_armed or fsm_floor is None:
                stop_cands.append(atr_stop)
            if fsm_floor is not None:
                stop_cands.append(fsm_floor)
            if prev_trail_stop is not None:
                stop_cands.append(prev_trail_stop)
            primary_ratchet_sl: Optional[float] = None
            if ratchet_on:
                for lk_r in runner_layers:
                    r_entry_val = ratchet_map.get(lk_r) or {}
                    r_brk = r_entry_val.get("broker_sl")
                    if r_brk is not None and float(r_brk) > 0.0:
                        r_brk_f = round(float(r_brk), digits)
                        stop_cands.append(r_brk_f)
                        if primary_ratchet_sl is None or (is_buy and r_brk_f > primary_ratchet_sl) or (not is_buy and r_brk_f < primary_ratchet_sl):
                            primary_ratchet_sl = r_brk_f
                    lv_r = layers.get(lk_r)
                    if isinstance(lv_r, dict):
                        ent_r = self._resolve_layer_entry(signal_data, lk_r, lv_r) or ref_entry
                        for rk_sl in ("ratchet_sl", "broker_sl", "sl"):
                            rv = lv_r.get(rk_sl)
                            if isinstance(rv, numbers.Real) and not isinstance(rv, bool) and float(rv) > 0.0 and ent_r:
                                if (is_buy and float(rv) >= float(ent_r) - 1e-6) or (not is_buy and float(rv) <= float(ent_r) + 1e-6):
                                    stop_cands.append(round(float(rv), digits))

            trail_stop = round(max(stop_cands) if is_buy else min(stop_cands), digits)
            signal_data["trail_stop"] = trail_stop
            signal_data["runner_sl_floor"] = trail_stop
            signal_data["soft_tp1_floor"] = trail_stop

            # (9) Tie-breaker for active_stop_source (FSM_FLOOR wins on exact tie fsm_floor == trail_stop)
            ratchet_tightened = bool(
                ratchet_on
                and primary_ratchet_sl is not None
                and (
                    (is_buy and primary_ratchet_sl >= trail_stop - 1e-9)
                    or (not is_buy and primary_ratchet_sl <= trail_stop + 1e-9)
                )
                and (
                    fsm_floor is None
                    or ((is_buy and primary_ratchet_sl > fsm_floor + 1e-9) or (not is_buy and primary_ratchet_sl < fsm_floor - 1e-9))
                )
            )
            active_stop_source = (
                "FSM_FLOOR"
                if (
                    fsm_floor is not None
                    and (
                        (is_buy and fsm_floor >= trail_stop - 1e-9)
                        or (not is_buy and fsm_floor <= trail_stop + 1e-9)
                    )
                )
                else ("PROFIT_RATCHET" if ratchet_tightened else "ATR_TRAIL")
            )
            signal_data["active_stop_source"] = active_stop_source

            # (10) Broker SL/TP sync
            if ratchet_on:
                # Per-ticket sync on non-runner layers (STAGE2_BANKER, LATE_RECOVERY): never broadcast runner SL!
                runner_set_all = set(runner_tickets)
                for lk_o, r_res_o in ratchet_map.items():
                    if lk_o in runner_layers:
                        continue
                    lv_o = layers.get(lk_o)
                    if not isinstance(lv_o, dict):
                        continue
                    lt_o = _int_num(lv_o.get("ticket"), 0)
                    if lt_o <= 0 or lt_o in runner_set_all:
                        continue
                    des_o = r_res_o.get("broker_sl")
                    tj_o, sq_o = tier_jump_map.get(lk_o, (False, False))
                    if des_o is not None:
                        s_o = self._sync_ticket_ratchet_sl(
                            signal_data=signal_data,
                            layer_key=lk_o,
                            ticket=lt_o,
                            desired_broker_sl=float(des_o),
                            force_sync=bool(tj_o or sq_o),
                            tick=tick,
                            sym_info=sym_info,
                            digits=digits,
                        )
                        tag_o = f"{r_res_o.get('tier')}_SQ" if sq_o else r_res_o.get("tier")
                        if (tj_o or sq_o) and r_res_o.get("tier") and lv_o.get("last_alerted_tier") != tag_o:
                            lv_o["last_alerted_tier"] = tag_o
                            signal_data["_last_ratchet_events"].append({
                                "type": "SQUEEZE" if sq_o else "TIER_JUMP",
                                "layer": lk_o,
                                "role": lv_o.get("role"),
                                "ticket": lt_o,
                                "entry_price": self._resolve_layer_entry(signal_data, lk_o, lv_o),
                                "peak_price": lv_o.get("mfe_peak_price"),
                                "mfe_dollars": lv_o.get("mfe_dollars"),
                                "tier": r_res_o.get("tier"),
                                "broker_sl": s_o.get("sl") or lv_o.get("ratchet_sl") or des_o,
                            })

                # Per-runner 2-step SL-first TP lift + monotonic broker SL sync
                step_dol = float(_cfg("TP1_TRAIL_STEP_DOLLARS", TP1_TRAIL_STEP_DOLLARS))
                for idx_r, rt in enumerate(runner_tickets):
                    lk_r = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                    lv_r = layers.get(lk_r) if (lk_r and isinstance(layers.get(lk_r), dict)) else None
                    l_entry_r = self._resolve_layer_entry(signal_data, lk_r, lv_r) or ref_entry or 0.0
                    l_mfe_r = _num((lv_r or {}).get("mfe_dollars"), _num(signal_data.get("peak_profit"), 0.0))
                    r_res_r = ratchet_map.get(lk_r or "", {})
                    tj_r, sq_r = tier_jump_map.get(lk_r or "", (False, False))
                    des_r = trail_stop
                    if r_res_r.get("broker_sl") is not None:
                        rb_f = round(float(r_res_r["broker_sl"]), digits)
                        des_r = round(max(des_r, rb_f) if is_buy else min(des_r, rb_f), digits)
                    lifted_now_r = False
                    if stepped_milestone_tp is not None and stepped_milestone_stage:
                        old_tp_r = (lv_r or {}).get("tp")
                        lift_r = self._lift_ticket_tp_two_step(
                            signal_data=signal_data,
                            layer_key=lk_r,
                            ticket=rt,
                            next_tp=float(stepped_milestone_tp),
                            next_tp_stage=stepped_milestone_stage,
                            desired_broker_sl=float(des_r),
                            entry_price=l_entry_r,
                            mfe_exec=l_mfe_r if l_mfe_r > 0.0 else None,
                            tick=tick,
                            sym_info=sym_info,
                            digits=digits,
                        )
                        if lift_r.get("lifted"):
                            lifted_now_r = True
                            signal_data["tp_stage"] = stepped_milestone_stage
                            signal_data["_last_ratchet_events"].append({
                                "type": "TP_LIFT",
                                "layer": lk_r,
                                "role": (lv_r or {}).get("role") or "CORE_RUNNER",
                                "ticket": rt,
                                "entry_price": l_entry_r,
                                "peak_price": (lv_r or {}).get("mfe_peak_price") or trail_peak,
                                "mfe_dollars": l_mfe_r,
                                "tier": r_res_r.get("tier") or (lv_r or {}).get("ratchet_tier"),
                                "broker_sl": lift_r.get("sl"),
                                "old_tp": old_tp_r,
                                "new_tp": lift_r.get("tp"),
                                "new_tp_stage": stepped_milestone_stage,
                            })
                        elif lift_r.get("already_at_tp"):
                            signal_data["tp_stage"] = proactive_lift_stage or stepped_milestone_stage
                        elif lift_r.get("aborted_sl_fail") and lift_r.get("notify_abort"):
                            signal_data["_last_lift_aborts"].append({
                                "layer": lk_r,
                                "ticket": rt,
                                "desired_sl": des_r,
                                "retcode": lift_r.get("retcode"),
                            })
                    if not lifted_now_r:
                        s_r = self._sync_ticket_ratchet_sl(
                            signal_data=signal_data,
                            layer_key=lk_r,
                            ticket=rt,
                            desired_broker_sl=float(des_r),
                            step_dollars=step_dol,
                            force_sync=bool(stage_changed or just_armed_s1 or tj_r or sq_r),
                            tick=tick,
                            sym_info=sym_info,
                            digits=digits,
                            floor_lock_sl=fsm_floor,
                        )
                        tag_r = f"{r_res_r.get('tier')}_SQ" if sq_r else r_res_r.get("tier")
                        if (tj_r or sq_r) and r_res_r.get("tier") and lv_r is not None and lv_r.get("last_alerted_tier") != tag_r:
                            lv_r["last_alerted_tier"] = tag_r
                            signal_data["_last_ratchet_events"].append({
                                "type": "SQUEEZE" if sq_r else "TIER_JUMP",
                                "layer": lk_r,
                                "role": lv_r.get("role") or "CORE_RUNNER",
                                "ticket": rt,
                                "entry_price": l_entry_r,
                                "peak_price": lv_r.get("mfe_peak_price") or trail_peak,
                                "mfe_dollars": l_mfe_r,
                                "tier": r_res_r.get("tier"),
                                "broker_sl": s_r.get("sl") or lv_r.get("ratchet_sl") or des_r,
                                "virt_sl": r_res_r.get("virt_sl"),
                            })
            else:
                # Legacy v3.5 sync path when PROFIT_RATCHET_ENABLED=False
                self._sync_tp1_trail_broker_sl(
                    signal_data,
                    p_symbol,
                    direction,
                    float(trail_stop),
                    digits,
                    tick=tick,
                    sym_info=sym_info,
                    runner_tp=stepped_milestone_tp,
                    runner_stage=stepped_milestone_stage,
                    force_sl_sync=bool(stage_changed or just_armed_s1),
                    fallback_sl=fsm_floor,
                )

            # (11) Exit check (paused during spread spike; otherwise close runner + ALL open layers)
            stop_hit = (live_price <= trail_stop + 1e-9) if is_buy else (live_price >= trail_stop - 1e-9)
            if stop_hit:
                if is_spike:
                    logger.info(
                        f"Runner {runner_tickets}: Staged {active_stop_source} exit PAUSED due to spread spike "
                        f"(live={live_price}, trail_stop={trail_stop}, spread=${spread_dollars:.2f} > ${eff_max_spread:.2f} max)"
                    )
                    return True

                if active_stop_source == "FSM_FLOOR":
                    floor_label = "Virtual TP1 Floor Protected" if stage <= 1 else "Virtual Floor Protected"
                    reason = (
                        f"Staged FSM Floor (Stage {stage}, {floor_label}, "
                        f"peak={trail_peak:.{digits}f}, floor={trail_stop:.{digits}f})"
                    )
                    code_out = active_stop_source
                elif active_stop_source == "PROFIT_RATCHET":
                    reason = (
                        f"Progressive Profit Ratchet SL (Stage {stage}, "
                        f"peak={trail_peak:.{digits}f}, ratchet_sl={trail_stop:.{digits}f})"
                    )
                    code_out = "PROFIT_RATCHET_SL"
                else:
                    reason = (
                        f"Staged ATR Trail (Stage {stage}, TP1 Trailing Stop, "
                        f"peak={trail_peak:.{digits}f}, stop={trail_stop:.{digits}f})"
                    )
                    code_out = active_stop_source
                logger.info(
                    f"Runner {runner_tickets}: {reason} triggered at live={live_price}. "
                    f"Closing runner + all open layers."
                )
                return self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason=reason,
                    close_all_market_tickets=True,
                    exit_reason_code=code_out,
                    exit_price=live_price,
                    exit_stage=stage,
                )

            # Near-Target Squeeze 2-tick virtual exit (spread-paused) when virt_sl > trail_stop
            if (
                ratchet_on
                and not is_spike
                and bool(_cfg("PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED", PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED))
                and primary_layer
                and primary_layer in ratchet_map
            ):
                pr_r = ratchet_map[primary_layer]
                lv_p = layers.get(primary_layer) if isinstance(layers.get(primary_layer), dict) else None
                virt_sl_r = pr_r.get("virt_sl")
                if pr_r.get("is_squeeze") and virt_sl_r is not None and lv_p is not None:
                    v_f = round(float(virt_sl_r), digits)
                    tighter_virt = (v_f > trail_stop + 1e-9) if is_buy else (v_f < trail_stop - 1e-9)
                    if tighter_virt:
                        v_hit = (live_price <= v_f + 1e-9) if is_buy else (live_price >= v_f - 1e-9)
                        if v_hit:
                            vt = _int_num(lv_p.get("virt_ratchet_ticks"), 0) + 1
                            lv_p["virt_ratchet_ticks"] = vt
                            if vt >= confirm_req:
                                reason_sq = (
                                    f"Near-Target Squeeze Harvest ({pr_r.get('tier')}, Stage {stage}, "
                                    f"peak={trail_peak:.{digits}f}, virt_sl={v_f:.{digits}f})"
                                )
                                logger.info(f"Runner {runner_tickets}: {reason_sq} triggered at live={live_price}.")
                                return self._finalize_runner_exit(
                                    signal_data,
                                    runner_tickets,
                                    runner_layers,
                                    reason=reason_sq,
                                    close_all_market_tickets=True,
                                    exit_reason_code="PROFIT_RATCHET_SL",
                                    exit_price=live_price,
                                    exit_stage=stage,
                                )
                        else:
                            lv_p["virt_ratchet_ticks"] = 0

            return True

        # 0. Dynamic ATR TP1 Trailing Stop (v3.4.2) with v3.4.1 TP1-Touch Fallback when disabled
        raw_tp1_val = signal_data.get("raw_tp1") or tp_levels.get("TP1")
        tp1_trail_on = bool(_cfg("TP1_TRAIL_ENABLED", TP1_TRAIL_ENABLED))
        tp1_touch_enabled = bool(_cfg("TP1_TOUCH_BANK_ENABLED", TP1_TOUCH_BANK_ENABLED))
        trail_active = False

        if tp1_trail_on:
            if raw_tp1_val is not None and not signal_data.get("tp1_touched"):
                touched_now = (chart_price >= float(raw_tp1_val)) if is_buy else (chart_price <= float(raw_tp1_val))
                if touched_now:
                    signal_data["tp1_touched"] = True
            if signal_data.get("tp1_touched"):
                trail_active = True
                trail_peak, _, trail_stop = self._update_tp1_trail_state(
                    signal_data,
                    p_symbol,
                    direction,
                    live_price,
                    pip_size,
                    digits,
                    runner_entry_fallback=runner_entry,
                )
                trail_hit = (live_price <= trail_stop + 1e-9) if is_buy else (live_price >= trail_stop - 1e-9)
                if trail_hit:
                    reason = f"TP1 Trailing Stop (peak={trail_peak:.{digits}f}, stop={trail_stop:.{digits}f})"
                    logger.info(
                        f"Runner {runner_tickets}: {reason} triggered at live={live_price}. "
                        f"Closing runner + all open layers."
                    )
                    return self._finalize_runner_exit(
                        signal_data,
                        runner_tickets,
                        runner_layers,
                        reason=reason,
                        close_all_market_tickets=True,
                    )
        elif tp1_touch_enabled:
            if raw_tp1_val is not None:
                touched_now = (chart_price >= float(raw_tp1_val)) if is_buy else (chart_price <= float(raw_tp1_val))
                if touched_now and not signal_data.get("tp1_touched"):
                    self._apply_tp1_touch_protection(
                        signal_data,
                        p_symbol,
                        direction,
                        float(raw_tp1_val),
                        pip_size,
                        digits,
                        tick=tick,
                        sym_info=sym_info,
                    )
            if signal_data.get("tp1_touched"):
                tf_raw = signal_data.get("tp1_touch_floor")
                if tf_raw is None and raw_tp1_val is not None:
                    tf_raw = self._compute_tp1_touch_floor(float(raw_tp1_val), direction, pip_size, digits)
                    signal_data["tp1_touch_floor"] = tf_raw
                if tf_raw is not None:
                    tf_val = float(tf_raw)
                    touch_bank_hit = (live_price <= tf_val + 1e-9) if is_buy else (live_price >= tf_val - 1e-9)
                    if touch_bank_hit:
                        logger.info(
                            f"Runner {runner_tickets}: TP1-Touch Fallback Bank triggered "
                            f"(live={live_price}, tp1_touch_floor={tf_val}). Closing runner + all open layers."
                        )
                        return self._finalize_runner_exit(
                            signal_data,
                            runner_tickets,
                            runner_layers,
                            reason="TP1-Touch Fallback Bank",
                            close_all_market_tickets=True,
                        )

        floor_price = signal_data.get("runner_sl_floor")
        if floor_price is None:
            floor_price = signal_data.get("soft_tp1_floor")

        # 1. Virtual Floor Protection (skip when active TP1 ATR trail owns virtual exit in Section 0)
        if not trail_active and floor_price is not None:
            fp = float(floor_price)
            floor_hit = (live_price <= fp) if is_buy else (live_price >= fp)
            if floor_hit:
                logger.info(f"Runner {runner_tickets}: Virtual floor hit (live={live_price}, floor={fp}). Closing at market.")
                cleared = signal_data.get("runner_cleared_milestones") or []
                label = "Virtual TP1 Floor Protected" if len(cleared) <= 1 else "Virtual Floor Protected"
                return self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason=f"{label} ({fp})",
                )

        # 2. Backstop auto-close for any non-runner L2 (TP2) and L3 (TP3)
        self._close_reached_layer_targets(signal_data, tp_levels, is_buy, live_price, runner_tickets)

        # 3. Ceiling Target Exit (TP{max_stage}, default TP4; TP5 when configured)
        ceiling_key = f"TP{max_stage}"
        ceiling_tp = tp_levels.get(ceiling_key)
        if ceiling_tp is not None:
            ceiling_val = float(ceiling_tp)
            if (is_buy and live_price >= ceiling_val) or (not is_buy and live_price <= ceiling_val):
                logger.info(f"Runner {runner_tickets}: Reached {ceiling_key} ceiling ({ceiling_val})! Closing at market.")
                return self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason=f"Target {ceiling_key} Reached ({ceiling_val})",
                    ceiling_stage=max_stage,
                )

        # 4. Clean Breakthrough Progression (TP1 -> ... -> TP{max_stage - 1})
        k = max(1, min(max_stage, _int_num(signal_data.get("runner_milestone_idx", 1), 1)))
        cleared = list(signal_data.get("runner_cleared_milestones") or [])
        clean_break_dist = float(_cfg("RUNNER_CLEAN_BREAK_PIPS", RUNNER_CLEAN_BREAK_PIPS)) * pip_size
        trail_dist = float(_cfg("TRAIL_DISTANCE_PIPS", TRAIL_DISTANCE_PIPS)) * pip_size
        raw_lock = float(_cfg("TP1_LOCK_BUFFER", TP1_LOCK_BUFFER))
        lock_buf = raw_lock * (pip_size / 0.10) if pip_size > 0 else raw_lock
        be_offset = float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS)) * pip_size
        be_floor: Optional[float] = None
        if runner_entry is not None and float(runner_entry) > 0:
            be_floor = round(float(runner_entry) + be_offset, digits) if is_buy else round(float(runner_entry) - be_offset, digits)

        gold_guard_on = bool(_cfg("GOLD_REVERSAL_GUARD_ENABLED", GOLD_REVERSAL_GUARD_ENABLED))
        stepped_milestone_tp: Optional[float] = None
        stepped_milestone_stage: Optional[str] = None
        while k < max_stage:
            target = tp_levels.get(f"TP{k}")
            if target is None:
                break
            target_f = float(target)
            clean_level = round(target_f + clean_break_dist, digits) if is_buy else round(target_f - clean_break_dist, digits)
            broke_clean = (live_price >= clean_level) if is_buy else (live_price <= clean_level)
            if not broke_clean:
                break

            m_name = f"TP{k}"
            if m_name not in cleared:
                cleared.append(m_name)
            if k >= 2:
                signal_data[f"tp{k}_reached"] = True

            signal_data["runner_proximity_armed"] = False
            signal_data["runner_reversal_ticks"] = 0
            signal_data["tp2_shortfall_armed"] = False
            prev_pk_px = signal_data.get("runner_peak_price")
            if prev_pk_px is not None:
                signal_data["runner_peak_price"] = max(float(prev_pk_px), live_price) if is_buy else min(float(prev_pk_px), live_price)
            else:
                signal_data["runner_peak_price"] = live_price
            if gold_guard_on:
                signal_data["reversal_guard_armed"] = True

            if k == 1:
                cands = []
                if not tp1_trail_on:
                    cands.append(target_f - lock_buf if is_buy else target_f + lock_buf)
                if be_floor is not None:
                    cands.append(be_floor)
                if floor_price is not None:
                    cands.append(float(floor_price))
                if cands:
                    floor_price = round(max(cands) if is_buy else min(cands), digits)
            else:
                prev_tp = tp_levels.get(f"TP{k - 1}")
                cands = []
                if not gold_guard_on and not tp1_trail_on:
                    trail_cand = target_f - trail_dist if is_buy else target_f + trail_dist
                    cands.append(trail_cand)
                if prev_tp is not None:
                    cands.append(float(prev_tp))
                if floor_price is not None:
                    cands.append(float(floor_price))
                if cands:
                    floor_price = round(max(cands) if is_buy else min(cands), digits)

            k += 1
            step_stage = f"TP{k}"
            step_tp = tp_levels.get(step_stage)
            if trail_active:
                stepped_milestone_tp = step_tp
                stepped_milestone_stage = step_stage
                signal_data["tp_stage"] = step_stage
                if floor_price is not None:
                    signal_data["trail_stop"] = floor_price
            else:
                self._step_runner_broker_tp_and_sl(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    new_sl=floor_price,
                    new_tp=step_tp,
                    new_stage=step_stage,
                    digits=digits,
                )
            logger.info(
                f"Runner {runner_tickets}: Clean break past {m_name} ({target_f}); "
                f"stepped to {step_stage}={step_tp}, floor={floor_price}"
            )

        signal_data["runner_milestone_idx"] = k
        signal_data["runner_milestone_name"] = f"TP{k}"
        signal_data["runner_cleared_milestones"] = cleared
        if floor_price is not None:
            signal_data["runner_sl_floor"] = floor_price
            signal_data["soft_tp1_floor"] = floor_price

        if trail_active and floor_price is not None:
            self._sync_tp1_trail_broker_sl(
                signal_data,
                p_symbol,
                direction,
                float(floor_price),
                digits,
                tick=tick,
                sym_info=sym_info,
                runner_tp=stepped_milestone_tp,
                runner_stage=stepped_milestone_stage,
            )

        # 5. Reversal Guard ("Bank It") + Proactive Broker TP Lift at Current TPk
        cur_target = tp_levels.get(f"TP{k}")
        if gold_guard_on:
            # Proactive broker TP lift to TP{k+1} at 70% of current leg so hard broker TP never caps momentum
            if cur_target is not None and bool(_cfg("RUNNER_PROACTIVE_TP_LIFT", RUNNER_PROACTIVE_TP_LIFT)) and k < max_stage:
                cur_target_f = float(cur_target)
                anchor = self._resolve_milestone_anchor(signal_data, tp_levels, k, fallback_entry=runner_entry)
                lift_now = False
                if anchor is not None and float(anchor) > 0:
                    leg_span = (cur_target_f - float(anchor)) if is_buy else (float(anchor) - cur_target_f)
                    if leg_span > 1e-9:
                        prog = ((live_price - float(anchor)) / leg_span) if is_buy else ((float(anchor) - live_price) / leg_span)
                        if prog + 1e-9 >= float(_cfg("RUNNER_EARLY_TP_LIFT_PCT", RUNNER_EARLY_TP_LIFT_PCT)):
                            lift_now = True
                if not lift_now:
                    eff_buf, _ = self._compute_proximity_and_pullback(cur_target_f, live_price, pip_size, anchor=anchor)
                    dist_t = (cur_target_f - live_price) if is_buy else (live_price - cur_target_f)
                    if dist_t <= eff_buf + 1e-9:
                        lift_now = True
                if lift_now:
                    lift_stage = f"TP{k + 1}"
                    lift_tp = tp_levels.get(lift_stage)
                    if lift_tp is not None:
                        self._step_runner_broker_tp_and_sl(
                            signal_data,
                            runner_tickets,
                            runner_layers,
                            new_sl=None,
                            new_tp=float(lift_tp),
                            new_stage=lift_stage,
                            digits=digits,
                        )

            guard_res = self.evaluate_gold_reversal_guard(
                signal_data,
                tick=tick,
                runner_entry_override=runner_entry,
            )
            if guard_res.get("triggered"):
                candle_ok = True
                if not guard_res.get("hard_ceiling_triggered") and _cfg("RUNNER_REVERSAL_CANDLE_CONFIRM", RUNNER_REVERSAL_CANDLE_CONFIRM):
                    candle = self.get_last_closed_m1_candle(p_symbol)
                    if not candle:
                        candle_ok = False
                    elif is_buy:
                        candle_ok = candle["close"] < candle["open"]
                    else:
                        candle_ok = candle["close"] > candle["open"]
                if candle_ok:
                    pk_px = guard_res.get("peak_price", live_price)
                    dd_pct = guard_res.get("drawdown_from_peak", 0.0) * 100.0
                    pb_dlr = guard_res.get("pullback_dollars", 0.0)
                    logger.info(
                        f"Runner {runner_tickets}: Gold Reversal Guard BANK IT at TP{k} "
                        f"(peak={pk_px}, live={live_price}, giveback={dd_pct:.1f}% / ${pb_dlr:.2f}). Closing at market."
                    )
                    return self._finalize_runner_exit(
                        signal_data,
                        runner_tickets,
                        runner_layers,
                        reason=f"Gold Reversal Guard (TP{k}, Peak: {pk_px}, Giveback: {dd_pct:.1f}% / ${pb_dlr:.2f})",
                    )
        elif cur_target is not None:
            cur_target_f = float(cur_target)
            anchor = self._resolve_milestone_anchor(signal_data, tp_levels, k, fallback_entry=runner_entry)
            cur_peak = signal_data.get("runner_peak_price")
            eff_peak_for_pb = float(cur_peak) if cur_peak is not None else live_price
            eff_buffer, eff_pullback = self._compute_proximity_and_pullback(
                cur_target_f, eff_peak_for_pb, pip_size, anchor=anchor
            )

            dist_to_target = (cur_target_f - live_price) if is_buy else (live_price - cur_target_f)
            if not signal_data.get("runner_proximity_armed") and dist_to_target <= eff_buffer + 1e-9:
                signal_data["runner_proximity_armed"] = True
                signal_data["runner_reversal_ticks"] = 0
                signal_data["tp2_shortfall_armed"] = (k == 2)
                signal_data["runner_peak_price"] = live_price
                logger.info(
                    f"Runner {runner_tickets}: Proximity guard ARMED near TP{k}={cur_target_f} "
                    f"(live={live_price}, buf={eff_buffer:.4f})"
                )
                if bool(_cfg("RUNNER_PROACTIVE_TP_LIFT", RUNNER_PROACTIVE_TP_LIFT)) and k < max_stage:
                    lift_stage = f"TP{k + 1}"
                    lift_tp = tp_levels.get(lift_stage)
                    if lift_tp is not None:
                        self._step_runner_broker_tp_and_sl(
                            signal_data,
                            runner_tickets,
                            runner_layers,
                            new_sl=None,
                            new_tp=float(lift_tp),
                            new_stage=lift_stage,
                            digits=digits,
                        )

            if signal_data.get("runner_proximity_armed"):
                prev_peak = signal_data.get("runner_peak_price")
                improved_peak = False
                if prev_peak is None:
                    new_peak = live_price
                else:
                    prev_peak_f = float(prev_peak)
                    if (is_buy and live_price > prev_peak_f + 1e-9) or (not is_buy and live_price < prev_peak_f - 1e-9):
                        new_peak = live_price
                        improved_peak = True
                    else:
                        new_peak = prev_peak_f
                signal_data["runner_peak_price"] = new_peak
                if improved_peak:
                    signal_data["runner_reversal_ticks"] = 0

                _, eff_pullback = self._compute_proximity_and_pullback(
                    cur_target_f, new_peak, pip_size, anchor=anchor
                )
                pullback = (new_peak - live_price) if is_buy else (live_price - new_peak)
                if round(pullback, 6) + 1e-9 >= round(eff_pullback, 6):
                    candle_ok = True
                    if _cfg("RUNNER_REVERSAL_CANDLE_CONFIRM", RUNNER_REVERSAL_CANDLE_CONFIRM):
                        candle = self.get_last_closed_m1_candle(p_symbol)
                        if not candle:
                            candle_ok = False
                        elif is_buy:
                            candle_ok = candle["close"] < candle["open"]
                        else:
                            candle_ok = candle["close"] > candle["open"]

                    if candle_ok:
                        rev_ticks = _int_num(signal_data.get("runner_reversal_ticks", 0), 0) + 1
                        signal_data["runner_reversal_ticks"] = rev_ticks
                        req_ticks = max(1, _int_num(_cfg("RUNNER_REVERSAL_CONFIRM_TICKS", RUNNER_REVERSAL_CONFIRM_TICKS), 2))
                        if rev_ticks >= req_ticks:
                            logger.info(
                                f"Runner {runner_tickets}: Bank It Proximity Guard confirmed ({rev_ticks}/{req_ticks} ticks) "
                                f"at TP{k} (peak={new_peak}, live={live_price}, pullback={pullback:.4f}). Closing at market."
                            )
                            return self._finalize_runner_exit(
                                signal_data,
                                runner_tickets,
                                runner_layers,
                                reason=f"Bank It Proximity Guard (TP{k}, Peak: {new_peak})",
                            )
                else:
                    signal_data["runner_reversal_ticks"] = 0

        # 6. Optional Continuous One-Way Hard Broker SL Trailing (disabled when Gold Reversal Guard or v3.4.2 ATR TP1 Trail is active)
        cont_trail_on = (
            bool(_cfg("RUNNER_CONTINUOUS_TRAIL_ENABLED", RUNNER_CONTINUOUS_TRAIL_ENABLED))
            or not gold_guard_on
        ) and not trail_active
        if cont_trail_on:
            trail_step = float(_cfg("TRAIL_STEP_PIPS", TRAIL_STEP_PIPS)) * pip_size
            if is_buy:
                floor_bound = float(floor_price) if floor_price is not None else 0.0
                candidate_sl = max(floor_bound, round(live_price - trail_dist, digits))
                if candidate_sl >= round((pos_sl or 0.0) + trail_step, digits) - 1e-9 and (live_price - candidate_sl) >= min_stop_dist - 1e-9:
                    logger.info(f"Runner {runner_tickets}: Trailing SL ratchet -> {candidate_sl} (live={live_price})")
                    any_mod = False
                    for rt in runner_tickets:
                        if self.modify_position_sltp(rt, sl=candidate_sl, tp=None):
                            any_mod = True
                    if any_mod and self.dry_run:
                        signal_data["broker_sl"] = candidate_sl
            else:
                floor_bound = float(floor_price) if floor_price is not None else 999999.0
                candidate_sl = min(floor_bound, round(live_price + trail_dist, digits))
                if (pos_sl == 0.0 or candidate_sl <= round(pos_sl - trail_step, digits) + 1e-9) and (candidate_sl - live_price) >= min_stop_dist - 1e-9:
                    logger.info(f"Runner {runner_tickets}: Trailing SL ratchet -> {candidate_sl} (live={live_price})")
                    any_mod = False
                    for rt in runner_tickets:
                        if self.modify_position_sltp(rt, sl=candidate_sl, tp=None):
                            any_mod = True
                    if any_mod and self.dry_run:
                        signal_data["broker_sl"] = candidate_sl

        return True

    def _manage_legacy_runner(self, signal_data: Dict[str, Any]) -> bool:
        """Legacy 3-layer runner trailing & TP2/TP3 shortfall guard (dynamic pip_size)."""
        ticket = signal_data.get("runner_ticket")
        if not ticket:
            return False

        signal_data["last_runner_ticket"] = ticket
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        floor_price = signal_data.get("soft_tp1_floor")
        raw_tp1 = signal_data.get("raw_tp1")
        raw_tp2 = signal_data.get("raw_tp2")
        raw_tp3 = signal_data.get("raw_tp3")
        using_tp3 = bool(signal_data.get("hold_past_tp2") and raw_tp3)
        runner_target = raw_tp3 if using_tp3 else raw_tp2
        target_label = "TP3" if using_tp3 else "TP2"

        if self.dry_run:
            p_symbol = symbol
            sym_info = self.get_symbol_info(p_symbol)
            tick = self.get_tick(p_symbol)
            if not tick:
                return True
            digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
            pos_sl = float(signal_data.get("soft_tp1_floor") or 0.0)
            stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
            pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
            min_stop_dist = stops_lvl * (pt or 0.01)
        else:
            with self._mt5_lock:
                positions = mt5.positions_get(ticket=ticket)
            if not positions:
                self.invalidate_realized_pnl_cache()
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = "Trailed Stop Loss Hit (Locked in Gain)"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False
            pos = positions[0]
            t_int_leg = _int_num(ticket, 0)
            if t_int_leg > 0:
                self._cached_pos_profits[t_int_leg] = round(
                    _num(getattr(pos, "profit", 0.0), 0.0) + _num(getattr(pos, "swap", 0.0), 0.0),
                    2,
                )
            pos_sl = _num(getattr(pos, "sl", 0.0), 0.0)
            raw_sym = getattr(pos, "symbol", None)
            p_symbol = raw_sym if isinstance(raw_sym, str) and raw_sym else symbol
            sym_info = self.get_symbol_info(p_symbol)
            tick = self.get_tick(p_symbol)
            if not sym_info or not tick:
                return True
            digits = _int_num(getattr(sym_info, "digits", 2), 2)
            stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0)
            pt = _num(getattr(sym_info, "point", 0.01), 0.01) or 0.01
            min_stop_dist = stops_lvl * pt

        pip_size = self.get_pip_size(p_symbol)
        trail_dist = float(_cfg("TRAIL_DISTANCE_PIPS", TRAIL_DISTANCE_PIPS)) * pip_size
        trail_step = float(_cfg("TRAIL_STEP_PIPS", TRAIL_STEP_PIPS)) * pip_size
        tp2_arm_dist = float(_cfg("TP2_SHORTFALL_ARM_PIPS", TP2_SHORTFALL_ARM_PIPS)) * pip_size
        tp2_pullback_dist = float(_cfg("TP2_SHORTFALL_PULLBACK_PIPS", TP2_SHORTFALL_PULLBACK_PIPS)) * pip_size
        no_tp2_arm_dist = float(_cfg("NO_TP2_SURGE_ARM_PIPS", NO_TP2_SURGE_ARM_PIPS)) * pip_size
        raw_lock = float(_cfg("TP1_LOCK_BUFFER", TP1_LOCK_BUFFER))
        lock_buf = raw_lock * (pip_size / 0.10) if pip_size > 0 else raw_lock

        if direction == "BUY":
            live_price = getattr(tick, "bid", None)
            if not isinstance(live_price, (int, float)) or live_price <= 0:
                return True

            if floor_price is not None and live_price <= floor_price:
                logger.info(f"Runner #{ticket}: Virtual TP1 floor hit (live={live_price} <= floor={floor_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"Virtual TP1 Floor Protected ({floor_price})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            if runner_target is not None and live_price >= runner_target:
                logger.info(f"Runner #{ticket}: Reached Target {target_label} ({runner_target})! Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["tp2_reached"] = True
                signal_data["runner_close_reason"] = f"Target {target_label} Reached ({runner_target})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            signal_data["runner_peak_price"] = max(signal_data.get("runner_peak_price") or live_price, live_price)
            peak = signal_data["runner_peak_price"]

            if runner_target is not None:
                arm_level = round(float(runner_target) - tp2_arm_dist, digits)
            elif raw_tp1 is not None:
                arm_level = round(float(raw_tp1) + no_tp2_arm_dist, digits)
            elif floor_price is not None:
                arm_level = round(float(floor_price) + lock_buf + no_tp2_arm_dist, digits)
            else:
                arm_level = None

            if arm_level is not None and live_price >= arm_level:
                signal_data["tp2_shortfall_armed"] = True

            if signal_data.get("tp2_shortfall_armed") and live_price <= round(peak - tp2_pullback_dist, digits):
                logger.info(f"Runner #{ticket}: TP2 Shortfall triggered (peak={peak}, live={live_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"TP2 Shortfall Guard Triggered (Peak: {peak})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            floor_bound = float(floor_price) if floor_price is not None else 0.0
            candidate_sl = max(floor_bound, round(live_price - trail_dist, digits))
            if candidate_sl >= round((pos_sl or 0.0) + trail_step, digits) and (live_price - candidate_sl) >= min_stop_dist:
                logger.info(f"Runner #{ticket}: Trailing SL ratchet -> {candidate_sl} (live={live_price})")
                self.modify_position_sltp(ticket, sl=candidate_sl, tp=None)

        elif direction == "SELL":
            live_price = getattr(tick, "ask", None)
            if not isinstance(live_price, (int, float)) or live_price <= 0:
                return True

            if floor_price is not None and live_price >= floor_price:
                logger.info(f"Runner #{ticket}: Virtual TP1 floor hit (live={live_price} >= floor={floor_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"Virtual TP1 Floor Protected ({floor_price})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            if runner_target is not None and live_price <= runner_target:
                logger.info(f"Runner #{ticket}: Reached Target {target_label} ({runner_target})! Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["tp2_reached"] = True
                signal_data["runner_close_reason"] = f"Target {target_label} Reached ({runner_target})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            signal_data["runner_peak_price"] = min(signal_data.get("runner_peak_price") or live_price, live_price)
            peak = signal_data["runner_peak_price"]

            if runner_target is not None:
                arm_level = round(float(runner_target) + tp2_arm_dist, digits)
            elif raw_tp1 is not None:
                arm_level = round(float(raw_tp1) - no_tp2_arm_dist, digits)
            elif floor_price is not None:
                arm_level = round(float(floor_price) - lock_buf - no_tp2_arm_dist, digits)
            else:
                arm_level = None

            if arm_level is not None and live_price <= arm_level:
                signal_data["tp2_shortfall_armed"] = True

            if signal_data.get("tp2_shortfall_armed") and live_price >= round(peak + tp2_pullback_dist, digits):
                logger.info(f"Runner #{ticket}: TP2 Shortfall triggered (peak={peak}, live={live_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"TP2 Shortfall Guard Triggered (Peak: {peak})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            floor_bound = float(floor_price) if floor_price is not None else 999999.0
            candidate_sl = min(floor_bound, round(live_price + trail_dist, digits))
            if (pos_sl == 0.0 or candidate_sl <= round(pos_sl - trail_step, digits)) and (candidate_sl - live_price) >= min_stop_dist:
                logger.info(f"Runner #{ticket}: Trailing SL ratchet -> {candidate_sl} (live={live_price})")
                self.modify_position_sltp(ticket, sl=candidate_sl, tp=None)

        return True

    def manage_designated_runner(self, signal_data: Dict[str, Any]) -> bool:
        """
        Manage the designated runner position(s):
        - v3.1 4-layer signals: TP1->TP2->TP3->TP4 (up to RUNNER_MAX_TP_STAGE) Stepped Momentum
          Scaling FSM with calibrated Anti-Choke Proximity Reversal Guard.
        - Legacy 3-layer signals: virtual TP1 floor, TP2/TP3 shortfall guard, and SL trailing.
        Returns True if runner remains active, False if runner exited.
        """
        if not signal_data.get("runner_ticket") and not signal_data.get("runner_tickets"):
            return False
        if _is_legacy_runner_signal(signal_data):
            return self._manage_legacy_runner(signal_data)
        return self._manage_4layer_runner(signal_data)

    def apply_smart_tp1_runner_strategy(
        self,
        layers: Dict[str, Dict[str, Any]],
        market_tickets: List[int],
        pending_tickets: List[int],
        symbol: str,
        direction: str,
        raw_tp1: float,
        raw_tp2: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Convenience wrapper creating signal_data dict and calling handle_tp1_scaling_and_select_runner."""
        sig_data = {
            "layers": copy.deepcopy(layers),
            "market_tickets": list(market_tickets),
            "pending_tickets": list(pending_tickets),
            "symbol": symbol,
            "direction": direction,
            "raw_tp1": raw_tp1,
            "raw_tp2": raw_tp2,
        }
        runner_t = self.handle_tp1_scaling_and_select_runner(sig_data)

        banked: List[str] = []
        banked_set = set(sig_data.get("last_banked_tickets") or [])
        for l_name, l_info in (sig_data.get("layers") or {}).items():
            if isinstance(l_info, dict) and l_info.get("ticket") in banked_set:
                banked.append(l_name)

        return {
            "status": sig_data["status"],
            "banked": banked,
            "runner": sig_data.get("runner_layer"),
            "runner_ticket": runner_t,
            "runner_layers": sig_data.get("runner_layers", []),
            "runner_tickets": sig_data.get("runner_tickets", []),
            "remaining_market_tickets": sig_data["market_tickets"],
            "trailed_sl": sig_data.get("soft_tp1_floor"),
            "soft_tp1_floor": sig_data.get("soft_tp1_floor"),
            "runner_sl_floor": sig_data.get("runner_sl_floor"),
        }

    def apply_partial_close(
        self,
        market_tickets: List[int],
        pending_tickets: List[int],
        symbol: str,
        direction: str,
    ) -> Optional[int]:
        """Partial close: banks profit on 1 open order and moves remaining to BE."""
        if self.dry_run:
            if len(market_tickets) > 1:
                closed = market_tickets[0]
                self.invalidate_realized_pnl_cache()
                logger.info(f"[DRY-RUN] Partial close: closed Order #{closed}, moving rest to BE.")
                return closed
            elif len(market_tickets) == 1:
                logger.info("[DRY-RUN] Only 1 order open; moved SL to BE.")
                return None
            return None

        active_positions = []
        with self._mt5_lock:
            for ticket in market_tickets:
                pos = mt5.positions_get(ticket=ticket)
                if pos:
                    active_positions.append(pos[0])

        if not active_positions:
            return None

        if len(active_positions) == 1:
            single_t = _int_num(getattr(active_positions[0], "ticket", 0), 0)
            if single_t:
                self.apply_breakeven(
                    market_tickets=[single_t],
                    pending_tickets=pending_tickets,
                    symbol=symbol,
                    direction=direction,
                )
            return None

        active_positions.sort(key=lambda p: _num(getattr(p, "profit", 0.0)), reverse=True)
        closed_ticket = _int_num(getattr(active_positions[0], "ticket", 0), 0)
        if not closed_ticket:
            return None

        if self.close_market_position(closed_ticket):
            remaining = [
                _int_num(getattr(p, "ticket", 0), 0)
                for p in active_positions
                if _int_num(getattr(p, "ticket", 0), 0) not in (0, closed_ticket)
            ]
            if remaining:
                self.apply_breakeven(
                    market_tickets=remaining,
                    pending_tickets=pending_tickets,
                    symbol=symbol,
                    direction=direction,
                )
            return closed_ticket

        return None

    def adjust_active_signal_sl_tp(
        self,
        signal_data: Dict[str, Any],
        new_sl: Optional[float] = None,
        labeled_tps: Optional[Dict[str, float]] = None,
        unlabeled_tps: Optional[Sequence[float]] = None,
        ratchet_sl_only: bool = False,
    ) -> Dict[str, Any]:
        """
        v3.3 Dynamic Active-Trade SL & TP1..TP5 Adjustment:
        1. Merges labeled_tps (exact TP1..TP5 keys) and unlabeled_tps without shifting TP2 into TP1,
           re-deriving any missing/non-monotonic higher stages up to TP5.
        2. Validates new_sl (enforcing forward-only ratchet on RUNNER_ACTIVE when ratchet_sl_only=True
           and live stop-level checks in live mode without blocking TP updates).
        3. Updates open market positions (runner_tickets & non-runner layers) and unfilled pending orders.
        """
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(symbol)
        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
        min_stop_dist = stops_lvl * (pt or 0.01)

        tick = self.get_tick(symbol)
        live_px: Optional[float] = None
        if tick is not None:
            cand_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
            if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                live_px = float(cand_px)

        tp_levels = dict(self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits))
        old_tp1 = tp_levels.get("TP1")

        clean_labeled: Dict[str, float] = {}
        if isinstance(labeled_tps, dict):
            for k, v in labeled_tps.items():
                up_k = str(k).strip().upper()
                if not up_k.startswith("TP") and up_k.isdigit():
                    up_k = f"TP{up_k}"
                if up_k in ("TP1", "TP2", "TP3", "TP4", "TP5") and isinstance(v, numbers.Real) and not isinstance(v, bool) and float(v) > 0:
                    clean_labeled[up_k] = round(float(v), digits)

        clean_unlabeled: List[float] = [
            round(float(x), digits)
            for x in (unlabeled_tps or [])
            if isinstance(x, numbers.Real) and not isinstance(x, bool) and float(x) > 0
        ]

        if clean_labeled or clean_unlabeled:
            for k, v in clean_labeled.items():
                tp_levels[k] = v

            u_idx = 0
            for stage_num in range(1, 6):
                if u_idx >= len(clean_unlabeled):
                    break
                sk = f"TP{stage_num}"
                if sk in clean_labeled:
                    continue
                tp_levels[sk] = clean_unlabeled[u_idx]
                clean_labeled[sk] = clean_unlabeled[u_idx]
                u_idx += 1

            steps = [
                float(_cfg("TP2_STEP_PIPS", TP2_STEP_PIPS)),
                float(_cfg("TP3_STEP_PIPS", TP3_STEP_PIPS)),
                float(_cfg("TP4_STEP_PIPS", TP4_STEP_PIPS)),
                float(_cfg("TP5_STEP_PIPS", TP5_STEP_PIPS)),
            ]
            sign = 1.0 if is_buy else -1.0

            if tp_levels.get("TP1") is None:
                for stage_num in range(2, 6):
                    higher = tp_levels.get(f"TP{stage_num}")
                    if higher is not None:
                        back_steps = sum(steps[: stage_num - 1]) * pip_size
                        tp_levels["TP1"] = round(float(higher) - sign * back_steps, digits)
                        break

            for stage_num in range(2, 6):
                sk = f"TP{stage_num}"
                prev_val = tp_levels.get(f"TP{stage_num - 1}")
                cur_val = tp_levels.get(sk)
                step_dist = steps[stage_num - 2] * pip_size
                if prev_val is not None:
                    is_monotonic = (cur_val is not None) and (
                        (is_buy and float(cur_val) > float(prev_val))
                        or (not is_buy and float(cur_val) < float(prev_val))
                    )
                    if sk not in clean_labeled and not is_monotonic:
                        tp_levels[sk] = round(float(prev_val) + sign * step_dist, digits)
                    elif cur_val is None:
                        tp_levels[sk] = round(float(prev_val) + sign * step_dist, digits)

            signal_data["tp_levels"] = dict(tp_levels)
            for i in range(1, 6):
                sk = f"TP{i}"
                if sk in tp_levels:
                    signal_data[f"raw_tp{i}"] = tp_levels[sk]
            if tp_levels.get("TP1") is not None:
                signal_data["tp1_price"] = tp_levels["TP1"]

            if "TP1" in clean_labeled or signal_data.get("final_tp") is None or tp_levels.get("TP1") != old_tp1:
                if tp_levels.get("TP1") is not None:
                    offset = float(_cfg("TP_FRONT_RUN_OFFSET", TP_FRONT_RUN_OFFSET)) * (pip_size / 0.10 if pip_size > 0 else 1.0)
                    new_final_tp = round(tp_levels["TP1"] - offset, digits) if is_buy else round(tp_levels["TP1"] + offset, digits)
                    signal_data["final_tp"] = new_final_tp
                    signal_data["hard_broker_tp"] = new_final_tp
                    prox_pips = float(_cfg("TP1_CLOSE_PROXIMITY_PIPS", TP1_CLOSE_PROXIMITY_PIPS))
                    prox_dist = prox_pips * pip_size
                    signal_data["proximity_close_price"] = (
                        round(tp_levels["TP1"] - prox_dist, digits)
                        if is_buy
                        else round(tp_levels["TP1"] + prox_dist, digits)
                    )

        staged_exit_on = bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))
        is_runner_phase = bool(signal_data.get("status") == "RUNNER_ACTIVE" or signal_data.get("tp1_reached"))
        has_protected_sl = bool(
            is_runner_phase
            or signal_data.get("be_armed")
            or str(signal_data.get("status") or "").strip().upper() == "BE_APPLIED"
            or signal_data.get("stage0_profit_lock_active")
            or signal_data.get("roles_locked")
        )
        layers = signal_data.get("layers") or {}
        existing_stops: List[float] = []
        for stop_k in ("trail_stop", "runner_sl_floor", "soft_tp1_floor", "broker_sl", "stage0_profit_lock_sl"):
            sv = signal_data.get(stop_k)
            if isinstance(sv, numbers.Real) and not isinstance(sv, bool) and float(sv) > 0.0:
                existing_stops.append(round(float(sv), digits))
        for lv in layers.values():
            if isinstance(lv, dict):
                for lsk in ("ratchet_sl", "broker_sl", "be_sl"):
                    sv = lv.get(lsk)
                    if isinstance(sv, numbers.Real) and not isinstance(sv, bool) and float(sv) > 0.0:
                        existing_stops.append(round(float(sv), digits))
                        has_protected_sl = True
        cur_floor: Optional[float] = (
            (max(existing_stops) if is_buy else min(existing_stops))
            if existing_stops
            else None
        )
        eff_sl: Optional[float] = cur_floor if (has_protected_sl and cur_floor is not None) else signal_data.get("sl")
        rejected_sl_reason: Optional[str] = None
        sl_updated = False

        if new_sl is not None and isinstance(new_sl, numbers.Real) and not isinstance(new_sl, bool) and float(new_sl) > 0:
            cand_sl = round(float(new_sl), digits)
            enforce_ratchet = bool(ratchet_sl_only or staged_exit_on or has_protected_sl)
            if enforce_ratchet and has_protected_sl and cur_floor is not None:
                worse_or_equal = (
                    (is_buy and cand_sl <= float(cur_floor) + 1e-9)
                    or (not is_buy and cand_sl >= float(cur_floor) - 1e-9)
                )
                if worse_or_equal:
                    rejected_sl_reason = f"Ratchet guard preserved existing floor ({cur_floor}) over {cand_sl}"

            if rejected_sl_reason is None and not self.dry_run and live_px is not None:
                if (is_buy and cand_sl >= live_px - min_stop_dist) or (not is_buy and cand_sl <= live_px + min_stop_dist):
                    rejected_sl_reason = f"SL {cand_sl} violates live stop distance (live={live_px}, min_dist={min_stop_dist})"

            if rejected_sl_reason is None:
                eff_sl = cand_sl
                sl_updated = True
                signal_data["sl"] = cand_sl
                if is_runner_phase:
                    if staged_exit_on:
                        prev_ts = signal_data.get("trail_stop")
                        if isinstance(prev_ts, numbers.Real) and not isinstance(prev_ts, bool) and float(prev_ts) > 0.0:
                            new_ts = round(
                                max(float(prev_ts), cand_sl) if is_buy else min(float(prev_ts), cand_sl),
                                digits,
                            )
                        else:
                            new_ts = cand_sl
                        signal_data["trail_stop"] = new_ts
                        signal_data["runner_sl_floor"] = new_ts
                        signal_data["soft_tp1_floor"] = new_ts
                        cur_fsm = signal_data.get("fsm_floor")
                        if isinstance(cur_fsm, numbers.Real) and not isinstance(cur_fsm, bool) and float(cur_fsm) > 0.0:
                            signal_data["active_stop_source"] = (
                                "FSM_FLOOR"
                                if (
                                    (is_buy and float(cur_fsm) >= new_ts - 1e-9)
                                    or (not is_buy and float(cur_fsm) <= new_ts + 1e-9)
                                )
                                else "ATR_TRAIL"
                            )
                    else:
                        signal_data["runner_sl_floor"] = cand_sl
                        signal_data["soft_tp1_floor"] = cand_sl

        mod_pos: List[int] = []
        mod_ord: List[int] = []
        market_tickets = [_int_num(t, 0) for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) > 0]
        pending_tickets = [_int_num(t, 0) for t in (signal_data.get("pending_tickets") or []) if _int_num(t, 0) > 0]
        configured_max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    5,
                ),
            ),
        )
        max_stage = self._resolve_effective_max_stage(configured_max_stage, tp_levels)
        signal_data["runner_max_tp_stage"] = configured_max_stage
        signal_data["effective_max_stage"] = max_stage

        if is_runner_phase:
            raw_rts = signal_data.get("runner_tickets") or (
                [signal_data["runner_ticket"]] if signal_data.get("runner_ticket") else []
            )
            runner_tickets = [_int_num(t, 0) for t in raw_rts if _int_num(t, 0) > 0]
            raw_rls = signal_data.get("runner_layers") or (
                [signal_data["runner_layer"]] if signal_data.get("runner_layer") else []
            )
            runner_layers = [str(x) for x in raw_rls if x]

            raw_stage = str(
                signal_data.get("tp_stage") or signal_data.get("runner_milestone_name") or "TP2"
            ).strip().upper()
            if raw_stage == "TP1" and max_stage >= 2:
                raw_stage = "TP2"
            if raw_stage not in tp_levels:
                m_idx_fallback = min(max_stage, max(2, _int_num(signal_data.get("runner_milestone_idx", 2), 2)))
                raw_stage = f"TP{m_idx_fallback}"
            runner_tp = tp_levels.get(raw_stage)
            signal_data["tp_stage"] = raw_stage

            sl_arg = eff_sl if sl_updated else None
            runner_sl_applied = False
            for idx_r, rt in enumerate(runner_tickets):
                lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                if self.modify_position_sltp(rt, sl=sl_arg, tp=runner_tp):
                    if rt not in mod_pos:
                        mod_pos.append(rt)
                    if sl_arg is not None:
                        runner_sl_applied = True
                        if l_info is not None:
                            l_info["sl"] = sl_arg
                    if l_info is not None and runner_tp is not None:
                        l_info["tp"] = runner_tp
                        l_info["tp_stage"] = raw_stage
                elif sl_arg is not None and runner_tp is not None:
                    if self.modify_position_sltp(rt, sl=None, tp=runner_tp):
                        if rt not in mod_pos:
                            mod_pos.append(rt)
                        if l_info is not None:
                            l_info["tp"] = runner_tp
                            l_info["tp_stage"] = raw_stage
            if runner_sl_applied and sl_arg is not None:
                signal_data["broker_sl"] = sl_arg

            runner_set = set(runner_tickets)
            for l_name, l_data in layers.items():
                if not isinstance(l_data, dict):
                    continue
                lt = _int_num(l_data.get("ticket"), 0)
                if lt > 0 and lt in market_tickets and lt not in runner_set:
                    l_stage = str(l_data.get("tp_stage") or "").strip().upper()
                    if l_stage in tp_levels and l_stage != "TP1":
                        l_tp = tp_levels.get(l_stage)
                    elif l_name == "L2" and l_stage == "TP2":
                        l_tp = tp_levels.get("TP2")
                    elif l_name == "L3" and l_stage == "TP3":
                        l_tp = tp_levels.get("TP3")
                    else:
                        l_tp = signal_data.get("final_tp")
                    if self.modify_position_sltp(lt, sl=sl_arg, tp=l_tp):
                        if lt not in mod_pos:
                            mod_pos.append(lt)
                        if sl_arg is not None and sl_updated:
                            l_data["sl"] = sl_arg
                            if has_protected_sl:
                                signal_data["broker_sl"] = sl_arg
                        if l_tp is not None:
                            l_data["tp"] = l_tp

            m_idx = max(1, min(max_stage, _int_num(signal_data.get("runner_milestone_idx", 1), 1)))
            m_target = tp_levels.get(f"TP{m_idx}")
            if m_target is not None and live_px is not None:
                anchor = self._resolve_milestone_anchor(signal_data, tp_levels, m_idx)
                eff_buf, _ = self._compute_proximity_and_pullback(
                    float(m_target), live_px, pip_size, anchor=anchor
                )
                dist = (float(m_target) - live_px) if is_buy else (live_px - float(m_target))
                if dist > eff_buf + 1e-9:
                    signal_data["runner_proximity_armed"] = False
                    signal_data["runner_reversal_ticks"] = 0
        else:
            handled_tkts = set()
            # Never clobber BE / Ratchet / Stage 0 profit-lock SL on TP-only edits (`new_sl is None`)!
            sl_arg = eff_sl if sl_updated else (None if has_protected_sl else signal_data.get("sl"))
            non_runner_sl_applied = False
            for l_data in layers.values():
                if not isinstance(l_data, dict):
                    continue
                lt = _int_num(l_data.get("ticket"), 0)
                if lt <= 0:
                    continue
                l_stage = str(l_data.get("tp_stage") or "TP1").strip().upper()
                l_role = str(l_data.get("role") or "").strip().upper()
                if l_role == "STAGE2_BANKER" or (l_stage == "TP2" and l_role in ("CORE_RUNNER", "SOLO_RUNNER", "STAGE2_BANKER", "")):
                    l_tp = tp_levels.get("TP2") if l_stage == "TP2" else (tp_levels.get(l_stage) if l_stage in tp_levels else signal_data.get("final_tp"))
                elif l_stage in tp_levels and l_stage != "TP1" and l_role in ("CORE_RUNNER", "SOLO_RUNNER"):
                    l_tp = tp_levels.get(l_stage)
                else:
                    l_tp = signal_data.get("final_tp")
                if lt in market_tickets:
                    handled_tkts.add(lt)
                    if self.modify_position_sltp(lt, sl=sl_arg, tp=l_tp):
                        if lt not in mod_pos:
                            mod_pos.append(lt)
                        if sl_arg is not None and sl_updated:
                            l_data["sl"] = sl_arg
                            non_runner_sl_applied = True
                        if l_tp is not None:
                            l_data["tp"] = l_tp
                elif lt in pending_tickets:
                    handled_tkts.add(lt)
                    if self.modify_pending_order_sltp(lt, sl=sl_arg, tp=l_tp):
                        if lt not in mod_ord:
                            mod_ord.append(lt)
                        if sl_arg is not None and sl_updated:
                            l_data["sl"] = sl_arg
                        if l_tp is not None:
                            l_data["tp"] = l_tp

            for mt in market_tickets:
                if mt not in handled_tkts:
                    if self.modify_position_sltp(mt, sl=sl_arg, tp=signal_data.get("final_tp")):
                        if mt not in mod_pos:
                            mod_pos.append(mt)
                        if sl_arg is not None and sl_updated:
                            non_runner_sl_applied = True
            for pt in pending_tickets:
                if pt not in handled_tkts:
                    if self.modify_pending_order_sltp(pt, sl=sl_arg, tp=signal_data.get("final_tp")) and pt not in mod_ord:
                        mod_ord.append(pt)
            if non_runner_sl_applied and sl_arg is not None and sl_updated and has_protected_sl:
                signal_data["broker_sl"] = sl_arg

            tp1_val = tp_levels.get("TP1")
            if tp1_val is not None and live_px is not None:
                sf_arm_dist = float(_cfg("SHORTFALL_ARM_PIPS", SHORTFALL_ARM_PIPS)) * pip_size
                dist_tp1 = (float(tp1_val) - live_px) if is_buy else (live_px - float(tp1_val))
                if dist_tp1 > sf_arm_dist + 1e-9:
                    signal_data["shortfall_armed"] = False
                    signal_data["shortfall_reversal_ticks"] = 0

        return {
            "sl": eff_sl,
            "tp_levels": dict(tp_levels),
            "final_tp": signal_data.get("final_tp"),
            "tp_stage": signal_data.get("tp_stage", "TP1"),
            "modified_positions": mod_pos,
            "modified_orders": mod_ord,
            "rejected_sl_reason": rejected_sl_reason,
        }

    def trail_active_signal(
        self,
        signal_data: Dict[str, Any],
        milestone: Optional[str] = None,
        trail_pips: Optional[float] = None,
        explicit_sl_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        v3.3 On-Demand Active-Trade Trailing & Milestone Stepping:
        - When milestone is TP1..TP4: triggers TP1 scaling handoff if not yet in RUNNER_ACTIVE,
          marks TP1..TP{m} cleared, locks SL floor safely (clamped against live stop distance),
          and steps runner broker TP to the next stage (or closes at ceiling if m >= max_stage).
        - Otherwise trails SL by trail_pips (default TRAIL_DISTANCE_PIPS) or explicit_sl_price.
        """
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(symbol)
        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
        min_stop_dist = stops_lvl * (pt or 0.01)

        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        configured_max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    5,
                ),
            ),
        )
        max_stage = self._resolve_effective_max_stage(configured_max_stage, tp_levels)
        signal_data["runner_max_tp_stage"] = configured_max_stage
        signal_data["effective_max_stage"] = max_stage

        tick = self.get_tick(symbol)
        live_px: Optional[float] = None
        if tick is not None:
            cand_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
            if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                live_px = float(cand_px)

        m_str = str(milestone or "").strip().upper()
        if m_str.isdigit():
            m_str = f"TP{m_str}"

        if m_str in ("TP1", "TP2", "TP3", "TP4", "TP5"):
            m_idx = int(m_str[-1])
            if not signal_data.get("tp1_reached") and signal_data.get("market_tickets"):
                self.handle_tp1_scaling_and_select_runner(signal_data)

            raw_rts = signal_data.get("runner_tickets") or (
                [signal_data["runner_ticket"]] if signal_data.get("runner_ticket") else []
            )
            runner_tickets = [_int_num(t, 0) for t in raw_rts if _int_num(t, 0) > 0]
            raw_rls = signal_data.get("runner_layers") or (
                [signal_data["runner_layer"]] if signal_data.get("runner_layer") else []
            )
            runner_layers = [str(x) for x in raw_rls if x]
            if not runner_tickets and signal_data.get("market_tickets"):
                runner_tickets = [_int_num(t, 0) for t in signal_data["market_tickets"] if _int_num(t, 0) > 0]

            cleared = list(signal_data.get("runner_cleared_milestones") or [])
            for i in range(1, m_idx + 1):
                ck = f"TP{i}"
                if ck not in cleared:
                    cleared.append(ck)
                signal_data[f"tp{i}_reached"] = True
            signal_data["runner_cleared_milestones"] = cleared
            signal_data["tp1_reached"] = True

            if m_idx >= max_stage and runner_tickets:
                staged_prompt_on = bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))
                self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason=f"Target TP{m_idx} Reached (Prompt)",
                    ceiling_stage=m_idx,
                    close_all_market_tickets=staged_prompt_on,
                    exit_reason_code="TP_CEILING" if staged_prompt_on else None,
                    exit_price=live_px if staged_prompt_on else None,
                    exit_stage=min(4, m_idx) if staged_prompt_on else None,
                )
                return {
                    "new_sl": signal_data.get("runner_sl_floor"),
                    "tp_stage": f"TP{m_idx}",
                    "next_tp": tp_levels.get(f"TP{m_idx}"),
                    "cleared_milestones": list(signal_data.get("runner_cleared_milestones") or []),
                    "modified_tickets": [],
                    "banked_tickets": list(signal_data.get("last_banked_tickets") or []),
                    "closed_runner_tickets": list(signal_data.get("last_runner_tickets") or []),
                    "status": signal_data.get("status"),
                }

            next_idx = min(max_stage, m_idx + 1)
            next_stage = f"TP{next_idx}"
            next_tp = tp_levels.get(next_stage)
            signal_data["runner_milestone_idx"] = next_idx
            signal_data["runner_milestone_name"] = next_stage
            signal_data["tp_stage"] = next_stage
            signal_data["runner_proximity_armed"] = False
            signal_data["runner_reversal_ticks"] = 0
            signal_data["tp2_shortfall_armed"] = False
            if runner_tickets:
                signal_data["status"] = "RUNNER_ACTIVE"

            if bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED)):
                new_stage = self.get_runner_fsm_stage(signal_data)
                fsm_floor = self._compute_staged_fsm_floor(
                    signal_data,
                    new_stage,
                    tp_levels,
                    direction,
                    pip_size,
                    digits,
                )
                if fsm_floor is not None:
                    signal_data["fsm_floor"] = fsm_floor

                prev_peak = signal_data.get("trail_peak")
                if not (isinstance(prev_peak, numbers.Real) and not isinstance(prev_peak, bool) and float(prev_peak) > 0.0):
                    prev_peak = signal_data.get("runner_peak_price")
                seed_px = live_px if live_px is not None else tp_levels.get(f"TP{m_idx}")
                if isinstance(prev_peak, numbers.Real) and not isinstance(prev_peak, bool) and float(prev_peak) > 0.0:
                    if seed_px is not None and float(seed_px) > 0.0:
                        trail_peak = round(
                            max(float(prev_peak), float(seed_px)) if is_buy else min(float(prev_peak), float(seed_px)),
                            digits,
                        )
                    else:
                        trail_peak = round(float(prev_peak), digits)
                elif seed_px is not None and float(seed_px) > 0.0:
                    trail_peak = round(float(seed_px), digits)
                else:
                    trail_peak = round(float(self._resolve_signal_primary_entry(signal_data) or 3895.0), digits)
                signal_data["trail_peak"] = trail_peak
                signal_data["runner_peak_price"] = trail_peak

                trail_dist_s, tier_used = self._compute_staged_trail_dist(symbol, new_stage, digits)
                atr_stop = round((trail_peak - trail_dist_s) if is_buy else (trail_peak + trail_dist_s), digits)
                signal_data["trail_dist"] = trail_dist_s
                signal_data["trail_tier_used"] = tier_used
                signal_data["atr_stop"] = atr_stop

                stop_cands: List[float] = [atr_stop]
                if fsm_floor is not None:
                    stop_cands.append(fsm_floor)
                for prev_k in ("trail_stop", "runner_sl_floor", "soft_tp1_floor", "broker_sl"):
                    pv = signal_data.get(prev_k)
                    if isinstance(pv, numbers.Real) and not isinstance(pv, bool) and float(pv) > 0.0:
                        stop_cands.append(round(float(pv), digits))
                active_stop = round(max(stop_cands) if is_buy else min(stop_cands), digits)
                active_stop_source = (
                    "FSM_FLOOR"
                    if (
                        fsm_floor is not None
                        and (
                            (is_buy and fsm_floor >= active_stop - 1e-9)
                            or (not is_buy and fsm_floor <= active_stop + 1e-9)
                        )
                    )
                    else "ATR_TRAIL"
                )
                signal_data["trail_stop"] = active_stop
                signal_data["runner_sl_floor"] = active_stop
                signal_data["soft_tp1_floor"] = active_stop
                signal_data["active_stop_source"] = active_stop_source
                signal_data["sl"] = active_stop

                self._sync_tp1_trail_broker_sl(
                    signal_data,
                    symbol,
                    direction,
                    float(active_stop),
                    digits,
                    tick=tick,
                    sym_info=sym_info,
                    runner_tp=next_tp,
                    runner_stage=next_stage,
                    force_sl_sync=True,
                    fallback_sl=fsm_floor,
                )

                return {
                    "new_sl": active_stop,
                    "tp_stage": next_stage,
                    "next_tp": next_tp,
                    "cleared_milestones": list(cleared),
                    "modified_tickets": list(runner_tickets),
                    "banked_tickets": list(signal_data.get("last_banked_tickets") or []),
                    "status": signal_data.get("status"),
                }

            raw_lock = float(_cfg("TP1_LOCK_BUFFER", TP1_LOCK_BUFFER))
            lock_buf = raw_lock * (pip_size / 0.10) if pip_size > 0 else raw_lock
            trail_dist = float(_cfg("TRAIL_DISTANCE_PIPS", TRAIL_DISTANCE_PIPS)) * pip_size
            existing_floors: List[float] = []
            for fk in ("trail_stop", "runner_sl_floor", "soft_tp1_floor", "broker_sl"):
                fv = signal_data.get(fk)
                if isinstance(fv, numbers.Real) and not isinstance(fv, bool) and float(fv) > 0.0:
                    existing_floors.append(round(float(fv), digits))
            cur_floor = (max(existing_floors) if is_buy else min(existing_floors)) if existing_floors else None

            if m_idx == 1:
                tp1_val = tp_levels.get("TP1")
                if tp1_val is not None:
                    cand_floor = round(float(tp1_val) - lock_buf, digits) if is_buy else round(float(tp1_val) + lock_buf, digits)
                else:
                    cand_floor = self._compute_initial_tp1_floor(signal_data, symbol, direction, pip_size, digits)
                if cur_floor is not None:
                    cand_floor = round(max(float(cur_floor), float(cand_floor)) if is_buy else min(float(cur_floor), float(cand_floor)), digits)
            else:
                prev_tp = tp_levels.get(f"TP{m_idx - 1}")
                cur_m_tp = tp_levels.get(f"TP{m_idx}")
                cands: List[float] = []
                if cur_floor is not None:
                    cands.append(float(cur_floor))
                if prev_tp is not None:
                    cands.append(float(prev_tp))
                if cur_m_tp is not None:
                    cands.append((float(cur_m_tp) - trail_dist) if is_buy else (float(cur_m_tp) + trail_dist))
                if cands:
                    cand_floor = round(max(cands) if is_buy else min(cands), digits)
                else:
                    cand_floor = self._compute_initial_tp1_floor(signal_data, symbol, direction, pip_size, digits)

            if live_px is not None and cand_floor is not None:
                safe_gap = max(min_stop_dist, 2.0 * pip_size)
                if is_buy and cand_floor > live_px - safe_gap:
                    cand_floor = round(live_px - safe_gap, digits)
                elif not is_buy and cand_floor < live_px + safe_gap:
                    cand_floor = round(live_px + safe_gap, digits)

            # Never loosen below existing cur_floor / broker_sl
            if cur_floor is not None and cand_floor is not None:
                cand_floor = round(
                    max(float(cur_floor), float(cand_floor)) if is_buy else min(float(cur_floor), float(cand_floor)),
                    digits,
                )

            cur_bsl = _num(signal_data.get("broker_sl"), 0.0)
            sl_to_send: Optional[float] = cand_floor
            if sl_to_send is not None and cur_bsl > 0.0:
                if (is_buy and sl_to_send <= cur_bsl + 1e-9) or (not is_buy and sl_to_send >= cur_bsl - 1e-9):
                    sl_to_send = None
            if sl_to_send is not None and live_px is not None:
                safe_gap = max(min_stop_dist, 2.0 * pip_size)
                if (is_buy and sl_to_send > live_px - safe_gap + 1e-9) or (not is_buy and sl_to_send < live_px + safe_gap - 1e-9):
                    sl_to_send = None

            signal_data["runner_sl_floor"] = cand_floor
            signal_data["soft_tp1_floor"] = cand_floor
            signal_data["sl"] = cand_floor

            layers = signal_data.get("layers") or {}
            tp_rounded = round(float(next_tp), digits) if next_tp is not None else None
            sl_rounded = round(float(sl_to_send), digits) if sl_to_send is not None else None
            sl_mod_ok = False
            for idx_r, rt in enumerate(runner_tickets):
                lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                already_at_tp = (
                    l_info is not None
                    and tp_rounded is not None
                    and abs(_num(l_info.get("tp"), 0.0) - tp_rounded) < 1e-6
                    and l_info.get("tp_stage") == next_stage
                )
                if sl_rounded is None and already_at_tp:
                    continue
                if self.modify_position_sltp(rt, sl=sl_rounded, tp=tp_rounded):
                    if sl_rounded is not None:
                        sl_mod_ok = True
                        if l_info is not None:
                            l_info["sl"] = sl_rounded
                    if l_info is not None and tp_rounded is not None:
                        l_info["tp"] = tp_rounded
                        l_info["tp_stage"] = next_stage
                elif sl_rounded is not None and tp_rounded is not None and not already_at_tp:
                    if self.modify_position_sltp(rt, sl=None, tp=tp_rounded) and l_info is not None:
                        l_info["tp"] = tp_rounded
                        l_info["tp_stage"] = next_stage
            if sl_mod_ok and sl_rounded is not None:
                signal_data["broker_sl"] = sl_rounded

            return {
                "new_sl": cand_floor,
                "tp_stage": next_stage,
                "next_tp": next_tp,
                "cleared_milestones": list(cleared),
                "modified_tickets": list(runner_tickets),
                "banked_tickets": list(signal_data.get("last_banked_tickets") or []),
                "status": signal_data.get("status"),
            }

        eff_pips = (
            float(trail_pips)
            if (
                trail_pips is not None
                and isinstance(trail_pips, numbers.Real)
                and not isinstance(trail_pips, bool)
                and float(trail_pips) > 0
            )
            else float(_cfg("TRAIL_DISTANCE_PIPS", TRAIL_DISTANCE_PIPS))
        )

        if (
            explicit_sl_price is not None
            and isinstance(explicit_sl_price, numbers.Real)
            and not isinstance(explicit_sl_price, bool)
            and float(explicit_sl_price) > 0
        ):
            cand_sl = round(float(explicit_sl_price), digits)
        else:
            ref_px = (
                live_px
                if live_px is not None
                else float(signal_data.get("recorded_entry_price") or signal_data.get("raw_tp1") or 3895.0)
            )
            cand_sl = round(ref_px - eff_pips * pip_size, digits) if is_buy else round(ref_px + eff_pips * pip_size, digits)

        if live_px is not None:
            if is_buy and (live_px - cand_sl) < min_stop_dist:
                cand_sl = round(live_px - min_stop_dist, digits)
            elif not is_buy and (cand_sl - live_px) < min_stop_dist:
                cand_sl = round(live_px + min_stop_dist, digits)

        raw_rts = signal_data.get("runner_tickets") or (
            [signal_data["runner_ticket"]] if signal_data.get("runner_ticket") else []
        )
        runner_tickets = [_int_num(t, 0) for t in raw_rts if _int_num(t, 0) > 0]
        runner_set = set(runner_tickets)
        is_runner_phase = bool(signal_data.get("status") == "RUNNER_ACTIVE" or signal_data.get("tp1_reached"))
        staged_exit_on = bool(_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))

        if is_runner_phase and staged_exit_on:
            prev_stops: List[float] = []
            for sk in ("trail_stop", "runner_sl_floor", "soft_tp1_floor", "broker_sl"):
                sv = signal_data.get(sk)
                if isinstance(sv, numbers.Real) and not isinstance(sv, bool) and float(sv) > 0.0:
                    prev_stops.append(round(float(sv), digits))
            prev_stop: Optional[float] = (
                (max(prev_stops) if is_buy else min(prev_stops))
                if prev_stops
                else None
            )
            if prev_stop is not None:
                worse_or_equal = (
                    (is_buy and cand_sl <= prev_stop + 1e-9)
                    or (not is_buy and cand_sl >= prev_stop - 1e-9)
                )
                if worse_or_equal:
                    cur_stage = str(signal_data.get("tp_stage") or signal_data.get("runner_milestone_name") or "TP1").upper()
                    return {
                        "new_sl": prev_stop,
                        "tp_stage": cur_stage,
                        "next_tp": tp_levels.get(cur_stage) or signal_data.get("final_tp"),
                        "cleared_milestones": list(signal_data.get("runner_cleared_milestones") or []),
                        "modified_tickets": [],
                        "banked_tickets": list(signal_data.get("last_banked_tickets") or []),
                        "status": signal_data.get("status"),
                    }

        target_tickets = (
            runner_tickets
            if (signal_data.get("status") == "RUNNER_ACTIVE" and runner_tickets)
            else [_int_num(t, 0) for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) > 0]
        )

        mod_tkts: List[int] = []
        for t in target_tickets:
            if self.modify_position_sltp(t, sl=cand_sl, tp=None):
                mod_tkts.append(t)

        signal_data["sl"] = cand_sl
        if is_runner_phase:
            if any(t in runner_set for t in mod_tkts) or (not runner_set and mod_tkts):
                signal_data["broker_sl"] = cand_sl
            if staged_exit_on:
                prev_ts = signal_data.get("trail_stop")
                if isinstance(prev_ts, numbers.Real) and not isinstance(prev_ts, bool) and float(prev_ts) > 0.0:
                    new_ts = round(
                        max(float(prev_ts), cand_sl) if is_buy else min(float(prev_ts), cand_sl),
                        digits,
                    )
                else:
                    new_ts = cand_sl
                signal_data["trail_stop"] = new_ts
                signal_data["runner_sl_floor"] = new_ts
                signal_data["soft_tp1_floor"] = new_ts
                cur_fsm = signal_data.get("fsm_floor")
                if isinstance(cur_fsm, numbers.Real) and not isinstance(cur_fsm, bool) and float(cur_fsm) > 0.0:
                    signal_data["active_stop_source"] = (
                        "FSM_FLOOR"
                        if (
                            (is_buy and float(cur_fsm) >= new_ts - 1e-9)
                            or (not is_buy and float(cur_fsm) <= new_ts + 1e-9)
                        )
                        else "ATR_TRAIL"
                    )
            else:
                signal_data["runner_sl_floor"] = cand_sl
                signal_data["soft_tp1_floor"] = cand_sl
        else:
            if mod_tkts:
                signal_data["broker_sl"] = cand_sl

        cur_stage = str(signal_data.get("tp_stage") or signal_data.get("runner_milestone_name") or "TP1").upper()
        return {
            "new_sl": cand_sl,
            "tp_stage": cur_stage,
            "next_tp": tp_levels.get(cur_stage) or signal_data.get("final_tp"),
            "cleared_milestones": list(signal_data.get("runner_cleared_milestones") or []),
            "modified_tickets": mod_tkts,
            "banked_tickets": list(signal_data.get("last_banked_tickets") or []),
            "status": signal_data.get("status"),
        }

    def _resolve_ticket_current_sl(
        self,
        signal_data: Dict[str, Any],
        ticket: int,
        l_info: Dict[str, Any],
        is_buy: bool,
        digits: int,
    ) -> Optional[float]:
        """Resolve tightest active SL on `ticket` from broker position or tracked layer state."""
        cands: List[float] = []
        if not self.dry_run:
            with self._mt5_lock:
                pos_res = mt5.positions_get(ticket=ticket)
            if pos_res:
                p_sl = _num(getattr(pos_res[0], "sl", 0.0), 0.0)
                if p_sl > 0.0:
                    cands.append(round(p_sl, digits))
        if not cands:
            for key in ("dynamic_atr_trail_sl", "ratchet_sl", "broker_sl", "be_sl", "sl"):
                val = _num(l_info.get(key), 0.0)
                if val > 0.0:
                    cands.append(round(val, digits))
            if not cands:
                sig_sl = _num(signal_data.get("sl"), 0.0)
                if sig_sl > 0.0:
                    cands.append(round(sig_sl, digits))
        if not cands:
            return None
        return max(cands) if is_buy else min(cands)

    def evaluate_dynamic_atr_trailing_stop(
        self,
        signal_data: Dict[str, Any],
        tick: Any = None,
    ) -> Dict[str, Any]:
        """
        Optional Dynamic ATR Trailing Stop (enabled when DYNAMIC_ATR_TRAIL_ENABLED=True).
        Dynamically adjusts per-ticket trailing distance with volatility (ATR), forward-only.
        """
        if not isinstance(signal_data, dict):
            return {"enabled": False, "updated_tickets": []}
        policy = self._get_dynamic_risk_policy()
        if not policy.trail_enabled:
            return {"enabled": False, "updated_tickets": []}

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        if tick is None:
            tick = self.get_tick(symbol)
        if tick is None or not self.is_valid_live_tick(symbol, tick):
            return {"enabled": True, "paused_by_spread": True, "updated_tickets": []}
        is_spike, _, _ = self.check_spread_spike(symbol, tick)
        if is_spike:
            return {"enabled": True, "paused_by_spread": True, "updated_tickets": []}

        atr = self.get_atr(symbol, policy.trail_timeframe, policy.atr_period)
        if atr <= 0.0:
            return {"enabled": True, "paused_by_spread": False, "updated_tickets": [], "atr": 0.0, "timeframe": policy.trail_timeframe}

        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
        raw_dist = max(0.0, stops_lvl * (pt or 0.01))
        safety_buf = float(_cfg("STOP_LEVEL_SAFETY_BUFFER_DOLLARS", STOP_LEVEL_SAFETY_BUFFER_DOLLARS)) if (not self.dry_run or raw_dist > 0.0) else 0.0
        stops_buffer = (raw_dist + max(0.0, safety_buf)) if raw_dist > 0.0 else 0.0

        live_price = float(tick.bid) if is_buy else float(tick.ask)
        layers = signal_data.get("layers") or {}
        prev_meta = signal_data.get("dynamic_atr_trail") if isinstance(signal_data.get("dynamic_atr_trail"), dict) else {}
        was_armed = bool(prev_meta.get("armed"))
        any_armed = False
        updated_tickets: List[Tuple[int, str, float, float, float]] = []
        last_trail_dist = prev_meta.get("trail_dist", 0.0)

        for lk in self._ordered_open_layer_keys(signal_data):
            lv = layers.get(lk)
            t_int = _int_num((lv or {}).get("ticket"), 0)
            if not isinstance(lv, dict) or t_int <= 0:
                continue
            entry_px = self._resolve_layer_entry(signal_data, lk, lv) or 0.0
            if entry_px <= 0.0:
                continue
            cur_sl = self._resolve_ticket_current_sl(signal_data, t_int, lv, is_buy, digits)
            step = compute_dynamic_atr_trail_stop(
                direction=direction,
                entry_price=entry_px,
                live_price=live_price,
                prev_peak=lv.get("dynamic_atr_trail_peak"),
                current_sl=cur_sl,
                atr=atr,
                policy=policy,
                digits=digits,
                stops_buffer=stops_buffer,
            )
            if step.get("peak_price") is not None:
                lv["dynamic_atr_trail_peak"] = step["peak_price"]
            last_trail_dist = step.get("trail_dist") or last_trail_dist
            any_armed = any_armed or bool(step.get("armed"))
            cand_sl = step.get("candidate_sl")
            if not step.get("should_update") or not cand_sl or cand_sl <= 0.0:
                continue
            if self.is_within_freeze_level(symbol, cand_sl, tick=tick, sym_info=sym_info):
                continue
            if not self.modify_position_sltp(t_int, sl=cand_sl, tp=None):
                continue

            lv["sl"] = cand_sl
            lv["broker_sl"] = cand_sl
            lv["dynamic_atr_trail_sl"] = cand_sl
            lv["dynamic_atr_trail_dist"] = step["trail_dist"]
            lv["dynamic_atr_trail_atr"] = step["atr"]
            prev_bsl = _num(signal_data.get("broker_sl"), 0.0)
            if prev_bsl <= 0.0 or (is_buy and cand_sl > prev_bsl) or (not is_buy and cand_sl < prev_bsl):
                signal_data["broker_sl"] = cand_sl
            updated_tickets.append((t_int, lk, cand_sl, step["trail_dist"], step["atr"]))

        if any_armed or updated_tickets:
            last_sl = updated_tickets[-1][2] if updated_tickets else prev_meta.get("last_sl")
            signal_data["dynamic_atr_trail"] = {
                "armed": any_armed,
                "timeframe": policy.trail_timeframe,
                "atr": round(atr, 4),
                "trail_dist": last_trail_dist,
                "last_sl": last_sl,
            }

        return {
            "enabled": True,
            "paused_by_spread": False,
            "atr": round(atr, 4),
            "timeframe": policy.trail_timeframe,
            "updated_tickets": updated_tickets,
            "newly_armed": bool(any_armed and not was_armed),
        }
