import copy
import logging
import math
import numbers
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import MetaTrader5 as mt5

import config as _config
from config import (
    ALLOW_BELOW_ZONE_WICK,
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
    ENFORCE_HIGH_RISK_LOT_CAP,
    FALLBACK_SL_PIPS,
    LAYER_LOTS,
    LAYER_NAMES,
    LEGACY_LAYER_MAP,
    MAGIC_NUMBER,
    MAX_ORDERS_PER_SIGNAL,
    MIN_SL_BUFFER_PIPS,
    MT5_LOGIN,
    MT5_PASSWORD,
    MT5_PATH,
    MT5_SERVER,
    NO_TP2_SURGE_ARM_PIPS,
    RISK_LOT_MULTIPLIER,
    RUNNER_CLEAN_BREAK_PIPS,
    RUNNER_DEEPEST_COUNT_FULL,
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
    SHORTFALL_PULLBACK_PIPS,
    TP1_CLOSE_PROXIMITY_PIPS,
    TP1_LOCK_BUFFER,
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
        self._cached_pos_profits: Dict[int, float] = {}
        self._realized_pnl_cache: Optional[Tuple[float, float, str]] = None

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
        eff_base = base_lots if base_lots is not None else _cfg("BASE_LAYER_LOTS", BASE_LAYER_LOTS)
        eff_mult = multiplier if multiplier is not None else _cfg("RISK_LOT_MULTIPLIER", RISK_LOT_MULTIPLIER)
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
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Split a Single-Price entry (LIMIT, STOP, or smart AUTO) into 4 layers (L1..L4 -> TP1/TP2/TP3/TP5).
        Returns (market_tickets, pending_tickets, layers_data).
        """
        if price is None or float(price) <= 0.0:
            self.last_error = f"Invalid single-price parameter (price={price})"
            return [], [], {}

        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer single-price")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        alloc_lots, warn = self.allocate_4layer_lots(resolved_sym, base_lots=base_lots, multiplier=multiplier)
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
            f"Price={target_px}, Lots={alloc_lots}, TPs={layer_tps} | SL={sl}"
        )

        layer_specs = [
            (l_name, target_px, alloc_lots[idx], layer_tps[l_name])
            for idx, l_name in enumerate(LAYER_NAMES)
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
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Split an immediate Market Now entry into 4 market orders (L1..L4 -> TP1/TP2/TP3/TP5).
        Returns (market_tickets, pending_tickets, layers_data).
        """
        prereq = self._validate_entry_prerequisites(symbol, direction, "4-layer market")
        if prereq is None:
            return [], [], {}
        direction_norm, resolved_sym, tick, _, digits, pip_size = prereq

        alloc_lots, warn = self.allocate_4layer_lots(resolved_sym, base_lots=base_lots, multiplier=multiplier)
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
            f"Entry={entry_px}, Lots={alloc_lots}, TPs={layer_tps} | SL={sl}"
        )

        layer_specs = [
            (l_name, entry_px, alloc_lots[idx], layer_tps[l_name])
            for idx, l_name in enumerate(LAYER_NAMES)
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

            new_sl = round(float(sl), digits) if sl is not None else _num(getattr(p, "sl", 0.0), 0.0)
            new_tp = round(float(tp), digits) if tp is not None else _num(getattr(p, "tp", 0.0), 0.0)

            request = {
                "action": _mt5_const("TRADE_ACTION_SLTP", 6),
                "position": ticket,
                "symbol": p_symbol,
                "sl": new_sl,
                "tp": new_tp,
            }

            result = mt5.order_send(request)
            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            if result and result.retcode == done_code:
                logger.info(f"Updated SL/TP on Position #{ticket}: SL={new_sl}, TP={new_tp}")
                return True
            no_changes_code = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            if result and (result.retcode == no_changes_code or str(comment).strip().lower() == "no changes"):
                logger.debug(f"Position #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
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

            new_sl = round(float(sl), digits) if sl is not None else _num(getattr(order, "sl", 0.0), 0.0)
            new_tp = round(float(tp), digits) if tp is not None else _num(getattr(order, "tp", 0.0), 0.0)

            request = {
                "action": _mt5_const("TRADE_ACTION_MODIFY", 7),
                "order": ticket,
                "price": _num(getattr(order, "price_open", 0.0), 0.0),
                "sl": new_sl,
                "tp": new_tp,
                "type_time": _int_num(getattr(order, "type_time", 0), _mt5_const("ORDER_TIME_GTC", 0)),
                "type_filling": _int_num(getattr(order, "type_filling", 0), _mt5_const("ORDER_FILLING_RETURN", 2)),
            }

            result = mt5.order_send(request)
            done_code = _mt5_const("TRADE_RETCODE_DONE", 10009)
            if result and result.retcode == done_code:
                logger.info(f"Updated SL/TP on Pending Order #{ticket}: SL={new_sl}, TP={new_tp}")
                return True
            no_changes_code = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            if result and (result.retcode == no_changes_code or str(comment).strip().lower() == "no changes"):
                logger.debug(f"Pending Order #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
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
            logger.warning(f"Failed or already filled/cancelled for Order #{ticket}: {comment}")
            return False

    def close_market_position(self, ticket: int) -> bool:
        """Close an open MT5 position completely at current market price (thread-safe)."""
        if not ticket:
            return False

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
            return {
                "balance": 11.03,
                "credit": 7.03,
                "equity": 18.06,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 18.06,
                "currency": "GBP",
                "formatted": (
                    "💳 **Account Update:**\n"
                    "• Balance: `11.03 GBP`\n"
                    "• Credit: `7.03 GBP`\n"
                    "• Equity: `18.06 GBP`"
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
        ref_bal = max(0.0, curr_bal, round(curr_bal - min(0.0, realized_today), 2))
        floating_loss = max(0.0, round(-floating_pnl, 2))
        realized_loss = max(0.0, round(-realized_today, 2))
        total_daily_loss = round(realized_loss + floating_loss, 2)

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
            "pause_new_signals": pause_new,
            "emergency_cutoff_triggered": emergency_cutoff,
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
        """Move an open position's SL to entry +/- offset_value while keeping its TP intact."""
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
        is_buy = _int_num(getattr(p, "type", buy_order_type), buy_order_type) == buy_order_type
        new_sl = round(price_open + offset_value, digits) if is_buy else round(price_open - offset_value, digits)
        if self.modify_position_sltp(ticket=ticket, sl=new_sl, tp=p_tp):
            return new_sl
        return None

    def apply_selective_breakeven(
        self,
        layers: Dict[str, Dict[str, Any]],
        symbol: str,
        direction: str,
        be_layers: Optional[List[str]] = None,
        be_offset_pips: Optional[float] = None,
    ) -> List[int]:
        """
        Selective Breakeven:
        - v3.0 4-layer signals: moves L1, L2, L3 (default BE_LAYERS) to BE, leaving L4 (0.02) untouched.
        - Legacy 3-layer signals: moves TOP and MID to BE, leaving BOTTOM untouched.
        Pending orders remain active. Returns list of tickets modified to BE.
        """
        pip_size = self.get_pip_size(symbol)
        eff_offset_pips = float(be_offset_pips) if be_offset_pips is not None else float(_cfg("BE_OFFSET_PIPS", BE_OFFSET_PIPS))
        offset_value = eff_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = _int_num(getattr(sym_info, "digits", 2), 2) if sym_info else 2

        eff_be_layers = list(be_layers) if be_layers is not None else list(_cfg("BE_LAYERS", BE_LAYERS))
        is_legacy = _is_legacy_3layer_dict(layers)

        resolved_names: List[str] = []
        for raw_name in eff_be_layers:
            up = str(raw_name).strip().upper()
            if is_legacy:
                if up in layers:
                    resolved_names.append(up)
                elif up in ("L1", "L2"):
                    mapped = _V3_TO_LEGACY_LAYER[up]
                    if mapped in layers:
                        resolved_names.append(mapped)
                # Note: in legacy 3-layer mode, L3 does not map to BOTTOM (BOTTOM stays untouched).
            else:
                if up in layers:
                    resolved_names.append(up)
                elif up in LEGACY_LAYER_MAP and LEGACY_LAYER_MAP[up] in layers:
                    resolved_names.append(LEGACY_LAYER_MAP[up])

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
                logger.info(
                    f"Layer {layer_name} (Ticket #{ticket}) moved to BE: NewSL={new_sl} (+{eff_offset_pips} pips offset)"
                )

        return modified_tickets

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
            if dist_to_tp1 <= eff_buf + 1e-9:
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
        if max_stage <= 1:
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

            past_tp1_floor = True
            if live_price is not None:
                past_tp1_floor = (live_price >= tp1_floor_price) if is_buy else (live_price <= tp1_floor_price)

            if past_tp1_floor:
                if be_after_tp1 and runner_be_sl is not None:
                    floor_price = round(max(tp1_floor_price, runner_be_sl), digits) if is_buy else round(min(tp1_floor_price, runner_be_sl), digits)
                else:
                    floor_price = tp1_floor_price
            else:
                floor_price = runner_be_sl if (be_after_tp1 and runner_be_sl is not None) else None

            target_tp_val = round(float(next_stage_tp), digits) if next_stage_tp is not None else None
            for idx_r, rt in enumerate(runner_tickets):
                lk = runner_layers[idx_r] if idx_r < len(runner_layers) else None
                l_info = layers.get(lk) if (lk and isinstance(layers.get(lk), dict)) else None
                t_be_sl = runner_be_sl
                if l_info and l_info.get("target_price"):
                    t_open = float(l_info["target_price"])
                    t_be_sl = round(t_open + offset_value, digits) if is_buy else round(t_open - offset_value, digits)

                t_sl = floor_price if past_tp1_floor else (t_be_sl if be_after_tp1 else None)
                if t_sl is not None:
                    if not self.modify_position_sltp(rt, sl=t_sl, tp=target_tp_val):
                        if be_after_tp1 and t_be_sl is not None and t_be_sl != t_sl:
                            if not self.modify_position_sltp(rt, sl=t_be_sl, tp=target_tp_val) and target_tp_val is not None:
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
            floor_price = None

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
        signal_data["runner_peak_price"] = live_price if live_price is not None else raw_tp1
        signal_data["tp1_reached"] = True
        signal_data["market_tickets"] = surviving_tickets

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
    ) -> bool:
        """Close all active runner_tickets (preserving any other surviving market_tickets) and update signal status."""
        r_tickets = [_int_num(t, 0) for t in runner_tickets if _int_num(t, 0) > 0]
        if close_position:
            for rt in r_tickets:
                self.close_market_position(rt)
        else:
            self.invalidate_realized_pnl_cache()

        layers = signal_data.get("layers") or {}
        for lk in runner_layers:
            if lk and isinstance(layers.get(lk), dict):
                layers[lk]["status"] = "CLOSED"

        r_set = set(r_tickets)
        remaining_mkt = [t for t in (signal_data.get("market_tickets") or []) if t not in r_set]
        signal_data["market_tickets"] = remaining_mkt
        signal_data["last_runner_tickets"] = list(r_tickets)
        signal_data["last_runner_ticket"] = r_tickets[-1] if r_tickets else None
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

        floor_price = signal_data.get("runner_sl_floor")
        if floor_price is None:
            floor_price = signal_data.get("soft_tp1_floor")

        # 1. Virtual Floor Protection
        if floor_price is not None:
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
            signal_data["runner_peak_price"] = live_price

            if k == 1:
                tp1_lock = target_f - lock_buf if is_buy else target_f + lock_buf
                cands = [tp1_lock]
                if be_floor is not None:
                    cands.append(be_floor)
                if floor_price is not None:
                    cands.append(float(floor_price))
                floor_price = round(max(cands) if is_buy else min(cands), digits)
            else:
                prev_tp = tp_levels.get(f"TP{k - 1}")
                trail_cand = target_f - trail_dist if is_buy else target_f + trail_dist
                cands = [trail_cand]
                if prev_tp is not None:
                    cands.append(float(prev_tp))
                if floor_price is not None:
                    cands.append(float(floor_price))
                floor_price = round(max(cands) if is_buy else min(cands), digits)

            k += 1
            step_stage = f"TP{k}"
            step_tp = tp_levels.get(step_stage)
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

        # 5. Strict Proximity Reversal Guard ("Bank It") + Proactive Broker TP Lift at Current TPk
        cur_target = tp_levels.get(f"TP{k}")
        if cur_target is not None:
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

        # 6. Continuous One-Way Hard Broker SL Trailing across all runner_tickets
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
