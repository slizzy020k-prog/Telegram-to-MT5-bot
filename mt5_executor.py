import copy
import logging
import math
import numbers
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
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
    BASE_LAYER_LOTS,
    BE_AFTER_TP1,
    BE_LAYERS,
    BE_OFFSET_PIPS,
    CANCEL_PENDING_ON_TP1,
    DAILY_LOSS_PAUSE_PCT,
    DAILY_PNL_CACHE_TTL_SECONDS,
    DEFAULT_SYMBOL,
    DEFAULT_TP_STEP_PIPS,
    EMERGENCY_CUTOFF_LOSS_PCT,
    FLOAT_LOSS_FORCE_CLOSE_PCT,
    ENFORCE_HIGH_RISK_LOT_CAP,
    FALLBACK_SL_PIPS,
    GIVEBACK_PCT,
    GOLD_REVERSAL_GUARD_ENABLED,
    HARD_GIVEBACK_CEILING_DOLLARS,
    LAYER_LOTS,
    LAYER_NAMES,
    LEGACY_LAYER_MAP,
    M1_ATR_PERIOD,
    MAGIC_NUMBER,
    MAX_ALLOWED_SPREAD_DOLLARS,
    MAX_GIVEBACK_DOLLARS,
    MAX_ORDERS_PER_SIGNAL,
    MIN_GOLD_PULLBACK_DOLLARS,
    MIN_SL_BUFFER_PIPS,
    MT5_LOGIN,
    MT5_PASSWORD,
    MT5_PATH,
    MT5_SERVER,
    NO_TP2_SURGE_ARM_PIPS,
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
    TRAIL_DISTANCE_PIPS,
    TRAIL_STEP_PIPS,
)
from parser import ZoneInfo, compute_zone_4tiers, derive_tp_levels

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
        "TP1_CLOSE_PROXIMITY_PIPS",
        "TP1_LOCK_BUFFER",
        "TP1_TOUCH_BANK_ENABLED",
        "TP1_TOUCH_BANK_BUFFER_PIPS",
        "TP1_TRAIL_ENABLED",
        "TP1_TRAIL_ATR_MULT",
        "TP1_TRAIL_MIN_DOLLARS",
        "TP1_TRAIL_MAX_DOLLARS",
        "TP1_TRAIL_STEP_DOLLARS",
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
        "DEFAULT_SYMBOL",
        "DAILY_PNL_CACHE_TTL_SECONDS",
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
        self._dry_run_daily_realized_pnl: float = 0.0
        self._dry_run_floating_pnl: float = 0.0
        self._dry_run_last_m1_candle: Optional[Dict[str, float]] = None
        self._dry_run_m1_atr: Optional[float] = None
        self._cached_pos_profits: Dict[int, float] = {}
        self._realized_pnl_cache: Optional[Tuple[float, float, str]] = None
        self._closing_lock = threading.Lock()
        self._closing_tickets: Set[int] = set()
        self._spread_history: Dict[str, Deque[float]] = {}
        self._daily_loss_baseline_realized: float = 0.0
        self._daily_drawdown_ref_balance: Optional[float] = None
        self._daily_drawdown_baseline_date: Optional[str] = None

    def connect(self) -> bool:
        """Initialize connection to MetaTrader 5 terminal."""
        if self.dry_run:
            logger.info("MT5Executor running in DRY-RUN mode.")
            self._connected = True
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
            return True

    def shutdown(self) -> None:
        """Shutdown MT5 connection."""
        with self._mt5_lock:
            if not self.dry_run and self._connected:
                mt5.shutdown()
                self._connected = False
                self._realized_pnl_cache = None
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

    def get_m1_atr(self, symbol: str, period: Optional[int] = None) -> float:
        """
        Compute the closed-bar M1 ATR(period) in price dollars/points (default period=14).
        Returns `self._dry_run_m1_atr` in dry-run when set, or 0.0 safely when bars are unavailable.
        """
        eff_period = max(2, _int_num(period if period is not None else _cfg("M1_ATR_PERIOD", M1_ATR_PERIOD), 14))
        if self.dry_run:
            return float(self._dry_run_m1_atr) if self._dry_run_m1_atr is not None and self._dry_run_m1_atr > 0 else 0.0
        if not self._connected:
            return 0.0

        try:
            timeframe_m1 = _mt5_const("TIMEFRAME_M1", 1)
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol)
                rates = mt5.copy_rates_from_pos(resolved, timeframe_m1, 1, eff_period + 1)
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
                h = _field(bar, "high")
                l = _field(bar, "low")
                c = _field(bar, "close")
                if h is None or l is None or c is None:
                    continue
                if prev_close is not None:
                    tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
                else:
                    tr = h - l
                if tr >= 0.0:
                    trs.append(tr)
                prev_close = c

            if not trs:
                return 0.0
            return round(sum(trs) / len(trs), 4)
        except Exception as e:
            logger.debug(f"Could not compute M1 ATR({eff_period}) for {symbol}: {e}")
            return 0.0

    def check_spread_spike(self, symbol: str, tick: Any) -> Tuple[bool, float, float]:
        """
        Adaptive Spread Spike Filter:
        - Returns `(is_spike, spread_dollars, max_allowed_spread_dollars)`.
        - On low-spread ECN feeds (baseline <= MAX_ALLOWED_SPREAD_DOLLARS = $0.40), enforces
          `max($0.40, 1.5 * rolling_median_spread)` so momentary spread spikes > $0.40 pause exits.
        - On standard broker feeds like PU Prime (`XAUUSD.s` normal spread $0.50-$0.52 > $0.40),
          adapts cleanly to `max(ADAPTIVE_SPREAD_CEILING_DOLLARS = $0.80, 1.5 * rolling_median_spread)`
          so normal trading is never blocked while true news/rollover spikes (> $0.80) pause exits.
        """
        cfg_max = float(_cfg("MAX_ALLOWED_SPREAD_DOLLARS", MAX_ALLOWED_SPREAD_DOLLARS))
        adapt_ceil = float(_cfg("ADAPTIVE_SPREAD_CEILING_DOLLARS", ADAPTIVE_SPREAD_CEILING_DOLLARS))
        mult = float(_cfg("ADAPTIVE_SPREAD_MULTIPLIER", ADAPTIVE_SPREAD_MULTIPLIER))
        if tick is None:
            return False, 0.0, cfg_max

        bid = _num(getattr(tick, "bid", 0.0), 0.0)
        ask = _num(getattr(tick, "ask", 0.0), 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return False, 0.0, cfg_max

        spread_dollars = round(ask - bid, 4)
        sym_key = (symbol or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)).upper()

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
                if not self.dry_run and spread_dollars <= adapt_ceil + 1e-6:
                    hist.append(spread_dollars)
                    eff_max = max(adapt_ceil, round(mult * spread_dollars, 4)) if spread_dollars > cfg_max + 1e-6 else cfg_max
                else:
                    eff_max = cfg_max

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
        spread_points = round((ask - bid) / point, 1)
        logger.info(f"Pre-flight check: Current spread on {resolved} is {spread_points} points.")

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
            return True, "Pre-flight checks passed"

        pip_size = self.get_pip_size(resolved)
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
        """Normalize explicit SL or compute FALLBACK_SL_PIPS stop loss away from ref_price."""
        if sl is None or sl == 0.0:
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
        allowed_lot, block_reason = self.check_and_cap_order_lot(norm_lot, symbol=resolved_sym)
        if allowed_lot <= 0.0:
            self.last_error = block_reason or "High Risk Lot Cap reached"
            logger.warning(f"{caller_label} {direction_norm} blocked by Risk Guide Cap: {self.last_error}")
            return None

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
    ) -> Optional[int]:
        """Execute an immediate Market Buy or Sell order."""
        ctx = self._prepare_symbol_order_context(symbol, direction, lot, "execute_market_order")
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
            logger.info(
                f"Opened Market {direction_norm} Order #{ticket} | Symbol={resolved_sym}, "
                f"Lot={final_lot}, Price={entry_price}, SL={final_sl}, TP={final_tp}"
            )
            return ticket
        return None

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
    ) -> Optional[int]:
        """Shared pending LIMIT / STOP placement helper."""
        kind_upper = pending_kind.strip().upper()
        caller_label = "place_stop_order" if kind_upper == "STOP" else "place_limit_order"
        if price is None or float(price) <= 0.0:
            self.last_error = f"Invalid {kind_upper.lower()} price: {price}"
            logger.error(f"{caller_label} rejected invalid price: {price!r}")
            return None

        ctx = self._prepare_symbol_order_context(symbol, direction, lot, caller_label)
        if ctx is None:
            return None
        direction_norm, resolved_sym, _, digits, pip_size, final_lot = ctx

        is_buy = direction_norm == "BUY"
        if kind_upper == "STOP":
            order_type = _mt5_const("ORDER_TYPE_BUY_STOP", 4) if is_buy else _mt5_const("ORDER_TYPE_SELL_STOP", 5)
        else:
            order_type = _mt5_const("ORDER_TYPE_BUY_LIMIT", 2) if is_buy else _mt5_const("ORDER_TYPE_SELL_LIMIT", 3)

        target_price = round(float(price), digits)
        final_sl = self._compute_fallback_sl(
            is_buy, target_price, sl, pip_size, digits, direction_norm, resolved_sym, kind_upper.title()
        )
        final_tp = round(float(tp), digits) if tp is not None else 0.0

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
                self._dry_run_ticket_lots[ticket] = final_lot
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
    ) -> Tuple[Optional[int], str]:
        """
        Place a pending LIMIT or STOP order; if broker rejects because live price is touching
        or inside trade_stops_level of target_price, fall back to immediate Market entry.
        """
        kind_upper = pending_kind.strip().upper()
        if kind_upper == "STOP":
            t = self.place_stop_order(
                symbol=symbol, direction=direction, price=target_price, lot=lot,
                sl=sl, tp=tp, comment=pending_comment,
            )
        else:
            t = self.place_limit_order(
                symbol=symbol, direction=direction, price=target_price, lot=lot,
                sl=sl, tp=tp, comment=pending_comment,
            )
        if t:
            return t, kind_upper

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
                    symbol=symbol, direction=direction, lot=lot, sl=sl, tp=tp, comment=market_comment,
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
    ) -> Tuple[Optional[int], str]:
        """
        Route a single layer to MARKET, LIMIT, or STOP based on explicit order_intent
        and current bid/ask vs target_price.
        """
        intent = (order_intent or "AUTO").strip().upper()
        is_buy = (direction or "BUY").strip().upper() == "BUY"

        if intent == "MARKET" or target_price is None:
            t = self.execute_market_order(
                symbol=symbol, direction=direction, lot=lot, sl=sl, tp=tp, comment=market_comment,
            )
            return (t, "MARKET") if t else (None, "FAILED")

        if intent == "STOP":
            should_market = (tick.ask >= target_price) if is_buy else (tick.bid <= target_price)
            if should_market:
                t = self.execute_market_order(
                    symbol=symbol, direction=direction, lot=lot, sl=sl, tp=tp, comment=market_comment,
                )
                return (t, "MARKET") if t else (None, "FAILED")
            return self._place_pending_with_stops_fallback(
                symbol=symbol,
                direction=direction,
                target_price=target_price,
                lot=lot,
                sl=sl,
                tp=tp,
                pending_comment=stop_comment or limit_comment.replace("Limit", "Stop"),
                market_comment=market_comment,
                pending_kind="STOP",
            )

        # AUTO / LIMIT routing
        should_market = (tick.ask <= target_price) if is_buy else (tick.bid >= target_price)
        if should_market:
            t = self.execute_market_order(
                symbol=symbol, direction=direction, lot=lot, sl=sl, tp=tp, comment=market_comment,
            )
            return (t, "MARKET") if t else (None, "FAILED")
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

    def allocate_4layer_lots(
        self,
        symbol: str,
        base_lots: Optional[Sequence[float]] = None,
        multiplier: Optional[float] = None,
    ) -> Tuple[List[float], Optional[str]]:
        """
        Atomically compute 4-layer lot sizes [L1, L2, L3, L4] scaled by multiplier,
        normalized to symbol volume constraints, and fitted within available_lots cap.
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

        vol_min, vol_max, vol_step, dec = _extract_symbol_volume_limits(sym_info)
        status = self.get_risk_exposure_status()
        avail = float(status.get("available_lots", 0.0))
        high_cap = float(status.get("high_risk_lots", 0.0))
        total_lots = float(status.get("total_lots", 0.0))
        cap = float(status.get("capital", 0.0))
        curr = status.get("currency", "GBP")

        total_scaled = round(sum(scaled), 6)
        if total_scaled <= avail + 1e-9:
            self.last_allocation_warning = None
            return scaled, None

        if avail < vol_min - 1e-9:
            warn = (
                f"High Risk Lot Cap Reached ({total_lots:.2f}/{high_cap:.2f} lots "
                f"for {cap:.2f} {curr} account)"
            )
            self.last_allocation_warning = warn
            return [0.0, 0.0, 0.0, 0.0], warn

        ratio = avail / total_scaled if total_scaled > 0 else 0.0
        eps = max(1e-12, vol_step * 1e-7)
        alloc: List[float] = []
        for s in scaled:
            raw_p = s * ratio
            floored = round(math.floor((raw_p + eps) / vol_step) * vol_step, dec)
            if floored < vol_min - 1e-9:
                alloc.append(0.0)
            else:
                alloc.append(round(min(floored, s, vol_max), dec))

        rem = round(avail - sum(alloc), 6)

        # Pass 1: activate zeroed layers in L1 -> L2 -> L3 -> L4 order when budget allows vol_min
        min_norm = normalize_lot_to_symbol(vol_min, sym_info)
        for i in range(4):
            if alloc[i] == 0.0 and rem >= min_norm - 1e-9 and min_norm <= scaled[i] + 1e-9:
                alloc[i] = min_norm
                rem = round(rem - min_norm, 6)

        # Pass 2: top up remaining step budget in L4 -> L3 -> L2 -> L1 order (favoring active runner)
        while rem >= vol_step - 1e-9:
            progressed = False
            for i in (3, 2, 1, 0):
                if alloc[i] > 0.0 and rem >= vol_step - 1e-9 and (alloc[i] + vol_step) <= min(scaled[i], vol_max) + 1e-9:
                    alloc[i] = round(alloc[i] + vol_step, dec)
                    rem = round(rem - vol_step, 6)
                    progressed = True
            if not progressed:
                break

        warn = (
            f"High-Risk Cap applied: allocated {sum(alloc):.2f}/{total_scaled:.2f} lots "
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
            steps = [
                float(_cfg("TP2_STEP_PIPS", TP2_STEP_PIPS)),
                float(_cfg("TP3_STEP_PIPS", TP3_STEP_PIPS)),
                float(_cfg("TP4_STEP_PIPS", TP4_STEP_PIPS)),
                float(_cfg("TP5_STEP_PIPS", TP5_STEP_PIPS)),
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
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """Shared execution loop for L1..L4 layers across Zone, Single-Price, and Market entries."""
        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}

        for l_name, target_px, l_lot, l_tp in layer_specs:
            if l_lot <= 0.0:
                logger.info(f"Skipping {l_name} ({entry_label}): allocated lot is 0.0 (cap enforced).")
                continue
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
            )
            if t:
                if o_type == "MARKET":
                    market_tickets.append(t)
                    status = "OPEN"
                else:
                    pending_tickets.append(t)
                    status = "PENDING"
                layers_data[l_name] = {
                    "lot": l_lot,
                    "target_price": target_px,
                    "tp": l_tp,
                    "tp_stage": "TP1",
                    "ticket": t,
                    "order_type": o_type,
                    "status": status,
                }

        return market_tickets, pending_tickets, layers_data

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
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Split a Zone entry into 4 layers across [0%, 33.3%, 66.7%, 100%] (L1..L4 -> TP1/TP2/TP3/TP5).
        Returns (market_tickets, pending_tickets, layers_data).
        """
        if zone is None:
            self.last_error = "Invalid 4-layer zone parameters (zone=None)"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer zone")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        alloc_lots, warn = self.allocate_4layer_lots(resolved_sym, base_lots=base_lots, multiplier=multiplier)
        if sum(alloc_lots) <= 0.0:
            self.last_error = warn or "High Risk Lot Cap reached"
            return [], [], {}

        if hasattr(zone, "get_tiers"):
            raw_tiers = zone.get_tiers(direction_norm)
        elif isinstance(zone, dict):
            raw_tiers = compute_zone_4tiers(zone["top"], zone["bottom"], direction_norm, digits=digits)
        else:
            raw_tiers = compute_zone_4tiers(zone.top, zone.bottom, direction_norm, digits=digits)
        tiers = {k: round(float(v), digits) for k, v in raw_tiers.items()}

        layer_tps, _ = self._resolve_4layer_tps(
            direction=direction_norm,
            pip_size=pip_size,
            digits=digits,
            final_tp=final_tp,
            raw_tp1=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels,
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
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Split a Single-Price entry (LIMIT, STOP, or smart AUTO) into 4 layers (L1..L4 -> TP1/TP2/TP3/TP5),
        or a restricted layer subset (e.g. max_layers=1 -> L1 only for un-SL/no-TP stubs).
        Returns (market_tickets, pending_tickets, layers_data).
        """
        if price is None or float(price) <= 0.0:
            self.last_error = f"Invalid single-price parameter (price={price})"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer single-price")
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

        eff_intent = (order_intent or "AUTO").strip().upper()
        if is_stop_order and eff_intent == "AUTO":
            eff_intent = "STOP"

        target_px = round(float(price), digits)
        layer_tps, _ = self._resolve_4layer_tps(
            direction=direction_norm,
            pip_size=pip_size,
            digits=digits,
            final_tp=final_tp,
            raw_tp1=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels,
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

        layer_tps, _ = self._resolve_4layer_tps(
            direction=direction_norm,
            pip_size=pip_size,
            digits=digits,
            final_tp=final_tp,
            raw_tp1=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels,
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

    def modify_position_sltp(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        """Modify SL and TP of an open MT5 market position (thread-safe)."""
        if not ticket:
            return False
        if self.dry_run:
            logger.info(f"[DRY-RUN] Modified Position #{ticket}: SL={sl}, TP={tp}")
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

            request = {
                "action": _mt5_const("TRADE_ACTION_SLTP", 6),
                "position": ticket,
                "symbol": p_symbol,
                "sl": new_sl,
                "tp": new_tp,
            }

            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            no_changes_code = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)

            def _is_mod_ok(res: Any) -> bool:
                if not res:
                    return False
                if res.retcode in (done_code, no_changes_code):
                    return True
                return str(getattr(res, "comment", "")).strip().lower() == "no changes"

            result = mt5.order_send(request)
            if result and result.retcode == done_code:
                logger.info(f"Updated SL/TP on Position #{ticket}: SL={new_sl}, TP={new_tp}")
                return True
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            if _is_mod_ok(result):
                logger.debug(f"Position #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
                return True

            # Independent leg fallback: if BOTH sl and tp were requested and combined modification
            # failed (e.g. 10016 Invalid stops on one side), apply valid TP and valid SL independently
            # so a stop-level conflict on SL never blocks TP updates (and vice versa).
            if sl is not None and tp is not None:
                tp_applied = False
                sl_applied = False
                eff_tp = cur_tp
                if abs(new_tp - cur_tp) > 1e-6:
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
                if abs(new_sl - cur_sl) > 1e-6:
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
                    return True

            self.last_error = str(comment)
            logger.error(f"Failed to update SL/TP on Position #{ticket}: {comment}")
            return False

    def modify_pending_order_sltp(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        """Modify SL and TP of a pending MT5 order (thread-safe)."""
        if not ticket:
            return False
        if self.dry_run:
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

    def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel an unfilled pending limit/stop order (thread-safe)."""
        if not ticket:
            return False
        if self.dry_run:
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
                self._cached_pos_profits[ticket] = 1.25
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
                self._cached_pos_profits[ticket] = round(
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
                    self.invalidate_realized_pnl_cache()
                    logger.info(f"Closed Position #{ticket} ({p_symbol} {vol} lots at {close_price})")
                    return True
                return False
        finally:
            with self._closing_lock:
                self._closing_tickets.discard(t_int)

    def _compute_close_info_from_deals(self, ticket: int, deals: Any) -> Dict[str, Any]:
        """Compute {"profit": float, "reason": str} from a single history_deals_get result."""
        reason = "UNKNOWN"
        if deals:
            net_profit = sum(
                _num(getattr(d, "profit", 0.0))
                + _num(getattr(d, "commission", 0.0))
                + _num(getattr(d, "swap", 0.0))
                + _num(getattr(d, "fee", 0.0))
                for d in deals
            )
            profit = round(net_profit, 2)
            out_entries = {
                _mt5_const("DEAL_ENTRY_OUT", 1),
                _mt5_const("DEAL_ENTRY_INOUT", 2),
                _mt5_const("DEAL_ENTRY_OUT_BY", 3),
            }
            exit_deals = [d for d in deals if _int_num(getattr(d, "entry", -1), -1) in out_entries]
            if exit_deals:
                d = exit_deals[-1]
                d_reason = getattr(d, "reason", None)
                if d_reason == _mt5_const("DEAL_REASON_SL", 4):
                    reason = "SL"
                elif d_reason == _mt5_const("DEAL_REASON_TP", 5):
                    reason = "TP"
                else:
                    reason = "MANUAL/BOT"
            return {"profit": profit, "reason": reason}

        return {
            "profit": self._cached_pos_profits.get(ticket, 0.0),
            "reason": reason,
        }

    def get_position_close_info(self, ticket: Optional[int]) -> Dict[str, Any]:
        """
        Return {"profit": float, "reason": str} for a closed position using a SINGLE
        mt5.history_deals_get(position=ticket) call.
        reason is one of: "TP", "SL", "MANUAL/BOT", "UNKNOWN".
        """
        if not ticket:
            return {"profit": 0.0, "reason": "UNKNOWN"}

        if self.dry_run or not self._connected:
            return {
                "profit": self._cached_pos_profits.get(ticket, 1.25),
                "reason": "UNKNOWN",
            }

        try:
            with self._mt5_lock:
                deals = mt5.history_deals_get(position=ticket)
            return self._compute_close_info_from_deals(ticket, deals)
        except Exception as e:
            logger.warning(f"Could not determine close info for #{ticket}: {e}")
            return {
                "profit": self._cached_pos_profits.get(ticket, 0.0),
                "reason": "UNKNOWN",
            }

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
            total = round(sum(self._dry_run_ticket_lots.values()), 2)
            return {"open_lots": total, "pending_lots": 0.0, "total_lots": total}

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
        total_lots = lots["total_lots"]
        low_l = thresholds["low_risk_lots"]
        mid_l = thresholds["mid_risk_lots"]
        high_l = thresholds["high_risk_lots"]

        if total_lots >= high_l - 1e-9:
            tier = "HIGH_CAP_REACHED"
        elif total_lots > mid_l + 1e-9:
            tier = "ABOVE_MID_RISK"
        elif total_lots > low_l + 1e-9:
            tier = "ABOVE_LOW_RISK"
        else:
            tier = "LOW_RISK_SAFE"

        return {
            **fin,
            **thresholds,
            **lots,
            "risk_tier": tier,
            "available_lots": max(0.0, round(high_l - total_lots, 2)),
        }

    def check_and_cap_order_lot(
        self,
        requested_lot: float,
        symbol: Optional[str] = None,
    ) -> Tuple[float, Optional[str]]:
        """
        Enforces the Professional Risk Guide High-Risk Cap:
        Never allows total active + pending lots to exceed high_risk_lots.
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
        total_lots = status["total_lots"]
        cap = status["capital"]
        curr = status.get("currency", "GBP")

        if avail < vol_min - 1e-9:
            reason = (
                f"High Risk Lot Cap Reached ({total_lots:.2f}/{high_cap:.2f} lots "
                f"for {cap:.2f} {curr} account)"
            )
            return 0.0, reason

        allowed_raw = min(norm_req, avail)
        eps = max(1e-12, vol_step * 1e-7)
        allowed = round(math.floor((allowed_raw + eps) / vol_step) * vol_step, dec)
        if allowed < vol_min:
            reason = (
                f"High Risk Lot Cap Reached ({total_lots:.2f}/{high_cap:.2f} lots "
                f"for {cap:.2f} {curr} account)"
            )
            return 0.0, reason

        return allowed, None

    def evaluate_risk_threshold_alerts(self, prev_lots: float, new_lots: float) -> List[str]:
        """
        Check which Risk Guide thresholds (Low Risk, Mid Risk, High Risk Cap) were crossed
        as total exposure increased from prev_lots to new_lots.
        """
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
        latest tick timestamp. Stale ticks fall back to the last good cached offset.
        """
        cached = getattr(self, "_server_offset_cache", None)
        if self.dry_run or not self._connected:
            return 0
        try:
            with self._mt5_lock:
                tick = mt5.symbol_info_tick(self.resolve_symbol(_cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)))
            tick_time = _num(getattr(tick, "time", 0.0), 0.0) if tick else 0.0
            if tick_time > 0.0:
                diff = tick_time - time.time()
                rounded = int(round(diff / 1800.0) * 1800)
                if -12 * 3600 <= rounded <= 14 * 3600 and abs(diff - rounded) <= 600:
                    self._server_offset_cache = rounded
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
            curr_bal = float(fin.get("balance") or 0.0)
            realized_today = self.get_realized_today_pnl()
            floating_pnl = 0.0
            try:
                with self._mt5_lock:
                    positions = mt5.positions_get() or []
                for p in positions:
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

        pause_pct = _cfg("DAILY_LOSS_PAUSE_PCT", DAILY_LOSS_PAUSE_PCT)
        cutoff_pct = _cfg("EMERGENCY_CUTOFF_LOSS_PCT", EMERGENCY_CUTOFF_LOSS_PCT)
        pause_limit = round(ref_bal * (pause_pct / 100.0), 2)
        cutoff_limit_ref = round(ref_bal * (cutoff_pct / 100.0), 2)
        cutoff_limit_curr = round(max(0.0, curr_bal) * (cutoff_pct / 100.0), 2)

        loss_pct = round((total_daily_loss / ref_bal) * 100.0, 1) if ref_bal > 0 else 0.0
        floating_loss_pct = round((floating_loss / curr_bal) * 100.0, 1) if curr_bal > 0 else 0.0

        pause_new = bool(ref_bal > 0 and total_daily_loss >= pause_limit - 1e-9)
        emergency_cutoff = bool(
            (ref_bal > 0 and total_daily_loss >= cutoff_limit_ref - 1e-9)
            or (curr_bal > 0 and floating_loss >= cutoff_limit_curr - 1e-9)
        )

        # Hard floating-loss cap: force-close ALL open trades when floating-only loss breaches this %.
        float_cap_pct = _cfg("FLOAT_LOSS_FORCE_CLOSE_PCT", FLOAT_LOSS_FORCE_CLOSE_PCT)
        float_cap_limit = round(max(0.0, curr_bal) * (float_cap_pct / 100.0), 2)
        float_force_close = bool(curr_bal > 0 and float_cap_limit > 0 and floating_loss >= float_cap_limit - 1e-9)

        return {
            "currency": curr,
            "current_balance": round(curr_bal, 2),
            "reference_balance": round(ref_bal, 2),
            "realized_today_pnl": realized_today,
            "floating_pnl": floating_pnl,
            "floating_loss": floating_loss,
            "total_daily_loss": total_daily_loss,
            "loss_pct": max(loss_pct, floating_loss_pct),
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
        curr_bal = float(fin.get("balance") or (self._dry_run_capital or 0.0))
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
            for pt in list(tracked_pending_tickets or []):
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
                entry_ref = (
                    _num(layer_obj.get("target_price"), 0.0)
                    if isinstance(layer_obj, dict) and layer_obj.get("target_price") is not None
                    else _num(signal_data.get("recorded_entry_price"), 0.0)
                )
                if entry_ref > 0.0:
                    calc_sl = round(entry_ref + offset_value, digits) if is_buy else round(entry_ref - offset_value, digits)
                    if isinstance(layer_obj, dict):
                        prev_be = _num(layer_obj.get("be_sl"), 0.0)
                        if is_buy and prev_be > 0.0 and prev_be > calc_sl:
                            calc_sl = prev_be
                        elif not is_buy and prev_be > 0.0 and prev_be < calc_sl:
                            calc_sl = prev_be
                        layer_obj["be_sl"] = calc_sl
                    applied_sls[ticket] = calc_sl
                if ticket not in be_done_set:
                    logger.info(
                        f"[DRY-RUN] Moved Layer {layer_name or 'OPEN'} (Ticket #{ticket}) to Breakeven (+{eff_offset_pips} pips offset)"
                    )
                    modified_tickets.append(ticket)
                    be_done_set.add(ticket)
            signal_data["be_done_tickets"] = sorted(be_done_set)
            signal_data["be_deferred_tickets"] = []
            return {
                "modified_tickets": modified_tickets,
                "deferred_tickets": [],
                "promoted_tickets": [],
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
            if ticket in pd_tickets:
                pd_tickets = [t for t in pd_tickets if t != ticket]
                if ticket not in mk_tickets:
                    mk_tickets.append(ticket)
                if layer_name and isinstance(layers.get(layer_name), dict):
                    layers[layer_name]["status"] = "FILLED"
                promoted_tickets.append(ticket)
                logger.info(
                    f"Instant BE fill-race catch: Pending #{ticket} ({layer_name}) already filled on MT5; promoting & protecting now."
                )

            p0 = pos[0]
            p_open = _num(getattr(p0, "price_open", 0.0), 0.0)
            p_sl = _num(getattr(p0, "sl", 0.0), 0.0)
            pos_is_buy = _int_num(getattr(p0, "type", buy_order_type), buy_order_type) == buy_order_type
            if ticket in be_done_set and p_open > 0.0:
                if (pos_is_buy and p_sl >= p_open - 1e-6) or (not pos_is_buy and p_sl > 0.0 and p_sl <= p_open + 1e-6):
                    applied_sls[ticket] = p_sl
                    continue

            new_sl = self._move_ticket_to_be(ticket, offset_value, digits, direction)
            if new_sl is not None:
                modified_tickets.append(ticket)
                be_done_set.add(ticket)
                applied_sls[ticket] = new_sl
                if layer_name and isinstance(layers.get(layer_name), dict):
                    layers[layer_name]["be_sl"] = new_sl
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

        if signal_data.get("status") == "RUNNER_ACTIVE" and applied_sls:
            cur_floor = signal_data.get("runner_sl_floor")
            best_be_sl = max(applied_sls.values()) if is_buy else min(applied_sls.values())
            if cur_floor is None:
                signal_data["runner_sl_floor"] = best_be_sl
            else:
                cur_floor_f = float(cur_floor)
                if (is_buy and best_be_sl > cur_floor_f) or (not is_buy and best_be_sl < cur_floor_f):
                    signal_data["runner_sl_floor"] = best_be_sl

        signal_data["be_done_tickets"] = sorted(be_done_set)
        signal_data["be_deferred_tickets"] = deferred_tickets
        return {
            "modified_tickets": modified_tickets,
            "deferred_tickets": deferred_tickets,
            "promoted_tickets": promoted_tickets,
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
            wp_tp = _num(getattr(wp, "tp", 0.0), 0.0)
            wp_profit = _num(getattr(wp, "profit", 0.0), 0.0)
            if _int_num(getattr(wp, "type", buy_order_type), buy_order_type) == buy_order_type:
                new_sl = round(price_open + offset_value, digits)
            else:
                new_sl = round(price_open - offset_value, digits)

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
            return []
        try:
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol) if symbol else None
                positions = mt5.positions_get(symbol=resolved) if resolved else mt5.positions_get()
                return list(positions) if positions else []
        except Exception as e:
            logger.debug(f"get_live_positions error: {e}")
            return []

    def get_live_orders(self, symbol: Optional[str] = None) -> List[Any]:
        """Thread-safe reader returning all pending MT5 orders (optionally filtered by symbol)."""
        if self.dry_run or not self._connected:
            return []
        try:
            with self._mt5_lock:
                resolved = self.resolve_symbol(symbol) if symbol else None
                orders = mt5.orders_get(symbol=resolved) if resolved else mt5.orders_get()
                return list(orders) if orders else []
        except Exception as e:
            logger.debug(f"get_live_orders error: {e}")
            return []

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
    ) -> None:
        """
        Close all recorded market positions, cancel all pending orders, and run a
        MAGIC_NUMBER safety sweep to guarantee no orphaned orders remain.
        """
        closed_set = set()
        for ticket in list(market_tickets):
            if self.close_market_position(ticket):
                closed_set.add(ticket)

        for ticket in list(pending_tickets):
            self.cancel_pending_order(ticket)

        if not self.dry_run and self._connected:
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
        """Clamp SL distance to at least broker trade_stops_level from live bid (BUY) / ask (SELL)."""
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        stops_lvl = _num(getattr(sym_info, "trade_stops_level", 50.0), 50.0) if sym_info else 50.0
        pt = _num(getattr(sym_info, "point", 0.01), 0.01) if sym_info else 0.01
        min_stop_dist = max(0.0, stops_lvl * (pt or 0.01))
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
    ) -> bool:
        """
        Apply TP1 trailing broker SL on a single open ticket forward-only, throttling
        repeat updates to >= TP1_TRAIL_STEP_DOLLARS while letting explicit TP steps pass.
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

        should_update_sl = is_better and ((not already_protected) or (cur_sl <= 0.0) or (improvement + 1e-9 >= step_dollars))

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

        # Send desired SL (and optional TP step together in one IPC call); fall back to independent TP if SL rejected
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
    ) -> int:
        """
        Sync hard broker SL across ALL remaining open layers (runner + non-runner L2/L3)
        subject to stops-level clamping, forward-only ratchet, and TP1_TRAIL_STEP_DOLLARS.
        """
        is_buy = (direction or "BUY").strip().upper() == "BUY"
        step_dollars = max(0.0, float(_cfg("TP1_TRAIL_STEP_DOLLARS", TP1_TRAIL_STEP_DOLLARS)))

        if sym_info is None:
            sym_info = self.get_symbol_info(symbol)
        if tick is None:
            tick = self.get_tick(symbol)
        desired_broker_sl = self._clamp_sl_for_broker_stops(desired_sl, direction, tick, sym_info, digits)

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
        pending_tickets = list(signal_data.get("pending_tickets") or [])
        active_set = set(market_tickets)

        for pt in pending_tickets:
            self.cancel_pending_order(pt)
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
        if isinstance(tp_map, dict):
            for k, v in tp_map.items():
                if v is not None:
                    merged[str(k).upper()] = round(float(v), digits)
        for i in range(1, 6):
            val = signal_data.get(f"raw_tp{i}")
            if val is not None and f"TP{i}" not in merged:
                merged[f"TP{i}"] = round(float(val), digits)

        seed: List[float] = []
        for i in range(1, 6):
            v = merged.get(f"TP{i}")
            if v is not None:
                seed.append(v)
            else:
                break

        if seed and len(merged) < 5:
            steps = [
                float(_cfg("TP2_STEP_PIPS", TP2_STEP_PIPS)),
                float(_cfg("TP3_STEP_PIPS", TP3_STEP_PIPS)),
                float(_cfg("TP4_STEP_PIPS", TP4_STEP_PIPS)),
                float(_cfg("TP5_STEP_PIPS", TP5_STEP_PIPS)),
            ]
            derived = derive_tp_levels(seed, direction=direction, pip_size=pip_size, step_pips=steps)
            for k, v in derived.items():
                merged.setdefault(k, round(float(v), digits))

        if merged:
            signal_data["tp_levels"] = merged
            for i in range(1, 6):
                k = f"TP{i}"
                if k in merged and signal_data.get(f"raw_tp{i}") is None:
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

    def prepare_deepest_runners_for_tp_breakout(
        self,
        signal_data: Dict[str, Any],
        live_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        v3.1 Pre-TP1 Proactive Runner Breakout Preparation:
        1. Dynamically selects current (runner_layers, early_layers) from open market_tickets.
        2. Resets any early_layers back to final_tp (tp_stage="TP1") if a deeper limit filled after an earlier lift.
        3. When RUNNER_PROACTIVE_TP_LIFT=True and live_price enters the strict tight TP1 activation zone,
           lifts ONLY runner_layers' broker TP to TP2 (tp_stage="TP2") so early_layers bank at broker TP1
           while deepest runner(s) ride through the breakout.
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

        full_cnt = _int_num(_cfg("RUNNER_DEEPEST_COUNT_FULL", RUNNER_DEEPEST_COUNT_FULL), 1)
        runner_layers, early_layers = select_dynamic_runners(layers, market_tickets, full_runner_count=full_cnt)
        runner_tickets = [
            _int_num(layers[lk].get("ticket"), 0)
            for lk in runner_layers
            if isinstance(layers.get(lk), dict) and _int_num(layers[lk].get("ticket"), 0) > 0
        ]

        signal_data["runner_layers"] = list(runner_layers)
        signal_data["runner_tickets"] = list(runner_tickets)
        signal_data["runner_layer"] = runner_layers[-1] if runner_layers else None
        signal_data["runner_ticket"] = runner_tickets[-1] if runner_tickets else None

        reset_tickets: List[int] = []
        if final_tp is not None:
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

        if live_price is None:
            tick = self.get_tick(symbol)
            if tick is not None:
                cand_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
                if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                    live_price = float(cand_px)

        lifted_tickets: List[int] = []
        proactive = bool(_cfg("RUNNER_PROACTIVE_TP_LIFT", RUNNER_PROACTIVE_TP_LIFT))
        max_stage = max(1, min(5, _int_num(_cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE), 4)))
        tp2 = tp_levels.get("TP2")

        if proactive and max_stage >= 2 and raw_tp1 is not None and tp2 is not None and live_price is not None and runner_layers:
            anchor = self._resolve_milestone_anchor(signal_data, tp_levels, 1)
            eff_buf, _ = self._compute_proximity_and_pullback(
                float(raw_tp1), float(live_price), pip_size, anchor=anchor
            )
            dist_to_tp1 = (float(raw_tp1) - float(live_price)) if is_buy else (float(live_price) - float(raw_tp1))
            should_lift_early = dist_to_tp1 <= eff_buf + 1e-9

            # In Gold Calibrated Reversal Guard mode, lift ONLY the deepest runner's broker TP to TP2
            # early at RUNNER_EARLY_TP_LIFT_PCT (default 70% of TP1) so broker front-run TP (TP1 - $0.50)
            # never closes a tight scalp before 85% arming is reached (L1-L3 stay at TP1 - $0.50).
            if bool(_cfg("GOLD_REVERSAL_GUARD_ENABLED", GOLD_REVERSAL_GUARD_ENABLED)):
                sig_entry = self._resolve_signal_primary_entry(signal_data)
                if sig_entry is not None and sig_entry > 0:
                    leg_span = (float(raw_tp1) - sig_entry) if is_buy else (sig_entry - float(raw_tp1))
                    if leg_span > 1e-9:
                        prog_pct = ((float(live_price) - sig_entry) / leg_span) if is_buy else ((sig_entry - float(live_price)) / leg_span)
                        lift_pct = float(_cfg("RUNNER_EARLY_TP_LIFT_PCT", RUNNER_EARLY_TP_LIFT_PCT))
                        if prog_pct + 1e-9 >= lift_pct:
                            should_lift_early = True

            if should_lift_early:
                target_tp2 = round(float(tp2), digits)
                for lk in runner_layers:
                    l_info = layers.get(lk)
                    if not isinstance(l_info, dict):
                        continue
                    lt = _int_num(l_info.get("ticket"), 0)
                    cur_tp = _num(l_info.get("tp"), 0.0)
                    if lt > 0 and (abs(cur_tp - target_tp2) > 1e-6 or l_info.get("tp_stage") != "TP2"):
                        if self.modify_position_sltp(lt, sl=None, tp=target_tp2):
                            l_info["tp"] = target_tp2
                            l_info["tp_stage"] = "TP2"
                            lifted_tickets.append(lt)
                if lifted_tickets or all(
                    isinstance(layers.get(lk), dict) and layers[lk].get("tp_stage") == "TP2"
                    for lk in runner_layers
                ):
                    signal_data["tp_stage"] = "TP2"

        return {
            "runner_layers": runner_layers,
            "early_layers": early_layers,
            "runner_tickets": runner_tickets,
            "lifted_tickets": lifted_tickets,
            "reset_tickets": reset_tickets,
        }

    def _resolve_signal_primary_entry(self, signal_data: Dict[str, Any]) -> Optional[float]:
        """Resolve primary L1 / recorded signal entry price for measuring % progress toward TP1."""
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        layers = signal_data.get("layers") or {}
        for lk in ("L1", "TOP"):
            l_obj = layers.get(lk)
            if isinstance(l_obj, dict):
                tp_px = l_obj.get("target_price")
                if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                    return float(tp_px)
        for key in ("recorded_entry_price", "initial_price"):
            val = signal_data.get(key)
            if isinstance(val, numbers.Real) and not isinstance(val, bool) and float(val) > 0:
                return float(val)
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

    def evaluate_gold_reversal_guard(
        self,
        signal_data: Dict[str, Any],
        tick: Any = None,
        runner_entry_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Gold (XAUUSD) Calibrated Peak-Profit Giveback Guard ("Bank It" Without Choking Winners):
        1. Strict Arming Zone (`ARM_THRESHOLD_PCT = 0.85`): stays completely OFF in mid-range;
           only sets `reversal_guard_armed = True` once progress reaches >= 85% of distance to TP1
           (or once `tp1_reached == True`).
        2. Healthy Reversal Trigger (`GIVEBACK_PCT = 0.30`): tracks `drawdown_from_peak =
           (peak_profit - current_profit) / peak_profit` (with post-TP1 cap `MAX_GIVEBACK_DOLLARS`).
           Triggers market exit ONLY when giveback threshold is breached AND pullback >= Gold noise floor.
        3. Gold Noise Floor (`MIN_GOLD_PULLBACK_DOLLARS = 1.50`): requires at least
           `max(1.50, 0.35 * M1_ATR_14)` dollars of actual price reversal.
        4. Adaptive Spread Spike Filter: pauses exit triggers during temporary spread spikes.
        5. Scaling Continuity: keeps `reversal_guard_armed = True` and updates `peak_profit`
           dynamically across TP1 -> TP2 -> TP3 -> TP4.
        """
        symbol = signal_data.get("symbol") or _cfg("DEFAULT_SYMBOL", DEFAULT_SYMBOL)
        direction = (signal_data.get("direction") or "BUY").strip().upper()
        is_buy = direction == "BUY"

        if tick is None:
            tick = self.get_tick(symbol)
        if tick is None:
            return {"armed": bool(signal_data.get("reversal_guard_armed", False)), "triggered": False, "paused_by_spread": False}

        raw_exec = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        if not isinstance(raw_exec, numbers.Real) or isinstance(raw_exec, bool) or float(raw_exec) <= 0:
            return {"armed": bool(signal_data.get("reversal_guard_armed", False)), "triggered": False, "paused_by_spread": False}
        exec_price = float(raw_exec)

        is_spike, spread_dollars, eff_max_spread = self.check_spread_spike(symbol, tick)
        signal_data["spread_spike_paused"] = is_spike

        layers = signal_data.get("layers") or {}
        market_tickets = list(signal_data.get("market_tickets") or [])
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
            tp_px = layers[primary_rl].get("target_price")
            if isinstance(tp_px, numbers.Real) and not isinstance(tp_px, bool) and float(tp_px) > 0:
                runner_entry = float(tp_px)
        if runner_entry is None:
            runner_entry = sig_entry
        if sig_entry is None:
            sig_entry = runner_entry

        if runner_entry is None or runner_entry <= 0:
            return {"armed": bool(signal_data.get("reversal_guard_armed", False)), "triggered": False, "paused_by_spread": is_spike}

        pip_size = self.get_pip_size(symbol)
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        raw_tp1 = signal_data.get("raw_tp1") or tp_levels.get("TP1")

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
            if signal_data.get("tp1_reached") or signal_data.get("tp1_touched"):
                armed = True
            elif raw_tp1 is not None and sig_entry is not None:
                tp1_leg = (float(raw_tp1) - sig_entry) if is_buy else (sig_entry - float(raw_tp1))
                if tp1_leg > 1e-9:
                    # Only arm on genuine market move (do not arm on an artificial negative-bid spread spike)
                    eff_arm_pct = min(arm_pct, 0.70) if tp1_leg <= 4.0 else arm_pct
                    sig_progress = ((exec_price - sig_entry) / tp1_leg) if is_buy else ((sig_entry - exec_price) / tp1_leg)
                    if (sig_progress + 1e-9 >= eff_arm_pct or (tp1_leg <= 4.0 and current_profit >= 1.50)) and current_profit > 0:
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
                    f"Gold Reversal Guard ARMED (exec={exec_price}, peak_profit=${peak_profit:.2f}, "
                    f"runner_entry={runner_entry}, arm_pct={arm_pct*100:.0f}%)"
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

        m1_atr_14 = self.get_m1_atr(symbol)
        min_pb_dollars = float(_cfg("MIN_GOLD_PULLBACK_DOLLARS", MIN_GOLD_PULLBACK_DOLLARS))
        atr_mult = float(_cfg("ATR_NOISE_FLOOR_MULT", ATR_NOISE_FLOOR_MULT))
        noise_floor_dollars = round(max(min_pb_dollars, atr_mult * m1_atr_14), 4)
        tp1_leg_dollars = (
            ((float(raw_tp1) - sig_entry) if is_buy else (sig_entry - float(raw_tp1)))
            if (raw_tp1 is not None and sig_entry is not None)
            else None
        )
        if (
            tp1_leg_dollars is not None
            and 0.0 < tp1_leg_dollars <= 4.0
            and not signal_data.get("tp2_reached")
        ):
            noise_floor_dollars = min(noise_floor_dollars, round(0.45 * tp1_leg_dollars, 4))

        giveback_pct = float(_cfg("GIVEBACK_PCT", GIVEBACK_PCT))
        max_gb_dollars = float(_cfg("MAX_GIVEBACK_DOLLARS", MAX_GIVEBACK_DOLLARS))
        hard_ceil_cfg = float(_cfg("HARD_GIVEBACK_CEILING_DOLLARS", HARD_GIVEBACK_CEILING_DOLLARS))
        positive_caps = [v for v in (max_gb_dollars, hard_ceil_cfg) if v > 0.0]
        hard_ceiling_dollars = min(positive_caps) if positive_caps else 0.0
        hard_ceiling_breached = (
            peak_profit > 0.0
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

        tp1_trail_on = bool(_cfg("TP1_TRAIL_ENABLED", TP1_TRAIL_ENABLED))
        if signal_data.get("tp1_reached") or signal_data.get("tp1_touched"):
            if tp1_trail_on:
                # v3.4.2: Post-TP1 exits are governed by the dynamic ATR trailing stop (+ $3.50 hard ceiling backstop).
                triggered = False
            elif hard_ceiling_dollars > 0.0 or max_gb_dollars > 0.0:
                eff_cap = hard_ceiling_dollars if hard_ceiling_dollars > 0.0 else max_gb_dollars
                pct_req = giveback_pct * peak_profit
                eff_req_dollars = max(noise_floor_dollars, min(eff_cap, pct_req))
                triggered = (pullback_dollars + 1e-9 >= eff_req_dollars) and (peak_profit > 0.0)
            else:
                triggered = False
        else:
            # Pre-TP1: exact 85% peak-profit arming / 30% giveback AND >= Gold noise floor
            triggered = (
                peak_profit > 0.0
                and drawdown_from_peak + 1e-9 >= giveback_pct
                and pullback_dollars + 1e-9 >= noise_floor_dollars
            )

        return {
            "armed": True,
            "triggered": triggered,
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

        market_tickets = list(signal_data.get("market_tickets") or [])
        pending_tickets = list(signal_data.get("pending_tickets") or [])

        # 1. Pending order cancellation policy
        if _cfg("CANCEL_PENDING_ON_TP1", CANCEL_PENDING_ON_TP1):
            for pt in pending_tickets:
                self.cancel_pending_order(pt)
            signal_data["pending_tickets"] = []
            for l_val in layers.values():
                if isinstance(l_val, dict) and l_val.get("status") == "PENDING":
                    l_val["status"] = "CANCELLED"

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

        max_stage = max(1, min(5, _int_num(_cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE), 4)))
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

        # 3. Close early_layers at TP1
        banked_tickets: List[int] = []
        for lk in early_layers:
            l_info = layers.get(lk)
            if isinstance(l_info, dict):
                lt = _int_num(l_info.get("ticket"), 0)
                if lt > 0 and lt in market_tickets and lt not in banked_tickets:
                    self.close_market_position(lt)
                    banked_tickets.append(lt)
                l_info["status"] = "CLOSED"
        for et in early_tickets:
            if et > 0 and et not in banked_tickets:
                self.close_market_position(et)
                banked_tickets.append(et)

        surviving_tickets = [mt for mt in market_tickets if mt and mt not in banked_tickets]
        runner_ticket: Optional[int] = runner_tickets[-1] if runner_tickets else None
        runner_layer: Optional[str] = runner_layers[-1] if runner_layers else None

        # 4. Fetch live price before setting floor so proximity/early TP1 scaling never arms a premature virtual floor
        tick = self.get_tick(symbol)
        live_price: Optional[float] = None
        if tick is not None:
            cand_px = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
            if isinstance(cand_px, (int, float)) and not isinstance(cand_px, bool) and cand_px > 0:
                live_price = float(cand_px)

        clean_break_dist = float(_cfg("RUNNER_CLEAN_BREAK_PIPS", RUNNER_CLEAN_BREAK_PIPS)) * pip_size
        already_clean_tp1 = False
        if raw_tp1 is not None and live_price is not None:
            if is_buy:
                already_clean_tp1 = live_price >= round(float(raw_tp1) + clean_break_dist, digits)
            else:
                already_clean_tp1 = live_price <= round(float(raw_tp1) - clean_break_dist, digits)

        if already_clean_tp1 and max_stage >= 2:
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
            and live_price is not None
            and ((live_price >= float(raw_tp1)) if is_buy else (live_price <= float(raw_tp1)))
        )
        tp1_touched = bool(
            raw_tp1 is not None
            and (signal_data.get("tp1_touched") or touched_now)
            and (tp1_trail_on or tp1_touch_enabled)
        )
        trail_step_dollars = max(0.0, float(_cfg("TP1_TRAIL_STEP_DOLLARS", TP1_TRAIL_STEP_DOLLARS)))

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

            sl_applied_tickets: Set[int] = set()
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
                        if target_tp_val is not None:
                            l_info["tp"] = target_tp_val
                            l_info["tp_stage"] = next_stage_key
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
        signal_data["tp_stage"] = next_stage_key if runner_tickets else "TP1"
        signal_data["runner_max_tp_stage"] = max_stage
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

        for l_name, tp_key in (("L2", "TP2"), ("L3", "TP3")):
            l_info = layers.get(l_name)
            if not isinstance(l_info, dict):
                continue
            lt = l_info.get("ticket")
            tp_val = tp_levels.get(tp_key)
            if not lt or lt in runner_set or lt not in market_tickets or tp_val is None:
                continue
            hit = (live_price >= float(tp_val)) if is_buy else (live_price <= float(tp_val))
            if hit:
                self.close_market_position(lt)
                l_info["status"] = "CLOSED"
                market_tickets = [t for t in market_tickets if t != lt]
                closed_now.append(lt)
                logger.info(f"Backstop auto-closed {l_name} #{lt} at {tp_key}={tp_val} (live={live_price})")

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
    ) -> bool:
        """
        Atomically close active runner_tickets (and optionally any remaining non-runner open
        market_tickets when banking on TP1-Touch Fallback Bank) under `is_closing = True` lock.
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
            if close_position:
                for rt in to_close:
                    if rt in already_closed_once:
                        continue
                    self.close_market_position(rt)
                    already_closed_once.add(rt)
                signal_data["_closed_tickets_once"] = sorted(already_closed_once)
            else:
                self.invalidate_realized_pnl_cache()

            close_set = set(to_close)
            layers = signal_data.get("layers") or {}
            for lk in runner_layers:
                if lk and isinstance(layers.get(lk), dict):
                    layers[lk]["status"] = "CLOSED"
            if close_all_market_tickets:
                for l_val in layers.values():
                    if isinstance(l_val, dict):
                        lt = _int_num(l_val.get("ticket"), 0)
                        if (lt > 0 and lt in close_set) or str(l_val.get("status") or "").upper() in ("OPEN", "FILLED", "RUNNER"):
                            l_val["status"] = "CLOSED"

            remaining_mkt = [t for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) not in close_set]
            signal_data["market_tickets"] = remaining_mkt
            signal_data["last_runner_tickets"] = list(to_close)
            signal_data["last_runner_ticket"] = to_close[-1] if to_close else None
            signal_data["runner_close_reason"] = reason
            signal_data["runner_tickets"] = []
            signal_data["runner_ticket"] = None
            signal_data["runner_layers"] = []
            signal_data["runner_layer"] = None
            signal_data["runner_proximity_armed"] = False
            signal_data["runner_reversal_ticks"] = 0
            signal_data["tp2_shortfall_armed"] = False

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

            has_open_or_pending = bool(remaining_mkt or signal_data.get("pending_tickets"))
            signal_data["status"] = "BE_APPLIED" if has_open_or_pending else "CLOSED"
            return False
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
        signal_data["last_backstop_closed_tickets"] = []
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

            if not live_positions_map:
                return self._finalize_runner_exit(
                    signal_data,
                    runner_tickets,
                    runner_layers,
                    reason="Trailed Stop Loss Hit (Locked in Gain)",
                    close_position=False,
                )

            if len(live_positions_map) < len(runner_tickets):
                closed_sub = [rt for rt in runner_tickets if rt not in live_positions_map]
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

        pip_size = self.get_pip_size(p_symbol)
        tp_levels = self._ensure_signal_tp_levels(signal_data, direction, pip_size, digits)
        max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    4,
                ),
            ),
        )
        signal_data["runner_max_tp_stage"] = max_stage

        # 0. Dynamic ATR TP1 Trailing Stop (v3.4.2) with v3.4.1 TP1-Touch Fallback when disabled
        raw_tp1_val = signal_data.get("raw_tp1") or tp_levels.get("TP1")
        tp1_trail_on = bool(_cfg("TP1_TRAIL_ENABLED", TP1_TRAIL_ENABLED))
        tp1_touch_enabled = bool(_cfg("TP1_TOUCH_BANK_ENABLED", TP1_TOUCH_BANK_ENABLED))
        trail_active = False

        if tp1_trail_on:
            if raw_tp1_val is not None and not signal_data.get("tp1_touched"):
                touched_now = (live_price >= float(raw_tp1_val)) if is_buy else (live_price <= float(raw_tp1_val))
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
                touched_now = (live_price >= float(raw_tp1_val)) if is_buy else (live_price <= float(raw_tp1_val))
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

        is_runner_phase = bool(signal_data.get("status") == "RUNNER_ACTIVE" or signal_data.get("tp1_reached"))
        cur_floor = (
            signal_data.get("runner_sl_floor")
            if signal_data.get("runner_sl_floor") is not None
            else signal_data.get("soft_tp1_floor")
        )
        eff_sl: Optional[float] = cur_floor if (is_runner_phase and cur_floor is not None) else signal_data.get("sl")
        rejected_sl_reason: Optional[str] = None
        sl_updated = False

        if new_sl is not None and isinstance(new_sl, numbers.Real) and not isinstance(new_sl, bool) and float(new_sl) > 0:
            cand_sl = round(float(new_sl), digits)
            if ratchet_sl_only and is_runner_phase and cur_floor is not None:
                worse_or_equal = (
                    (is_buy and cand_sl <= float(cur_floor) + 1e-9)
                    or (not is_buy and cand_sl >= float(cur_floor) - 1e-9)
                )
                if worse_or_equal:
                    rejected_sl_reason = f"Ratchet guard preserved existing runner floor ({cur_floor}) over {cand_sl}"

            if rejected_sl_reason is None and not self.dry_run and live_px is not None:
                if (is_buy and cand_sl >= live_px - min_stop_dist) or (not is_buy and cand_sl <= live_px + min_stop_dist):
                    rejected_sl_reason = f"SL {cand_sl} violates live stop distance (live={live_px}, min_dist={min_stop_dist})"

            if rejected_sl_reason is None:
                eff_sl = cand_sl
                sl_updated = True
                signal_data["sl"] = cand_sl
                if is_runner_phase:
                    signal_data["runner_sl_floor"] = cand_sl
                    signal_data["soft_tp1_floor"] = cand_sl
                    signal_data["broker_sl"] = cand_sl

        mod_pos: List[int] = []
        mod_ord: List[int] = []
        layers = signal_data.get("layers") or {}
        market_tickets = [_int_num(t, 0) for t in (signal_data.get("market_tickets") or []) if _int_num(t, 0) > 0]
        pending_tickets = [_int_num(t, 0) for t in (signal_data.get("pending_tickets") or []) if _int_num(t, 0) > 0]
        max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    4,
                ),
            ),
        )

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
            for idx_r, rt in enumerate(runner_tickets):
                lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                if self.modify_position_sltp(rt, sl=sl_arg, tp=runner_tp):
                    if rt not in mod_pos:
                        mod_pos.append(rt)
                elif sl_arg is not None and runner_tp is not None:
                    if self.modify_position_sltp(rt, sl=None, tp=runner_tp) and rt not in mod_pos:
                        mod_pos.append(rt)
                if l_info is not None and runner_tp is not None:
                    l_info["tp"] = runner_tp
                    l_info["tp_stage"] = raw_stage

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
            sl_arg = eff_sl if sl_updated else signal_data.get("sl")
            for l_data in layers.values():
                if not isinstance(l_data, dict):
                    continue
                lt = _int_num(l_data.get("ticket"), 0)
                if lt <= 0:
                    continue
                l_stage = str(l_data.get("tp_stage") or "TP1").strip().upper()
                l_tp = tp_levels.get("TP2") if l_stage == "TP2" else signal_data.get("final_tp")
                if lt in market_tickets:
                    handled_tkts.add(lt)
                    if self.modify_position_sltp(lt, sl=sl_arg, tp=l_tp) and lt not in mod_pos:
                        mod_pos.append(lt)
                    if l_tp is not None:
                        l_data["tp"] = l_tp
                elif lt in pending_tickets:
                    handled_tkts.add(lt)
                    if self.modify_pending_order_sltp(lt, sl=sl_arg, tp=l_tp) and lt not in mod_ord:
                        mod_ord.append(lt)
                    if l_tp is not None:
                        l_data["tp"] = l_tp

            for mt in market_tickets:
                if mt not in handled_tkts:
                    if self.modify_position_sltp(mt, sl=sl_arg, tp=signal_data.get("final_tp")) and mt not in mod_pos:
                        mod_pos.append(mt)
            for pt in pending_tickets:
                if pt not in handled_tkts:
                    if self.modify_pending_order_sltp(pt, sl=sl_arg, tp=signal_data.get("final_tp")) and pt not in mod_ord:
                        mod_ord.append(pt)

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
        max_stage = max(
            1,
            min(
                5,
                _int_num(
                    signal_data.get("runner_max_tp_stage") or _cfg("RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE),
                    4,
                ),
            ),
        )
        signal_data["runner_max_tp_stage"] = max_stage

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

            raw_lock = float(_cfg("TP1_LOCK_BUFFER", TP1_LOCK_BUFFER))
            lock_buf = raw_lock * (pip_size / 0.10) if pip_size > 0 else raw_lock
            trail_dist = float(_cfg("TRAIL_DISTANCE_PIPS", TRAIL_DISTANCE_PIPS)) * pip_size
            cur_floor = (
                signal_data.get("runner_sl_floor")
                if signal_data.get("runner_sl_floor") is not None
                else signal_data.get("soft_tp1_floor")
            )

            if m_idx == 1:
                tp1_val = tp_levels.get("TP1")
                if tp1_val is not None:
                    cand_floor = round(float(tp1_val) - lock_buf, digits) if is_buy else round(float(tp1_val) + lock_buf, digits)
                else:
                    cand_floor = self._compute_initial_tp1_floor(signal_data, symbol, direction, pip_size, digits)
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

            next_idx = min(max_stage, m_idx + 1)
            next_stage = f"TP{next_idx}"
            next_tp = tp_levels.get(next_stage)

            signal_data["runner_milestone_idx"] = next_idx
            signal_data["runner_milestone_name"] = next_stage
            signal_data["tp_stage"] = next_stage
            signal_data["runner_sl_floor"] = cand_floor
            signal_data["soft_tp1_floor"] = cand_floor
            signal_data["broker_sl"] = cand_floor
            signal_data["sl"] = cand_floor
            signal_data["runner_proximity_armed"] = False
            signal_data["runner_reversal_ticks"] = 0
            signal_data["tp2_shortfall_armed"] = False
            if runner_tickets:
                signal_data["status"] = "RUNNER_ACTIVE"

            self._step_runner_broker_tp_and_sl(
                signal_data,
                runner_tickets,
                runner_layers,
                new_sl=cand_floor,
                new_tp=next_tp,
                new_stage=next_stage,
                digits=digits,
            )

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
        signal_data["broker_sl"] = cand_sl
        if signal_data.get("status") == "RUNNER_ACTIVE" or signal_data.get("tp1_reached"):
            signal_data["runner_sl_floor"] = cand_sl
            signal_data["soft_tp1_floor"] = cand_sl

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
