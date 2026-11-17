import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env file if it exists
load_dotenv(BASE_DIR / ".env")

_cfg_logger = logging.getLogger("copier.config")

# Telegram API credentials (from https://my.telegram.org)
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "12345678"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "your_telegram_api_hash")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "copier_session")

# Forwarded messages policy: if False, Gate 1 discards all forwarded messages
ALLOW_FORWARDS = os.getenv("ALLOW_FORWARDS", "false").lower() in ("true", "1", "yes")

# Target Telegram channel/group IDs to monitor
_raw_channels = os.getenv("CHANNEL_IDS", "[-1001234567890]")
try:
    if _raw_channels.strip().startswith("["):
        CHANNEL_IDS = [int(ch) if str(ch).lstrip("-").isdigit() else str(ch) for ch in json.loads(_raw_channels)]
    else:
        CHANNEL_IDS = [
            int(ch.strip()) if ch.strip().lstrip("-").isdigit() else ch.strip()
            for ch in _raw_channels.split(",")
            if ch.strip()
        ]
except Exception:
    CHANNEL_IDS = [-1001234567890]

# Unique magic number tagging bot trades
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", "777001"))

# Default symbol used for all signals (PU Prime exact Gold symbol: XAUUSD.s)
DEFAULT_SYMBOL = os.getenv("DEFAULT_SYMBOL", "XAUUSD.s")

# Single-price order lot size (when message gives 1 price e.g. "Buy limit 3890" instead of a zone range)
SINGLE_PRICE_LOT = float(os.getenv("SINGLE_PRICE_LOT", "0.01"))

# Seconds between live checks of mt5.terminal_info().trade_allowed
CHECK_ALGO_TRADING_INTERVAL = int(os.getenv("CHECK_ALGO_TRADING_INTERVAL", "30"))

# Ignored symbols: rejects any message explicitly naming a non-Gold pair
_default_ignored = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "US30", "NAS100", "USTEC", "BTCUSD"]
_raw_ignored = os.getenv("IGNORED_SYMBOLS")
if _raw_ignored:
    try:
        IGNORED_SYMBOLS = json.loads(_raw_ignored)
    except Exception as _e:
        _cfg_logger.warning(f"Could not parse IGNORED_SYMBOLS JSON: {_e}")
        IGNORED_SYMBOLS = _default_ignored
else:
    IGNORED_SYMBOLS = _default_ignored

# Asymmetric layer lots (TOP=0.01, MID=0.01, BOTTOM=0.02 => 0.04 max exposure)
_default_layer_lots = {"TOP": 0.01, "MID": 0.01, "BOTTOM": 0.02}
_raw_layer_lots = os.getenv("LAYER_LOTS")
if _raw_layer_lots:
    try:
        LAYER_LOTS = json.loads(_raw_layer_lots)
    except Exception as _e:
        _cfg_logger.warning(f"Could not parse LAYER_LOTS JSON: {_e}")
        LAYER_LOTS = _default_layer_lots
else:
    LAYER_LOTS = _default_layer_lots

# Hard cap preventing any signal, edit, or "Active" reply from exceeding 3 trades
MAX_ORDERS_PER_SIGNAL = int(os.getenv("MAX_ORDERS_PER_SIGNAL", "3"))

# Front-run TP offset (e.g. 0.50 points on XAUUSD: TP1 - 0.50 on BUY, TP1 + 0.50 on SELL)
TP_FRONT_RUN_OFFSET = float(os.getenv("TP_FRONT_RUN_OFFSET", "0.50"))

# Runner Trailing, Floor & Shortfall Guard Settings
TP1_LOCK_BUFFER = float(os.getenv("TP1_LOCK_BUFFER", "0.50"))               # $0.50 buffer below TP1 (BUY) or above TP1 (SELL) for virtual & hard floor
TRAIL_DISTANCE_PIPS = float(os.getenv("TRAIL_DISTANCE_PIPS", "15"))         # $1.50 distance behind live price for hard broker SL trailing
TRAIL_STEP_PIPS = float(os.getenv("TRAIL_STEP_PIPS", "5"))                  # $0.50 minimum SL improvement before sending MT5 modify request
TP2_SHORTFALL_ARM_PIPS = float(os.getenv("TP2_SHORTFALL_ARM_PIPS", "20"))  # $2.00 before TP2 (arms TP2 peak pullback detector)
TP2_SHORTFALL_PULLBACK_PIPS = float(os.getenv("TP2_SHORTFALL_PULLBACK_PIPS", "8"))  # $0.80 drop from runner peak triggers instant market close
NO_TP2_SURGE_ARM_PIPS = float(os.getenv("NO_TP2_SURGE_ARM_PIPS", "40"))   # Fallback arming distance ($4.00 past TP1) if signal omitted TP2
SHORTFALL_ARM_PIPS = float(os.getenv("SHORTFALL_ARM_PIPS", "20"))          # $2.00 before raw_tp1 (arms TP1 shortfall detector)
SHORTFALL_PULLBACK_PIPS = float(os.getenv("SHORTFALL_PULLBACK_PIPS", "8")) # $0.80 drop from peak inside TP1 arming zone

# Closes all open layers & cancels pending limits once price is within 20 pips ($2.00 on Gold) of raw_tp1
TP1_CLOSE_PROXIMITY_PIPS = int(os.getenv("TP1_CLOSE_PROXIMITY_PIPS", "20"))

# Selective Breakeven: only moves TOP and MID layers to BE, leaves BOTTOM untouched
_default_be_layers = ["TOP", "MID"]
_raw_be_layers = os.getenv("BE_LAYERS")
if _raw_be_layers:
    try:
        BE_LAYERS = json.loads(_raw_be_layers)
    except Exception as _e:
        _cfg_logger.warning(f"Could not parse BE_LAYERS JSON: {_e}")
        BE_LAYERS = _default_be_layers
else:
    BE_LAYERS = _default_be_layers

# Breakeven offset in pips (+2.0 pips = +$0.20 on Gold to cover broker commission)
BE_OFFSET_PIPS = float(os.getenv("BE_OFFSET_PIPS", "2.0"))

# Wick-tolerant entry protection
ALLOW_BELOW_ZONE_WICK = os.getenv("ALLOW_BELOW_ZONE_WICK", "true").lower() in ("true", "1", "yes")
MIN_SL_BUFFER_PIPS = int(os.getenv("MIN_SL_BUFFER_PIPS", "20"))

# Emergency fallback SL if signal has no SL initially (in pips: 150 pips = $15 on Gold)
FALLBACK_SL_PIPS = float(os.getenv("FALLBACK_SL_PIPS", "150"))

# Max SL filter in pips: reject fat-finger typos (e.g. SL 387 instead of 3875)
MAX_SL_PIPS = float(os.getenv("MAX_SL_PIPS", "350"))

# Saved Messages ("me") telegram notifications
NOTIFY_SAVED_MESSAGES = os.getenv("NOTIFY_SAVED_MESSAGES", "true").lower() in ("true", "1", "yes")

# Dry-run mode (simulates MT5 execution without live broker connection)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# Deduplication window in seconds
DEDUPLICATION_WINDOW_SECONDS = int(os.getenv("DEDUPLICATION_WINDOW_SECONDS", "5"))

# Seconds to wait on unpriced "Sell now" / "Buy now" before market entry (0 = execute immediately)
SELL_NOW_DELAY_SECONDS = max(0.0, float(os.getenv("SELL_NOW_DELAY_SECONDS") or "0"))

# Polling interval (seconds) inside the delayed "Buy now" / "Sell now" window for mid-delay TP1 checks
DELAYED_POLL_INTERVAL_SECONDS = max(
    0.05, min(1.0, float(os.getenv("DELAYED_POLL_INTERVAL_SECONDS") or "0.25"))
)

# Bad-Position Entry Guard: abort delayed "now" market entry immediately if post-delay live price is worse
# than the arrival market snapshot (or wait up to NOW_ENTRY_MAX_WAIT_SECONDS when set to false).
NOW_PRICE_GUARD_ABORT_ON_MISMATCH = (
    (os.getenv("NOW_PRICE_GUARD_ABORT_ON_MISMATCH") or "true").strip().lower() in ("true", "1", "yes")
)

# Maximum time (seconds) to wait for price recovery when NOW_PRICE_GUARD_ABORT_ON_MISMATCH=false
NOW_ENTRY_MAX_WAIT_SECONDS = max(0.0, float(os.getenv("NOW_ENTRY_MAX_WAIT_SECONDS") or "180"))

# Daily Loss Limit (configured via `/set_daily_loss` or `dailyloss` in Saved Messages): when realized
# closed losses hit the limit and halt the session, cancel all pending orders and leave any existing
# filled positions protected by their broker SL/TP (`false` by default) unless set to `true`.
DAILY_LOSS_CLOSE_OPEN_TRADES = (
    (os.getenv("DAILY_LOSS_CLOSE_OPEN_TRADES") or "false").strip().lower() in ("true", "1", "yes")
)

# Max age (seconds) of an untracked message for an edit to be treated as a new signal (blocks old emoji reactions)
MAX_NEW_EDIT_AGE_SECONDS = int(os.getenv("MAX_NEW_EDIT_AGE_SECONDS", "180"))

# Fallback TP1 (in pips: 50 pips = $5.00 on Gold) if a "Buy now"/"Sell now" never gets edited with a TP
FALLBACK_TP1_PIPS = float(os.getenv("FALLBACK_TP1_PIPS", "50"))
UNPRICED_TP_GRACE_SECONDS = float(os.getenv("UNPRICED_TP_GRACE_SECONDS", "45"))

# Dynamic Risk Guide Cap: blocks opening new lots beyond the High-Risk threshold for current account capital
ENFORCE_HIGH_RISK_LOT_CAP = os.getenv("ENFORCE_HIGH_RISK_LOT_CAP", "true").lower() in ("true", "1", "yes")

# Two-Stage Daily Loss & Account Drawdown Protection:
# - At 50% daily/floating loss: pause NEW signals, but keep existing trades running naturally to SL/TP.
# - At 90% daily/floating loss: urgency cut-off immediately closes losing trades to protect remaining capital.
DAILY_LOSS_PAUSE_PCT = float(os.getenv("DAILY_LOSS_PAUSE_PCT", "50.0"))
EMERGENCY_CUTOFF_LOSS_PCT = float(os.getenv("EMERGENCY_CUTOFF_LOSS_PCT", "90.0"))

# Runtime Uptime, Power & Gaming/UI Impact Controls:
# - PREVENT_SYSTEM_SLEEP: keeps Windows system/network awake while running (allows monitor sleep)
# - IDLE_MT5_POLL_INTERVAL_SECONDS: throttles MT5 account/drawdown IPC polling when 0 trades/limits are active
# - TELEGRAM_KEEPALIVE_SECONDS: active PingRequest + timeout cycle to recover quickly after Wi-Fi drops
# - CONSOLE_TITLE_UPDATES: toggles Win32 SetConsoleTitleW window title-bar updates
PREVENT_SYSTEM_SLEEP = os.getenv("PREVENT_SYSTEM_SLEEP", "true").lower() in ("true", "1", "yes")
IDLE_MT5_POLL_INTERVAL_SECONDS = max(1.0, float(os.getenv("IDLE_MT5_POLL_INTERVAL_SECONDS") or "5.0"))
TELEGRAM_KEEPALIVE_SECONDS = max(5.0, float(os.getenv("TELEGRAM_KEEPALIVE_SECONDS") or "20.0"))
CONSOLE_TITLE_UPDATES = os.getenv("CONSOLE_TITLE_UPDATES", "true").lower() in ("true", "1", "yes")

# MT5 Optional Direct Login Settings
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0")) if os.getenv("MT5_LOGIN") else None
MT5_PASSWORD = os.getenv("MT5_PASSWORD") or None
MT5_SERVER = os.getenv("MT5_SERVER") or None
MT5_PATH = os.getenv("MT5_PATH") or None

# File paths
STATE_FILE_PATH = Path(os.getenv("STATE_FILE_PATH", str(BASE_DIR / "signals_state.json")))
LOG_FILE_PATH = Path(os.getenv("LOG_FILE_PATH", str(BASE_DIR / "copier.log")))
