import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env file if it exists (does not overwrite pre-set environment variables)
load_dotenv(BASE_DIR / ".env")

_cfg_logger = logging.getLogger("copier.config")


# ---------------------------------------------------------------------------
# SAFE ENV PARSING & VALIDATION HELPERS
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable with safe fallback."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    _cfg_logger.warning(f"Invalid bool for {name}={raw!r}; using default {default}.")
    return default


def _env_int(
    name: str,
    default: int,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> int:
    """Parse and clamp an integer environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        val = default
    else:
        try:
            val = int(float(raw.strip()))
        except ValueError:
            _cfg_logger.warning(f"Invalid int for {name}={raw!r}; using default {default}.")
            val = default
    if min_val is not None and val < min_val:
        _cfg_logger.warning(f"Clamping {name}={val} up to {min_val}.")
        val = min_val
    if max_val is not None and val > max_val:
        _cfg_logger.warning(f"Clamping {name}={val} down to {max_val}.")
        val = max_val
    return val


def _env_float(
    name: str,
    default: float,
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
    positive_only: bool = False,
) -> float:
    """Parse, validate, and clamp a finite float environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        val = float(default)
    else:
        try:
            parsed = float(raw.strip())
            if not math.isfinite(parsed):
                raise ValueError("non-finite")
            val = parsed
        except ValueError:
            _cfg_logger.warning(f"Invalid float for {name}={raw!r}; using default {default}.")
            val = float(default)
    if positive_only and val <= 0.0:
        _cfg_logger.warning(f"{name}={val} must be > 0; using default {default}.")
        val = float(default)
    if min_val is not None and val < min_val:
        _cfg_logger.warning(f"Clamping {name}={val} up to {min_val}.")
        val = float(min_val)
    if max_val is not None and val > max_val:
        _cfg_logger.warning(f"Clamping {name}={val} down to {max_val}.")
        val = float(max_val)
    return val


def _env_choice(name: str, default: str, choices: Tuple[str, ...]) -> str:
    """Parse an uppercase enumerated choice environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().upper()
    if val in choices:
        return val
    _cfg_logger.warning(f"Invalid choice for {name}={raw!r} (expected {choices}); using {default}.")
    return default


def _parse_json_list(name: str, default: List[Any]) -> List[Any]:
    """Parse a JSON list or comma-separated list from env with safe fallback."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and parsed:
                return parsed
        except Exception as e:
            _cfg_logger.warning(f"Could not parse {name} JSON: {e}")
            return list(default)
        _cfg_logger.warning(f"{name} JSON must be a non-empty list; using default.")
        return list(default)
    items = [part.strip() for part in stripped.split(",") if part.strip()]
    return items if items else list(default)


def _is_positive_number(val: Any) -> bool:
    return (
        isinstance(val, (int, float))
        and not isinstance(val, bool)
        and math.isfinite(float(val))
        and float(val) > 0.0
    )


def _parse_base_layer_lots() -> List[float]:
    """
    Parse BASE_LAYER_LOTS (or legacy LAYER_LOTS) into 4 strictly positive floats.
    Falls back safely to [0.01, 0.01, 0.01, 0.02] on malformed or wrong-length input.
    """
    default_lots = [0.01, 0.01, 0.01, 0.02]
    raw_base = os.getenv("BASE_LAYER_LOTS")
    if raw_base and raw_base.strip():
        stripped = raw_base.strip()
        parsed_vals: Optional[List[Any]] = None
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                loaded = json.loads(stripped)
                if isinstance(loaded, list):
                    parsed_vals = loaded
                elif isinstance(loaded, dict) and all(k in loaded for k in ("L1", "L2", "L3", "L4")):
                    parsed_vals = [loaded["L1"], loaded["L2"], loaded["L3"], loaded["L4"]]
            except Exception as e:
                _cfg_logger.warning(f"Could not parse BASE_LAYER_LOTS JSON: {e}; using default {default_lots}.")
                return list(default_lots)
        else:
            try:
                parsed_vals = [float(p.strip()) for p in stripped.split(",") if p.strip()]
            except ValueError as e:
                _cfg_logger.warning(f"Could not parse BASE_LAYER_LOTS list: {e}; using default {default_lots}.")
                return list(default_lots)

        if parsed_vals and len(parsed_vals) == 4 and all(_is_positive_number(v) for v in parsed_vals):
            return [round(float(v), 4) for v in parsed_vals]

        _cfg_logger.warning(
            f"BASE_LAYER_LOTS must contain 4 positive numbers (got {raw_base!r}); using default {default_lots}."
        )
        return list(default_lots)

    # Backward-compatible check on LAYER_LOTS JSON env var
    raw_layer_dict = os.getenv("LAYER_LOTS")
    if raw_layer_dict and raw_layer_dict.strip():
        try:
            loaded = json.loads(raw_layer_dict.strip())
            if isinstance(loaded, dict):
                if all(k in loaded and _is_positive_number(loaded[k]) for k in ("L1", "L2", "L3", "L4")):
                    return [round(float(loaded[k]), 4) for k in ("L1", "L2", "L3", "L4")]
                if all(k in loaded and _is_positive_number(loaded[k]) for k in ("TOP", "MID", "BOTTOM")):
                    return [
                        round(float(loaded["TOP"]), 4),
                        round(float(loaded["MID"]), 4),
                        round(float(loaded["MID"]), 4),
                        round(float(loaded["BOTTOM"]), 4),
                    ]
            elif isinstance(loaded, list) and len(loaded) == 4 and all(_is_positive_number(v) for v in loaded):
                return [round(float(v), 4) for v in loaded]
        except Exception as e:
            _cfg_logger.warning(f"Could not parse LAYER_LOTS JSON: {e}; using default {default_lots}.")

    return list(default_lots)


# ===========================================================================
# STRATEGY & 4-LAYER ACTIVE RUNNER CONFIGURATION (v3.1)
# ===========================================================================

# Canonical 4-layer names & legacy v2 alias mapping
LAYER_NAMES: Tuple[str, ...] = ("L1", "L2", "L3", "L4")
LEGACY_LAYER_MAP: Dict[str, str] = {"TOP": "L1", "MID": "L2", "BOTTOM": "L4"}

# Base 4-layer lot split: [0.01, 0.01, 0.01, 0.02] (all layers start at TP1; deepest runner(s) scale TP1->TP4)
BASE_LAYER_LOTS: List[float] = _parse_base_layer_lots()
RISK_LOT_MULTIPLIER: float = _env_float(
    "RISK_LOT_MULTIPLIER", 1.0, min_val=0.01, max_val=100.0, positive_only=True
)
LAYER_LOTS: Dict[str, float] = {name: BASE_LAYER_LOTS[idx] for idx, name in enumerate(LAYER_NAMES)}

# Hard cap on orders placed per signal (4 layers across Zone, Single-Price LIMIT/STOP, and Market Now)
MAX_ORDERS_PER_SIGNAL: int = _env_int("MAX_ORDERS_PER_SIGNAL", 4, min_val=1, max_val=8)

# Front-run offset applied to TP1 (e.g. 0.50 points on XAUUSD: TP1 - 0.50 BUY / TP1 + 0.50 SELL)
TP_FRONT_RUN_OFFSET: float = _env_float("TP_FRONT_RUN_OFFSET", 0.50, min_val=0.0, max_val=50.0)

# Front-run offset applied to pending LIMIT entry layers (e.g. 0.50 points/$0.50 on XAUUSD:
# BUY LIMITs placed +0.50 higher, SELL LIMITs placed -0.50 lower so spread/round-number
# bounces like Msg 13191 dipping to 3710.44 bid above a 3710.00 BUY zone still fill L1)
LIMIT_ENTRY_FRONT_RUN_OFFSET: float = _env_float("LIMIT_ENTRY_FRONT_RUN_OFFSET", 0.50, min_val=0.0, max_val=10.0)

# Deterministic TP1 -> TP5 auto-derivation extension steps (in pips; 100 pips = $10.00 on XAUUSD)
DEFAULT_TP_STEP_PIPS: float = _env_float(
    "DEFAULT_TP_STEP_PIPS", 100.0, min_val=1.0, max_val=2000.0, positive_only=True
)
TP2_STEP_PIPS: float = _env_float(
    "TP2_STEP_PIPS", DEFAULT_TP_STEP_PIPS, min_val=1.0, max_val=2000.0, positive_only=True
)
TP3_STEP_PIPS: float = _env_float(
    "TP3_STEP_PIPS", DEFAULT_TP_STEP_PIPS, min_val=1.0, max_val=2000.0, positive_only=True
)
TP4_STEP_PIPS: float = _env_float(
    "TP4_STEP_PIPS", DEFAULT_TP_STEP_PIPS, min_val=1.0, max_val=2000.0, positive_only=True
)
TP5_STEP_PIPS: float = _env_float(
    "TP5_STEP_PIPS", DEFAULT_TP_STEP_PIPS, min_val=1.0, max_val=2000.0, positive_only=True
)

# Dynamic Multi-Layer Active Runner Scaling & Gold Calibrated Peak-Profit Giveback Guard ("Bank It")
GOLD_REVERSAL_GUARD_ENABLED: bool = _env_bool("GOLD_REVERSAL_GUARD_ENABLED", True)
ARM_THRESHOLD_PCT: float = _env_float("ARM_THRESHOLD_PCT", 0.90, min_val=0.10, max_val=0.99)
GIVEBACK_PCT: float = _env_float("GIVEBACK_PCT", 0.30, min_val=0.05, max_val=0.95)
MIN_GOLD_PULLBACK_DOLLARS: float = _env_float(
    "MIN_GOLD_PULLBACK_DOLLARS", 1.50, min_val=0.10, max_val=50.0, positive_only=True
)
REVERSAL_CONFIRM_TICKS: int = _env_int("REVERSAL_CONFIRM_TICKS", 2, min_val=1, max_val=10)
STAGE0_PROFIT_LOCK_ENABLED: bool = _env_bool("STAGE0_PROFIT_LOCK_ENABLED", True)
STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS: float = _env_float(
    "STAGE0_MIN_PROFIT_TO_LOCK_DOLLARS", 1.20, min_val=0.20, max_val=50.0, positive_only=True
)
CANCEL_PENDING_ON_STAGE0_LOCK_EXIT: bool = _env_bool("CANCEL_PENDING_ON_STAGE0_LOCK_EXIT", True)
ATR_NOISE_FLOOR_MULT: float = _env_float("ATR_NOISE_FLOOR_MULT", 0.35, min_val=0.0, max_val=5.0)
M1_ATR_PERIOD: int = _env_int("M1_ATR_PERIOD", 14, min_val=2, max_val=200)
MAX_GIVEBACK_DOLLARS: float = _env_float("MAX_GIVEBACK_DOLLARS", 4.50, min_val=0.0, max_val=200.0)
MAX_ALLOWED_SPREAD_DOLLARS: float = _env_float(
    "MAX_ALLOWED_SPREAD_DOLLARS", 0.45, min_val=0.05, max_val=20.0, positive_only=True
)
ADAPTIVE_SPREAD_CEILING_DOLLARS: float = _env_float(
    "ADAPTIVE_SPREAD_CEILING_DOLLARS", 0.80, min_val=0.05, max_val=20.0, positive_only=True
)
ADAPTIVE_SPREAD_MULTIPLIER: float = _env_float(
    "ADAPTIVE_SPREAD_MULTIPLIER", 1.50, min_val=1.0, max_val=5.0, positive_only=True
)
RUNNER_EARLY_TP_LIFT_PCT: float = _env_float("RUNNER_EARLY_TP_LIFT_PCT", 0.70, min_val=0.10, max_val=0.99)
RUNNER_CONTINUOUS_TRAIL_ENABLED: bool = _env_bool("RUNNER_CONTINUOUS_TRAIL_ENABLED", False)

RUNNER_DEEPEST_COUNT_FULL: int = _env_int("RUNNER_DEEPEST_COUNT_FULL", 1, min_val=1, max_val=2)
RUNNER_MAX_TP_STAGE: int = _env_int("RUNNER_MAX_TP_STAGE", 5, min_val=1, max_val=5)
RUNNER_PROACTIVE_TP_LIFT: bool = _env_bool("RUNNER_PROACTIVE_TP_LIFT", True)
RUNNER_PROXIMITY_MODE: str = _env_choice("RUNNER_PROXIMITY_MODE", "PIPS", ("PIPS", "PCT", "BOTH"))
RUNNER_PROXIMITY_BUFFER_PIPS: float = _env_float(
    "RUNNER_PROXIMITY_BUFFER_PIPS", 10.0, min_val=0.0, max_val=500.0
)
RUNNER_PROXIMITY_BUFFER_PCT: float = _env_float(
    "RUNNER_PROXIMITY_BUFFER_PCT", 0.05, min_val=0.0, max_val=10.0
)
RUNNER_MAX_MIDRANGE_RATIO: float = _env_float(
    "RUNNER_MAX_MIDRANGE_RATIO", 0.35, min_val=0.05, max_val=0.95
)
RUNNER_REVERSAL_PULLBACK_PIPS: float = _env_float(
    "RUNNER_REVERSAL_PULLBACK_PIPS", 12.0, min_val=0.0, max_val=500.0
)
RUNNER_REVERSAL_PULLBACK_PCT: float = _env_float(
    "RUNNER_REVERSAL_PULLBACK_PCT", 0.02, min_val=0.0, max_val=10.0
)
RUNNER_REVERSAL_CONFIRM_TICKS: int = _env_int(
    "RUNNER_REVERSAL_CONFIRM_TICKS", 2, min_val=1, max_val=10
)
RUNNER_REVERSAL_CANDLE_CONFIRM: bool = _env_bool("RUNNER_REVERSAL_CANDLE_CONFIRM", False)
RUNNER_CLEAN_BREAK_PIPS: float = _env_float(
    "RUNNER_CLEAN_BREAK_PIPS", 5.0, min_val=0.0, max_val=200.0
)

# TP1 Scaling, Breakeven & Pending Order Policy
BANK_ALL_AT_TP1: bool = _env_bool("BANK_ALL_AT_TP1", False)
BANK_SINGLE_LAYER_AT_TP1: bool = _env_bool("BANK_SINGLE_LAYER_AT_TP1", False)
CANCEL_PENDING_ON_TP1: bool = _env_bool("CANCEL_PENDING_ON_TP1", True)
BE_AFTER_TP1: bool = _env_bool("BE_AFTER_TP1", True)
_default_be_layers = ["L1", "L2", "L3", "L4"]
BE_LAYERS: List[str] = [
    str(item).strip().upper()
    for item in _parse_json_list("BE_LAYERS", _default_be_layers)
    if str(item).strip()
] or list(_default_be_layers)
BE_OFFSET_PIPS: float = _env_float("BE_OFFSET_PIPS", 5.0, min_val=0.0, max_val=50.0)
STAGE0_PROFIT_LOCK_OFFSET_PIPS: float = _env_float(
    "STAGE0_PROFIT_LOCK_OFFSET_PIPS", BE_OFFSET_PIPS, min_val=0.0, max_val=100.0
)

# Legacy runner / pre-TP1 shortfall thresholds (calibrated v3.1 anti-choke defaults)
TP1_LOCK_BUFFER: float = _env_float("TP1_LOCK_BUFFER", 0.50, min_val=0.0, max_val=50.0)
TRAIL_DISTANCE_PIPS: float = _env_float("TRAIL_DISTANCE_PIPS", 15.0, min_val=0.0, max_val=500.0)
TRAIL_STEP_PIPS: float = _env_float("TRAIL_STEP_PIPS", 5.0, min_val=0.0, max_val=200.0)
TP2_SHORTFALL_ARM_PIPS: float = _env_float("TP2_SHORTFALL_ARM_PIPS", 20.0, min_val=0.0, max_val=500.0)
TP2_SHORTFALL_PULLBACK_PIPS: float = _env_float("TP2_SHORTFALL_PULLBACK_PIPS", 8.0, min_val=0.0, max_val=200.0)
NO_TP2_SURGE_ARM_PIPS: float = _env_float("NO_TP2_SURGE_ARM_PIPS", 40.0, min_val=0.0, max_val=500.0)
SHORTFALL_ARM_PIPS: float = _env_float("SHORTFALL_ARM_PIPS", 8.0, min_val=0.0, max_val=500.0)
SHORTFALL_PULLBACK_PIPS: float = _env_float("SHORTFALL_PULLBACK_PIPS", 20.0, min_val=0.0, max_val=200.0)
SHORTFALL_CONFIRM_TICKS: int = _env_int("SHORTFALL_CONFIRM_TICKS", 3, min_val=1, max_val=10)
TP1_CLOSE_PROXIMITY_PIPS: int = _env_int("TP1_CLOSE_PROXIMITY_PIPS", 20, min_val=0, max_val=500)

# Stop-Loss, Entry Protection & Risk Caps
ALLOW_BELOW_ZONE_WICK: bool = _env_bool("ALLOW_BELOW_ZONE_WICK", True)
MIN_SL_BUFFER_PIPS: int = _env_int("MIN_SL_BUFFER_PIPS", 20, min_val=0, max_val=500)
FALLBACK_SL_PIPS: float = _env_float("FALLBACK_SL_PIPS", 150.0, min_val=1.0, max_val=2000.0, positive_only=True)
MAX_SL_PIPS: float = _env_float("MAX_SL_PIPS", 350.0, min_val=10.0, max_val=5000.0, positive_only=True)
FALLBACK_TP1_PIPS: float = _env_float("FALLBACK_TP1_PIPS", 50.0, min_val=1.0, max_val=2000.0, positive_only=True)
UNPRICED_TP_GRACE_SECONDS: float = _env_float("UNPRICED_TP_GRACE_SECONDS", 45.0, min_val=0.0, max_val=600.0)
ENFORCE_HIGH_RISK_LOT_CAP: bool = _env_bool("ENFORCE_HIGH_RISK_LOT_CAP", True)
# Automatically adjust layer lots & max risk cap based on live balance following the Risk Guide:
# £50 -> 0.05 max | £100 -> 0.10 max | £200 -> 0.20 max | £500 -> 0.50 max | £1000 -> 1.00 max
AUTO_ADJUST_LOTS_TO_BALANCE: bool = _env_bool("AUTO_ADJUST_LOTS_TO_BALANCE", False)
DAILY_LOSS_PAUSE_PCT: float = _env_float("DAILY_LOSS_PAUSE_PCT", 50.0, min_val=1.0, max_val=100.0, positive_only=True)
EMERGENCY_CUTOFF_LOSS_PCT: float = _env_float(
    "EMERGENCY_CUTOFF_LOSS_PCT", 90.0, min_val=1.0, max_val=100.0, positive_only=True
)
DAILY_LOSS_CLOSE_OPEN_TRADES: bool = _env_bool("DAILY_LOSS_CLOSE_OPEN_TRADES", False)
# Hard floating-loss cap: force-close ALL open positions the moment floating-only loss hits this % of balance.
# This fires BEFORE the 90% emergency cutoff to stop broker margin stop-outs.
# Default 60.0% — change live with /floatcap <pct> in Saved Messages.
FLOAT_LOSS_FORCE_CLOSE_PCT: float = _env_float(
    "FLOAT_LOSS_FORCE_CLOSE_PCT", 60.0, min_val=1.0, max_val=100.0, positive_only=True
)

# Delayed Market Entry, Price Guard & Reconnect Catch-Up
# Staged Market Entry: when True, bare BUY/SELL market orders (no zone) execute ONLY L1 at market,
# then wait for the provider's edit with zone/limits before placing L2..L4.
# Prevents stacking all 4 layers at the same market entry price. Default True.
STAGED_MARKET_ENTRY: bool = _env_bool("STAGED_MARKET_ENTRY", True)
SELL_NOW_DELAY_SECONDS: float = _env_float("SELL_NOW_DELAY_SECONDS", 0.0, min_val=0.0, max_val=300.0)
DELAYED_POLL_INTERVAL_SECONDS: float = _env_float(
    "DELAYED_POLL_INTERVAL_SECONDS", 0.25, min_val=0.05, max_val=1.0
)
NOW_PRICE_GUARD_ABORT_ON_MISMATCH: bool = _env_bool("NOW_PRICE_GUARD_ABORT_ON_MISMATCH", True)
NOW_ENTRY_MAX_WAIT_SECONDS: float = _env_float("NOW_ENTRY_MAX_WAIT_SECONDS", 180.0, min_val=0.0, max_val=3600.0)
MAX_NEW_EDIT_AGE_SECONDS: int = _env_int("MAX_NEW_EDIT_AGE_SECONDS", 3600, min_val=0, max_val=86400)
MAX_EDIT_EVENT_FRESHNESS_SECONDS: int = _env_int("MAX_EDIT_EVENT_FRESHNESS_SECONDS", 180, min_val=5, max_val=3600)
DEDUPLICATION_WINDOW_SECONDS: int = _env_int("DEDUPLICATION_WINDOW_SECONDS", 5, min_val=0, max_val=300)

CATCHUP_WINDOW_SECONDS: int = _env_int("CATCHUP_WINDOW_SECONDS", 1800, min_val=0, max_val=86400)
CATCHUP_MAX_MESSAGES: int = _env_int("CATCHUP_MAX_MESSAGES", 25, min_val=0, max_val=500)
CATCHUP_MARKET_MAX_AGE_SECONDS: int = _env_int("CATCHUP_MARKET_MAX_AGE_SECONDS", 60, min_val=0, max_val=3600)
CATCHUP_MARKET_MAX_SLIPPAGE_PIPS: float = _env_float(
    "CATCHUP_MARKET_MAX_SLIPPAGE_PIPS", 15.0, min_val=0.0, max_val=500.0
)
# Auto Market Entry Slippage: when a signal says "SELL 3728" or "BUY 3728" without "LIMIT",
# if live price is within this many pips of the quoted entry, activate at market immediately.
AUTO_MARKET_SLIPPAGE_PIPS: float = _env_float(
    "AUTO_MARKET_SLIPPAGE_PIPS", 15.0, min_val=0.0, max_val=100.0
)

# Deleted-Signal Trade Protection (0ms push + 1.5s fast sweep auto-closes positions & cancels pending orders ASAP)
CLOSE_ON_SIGNAL_DELETED: bool = _env_bool("CLOSE_ON_SIGNAL_DELETED", True)
DELETED_SIGNAL_POLL_INTERVAL_SECONDS: float = _env_float(
    "DELETED_SIGNAL_POLL_INTERVAL_SECONDS", 1.5, min_val=0.0, max_val=3600.0
)

# Realized Daily PnL Cache TTL (explicitly invalidated on every close)
DAILY_PNL_CACHE_TTL_SECONDS: float = _env_float(
    "DAILY_PNL_CACHE_TTL_SECONDS", 5.0, min_val=0.0, max_val=300.0
)


# ===========================================================================
# SYMBOL, TELEGRAM & RUNTIME CONTROLS
# ===========================================================================

# Unique magic number tagging bot trades
MAGIC_NUMBER: int = _env_int("MAGIC_NUMBER", 777001, min_val=1)

# Default symbol used for all signals (PU Prime exact Gold symbol: XAUUSD.s)
DEFAULT_SYMBOL: str = (os.getenv("DEFAULT_SYMBOL") or "XAUUSD.s").strip() or "XAUUSD.s"

# Legacy single-price lot setting (retained for backward compatibility)
SINGLE_PRICE_LOT: float = _env_float("SINGLE_PRICE_LOT", 0.01, min_val=0.01, max_val=100.0, positive_only=True)

# Seconds between live checks of mt5.terminal_info().trade_allowed
CHECK_ALGO_TRADING_INTERVAL: int = _env_int("CHECK_ALGO_TRADING_INTERVAL", 30, min_val=1, max_val=3600)

# Ignored symbols: rejects any message explicitly naming a non-Gold pair
_default_ignored = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "US30", "NAS100", "USTEC", "BTCUSD"]
IGNORED_SYMBOLS: List[str] = [
    str(sym).strip() for sym in _parse_json_list("IGNORED_SYMBOLS", _default_ignored) if str(sym).strip()
]

# Telegram API credentials (from https://my.telegram.org)
TELEGRAM_API_ID: int = _env_int("TELEGRAM_API_ID", 12345678, min_val=1)
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH") or "your_telegram_api_hash"
TELEGRAM_SESSION_NAME: str = os.getenv("TELEGRAM_SESSION_NAME") or "copier_session"

# Forwarded messages policy: if False, Gate 1 discards all forwarded messages
ALLOW_FORWARDS: bool = _env_bool("ALLOW_FORWARDS", False)

# Target Telegram channel/group IDs to monitor
_raw_channels = os.getenv("CHANNEL_IDS", "[-1001234567890]")
try:
    if _raw_channels.strip().startswith("["):
        CHANNEL_IDS = [
            int(ch) if str(ch).lstrip("-").isdigit() else str(ch)
            for ch in json.loads(_raw_channels)
        ]
    else:
        CHANNEL_IDS = [
            int(ch.strip()) if ch.strip().lstrip("-").isdigit() else ch.strip()
            for ch in _raw_channels.split(",")
            if ch.strip()
        ]
except Exception:
    CHANNEL_IDS = [-1001234567890]

# Saved Messages ("me") telegram notifications & Dry-run mode
NOTIFY_SAVED_MESSAGES: bool = _env_bool("NOTIFY_SAVED_MESSAGES", True)
DRY_RUN: bool = _env_bool("DRY_RUN", False)

# Runtime Uptime, Power & UI Impact Controls
PREVENT_SYSTEM_SLEEP: bool = _env_bool("PREVENT_SYSTEM_SLEEP", True)
IDLE_MT5_POLL_INTERVAL_SECONDS: float = _env_float(
    "IDLE_MT5_POLL_INTERVAL_SECONDS", 5.0, min_val=1.0, max_val=60.0
)
TELEGRAM_KEEPALIVE_SECONDS: float = _env_float(
    "TELEGRAM_KEEPALIVE_SECONDS", 20.0, min_val=5.0, max_val=300.0
)
CONSOLE_TITLE_UPDATES: bool = _env_bool("CONSOLE_TITLE_UPDATES", True)

# MT5 Optional Direct Login Settings
_raw_mt5_login = (os.getenv("MT5_LOGIN") or "").strip()
try:
    MT5_LOGIN: Optional[int] = int(_raw_mt5_login) if _raw_mt5_login else None
except ValueError:
    _cfg_logger.warning(f"Invalid MT5_LOGIN={_raw_mt5_login!r}; ignoring.")
    MT5_LOGIN = None
MT5_PASSWORD: Optional[str] = os.getenv("MT5_PASSWORD") or None
MT5_SERVER: Optional[str] = os.getenv("MT5_SERVER") or None
MT5_PATH: Optional[str] = os.getenv("MT5_PATH") or None

# ===========================================================================
# FILE PATHS
# ===========================================================================

STATE_FILE_PATH: Path = Path(os.getenv("STATE_FILE_PATH") or str(BASE_DIR / "signals_state.json"))
LOG_FILE_PATH: Path = Path(os.getenv("LOG_FILE_PATH") or str(BASE_DIR / "copier.log"))
TRADES_LEDGER_PATH: Path = Path(os.getenv("TRADES_LEDGER_PATH") or str(BASE_DIR / "trades_ledger.jsonl"))
LOCK_FILE_PATH: Path = Path(os.getenv("LOCK_FILE_PATH") or str(BASE_DIR / "copier.lock"))


# ===========================================================================
# v3.4.1 TP1-TOUCH BANK SYSTEM & HARD GIVEBACK CEILING
# ===========================================================================
TP1_TOUCH_BANK_ENABLED: bool = _env_bool("TP1_TOUCH_BANK_ENABLED", True)
TP1_TOUCH_BANK_BUFFER_PIPS: float = _env_float(
    "TP1_TOUCH_BANK_BUFFER_PIPS", 12.0, min_val=1.0, max_val=100.0
)
HARD_GIVEBACK_CEILING_DOLLARS: float = _env_float(
    "HARD_GIVEBACK_CEILING_DOLLARS", 3.50, min_val=0.50, max_val=50.0
)


# ===========================================================================
# v3.5.0 STAGED GOLD RUNNER EXIT ARCHITECTURE (reuses v3.4.2 Stage 1 keys)
# ===========================================================================
STAGED_EXIT_ENABLED: bool = _env_bool("STAGED_EXIT_ENABLED", True)

# Stage 1 (TP1 cleared) — reuses v3.4.2 keys for .env compatibility
TP1_TRAIL_ENABLED: bool = _env_bool("TP1_TRAIL_ENABLED", True)
TP1_TRAIL_ATR_MULT: float = _env_float("TP1_TRAIL_ATR_MULT", 1.55, min_val=0.1, max_val=20.0)
TP1_TRAIL_MIN_DOLLARS: float = _env_float("TP1_TRAIL_MIN_DOLLARS", 2.50, min_val=0.10, max_val=100.0)
TP1_TRAIL_MAX_DOLLARS: float = _env_float("TP1_TRAIL_MAX_DOLLARS", 3.50, min_val=0.10, max_val=100.0)
TP1_TRAIL_STEP_DOLLARS: float = _env_float("TP1_TRAIL_STEP_DOLLARS", 0.25, min_val=0.01, max_val=20.0)
STAGE1_ATR_ACTIVATION_PCT: float = _env_float(
    "STAGE1_ATR_ACTIVATION_PCT", 0.50, min_val=0.0, max_val=1.0
)

# Stage 2 (TP2 cleared)
TRAIL_TP2_ATR_MULT: float = _env_float("TRAIL_TP2_ATR_MULT", 2.0, min_val=0.1, max_val=20.0)
TRAIL_TP2_MIN: float = _env_float("TRAIL_TP2_MIN", 3.00, min_val=0.10, max_val=100.0)
TRAIL_TP2_MAX: float = _env_float("TRAIL_TP2_MAX", 4.50, min_val=0.10, max_val=100.0)

# Stage 3 (TP3 cleared)
TRAIL_TP3_ATR_MULT: float = _env_float("TRAIL_TP3_ATR_MULT", 2.5, min_val=0.1, max_val=20.0)
TRAIL_TP3_MIN: float = _env_float("TRAIL_TP3_MIN", 4.00, min_val=0.10, max_val=100.0)
TRAIL_TP3_MAX: float = _env_float("TRAIL_TP3_MAX", 6.00, min_val=0.10, max_val=100.0)

# Stage 4 (TP4 cleared — active between TP4 and TP5; RUNNER_MAX_TP_STAGE defaults to 5)
TRAIL_TP4_ATR_MULT: float = _env_float("TRAIL_TP4_ATR_MULT", 3.0, min_val=0.1, max_val=20.0)
TRAIL_TP4_MIN: float = _env_float("TRAIL_TP4_MIN", 5.00, min_val=0.10, max_val=100.0)
TRAIL_TP4_MAX: float = _env_float("TRAIL_TP4_MAX", 8.00, min_val=0.10, max_val=100.0)

_STAGED_TRAIL_TIER_KEYS: Dict[int, Tuple[str, str, str]] = {
    1: ("TP1_TRAIL_ATR_MULT", "TP1_TRAIL_MIN_DOLLARS", "TP1_TRAIL_MAX_DOLLARS"),
    2: ("TRAIL_TP2_ATR_MULT", "TRAIL_TP2_MIN", "TRAIL_TP2_MAX"),
    3: ("TRAIL_TP3_ATR_MULT", "TRAIL_TP3_MIN", "TRAIL_TP3_MAX"),
    4: ("TRAIL_TP4_ATR_MULT", "TRAIL_TP4_MIN", "TRAIL_TP4_MAX"),
}


def validate_staged_trail_tiers(
    tiers: Optional[Dict[int, Tuple[float, float, float]]] = None,
    *,
    update_globals: Optional[bool] = None,
    emit_warnings: bool = True,
) -> Dict[int, Tuple[float, float, float]]:
    """
    Validate and normalize the 4 staged ATR trail tiers (Stage 1 -> 2 -> 3 -> 4).

    - When `tiers` is None, reads current module globals dynamically (respecting runtime
      `monkeypatch.setattr(config, ...)`). If any tier has `min > max`, swaps them,
      emits `_cfg_logger.warning(...)`, and updates the module globals (when
      `update_globals` is True; defaults to True when `tiers is None`, False otherwise).
    - When `tiers` is passed explicitly, validates/normalizes the passed dict without
      mutating module globals (unless `update_globals=True` is explicitly passed).
    - If tiers do not widen monotonically across consecutive stages (`atr_mult`, `min`,
      `max` non-decreasing from Stage 1 -> 2 -> 3 -> 4), emits `_cfg_logger.warning(...)`
      without crashing or altering the values.
    - Returns normalized dict `{1: (mult, min_d, max_d), 2: ..., 3: ..., 4: ...}`.
    """
    g = globals()
    if tiers is None:
        raw_tiers: Dict[int, Tuple[float, float, float]] = {
            stage: (float(g[mult_k]), float(g[min_k]), float(g[max_k]))
            for stage, (mult_k, min_k, max_k) in _STAGED_TRAIL_TIER_KEYS.items()
        }
    else:
        raw_tiers = {
            int(stage): (float(vals[0]), float(vals[1]), float(vals[2]))
            for stage, vals in tiers.items()
        }

    do_update = (tiers is None) if update_globals is None else bool(update_globals)
    normalized: Dict[int, Tuple[float, float, float]] = {}

    for stage in sorted(raw_tiers.keys()):
        mult_f, min_f, max_f = raw_tiers[stage]
        if min_f > max_f:
            orig_min, orig_max = min_f, max_f
            min_f, max_f = max_f, min_f
            if emit_warnings:
                _cfg_logger.warning(
                    f"Staged trail Stage {stage} has min ({orig_min:.2f}) > max ({orig_max:.2f}); "
                    f"swapped to min={min_f:.2f}, max={max_f:.2f}."
                )
            if do_update and stage in _STAGED_TRAIL_TIER_KEYS:
                _, min_k, max_k = _STAGED_TRAIL_TIER_KEYS[stage]
                g[min_k] = min_f
                g[max_k] = max_f
        normalized[stage] = (mult_f, min_f, max_f)

    ordered_stages = sorted(normalized.keys())
    for prev_s, cur_s in zip(ordered_stages, ordered_stages[1:]):
        prev_mult, prev_min, prev_max = normalized[prev_s]
        cur_mult, cur_min, cur_max = normalized[cur_s]
        narrowed: List[str] = []
        if cur_mult < prev_mult - 1e-9:
            narrowed.append(f"atr_mult ({prev_mult:.2f} -> {cur_mult:.2f})")
        if cur_min < prev_min - 1e-9:
            narrowed.append(f"min (${prev_min:.2f} -> ${cur_min:.2f})")
        if cur_max < prev_max - 1e-9:
            narrowed.append(f"max (${prev_max:.2f} -> ${cur_max:.2f})")
        if narrowed and emit_warnings:
            _cfg_logger.warning(
                f"Staged trail tiers do not widen from Stage {prev_s} to Stage {cur_s}: "
                f"{', '.join(narrowed)} (keeping configured values)."
            )

    if do_update:
        g["STAGED_TRAIL_TIERS"] = dict(normalized)
    return normalized


STAGED_TRAIL_TIERS: Dict[int, Tuple[float, float, float]] = validate_staged_trail_tiers()


# ===========================================================================
# v3.6.0 PROGRESSIVE PROFIT RATCHET + HIGHER-TP STAGED BANKING
# ===========================================================================
PROFIT_RATCHET_ENABLED: bool = _env_bool("PROFIT_RATCHET_ENABLED", True)
RATCHET_BROKER_STEP_DOLLARS: float = _env_float(
    "RATCHET_BROKER_STEP_DOLLARS", 0.25, min_val=0.05, max_val=10.0, positive_only=True
)
STOP_LEVEL_SAFETY_BUFFER_DOLLARS: float = _env_float(
    "STOP_LEVEL_SAFETY_BUFFER_DOLLARS", 0.05, min_val=0.0, max_val=5.0
)
HARD_MAX_SPREAD_DOLLARS: float = _env_float(
    "HARD_MAX_SPREAD_DOLLARS", 1.25, min_val=0.20, max_val=20.0, positive_only=True
)
MIN_VALID_SPREAD_DOLLARS: float = _env_float(
    "MIN_VALID_SPREAD_DOLLARS", 0.05, min_val=0.0, max_val=5.0
)

# Ratchet Tiers (Stage 0 -> Stage 5 + Near-Target Squeeze)
RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS: float = _env_float(
    "RATCHET_TIGHT_TP1_MAX_LEG_DOLLARS", 3.50, min_val=0.50, max_val=20.0, positive_only=True
)
RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIGHT_TP1_MIN_MFE_DOLLARS", 2.00, min_val=0.50, max_val=20.0, positive_only=True
)
RATCHET_TIGHT_TP1_LOCK_PCT: float = _env_float(
    "RATCHET_TIGHT_TP1_LOCK_PCT", 0.40, min_val=0.10, max_val=0.90
)
RATCHET_LIFT_MIN_MFE_DOLLARS: float = _env_float(
    "RATCHET_LIFT_MIN_MFE_DOLLARS", 1.50, min_val=0.20, max_val=20.0, positive_only=True
)
RATCHET_TIER1A_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIER1A_MFE_DOLLARS", 3.50, min_val=0.50, max_val=20.0, positive_only=True
)
RATCHET_TIER1A_LOCK_DOLLARS: float = _env_float(
    "RATCHET_TIER1A_LOCK_DOLLARS", 0.50, min_val=0.10, max_val=10.0, positive_only=True
)
RATCHET_TIER1B_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIER1B_MFE_DOLLARS", 4.25, min_val=0.50, max_val=20.0, positive_only=True
)
RATCHET_TIER1B_LOCK_DOLLARS: float = _env_float(
    "RATCHET_TIER1B_LOCK_DOLLARS", 0.75, min_val=0.10, max_val=10.0, positive_only=True
)
RATCHET_TIER2_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIER2_MFE_DOLLARS", 5.00, min_val=1.00, max_val=30.0, positive_only=True
)
RATCHET_TIER2_RETENTION_PCT: float = _env_float(
    "RATCHET_TIER2_RETENTION_PCT", 0.30, min_val=0.10, max_val=0.90
)
RATCHET_TIER2_MIN_LOCK_DOLLARS: float = _env_float(
    "RATCHET_TIER2_MIN_LOCK_DOLLARS", 1.50, min_val=0.20, max_val=20.0, positive_only=True
)
RATCHET_TIER2_MIN_CUSHION: float = _env_float(
    "RATCHET_TIER2_MIN_CUSHION", 3.50, min_val=0.50, max_val=20.0, positive_only=True
)
RATCHET_TIER2_MAX_CUSHION: float = _env_float(
    "RATCHET_TIER2_MAX_CUSHION", 4.00, min_val=0.50, max_val=25.0, positive_only=True
)
RATCHET_TIER3_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIER3_MFE_DOLLARS", 8.00, min_val=1.00, max_val=50.0, positive_only=True
)
RATCHET_TIER3_RETENTION_PCT: float = _env_float(
    "RATCHET_TIER3_RETENTION_PCT", 0.55, min_val=0.10, max_val=0.95
)
RATCHET_TIER3_MIN_CUSHION: float = _env_float(
    "RATCHET_TIER3_MIN_CUSHION", 3.50, min_val=0.50, max_val=25.0, positive_only=True
)
RATCHET_TIER3_MAX_CUSHION: float = _env_float(
    "RATCHET_TIER3_MAX_CUSHION", 4.25, min_val=0.50, max_val=30.0, positive_only=True
)
RATCHET_TIER4_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIER4_MFE_DOLLARS", 15.00, min_val=2.00, max_val=100.0, positive_only=True
)
RATCHET_TIER4_RETENTION_PCT: float = _env_float(
    "RATCHET_TIER4_RETENTION_PCT", 0.68, min_val=0.10, max_val=0.95
)
RATCHET_TIER4_MIN_CUSHION: float = _env_float(
    "RATCHET_TIER4_MIN_CUSHION", 3.75, min_val=0.50, max_val=30.0, positive_only=True
)
RATCHET_TIER4_MAX_CUSHION: float = _env_float(
    "RATCHET_TIER4_MAX_CUSHION", 5.00, min_val=0.50, max_val=40.0, positive_only=True
)
RATCHET_TIER5_MFE_DOLLARS: float = _env_float(
    "RATCHET_TIER5_MFE_DOLLARS", 25.00, min_val=5.00, max_val=200.0, positive_only=True
)
RATCHET_TIER5_RETENTION_PCT: float = _env_float(
    "RATCHET_TIER5_RETENTION_PCT", 0.78, min_val=0.10, max_val=0.98
)
RATCHET_TIER5_MIN_CUSHION: float = _env_float(
    "RATCHET_TIER5_MIN_CUSHION", 4.50, min_val=0.50, max_val=40.0, positive_only=True
)
RATCHET_TIER5_MAX_CUSHION: float = _env_float(
    "RATCHET_TIER5_MAX_CUSHION", 6.00, min_val=0.50, max_val=50.0, positive_only=True
)
RATCHET_STAGE4_RETENTION_PCT: float = _env_float(
    "RATCHET_STAGE4_RETENTION_PCT", 0.85, min_val=0.10, max_val=0.98
)
RATCHET_STAGE4_MIN_CUSHION: float = _env_float(
    "RATCHET_STAGE4_MIN_CUSHION", 3.00, min_val=0.50, max_val=30.0, positive_only=True
)
RATCHET_STAGE4_MAX_CUSHION: float = _env_float(
    "RATCHET_STAGE4_MAX_CUSHION", 4.50, min_val=0.50, max_val=40.0, positive_only=True
)
PEAK_HARVEST_ARM_PCT: float = _env_float(
    "PEAK_HARVEST_ARM_PCT", 0.85, min_val=0.50, max_val=0.99
)
PEAK_HARVEST_CUSHION_MULT: float = _env_float(
    "PEAK_HARVEST_CUSHION_MULT", 0.65, min_val=0.20, max_val=1.00
)
PEAK_HARVEST_MIN_BROKER_CUSHION: float = _env_float(
    "PEAK_HARVEST_MIN_BROKER_CUSHION", 2.75, min_val=0.50, max_val=20.0, positive_only=True
)
PEAK_HARVEST_MIN_VIRT_CUSHION: float = _env_float(
    "PEAK_HARVEST_MIN_VIRT_CUSHION", 2.25, min_val=0.50, max_val=20.0, positive_only=True
)
PEAK_HARVEST_MAX_CUSHION: float = _env_float(
    "PEAK_HARVEST_MAX_CUSHION", 3.50, min_val=0.50, max_val=25.0, positive_only=True
)

# Pre-TP1 BANK_HARVEST, Tiered Waterfall & Pending Order Hygiene
BANK_HARVEST_ENABLED: bool = _env_bool("BANK_HARVEST_ENABLED", True)
BANK_HARVEST_ARM_MFE_DOLLARS: float = _env_float(
    "BANK_HARVEST_ARM_MFE_DOLLARS", 5.00, min_val=1.00, max_val=50.0, positive_only=True
)
BANK_HARVEST_ALT_ARM_PCT: float = _env_float(
    "BANK_HARVEST_ALT_ARM_PCT", 0.80, min_val=0.30, max_val=0.99
)
BANK_HARVEST_ALT_MIN_MFE_DOLLARS: float = _env_float(
    "BANK_HARVEST_ALT_MIN_MFE_DOLLARS", 2.50, min_val=0.50, max_val=20.0, positive_only=True
)
BANK_HARVEST_PULLBACK_ATR_MULT: float = _env_float(
    "BANK_HARVEST_PULLBACK_ATR_MULT", 1.15, min_val=0.20, max_val=5.0, positive_only=True
)
BANK_HARVEST_PULLBACK_MIN: float = _env_float(
    "BANK_HARVEST_PULLBACK_MIN", 1.75, min_val=0.50, max_val=20.0, positive_only=True
)
BANK_HARVEST_PULLBACK_MAX: float = _env_float(
    "BANK_HARVEST_PULLBACK_MAX", 2.50, min_val=0.50, max_val=25.0, positive_only=True
)
BANK_HARVEST_BROKER_CUSHION_DOLLARS: float = _env_float(
    "BANK_HARVEST_BROKER_CUSHION_DOLLARS", 2.50, min_val=0.50, max_val=25.0, positive_only=True
)
TIERED_STAGE_BANKING_ENABLED: bool = _env_bool("TIERED_STAGE_BANKING_ENABLED", True)
CANCEL_PENDING_ON_BE: bool = _env_bool("CANCEL_PENDING_ON_BE", True)
CANCEL_PENDING_ON_PROFIT_LOCK: bool = _env_bool("CANCEL_PENDING_ON_PROFIT_LOCK", True)
PENDING_PNL_BACKFILL_MAX_SECONDS: float = _env_float(
    "PENDING_PNL_BACKFILL_MAX_SECONDS", 60.0, min_val=5.0, max_val=600.0, positive_only=True
)

# v3.6.0+ Pending Order Consolidation, Duplicate Guard & TP Sanity (Fix B)
SINGLE_PRICE_CONSOLIDATE_ORDERS: bool = _env_bool("SINGLE_PRICE_CONSOLIDATE_ORDERS", True)
PENDING_DUPLICATE_PRICE_TOLERANCE: float = _env_float(
    "PENDING_DUPLICATE_PRICE_TOLERANCE", 0.50, min_val=0.0, max_val=20.0
)
PENDING_DUPLICATE_ORDER_TOLERANCE: float = _env_float(
    "PENDING_DUPLICATE_ORDER_TOLERANCE", 0.25, min_val=0.0, max_val=20.0
)
MAX_TP_DISTANCE_DOLLARS: float = _env_float(
    "MAX_TP_DISTANCE_DOLLARS", 250.0, min_val=10.0, max_val=2000.0, positive_only=True
)


def validate_ratchet_cushion_pairs() -> None:
    """Ensure all v3.6 min/max cushion and pullback pairs satisfy min <= max."""
    g = globals()
    pairs = (
        ("RATCHET_TIER2_MIN_CUSHION", "RATCHET_TIER2_MAX_CUSHION"),
        ("RATCHET_TIER3_MIN_CUSHION", "RATCHET_TIER3_MAX_CUSHION"),
        ("RATCHET_TIER4_MIN_CUSHION", "RATCHET_TIER4_MAX_CUSHION"),
        ("RATCHET_TIER5_MIN_CUSHION", "RATCHET_TIER5_MAX_CUSHION"),
        ("RATCHET_STAGE4_MIN_CUSHION", "RATCHET_STAGE4_MAX_CUSHION"),
        ("BANK_HARVEST_PULLBACK_MIN", "BANK_HARVEST_PULLBACK_MAX"),
        ("PEAK_HARVEST_MIN_BROKER_CUSHION", "PEAK_HARVEST_MAX_CUSHION"),
        ("PEAK_HARVEST_MIN_VIRT_CUSHION", "PEAK_HARVEST_MAX_CUSHION"),
    )
    for min_k, max_k in pairs:
        mn, mx = float(g[min_k]), float(g[max_k])
        if mn > mx:
            _cfg_logger.warning(f"{min_k} ({mn:.2f}) > {max_k} ({mx:.2f}); swapping to keep min <= max.")
            g[min_k], g[max_k] = mx, mn


validate_ratchet_cushion_pairs()
