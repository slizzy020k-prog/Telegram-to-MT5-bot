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

# Active Runner Defaults, Spread Guards & Breakeven Policy (Pure Dynamic ATR Engine)
M1_ATR_PERIOD: int = _env_int("M1_ATR_PERIOD", 14, min_val=2, max_val=200)
MAX_ALLOWED_SPREAD_DOLLARS: float = _env_float(
    "MAX_ALLOWED_SPREAD_DOLLARS", 0.45, min_val=0.05, max_val=20.0, positive_only=True
)
ADAPTIVE_SPREAD_CEILING_DOLLARS: float = _env_float(
    "ADAPTIVE_SPREAD_CEILING_DOLLARS", 0.80, min_val=0.05, max_val=20.0, positive_only=True
)
ADAPTIVE_SPREAD_MULTIPLIER: float = _env_float(
    "ADAPTIVE_SPREAD_MULTIPLIER", 1.50, min_val=1.0, max_val=5.0, positive_only=True
)

RUNNER_DEEPEST_COUNT_FULL: int = _env_int("RUNNER_DEEPEST_COUNT_FULL", 1, min_val=1, max_val=2)
RUNNER_MAX_TP_STAGE: int = _env_int("RUNNER_MAX_TP_STAGE", 5, min_val=1, max_val=5)

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

# Manual /trail, /be, entry proximity & deprecated shortfall compatibility constants
TP1_LOCK_BUFFER: float = _env_float("TP1_LOCK_BUFFER", 0.50, min_val=0.0, max_val=50.0)
TRAIL_DISTANCE_PIPS: float = _env_float("TRAIL_DISTANCE_PIPS", 15.0, min_val=0.0, max_val=500.0)
TRAIL_STEP_PIPS: float = _env_float("TRAIL_STEP_PIPS", 5.0, min_val=0.0, max_val=200.0)
TP2_SHORTFALL_ARM_PIPS: float = _env_float("TP2_SHORTFALL_ARM_PIPS", 20.0, min_val=0.0, max_val=500.0)
TP2_SHORTFALL_PULLBACK_PIPS: float = _env_float("TP2_SHORTFALL_PULLBACK_PIPS", 8.0, min_val=0.0, max_val=200.0)
NO_TP2_SURGE_ARM_PIPS: float = _env_float("NO_TP2_SURGE_ARM_PIPS", 40.0, min_val=0.0, max_val=500.0)
TP1_CLOSE_PROXIMITY_PIPS: int = _env_int("TP1_CLOSE_PROXIMITY_PIPS", 20, min_val=0, max_val=500)

# Stop-Loss, Entry Protection & Risk Caps
ALLOW_BELOW_ZONE_WICK: bool = _env_bool("ALLOW_BELOW_ZONE_WICK", True)
MIN_SL_BUFFER_PIPS: int = _env_int("MIN_SL_BUFFER_PIPS", 20, min_val=0, max_val=500)
FALLBACK_SL_PIPS: float = _env_float("FALLBACK_SL_PIPS", 150.0, min_val=1.0, max_val=2000.0, positive_only=True)
MAX_SL_PIPS: float = _env_float("MAX_SL_PIPS", 350.0, min_val=10.0, max_val=5000.0, positive_only=True)
FALLBACK_TP1_PIPS: float = _env_float("FALLBACK_TP1_PIPS", 50.0, min_val=1.0, max_val=2000.0, positive_only=True)
UNPRICED_TP_GRACE_SECONDS: float = _env_float("UNPRICED_TP_GRACE_SECONDS", 45.0, min_val=0.0, max_val=600.0)
# Risk & Loss Caps — disabled (unlimited) by default; toggle live via Saved Messages
# (/lotcap, /losslimit, /floatcap, dailyloss).
ENFORCE_HIGH_RISK_LOT_CAP: bool = _env_bool("ENFORCE_HIGH_RISK_LOT_CAP", False)
# Automatically adjust layer lots & max risk cap based on live balance following the Risk Guide:
# £50 -> 0.05 max | £100 -> 0.10 max | £200 -> 0.20 max | £500 -> 0.50 max | £1000 -> 1.00 max
AUTO_ADJUST_LOTS_TO_BALANCE: bool = _env_bool("AUTO_ADJUST_LOTS_TO_BALANCE", False)
DAILY_LOSS_PAUSE_PCT: float = _env_float(
    "DAILY_LOSS_PAUSE_PCT", 0.0, min_val=0.0, max_val=100.0, positive_only=False
)
EMERGENCY_CUTOFF_LOSS_PCT: float = _env_float(
    "EMERGENCY_CUTOFF_LOSS_PCT", 0.0, min_val=0.0, max_val=100.0, positive_only=False
)
DAILY_LOSS_CLOSE_OPEN_TRADES: bool = _env_bool("DAILY_LOSS_CLOSE_OPEN_TRADES", False)
# Optional floating-loss hard cap: 0.0 = disabled (unlimited). Enable live with /floatcap <1-100>.
FLOAT_LOSS_FORCE_CLOSE_PCT: float = _env_float(
    "FLOAT_LOSS_FORCE_CLOSE_PCT", 0.0, min_val=0.0, max_val=100.0, positive_only=False
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
# BROKER COMPLIANCE, SPREAD GUARDS & ACCOUNTING HYGIENE
# ===========================================================================
STOP_LEVEL_SAFETY_BUFFER_DOLLARS: float = _env_float(
    "STOP_LEVEL_SAFETY_BUFFER_DOLLARS", 0.05, min_val=0.0, max_val=5.0
)
HARD_MAX_SPREAD_DOLLARS: float = _env_float(
    "HARD_MAX_SPREAD_DOLLARS", 1.25, min_val=0.20, max_val=20.0, positive_only=True
)
MIN_VALID_SPREAD_DOLLARS: float = _env_float(
    "MIN_VALID_SPREAD_DOLLARS", 0.05, min_val=0.0, max_val=5.0
)
TIERED_STAGE_BANKING_ENABLED: bool = _env_bool("TIERED_STAGE_BANKING_ENABLED", True)
CANCEL_PENDING_ON_BE: bool = _env_bool("CANCEL_PENDING_ON_BE", True)
CANCEL_PENDING_ON_PROFIT_LOCK: bool = _env_bool("CANCEL_PENDING_ON_PROFIT_LOCK", True)
PENDING_PNL_BACKFILL_MAX_SECONDS: float = _env_float(
    "PENDING_PNL_BACKFILL_MAX_SECONDS", 60.0, min_val=5.0, max_val=600.0, positive_only=True
)
STAGE1_ATR_ACTIVATION_PCT: float = _env_float(
    "STAGE1_ATR_ACTIVATION_PCT", 0.50, min_val=0.0, max_val=1.0
)

# Pending Order Consolidation, Duplicate Guard & TP Sanity
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

# ===========================================================================
# PURE DYNAMIC ATR ENGINE (ATR-SCALED SL/TP, BE LOCK & TRAILING SL/TP)
# ===========================================================================
DYNAMIC_ATR_SLTP_ENABLED: bool = _env_bool("DYNAMIC_ATR_SLTP_ENABLED", True)
DYNAMIC_SLTP_MODE: str = _env_choice(
    "DYNAMIC_SLTP_MODE", "ADAPTIVE", ("ADAPTIVE", "FALLBACK_ONLY", "PURE_ATR")
)
DYNAMIC_ATR_TIMEFRAME: str = _env_choice(
    "DYNAMIC_ATR_TIMEFRAME", "M5", ("M1", "M5", "M15", "H1")
)
DYNAMIC_ATR_PERIOD: int = _env_int("DYNAMIC_ATR_PERIOD", 14, min_val=2, max_val=200)
DYNAMIC_ATR_BASELINE_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_BASELINE_DOLLARS", 4.00, min_val=0.10, max_val=200.0, positive_only=True
)
DYNAMIC_SL_ATR_MULT: float = _env_float(
    "DYNAMIC_SL_ATR_MULT", 2.50, min_val=0.20, max_val=20.0, positive_only=True
)
DYNAMIC_TP_ATR_MULT: float = _env_float(
    "DYNAMIC_TP_ATR_MULT", 1.50, min_val=0.20, max_val=20.0, positive_only=True
)
DYNAMIC_TP_STEP_ATR_MULT: float = _env_float(
    "DYNAMIC_TP_STEP_ATR_MULT", 1.50, min_val=0.20, max_val=20.0, positive_only=True
)
DYNAMIC_SL_MIN_DOLLARS: float = _env_float(
    "DYNAMIC_SL_MIN_DOLLARS", 4.50, min_val=0.20, max_val=200.0, positive_only=True
)
DYNAMIC_SL_MAX_DOLLARS: float = _env_float(
    "DYNAMIC_SL_MAX_DOLLARS", 35.00, min_val=0.50, max_val=500.0, positive_only=True
)
DYNAMIC_TP_MIN_DOLLARS: float = _env_float(
    "DYNAMIC_TP_MIN_DOLLARS", 2.50, min_val=0.20, max_val=200.0, positive_only=True
)
DYNAMIC_TP_MAX_DOLLARS: float = _env_float(
    "DYNAMIC_TP_MAX_DOLLARS", 50.00, min_val=0.50, max_val=500.0, positive_only=True
)
DYNAMIC_SLTP_SCALE_MIN: float = _env_float(
    "DYNAMIC_SLTP_SCALE_MIN", 0.75, min_val=0.20, max_val=2.0, positive_only=True
)
DYNAMIC_SLTP_SCALE_MAX: float = _env_float(
    "DYNAMIC_SLTP_SCALE_MAX", 1.75, min_val=0.50, max_val=5.0, positive_only=True
)

# Primary Dynamic ATR Trailing Stop, BE Lock & Dynamic Trailing TP
DYNAMIC_ATR_TRAIL_ENABLED: bool = _env_bool("DYNAMIC_ATR_TRAIL_ENABLED", True)
DYNAMIC_ATR_TRAIL_TIMEFRAME: str = _env_choice(
    "DYNAMIC_ATR_TRAIL_TIMEFRAME", "M5", ("M1", "M5", "M15", "H1")
)
DYNAMIC_ATR_TRAIL_MULT: float = _env_float(
    "DYNAMIC_ATR_TRAIL_MULT", 1.40, min_val=0.20, max_val=20.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", 10.00, min_val=0.50, max_val=500.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_EXTENDED_MULT: float = _env_float(
    "DYNAMIC_ATR_TRAIL_EXTENDED_MULT", 1.00, min_val=0.10, max_val=20.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS", 18.00, min_val=1.00, max_val=500.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_HOMERUN_MULT: float = _env_float(
    "DYNAMIC_ATR_TRAIL_HOMERUN_MULT", 0.80, min_val=0.10, max_val=20.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_ACTIVATION_MULT: float = _env_float(
    "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT", 1.00, min_val=0.0, max_val=20.0
)
DYNAMIC_ATR_TRAIL_MIN_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_TRAIL_MIN_DOLLARS", 2.50, min_val=0.20, max_val=100.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_MAX_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_TRAIL_MAX_DOLLARS", 12.00, min_val=0.50, max_val=200.0, positive_only=True
)
DYNAMIC_ATR_TRAIL_STEP_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_TRAIL_STEP_DOLLARS", 0.25, min_val=0.05, max_val=20.0, positive_only=True
)
DYNAMIC_ATR_BE_ACTIVATION_MULT: float = _env_float(
    "DYNAMIC_ATR_BE_ACTIVATION_MULT", 0.75, min_val=0.0, max_val=20.0
)
DYNAMIC_ATR_BE_MIN_MFE_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS", 3.50, min_val=0.0, max_val=50.0
)
DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS: float = _env_float(
    "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS", 0.50, min_val=0.0, max_val=10.0
)
DYNAMIC_ATR_TP_TRAIL_ENABLED: bool = _env_bool("DYNAMIC_ATR_TP_TRAIL_ENABLED", True)
DYNAMIC_ATR_TP_LIFT_ATR_MULT: float = _env_float(
    "DYNAMIC_ATR_TP_LIFT_ATR_MULT", 0.50, min_val=0.05, max_val=5.0, positive_only=True
)
DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT: float = _env_float(
    "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT", 0.75, min_val=0.10, max_val=0.99
)
DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT: float = _env_float(
    "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT", 0.50, min_val=0.05, max_val=0.99
)

# Trade Lifecycle Guard (R-relative profit floor + trade-age management)
# Both features ship DISABLED: enable live with `/rlock on` and `/lifecycle on`.
R_LOCK_ENABLED: bool = _env_bool("R_LOCK_ENABLED", False)
R_LOCK_TIERS: str = os.getenv("R_LOCK_TIERS", "0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75").strip()
R_LOCK_MIN_R_DOLLARS: float = _env_float(
    "R_LOCK_MIN_R_DOLLARS", 0.50, min_val=0.0, max_val=100.0
)
LIFECYCLE_ENABLED: bool = _env_bool("LIFECYCLE_ENABLED", False)
LIFECYCLE_DERISK_SECONDS: int = _env_int(
    "LIFECYCLE_DERISK_SECONDS", 7200, min_val=0, max_val=604800
)
LIFECYCLE_FLATTEN_SECONDS: int = _env_int(
    "LIFECYCLE_FLATTEN_SECONDS", 21600, min_val=0, max_val=604800
)
LIFECYCLE_PENDING_EXPIRE_SECONDS: int = _env_int(
    "LIFECYCLE_PENDING_EXPIRE_SECONDS", 43200, min_val=0, max_val=604800
)
LIFECYCLE_SESSION_FLATTEN_ENABLED: bool = _env_bool("LIFECYCLE_SESSION_FLATTEN_ENABLED", False)
LIFECYCLE_SESSION_FLATTEN_AT: str = os.getenv("LIFECYCLE_SESSION_FLATTEN_AT", "23:30").strip()
LIFECYCLE_MAX_CLOSE_RETRIES: int = _env_int(
    "LIFECYCLE_MAX_CLOSE_RETRIES", 3, min_val=0, max_val=50
)


def validate_dynamic_risk_pairs() -> None:
    """Ensure all dynamic risk min/max pairs satisfy min <= max."""
    g = globals()
    pairs = (
        ("DYNAMIC_SL_MIN_DOLLARS", "DYNAMIC_SL_MAX_DOLLARS"),
        ("DYNAMIC_TP_MIN_DOLLARS", "DYNAMIC_TP_MAX_DOLLARS"),
        ("DYNAMIC_SLTP_SCALE_MIN", "DYNAMIC_SLTP_SCALE_MAX"),
        ("DYNAMIC_ATR_TRAIL_MIN_DOLLARS", "DYNAMIC_ATR_TRAIL_MAX_DOLLARS"),
        ("DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS"),
    )
    for min_k, max_k in pairs:
        mn, mx = float(g[min_k]), float(g[max_k])
        if mn > mx:
            _cfg_logger.warning(f"{min_k} ({mn:.2f}) > {max_k} ({mx:.2f}); swapping to keep min <= max.")
            g[min_k], g[max_k] = mx, mn


validate_dynamic_risk_pairs()


def validate_lifecycle_pairs() -> None:
    """Ensure the trade-age guard thresholds are ordered (flatten must not precede de-risk)."""
    g = globals()
    derisk = int(g.get("LIFECYCLE_DERISK_SECONDS", 0))
    flatten = int(g.get("LIFECYCLE_FLATTEN_SECONDS", 0))
    if flatten < derisk:
        _cfg_logger.warning(
            f"LIFECYCLE_FLATTEN_SECONDS ({flatten}) < LIFECYCLE_DERISK_SECONDS ({derisk}); "
            "raising the flatten threshold to match."
        )
        g["LIFECYCLE_FLATTEN_SECONDS"] = derisk


validate_lifecycle_pairs()
