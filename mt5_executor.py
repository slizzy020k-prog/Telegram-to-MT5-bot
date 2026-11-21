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
    AUTO_ADJUST_LOTS_TO_BALANCE,
    BANK_ALL_AT_TP1,
    BANK_SINGLE_LAYER_AT_TP1,
    BASE_LAYER_LOTS,
    BE_AFTER_TP1,
    BE_LAYERS,
    BE_OFFSET_PIPS,
    CANCEL_PENDING_ON_BE,
    CANCEL_PENDING_ON_PROFIT_LOCK,
    CANCEL_PENDING_ON_TP1,
    DAILY_LOSS_PAUSE_PCT,
    DAILY_PNL_CACHE_TTL_SECONDS,
    DEFAULT_SYMBOL,
    DEFAULT_TP_STEP_PIPS,
    DYNAMIC_ATR_BASELINE_DOLLARS,
    DYNAMIC_ATR_BE_ACTIVATION_MULT,
    DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS,
    DYNAMIC_ATR_BE_MIN_MFE_DOLLARS,
    DYNAMIC_ATR_PERIOD,
    DYNAMIC_ATR_SLTP_ENABLED,
    DYNAMIC_ATR_TIMEFRAME,
    DYNAMIC_ATR_TP_LIFT_ATR_MULT,
    DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT,
    DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT,
    DYNAMIC_ATR_TP_TRAIL_ENABLED,
    DYNAMIC_ATR_TRAIL_ACTIVATION_MULT,
    DYNAMIC_ATR_TRAIL_ENABLED,
    DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS,
    DYNAMIC_ATR_TRAIL_EXTENDED_MULT,
    DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS,
    DYNAMIC_ATR_TRAIL_HOMERUN_MULT,
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
    HARD_MAX_SPREAD_DOLLARS,
    LAYER_LOTS,
    LAYER_NAMES,
    LEGACY_LAYER_MAP,
    LIFECYCLE_DERISK_SECONDS,
    LIFECYCLE_ENABLED,
    LIFECYCLE_FLATTEN_SECONDS,
    LIFECYCLE_MAX_CLOSE_RETRIES,
    LIFECYCLE_PENDING_EXPIRE_SECONDS,
    LIFECYCLE_SESSION_FLATTEN_AT,
    LIFECYCLE_SESSION_FLATTEN_ENABLED,
    M1_ATR_PERIOD,
    MAGIC_NUMBER,
    MAX_ORDERS_PER_SIGNAL,
    MAX_TP_DISTANCE_DOLLARS,
    MIN_SL_BUFFER_PIPS,
    MIN_VALID_SPREAD_DOLLARS,
    MT5_LOGIN,
    MT5_PASSWORD,
    MT5_PATH,
    MT5_SERVER,
    PENDING_DUPLICATE_ORDER_TOLERANCE,
    PENDING_DUPLICATE_PRICE_TOLERANCE,
    RISK_LOT_MULTIPLIER,
    R_LOCK_ENABLED,
    R_LOCK_MIN_R_DOLLARS,
    R_LOCK_TIERS,
    RUNNER_DEEPEST_COUNT_FULL,
    RUNNER_MAX_TP_STAGE,
    SINGLE_PRICE_CONSOLIDATE_ORDERS,
    STOP_LEVEL_SAFETY_BUFFER_DOLLARS,
    TIERED_STAGE_BANKING_ENABLED,
    TP1_CLOSE_PROXIMITY_PIPS,
    TP1_LOCK_BUFFER,
    TP2_STEP_PIPS,
    TP3_STEP_PIPS,
    TP4_STEP_PIPS,
    TP5_STEP_PIPS,
    TP_FRONT_RUN_OFFSET,
    LIMIT_ENTRY_FRONT_RUN_OFFSET,
    TRAIL_DISTANCE_PIPS,
    TRAIL_STEP_PIPS,
)
from parser import ZoneInfo, compute_zone_4tiers, derive_tp_levels, merge_tp_levels_for_active_signal
from dynamic_risk import (
    DynamicRiskPolicy,
    compute_atr_fallback_sl_distance,
    compute_dynamic_atr_trail_stop,
    compute_dynamic_tp_step,
    read_dynamic_risk_policy,
    scale_sl_tp_by_atr,
)
from risk_lifecycle import (
    ACTION_DERISK,
    ACTION_EXPIRE_PENDING,
    ACTION_FLATTEN,
    ACTION_NONE,
    ACTION_SESSION_FLATTEN,
    LifecyclePolicy,
    compute_r_lock_floor,
    read_lifecycle_policy,
    stamp_fill_times,
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
        "BANK_ALL_AT_TP1",
        "BANK_SINGLE_LAYER_AT_TP1",
        "CANCEL_PENDING_ON_TP1",
        "FALLBACK_SL_PIPS",
        "TP_FRONT_RUN_OFFSET",
        "LIMIT_ENTRY_FRONT_RUN_OFFSET",
        "TP1_CLOSE_PROXIMITY_PIPS",
        "TP1_LOCK_BUFFER",
        "DEFAULT_TP_STEP_PIPS",
        "TP2_STEP_PIPS",
        "TP3_STEP_PIPS",
        "TP4_STEP_PIPS",
        "TP5_STEP_PIPS",
        "TRAIL_DISTANCE_PIPS",
        "TRAIL_STEP_PIPS",
        "M1_ATR_PERIOD",
        "ADAPTIVE_SPREAD_CEILING_DOLLARS",
        "ADAPTIVE_SPREAD_MULTIPLIER",
        "RUNNER_DEEPEST_COUNT_FULL",
        "RUNNER_MAX_TP_STAGE",
        "MIN_SL_BUFFER_PIPS",
        "ENFORCE_HIGH_RISK_LOT_CAP",
        "DAILY_LOSS_PAUSE_PCT",
        "EMERGENCY_CUTOFF_LOSS_PCT",
        "FLOAT_LOSS_FORCE_CLOSE_PCT",
        "DEFAULT_SYMBOL",
        "DAILY_PNL_CACHE_TTL_SECONDS",
        "STOP_LEVEL_SAFETY_BUFFER_DOLLARS",
        "HARD_MAX_SPREAD_DOLLARS",
        "MIN_VALID_SPREAD_DOLLARS",
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
        "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS",
        "DYNAMIC_ATR_TRAIL_EXTENDED_MULT",
        "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS",
        "DYNAMIC_ATR_TRAIL_HOMERUN_MULT",
        "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT",
        "DYNAMIC_ATR_TRAIL_MIN_DOLLARS",
        "DYNAMIC_ATR_TRAIL_MAX_DOLLARS",
        "DYNAMIC_ATR_TRAIL_STEP_DOLLARS",
        "DYNAMIC_ATR_BE_ACTIVATION_MULT",
        "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS",
        "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS",
        "DYNAMIC_ATR_TP_TRAIL_ENABLED",
        "DYNAMIC_ATR_TP_LIFT_ATR_MULT",
        "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT",
        "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT",
        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
        "MT5_PATH",
        "R_LOCK_ENABLED",
        "R_LOCK_TIERS",
        "R_LOCK_MIN_R_DOLLARS",
        "LIFECYCLE_ENABLED",
        "LIFECYCLE_DERISK_SECONDS",
        "LIFECYCLE_FLATTEN_SECONDS",
        "LIFECYCLE_PENDING_EXPIRE_SECONDS",
        "LIFECYCLE_SESSION_FLATTEN_ENABLED",
        "LIFECYCLE_SESSION_FLATTEN_AT",
        "LIFECYCLE_MAX_CLOSE_RETRIES",
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
        # Stop actually sent to the broker per ticket. The `sl` argument of an
        # order is only a REQUEST: when it is None the executor computes a
        # fallback stop and the broker holds that one, so the initial risk R
        # has to be captured from here.
        self._last_order_sls: Dict[int, float] = {}
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
            mt5_path = _cfg("MT5_PATH", MT5_PATH)
            if mt5_path:
                init_kwargs["path"] = mt5_path

            if not mt5.initialize(**init_kwargs):
                err = mt5.last_error()
                logger.error(f"MT5 initialize() failed: {err}")
                return False

            mt5_login = _cfg("MT5_LOGIN", MT5_LOGIN)
            mt5_password = _cfg("MT5_PASSWORD", MT5_PASSWORD)
            mt5_server = _cfg("MT5_SERVER", MT5_SERVER)

            if mt5_login:
                try:
                    login_int = int(mt5_login)
                except (TypeError, ValueError):
                    logger.error(f"Invalid MT5_LOGIN={mt5_login!r}")
                    return False

                cur_acc = mt5.account_info()
                cur_login = _int_num(getattr(cur_acc, "login", None), 0) if cur_acc is not None else 0
                cur_srv = str(getattr(cur_acc, "server", "") or "").strip() if cur_acc is not None else ""
                srv_target = str(mt5_server).strip() if mt5_server else ""
                if cur_login == login_int and (not srv_target or cur_srv == srv_target):
                    logger.info(
                        f"Connected to MT5 active account: {login_int} "
                        f"on {cur_srv or 'N/A'} "
                        f"(Balance: {_num(getattr(cur_acc, 'balance', 0.0))} {getattr(cur_acc, 'currency', '')})"
                    )
                else:
                    login_kwargs: Dict[str, Any] = {"login": login_int}
                    if srv_target:
                        login_kwargs["server"] = srv_target
                    if mt5_password:
                        login_kwargs["password"] = str(mt5_password)

                    logged_in = bool(mt5.login(**login_kwargs))
                    if not logged_in and srv_target and not mt5_password:
                        logged_in = bool(mt5.login(login=login_int))

                    if not logged_in:
                        err = mt5.last_error()
                        logger.error(
                            f"MT5 login failed for account {login_int} ({srv_target or 'saved server'}): {err}"
                        )
                        return False

                    acc_info = mt5.account_info()
                    if acc_info:
                        logger.info(
                            f"MT5 logged in successfully to account {getattr(acc_info, 'login', login_int)} "
                            f"on {getattr(acc_info, 'server', srv_target or 'N/A')} "
                            f"(Balance: {_num(getattr(acc_info, 'balance', 0.0))} {getattr(acc_info, 'currency', '')})"
                        )
                    else:
                        logger.info(
                            f"MT5 logged in successfully to account {login_int} ({srv_target or 'saved server'})"
                        )
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

    def get_last_placed_sl(self, ticket: Optional[int]) -> Optional[float]:
        """Return the stop that was actually sent to the broker for a ticket, if known.

        This is pure bookkeeping: it RECORDS the stop an order was already
        carrying, so the caller can capture the real initial risk R.
        """
        try:
            return self._last_order_sls.get(int(ticket))
        except (TypeError, ValueError):
            return None

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
        Adaptive Spread Spike Filter (`min(HARD_MAX, max(ADAPTIVE_CEIL, mult * median))`):
        - Returns `(is_spike, spread_dollars, max_allowed_spread_dollars)`.
        - Caps eff_max at HARD_MAX_SPREAD_DOLLARS ($1.25) so slow rollover ramps ($0.52 -> $2.60)
          cannot poison the rolling median history.
        - Corrupt zero-spread live ticks (< MIN_VALID_SPREAD_DOLLARS) do not pollute history.
        """
        adapt_ceil = float(_cfg("ADAPTIVE_SPREAD_CEILING_DOLLARS", ADAPTIVE_SPREAD_CEILING_DOLLARS))
        hard_max = float(_cfg("HARD_MAX_SPREAD_DOLLARS", HARD_MAX_SPREAD_DOLLARS))
        min_valid = float(_cfg("MIN_VALID_SPREAD_DOLLARS", MIN_VALID_SPREAD_DOLLARS))
        mult = float(_cfg("ADAPTIVE_SPREAD_MULTIPLIER", ADAPTIVE_SPREAD_MULTIPLIER))
        first_max = round(min(hard_max, adapt_ceil), 4)
        if tick is None:
            return False, 0.0, first_max

        bid = _num(getattr(tick, "bid", 0.0), 0.0)
        ask = _num(getattr(tick, "ask", 0.0), 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return False, 0.0, first_max

        spread_dollars = round(ask - bid, 4)
        if not self.dry_run and spread_dollars + 1e-9 < min_valid:
            return True, spread_dollars, first_max

        sym_key = (symbol or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)).strip()

        with self._closing_lock:
            hist = self._spread_history.setdefault(sym_key, deque(maxlen=30))
            if hist:
                sorted_h = sorted(hist)
                med = sorted_h[len(sorted_h) // 2]
                eff_max = round(min(hard_max, max(adapt_ceil, round(mult * med, 4))), 4)
            else:
                eff_max = first_max

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
            DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS),
            DYNAMIC_ATR_TRAIL_EXTENDED_MULT=_cfg("DYNAMIC_ATR_TRAIL_EXTENDED_MULT", DYNAMIC_ATR_TRAIL_EXTENDED_MULT),
            DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS", DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS),
            DYNAMIC_ATR_TRAIL_HOMERUN_MULT=_cfg("DYNAMIC_ATR_TRAIL_HOMERUN_MULT", DYNAMIC_ATR_TRAIL_HOMERUN_MULT),
            DYNAMIC_ATR_TRAIL_ACTIVATION_MULT=_cfg("DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", DYNAMIC_ATR_TRAIL_ACTIVATION_MULT),
            DYNAMIC_ATR_TRAIL_MIN_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_MIN_DOLLARS", DYNAMIC_ATR_TRAIL_MIN_DOLLARS),
            DYNAMIC_ATR_TRAIL_MAX_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_MAX_DOLLARS", DYNAMIC_ATR_TRAIL_MAX_DOLLARS),
            DYNAMIC_ATR_TRAIL_STEP_DOLLARS=_cfg("DYNAMIC_ATR_TRAIL_STEP_DOLLARS", DYNAMIC_ATR_TRAIL_STEP_DOLLARS),
            DYNAMIC_ATR_BE_ACTIVATION_MULT=_cfg("DYNAMIC_ATR_BE_ACTIVATION_MULT", DYNAMIC_ATR_BE_ACTIVATION_MULT),
            DYNAMIC_ATR_BE_MIN_MFE_DOLLARS=_cfg("DYNAMIC_ATR_BE_MIN_MFE_DOLLARS", DYNAMIC_ATR_BE_MIN_MFE_DOLLARS),
            DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS=_cfg("DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS),
            DYNAMIC_ATR_TP_TRAIL_ENABLED=_cfg("DYNAMIC_ATR_TP_TRAIL_ENABLED", DYNAMIC_ATR_TP_TRAIL_ENABLED),
            DYNAMIC_ATR_TP_LIFT_ATR_MULT=_cfg("DYNAMIC_ATR_TP_LIFT_ATR_MULT", DYNAMIC_ATR_TP_LIFT_ATR_MULT),
            DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT=_cfg("DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT", DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT),
            DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT=_cfg("DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT", DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT),
        )
        return read_dynamic_risk_policy(proxy)

    def _get_lifecycle_policy(self) -> LifecyclePolicy:
        """Resolve the LifecyclePolicy respecting monkeypatches on either config or mt5_executor."""
        proxy = SimpleNamespace(
            R_LOCK_ENABLED=_cfg("R_LOCK_ENABLED", R_LOCK_ENABLED),
            R_LOCK_TIERS=_cfg("R_LOCK_TIERS", R_LOCK_TIERS),
            R_LOCK_MIN_R_DOLLARS=_cfg("R_LOCK_MIN_R_DOLLARS", R_LOCK_MIN_R_DOLLARS),
            LIFECYCLE_ENABLED=_cfg("LIFECYCLE_ENABLED", LIFECYCLE_ENABLED),
            LIFECYCLE_DERISK_SECONDS=_cfg("LIFECYCLE_DERISK_SECONDS", LIFECYCLE_DERISK_SECONDS),
            LIFECYCLE_FLATTEN_SECONDS=_cfg("LIFECYCLE_FLATTEN_SECONDS", LIFECYCLE_FLATTEN_SECONDS),
            LIFECYCLE_PENDING_EXPIRE_SECONDS=_cfg(
                "LIFECYCLE_PENDING_EXPIRE_SECONDS", LIFECYCLE_PENDING_EXPIRE_SECONDS
            ),
            LIFECYCLE_SESSION_FLATTEN_ENABLED=_cfg(
                "LIFECYCLE_SESSION_FLATTEN_ENABLED", LIFECYCLE_SESSION_FLATTEN_ENABLED
            ),
            LIFECYCLE_SESSION_FLATTEN_AT=_cfg(
                "LIFECYCLE_SESSION_FLATTEN_AT", LIFECYCLE_SESSION_FLATTEN_AT
            ),
            LIFECYCLE_MAX_CLOSE_RETRIES=_cfg(
                "LIFECYCLE_MAX_CLOSE_RETRIES", LIFECYCLE_MAX_CLOSE_RETRIES
            ),
        )
        return read_lifecycle_policy(proxy)

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
            # Record the stop that was actually sent to the broker (the `sl`
            # argument is only a request -- the broker may hold a computed
            # fallback stop instead).
            self._last_order_sls[ticket] = float(final_sl)
            if len(self._last_order_sls) > 512:
                self._last_order_sls = dict(list(self._last_order_sls.items())[-256:])
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
            # Record the stop that was actually sent to the broker (see
            # `execute_market_order`): the `sl` argument is only a request.
            self._last_order_sls[ticket] = float(final_sl)
            if len(self._last_order_sls) > 512:
                self._last_order_sls = dict(list(self._last_order_sls.items())[-256:])
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
        # The broker's price precision for this symbol: the captured stop is
        # rounded to it, exactly like the stop that is sent to the broker.
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
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
                # The stop the broker actually holds for this ticket. The
                # `sl` argument is only a REQUEST: when it is None (or 0.0)
                # the executor computes an ATR-based or FALLBACK_SL_PIPS stop
                # and sends THAT to the broker, so the recorded broker stop is
                # the authoritative initial risk R.
                placed_sl = self.get_last_placed_sl(t)
                if placed_sl is not None and float(placed_sl) > 0.0:
                    initial_sl = round(float(placed_sl), digits)
                elif sl is not None and float(sl) > 0.0:
                    initial_sl = round(float(sl), digits)
                else:
                    # A zero stop is not a stop: the executor treats `sl == 0.0` as
                    # "no stop given" and computes a fallback instead, so there is
                    # no initial risk R to record.
                    initial_sl = None
                layer_dict: Dict[str, Any] = {
                    "lot": l_lot,
                    "remaining_lot": l_lot,
                    "target_price": target_px,
                    "tp": l_tp,
                    "tp_stage": "TP1",
                    "ticket": t,
                    "order_type": o_type,
                    "status": status,
                    # The stop the order was ACTUALLY placed with. The `sl`
                    # argument is only a request: when it is None the executor
                    # computes a fallback stop and the broker holds that one, so
                    # the recorded broker stop wins. The trailing engine
                    # overwrites `sl`/`broker_sl`, so the initial risk R must be
                    # captured here to stay available for the R-relative profit
                    # floor.
                    "initial_sl": initial_sl,
                }
                if o_type == "MARKET":
                    # A MARKET layer fills immediately at placement, so this is the
                    # fill time. MARKET layers never go through the PENDING ->
                    # FILLED transition, so without this stamp the first fill of a
                    # normal zone signal (L1 at market, L2-L4 as limits) would
                    # never be recorded.
                    layer_dict["filled_at"] = datetime.now(timezone.utc).isoformat()
                if layer_entry_px is not None:
                    layer_dict["entry_price"] = layer_entry_px
                    layer_dict["entry"] = layer_entry_px
                layers_data[l_name] = layer_dict

        return market_tickets, pending_tickets, layers_data

    def _resolve_zone_raw_tiers(
        self,
        direction: str,
        zone: Any,
        digits: int,
    ) -> Dict[str, float]:
        """Return the raw (pre front-run) tier prices of a zone, keyed L1..L4."""
        direction_norm = (direction or "BUY").strip().upper()
        if hasattr(zone, "get_tiers"):
            return dict(zone.get_tiers(direction_norm))
        if isinstance(zone, dict):
            return dict(compute_zone_4tiers(zone["top"], zone["bottom"], direction_norm, digits=digits))
        return dict(compute_zone_4tiers(zone.top, zone.bottom, direction_norm, digits=digits))

    def _resolve_zone_tiers(
        self,
        symbol: str,
        direction: str,
        zone: Any,
        final_tp: Optional[float],
        raw_tp1: Optional[float],
        pip_size: float,
        digits: int,
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Return (raw_tiers, front_run_tiers) for a zone entry."""
        direction_norm = (direction or "BUY").strip().upper()
        raw_tiers = self._resolve_zone_raw_tiers(direction_norm, zone, digits)
        tiers = {
            k: self._apply_limit_entry_front_run(
                symbol=symbol,
                direction=direction_norm,
                target_price=float(v),
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                pip_size=pip_size,
                digits=digits,
            )
            for k, v in raw_tiers.items()
        }
        return raw_tiers, tiers

    @staticmethod
    def _zone_anchors_from_tiers(
        direction: str,
        raw_tiers: Dict[str, float],
        tiers: Dict[str, float],
    ) -> Tuple[float, float]:
        """Return (sl_anchor, tp_anchor) for ATR-based SL/TP scaling of a zone entry.

        Single source of truth for the zone anchor formula. `sl_anchor` is the extreme of the
        raw AND front-run-shifted tiers; `tp_anchor` is the front-run-shifted L1 tier.
        """
        if (direction or "BUY").strip().upper() == "BUY":
            sl_anchor = min(min(raw_tiers.values()), min(tiers.values()))
        else:
            sl_anchor = max(max(raw_tiers.values()), max(tiers.values()))
        return float(sl_anchor), float(tiers["L1"])

    def compute_zone_atr_anchors(
        self,
        symbol: str,
        direction: str,
        zone: Any,
        final_tp: Optional[float],
        raw_tp1: Optional[float],
        pip_size: float,
        digits: int,
    ) -> Tuple[float, float]:
        """Return (sl_anchor, tp_anchor) for ATR-based SL/TP scaling of a zone entry.

        MUST stay the single source of truth shared by `execute_4layer_zone` (fresh signal)
        and `main._handle_message_edited_unlocked` (stub-to-zone edit). Otherwise an edited
        signal scales its SL/TP differently from an identical fresh signal on the same zone.
        """
        direction_norm = (direction or "BUY").strip().upper()
        raw_tiers = self._resolve_zone_raw_tiers(direction_norm, zone, digits)
        try:
            tiers = {
                k: self._apply_limit_entry_front_run(
                    symbol=symbol,
                    direction=direction_norm,
                    target_price=float(v),
                    final_tp=final_tp,
                    raw_tp1=raw_tp1,
                    pip_size=pip_size,
                    digits=digits,
                )
                for k, v in raw_tiers.items()
            }
        except Exception as exc:  # defensive: tier math must never abort an edit
            logger.warning(
                f"Zone ATR anchors: front-run tier shift failed ({exc}); using raw zone tiers"
            )
            tiers = dict(raw_tiers)
        return self._zone_anchors_from_tiers(direction_norm, raw_tiers, tiers)

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

        raw_tiers, tiers = self._resolve_zone_tiers(
            symbol=resolved_sym,
            direction=direction_norm,
            zone=zone,
            final_tp=final_tp,
            raw_tp1=raw_tp1 or (tp_levels or {}).get("TP1"),
            pip_size=pip_size,
            digits=digits,
        )

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

        sl_anchor, tp_anchor = self._zone_anchors_from_tiers(direction_norm, raw_tiers, tiers)
        sl, layer_tps, _ = self._prepare_dynamic_sltp_inputs(
            symbol=resolved_sym,
            direction=direction_norm,
            sl_anchor=sl_anchor,
            tp_anchor=tp_anchor,
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
                        for stop_k in ("be_sl", "sl", "broker_sl", "dynamic_atr_trail_sl"):
                            prev_v = _num(layer_obj.get(stop_k), 0.0)
                            if prev_v > 0.0:
                                if is_buy and prev_v > calc_sl:
                                    calc_sl = prev_v
                                elif not is_buy and prev_v < calc_sl:
                                    # Only tighten against previous green / BE / trail stops (never clamp above wide red SL)
                                    if stop_k != "sl" or prev_v <= entry_ref + 1e-9:
                                        calc_sl = prev_v
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
                    # This ticket is about to leave `pending_tickets`, so the
                    # 1-second reconciler will never see it again. Stamp the fill
                    # HERE, otherwise the promoted position has no fill time at
                    # all and the trade-age guard falls back to the signal
                    # timestamp -- making a brand-new position look hours old.
                    stamp_fill_times(signal_data, layers[layer_name])
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

        tick = self.get_tick(symbol)
        stops_buffer = self._compute_stops_buffer(sym_info)

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

            ref_price = 0.0
            if tick is not None:
                raw_ref = float(tick.bid) if wp_is_buy else float(tick.ask)
                if math.isfinite(raw_ref) and raw_ref > 0.0:
                    ref_price = raw_ref
            if ref_price <= 0.0:
                ref_price = _num(getattr(wp, "price_current", 0.0), 0.0)

            stops_valid = True
            if ref_price > 0.0:
                if wp_is_buy:
                    stops_valid = (ref_price - new_sl) > 0.0 and (ref_price - new_sl) >= stops_buffer - 1e-9
                else:
                    stops_valid = (new_sl - ref_price) > 0.0 and (new_sl - ref_price) >= stops_buffer - 1e-9
                if stops_valid and tick is not None and self.is_within_freeze_level(symbol, new_sl, tick=tick, sym_info=sym_info):
                    stops_valid = False

            if not stops_valid:
                err_msg = f"Underwater / within stops_level ({stops_buffer:.{digits}f}) for BE SL {new_sl:.{digits}f}"
                failed_be_tickets.append((wp_ticket, err_msg))
                logger.warning(f"Could not move worst entry #{wp_ticket} to BE ({err_msg})")
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

    # --- trade lifecycle actions (age-based de-risk / flatten) -------------

    @staticmethod
    def _lifecycle_tickets(values: Any) -> List[int]:
        """Normalise a ticket collection into positive integers (never raises)."""
        out: List[int] = []
        try:
            items = list(values or [])
        except Exception:
            return out
        for v in items:
            try:
                t = int(v)
            except Exception:
                continue
            if t > 0:
                out.append(t)
        return out

    def _lifecycle_open_tickets(self, signal_data: Dict[str, Any]) -> List[int]:
        """The market tickets this signal believes are open.

        Mirrors the ticket selection of `close_signal_exposure` (pending orders
        excluded) so the flatten can tell "the close did not happen" from
        "there was nothing left to close".
        """
        accounted = {
            _int_num(x, 0)
            for x in (signal_data.get("accounted_tickets") or [])
            if _int_num(x, 0) > 0
        }
        tickets: List[int] = []
        seen: Set[int] = set()

        def _add(val: Any) -> None:
            t = _int_num(val, 0)
            if t > 0 and t not in seen and t not in accounted:
                seen.add(t)
                tickets.append(t)

        for t in (signal_data.get("market_tickets") or []):
            _add(t)
        for t in (signal_data.get("runner_tickets") or []):
            _add(t)
        if signal_data.get("runner_ticket") is not None:
            _add(signal_data.get("runner_ticket"))
        layers = signal_data.get("layers")
        if isinstance(layers, dict):
            for lv in layers.values():
                if not isinstance(lv, dict):
                    continue
                if str(lv.get("status") or "").strip().upper() in ("OPEN", "FILLED", "RUNNER"):
                    _add(lv.get("ticket"))
        return tickets

    def _publish_lifecycle_initial_r(self, signal_data: Dict[str, Any]) -> Optional[float]:
        """Publish the signal's mean initial risk per ounce (`initial_r_per_oz`).

        Only the layers that HOLD a position contribute: a PENDING layer has
        no entry yet, so its planned `target_price` is a planned price and not
        a realised risk. A layer whose risk is zero (entry == initial stop) is
        ignored, and the field is only written when the mean is greater than
        zero -- a zero R would make the ledger's `r_at_exit` meaningless.
        """
        layers = signal_data.get("layers") or {}
        if not isinstance(layers, dict):
            return None
        risks: List[float] = []
        for lv in layers.values():
            if not isinstance(lv, dict):
                continue
            if str(lv.get("status") or "").strip().upper() in ("PENDING", "CANCELLED", "CLOSED"):
                continue
            entry = _num(lv.get("entry_price") or lv.get("entry"), 0.0)
            stop = _num(lv.get("initial_sl"), 0.0)
            if entry <= 0.0 or stop <= 0.0:
                continue
            risk = abs(entry - stop)
            if risk <= 0.0:
                continue
            risks.append(risk)
        if not risks:
            return None
        mean_r = round(sum(risks) / len(risks), 4)
        if mean_r <= 0.0:
            return None
        signal_data["initial_r_per_oz"] = mean_r
        return mean_r

    def apply_lifecycle_derisk(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Cancel unfilled pending limits and move every open layer to breakeven.

        Never loosens an existing stop: a layer whose broker SL is already
        tighter than the breakeven price is left untouched.
        """
        result: Dict[str, Any] = {
            "action": ACTION_DERISK,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
        if not isinstance(signal_data, dict):
            result["ok"] = False
            return result

        try:
            cancelled = self.cancel_signal_pending_orders(signal_data) or []
            result["cancelled"] = self._lifecycle_tickets(cancelled)
        except Exception as exc:
            logger.warning(f"[LIFECYCLE DERISK] cancel pending failed: {exc}")
            result["failed"].append(f"cancel:{exc}")

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        sign = 1.0 if is_buy else -1.0

        sym_info = None
        try:
            sym_info = self.get_symbol_info(symbol)
        except Exception as exc:
            logger.warning(f"[LIFECYCLE DERISK] symbol info for {symbol} failed: {exc}")
            result["failed"].append(f"sym_info:{exc}")
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        offset = max(
            0.0,
            _cfg("DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS),
        )
        layers = signal_data.get("layers") or {}

        # Publish the signal's initial risk (per ounce) so the ledger can record
        # `initial_r_per_oz` and `r_at_exit` at close time.
        self._publish_lifecycle_initial_r(signal_data)

        # The market decides whether the breakeven stop is even a valid stop: a
        # BUY stop above the market (or a SELL stop below it) is rejected by the
        # broker with retcode 10016 (invalid stops). The check runs against a
        # live broker only -- a dry run has nothing that could reject it.
        tick = None
        live_price = 0.0
        price_unavailable = False
        if not self.dry_run:
            try:
                tick = self.get_tick(symbol)
            except Exception as exc:
                logger.warning(f"[LIFECYCLE DERISK] tick for {symbol} failed: {exc}")
                result["failed"].append(f"tick:{exc}")
            if tick is None:
                price_unavailable = True
            else:
                _raw_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
                live_price = _num(_raw_px, 0.0)
                if not (live_price > 0.0):
                    price_unavailable = True
        stops_buffer = self._compute_stops_buffer(sym_info)

        for lk, lv in list(layers.items()):
            if not isinstance(lv, dict):
                continue
            # Only a layer that HOLDS a position can be moved to breakeven. A
            # PENDING layer is an unfilled limit that `cancel_signal_pending_orders`
            # has just cancelled -- modifying it would fail at the broker and
            # would mark an otherwise successful de-risk as failed.
            if str(lv.get("status") or "").strip().upper() in ("PENDING", "CANCELLED", "CLOSED"):
                continue
            ticket = _int_num(lv.get("ticket"), 0)
            if ticket <= 0:
                continue
            entry = _num(lv.get("entry_price") or lv.get("entry") or lv.get("target_price"), 0.0)
            if entry <= 0.0:
                result["failed"].append(f"{lk}:no_entry")
                continue
            be_sl = round(entry + offset if is_buy else entry - offset, digits)
            cur_sl = _num(lv.get("broker_sl") or lv.get("sl"), 0.0)
            if cur_sl > 0.0:
                already_tighter = (cur_sl >= be_sl - 1e-6) if is_buy else (cur_sl <= be_sl + 1e-6)
                if already_tighter:
                    continue
            try:
                # Fail closed: without a usable live price the breakeven stop
                # cannot be validated against the market, so it is NOT sent.
                # The layer keeps its existing (already valid) stop and the
                # next tick tries again.
                if price_unavailable:
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: no live price for {symbol}; "
                        f"the BE stop {be_sl} is not sent and the existing stop is kept."
                    )
                    continue
                # The breakeven stop has to sit on the correct side of the
                # market and far enough away from it, otherwise the broker
                # rejects the modify with retcode 10016 (invalid stops).
                if live_price > 0.0 and sign * (live_price - be_sl) < stops_buffer - 1e-9:
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: BE stop {be_sl} is not a "
                        f"valid stop at the market ({live_price}); the existing stop is kept."
                    )
                    continue
                if self.is_within_freeze_level(symbol, be_sl, tick=tick, sym_info=sym_info):
                    logger.info(
                        f"[LIFECYCLE DERISK] {lk} ticket {ticket}: BE stop {be_sl} is inside the "
                        f"broker freeze level; the existing stop is kept."
                    )
                    continue
                if self.modify_position_sltp(ticket, sl=be_sl, tp=None):
                    lv["sl"] = be_sl
                    lv["broker_sl"] = be_sl
                    result["moved"].append({"layer": lk, "ticket": ticket, "sl": be_sl})
                else:
                    result["failed"].append(f"{lk}:modify_failed")
            except Exception as exc:
                logger.warning(f"[LIFECYCLE DERISK] layer {lk} ticket {ticket} failed: {exc}")
                result["failed"].append(f"{lk}:{exc}")

        result["ok"] = not result["failed"]
        logger.info(
            f"[LIFECYCLE DERISK] moved={len(result['moved'])} cancelled={len(result['cancelled'])} "
            f"failed={len(result['failed'])}"
        )
        return result

    def apply_lifecycle_flatten(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Close every open position at market and cancel every pending limit."""
        result: Dict[str, Any] = {
            "action": ACTION_FLATTEN,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
        if not isinstance(signal_data, dict):
            result["ok"] = False
            return result

        # A signal that is flattened before any de-risk still has to carry
        # its initial risk for the ledger.
        self._publish_lifecycle_initial_r(signal_data)

        max_attempts = max(1, self._get_lifecycle_policy().max_close_retries)
        retries = _int_num(signal_data.get("lifecycle_retry_count"), 0)

        # The retry budget is spent: the guard stops closing and leaves the
        # position to its broker SL (the safe fallback).
        if retries >= max_attempts:
            result["ok"] = False
            result["gave_up"] = True
            result["failed"].append("close:give_up")
            logger.error(
                f"[LIFECYCLE FLATTEN] giving up after {retries} failed attempts "
                f"(max {max_attempts}); the position stays on its broker SL."
            )
            return result

        expected_open = self._lifecycle_open_tickets(signal_data)
        try:
            exposure = self.close_signal_exposure(signal_data) or {}
            result["closed"] = self._lifecycle_tickets(exposure.get("closed_tickets") or [])
            result["cancelled"] = self._lifecycle_tickets(exposure.get("cancelled_orders") or [])
        except Exception as exc:
            logger.warning(f"[LIFECYCLE FLATTEN] close failed: {exc}")
            result["failed"].append(f"close:{exc}")

        if not result["failed"] and expected_open:
            # A close that left the position open is a FAILURE: the guard has to
            # retry (and eventually give up) instead of claiming a flatten that
            # never happened.
            closed_set = set(result["closed"])
            live_ids: Set[int] = set()
            query_ok = True
            try:
                live_positions = self.get_live_positions() or []
                # `get_live_positions` records whether the broker query really
                # succeeded. A FAILED query returns an empty list, which is
                # indistinguishable from "the account has no positions" unless
                # the flag is read.
                query_ok = bool(self._last_live_pos_query_ok)
                live_ids = {
                    _int_num(getattr(p, "ticket", 0), 0)
                    for p in live_positions
                }
            except Exception as exc:
                logger.warning(f"[LIFECYCLE FLATTEN] live position check failed: {exc}")
                live_ids = set()
                query_ok = False
            unverified = [t for t in expected_open if t not in closed_set]
            if unverified and not self.dry_run and not query_ok:
                # The broker could not be asked, so the close cannot be
                # verified: an unverified close is a FAILED close. The retry
                # counter must never be reset on it.
                logger.warning(
                    f"[LIFECYCLE FLATTEN] the live position query failed; the close of "
                    f"{','.join(str(t) for t in unverified)} could not be verified."
                )
                result["failed"].append("close:unverified")
            still_open = [t for t in expected_open if t not in closed_set and t in live_ids]
            if still_open:
                result["failed"].append(
                    "close:still_open:" + ",".join(str(t) for t in still_open)
                )

        if result["failed"]:
            result["ok"] = False
            retries += 1
            signal_data["lifecycle_retry_count"] = retries
            if retries >= max_attempts:
                logger.error(
                    f"[LIFECYCLE FLATTEN] giving up after {retries} failed attempts "
                    f"(max {max_attempts}); the position stays on its broker SL."
                )
                result["gave_up"] = True
        else:
            signal_data["lifecycle_retry_count"] = 0

        logger.info(
            f"[LIFECYCLE FLATTEN] closed={len(result['closed'])} cancelled={len(result['cancelled'])} "
            f"failed={len(result['failed'])}"
        )
        return result

    def apply_lifecycle_pending_expire(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Cancel the unfilled pending limits of a signal that never filled."""
        result: Dict[str, Any] = {
            "action": ACTION_EXPIRE_PENDING,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }
        if not isinstance(signal_data, dict):
            result["ok"] = False
            return result
        try:
            cancelled = self.cancel_signal_pending_orders(signal_data) or []
            result["cancelled"] = self._lifecycle_tickets(cancelled)
        except Exception as exc:
            logger.warning(f"[LIFECYCLE EXPIRE_PENDING] cancel failed: {exc}")
            result["failed"].append(f"cancel:{exc}")
            result["ok"] = False
        logger.info(f"[LIFECYCLE EXPIRE_PENDING] cancelled={len(result['cancelled'])}")
        return result

    def apply_lifecycle_action(self, signal_data: Dict[str, Any], action: str) -> Dict[str, Any]:
        """Dispatch a lifecycle action. Unknown actions are a no-op."""
        token = str(action or "").strip().upper()
        if token == ACTION_DERISK:
            return self.apply_lifecycle_derisk(signal_data)
        if token in (ACTION_FLATTEN, ACTION_SESSION_FLATTEN):
            return self.apply_lifecycle_flatten(signal_data)
        if token == ACTION_EXPIRE_PENDING:
            return self.apply_lifecycle_pending_expire(signal_data)
        return {
            "action": ACTION_NONE,
            "ok": True,
            "moved": [],
            "closed": [],
            "cancelled": [],
            "failed": [],
        }

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

    def _compute_stops_buffer(self, sym_info: Any) -> float:
        """Return broker `trade_stops_level * point + STOP_LEVEL_SAFETY_BUFFER_DOLLARS` when stops > 0."""
        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
        raw_dist = max(0.0, stops_lvl * (pt or 0.01))
        if raw_dist <= 0.0:
            return 0.0
        safety_buf = float(_cfg("STOP_LEVEL_SAFETY_BUFFER_DOLLARS", STOP_LEVEL_SAFETY_BUFFER_DOLLARS)) if (not self.dry_run or raw_dist > 0.0) else 0.0
        return raw_dist + max(0.0, safety_buf)

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
        min_stop_dist = self._compute_stops_buffer(sym_info)
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

    @staticmethod
    def get_runner_fsm_stage(signal_data: Optional[Dict[str, Any]]) -> int:
        """Return canonical runner stage (0..4) for signal_data."""
        return _get_runner_fsm_stage(signal_data)

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
                dt_meta = signal_data.get("dynamic_atr_trail")
                if isinstance(dt_meta, dict) and dt_meta:
                    tier_used = dict(dt_meta)
                else:
                    pol = self._get_dynamic_risk_policy()
                    atr_v = self.get_atr(symbol, pol.trail_timeframe, pol.atr_period)
                    t_dist = (
                        round(max(pol.trail_min_dollars, min(pol.trail_max_dollars, atr_v * pol.trail_atr_mult)), digits)
                        if atr_v > 0.0
                        else round(pol.trail_min_dollars, digits)
                    )
                    tier_used = {
                        "stage": stage_val,
                        "timeframe": pol.trail_timeframe,
                        "atr_mult": round(float(pol.trail_atr_mult), 4),
                        "min_dollars": round(float(pol.trail_min_dollars), 4),
                        "max_dollars": round(float(pol.trail_max_dollars), 4),
                        "atr": round(float(atr_v), 4),
                        "trail_dist": float(t_dist),
                    }

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
        Assign sticky roles across open market layers (`SOLO_RUNNER`, `BANK_TP1`,
        `STAGE2_BANKER`, `CORE_RUNNER`) or assign `LATE_RECOVERY` to late race fills
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
            any_be_locked = bool(
                signal_data.get("be_armed")
                or str(signal_data.get("status") or "").strip().upper() == "BE_APPLIED"
                or any(isinstance(layers.get(k), dict) and layers[k].get("be_locked") for k in open_keys)
            )
            tp1_cleared = bool(
                signal_data.get("tp1_reached")
                or "TP1" in {str(x).strip().upper() for x in (signal_data.get("runner_cleared_milestones") or [])}
            )
            # roles lock only on BE-lock or TP1-clear, never on a pre-BE TP lift.
            if any_be_locked or tp1_cleared:
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
                elif r not in ("SOLO_RUNNER", "BANK_TP1", "STAGE2_BANKER", "CORE_RUNNER", "LATE_RECOVERY"):
                    lv["role"] = "BANK_TP1"
                    r = "BANK_TP1"
                assigned[lk] = r
        else:
            n = len(open_keys)
            if n == 1:
                assigned[open_keys[0]] = "SOLO_RUNNER"
            elif n in (2, 3):
                for early_k in open_keys[:-1]:
                    assigned[early_k] = "BANK_TP1"
                assigned[open_keys[-1]] = "CORE_RUNNER"
            else:
                for mid_k in open_keys[:-2]:
                    assigned[mid_k] = "BANK_TP1"
                assigned[open_keys[-2]] = "STAGE2_BANKER" if tiered_on else "BANK_TP1"
                assigned[open_keys[-1]] = "CORE_RUNNER"
            for lk, role_val in assigned.items():
                layers[lk]["role"] = role_val

        r_layers = [lk for lk in open_keys if assigned.get(lk) in ("SOLO_RUNNER", "CORE_RUNNER")]
        if not r_layers and open_keys:
            non_bank = [lk for lk in open_keys if assigned.get(lk) != "BANK_TP1"]
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
        Idempotently hydrate active or recently filled layers (including pre-existing live positions
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
                    # First fill of this signal: start the position-age clock used
                    # by the trade-age guard (`risk_lifecycle.evaluate_trade_age`).
                    if stamp_fill_times(signal_data, lv):
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
                            if not lv.get("be_locked"):
                                lv["be_locked"] = True
                                changed = True
                            lock_dol = (r_psl - cur_entry) if is_buy else (cur_entry - r_psl)
                            if lock_dol >= 0.65 - 1e-6:
                                prev_dsl = _num(lv.get("dynamic_atr_trail_sl"), 0.0)
                                if prev_dsl <= 0.0 or (is_buy and r_psl > prev_dsl + 1e-6) or (not is_buy and r_psl < prev_dsl - 1e-6):
                                    lv["dynamic_atr_trail_sl"] = r_psl
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

    def prepare_deepest_runners_for_tp_breakout(
        self,
        signal_data: Dict[str, Any],
        live_price: Optional[float] = None,
        chart_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Delegate breakout preparation cleanly to the unified Dynamic ATR Engine."""
        if not isinstance(signal_data, dict):
            return {
                "runner_layers": [],
                "early_layers": [],
                "runner_tickets": [],
                "lifted_tickets": [],
                "reset_tickets": [],
                "engine_result": {},
            }
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        tick_obj: Any = None
        if live_price is not None:
            sym_info = self.get_symbol_info(symbol)
            digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
            if is_buy:
                b_px = float(live_price)
                a_px = round(b_px + 0.20, digits)
            else:
                a_px = float(live_price)
                b_px = float(chart_price) if (chart_price is not None and float(chart_price) < a_px) else round(a_px - 0.20, digits)
            tick_obj = SimpleNamespace(bid=b_px, ask=a_px)

        dt_res = self.evaluate_dynamic_atr_engine(signal_data, tick=tick_obj)
        open_keys = self._ordered_open_layer_keys(signal_data)
        runner_layers = list(signal_data.get("runner_layers") or [])
        early_layers = [lk for lk in open_keys if lk not in runner_layers]
        runner_tickets = list(signal_data.get("runner_tickets") or [])
        lifted_tickets = [t for t, *_ in (dt_res.get("tp_lifted_tickets") or [])]
        return {
            "runner_layers": runner_layers,
            "early_layers": early_layers,
            "runner_tickets": runner_tickets,
            "lifted_tickets": lifted_tickets,
            "reset_tickets": [],
            "engine_result": dt_res,
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

    def handle_tp1_scaling_and_select_runner(self, signal_data: Dict[str, Any]) -> Optional[int]:
        """
        Dynamic Multi-Layer TP1 Scaling & Pure Dynamic ATR Engine Initialization:
        1. Cancels unfilled pending_tickets when CANCEL_PENDING_ON_TP1=True.
        2. Assigns/preserves sticky roles (`SOLO_RUNNER`, `BANK_TP1`, `STAGE2_BANKER`, `CORE_RUNNER`).
        3. Optional `SOLO_RUNNER` partial close at TP1 when volume >= 2 * volume_min.
        4. Closes early `BANK_TP1` layers (subject to `BANK_ALL_AT_TP1` / `BANK_SINGLE_LAYER_AT_TP1`),
           locks `roles_locked=True`, lifts `CORE_RUNNER`/`SOLO_RUNNER`/`STAGE2_BANKER` broker TP to
           `TP2` and locks BE (`entry ± policy.be_lock_offset_dollars`) respecting broker stops &
           freeze levels, marks `tp1_reached=True` & `runner_cleared_milestones=["TP1"]`, and
           executes one `evaluate_dynamic_atr_engine` pass.
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
        sign = 1.0 if is_buy else -1.0
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        market_tickets = list(signal_data.get("market_tickets") or [])
        tiered_on = bool(_cfg("TIERED_STAGE_BANKING_ENABLED", TIERED_STAGE_BANKING_ENABLED))

        # 1. Pending order cancellation policy
        if _cfg("CANCEL_PENDING_ON_TP1", CANCEL_PENDING_ON_TP1):
            self.cancel_signal_pending_orders(signal_data)
            signal_data["pending_tickets"] = []
            for l_val in layers.values():
                if isinstance(l_val, dict) and str(l_val.get("status") or "").strip().upper() == "PENDING":
                    l_val["status"] = "CANCELLED"

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
        bank_all_tp1 = bool(_cfg("BANK_ALL_AT_TP1", BANK_ALL_AT_TP1)) or (max_stage <= 1)
        bank_single_tp1 = bool(_cfg("BANK_SINGLE_LAYER_AT_TP1", BANK_SINGLE_LAYER_AT_TP1))

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

        # Keep STAGE2_BANKER open past TP1 to aim for TP2 when Tiered Banking is enabled
        stage2_bankers: List[Tuple[str, int]] = []
        if tiered_on and not bank_all_tp1 and not (bank_single_tp1 and len(market_tickets) == 1) and max_stage >= 2:
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

        # N=1 SOLO_RUNNER partial close at TP1 when volume >= 2 * volume_min
        if len(runner_layers) == 1 and not early_layers and not bank_all_tp1 and not bank_single_tp1 and max_stage >= 2:
            solo_lk = runner_layers[0]
            solo_info = layers.get(solo_lk) if isinstance(layers.get(solo_lk), dict) else None
            solo_tk = _int_num(runner_tickets[0], 0) if runner_tickets else 0
            if (
                solo_info is not None
                and str(roles.get(solo_lk) or solo_info.get("role") or "").strip().upper() == "SOLO_RUNNER"
                and not solo_info.get("solo_partial_done")
                and solo_tk > 0
            ):
                vol_min, _, _, dec = _extract_symbol_volume_limits(sym_info)
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

        # 3. Close early_layers at TP1
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

        # Roles lock here only because this is the TP1 breakout path: the handler marks
        # TP1 cleared (tp1_reached / runner_cleared_milestones) in the same call chain, which
        # satisfies the "roles lock only on BE-lock or TP1-clear" rule.
        if banked_tickets or runner_tickets or stage2_bankers:
            signal_data["roles_locked"] = True

        surviving_tickets = [mt for mt in market_tickets if mt and mt not in banked_tickets]
        runner_ticket: Optional[int] = runner_tickets[-1] if runner_tickets else None
        runner_layer: Optional[str] = runner_layers[-1] if runner_layers else None

        # 4. Lift CORE_RUNNER / SOLO_RUNNER / STAGE2_BANKER TP to TP2 and lock initial BE SL
        tick = self.get_tick(symbol)
        live_price: Optional[float] = None
        if tick is not None and self.is_valid_live_tick(symbol, tick):
            cand_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
            if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                live_price = float(cand_px)

        policy = self._get_dynamic_risk_policy()
        be_lock_offset = max(0.0, float(policy.be_lock_offset_dollars))
        stops_buffer = self._compute_stops_buffer(sym_info)
        tp2_val = round(float(tp_levels["TP2"]), digits) if (max_stage >= 2 and tp_levels.get("TP2") is not None) else None
        primary_entry = self._resolve_signal_primary_entry(signal_data)

        surviving_entries: List[Tuple[Optional[str], int, Optional[Dict[str, Any]]]] = []
        seen_surv: Set[int] = set()
        for idx_r, rt in enumerate(runner_tickets):
            lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
            l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
            if rt not in seen_surv:
                seen_surv.add(rt)
                surviving_entries.append((lk, rt, l_info))
        for s2_lk, s2_tk in stage2_bankers:
            s2_info = layers.get(s2_lk) if isinstance(layers.get(s2_lk), dict) else None
            if s2_tk not in seen_surv:
                seen_surv.add(s2_tk)
                surviving_entries.append((s2_lk, s2_tk, s2_info))

        sl_applied_tickets: Set[int] = set()
        be_after_tp1 = bool(_cfg("BE_AFTER_TP1", BE_AFTER_TP1))
        for lk, rt, l_info in surviving_entries:
            role = str(
                roles.get(lk or "")
                or (l_info.get("role") if isinstance(l_info, dict) else "")
                or ("SOLO_RUNNER" if len(surviving_tickets) == 1 else "CORE_RUNNER")
            ).strip().upper()
            l_entry = (self._resolve_layer_entry(signal_data, lk, l_info) if l_info is not None else None) or primary_entry
            if not self.dry_run and not l_entry:
                with self._mt5_lock:
                    r_pos = mt5.positions_get(ticket=rt)
                if r_pos:
                    l_entry = _num(getattr(r_pos[0], "price_open", 0.0), 0.0) or l_entry

            cur_sl = self._resolve_ticket_current_sl(signal_data, rt, l_info or {}, is_buy, digits)
            cur_tp: Optional[float] = None
            if not self.dry_run:
                with self._mt5_lock:
                    p_res = mt5.positions_get(ticket=rt)
                if p_res:
                    cur_tp_v = _num(getattr(p_res[0], "tp", 0.0), 0.0)
                    if cur_tp_v > 0.0:
                        cur_tp = round(cur_tp_v, digits)
            if cur_tp is None and isinstance(l_info, dict):
                cur_tp_v = _num(l_info.get("tp"), 0.0)
                if cur_tp_v > 0.0:
                    cur_tp = round(cur_tp_v, digits)

            desired_sl: Optional[float] = None
            if be_after_tp1 and l_entry and l_entry > 0.0:
                cand_be = round(l_entry + sign * be_lock_offset, digits)
                if cur_sl is None or cur_sl <= 0.0 or sign * (cand_be - cur_sl) > 1e-9:
                    if live_price is None or sign * (live_price - cand_be) >= stops_buffer - 1e-9:
                        sl_freeze_ok = (
                            tick is None
                            or not self.is_within_freeze_level(symbol, cand_be, tick=tick, sym_info=sym_info)
                        )
                        if sl_freeze_ok and cur_sl is not None and cur_sl > 0.0 and tick is not None:
                            sl_freeze_ok = not self.is_within_freeze_level(symbol, cur_sl, tick=tick, sym_info=sym_info)
                        if sl_freeze_ok:
                            desired_sl = cand_be

            desired_tp: Optional[float] = None
            if tp2_val is not None and role in ("CORE_RUNNER", "SOLO_RUNNER", "STAGE2_BANKER"):
                if cur_tp is None or abs(cur_tp - tp2_val) >= 1e-6:
                    if live_price is None or sign * (tp2_val - live_price) >= stops_buffer - 1e-9:
                        tp_freeze_ok = (
                            tick is None
                            or not self.is_within_freeze_level(symbol, tp2_val, tick=tick, sym_info=sym_info)
                        )
                        if tp_freeze_ok and cur_tp is not None and cur_tp > 0.0 and tick is not None:
                            tp_freeze_ok = not self.is_within_freeze_level(symbol, cur_tp, tick=tick, sym_info=sym_info)
                        if tp_freeze_ok:
                            desired_tp = tp2_val

            if desired_sl is not None or desired_tp is not None:
                if self.modify_position_sltp(rt, sl=desired_sl, tp=desired_tp):
                    if desired_sl is not None:
                        sl_applied_tickets.add(rt)
                        if isinstance(l_info, dict):
                            l_info["sl"] = desired_sl
                            l_info["broker_sl"] = desired_sl
                            l_info["be_sl"] = desired_sl
                            l_info["be_locked"] = True
                        prev_bsl = _num(signal_data.get("broker_sl"), 0.0)
                        if prev_bsl <= 0.0 or sign * (desired_sl - prev_bsl) > 1e-9:
                            signal_data["broker_sl"] = desired_sl
                            signal_data["trail_stop"] = desired_sl
                            signal_data["runner_sl_floor"] = desired_sl
                            signal_data["soft_tp1_floor"] = desired_sl
                            signal_data["sl"] = desired_sl
                    if desired_tp is not None and isinstance(l_info, dict):
                        l_info["tp"] = desired_tp
                        l_info["tp_stage"] = "TP2"

            if isinstance(l_info, dict) and rt in runner_tickets:
                l_info["status"] = "RUNNER"

        # 5. Update canonical signal state & trigger one Dynamic ATR Engine pass
        cleared = list(signal_data.get("runner_cleared_milestones") or [])
        if "TP1" not in cleared:
            cleared.append("TP1")

        next_stage_idx = 2 if (max_stage >= 2 and tp2_val is not None) else 1
        cur_m_idx = _int_num(signal_data.get("runner_milestone_idx"), 1)
        eff_m_idx = max(cur_m_idx, next_stage_idx) if runner_tickets else 1
        eff_m_name = f"TP{eff_m_idx}"

        signal_data["last_banked_tickets"] = banked_tickets
        signal_data["runner_layers"] = list(runner_layers)
        signal_data["runner_tickets"] = list(runner_tickets)
        signal_data["runner_layer"] = runner_layer
        signal_data["runner_ticket"] = runner_ticket
        if runner_layers:
            signal_data["last_runner_layers"] = list(runner_layers)
            signal_data["last_runner_layer"] = runner_layer
        if runner_tickets:
            signal_data["last_runner_tickets"] = list(runner_tickets)
            signal_data["last_runner_ticket"] = runner_ticket
        signal_data["tp_stage"] = eff_m_name if runner_tickets else "TP1"
        signal_data["runner_max_tp_stage"] = configured_max_stage
        signal_data["effective_max_stage"] = max_stage
        signal_data["runner_milestone_idx"] = eff_m_idx
        signal_data["runner_milestone_name"] = eff_m_name
        signal_data["runner_cleared_milestones"] = cleared
        signal_data["tp1_reached"] = True
        signal_data["tp1_touched"] = True
        signal_data["market_tickets"] = surviving_tickets
        if live_price is not None:
            prev_pk_px = signal_data.get("runner_peak_price") or signal_data.get("trail_peak")
            if isinstance(prev_pk_px, numbers.Real) and not isinstance(prev_pk_px, bool) and float(prev_pk_px) > 0.0:
                new_pk = round(max(float(prev_pk_px), live_price) if is_buy else min(float(prev_pk_px), live_price), digits)
            else:
                new_pk = round(live_price, digits)
            signal_data["runner_peak_price"] = new_pk
            signal_data["trail_peak"] = new_pk

        if runner_tickets:
            signal_data["status"] = "RUNNER_ACTIVE"
        elif surviving_tickets:
            signal_data["status"] = "BE_APPLIED" if be_after_tp1 else "ACTIVE"
        elif signal_data.get("pending_tickets"):
            signal_data["status"] = "ACTIVE"
        else:
            signal_data["status"] = "CLOSED"

        if surviving_tickets and tick is not None:
            dt_res = self.evaluate_dynamic_atr_engine(signal_data, tick=tick)
            signal_data["_last_dynamic_atr_events"] = list(dt_res.get("events") or [])
            signal_data["_last_dynamic_atr_result"] = dt_res

        return runner_ticket

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
        Pure Dynamic ATR Runner Engine Manager:
        - Live mode: verifies broker positions, detects full/partial broker SL/TP closures and
          updates `_cached_pos_profits` + `_finalize_runner_exit(..., close_position=False)`.
        - Delegates all BE locks, Milestone Floors, Dynamic ATR Trail SL, Dynamic Trailing TP,
          and pending sweeps to `evaluate_dynamic_atr_engine` — ZERO virtual closes.
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

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        p_symbol = symbol
        layers = signal_data.get("layers") or {}

        if not self.dry_run:
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
                close_info = self.get_position_close_info(runner_tickets[-1])
                raw_rc = str(close_info.get("reason", "UNKNOWN")).strip().upper()
                reason_label_map = {
                    "TP": "Broker Take Profit Hit",
                    "SL": "Trailed Stop Loss Hit (Locked in Gain)",
                    "MANUAL/BOT": "Closed Manually / from MT5",
                }
                sl_code = "BROKER_SL"
                for rl_k in runner_layers:
                    lv_c = layers.get(rl_k) if isinstance(layers.get(rl_k), dict) else {}
                    if _num(lv_c.get("dynamic_atr_trail_sl"), 0.0) > 0.0:
                        sl_code = "DYNAMIC_ATR_TRAIL_SL"
                        break
                    if lv_c.get("be_locked") or _num(lv_c.get("be_sl"), 0.0) > 0.0:
                        sl_code = "BE_SL"
                code_map = {
                    "TP": "TP_CEILING",
                    "SL": sl_code,
                    "MANUAL/BOT": "MANUAL",
                }
                ext_reason = reason_label_map.get(raw_rc, "Trailed Stop Loss Hit (Locked in Gain)")
                ext_code = code_map.get(raw_rc, sl_code)
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
                    exit_reason_code=ext_code,
                    exit_price=bsl if (ext_code in ("BROKER_SL", "BE_SL", "DYNAMIC_ATR_TRAIL_SL") and bsl > 0.0) else None,
                    exit_stage=self.get_runner_fsm_stage(signal_data),
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
            raw_sym = getattr(live_positions_map[primary_ticket], "symbol", None)
            if isinstance(raw_sym, str) and raw_sym:
                p_symbol = raw_sym

        tick = self.get_tick(p_symbol)
        if not tick:
            return True

        dt_res = self.evaluate_dynamic_atr_engine(signal_data, tick=tick)
        signal_data["_last_dynamic_atr_events"] = list(dt_res.get("events") or [])
        signal_data["_last_dynamic_atr_result"] = dt_res
        return True

    def _manage_legacy_runner(self, signal_data: Dict[str, Any]) -> bool:
        """Legacy 3-layer runner manager delegating broker trailing to Pure Dynamic ATR Engine."""
        ticket = signal_data.get("runner_ticket")
        if not ticket:
            return False

        signal_data["last_runner_ticket"] = ticket
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        p_symbol = symbol

        if not self.dry_run:
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
            raw_sym = getattr(pos, "symbol", None)
            if isinstance(raw_sym, str) and raw_sym:
                p_symbol = raw_sym

        tick = self.get_tick(p_symbol)
        if not tick:
            return True

        dt_res = self.evaluate_dynamic_atr_engine(signal_data, tick=tick)
        signal_data["_last_dynamic_atr_events"] = list(dt_res.get("events") or [])
        signal_data["_last_dynamic_atr_result"] = dt_res
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
            # Route through the single protected merge so a TP edit can never push a
            # live take-profit backwards and the ladder always stays strictly ordered.
            # The previous inline merge overwrote stages directly and promoted unlabelled
            # assignments to "labelled", which bypassed the monotonicity repair and the
            # never-loosen guard (Qwen review CRITICAL 1).
            tp_levels = merge_tp_levels_for_active_signal(
                tp_levels,
                clean_labeled,
                clean_unlabeled,
                direction=direction,
                pip_size=pip_size,
            )

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

        is_runner_phase = bool(signal_data.get("status") == "RUNNER_ACTIVE" or signal_data.get("tp1_reached"))
        layers = signal_data.get("layers") or {}
        has_protected_sl = bool(
            is_runner_phase
            or signal_data.get("be_armed")
            or str(signal_data.get("status") or "").strip().upper() == "BE_APPLIED"
            or signal_data.get("roles_locked")
            or any(
                isinstance(lv, dict)
                and (
                    lv.get("be_locked")
                    or (isinstance(lv.get("dynamic_atr_trail_sl"), numbers.Real) and not isinstance(lv.get("dynamic_atr_trail_sl"), bool) and float(lv.get("dynamic_atr_trail_sl")) > 0.0)
                    or (isinstance(lv.get("be_sl"), numbers.Real) and not isinstance(lv.get("be_sl"), bool) and float(lv.get("be_sl")) > 0.0)
                )
                for lv in layers.values()
            )
        )
        for stop_k in ("trail_stop", "runner_sl_floor", "soft_tp1_floor"):
            sv = signal_data.get(stop_k)
            if isinstance(sv, numbers.Real) and not isinstance(sv, bool) and float(sv) > 0.0:
                has_protected_sl = True

        existing_stops: List[float] = []
        if has_protected_sl:
            for stop_k in ("trail_stop", "runner_sl_floor", "soft_tp1_floor", "broker_sl"):
                sv = signal_data.get(stop_k)
                if isinstance(sv, numbers.Real) and not isinstance(sv, bool) and float(sv) > 0.0:
                    existing_stops.append(round(float(sv), digits))
            for lv in layers.values():
                if isinstance(lv, dict):
                    for lsk in ("dynamic_atr_trail_sl", "be_sl", "broker_sl"):
                        sv = lv.get(lsk)
                        if isinstance(sv, numbers.Real) and not isinstance(sv, bool) and float(sv) > 0.0:
                            existing_stops.append(round(float(sv), digits))
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
            enforce_ratchet = bool(ratchet_sl_only or has_protected_sl)
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
                            "MILESTONE_FLOOR"
                            if (
                                (is_buy and float(cur_fsm) >= new_ts - 1e-9)
                                or (not is_buy and float(cur_fsm) <= new_ts + 1e-9)
                            )
                            else "ATR_TRAIL"
                        )

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
        else:
            handled_tkts = set()
            # Never clobber BE / Dynamic ATR Trail / protected SL on TP-only edits (`new_sl is None`)!
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
                self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason=f"Target TP{m_idx} Reached (Prompt)",
                    ceiling_stage=m_idx,
                    close_all_market_tickets=True,
                    exit_reason_code="TP_CEILING",
                    exit_price=live_px,
                    exit_stage=min(4, m_idx),
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
            if runner_tickets:
                signal_data["status"] = "RUNNER_ACTIVE"

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

        if is_runner_phase:
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
                    "MILESTONE_FLOOR"
                    if (
                        (is_buy and float(cur_fsm) >= new_ts - 1e-9)
                        or (not is_buy and float(cur_fsm) <= new_ts + 1e-9)
                    )
                    else "ATR_TRAIL"
                )
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
            for key in ("dynamic_atr_trail_sl", "broker_sl", "be_sl", "sl"):
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

    def evaluate_dynamic_atr_engine(
        self,
        signal_data: Dict[str, Any],
        tick: Any = None,
    ) -> Dict[str, Any]:
        """
        Unified Pure Dynamic ATR Engine:
        - Single engine for BE Lock (`max(0.75 * ATR, $3.50)` -> `entry ± $0.50` + pending sweep),
          Milestone Floors (`TP(m-1)` once `TPm` is cleared),
          Forward-only Dynamic Trailing SL (M5 ATR * mult clamped to [$2.50, max]), and
          Momentum-gated Dynamic Trailing TP (`TP1 -> TP2 -> ... -> max_stage`).
        - Strictly broker-executed SL/TP via `modify_position_sltp`; NEVER calls `close_market_position`.
        """
        policy = self._get_dynamic_risk_policy()
        empty_disabled = {
            "enabled": False,
            "paused_by_spread": False,
            "atr": 0.0,
            "timeframe": "M5",
            "trail_mult": policy.trail_atr_mult,
            "trail_zone": "STANDARD",
            "updated_tickets": [],
            "tp_lifted_tickets": [],
            "pending_swept": [],
            "newly_cleared": [],
            "newly_armed": False,
            "newly_be_locked": [],
            "events": [],
        }
        if not isinstance(signal_data, dict):
            return empty_disabled
        if not policy.trail_enabled:
            return empty_disabled

        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"
        sign = 1.0 if is_buy else -1.0
        if tick is None:
            tick = self.get_tick(symbol)
        if tick is None or not self.is_valid_live_tick(symbol, tick):
            signal_data["spread_spike_paused"] = True
            return {
                "enabled": True,
                "paused_by_spread": True,
                "atr": 0.0,
                "timeframe": policy.trail_timeframe,
                "trail_mult": policy.trail_atr_mult,
                "trail_zone": "STANDARD",
                "updated_tickets": [],
                "tp_lifted_tickets": [],
                "pending_swept": [],
                "newly_cleared": [],
                "newly_armed": False,
                "newly_be_locked": [],
                "events": [],
            }
        is_spike, _, _ = self.check_spread_spike(symbol, tick)
        signal_data["spread_spike_paused"] = bool(is_spike)
        if is_spike:
            return {
                "enabled": True,
                "paused_by_spread": True,
                "atr": 0.0,
                "timeframe": policy.trail_timeframe,
                "trail_mult": policy.trail_atr_mult,
                "trail_zone": "STANDARD",
                "updated_tickets": [],
                "tp_lifted_tickets": [],
                "pending_swept": [],
                "newly_cleared": [],
                "newly_armed": False,
                "newly_be_locked": [],
                "events": [],
            }

        atr = self.get_atr(symbol, policy.trail_timeframe, policy.atr_period)
        if atr <= 0.0:
            return {
                "enabled": True,
                "paused_by_spread": False,
                "atr": 0.0,
                "timeframe": policy.trail_timeframe,
                "trail_mult": policy.trail_atr_mult,
                "trail_zone": "STANDARD",
                "updated_tickets": [],
                "tp_lifted_tickets": [],
                "pending_swept": [],
                "newly_cleared": [],
                "newly_armed": False,
                "newly_be_locked": [],
                "events": [],
            }

        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        pip_size = self.get_pip_size(symbol)
        stops_buffer = self._compute_stops_buffer(sym_info)
        live_price = float(tick.bid) if is_buy else float(tick.ask)

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

        layers = signal_data.get("layers") or {}
        open_keys = self._ordered_open_layer_keys(signal_data)
        roles = self.assign_or_preserve_layer_roles(signal_data)
        bank_all_tp1 = bool(_cfg("BANK_ALL_AT_TP1", BANK_ALL_AT_TP1))
        bank_single_tp1 = bool(_cfg("BANK_SINGLE_LAYER_AT_TP1", BANK_SINGLE_LAYER_AT_TP1))

        prev_meta = signal_data.get("dynamic_atr_trail") if isinstance(signal_data.get("dynamic_atr_trail"), dict) else {}
        was_armed = bool(prev_meta.get("armed"))
        was_trail_armed = bool(prev_meta.get("trail_armed", False))
        any_armed = False
        any_be_armed = False
        any_trail_armed = False
        last_trail_dist = prev_meta.get("trail_dist", 0.0)
        last_trail_mult = prev_meta.get("trail_mult") or policy.trail_atr_mult
        last_trail_zone = prev_meta.get("trail_zone") or "STANDARD"
        last_stop_source = prev_meta.get("stop_source") or signal_data.get("active_stop_source")
        last_sl_applied: Optional[float] = None
        trail_armed_sl: Optional[float] = None
        last_atr_sl_seen: Optional[float] = None

        updated_tickets: List[Tuple[int, str, float, float, float]] = []
        tp_lifted_tickets: List[Tuple[int, str, float, float, str]] = []
        newly_cleared: List[str] = []
        newly_be_locked: List[Tuple[int, str, float]] = []
        events: List[Dict[str, Any]] = []
        be_lock_events: List[Dict[str, Any]] = []
        emitted_milestones_this_tick: Set[str] = set()
        sweep_pendings = False
        pending_swept: List[int] = []

        runner_ticket_set: Set[int] = {
            _int_num(t, 0) for t in (signal_data.get("runner_tickets") or []) if _int_num(t, 0) > 0
        }
        if not runner_ticket_set and signal_data.get("runner_ticket"):
            rt_single = _int_num(signal_data.get("runner_ticket"), 0)
            if rt_single > 0:
                runner_ticket_set.add(rt_single)

        def _forward_update_sig(key: str, val: Optional[float]) -> None:
            if val is None or val <= 0.0:
                return
            cur_v = _num(signal_data.get(key), 0.0)
            if cur_v <= 0.0 or sign * (val - cur_v) > 1e-9:
                signal_data[key] = round(val, digits)

        sig_cleared = list(signal_data.get("runner_cleared_milestones") or [])
        seen_cleared: Set[str] = {str(x).strip().upper() for x in sig_cleared if str(x).strip()}
        sig_cleared = [str(x).strip().upper() for x in sig_cleared if str(x).strip()]

        for lk in open_keys:
            lv = layers.get(lk)
            t_int = _int_num((lv or {}).get("ticket"), 0)
            if not isinstance(lv, dict) or t_int <= 0:
                continue
            entry_px = self._resolve_layer_entry(signal_data, lk, lv) or 0.0
            if entry_px <= 0.0:
                continue

            role = str(roles.get(lk) or lv.get("role") or "").strip().upper()
            layer_be_locked = bool(
                lv.get("be_locked")
                or signal_data.get("be_armed")
                or str(signal_data.get("status") or "").strip().upper() == "BE_APPLIED"
                or bool(prev_meta.get("be_armed"))
            )

            # Read live position SL/TP once if live
            live_pos_tp: Optional[float] = None
            if not self.dry_run:
                with self._mt5_lock:
                    pos_res = mt5.positions_get(ticket=t_int)
                if pos_res:
                    p_tp = _num(getattr(pos_res[0], "tp", 0.0), 0.0)
                    if p_tp > 0.0:
                        live_pos_tp = round(p_tp, digits)
                    self._cached_pos_profits[t_int] = round(
                        _num(getattr(pos_res[0], "profit", 0.0), 0.0) + _num(getattr(pos_res[0], "swap", 0.0), 0.0),
                        2,
                    )

            # Track per-layer and runner execution peaks
            prev_l_peak = lv.get("dynamic_atr_trail_peak")
            if isinstance(prev_l_peak, numbers.Real) and not isinstance(prev_l_peak, bool) and float(prev_l_peak) > 0.0:
                layer_peak = round(max(float(prev_l_peak), live_price) if is_buy else min(float(prev_l_peak), live_price), digits)
            else:
                layer_peak = round(live_price, digits)
            lv["dynamic_atr_trail_peak"] = layer_peak
            lv["mfe_peak_price"] = layer_peak
            lv["mfe_dollars"] = round(max(_num(lv.get("mfe_dollars"), 0.0), sign * (layer_peak - entry_px), 0.0), 6)

            if t_int in runner_ticket_set or role in ("CORE_RUNNER", "SOLO_RUNNER"):
                prev_tpk = signal_data.get("trail_peak") or signal_data.get("runner_peak_price")
                if isinstance(prev_tpk, numbers.Real) and not isinstance(prev_tpk, bool) and float(prev_tpk) > 0.0:
                    sig_peak = round(max(float(prev_tpk), live_price) if is_buy else min(float(prev_tpk), live_price), digits)
                else:
                    sig_peak = round(live_price, digits)
                signal_data["trail_peak"] = sig_peak
                signal_data["runner_peak_price"] = sig_peak
                signal_data["peak_profit"] = round(max(0.0, sign * (sig_peak - entry_px), _num(signal_data.get("peak_profit"), 0.0)), 6)

            # 1) Dynamic Trailing TP
            step_newly_cleared: List[str] = []
            allow_tp_lift = (
                not bank_all_tp1
                and not (bank_single_tp1 and len(open_keys) == 1)
                and role not in ("BANK_TP1", "LATE_RECOVERY")
            )
            if not allow_tp_lift:
                if ("TP1" not in seen_cleared) and str(lv.get("tp_stage") or "TP1").strip().upper() != "TP1":
                    lv["tp_stage"] = "TP1"
                    tp1_target = None
                    if tp_levels and tp_levels.get("TP1") is not None:
                        tp1_target = round(float(tp_levels["TP1"]), digits)
                    elif signal_data.get("final_tp") is not None:
                        tp1_target = round(float(signal_data["final_tp"]), digits)
                    cur_layer_tp = live_pos_tp if live_pos_tp is not None else _num(lv.get("tp"), 0.0)
                    if tp1_target is not None and tp1_target > 0.0 and (cur_layer_tp <= 0.0 or abs(cur_layer_tp - tp1_target) >= 1e-6):
                        cur_tp_freeze_ok = cur_layer_tp <= 0.0 or not self.is_within_freeze_level(symbol, cur_layer_tp, tick=tick, sym_info=sym_info)
                        tgt_tp_freeze_ok = not self.is_within_freeze_level(symbol, tp1_target, tick=tick, sym_info=sym_info)
                        dist_ok = sign * (tp1_target - live_price) >= stops_buffer - 1e-9
                        if cur_tp_freeze_ok and tgt_tp_freeze_ok and dist_ok:
                            if self.modify_position_sltp(t_int, sl=None, tp=tp1_target):
                                lv["tp"] = tp1_target
                                logger.info(f"Demoted layer {lk} ({role}) TP reset to TP1={tp1_target}")

            if allow_tp_lift and tp_levels:
                layer_max_stage = min(2, max_stage) if role == "STAGE2_BANKER" else max_stage
                l_stage_str = str(lv.get("tp_stage") or signal_data.get("tp_stage") or "TP1").strip().upper()
                if l_stage_str.startswith("TP") and l_stage_str[2:].isdigit():
                    l_stage_idx = max(1, min(layer_max_stage, int(l_stage_str[2:])))
                else:
                    l_stage_idx = max(1, min(layer_max_stage, _int_num(signal_data.get("runner_milestone_idx"), 1)))

                tp_step = compute_dynamic_tp_step(
                    direction=direction,
                    entry_price=entry_px,
                    live_price=live_price,
                    peak_price=layer_peak,
                    atr=atr,
                    tp_levels=tp_levels,
                    current_stage_idx=l_stage_idx,
                    max_stage=layer_max_stage,
                    cleared_milestones=sig_cleared,
                    policy=policy,
                    digits=digits,
                    stops_buffer=stops_buffer,
                    be_locked=layer_be_locked,
                )
                for m_tok in list(tp_step.get("cleared_milestones") or []):
                    m_u = str(m_tok).strip().upper()
                    if m_u and m_u not in seen_cleared:
                        seen_cleared.add(m_u)
                        sig_cleared.append(m_u)
                for m_tok in list(tp_step.get("newly_cleared") or []):
                    m_u = str(m_tok).strip().upper()
                    if m_u and m_u not in step_newly_cleared:
                        step_newly_cleared.append(m_u)
                    if m_u and m_u not in newly_cleared:
                        newly_cleared.append(m_u)
                signal_data["runner_cleared_milestones"] = list(sig_cleared)

                if "TP1" in seen_cleared:
                    signal_data["tp1_reached"] = True
                    signal_data["tp1_touched"] = True
                    signal_data["roles_locked"] = True
                    if (runner_ticket_set or role in ("CORE_RUNNER", "SOLO_RUNNER")) and str(signal_data.get("status") or "").strip().upper() != "CLOSED":
                        signal_data["status"] = "RUNNER_ACTIVE"
                for m_idx in range(2, 6):
                    if f"TP{m_idx}" in seen_cleared:
                        signal_data[f"tp{m_idx}_reached"] = True

                if tp_step.get("should_lift") and tp_step.get("target_tp"):
                    target_tp = round(float(tp_step["target_tp"]), digits)
                    target_stage_name = str(tp_step["target_stage_name"])
                    target_stage_idx = int(tp_step["target_stage_idx"])
                    cur_tp = live_pos_tp if live_pos_tp is not None else (_num(lv.get("tp"), 0.0) or _num(tp_levels.get(f"TP{l_stage_idx}"), 0.0))
                    if cur_tp > 0.0 and abs(cur_tp - target_tp) < 1e-6:
                        lv["tp"] = target_tp
                        lv["tp_stage"] = target_stage_name
                    else:
                        cur_tp_freeze_ok = cur_tp <= 0.0 or not self.is_within_freeze_level(symbol, cur_tp, tick=tick, sym_info=sym_info)
                        tgt_tp_freeze_ok = not self.is_within_freeze_level(symbol, target_tp, tick=tick, sym_info=sym_info)
                        dist_ok = sign * (target_tp - live_price) >= stops_buffer - 1e-9
                        if cur_tp_freeze_ok and tgt_tp_freeze_ok and dist_ok:
                            if self.modify_position_sltp(t_int, sl=None, tp=target_tp):
                                old_tp_rec = round(float(cur_tp), digits) if cur_tp > 0.0 else target_tp
                                lv["tp"] = target_tp
                                lv["tp_stage"] = target_stage_name
                                if ("TP1" in seen_cleared) or layer_be_locked:
                                    signal_data["roles_locked"] = True
                                cur_sig_m = _int_num(signal_data.get("runner_milestone_idx"), 1)
                                if role in ("CORE_RUNNER", "SOLO_RUNNER") or target_stage_idx > cur_sig_m:
                                    signal_data["tp_stage"] = target_stage_name
                                    signal_data["runner_milestone_idx"] = target_stage_idx
                                    signal_data["runner_milestone_name"] = target_stage_name
                                tp_lifted_tickets.append((t_int, lk, old_tp_rec, target_tp, target_stage_name))
                                events.append({
                                    "type": "TP_LIFT",
                                    "layer": lk,
                                    "role": role,
                                    "ticket": t_int,
                                    "old_tp": old_tp_rec,
                                    "new_tp": target_tp,
                                    "new_tp_stage": target_stage_name,
                                    "atr": round(atr, 4),
                                })

            # 1b) R-relative profit floor.
            # The ATR trail owns the tight stop; this floor only ever raises the
            # stop, and it scales with the trade's own initial risk R so a wide
            # stop is not protected after the same absolute move as a scalp.
            r_lock_floor = None
            lifecycle_pol = self._get_lifecycle_policy()
            initial_sl = _num(lv.get("initial_sl"), 0.0)
            if lifecycle_pol.r_lock_enabled and initial_sl > 0.0:
                r_lock_floor = compute_r_lock_floor(
                    direction=direction,
                    entry_price=entry_px,
                    initial_sl=initial_sl,
                    mfe_dollars=_num(lv.get("mfe_dollars"), 0.0),
                    live_price=live_price,
                    policy=lifecycle_pol,
                    digits=digits,
                )
            elif lifecycle_pol.r_lock_enabled and not lv.get("_r_lock_skipped_logged"):
                lv["_r_lock_skipped_logged"] = True
                logger.info(
                    f"[R-LOCK SKIPPED] {lk} has no initial_sl (the layer was placed without a stop, "
                    "or the signal predates the upgrade); ATR trail only."
                )

            # 2) Dynamic Trailing SL (BE Lock + Forward-Only ATR Trail + Milestone Floor)
            cur_sl = self._resolve_ticket_current_sl(signal_data, t_int, lv, is_buy, digits)
            trail_step = compute_dynamic_atr_trail_stop(
                direction=direction,
                entry_price=entry_px,
                live_price=live_price,
                prev_peak=lv.get("dynamic_atr_trail_peak"),
                current_sl=cur_sl,
                atr=atr,
                policy=policy,
                digits=digits,
                stops_buffer=stops_buffer,
                cleared_milestones=sig_cleared,
                tp_levels=tp_levels,
                apply_milestone_floor=(role not in ("BANK_TP1", "LATE_RECOVERY")),
                r_lock_floor=r_lock_floor,
            )
            if trail_step.get("peak_price") is not None:
                lv["dynamic_atr_trail_peak"] = trail_step["peak_price"]
                lv["mfe_peak_price"] = trail_step["peak_price"]
            last_trail_dist = trail_step.get("trail_dist") or last_trail_dist
            last_trail_mult = trail_step.get("trail_mult") or last_trail_mult
            last_trail_zone = trail_step.get("trail_zone") or last_trail_zone
            any_armed = any_armed or bool(trail_step.get("armed"))
            any_be_armed = any_be_armed or bool(trail_step.get("be_armed"))
            any_trail_armed = any_trail_armed or bool(trail_step.get("trail_armed"))
            if trail_step.get("atr_sl"):
                last_atr_sl_seen = float(trail_step["atr_sl"])
                _forward_update_sig("atr_stop", last_atr_sl_seen)
            if trail_step.get("milestone_floor"):
                _forward_update_sig("fsm_floor", float(trail_step["milestone_floor"]))

            if trail_step.get("should_sweep_pending") or ("TP1" in seen_cleared):
                sweep_pendings = True

            for m_tok in step_newly_cleared:
                if m_tok not in emitted_milestones_this_tick:
                    emitted_milestones_this_tick.add(m_tok)
                    events.append({
                        "type": "MILESTONE_CLEARED",
                        "milestone": m_tok,
                        "sl_floor": trail_step.get("milestone_floor") or last_sl_applied or signal_data.get("fsm_floor"),
                        "atr": round(atr, 4),
                    })

            cand_sl = trail_step.get("candidate_sl")
            if not trail_step.get("should_update") or not cand_sl or cand_sl <= 0.0:
                continue
            cur_sl_freeze_ok = cur_sl is None or cur_sl <= 0.0 or not self.is_within_freeze_level(symbol, cur_sl, tick=tick, sym_info=sym_info)
            cand_sl_freeze_ok = not self.is_within_freeze_level(symbol, cand_sl, tick=tick, sym_info=sym_info)
            sl_dist_ok = sign * (live_price - float(cand_sl)) >= stops_buffer - 1e-9
            if not (cur_sl_freeze_ok and cand_sl_freeze_ok and sl_dist_ok):
                continue
            if not self.modify_position_sltp(t_int, sl=cand_sl, tp=None):
                continue

            was_be_locked = bool(lv.get("be_locked"))
            stop_source = str(trail_step.get("stop_source") or "ATR_TRAIL")
            lv["sl"] = cand_sl
            lv["broker_sl"] = cand_sl
            if trail_step.get("be_armed") and not was_be_locked:
                lv["be_locked"] = True
                signal_data["roles_locked"] = True
                signal_data["be_armed"] = True
                lv["be_sl"] = trail_step.get("be_sl") or cand_sl
                newly_be_locked.append((t_int, lk, cand_sl))
                be_lock_events.append({
                    "type": "BE_LOCK",
                    "layer": lk,
                    "role": role,
                    "ticket": t_int,
                    "entry_price": round(entry_px, digits),
                    "sl": cand_sl,
                    "mfe_dollars": trail_step.get("mfe_dollars", 0.0),
                    "atr": trail_step.get("atr", round(atr, 4)),
                    "pending_swept": [],
                })
            if stop_source in ("ATR_TRAIL", "MILESTONE_FLOOR", "R_LOCK"):
                lv["dynamic_atr_trail_sl"] = cand_sl
                lv["dynamic_atr_trail_dist"] = trail_step["trail_dist"]
                lv["dynamic_atr_trail_atr"] = trail_step["atr"]
                lv["dynamic_atr_trail_mult"] = trail_step.get("trail_mult") or last_trail_mult
                lv["dynamic_atr_trail_zone"] = trail_step.get("trail_zone") or last_trail_zone

            lv["dynamic_atr_trail_mult"] = trail_step.get("trail_mult") or last_trail_mult
            lv["dynamic_atr_trail_zone"] = trail_step.get("trail_zone") or last_trail_zone

            _forward_update_sig("broker_sl", cand_sl)
            _forward_update_sig("trail_stop", cand_sl)
            _forward_update_sig("runner_sl_floor", cand_sl)
            _forward_update_sig("soft_tp1_floor", cand_sl)
            _forward_update_sig("sl", cand_sl)
            signal_data["trail_dist"] = trail_step["trail_dist"]
            signal_data["active_stop_source"] = stop_source
            last_stop_source = stop_source
            last_sl_applied = cand_sl
            if trail_step.get("trail_armed") and not was_trail_armed and trail_armed_sl is None:
                trail_armed_sl = cand_sl

            updated_tickets.append((t_int, lk, cand_sl, trail_step["trail_dist"], trail_step["atr"]))

        if sweep_pendings and bool(_cfg("CANCEL_PENDING_ON_PROFIT_LOCK", CANCEL_PENDING_ON_PROFIT_LOCK)) and signal_data.get("pending_tickets"):
            pending_swept = list(self.cancel_signal_pending_orders(signal_data) or [])

        for be_ev in be_lock_events:
            be_ev["pending_swept"] = list(pending_swept)
            events.append(be_ev)

        if any_trail_armed and not was_trail_armed:
            events.append({
                "type": "ATR_TRAIL_ARMED",
                "timeframe": policy.trail_timeframe,
                "atr": round(atr, 4),
                "trail_dist": last_trail_dist,
                "sl": trail_armed_sl if trail_armed_sl is not None else (last_sl_applied if last_sl_applied is not None else (signal_data.get("broker_sl") or last_atr_sl_seen)),
            })

        if any_armed or updated_tickets or tp_lifted_tickets or was_armed:
            last_sl = updated_tickets[-1][2] if updated_tickets else (prev_meta.get("last_sl") or signal_data.get("broker_sl"))
            signal_data["dynamic_atr_trail"] = {
                "armed": bool(any_armed or was_armed),
                "be_armed": bool(any_be_armed or prev_meta.get("be_armed")),
                "trail_armed": bool(any_trail_armed or was_trail_armed),
                "timeframe": policy.trail_timeframe,
                "atr": round(atr, 4),
                "trail_dist": last_trail_dist,
                "trail_mult": last_trail_mult,
                "trail_zone": last_trail_zone,
                "last_sl": last_sl,
                "stop_source": last_stop_source,
            }

        return {
            "enabled": True,
            "paused_by_spread": False,
            "atr": round(atr, 4),
            "timeframe": policy.trail_timeframe,
            "trail_mult": last_trail_mult,
            "trail_zone": last_trail_zone,
            "updated_tickets": updated_tickets,
            "tp_lifted_tickets": tp_lifted_tickets,
            "pending_swept": pending_swept,
            "newly_cleared": newly_cleared,
            "newly_armed": bool(any_armed and not was_armed),
            "newly_be_locked": newly_be_locked,
            "events": events,
        }

    def evaluate_dynamic_atr_trailing_stop(
        self,
        signal_data: Dict[str, Any],
        tick: Any = None,
    ) -> Dict[str, Any]:
        """Delegate to the unified Pure Dynamic ATR Engine (`evaluate_dynamic_atr_engine`)."""
        return self.evaluate_dynamic_atr_engine(signal_data, tick=tick)
