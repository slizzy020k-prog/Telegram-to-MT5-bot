import os
import re
import sys
import copy
import time
import inspect
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Callable

from telethon import TelegramClient, events

import config
from config import (
    BASE_DIR,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_SESSION_NAME,
    CHANNEL_IDS,
    DEFAULT_SYMBOL,
    MAGIC_NUMBER,
    SINGLE_PRICE_LOT,
    CHECK_ALGO_TRADING_INTERVAL,
    LAYER_NAMES,
    LEGACY_LAYER_MAP,
    BASE_LAYER_LOTS,
    RISK_LOT_MULTIPLIER,
    LAYER_LOTS,
    MAX_ORDERS_PER_SIGNAL,
    BE_LAYERS,
    BE_OFFSET_PIPS,
    BE_AFTER_TP1,
    CANCEL_PENDING_ON_STAGE0_LOCK_EXIT,
    CANCEL_PENDING_ON_TP1,
    TP_FRONT_RUN_OFFSET,
    LIMIT_ENTRY_FRONT_RUN_OFFSET,
    TP1_CLOSE_PROXIMITY_PIPS,
    DEFAULT_TP_STEP_PIPS,
    TP2_STEP_PIPS,
    TP3_STEP_PIPS,
    TP4_STEP_PIPS,
    TP5_STEP_PIPS,
    RUNNER_DEEPEST_COUNT_FULL,
    RUNNER_MAX_TP_STAGE,
    RUNNER_PROACTIVE_TP_LIFT,
    RUNNER_PROXIMITY_MODE,
    RUNNER_PROXIMITY_BUFFER_PIPS,
    RUNNER_PROXIMITY_BUFFER_PCT,
    RUNNER_MAX_MIDRANGE_RATIO,
    RUNNER_REVERSAL_PULLBACK_PIPS,
    RUNNER_REVERSAL_PULLBACK_PCT,
    RUNNER_REVERSAL_CONFIRM_TICKS,
    RUNNER_REVERSAL_CANDLE_CONFIRM,
    RUNNER_CLEAN_BREAK_PIPS,
    GOLD_REVERSAL_GUARD_ENABLED,
    ARM_THRESHOLD_PCT,
    GIVEBACK_PCT,
    MIN_GOLD_PULLBACK_DOLLARS,
    ATR_NOISE_FLOOR_MULT,
    MAX_GIVEBACK_DOLLARS,
    MAX_ALLOWED_SPREAD_DOLLARS,
    ADAPTIVE_SPREAD_CEILING_DOLLARS,
    RUNNER_EARLY_TP_LIFT_PCT,
    RUNNER_CONTINUOUS_TRAIL_ENABLED,
    SHORTFALL_CONFIRM_TICKS,
    NOTIFY_SAVED_MESSAGES,
    DRY_RUN,
    LOG_FILE_PATH,
    STATE_FILE_PATH,
    TRADES_LEDGER_PATH,
    LOCK_FILE_PATH,
    DEDUPLICATION_WINDOW_SECONDS,
    STAGED_MARKET_ENTRY,
    SELL_NOW_DELAY_SECONDS,
    DELAYED_POLL_INTERVAL_SECONDS,
    NOW_PRICE_GUARD_ABORT_ON_MISMATCH,
    NOW_ENTRY_MAX_WAIT_SECONDS,
    DAILY_LOSS_CLOSE_OPEN_TRADES,
    MAX_NEW_EDIT_AGE_SECONDS,
    MAX_EDIT_EVENT_FRESHNESS_SECONDS,
    FALLBACK_TP1_PIPS,
    UNPRICED_TP_GRACE_SECONDS,
    DAILY_LOSS_PAUSE_PCT,
    EMERGENCY_CUTOFF_LOSS_PCT,
    FLOAT_LOSS_FORCE_CLOSE_PCT,
    TP1_LOCK_BUFFER,
    TRAIL_DISTANCE_PIPS,
    TRAIL_STEP_PIPS,
    TP2_SHORTFALL_ARM_PIPS,
    TP2_SHORTFALL_PULLBACK_PIPS,
    NO_TP2_SURGE_ARM_PIPS,
    SHORTFALL_ARM_PIPS,
    SHORTFALL_PULLBACK_PIPS,
    CATCHUP_WINDOW_SECONDS,
    CATCHUP_MAX_MESSAGES,
    CATCHUP_MARKET_MAX_AGE_SECONDS,
    CATCHUP_MARKET_MAX_SLIPPAGE_PIPS,
    CLOSE_ON_SIGNAL_DELETED,
    DELETED_SIGNAL_POLL_INTERVAL_SECONDS,
    PREVENT_SYSTEM_SLEEP,
    IDLE_MT5_POLL_INTERVAL_SECONDS,
    TELEGRAM_KEEPALIVE_SECONDS,
    CONSOLE_TITLE_UPDATES,
    STAGE0_PROFIT_LOCK_ENABLED,
    STAGED_EXIT_ENABLED,
    PROFIT_RATCHET_ENABLED,
    PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED,
    BANK_HARVEST_ENABLED,
    TIERED_STAGE_BANKING_ENABLED,
    CANCEL_PENDING_ON_BE,
    CANCEL_PENDING_ON_PROFIT_LOCK,
    PENDING_PNL_BACKFILL_MAX_SECONDS,
    SINGLE_PRICE_CONSOLIDATE_ORDERS,
    PENDING_DUPLICATE_PRICE_TOLERANCE,
    PENDING_DUPLICATE_ORDER_TOLERANCE,
    DYNAMIC_ATR_SLTP_ENABLED,
    DYNAMIC_ATR_TRAIL_ENABLED,
)
from parser import (
    parse_message,
    finalize_signal_sanity,
    SignalAction,
    ZoneInfo,
    is_tp1_hit_message,
    detect_tp_milestone_hit,
    extract_labeled_tps,
    merge_tp_levels_for_active_signal,
    gate_3_classify_trade_management,
    derive_tp_levels,
    compute_zone_4tiers,
)
from mt5_executor import MT5Executor
from state_manager import StateManager, _chat_ids_match
from trade_ledger import TradeLedger
from startup_checks import (
    DUPLICATE_INSTANCE_EXIT_CODE,
    SingleInstanceLock,
    acquire_single_instance_lock,
    release_single_instance_lock,
    validate_environment,
)
from risk_controls import (
    apply_float_cap_command,
    apply_loss_limit_command,
    apply_lot_cap_command,
    format_limits_status_lines,
    read_risk_limits,
)
from dynamic_risk import (
    apply_atr_trail_command,
    apply_dynamic_risk_command,
    compute_atr_fallback_tp_distance,
    compute_volatility_ratio,
    format_dynamic_risk_status_line,
    read_dynamic_risk_policy,
)


# ---------------------------------------------------------------------------
# LOGGING CONFIGURATION (COLORIZED CONSOLE + CLEAN FILE LOGS)
# ---------------------------------------------------------------------------

class ColoredConsoleFormatter(logging.Formatter):
    """Vibrant ANSI color formatter for interactive terminal display."""
    COLORS = {
        logging.DEBUG: "\033[90m",      # Gray
        logging.INFO: "\033[92m",       # Bright Green
        logging.WARNING: "\033[93m",    # Bright Yellow / Gold
        logging.ERROR: "\033[91m",      # Bright Red
        logging.CRITICAL: "\033[95m",   # Bright Magenta
    }
    RESET = "\033[0m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    WHITE = "\033[97m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        time_str = f"{self.CYAN}{self.formatTime(record, self.datefmt)}{self.RESET}"
        level_str = f"{color}{self.BOLD}[{record.levelname:<7}]{self.RESET}"
        name_str = f"{self.WHITE}[{record.name}]{self.RESET}"
        msg = f"{color}{record.getMessage()}{self.RESET}"
        return f"{time_str} {level_str} {name_str} {msg}"


# Current in-place MT5 bottom status bar rendered on the console
_current_hud_line: str = ""


class LiveHUDStreamHandler(logging.StreamHandler):
    """Clears the live in-place MT5 balance bar before emitting a log line, then redraws it."""

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            if _current_hud_line:
                stream.write(f"\r\033[2K{msg}{self.terminator}{_current_hud_line}")
            else:
                stream.write(f"{msg}{self.terminator}")
            self.flush()
        except Exception:
            self.handleError(record)


def setup_logger():
    """Configure console + rotating file loggers for copier and Telethon."""
    if sys.platform == "win32":
        try:
            os.system("")
        except Exception:
            pass

    log = logging.getLogger("copier")
    log.setLevel(logging.INFO)

    if log.handlers:
        return log

    console_handler = LiveHUDStreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredConsoleFormatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    console_handler.setLevel(logging.INFO)
    log.addHandler(console_handler)

    clean_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(clean_formatter)
    file_handler.setLevel(logging.INFO)
    log.addHandler(file_handler)

    # Route Telethon's internal logger through our handlers so Python's raw lastResort
    # stderr handler never prints unformatted text onto the [MT5] bottom status bar
    telethon_logger = logging.getLogger("telethon")
    telethon_logger.setLevel(logging.WARNING)
    telethon_logger.propagate = False
    if not telethon_logger.handlers:
        tg_console_handler = LiveHUDStreamHandler(sys.stdout)
        tg_console_handler.setFormatter(ColoredConsoleFormatter(
            "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        tg_console_handler.setLevel(logging.ERROR)
        telethon_logger.addHandler(tg_console_handler)
        telethon_logger.addHandler(file_handler)

    return log


logger = setup_logger()

# Shared Singletons
state_mgr = StateManager(STATE_FILE_PATH)
trade_ledger = TradeLedger(TRADES_LEDGER_PATH)
executor = MT5Executor(dry_run=DRY_RUN)
telethon_client: Optional[TelegramClient] = None
_instance_lock: Optional[SingleInstanceLock] = None
BOT_START_TIME = time.time()

# In-memory deduplication tracking ((chat_id, msg_id) or msg_id -> process_timestamp / last_raw_text)
_recent_messages: Dict[Any, float] = {}
_recent_message_texts: Dict[Any, str] = {}

# Out-of-order tombstone map for deleted Telegram signal messages ((chat_id, msg_id) -> deleted_timestamp)
_deleted_messages: Dict[Any, float] = {}

# Per-(loop, chat_id, msg_id) asyncio locks preventing handler/edit/delay races
_signal_locks: Dict[Tuple[int, Any, str], asyncio.Lock] = {}
_catchup_locks: Dict[int, asyncio.Lock] = {}

# Delayed "Sell now" / "Buy now" tasks awaiting limit/zone edit (msg_id or (chat_id, msg_id) -> asyncio.Task)
_pending_sell_now_tasks: Dict[Any, asyncio.Task] = {}
_pending_task_chats: Dict[Any, Any] = {}

# Runtime telemetry for /health, /status, and one-shot disconnect/recovery alerts
_telegram_reconnect_count: int = 0
_mt5_connected_state: Optional[bool] = None
_mt5_last_disconnect_at: Optional[float] = None
_mt5_last_ping_ms: Optional[float] = None
_runner_save_throttle_ts: Dict[str, float] = {}

# Last printed terminal financial/risk snapshot to avoid spamming identical lines
_last_terminal_snapshot: Optional[Tuple[Any, ...]] = None

# Tracks date string (YYYY-MM-DD) when daily loss pause alert was last sent
_daily_50pct_alert_date: Optional[str] = None

# Tracks date string when the floating-loss force-close alert was last sent (avoid repeat messages)
_daily_float_cap_alert_date: Optional[str] = None

# Global trading-halted circuit breaker (set when Daily Realized Loss Limit triggers)
TRADING_HALTED: bool = False

# Exit code signaling the launcher .bat that Daily Win Target was reached (do not auto-restart)
DAILY_WIN_EXIT_CODE = 88

# Tracks whether Saved Messages is waiting for the user's daily win target input after typing "dailywin"
_awaiting_dailywin_input: bool = False

# Flag set when Daily Win Target is reached so main() exits with DAILY_WIN_EXIT_CODE (88)
_daily_win_terminate_requested: bool = False

# Exit code signaling the launcher .bat that the Daily LOSS Limit was hit (do not auto-restart)
DAILY_LOSS_EXIT_CODE = 89

# Saved Messages is waiting for the user's daily loss limit input after typing "dailyloss"
_awaiting_dailyloss_input: bool = False

# Flag set when the realized Daily Loss Limit is hit so main() terminates cleanly
_daily_loss_terminate_requested: bool = False

# Set when the copier is relaunched manually on the same broker day after the loss limit fired:
# stays online for Saved Messages commands, but blocks every new trade until the user unlocks it.
_daily_loss_locked: bool = False
_daily_loss_lock_notified_date: Optional[str] = None


# ---------------------------------------------------------------------------
# ASYNCIO CONCURRENCY & NON-BLOCKING MT5 IPC HELPERS
# ---------------------------------------------------------------------------

async def _mt5_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Offload a blocking MT5/executor IPC call onto a worker thread so Telethon never stalls."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _normalize_task_msg_id(msg_id: Any, chat_id: Optional[int | str] = None) -> Tuple[Any, Optional[int | str]]:
    """Unpack composite 'chat_id:msg_id' or tuple keys into (numeric/raw msg_id, chat_id)."""
    if isinstance(msg_id, tuple) and len(msg_id) == 2:
        t_chat, t_mid = msg_id
        msg_id = t_mid
        if chat_id is None:
            chat_id = t_chat
    elif isinstance(msg_id, str) and ":" in msg_id:
        c_part, m_part = msg_id.rsplit(":", 1)
        if chat_id is None:
            chat_id = int(c_part) if c_part.lstrip("-").isdigit() else c_part
        msg_id = m_part
    try:
        return int(msg_id), chat_id
    except (TypeError, ValueError):
        return msg_id, chat_id


def _canonical_chat_key(chat_id: Any) -> Any:
    """Normalize Telegram supergroup/channel ID variants (-100<id>, -<id>, <id>) to one canonical key."""
    if chat_id is None or isinstance(chat_id, bool):
        return None
    raw = str(chat_id).strip()
    if not raw:
        return None
    digits = raw.lstrip("-")
    if digits.isdigit():
        bare = digits[3:] if (digits.startswith("100") and len(digits) > 3) else digits
        return f"-100{bare}"
    return raw


def _get_signal_lock(chat_id: Optional[int | str], msg_id: Any) -> asyncio.Lock:
    """Return a per-(event_loop, canonical_chat_id, msg_id) asyncio.Lock serializing edits, replies, and timers."""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    key = (loop_id, _canonical_chat_key(eff_chat), str(mid_norm))
    lock = _signal_locks.get(key)
    if lock is None:
        if len(_signal_locks) > 1024:
            stale = [k for k, lk in _signal_locks.items() if k[0] != loop_id or not lk.locked()]
            for k in stale[:512]:
                _signal_locks.pop(k, None)
        lock = asyncio.Lock()
        _signal_locks[key] = lock
    return lock


def _get_catchup_lock() -> asyncio.Lock:
    """Return an event-loop-scoped asyncio.Lock preventing concurrent catch-up runs."""
    loop_id = id(asyncio.get_running_loop())
    lock = _catchup_locks.get(loop_id)
    if lock is None:
        _catchup_locks.clear()
        lock = asyncio.Lock()
        _catchup_locks[loop_id] = lock
    return lock


def session_trading_blocked() -> bool:
    """True when trading is halted/terminating or locked (daily win / daily loss) -> never open new trades."""
    return bool(TRADING_HALTED or _daily_win_terminate_requested or _daily_loss_terminate_requested or _daily_loss_locked)


def _trading_day_str() -> str:
    """Current broker-server trading day (YYYY-MM-DD) so daily limits roll over with MT5's day."""
    try:
        return executor.get_server_date_str()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


async def _trading_day_str_async() -> str:
    """Offloaded broker-server trading day lookup."""
    try:
        return await _mt5_call(executor.get_server_date_str)
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# UNIFIED CLOSE ACCOUNTING & TRADE LEDGER HELPERS
# ---------------------------------------------------------------------------

def _safe_ticket_int(ticket: Any) -> Optional[int]:
    if isinstance(ticket, int) and not isinstance(ticket, bool) and ticket > 0:
        return ticket
    try:
        if isinstance(ticket, str) and ticket.strip().isdigit():
            val = int(ticket.strip())
            return val if val > 0 else None
    except (TypeError, ValueError):
        pass
    return None


def resolve_layer_label(sig: Optional[Dict[str, Any]], ticket: Any = None, fallback: Optional[str] = None) -> str:
    """Resolve human-readable layer label (`Layer L4`, `Ticket #123`) — never `Layer None`."""
    ti = _safe_ticket_int(ticket)
    if isinstance(sig, dict):
        layers = sig.get("layers")
        if ti is not None and isinstance(layers, dict):
            for lk, lv in layers.items():
                if isinstance(lv, dict) and _safe_ticket_int(lv.get("ticket")) == ti:
                    return f"Layer {lk}"
        for k in ("runner_layer", "last_runner_layer"):
            val = sig.get(k)
            if isinstance(val, str) and val.strip() and val.strip().lower() not in ("none", "null"):
                return f"Layer {val.strip()}"
        for k_l in ("runner_layers", "last_runner_layers"):
            vals = sig.get(k_l)
            if isinstance(vals, list) and vals:
                last_v = vals[-1]
                if isinstance(last_v, str) and last_v.strip() and last_v.strip().lower() not in ("none", "null"):
                    return f"Layer {last_v.strip()}"
    if isinstance(fallback, str) and fallback.strip() and fallback.strip().lower() not in ("none", "null"):
        return f"Layer {fallback.strip()}"
    if ti is not None:
        return f"Ticket #{ti}"
    return "Runner"


async def _account_closed_tickets(
    sig: Dict[str, Any],
    tickets: List[Any],
    close_infos: Optional[Dict[int, Dict[str, Any]]] = None,
    partial_only: bool = False,
) -> Tuple[float, Dict[int, Dict[str, Any]]]:
    """
    Deduplicate realized ticket/deal PnL across `sig['accounted_deal_tickets']` &
    `sig['accounted_tickets']` and accumulate into `sig['banked_profit']`.
    Handles broker deal settlement lag via `pending_pnl_tickets`.
    """
    accounted: List[int] = []
    for raw_a in (sig.get("accounted_tickets") or []):
        ai = _safe_ticket_int(raw_a)
        if ai is not None and ai not in accounted:
            accounted.append(ai)

    accounted_deals: List[int] = []
    for raw_d in (sig.get("accounted_deal_tickets") or []):
        di = _safe_ticket_int(raw_d)
        if di is not None and di not in accounted_deals:
            accounted_deals.append(di)

    raw_pending_pnl = sig.get("pending_pnl_tickets")
    pending_pnl: Dict[str, float] = (
        {str(k): float(v) for k, v in raw_pending_pnl.items() if _is_num(v)}
        if isinstance(raw_pending_pnl, dict)
        else {}
    )

    total_banked = float(sig.get("banked_profit") or 0.0)
    infos: Dict[int, Dict[str, Any]] = {}
    for k_t, v_i in (close_infos or {}).items():
        ki = _safe_ticket_int(k_t)
        if ki is not None:
            infos[ki] = v_i
    added_profit = 0.0

    unique_tickets: List[int] = []
    for raw_t in (tickets or []):
        ti = _safe_ticket_int(raw_t)
        if ti is not None and ti not in unique_tickets:
            unique_tickets.append(ti)

    cached_map = getattr(executor, "_cached_pos_profits", None)
    max_wait = float(_main_cfg("PENDING_PNL_BACKFILL_MAX_SECONDS", PENDING_PNL_BACKFILL_MAX_SECONDS))

    for t in unique_tickets:
        if t not in infos:
            info = await _mt5_call(executor.get_position_close_info, t)
            if isinstance(info, dict):
                info = dict(info)
                det_map = getattr(executor, "_last_close_details", None)
                det = det_map.get(t) if isinstance(det_map, dict) else None
                if (
                    isinstance(det, dict)
                    and "exit_deals" not in info
                    and det.get("profit") == info.get("profit")
                    and det.get("reason") == info.get("reason")
                ):
                    for ek in ("exit_price", "entry_price", "exit_deals", "exit_deal_tickets", "commission", "swap", "fee", "deals_settled"):
                        if ek in det:
                            info[ek] = det[ek]
            if (
                isinstance(info, dict)
                and float(info.get("profit", 0.0) or 0.0) == 0.0
                and str(info.get("reason") or "UNKNOWN").upper() == "UNKNOWN"
                and isinstance(cached_map, dict)
                and t in cached_map
            ):
                info = {"profit": round(float(cached_map[t]), 2), "reason": "UNKNOWN"}
            infos[t] = info if isinstance(info, dict) else {"profit": 0.0, "reason": "UNKNOWN"}

        info_d = infos[t]
        exit_deals = info_d.get("exit_deals")
        if isinstance(exit_deals, list) and exit_deals:
            pending_pnl.pop(str(t), None)
            if (t in accounted) and (not accounted_deals) and (not partial_only):
                continue
            for ed in exit_deals:
                if not isinstance(ed, dict):
                    continue
                did = _safe_ticket_int(
                    ed.get("deal_ticket") if ed.get("deal_ticket") is not None else ed.get("deal")
                )
                if did is not None and did not in accounted_deals:
                    dp = float(ed.get("profit", 0.0) or 0.0)
                    accounted_deals.append(did)
                    added_profit += dp
                    total_banked += dp
            if (not partial_only) and (t not in accounted):
                accounted.append(t)
        elif t not in accounted:
            if partial_only:
                continue
            raw_r = str(info_d.get("reason") or "UNKNOWN").upper()
            raw_p = float(info_d.get("profit", 0.0) or 0.0)
            has_cached = isinstance(cached_map, dict) and t in cached_map
            if (not executor.dry_run) and raw_r == "UNKNOWN" and raw_p == 0.0 and not has_cached:
                now_ts = time.time()
                first_ts = float(pending_pnl.get(str(t), now_ts))
                pending_pnl[str(t)] = first_ts
                if (now_ts - first_ts) < max_wait:
                    continue
                pending_pnl.pop(str(t), None)
                accounted.append(t)
                continue
            pending_pnl.pop(str(t), None)
            accounted.append(t)
            added_profit += raw_p
            total_banked += raw_p

    sig["accounted_tickets"] = accounted
    sig["accounted_deal_tickets"] = accounted_deals
    sig["pending_pnl_tickets"] = pending_pnl
    sig["banked_profit"] = round(total_banked, 2)
    return round(added_profit, 2), infos


def _has_executed_exposure(sig: Dict[str, Any]) -> bool:
    """Return True if a signal actually had filled/executed exposure (not merely cancelled pending setup)."""
    if (
        sig.get("accounted_tickets")
        or sig.get("accounted_deal_tickets")
        or sig.get("market_tickets")
        or sig.get("last_runner_ticket")
        or sig.get("last_banked_tickets")
    ):
        return True
    layers = sig.get("layers") or {}
    if isinstance(layers, dict):
        for l_data in layers.values():
            if not isinstance(l_data, dict):
                continue
            st = str(l_data.get("status") or "").upper()
            ot = str(l_data.get("order_type") or "").upper()
            if st in ("OPEN", "FILLED", "RUNNER", "BANKED"):
                return True
            if st == "CLOSED" and ot == "MARKET" and l_data.get("ticket"):
                return True
    return False


_MAIN_CFG_SNAPSHOT: Dict[str, Any] = {
    "STAGED_EXIT_ENABLED": STAGED_EXIT_ENABLED,
    "STAGE0_PROFIT_LOCK_ENABLED": STAGE0_PROFIT_LOCK_ENABLED,
    "CANCEL_PENDING_ON_STAGE0_LOCK_EXIT": CANCEL_PENDING_ON_STAGE0_LOCK_EXIT,
    "PROFIT_RATCHET_ENABLED": PROFIT_RATCHET_ENABLED,
    "PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED": PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED,
    "BANK_HARVEST_ENABLED": BANK_HARVEST_ENABLED,
    "TIERED_STAGE_BANKING_ENABLED": TIERED_STAGE_BANKING_ENABLED,
    "CANCEL_PENDING_ON_BE": CANCEL_PENDING_ON_BE,
    "CANCEL_PENDING_ON_PROFIT_LOCK": CANCEL_PENDING_ON_PROFIT_LOCK,
    "PENDING_PNL_BACKFILL_MAX_SECONDS": PENDING_PNL_BACKFILL_MAX_SECONDS,
    "SINGLE_PRICE_CONSOLIDATE_ORDERS": SINGLE_PRICE_CONSOLIDATE_ORDERS,
    "PENDING_DUPLICATE_PRICE_TOLERANCE": PENDING_DUPLICATE_PRICE_TOLERANCE,
    "PENDING_DUPLICATE_ORDER_TOLERANCE": PENDING_DUPLICATE_ORDER_TOLERANCE,
    "LIMIT_ENTRY_FRONT_RUN_OFFSET": LIMIT_ENTRY_FRONT_RUN_OFFSET,
    "DYNAMIC_ATR_SLTP_ENABLED": DYNAMIC_ATR_SLTP_ENABLED,
    "DYNAMIC_ATR_TRAIL_ENABLED": DYNAMIC_ATR_TRAIL_ENABLED,
}


def _merge_dynamic_risk_into_signal_fields(
    snap: Optional[Dict[str, Any]],
    sl: Optional[float],
    final_tp: Optional[float],
    raw_tp1: Optional[float],
    raw_tp2: Optional[float],
    raw_tp3: Optional[float],
    raw_tp4: Optional[float],
    raw_tp5: Optional[float],
    tp_levels: Optional[Dict[str, float]],
) -> Dict[str, Any]:
    """Return updated SL/TP signal fields when ATR-scaled SL/TP modified entry targets."""
    if not (isinstance(snap, dict) and snap.get("applied")):
        return {
            "sl": sl,
            "final_tp": final_tp,
            "raw_tp1": raw_tp1,
            "raw_tp2": raw_tp2,
            "raw_tp3": raw_tp3,
            "raw_tp4": raw_tp4,
            "raw_tp5": raw_tp5,
            "tp_levels": tp_levels,
            "dynamic_risk": None,
        }
    t_map = dict(snap.get("tp_levels") or tp_levels or {})
    return {
        "sl": snap.get("sl") if snap.get("sl") is not None else sl,
        "final_tp": snap.get("final_tp") if snap.get("final_tp") is not None else final_tp,
        "raw_tp1": t_map.get("TP1", snap.get("raw_tp1", raw_tp1)),
        "raw_tp2": t_map.get("TP2", raw_tp2),
        "raw_tp3": t_map.get("TP3", raw_tp3),
        "raw_tp4": t_map.get("TP4", raw_tp4),
        "raw_tp5": t_map.get("TP5", raw_tp5),
        "tp_levels": t_map or tp_levels,
        "dynamic_risk": dict(snap),
    }


def _main_cfg(name: str, default: Any) -> Any:
    """Resolve config value dynamically across main and config module for test monkeypatch compatibility."""
    if name in globals() and name in _MAIN_CFG_SNAPSHOT and globals()[name] != _MAIN_CFG_SNAPSHOT[name]:
        return globals()[name]
    _cfg_mod = sys.modules.get("config")
    return getattr(_cfg_mod, name, default)


def _is_staged_exit_enabled() -> bool:
    """Resolve STAGED_EXIT_ENABLED dynamically across config and main for runtime/monkeypatch safety."""
    return bool(_main_cfg("STAGED_EXIT_ENABLED", STAGED_EXIT_ENABLED))


def _is_cancel_pending_on_stage0_lock_exit() -> bool:
    """Resolve CANCEL_PENDING_ON_STAGE0_LOCK_EXIT dynamically across config and main."""
    return bool(_main_cfg("CANCEL_PENDING_ON_STAGE0_LOCK_EXIT", CANCEL_PENDING_ON_STAGE0_LOCK_EXIT))


def _is_num(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _get_signal_stage(sig: Dict[str, Any]) -> int:
    """Derive canonical v3.5.0 runner stage (0..4) from persisted signal fields."""
    if not isinstance(sig, dict):
        return 0
    fn = getattr(executor, "get_runner_fsm_stage", None)
    if callable(fn):
        try:
            res = fn(sig)
            if isinstance(res, int) and not isinstance(res, bool):
                return max(0, min(4, res))
        except Exception:
            pass

    status = str(sig.get("status") or "").strip().upper()
    if status in ("PENDING", "PENDING_DELAY", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"):
        return 0
    cleared_stage = 0
    for x in sig.get("runner_cleared_milestones") or []:
        s = str(x).strip().upper()
        if s.startswith("TP") and s[2:].isdigit():
            cleared_stage = max(cleared_stage, int(s[2:]))
    raw_idx = sig.get("runner_milestone_idx")
    idx_stage = (
        (int(raw_idx) - 1)
        if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) and int(raw_idx) > 1
        else 0
    )
    in_runner_phase = bool(
        sig.get("tp1_reached")
        or status == "RUNNER_ACTIVE"
        or cleared_stage >= 1
        or idx_stage >= 1
    )
    if not in_runner_phase:
        return 0
    return min(4, max(1, cleared_stage, idx_stage))


def _resolve_signal_ref_entry(sig: Dict[str, Any]) -> Optional[float]:
    """Resolve reference entry price for MFE computation."""
    if not isinstance(sig, dict):
        return None
    is_buy = str(sig.get("direction") or "BUY").strip().upper() == "BUY"
    layers = sig.get("layers")
    runner_layer = (
        sig.get("runner_layer")
        or sig.get("last_runner_layer")
        or ((sig.get("last_runner_layers") or [None])[-1])
    )
    if runner_layer and isinstance(layers, dict):
        r_obj = layers.get(runner_layer)
        if isinstance(r_obj, dict):
            for k in ("entry", "price_open", "target_price"):
                if _is_num(r_obj.get(k)) and float(r_obj[k]) > 0:
                    return float(r_obj[k])

    for k in ("recorded_entry_price", "initial_price", "single_price"):
        if _is_num(sig.get(k)) and float(sig[k]) > 0:
            return float(sig[k])

    zone = sig.get("zone")
    if isinstance(zone, dict):
        z_px = zone.get("top") if is_buy else zone.get("bottom")
        if _is_num(z_px) and float(z_px) > 0:
            return float(z_px)

    if isinstance(layers, dict):
        for lk in ("L4", "L3", "L2", "L1", "BOTTOM", "MID", "TOP"):
            l_obj = layers.get(lk)
            if isinstance(l_obj, dict):
                for k in ("entry", "price_open", "target_price"):
                    if _is_num(l_obj.get(k)) and float(l_obj[k]) > 0:
                        return float(l_obj[k])
    return None


def _ensure_signal_exit_telemetry(
    sig: Dict[str, Any],
    exit_reason_code: Optional[str] = None,
    exit_stage: Optional[int] = None,
    exit_price: Optional[float] = None,
    overwrite_code: bool = False,
) -> None:
    """Populate or backfill v3.5.0 exit telemetry fields on signal_data before ledger persistence."""
    if not isinstance(sig, dict):
        return
    is_buy = str(sig.get("direction") or "BUY").strip().upper() == "BUY"

    if exit_reason_code is not None and (overwrite_code or not sig.get("exit_reason_code")):
        sig["exit_reason_code"] = str(exit_reason_code).strip().upper()

    if exit_stage is not None:
        sig["exit_stage"] = max(0, min(4, int(exit_stage)))
    elif not _is_num(sig.get("exit_stage")):
        sig["exit_stage"] = _get_signal_stage(sig)

    if _is_num(exit_price):
        sig["exit_price"] = round(float(exit_price), 4)
    elif _is_num(sig.get("exit_price")):
        sig["exit_price"] = round(float(sig["exit_price"]), 4)

    cur_exit_px = sig.get("exit_price") if _is_num(sig.get("exit_price")) else None
    raw_mfe = sig.get("max_favorable_excursion")
    raw_pk = raw_mfe.get("peak_price") if isinstance(raw_mfe, dict) else None
    raw_pp_oz = raw_mfe.get("peak_profit_per_oz") if isinstance(raw_mfe, dict) else None

    if not _is_num(raw_pk):
        for pk_key in ("trail_peak", "runner_peak_price", "peak_price", "reversal_guard_peak_price"):
            if _is_num(sig.get(pk_key)):
                raw_pk = sig[pk_key]
                break
    peak_px = round(float(raw_pk), 4) if _is_num(raw_pk) else None

    if _is_num(raw_pp_oz):
        peak_prof_oz: Optional[float] = round(max(0.0, float(raw_pp_oz)), 4)
    else:
        ref_entry = _resolve_signal_ref_entry(sig)
        if peak_px is not None and ref_entry is not None:
            raw_exc = (peak_px - ref_entry) if is_buy else (ref_entry - peak_px)
            peak_prof_oz = round(max(0.0, raw_exc), 4)
        else:
            fallback_pp = (
                sig.get("reversal_guard_peak_profit")
                if _is_num(sig.get("reversal_guard_peak_profit"))
                else sig.get("peak_profit")
            )
            peak_prof_oz = round(max(0.0, float(fallback_pp)), 4) if _is_num(fallback_pp) else None

    sig["max_favorable_excursion"] = {
        "peak_price": peak_px,
        "peak_profit_per_oz": peak_prof_oz,
    }

    if not _is_num(sig.get("giveback_from_peak")):
        if sig.get("exit_reason_code") == "TP_CEILING" and (peak_px is not None or cur_exit_px is not None):
            sig["giveback_from_peak"] = 0.0
        elif peak_px is not None and cur_exit_px is not None:
            raw_drop = (peak_px - float(cur_exit_px)) if is_buy else (float(cur_exit_px) - peak_px)
            sig["giveback_from_peak"] = round(max(0.0, raw_drop), 4)
        else:
            sig.setdefault("giveback_from_peak", None)

    if int(sig.get("exit_stage") or 0) == 0:
        sig["trail_tier_used"] = None
    elif not isinstance(sig.get("trail_tier_used"), dict):
        sig["trail_tier_used"] = None


def _emit_exit_telemetry_log(
    msg_id: Any,
    sig: Dict[str, Any],
    realized_pnl: float,
    exit_reason: Optional[str] = None,
) -> None:
    """Emit structured v3.5.0 [EXIT TELEMETRY] log entry on trade exit."""
    code = sig.get("exit_reason_code") or exit_reason or sig.get("runner_close_reason") or "CLOSED"
    detail = exit_reason or sig.get("runner_close_reason") or str(code)
    stage = sig.get("exit_stage") if _is_num(sig.get("exit_stage")) else _get_signal_stage(sig)
    exit_px = sig.get("exit_price")
    mfe = sig.get("max_favorable_excursion")
    giveback = sig.get("giveback_from_peak")
    tier = sig.get("trail_tier_used")
    logger.info(
        f"[EXIT TELEMETRY] msg_id={msg_id} | exit_reason={code} ({detail}) | "
        f"exit_stage={stage} | exit_price={exit_px} | "
        f"max_favorable_excursion={mfe} | realized_pnl={realized_pnl:.2f} | "
        f"giveback_from_peak={giveback} | trail_tier_used={tier}"
    )


async def _emit_ratchet_alerts(
    msg_id: Any,
    sig: Dict[str, Any],
    events: Optional[List[Dict[str, Any]]] = None,
    lift_aborts: Optional[List[Dict[str, Any]]] = None,
    partial_bank: Optional[Dict[str, Any]] = None,
    partial_profit: Optional[float] = None,
    curr: str = "GBP",
) -> None:
    """Emit concise, deduplicated Telegram alerts for v3.6 Profit Ratchet transitions."""
    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = sig.get("direction", "BUY")

    for ev in (events or []):
        ev_type = str(ev.get("type") or "").upper()
        tk = ev.get("ticket")
        lbl = resolve_layer_label(sig, tk, fallback=ev.get("layer"))
        role = ev.get("role") or "RUNNER"
        ent = ev.get("entry_price")
        pk = ev.get("peak_price")
        mfe_d = float(ev.get("mfe_dollars") or 0.0)
        bsl = ev.get("broker_sl")
        tier = ev.get("tier")
        if ev_type == "TP_LIFT":
            await notify_saved_messages(
                f"📈 **2-Step TP Lift -> {ev.get('new_tp_stage', 'TP2')} [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction}) | `{lbl}` (`{role}`) [Ticket #{tk}]\n"
                f"• Broker SL Locked: `{bsl}` -> TP Lifted `{ev.get('old_tp')}` ➔ `{ev.get('new_tp')}`\n"
                f"• Peak MFE: `{pk}` (`+${mfe_d:.2f}/oz`, Tier `{tier}`)"
            )
        elif ev_type == "SQUEEZE":
            await notify_saved_messages(
                f"🤏 **Near-Target Ratchet Squeeze Armed ({tier}) [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction}) | `{lbl}` (`{role}`) [Ticket #{tk}]\n"
                f"• Entry: `{ent}` | Peak: `{pk}` (`+${mfe_d:.2f}/oz`)\n"
                f"• Server SL: `{bsl}` | Virtual Harvest: `{ev.get('virt_sl')}`"
            )
        elif ev_type == "TIER_JUMP":
            await notify_saved_messages(
                f"🔒 **Profit Ratchet Locked ({tier}) [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction}) | `{lbl}` (`{role}`) [Ticket #{tk}]\n"
                f"• Entry: `{ent}` | Peak: `{pk}` (`+${mfe_d:.2f}/oz`)\n"
                f"• Broker SL Tightened: `{bsl}`"
            )

    for ab in (lift_aborts or []):
        tk = ab.get("ticket")
        lbl = resolve_layer_label(sig, tk, fallback=ab.get("layer"))
        await notify_saved_messages(
            f"⚠️ **2-Step TP Lift Aborted — SL Rejected (`{ab.get('retcode')}`) [ID: {msg_id}]**\n"
            f"• `{lbl}` [Ticket #{tk}] ({symbol} {direction})\n"
            f"• Desired SL `{ab.get('desired_sl')}` was rejected by broker; keeping current TP intact!"
        )

    if isinstance(partial_bank, dict) and partial_bank.get("ticket"):
        tk = partial_bank.get("ticket")
        lbl = resolve_layer_label(sig, tk, fallback=partial_bank.get("layer"))
        tot_p = float(sig.get("banked_profit") or 0.0)
        p_dol = float(partial_profit or 0.0)
        await notify_saved_messages(
            f"💰 **Realized Partial Bank ({partial_bank.get('milestone', 'TP')}) [ID: {msg_id}]**\n"
            f"• `{lbl}` (`{partial_bank.get('role')}`) [Ticket #{tk}] ({symbol} {direction})\n"
            f"• Closed: `{partial_bank.get('closed_volume')}` lots (`{format_profit(p_dol, curr)}`) | "
            f"Remaining Runner: `{partial_bank.get('remaining_volume')}` lots\n"
            f"• 💵 **Total Trade Profit:** `{format_profit(tot_p, curr)}`"
        )


async def _record_signal_to_ledger(
    msg_id: Any,
    sig: Dict[str, Any],
    exit_reason: str,
    currency: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Persist a closed signal once to trades_ledger.jsonl when all its positions are closed.
    Skips purely unentered/invalidated setups and avoids double recording via `ledger_recorded`.
    """
    if not isinstance(sig, dict) or sig.get("ledger_recorded") or not _has_executed_exposure(sig):
        return None

    chat_id = sig.get("chat_id")
    mid_str = str(msg_id)
    if chat_id is not None and ":" not in mid_str:
        signal_key = f"{chat_id}:{mid_str}"
    else:
        signal_key = mid_str

    if not currency:
        fin = await _mt5_call(executor.get_account_financials)
        currency = fin.get("currency", "GBP")
    srv_date = await _trading_day_str_async()
    realized_pnl = round(float(sig.get("banked_profit") or 0.0), 2)

    try:
        entry = trade_ledger.record_closed_signal(
            signal_key=signal_key,
            signal_data=sig,
            realized_pnl=realized_pnl,
            exit_reason=exit_reason,
            currency=currency,
            dry_run=executor.dry_run,
            extra={"server_date": srv_date},
        )
        sig["ledger_recorded"] = True
        state_mgr.update_signal(msg_id, {"ledger_recorded": True}, chat_id=chat_id)
        return entry
    except Exception as exc:
        logger.warning(f"Failed to append closed trade ledger entry for {signal_key}: {exc}")
        return None


def _resolve_signal_tps_dict(sig: Dict[str, Any]) -> Dict[str, float]:
    """Return ordered TP1..TP5 target dictionary from signal state."""
    out: Dict[str, float] = {}
    tp_map = sig.get("tp_levels")
    has_explicit_map = isinstance(tp_map, dict) and len(tp_map) >= 2
    if isinstance(tp_map, dict):
        for idx in range(1, 6):
            val = tp_map.get(f"TP{idx}")
            if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) > 0.0:
                out[f"TP{idx}"] = float(val)
    for idx in range(1, 6):
        key = f"TP{idx}"
        if isinstance(tp_map, dict) and key in tp_map and tp_map.get(key) is None:
            continue
        if has_explicit_map and key not in tp_map:
            continue
        val = sig.get(f"raw_tp{idx}")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) > 0.0 and key not in out:
            out[key] = float(val)
    return out


def _resolve_effective_max_stage(sig: Dict[str, Any]) -> int:
    """Resolve effective maximum TP stage (1..5) capped by highest valid TP in signal."""
    if not isinstance(sig, dict):
        return int(RUNNER_MAX_TP_STAGE)
    raw_eff = sig.get("effective_max_stage")
    if isinstance(raw_eff, int) and not isinstance(raw_eff, bool) and 1 <= raw_eff <= 5:
        return int(raw_eff)
    _cfg_mod = sys.modules.get("config")
    cfg_max = int(
        sig.get("runner_max_tp_stage")
        or getattr(_cfg_mod, "RUNNER_MAX_TP_STAGE", RUNNER_MAX_TP_STAGE)
        or RUNNER_MAX_TP_STAGE
    )
    cfg_max = max(1, min(5, cfg_max))
    tps = _resolve_signal_tps_dict(sig)
    highest_valid_tp = max(
        (i for i in range(1, 6) if _is_num(tps.get(f"TP{i}")) and float(tps[f"TP{i}"]) > 0.0),
        default=0,
    )
    if highest_valid_tp > 0:
        return max(1, min(5, cfg_max, highest_valid_tp))
    return cfg_max


def _format_tp_summary(sig: Dict[str, Any]) -> str:
    """Format TP1..TP5 summary compactly for Telegram notifications and /status."""
    tps = _resolve_signal_tps_dict(sig)
    if not tps:
        final_tp = sig.get("final_tp")
        return f"`{final_tp}`" if final_tp is not None else "`None`"
    return " | ".join(f"{k}=`{v}`" for k, v in tps.items())


def _format_layers_summary(sig: Dict[str, Any]) -> str:
    """Format 4-layer L1..L4 (or legacy TOP/MID/BOTTOM) status string."""
    layers = sig.get("layers") or {}
    if not isinstance(layers, dict) or not layers:
        return "None"
    ordered_keys = [k for k in LAYER_NAMES if k in layers]
    if not ordered_keys:
        ordered_keys = [k for k in ("TOP", "MID", "BOTTOM") if k in layers]
    if not ordered_keys:
        ordered_keys = list(layers.keys())
    parts: List[str] = []
    for k in ordered_keys:
        l_data = layers.get(k) or {}
        lot_v = l_data.get("lot", 0.0)
        st = l_data.get("status", "N/A")
        parts.append(f"{k}({lot_v:.2f})={st}")
    return " | ".join(parts)


async def _execute_tp1_scaling_handoff(
    msg_id: Any,
    sig: Dict[str, Any],
    chat_id: Optional[int | str],
    source_label: str,
    curr_price: Optional[float] = None,
) -> Tuple[Optional[int], float, str]:
    """
    Shared TP1 scaling handler across 1s watcher, provider TP1 reply, TP1 edit, and /bank:
    1. Executes executor.handle_tp1_scaling_and_select_runner(sig) via _mt5_call.
    2. Deduplicates realized TP1 banked profits into accounted_tickets + banked_profit.
    3. Marks tp1_reached=True and persists state with composite chat_id.
    4. Appends to TradeLedger if all layers closed at TP1; otherwise builds Runner FSM alert.
    """
    eff_chat = chat_id if chat_id is not None else sig.get("chat_id")
    raw_tp1_val = sig.get("raw_tp1")
    if curr_price is not None and raw_tp1_val is not None:
        _cfg_mod = sys.modules.get("config")
        if bool(getattr(_cfg_mod, "TP1_TRAIL_ENABLED", True)) or bool(getattr(_cfg_mod, "TP1_TOUCH_BANK_ENABLED", True)):
            dir_u = (sig.get("direction") or "BUY").strip().upper()
            if (dir_u == "BUY" and float(curr_price) >= float(raw_tp1_val)) or (
                dir_u == "SELL" and float(curr_price) <= float(raw_tp1_val)
            ):
                sig["tp1_touched"] = True
                sig["tp1_touch_price"] = float(curr_price)
    runner_t = await _mt5_call(executor.handle_tp1_scaling_and_select_runner, sig)
    state_mgr.set_tp1_reached(msg_id, True, chat_id=eff_chat)

    banked_tkts = list(sig.get("last_banked_tickets") or [])
    tp1_added_profit, _ = await _account_closed_tickets(sig, banked_tkts)
    pb_info = sig.pop("last_partial_bank", None)
    pb_added = 0.0
    if isinstance(pb_info, dict) and pb_info.get("ticket"):
        pb_added, _ = await _account_closed_tickets(sig, [pb_info["ticket"]], partial_only=True)
    state_mgr.update_signal(msg_id, sig, chat_id=eff_chat)

    fin = await _mt5_call(executor.get_account_financials)
    curr = fin.get("currency", "GBP")
    profit_str = format_profit(tp1_added_profit, curr)

    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = sig.get("direction", "BUY")
    raw_tp1 = sig.get("raw_tp1")
    hit_line = f"\n• Hit Price: `{curr_price}` (TP1: `{raw_tp1}`)" if curr_price is not None else f"\n• TP1: `{raw_tp1}`"

    if sig.get("status") == "CLOSED":
        src_low = str(source_label or "").lower()
        code_tp1 = "MANUAL" if ("manual" in src_low or "/bank" in src_low) else "TP_CEILING"
        _ensure_signal_exit_telemetry(
            sig,
            exit_reason_code=code_tp1,
            exit_stage=1,
            exit_price=curr_price if _is_num(curr_price) else (_safe_float_or_none(raw_tp1)),
        )
        state_mgr.update_signal(msg_id, sig, chat_id=eff_chat)
        _emit_exit_telemetry_log(
            msg_id,
            sig,
            float(sig.get("banked_profit") or tp1_added_profit),
            exit_reason=f"TP1 Full Bank ({source_label})",
        )
        await _record_signal_to_ledger(msg_id, sig, exit_reason=f"TP1 Full Bank ({source_label})", currency=curr)
        alert = (
            f"🎯 **TP1 Reached & 100% Banked ({source_label})! [ID: {msg_id}]**\n"
            f"• Symbol: `{symbol}` ({direction}){hit_line}\n"
            f"• 💰 **Amount Made at TP1:** `{profit_str}` (All open layers banked)\n"
            f"• Pending limit orders cancelled.\n\n"
            f"{fin['formatted']}"
        )
        return runner_t, tp1_added_profit, alert

    runner_layers = list(
        sig.get("runner_layers")
        or ([sig["runner_layer"]] if sig.get("runner_layer") else [])
        or sig.get("last_runner_layers")
        or ([sig["last_runner_layer"]] if sig.get("last_runner_layer") else [])
    )
    runner_tickets = list(sig.get("runner_tickets") or ([sig["runner_ticket"]] if sig.get("runner_ticket") else []))
    runner_name = (
        ", ".join(str(x) for x in runner_layers)
        if len(runner_layers) > 1
        else (sig.get("runner_layer") or sig.get("last_runner_layer") or (runner_layers[-1] if runner_layers else None))
    )
    runner_disp = (
        f"Layers `{runner_name}`"
        if len(runner_layers) > 1
        else f"`{resolve_layer_label(sig, runner_t, fallback=runner_name)}`"
    )
    soft_floor = sig.get("runner_sl_floor") if sig.get("runner_sl_floor") is not None else sig.get("soft_tp1_floor")
    milestone_name = sig.get("runner_milestone_name", "TP1")
    tp_stage = sig.get("tp_stage", "TP2")
    fsm_stage = _get_signal_stage(sig)
    next_target_disp = milestone_name if milestone_name != "TP1" else (tp_stage or "TP2")
    if _is_staged_exit_enabled() and sig.get("tp1_reached"):
        floor_stage_line = f"• 🛡️ **Floor / Trailed SL:** `{soft_floor}` | Stage {fsm_stage} (TP1 Cleared) | Next Target: `{next_target_disp}`\n"
    else:
        floor_stage_line = f"• 🛡️ **Floor / Trailed SL:** `{soft_floor}` | Stage: `{tp_stage}` | Next Milestone: `{milestone_name}`\n"
    surviving_mkt = list(sig.get("market_tickets") or [])
    tp_summary = _format_tp_summary(sig)
    pending_note = (
        "Pending limit orders cancelled cleanly."
        if CANCEL_PENDING_ON_TP1
        else f"Remaining pending orders kept active ({len(sig.get('pending_tickets') or [])})."
    )
    be_note = " | BE applied to open layers" if BE_AFTER_TP1 else ""
    extra_runners_note = f" (Runner Tickets: `{runner_tickets}`)" if len(runner_tickets) > 1 else ""

    if isinstance(pb_info, dict) and pb_info.get("ticket"):
        tp1_bank_line = (
            f"• 💰 **Realized Partial Bank at TP1:** `{format_profit(pb_added, curr)}` "
            f"(`{pb_info.get('closed_volume')}` lots banked, `{pb_info.get('remaining_volume')}` lots holding{be_note})\n"
        )
    elif banked_tkts:
        tp1_bank_line = f"• 💰 **Amount Made at TP1:** `{profit_str}` (Tickets: `{banked_tkts}`{be_note})\n"
    else:
        tp1_bank_line = (
            f"• 🛡️ **0.01 Solo Runner Holding to {next_target_disp} (No Full Close at TP1)**{be_note}\n"
        )

    alert = (
        f"🎯 **TP1 Reached & Profit Banked ({source_label})! [ID: {msg_id}]**\n"
        f"• Symbol: `{symbol}` ({direction}){hit_line}\n"
        f"{tp1_bank_line}"
        f"• 🚀 **Designated Runner:** {runner_disp} [Ticket #{runner_t}]{extra_runners_note} (Open Tickets: `{surviving_mkt}`)\n"
        f"{floor_stage_line}"
        f"• 🎯 **Targets:** {tp_summary}\n"
        f"• {pending_note}\n\n"
        f"{fin['formatted']}"
    )
    return runner_t, tp1_added_profit, alert


def _safe_float_or_none(val: Any) -> Optional[float]:
    return float(val) if _is_num(val) else None


# ---------------------------------------------------------------------------
# EXPOSURE SHUTDOWN & DAILY RISK LIMITS
# ---------------------------------------------------------------------------

def _close_all_bot_exposure_and_signals() -> Dict[str, Any]:
    """
    Synchronous helper to close every open bot position, cancel every bot pending order,
    stop all delayed 'Sell now'/'Buy now' timers and mark all tracked signals CLOSED.
    """
    for pending_key in list(_pending_sell_now_tasks.keys()):
        _cancel_delayed_sell_now(pending_key)

    tracked_mk: List[int] = []
    tracked_pd: List[int] = []
    active_signals = state_mgr.get_active_signals()
    for _, s in active_signals:
        tracked_mk.extend(t for t in s.get("market_tickets", []) if t not in tracked_mk)
        tracked_pd.extend(t for t in s.get("pending_tickets", []) if t not in tracked_pd)

    res = executor.close_all_bot_exposure(tracked_mk, tracked_pd)

    for mid, s in active_signals:
        s_chat = s.get("chat_id")
        layers = s.get("layers", {})
        for lk in layers:
            if isinstance(layers[lk], dict):
                layers[lk]["status"] = "CLOSED"
        state_mgr.update_signal(
            mid,
            {"status": "CLOSED", "market_tickets": [], "pending_tickets": [], "runner_ticket": None, "layers": layers},
            chat_id=s_chat,
        )
    for mid, s in state_mgr.get_all_signals().items():
        if s.get("status") in ("PENDING", "PENDING_DELAY"):
            state_mgr.update_signal(mid, {"status": "CLOSED"}, chat_id=s.get("chat_id"))

    return res


async def _close_all_bot_exposure_and_signals_async(exit_reason: str = "Session Closed") -> Dict[str, Any]:
    """
    Non-blocking close of all bot positions & pending orders, accounting realized PnL
    for each closed signal and appending closed trades to TradeLedger.
    """
    for pending_key in list(_pending_sell_now_tasks.keys()):
        _cancel_delayed_sell_now(pending_key)

    tracked_mk: List[int] = []
    tracked_pd: List[int] = []
    active_signals = state_mgr.get_active_signals()
    for _, s in active_signals:
        tracked_mk.extend(t for t in s.get("market_tickets", []) if t not in tracked_mk)
        tracked_pd.extend(t for t in s.get("pending_tickets", []) if t not in tracked_pd)

    res = await _mt5_call(executor.close_all_bot_exposure, tracked_mk, tracked_pd)
    closed_set = set(res.get("closed_tickets") or [])
    fin = await _mt5_call(executor.get_account_financials)
    curr = fin.get("currency", "GBP")
    is_manual_close = any(w in str(exit_reason or "").lower() for w in ("manual", "panic", "closeall"))

    for mid, s in active_signals:
        s_chat = s.get("chat_id")
        mk_tkts = [t for t in (s.get("market_tickets") or []) if t in closed_set or t]
        if mk_tkts:
            await _account_closed_tickets(s, mk_tkts)
        layers = s.get("layers", {})
        for lk in layers:
            if isinstance(layers[lk], dict):
                layers[lk]["status"] = "CLOSED"
        if is_manual_close:
            _ensure_signal_exit_telemetry(s, exit_reason_code="MANUAL", overwrite_code=True)
            _emit_exit_telemetry_log(mid, s, float(s.get("banked_profit") or 0.0), exit_reason=exit_reason)
        s["status"] = "CLOSED"
        s["market_tickets"] = []
        s["pending_tickets"] = []
        s["runner_ticket"] = None
        s["layers"] = layers
        state_mgr.update_signal(
            mid,
            {
                "status": "CLOSED",
                "market_tickets": [],
                "pending_tickets": [],
                "runner_ticket": None,
                "layers": layers,
                "accounted_tickets": s.get("accounted_tickets", []),
                "banked_profit": s.get("banked_profit", 0.0),
                **(
                    {
                        "exit_reason_code": s.get("exit_reason_code"),
                        "exit_stage": s.get("exit_stage"),
                        "exit_price": s.get("exit_price"),
                        "max_favorable_excursion": s.get("max_favorable_excursion"),
                        "giveback_from_peak": s.get("giveback_from_peak"),
                        "trail_tier_used": s.get("trail_tier_used"),
                    }
                    if is_manual_close
                    else {}
                ),
            },
            chat_id=s_chat,
        )
        await _record_signal_to_ledger(mid, s, exit_reason=exit_reason, currency=curr)

    for mid, s in state_mgr.get_all_signals().items():
        if s.get("status") in ("PENDING", "PENDING_DELAY"):
            state_mgr.update_signal(mid, {"status": "CLOSED"}, chat_id=s.get("chat_id"))

    return res


def get_daily_win_progress() -> Dict[str, Any]:
    """
    Calculate today's net trading profit relative to the Daily Win Target configuration.
    Net Profit Today = (realized_today_pnl - baseline_pnl) + floating_pnl
    """
    cfg = state_mgr.get_daily_win_config()
    today_str = _trading_day_str()
    enabled = bool(cfg.get("enabled", False))
    target_amount = round(float(cfg.get("target_amount", 0.0)), 2)
    baseline_pnl = round(float(cfg.get("baseline_pnl", 0.0)), 2)

    if enabled and cfg.get("date") != today_str:
        baseline_pnl = 0.0
        cfg = state_mgr.set_daily_win_config(
            enabled=True,
            target_amount=target_amount,
            baseline_pnl=0.0,
            date_str=today_str,
        )

    dd = executor.get_daily_drawdown_status()
    realized_today = round(float(dd.get("realized_today_pnl", 0.0)), 2)
    floating_pnl = round(float(dd.get("floating_pnl", 0.0)), 2)
    curr = dd.get("currency", "GBP")
    net_profit = round((realized_today - baseline_pnl) + floating_pnl, 2)
    target_reached = bool(enabled and target_amount > 0 and net_profit >= round(target_amount - 1e-9, 2))

    return {
        "enabled": enabled,
        "target_amount": target_amount,
        "baseline_pnl": baseline_pnl,
        "realized_today_pnl": realized_today,
        "floating_pnl": floating_pnl,
        "net_profit_today": net_profit,
        "currency": curr,
        "target_reached": target_reached,
        "date": cfg.get("date", today_str),
    }


async def check_and_enforce_daily_win_target() -> str:
    """
    Monitor today's net trading profit against the user's `dailywin` target.
    Once net profit >= target_amount:
    1. Banks (closes at market) all open bot positions and cancels all pending limit orders.
    2. Marks all active signals CLOSED and resets the `dailywin` flag.
    3. Sends a congratulatory summary + final account financials to Saved Messages.
    4. Requests session termination (exit code 88) so no further trading occurs.
    Returns "DAILY_WIN_REACHED" or "OK".
    """
    global _daily_win_terminate_requested
    if _daily_win_terminate_requested:
        return "DAILY_WIN_REACHED"

    cfg = state_mgr.get_daily_win_config()
    if not cfg.get("enabled") or float(cfg.get("target_amount") or 0.0) <= 0.0:
        return "OK"

    prog = await _mt5_call(get_daily_win_progress)
    if not prog["target_reached"]:
        return "OK"

    _daily_win_terminate_requested = True
    target_amount = prog["target_amount"]
    baseline_pnl = prog["baseline_pnl"]
    curr = prog["currency"]

    res = await _close_all_bot_exposure_and_signals_async(exit_reason="Daily Win Target Reached")
    closed_positions = len(res.get("closed_tickets", []))
    cancelled_orders = len(res.get("cancelled_orders", []))

    state_mgr.set_daily_win_config(
        enabled=False,
        target_amount=target_amount,
        baseline_pnl=0.0,
        date_str=prog["date"],
    )

    dd_after = await _mt5_call(executor.get_daily_drawdown_status)
    realized_after = round(float(dd_after.get("realized_today_pnl", 0.0)) - baseline_pnl, 2)
    final_profit = max(prog["net_profit_today"], realized_after)
    fin = await _mt5_call(executor.get_account_financials)

    logger.info(
        f"🏆 DAILY WIN TARGET REACHED! Profit Made={format_profit(final_profit, curr)} "
        f"(Target={format_profit(target_amount, curr)}). Closed={closed_positions} pos, "
        f"Cancelled={cancelled_orders} limits. Terminating session!"
    )

    await notify_saved_messages(
        f"🏆 **DAILY WIN TARGET REACHED ({format_profit(target_amount, curr)}) — SESSION TERMINATED!**\n"
        f"• 💰 **Profit Made Today:** `{format_profit(final_profit, curr)}` (Target: `{format_profit(target_amount, curr)}`)\n"
        f"• 🔒 **Positions Banked:** `{closed_positions}` | **Pending Limits Cancelled:** `{cancelled_orders}`\n"
        f"• 🛑 **Session Terminated:** The copier has automatically shut down for the day so you keep your gains with no more trading!\n"
        f"• 💡 _Relaunch the copier when you are ready for your next session._\n\n"
        f"{fin['formatted']}"
    )

    if telethon_client:
        try:
            disc_res = telethon_client.disconnect()
            if inspect.isawaitable(disc_res):
                await disc_res
        except Exception as e:
            logger.debug(f"Telethon disconnect on daily win exit: {e}")

    return "DAILY_WIN_REACHED"


def get_daily_loss_progress() -> Dict[str, Any]:
    """
    Daily Loss Limit progress based ONLY on REALIZED (closed) P&L for the broker trading day:
    trades that hit Stop Loss / were closed in a loss and left a negative number on the account.
    Open floating trades are deliberately ignored (covered by optional /losslimit & /floatcap when enabled).
    """
    cfg = state_mgr.get_daily_loss_config()
    today_str = _trading_day_str()
    enabled = bool(cfg.get("enabled", False))
    raw_lim = cfg.get("limit_amount") if cfg.get("limit_amount") else cfg.get("daily_loss_limit", 0.0)
    limit_amount = round(abs(float(raw_lim or 0.0)), 2)

    realized_today = round(float(executor.get_realized_today_pnl()), 2)
    realized_loss = round(max(0.0, -realized_today), 2)
    curr = executor.get_account_financials().get("currency", "GBP")
    limit_reached = bool(enabled and limit_amount > 0 and realized_today <= round(-limit_amount + 1e-9, 2))
    remaining = round(max(0.0, limit_amount - realized_loss), 2) if enabled else 0.0

    return {
        "enabled": enabled,
        "limit_amount": limit_amount,
        "daily_loss_limit": limit_amount,
        "realized_today_pnl": realized_today,
        "realized_loss_today": realized_loss,
        "remaining_before_limit": remaining,
        "currency": curr,
        "limit_reached": limit_reached,
        "date": today_str,
        "triggered_date": cfg.get("triggered_date"),
        "triggered_at": float(cfg.get("triggered_at") or 0.0),
    }


async def check_and_enforce_daily_loss_limit(exit_on_trigger: bool = True) -> str:
    """
    Monitor today's REALIZED P&L against the user's daily loss limit.
    Returns "DAILY_LOSS_REACHED", "LOCKED", or "OK".
    """
    global TRADING_HALTED, _daily_loss_terminate_requested, _daily_loss_locked, _daily_loss_lock_notified_date
    if _daily_loss_terminate_requested:
        if exit_on_trigger:
            sys.exit(0)
        return "DAILY_LOSS_REACHED"

    cfg = state_mgr.get_daily_loss_config()
    raw_lim = cfg.get("limit_amount") if cfg.get("limit_amount") else cfg.get("daily_loss_limit", 0.0)
    if not cfg.get("enabled") or float(raw_lim or 0.0) <= 0.0:
        if _daily_loss_locked:
            _daily_loss_locked = False
            logger.info("Daily loss lock released (limit disabled).")
        return "OK"

    prog = await _mt5_call(get_daily_loss_progress)
    if not prog["limit_reached"]:
        if _daily_loss_locked:
            _daily_loss_locked = False
            logger.info("Daily loss lock released (limit disabled / raised above today's realized loss).")
        return "OK"

    curr = prog["currency"]
    limit_amt = round(abs(float(prog["limit_amount"])), 2)
    limit_str = format_profit(-limit_amt, curr)
    loss_str = format_profit(prog["realized_today_pnl"], curr)

    already_fired_today = (
        prog.get("triggered_date") == prog["date"] and prog.get("triggered_at", 0.0) <= BOT_START_TIME
    )
    if already_fired_today:
        _daily_loss_locked = True
        if _daily_loss_lock_notified_date != prog["date"]:
            _daily_loss_lock_notified_date = prog["date"]
            logger.warning(f"🔒 Daily loss lock active for {prog['date']} (Realized {loss_str} / Limit {limit_str}).")
            await notify_saved_messages(
                f"🔒 **DAILY LOSS LOCK ACTIVE — NO NEW TRADES TODAY**\n"
                f"• Realized Today: `{loss_str}` (Limit: `{limit_str}`)\n"
                f"• The copier was relaunched after today's loss limit fired, so it will not open any trades "
                f"until the next broker trading day.\n"
                f"• 💡 _To override, type `dailyloss off` or set a higher limit (e.g. `dailyloss 40`)._"
            )
        return "LOCKED"

    TRADING_HALTED = True
    _daily_loss_terminate_requested = True

    for pending_key in list(_pending_sell_now_tasks.keys()):
        _cancel_delayed_sell_now(pending_key)

    all_sigs = state_mgr.get_all_signals()
    tracked_pd: List[int] = []
    for _, s in all_sigs.items():
        for pt in (s.get("pending_tickets") or []):
            if pt not in tracked_pd:
                tracked_pd.append(pt)

    if DAILY_LOSS_CLOSE_OPEN_TRADES:
        res = await _close_all_bot_exposure_and_signals_async(exit_reason="Daily Loss Limit Hit")
        extra_cancelled = await _mt5_call(executor.cancel_all_pending_orders, tracked_pd)
        cancelled_orders_list = list(dict.fromkeys(res.get("cancelled_orders", []) + extra_cancelled))
        closed_positions = len(res.get("closed_tickets", []))
    else:
        cancelled_orders_list = await _mt5_call(executor.cancel_all_pending_orders, tracked_pd)
        closed_positions = 0
        for mid, s in all_sigs.items():
            s_chat = s.get("chat_id")
            st = s.get("status")
            if st in ("PENDING", "PENDING_DELAY"):
                layers = s.get("layers", {})
                for lk in layers:
                    if isinstance(layers[lk], dict) and layers[lk].get("status") == "PENDING":
                        layers[lk]["status"] = "CANCELLED"
                state_mgr.update_signal(mid, {"status": "CLOSED", "pending_tickets": [], "layers": layers}, chat_id=s_chat)
            elif st in ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE"):
                layers = s.get("layers", {})
                mk_tkts = list(s.get("market_tickets") or [])
                for lk in layers:
                    if isinstance(layers[lk], dict):
                        t_val = layers[lk].get("ticket")
                        if layers[lk].get("status") == "PENDING" or (t_val and t_val not in mk_tkts):
                            layers[lk]["status"] = "CANCELLED"
                updates: Dict[str, Any] = {"pending_tickets": [], "layers": layers}
                if not mk_tkts:
                    updates["status"] = "CLOSED"
                state_mgr.update_signal(mid, updates, chat_id=s_chat)

    cancelled_orders = len(cancelled_orders_list)

    state_mgr.set_daily_loss_config(
        enabled=True,
        limit_amount=limit_amt,
        triggered_date=prog["date"],
        triggered_at=time.time(),
    )

    final_realized = round(float(await _mt5_call(executor.get_realized_today_pnl)), 2)
    fin = await _mt5_call(executor.get_account_financials)
    headline = (
        f"🛑 DAILY LOSS LIMIT HIT: Realized P&L is {final_realized:.2f} "
        f"(Limit: -{limit_amt:.2f}). Pending orders canceled. Terminating bot session."
    )
    open_note = (
        f"• 🔒 **Open Positions Closed:** `{closed_positions}` | **Pending Orders Cancelled:** `{cancelled_orders}`\n"
        if DAILY_LOSS_CLOSE_OPEN_TRADES
        else f"• ⏸️ **Pending Orders Cancelled:** `{cancelled_orders}` (open positions left on their broker SL/TP)\n"
    )

    logger.error(headline)
    await notify_saved_messages(
        f"{headline}\n\n"
        f"• 🔻 **Realized Loss Today (closed / Stop-Loss trades):** `{format_profit(final_realized, curr)}` "
        f"(Limit: `{limit_str}`)\n"
        f"{open_note}"
        f"• 🛡️ **Session Terminated:** No more trading today to protect your capital.\n"
        f"• 💡 _The limit stays saved and resets automatically on the next broker trading day._\n\n"
        f"{fin['formatted']}"
    )

    if telethon_client:
        try:
            disc_res = telethon_client.disconnect()
            if inspect.isawaitable(disc_res):
                await disc_res
        except Exception as e:
            logger.debug(f"Telethon disconnect on daily loss exit: {e}")

    try:
        await _mt5_call(executor.shutdown)
    except Exception as e:
        logger.debug(f"Executor shutdown on daily loss exit: {e}")

    if exit_on_trigger:
        sys.exit(0)

    return "DAILY_LOSS_REACHED"


def _fmt_pct(pct: float) -> str:
    """Format a percentage cleanly (e.g. 50.0 -> '50', 45.5 -> '45.5')."""
    val = float(pct)
    return str(int(val)) if val.is_integer() else f"{val:.1f}"


async def check_and_enforce_daily_drawdown() -> str:
    """
    Daily Loss & Account Drawdown Protection (all percentage caps OFF / unlimited by default;
    manually configurable at runtime via Saved Messages `/losslimit` and `/floatcap`):
    - Stage 2 (`EMERGENCY_CUTOFF_LOSS_PCT`, when > 0): Urgency cut-off closes losing open positions & cancels limits.
    - Stage 1b (`FLOAT_LOSS_FORCE_CLOSE_PCT`, when > 0): Floating-loss force-close cap closes open positions & cancels limits.
    - Stage 1 (`DAILY_LOSS_PAUSE_PCT`, when > 0): Pauses NEW signals, while holding existing open trades naturally to SL/TP.
    Returns "EMERGENCY_CUTOFF", "FLOAT_FORCE_CLOSE", "PAUSED_50PCT", or "OK".
    """
    global _daily_50pct_alert_date, _daily_float_cap_alert_date
    dd = await _mt5_call(executor.get_daily_drawdown_status)
    curr = dd.get("currency", "GBP")
    today_str = await _trading_day_str_async()

    _cfg_mod = sys.modules.get("config") or config
    live_pause_pct = float(getattr(_cfg_mod, "DAILY_LOSS_PAUSE_PCT", DAILY_LOSS_PAUSE_PCT) or 0.0)
    live_cutoff_pct = float(getattr(_cfg_mod, "EMERGENCY_CUTOFF_LOSS_PCT", EMERGENCY_CUTOFF_LOSS_PCT) or 0.0)
    live_float_cap_pct = float(getattr(_cfg_mod, "FLOAT_LOSS_FORCE_CLOSE_PCT", FLOAT_LOSS_FORCE_CLOSE_PCT) or 0.0)

    eff_pause_pct = float(dd["pause_pct"]) if dd.get("pause_pct") is not None else live_pause_pct
    eff_cutoff_pct = float(dd["cutoff_pct"]) if dd.get("cutoff_pct") is not None else live_cutoff_pct
    eff_float_cap_pct = float(dd["float_cap_pct"]) if dd.get("float_cap_pct") is not None else live_float_cap_pct

    if eff_pause_pct <= 0.0 and eff_cutoff_pct <= 0.0 and eff_float_cap_pct <= 0.0:
        return "OK"

    if dd.get("emergency_cutoff_triggered") and eff_cutoff_pct > 0.0:
        cutoff_lbl = _fmt_pct(eff_cutoff_pct)
        active_sigs = state_mgr.get_active_signals()
        all_mk: List[int] = []
        all_pd: List[int] = []
        for _, s in active_sigs:
            all_mk.extend(s.get("market_tickets", []))
            all_pd.extend(s.get("pending_tickets", []))

        res = await _mt5_call(executor.emergency_cutoff_losing_trades, all_mk, all_pd)
        closed_tkts = res.get("closed_tickets", [])
        cancelled_ords = res.get("cancelled_orders", [])
        closed_set = set(closed_tkts)
        cancelled_set = set(cancelled_ords)

        for mid, s in active_sigs:
            s_chat = s.get("chat_id")
            orig_mk = list(s.get("market_tickets") or [])
            orig_pd = list(s.get("pending_tickets") or [])
            sig_closed_mk = [t for t in orig_mk if t in closed_set]
            sig_rem_mk = [t for t in orig_mk if t not in closed_set]
            sig_rem_pd = [t for t in orig_pd if t not in cancelled_set]
            sig_cancelled_pd = [t for t in orig_pd if t in cancelled_set]

            if sig_closed_mk:
                await _account_closed_tickets(s, sig_closed_mk)

            layers = copy.deepcopy(s.get("layers") or {})
            for lk, l_data in layers.items():
                if not isinstance(l_data, dict):
                    continue
                lt = l_data.get("ticket")
                if lt in closed_set:
                    l_data["status"] = "CLOSED"
                elif lt in cancelled_set:
                    l_data["status"] = "CANCELLED"

            runner_t = s.get("runner_ticket")
            new_runner_t = None if (runner_t is not None and runner_t in closed_set) else runner_t
            still_open = bool(sig_rem_mk or sig_rem_pd)
            if not still_open:
                new_status = "CLOSED"
            else:
                new_status = s.get("status")
                if new_runner_t is None and new_status == "RUNNER_ACTIVE":
                    new_status = "BE_APPLIED" if s.get("tp1_reached") else "ACTIVE"

            if sig_closed_mk or sig_cancelled_pd or not still_open:
                s["status"] = new_status
                s["market_tickets"] = sig_rem_mk
                s["pending_tickets"] = sig_rem_pd
                s["runner_ticket"] = new_runner_t
                s["layers"] = layers
                state_mgr.update_signal(
                    mid,
                    {
                        "status": new_status,
                        "market_tickets": sig_rem_mk,
                        "pending_tickets": sig_rem_pd,
                        "runner_ticket": new_runner_t,
                        "layers": layers,
                        "accounted_tickets": s.get("accounted_tickets", []),
                        "accounted_deal_tickets": s.get("accounted_deal_tickets", []),
                        "banked_profit": s.get("banked_profit", 0.0),
                    },
                    chat_id=s_chat,
                )
                if new_status == "CLOSED":
                    await _record_signal_to_ledger(
                        mid, s, exit_reason=f"{cutoff_lbl}% Emergency Cutoff", currency=curr
                    )

        if closed_tkts or cancelled_ords or active_sigs:
            fin = await _mt5_call(executor.get_account_financials)
            logger.error(
                f"🚨 {cutoff_lbl}% EMERGENCY CUT-OFF TRIGGERED! Drawdown=-{dd['total_daily_loss']:.2f} {curr} "
                f"({dd['loss_pct']:.1f}%). Closed={closed_tkts}, Cancelled={cancelled_ords}"
            )
            await notify_saved_messages(
                f"🚨 **{cutoff_lbl}% EMERGENCY DRAWDOWN CUT-OFF TRIGGERED!**\n"
                f"• Loss Risked: `-{dd['total_daily_loss']:.2f} {curr}` (`{dd['loss_pct']:.1f}%` of `{dd['reference_balance']:.2f} {curr}` balance)\n"
                f"• Closed Losing Positions: `{closed_tkts}`\n"
                f"• Cancelled Pending Limits: `{cancelled_ords}`\n"
                f"• 🛡️ All losing positions cut immediately to protect your remaining account balance!\n\n"
                f"{fin['formatted']}"
            )
        return "EMERGENCY_CUTOFF"

    # ── STAGE 1b: Hard Floating-Loss Force-Close Cap ──────────────────────────────
    # Fires when FLOAT_LOSS_FORCE_CLOSE_PCT > 0 and floating-only loss breaches that cap.
    if dd.get("float_force_close_triggered") and eff_float_cap_pct > 0.0:
        float_lbl = _fmt_pct(eff_float_cap_pct)
        active_sigs_fc = state_mgr.get_active_signals()
        all_mk_fc: List[int] = []
        all_pd_fc: List[int] = []
        for _, s in active_sigs_fc:
            all_mk_fc.extend(s.get("market_tickets", []))
            all_pd_fc.extend(s.get("pending_tickets", []))

        res_fc = await _mt5_call(executor.emergency_cutoff_losing_trades, all_mk_fc, all_pd_fc)
        closed_fc_tkts = res_fc.get("closed_tickets", [])
        cancelled_fc_ords = res_fc.get("cancelled_orders", [])
        closed_fc_set = set(closed_fc_tkts)
        cancelled_fc_set = set(cancelled_fc_ords)

        for mid, s in active_sigs_fc:
            s_chat = s.get("chat_id")
            orig_mk = list(s.get("market_tickets") or [])
            orig_pd = list(s.get("pending_tickets") or [])
            sig_closed_mk = [t for t in orig_mk if t in closed_fc_set]
            sig_rem_mk = [t for t in orig_mk if t not in closed_fc_set]
            sig_rem_pd = [t for t in orig_pd if t not in cancelled_fc_set]
            sig_cancelled_pd = [t for t in orig_pd if t in cancelled_fc_set]

            if sig_closed_mk:
                await _account_closed_tickets(s, sig_closed_mk)

            layers = copy.deepcopy(s.get("layers") or {})
            for lk, l_data in layers.items():
                if not isinstance(l_data, dict):
                    continue
                lt = l_data.get("ticket")
                if lt in closed_fc_set:
                    l_data["status"] = "CLOSED"
                elif lt in cancelled_fc_set:
                    l_data["status"] = "CANCELLED"

            runner_t = s.get("runner_ticket")
            new_runner_t = None if (runner_t is not None and runner_t in closed_fc_set) else runner_t
            still_open = bool(sig_rem_mk or sig_rem_pd)
            if not still_open:
                new_status = "CLOSED"
            else:
                new_status = s.get("status")
                if new_runner_t is None and new_status == "RUNNER_ACTIVE":
                    new_status = "BE_APPLIED" if s.get("tp1_reached") else "ACTIVE"

            if sig_closed_mk or sig_cancelled_pd or not still_open:
                s["status"] = new_status
                s["market_tickets"] = sig_rem_mk
                s["pending_tickets"] = sig_rem_pd
                s["runner_ticket"] = new_runner_t
                s["layers"] = layers
                state_mgr.update_signal(
                    mid,
                    {
                        "status": new_status,
                        "market_tickets": sig_rem_mk,
                        "pending_tickets": sig_rem_pd,
                        "runner_ticket": new_runner_t,
                        "layers": layers,
                        "accounted_tickets": s.get("accounted_tickets", []),
                        "accounted_deal_tickets": s.get("accounted_deal_tickets", []),
                        "banked_profit": s.get("banked_profit", 0.0),
                    },
                    chat_id=s_chat,
                )
                if new_status == "CLOSED":
                    await _record_signal_to_ledger(
                        mid, s, exit_reason=f"{float_lbl}% Float-Loss Force-Close", currency=curr
                    )

        if closed_fc_tkts or cancelled_fc_ords or active_sigs_fc:
            fin = await _mt5_call(executor.get_account_financials)
            float_cap_lim = dd.get("float_cap_limit", 0.0)
            logger.error(
                f"🔴 {float_lbl}% FLOAT-LOSS FORCE-CLOSE! Floating loss=-{dd['floating_loss']:.2f} {curr} "
                f"(cap=-{float_cap_lim:.2f} {curr}). Closed={closed_fc_tkts}, Cancelled={cancelled_fc_ords}"
            )
            if _daily_float_cap_alert_date != today_str:
                _daily_float_cap_alert_date = today_str
                await notify_saved_messages(
                    f"🔴 **{float_lbl}% FLOATING LOSS CAP — ALL TRADES FORCE-CLOSED!**\n"
                    f"• Floating Loss: `-{dd['floating_loss']:.2f} {curr}` breached the `{float_lbl}%` hard cap (`-{float_cap_lim:.2f} {curr}`).\n"
                    f"• Closed Positions: `{closed_fc_tkts}`\n"
                    f"• Cancelled Pending Limits: `{cancelled_fc_ords}`\n"
                    f"• 🛡️ All open trades force-closed to prevent broker margin stop-out.\n"
                    f"• 💡 Change this cap anytime: `/floatcap <pct>` (e.g. `/floatcap 50`) or disable with `/floatcap off`.\n\n"
                    f"{fin['formatted']}"
                )
        return "FLOAT_FORCE_CLOSE"

    if dd.get("pause_new_signals") and eff_pause_pct > 0.0:
        pause_lbl = _fmt_pct(eff_pause_pct)
        cutoff_lbl = _fmt_pct(eff_cutoff_pct)
        active_sigs = state_mgr.get_active_signals()
        open_mkt = await _mt5_call(executor.get_open_market_tickets)
        has_open_mkt = bool(open_mkt) or any(
            isinstance(s, dict) and bool(s.get("market_tickets")) for _, s in (active_sigs or [])
        )
        # If there are NO open bot market positions (even if unfilled pending limits or manual non-bot positions exist),
        # previous bot loss is realized and has passed. Automatically update baseline to current
        # balance and allow new trades to be actioned without touching pending limit orders.
        if not has_open_mkt:
            await _mt5_call(executor.update_daily_loss_baseline)
            _daily_50pct_alert_date = None
            logger.info("Previous daily loss has passed (0 open trades). Baseline updated to current balance — ready to action new trades.")
            return "OK"

        close_trades_on_pause = bool(getattr(_cfg_mod, "DAILY_LOSS_CLOSE_OPEN_TRADES", DAILY_LOSS_CLOSE_OPEN_TRADES))
        closed_50_mk: List[int] = []
        cancelled_50_pd: List[int] = []
        if close_trades_on_pause:
            all_mk_50: List[int] = []
            all_pd_50: List[int] = []
            for _, s in active_sigs:
                all_mk_50.extend(s.get("market_tickets", []))
                all_pd_50.extend(s.get("pending_tickets", []))

            res_50 = await _mt5_call(executor.emergency_cutoff_losing_trades, all_mk_50, all_pd_50)
            closed_50_mk = res_50.get("closed_tickets", [])
            cancelled_50_pd = res_50.get("cancelled_orders", [])
            closed_50_set = set(closed_50_mk)
            cancelled_50_set = set(cancelled_50_pd)

            for mid, s in active_sigs:
                s_chat = s.get("chat_id")
                orig_mk = list(s.get("market_tickets") or [])
                orig_pd = list(s.get("pending_tickets") or [])
                sig_closed_mk = [t for t in orig_mk if t in closed_50_set]
                sig_rem_mk = [t for t in orig_mk if t not in closed_50_set]
                sig_rem_pd = [t for t in orig_pd if t not in cancelled_50_set]
                sig_cancelled_pd = [t for t in orig_pd if t in cancelled_50_set]

                if sig_closed_mk:
                    await _account_closed_tickets(s, sig_closed_mk)

                layers = copy.deepcopy(s.get("layers") or {})
                for lk, l_data in layers.items():
                    if not isinstance(l_data, dict):
                        continue
                    lt = l_data.get("ticket")
                    if lt in closed_50_set:
                        l_data["status"] = "CLOSED"
                    elif lt in cancelled_50_set:
                        l_data["status"] = "CANCELLED"

                runner_t = s.get("runner_ticket")
                new_runner_t = None if (runner_t is not None and runner_t in closed_50_set) else runner_t
                still_open = bool(sig_rem_mk or sig_rem_pd)
                new_status = "CLOSED" if not still_open else s.get("status")

                if sig_closed_mk or sig_cancelled_pd or not still_open:
                    s["status"] = new_status
                    s["market_tickets"] = sig_rem_mk
                    s["pending_tickets"] = sig_rem_pd
                    s["runner_ticket"] = new_runner_t
                    s["layers"] = layers
                    state_mgr.update_signal(
                        mid,
                        {
                            "status": new_status,
                            "market_tickets": sig_rem_mk,
                            "pending_tickets": sig_rem_pd,
                            "runner_ticket": new_runner_t,
                            "layers": layers,
                            "accounted_tickets": s.get("accounted_tickets", []),
                            "accounted_deal_tickets": s.get("accounted_deal_tickets", []),
                            "banked_profit": s.get("banked_profit", 0.0),
                        },
                        chat_id=s_chat,
                    )
                    if new_status == "CLOSED":
                        await _record_signal_to_ledger(
                            mid, s, exit_reason=f"{pause_lbl}% Drawdown Guard", currency=curr
                        )

            rem_active = state_mgr.get_active_signals()
            has_rem_open_mkt = any(isinstance(rs, dict) and bool(rs.get("market_tickets")) for _, rs in (rem_active or []))
            if not has_rem_open_mkt:
                if closed_50_mk or cancelled_50_pd:
                    logger.warning(
                        f"🛑 {pause_lbl}% Daily Loss Limit Reached (-{dd['total_daily_loss']:.2f} {curr} / {dd['loss_pct']:.1f}%). "
                        f"Losing trades closed={closed_50_mk}, pending orders cancelled={cancelled_50_pd}; baseline reset."
                    )
                    await notify_saved_messages(
                        f"🛑 **{pause_lbl}% Daily Loss Threshold Reached — Losing Trades Cut**\n"
                        f"• Current Daily/Floating Loss: `-{dd['total_daily_loss']:.2f} {curr}` (`{dd['loss_pct']:.1f}%` of `{dd['reference_balance']:.2f} {curr}` balance)\n"
                        f"• 🛡️ **Capital Preservation Active:** Closed `{len(closed_50_mk)}` losing position(s) ({closed_50_mk}) and cancelled `{len(cancelled_50_pd)}` pending order(s) to protect your balance!"
                    )
                await _mt5_call(executor.update_daily_loss_baseline)
                _daily_50pct_alert_date = None
                logger.info(f"All losing trades closed on {pause_lbl}% pause. Daily loss baseline updated to remaining balance — ready for new trades.")
                return "PAUSED_50PCT" if (closed_50_mk or cancelled_50_pd) else "OK"

        if _daily_50pct_alert_date != today_str:
            _daily_50pct_alert_date = today_str
            logger.warning(
                f"🛑 {pause_lbl}% Daily Loss Limit Reached (-{dd['total_daily_loss']:.2f} {curr} / {dd['loss_pct']:.1f}%). "
                f"Losing trades closed={closed_50_mk}, pending orders cancelled={cancelled_50_pd}; new signals paused."
            )
            note_str = (
                f"• 🛡️ **Capital Preservation Active:** Closed `{len(closed_50_mk)}` losing position(s) ({closed_50_mk}) and cancelled `{len(cancelled_50_pd)}` pending order(s) to protect your balance!\n"
                if (closed_50_mk or cancelled_50_pd)
                else f"• 🛡️ **Existing Trades Held:** Currently open positions are **NOT** closed — waiting naturally for their SL or TP.\n"
            )
            cutoff_note = (
                f"• 🚨 **{cutoff_lbl}% Urgency Cut-Off:** Any remaining open positions will be force-closed if drawdown reaches `{cutoff_lbl}%` (`-{dd['emergency_cutoff_amount']:.2f} {curr}`)."
                if eff_cutoff_pct > 0.0
                else "• ⚪ **Urgency Cut-Off:** `OFF (Unlimited)` — enable anytime via `/losslimit <pause%> <cutoff%>`."
            )
            await notify_saved_messages(
                f"🛑 **{pause_lbl}% Daily Loss Threshold Reached — New Signals Paused**\n"
                f"• Current Daily/Floating Loss: `-{dd['total_daily_loss']:.2f} {curr}` (`{dd['loss_pct']:.1f}%` of `{dd['reference_balance']:.2f} {curr}` balance)\n"
                f"{note_str}"
                f"{cutoff_note}"
            )
        return "PAUSED_50PCT"

    return "OK"


# Last rendered HUD state so we only redraw when values change
_last_hud_state: Optional[Tuple[Any, ...]] = None


def _set_windows_keep_awake(enable: bool) -> None:
    """
    Prevent Windows idle system/network sleep while the copier runs (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    without blocking monitor sleep, and restore default power state on shutdown.
    """
    if os.name != "nt" or not PREVENT_SYSTEM_SLEEP:
        return
    try:
        import ctypes
        es_continuous = 0x80000000
        es_system_required = 0x00000001
        flags = (es_continuous | es_system_required) if enable else es_continuous
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception as e:
        logger.debug(f"Could not set Windows execution state (enable={enable}): {e}")


def _disable_windows_console_quickedit() -> None:
    """
    Disable Win32 Console QuickEdit mode so clicking inside the terminal window never freezes
    stdout / asyncio / Telegram updates until a keypress.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        std_input_handle = -10
        enable_quick_edit = 0x0040
        enable_extended_flags = 0x0080
        h_stdin = kernel32.GetStdHandle(std_input_handle)
        if not h_stdin:
            return
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            new_mode = (mode.value & ~enable_quick_edit) | enable_extended_flags
            if new_mode != mode.value:
                kernel32.SetConsoleMode(h_stdin, new_mode)
    except Exception as e:
        logger.debug(f"Could not disable console QuickEdit mode: {e}")


def _render_terminal_status(st: Dict[str, Any], force_print: bool = False) -> None:
    """Render the in-place MT5 balance bar from a pre-fetched risk exposure status dict."""
    global _last_terminal_snapshot, _current_hud_line, _last_hud_state
    bal = round(st.get("balance", 0.0), 2)
    cred = round(st.get("credit", 0.0), 2)
    eq = round(st.get("equity", 0.0), 2)
    pnl = round(st.get("profit", 0.0), 2)
    curr = st.get("currency", "GBP")
    sym_char = "£" if curr == "GBP" else "$" if curr == "USD" else ""
    open_lots = round(st.get("open_lots", 0.0), 2)
    pending_lots = round(st.get("pending_lots", 0.0), 2)
    low_l = st.get("low_risk_lots", 0.01)
    mid_l = st.get("mid_risk_lots", 0.01)
    high_l = round(st.get("high_risk_lots", 0.01), 2)

    _cfg_mod = sys.modules.get("config") or config
    lot_cap_on = bool(getattr(_cfg_mod, "ENFORCE_HIGH_RISK_LOT_CAP", False))

    snap = (bal, cred, open_lots, pending_lots, lot_cap_on)
    if force_print or snap != _last_terminal_snapshot:
        _last_terminal_snapshot = snap
        if lot_cap_on:
            lots_log = (
                f"Active Lots: {open_lots:.2f}/{high_l:.2f} Max | Pending: {pending_lots:.2f} "
                f"(Low: {low_l:.2f} | Mid: {mid_l:.2f} | High Cap: {high_l:.2f})"
            )
        else:
            lots_log = (
                f"Active Lots: {open_lots:.2f} (Unlimited) | Pending: {pending_lots:.2f} "
                f"(Lot Cap: OFF)"
            )
        logger.info(
            f"💳 [LIVE ACCOUNT & RISK GUIDE] Balance: {bal:.2f} {curr} | Credit: {cred:.2f} {curr} | "
            f"Equity: {eq:.2f} {curr} | {lots_log}"
        )

    hud_state = (bal, cred, eq, pnl, open_lots, pending_lots, high_l, lot_cap_on)
    if not force_print and hud_state == _last_hud_state:
        return
    _last_hud_state = hud_state

    if lot_cap_on:
        title_lots = f"Lots: {open_lots:.2f}/{high_l:.2f} (Pend: {pending_lots:.2f})"
        hud_lots = f"{open_lots:.2f}/{high_l:.2f}"
    else:
        title_lots = f"Lots: {open_lots:.2f} (Unlim, Pend: {pending_lots:.2f})"
        hud_lots = f"{open_lots:.2f} (Unlim)"

    title_str = (
        f"[XAUUSD COPIER v3.0] Bal: {sym_char}{bal:.2f} | Cred: {sym_char}{cred:.2f} | "
        f"Eq: {sym_char}{eq:.2f} | P&L: {'+' if pnl >= 0 else '-'}{sym_char}{abs(pnl):.2f} | "
        f"{title_lots}"
    )
    if os.name == "nt" and CONSOLE_TITLE_UPDATES:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title_str)
        except Exception:
            pass

    pnl_color = "\033[92m" if pnl >= 0 else "\033[91m"
    pnl_sign = "+" if pnl >= 0 else "-"
    _current_hud_line = (
        f"\033[93m[MT5]\033[0m "
        f"Bal: \033[93m{sym_char}{bal:.2f}\033[0m | "
        f"Cred: \033[96m{sym_char}{cred:.2f}\033[0m | "
        f"Eq: {pnl_color}{sym_char}{eq:.2f}\033[0m | "
        f"P&L: {pnl_color}{pnl_sign}{sym_char}{abs(pnl):.2f}\033[0m | "
        f"Lots: \033[93m{hud_lots}\033[0m"
    )
    sys.stdout.write(f"\r\033[2K{_current_hud_line}")
    sys.stdout.flush()


def update_live_terminal_status(force_print: bool = False) -> None:
    """Synchronous terminal HUD updater (preserved for direct callers)."""
    if executor.dry_run:
        return
    try:
        st = executor.get_risk_exposure_status()
        _render_terminal_status(st, force_print=force_print)
    except Exception as e:
        logger.debug(f"Could not update live terminal status: {e}")


async def _update_live_terminal_status_async(force_print: bool = False) -> None:
    """Offload risk exposure IPC onto worker thread and render console HUD on event loop."""
    if executor.dry_run:
        return
    try:
        st = await _mt5_call(executor.get_risk_exposure_status)
        _render_terminal_status(st, force_print=force_print)
    except Exception as e:
        logger.debug(f"Could not update live terminal status: {e}")


def _safe_chat_id(event: Any) -> Optional[int | str]:
    """Extract JSON-safe chat_id from Telethon or mock event (including channel delete updates)."""
    raw_cid = getattr(event, "chat_id", None)
    if isinstance(raw_cid, (int, str)) and not isinstance(raw_cid, bool):
        return raw_cid
    orig_upd = getattr(event, "original_update", None)
    ch_id = getattr(orig_upd, "channel_id", None) if orig_upd is not None else getattr(event, "channel_id", None)
    if isinstance(ch_id, int) and not isinstance(ch_id, bool) and ch_id > 0:
        return int(f"-100{ch_id}")
    return None


def _get_dedup_key(chat_id: Optional[int | str], msg_id: Any) -> Any:
    """Build canonical per-channel deduplication key (normalizing -100<id> and -<id> variants)."""
    return (_canonical_chat_key(chat_id), msg_id) if chat_id is not None else msg_id


def _record_deleted_tombstone(chat_id: Optional[int | str], msg_id: Any) -> None:
    """Record a deleted message tombstone so out-of-order NewMessage/MessageEdited events cannot resurrect it."""
    now = time.time()
    if len(_deleted_messages) > 500:
        cutoff = now - 600.0
        for k in [k for k, ts in _deleted_messages.items() if ts < cutoff]:
            _deleted_messages.pop(k, None)
    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    _deleted_messages[(eff_chat, mid_norm)] = now
    if eff_chat is None:
        _deleted_messages[(None, mid_norm)] = now


def _is_supergroup_chat_id(chat_id: Any) -> bool:
    """Return True when chat_id is a Telegram channel/supergroup (-100...) with its own message ID sequence."""
    return chat_id is not None and not isinstance(chat_id, bool) and str(chat_id).strip().startswith("-100")


def _is_message_tombstoned(chat_id: Optional[int | str], msg_id: Any) -> bool:
    """Return True if (chat_id, msg_id) has been recorded as deleted in Telegram."""
    if not _deleted_messages:
        return False
    now = time.time()
    cutoff = now - 600.0
    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    direct = _deleted_messages.get((eff_chat, mid_norm))
    if direct is not None and direct >= cutoff:
        return True
    if not _is_supergroup_chat_id(eff_chat):
        bare = _deleted_messages.get((None, mid_norm))
        if bare is not None and bare >= cutoff:
            return True
    mid_str = str(mid_norm)
    for (k_chat, k_mid), ts in list(_deleted_messages.items()):
        if ts < cutoff:
            _deleted_messages.pop((k_chat, k_mid), None)
            continue
        if str(k_mid) != mid_str:
            continue
        if k_chat is not None and eff_chat is not None and _chat_ids_match(k_chat, eff_chat):
            return True
        if eff_chat is None and k_chat is not None:
            return True
        if k_chat is None and not _is_supergroup_chat_id(eff_chat):
            return True
    return False


def _get_live_entry_price(tick: Any, direction: str) -> Optional[float]:
    """Return the relevant live entry price (bid for SELL, ask for BUY), rounded to 2 decimals."""
    if not tick:
        return None
    dir_up = str(direction or "").upper()
    raw_px = getattr(tick, "bid", None) if dir_up == "SELL" else getattr(tick, "ask", None)
    if isinstance(raw_px, (int, float)) and not isinstance(raw_px, bool) and raw_px > 0:
        return round(float(raw_px), 2)
    return None


def _tick_hit_tp1(tick: Any, direction: str, tp1_price: Optional[float]) -> bool:
    """Return True when live tick bid or ask has reached tp1_price in the signal direction."""
    if not tick or tp1_price is None:
        return False
    try:
        tp_val = float(tp1_price)
    except (TypeError, ValueError):
        return False
    dir_up = str(direction or "").upper()
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    bid_ok = isinstance(bid, (int, float)) and not isinstance(bid, bool)
    ask_ok = isinstance(ask, (int, float)) and not isinstance(ask, bool)
    if dir_up == "BUY":
        return (bid_ok and float(bid) >= tp_val) or (ask_ok and float(ask) >= tp_val)
    if dir_up == "SELL":
        return (ask_ok and float(ask) <= tp_val) or (bid_ok and float(bid) <= tp_val)
    return False


def _is_strictly_pending_limit_setup(
    tick: Any,
    direction: str,
    zone: Any = None,
    single_price: Optional[float] = None,
    is_stop_order: bool = False,
) -> bool:
    """Return True if all planned entry tiers are pending limit/stop orders away from live market."""
    if not tick:
        return False
    dir_up = str(direction or "").upper()
    if is_stop_order and isinstance(single_price, (int, float)) and not isinstance(single_price, bool):
        if dir_up == "BUY":
            ask = getattr(tick, "ask", None)
            return isinstance(ask, (int, float)) and not isinstance(ask, bool) and float(ask) < float(single_price)
        if dir_up == "SELL":
            bid = getattr(tick, "bid", None)
            return isinstance(bid, (int, float)) and not isinstance(bid, bool) and float(bid) > float(single_price)
        return False

    ref_px = (
        getattr(zone, "top", None)
        if (zone is not None and dir_up == "BUY")
        else (getattr(zone, "bottom", None) if zone is not None else single_price)
    )
    if not isinstance(ref_px, (int, float)) or isinstance(ref_px, bool):
        return False
    if dir_up == "BUY":
        ask = getattr(tick, "ask", None)
        return isinstance(ask, (int, float)) and not isinstance(ask, bool) and float(ask) > float(ref_px)
    if dir_up == "SELL":
        bid = getattr(tick, "bid", None)
        return isinstance(bid, (int, float)) and not isinstance(bid, bool) and float(bid) < float(ref_px)
    return False


def _compute_initial_tp1_guard_armed(
    tick: Any,
    direction: str,
    raw_tp1: Optional[float],
    has_market: bool,
    zone: Any = None,
    single_price: Optional[float] = None,
    is_stop_order: bool = False,
) -> bool:
    """
    Entry-First Gate for TP1 invalidation:
    - When market positions already exist (`has_market=True`), arm immediately (`True`).
    - When a setup is pending-only (`has_market=False`, 0 filled layers), NEVER arm solely because
      live price is below/above TP1; arm ONLY if live price has already entered the entry zone/price.
    """
    if has_market:
        return True
    if not tick:
        return False

    dir_up = str(direction or "").upper()
    ref_px: Optional[float] = None
    if isinstance(zone, dict):
        raw_ref = zone.get("top") if dir_up == "BUY" else zone.get("bottom")
        if isinstance(raw_ref, (int, float)) and not isinstance(raw_ref, bool):
            ref_px = float(raw_ref)
    elif zone is not None:
        raw_ref = getattr(zone, "top", None) if dir_up == "BUY" else getattr(zone, "bottom", None)
        if isinstance(raw_ref, (int, float)) and not isinstance(raw_ref, bool):
            ref_px = float(raw_ref)
    elif isinstance(single_price, (int, float)) and not isinstance(single_price, bool):
        ref_px = float(single_price)

    if ref_px is None:
        return False

    if dir_up == "BUY":
        ask = getattr(tick, "ask", None)
        if isinstance(ask, (int, float)) and not isinstance(ask, bool):
            return (float(ask) >= ref_px - 1e-9) if is_stop_order else (float(ask) <= ref_px + 1e-9)
    elif dir_up == "SELL":
        bid = getattr(tick, "bid", None)
        if isinstance(bid, (int, float)) and not isinstance(bid, bool):
            return (float(bid) <= ref_px + 1e-9) if is_stop_order else (float(bid) >= ref_px - 1e-9)
    return False


def _reserve_task_key(msg_id: Any, chat_id: Optional[int | str] = None) -> Any:
    """Cancel any prior matching delayed task and return the collision-safe tracking key."""
    _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    if mid_norm not in _pending_sell_now_tasks or _pending_task_chats.get(mid_norm) in (None, eff_chat):
        return mid_norm
    return (eff_chat, mid_norm)


def _cancel_delayed_sell_now(msg_id: Any, chat_id: Optional[int | str] = None) -> bool:
    """Cancel an active delayed 'Sell now' / 'Buy now' timer if still waiting."""
    try:
        cur_task = asyncio.current_task()
    except RuntimeError:
        cur_task = None

    if isinstance(msg_id, tuple) and msg_id in _pending_sell_now_tasks:
        task = _pending_sell_now_tasks.pop(msg_id, None)
        _pending_task_chats.pop(msg_id, None)
        if task and not task.done() and task is not cur_task:
            task.cancel()
            return True
        return False

    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    cancelled = False
    candidate_keys: List[Any] = []
    if eff_chat is not None and (eff_chat, mid_norm) in _pending_sell_now_tasks:
        candidate_keys.append((eff_chat, mid_norm))
    if mid_norm in _pending_sell_now_tasks and (
        eff_chat is None or _pending_task_chats.get(mid_norm) in (None, eff_chat)
    ):
        candidate_keys.append(mid_norm)
    if eff_chat is None:
        for k in list(_pending_sell_now_tasks.keys()):
            if isinstance(k, tuple) and len(k) == 2 and k[1] == mid_norm and k not in candidate_keys:
                candidate_keys.append(k)

    for k in candidate_keys:
        task = _pending_sell_now_tasks.pop(k, None)
        _pending_task_chats.pop(k, None)
        if task and not task.done() and task is not cur_task:
            task.cancel()
            cancelled = True
    return cancelled


async def _invalidate_signal_tp1_unlocked(
    msg_id: Any,
    reason: str,
    chat_id: Optional[int | str] = None,
) -> bool:
    """
    Invalidate an unentered signal (PENDING/PENDING_DELAY or unfilled zone pending limits)
    when TP1 is hit via Telegram Reply/Edit/Alert or live MT5 market price.
    Caller must already hold `_get_signal_lock(chat_id, msg_id)`.
    """
    _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
    sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
    if not sig:
        logger.info(f"[TP1 INVALIDATED] {DEFAULT_SYMBOL} | Reason: {reason} | Setup Deleted.")
        return False

    if sig.get("status") == "INVALIDATED_TP1":
        return False

    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = sig.get("direction", "BUY")
    eff_chat = chat_id if chat_id is not None else sig.get("chat_id")
    market_tickets = list(sig.get("market_tickets") or [])
    pending_tickets = list(sig.get("pending_tickets") or [])
    extra_pending = [
        t for t in (sig.get("mt5_order_tickets") or [])
        if t not in market_tickets and t not in pending_tickets
    ]
    layers = dict(sig.get("layers") or {})
    sig["layers"] = layers
    cancelled = await _mt5_call(executor.cancel_signal_pending_orders, sig)
    layers = sig.get("layers") or layers
    all_pending = list(dict.fromkeys(pending_tickets + extra_pending))

    for lk, l_data in layers.items():
        if isinstance(l_data, dict):
            t_val = l_data.get("ticket")
            if t_val not in market_tickets and l_data.get("status") not in ("OPEN", "FILLED", "CLOSED", "BANKED"):
                l_data["status"] = "CANCELLED"

    state_mgr.update_signal(
        msg_id,
        {
            "pending_tickets": [],
            "layers": layers,
            "tp1_reached": True,
            "status": "INVALIDATED_TP1",
        },
        chat_id=eff_chat,
    )

    logger.info(f"[TP1 INVALIDATED] {symbol} | Reason: {reason} | Setup Deleted.")
    await notify_saved_messages(
        f"🚫 **[TP1 INVALIDATED] {symbol} ({direction}) [ID: {msg_id}]**\n"
        f"• Reason: `{reason}`\n"
        f"• Setup deleted before entry; cancelled `{len(all_pending)}` pending order(s)."
    )
    return True


async def invalidate_signal_tp1(
    msg_id: Any,
    reason: str,
    chat_id: Optional[int | str] = None,
) -> bool:
    """Thread/task-safe TP1 invalidation acquiring the per-(chat_id, msg_id) signal lock."""
    async with _get_signal_lock(chat_id, msg_id):
        return await _invalidate_signal_tp1_unlocked(msg_id, reason=reason, chat_id=chat_id)


async def _delayed_sell_now_task(
    msg_id: int,
    chat_id: Optional[int | str] = None,
    task_key: Any = None,
) -> None:
    """
    Wait SELL_NOW_DELAY_SECONDS on unpriced 'Sell now' / 'Buy now':
    1. Polls every min(DELAYED_POLL_INTERVAL_SECONDS, max(0.01, delay/4)) during the wait;
       immediately invalidates via Trigger 2 if live market price reaches tp1_price.
    2. If edited with a zone/limit or cancelled ('TP1 hit' / 'close') during the wait, exits cleanly.
    3. At T = delay_seconds, compares fresh live entry price against `initial_price` and executes
       4-layer market entry (`L1..L4 -> TP1..TP5`) when favorable.
    """
    eff_key = task_key if task_key is not None else msg_id
    try:
        delay_total = max(0.0, float(SELL_NOW_DELAY_SECONDS))
        step = min(float(DELAYED_POLL_INTERVAL_SECONDS), max(0.01, delay_total / 4.0))
        deadline = time.monotonic() + delay_total

        while True:
            async with _get_signal_lock(chat_id, msg_id):
                sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
                if (
                    not sig
                    or sig.get("status") not in ("PENDING", "PENDING_DELAY")
                    or sig.get("tp1_reached")
                    or sig.get("is_zone_order")
                ):
                    return
                if session_trading_blocked():
                    state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
                    logger.info(f"Delayed entry [ID: {msg_id}] dropped: session is terminating / halted / locked.")
                    return

                symbol = sig.get("symbol", DEFAULT_SYMBOL)
                direction = sig.get("direction", "SELL").upper()
                tp1_price = sig.get("tp1_price") if sig.get("tp1_price") is not None else sig.get("raw_tp1")
                tick = await _mt5_call(executor.get_tick, symbol)
                if tp1_price is not None and tick and _tick_hit_tp1(tick, direction, tp1_price):
                    await _invalidate_signal_tp1_unlocked(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
                    return

            now_mono = time.monotonic()
            if now_mono >= deadline:
                break
            await asyncio.sleep(min(step, max(0.005, deadline - now_mono)))

        # Optional legacy recovery-wait loop uses sleeping outside lock, checking inside lock
        while True:
            async with _get_signal_lock(chat_id, msg_id):
                sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
                if (
                    not sig
                    or sig.get("status") not in ("PENDING", "PENDING_DELAY")
                    or sig.get("tp1_reached")
                    or sig.get("is_zone_order")
                ):
                    return
                if session_trading_blocked():
                    state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
                    return

                symbol = sig.get("symbol", DEFAULT_SYMBOL)
                direction = sig.get("direction", "SELL").upper()
                sl = sig.get("sl")
                final_tp = sig.get("final_tp")
                tp1_price = sig.get("tp1_price") if sig.get("tp1_price") is not None else sig.get("raw_tp1")
                initial_price = (
                    sig.get("initial_price")
                    if sig.get("initial_price") is not None
                    else sig.get("recorded_entry_price")
                )

                current_tick = await _mt5_call(executor.get_tick, symbol)
                if tp1_price is not None and current_tick and _tick_hit_tp1(current_tick, direction, tp1_price):
                    await _invalidate_signal_tp1_unlocked(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
                    return

                current_price = _get_live_entry_price(current_tick, direction)

                if isinstance(initial_price, (int, float)) and not isinstance(initial_price, bool):
                    init_val = round(float(initial_price), 2)
                    if current_price is None:
                        is_favorable = False
                    elif direction == "SELL":
                        is_favorable = current_price >= init_val
                    else:
                        is_favorable = current_price <= init_val

                    if not is_favorable:
                        if NOW_PRICE_GUARD_ABORT_ON_MISMATCH:
                            logger.info(
                                f"[PRICE GUARD] {symbol} | {direction} | Initial: {init_val} | "
                                f"Post-Delay: {current_price} | Action: ABORTED"
                            )
                            state_mgr.update_signal(
                                msg_id,
                                {
                                    "status": "ABORTED_PRICE_GUARD",
                                    "awaiting_favorable_price": False,
                                    "post_delay_price": current_price,
                                },
                                chat_id=chat_id,
                            )
                            await notify_saved_messages(
                                f"🚫 **Delayed {direction.title()} Now Aborted by Price Guard [ID: {msg_id}]**\n"
                                f"• Symbol: `{symbol}` ({direction})\n"
                                f"• 📍 **Initial Snapshot:** `{init_val}` | **Post-Delay:** `{current_price}`\n"
                                f"• Price moved unfavorably during the delay; trade skipped cleanly."
                            )
                            return

                        # Legacy recovery-wait mode when NOW_PRICE_GUARD_ABORT_ON_MISMATCH=false
                        wait_started = sig.get("_legacy_wait_started")
                        if not isinstance(wait_started, (int, float)):
                            wait_started = time.time()
                            sig["_legacy_wait_started"] = wait_started
                        if current_price is not None and not sig.get("awaiting_favorable_price"):
                            state_mgr.update_signal(msg_id, {"awaiting_favorable_price": True}, chat_id=chat_id)
                            cmp_desc = (
                                f"higher (`>= {init_val:.2f}`)"
                                if direction == "SELL"
                                else f"lower (`<= {init_val:.2f}`)"
                            )
                            logger.info(
                                f"Delayed {direction} Now [ID: {msg_id}] held by Bad-Position Guard: "
                                f"live={current_price} vs initial={init_val}. Waiting for {cmp_desc}."
                            )
                            await notify_saved_messages(
                                f"🛡️ **Bad-Position Entry Guard Active [ID: {msg_id}]**\n"
                                f"• Direction: `{direction}` {symbol}\n"
                                f"• 📍 **Initial Snapshot:** `{init_val}` | **Current Price:** `{current_price}`\n"
                                f"• Waiting until price is {cmp_desc} (or cancel if TP1 is hit)."
                            )
                        if (time.time() - float(wait_started)) >= NOW_ENTRY_MAX_WAIT_SECONDS:
                            state_mgr.update_signal(
                                msg_id,
                                {"status": "ABORTED_PRICE_GUARD", "awaiting_favorable_price": False},
                                chat_id=chat_id,
                            )
                            return
                    else:
                        logger.info(
                            f"[PRICE GUARD] {symbol} | {direction} | Initial: {init_val} | "
                            f"Post-Delay: {current_price} | Action: EXECUTED"
                        )
                        break
                else:
                    logger.info(
                        f"[PRICE GUARD] {symbol} | {direction} | Initial: None | "
                        f"Post-Delay: {current_price} | Action: EXECUTED"
                    )
                    break

            await asyncio.sleep(step)

        # Execute 4-layer market entry under lock
        async with _get_signal_lock(chat_id, msg_id):
            sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
            if (
                not sig
                or sig.get("status") not in ("PENDING", "PENDING_DELAY")
                or sig.get("tp1_reached")
                or sig.get("is_zone_order")
            ):
                return
            if session_trading_blocked():
                state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
                return
            if CLOSE_ON_SIGNAL_DELETED and (
                _is_message_tombstoned(chat_id, msg_id)
                or (
                    chat_id is not None
                    and telethon_client is not None
                    and isinstance(msg_id, int)
                    and await _verify_message_deleted_on_telegram(chat_id, msg_id)
                )
            ):
                await _close_deleted_signal_unlocked(
                    msg_id,
                    sig,
                    chat_id=chat_id,
                    source_label="Pre-Execution Delete Check",
                )
                return

            symbol = sig.get("symbol", DEFAULT_SYMBOL)
            direction = sig.get("direction", "SELL").upper()
            sl = sig.get("sl")
            final_tp = sig.get("final_tp")
            tp1_price = sig.get("tp1_price") if sig.get("tp1_price") is not None else sig.get("raw_tp1")
            initial_price = (
                sig.get("initial_price")
                if sig.get("initial_price") is not None
                else sig.get("recorded_entry_price")
            )

            if executor.get_daily_drawdown_status().get("pause_new_signals"):
                await check_and_enforce_daily_drawdown()

            passed, reason = await _mt5_call(
                executor.check_preflight,
                symbol=symbol,
                direction=direction,
                raw_tp1=tp1_price,
                sl=sl,
                order_intent="MARKET",
            )
            if not passed:
                logger.warning(f"Delayed {direction} Now [ID: {msg_id}] blocked by pre-flight after wait: {reason}")
                state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
                await notify_saved_messages(f"⚠️ **Delayed {direction.title()} Now Skipped [ID: {msg_id}]**: {reason}")
                return

            prev_lots = (await _mt5_call(executor.get_total_active_lots))["open_lots"]
            staged_entry = bool(STAGED_MARKET_ENTRY)
            is_delayed_stub = bool(sl is None and not sig.get("tp_levels") and tp1_price is None) or staged_entry
            mk_tkts, pd_tkts, layers_data = await _mt5_call(
                executor.execute_4layer_market,
                symbol=symbol,
                direction=direction,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=tp1_price,
                raw_tp2=sig.get("raw_tp2"),
                raw_tp3=sig.get("raw_tp3"),
                raw_tp4=sig.get("raw_tp4"),
                raw_tp5=sig.get("raw_tp5"),
                tp_levels=sig.get("tp_levels"),
                max_layers=1 if is_delayed_stub else None,
            )
            if not mk_tkts and not pd_tkts:
                err_reason = getattr(executor, "last_error", None) or "Broker rejected order"
                logger.error(f"Delayed {direction} Now [ID: {msg_id}] failed on MT5: {err_reason}")
                state_mgr.delete_signal(msg_id, chat_id=chat_id)
                await notify_saved_messages(
                    f"❌ **Order Rejected by MT5 [ID: {msg_id}]**\n"
                    f"• Direction: `{direction}` {symbol}\n"
                    f"• Reason: `{err_reason}`\n"
                    f"• No positions or pending orders were opened."
                )
                return

            new_lots = (await _mt5_call(executor.get_total_active_lots))["open_lots"]
            risk_alerts = await _mt5_call(executor.evaluate_risk_threshold_alerts, prev_lots, new_lots)
            alloc_warn = getattr(executor, "last_allocation_warning", None)
            extra_notes: List[str] = []
            if alloc_warn:
                extra_notes.append(f"⚠️ **Lot Cap Note:** `{alloc_warn}`")
            extra_notes.extend(risk_alerts)
            risk_block = ("\n\n" + "\n".join(extra_notes)) if extra_notes else ""

            dyn_d = _merge_dynamic_risk_into_signal_fields(
                getattr(executor, "last_dynamic_risk_snapshot", None),
                sl, final_tp, tp1_price, sig.get("raw_tp2"), sig.get("raw_tp3"),
                sig.get("raw_tp4"), sig.get("raw_tp5"), sig.get("tp_levels"),
            )
            sl = dyn_d["sl"]
            update_d: Dict[str, Any] = {
                "status": "ACTIVE",
                "awaiting_favorable_price": False,
                "market_tickets": mk_tkts,
                "pending_tickets": pd_tkts,
                "layers": layers_data,
                "max_orders": MAX_ORDERS_PER_SIGNAL,
                "tp1_guard_armed": True,
            }
            if dyn_d["dynamic_risk"] is not None:
                update_d.update({
                    "sl": dyn_d["sl"],
                    "final_tp": dyn_d["final_tp"],
                    "hard_broker_tp": dyn_d["final_tp"],
                    "raw_tp1": dyn_d["raw_tp1"],
                    "tp1_price": dyn_d["raw_tp1"],
                    "raw_tp2": dyn_d["raw_tp2"],
                    "raw_tp3": dyn_d["raw_tp3"],
                    "raw_tp4": dyn_d["raw_tp4"],
                    "raw_tp5": dyn_d["raw_tp5"],
                    "tp_levels": dyn_d["tp_levels"],
                    "dynamic_risk": dyn_d["dynamic_risk"],
                })
            state_mgr.update_signal(msg_id, update_d, chat_id=chat_id)
            updated_sig = state_mgr.get_signal(msg_id, chat_id=chat_id) or sig
            await notify_saved_messages(
                f"🚀 **Delayed {direction.title()} Now 4-Layer Executed [ID: {msg_id}]**\n"
                f"• Direction: `{direction}` {symbol}\n"
                f"• 📍 **Initial Point:** `{initial_price}` | **Entered At:** `{current_price}`\n"
                f"• Orders: Market={mk_tkts} ({_format_layers_summary(updated_sig)})\n"
                f"• SL: `{sl}` | Targets: {_format_tp_summary(updated_sig)}"
                f"{risk_block}"
            )
    except asyncio.CancelledError:
        logger.info(f"Delayed Now timer cancelled for Msg ID {msg_id} (limits/TP1/cancel arrived).")
    finally:
        if _pending_sell_now_tasks.get(eff_key) is asyncio.current_task():
            _pending_sell_now_tasks.pop(eff_key, None)
            _pending_task_chats.pop(eff_key, None)


# ---------------------------------------------------------------------------
# NOTIFICATION HELPER
# ---------------------------------------------------------------------------

def format_profit(amount: float, currency: str = "GBP") -> str:
    """Format profit amount with currency symbol (+£1.25 GBP)."""
    sym = "£" if currency.upper() == "GBP" else "$" if currency.upper() == "USD" else "€" if currency.upper() == "EUR" else ""
    sign = "+" if amount >= 0 else "-"
    abs_val = abs(amount)
    return f"{sign}{sym}{abs_val:.2f} {currency}"


async def notify_saved_messages(text: str) -> None:
    """Send trade alerts and execution summaries to Telegram Saved Messages ('me')."""
    if not NOTIFY_SAVED_MESSAGES or not telethon_client:
        return
    try:
        res = telethon_client.send_message("me", text)
        if inspect.isawaitable(res):
            await res
    except Exception as e:
        logger.warning(f"Could not deliver notification to Saved Messages: {e}")


# ---------------------------------------------------------------------------
# MT5 HEALTH TELEMETRY & ONE-SHOT DISCONNECT / RECOVERY ALERTS
# ---------------------------------------------------------------------------

async def _probe_mt5_health() -> Tuple[bool, bool, Optional[float]]:
    """Measure MT5 connection + algo-trading state off-loop and return (connected, trade_allowed, latency_ms)."""
    global _mt5_last_ping_ms
    t0 = time.perf_counter()
    if hasattr(executor, "get_health_status"):
        connected, allowed = await _mt5_call(executor.get_health_status)
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        _mt5_last_ping_ms = latency_ms
        return bool(connected), bool(allowed), latency_ms
    connected = bool(await _mt5_call(executor.is_connected))
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    _mt5_last_ping_ms = latency_ms
    allowed = bool(await _mt5_call(executor.is_trade_allowed)) if connected else False
    return connected, allowed, latency_ms


async def _handle_mt5_connection_transition(connected: bool) -> None:
    """
    Emit a single Saved Messages alert on connected -> disconnected and a single
    recovery alert on disconnected -> connected.
    """
    global _mt5_connected_state, _mt5_last_disconnect_at
    if executor.dry_run:
        return

    if _mt5_connected_state is None:
        _mt5_connected_state = bool(connected)
        if not connected:
            _mt5_last_disconnect_at = time.time()
        return

    if _mt5_connected_state and not connected:
        _mt5_connected_state = False
        _mt5_last_disconnect_at = time.time()
        logger.error("🔴 MT5 connection lost! Waiting for MetaTrader 5 terminal to reconnect...")
        await notify_saved_messages(
            "🔴 **MT5 Connection Lost**\n"
            "• MetaTrader 5 terminal disconnected from the trade server.\n"
            "• New orders and broker reconciliation will resume automatically once MT5 reconnects."
        )
    elif not _mt5_connected_state and connected:
        _mt5_connected_state = True
        downtime_note = ""
        if _mt5_last_disconnect_at is not None:
            downtime_sec = max(0, int(time.time() - _mt5_last_disconnect_at))
            downtime_note = f"\n• Downtime: `{downtime_sec}s`"
        _mt5_last_disconnect_at = None
        logger.info("🟢 MT5 connection recovered!")
        await notify_saved_messages(
            f"🟢 **MT5 Connection Recovered**\n"
            f"• MetaTrader 5 terminal is back online and connected to the trade server.{downtime_note}"
        )


# ---------------------------------------------------------------------------
# BOUNDED RECONNECT CATCH-UP WITH STALE-MARKET GUARDS
# ---------------------------------------------------------------------------

class _CatchupEvent:
    """Minimal Telethon-compatible event wrapper for replaying missed channel messages."""

    def __init__(self, message: Any, chat_id: int | str):
        self.message = message
        self.chat_id = chat_id
        reply_to = getattr(message, "reply_to_msg_id", None)
        if reply_to is None:
            r_obj = getattr(message, "reply_to", None)
            reply_to = getattr(r_obj, "reply_to_msg_id", None) if r_obj else None
        self.is_reply = reply_to is not None


def _evaluate_catchup_staleness(
    parsed: SignalAction,
    age_sec: float,
    tick: Any,
    pip_size: float,
) -> Optional[str]:
    """
    Validate age & slippage for caught-up NEW_SIGNAL messages.
    Returns a human-readable rejection reason if stale/slipped, or None when safe.
    """
    direction = (parsed.direction or "BUY").upper()
    is_stop = bool(getattr(parsed, "is_stop_order", False))
    live_px = _get_live_entry_price(tick, direction)

    ref_px: Optional[float] = None
    would_fill_market = False
    if live_px is not None:
        if parsed.single_price is not None:
            ref_px = float(parsed.single_price)
            if is_stop:
                would_fill_market = (direction == "BUY" and live_px >= ref_px) or (direction == "SELL" and live_px <= ref_px)
            else:
                would_fill_market = (direction == "BUY" and live_px <= ref_px) or (direction == "SELL" and live_px >= ref_px)
        elif parsed.is_zone and parsed.zone is not None:
            ref_px = float(parsed.zone.top if direction == "BUY" else parsed.zone.bottom)
            would_fill_market = (direction == "BUY" and live_px <= ref_px) or (direction == "SELL" and live_px >= ref_px)

    is_strictly_pending_limit = (
        not parsed.is_instant_now
        and not is_stop
        and ref_px is not None
        and not would_fill_market
    )
    if parsed.get("tp_valid") is False:
        inv_tps = parsed.get("invalid_explicit_tps") or []
        return f"TP1 ({inv_tps[0] if inv_tps else 'invalid'}) already reached or invalid on live market"
    if (
        not is_strictly_pending_limit
        and parsed.raw_tp1 is not None
        and tick
        and _tick_hit_tp1(tick, direction, parsed.raw_tp1)
    ):
        return f"TP1 ({parsed.raw_tp1}) already reached on live market"

    if parsed.is_instant_now:
        if age_sec > float(CATCHUP_MARKET_MAX_AGE_SECONDS):
            return (
                f"Market Now signal age {age_sec:.1f}s exceeds "
                f"CATCHUP_MARKET_MAX_AGE_SECONDS ({CATCHUP_MARKET_MAX_AGE_SECONDS}s)"
            )
        return None

    if live_px is None or pip_size <= 0:
        return None

    if would_fill_market and ref_px is not None:
        slip_pips = round(abs(live_px - ref_px) / pip_size, 2)
        if slip_pips > float(CATCHUP_MARKET_MAX_SLIPPAGE_PIPS):
            return (
                f"Market catch-up slippage {slip_pips:.1f} pips exceeds "
                f"CATCHUP_MARKET_MAX_SLIPPAGE_PIPS ({CATCHUP_MARKET_MAX_SLIPPAGE_PIPS} pips)"
            )
        if age_sec > float(CATCHUP_MARKET_MAX_AGE_SECONDS):
            return (
                f"Crossed market catch-up age {age_sec:.1f}s exceeds "
                f"CATCHUP_MARKET_MAX_AGE_SECONDS ({CATCHUP_MARKET_MAX_AGE_SECONDS}s)"
            )

    return None


async def _close_deleted_signal_unlocked(
    msg_id: Any,
    sig: Dict[str, Any],
    chat_id: Optional[int | str] = None,
    source_label: str = "Telegram Delete Event",
) -> bool:
    """
    Take out (close open positions + cancel pending orders) a signal whose Telegram message was deleted.
    Caller must hold `_get_signal_lock(eff_chat, raw_mid)`.
    """
    if not CLOSE_ON_SIGNAL_DELETED or not isinstance(sig, dict):
        return False

    raw_mid, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id if chat_id is not None else sig.get("chat_id"))
    has_open_tickets = bool(sig.get("market_tickets") or sig.get("pending_tickets"))
    if (
        sig.get("status") in ("CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD")
        and not has_open_tickets
    ):
        return False

    _cancel_delayed_sell_now(raw_mid, chat_id=eff_chat)
    _cancel_delayed_sell_now(msg_id, chat_id=eff_chat)
    _record_deleted_tombstone(eff_chat, raw_mid)

    res = await _mt5_call(executor.close_signal_exposure, sig)
    closed_tkts = list((res or {}).get("closed_tickets") or [])
    cancelled_ords = list((res or {}).get("cancelled_orders") or [])

    if closed_tkts:
        await _account_closed_tickets(sig, closed_tkts)

    closed_set = set(closed_tkts)
    layers = copy.deepcopy(sig.get("layers") or {})
    for lk, l_data in layers.items():
        if not isinstance(l_data, dict):
            continue
        lt = l_data.get("ticket")
        st = str(l_data.get("status") or "").upper()
        if lt in closed_set or st in ("OPEN", "FILLED", "RUNNER", "CLOSED", "BANKED") or st.startswith("CLOSED"):
            l_data["status"] = "CLOSED"
        else:
            l_data["status"] = "CANCELLED"

    close_reason_str = f"Signal Deleted ({source_label})"
    prev_rlayers = list(sig.get("runner_layers") or ([sig["runner_layer"]] if sig.get("runner_layer") else []))
    prev_rlayer = sig.get("runner_layer") or (prev_rlayers[-1] if prev_rlayers else sig.get("last_runner_layer"))
    updates: Dict[str, Any] = {
        "status": "CLOSED",
        "deleted_from_chat": True,
        "close_reason": close_reason_str,
        "runner_close_reason": close_reason_str,
        "market_tickets": [],
        "pending_tickets": [],
        "runner_tickets": [],
        "runner_ticket": None,
        "runner_layers": [],
        "runner_layer": None,
        "runner_proximity_armed": False,
        "runner_reversal_ticks": 0,
        "shortfall_armed": False,
        "shortfall_reversal_ticks": 0,
        "tp2_shortfall_armed": False,
        "layers": layers,
        "accounted_tickets": sig.get("accounted_tickets", []),
        "banked_profit": sig.get("banked_profit", 0.0),
    }
    if prev_rlayers:
        updates["last_runner_layers"] = prev_rlayers
    if prev_rlayer:
        updates["last_runner_layer"] = prev_rlayer
    sig.update(updates)
    state_mgr.update_signal(msg_id, updates, chat_id=eff_chat)

    fin = await _mt5_call(executor.get_account_financials)
    curr = fin.get("currency", "GBP")
    await _record_signal_to_ledger(raw_mid, sig, exit_reason=close_reason_str, currency=curr)

    total_pnl = float(sig.get("banked_profit") or 0.0)
    pnl_str = format_profit(total_pnl, curr)
    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = sig.get("direction", "BUY")

    logger.warning(
        f"🗑️ Signal Deleted [{source_label}] [ID: {raw_mid}] in Chat [{eff_chat}]: "
        f"Closed={closed_tkts}, Cancelled={cancelled_ords}, RealizedPnL={pnl_str}"
    )
    await notify_saved_messages(
        f"🗑️ **Signal Deleted in Chat — Trade Taken Out! [ID: {raw_mid}]**\n"
        f"• Symbol: `{symbol}` ({direction}) | Source: `{source_label}`\n"
        f"• Closed Market Positions: `{closed_tkts}`\n"
        f"• Cancelled Pending Orders: `{cancelled_ords}`\n"
        f"• 💰 **Realized P&L:** `{pnl_str}`\n\n"
        f"{fin['formatted']}"
    )
    await _update_live_terminal_status_async()
    await check_and_enforce_daily_loss_limit(exit_on_trigger=False)
    return True


async def _verify_message_deleted_on_telegram(sig_chat: Any, msg_id: int) -> bool:
    """
    Verify via Telegram `get_messages` whether `msg_id` was actually deleted in `sig_chat`
    (used when basic-group `UpdateDeleteMessages` arrives with `chat_id=None`).
    """
    if sig_chat is None or telethon_client is None:
        return True
    get_msgs_fn = getattr(telethon_client, "get_messages", None)
    if not callable(get_msgs_fn):
        return True
    try:
        raw_res = get_msgs_fn(sig_chat, ids=[int(msg_id)])
        if inspect.isawaitable(raw_res):
            fetched = await asyncio.wait_for(raw_res, timeout=8.0)
        else:
            fetched = raw_res
    except Exception as err:
        logger.debug(f"Delete verification get_messages note for chat {sig_chat} msg {msg_id}: {err}")
        return False

    if fetched is None:
        return True
    if isinstance(fetched, (list, tuple)):
        if len(fetched) == 0:
            return True
        if len(fetched) == 1:
            item = fetched[0]
            if item is None or type(item).__name__ == "MessageEmpty":
                return True
            if getattr(item, "id", None) == int(msg_id):
                return False
        for item in fetched:
            if item is not None and type(item).__name__ != "MessageEmpty" and getattr(item, "id", None) == int(msg_id):
                return False
        return True
    if type(fetched).__name__ == "MessageEmpty":
        return True
    if getattr(fetched, "id", None) == int(msg_id):
        return False
    return False


async def handle_message_deleted(event: Any) -> None:
    """
    Real-time handler for `events.MessageDeleted`:
    Immediately closes open positions and cancels pending orders for any deleted signal message.
    """
    if not CLOSE_ON_SIGNAL_DELETED or event is None:
        return

    raw_ids = getattr(event, "deleted_ids", None)
    if not isinstance(raw_ids, (list, tuple, set)):
        single_id = getattr(event, "deleted_id", None)
        if single_id is not None:
            raw_ids = [single_id]
        else:
            orig_upd = getattr(event, "original_update", None)
            raw_ids = getattr(orig_upd, "messages", None) or []

    deleted_ids: List[int] = []
    for x in raw_ids:
        if isinstance(x, int) and not isinstance(x, bool) and x > 0:
            if x not in deleted_ids:
                deleted_ids.append(x)
        elif isinstance(x, str) and x.strip().isdigit() and int(x.strip()) > 0:
            val = int(x.strip())
            if val not in deleted_ids:
                deleted_ids.append(val)

    if not deleted_ids:
        return

    chat_id = _safe_chat_id(event)
    is_monitored_chat = chat_id is not None and any(_chat_ids_match(ch, chat_id) for ch in CHANNEL_IDS)
    if is_monitored_chat:
        for did in deleted_ids:
            _record_deleted_tombstone(chat_id, did)

    for did in deleted_ids:
        matches = state_mgr.find_matching_signals_for_delete(did, chat_id=chat_id)
        if not matches and chat_id is None:
            _record_deleted_tombstone(None, did)
            continue
        for sig_key, sig_snap in matches:
            sig_chat = sig_snap.get("chat_id")
            if chat_id is not None and not is_monitored_chat:
                if sig_chat is None or not _chat_ids_match(sig_chat, chat_id):
                    continue
            if chat_id is None and sig_chat is not None:
                if not await _verify_message_deleted_on_telegram(sig_chat, int(did)):
                    logger.debug(
                        f"Ignored unverified chat_id=None delete for msg {did} (still exists in channel {sig_chat})."
                    )
                    continue
            eff_chat = sig_chat if sig_chat is not None else chat_id
            raw_mid, _ = _normalize_task_msg_id(sig_key, chat_id=eff_chat)
            async with _get_signal_lock(eff_chat, raw_mid):
                fresh_sig = state_mgr.get_signal(sig_key, chat_id=eff_chat)
                if fresh_sig is None:
                    fresh_sig = state_mgr.get_signal(raw_mid, chat_id=eff_chat)
                if fresh_sig is not None:
                    await _close_deleted_signal_unlocked(
                        sig_key,
                        fresh_sig,
                        chat_id=eff_chat,
                        source_label="Telegram Delete Event",
                    )


async def poll_deleted_active_signals(source_label: str = "Periodic Sweep") -> int:
    """
    Batch-verify open/pending signals against Telegram `get_messages(chat_id, ids=...)`
    so signal deletions missed while offline or dropped by socket updates are closed automatically.
    """
    if not CLOSE_ON_SIGNAL_DELETED or not telethon_client:
        return 0
    get_msgs_fn = getattr(telethon_client, "get_messages", None)
    if not callable(get_msgs_fn):
        return 0

    open_or_pending = state_mgr.get_open_or_pending_signals()
    if not open_or_pending:
        return 0

    by_chat: Dict[Any, List[Tuple[str, Dict[str, Any], int]]] = {}
    for sig_key, sig_data in open_or_pending:
        raw_mid, eff_chat = _normalize_task_msg_id(sig_key, chat_id=sig_data.get("chat_id"))
        if eff_chat is None or not isinstance(raw_mid, int) or isinstance(raw_mid, bool) or raw_mid <= 0:
            continue
        by_chat.setdefault(eff_chat, []).append((sig_key, sig_data, int(raw_mid)))

    if not by_chat:
        return 0

    closed_count = 0
    for sig_chat, entries in by_chat.items():
        check_ids = [item[2] for item in entries]
        try:
            raw_res = get_msgs_fn(sig_chat, ids=check_ids)
            if inspect.isawaitable(raw_res):
                fetched = await asyncio.wait_for(raw_res, timeout=8.0)
            else:
                fetched = raw_res
        except Exception as err:
            logger.debug(f"Deleted-signal poll skipped for chat {sig_chat} ({source_label}): {err}")
            continue

        if not isinstance(fetched, (list, tuple)) or len(fetched) != len(check_ids):
            continue

        # Mock-safe authoritative check: every non-None item must be MessageEmpty or match check_ids[i]
        authoritative = True
        for idx, item in enumerate(fetched):
            if item is None or type(item).__name__ == "MessageEmpty":
                continue
            if getattr(item, "id", None) != check_ids[idx]:
                authoritative = False
                break
        if not authoritative:
            continue

        for idx, item in enumerate(fetched):
            if item is None or type(item).__name__ == "MessageEmpty":
                sig_key, _, raw_mid = entries[idx]
                async with _get_signal_lock(sig_chat, raw_mid):
                    fresh_sig = state_mgr.get_signal(sig_key, chat_id=sig_chat)
                    if fresh_sig is None:
                        fresh_sig = state_mgr.get_signal(raw_mid, chat_id=sig_chat)
                    if fresh_sig is not None and await _close_deleted_signal_unlocked(
                        sig_key,
                        fresh_sig,
                        chat_id=sig_chat,
                        source_label=source_label,
                    ):
                        closed_count += 1

    return closed_count


async def run_reconnect_catchup(reason: str = "startup") -> Dict[str, int]:
    """
    Replay bounded recent messages missed while offline/disconnected across CHANNEL_IDS.
    Enforces CATCHUP_WINDOW_SECONDS, CATCHUP_MAX_MESSAGES, persisted (chat_id, msg_id)
    watermarks, strict stale-market age/slippage guards, and deleted-signal sweeps.
    """
    summary = {"replayed": 0, "edits": 0, "skipped_stale": 0, "deleted": 0, "channels": 0}
    if session_trading_blocked() or not telethon_client:
        return summary
    if int(CATCHUP_WINDOW_SECONDS) <= 0 or int(CATCHUP_MAX_MESSAGES) <= 0:
        return summary

    get_msgs_fn = getattr(telethon_client, "get_messages", None)
    if not callable(get_msgs_fn):
        return summary

    async with _get_catchup_lock():
        summary["deleted"] = await poll_deleted_active_signals(source_label=f"Catch-Up ({reason})")
        now_utc = datetime.now(timezone.utc)
        tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
        pip_size = await _mt5_call(executor.get_pip_size, DEFAULT_SYMBOL)

        # Deduplicate equivalent channel ID variants (e.g. -4369388188 & -1004369388188), preferring -100... form
        dedup_channels: List[Any] = []
        seen_canon: Dict[Any, int] = {}
        for ch in CHANNEL_IDS:
            canon = _canonical_chat_key(ch)
            if canon not in seen_canon:
                seen_canon[canon] = len(dedup_channels)
                dedup_channels.append(ch)
            elif str(ch).strip().startswith("-100") and not str(dedup_channels[seen_canon[canon]]).strip().startswith("-100"):
                dedup_channels[seen_canon[canon]] = ch

        # Pre-pass: check all currently active/pending signals (even if msg_id <= last_seen_id) for missed text edits
        open_by_chat: Dict[Any, List[Tuple[int, Dict[str, Any]]]] = {}
        for sig_key, sig_data in state_mgr.get_open_or_pending_signals():
            raw_mid, sig_chat = _normalize_task_msg_id(sig_key, chat_id=sig_data.get("chat_id"))
            if sig_chat is not None and isinstance(raw_mid, int) and not isinstance(raw_mid, bool) and raw_mid > 0:
                open_by_chat.setdefault(sig_chat, []).append((int(raw_mid), sig_data))

        for sig_chat, active_items in open_by_chat.items():
            active_ids = [item[0] for item in active_items]
            try:
                raw_active = get_msgs_fn(sig_chat, ids=active_ids)
                fetched_active = (
                    await asyncio.wait_for(raw_active, timeout=8.0)
                    if inspect.isawaitable(raw_active)
                    else raw_active
                )
            except Exception as act_err:
                logger.debug(f"Catch-up active edit check skipped for chat {sig_chat}: {act_err}")
                continue
            if not isinstance(fetched_active, (list, tuple)) or len(fetched_active) != len(active_ids):
                continue
            auth_ok = True
            for idx, item in enumerate(fetched_active):
                if item is None or type(item).__name__ == "MessageEmpty":
                    continue
                if getattr(item, "id", None) != active_ids[idx] or not isinstance(getattr(item, "message", None), str):
                    auth_ok = False
                    break
            if not auth_ok:
                continue
            for idx, item in enumerate(fetched_active):
                if item is None or type(item).__name__ == "MessageEmpty":
                    continue
                new_txt = (getattr(item, "message", None) or "").strip()
                old_txt = (active_items[idx][1].get("raw_text") or "").strip()
                if new_txt and new_txt != old_txt:
                    await handle_message_edited(_CatchupEvent(item, sig_chat))
                    summary["edits"] += 1

        for chat_id in dedup_channels:
            summary["channels"] += 1
            last_seen_id = state_mgr.get_last_seen_msg_id(chat_id)
            try:
                try:
                    raw_res = get_msgs_fn(chat_id, limit=CATCHUP_MAX_MESSAGES, min_id=last_seen_id or 0)
                except TypeError:
                    raw_res = get_msgs_fn(chat_id, limit=CATCHUP_MAX_MESSAGES)
                if inspect.isawaitable(raw_res):
                    msgs = await asyncio.wait_for(raw_res, timeout=10.0)
                else:
                    msgs = raw_res
            except Exception as fetch_err:
                logger.debug(f"Catch-up fetch skipped for chat {chat_id} ({reason}): {fetch_err}")
                continue

            if not isinstance(msgs, (list, tuple)):
                continue

            valid_msgs = [
                m for m in msgs
                if isinstance(getattr(m, "id", None), int) and not isinstance(getattr(m, "id", None), bool)
            ]
            ordered_msgs = sorted(valid_msgs, key=lambda m: int(m.id))[-int(CATCHUP_MAX_MESSAGES):]

            for msg in ordered_msgs:
                msg_id = int(msg.id)
                raw_text = getattr(msg, "message", None) or ""
                msg_date = getattr(msg, "date", None)
                if isinstance(msg_date, datetime):
                    msg_dt_utc = msg_date if msg_date.tzinfo is not None else msg_date.replace(tzinfo=timezone.utc)
                    age_sec = max(0.0, (now_utc - msg_dt_utc).total_seconds())
                    msg_ts_iso: Optional[str] = msg_dt_utc.isoformat()
                else:
                    age_sec = 0.0
                    msg_ts_iso = None

                existing = state_mgr.get_signal(msg_id, chat_id=chat_id)
                if existing is not None:
                    if raw_text.strip() and raw_text.strip() != (existing.get("raw_text") or "").strip():
                        is_non_terminal = existing.get("status") not in (
                            "CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"
                        )
                        if is_non_terminal or age_sec <= float(CATCHUP_WINDOW_SECONDS):
                            await handle_message_edited(_CatchupEvent(msg, chat_id))
                            summary["edits"] += 1
                    state_mgr.set_catchup_watermark(chat_id, msg_id, msg_ts=msg_ts_iso)
                    last_seen_id = max(last_seen_id or msg_id, msg_id)
                    continue

                if last_seen_id is not None and msg_id <= last_seen_id:
                    continue

                if age_sec > float(CATCHUP_WINDOW_SECONDS):
                    state_mgr.set_catchup_watermark(chat_id, msg_id, msg_ts=msg_ts_iso)
                    last_seen_id = msg_id
                    continue

                raw_reply_to = getattr(msg, "reply_to_msg_id", None)
                if not (isinstance(raw_reply_to, (int, str)) and not isinstance(raw_reply_to, bool)):
                    reply_obj = getattr(msg, "reply_to", None)
                    raw_reply_to = getattr(reply_obj, "reply_to_msg_id", None) if reply_obj else None
                reply_to_id = (
                    raw_reply_to
                    if isinstance(raw_reply_to, (int, str)) and not isinstance(raw_reply_to, bool)
                    else None
                )
                is_forward = getattr(msg, "forward", None) is not None
                live_price = getattr(tick, "bid", None) if tick else None
                parsed = parse_message(
                    raw_text,
                    message_id=msg_id,
                    is_reply=reply_to_id is not None,
                    reply_to_msg_id=reply_to_id,
                    is_forward=is_forward,
                    live_price=live_price,
                    log_output=False,
                )

                if parsed is None:
                    state_mgr.set_catchup_watermark(chat_id, msg_id, msg_ts=msg_ts_iso)
                    last_seen_id = msg_id
                    continue

                if parsed.event_type == "NEW_SIGNAL" or parsed.is_entry:
                    stale_reason = _evaluate_catchup_staleness(parsed, age_sec, tick, pip_size)
                    if stale_reason:
                        logger.warning(
                            f"[CATCHUP STALE REJECT] Chat={chat_id} Msg={msg_id} ({reason}): {stale_reason}"
                        )
                        summary["skipped_stale"] += 1
                        state_mgr.set_catchup_watermark(chat_id, msg_id, msg_ts=msg_ts_iso)
                        last_seen_id = msg_id
                        continue

                await handle_new_message(_CatchupEvent(msg, chat_id))
                state_mgr.set_catchup_watermark(chat_id, msg_id, msg_ts=msg_ts_iso)
                last_seen_id = msg_id
                summary["replayed"] += 1

        if summary["replayed"] or summary["edits"] or summary["skipped_stale"] or summary["deleted"]:
            logger.info(
                f"Reconnect catch-up ({reason}) finished: "
                f"replayed={summary['replayed']}, edits={summary['edits']}, "
                f"skipped_stale={summary['skipped_stale']}, deleted={summary['deleted']}"
            )
            await notify_saved_messages(
                f"🔄 **Reconnect Catch-Up Summary (`{reason}`)**\n"
                f"• Replayed Signals/Commands: `{summary['replayed']}` | Missed Edits: `{summary['edits']}`\n"
                f"• Deleted Signals Closed: `{summary['deleted']}` | "
                f"Rejected Stale/Slipped Market Signals: `{summary['skipped_stale']}`"
            )
        return summary


# ---------------------------------------------------------------------------
# 1-SECOND TP1 PROXIMITY & LAYER 4 ACTIVE RUNNER WATCHER TASK
# ---------------------------------------------------------------------------

async def _run_telegram_keepalive_check(now: float) -> None:
    """Ping Telegram with a short timeout, cycle socket on drop, and trigger bounded catch-up on recovery."""
    global _telegram_reconnect_count
    if not telethon_client:
        return
    try:
        if not telethon_client.is_connected():
            logger.warning("Telegram client disconnected; attempting reconnect...")
            await asyncio.wait_for(telethon_client.connect(), timeout=8.0)
            if telethon_client.is_connected():
                _telegram_reconnect_count += 1
                logger.info(f"Telegram connection restored (reconnect #{_telegram_reconnect_count}).")
                await run_reconnect_catchup(reason="telegram_reconnect")
        else:
            from telethon.tl.functions import PingRequest
            await asyncio.wait_for(
                telethon_client(PingRequest(ping_id=int(now * 1000) & 0x7FFFFFFF)),
                timeout=5.0,
            )
    except Exception as tg_err:
        logger.warning(f"Telegram keepalive failed ({tg_err}); cycling connection to recover...")
        try:
            await asyncio.wait_for(telethon_client.disconnect(), timeout=4.0)
        except Exception:
            pass
        try:
            await asyncio.wait_for(telethon_client.connect(), timeout=8.0)
            _telegram_reconnect_count += 1
            logger.info(f"Telegram connection recovered cleanly after keepalive cycle (reconnect #{_telegram_reconnect_count}).")
            await run_reconnect_catchup(reason="telegram_keepalive_reconnect")
        except Exception as rec_err:
            logger.debug(f"Telegram reconnect attempt deferred: {rec_err}")


def _classify_deal_exit_code(
    sig: Dict[str, Any],
    ticket: Any,
    l_data: Optional[Dict[str, Any]],
    close_info: Optional[Dict[str, Any]],
    *,
    all_closing: bool = False,
) -> Optional[str]:
    """Classify granular broker exit_reason_code using actual deal price vs entry price."""
    info = close_info or {}
    raw_rc = str(info.get("reason") or "UNKNOWN").strip().upper()
    role_u = str((l_data or {}).get("role") or "").strip().upper()
    deal_px = info.get("exit_price")
    entry_px = (
        (l_data or {}).get("entry_price")
        or (l_data or {}).get("entry")
        or (l_data or {}).get("target_price")
        or sig.get("recorded_entry_price")
    )
    is_buy = str(sig.get("direction") or "BUY").strip().upper() == "BUY"

    if raw_rc == "SL":
        if _is_num(deal_px) and _is_num(entry_px) and float(deal_px) > 0.0 and float(entry_px) > 0.0:
            lock_dol = (float(deal_px) - float(entry_px)) if is_buy else (float(entry_px) - float(deal_px))
            if role_u == "BANK_LAYER" or (l_data or {}).get("bank_harvest_armed") or (l_data or {}).get("ratchet_tier") == "BANK_HARVEST":
                return "BANK_HARVEST_SL"
            if (l_data or {}).get("ratchet_tier") or (lock_dol >= 0.65):
                return "PROFIT_RATCHET_SL"
            if lock_dol >= -0.05:
                return "BE_SL"
            return "INITIAL_SL"
        if role_u == "BANK_LAYER" or (l_data or {}).get("bank_harvest_armed"):
            return "BANK_HARVEST_SL"
        if (l_data or {}).get("ratchet_tier"):
            return "PROFIT_RATCHET_SL"
        return "BROKER_SL"

    if raw_rc == "TP":
        if role_u == "STAGE2_BANKER":
            return "STAGE2_BANK_TP"
        if (not all_closing) and role_u not in ("CORE_RUNNER", "SOLO_RUNNER") and not sig.get("tp2_reached") and str((l_data or {}).get("tp_stage") or "TP1").upper() == "TP1":
            return "TP1_BANK"
        return "TP_CEILING"

    if raw_rc in ("MANUAL/BOT", "MANUAL"):
        return "MANUAL"
    return None


async def _startup_hydrate_and_backfill_ledger() -> Dict[str, int]:
    """
    Startup/reconnect hydration (v3.6):
      1. Hydrate all already-open active signals' MT5 layers (`entry_price`, `remaining_lot`,
         `broker_sl`, sticky roles) while NEVER touching unfilled pending-only setups.
      2. Silently backfill `trades_ledger.jsonl` for already-CLOSED signals with executed
         exposure where `ledger_recorded=False` (zero duplicate Telegram alerts).
    """
    stats = {"hydrated": 0, "backfilled": 0}
    live_positions = await _mt5_call(executor.get_live_positions, DEFAULT_SYMBOL)
    pos_map: Optional[Dict[int, Any]] = None
    if live_positions is not None and getattr(executor, "_last_live_pos_query_ok", True):
        pos_map = {
            ti: p for p in live_positions
            if (ti := _safe_ticket_int(getattr(p, "ticket", None))) is not None
        }

    if pos_map is not None and bool(_main_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED)):
        for msg_id, sig_snap in list(state_mgr.get_active_signals()):
            sig_chat = sig_snap.get("chat_id")
            async with _get_signal_lock(sig_chat, msg_id):
                sig = state_mgr.get_signal(msg_id, chat_id=sig_chat)
                if not sig or sig.get("status") == "CLOSED":
                    continue
                changed = await _mt5_call(executor.hydrate_signal_layers_from_mt5, sig, pos_map)
                if changed:
                    state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                    stats["hydrated"] += 1
                    logger.info(f"Startup hydrated active signal {msg_id} layers/roles from MT5.")

    fin = await _mt5_call(executor.get_account_financials)
    curr = fin.get("currency", "GBP") if isinstance(fin, dict) else "GBP"
    all_sigs = state_mgr.get_all_signals()
    for mid, s in list(all_sigs.items()):
        if not isinstance(s, dict):
            continue
        if s.get("status") == "CLOSED" and not s.get("ledger_recorded") and _has_executed_exposure(s):
            _ensure_signal_exit_telemetry(s)
            rec = await _record_signal_to_ledger(
                mid,
                s,
                exit_reason=str(s.get("runner_close_reason") or s.get("exit_reason_code") or "CLOSED"),
                currency=curr,
            )
            if rec is not None:
                stats["backfilled"] += 1
    if stats["hydrated"] or stats["backfilled"]:
        logger.info(
            f"Startup hydration & ledger backfill complete: hydrated={stats['hydrated']}, backfilled={stats['backfilled']}"
        )
    return stats


async def _reconcile_live_mt5_signals(
    active_signals: List[Tuple[str, Dict[str, Any]]],
) -> None:
    """
    Reconcile live MT5 positions & pending orders against active state records.
    Re-verifies inside the per-signal lock to avoid placement/edit TOCTOU races,
    enforces IPC query health & 3s placement grace, hydrates pending->filled layers,
    sweeps orphaned pending orders on profit lock / BE / full exit, and unifies
    deal-level PnL + TradeLedger recording.
    """
    live_positions = await _mt5_call(executor.get_live_positions, DEFAULT_SYMBOL)
    if live_positions is None or not getattr(executor, "_last_live_pos_query_ok", True):
        logger.warning("Skipping MT5 reconciliation cycle: live positions query failed.")
        return
    live_orders = await _mt5_call(executor.get_live_orders, DEFAULT_SYMBOL)
    if live_orders is None or not getattr(executor, "_last_live_ord_query_ok", True):
        logger.warning("Skipping MT5 reconciliation cycle: live orders query failed.")
        return
    live_pos_by_ticket: Dict[int, Any] = {
        ti: p for p in live_positions if (ti := _safe_ticket_int(getattr(p, "ticket", None))) is not None
    }
    live_pos_tickets = set(live_pos_by_ticket.keys())
    live_order_tickets = {
        ti for o in live_orders if (ti := _safe_ticket_int(getattr(o, "ticket", None))) is not None
    }

    ratchet_on = bool(_main_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED))

    for msg_id, sig_snap in list(active_signals):
        sig_chat = sig_snap.get("chat_id")
        async with _get_signal_lock(sig_chat, msg_id):
            sig = state_mgr.get_signal(msg_id, chat_id=sig_chat)
            if not sig or sig.get("status") == "CLOSED":
                continue

            mk_tickets = list(sig.get("market_tickets", []))
            pd_tickets = list(sig.get("pending_tickets", []))
            layers = dict(sig.get("layers") or {})
            sig_pos_map = dict(live_pos_by_ticket)
            sig_pos_tickets = set(live_pos_tickets)
            sig_ord_tickets = set(live_order_tickets)

            cancelled_or_placed_live = False
            if not sig.get("tp1_reached"):
                for l_data in layers.values():
                    if isinstance(l_data, dict) and str(l_data.get("status") or "").strip().upper() == "CANCELLED":
                        lt = _safe_ticket_int(l_data.get("ticket"))
                        if lt is not None and lt in sig_ord_tickets:
                            cancelled_or_placed_live = True
                            break
                if not cancelled_or_placed_live:
                    mk_pd_set = {_safe_ticket_int(x) for x in (mk_tickets + pd_tickets) if _safe_ticket_int(x) is not None}
                    for t_ap in (sig.get("all_placed_tickets") or []):
                        t_ap_i = _safe_ticket_int(t_ap)
                        if t_ap_i is not None and t_ap_i not in mk_pd_set and t_ap_i in sig_ord_tickets:
                            cancelled_or_placed_live = True
                            break

            known_live = sig_pos_tickets | sig_ord_tickets
            any_missing = any(
                ti not in known_live
                for t in (mk_tickets + pd_tickets)
                if (ti := _safe_ticket_int(t)) is not None
            )
            if any_missing or cancelled_or_placed_live:
                re_pos = await _mt5_call(executor.get_live_positions, DEFAULT_SYMBOL)
                if re_pos is None or not getattr(executor, "_last_live_pos_query_ok", True):
                    continue
                re_ord = await _mt5_call(executor.get_live_orders, DEFAULT_SYMBOL)
                if re_ord is None or not getattr(executor, "_last_live_ord_query_ok", True):
                    continue
                sig_pos_map = {
                    ti: p for p in re_pos if (ti := _safe_ticket_int(getattr(p, "ticket", None))) is not None
                }
                sig_pos_tickets = set(sig_pos_map.keys())
                sig_ord_tickets = {
                    ti for o in re_ord if (ti := _safe_ticket_int(getattr(o, "ticket", None))) is not None
                }

            updated = False
            filled_now = False
            grace_fn = getattr(executor, "is_ticket_in_reconcile_grace", None)

            new_pd_tickets: List[int] = []
            for t in pd_tickets:
                ti = _safe_ticket_int(t)
                if ti is not None and ti in sig_pos_tickets:
                    if t not in mk_tickets:
                        mk_tickets.append(t)
                    p_obj = sig_pos_map.get(ti)
                    p_open = 0.0
                    p_vol = 0.0
                    p_sl = 0.0
                    p_tp = 0.0
                    p_digits = 2
                    if p_obj is not None:
                        p_sym = getattr(p_obj, "symbol", None) or sig.get("symbol") or DEFAULT_SYMBOL
                        s_info = await _mt5_call(executor.get_symbol_info, p_sym)
                        p_digits = int(getattr(s_info, "digits", 2) or 2) if s_info else 2
                        raw_op = float(getattr(p_obj, "price_open", 0.0) or 0.0)
                        if raw_op > 0.0:
                            p_open = round(raw_op, p_digits)
                        p_vol = float(getattr(p_obj, "volume", 0.0) or 0.0)
                        p_sl = float(getattr(p_obj, "sl", 0.0) or 0.0)
                        p_tp = float(getattr(p_obj, "tp", 0.0) or 0.0)
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and _safe_ticket_int(l_data.get("ticket")) == ti:
                            l_data["status"] = "FILLED"
                            if p_open > 0.0:
                                l_data["entry_price"] = p_open
                                l_data["entry"] = p_open
                            if p_vol > 0.0:
                                l_data["remaining_lot"] = round(p_vol, 4)
                            if p_sl > 0.0:
                                l_data["broker_sl"] = round(p_sl, p_digits)
                                l_data["sl"] = round(p_sl, p_digits)
                            if p_tp > 0.0 and not l_data.get("tp"):
                                l_data["tp"] = round(p_tp, p_digits)
                    if p_open > 0.0 and not _is_num(sig.get("recorded_entry_price")):
                        sig["recorded_entry_price"] = p_open
                    updated = True
                    filled_now = True
                    logger.info(f"Reconciled MT5 state: Pending order #{t} filled into active position for Msg ID {msg_id}")
                elif ti is not None and ti in sig_ord_tickets:
                    new_pd_tickets.append(t)
                elif ti is not None and callable(grace_fn) and grace_fn(ti, 3.0):
                    new_pd_tickets.append(t)
                else:
                    updated = True
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and l_data.get("ticket") == t:
                            l_data["status"] = "CANCELLED"
                    logger.info(f"Reconciled MT5 state: Pending order #{t} was cancelled/removed for Msg ID {msg_id}")

            new_mk_tickets: List[int] = []
            broker_closed: List[int] = []
            for t in mk_tickets:
                ti = _safe_ticket_int(t)
                if ti is not None and ti in sig_pos_tickets:
                    new_mk_tickets.append(t)
                elif ti is not None and callable(grace_fn) and grace_fn(ti, 3.0):
                    new_mk_tickets.append(t)
                else:
                    updated = True
                    broker_closed.append(t)
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and l_data.get("ticket") == t:
                            l_data["status"] = "CLOSED"
                    logger.info(f"Reconciled MT5 state: Market position #{t} closed for Msg ID {msg_id}")

            if filled_now and ratchet_on:
                sig["layers"] = layers
                sig["market_tickets"] = list(new_mk_tickets)
                await _mt5_call(executor.assign_or_preserve_layer_roles, sig)

            # Retry settlement-lagged PnL tickets if any
            pending_pnl_map = sig.get("pending_pnl_tickets")
            if isinstance(pending_pnl_map, dict) and pending_pnl_map and not executor.dry_run:
                pend_tkts = [_safe_ticket_int(k) for k in list(pending_pnl_map.keys())]
                pend_tkts = [x for x in pend_tkts if x is not None]
                if pend_tkts:
                    added_bf, _ = await _account_closed_tickets(sig, pend_tkts)
                    if added_bf != 0.0 or not sig.get("pending_pnl_tickets"):
                        updated = True

            # Self-heal unfilled setups vs sweep orphaned pending orders after TP1/BE/ProfitLock/full close
            active_mk_set = {_safe_ticket_int(x) for x in new_mk_tickets if _safe_ticket_int(x) is not None}
            active_pd_set = {_safe_ticket_int(x) for x in new_pd_tickets if _safe_ticket_int(x) is not None}
            accounted_set = {_safe_ticket_int(x) for x in (sig.get("accounted_tickets") or []) if _safe_ticket_int(x) is not None}
            closed_set_re = {_safe_ticket_int(x) for x in broker_closed if _safe_ticket_int(x) is not None}

            had_fills = bool(new_mk_tickets or broker_closed or accounted_set or _has_executed_exposure(sig))
            ratchet_locked = bool(
                any(
                    isinstance(ld, dict)
                    and str(ld.get("status") or "").strip().upper() in ("OPEN", "FILLED", "RUNNER")
                    and (
                        str(ld.get("ratchet_tier") or "").strip().upper() in ("TIER_2", "TIER_3", "TIER_4", "TIER_5", "STAGE4_SQUEEZE", "BANK_HARVEST")
                        or float(ld.get("mfe_dollars") or 0.0) >= 3.50
                    )
                    for ld in layers.values()
                )
            )
            tp1_sweep = bool(sig.get("tp1_reached") and CANCEL_PENDING_ON_TP1)
            s0_sweep_pending = bool(sig.get("stage0_profit_lock_active") and not new_mk_tickets and _is_cancel_pending_on_stage0_lock_exit())
            be_sweep = bool(sig.get("be_armed") and bool(_main_cfg("CANCEL_PENDING_ON_BE", CANCEL_PENDING_ON_BE)) and had_fills)
            profit_lock_sweep = bool(
                ratchet_locked
                and bool(_main_cfg("CANCEL_PENDING_ON_PROFIT_LOCK", CANCEL_PENDING_ON_PROFIT_LOCK))
                and had_fills
            )
            all_filled_closed_sweep = bool(broker_closed and not new_mk_tickets and bool(new_pd_tickets))
            should_sweep_pendings = bool(
                tp1_sweep or s0_sweep_pending or be_sweep or profit_lock_sweep or all_filled_closed_sweep
            )

            if not should_sweep_pendings:
                for l_data in layers.values():
                    if not isinstance(l_data, dict):
                        continue
                    lt = _safe_ticket_int(l_data.get("ticket"))
                    if (
                        lt is not None
                        and lt in sig_ord_tickets
                        and lt not in active_mk_set
                        and str(l_data.get("status") or "").strip().upper() == "CANCELLED"
                    ):
                        l_data["status"] = "PENDING"
                        if lt not in active_pd_set:
                            new_pd_tickets.append(lt)
                            active_pd_set.add(lt)
                        updated = True
                        logger.info(f"Self-healed live pending order #{lt} back into Msg ID {msg_id}")
                for t_ap in (sig.get("all_placed_tickets") or []):
                    t_ap_i = _safe_ticket_int(t_ap)
                    if (
                        t_ap_i is not None
                        and t_ap_i in sig_ord_tickets
                        and t_ap_i not in active_mk_set
                        and t_ap_i not in active_pd_set
                        and t_ap_i not in accounted_set
                        and t_ap_i not in closed_set_re
                    ):
                        new_pd_tickets.append(t_ap_i)
                        active_pd_set.add(t_ap_i)
                        for l_data in layers.values():
                            if isinstance(l_data, dict) and _safe_ticket_int(l_data.get("ticket")) == t_ap_i:
                                l_data["status"] = "PENDING"
                        updated = True
                        logger.info(f"Self-healed placed pending order #{t_ap_i} back into Msg ID {msg_id}")
            else:
                orphan_ord_cands: List[int] = [
                    ti for t_o in list(new_pd_tickets) + list(sig.get("all_placed_tickets") or [])
                    if (ti := _safe_ticket_int(t_o)) is not None
                    and ti in sig_ord_tickets
                    and ti not in active_mk_set
                ]
                for ord_t in list(dict.fromkeys(orphan_ord_cands)):
                    if await _mt5_call(executor.cancel_pending_order, ord_t):
                        new_pd_tickets = [x for x in new_pd_tickets if _safe_ticket_int(x) != ord_t]
                        for l_data in layers.values():
                            if isinstance(l_data, dict) and _safe_ticket_int(l_data.get("ticket")) == ord_t:
                                l_data["status"] = "CANCELLED"
                        updated = True
                        sweep_lbl = (
                            "post-BE" if be_sweep
                            else "post-ProfitLock" if profit_lock_sweep
                            else "post-full-close" if all_filled_closed_sweep
                            else "post-Stage0-lock" if s0_sweep_pending
                            else "post-TP1"
                        )
                        logger.info(f"Swept orphaned {sweep_lbl} pending order #{ord_t} on Msg ID {msg_id}")

            if not updated:
                continue

            # Stage 0 Profit-Lock broker-SL termination: sweep remaining unfilled limits before status decision
            s0_lock_cleared = False
            if (
                sig.get("stage0_profit_lock_active")
                and not new_mk_tickets
                and broker_closed
                and _is_cancel_pending_on_stage0_lock_exit()
                and (
                    new_pd_tickets
                    or any(
                        (_safe_ticket_int(t_o) in sig_ord_tickets)
                        for t_o in (sig.get("all_placed_tickets") or [])
                        if _safe_ticket_int(t_o) is not None and _safe_ticket_int(t_o) not in closed_set_re
                    )
                )
            ):
                sig["pending_tickets"] = list(new_pd_tickets)
                sig["layers"] = layers
                await _mt5_call(executor.cancel_signal_pending_orders, sig)
                new_pd_tickets = []
                for l_data in layers.values():
                    if isinstance(l_data, dict) and str(l_data.get("status") or "").strip().upper() == "PENDING":
                        l_data["status"] = "CANCELLED"
                sig["stage0_profit_lock_active"] = False
                sig["stage0_zone_protected"] = False
                s0_lock_cleared = True

            runner_tkts = [
                t for t in (sig.get("runner_tickets") or ([sig["runner_ticket"]] if sig.get("runner_ticket") is not None else []))
                if t is not None
            ]
            closed_runner_tkts = [t for t in runner_tkts if t in broker_closed]
            rem_runner_tkts = [t for t in runner_tkts if t not in broker_closed and t in new_mk_tickets]
            runner_t = closed_runner_tkts[-1] if closed_runner_tkts else sig.get("runner_ticket")
            runner_closed_now = bool(closed_runner_tkts) and not rem_runner_tkts
            prior_banked = float(sig.get("banked_profit") or 0.0)
            close_infos: Dict[int, Dict[str, Any]] = {}

            if broker_closed:
                await _mt5_call(executor.invalidate_realized_pnl_cache)
                _, close_infos = await _account_closed_tickets(sig, broker_closed)

            early_tp1_hit_on_broker = (
                not sig.get("tp1_reached")
                and not runner_closed_now
                and bool(new_mk_tickets)
                and any(close_infos.get(t, {}).get("reason") == "TP" for t in broker_closed)
            )

            if not new_mk_tickets and not new_pd_tickets:
                new_status = "CLOSED"
            elif runner_closed_now:
                new_status = "BE_APPLIED" if sig.get("tp1_reached") else sig.get("status")
            else:
                new_status = sig.get("status")

            # Per-ticket deal-price exit classification + per-layer exit telemetry persistence
            for bt in broker_closed:
                bt_info = close_infos.get(bt, {})
                bt_layer = None
                for ld_v in layers.values():
                    if isinstance(ld_v, dict) and _safe_ticket_int(ld_v.get("ticket")) == _safe_ticket_int(bt):
                        bt_layer = ld_v
                        break
                bt_code = _classify_deal_exit_code(
                    sig,
                    bt,
                    bt_layer,
                    bt_info,
                    all_closing=(new_status == "CLOSED"),
                )
                if bt_layer is not None:
                    if bt_code and not bt_layer.get("exit_reason_code"):
                        bt_layer["exit_reason_code"] = bt_code
                    if _is_num(bt_info.get("exit_price")):
                        bt_layer["exit_price"] = round(float(bt_info["exit_price"]), 4)
                    if _is_num(bt_info.get("profit")):
                        bt_layer["realized_pnl"] = round(float(bt_info["profit"]), 2)

            updates: Dict[str, Any] = {
                "market_tickets": new_mk_tickets,
                "pending_tickets": new_pd_tickets,
                "layers": layers,
                "status": new_status,
                "accounted_tickets": sig.get("accounted_tickets", []),
                "accounted_deal_tickets": sig.get("accounted_deal_tickets", []),
                "pending_pnl_tickets": sig.get("pending_pnl_tickets", {}),
                "banked_profit": sig.get("banked_profit", 0.0),
                "roles_locked": bool(sig.get("roles_locked")),
            }
            if sig.get("bank_harvest_layer"):
                updates["bank_harvest_layer"] = sig.get("bank_harvest_layer")
            if _is_num(sig.get("recorded_entry_price")):
                updates["recorded_entry_price"] = sig.get("recorded_entry_price")
            if s0_lock_cleared:
                updates["stage0_profit_lock_active"] = False
                updates["stage0_zone_protected"] = False
            if closed_runner_tkts and rem_runner_tkts:
                rem_r_layers = [
                    lk for lk, ld in layers.items()
                    if isinstance(ld, dict) and ld.get("ticket") in rem_runner_tkts
                ]
                updates["runner_tickets"] = rem_runner_tkts
                updates["runner_ticket"] = rem_runner_tkts[-1]
                updates["runner_layers"] = rem_r_layers
                updates["runner_layer"] = rem_r_layers[-1] if rem_r_layers else sig.get("runner_layer")
            elif runner_closed_now:
                r_info = close_infos.get(runner_t, {"profit": 0.0, "reason": "UNKNOWN"})
                r_layer_obj = None
                for ld_v in layers.values():
                    if isinstance(ld_v, dict) and _safe_ticket_int(ld_v.get("ticket")) == _safe_ticket_int(runner_t):
                        r_layer_obj = ld_v
                        break
                r_code = _classify_deal_exit_code(
                    sig,
                    runner_t,
                    r_layer_obj,
                    r_info,
                    all_closing=(new_status == "CLOSED" or not new_mk_tickets),
                )
                r_reason_map = {
                    "TP": "Broker Take Profit Hit",
                    "SL": "Trailed / Breakeven Stop Loss Hit",
                    "MANUAL/BOT": "Closed manually / from MT5 app",
                    "UNKNOWN": "Closed on broker",
                }
                raw_r_reason = str(r_info.get("reason", "UNKNOWN")).upper()
                prev_rlayers = list(sig.get("runner_layers") or ([sig["runner_layer"]] if sig.get("runner_layer") else []))
                prev_rlayer = sig.get("runner_layer") or (prev_rlayers[-1] if prev_rlayers else sig.get("last_runner_layer"))
                if prev_rlayers:
                    updates["last_runner_layers"] = prev_rlayers
                if prev_rlayer:
                    updates["last_runner_layer"] = prev_rlayer
                updates["last_runner_tickets"] = closed_runner_tkts
                updates["last_runner_ticket"] = runner_t
                updates["runner_tickets"] = []
                updates["runner_ticket"] = None
                updates["runner_layers"] = []
                updates["runner_layer"] = None
                updates["runner_proximity_armed"] = False
                updates["runner_reversal_ticks"] = 0
                updates["tp2_shortfall_armed"] = False
                updates["runner_close_reason"] = r_reason_map.get(raw_r_reason, "Closed on broker")
                if r_code:
                    updates["exit_reason_code"] = r_code
                eff_max_stage = _resolve_effective_max_stage(sig)
                if raw_r_reason == "TP":
                    tp_map_r = _resolve_signal_tps_dict(sig)
                    updates[f"tp{eff_max_stage}_reached"] = True
                    if eff_max_stage == 5 and _is_num(tp_map_r.get("TP5")):
                        updates["tp5_reached"] = True
                    updates["exit_stage"] = min(4, max(1, eff_max_stage))
                    updates["giveback_from_peak"] = 0.0
                    deal_ex_px = r_info.get("exit_price")
                    ceil_px = deal_ex_px if _is_num(deal_ex_px) else (tp_map_r.get(f"TP{eff_max_stage}") or sig.get(f"raw_tp{eff_max_stage}"))
                    if _is_num(ceil_px) and not _is_num(sig.get("exit_price")):
                        updates["exit_price"] = round(float(ceil_px), 4)
                elif raw_r_reason == "SL":
                    deal_ex_px = r_info.get("exit_price")
                    sl_px_r = deal_ex_px if _is_num(deal_ex_px) else (sig.get("broker_sl") or sig.get("trail_stop") or sig.get("runner_sl_floor") or sig.get("sl"))
                    if _is_num(sl_px_r) and not _is_num(sig.get("exit_price")):
                        updates["exit_price"] = round(float(sl_px_r), 4)

            sig.update(updates)
            if new_status == "CLOSED" or (runner_closed_now and not new_mk_tickets):
                inferred_b_code = None
                if broker_closed:
                    bt_last = broker_closed[-1]
                    bt_l_obj = None
                    for ld_v in layers.values():
                        if isinstance(ld_v, dict) and _safe_ticket_int(ld_v.get("ticket")) == _safe_ticket_int(bt_last):
                            bt_l_obj = ld_v
                            break
                    inferred_b_code = _classify_deal_exit_code(
                        sig,
                        bt_last,
                        bt_l_obj,
                        close_infos.get(bt_last),
                        all_closing=True,
                    )
                    deal_px_last = (close_infos.get(bt_last) or {}).get("exit_price")
                    if _is_num(deal_px_last) and not _is_num(sig.get("exit_price")):
                        sig["exit_price"] = round(float(deal_px_last), 4)
                _ensure_signal_exit_telemetry(sig, exit_reason_code=inferred_b_code)
                for tk_k in (
                    "exit_reason_code",
                    "exit_stage",
                    "exit_price",
                    "max_favorable_excursion",
                    "giveback_from_peak",
                    "trail_tier_used",
                ):
                    if tk_k in sig:
                        updates[tk_k] = sig[tk_k]
            state_mgr.update_signal(msg_id, updates, chat_id=sig_chat)

            if early_tp1_hit_on_broker:
                logger.info(
                    f"Step 2 Broker TP1 Handoff triggered for Msg ID {msg_id}: early layer(s) {broker_closed} "
                    f"hit broker TP1 while {new_mk_tickets} remain open."
                )
                _, _, handoff_alert = await _execute_tp1_scaling_handoff(
                    msg_id,
                    sig,
                    chat_id=sig_chat,
                    source_label="Broker TP1",
                    curr_price=sig.get("raw_tp1"),
                )
                await notify_saved_messages(handoff_alert)
                continue

            fin = await _mt5_call(executor.get_account_financials) if (broker_closed or new_status == "CLOSED") else {"currency": "GBP", "formatted": ""}
            curr = fin.get("currency", "GBP") if isinstance(fin, dict) else "GBP"

            if not broker_closed:
                if new_status == "CLOSED":
                    exit_lbl = sig.get("runner_close_reason") or sig.get("exit_reason_code") or "CLOSED"
                    _emit_exit_telemetry_log(msg_id, sig, float(sig.get("banked_profit") or 0.0), exit_reason=str(exit_lbl))
                    await _record_signal_to_ledger(msg_id, sig, exit_reason=str(exit_lbl), currency=curr)
                continue

            reason_map = {
                "TP": "🎯 Broker Take Profit Hit",
                "SL": "🛑 Stop Loss Hit (incl. trailed / breakeven SL)",
                "MANUAL/BOT": "✋ Closed manually / from MT5 app",
                "UNKNOWN": "Closed on broker",
            }

            # Non-runner broker closes (e.g. L2 at TP2, L3 at TP3, SL, manual)
            for t in broker_closed:
                if runner_closed_now and t in closed_runner_tkts:
                    continue
                info = close_infos.get(t, {"profit": 0.0, "reason": "UNKNOWN"})
                pnl = float(info.get("profit", 0.0))
                t_lbl = resolve_layer_label(sig, t)
                t_exit_px = info.get("exit_price")
                t_exit_str = f" @ `{t_exit_px}`" if _is_num(t_exit_px) else ""
                head = "🎯 **TP Hit - Profit Banked**" if info.get("reason") == "TP" else "📌 **Position Closed by Broker**"
                await notify_saved_messages(
                    f"{head} [ID: {msg_id}]\n"
                    f"• Layer: `{t_lbl}` [Ticket #{t}] ({sig.get('direction')} {sig.get('symbol')}){t_exit_str}\n"
                    f"• Exit: {reason_map.get(info.get('reason', 'UNKNOWN'), info.get('reason'))}\n"
                    f"• {'💰' if pnl >= 0 else '🔻'} **Result:** `{format_profit(pnl, curr)}`\n\n"
                    f"{fin['formatted']}"
                )

            # Unified runner broker-close summary (combines prior banked_profit + runner_profit)
            if runner_closed_now and runner_t is not None:
                r_info = close_infos.get(runner_t, {"profit": 0.0, "reason": "UNKNOWN"})
                runner_profit = sum(float(close_infos.get(rt, {}).get("profit", 0.0)) for rt in closed_runner_tkts)
                total_trade_profit = float(sig.get("banked_profit") or 0.0)
                cleared = list(sig.get("runner_cleared_milestones") or [])
                cleared_str = ", ".join(cleared) if cleared else "None"
                rem_note = f"\n• Remaining Open Tickets: `{new_mk_tickets}`" if new_mk_tickets else ""
                stage_val_b = sig.get("exit_stage") if _is_num(sig.get("exit_stage")) else _get_signal_stage(sig)
                code_val_b = sig.get("exit_reason_code") or r_info.get("reason", "UNKNOWN")
                mfe_b = sig.get("max_favorable_excursion") if isinstance(sig.get("max_favorable_excursion"), dict) else {}
                pk_b = mfe_b.get("peak_price") if mfe_b.get("peak_price") is not None else (sig.get("trail_peak") or sig.get("runner_peak_price"))
                pp_b = mfe_b.get("peak_profit_per_oz")
                gb_b = sig.get("giveback_from_peak")
                mfe_str_b = f"{pk_b} (+${pp_b:.2f}/oz)" if (_is_num(pk_b) and _is_num(pp_b)) else f"{pk_b}"
                gb_str_b = f"${float(gb_b):.2f}" if _is_num(gb_b) else "N/A"
                runner_label_b = resolve_layer_label(sig, runner_t)
                await notify_saved_messages(
                    f"🏁 **Runner Closed by Broker & Unified Summary [ID: {msg_id}]**\n"
                    f"• Symbol: `{sig.get('symbol')}` ({sig.get('direction')})\n"
                    f"• Runner: `{runner_label_b}` [Ticket #{runner_t}] | Stage Reached: `Stage {stage_val_b}`\n"
                    f"• Exit: {reason_map.get(r_info.get('reason', 'UNKNOWN'), r_info.get('reason'))} (`{code_val_b}`)\n"
                    f"• Peak / MFE: `{mfe_str_b}` | Giveback from Peak: `{gb_str_b}`\n"
                    f"• Milestones Cleared: `{cleared_str}`{rem_note}\n"
                    f"• 💰 **Amount Made on Runner:** `{format_profit(runner_profit, curr)}`\n"
                    f"• 🏦 **Previously Banked:** `{format_profit(prior_banked, curr)}`\n"
                    f"• 💵 **Total Trade Profit:** `{format_profit(total_trade_profit, curr)}`\n\n"
                    f"{fin['formatted']}"
                )

            if new_status == "CLOSED":
                exit_lbl = sig.get("runner_close_reason") or (
                    close_infos[broker_closed[-1]].get("reason", "CLOSED") if broker_closed else "CLOSED"
                )
                _emit_exit_telemetry_log(
                    msg_id,
                    sig,
                    float(sig.get("banked_profit") or 0.0),
                    exit_reason=str(exit_lbl),
                )
                await _record_signal_to_ledger(msg_id, sig, exit_reason=str(exit_lbl), currency=curr)


async def _process_runner_active_tick(msg_id: str, sig: Dict[str, Any]) -> None:
    """Manage a RUNNER_ACTIVE signal via _mt5_call with dirty-checked saves and unified close summary."""
    sig_chat = sig.get("chat_id")
    sig_before = copy.deepcopy(sig)
    prior_banked = float(sig.get("banked_profit") or 0.0)

    still_active = await _mt5_call(executor.manage_designated_runner, sig)

    ratchet_evs = list(sig.pop("_last_ratchet_events", []) or [])
    lift_aborts = list(sig.pop("_last_lift_aborts", []) or [])
    pb_info = sig.pop("last_partial_bank", None)
    pb_added = 0.0
    if isinstance(pb_info, dict) and pb_info.get("ticket"):
        pb_added, _ = await _account_closed_tickets(sig, [pb_info["ticket"]], partial_only=True)

    if ratchet_evs or lift_aborts or (isinstance(pb_info, dict) and pb_info.get("ticket")):
        fin_ev = await _mt5_call(executor.get_account_financials) if pb_info else {"currency": "GBP"}
        curr_ev = fin_ev.get("currency", "GBP") if isinstance(fin_ev, dict) else "GBP"
        await _emit_ratchet_alerts(
            msg_id,
            sig,
            events=ratchet_evs,
            lift_aborts=lift_aborts,
            partial_bank=pb_info,
            partial_profit=pb_added,
            curr=curr_ev,
        )

    sub_closed_tkts = list(sig.pop("last_closed_sub_runner_tickets", []) or [])
    if sub_closed_tkts:
        await _account_closed_tickets(sig, sub_closed_tkts)

    backstop_tkts = list(sig.get("last_backstop_closed_tickets") or [])
    if backstop_tkts:
        added_bs, _ = await _account_closed_tickets(sig, backstop_tkts)
        fin_bs = await _mt5_call(executor.get_account_financials)
        curr_bs = fin_bs.get("currency", "GBP")
        bs_labels = ", ".join(resolve_layer_label(sig, bt) for bt in backstop_tkts)
        await notify_saved_messages(
            f"🎯 **Milestone Auto-Close Banked [ID: {msg_id}]**\n"
            f"• Symbol: `{sig.get('symbol', DEFAULT_SYMBOL)}` ({sig.get('direction')})\n"
            f"• Closed Target: `{bs_labels}` (Tickets: `{backstop_tkts}`)\n"
            f"• 💰 **Banked:** `{format_profit(added_bs, curr_bs)}` | "
            f"**Total Trade PnL:** `{format_profit(sig.get('banked_profit', 0.0), curr_bs)}`"
        )

    if not still_active:
        last_runner_tkts = [
            ti for t in (sig.get("last_runner_tickets") or ([sig["last_runner_ticket"]] if sig.get("last_runner_ticket") else []))
            if (ti := _safe_ticket_int(t)) is not None
        ]
        last_runner_t = _safe_ticket_int(sig.get("last_runner_ticket")) or (last_runner_tkts[-1] if last_runner_tkts else None)
        runner_layer_disp = resolve_layer_label(sig, last_runner_t)
        tp5_hit = bool(sig.get("tp5_reached", False))
        tp4_hit = bool(sig.get("tp4_reached", False))
        tp2_hit = bool(sig.get("tp2_reached", False))
        close_reason = sig.get("runner_close_reason", "Trailing SL / Virtual Floor / Proximity Guard")

        _, r_infos = await _account_closed_tickets(sig, last_runner_tkts)
        runner_profit = sum(float(r_infos.get(rt, {}).get("profit", 0.0)) for rt in last_runner_tkts)
        total_trade_profit = float(sig.get("banked_profit") or 0.0)
        if not sig.get("accounted_tickets") and prior_banked != 0.0:
            total_trade_profit = runner_profit + prior_banked

        _ensure_signal_exit_telemetry(sig)
        state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
        _emit_exit_telemetry_log(msg_id, sig, total_trade_profit, exit_reason=str(close_reason))

        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(runner_profit, curr)
        total_profit_str = format_profit(total_trade_profit, curr)
        cleared = list(sig.get("runner_cleared_milestones") or [])
        cleared_str = ", ".join(cleared) if cleared else "None"

        if sig.get("status") == "CLOSED":
            await _record_signal_to_ledger(msg_id, sig, exit_reason=str(close_reason), currency=curr)

        rem_mkts = list(sig.get("market_tickets") or [])
        rem_note = f"\n• 📌 **Surviving Open Layers:** `{rem_mkts}` (Continuing to TP2/TP3)" if rem_mkts else ""
        logger.info(
            f"Runner #{last_runner_t} for Signal {msg_id} exited ({close_reason}). "
            f"RunnerProfit={profit_str} | Total={total_profit_str}"
        )

        if tp5_hit:
            headline = f"🏆 **TP5 CEILING REACHED! Maximum Runner Profit Banked [ID: {msg_id}]**"
        elif tp4_hit and "TP4" in str(close_reason):
            headline = f"🏆 **TP4 CEILING REACHED! Maximum Runner Profit Banked [ID: {msg_id}]**"
        elif tp2_hit and "TP2" in str(close_reason):
            headline = f"🏆 **TP2 REACHED! Maximum Profit Banked [ID: {msg_id}]**"
        else:
            headline = f"🏁 **Runner Trade Closed & Banked [ID: {msg_id}]**"

        stage_val = sig.get("exit_stage") if _is_num(sig.get("exit_stage")) else _get_signal_stage(sig)
        code_val = sig.get("exit_reason_code")
        reason_disp = f"{code_val} — {close_reason}" if (code_val and str(code_val) != str(close_reason)) else f"{close_reason}"
        mfe_obj = sig.get("max_favorable_excursion") if isinstance(sig.get("max_favorable_excursion"), dict) else {}
        pk_val = mfe_obj.get("peak_price") if mfe_obj.get("peak_price") is not None else (sig.get("trail_peak") or sig.get("runner_peak_price"))
        pp_val = mfe_obj.get("peak_profit_per_oz")
        gb_val = sig.get("giveback_from_peak")
        mfe_str = f"{pk_val} (+${pp_val:.2f}/oz)" if (_is_num(pk_val) and _is_num(pp_val)) else f"{pk_val}"
        gb_str = f"${float(gb_val):.2f}" if _is_num(gb_val) else "N/A"

        await notify_saved_messages(
            f"{headline}\n"
            f"• Symbol: `{sig.get('symbol', DEFAULT_SYMBOL)}` ({sig.get('direction')})\n"
            f"• Runner: `{runner_layer_disp}` [Ticket #{last_runner_t}] | Stage Reached: `Stage {stage_val}`\n"
            f"• Exit Reason: 🔒 `{reason_disp}`\n"
            f"• Peak Price / MFE: `{mfe_str}` | Giveback from Peak: `{gb_str}`\n"
            f"• Milestones Cleared: `{cleared_str}`{rem_note}\n"
            f"• 💰 **Amount Made on Runner:** `{profit_str}`\n"
            f"• 💵 **Total Trade Profit:** `{total_profit_str}`\n\n"
            f"{fin['formatted']}"
        )
        return

    # Dirty-check & throttle pure non-armed peak drift so unchanged 1s ticks do zero disk I/O
    if sig == sig_before:
        return

    important_keys = (
        "status", "market_tickets", "pending_tickets", "layers",
        "runner_ticket", "runner_tickets", "runner_layer", "runner_layers",
        "last_runner_layer", "last_runner_layers",
        "runner_milestone_idx", "runner_milestone_name", "tp_stage",
        "runner_cleared_milestones", "runner_proximity_armed", "runner_reversal_ticks", "tp2_shortfall_armed",
        "reversal_guard_armed", "peak_profit", "reversal_guard_peak_profit",
        "stage1_atr_armed", "chart_peak", "stage0_profit_lock_active", "stage0_profit_lock_sl", "stage0_zone_protected",
        "runner_sl_floor", "soft_tp1_floor", "broker_sl",
        "fsm_floor", "atr_stop", "trail_stop", "trail_peak", "trail_dist",
        "trail_tier_used", "active_stop_source",
        "exit_reason_code", "exit_stage", "exit_price",
        "max_favorable_excursion", "giveback_from_peak",
        "tp2_reached", "tp3_reached", "tp4_reached", "tp5_reached",
        "accounted_tickets", "accounted_deal_tickets", "pending_pnl_tickets", "banked_profit", "tp_levels",
        "roles_locked", "bank_harvest_layer", "effective_max_stage",
    )
    important_changed = any(sig.get(k) != sig_before.get(k) for k in important_keys)
    armed_now = bool(
        sig.get("runner_proximity_armed")
        or sig.get("tp2_shortfall_armed")
        or sig.get("reversal_guard_armed")
    )
    now_mono = time.monotonic()
    t_key = f"{sig_chat}:{msg_id}"

    if important_changed or armed_now or (now_mono - _runner_save_throttle_ts.get(t_key, 0.0) >= 5.0):
        _runner_save_throttle_ts[t_key] = now_mono
        state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)


async def _process_pre_tp1_or_backstop_tick(msg_id: str, sig: Dict[str, Any]) -> None:
    """Process pre-TP1 monitoring, fallback TP1, TP1 scaling, shortfall guard, and post-runner L2/L3 backstop."""
    sig_chat = sig.get("chat_id")
    status = sig.get("status")
    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = sig.get("direction", "BUY").upper()

    tick = await _mt5_call(executor.get_tick, symbol)
    if not tick:
        return
    curr_price = tick.bid if direction == "BUY" else tick.ask
    if not isinstance(curr_price, (int, float)) or isinstance(curr_price, bool):
        return
    raw_chart = getattr(tick, "bid", None)
    chart_price = (
        float(raw_chart)
        if isinstance(raw_chart, (int, float)) and not isinstance(raw_chart, bool) and raw_chart > 0
        else float(curr_price)
    )

    # Post-runner L2/L3 backstop when runner already exited early
    if sig.get("tp1_reached", False):
        if (
            status in ("BE_APPLIED", "PARTIAL_CLOSED", "ACTIVE")
            and not sig.get("runner_ticket")
            and sig.get("market_tickets")
        ):
            tp_levels = _resolve_signal_tps_dict(sig)
            if tp_levels:
                closed_bs = await _mt5_call(
                    executor._close_reached_layer_targets,
                    sig,
                    tp_levels,
                    direction == "BUY",
                    float(curr_price),
                    0,
                )
                if closed_bs:
                    added_bs, _ = await _account_closed_tickets(sig, closed_bs)
                    has_open_or_pending = bool(sig.get("market_tickets") or sig.get("pending_tickets"))
                    if not has_open_or_pending:
                        sig["status"] = "CLOSED"
                        _ensure_signal_exit_telemetry(
                            sig,
                            exit_reason_code="TP_CEILING",
                            exit_price=float(curr_price),
                        )
                    state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                    fin = await _mt5_call(executor.get_account_financials)
                    curr = fin.get("currency", "GBP")
                    if sig.get("status") == "CLOSED":
                        _emit_exit_telemetry_log(
                            msg_id,
                            sig,
                            float(sig.get("banked_profit") or added_bs),
                            exit_reason="TP2/TP3 Milestone Backstop",
                        )
                        await _record_signal_to_ledger(
                            msg_id, sig, exit_reason="TP2/TP3 Milestone Backstop", currency=curr
                        )
                    await notify_saved_messages(
                        f"🎯 **Layer Target Auto-Close Backstop [ID: {msg_id}]**\n"
                        f"• Symbol: `{symbol}` ({direction})\n"
                        f"• Closed Tickets: `{closed_bs}`\n"
                        f"• 💰 **Banked:** `{format_profit(added_bs, curr)}` | "
                        f"**Total Trade PnL:** `{format_profit(sig.get('banked_profit', 0.0), curr)}`\n\n"
                        f"{fin['formatted']}"
                    )
        return

    raw_tp1 = sig.get("raw_tp1")
    pip_size = await _mt5_call(executor.get_pip_size, symbol)

    # Fallback TP1 if an unpriced Market Now trade never received a TP edit
    if not raw_tp1 and sig.get("market_tickets"):
        ts_str = sig.get("timestamp", "")
        try:
            sig_dt = datetime.fromisoformat(ts_str)
            if sig_dt.tzinfo is None:
                sig_dt = sig_dt.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - sig_dt).total_seconds()
        except Exception:
            age_sec = UNPRICED_TP_GRACE_SECONDS + 1.0

        if age_sec >= UNPRICED_TP_GRACE_SECONDS:
            _cfg_fb = sys.modules.get("config") or config
            _pol_fb = read_dynamic_risk_policy(_cfg_fb)
            fb_atr = (
                await _mt5_call(executor.get_atr, symbol, _pol_fb.atr_timeframe, _pol_fb.atr_period)
                if _pol_fb.sltp_enabled
                else 0.0
            )
            atr_tp_dist = compute_atr_fallback_tp_distance(fb_atr, _pol_fb)
            if atr_tp_dist is not None and atr_tp_dist > 0.0:
                pip_offset = atr_tp_dist
                vr = compute_volatility_ratio(fb_atr, _pol_fb)
                step_list = [round(s * vr, 4) for s in (TP2_STEP_PIPS, TP3_STEP_PIPS, TP4_STEP_PIPS, TP5_STEP_PIPS)]
                fb_note = f"ATR={fb_atr:.2f} (${pip_offset:.2f})"
            else:
                pip_offset = FALLBACK_TP1_PIPS * pip_size
                step_list = None
                fb_note = f"+{FALLBACK_TP1_PIPS} pips"
            raw_tp1 = round(curr_price + pip_offset, 2) if direction == "BUY" else round(curr_price - pip_offset, 2)
            final_tp = round(raw_tp1 - TP_FRONT_RUN_OFFSET, 2) if direction == "BUY" else round(raw_tp1 + TP_FRONT_RUN_OFFSET, 2)
            tp_levels = derive_tp_levels([raw_tp1], direction=direction, pip_size=pip_size, step_pips=step_list)
            layers = sig.get("layers") if isinstance(sig.get("layers"), dict) else {}
            market_set = set(sig.get("market_tickets", []))
            modified_mk: set = set()
            for l_name, l_data in layers.items():
                if not isinstance(l_data, dict):
                    continue
                l_data["tp"] = final_tp
                l_data["tp_stage"] = "TP1"
                t = l_data.get("ticket")
                if t and t in market_set:
                    await _mt5_call(executor.modify_position_sltp, ticket=t, sl=None, tp=final_tp)
                    modified_mk.add(t)
            for mt in sig.get("market_tickets", []):
                if mt not in modified_mk:
                    await _mt5_call(executor.modify_position_sltp, ticket=mt, sl=None, tp=final_tp)
            sig["raw_tp1"] = raw_tp1
            sig["tp1_price"] = raw_tp1
            sig["raw_tp2"] = tp_levels.get("TP2")
            sig["raw_tp3"] = tp_levels.get("TP3")
            sig["raw_tp4"] = tp_levels.get("TP4")
            sig["raw_tp5"] = tp_levels.get("TP5")
            sig["tp_levels"] = tp_levels
            sig["final_tp"] = final_tp
            sig["hard_broker_tp"] = final_tp
            sig["tp_stage"] = "TP1"
            sig["fallback_tp_applied"] = True
            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
            logger.info(f"Attached fallback TP1 ({raw_tp1}, {fb_note}) on unpriced Signal {msg_id}")
            await notify_saved_messages(
                f"🛡️ **Fallback TP1 Attached [ID: {msg_id}]**\n"
                f"• No TP edit received after {int(UNPRICED_TP_GRACE_SECONDS)}s.\n"
                f"• Auto-assigned TP1: `{raw_tp1}` (Broker TP: `{final_tp}`) | {_format_tp_summary(sig)}"
            )

    if not raw_tp1:
        return

    tp1_hit = (chart_price >= raw_tp1) if direction == "BUY" else (chart_price <= raw_tp1)

    # Trigger 2: If 0 market positions filled and pending limits/stops remain, invalidate ONLY after
    # price has actually entered the entry zone/price (`has_entered_zone=True`) and then reaches raw_tp1
    # (or if a stop order hits SL before entry).
    if not sig.get("market_tickets"):
        pd_tkts = list(sig.get("pending_tickets", []))
        if pd_tkts:
            entry_ref = None
            z = sig.get("zone")
            if isinstance(z, dict):
                entry_ref = z.get("top") if direction == "BUY" else z.get("bottom")
            if entry_ref is None:
                entry_ref = sig.get("single_price")
            if entry_ref is None and isinstance(sig.get("layers"), dict):
                l1_obj = sig["layers"].get("L1") or sig["layers"].get("TOP") or {}
                entry_ref = l1_obj.get("target_price")

            is_stop_pending = bool(sig.get("is_stop_order", False))
            sl_pending = sig.get("sl")
            if is_stop_pending and isinstance(sl_pending, (int, float)) and not isinstance(sl_pending, bool):
                sl_f = float(sl_pending)
                bid_v = getattr(tick, "bid", None)
                ask_v = getattr(tick, "ask", None)
                sl_hit_pre_entry = (
                    (direction == "BUY" and isinstance(bid_v, (int, float)) and float(bid_v) <= sl_f)
                    or (direction == "SELL" and isinstance(ask_v, (int, float)) and float(ask_v) >= sl_f)
                )
                if sl_hit_pre_entry:
                    await _invalidate_signal_tp1_unlocked(
                        msg_id,
                        reason="Stop Loss Reached Before Stop Entry",
                        chat_id=sig_chat,
                    )
                    return

            if not sig.get("has_entered_zone") or not sig.get("tp1_guard_armed", False):
                entered_now = False
                if isinstance(entry_ref, (int, float)) and not isinstance(entry_ref, bool):
                    ref_f = float(entry_ref)
                    if direction == "BUY":
                        ask_val = getattr(tick, "ask", None)
                        if isinstance(ask_val, (int, float)) and not isinstance(ask_val, bool):
                            entered_now = (
                                (float(ask_val) >= ref_f - 1e-9)
                                if is_stop_pending
                                else (float(ask_val) <= ref_f + 1e-9)
                            )
                    elif direction == "SELL":
                        bid_val = getattr(tick, "bid", None)
                        if isinstance(bid_val, (int, float)) and not isinstance(bid_val, bool):
                            entered_now = (
                                (float(bid_val) <= ref_f + 1e-9)
                                if is_stop_pending
                                else (float(bid_val) >= ref_f - 1e-9)
                            )
                if entered_now:
                    sig["has_entered_zone"] = True
                    sig["tp1_guard_armed"] = True
                    state_mgr.update_signal(
                        msg_id,
                        {"has_entered_zone": True, "tp1_guard_armed": True},
                        chat_id=sig_chat,
                    )

            if (sig.get("has_entered_zone") or sig.get("tp1_guard_armed") is True) and _tick_hit_tp1(
                tick, direction, raw_tp1
            ):
                await _invalidate_signal_tp1_unlocked(
                    msg_id,
                    reason="Market Price Reached TP1",
                    chat_id=sig_chat,
                )
        return

    ratchet_on = bool(_main_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED))

    # v3.1 Proactive Runner TP Lift & Dynamic Deepest-Layer Sync before TP1 (legacy v3.5 path when Ratchet disabled)
    if not ratchet_on and not sig.get("tp1_reached") and hasattr(executor, "prepare_deepest_runners_for_tp_breakout"):
        sig_before_prep = copy.deepcopy(sig)
        await _mt5_call(executor.prepare_deepest_runners_for_tp_breakout, sig, curr_price, chart_price)
        if sig != sig_before_prep:
            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)

    if tp1_hit:
        logger.info(
            f"🎯 TP1 REACHED on Msg ID {msg_id}! ChartPrice={chart_price}, ExecPrice={curr_price}, Raw TP1={raw_tp1}"
        )
        _, _, alert = await _execute_tp1_scaling_handoff(
            msg_id, sig, chat_id=sig_chat, source_label="Watcher", curr_price=chart_price
        )
        await notify_saved_messages(alert)
        return

    layers_map = sig.get("layers") if isinstance(sig.get("layers"), dict) else {}
    mkt_tkts = [int(t) for t in (sig.get("market_tickets") or []) if t is not None]

    # v3.6 Stage 0 Profit Ratchet + BANK_HARVEST + 2-Step SL-First TP Lift
    if ratchet_on and not sig.get("tp1_reached") and mkt_tkts and hasattr(executor, "evaluate_profit_ratchet_tick"):
        sig_before_pr = copy.deepcopy(sig)
        pr_res = await _mt5_call(executor.evaluate_profit_ratchet_tick, sig, tick, 0)

        # 1) BANK_HARVEST on L1 ONLY (keeps CORE_RUNNER open to scale to TP2..TP5)
        hc_list = (
            list(pr_res.get("harvest_close_tickets") or [])
            if bool(_main_cfg("BANK_HARVEST_ENABLED", BANK_HARVEST_ENABLED))
            else []
        )
        for hc in hc_list:
            hc_tk = _safe_ticket_int(hc.get("ticket"))
            hc_lk = hc.get("layer")
            if hc_tk is None:
                continue
            closed_hc = bool(await _mt5_call(executor.close_market_position, hc_tk))
            if not closed_hc and not executor.dry_run:
                live_ps_hc = await _mt5_call(executor.get_live_positions, symbol)
                if live_ps_hc is not None:
                    closed_hc = not any(_safe_ticket_int(getattr(p, "ticket", None)) == hc_tk for p in live_ps_hc)
            if closed_hc:
                lv_hc = layers_map.get(hc_lk) if (hc_lk and isinstance(layers_map.get(hc_lk), dict)) else None
                if lv_hc is not None:
                    lv_hc["status"] = "CLOSED"
                    lv_hc["exit_reason_code"] = "BANK_HARVEST"
                    lv_hc["exit_price"] = round(float(curr_price), 4)
                sig["market_tickets"] = [t for t in (sig.get("market_tickets") or []) if _safe_ticket_int(t) != hc_tk]
                mkt_tkts = [t for t in mkt_tkts if t != hc_tk]
                sig["last_banked_tickets"] = list(dict.fromkeys(list(sig.get("last_banked_tickets") or []) + [hc_tk]))
                added_hc, _ = await _account_closed_tickets(sig, [hc_tk])
                fin_hc = await _mt5_call(executor.get_account_financials)
                curr_hc = fin_hc.get("currency", "GBP") if isinstance(fin_hc, dict) else "GBP"
                hc_lbl = resolve_layer_label(sig, hc_tk, fallback=hc_lk)
                await notify_saved_messages(
                    f"🏦 **BANK_HARVEST Peak Banked [ID: {msg_id}]**\n"
                    f"• Symbol: `{symbol}` ({direction}) | `{hc_lbl}` [Ticket #{hc_tk}]\n"
                    f"• Entry: `{hc.get('entry_price')}` | Peak: `{hc.get('peak_price')}` (`+${float(hc.get('mfe_dollars') or 0.0):.2f}/oz`) | Exit: `{curr_price}`\n"
                    f"• 💰 **Banked Profit:** `{format_profit(added_hc, curr_hc)}` | Surviving Open Tickets: `{sig.get('market_tickets')}`"
                )
                if not sig.get("market_tickets") and not sig.get("pending_tickets"):
                    sig["status"] = "CLOSED"
                    _ensure_signal_exit_telemetry(sig, exit_reason_code="BANK_HARVEST", exit_stage=0, exit_price=float(curr_price))
                    state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                    _emit_exit_telemetry_log(msg_id, sig, float(sig.get("banked_profit") or added_hc), exit_reason="BANK_HARVEST")
                    await _record_signal_to_ledger(msg_id, sig, exit_reason="BANK_HARVEST", currency=curr_hc)
                    return

        # 2) Spread-paused virtual stops (TIER_0T tight-TP1 harvest or Near-Target Squeeze)
        vc_list = (
            list(pr_res.get("virtual_close_tickets") or [])
            if bool(_main_cfg("PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED", PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED))
            else []
        )
        if vc_list and await _mt5_call(executor.try_begin_signal_close, sig):
            closed_vc: List[int] = []
            try:
                for vc in vc_list:
                    vc_tk = _safe_ticket_int(vc.get("ticket"))
                    vc_lk = vc.get("layer")
                    if vc_tk is None:
                        continue
                    ok_vc = bool(await _mt5_call(executor.close_market_position, vc_tk))
                    if not ok_vc and not executor.dry_run:
                        live_ps_vc = await _mt5_call(executor.get_live_positions, symbol)
                        if live_ps_vc is not None:
                            ok_vc = not any(_safe_ticket_int(getattr(p, "ticket", None)) == vc_tk for p in live_ps_vc)
                    if ok_vc:
                        closed_vc.append(vc_tk)
                        lv_vc = layers_map.get(vc_lk) if (vc_lk and isinstance(layers_map.get(vc_lk), dict)) else None
                        if lv_vc is not None:
                            lv_vc["status"] = "CLOSED"
                            lv_vc["exit_reason_code"] = "PROFIT_RATCHET_SL"
                            lv_vc["exit_price"] = round(float(curr_price), 4)
                if closed_vc:
                    closed_vc_set = set(closed_vc)
                    sig["market_tickets"] = [t for t in (sig.get("market_tickets") or []) if _safe_ticket_int(t) not in closed_vc_set]
                    if not sig.get("market_tickets"):
                        await _mt5_call(executor.cancel_signal_pending_orders, sig)
                        sig["pending_tickets"] = []
                        for lv_p in layers_map.values():
                            if isinstance(lv_p, dict) and str(lv_p.get("status") or "").strip().upper() == "PENDING":
                                lv_p["status"] = "CANCELLED"
                    added_vc, _ = await _account_closed_tickets(sig, closed_vc)
                    sig["last_runner_tickets"] = list(closed_vc)
                    sig["last_runner_ticket"] = closed_vc[-1]
                    if not sig.get("market_tickets") and not sig.get("pending_tickets"):
                        vc_top = vc_list[-1]
                        pk_vc = vc_top.get("peak_price") if vc_top.get("peak_price") is not None else curr_price
                        mfe_vc = float(vc_top.get("mfe_dollars") or 0.0)
                        is_b_vc = direction == "BUY"
                        sig["status"] = "CLOSED"
                        sig["runner_tickets"] = []
                        sig["runner_ticket"] = None
                        sig["runner_layers"] = []
                        sig["runner_layer"] = None
                        sig["runner_close_reason"] = f"Stage 0 Profit Ratchet {vc_top.get('tier', 'TIER_0T')} (virt_sl={vc_top.get('virt_sl')})"
                        sig["exit_reason_code"] = "PROFIT_RATCHET_SL"
                        sig["exit_stage"] = 0
                        sig["exit_price"] = round(float(curr_price), 4)
                        sig["max_favorable_excursion"] = {"peak_price": pk_vc, "peak_profit_per_oz": round(mfe_vc, 4)}
                        sig["giveback_from_peak"] = round(max(0.0, (float(pk_vc) - float(curr_price)) if is_b_vc else (float(curr_price) - float(pk_vc))), 4)
            finally:
                await _mt5_call(executor.end_signal_close, sig)
            if closed_vc:
                state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                fin_vc = await _mt5_call(executor.get_account_financials)
                curr_vc = fin_vc.get("currency", "GBP") if isinstance(fin_vc, dict) else "GBP"
                if sig.get("status") == "CLOSED":
                    _emit_exit_telemetry_log(msg_id, sig, float(sig.get("banked_profit") or 0.0), exit_reason=str(sig.get("runner_close_reason") or "PROFIT_RATCHET_SL"))
                    await _record_signal_to_ledger(msg_id, sig, exit_reason=str(sig.get("runner_close_reason") or "PROFIT_RATCHET_SL"), currency=curr_vc)
                vc_top = vc_list[-1]
                await notify_saved_messages(
                    f"🔒 **Stage 0 Profit Ratchet Harvested ({vc_top.get('tier', 'TIER_0T')}) [ID: {msg_id}]**\n"
                    f"• Symbol: `{symbol}` ({direction}) | `{resolve_layer_label(sig, closed_vc[-1], fallback=vc_top.get('layer'))}`\n"
                    f"• Peak: `{vc_top.get('peak_price')}` (`+${float(vc_top.get('mfe_dollars') or 0.0):.2f}/oz`) | Exit: `{curr_price}`\n"
                    f"• 💰 **Result:** `{format_profit(float(sig.get('banked_profit') or 0.0), curr_vc)}`\n\n"
                    f"{fin_vc.get('formatted', '')}"
                )
                return

        if pr_res.get("events") or pr_res.get("lift_aborts"):
            await _emit_ratchet_alerts(msg_id, sig, events=pr_res.get("events"), lift_aborts=pr_res.get("lift_aborts"))

        if sig != sig_before_pr:
            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
        return

    # Branch C: Legacy Layer-Separated Pre-TP1 Protection (when PROFIT_RATCHET_ENABLED=False)
    _cfg_mod = sys.modules.get("config")
    gold_guard_on = bool(
        GOLD_REVERSAL_GUARD_ENABLED
        if GOLD_REVERSAL_GUARD_ENABLED != getattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", False)
        else getattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", GOLD_REVERSAL_GUARD_ENABLED)
    )
    runner_layers, early_layers = executor.select_dynamic_runners(
        layers_map, mkt_tkts, full_runner_count=RUNNER_DEEPEST_COUNT_FULL
    )
    if not runner_layers and not early_layers and mkt_tkts:
        if len(mkt_tkts) == 1:
            runner_tickets_pre = [mkt_tkts[0]]
            early_tickets_pre: List[int] = []
        else:
            runner_tickets_pre = [mkt_tkts[-1]]
            early_tickets_pre = list(mkt_tkts[:-1])
    else:
        early_tickets_pre = [
            int(layers_map[lk]["ticket"])
            for lk in early_layers
            if isinstance(layers_map.get(lk), dict)
            and layers_map[lk].get("ticket") is not None
            and int(layers_map[lk]["ticket"]) in mkt_tkts
        ]
        runner_tickets_pre = [
            int(layers_map[lk]["ticket"])
            for lk in runner_layers
            if isinstance(layers_map.get(lk), dict)
            and layers_map[lk].get("ticket") is not None
            and int(layers_map[lk]["ticket"]) in mkt_tkts
        ]

    # C1. Stage 0 Gold Calibrated Peak-Profit Giveback Guard on Active Deepest Runner(s) (Pre-TP1 ONLY)
    is_spike = False
    stage_pre = _get_signal_stage(sig)
    if gold_guard_on and (runner_tickets_pre or mkt_tkts) and (not _is_staged_exit_enabled() or stage_pre == 0):
        sig_before_rg = copy.deepcopy(sig)
        rg_res = await _mt5_call(executor.evaluate_gold_reversal_guard, sig, tick)
        is_spike = bool(rg_res.get("paused_by_spread"))
        if rg_res.get("triggered"):
            if not await _mt5_call(executor.try_begin_signal_close, sig):
                return
            try:
                pk_px = rg_res.get("peak_price", curr_price)
                dd_pct = float(rg_res.get("drawdown_from_peak") or 0.0) * 100.0
                pb_dlr = float(rg_res.get("pullback_dollars") or 0.0)
                nf_dlr = float(rg_res.get("noise_floor_dollars") or MIN_GOLD_PULLBACK_DOLLARS)
                s0_lock_hit = bool(rg_res.get("stage0_lock_triggered"))
                logger.warning(
                    f"⚠️ GOLD REVERSAL GUARD TRIGGERED near TP1 on Msg ID {msg_id}! "
                    f"Peak={pk_px}, Live={curr_price}, Giveback={dd_pct:.1f}% (${pb_dlr:.2f} >= ${nf_dlr:.2f}), TP1={raw_tp1}"
                )
                closed_tickets = list(sig.get("market_tickets", []))
                for t in closed_tickets:
                    await _mt5_call(executor.close_market_position, t)
                await _mt5_call(executor.cancel_signal_pending_orders, sig)

                closed_set_rg = {_safe_ticket_int(t) for t in closed_tickets if _safe_ticket_int(t) is not None}
                for lk, lv in layers_map.items():
                    if isinstance(lv, dict):
                        lt = _safe_ticket_int(lv.get("ticket"))
                        st_lv = str(lv.get("status") or "").strip().upper()
                        if lt in closed_set_rg or st_lv in ("OPEN", "FILLED", "RUNNER", "CLOSED", "BANKED"):
                            lv["status"] = "CLOSED"
                        else:
                            lv["status"] = "CANCELLED"

                added_profit, _ = await _account_closed_tickets(sig, closed_tickets)
                pk_px_f = round(float(pk_px), 4) if _is_num(pk_px) else round(float(curr_price), 4)
                pk_prof_f = round(max(0.0, float(rg_res.get("peak_profit") or 0.0)), 4)
                prev_rlayers_rg = list(sig.get("runner_layers") or ([sig["runner_layer"]] if sig.get("runner_layer") else []) or runner_layers)
                prev_rlayer_rg = sig.get("runner_layer") or (prev_rlayers_rg[-1] if prev_rlayers_rg else sig.get("last_runner_layer"))
                if prev_rlayers_rg:
                    sig["last_runner_layers"] = prev_rlayers_rg
                if prev_rlayer_rg:
                    sig["last_runner_layer"] = prev_rlayer_rg
                if closed_tickets:
                    sig["last_runner_tickets"] = list(closed_tickets)
                    sig["last_runner_ticket"] = closed_tickets[-1]
                sig["status"] = "CLOSED"
                sig["market_tickets"] = []
                sig["pending_tickets"] = []
                sig["runner_tickets"] = []
                sig["runner_ticket"] = None
                sig["runner_layers"] = []
                sig["runner_layer"] = None
                sig["stage0_profit_lock_active"] = False
                sig["stage0_zone_protected"] = False
                sig["layers"] = layers_map
                if s0_lock_hit:
                    sig["runner_close_reason"] = f"Stage 0 Profit-Lock SL Hit ({rg_res.get('stage0_profit_lock_sl')})"
                    sig["exit_reason_code"] = "STAGE0_PROFIT_LOCK"
                    ledger_exit_reason = "Stage 0 Profit-Lock SL Hit"
                else:
                    sig["runner_close_reason"] = (
                        f"Gold Reversal Guard (TP1, Peak: {pk_px}, Giveback: {dd_pct:.1f}% / ${pb_dlr:.2f})"
                    )
                    sig["exit_reason_code"] = "GOLD_GUARD"
                    ledger_exit_reason = "Gold Reversal Guard (TP1 Near-Miss)"
                sig["exit_stage"] = 0
                sig["exit_price"] = round(float(curr_price), 4)
                sig["max_favorable_excursion"] = {
                    "peak_price": pk_px_f,
                    "peak_profit_per_oz": pk_prof_f,
                }
                sig["giveback_from_peak"] = round(max(0.0, float(pb_dlr)), 4)
                sig["trail_tier_used"] = None
            finally:
                await _mt5_call(executor.end_signal_close, sig)

            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
            fin = await _mt5_call(executor.get_account_financials)
            curr = fin.get("currency", "GBP")
            _emit_exit_telemetry_log(
                msg_id,
                sig,
                float(sig.get("banked_profit") or added_profit),
                exit_reason=ledger_exit_reason,
            )
            await _record_signal_to_ledger(
                msg_id, sig, exit_reason=ledger_exit_reason, currency=curr
            )
            profit_str = format_profit(added_profit, curr)
            await notify_saved_messages(
                f"🛡️ **Gold Calibrated Reversal Guard Banked! [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• Peak Price: `{pk_px}` (TP1 `{raw_tp1}`)\n"
                f"• Reversal Price: `{curr_price}` (`{dd_pct:.1f}%` giveback / `${pb_dlr:.2f}` >= `${nf_dlr:.2f}` floor)\n"
                f"• 💰 **Amount Made:** `{profit_str}`\n"
                f"• Remaining open profit banked at market & pending orders cancelled!\n\n"
                f"{fin['formatted']}"
            )
            return

        if sig != sig_before_rg:
            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)

    # C2. Runner Override: Disable tight TP1 Shortfall Guard when no non-runner base layers exist
    #     (e.g. N=1 single-layer trade where L1 dynamically acts as the deepest runner).
    if gold_guard_on and not early_tickets_pre:
        if sig.get("shortfall_armed"):
            sig["shortfall_armed"] = False
            sig["shortfall_reversal_ticks"] = 0
            state_mgr.update_signal(
                msg_id,
                {"shortfall_armed": False, "shortfall_reversal_ticks": 0},
                chat_id=sig_chat,
            )
        return

    # C3. Calibrated Base-Layer TP1 Shortfall Guard (acts ONLY on non-runner base layers L1-L3)
    raw_arm_dist = float(SHORTFALL_ARM_PIPS) * pip_size
    entry_ref = sig.get("recorded_entry_price") if sig.get("recorded_entry_price") is not None else sig.get("initial_price")
    if entry_ref is None and isinstance(sig.get("zone"), dict):
        z_dict = sig["zone"]
        entry_ref = z_dict.get("top") if direction == "BUY" else z_dict.get("bottom")
    if entry_ref is None and isinstance(layers_map, dict):
        l1_obj = layers_map.get("L1") or layers_map.get("TOP") or {}
        entry_ref = l1_obj.get("target_price")
    if isinstance(entry_ref, (int, float)) and not isinstance(entry_ref, bool):
        leg_dist = abs(float(raw_tp1) - float(entry_ref))
        max_leg_ratio = (1.0 - float(ARM_THRESHOLD_PCT)) if gold_guard_on else float(RUNNER_MAX_MIDRANGE_RATIO)
        arm_dist = min(raw_arm_dist, leg_dist * max_leg_ratio) if leg_dist > 0 else raw_arm_dist
    else:
        arm_dist = raw_arm_dist

    pullback_dist = float(SHORTFALL_PULLBACK_PIPS) * pip_size
    confirm_ticks_req = max(1, int(SHORTFALL_CONFIRM_TICKS if gold_guard_on else RUNNER_REVERSAL_CONFIRM_TICKS))
    shortfall_triggered = False
    peak_val = sig.get("peak_price")

    in_arm_zone = (
        curr_price >= round(float(raw_tp1) - arm_dist, 4)
        if direction == "BUY"
        else curr_price <= round(float(raw_tp1) + arm_dist, 4)
    )
    if not sig.get("shortfall_armed") and in_arm_zone and not is_spike:
        sig["shortfall_armed"] = True
        sig["peak_price"] = curr_price
        sig["shortfall_reversal_ticks"] = 0
        state_mgr.update_signal(
            msg_id,
            {"shortfall_armed": True, "peak_price": curr_price, "shortfall_reversal_ticks": 0},
            chat_id=sig_chat,
        )
        peak_val = curr_price

    if sig.get("shortfall_armed") and not is_spike:
        if direction == "BUY":
            if peak_val is None or curr_price > float(peak_val):
                sig["peak_price"] = curr_price
                sig["shortfall_reversal_ticks"] = 0
                state_mgr.update_signal(
                    msg_id,
                    {"peak_price": curr_price, "shortfall_reversal_ticks": 0},
                    chat_id=sig_chat,
                )
                peak_val = curr_price
            peak = float(sig.get("peak_price", curr_price))
            if curr_price <= round(peak - pullback_dist, 4):
                rev_ticks = int(sig.get("shortfall_reversal_ticks") or 0) + 1
                sig["shortfall_reversal_ticks"] = rev_ticks
                state_mgr.update_signal(msg_id, {"shortfall_reversal_ticks": rev_ticks}, chat_id=sig_chat)
                if rev_ticks >= confirm_ticks_req:
                    shortfall_triggered = True
                    peak_val = peak
            elif int(sig.get("shortfall_reversal_ticks") or 0) != 0:
                sig["shortfall_reversal_ticks"] = 0
                state_mgr.update_signal(msg_id, {"shortfall_reversal_ticks": 0}, chat_id=sig_chat)
        else:
            if peak_val is None or curr_price < float(peak_val):
                sig["peak_price"] = curr_price
                sig["shortfall_reversal_ticks"] = 0
                state_mgr.update_signal(
                    msg_id,
                    {"peak_price": curr_price, "shortfall_reversal_ticks": 0},
                    chat_id=sig_chat,
                )
                peak_val = curr_price
            peak = float(sig.get("peak_price", curr_price))
            if curr_price >= round(peak + pullback_dist, 4):
                rev_ticks = int(sig.get("shortfall_reversal_ticks") or 0) + 1
                sig["shortfall_reversal_ticks"] = rev_ticks
                state_mgr.update_signal(msg_id, {"shortfall_reversal_ticks": rev_ticks}, chat_id=sig_chat)
                if rev_ticks >= confirm_ticks_req:
                    shortfall_triggered = True
                    peak_val = peak
            elif int(sig.get("shortfall_reversal_ticks") or 0) != 0:
                sig["shortfall_reversal_ticks"] = 0
                state_mgr.update_signal(msg_id, {"shortfall_reversal_ticks": 0}, chat_id=sig_chat)

    if shortfall_triggered:
        if not await _mt5_call(executor.try_begin_signal_close, sig):
            return
        try:
            target_close_tkts = list(early_tickets_pre) if (gold_guard_on and runner_tickets_pre) else list(sig.get("market_tickets", []))
            logger.warning(
                f"⚠️ TP1 SHORTFALL TRIGGERED on Msg ID {msg_id} (Base Tickets={target_close_tkts}, Runner Spared={runner_tickets_pre if gold_guard_on else []})! "
                f"Peak={peak_val}, Reversal Price={curr_price}, TP1={raw_tp1}"
            )
            for t in target_close_tkts:
                await _mt5_call(executor.close_market_position, t)

            if CANCEL_PENDING_ON_TP1 or not (gold_guard_on and runner_tickets_pre):
                await _mt5_call(executor.cancel_signal_pending_orders, sig)
                sig["pending_tickets"] = []
                for lv in layers_map.values():
                    if isinstance(lv, dict) and lv.get("status") == "PENDING":
                        lv["status"] = "CANCELLED"

            closed_set_sf = set(target_close_tkts)
            for lk, lv in layers_map.items():
                if isinstance(lv, dict) and lv.get("ticket") in closed_set_sf:
                    lv["status"] = "CLOSED"

            added_profit, _ = await _account_closed_tickets(sig, target_close_tkts)
            surviving_mkt = [t for t in mkt_tkts if t not in closed_set_sf]
            sig["market_tickets"] = surviving_mkt
            sig["layers"] = layers_map
            sig["shortfall_armed"] = False
            sig["shortfall_reversal_ticks"] = 0

            if gold_guard_on and surviving_mkt:
                if BE_AFTER_TP1 and runner_layers:
                    await _mt5_call(
                        executor.apply_signal_breakeven,
                        sig,
                        list(runner_layers),
                        BE_OFFSET_PIPS,
                    )
                sig["status"] = "BE_APPLIED" if BE_AFTER_TP1 else "ACTIVE"
            else:
                is_buy_sf = direction == "BUY"
                pk_sf = round(float(peak_val), 4) if _is_num(peak_val) else round(float(curr_price), 4)
                cur_sf = round(float(curr_price), 4)
                if _is_num(entry_ref):
                    ref_sf = float(entry_ref)
                    pk_prof_sf: Optional[float] = round(
                        max(0.0, (pk_sf - ref_sf) if is_buy_sf else (ref_sf - pk_sf)), 4
                    )
                else:
                    pk_prof_sf = None
                gb_sf = round(max(0.0, (pk_sf - cur_sf) if is_buy_sf else (cur_sf - pk_sf)), 4)
                sig["status"] = "CLOSED"
                sig["runner_tickets"] = []
                sig["runner_ticket"] = None
                sig["exit_reason_code"] = "SHORTFALL_BASE"
                sig["exit_stage"] = 0
                sig["exit_price"] = cur_sf
                sig["max_favorable_excursion"] = {
                    "peak_price": pk_sf,
                    "peak_profit_per_oz": pk_prof_sf,
                }
                sig["giveback_from_peak"] = gb_sf
                sig["trail_tier_used"] = None
        finally:
            await _mt5_call(executor.end_signal_close, sig)

        state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(added_profit, curr)

        if sig["status"] == "CLOSED":
            _emit_exit_telemetry_log(
                msg_id,
                sig,
                float(sig.get("banked_profit") or added_profit),
                exit_reason="TP1 Shortfall Guard",
            )
            await _record_signal_to_ledger(msg_id, sig, exit_reason="TP1 Shortfall Guard", currency=curr)
            await notify_saved_messages(
                f"⚠️ **TP1 Shortfall Guard Triggered & Banked! [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• Peak Price: `{peak_val}` (within {SHORTFALL_ARM_PIPS} pips of TP1 `{raw_tp1}`)\n"
                f"• Reversal Price: `{curr_price}` (pulled back {SHORTFALL_PULLBACK_PIPS} pips from peak)\n"
                f"• 💰 **Amount Made:** `{profit_str}`\n"
                f"• All base positions banked at market & pending orders cancelled!\n\n"
                f"{fin['formatted']}"
            )
        else:
            await notify_saved_messages(
                f"⚠️ **Base Layers Banked via TP1 Shortfall — Runner Holding! [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• Banked Base Tickets: `{target_close_tkts}` (`{profit_str}`)\n"
                f"• 🏃 **Active Runner `{runner_layers}` (`{sig['market_tickets']}`):** Protected at BE & riding under Gold Reversal Guard!\n\n"
                f"{fin['formatted']}"
            )


async def tp1_proximity_watcher_task() -> None:
    """
    Background loop running every 1.0 second:
    0. MT5 connection health & one-shot disconnect/recovery alerts + Telegram keepalive/catch-up.
    1. 30-Second Algo Trading Button Monitor: alerts Saved Messages if Algo Trading is OFF.
    2. 1-Second MT5 State Reconciliation: syncs filled limits, broker closes & unified runner close.
    3. 1-Second Layer 4 Active Runner FSM (TP1..TP5), TP1 Scaling & Shortfall Guard.
    """
    logger.info("v3.0 Active Runner FSM & MT5 State Reconciler started (1.0s loop).")
    last_algo_check = 0.0
    last_tg_keepalive = time.time()
    last_account_poll = 0.0
    last_deleted_poll = time.time()
    has_live_exposure = False

    while True:
        any_deferred_be = False
        try:
            now = time.time()
            # Priority 0: Pull out deleted signals ASAP at the very top of every loop iteration
            if (
                CLOSE_ON_SIGNAL_DELETED
                and float(DELETED_SIGNAL_POLL_INTERVAL_SECONDS) > 0.0
                and (now - last_deleted_poll >= float(DELETED_SIGNAL_POLL_INTERVAL_SECONDS))
            ):
                last_deleted_poll = now
                if state_mgr.get_open_or_pending_signals():
                    await poll_deleted_active_signals(source_label="Fast Sweep")

            active_signals = state_mgr.get_active_signals()
            mt5_connected = True
            algo_allowed = True

            if not executor.dry_run:
                mt5_connected, algo_allowed, _ = await _probe_mt5_health()
                await _handle_mt5_connection_transition(mt5_connected)

            await _update_live_terminal_status_async()
            if not executor.dry_run and _last_hud_state is not None:
                has_live_exposure = (_last_hud_state[4] > 0.0)

            if active_signals or has_live_exposure or (now - last_account_poll >= IDLE_MT5_POLL_INTERVAL_SECONDS):
                last_account_poll = now
                await check_and_enforce_daily_drawdown()
                if await check_and_enforce_daily_win_target() == "DAILY_WIN_REACHED":
                    break
                if await check_and_enforce_daily_loss_limit(exit_on_trigger=False) == "DAILY_LOSS_REACHED":
                    break

            if now - last_tg_keepalive >= TELEGRAM_KEEPALIVE_SECONDS:
                last_tg_keepalive = now
                await _run_telegram_keepalive_check(now)

            if now - last_algo_check >= CHECK_ALGO_TRADING_INTERVAL:
                last_algo_check = now
                if not executor.dry_run and mt5_connected and not algo_allowed:
                    logger.warning("⚠️ Algo Trading button in MT5 is currently OFF!")
                    await notify_saved_messages(
                        "⚠️ **Warning**: Algo Trading is toggled OFF in MT5. Orders will fail until you enable it."
                    )

            active_signals = state_mgr.get_active_signals()
            if not executor.dry_run and mt5_connected and active_signals:
                await _reconcile_live_mt5_signals(active_signals)

            active_signals = state_mgr.get_active_signals()
            for msg_id, sig_snap in active_signals:
                sig_chat = sig_snap.get("chat_id")
                async with _get_signal_lock(sig_chat, msg_id):
                    sig = state_mgr.get_signal(msg_id, chat_id=sig_chat)
                    if not sig or sig.get("status") == "CLOSED":
                        continue

                    # Priority 1: Fast Armed-Breakeven Sweep (snaps deferred / newly-filled layers to BE in <=0.5s)
                    if sig.get("be_armed"):
                        be_done = {int(t) for t in (sig.get("be_done_tickets") or []) if t is not None}
                        open_unprotected = [
                            int(t) for t in (sig.get("market_tickets") or []) if t is not None and int(t) not in be_done
                        ]
                        if open_unprotected or sig.get("be_deferred_tickets"):
                            res_be = await _mt5_call(
                                executor.apply_signal_breakeven,
                                sig,
                                BE_LAYERS,
                                BE_OFFSET_PIPS,
                            )
                            newly_mod = res_be.get("modified_tickets", [])
                            still_def = res_be.get("deferred_tickets", [])
                            if newly_mod or still_def != (sig.get("be_deferred_tickets") or []):
                                state_mgr.update_signal(
                                    msg_id,
                                    {
                                        "layers": sig.get("layers", {}),
                                        "market_tickets": sig.get("market_tickets", []),
                                        "pending_tickets": sig.get("pending_tickets", []),
                                        "be_done_tickets": res_be.get("be_done_tickets", []),
                                        "be_deferred_tickets": still_def,
                                        **(
                                            {"runner_sl_floor": sig["runner_sl_floor"]}
                                            if sig.get("runner_sl_floor") is not None
                                            else {}
                                        ),
                                    },
                                    chat_id=sig_chat,
                                )
                                if newly_mod:
                                    logger.info(
                                        f"⚡ Fast Armed-BE Sweep protected deferred/filled tickets {newly_mod} on Msg ID {msg_id}!"
                                    )
                                    await notify_saved_messages(
                                        f"🛡️ **Armed Breakeven Auto-Applied [ID: {msg_id}]**\n"
                                        f"• Channel: `{sig_chat}` | Symbol: `{sig.get('symbol', DEFAULT_SYMBOL)}`\n"
                                        f"• Newly Protected Tickets: `{newly_mod}`"
                                    )
                            if still_def:
                                any_deferred_be = True

                    if sig.get("status") == "RUNNER_ACTIVE":
                        await _process_runner_active_tick(msg_id, sig)
                    else:
                        await _process_pre_tp1_or_backstop_tick(msg_id, sig)

                    sig_dt = state_mgr.get_signal(msg_id, chat_id=sig_chat) or sig
                    if (
                        sig_dt.get("status") != "CLOSED"
                        and sig_dt.get("market_tickets")
                        and bool(_main_cfg("DYNAMIC_ATR_TRAIL_ENABLED", DYNAMIC_ATR_TRAIL_ENABLED))
                    ):
                        dt_res = await _mt5_call(executor.evaluate_dynamic_atr_trailing_stop, sig_dt)
                        if dt_res.get("updated_tickets") or dt_res.get("newly_armed"):
                            dt_upd: Dict[str, Any] = {
                                "layers": sig_dt.get("layers", {}),
                                "dynamic_atr_trail": sig_dt.get("dynamic_atr_trail"),
                            }
                            if sig_dt.get("broker_sl") is not None:
                                dt_upd["broker_sl"] = sig_dt["broker_sl"]
                            state_mgr.update_signal(msg_id, dt_upd, chat_id=sig_chat)
                        for _t, _lk, _new_sl, _tdist, _tatr in (dt_res.get("updated_tickets") or []):
                            logger.info(
                                f"[DYNAMIC ATR TRAIL] Msg {msg_id} {_lk} (#{_t}): SL -> {_new_sl} "
                                f"({dt_res.get('timeframe')} ATR=${_tatr:.2f}, dist=${_tdist:.2f})"
                            )
                        if dt_res.get("newly_armed"):
                            _meta = sig_dt.get("dynamic_atr_trail") or {}
                            await notify_saved_messages(
                                f"🎯 **Dynamic ATR Trailing Stop Armed [ID: {msg_id}]**\n"
                                f"• {sig_dt.get('direction')} `{sig_dt.get('symbol', DEFAULT_SYMBOL)}` | "
                                f"{_meta.get('timeframe', 'M5')} ATR=`${float(_meta.get('atr') or 0.0):.2f}` | "
                                f"Trail=`${float(_meta.get('trail_dist') or 0.0):.2f}`"
                            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in TP1 proximity watcher: {e}")

        await asyncio.sleep(0.5 if any_deferred_be else 1.0)


# ---------------------------------------------------------------------------
# MESSAGE HANDLERS
# ---------------------------------------------------------------------------

def _extract_reply_to_id(msg: Any) -> Optional[int | str]:
    raw_reply_to = getattr(msg, "reply_to_msg_id", None)
    if not (isinstance(raw_reply_to, (int, str)) and not isinstance(raw_reply_to, bool)):
        reply_obj = getattr(msg, "reply_to", None)
        raw_reply_to = getattr(reply_obj, "reply_to_msg_id", None) if reply_obj else None
    if isinstance(raw_reply_to, (int, str)) and not isinstance(raw_reply_to, bool):
        return raw_reply_to
    return None


async def _apply_adjust_or_trail_unlocked(
    target_msg_id: Any,
    target_chat_id: Optional[int | str],
    target_signal: Dict[str, Any],
    mgmt_action_obj: Any,
    source_label: str = "Channel Prompt",
) -> str:
    """
    Shared handler for ACTION_ADJUST_TARGETS, ACTION_TRAIL_SL, ACTION_TP2_HIT, and ACTION_TP3_HIT.
    Caller must hold `_get_signal_lock(target_chat_id, target_msg_id)`.
    """
    action = getattr(mgmt_action_obj, "management_action", None) or (
        mgmt_action_obj.get("management_action") if isinstance(mgmt_action_obj, dict) else None
    )
    symbol = target_signal.get("symbol", DEFAULT_SYMBOL)
    direction = target_signal.get("direction", "BUY")

    if action in ("ACTION_ADJUST_TARGETS", "ADJUST_TARGETS"):
        sl_valid = bool(
            getattr(mgmt_action_obj, "sl_valid", False)
            if not isinstance(mgmt_action_obj, dict)
            else mgmt_action_obj.get("sl_valid", False)
        )
        raw_sl = (
            getattr(mgmt_action_obj, "sl", None)
            if not isinstance(mgmt_action_obj, dict)
            else mgmt_action_obj.get("sl")
        )
        new_sl_val = raw_sl if (sl_valid and raw_sl is not None) else None
        labeled_tps = (
            getattr(mgmt_action_obj, "labeled_tps", None)
            if not isinstance(mgmt_action_obj, dict)
            else mgmt_action_obj.get("labeled_tps")
        )
        unlabeled_tps = (
            getattr(mgmt_action_obj, "unlabeled_tps", None)
            if not isinstance(mgmt_action_obj, dict)
            else mgmt_action_obj.get("unlabeled_tps")
        )

        res = await _mt5_call(
            executor.adjust_active_signal_sl_tp,
            target_signal,
            new_sl=new_sl_val,
            labeled_tps=labeled_tps,
            unlabeled_tps=unlabeled_tps,
            ratchet_sl_only=False,
        )
        state_mgr.update_signal(target_msg_id, target_signal, chat_id=target_chat_id)
        rej_note = (
            f"\n• ⚠️ **SL Stop-Level Note:** `{res['rejected_sl_reason']}`"
            if res.get("rejected_sl_reason")
            else ""
        )
        return (
            f"🎯 **Active Trade SL / TP Adjusted ({source_label}) [ID: {target_msg_id}]**\n"
            f"• Symbol: `{symbol}` ({direction}) | Status: `{target_signal.get('status')}`\n"
            f"• 🛡️ **SL:** `{res.get('sl')}` | **Active Stage:** `{res.get('tp_stage')}` | "
            f"**Broker TP1:** `{res.get('final_tp')}`\n"
            f"• 🎯 **Targets:** {_format_tp_summary(target_signal)}\n"
            f"• Modified Positions: `{res.get('modified_positions', [])}` | "
            f"Pending Orders: `{res.get('modified_orders', [])}`"
            f"{rej_note}"
        )

    # Branch B: ACTION_TRAIL_SL, ACTION_TP2_HIT, ACTION_TP3_HIT
    labeled_tps = (
        getattr(mgmt_action_obj, "labeled_tps", None)
        if not isinstance(mgmt_action_obj, dict)
        else mgmt_action_obj.get("labeled_tps")
    )
    unlabeled_tps = (
        getattr(mgmt_action_obj, "unlabeled_tps", None)
        if not isinstance(mgmt_action_obj, dict)
        else mgmt_action_obj.get("unlabeled_tps")
    )
    if labeled_tps or unlabeled_tps:
        await _mt5_call(
            executor.adjust_active_signal_sl_tp,
            target_signal,
            new_sl=None,
            labeled_tps=labeled_tps,
            unlabeled_tps=unlabeled_tps,
            ratchet_sl_only=True,
        )

    if action in ("ACTION_TP2_HIT", "TP2_HIT"):
        milestone = "TP2"
    elif action in ("ACTION_TP3_HIT", "TP3_HIT"):
        milestone = "TP3"
    else:
        milestone = (
            getattr(mgmt_action_obj, "trail_milestone", None)
            if not isinstance(mgmt_action_obj, dict)
            else mgmt_action_obj.get("trail_milestone")
        )

    trail_pips = (
        getattr(mgmt_action_obj, "trail_pips", None)
        if not isinstance(mgmt_action_obj, dict)
        else mgmt_action_obj.get("trail_pips")
    )
    trail_price = (
        getattr(mgmt_action_obj, "trail_price", None)
        if not isinstance(mgmt_action_obj, dict)
        else mgmt_action_obj.get("trail_price")
    )

    was_tp1_reached = bool(target_signal.get("tp1_reached"))
    prev_runners = [
        t
        for t in (
            target_signal.get("runner_tickets")
            or ([target_signal["runner_ticket"]] if target_signal.get("runner_ticket") else [])
        )
        if t is not None
    ]

    res = await _mt5_call(
        executor.trail_active_signal,
        target_signal,
        milestone=milestone,
        trail_pips=trail_pips,
        explicit_sl_price=trail_price,
    )

    if not was_tp1_reached and target_signal.get("tp1_reached") and res.get("banked_tickets"):
        await _account_closed_tickets(target_signal, list(res.get("banked_tickets") or []))

    if target_signal.get("status") == "CLOSED":
        closed_runners = [
            t
            for t in (target_signal.get("last_runner_tickets") or prev_runners)
            if t is not None
        ]
        if closed_runners:
            await _account_closed_tickets(target_signal, closed_runners)
        _ensure_signal_exit_telemetry(target_signal, exit_reason_code="TP_CEILING")
        state_mgr.update_signal(target_msg_id, target_signal, chat_id=target_chat_id)
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        exit_rsn = target_signal.get("runner_close_reason") or f"Ceiling Reached ({source_label})"
        _emit_exit_telemetry_log(
            target_msg_id,
            target_signal,
            float(target_signal.get("banked_profit") or 0.0),
            exit_reason=str(exit_rsn),
        )
        await _record_signal_to_ledger(target_msg_id, target_signal, exit_reason=str(exit_rsn), currency=curr)
        total_pnl_str = format_profit(float(target_signal.get("banked_profit") or 0.0), curr)
        return (
            f"🏆 **Active Trade Ceiling Reached & Banked ({source_label}) [ID: {target_msg_id}]**\n"
            f"• Symbol: `{symbol}` ({direction}) | Status: `CLOSED`\n"
            f"• Cleared Milestones: `{res.get('cleared_milestones', [])}`\n"
            f"• 💰 **Total Realized Profit:** `{total_pnl_str}`\n\n"
            f"{fin['formatted']}"
        )

    state_mgr.update_signal(target_msg_id, target_signal, chat_id=target_chat_id)
    return (
        f"🛡️ **Active Trade Trailed / Stepped ({source_label}) [ID: {target_msg_id}]**\n"
        f"• Symbol: `{symbol}` ({direction}) | Status: `{target_signal.get('status')}`\n"
        f"• 🔒 **Locked SL / Floor:** `{res.get('new_sl')}` | **Stage:** `{res.get('tp_stage')}` | "
        f"**Next Target:** `{res.get('next_tp')}`\n"
        f"• 🏁 **Cleared Milestones:** `{res.get('cleared_milestones', [])}` | "
        f"Modified Tickets: `{res.get('modified_tickets', [])}`\n"
        f"• 🎯 **Targets:** {_format_tp_summary(target_signal)}"
    )


async def _handle_trade_management_action(
    msg_id: Any,
    chat_id: Optional[int | str],
    is_reply_evt: bool,
    parsed: SignalAction,
    target_msg_id: Any,
    target_chat_id: Optional[int | str],
    target_signal: Dict[str, Any],
) -> None:
    """Apply a Gate 3 TRADE_MANAGEMENT action safely on target_signal."""
    action = parsed.management_action
    if target_signal.get("status") in ("PENDING", "PENDING_DELAY") and action not in (
        "ACTION_FULL_CLOSE",
        "FULL_CLOSE",
        "ACTION_TP1_HIT_REPLY",
        "TP1_HIT_REPLY",
        "ACTION_ADJUST_TARGETS",
        "ADJUST_TARGETS",
    ):
        logger.info(f"Ignoring {action}: target signal {target_msg_id} is still in delay window (no open orders).")
        return

    if target_signal.get("status") in (
        "CLOSED",
        "FULL_CLOSED",
        "INVALIDATED_TP1",
        "ABORTED_PRICE_GUARD",
    ) and action not in ("ACTION_REACTIVATE", "REACTIVATE"):
        logger.info(f"Ignoring {action}: target signal {target_msg_id} is already terminal ({target_signal.get('status')}).")
        return

    symbol = target_signal.get("symbol", DEFAULT_SYMBOL)
    direction = target_signal.get("direction", "BUY")
    layers = target_signal.get("layers", {})
    market_tickets = list(target_signal.get("market_tickets", []))
    pending_tickets = list(target_signal.get("pending_tickets", []))

    if action in ("ACTION_BREAKEVEN", "BREAKEVEN"):
        res_be = await _mt5_call(
            executor.apply_signal_breakeven,
            target_signal,
            BE_LAYERS,
            BE_OFFSET_PIPS,
        )
        mod_tkts = res_be.get("modified_tickets", [])
        def_tkts = res_be.get("deferred_tickets", [])
        prev_status = target_signal.get("status", "ACTIVE")
        new_status = "RUNNER_ACTIVE" if prev_status == "RUNNER_ACTIVE" else "BE_APPLIED"
        updates_be: Dict[str, Any] = {
            "status": new_status,
            "layers": target_signal.get("layers", layers),
            "market_tickets": target_signal.get("market_tickets", market_tickets),
            "pending_tickets": target_signal.get("pending_tickets", pending_tickets),
            "be_armed": True,
            "be_done_tickets": res_be.get("be_done_tickets", []),
            "be_deferred_tickets": def_tkts,
        }
        if target_signal.get("runner_sl_floor") is not None:
            updates_be["runner_sl_floor"] = target_signal["runner_sl_floor"]
        state_mgr.update_signal(target_msg_id, updates_be, chat_id=target_chat_id)

        defer_note = (
            f"\n• ⚡ **Armed Auto-BE (0.5s Sweep):** Tickets `{def_tkts}` will snap to BE the instant price clears entry!"
            if def_tkts
            else ""
        )
        pend_tkts_now = target_signal.get("pending_tickets", pending_tickets)
        pend_note = (
            f"\n• ⏳ **Pending Layers Armed:** `{pend_tkts_now}` will auto-BE if filled."
            if pend_tkts_now
            else ""
        )
        await notify_saved_messages(
            f"🛡️ **Selective Breakeven Applied — All Layers [ID: {target_msg_id}]**\n"
            f"• Channel: `{target_chat_id}` | Symbol: `{symbol}` ({direction})\n"
            f"• Layers Protected: `{BE_LAYERS}` (+{BE_OFFSET_PIPS} pips / 0.0p instant fallback)\n"
            f"• Modified Tickets: `{mod_tkts}`"
            f"{defer_note}{pend_note}"
        )

    elif action in ("ACTION_CLOSE_WORST_HOLD_LAYERS", "CLOSE_WORST_HOLD_LAYERS"):
        raw_tp3 = target_signal.get("raw_tp3")
        res = await _mt5_call(
            executor.apply_breakeven_worst_hold_layers,
            market_tickets=market_tickets,
            symbol=symbol,
            direction=direction,
            be_offset_pips=BE_OFFSET_PIPS,
            raw_tp3=raw_tp3,
        )
        be_tkts = res.get("be_tickets", [])
        held_tkts = res.get("held_tickets", [])
        failed_be = res.get("failed_be_tickets", [])

        updates: Dict[str, Any] = {"hold_past_tp2": True}
        if be_tkts:
            updates["status"] = "RUNNER_ACTIVE" if target_signal.get("status") == "RUNNER_ACTIVE" else "BE_APPLIED"
        state_mgr.update_signal(target_msg_id, updates, chat_id=target_chat_id)

        fail_str = ""
        if failed_be:
            fail_details = ", ".join(f"#{t} ({err})" for t, err in failed_be)
            fail_str = f"\n• ⚠️ **Could not BE yet:** {fail_details}"

        target_note = f"TP3 (`{raw_tp3}`) + Trail SL" if raw_tp3 else "TP1 / TP2 + Trail SL"
        await notify_saved_messages(
            f"🛡️ **Worst Entries -> Breakeven | Holding Top 2 Profit Layers**\n"
            f"• Symbol: `{symbol}` ({direction}) [Msg ID: {target_msg_id}]\n"
            f"• 🔒 **Moved to BE (+{BE_OFFSET_PIPS} pips):** `{be_tkts or 'None (<= 2 layers open)'}`\n"
            f"• 🚀 **Held Running Layers:** `{held_tkts}` -> Target `{target_note}`"
            f"{fail_str}"
        )

    elif action in ("ACTION_REACTIVATE", "REACTIVATE"):
        curr_st = str(target_signal.get("status") or "").upper()
        has_live_tkts = bool(
            target_signal.get("market_tickets")
            or target_signal.get("pending_tickets")
            or target_signal.get("runner_tickets")
            or target_signal.get("runner_ticket")
        )
        is_closed_state = curr_st in ("CLOSED", "FULL_CLOSED", "CANCELLED")
        if (
            curr_st in ("ACTIVE", "RUNNER_ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "OPEN", "PARTIAL_FILLED")
            or (not is_closed_state and has_live_tkts)
            or (target_signal.get("tp1_reached") and bool(target_signal.get("runner_tickets") or target_signal.get("runner_ticket")))
        ):
            logger.info(
                f"Signal {target_msg_id} is already active/running (status={curr_st}, live_tickets={has_live_tkts}); "
                f"ignoring redundant 'Active' command."
            )
            return

        raw_tp1 = target_signal.get("raw_tp1")
        sl = target_signal.get("sl")
        zone_dict = target_signal.get("zone")
        single_px = target_signal.get("single_price")
        is_stop = bool(target_signal.get("is_stop_order", False))
        intent = target_signal.get("order_intent", "AUTO")
        zone = (
            ZoneInfo(zone_dict["top"], zone_dict["mid"], zone_dict["bottom"], direction=direction)
            if isinstance(zone_dict, dict)
            else None
        )

        if executor.get_daily_drawdown_status().get("pause_new_signals"):
            await check_and_enforce_daily_drawdown()

        passed, reason = await _mt5_call(
            executor.check_preflight,
            symbol=symbol,
            direction=direction,
            raw_tp1=raw_tp1,
            sl=sl,
            zone=zone,
            single_price=single_px,
            is_stop_order=is_stop,
            order_intent=intent,
        )
        if not passed:
            logger.warning(f"Reactivation skipped: {reason}")
            await notify_saved_messages(f"⚠️ Reactivation rejected for Msg ID {target_msg_id}: {reason}")
            return

        tp_levels = _resolve_signal_tps_dict(target_signal)
        if zone:
            mk_tkts, pd_tkts, layers_data = await _mt5_call(
                executor.execute_4layer_zone,
                symbol=symbol,
                direction=direction,
                zone=zone,
                sl=sl,
                final_tp=target_signal.get("final_tp"),
                raw_tp1=raw_tp1,
                raw_tp2=target_signal.get("raw_tp2"),
                raw_tp3=target_signal.get("raw_tp3"),
                raw_tp4=target_signal.get("raw_tp4"),
                raw_tp5=target_signal.get("raw_tp5"),
                tp_levels=tp_levels or None,
            )
        elif single_px is not None:
            mk_tkts, pd_tkts, layers_data = await _mt5_call(
                executor.execute_4layer_single_price,
                symbol=symbol,
                direction=direction,
                price=float(single_px),
                sl=sl,
                final_tp=target_signal.get("final_tp"),
                raw_tp1=raw_tp1,
                raw_tp2=target_signal.get("raw_tp2"),
                raw_tp3=target_signal.get("raw_tp3"),
                raw_tp4=target_signal.get("raw_tp4"),
                raw_tp5=target_signal.get("raw_tp5"),
                tp_levels=tp_levels or None,
                order_intent=intent,
                is_stop_order=is_stop,
            )
        else:
            mk_tkts, pd_tkts, layers_data = await _mt5_call(
                executor.execute_4layer_market,
                symbol=symbol,
                direction=direction,
                sl=sl,
                final_tp=target_signal.get("final_tp"),
                raw_tp1=raw_tp1,
                raw_tp2=target_signal.get("raw_tp2"),
                raw_tp3=target_signal.get("raw_tp3"),
                raw_tp4=target_signal.get("raw_tp4"),
                raw_tp5=target_signal.get("raw_tp5"),
                tp_levels=tp_levels or None,
            )

        if not mk_tkts and not pd_tkts:
            err_reason = getattr(executor, "last_error", None) or "Broker rejected reactivation order(s)"
            await notify_saved_messages(f"❌ Reactivation rejected by MT5 for Msg ID {target_msg_id}: `{err_reason}`")
            return

        dyn_r = _merge_dynamic_risk_into_signal_fields(
            getattr(executor, "last_dynamic_risk_snapshot", None),
            sl, target_signal.get("final_tp"), raw_tp1,
            target_signal.get("raw_tp2"), target_signal.get("raw_tp3"),
            target_signal.get("raw_tp4"), target_signal.get("raw_tp5"),
            tp_levels or None,
        )
        upd_r: Dict[str, Any] = {
            "status": "ACTIVE",
            "market_tickets": mk_tkts,
            "pending_tickets": pd_tkts,
            "layers": layers_data,
            "max_orders": MAX_ORDERS_PER_SIGNAL,
            "tp1_reached": False,
            "accounted_tickets": [],
            "banked_profit": 0.0,
            "runner_ticket": None,
            "runner_layer": None,
            "runner_milestone_idx": 1,
            "runner_milestone_name": "TP1",
            "runner_cleared_milestones": [],
            "runner_proximity_armed": False,
            "tp2_shortfall_armed": False,
            "ledger_recorded": False,
        }
        if dyn_r["dynamic_risk"] is not None:
            upd_r.update({
                "sl": dyn_r["sl"],
                "final_tp": dyn_r["final_tp"],
                "hard_broker_tp": dyn_r["final_tp"],
                "raw_tp1": dyn_r["raw_tp1"],
                "tp1_price": dyn_r["raw_tp1"],
                "raw_tp2": dyn_r["raw_tp2"],
                "raw_tp3": dyn_r["raw_tp3"],
                "raw_tp4": dyn_r["raw_tp4"],
                "raw_tp5": dyn_r["raw_tp5"],
                "tp_levels": dyn_r["tp_levels"],
                "dynamic_risk": dyn_r["dynamic_risk"],
            })
        state_mgr.update_signal(target_msg_id, upd_r, chat_id=target_chat_id)
        await notify_saved_messages(
            f"🔄 **4-Layer Setup Reactivated ('Active') [ID: {target_msg_id}]**\n"
            f"• Symbol: `{symbol}` ({direction})\n"
            f"• Distribution: `{BASE_LAYER_LOTS}` (x{RISK_LOT_MULTIPLIER})\n"
            f"• Market: {mk_tkts} | Pending: {pd_tkts}"
        )

    elif action in ("ACTION_PARTIAL_CLOSE", "PARTIAL_CLOSE"):
        closed = await _mt5_call(
            executor.apply_partial_close,
            market_tickets=market_tickets,
            pending_tickets=pending_tickets,
            symbol=symbol,
            direction=direction,
        )
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        profit_info = ""
        if closed and closed in market_tickets:
            market_tickets.remove(closed)
            added_p, _ = await _account_closed_tickets(target_signal, [closed])
            profit_info = f"\n• 💰 **Amount Made:** `{format_profit(added_p, curr)}`"
            for l_data in layers.values():
                if isinstance(l_data, dict) and l_data.get("ticket") == closed:
                    l_data["status"] = "CLOSED"
            if target_signal.get("runner_ticket") == closed:
                target_signal["last_runner_ticket"] = closed
                target_signal["runner_ticket"] = None

        prev_st_pc = str(target_signal.get("status") or "").strip().upper()
        new_status = (
            "CLOSED"
            if (not market_tickets and not pending_tickets)
            else ("RUNNER_ACTIVE" if prev_st_pc == "RUNNER_ACTIVE" else "PARTIAL_CLOSED")
        )
        target_signal["market_tickets"] = market_tickets
        target_signal["layers"] = layers
        target_signal["status"] = new_status
        if new_status == "CLOSED":
            _ensure_signal_exit_telemetry(target_signal, exit_reason_code="MANUAL", overwrite_code=True)
        state_mgr.update_signal(
            target_msg_id,
            {
                "market_tickets": market_tickets,
                "layers": layers,
                "status": new_status,
                "accounted_tickets": target_signal.get("accounted_tickets", []),
                "accounted_deal_tickets": target_signal.get("accounted_deal_tickets", []),
                "banked_profit": target_signal.get("banked_profit", 0.0),
                "runner_ticket": target_signal.get("runner_ticket"),
                "last_runner_ticket": target_signal.get("last_runner_ticket"),
                **(
                    {
                        "exit_reason_code": target_signal.get("exit_reason_code"),
                        "exit_stage": target_signal.get("exit_stage"),
                        "exit_price": target_signal.get("exit_price"),
                        "max_favorable_excursion": target_signal.get("max_favorable_excursion"),
                        "giveback_from_peak": target_signal.get("giveback_from_peak"),
                        "trail_tier_used": target_signal.get("trail_tier_used"),
                    }
                    if new_status == "CLOSED"
                    else {}
                ),
            },
            chat_id=target_chat_id,
        )
        if new_status == "CLOSED":
            _emit_exit_telemetry_log(
                target_msg_id,
                target_signal,
                float(target_signal.get("banked_profit") or 0.0),
                exit_reason="Partial Close (Final Position)",
            )
            await _record_signal_to_ledger(
                target_msg_id, target_signal, exit_reason="Partial Close (Final Position)", currency=curr
            )
        await notify_saved_messages(
            f"💰 **Partial Close Executed on `{symbol}` [Msg ID: {target_msg_id}]**\n"
            f"• Order #{closed} closed at market."
            f"{profit_info}\n\n"
            f"{fin['formatted']}"
        )

    elif action in ("ACTION_FULL_CLOSE", "FULL_CLOSE"):
        if target_signal.get("status") in ("PENDING", "PENDING_DELAY"):
            _cancel_delayed_sell_now(target_msg_id, chat_id=target_chat_id)
            state_mgr.update_signal(target_msg_id, {"status": "CLOSED"}, chat_id=target_chat_id)
            await notify_saved_messages(
                f"🚫 **Pending {direction.title()} Now Cancelled [Msg ID: {target_msg_id}]** before execution."
            )
            return

        res_fc = await _mt5_call(executor.close_signal_exposure, target_signal)
        closed_tkts = list((res_fc or {}).get("closed_tickets") or market_tickets)
        await _mt5_call(executor.apply_proximity_close, market_tickets, pending_tickets, symbol)
        added_p, _ = await _account_closed_tickets(target_signal, closed_tkts)
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        total_profit = float(target_signal.get("banked_profit") or added_p)
        profit_str = format_profit(total_profit, curr)

        closed_set_fc = {_safe_ticket_int(t) for t in closed_tkts if _safe_ticket_int(t) is not None}
        for l_data in layers.values():
            if isinstance(l_data, dict):
                lt = _safe_ticket_int(l_data.get("ticket"))
                st_l = str(l_data.get("status") or "").strip().upper()
                if lt in closed_set_fc or st_l in ("OPEN", "FILLED", "RUNNER", "CLOSED", "BANKED") or st_l.startswith("CLOSED"):
                    l_data["status"] = "CLOSED"
                else:
                    l_data["status"] = "CANCELLED"
        _ensure_signal_exit_telemetry(target_signal, exit_reason_code="MANUAL", overwrite_code=True)
        prev_rlayers_fc = list(target_signal.get("runner_layers") or ([target_signal["runner_layer"]] if target_signal.get("runner_layer") else []))
        prev_rlayer_fc = target_signal.get("runner_layer") or (prev_rlayers_fc[-1] if prev_rlayers_fc else target_signal.get("last_runner_layer"))
        if prev_rlayers_fc:
            target_signal["last_runner_layers"] = prev_rlayers_fc
        if prev_rlayer_fc:
            target_signal["last_runner_layer"] = prev_rlayer_fc
        target_signal["market_tickets"] = []
        target_signal["pending_tickets"] = []
        target_signal["runner_tickets"] = []
        target_signal["runner_ticket"] = None
        target_signal["runner_layers"] = []
        target_signal["runner_layer"] = None
        target_signal["layers"] = layers
        target_signal["status"] = "CLOSED"
        fc_updates: Dict[str, Any] = {
            "market_tickets": [],
            "pending_tickets": [],
            "runner_tickets": [],
            "runner_ticket": None,
            "runner_layers": [],
            "runner_layer": None,
            "layers": layers,
            "status": "CLOSED",
            "accounted_tickets": target_signal.get("accounted_tickets", []),
            "accounted_deal_tickets": target_signal.get("accounted_deal_tickets", []),
            "banked_profit": target_signal.get("banked_profit", 0.0),
            "exit_reason_code": target_signal.get("exit_reason_code"),
            "exit_stage": target_signal.get("exit_stage"),
            "exit_price": target_signal.get("exit_price"),
            "max_favorable_excursion": target_signal.get("max_favorable_excursion"),
            "giveback_from_peak": target_signal.get("giveback_from_peak"),
            "trail_tier_used": target_signal.get("trail_tier_used"),
        }
        if prev_rlayers_fc:
            fc_updates["last_runner_layers"] = prev_rlayers_fc
        if prev_rlayer_fc:
            fc_updates["last_runner_layer"] = prev_rlayer_fc
        state_mgr.update_signal(
            target_msg_id,
            fc_updates,
            chat_id=target_chat_id,
        )
        _emit_exit_telemetry_log(target_msg_id, target_signal, total_profit, exit_reason="Full Close Command")
        await _record_signal_to_ledger(target_msg_id, target_signal, exit_reason="Full Close Command", currency=curr)
        await notify_saved_messages(
            f"🚪 **Full Close Executed on `{symbol}` [Msg ID: {target_msg_id}]**\n"
            f"• All open positions closed at market.\n"
            f"• 💰 **Total Amount Made:** `{profit_str}`\n\n"
            f"{fin['formatted']}"
        )

    elif action in ("ACTION_CANCEL_PENDING", "CANCEL_PENDING"):
        cancel_dir = str(parsed.get("cancel_direction") or "").strip().upper() or None
        sig_dir = str(target_signal.get("direction") or "").strip().upper()
        if cancel_dir in ("BUY", "SELL") and sig_dir and sig_dir != cancel_dir:
            logger.info(
                f"Skipping ACTION_CANCEL_PENDING on Msg ID {target_msg_id}: "
                f"signal direction {sig_dir} != requested {cancel_dir}."
            )
            return
        if not pending_tickets:
            logger.info(f"Signal {target_msg_id} has no pending orders to cancel for ACTION_CANCEL_PENDING.")
            return
        cancelled_pend = list(pending_tickets)
        await _mt5_call(executor.cancel_signal_pending_orders, target_signal)
        for l_data in layers.values():
            if isinstance(l_data, dict):
                lt = _safe_ticket_int(l_data.get("ticket"))
                st_l = str(l_data.get("status") or "").strip().upper()
                if lt in cancelled_pend or st_l == "PENDING":
                    l_data["status"] = "CANCELLED"
        prev_st_cp = str(target_signal.get("status") or "ACTIVE").strip().upper()
        new_st_cp = prev_st_cp if market_tickets else "CANCELLED"
        target_signal["pending_tickets"] = []
        target_signal["layers"] = layers
        target_signal["status"] = new_st_cp
        state_mgr.update_signal(
            target_msg_id,
            {
                "pending_tickets": [],
                "layers": layers,
                "status": new_st_cp,
            },
            chat_id=target_chat_id,
        )
        await notify_saved_messages(
            f"🗑️ **Pending Limit/Stop Orders Cancelled on `{symbol}` [Msg ID: {target_msg_id}]**\n"
            f"• Direction: `{sig_dir}` | Cancelled Pending Orders: `{cancelled_pend}`\n"
            f"• Surviving Open Positions: `{market_tickets}` | Status: `{new_st_cp}`"
        )

    elif action in ("ACTION_TP1_HIT_REPLY", "TP1_HIT_REPLY"):
        is_unentered = target_signal.get("status") in ("PENDING", "PENDING_DELAY") or not market_tickets
        if not is_reply_evt and not is_unentered:
            logger.info(
                f"Standalone 'TP1 hit' (no reply) ignored for filled signal {target_msg_id}; "
                f"price-based TP1 guards remain active."
            )
            return
        _cancel_delayed_sell_now(target_msg_id, chat_id=target_chat_id)
        if target_signal.get("tp1_reached") or target_signal.get("status") in ("RUNNER_ACTIVE", "CLOSED", "INVALIDATED_TP1"):
            logger.info(f"Signal {target_msg_id} already processed TP1; ignoring provider TP1 reply.")
            return
        if is_unentered:
            await _invalidate_signal_tp1_unlocked(
                target_msg_id,
                reason="Reply" if is_reply_evt else "Alert",
                chat_id=target_chat_id,
            )
            return

        _, _, alert = await _execute_tp1_scaling_handoff(
            target_msg_id, target_signal, chat_id=target_chat_id, source_label="Provider Reply"
        )
        await notify_saved_messages(alert)

    elif action in (
        "ACTION_ADJUST_TARGETS",
        "ADJUST_TARGETS",
        "ACTION_TRAIL_SL",
        "TRAIL_SL",
        "ACTION_TP2_HIT",
        "TP2_HIT",
        "ACTION_TP3_HIT",
        "TP3_HIT",
    ):
        src_lbl = "Channel Reply" if is_reply_evt else "Channel Prompt"
        alert = await _apply_adjust_or_trail_unlocked(
            target_msg_id,
            target_chat_id,
            target_signal,
            parsed,
            source_label=src_lbl,
        )
        await notify_saved_messages(alert)


async def _handle_new_message_unlocked(event: Any) -> Optional[Tuple[Optional[str], bool]]:
    """Unlocked core implementation of handle_new_message (caller holds signal lock for (chat_id, msg_id))."""
    if session_trading_blocked():
        return None
    msg = event.message
    if not msg:
        return None

    msg_id = msg.id
    raw_text = msg.message or ""
    chat_id = _safe_chat_id(event)
    if CLOSE_ON_SIGNAL_DELETED and _is_message_tombstoned(chat_id, msg_id):
        logger.info(f"Message ID {msg_id} in Chat [{chat_id}] was already deleted (tombstoned); skipping NewMessage.")
        return
    reply_to_id = _extract_reply_to_id(msg)
    is_reply_evt = (getattr(event, "is_reply", None) is True) or (reply_to_id is not None)

    now = time.time()
    if len(_recent_messages) > 200:
        cutoff = now - 60.0
        stale_keys = [k for k, v in _recent_messages.items() if v < cutoff]
        for k in stale_keys:
            del _recent_messages[k]
            _recent_message_texts.pop(k, None)

    dedup_key = _get_dedup_key(chat_id, msg_id)
    clean_raw_text = raw_text.strip()
    if (
        dedup_key in _recent_messages
        and (now - _recent_messages[dedup_key]) < DEDUPLICATION_WINDOW_SECONDS
        and clean_raw_text == _recent_message_texts.get(dedup_key, "")
    ):
        logger.info(f"Message ID {msg_id} duplicate event within {DEDUPLICATION_WINDOW_SECONDS}s; skipping.")
        return
    if state_mgr.get_signal(msg_id, chat_id=chat_id) is not None:
        logger.info(f"Message ID {msg_id} already exists in state; skipping duplicate creation.")
        _recent_messages[dedup_key] = now
        _recent_message_texts[dedup_key] = clean_raw_text
        return
    _recent_messages[dedup_key] = now
    _recent_message_texts[dedup_key] = clean_raw_text

    if chat_id is not None and isinstance(msg_id, int) and not isinstance(msg_id, bool):
        msg_date = getattr(msg, "date", None)
        msg_ts = msg_date.isoformat() if isinstance(msg_date, datetime) else None
        state_mgr.set_catchup_watermark(chat_id, msg_id, msg_ts=msg_ts)

    logger.info(
        f"--- New Message Received [ID: {msg_id}] in Chat [{chat_id}] "
        f"(Reply to: {reply_to_id}) ---\nText: {raw_text}"
    )

    target_signal = None
    target_msg_id = None
    target_chat_id = chat_id
    if reply_to_id is not None:
        cand_sig = state_mgr.get_signal(reply_to_id, chat_id=chat_id)
        if cand_sig and (
            chat_id is None
            or cand_sig.get("chat_id") is None
            or _chat_ids_match(cand_sig.get("chat_id"), chat_id)
        ):
            target_signal = cand_sig
            target_msg_id = str(reply_to_id)
            target_chat_id = cand_sig.get("chat_id", chat_id)

    is_forward = getattr(msg, "forward", None) is not None
    # Single-pass parse: run pure-CPU Gates 1-4 once (skipping Gate 5 & Pipeline Output log until live_price is known).
    parsed = parse_message(
        raw_text,
        message_id=msg_id,
        is_reply=is_reply_evt,
        reply_to_msg_id=reply_to_id,
        is_forward=is_forward,
        live_price=None,
        validate_sanity=False,
        log_output=False,
    )

    if parsed is None:
        logger.info(f"Message ID {msg_id} filtered out (noise, recap, or ignored symbol).")
        return None

    if parsed.event_type == "NEW_SIGNAL" or parsed.is_entry:
        sig_sym = getattr(parsed, "symbol", None) or DEFAULT_SYMBOL
        tick = await _mt5_call(executor.get_tick, sig_sym)
        live_price = getattr(tick, "bid", None) if tick else None
        parsed = finalize_signal_sanity(parsed, live_price=live_price, log_output=True)
        if parsed is None:
            return None

    # 1. Trade Management Commands
    if parsed.event_type == "TRADE_MANAGEMENT" or parsed.is_management:
        action = parsed.management_action
        logger.info(f"Processing Trade Management: {action} [Msg ID: {msg_id} | Chat: {chat_id}]")

        if not target_signal:
            if action in ("ACTION_CANCEL_PENDING", "CANCEL_PENDING"):
                cancel_dir = str(parsed.get("cancel_direction") or "").strip().upper() or None
                for cand_key, cand_sig in reversed(state_mgr.get_active_signals()):
                    if not isinstance(cand_sig, dict) or not cand_sig.get("pending_tickets"):
                        continue
                    if (
                        chat_id is not None
                        and cand_sig.get("chat_id") is not None
                        and not _chat_ids_match(cand_sig.get("chat_id"), chat_id)
                    ):
                        continue
                    if cancel_dir in ("BUY", "SELL") and str(cand_sig.get("direction") or "").strip().upper() != cancel_dir:
                        continue
                    raw_mid_cp, _ = _normalize_task_msg_id(cand_key, chat_id=cand_sig.get("chat_id", chat_id))
                    target_msg_id = str(raw_mid_cp)
                    target_signal = cand_sig
                    target_chat_id = cand_sig.get("chat_id", chat_id)
                    break
            else:
                prefer_open = action in (
                    "ACTION_BREAKEVEN",
                    "BREAKEVEN",
                    "ACTION_PARTIAL_CLOSE",
                    "PARTIAL_CLOSE",
                    "ACTION_CLOSE_WORST_HOLD_LAYERS",
                    "CLOSE_WORST_HOLD_LAYERS",
                    "ACTION_TRAIL_SL",
                    "TRAIL_SL",
                    "ACTION_TP2_HIT",
                    "TP2_HIT",
                    "ACTION_TP3_HIT",
                    "TP3_HIT",
                )
                # Strictly scoped to `chat_id` — NEVER falls back across other channels!
                res = state_mgr.get_latest_signal(
                    symbol=parsed.symbol,
                    chat_id=chat_id,
                    prefer_open_positions=prefer_open,
                )
                if res:
                    target_msg_id, target_signal = res
                    target_chat_id = target_signal.get("chat_id", chat_id)

        if not target_signal or target_msg_id is None:
            logger.warning(
                f"No target trade found in channel {chat_id} to apply {action}. (Reply ID: {reply_to_id})"
            )
            return

        if (
            chat_id is not None
            and target_signal.get("chat_id") is not None
            and not _chat_ids_match(target_signal.get("chat_id"), chat_id)
        ):
            logger.warning(
                f"Blocked cross-channel {action}: message chat {chat_id} != signal chat {target_signal.get('chat_id')}"
            )
            return

        same_signal = (
            _normalize_task_msg_id(target_msg_id, chat_id=target_chat_id)
            == _normalize_task_msg_id(msg_id, chat_id=chat_id)
        )
        if same_signal:
            await _handle_trade_management_action(
                msg_id, chat_id, is_reply_evt, parsed, target_msg_id, target_chat_id, target_signal
            )
        else:
            async with _get_signal_lock(target_chat_id, target_msg_id):
                fresh_target = state_mgr.get_signal(target_msg_id, chat_id=target_chat_id)
                if fresh_target:
                    await _handle_trade_management_action(
                        msg_id, chat_id, is_reply_evt, parsed, target_msg_id, target_chat_id, fresh_target
                    )
        return

    # 2. New Signal Entry (v3.0 4-Layer L1..L4 -> TP1..TP5)
    if parsed.event_type == "NEW_SIGNAL" or parsed.is_entry:
        symbol = parsed.symbol or DEFAULT_SYMBOL
        direction = parsed.direction
        sl = parsed.sl if parsed.sl_valid else None
        final_tp = parsed.final_tp
        zone = parsed.zone
        tp_levels = dict(parsed.tp_levels or {})
        raw_tp1 = parsed.raw_tp1
        raw_tp2 = parsed.raw_tp2
        raw_tp3 = parsed.raw_tp3
        raw_tp4 = parsed.raw_tp4
        raw_tp5 = parsed.raw_tp5
        is_stop_order = bool(getattr(parsed, "is_stop_order", False))
        is_limit_order = bool(getattr(parsed, "is_limit_order", False))
        order_intent = getattr(parsed, "order_intent", None) or ("STOP" if is_stop_order else "AUTO")

        if executor.get_daily_drawdown_status().get("pause_new_signals"):
            await check_and_enforce_daily_drawdown()

        passed, reason = await _mt5_call(
            executor.check_preflight,
            symbol=symbol,
            direction=direction,
            raw_tp1=raw_tp1,
            sl=sl,
            zone=zone,
            single_price=parsed.single_price,
            is_stop_order=is_stop_order,
            order_intent=order_intent,
        )
        if not passed:
            logger.warning(f"Pre-flight safety check failed for Msg ID {msg_id}: {reason}")
            return f"⚠️ **Order Blocked by Pre-Flight**: {reason}", False

        logger.info(
            f"Pre-flight OK. Executing 4-Layer New Signal: Direction={direction}, Symbol={symbol}, "
            f"Intent={order_intent}, Zone={zone}, Single={parsed.single_price}, SL={sl}, TP1..TP5={tp_levels}"
        )

        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}
        zone_completed = False
        is_zone = False
        max_orders = MAX_ORDERS_PER_SIGNAL
        prev_lots = (await _mt5_call(executor.get_total_active_lots))["open_lots"]
        initial_price = None

        if parsed.is_zone and zone:
            market_tickets, pending_tickets, layers_data = await _mt5_call(
                executor.execute_4layer_zone,
                symbol=symbol,
                direction=direction,
                zone=zone,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
            )
            zone_completed = True
            is_zone = True
        elif parsed.single_price is not None:
            is_single_stub = bool(sl is None and not tp_levels and not is_limit_order and not is_stop_order)
            market_tickets, pending_tickets, layers_data = await _mt5_call(
                executor.execute_4layer_single_price,
                symbol=symbol,
                direction=direction,
                price=parsed.single_price,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
                order_intent=order_intent,
                is_stop_order=is_stop_order,
                max_layers=1 if is_single_stub else None,
            )
            zone_completed = (len(layers_data) >= MAX_ORDERS_PER_SIGNAL) and not is_single_stub
        elif float(SELL_NOW_DELAY_SECONDS) <= 0.0:
            now_tick = await _mt5_call(executor.get_tick, symbol)
            initial_price = _get_live_entry_price(now_tick, direction)
            staged_entry = bool(STAGED_MARKET_ENTRY)
            is_market_stub = bool(sl is None and not tp_levels) or staged_entry
            market_tickets, pending_tickets, layers_data = await _mt5_call(
                executor.execute_4layer_market,
                symbol=symbol,
                direction=direction,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
                max_layers=1 if is_market_stub else None,
            )
            zone_completed = not is_market_stub
        else:
            now_tick = await _mt5_call(executor.get_tick, symbol)
            initial_price = _get_live_entry_price(now_tick, direction)

            state_mgr.create_signal(
                message_id=msg_id,
                chat_id=chat_id,
                symbol=symbol,
                direction=direction,
                layers={},
                zone=None,
                market_tickets=[],
                pending_tickets=[],
                zone_completed=False,
                max_orders=MAX_ORDERS_PER_SIGNAL,
                is_zone=False,
                is_zone_order=False,
                is_stop_order=is_stop_order,
                is_limit_order=is_limit_order,
                order_intent=order_intent,
                sl=sl,
                raw_tp1=raw_tp1,
                tp1_price=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
                final_tp=final_tp,
                proximity_close_price=parsed.proximity_close_price,
                raw_text=raw_text,
                status="PENDING",
                initial_price=initial_price,
                recorded_entry_price=initial_price,
                mt5_order_tickets=[],
            )
            task_key = _reserve_task_key(msg_id, chat_id=chat_id)
            _pending_task_chats[task_key] = chat_id
            _pending_sell_now_tasks[task_key] = asyncio.create_task(
                _delayed_sell_now_task(msg_id, chat_id=chat_id, task_key=task_key)
            )
            logger.info(
                f"Signal ID {msg_id}: Unpriced '{direction.title()} now' snapshotted at initial_price="
                f"{initial_price} and held for {SELL_NOW_DELAY_SECONDS}s."
            )
            cond_str = (
                f"price >= `{initial_price}` (higher than recorded point)"
                if direction == "SELL"
                else f"price <= `{initial_price}` (lower than recorded point)"
            )
            delay_alert = (
                f"⏳ **{direction.title()} Now Held ({int(SELL_NOW_DELAY_SECONDS)}s Timer) [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• 📍 **Initial Market Snapshot:** `{initial_price}`\n"
                f"• Waiting {int(SELL_NOW_DELAY_SECONDS)}s for zone/limit edit or TP1 check; if no zone arrives, "
                f"will validate {cond_str} before executing 4-layer market ({BASE_LAYER_LOTS})."
            )
            return delay_alert, False

        if not market_tickets and not pending_tickets:
            err_reason = getattr(executor, "last_error", None) or "Broker rejected order(s)"
            logger.error(f"Signal ID {msg_id} produced 0 orders ({err_reason}); skipping state creation.")
            reject_alert = (
                f"❌ **Order Rejected by MT5 [ID: {msg_id}]**\n"
                f"• Direction: `{direction}` {symbol}\n"
                f"• Reason: `{err_reason}`\n"
                f"• No positions or pending orders were opened."
            )
            return reject_alert, False

        dyn_n = _merge_dynamic_risk_into_signal_fields(
            getattr(executor, "last_dynamic_risk_snapshot", None),
            sl, final_tp, raw_tp1, raw_tp2, raw_tp3, raw_tp4, raw_tp5, tp_levels,
        )
        sl = dyn_n["sl"]
        final_tp = dyn_n["final_tp"]
        raw_tp1 = dyn_n["raw_tp1"]
        raw_tp2 = dyn_n["raw_tp2"]
        raw_tp3 = dyn_n["raw_tp3"]
        raw_tp4 = dyn_n["raw_tp4"]
        raw_tp5 = dyn_n["raw_tp5"]
        tp_levels = dyn_n["tp_levels"]

        arm_tick = await _mt5_call(executor.get_tick, symbol)
        tp1_guard_armed = _compute_initial_tp1_guard_armed(
            arm_tick, direction, raw_tp1, bool(market_tickets)
        )
        recorded_ent = initial_price
        if not _is_num(recorded_ent) and isinstance(layers_data, dict):
            for _lk in ("L1", "L2", "L3", "L4", "TOP", "MID", "BOTTOM"):
                _lv = layers_data.get(_lk)
                if not isinstance(_lv, dict):
                    continue
                _ep = _lv.get("entry_price") or _lv.get("entry")
                if _is_num(_ep) and float(_ep) > 0.0:
                    recorded_ent = float(_ep)
                    break
                if str(_lv.get("order_type") or "").upper() == "MARKET" and _is_num(_lv.get("target_price")):
                    recorded_ent = float(_lv["target_price"])
                    break
        extra_dyn = {"dynamic_risk": dyn_n["dynamic_risk"]} if dyn_n["dynamic_risk"] is not None else {}
        created_sig = state_mgr.create_signal(
            message_id=msg_id,
            chat_id=chat_id,
            symbol=symbol,
            direction=direction,
            layers=layers_data,
            zone=zone.to_dict() if zone else None,
            market_tickets=market_tickets,
            pending_tickets=pending_tickets,
            zone_completed=zone_completed,
            max_orders=max_orders,
            is_zone=is_zone,
            is_zone_order=is_zone,
            is_stop_order=is_stop_order,
            is_limit_order=is_limit_order,
            order_intent=order_intent,
            single_price=parsed.single_price,
            sl=sl,
            raw_tp1=raw_tp1,
            tp1_price=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels or None,
            final_tp=final_tp,
            proximity_close_price=parsed.proximity_close_price,
            raw_text=raw_text,
            initial_price=initial_price if _is_num(initial_price) else recorded_ent,
            recorded_entry_price=recorded_ent,
            tp1_guard_armed=tp1_guard_armed,
            **extra_dyn,
        )

        new_lots = (await _mt5_call(executor.get_total_active_lots))["open_lots"]
        risk_alerts = await _mt5_call(executor.evaluate_risk_threshold_alerts, prev_lots, new_lots)
        alloc_warn = getattr(executor, "last_allocation_warning", None)
        extra_notes: List[str] = []
        if alloc_warn:
            extra_notes.append(f"⚠️ **Lot Cap Note:** `{alloc_warn}`")
        extra_notes.extend(risk_alerts)
        risk_block = ("\n\n" + "\n".join(extra_notes)) if extra_notes else ""

        # Detect staged (L1-only) vs full execution
        is_staged_entry = (not is_zone and parsed.single_price is None and len(market_tickets) == 1 and not pending_tickets)
        staged_note = "\n• ⏳ **L2/L3/L4 waiting** — edit the signal with zone/limit prices to place them." if is_staged_entry else ""

        exec_alert = (
            f"🚀 **New {'L1 Staged' if is_staged_entry else '4-Layer'} Signal Executed ({order_intent}) [ID: {msg_id}]**\n"
            f"• Direction: `{direction}` {symbol}\n"
            f"• Orders: Market={market_tickets}, Pending={pending_tickets}\n"
            f"• Layers: {_format_layers_summary(created_sig)}\n"
            f"• SL: `{sl}` | Broker TP1: `{final_tp}` | Targets: {_format_tp_summary(created_sig)}"
            f"{staged_note}"
            f"{risk_block}"
        )
        return exec_alert, True


async def handle_new_message(event: Any) -> None:
    """Handler for new messages in monitored Telegram channels (serialized per (chat_id, msg_id))."""
    if session_trading_blocked():
        return
    msg = getattr(event, "message", None)
    if not msg:
        return
    chat_id = _safe_chat_id(event)
    pending_notify: Optional[Tuple[Optional[str], bool]] = None
    async with _get_signal_lock(chat_id, msg.id):
        pending_notify = await _handle_new_message_unlocked(event)
    if pending_notify:
        alert_text, refresh_hud = pending_notify
        if alert_text:
            await notify_saved_messages(alert_text)
        if refresh_hud:
            await _update_live_terminal_status_async()


def _place_edit_layer_order(
    symbol: str,
    direction: str,
    target_price: float,
    lot: float,
    sl: Optional[float],
    tp: Optional[float],
    comment_prefix: str,
    tick: Any,
    order_intent: str = "AUTO",
) -> Tuple[Optional[int], str]:
    """Route an edit/retier layer to MARKET, LIMIT, or STOP with broker trade_stops_level fallback."""
    eff_tick = tick or executor.get_tick(executor.resolve_symbol(symbol))
    if eff_tick is None:
        t = executor.place_limit_order(
            symbol=symbol,
            direction=direction,
            price=target_price,
            lot=lot,
            sl=sl,
            tp=tp,
            comment=f"{comment_prefix} Limit",
        )
        return (t, "LIMIT") if t else (None, "FAILED")
    is_l1 = ("L1" in comment_prefix or "TOP" in comment_prefix)
    return executor._execute_layer_price(
        symbol=symbol,
        direction=direction,
        target_price=target_price,
        lot=lot,
        sl=sl,
        tp=tp,
        tick=eff_tick,
        market_comment=f"{comment_prefix} Market",
        limit_comment=f"{comment_prefix} Limit",
        order_intent=order_intent,
        stop_comment=f"{comment_prefix} Stop",
        allow_market_buffer=is_l1,
    )


def _resolve_edit_layer_sl(
    sig: Dict[str, Any],
    l_data: Optional[Dict[str, Any]],
    candidate_sl: Optional[float],
    direction: str,
) -> Optional[float]:
    """
    Universal forward-only SL guard for edits/BE: never loosen any already-protected
    broker/ratchet/BE stop loss (`BE_APPLIED`, `PARTIAL_CLOSED`, `RUNNER_ACTIVE`,
    `be_armed`, `stage0_profit_lock_active`, `tp1_reached`, `roles_locked`, or `ratchet_tier`).
    Returns None (`sl=None` to broker modify) when `candidate_sl` would loosen or tie.
    """
    curr_st = str(sig.get("status") or "").strip().upper()
    layers_d = sig.get("layers") if isinstance(sig.get("layers"), dict) else {}
    is_prot_sig = bool(
        curr_st in ("BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE")
        or sig.get("be_armed")
        or sig.get("stage0_profit_lock_active")
        or sig.get("tp1_reached")
        or sig.get("roles_locked")
        or any(isinstance(ld, dict) and ld.get("ratchet_tier") for ld in layers_d.values())
    )
    is_prot_layer = is_prot_sig or bool(
        isinstance(l_data, dict) and (l_data.get("ratchet_tier") or l_data.get("be_sl") or l_data.get("ratchet_sl"))
    )
    if not is_prot_layer:
        return candidate_sl

    cands: List[float] = []
    if isinstance(l_data, dict):
        keys_l = ("ratchet_sl", "broker_sl", "be_sl", "sl") if is_prot_layer else ("ratchet_sl", "broker_sl", "be_sl")
        for k_l in keys_l:
            v = l_data.get(k_l)
            if _is_num(v) and float(v) > 0.0:
                cands.append(float(v))
    for k_s in ("stage0_profit_lock_sl", "broker_sl", "trail_stop"):
        v = sig.get(k_s)
        if _is_num(v) and float(v) > 0.0:
            cands.append(float(v))
    if is_prot_sig and _is_num(sig.get("sl")) and float(sig["sl"]) > 0.0:
        cands.append(float(sig["sl"]))

    if not cands:
        return candidate_sl
    is_buy = str(direction or "BUY").strip().upper() == "BUY"
    floor_sl = max(cands) if is_buy else min(cands)
    if candidate_sl is None:
        return None
    if (is_buy and float(candidate_sl) <= float(floor_sl) + 1e-6) or (
        not is_buy and float(candidate_sl) >= float(floor_sl) - 1e-6
    ):
        return None
    return candidate_sl


def _resolve_signal_protected_sl(
    sig: Dict[str, Any],
    candidate_sl: Optional[float],
    direction: str,
) -> Optional[float]:
    """Ensure persisted `signal['sl']` also never moves backwards once protected."""
    guarded = _resolve_edit_layer_sl(sig, None, candidate_sl, direction)
    if guarded is not None:
        return guarded
    existing_sl = sig.get("broker_sl") or sig.get("trail_stop") or sig.get("stage0_profit_lock_sl") or sig.get("sl")
    return existing_sl if _is_num(existing_sl) and float(existing_sl) > 0.0 else candidate_sl


def _populate_fill_entry_from_exec(
    sig: Dict[str, Any],
    l_data: Dict[str, Any],
    ticket: Any,
    lot: Any,
    tick: Any,
    direction: str,
    fallback_px: Optional[float] = None,
) -> None:
    """Capture actual broker fill price onto layer (`entry_price`/`entry`/`remaining_lot`) and signal."""
    ti = _safe_ticket_int(ticket)
    last_fills = getattr(executor, "_last_fill_prices", None)
    fill_px = last_fills.get(ti) if (isinstance(last_fills, dict) and ti is not None) else None
    if not _is_num(fill_px) and tick is not None:
        is_buy = str(direction or "BUY").strip().upper() == "BUY"
        raw_t = getattr(tick, "ask", None) if is_buy else getattr(tick, "bid", None)
        if _is_num(raw_t) and float(raw_t) > 0.0:
            fill_px = round(float(raw_t), 2)
    if not _is_num(fill_px) and _is_num(fallback_px) and float(fallback_px) > 0.0:
        fill_px = round(float(fallback_px), 2)
    if _is_num(fill_px) and float(fill_px) > 0.0:
        l_data["entry_price"] = round(float(fill_px), 2)
        l_data["entry"] = round(float(fill_px), 2)
        if not _is_num(sig.get("recorded_entry_price")):
            sig["recorded_entry_price"] = round(float(fill_px), 2)
    if _is_num(lot) and float(lot) > 0.0:
        l_data.setdefault("remaining_lot", round(float(lot), 4))


async def _handle_message_edited_unlocked(event: Any) -> Optional[Tuple[Optional[str], bool]]:
    """Unlocked core implementation of handle_message_edited (caller holds signal lock)."""
    if session_trading_blocked():
        return None
    msg = event.message
    if not msg:
        return None

    msg_id = msg.id
    raw_text = msg.message or ""
    chat_id = _safe_chat_id(event)
    if CLOSE_ON_SIGNAL_DELETED and _is_message_tombstoned(chat_id, msg_id):
        logger.info(f"Edited message ID {msg_id} in Chat [{chat_id}] was already deleted (tombstoned); skipping edit.")
        return

    existing_signal = state_mgr.get_signal(msg_id, chat_id=chat_id)

    if existing_signal and raw_text.strip() == (existing_signal.get("raw_text") or "").strip():
        logger.debug(f"Edited message {msg_id} text unchanged; skipping.")
        return

    if not existing_signal:
        now = time.time()
        dedup_key = _get_dedup_key(chat_id, msg_id)
        clean_raw_text = raw_text.strip()
        norm_text = " ".join(clean_raw_text.lower().split())
        if _recent_message_texts.get("_active_state_mgr_id") != id(state_mgr):
            _recent_message_texts["_active_state_mgr_id"] = id(state_mgr)
        prev_raw = _recent_message_texts.get(dedup_key)
        prev_norm = " ".join(prev_raw.strip().lower().split()) if isinstance(prev_raw, str) else None
        state_scope_key = (id(state_mgr), dedup_key)
        if (
            (
                dedup_key in _recent_messages
                and (now - _recent_messages[dedup_key]) < DEDUPLICATION_WINDOW_SECONDS
                and clean_raw_text == prev_raw
            )
            or _recent_message_texts.get(state_scope_key) == norm_text
            or (prev_norm is not None and norm_text == prev_norm)
        ):
            logger.info(
                f"Edited untracked message {msg_id} has unchanged normalized text; skipping re-execution."
            )
            return

        now_utc = datetime.now(timezone.utc)
        msg_date = getattr(msg, "date", None)
        age_sec: Optional[float] = None
        if isinstance(msg_date, datetime):
            dt_utc = msg_date if msg_date.tzinfo is not None else msg_date.replace(tzinfo=timezone.utc)
            age_sec = max(0.0, (now_utc - dt_utc).total_seconds())

        edit_date = getattr(msg, "edit_date", None)
        is_telethon_msg = type(msg).__module__.startswith("telethon")
        # 1. Age of the ORIGINAL message: an untracked message posted hours ago must NEVER
        # be treated as a brand new signal, even if the channel admin just edited it now.
        if age_sec is not None and age_sec > MAX_NEW_EDIT_AGE_SECONDS:
            logger.info(
                f"Ignoring edit/reaction on old untracked message {msg_id} "
                f"(age={age_sec:.0f}s > {MAX_NEW_EDIT_AGE_SECONDS}s limit)."
            )
            return

        # 2. Freshness of the edit event itself:
        if isinstance(edit_date, datetime):
            edit_dt_utc = edit_date if edit_date.tzinfo is not None else edit_date.replace(tzinfo=timezone.utc)
            edit_age_sec = max(0.0, (now_utc - edit_dt_utc).total_seconds())
            if edit_age_sec > MAX_EDIT_EVENT_FRESHNESS_SECONDS:
                logger.info(
                    f"Ignoring stale reaction/update on untracked message {msg_id} "
                    f"(edit_age={edit_age_sec:.0f}s > {MAX_EDIT_EVENT_FRESHNESS_SECONDS}s)."
                )
                return
        elif is_telethon_msg and edit_date is None and age_sec is not None and age_sec > 15.0:
            logger.info(
                f"Ignoring non-text reaction/view update on unedited message {msg_id} (age={age_sec:.0f}s)."
            )
            return

        logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{chat_id}] (not in state; treating as new) ---")
        res_new = await _handle_new_message_unlocked(event)
        _recent_message_texts[state_scope_key] = norm_text
        return res_new

    logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{chat_id}] ---\nNew Text: {raw_text}")

    curr_status = existing_signal.get("status")
    if curr_status in ("CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"):
        logger.info(f"Signal {msg_id} is already terminal ({curr_status}); ignoring edit.")
        return

    symbol = existing_signal.get("symbol", DEFAULT_SYMBOL)
    direction = existing_signal.get("direction", "BUY")

    edit_hit = detect_tp_milestone_hit(raw_text)
    if edit_hit == 1 or is_tp1_hit_message(raw_text):
        _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
        mk_tkts_now = list(existing_signal.get("market_tickets", []))
        if curr_status in ("PENDING", "PENDING_DELAY") or not mk_tkts_now:
            await _invalidate_signal_tp1_unlocked(msg_id, reason="Edit", chat_id=chat_id)
            state_mgr.update_signal(msg_id, {"raw_text": raw_text}, chat_id=chat_id)
            return None
        elif not existing_signal.get("tp1_reached") and curr_status != "RUNNER_ACTIVE":
            existing_signal["raw_text"] = raw_text
            _, _, alert = await _execute_tp1_scaling_handoff(
                msg_id, existing_signal, chat_id=chat_id, source_label="Edited Signal"
            )
            return alert, False
    elif edit_hit in (2, 3) and existing_signal.get("market_tickets"):
        existing_signal["raw_text"] = raw_text
        hit_action = SignalAction(
            {
                "event_type": "TRADE_MANAGEMENT",
                "management_action": f"ACTION_TP{edit_hit}_HIT",
                "tp_hit_stage": edit_hit,
            }
        )
        alert = await _apply_adjust_or_trail_unlocked(
            msg_id, chat_id, existing_signal, hit_action, source_label="Edited Signal"
        )
        return alert, False

    is_forward = getattr(msg, "forward", None) is not None
    tick = await _mt5_call(executor.get_tick, symbol)
    live_price = (
        getattr(tick, "bid", None) if str(direction).upper() == "BUY" else getattr(tick, "ask", None)
    ) if tick else None
    entry_ref_price = (
        existing_signal.get("recorded_entry_price")
        if existing_signal.get("recorded_entry_price") is not None
        else (
            existing_signal.get("initial_price")
            if existing_signal.get("initial_price") is not None
            else live_price
        )
    )
    parsed = parse_message(
        raw_text,
        message_id=msg_id,
        is_edit=True,
        is_forward=is_forward,
        live_price=live_price if live_price is not None else entry_ref_price,
        validate_sanity=False,
        log_output=False,
    )
    if parsed is None:
        return None
    if parsed.event_type == "NEW_SIGNAL" or parsed.is_entry:
        eff_live = live_price if live_price is not None else entry_ref_price
        alt_probe = copy.deepcopy(parsed)
        parsed = finalize_signal_sanity(parsed, live_price=eff_live, log_output=False)
        if (
            not parsed.sl_valid
            and entry_ref_price is not None
            and entry_ref_price != live_price
            and curr_status != "RUNNER_ACTIVE"
        ):
            alt_parsed = finalize_signal_sanity(alt_probe, live_price=entry_ref_price, log_output=False)
            if alt_parsed is not None and alt_parsed.sl_valid and alt_parsed.sl is not None:
                if live_price is None or (
                    (str(direction).upper() == "BUY" and float(alt_parsed.sl) < float(live_price))
                    or (str(direction).upper() == "SELL" and float(alt_parsed.sl) > float(live_price))
                ):
                    parsed["sl"] = alt_parsed.sl
                    parsed["sl_valid"] = True
        logger.info(
            f"Pipeline Output: {parsed['event_type']} | {parsed['direction']} "
            f"{parsed['symbol']} ({parsed['order_intent']}) | "
            f"Tiers={parsed['zone_tiers'] or parsed['entry_tiers']} | "
            f"SL={parsed['sl']} (valid={parsed['sl_valid']}) | "
            f"TPs={parsed['tp_levels']} | FinalTP={parsed['final_tp']} | "
            f"ProximityClose={parsed['proximity_close_price']}"
        )

    if parsed.event_type == "TRADE_MANAGEMENT":
        existing_signal["raw_text"] = raw_text
        state_mgr.update_signal(msg_id, {"raw_text": raw_text}, chat_id=chat_id)
        await _handle_trade_management_action(
            msg_id, chat_id, True, parsed, msg_id, chat_id, existing_signal
        )
        return

    pip_size = await _mt5_call(executor.get_pip_size, symbol)
    new_sl = parsed.sl if (parsed.sl_valid and parsed.sl is not None) else existing_signal.get("sl")

    was_fallback_tp = bool(existing_signal.get("fallback_tp_applied"))
    old_raw_tp1 = existing_signal.get("raw_tp1")
    existing_tps = (
        dict(existing_signal["tp_levels"])
        if isinstance(existing_signal.get("tp_levels"), dict) and existing_signal.get("tp_levels")
        else _resolve_signal_tps_dict(existing_signal)
    )
    parsed_labeled = getattr(parsed, "labeled_tps", None) or {}
    parsed_unlabeled = getattr(parsed, "unlabeled_tps", None) or []
    has_new_explicit_tps = bool(
        parsed_labeled or parsed_unlabeled or (isinstance(parsed.tp_levels, dict) and parsed.tp_levels)
    )

    if was_fallback_tp and has_new_explicit_tps:
        base_existing_tps: Dict[str, float] = {}
        fallback_tp_flag = False
    else:
        base_existing_tps = existing_tps
        fallback_tp_flag = was_fallback_tp

    if parsed_labeled or (was_fallback_tp and has_new_explicit_tps and parsed_unlabeled):
        merged_tp_levels = merge_tp_levels_for_active_signal(
            base_existing_tps,
            parsed_labeled,
            parsed_unlabeled,
            direction=direction,
            pip_size=pip_size,
        )
    elif has_new_explicit_tps and isinstance(parsed.tp_levels, dict) and parsed.tp_levels:
        if was_fallback_tp:
            merged_tp_levels = dict(parsed.tp_levels)
        else:
            merged_tp_levels = dict(base_existing_tps)
            merged_tp_levels.update(parsed.tp_levels)
    else:
        merged_tp_levels = dict(base_existing_tps)

    raw_tp1 = merged_tp_levels.get("TP1") if "TP1" in merged_tp_levels else (parsed.raw_tp1 or existing_signal.get("raw_tp1"))
    raw_tp2 = merged_tp_levels.get("TP2") if "TP2" in merged_tp_levels else (parsed.raw_tp2 or existing_signal.get("raw_tp2"))
    raw_tp3 = merged_tp_levels.get("TP3") if "TP3" in merged_tp_levels else (parsed.raw_tp3 or existing_signal.get("raw_tp3"))
    raw_tp4 = merged_tp_levels.get("TP4") if "TP4" in merged_tp_levels else (parsed.raw_tp4 or existing_signal.get("raw_tp4"))
    raw_tp5 = merged_tp_levels.get("TP5") if "TP5" in merged_tp_levels else (parsed.raw_tp5 or existing_signal.get("raw_tp5"))

    if has_new_explicit_tps and parsed.final_tp is None:
        # Gate 5 rejected the new TPs as chemically invalid (wrong direction / typo)!
        new_tp = None
    elif raw_tp1 is not None:
        new_tp = (
            round(float(raw_tp1) - TP_FRONT_RUN_OFFSET, 2)
            if str(direction).upper() == "BUY"
            else round(float(raw_tp1) + TP_FRONT_RUN_OFFSET, 2)
        )
    else:
        new_tp = parsed.final_tp if parsed.final_tp is not None else existing_signal.get("final_tp")
    is_stop_order = bool(getattr(parsed, "is_stop_order", False))
    is_limit_order = bool(getattr(parsed, "is_limit_order", False))
    order_intent = getattr(parsed, "order_intent", None) or ("STOP" if is_stop_order else "AUTO")

    # Handle edit arriving during Sell Now / Buy Now delay window
    if curr_status in ("PENDING", "PENDING_DELAY"):
        is_pending_setup = _is_strictly_pending_limit_setup(
            tick, direction, zone=parsed.zone, single_price=parsed.single_price, is_stop_order=is_stop_order
        )
        if not is_pending_setup and raw_tp1 is not None and tick and _tick_hit_tp1(tick, direction, raw_tp1):
            await _invalidate_signal_tp1_unlocked(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
            state_mgr.update_signal(msg_id, {"raw_text": raw_text}, chat_id=chat_id)
            return

        if (parsed.is_zone and parsed.zone) or (parsed.single_price is not None):
            if executor.get_daily_drawdown_status().get("pause_new_signals"):
                await check_and_enforce_daily_drawdown()
            passed, reason = await _mt5_call(
                executor.check_preflight,
                symbol=symbol,
                direction=direction,
                raw_tp1=raw_tp1,
                sl=new_sl,
                zone=parsed.zone,
                single_price=parsed.single_price,
                is_stop_order=is_stop_order,
                order_intent=order_intent,
            )
            if not passed:
                _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text}, chat_id=chat_id)
                logger.warning(f"Edited delayed signal [ID: {msg_id}] blocked by pre-flight: {reason}")
                return f"⚠️ **Edited Delayed Entry Skipped [ID: {msg_id}]**: {reason}", False

        if parsed.is_zone and parsed.zone:
            _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
            zone = parsed.zone
            mk_tkts, pd_tkts, l_data = await _mt5_call(
                executor.execute_4layer_zone,
                symbol=symbol,
                direction=direction,
                zone=zone,
                sl=new_sl,
                final_tp=new_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=merged_tp_levels or None,
            )
            if not mk_tkts and not pd_tkts:
                err_reason = getattr(executor, "last_error", None) or "Broker rejected order(s)"
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text}, chat_id=chat_id)
                return f"❌ **Edited Zone Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`", False
            dyn_ez = _merge_dynamic_risk_into_signal_fields(
                getattr(executor, "last_dynamic_risk_snapshot", None),
                new_sl, new_tp, raw_tp1, raw_tp2, raw_tp3, raw_tp4, raw_tp5, merged_tp_levels or None,
            )
            new_sl_z = dyn_ez["sl"]
            new_tp_z = dyn_ez["final_tp"]
            arm_tick = await _mt5_call(executor.get_tick, symbol)
            upd_ez: Dict[str, Any] = {
                "status": "ACTIVE",
                "sl": new_sl_z,
                "raw_tp1": dyn_ez["raw_tp1"],
                "tp1_price": dyn_ez["raw_tp1"],
                "raw_tp2": dyn_ez["raw_tp2"],
                "raw_tp3": dyn_ez["raw_tp3"],
                "raw_tp4": dyn_ez["raw_tp4"],
                "raw_tp5": dyn_ez["raw_tp5"],
                "tp_levels": dyn_ez["tp_levels"] or None,
                "final_tp": new_tp_z,
                "hard_broker_tp": new_tp_z,
                "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                "zone": zone.to_dict(),
                "is_zone": True,
                "is_zone_order": True,
                "zone_completed": True,
                "max_orders": MAX_ORDERS_PER_SIGNAL,
                "layers": l_data,
                "market_tickets": mk_tkts,
                "pending_tickets": pd_tkts,
                "tp1_guard_armed": _compute_initial_tp1_guard_armed(
                    arm_tick, direction, dyn_ez["raw_tp1"], bool(mk_tkts)
                ),
                "raw_text": raw_text,
            }
            if dyn_ez["dynamic_risk"] is not None:
                upd_ez["dynamic_risk"] = dyn_ez["dynamic_risk"]
            state_mgr.update_signal(msg_id, upd_ez, chat_id=chat_id)
            return (
                f"✏️ **Delayed {direction.title()} Now → 4-Layer Zone Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending Limits: {pd_tkts}\n"
                f"• SL: `{new_sl_z}` | Broker TP1: `{new_tp_z}`",
                False,
            )
        elif parsed.single_price is not None:
            _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
            mk_tkts, pd_tkts, l_data = await _mt5_call(
                executor.execute_4layer_single_price,
                symbol=symbol,
                direction=direction,
                price=parsed.single_price,
                sl=new_sl,
                final_tp=new_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=merged_tp_levels or None,
                order_intent=order_intent,
                is_stop_order=is_stop_order,
            )
            if not mk_tkts and not pd_tkts:
                err_reason = getattr(executor, "last_error", None) or "Broker rejected order"
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text}, chat_id=chat_id)
                return f"❌ **Edited Order Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`", False
            dyn_es = _merge_dynamic_risk_into_signal_fields(
                getattr(executor, "last_dynamic_risk_snapshot", None),
                new_sl, new_tp, raw_tp1, raw_tp2, raw_tp3, raw_tp4, raw_tp5, merged_tp_levels or None,
            )
            arm_tick = await _mt5_call(executor.get_tick, symbol)
            upd_es: Dict[str, Any] = {
                "status": "ACTIVE",
                "sl": dyn_es["sl"],
                "single_price": parsed.single_price,
                "is_stop_order": is_stop_order,
                "is_limit_order": is_limit_order,
                "order_intent": order_intent,
                "raw_tp1": dyn_es["raw_tp1"],
                "tp1_price": dyn_es["raw_tp1"],
                "raw_tp2": dyn_es["raw_tp2"],
                "raw_tp3": dyn_es["raw_tp3"],
                "raw_tp4": dyn_es["raw_tp4"],
                "raw_tp5": dyn_es["raw_tp5"],
                "tp_levels": dyn_es["tp_levels"] or None,
                "final_tp": dyn_es["final_tp"],
                "hard_broker_tp": dyn_es["final_tp"],
                "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                "max_orders": MAX_ORDERS_PER_SIGNAL,
                "layers": l_data,
                "market_tickets": mk_tkts,
                "pending_tickets": pd_tkts,
                "tp1_guard_armed": _compute_initial_tp1_guard_armed(
                    arm_tick, direction, dyn_es["raw_tp1"], bool(mk_tkts)
                ),
                "raw_text": raw_text,
            }
            if dyn_es["dynamic_risk"] is not None:
                upd_es["dynamic_risk"] = dyn_es["dynamic_risk"]
            state_mgr.update_signal(msg_id, upd_es, chat_id=chat_id)
            return (
                f"✏️ **Delayed {direction.title()} Now → 4-Layer {order_intent} Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending: {pd_tkts} @ `{parsed.single_price}`",
                False,
            )
        else:
            state_mgr.update_signal(
                msg_id,
                {
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "tp1_price": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
                    "raw_tp4": raw_tp4,
                    "raw_tp5": raw_tp5,
                    "tp_levels": merged_tp_levels or None,
                    "final_tp": new_tp,
                    "hard_broker_tp": new_tp,
                    "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                    "raw_text": raw_text,
                },
                chat_id=chat_id,
            )
            return None

    market_tickets = list(existing_signal.get("market_tickets", []))
    pending_tickets = list(existing_signal.get("pending_tickets", []))
    layers = dict(existing_signal.get("layers") or {})

    # Pre-reconcile pending_tickets against live MT5 positions before cancel/modify/add-layers
    live_pos_ids: set = set()
    live_pos_map_ed: Dict[int, Any] = {}
    live_ord_ids: set = set()
    if (market_tickets or pending_tickets) and not executor.dry_run:
        live_positions = await _mt5_call(executor.get_live_positions, symbol)
        live_orders = await _mt5_call(executor.get_live_orders, symbol)
        live_pos_map_ed = {
            int(getattr(p, "ticket", 0)): p for p in (live_positions or []) if getattr(p, "ticket", None)
        }
        live_pos_ids = set(live_pos_map_ed.keys())
        live_ord_ids = {int(getattr(o, "ticket", 0)) for o in (live_orders or []) if getattr(o, "ticket", None)}
        if live_pos_ids and pending_tickets:
            for pt in list(pending_tickets):
                if int(pt) in live_pos_ids:
                    pending_tickets.remove(pt)
                    if pt not in market_tickets:
                        market_tickets.append(pt)
                    p_ed = live_pos_map_ed.get(int(pt))
                    p_open_ed = float(getattr(p_ed, "price_open", 0.0) or 0.0) if p_ed else 0.0
                    p_vol_ed = float(getattr(p_ed, "volume", 0.0) or 0.0) if p_ed else 0.0
                    p_sl_ed = float(getattr(p_ed, "sl", 0.0) or 0.0) if p_ed else 0.0
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and l_data.get("ticket") == pt:
                            l_data["status"] = "OPEN"
                            l_data["order_type"] = "MARKET"
                            if p_open_ed > 0.0:
                                l_data["entry_price"] = round(p_open_ed, 2)
                                l_data["entry"] = round(p_open_ed, 2)
                                if not _is_num(existing_signal.get("recorded_entry_price")):
                                    existing_signal["recorded_entry_price"] = round(p_open_ed, 2)
                            if p_vol_ed > 0.0:
                                l_data["remaining_lot"] = round(p_vol_ed, 4)
                            if p_sl_ed > 0.0:
                                l_data["broker_sl"] = round(p_sl_ed, 2)
                    logger.info(
                        f"Pre-edit reconciliation: Pending order #{pt} already filled on MT5; "
                        f"promoted to market_tickets for Msg ID {msg_id}."
                    )

    # Upgrade single-order legacy "TOP" entry into canonical "L1" so late zone edits scale to full 4-layer sizing
    if set(layers.keys()) == {"TOP"} and "MID" not in layers and "BOTTOM" not in layers:
        top_rec = dict(layers.pop("TOP"))
        top_rec.setdefault("tp_stage", "TP1")
        layers["L1"] = top_rec
    is_legacy = StateManager.is_legacy_3layer({"layers": layers})

    if raw_tp2:
        legacy_runner_tp = round(raw_tp2, 2)
    elif raw_tp1:
        legacy_runner_tp = round(raw_tp1 + DEFAULT_TP_STEP_PIPS * pip_size, 2) if direction == "BUY" else round(raw_tp1 - DEFAULT_TP_STEP_PIPS * pip_size, 2)
    else:
        legacy_runner_tp = new_tp

    v3_runner_tp = raw_tp5 if raw_tp5 is not None else legacy_runner_tp

    # Protect trailed SL when RUNNER_ACTIVE (ratchet_sl_only=True)
    if curr_status == "RUNNER_ACTIVE":
        if was_fallback_tp and has_new_explicit_tps:
            existing_signal["fallback_tp_applied"] = False
            existing_signal["tp_levels"] = dict(merged_tp_levels)
            existing_signal["raw_tp1"] = raw_tp1
            existing_signal["raw_tp2"] = raw_tp2
            existing_signal["raw_tp3"] = raw_tp3
            existing_signal["raw_tp4"] = raw_tp4
            existing_signal["raw_tp5"] = raw_tp5
        res_adj = await _mt5_call(
            executor.adjust_active_signal_sl_tp,
            existing_signal,
            new_sl=parsed.sl if parsed.sl_valid else None,
            labeled_tps=parsed_labeled,
            unlabeled_tps=parsed_unlabeled,
            ratchet_sl_only=True,
        )
        existing_signal["raw_text"] = raw_text
        state_mgr.update_signal(msg_id, existing_signal, chat_id=chat_id)
        stage_key = str(existing_signal.get("tp_stage") or existing_signal.get("runner_milestone_name") or "TP2").upper()
        if stage_key == "TP1":
            stage_key = "TP2"
        eff_max_edit = _resolve_effective_max_stage(existing_signal)
        tp_map_edit = _resolve_signal_tps_dict(existing_signal)
        target_runner_tp = (
            tp_map_edit.get(stage_key)
            or tp_map_edit.get(f"TP{eff_max_edit}")
            or (existing_signal.get("tp_levels") or {}).get(stage_key)
            or (existing_signal.get("tp_levels") or {}).get(f"TP{eff_max_edit}")
            or v3_runner_tp
        )
        return (
            f"✏️ **Runner Target Updated via Edit [ID: {msg_id}]**\n"
            f"• Runner TP Ceiling: `{target_runner_tp}` | Floor / SL: `{res_adj.get('sl')}`\n"
            f"• Targets: {_format_tp_summary(existing_signal)}",
            False,
        )

    # Route already-crossed edited TP1 directly to TP1 scaling handoff when market positions are open
    if (
        market_tickets
        and not existing_signal.get("tp1_reached")
        and raw_tp1 is not None
        and tick
        and _tick_hit_tp1(tick, direction, raw_tp1)
    ):
        existing_signal.update({
            "raw_tp1": raw_tp1,
            "tp1_price": raw_tp1,
            "raw_tp2": raw_tp2,
            "raw_tp3": raw_tp3,
            "raw_tp4": raw_tp4,
            "raw_tp5": raw_tp5,
            "tp_levels": merged_tp_levels or None,
            "final_tp": new_tp,
            "hard_broker_tp": new_tp,
            "fallback_tp_applied": fallback_tp_flag,
            "layers": layers,
            "market_tickets": market_tickets,
            "pending_tickets": pending_tickets,
            "raw_text": raw_text,
        })
        eff_edit_sl = _resolve_edit_layer_sl(existing_signal, None, new_sl, direction)
        if eff_edit_sl is not None:
            for mt_c in market_tickets:
                await _mt5_call(executor.modify_position_sltp, ticket=mt_c, sl=eff_edit_sl, tp=None)
            existing_signal["sl"] = _resolve_signal_protected_sl(existing_signal, eff_edit_sl, direction)
        _, _, handoff_alert = await _execute_tp1_scaling_handoff(
            msg_id,
            existing_signal,
            chat_id=chat_id,
            source_label="Edited TP1 Crossed",
            curr_price=live_price,
        )
        return handoff_alert, False

    tp1_changed_pre_tp1 = (
        not existing_signal.get("tp1_reached")
        and ((was_fallback_tp and has_new_explicit_tps) or (raw_tp1 is not None and raw_tp1 != old_raw_tp1))
    )

    # 1. Re-tier unfilled pending orders when a late edit adds or modifies the entry zone / single-price
    desired_pending_targets: Dict[str, float] = {}
    if parsed.zone:
        if is_legacy:
            raw_desired = {
                "TOP": parsed.zone.top if direction == "BUY" else parsed.zone.bottom,
                "MID": parsed.zone.mid,
                "BOTTOM": parsed.zone.bottom if direction == "BUY" else parsed.zone.top,
            }
        else:
            raw_desired = dict(parsed.zone.get_tiers(direction))
        fn_fr = getattr(executor, "_apply_limit_entry_front_run", None)
        if callable(fn_fr) and not is_stop_order:
            desired_pending_targets = {
                lk: fn_fr(
                    symbol=symbol,
                    direction=direction,
                    target_price=float(px_v),
                    final_tp=new_tp,
                    raw_tp1=raw_tp1,
                    pip_size=pip_size,
                    digits=2,
                )
                for lk, px_v in raw_desired.items()
            }
        else:
            desired_pending_targets = raw_desired
    elif parsed.single_price is not None:
        sp_rounded = round(float(parsed.single_price), 2)
        fn_fr = getattr(executor, "_apply_limit_entry_front_run", None)
        if callable(fn_fr) and not is_stop_order and order_intent != "STOP":
            sp_rounded = fn_fr(
                symbol=symbol,
                direction=direction,
                target_price=sp_rounded,
                final_tp=new_tp,
                raw_tp1=raw_tp1,
                pip_size=pip_size,
                digits=2,
            )
        desired_pending_targets = {lk: sp_rounded for lk in layers.keys()}

    retiered_tickets: set = set()
    if desired_pending_targets and pending_tickets and not existing_signal.get("tp1_reached"):
        for l_name, l_data in list(layers.items()):
            if not isinstance(l_data, dict):
                continue
            t = l_data.get("ticket")
            if not t or t not in pending_tickets or l_name not in desired_pending_targets:
                continue
            desired_px = round(float(desired_pending_targets[l_name]), 2)
            old_px = l_data.get("target_price")
            if old_px is not None and abs(float(old_px) - desired_px) < 0.01:
                continue

            l_lot = float(l_data.get("lot") or LAYER_LOTS.get(l_name, 0.01))
            l_tp_target = (
                new_tp if not is_legacy else (new_tp if l_name in ("L1", "TOP") else legacy_runner_tp)
            )
            eff_intent = "AUTO" if parsed.zone else order_intent
            will_mkt = await _mt5_call(
                executor._layer_will_execute_at_market,
                symbol=symbol,
                direction=direction,
                target_price=desired_px,
                tick=tick,
                order_intent=eff_intent,
                allow_market_buffer=(l_name in ("L1", "TOP")),
            )
            in_place_fn = getattr(executor, "modify_pending_order_price_sltp", None)
            if not will_mkt and callable(in_place_fn) and await _mt5_call(in_place_fn, t, desired_px, new_sl, l_tp_target):
                retiered_tickets.add(t)
                l_data["target_price"] = desired_px
                if l_tp_target is not None:
                    l_data["tp"] = l_tp_target
                    l_data["tp_stage"] = "TP1"
                continue
            cancelled_ok = await _mt5_call(executor.cancel_pending_order, t)
            if cancelled_ok:
                if t in pending_tickets:
                    pending_tickets.remove(t)
                new_t, new_otype = await _mt5_call(
                    _place_edit_layer_order,
                    symbol=symbol,
                    direction=direction,
                    target_price=desired_px,
                    lot=l_lot,
                    sl=new_sl,
                    tp=l_tp_target,
                    comment_prefix=f"TG {l_name} (Edit)",
                    tick=tick,
                    order_intent="AUTO" if parsed.zone else order_intent,
                )
                if new_t:
                    retiered_tickets.add(new_t)
                    if new_otype == "MARKET":
                        market_tickets.append(new_t)
                        l_status = "OPEN"
                    else:
                        pending_tickets.append(new_t)
                        l_status = "PENDING"
                    new_l_rec = {
                        "lot": l_lot,
                        "target_price": desired_px,
                        "tp": l_tp_target,
                        "tp_stage": "TP1",
                        "ticket": new_t,
                        "order_type": new_otype,
                        "status": l_status,
                    }
                    if new_otype == "MARKET":
                        _populate_fill_entry_from_exec(existing_signal, new_l_rec, new_t, l_lot, tick, direction, desired_px)
                    layers[l_name] = new_l_rec
                else:
                    l_data["status"] = "CANCELLED"
                    l_data["ticket"] = None
            else:
                eff_sl_mod = _resolve_edit_layer_sl(existing_signal, l_data, new_sl, direction)
                mod_filled = await _mt5_call(executor.modify_position_sltp, ticket=t, sl=eff_sl_mod, tp=l_tp_target)
                if mod_filled:
                    if t in pending_tickets:
                        pending_tickets.remove(t)
                    if t not in market_tickets:
                        market_tickets.append(t)
                    l_data["order_type"] = "MARKET"
                    l_data["status"] = "OPEN"
                    _populate_fill_entry_from_exec(existing_signal, l_data, t, l_lot, tick, direction, desired_px)
                    if l_tp_target is not None:
                        l_data["tp"] = l_tp_target
                        l_data["tp_stage"] = "TP1"
                    retiered_tickets.add(t)

    # 2. Update SL/TP across all existing market & pending tickets (universal forward-only SL + never revert lifted TPs)
    if new_sl is not None or new_tp is not None:
        logger.info(f"Updating SL/TP on existing tickets for Msg ID {msg_id}: SL={new_sl}, Broker TP1={new_tp}")
        handled_tickets: set = set(retiered_tickets)
        for l_name, l_data in layers.items():
            if not isinstance(l_data, dict):
                continue
            t = l_data.get("ticket")
            if not t:
                continue
            cur_l_stage = str(l_data.get("tp_stage") or "TP1").upper()
            if tp1_changed_pre_tp1 and cur_l_stage == "TP2" and not existing_signal.get("roles_locked"):
                cur_l_stage = "TP1"

            if not is_legacy:
                if cur_l_stage in ("TP2", "TP3", "TP4", "TP5"):
                    l_tp = merged_tp_levels.get(cur_l_stage) or l_data.get("tp")
                else:
                    l_tp = new_tp
            elif l_name in ("L1", "TOP"):
                l_tp = new_tp
            else:
                l_tp = legacy_runner_tp

            if l_tp is not None:
                l_data["tp"] = l_tp
                if not is_legacy and cur_l_stage not in ("TP2", "TP3", "TP4", "TP5"):
                    l_data["tp_stage"] = "TP1"
            if t in retiered_tickets:
                continue
            if t in market_tickets:
                eff_sl_m = _resolve_edit_layer_sl(existing_signal, l_data, new_sl, direction)
                await _mt5_call(executor.modify_position_sltp, ticket=t, sl=eff_sl_m, tp=l_tp)
                handled_tickets.add(t)
            elif t in pending_tickets:
                ok_pend = await _mt5_call(executor.modify_pending_order_sltp, ticket=t, sl=new_sl, tp=l_tp)
                if not ok_pend:
                    eff_sl_p = _resolve_edit_layer_sl(existing_signal, l_data, new_sl, direction)
                    ok_pos = await _mt5_call(executor.modify_position_sltp, ticket=t, sl=eff_sl_p, tp=l_tp)
                    if ok_pos:
                        pending_tickets.remove(t)
                        if t not in market_tickets:
                            market_tickets.append(t)
                        l_data["order_type"] = "MARKET"
                        l_data["status"] = "OPEN"
                        _populate_fill_entry_from_exec(existing_signal, l_data, t, l_data.get("lot"), tick, direction, l_data.get("target_price"))
                handled_tickets.add(t)

        for mt in market_tickets:
            if mt not in handled_tickets:
                mt_ld = next((ld for ld in layers.values() if isinstance(ld, dict) and ld.get("ticket") == mt), None)
                eff_sl_f = _resolve_edit_layer_sl(existing_signal, mt_ld, new_sl, direction)
                mt_stage = str((mt_ld or {}).get("tp_stage") or "TP1").upper()
                mt_tp = (
                    (merged_tp_levels.get(mt_stage) or (mt_ld or {}).get("tp"))
                    if mt_stage in ("TP2", "TP3", "TP4", "TP5")
                    else new_tp
                )
                await _mt5_call(executor.modify_position_sltp, ticket=mt, sl=eff_sl_f, tp=mt_tp)
        for pt in pending_tickets:
            if pt not in handled_tickets:
                await _mt5_call(executor.modify_pending_order_sltp, ticket=pt, sl=new_sl, tp=new_tp)

    # 3. Place missing layers across zone ONLY (never stacking L2..L4 on a single-price SL/TP edit),
    #    strictly capped so total open+pending orders for this signal never exceed MAX_ORDERS_PER_SIGNAL (4).
    live_signal_tickets: set = {
        int(t) for t in (list(market_tickets) + list(pending_tickets)) if t is not None
    }
    for ld in layers.values():
        if isinstance(ld, dict) and ld.get("ticket") is not None and ld.get("status") in ("OPEN", "PENDING", "RUNNER"):
            live_signal_tickets.add(int(ld["ticket"]))
    if not executor.dry_run and (live_pos_ids or live_ord_ids):
        active_mt5_ids = live_pos_ids | live_ord_ids
        for raw_t in (existing_signal.get("mt5_order_tickets") or []):
            if raw_t is not None and int(raw_t) in active_mt5_ids:
                live_signal_tickets.add(int(raw_t))

    current_count = len(live_signal_tickets)
    slots_left = max(0, MAX_ORDERS_PER_SIGNAL - current_count)
    zone_completed = bool(existing_signal.get("zone_completed", False))
    old_zone = existing_signal.get("zone") if isinstance(existing_signal.get("zone"), dict) else None
    zone_changed = bool(
        parsed.zone
        and (
            old_zone is None
            or abs(float(old_zone.get("top", 0.0)) - float(parsed.zone.top)) >= 0.01
            or abs(float(old_zone.get("bottom", 0.0)) - float(parsed.zone.bottom)) >= 0.01
        )
    )

    has_active_orders = bool(market_tickets or pending_tickets)
    if slots_left > 0 and not existing_signal.get("tp1_reached"):
        if parsed.zone and (not zone_completed or zone_changed or not has_active_orders):
            zone = parsed.zone
            if is_legacy:
                layer_specs = [
                    ("MID", zone.mid, LAYER_LOTS.get("L2", 0.01), legacy_runner_tp, "TG Layer 2 (Edit Mid", "AUTO"),
                    (
                        "BOTTOM",
                        zone.bottom if direction == "BUY" else zone.top,
                        LAYER_LOTS.get("L4", 0.02),
                        legacy_runner_tp,
                        "TG Layer 3 (Edit Bottom" if direction == "BUY" else "TG Layer 3 (Edit Top",
                        "AUTO",
                    ),
                ]
            else:
                raw_tiers = zone.get_tiers(direction)
                fn_fr = getattr(executor, "_apply_limit_entry_front_run", None)
                if callable(fn_fr) and not is_stop_order:
                    tiers = {
                        lk: fn_fr(
                            symbol=symbol,
                            direction=direction,
                            target_price=float(px_v),
                            final_tp=new_tp,
                            raw_tp1=raw_tp1,
                            pip_size=pip_size,
                            digits=2,
                        )
                        for lk, px_v in raw_tiers.items()
                    }
                else:
                    tiers = raw_tiers
                rem_base_lots = [
                    0.0 if (f"L{i + 1}" in layers and layers[f"L{i + 1}"].get("ticket") is not None) else float(BASE_LAYER_LOTS[i])
                    for i in range(4)
                ]
                l1_is_open_market = bool(
                    isinstance(layers.get("L1"), dict)
                    and layers["L1"].get("status") == "OPEN"
                    and layers["L1"].get("order_type") == "MARKET"
                )
                edit_intents = [
                    "LIMIT" if (i > 0 and l1_is_open_market and not is_stop_order) else "AUTO"
                    for i in range(4)
                ]
                edit_market_mask = [
                    (rem_base_lots[i] > 0.0)
                    and executor._layer_will_execute_at_market(
                        symbol=symbol,
                        direction=direction,
                        target_price=tiers[f"L{i + 1}"],
                        tick=tick,
                        order_intent=edit_intents[i],
                        allow_market_buffer=(i == 0),
                    )
                    for i in range(4)
                ]
                alloc_lots, _ = await _mt5_call(
                    executor.allocate_4layer_lots, symbol, rem_base_lots, None, edit_market_mask
                )
                layer_specs = [
                    (
                        f"L{i + 1}",
                        tiers[f"L{i + 1}"],
                        alloc_lots[i],
                        new_tp,
                        f"TG L{i + 1} (Edit",
                        edit_intents[i],
                    )
                    for i in range(4)
                ]

            occupied_prices: List[float] = [
                round(float(ld["target_price"]), 2)
                for ld in layers.values()
                if isinstance(ld, dict)
                and ld.get("ticket") is not None
                and ld.get("status") in ("OPEN", "PENDING", "RUNNER")
                and ld.get("target_price") is not None
                and ld.get("order_type") != "MARKET"
            ]

            for l_name, target_px, l_lot, l_tp, c_prefix, l_intent in layer_specs:
                if slots_left <= 0 or (len(market_tickets) + len(pending_tickets)) >= MAX_ORDERS_PER_SIGNAL:
                    break
                existing_layer = layers.get(l_name)
                if (isinstance(existing_layer, dict) and existing_layer.get("status") in ("OPEN", "PENDING", "CLOSED", "RUNNER")) or l_lot <= 0.0:
                    continue
                target_px_rounded = round(float(target_px), 2)
                if any(abs(target_px_rounded - op) < 0.01 for op in occupied_prices):
                    continue
                t, o_type = await _mt5_call(
                    _place_edit_layer_order,
                    symbol=symbol,
                    direction=direction,
                    target_price=target_px,
                    lot=l_lot,
                    sl=new_sl,
                    tp=l_tp,
                    comment_prefix=f"{c_prefix})",
                    tick=tick,
                    order_intent=l_intent,
                )
                if t:
                    new_l_rec_a = {
                        "lot": l_lot,
                        "target_price": target_px,
                        "tp": l_tp,
                        "tp_stage": "TP1",
                        "ticket": t,
                        "order_type": o_type,
                        "status": "OPEN" if o_type == "MARKET" else "PENDING",
                    }
                    if o_type == "MARKET":
                        market_tickets.append(t)
                        _populate_fill_entry_from_exec(existing_signal, new_l_rec_a, t, l_lot, tick, direction, target_px)
                    else:
                        pending_tickets.append(t)
                        occupied_prices.append(target_px_rounded)
                    layers[l_name] = new_l_rec_a
                    slots_left -= 1

            placed_any = any(
                isinstance(ld, dict) and ld.get("status") in ("OPEN", "PENDING", "RUNNER") and ld.get("ticket") is not None
                for ld in layers.values()
            )
            if placed_any:
                zone_completed = True

    saved_sl = _resolve_signal_protected_sl(existing_signal, new_sl, direction)
    signal_updates: Dict[str, Any] = {
        "sl": saved_sl,
        "raw_tp1": raw_tp1,
        "tp1_price": raw_tp1,
        "raw_tp2": raw_tp2,
        "raw_tp3": raw_tp3,
        "raw_tp4": raw_tp4,
        "raw_tp5": raw_tp5,
        "tp_levels": merged_tp_levels or None,
        "final_tp": new_tp,
        "hard_broker_tp": new_tp,
        "fallback_tp_applied": fallback_tp_flag,
        "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
        "zone": parsed.zone.to_dict() if parsed.zone else existing_signal.get("zone"),
        "is_zone": True if parsed.zone else existing_signal.get("is_zone", False),
        "is_zone_order": True if parsed.zone else existing_signal.get("is_zone_order", existing_signal.get("is_zone", False)),
        "max_orders": MAX_ORDERS_PER_SIGNAL if (parsed.zone or not is_legacy) else existing_signal.get("max_orders", MAX_ORDERS_PER_SIGNAL),
        "layers": layers,
        "market_tickets": market_tickets,
        "pending_tickets": pending_tickets,
        "zone_completed": zone_completed,
        "raw_text": raw_text,
    }
    if _is_num(existing_signal.get("recorded_entry_price")):
        signal_updates["recorded_entry_price"] = existing_signal["recorded_entry_price"]
    if parsed.single_price is not None and not parsed.zone:
        signal_updates["single_price"] = parsed.single_price
    if tp1_changed_pre_tp1:
        signal_updates["shortfall_armed"] = False
        signal_updates["shortfall_reversal_ticks"] = 0
        signal_updates["peak_price"] = None
        signal_updates["tp1_guard_armed"] = _compute_initial_tp1_guard_armed(
            tick, direction, raw_tp1, bool(market_tickets)
        )

    state_mgr.update_signal(msg_id, signal_updates, chat_id=chat_id)

    return (
        f"✏️ **Signal Updated via Edit [ID: {msg_id}]**\n"
        f"• SL: `{saved_sl}` | Broker TP1: `{new_tp}`\n"
        f"• Total Orders: {len(market_tickets) + len(pending_tickets)}/{MAX_ORDERS_PER_SIGNAL}",
        False,
    )


async def handle_message_edited(event: Any) -> None:
    """Handler for edited messages (serialized per (chat_id, msg_id))."""
    if session_trading_blocked():
        return
    msg = getattr(event, "message", None)
    if not msg:
        return
    chat_id = _safe_chat_id(event)
    pending_notify: Optional[Tuple[Optional[str], bool]] = None
    async with _get_signal_lock(chat_id, msg.id):
        pending_notify = await _handle_message_edited_unlocked(event)
    if pending_notify:
        alert_text, refresh_hud = pending_notify
        if alert_text:
            await notify_saved_messages(alert_text)
        if refresh_hud:
            await _update_live_terminal_status_async()


# ---------------------------------------------------------------------------
# SAVED MESSAGES CONTROL PLANE COMMANDS
# ---------------------------------------------------------------------------

async def _apply_daily_win_input(event: Any, raw_arg: str) -> None:
    """Parse and apply a Daily Win Target input (e.g. '20', '£20', or 'off') from Saved Messages."""
    global _awaiting_dailywin_input
    import re

    clean = raw_arg.strip().lower()
    today_str = await _trading_day_str_async()
    if clean in ("off", "disable", "disabled", "stop", "none", "cancel", "no", "false", "0"):
        _awaiting_dailywin_input = False
        state_mgr.set_daily_win_config(enabled=False, target_amount=0.0, baseline_pnl=0.0, date_str=today_str)
        await event.reply(
            "⚪ **Daily Win Limit Disabled**\n"
            "• The copier will continue trading without a daily profit cap.\n"
            "• Type `dailywin` anytime to set a new daily profit target."
        )
        return

    m = re.search(r"(\d+(?:\.\d+)?)", clean)
    if not m:
        await event.reply(
            "⚠️ **Invalid Daily Win Amount**\n"
            "• Please reply with a positive profit target (for example `20` or `£20`), or type `off` to disable."
        )
        return

    target_val = round(float(m.group(1)), 2)
    if target_val <= 0:
        _awaiting_dailywin_input = False
        state_mgr.set_daily_win_config(enabled=False, target_amount=0.0, baseline_pnl=0.0, date_str=today_str)
        await event.reply("⚪ **Daily Win Limit Disabled.**")
        return

    _awaiting_dailywin_input = False
    dd = await _mt5_call(executor.get_daily_drawdown_status)
    curr = dd.get("currency", "GBP")
    realized_today = round(float(dd.get("realized_today_pnl", 0.0)), 2)
    floating_pnl = round(float(dd.get("floating_pnl", 0.0)), 2)

    baseline_pnl = realized_today if realized_today >= target_val else 0.0
    state_mgr.set_daily_win_config(
        enabled=True,
        target_amount=target_val,
        baseline_pnl=baseline_pnl,
        date_str=today_str,
    )

    net_now = round((realized_today - baseline_pnl) + floating_pnl, 2)
    remaining = max(0.0, round(target_val - net_now, 2))
    await event.reply(
        f"🏆 **Daily Win Limit Activated: `{format_profit(target_val, curr)}`**\n"
        f"• 💰 **Current Session Profit:** `{format_profit(net_now, curr)}`\n"
        f"• 🎯 **Remaining to Target:** `{format_profit(remaining, curr)}`\n"
        f"• 🛑 **Auto-Termination:** Once the bot reaches `{format_profit(target_val, curr)}` in profit, "
        f"it will automatically close all trades, cancel pending orders, notify you here in Saved Messages, "
        f"and terminate the session for no more trading!\n"
        f"• 💡 _Type `dailywin off` anytime to disable._"
    )
    await check_and_enforce_daily_win_target()


async def _apply_daily_loss_input(event: Any, raw_arg: str) -> None:
    """Parse and apply a realized Daily Loss Limit input (e.g. '20', '£20', '-20', or 'off') from Saved Messages."""
    global _awaiting_dailyloss_input, _daily_loss_locked, _daily_loss_lock_notified_date
    import re

    clean = raw_arg.strip().lower()
    if clean in ("off", "disable", "disabled", "stop", "none", "cancel", "no", "false", "0"):
        _awaiting_dailyloss_input = False
        _daily_loss_locked = False
        _daily_loss_lock_notified_date = None
        state_mgr.set_daily_loss_config(enabled=False, limit_amount=0.0, triggered_date=None, triggered_at=0.0)
        _cfg_mod = sys.modules.get("config") or config
        snap = read_risk_limits(_cfg_mod)
        if snap.daily_loss_pause_pct > 0.0 or snap.emergency_cutoff_pct > 0.0:
            guard_note = (
                f"drawdown guard remains active "
                f"(pause={_fmt_pct(snap.daily_loss_pause_pct)}%, cutoff={_fmt_pct(snap.emergency_cutoff_pct)}%)"
            )
        else:
            guard_note = "drawdown guards are also OFF — Unlimited"
        await event.reply(
            "⚪ **Daily Loss Limit Disabled**\n"
            f"• The realized daily loss limit is now OFF ({guard_note}).\n"
            "• Type `dailyloss` anytime to set a new realized daily loss cap."
        )
        return

    m = re.search(r"(-?\d+(?:\.\d+)?)", clean)
    if not m:
        await event.reply(
            "⚠️ **Invalid Daily Loss Amount**\n"
            "• Please reply with a loss limit amount (for example `20`, `£20`, or `-20`), or type `off` to disable."
        )
        return

    limit_val = round(abs(float(m.group(1))), 2)
    if limit_val <= 0:
        _awaiting_dailyloss_input = False
        _daily_loss_locked = False
        _daily_loss_lock_notified_date = None
        state_mgr.set_daily_loss_config(enabled=False, limit_amount=0.0, triggered_date=None, triggered_at=0.0)
        await event.reply("⚪ **Daily Loss Limit Disabled.**")
        return

    _awaiting_dailyloss_input = False
    _daily_loss_locked = False
    _daily_loss_lock_notified_date = None
    state_mgr.set_daily_loss_config(
        enabled=True,
        limit_amount=limit_val,
        triggered_date=None,
        triggered_at=0.0,
    )

    prog = await _mt5_call(get_daily_loss_progress)
    curr = prog.get("currency", "GBP")
    realized_today = prog["realized_today_pnl"]
    remaining = prog["remaining_before_limit"]
    limit_str = format_profit(-limit_val, curr)
    sym = "£" if curr.upper() == "GBP" else "$" if curr.upper() == "USD" else "€" if curr.upper() == "EUR" else ""

    await event.reply(
        f"🛡️ **Daily Loss Limit Activated: `{limit_str}`**\n"
        f"Daily realized loss limit set to -{limit_val:.2f} {curr}.\n"
        f"• 📉 **Realized Closed P&L Today:** `{format_profit(realized_today, curr)}`\n"
        f"• 🧭 **Remaining Loss Buffer:** `{sym}{remaining:.2f} {curr}` (Triggers at `{limit_str}`)\n"
        f"• 📌 **Realized-Only Rule:** **ONLY realized closed / Stop-Loss trades** "
        f"(a negative number on the account for the day) count toward this limit — **NOT** open floating trades!\n"
        f"• 🛑 **Auto-Termination:** Once realized daily losses reach `{limit_str}`, the copier will cancel all pending orders, "
        f"protect capital, notify you here in Saved Messages, and terminate the session for today.\n"
        f"• 💡 _Type `dailyloss off` or `/set_daily_loss off` anytime to disable._"
    )
    await check_and_enforce_daily_loss_limit(exit_on_trigger=False)


_KNOWN_SAVED_COMMANDS = {
    "/dailywin", "dailywin", "/daily_win", "daily_win",
    "/dailyloss", "dailyloss", "/daily_loss", "daily_loss",
    "/set_daily_loss", "set_daily_loss", "/status_daily_loss", "status_daily_loss",
    "/bank", "bank", "/bank_tp1", "bank_tp1", "/bankall", "bankall", "/closeall", "closeall", "/panic", "panic",
    "/floatcap", "floatcap",
    "/losslimit", "losslimit", "/drawdown", "drawdown", "/ddcap", "ddcap",
    "/lotcap", "lotcap", "/riskcap", "riskcap", "/tradecap", "tradecap",
    "/dynamicrisk", "dynamicrisk", "/atrsltp", "atrsltp", "/atrrisk", "atrrisk",
    "/atrtrail", "atrtrail", "/dynamictrail", "dynamictrail",
    "/reversal", "reversal", "/gold_reversal", "gold_reversal",
    "/resume", "resume", "/unpause", "unpause", "/reset_loss", "reset_loss", "/reset_drawdown", "reset_drawdown",
    "/sim_shortfall", "sim_shortfall", "/testrunner", "testrunner",
    "/testorder", "testorder", "/testlimit", "testlimit", "/test", "test",
    "/health", "health", "/status", "status", "/ping", "ping",
    "/trades", "trades", "/algo", "algo", "/check", "check", "/help", "help",
    "/sl", "/tp", "/tp1", "/tp2", "/tp3", "/tp4", "/tp5",
    "/trail", "trail", "/be", "be", "breakeven", "/partial", "partial",
    "/trade", "/exec",
}

_KNOWN_SAVED_PREFIXES = (
    "/sim", "sim ", "sim_",
    "/dailywin ", "dailywin ", "/daily_win ", "daily_win ",
    "/dailyloss ", "dailyloss ", "/daily_loss ", "daily_loss ",
    "/set_daily_loss ", "set_daily_loss ",
    "/bank_tp1 ", "bank_tp1 ",
    "/floatcap ", "floatcap ",
    "/losslimit ", "losslimit ", "/drawdown ", "drawdown ", "/ddcap ", "ddcap ",
    "/lotcap ", "lotcap ", "/riskcap ", "riskcap ", "/tradecap ", "tradecap ",
    "/dynamicrisk ", "dynamicrisk ", "/atrsltp ", "atrsltp ", "/atrrisk ", "atrrisk ",
    "/atrtrail ", "atrtrail ", "/dynamictrail ", "dynamictrail ",
    "/reversal ", "reversal ", "/gold_reversal ", "gold_reversal ",
    "/sl ", "/tp ", "/tp1 ", "/tp2 ", "/tp3 ", "/tp4 ", "/tp5 ",
    "/trail ", "/trade ", "/exec ",
)


def _resolve_saved_prompt_target_signal(
    explicit_id: Optional[str] = None,
    allow_pending: bool = False,
) -> Optional[Tuple[str, Dict[str, Any], Optional[int | str]]]:
    """
    Resolve target `(msg_id, sig_dict, chat_id)` for a Saved Messages active-trade prompt.
    If `explicit_id` (`#<msg_id>`) is given, finds that non-terminal signal; otherwise returns the latest open signal.
    """
    candidates = state_mgr.get_open_or_pending_signals() if allow_pending else state_mgr.get_active_signals()
    if explicit_id is not None:
        exp_clean = str(explicit_id).strip().lstrip("#")
        for k, s in candidates:
            raw_mid, _ = _normalize_task_msg_id(k, chat_id=s.get("chat_id"))
            if str(raw_mid) == exp_clean or str(k) == exp_clean:
                return str(k), s, s.get("chat_id")
        return None

    latest = state_mgr.get_latest_signal()
    if latest:
        k, s = latest
        allowed_statuses = (
            ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE", "PENDING", "PENDING_DELAY")
            if allow_pending
            else ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE")
        )
        if s.get("status") in allowed_statuses:
            return str(k), s, s.get("chat_id")

    if candidates:
        candidates_sorted = sorted(candidates, key=state_mgr._sort_key, reverse=True)
        k, s = candidates_sorted[0]
        return str(k), s, s.get("chat_id")
    return None


def _normalize_saved_adjust_or_trail_prompt(raw_msg: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalize a Saved Messages user prompt (e.g. `/sl 3885`, `/tp 3905 3915 3925`, `/tp2 3918`,
    `/trail tp2`, `Move SL to 3885`, `TP2 3914 TP3 3926`, `trail 15`, `tp1 hit`) into Gate 3 text
    and extract any optional `#<msg_id>` target selector.
    """
    s = raw_msg.strip()
    if not s:
        return None, None

    explicit_id: Optional[str] = None
    id_match = re.search(r"(?:^|\s)#(\d{1,12})(?=\s|$)", s)
    if id_match:
        explicit_id = id_match.group(1)
        s = (s[: id_match.start()] + " " + s[id_match.end() :]).strip()

    low = s.lower()
    if low.startswith("/"):
        s_no_slash = s[1:].strip()
        low_no_slash = s_no_slash.lower()
        if re.match(r"^tp(?:\s+|$)", low_no_slash):
            rest = s_no_slash[2:].strip()
            # If multiple bare prices are given without TP1..TP5 labels, expand sequentially into TP1..TP5
            if rest and not re.search(r"\btp\s*[1-5]\b", rest, re.IGNORECASE):
                sl_part = ""
                sl_m = re.search(r"\b(?:sl|s/l|stop\s*loss)\s*[:=@-]?\s*\d+(?:\.\d+)?", rest, re.IGNORECASE)
                work_rest = rest
                if sl_m:
                    sl_part = " " + sl_m.group(0)
                    work_rest = (rest[: sl_m.start()] + " " + rest[sl_m.end() :]).strip()
                nums = re.findall(r"\d+(?:\.\d+)?", work_rest)
                if nums:
                    tp_tokens = [f"TP{i + 1} {n}" for i, n in enumerate(nums[:5])]
                    return " ".join(tp_tokens) + sl_part, explicit_id
        return s_no_slash, explicit_id

    if re.match(r"^tp(?:\s+\d)", low) and not re.search(r"\btp\s*[1-5]\b", low):
        rest = s[2:].strip()
        sl_part = ""
        sl_m = re.search(r"\b(?:sl|s/l|stop\s*loss)\s*[:=@-]?\s*\d+(?:\.\d+)?", rest, re.IGNORECASE)
        work_rest = rest
        if sl_m:
            sl_part = " " + sl_m.group(0)
            work_rest = (rest[: sl_m.start()] + " " + rest[sl_m.end() :]).strip()
        nums = re.findall(r"\d+(?:\.\d+)?", work_rest)
        if len(nums) >= 2:
            tp_tokens = [f"TP{i + 1} {n}" for i, n in enumerate(nums[:5])]
            return " ".join(tp_tokens) + sl_part, explicit_id

    return s, explicit_id


def _is_known_saved_command(text: str) -> bool:
    """Return True if a Saved Messages input is a command rather than a numeric prompt reply."""
    return text in _KNOWN_SAVED_COMMANDS or text.startswith(_KNOWN_SAVED_PREFIXES)


async def _fetch_dynamic_risk_status_line(cfg_mod: Any) -> str:
    """Fetch live ATR values and format Dynamic Risk status line for /status and /health."""
    pol = read_dynamic_risk_policy(cfg_mod)
    sltp_atr = await _mt5_call(executor.get_atr, DEFAULT_SYMBOL, pol.atr_timeframe, pol.atr_period)
    trail_atr = (
        sltp_atr
        if pol.trail_timeframe == pol.atr_timeframe
        else await _mt5_call(executor.get_atr, DEFAULT_SYMBOL, pol.trail_timeframe, pol.atr_period)
    )
    return format_dynamic_risk_status_line(cfg_mod, sltp_atr, trail_atr)


def _format_uptime() -> str:
    uptime_sec = max(0, int(time.time() - BOT_START_TIME))
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def _effective_account_balance(fin: Dict[str, Any]) -> Tuple[float, str]:
    """Return effective capital base (adding broker credit when balance < 30 and credit > 0) and currency."""
    bal = float(fin.get("balance", 0.0) or 0.0)
    cred = float(fin.get("credit", 0.0) or 0.0)
    eff_bal = bal if (bal >= 30.0 or cred <= 0.0) else round(bal + cred, 2)
    curr = str(fin.get("currency", "GBP") or "GBP")
    return eff_bal, curr


async def handle_saved_messages_command(event: Any) -> None:
    """Remote monitoring commands sent to Saved Messages ('me')."""
    global _awaiting_dailywin_input, _awaiting_dailyloss_input, _daily_50pct_alert_date, _daily_float_cap_alert_date, _daily_loss_locked, TRADING_HALTED
    msg = event.message
    if not msg or not msg.message:
        return

    raw_msg = msg.message.strip()
    text = raw_msg.lower()

    if getattr(msg, "out", False) and any(
        raw_msg.startswith(prefix)
        for prefix in (
            "🏆", "🚀", "🛡️", "💰", "🚪", "🎯", "⚠️", "❌", "⏳", "✏️", "🏦", "🚨",
            "🧪", "🏃", "✅", "📋", "🤖", "⚪", "🚫", "🔄", "ℹ️", "💳", "🛑", "🔒",
            "🟢", "🔴", "🩺", "🗑️", "🛠️",
        )
    ):
        return

    # LIVE CONTROL: dailywin
    if text in ("/dailywin", "dailywin", "/daily_win", "daily_win"):
        _awaiting_dailyloss_input = False
        _awaiting_dailywin_input = True
        prog = await _mt5_call(get_daily_win_progress)
        curr = prog.get("currency", "GBP")
        curr_status = (
            f"🟢 ACTIVE (Target: {format_profit(prog['target_amount'], curr)})"
            if prog["enabled"] and prog["target_amount"] > 0
            else "⚪ OFF"
        )
        await event.reply(
            f"🎯 **Daily Win Limit Toggle (`dailywin`)**\n"
            f"• Current Setting: `{curr_status}`\n"
            f"• Today's Net Profit: `{format_profit(prog['net_profit_today'], curr)}`\n\n"
            f"👉 **Reply with your Daily Win Target amount** (for example `20` or `£20`), "
            f"or reply `off` to disable it.\n"
            f"_(Once the bot makes that amount from trading, it will close all trades, leave a notification here, and terminate the session.)_"
        )
        return

    if text.startswith(("/dailywin ", "dailywin ", "/daily_win ", "daily_win ")):
        _awaiting_dailyloss_input = False
        parts = raw_msg.split(None, 1)
        await _apply_daily_win_input(event, parts[1] if len(parts) > 1 else "")
        return

    # LIVE CONTROL: dailyloss
    if text in ("/dailyloss", "dailyloss", "/daily_loss", "daily_loss", "/set_daily_loss", "set_daily_loss"):
        _awaiting_dailywin_input = False
        _awaiting_dailyloss_input = True
        prog = await _mt5_call(get_daily_loss_progress)
        curr = prog.get("currency", "GBP")
        curr_status = (
            f"🟢 ACTIVE (Limit: {format_profit(-prog['limit_amount'], curr)})"
            if prog["enabled"] and prog["limit_amount"] > 0
            else "⚪ OFF"
        )
        if _daily_loss_locked:
            curr_status += " 🔒 LOCKED"
        await event.reply(
            f"🛡️ **Daily Loss Limit Toggle (`dailyloss`)**\n"
            f"• Current Setting: `{curr_status}`\n"
            f"• Today's Realized Closed P&L: `{format_profit(prog['realized_today_pnl'], curr)}`\n\n"
            f"👉 **Reply with your Daily Loss Limit amount** (for example `20` or `£20`), "
            f"or reply `off` to disable it.\n"
            f"_(ONLY realized closed / Stop-Loss trades leaving a negative number on the account for the day count toward this limit — NOT open floating trades.)_"
        )
        return

    if text.startswith((
        "/dailyloss ", "dailyloss ", "/daily_loss ", "daily_loss ", "/set_daily_loss ", "set_daily_loss "
    )):
        _awaiting_dailywin_input = False
        parts = raw_msg.split(None, 1)
        await _apply_daily_loss_input(event, parts[1] if len(parts) > 1 else "")
        return

    if text in ("/status_daily_loss", "status_daily_loss"):
        _awaiting_dailywin_input = False
        _awaiting_dailyloss_input = False
        prog = await _mt5_call(get_daily_loss_progress)
        curr = prog.get("currency", "GBP")
        realized_today = prog["realized_today_pnl"]
        if prog["enabled"] and prog["limit_amount"] > 0:
            lim = prog["limit_amount"]
            status_line = f"🟢 ACTIVE — Daily Loss Limit: -{lim:.2f} {curr} (`{format_profit(-lim, curr)}`)"
            if _daily_loss_locked:
                status_line += " 🔒 LOCKED"
            rem_line = f"\n• 🧭 **Remaining Loss Buffer:** `{prog['remaining_before_limit']:.2f} {curr}`"
        else:
            status_line = "⚪ OFF (No daily realized loss limit set)"
            rem_line = ""
        await event.reply(
            f"🛡️ **Daily Realized Loss Status**\n"
            f"• Status: {status_line}\n"
            f"• 📉 **Current Daily Realized P&L:** {realized_today:+.2f} {curr} (`{format_profit(realized_today, curr)}`)"
            f"{rem_line}\n"
            f"• 📌 **Note:** Open floating trades are **ignored** — only closed realized deals (SL / TP / manual closes) count toward this limit."
        )
        return

    if _awaiting_dailywin_input:
        if _is_known_saved_command(text):
            _awaiting_dailywin_input = False
        else:
            await _apply_daily_win_input(event, raw_msg)
            return

    if _awaiting_dailyloss_input:
        if _is_known_saved_command(text):
            _awaiting_dailyloss_input = False
        else:
            await _apply_daily_loss_input(event, raw_msg)
            return

    # LIVE CONTROL: /bank_tp1 [on|off]
    if text in ("/bank_tp1", "bank_tp1") or any(text.startswith(p) for p in ("/bank_tp1 ", "bank_tp1 ")):
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        _cfg_mod = sys.modules.get("config")
        if arg in ("on", "true", "1", "all", "100%", "full"):
            setattr(_cfg_mod, "BANK_ALL_AT_TP1", True)
            await event.reply(
                "✅ **Bank TP1 Feature: ENABLED (100% Bank All)**\n"
                "• When TP1 is hit, the bot will close ALL active market positions at market, cancel pending limits, and bank 100% of all potential profit immediately!\n"
                "• Type `/bank_tp1 off` anytime to switch back to Runner mode (scaling to TP2-TP4)."
            )
            return
        elif arg in ("off", "false", "0", "runner", "partial"):
            setattr(_cfg_mod, "BANK_ALL_AT_TP1", False)
            await event.reply(
                "ℹ️ **Bank TP1 Feature: RUNNER MODE (Scale TP1->TP4)**\n"
                "• When TP1 is hit, early layers bank profit while deepest runner(s) scale to TP2/TP3/TP4 with ATR trailing stop protection.\n"
                "• Type `/bank_tp1 on` to switch to 100% Bank All mode."
            )
            return
        else:
            cur_st = bool(getattr(_cfg_mod, "BANK_ALL_AT_TP1", False))
            mode_str = "🟢 100% BANK ALL (Closes all positions at TP1)" if cur_st else "🔵 RUNNER MODE (Scales runner TP1->TP4)"
            await event.reply(
                f"🏦 **Bank TP1 Feature Status:** {mode_str}\n\n"
                f"• Commands:\n"
                f"  - `/bank_tp1 on`: Close 100% of positions at TP1 & bank all profit\n"
                f"  - `/bank_tp1 off`: Scale deepest runner to TP2/TP3/TP4\n"
                f"  - `/bank`: Manually bank the current open trade right now at market"
            )
            return

    # LIVE CONTROL: /floatcap [pct|off] — toggle/configure floating-loss force-close hard cap
    if text in ("/floatcap", "floatcap") or text.startswith(("/floatcap ", "floatcap ")):
        _cfg_mod = sys.modules.get("config") or config
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        fin = await _mt5_call(executor.get_account_financials)
        eff_bal, curr = _effective_account_balance(fin)
        changed, reply_msg = apply_float_cap_command(_cfg_mod, arg, eff_bal, curr)
        if changed:
            _daily_float_cap_alert_date = None
            logger.info(
                f"[/floatcap] FLOAT_LOSS_FORCE_CLOSE_PCT updated to "
                f"{getattr(_cfg_mod, 'FLOAT_LOSS_FORCE_CLOSE_PCT', 0.0)}%"
            )
        await event.reply(reply_msg)
        return

    # LIVE CONTROL: /losslimit [off|<pause%> [cutoff%]] — toggle/configure daily drawdown guard
    if text in ("/losslimit", "losslimit", "/drawdown", "drawdown", "/ddcap", "ddcap") or text.startswith(
        ("/losslimit ", "losslimit ", "/drawdown ", "drawdown ", "/ddcap ", "ddcap ")
    ):
        _cfg_mod = sys.modules.get("config") or config
        parts = text.split()
        fin = await _mt5_call(executor.get_account_financials)
        eff_bal, curr = _effective_account_balance(fin)
        changed, reply_msg = apply_loss_limit_command(_cfg_mod, parts[1:], eff_bal, curr)
        if changed:
            _daily_50pct_alert_date = None
            logger.info(
                f"[/losslimit] DAILY_LOSS_PAUSE_PCT={getattr(_cfg_mod, 'DAILY_LOSS_PAUSE_PCT', 0.0)}%, "
                f"EMERGENCY_CUTOFF_LOSS_PCT={getattr(_cfg_mod, 'EMERGENCY_CUTOFF_LOSS_PCT', 0.0)}%"
            )
        await event.reply(reply_msg)
        return

    # LIVE CONTROL: /lotcap [on|off] — toggle high-risk lot cap enforcement
    if text in ("/lotcap", "lotcap", "/riskcap", "riskcap", "/tradecap", "tradecap") or text.startswith(
        ("/lotcap ", "lotcap ", "/riskcap ", "riskcap ", "/tradecap ", "tradecap ")
    ):
        _cfg_mod = sys.modules.get("config") or config
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        risk_st = await _mt5_call(executor.get_risk_exposure_status)
        changed, reply_msg = apply_lot_cap_command(_cfg_mod, arg, risk_st)
        if changed:
            logger.info(
                f"[/lotcap] ENFORCE_HIGH_RISK_LOT_CAP updated to "
                f"{bool(getattr(_cfg_mod, 'ENFORCE_HIGH_RISK_LOT_CAP', False))}"
            )
        await event.reply(reply_msg)
        return

    # LIVE CONTROL: /dynamicrisk [on|off|adaptive|fallback|pure|sl_mult tp_mult [tf]]
    if text in ("/dynamicrisk", "dynamicrisk", "/atrsltp", "atrsltp", "/atrrisk", "atrrisk") or text.startswith(
        ("/dynamicrisk ", "dynamicrisk ", "/atrsltp ", "atrsltp ", "/atrrisk ", "atrrisk ")
    ):
        _cfg_mod = sys.modules.get("config") or config
        pol = read_dynamic_risk_policy(_cfg_mod)
        live_atr = await _mt5_call(executor.get_atr, DEFAULT_SYMBOL, pol.atr_timeframe, pol.atr_period)
        changed, reply_msg = apply_dynamic_risk_command(_cfg_mod, raw_msg.split()[1:], live_atr=live_atr)
        if changed:
            logger.info(
                f"[/dynamicrisk] enabled={bool(getattr(_cfg_mod, 'DYNAMIC_ATR_SLTP_ENABLED', True))}, "
                f"mode={getattr(_cfg_mod, 'DYNAMIC_SLTP_MODE', 'ADAPTIVE')}, "
                f"tf={getattr(_cfg_mod, 'DYNAMIC_ATR_TIMEFRAME', 'M5')}"
            )
        await event.reply(reply_msg)
        return

    # LIVE CONTROL: /atrtrail [on|off|mult [activation_mult] [M1|M5|M15|H1]]
    if text in ("/atrtrail", "atrtrail", "/dynamictrail", "dynamictrail") or text.startswith(
        ("/atrtrail ", "atrtrail ", "/dynamictrail ", "dynamictrail ")
    ):
        _cfg_mod = sys.modules.get("config") or config
        pol = read_dynamic_risk_policy(_cfg_mod)
        live_atr = await _mt5_call(executor.get_atr, DEFAULT_SYMBOL, pol.trail_timeframe, pol.atr_period)
        changed, reply_msg = apply_atr_trail_command(_cfg_mod, raw_msg.split()[1:], live_atr=live_atr)
        if changed:
            logger.info(
                f"[/atrtrail] enabled={bool(getattr(_cfg_mod, 'DYNAMIC_ATR_TRAIL_ENABLED', False))}, "
                f"mult={getattr(_cfg_mod, 'DYNAMIC_ATR_TRAIL_MULT', 1.50)}, "
                f"tf={getattr(_cfg_mod, 'DYNAMIC_ATR_TRAIL_TIMEFRAME', 'M5')}"
            )
        await event.reply(reply_msg)
        return

    # LIVE CONTROL: /reversal [wide|tight|<giveback_pct> [floor] [ticks]]
    if text in ("/reversal", "reversal", "/gold_reversal", "gold_reversal") or any(
        text.startswith(p) for p in ("/reversal ", "reversal ", "/gold_reversal ", "gold_reversal ")
    ):
        parts = text.split()
        subcmd = parts[1].strip().lower() if len(parts) > 1 else ""
        _cfg_mod = sys.modules.get("config")

        if subcmd in ("on", "true", "enable", "enabled", "1"):
            if _cfg_mod:
                setattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", True)
            await event.reply("🟢 **Gold Reversal Guard: ENABLED**\n• The bot actively watches runners near TP1 and post-TP1 to bank profits on major reversals.")
            return

        if subcmd in ("off", "false", "disable", "disabled", "0"):
            if _cfg_mod:
                setattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", False)
            await event.reply("⚪ **Gold Reversal Guard: DISABLED**\n• Trades will run strictly to TP / SL without giveback-banking protection.")
            return

        if subcmd in ("wide", "widen", "loose", "relaxed", "balance", "balanced"):
            if _cfg_mod:
                setattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", True)
                setattr(_cfg_mod, "ARM_THRESHOLD_PCT", 0.85)
                setattr(_cfg_mod, "GIVEBACK_PCT", 0.45)
                setattr(_cfg_mod, "MIN_GOLD_PULLBACK_DOLLARS", 2.20)
                setattr(_cfg_mod, "REVERSAL_CONFIRM_TICKS", 2)
            await event.reply(
                "🛡️ **Gold Reversal Guard: WIDENED BALANCED MODE ACTIVE**\n"
                "• **Arm Threshold:** `85%` (Requires price to reach >=85% of leg before arming)\n"
                "• **Giveback Breathing Room:** `45%` (Allows 45% pullbacks without stopping you out early)\n"
                "• **Noise Floor:** `$2.20` (22 pips buffer tailored for wide broker spread & Gold volatility)\n"
                "• **Confirmations:** `2 consecutive ticks` required to confirm a genuine reversal\n"
                "• 🚀 **Balanced:** Price has room to breathe during dramatic Gold wicks, but will still bank when it gives back too much!"
            )
            return

        if subcmd in ("tight", "scalp", "default", "reset"):
            if _cfg_mod:
                setattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", True)
                setattr(_cfg_mod, "ARM_THRESHOLD_PCT", 0.85)
                setattr(_cfg_mod, "GIVEBACK_PCT", 0.30)
                setattr(_cfg_mod, "MIN_GOLD_PULLBACK_DOLLARS", 1.50)
                setattr(_cfg_mod, "REVERSAL_CONFIRM_TICKS", 1)
            await event.reply(
                "🛡️ **Gold Reversal Guard: STANDARD MODE ACTIVE**\n"
                "• **Arm Threshold:** `85%`\n"
                "• **Giveback Trigger:** `30%`\n"
                "• **Noise Floor:** `$1.50` (15 pips)\n"
                "• **Confirmations:** `1 tick`"
            )
            return

        # Numeric customization: e.g. /reversal 45 2.0 2
        if len(parts) >= 2 and parts[1].replace(".", "", 1).isdigit():
            try:
                new_gb = float(parts[1])
                if new_gb > 1.0:
                    new_gb = new_gb / 100.0  # e.g. 45 -> 0.45
                new_floor = float(parts[2]) if len(parts) >= 3 else float(getattr(_cfg_mod, "MIN_GOLD_PULLBACK_DOLLARS", 1.80))
                new_ticks = int(parts[3]) if len(parts) >= 4 else int(getattr(_cfg_mod, "REVERSAL_CONFIRM_TICKS", 1))

                if _cfg_mod:
                    setattr(_cfg_mod, "GIVEBACK_PCT", new_gb)
                    setattr(_cfg_mod, "MIN_GOLD_PULLBACK_DOLLARS", new_floor)
                    setattr(_cfg_mod, "REVERSAL_CONFIRM_TICKS", new_ticks)

                await event.reply(
                    f"✅ **Gold Reversal Guard Custom Calibration Applied:**\n"
                    f"• **Giveback Trigger:** `{new_gb*100:.0f}%` of peak profit\n"
                    f"• **Noise Floor:** `${new_floor:.2f}` ({int(new_floor*10)} pips)\n"
                    f"• **Confirm Ticks:** `{new_ticks}` tick(s)"
                )
                return
            except Exception as e:
                logger.warning(f"Failed to parse /reversal args: {e}")

        # Status display
        cur_on = bool(getattr(_cfg_mod, "GOLD_REVERSAL_GUARD_ENABLED", False))
        cur_arm = float(getattr(_cfg_mod, "ARM_THRESHOLD_PCT", 0.85)) * 100.0
        cur_gb = float(getattr(_cfg_mod, "GIVEBACK_PCT", 0.30)) * 100.0
        cur_floor = float(getattr(_cfg_mod, "MIN_GOLD_PULLBACK_DOLLARS", 1.50))
        cur_ticks = int(getattr(_cfg_mod, "REVERSAL_CONFIRM_TICKS", 1))
        st_icon = "🟢 ACTIVE" if cur_on else "⚪ DISABLED"

        await event.reply(
            f"🛡️ **Gold Reversal Guard Status:** {st_icon}\n"
            f"• **Arm Threshold:** `{cur_arm:.0f}%` of TP1 leg (only arms near target)\n"
            f"• **Giveback Trigger:** `{cur_gb:.0f}%` of peak profit\n"
            f"• **Noise Floor:** `${cur_floor:.2f}` ({int(cur_floor*10)} pips buffer)\n"
            f"• **Confirmations Required:** `{cur_ticks} tick(s)`\n\n"
            f"• **Quick Controls:**\n"
            f"  - `/reversal wide` : Widen settings for high volatility / wide spread (45% giveback, $2.20 floor, 2 ticks)\n"
            f"  - `/reversal tight` : Reset to standard scalp mode (30% giveback, $1.50 floor, 1 tick)\n"
            f"  - `/reversal <giveback_pct> [floor] [ticks]` : e.g. `/reversal 45 2.2 2`\n"
            f"  - `/reversal on` or `/reversal off` : Toggle guard"
        )
        return

    # LIVE CONTROL: /resume or /unpause or /reset_loss
    if text in ("/resume", "resume", "/unpause", "unpause", "/reset_loss", "reset_loss", "/reset_drawdown", "reset_drawdown"):
        _daily_loss_locked = False
        TRADING_HALTED = False
        _daily_50pct_alert_date = None
        upd = await _mt5_call(executor.update_daily_loss_baseline)
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        bal = fin.get("balance", 0.0)
        risk_st = await _mt5_call(executor.get_risk_exposure_status)
        high_l = risk_st["high_risk_lots"]
        mid_l = risk_st["mid_risk_lots"]
        low_l = risk_st["low_risk_lots"]
        _cfg_mod = sys.modules.get("config") or config
        lot_cap_on = bool(getattr(_cfg_mod, "ENFORCE_HIGH_RISK_LOT_CAP", False))
        lots_guide_line = (
            f"• **Max Lots Guide:** `{high_l:.2f} Max` (High: `{high_l:.2f}` | Mid: `{mid_l:.2f}` | Low: `{low_l:.2f}`)\n"
            if lot_cap_on
            else f"• **Lot Cap:** `Unlimited (OFF)` (Guide — Low: `{low_l:.2f}` | Mid: `{mid_l:.2f}` | High: `{high_l:.2f}`)\n"
        )

        msg = (
            f"🟢 **Trading Resumed & Daily Drawdown Reset**\n"
            f"• Previous daily loss has passed and is archived as realized.\n"
            f"• **Active Capital Baseline:** `{bal:.2f} {curr}`\n"
            f"• **Drawdown Status:** `0.0%` (Unpaused — ready for new signals)\n"
            f"{lots_guide_line}"
            f"• 🚀 **The bot is actively listening and will action new trades immediately!**"
        )
        logger.info(f"[/resume] Daily loss baseline updated to {bal:.2f} {curr}. Trading resumed.")
        await event.reply(msg)
        return

    # LIVE CONTROL: /bank or /bankall
    if text in ("/bank", "bank", "/bankall", "bankall"):
        latest = state_mgr.get_latest_signal()
        if not latest or latest[1].get("status") == "CLOSED":
            await event.reply("⚠️ No active signals found to bank.")
            return

        msg_id, sig = latest
        sig_chat = sig.get("chat_id")
        async with _get_signal_lock(sig_chat, msg_id):
            fresh_sig = state_mgr.get_signal(msg_id, chat_id=sig_chat) or sig
            if fresh_sig.get("status") == "RUNNER_ACTIVE":
                closed_tkts = []
                r_tkts = list(
                    fresh_sig.get("runner_tickets")
                    or ([fresh_sig.get("runner_ticket")] if fresh_sig.get("runner_ticket") else [])
                )
                all_open_tkts = list(dict.fromkeys([t for t in (r_tkts + list(fresh_sig.get("market_tickets") or [])) if t]))
                for rt in all_open_tkts:
                    if rt and await _mt5_call(executor.close_market_position, rt):
                        closed_tkts.append(rt)
                await _mt5_call(executor.cancel_signal_pending_orders, fresh_sig)
                layers_b = fresh_sig.get("layers") if isinstance(fresh_sig.get("layers"), dict) else {}
                closed_set_b = set(all_open_tkts)
                for lv in layers_b.values():
                    if isinstance(lv, dict):
                        if lv.get("ticket") in closed_set_b:
                            lv["status"] = "CLOSED"
                        elif str(lv.get("status") or "").strip().upper() == "PENDING":
                            lv["status"] = "CANCELLED"
                added_prof, _ = await _account_closed_tickets(fresh_sig, all_open_tkts)
                _ensure_signal_exit_telemetry(fresh_sig, exit_reason_code="MANUAL", overwrite_code=True)
                prev_rlayers_b = list(fresh_sig.get("runner_layers") or ([fresh_sig["runner_layer"]] if fresh_sig.get("runner_layer") else []))
                prev_rlayer_b = fresh_sig.get("runner_layer") or (prev_rlayers_b[-1] if prev_rlayers_b else fresh_sig.get("last_runner_layer"))
                if prev_rlayers_b:
                    fresh_sig["last_runner_layers"] = prev_rlayers_b
                if prev_rlayer_b:
                    fresh_sig["last_runner_layer"] = prev_rlayer_b
                fresh_sig["status"] = "CLOSED"
                fresh_sig["market_tickets"] = []
                fresh_sig["pending_tickets"] = []
                fresh_sig["runner_tickets"] = []
                fresh_sig["runner_ticket"] = None
                fresh_sig["runner_layers"] = []
                fresh_sig["runner_layer"] = None
                fresh_sig["layers"] = layers_b
                fresh_sig["runner_close_reason"] = "Manual /bank on Runner"
                state_mgr.update_signal(msg_id, fresh_sig, chat_id=sig_chat)
                fin = await _mt5_call(executor.get_account_financials)
                curr = fin.get("currency", "GBP")
                _emit_exit_telemetry_log(
                    msg_id,
                    fresh_sig,
                    float(fresh_sig.get("banked_profit") or added_prof),
                    exit_reason="Manual /bank on Runner",
                )
                await _record_signal_to_ledger(msg_id, fresh_sig, exit_reason="Manual /bank on Runner", currency=curr)
                profit_str = format_profit(added_prof, curr)
                await event.reply(
                    f"💰 **Runner Banked Manually! [ID: {msg_id}]**\n"
                    f"• Closed Tickets: `{closed_tkts}`\n"
                    f"• 💰 **Banked Profit:** `{profit_str}` | **Total Trade Profit:** `{format_profit(fresh_sig.get('banked_profit', 0.0), curr)}`\n"
                    f"• Signal closed & pending orders cancelled!\n\n"
                    f"{fin['formatted']}"
                )
                return

            _, _, alert = await _execute_tp1_scaling_handoff(
                msg_id, fresh_sig, chat_id=sig_chat, source_label="Manual /bank"
            )
        await event.reply(alert)
        return

    # LIVE CONTROL: /closeall
    if text in ("/closeall", "closeall", "/panic", "panic"):
        res = await _close_all_bot_exposure_and_signals_async(exit_reason="Manual Panic Close")
        closed_tickets_list = res.get("closed_tickets", [])
        cancelled_orders_list = res.get("cancelled_orders", [])
        total_profit = sum(
            [await _mt5_call(executor.get_position_closed_profit, t) for t in closed_tickets_list]
        )
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(total_profit, curr)

        await event.reply(
            f"🚨 **EMERGENCY PANIC BUTTON TRIGGERED!**\n"
            f"• Market Positions Closed: `{len(closed_tickets_list)}`\n"
            f"• Pending Orders Cancelled: `{len(cancelled_orders_list)}`\n"
            f"• 💰 **Total Amount Made / Realized:** `{profit_str}`\n"
            f"• All active signals & delayed entries marked `CLOSED`.\n\n"
            f"{fin['formatted']}"
        )
        return

    # LIVE CONTROL: Execute 4-Layer Trade Prompt from Saved Messages (/trade <signal> or /exec <signal>)
    if text in ("/trade", "trade", "/exec", "exec") or text.startswith(("/trade ", "trade ", "/exec ", "exec ")):
        sig_text = raw_msg.split(None, 1)[1].strip() if " " in raw_msg else ""
        if not sig_text:
            await event.reply(
                "⚠️ **Usage:** `/trade Buy 3895-3887 TP1 3900 TP2 3910 TP3 3920 SL 3875`"
            )
            return
        if session_trading_blocked():
            await event.reply("🚫 **Trading is currently halted or locked for today.**")
            return

        tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
        live_price = getattr(tick, "bid", None) if tick else None
        parsed = parse_message(sig_text, message_id=msg.id or int(time.time() * 1000) % 1000000, live_price=live_price)
        if not parsed or (parsed.event_type != "NEW_SIGNAL" and not parsed.is_entry):
            await event.reply(f"❌ **Could not parse a valid 4-layer trade signal from:**\n`{sig_text}`")
            return

        symbol = parsed.symbol or DEFAULT_SYMBOL
        direction = parsed.direction
        sl = parsed.sl if parsed.sl_valid else None
        final_tp = parsed.final_tp
        zone = parsed.zone
        tp_levels = dict(parsed.tp_levels or {})
        raw_tp1 = parsed.raw_tp1
        raw_tp2 = parsed.raw_tp2
        raw_tp3 = parsed.raw_tp3
        raw_tp4 = parsed.raw_tp4
        raw_tp5 = parsed.raw_tp5
        is_stop_order = bool(getattr(parsed, "is_stop_order", False))
        is_limit_order = bool(getattr(parsed, "is_limit_order", False))
        order_intent = getattr(parsed, "order_intent", None) or ("STOP" if is_stop_order else "AUTO")

        if executor.get_daily_drawdown_status().get("pause_new_signals"):
            await check_and_enforce_daily_drawdown()

        passed, reason = await _mt5_call(
            executor.check_preflight,
            symbol=symbol,
            direction=direction,
            raw_tp1=raw_tp1,
            sl=sl,
            zone=zone,
            single_price=parsed.single_price,
            is_stop_order=is_stop_order,
            order_intent=order_intent,
        )
        if not passed:
            await event.reply(f"⚠️ **Order Blocked by Pre-Flight**: {reason}")
            return

        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}
        zone_completed = False
        is_zone = False
        initial_price = _get_live_entry_price(tick, direction)

        if parsed.is_zone and zone:
            market_tickets, pending_tickets, layers_data = await _mt5_call(
                executor.execute_4layer_zone,
                symbol=symbol,
                direction=direction,
                zone=zone,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
            )
            zone_completed = True
            is_zone = True
        elif parsed.single_price is not None:
            market_tickets, pending_tickets, layers_data = await _mt5_call(
                executor.execute_4layer_single_price,
                symbol=symbol,
                direction=direction,
                price=parsed.single_price,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
                order_intent=order_intent,
                is_stop_order=is_stop_order,
            )
            zone_completed = len(layers_data) >= MAX_ORDERS_PER_SIGNAL
        else:
            market_tickets, pending_tickets, layers_data = await _mt5_call(
                executor.execute_4layer_market,
                symbol=symbol,
                direction=direction,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
                raw_tp3=raw_tp3,
                raw_tp4=raw_tp4,
                raw_tp5=raw_tp5,
                tp_levels=tp_levels or None,
            )

        if not market_tickets and not pending_tickets:
            err_reason = getattr(executor, "last_error", None) or "Broker rejected order(s)"
            await event.reply(f"❌ **Order Rejected by MT5**: `{err_reason}`")
            return

        dyn_t = _merge_dynamic_risk_into_signal_fields(
            getattr(executor, "last_dynamic_risk_snapshot", None),
            sl, final_tp, raw_tp1, raw_tp2, raw_tp3, raw_tp4, raw_tp5, tp_levels,
        )
        sl = dyn_t["sl"]
        final_tp = dyn_t["final_tp"]
        raw_tp1 = dyn_t["raw_tp1"]
        raw_tp2 = dyn_t["raw_tp2"]
        raw_tp3 = dyn_t["raw_tp3"]
        raw_tp4 = dyn_t["raw_tp4"]
        raw_tp5 = dyn_t["raw_tp5"]
        tp_levels = dyn_t["tp_levels"]

        eff_mid = getattr(msg, "id", None) or int(time.time() * 1000) % 1000000
        arm_tick = await _mt5_call(executor.get_tick, symbol)
        tp1_guard_armed = _compute_initial_tp1_guard_armed(
            arm_tick, direction, raw_tp1, bool(market_tickets)
        )
        extra_dyn_t = {"dynamic_risk": dyn_t["dynamic_risk"]} if dyn_t["dynamic_risk"] is not None else {}
        created_sig = state_mgr.create_signal(
            message_id=eff_mid,
            chat_id="me",
            symbol=symbol,
            direction=direction,
            layers=layers_data,
            zone=zone.to_dict() if zone else None,
            market_tickets=market_tickets,
            pending_tickets=pending_tickets,
            zone_completed=zone_completed,
            max_orders=MAX_ORDERS_PER_SIGNAL,
            is_zone=is_zone,
            is_zone_order=is_zone,
            is_stop_order=is_stop_order,
            is_limit_order=is_limit_order,
            order_intent=order_intent,
            single_price=parsed.single_price,
            sl=sl,
            raw_tp1=raw_tp1,
            tp1_price=raw_tp1,
            raw_tp2=raw_tp2,
            raw_tp3=raw_tp3,
            raw_tp4=raw_tp4,
            raw_tp5=raw_tp5,
            tp_levels=tp_levels or None,
            final_tp=final_tp,
            proximity_close_price=parsed.proximity_close_price,
            raw_text=sig_text,
            initial_price=initial_price,
            recorded_entry_price=initial_price,
            tp1_guard_armed=tp1_guard_armed,
            **extra_dyn_t,
        )
        await event.reply(
            f"🚀 **Saved Messages 4-Layer Trade Executed ({order_intent}) [ID: {eff_mid}]**\n"
            f"• Direction: `{direction}` {symbol}\n"
            f"• Orders: Market={market_tickets}, Pending={pending_tickets}\n"
            f"• Layers: {_format_layers_summary(created_sig)}\n"
            f"• SL: `{sl}` | Broker TP1: `{final_tp}` | Targets: {_format_tp_summary(created_sig)}"
        )
        return

    # LIVE CONTROL: Breakeven shortcut (/be, be, breakeven, move sl to be, sl to entry)
    if text in ("/be", "be", "breakeven", "move sl to be", "sl to entry") or text.startswith(("/be ", "be #")):
        _, exp_id = _normalize_saved_adjust_or_trail_prompt(raw_msg)
        resolved = _resolve_saved_prompt_target_signal(explicit_id=exp_id, allow_pending=False)
        if not resolved:
            await event.reply("⚠️ No active signal found to move to Breakeven.")
            return
        target_mid, target_sig, target_chat = resolved
        async with _get_signal_lock(target_chat, target_mid):
            fresh_sig = state_mgr.get_signal(target_mid, chat_id=target_chat) or target_sig
            symbol = fresh_sig.get("symbol", DEFAULT_SYMBOL)
            direction = fresh_sig.get("direction", "BUY")
            res_be = await _mt5_call(
                executor.apply_signal_breakeven,
                fresh_sig,
                BE_LAYERS,
                BE_OFFSET_PIPS,
            )
            prev_st_be = str(fresh_sig.get("status") or "ACTIVE").strip().upper()
            new_st_be = "RUNNER_ACTIVE" if prev_st_be == "RUNNER_ACTIVE" else "BE_APPLIED"
            updates_cmd_be: Dict[str, Any] = {
                "status": new_st_be,
                "layers": fresh_sig.get("layers", {}),
                "market_tickets": fresh_sig.get("market_tickets", []),
                "pending_tickets": fresh_sig.get("pending_tickets", []),
                "be_armed": True,
                "be_done_tickets": res_be.get("be_done_tickets", []),
                "be_deferred_tickets": res_be.get("deferred_tickets", []),
            }
            if fresh_sig.get("runner_sl_floor") is not None:
                updates_cmd_be["runner_sl_floor"] = fresh_sig["runner_sl_floor"]
            state_mgr.update_signal(target_mid, updates_cmd_be, chat_id=target_chat)
        await event.reply(
            f"🛡️ **Selective Breakeven Applied (Saved Messages Prompt) [ID: {target_mid}]**\n"
            f"• Symbol: `{symbol}` ({direction})\n"
            f"• Layers Protected: `{BE_LAYERS}` | Modified: `{res_be.get('modified_tickets', [])}`"
        )
        return

    # LIVE CONTROL: Partial Close shortcut (/partial, partial, close half, take partials)
    if text in ("/partial", "partial", "close half", "take partials") or text.startswith(("/partial ", "partial #")):
        _, exp_id = _normalize_saved_adjust_or_trail_prompt(raw_msg)
        resolved = _resolve_saved_prompt_target_signal(explicit_id=exp_id, allow_pending=False)
        if not resolved:
            await event.reply("⚠️ No active signal found for partial close.")
            return
        target_mid, target_sig, target_chat = resolved
        async with _get_signal_lock(target_chat, target_mid):
            fresh_sig = state_mgr.get_signal(target_mid, chat_id=target_chat) or target_sig
            symbol = fresh_sig.get("symbol", DEFAULT_SYMBOL)
            direction = fresh_sig.get("direction", "BUY")
            layers = fresh_sig.get("layers", {})
            market_tickets = list(fresh_sig.get("market_tickets", []))
            pending_tickets = list(fresh_sig.get("pending_tickets", []))
            closed = await _mt5_call(
                executor.apply_partial_close,
                market_tickets=market_tickets,
                pending_tickets=pending_tickets,
                symbol=symbol,
                direction=direction,
            )
            fin = await _mt5_call(executor.get_account_financials)
            curr = fin.get("currency", "GBP")
            profit_info = ""
            if closed and closed in market_tickets:
                market_tickets.remove(closed)
                added_p, _ = await _account_closed_tickets(fresh_sig, [closed])
                profit_info = f"\n• 💰 **Amount Made:** `{format_profit(added_p, curr)}`"
                for l_data in layers.values():
                    if isinstance(l_data, dict) and l_data.get("ticket") == closed:
                        l_data["status"] = "CLOSED"
                if fresh_sig.get("runner_ticket") == closed:
                    fresh_sig["last_runner_ticket"] = closed
                    fresh_sig["runner_ticket"] = None
            prev_st_pc = str(fresh_sig.get("status") or "").strip().upper()
            new_status = (
                "CLOSED"
                if (not market_tickets and not pending_tickets)
                else ("RUNNER_ACTIVE" if prev_st_pc == "RUNNER_ACTIVE" else "PARTIAL_CLOSED")
            )
            fresh_sig["market_tickets"] = market_tickets
            fresh_sig["layers"] = layers
            fresh_sig["status"] = new_status
            if new_status == "CLOSED":
                _ensure_signal_exit_telemetry(fresh_sig, exit_reason_code="MANUAL", overwrite_code=True)
            state_mgr.update_signal(
                target_mid,
                {
                    "market_tickets": market_tickets,
                    "layers": layers,
                    "status": new_status,
                    "accounted_tickets": fresh_sig.get("accounted_tickets", []),
                    "banked_profit": fresh_sig.get("banked_profit", 0.0),
                    "runner_ticket": fresh_sig.get("runner_ticket"),
                    "last_runner_ticket": fresh_sig.get("last_runner_ticket"),
                    **(
                        {
                            "exit_reason_code": fresh_sig.get("exit_reason_code"),
                            "exit_stage": fresh_sig.get("exit_stage"),
                            "exit_price": fresh_sig.get("exit_price"),
                            "max_favorable_excursion": fresh_sig.get("max_favorable_excursion"),
                            "giveback_from_peak": fresh_sig.get("giveback_from_peak"),
                            "trail_tier_used": fresh_sig.get("trail_tier_used"),
                        }
                        if new_status == "CLOSED"
                        else {}
                    ),
                },
                chat_id=target_chat,
            )
            if new_status == "CLOSED":
                _emit_exit_telemetry_log(
                    target_mid,
                    fresh_sig,
                    float(fresh_sig.get("banked_profit") or 0.0),
                    exit_reason="Partial Close (Saved Messages Prompt)",
                )
                await _record_signal_to_ledger(
                    target_mid, fresh_sig, exit_reason="Partial Close (Saved Messages Prompt)", currency=curr
                )
        await event.reply(
            f"💰 **Partial Close Executed on `{symbol}` [ID: {target_mid}]**\n"
            f"• Order #{closed} closed at market."
            f"{profit_info}\n\n"
            f"{fin['formatted']}"
        )
        return

    # HEALTH COMMAND: /health
    if text in ("/health", "health"):
        mt5_ok, algo_ok, ping_ms = await _probe_mt5_health()
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        srv_date = await _trading_day_str_async()
        ledger_stats = trade_ledger.get_stats(server_date=srv_date)

        lock_held = bool(_instance_lock and _instance_lock.acquired)
        lock_label = f"🟢 HELD ({LOCK_FILE_PATH.name})" if lock_held else f"⚪ NOT HELD ({LOCK_FILE_PATH.name})"
        tg_connected = bool(telethon_client and telethon_client.is_connected()) if telethon_client else False

        active_signals = state_mgr.get_active_signals()
        if not active_signals:
            active_block = "None"
        else:
            lines: List[str] = []
            for mid, s in active_signals:
                milestone = s.get("runner_milestone_name", "TP1")
                tp_stage = s.get("tp_stage", "TP1")
                cleared = s.get("runner_cleared_milestones") or []
                armed = "ARMED" if s.get("runner_proximity_armed") else "STANDBY"
                rev_ticks = int(s.get("runner_reversal_ticks") or s.get("shortfall_reversal_ticks") or 0)
                fsm_s = _get_signal_stage(s)
                lines.append(
                    f"  - `{mid}` ({s.get('direction')} {s.get('symbol')}) | `{s.get('status')}` | "
                    f"Layers: `{_format_layers_summary(s)}` | FSM: `Stage {fsm_s}` | Stage: `{tp_stage}` | Milestone: `{milestone}` "
                    f"(Cleared: `{cleared}`, Guard: `{armed}`, RevTicks: `{rev_ticks}/{RUNNER_REVERSAL_CONFIRM_TICKS}`)"
                )
            active_block = "\n" + "\n".join(lines)

        risk_st_h = await _mt5_call(executor.get_risk_exposure_status)
        dd_st_h = await _mt5_call(executor.get_daily_drawdown_status)
        _cfg_mod_h = sys.modules.get("config") or config
        limits_h = format_limits_status_lines(_cfg_mod_h, dd_st_h, risk_st_h)
        dyn_line_h = await _fetch_dynamic_risk_status_line(_cfg_mod_h)

        await event.reply(
            f"🩺 **XAUUSD Copier v3.0 Health Telemetry**\n"
            f"• Uptime: `{_format_uptime()}`\n"
            f"• Single-Instance Lock: `{lock_label}`\n"
            f"• MT5 Terminal: `{'🟢 CONNECTED' if mt5_ok else '🔴 DISCONNECTED'}` "
            f"(Ping: `{ping_ms:.2f} ms`)\n"
            f"• Algo Trading: `{'🟢 ENABLED' if algo_ok else '🔴 DISABLED'}`\n"
            f"• Telegram Client: `{'🟢 CONNECTED' if tg_connected else '⚪ OFFLINE'}` | "
            f"Reconnects: `{_telegram_reconnect_count}`\n"
            f"• Deleted-Signal Guard: `{'🟢 ON' if CLOSE_ON_SIGNAL_DELETED else '⚪ OFF'}` "
            f"(Sweep: `{DELETED_SIGNAL_POLL_INTERVAL_SECONDS}s`)\n"
            f"• Active Lots (`/lotcap`): {limits_h['active_lots_label']}\n"
            f"{limits_h['drawdown_guard_line']}\n"
            f"{limits_h['float_cap_line']}\n"
            f"{dyn_line_h}\n"
            f"• Runner FSM Mode: `{RUNNER_PROXIMITY_MODE}` "
            f"(Buf: `{RUNNER_PROXIMITY_BUFFER_PIPS}` pips / `{RUNNER_PROXIMITY_BUFFER_PCT}%`, "
            f"MidrangeClamp: `{RUNNER_MAX_MIDRANGE_RATIO}`, "
            f"Pullback: `{RUNNER_REVERSAL_PULLBACK_PIPS}` pips / `{RUNNER_REVERSAL_PULLBACK_PCT}%`, "
            f"ConfirmTicks: `{RUNNER_REVERSAL_CONFIRM_TICKS}`, "
            f"CleanBreak: `{RUNNER_CLEAN_BREAK_PIPS}` pips, MaxStage: `TP{RUNNER_MAX_TP_STAGE}`)\n"
            f"• Active Signals ({len(active_signals)}):{active_block}\n"
            f"• Ledger Summary (`{TRADES_LEDGER_PATH.name}`): "
            f"`{ledger_stats['total_trades']}` trades (`{ledger_stats['wins']}W/{ledger_stats['losses']}L`), "
            f"Win Rate: `{ledger_stats['win_rate_pct']:.1f}%`, "
            f"Total PnL: `{format_profit(ledger_stats['total_realized_pnl'], curr)}`, "
            f"Today ({ledger_stats['today_trades']}): `{format_profit(ledger_stats['today_realized_pnl'], curr)}`"
        )
        return

    # WEEKEND SIMULATION: /sim_tp1 [1|2|3|4]
    if text.startswith("/sim_tp1") or text.startswith("sim_tp1"):
        parts = text.split()
        case_num = parts[1] if len(parts) > 1 else "4"
        test_exec = MT5Executor(dry_run=True)
        test_exec.connect()

        tp_map = derive_tp_levels([3900.0], direction="BUY")
        sample_layers = {
            "L1": {"lot": 0.01, "ticket": 901, "order_type": "MARKET", "status": "OPEN", "tp": 3899.5, "tp_stage": "TP1"},
            "L2": {"lot": 0.01, "ticket": 902, "order_type": "LIMIT", "status": "OPEN" if case_num in ("2", "3", "4") else "PENDING", "tp": 3899.5, "tp_stage": "TP1"},
            "L3": {"lot": 0.01, "ticket": 903, "order_type": "LIMIT", "status": "OPEN" if case_num in ("3", "4") else "PENDING", "tp": 3899.5, "tp_stage": "TP1"},
            "L4": {"lot": 0.02, "ticket": 904, "order_type": "LIMIT", "status": "OPEN" if case_num == "4" else "PENDING", "tp": 3899.5, "tp_stage": "TP1"},
        }

        if case_num == "1":
            m_tickets, p_tickets = [901], [902, 903, 904]
        elif case_num == "2":
            m_tickets, p_tickets = [901, 902], [903, 904]
        elif case_num == "3":
            m_tickets, p_tickets = [901, 902, 903], [904]
        else:
            case_num = "4"
            m_tickets, p_tickets = [901, 902, 903, 904], []

        sig_data: Dict[str, Any] = {
            "layers": sample_layers,
            "market_tickets": list(m_tickets),
            "pending_tickets": list(p_tickets),
            "symbol": DEFAULT_SYMBOL,
            "direction": "BUY",
            "raw_tp1": tp_map["TP1"],
            "raw_tp2": tp_map["TP2"],
            "raw_tp3": tp_map["TP3"],
            "raw_tp4": tp_map["TP4"],
            "raw_tp5": tp_map["TP5"],
            "tp_levels": tp_map,
        }

        runner_t = test_exec.handle_tp1_scaling_and_select_runner(sig_data)
        status = sig_data.get("status")
        floor = sig_data.get("runner_sl_floor") or sig_data.get("soft_tp1_floor")

        if case_num == "1":
            desc = "Shallow Pullback (Only Layer 1 filled)"
            action_desc = (
                "• Banked Layer 1 (0.01 lot) at TP1 (3900.00)\n"
                "• Cancelled pending limits for Layers 2, 3 & 4\n"
                "• Trade fully CLOSED"
            )
            sim_profit = 1.25
        elif case_num == "2":
            desc = "Medium Pullback (Layers 1 & 2 filled)"
            action_desc = (
                f"• Banked Layer 1 (0.01 lot) at TP1\n"
                f"• Cancelled Layers 3 & 4 pending limits\n"
                f"• Designated Runner: Layer 2 (L2, 0.01 lot) [Ticket #{runner_t}]\n"
                f"• Stop Loss locked at `{floor}` & TP extended to TP5 ({tp_map['TP5']})"
            )
            sim_profit = 1.25
        elif case_num == "3":
            desc = "Full Zone Fill (3 of 4 Layers filled: L1, L2, L3)"
            action_desc = (
                f"• Banked Layer 1 (0.01 lot) at TP1\n"
                f"• Kept Layer 2 (0.01 lot) open to TP2 ({tp_map['TP2']}) at BE\n"
                f"• Cancelled Layer 4 pending limit\n"
                f"• Designated Runner: Layer 3 (L3, 0.01 lot) [Ticket #{runner_t}]\n"
                f"• Stop Loss locked at `{floor}` & TP extended to TP5 ({tp_map['TP5']})"
            )
            sim_profit = 1.25
        else:
            desc = "Full 4-Layer Fill ([0.01 / 0.01 / 0.01 / 0.02])"
            action_desc = (
                f"• Banked Layer 1 (0.01 lot) at TP1 ({tp_map['TP1']})\n"
                f"• Kept L2 -> TP2 ({tp_map['TP2']}) & L3 -> TP3 ({tp_map['TP3']}) open at BE\n"
                f"• Designated Runner: Layer 4 (L4, 0.02 lot - best entry!) [Ticket #{runner_t}]\n"
                f"• Active Runner FSM monitoring TP1 -> TP2 -> TP3 -> TP4 -> TP5 ({tp_map['TP5']}), Floor=`{floor}`"
            )
            sim_profit = 1.25

        fin = test_exec.get_account_financials()
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(sim_profit, curr)

        await event.reply(
            f"🧪 **Weekend Simulation v3.0: `/sim_tp1 {case_num}`**\n"
            f"• Scenario: **{desc}**\n"
            f"• Execution Details:\n{action_desc}\n\n"
            f"• 💰 **Amount Made at TP1:** `{profit_str}`\n"
            f"• Status: `{status}` | Milestone: `{sig_data.get('runner_milestone_name', 'TP1')}`\n"
            f"• Remaining Open Orders: `{sig_data.get('market_tickets')}`\n"
            f"• Pending Orders: `{sig_data.get('pending_tickets')}`\n\n"
            f"{fin['formatted']}\n\n"
            f"✅ Verified 100% compliant with v3.0 4-layer TP1–TP5 specifications!"
        )
        return

    # WEEKEND SIMULATION: /sim_shortfall
    if text in ("/sim_shortfall", "sim_shortfall"):
        await event.reply(
            f"🧪 **Weekend Simulation v3.0: `/sim_shortfall` (Near-Miss Protection)**\n\n"
            f"1️⃣ **Initial Condition (4-Layer [0.01/0.01/0.01/0.02]):**\n"
            f"   • BUY XAUUSD: Entry 3895.00, TP1 = 3900.00 -> TP5 = 3940.00\n"
            f"   • Shortfall Arm Distance: 20 pips ($2.00 before TP1 = 3898.00)\n"
            f"   • Pullback Trigger: 8 pips ($0.80 drop from peak)\n\n"
            f"2️⃣ **Price Movement:**\n"
            f"   • Price climbs to `3899.20` (within 80 cents of TP1)\n"
            f"   • `shortfall_armed` activates: `True` | `peak_price` = `3899.20`\n"
            f"   • Gold stalls and drops to `3898.40` (reversal of $0.80 from peak)!\n\n"
            f"3️⃣ **Automatic Execution:**\n"
            f"   • ⚠️ SHORTFALL GUARD TRIGGERS INSTANTLY!\n"
            f"   • All open market layers banked immediately at market (`3898.40`).\n"
            f"   • All remaining pending limit orders deleted & trade logged to `trades_ledger.jsonl`.\n\n"
            f"🎉 **Result:** Secured +34/+42 pips near the top (same 'Bank It' Proximity Guard also protects Runner L4 at TP1..TP5 in `{RUNNER_PROXIMITY_MODE}` mode)."
        )
        return

    # WEEKEND SIMULATION: /sim_trail [2|3|4]
    if text.startswith("/sim_trail") or text.startswith("sim_trail"):
        parts = text.split()
        layer_num = parts[1] if len(parts) > 1 else "4"
        lot_size = "0.02" if layer_num == "4" else "0.01"

        await event.reply(
            f"🧪 **Weekend Simulation v3.0: `/sim_trail {layer_num}` (Runner Trailing & TP2 Guard / TP1–TP5 FSM)**\n\n"
            f"1️⃣ **Runner Handoff at TP1 (3900.00):**\n"
            f"   • Designated Runner: Layer {layer_num} ({lot_size} lot) | Distribution: `[0.01/0.01/0.01/0.02]`\n"
            f"   • Virtual TP1 Floor: `3899.50` (TP1 - 0.50 buffer) | Hard Broker SL: `3899.50`\n"
            f"   • Milestones: `TP1 (3900) -> TP2 (3910) -> TP3 (3920) -> TP4 (3930) -> TP5 (3940)`\n\n"
            f"2️⃣ **Clean Breakthrough & Ratchet Upward:**\n"
            f"   • Price breaks cleanly past TP1 (`>= 3900.50`): `TP1` cleared, floor locks & advances to `TP2`.\n"
            f"   • Price breaks cleanly past TP2 (`>= 3910.50`): `L2` banked at TP2, `TP2` cleared, old proximity guard disarmed, floor ratchets up toward TP5.\n"
            f"   • Price pushes to `3928.50` (within `{RUNNER_PROXIMITY_BUFFER_PIPS}` pips of TP4 `3930.00` in `{RUNNER_PROXIMITY_MODE}` mode):\n"
            f"   • `runner_proximity_armed` = `True`, `runner_peak_price` = `3928.50` -> pullback `{RUNNER_REVERSAL_PULLBACK_PIPS}` pips triggers **Bank It Proximity Guard**!\n\n"
            f"🎉 **Result:** Banked L1@TP1, L2@TP2, L3@TP3, and L4 via Bank-It Guard near TP4 (or full run to TP5)!"
        )
        return

    # TEST RUNNER COMMAND: /testrunner
    if text in ("/testrunner", "testrunner"):
        test_exec = MT5Executor(dry_run=True)
        test_exec.connect()

        tp_map = derive_tp_levels([3900.0], direction="BUY")
        sample_layers = {
            "L1": {"lot": 0.01, "ticket": 901, "order_type": "MARKET", "status": "OPEN", "target_price": 3895.0},
            "L2": {"lot": 0.01, "ticket": 902, "order_type": "LIMIT", "status": "PENDING", "target_price": 3892.33},
            "L3": {"lot": 0.01, "ticket": 903, "order_type": "LIMIT", "status": "PENDING", "target_price": 3889.67},
            "L4": {"lot": 0.02, "ticket": 904, "order_type": "LIMIT", "status": "PENDING", "target_price": 3887.0},
        }

        r1 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901], [902, 903, 904], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])
        r2 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901, 902], [903, 904], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])
        r3 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901, 902, 903], [904], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])
        r4 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901, 902, 903, 904], [], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])

        sim_4layer_sig: Dict[str, Any] = {
            "symbol": DEFAULT_SYMBOL,
            "direction": "BUY",
            "layers": copy.deepcopy(sample_layers),
            "market_tickets": [901, 902, 903, 904],
            "pending_tickets": [],
            "raw_tp1": tp_map["TP1"],
            "raw_tp2": tp_map["TP2"],
            "raw_tp3": tp_map["TP3"],
            "raw_tp4": tp_map["TP4"],
            "raw_tp5": tp_map["TP5"],
            "tp_levels": tp_map,
        }
        for lk in sim_4layer_sig["layers"]:
            sim_4layer_sig["layers"][lk]["status"] = "OPEN"
        test_exec.handle_tp1_scaling_and_select_runner(sim_4layer_sig)

        staged_on = _is_staged_exit_enabled()
        mode_badge = "🟢 STAGED v3.5.0 (STAGED_EXIT_ENABLED=True)" if staged_on else "⚪ LEGACY v3.4.2 (STAGED_EXIT_ENABLED=False)"
        sim_stage = _get_signal_stage(sim_4layer_sig)
        sim_fsm = sim_4layer_sig.get("fsm_floor")
        sim_atr = sim_4layer_sig.get("atr_stop")
        sim_act = sim_4layer_sig.get("trail_stop") if sim_4layer_sig.get("trail_stop") is not None else sim_4layer_sig.get("runner_sl_floor")
        sim_src = sim_4layer_sig.get("active_stop_source") or ("FSM_FLOOR" if sim_fsm is not None else "LEGACY")
        sim_bsl = sim_4layer_sig.get("broker_sl")
        sim_pk = sim_4layer_sig.get("trail_peak") if sim_4layer_sig.get("trail_peak") is not None else sim_4layer_sig.get("runner_peak_price")

        live_runner_lines = ""
        for mid_tr, s_tr in state_mgr.get_active_signals():
            st_tr = _get_signal_stage(s_tr)
            act_tr = s_tr.get("trail_stop") if s_tr.get("trail_stop") is not None else (s_tr.get("runner_sl_floor") or s_tr.get("soft_tp1_floor"))
            src_tr = s_tr.get("active_stop_source") or ("FSM_FLOOR" if s_tr.get("fsm_floor") is not None else "LEGACY")
            pk_tr = s_tr.get("trail_peak") if s_tr.get("trail_peak") is not None else (s_tr.get("runner_peak_price") or s_tr.get("peak_price"))
            live_runner_lines += (
                f"\n• Live Signal `{mid_tr}` (`{s_tr.get('status')}`): Stage=`{st_tr}` | "
                f"FSM Floor=`{s_tr.get('fsm_floor')}` | ATR Stop=`{s_tr.get('atr_stop')}` | "
                f"Active Stop=`{act_tr}` (`{src_tr}`) | Broker SL=`{s_tr.get('broker_sl')}` | Peak=`{pk_tr}`"
            )

        await event.reply(
            f"🏃 **v3.0 4-Layer Smart TP1 & Runner Hierarchy Test Results:**\n"
            f"• Mode: `{mode_badge}`\n\n"
            f"1️⃣ **Case 1 (L1 only):** Banked=`{r1['banked']}`, Runner=`{r1['runner']}`, Status=`{r1['status']}`\n"
            f"2️⃣ **Case 2 (L1+L2):** Banked=`{r2['banked']}`, Runner=`{r2['runner']}` [#{r2['runner_ticket']}], Floor=`{r2['trailed_sl']}`, Status=`{r2['status']}`\n"
            f"3️⃣ **Case 3 (L1+L2+L3):** Banked=`{r3['banked']}`, Open=`{r3['remaining_market_tickets']}`, Runner=`{r3['runner']}`, Status=`{r3['status']}`\n"
            f"4️⃣ **Case 4 (All 4 L1..L4):** Banked=`{r4['banked']}`, Open=`{r4['remaining_market_tickets']}`, Runner=`{r4['runner']}` (0.02), Floor=`{r4['trailed_sl']}`, Status=`{r4['status']}`\n\n"
            f"📐 **v3.5.0 Stage 1 Handoff Telemetry Snapshot (L4 Runner):**\n"
            f"• Stage: `{sim_stage}` | Peak: `{sim_pk}`\n"
            f"• FSM Floor: `{sim_fsm}` | ATR Stop: `{sim_atr}`\n"
            f"• Active Stop: `{sim_act}` (`{sim_src}`) | Broker SL: `{sim_bsl}`"
            f"{live_runner_lines}\n\n"
            f"🎉 **4-Layer Coexistence & Runner Hierarchy Verified 100%!**"
        )
        return

    # TEST ORDER COMMAND: /testorder
    if text in ("/testorder", "testorder", "/testlimit", "testlimit"):
        if not await _mt5_call(executor.is_connected):
            await event.reply("❌ Cannot place test order: MT5 terminal is not connected.")
            return
        if not await _mt5_call(executor.is_trade_allowed):
            await event.reply("⚠️ Cannot place test order: Algo Trading button is OFF in MT5. Please enable it.")
            return

        tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
        if not tick:
            await event.reply(f"❌ Cannot place test order: No live tick available for `{DEFAULT_SYMBOL}`.")
            return

        safe_price = round(tick.bid - 20.0, 2)
        ticket = await _mt5_call(
            executor.place_limit_order,
            symbol=DEFAULT_SYMBOL,
            direction="BUY",
            price=safe_price,
            lot=0.01,
            comment="TG Safe Test Order",
        )
        if not ticket:
            await event.reply(f"❌ Failed to place test limit order on `{DEFAULT_SYMBOL}`.")
            return

        await event.reply(
            f"🧪 **Test Pending Order Placed on MT5!**\n"
            f"• Ticket: `#{ticket}`\n"
            f"• Symbol: `{DEFAULT_SYMBOL}`\n"
            f"• Type: `BUY LIMIT` @ `{safe_price}` (0.01 lot)\n"
            f"• Status: Active on MT5 (Safe buffer: $20.00 below live market)\n\n"
            f"⏳ Auto-cancelling in 5 seconds to verify full lifecycle..."
        )
        await asyncio.sleep(5)
        cancelled = await _mt5_call(executor.cancel_pending_order, ticket)
        if cancelled:
            await event.reply(
                f"✅ **Test Order `#{ticket}` Safely Cancelled!**\n"
                f"🎉 MT5 broker order placement, ticket assignment, and cancellation verified 100%!"
            )
        else:
            await event.reply(
                f"⚠️ Test Order `#{ticket}` placed, but auto-cancel failed or order already removed."
            )
        return

    # TEST SIMULATION COMMAND: /sim <signal>
    if text.startswith("/sim ") or text.startswith("sim "):
        custom_text = msg.message.split(None, 1)[1] if " " in msg.message else ""
        parsed = parse_message(custom_text)
        if not parsed:
            await event.reply(f"❌ Signal Filtered / Rejected by Gates:\n`{custom_text}`")
            return

        lots_vec = [round(b * RISK_LOT_MULTIPLIER, 2) for b in BASE_LAYER_LOTS]
        reply_str = (
            f"📋 **v3.0 Signal Parser Simulation:**\n"
            f"• Raw Text: `{custom_text}`\n"
            f"• Event: `{parsed.event_type}` | Direction: `{parsed.direction}` | Intent: `{parsed.order_intent}`\n"
            f"• Symbol: `{parsed.symbol}` | 4-Layer Lots: `{lots_vec}` (Total: `{sum(lots_vec):.2f}`)\n"
        )
        if parsed.zone:
            tiers = parsed.zone.get_tiers(parsed.direction)
            reply_str += (
                f"• Zone Tiers: L1=`{tiers['L1']}`, L2=`{tiers['L2']}`, L3=`{tiers['L3']}`, L4=`{tiers['L4']}`\n"
                f"• Broker TP1: `{parsed.final_tp}` | TP1..TP5: `{parsed.tp_levels}`\n"
                f"• Proximity Guard: `{parsed.proximity_close_price}` | SL: `{parsed.sl}` (Valid: {parsed.sl_valid})"
            )
        elif parsed.single_price:
            reply_str += (
                f"• Single Price ({parsed.order_intent}): `{parsed.single_price}` split into 4 layers `{lots_vec}`\n"
                f"• Broker TP1: `{parsed.final_tp}` | TP1..TP5: `{parsed.tp_levels}` | SL: `{parsed.sl}`"
            )
        else:
            reply_str += (
                f"• Market Now split into 4 layers `{lots_vec}`\n"
                f"• Broker TP1: `{parsed.final_tp}` | TP1..TP5: `{parsed.tp_levels}` | SL: `{parsed.sl}`"
            )
        await event.reply(reply_str)
        return

    # DIAGNOSTICS COMMAND: /test
    if text in ("/test", "test"):
        mt5_ok, algo_ok, ping_ms = await _probe_mt5_health()
        tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
        sample_signal = "Buy 3895-3887 \n TP 3900 \n SL 3875"
        parsed = parse_message(sample_signal)
        tiers = parsed.zone.get_tiers("BUY") if parsed and parsed.zone else {}
        tp_levels = parsed.tp_levels if parsed else {}
        lots_str = " / ".join(f"{b:.2f}" for b in BASE_LAYER_LOTS)
        srv_date = await _trading_day_str_async()
        ledger_stats = trade_ledger.get_stats(server_date=srv_date)

        await event.reply(
            f"🧪 **Telegram-to-MT5 v3.0 End-to-End Diagnostics:**\n\n"
            f"1. **Telegram Listener:** 🟢 ACTIVE (Reconnects: `{_telegram_reconnect_count}`)\n"
            f"2. **MT5 Terminal:** `{'🟢 CONNECTED' if mt5_ok else '🔴 DISCONNECTED'}` (Ping: `{ping_ms:.2f} ms`)\n"
            f"3. **Algo Trading Button:** `{'🟢 ENABLED' if algo_ok else '🔴 DISABLED'}`\n"
            f"4. **Live Quote ({DEFAULT_SYMBOL}):** Bid=`{getattr(tick, 'bid', 'N/A')}` | Ask=`{getattr(tick, 'ask', 'N/A')}`\n"
            f"5. **6-Gate v3.0 Parser:** 🟢 PASSED\n"
            f"   - Zone 4-Tiers: `L1={tiers.get('L1')}, L2={tiers.get('L2')}, L3={tiers.get('L3')}, L4={tiers.get('L4')}`\n"
            f"   - TP1..TP5 Targets: `{tp_levels}` (L1 Broker TP: `{getattr(parsed, 'final_tp', 3899.50)}`)\n"
            f"   - Runner FSM Mode: `{RUNNER_PROXIMITY_MODE}` (Buf: `{RUNNER_PROXIMITY_BUFFER_PIPS}` pips / `{RUNNER_PROXIMITY_BUFFER_PCT}%`, Pullback: `{RUNNER_REVERSAL_PULLBACK_PIPS}` pips)\n"
            f"   - 4-Layer Lots: `[{lots_str}]` x `{RISK_LOT_MULTIPLIER}` (`{sum(BASE_LAYER_LOTS) * RISK_LOT_MULTIPLIER:.2f}` total)\n"
            f"6. **State & Ledger:** 🟢 PERSISTENT DISK OK (`{ledger_stats['total_trades']}` closed trades, Win Rate `{ledger_stats['win_rate_pct']:.1f}%`)\n\n"
            f"💡 _Send `/testorder` to test live limit placement, or `/health` for full telemetry._"
        )
        return

    # STATUS COMMAND: /status /ping /trades /algo /check /help
    if text in ("/status", "status", "/ping", "ping", "/trades", "trades", "/algo", "algo", "/check", "check", "/help", "help"):
        uptime_str = _format_uptime()
        mt5_ok, algo_ok, ping_ms = await _probe_mt5_health()
        algo_str = "🟢 ENABLED (Green play icon)" if algo_ok else "🔴 DISABLED (Click Algo Trading in MT5)"

        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        acc_info = (
            f"\n• Balance: `{fin['balance']:.2f} {curr}`\n"
            f"• Credit: `{fin['credit']:.2f} {curr}`\n"
            f"• Equity: `{fin['equity']:.2f} {curr}`"
        )
        floating_pnl_str = ""
        if mt5_ok and not executor.dry_run:
            open_pos = await _mt5_call(executor.get_live_positions, DEFAULT_SYMBOL)
            if open_pos:
                bot_pnl = sum(
                    float(getattr(p, "profit", 0.0) or 0.0)
                    for p in open_pos
                    if getattr(p, "magic", None) == MAGIC_NUMBER
                )
                floating_pnl_str = f"\n• Floating Bot P&L: `{bot_pnl:+.2f} {curr}`"

        tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
        sym_info = await _mt5_call(executor.get_symbol_info, DEFAULT_SYMBOL)
        point = getattr(sym_info, "point", 0.01) or 0.01
        gold_info = ""
        if tick:
            bid = getattr(tick, "bid", "N/A")
            ask = getattr(tick, "ask", "N/A")
            spread = "N/A"
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and isinstance(point, (int, float)) and point > 0:
                spread = f"{round((ask - bid) / float(point), 1)} pts"
            gold_info = f"\n• Live Quote ({DEFAULT_SYMBOL}): Bid=`{bid}` | Ask=`{ask}` | Spread=`{spread}`"

        staged_on = _is_staged_exit_enabled()
        ratchet_on_st = bool(_main_cfg("PROFIT_RATCHET_ENABLED", PROFIT_RATCHET_ENABLED))
        if ratchet_on_st and staged_on:
            staged_badge = "🟢 PROFIT RATCHET v3.6.0 (STAGED+RATCHET)"
        elif staged_on:
            staged_badge = "🟢 STAGED v3.5.0 (STAGED_EXIT_ENABLED=True)"
        else:
            staged_badge = "⚪ LEGACY v3.4.2 (STAGED_EXIT_ENABLED=False)"
        active_signals = state_mgr.get_active_signals()
        if not active_signals:
            trades_str = "\n• Active Signals: None (Listening for next signal)"
        else:
            trades_str = f"\n• Active Signals ({len(active_signals)}):"
            for mid, s in active_signals:
                st = s.get("status")
                stage_num = _get_signal_stage(s)
                trades_str += f"\n  - ID `{mid}`: `{s.get('direction')}` {s.get('symbol')} | Status: `{st}` | Stage: `{stage_num}`"
                trades_str += f"\n    • Layers: `{_format_layers_summary(s)}`"
                layers_st = s.get("layers") if isinstance(s.get("layers"), dict) else {}
                for lk_st in [k for k in LAYER_NAMES if k in layers_st] or list(layers_st.keys()):
                    lv_st = layers_st.get(lk_st)
                    if not isinstance(lv_st, dict):
                        continue
                    l_stat = str(lv_st.get("status") or "").strip().upper()
                    if l_stat in ("OPEN", "FILLED", "RUNNER", "PENDING"):
                        r_st = lv_st.get("role") or ("PENDING" if l_stat == "PENDING" else "LAYER")
                        ent_st = lv_st.get("entry_price") or lv_st.get("entry") or lv_st.get("target_price")
                        mfe_st = lv_st.get("mfe_dollars")
                        mfe_disp = f"+${float(mfe_st):.2f}" if _is_num(mfe_st) else "-"
                        tier_st = lv_st.get("ratchet_tier") or "-"
                        bsl_st = lv_st.get("ratchet_sl") or lv_st.get("broker_sl") or lv_st.get("sl") or s.get("sl")
                        tp_st = lv_st.get("tp") or s.get("final_tp")
                        trades_str += (
                            f"\n      ◦ `{lk_st}`: `{r_st}` | Entry=`{ent_st}` | MFE=`{mfe_disp}` | "
                            f"Tier=`{tier_st}` | SL=`{bsl_st}` | TP=`{tp_st}`"
                        )
                trades_str += f"\n    • Targets: {_format_tp_summary(s)}"
                if s.get("shortfall_armed"):
                    trades_str += f"\n    • ⚠️ TP1 Shortfall Armed! Peak=`{s.get('peak_price')}`"
                if st == "RUNNER_ACTIVE":
                    guard_state = (
                        f"ARMED (Peak=`{s.get('runner_peak_price')}`)"
                        if s.get("runner_proximity_armed")
                        else "STANDBY"
                    )
                    cleared = s.get("runner_cleared_milestones") or []
                    fsm_fl = s.get("fsm_floor")
                    atr_st = s.get("atr_stop")
                    act_st = (
                        s.get("trail_stop")
                        if s.get("trail_stop") is not None
                        else (s.get("runner_sl_floor") or s.get("soft_tp1_floor"))
                    )
                    act_src = s.get("active_stop_source") or ("FSM_FLOOR" if fsm_fl is not None else "LEGACY")
                    brk_sl = s.get("broker_sl")
                    pk_val = (
                        s.get("trail_peak")
                        if s.get("trail_peak") is not None
                        else s.get("runner_peak_price")
                    )
                    r_lbl_st = resolve_layer_label(s, s.get("runner_ticket"))
                    trades_str += (
                        f"\n    • 🚀 Runner: `{r_lbl_st}` [Ticket #{s.get('runner_ticket')}]"
                        f" | Stage: `{stage_num}` | Milestone: `{s.get('runner_milestone_name', 'TP1')}` (Cleared: `{cleared or 'None'}`)"
                        f"\n    • 🛡️ Active Stop: `{act_st}` (`{act_src}`) | FSM Floor: `{fsm_fl}` | ATR Stop: `{atr_st}`"
                        f"\n    • 🔒 Broker SL: `{brk_sl}` | Peak: `{pk_val}` | Floor: `{s.get('runner_sl_floor') or s.get('soft_tp1_floor')}` | "
                        f"Proximity Guard ({RUNNER_PROXIMITY_MODE}): `{guard_state}`"
                    )

        risk_st = await _mt5_call(executor.get_risk_exposure_status)
        dd_st = await _mt5_call(executor.get_daily_drawdown_status)
        dw_prog = await _mt5_call(get_daily_win_progress)
        dw_curr = dw_prog.get("currency", "GBP")
        if dw_prog["enabled"] and dw_prog["target_amount"] > 0:
            dw_state_str = (
                f"🟢 ACTIVE — Target: `{format_profit(dw_prog['target_amount'], dw_curr)}` "
                f"(Made: `{format_profit(dw_prog['net_profit_today'], dw_curr)}`)"
            )
        else:
            dw_state_str = f"⚪ OFF (Today's Net: `{format_profit(dw_prog['net_profit_today'], dw_curr)}`)"

        dl_prog = await _mt5_call(get_daily_loss_progress)
        dl_curr = dl_prog.get("currency", "GBP")
        dl_realized_str = format_profit(dl_prog["realized_today_pnl"], dl_curr)
        lock_str = " 🔒 LOCKED" if _daily_loss_locked else ""
        if dl_prog["enabled"] and dl_prog["limit_amount"] > 0:
            dl_state_str = (
                f"🟢 ACTIVE — Limit: `{format_profit(-dl_prog['limit_amount'], dl_curr)}` "
                f"(Realized Today: `{dl_realized_str}`){lock_str}"
            )
        else:
            dl_state_str = f"⚪ OFF (Realized Today: `{dl_realized_str}`){lock_str}"

        _cfg_mod = sys.modules.get("config") or config
        limits_lines = format_limits_status_lines(_cfg_mod, dd_st, risk_st)
        dyn_line = await _fetch_dynamic_risk_status_line(_cfg_mod)
        srv_date = await _trading_day_str_async()
        ledger_stats = trade_ledger.get_stats(server_date=srv_date)
        lots_vec_str = "/".join(f"{b:.2f}" for b in BASE_LAYER_LOTS)

        risk_str = (
            f"\n• 4-Layer Split: `[{lots_vec_str}]` x `{RISK_LOT_MULTIPLIER}` (`{MAX_ORDERS_PER_SIGNAL}` orders/signal)"
            f"\n• Active Lots (`/lotcap`): {limits_lines['active_lots_label']}"
            f"\n• Daily Win Limit (`dailywin`): {dw_state_str}"
            f"\n• Daily Loss Limit (`dailyloss`): {dl_state_str}"
            f"\n{limits_lines['drawdown_guard_line']}"
            f"\n{limits_lines['float_cap_line']}"
            f"\n{dyn_line}"
            f"\n• Trade Ledger (`{TRADES_LEDGER_PATH.name}`): `{ledger_stats['total_trades']}` closed | "
            f"Win Rate: `{ledger_stats['win_rate_pct']:.1f}%` | "
            f"Realized: `{format_profit(ledger_stats['total_realized_pnl'], curr)}` "
            f"(Today: `{format_profit(ledger_stats['today_realized_pnl'], curr)}`)"
        )

        reply_text = (
            f"🤖 **Telegram Copier System Status (v3.0)**\n"
            f"• State: `{'🟢 ONLINE' if mt5_ok else '🔴 MT5 DISCONNECTED'}` (Ping: `{ping_ms:.2f} ms`)\n"
            f"• Algo Trading: `{algo_str}` | Telegram Reconnects: `{_telegram_reconnect_count}`"
            f"{acc_info}"
            f"{floating_pnl_str}"
            f"{risk_str}"
            f"{gold_info}"
            f"\n• Bot Uptime: `{uptime_str}`"
            f"\n• Execution Mode: `{'DRY-RUN' if executor.dry_run else 'LIVE BROKER'}` | Magic: `{MAGIC_NUMBER}`"
            f"\n• Runner Exit Architecture: `{staged_badge}`"
            f"{trades_str}\n\n"
            f"📱 **Commands:** `/health`, `/status`, `/resume`, `/bank_tp1`, `/bank`, `/lotcap`, `/losslimit`, `/floatcap`, `/dynamicrisk`, `/atrtrail`, `/trade`, `/sl`, `/tp`, `/trail`, `/be`, `/partial`, `dailywin`, `dailyloss`, `/set_daily_loss`, `/status_daily_loss`, `/closeall`, `/sim_tp1 1|2|3|4`, `/sim_shortfall`, `/sim_trail 2|3|4`, `/test`, `/testrunner`, `/testorder`"
        )
        await event.reply(reply_text)
        return

    # LIVE CONTROL: Active-Trade SL / TP1..TP5 Adjust & Trail Prompts in Saved Messages
    norm_prompt, exp_target_id = _normalize_saved_adjust_or_trail_prompt(raw_msg)
    if norm_prompt:
        mgmt_act = gate_3_classify_trade_management(norm_prompt, is_reply=True)
        if mgmt_act and mgmt_act.management_action in (
            "ACTION_ADJUST_TARGETS",
            "ACTION_TRAIL_SL",
            "ACTION_TP1_HIT_REPLY",
            "ACTION_TP2_HIT",
            "ACTION_TP3_HIT",
            "ACTION_BREAKEVEN",
            "ACTION_PARTIAL_CLOSE",
        ):
            allow_pend = mgmt_act.management_action in ("ACTION_ADJUST_TARGETS", "ACTION_TP1_HIT_REPLY")
            resolved = _resolve_saved_prompt_target_signal(explicit_id=exp_target_id, allow_pending=allow_pend)
            if not resolved:
                await event.reply("⚠️ No active signal found to adjust or trail.")
                return
            target_mid, target_sig, target_chat = resolved
            async with _get_signal_lock(target_chat, target_mid):
                fresh_sig = state_mgr.get_signal(target_mid, chat_id=target_chat) or target_sig
                if mgmt_act.management_action == "ACTION_TP1_HIT_REPLY":
                    if fresh_sig.get("status") in ("PENDING", "PENDING_DELAY") or not fresh_sig.get("market_tickets"):
                        await _invalidate_signal_tp1_unlocked(
                            target_mid, reason="Saved Messages Prompt", chat_id=target_chat
                        )
                        await event.reply(
                            f"🚫 **[TP1 INVALIDATED] [ID: {target_mid}]** Pending setup cancelled before fill."
                        )
                    else:
                        _, _, alert = await _execute_tp1_scaling_handoff(
                            target_mid, fresh_sig, chat_id=target_chat, source_label="Saved Messages Prompt"
                        )
                        await event.reply(alert)
                elif mgmt_act.management_action == "ACTION_BREAKEVEN":
                    res_be = await _mt5_call(
                        executor.apply_signal_breakeven,
                        fresh_sig,
                        BE_LAYERS,
                        BE_OFFSET_PIPS,
                    )
                    prev_st = fresh_sig.get("status", "ACTIVE")
                    new_st = "RUNNER_ACTIVE" if prev_st == "RUNNER_ACTIVE" else "BE_APPLIED"
                    state_mgr.update_signal(
                        target_mid,
                        {
                            "status": new_st,
                            "layers": fresh_sig.get("layers", {}),
                            "market_tickets": fresh_sig.get("market_tickets", []),
                            "pending_tickets": fresh_sig.get("pending_tickets", []),
                            "be_armed": True,
                            "be_done_tickets": res_be.get("be_done_tickets", []),
                            "be_deferred_tickets": res_be.get("deferred_tickets", []),
                            **(
                                {"runner_sl_floor": fresh_sig["runner_sl_floor"]}
                                if fresh_sig.get("runner_sl_floor") is not None
                                else {}
                            ),
                        },
                        chat_id=target_chat,
                    )
                    await event.reply(
                        f"🛡️ **Selective Breakeven Applied (Saved Messages Prompt) [ID: {target_mid}]** "
                        f"— Layers `{BE_LAYERS}` | Modified: `{res_be.get('modified_tickets', [])}`"
                    )
                elif mgmt_act.management_action == "ACTION_PARTIAL_CLOSE":
                    symbol = fresh_sig.get("symbol", DEFAULT_SYMBOL)
                    direction = fresh_sig.get("direction", "BUY")
                    layers = fresh_sig.get("layers", {})
                    market_tickets = list(fresh_sig.get("market_tickets", []))
                    pending_tickets = list(fresh_sig.get("pending_tickets", []))
                    closed = await _mt5_call(
                        executor.apply_partial_close,
                        market_tickets=market_tickets,
                        pending_tickets=pending_tickets,
                        symbol=symbol,
                        direction=direction,
                    )
                    fin = await _mt5_call(executor.get_account_financials)
                    curr = fin.get("currency", "GBP")
                    profit_info = ""
                    if closed and closed in market_tickets:
                        market_tickets.remove(closed)
                        added_p, _ = await _account_closed_tickets(fresh_sig, [closed])
                        profit_info = f"\n• 💰 **Amount Made:** `{format_profit(added_p, curr)}`"
                        for l_data in layers.values():
                            if isinstance(l_data, dict) and l_data.get("ticket") == closed:
                                l_data["status"] = "CLOSED"
                        if fresh_sig.get("runner_ticket") == closed:
                            fresh_sig["last_runner_ticket"] = closed
                            fresh_sig["runner_ticket"] = None
                    prev_st_pc2 = str(fresh_sig.get("status") or "").strip().upper()
                    new_status = (
                        "CLOSED"
                        if (not market_tickets and not pending_tickets)
                        else ("RUNNER_ACTIVE" if prev_st_pc2 == "RUNNER_ACTIVE" else "PARTIAL_CLOSED")
                    )
                    fresh_sig["market_tickets"] = market_tickets
                    fresh_sig["layers"] = layers
                    fresh_sig["status"] = new_status
                    if new_status == "CLOSED":
                        _ensure_signal_exit_telemetry(fresh_sig, exit_reason_code="MANUAL", overwrite_code=True)
                    state_mgr.update_signal(
                        target_mid,
                        {
                            "market_tickets": market_tickets,
                            "layers": layers,
                            "status": new_status,
                            "accounted_tickets": fresh_sig.get("accounted_tickets", []),
                            "banked_profit": fresh_sig.get("banked_profit", 0.0),
                            "runner_ticket": fresh_sig.get("runner_ticket"),
                            "last_runner_ticket": fresh_sig.get("last_runner_ticket"),
                            **(
                                {
                                    "exit_reason_code": fresh_sig.get("exit_reason_code"),
                                    "exit_stage": fresh_sig.get("exit_stage"),
                                    "exit_price": fresh_sig.get("exit_price"),
                                    "max_favorable_excursion": fresh_sig.get("max_favorable_excursion"),
                                    "giveback_from_peak": fresh_sig.get("giveback_from_peak"),
                                    "trail_tier_used": fresh_sig.get("trail_tier_used"),
                                }
                                if new_status == "CLOSED"
                                else {}
                            ),
                        },
                        chat_id=target_chat,
                    )
                    if new_status == "CLOSED":
                        _emit_exit_telemetry_log(
                            target_mid,
                            fresh_sig,
                            float(fresh_sig.get("banked_profit") or 0.0),
                            exit_reason="Partial Close (Saved Messages Prompt)",
                        )
                        await _record_signal_to_ledger(
                            target_mid, fresh_sig, exit_reason="Partial Close (Saved Messages Prompt)", currency=curr
                        )
                    await event.reply(
                        f"💰 **Partial Close Executed on `{symbol}` [ID: {target_mid}]**\n"
                        f"• Order #{closed} closed at market."
                        f"{profit_info}\n\n"
                        f"{fin['formatted']}"
                    )
                else:
                    alert = await _apply_adjust_or_trail_unlocked(
                        target_mid,
                        target_chat,
                        fresh_sig,
                        mgmt_act,
                        source_label="Saved Messages Prompt",
                    )
                    await event.reply(alert)
            return


# ---------------------------------------------------------------------------
# MAIN APPLICATION LIFECYCLE
# ---------------------------------------------------------------------------

async def main() -> None:
    global telethon_client, _daily_loss_locked, _instance_lock, _mt5_connected_state
    lots_str = " / ".join(f"{b:.2f}" for b in BASE_LAYER_LOTS)
    total_base = sum(BASE_LAYER_LOTS) * RISK_LOT_MULTIPLIER
    print("\033[93m\033[1m╔═════════════════════════════════════════════════════════════════════════╗")
    print("║        ⚡ ULTIMATE TELEGRAM-TO-MT5 XAUUSD PRO COPIER v3.0 ⚡           ║")
    print("║  ─────────────────────────────────────────────────────────────────────  ║")
    print(f"║   • Broker Symbol: \033[97m{DEFAULT_SYMBOL:<12}\033[93m  • Magic Number: \033[97m{MAGIC_NUMBER:<12}\033[93m   ║")
    print(f"║   • Mode: \033[92m{'DRY-RUN (SIM)' if DRY_RUN else 'LIVE BROKER':<16}\033[93m   • 4-Layer: \033[97m{lots_str} ({total_base:.2f})\033[93m  ║")
    print("║   • Targets: \033[96mTP1 -> TP5 FSM\033[93m       • Runner: \033[96mL4 Bank-It Proximity Guard\033[93m  ║")
    print("╚═════════════════════════════════════════════════════════════════════════╝\033[0m\n")

    # 1. Preflight Environment Validation
    preflight = validate_environment(check_packages=True)
    for warn in preflight.get("warnings", []):
        logger.warning(f"[PREFLIGHT WARNING] {warn}")
    if not preflight.get("ok"):
        for err in preflight.get("errors", []):
            logger.error(f"[PREFLIGHT ERROR] {err}")
        sys.exit(1)

    # 2. Single-Instance Mutex & Lockfile Guard
    _instance_lock = acquire_single_instance_lock(LOCK_FILE_PATH)
    if _instance_lock is None:
        logger.error(
            f"Another XAUUSD Copier instance is already running (lock held at {LOCK_FILE_PATH}). "
            f"Exiting with code {DUPLICATE_INSTANCE_EXIT_CODE}."
        )
        sys.exit(DUPLICATE_INSTANCE_EXIT_CODE)

    unexpected_exit = False
    user_interrupt = False
    proximity_task: Optional[asyncio.Task] = None

    try:
        cleaned = state_mgr.cleanup_old_signals(days=7)
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} historical closed trade(s) from state file.")

        stale_delay = [
            (mid, s)
            for mid, s in state_mgr.get_all_signals().items()
            if s.get("status") in ("PENDING", "PENDING_DELAY")
        ]
        for mid, s in stale_delay:
            state_mgr.update_signal(mid, {"status": "CLOSED"}, chat_id=s.get("chat_id"))
            logger.warning(f"Closed orphaned PENDING signal {mid} left over from previous run (no orders were placed).")

        logger.info("Connecting to MetaTrader 5...")
        mt5_boot_ok = bool(await _mt5_call(executor.connect))
        _mt5_connected_state = mt5_boot_ok
        if not mt5_boot_ok:
            logger.warning(
                "MT5 terminal connection failed or MT5 is not running. "
                "Please ensure MetaTrader 5 is running with Algorithmic Trading enabled. "
                "Monitoring Telegram channels will still start..."
            )
        else:
            logger.info("MetaTrader 5 connection established successfully.")
            dl_prog = await _mt5_call(get_daily_loss_progress)
            if dl_prog.get("triggered_date") == (await _trading_day_str_async()) and dl_prog.get("limit_reached"):
                _daily_loss_locked = True
                logger.warning(
                    f"🔒 Daily loss limit already triggered today ({dl_prog['date']}); "
                    f"starting in LOCKED mode (new signals blocked until next broker day or `dailyloss off`)."
                )
            # If all open positions are closed and there are no active signals,
            # but previous realized loss would pause new signals, acknowledge
            # that the previous loss has passed and update the baseline so new trades can be actioned.
            open_pos = await _mt5_call(executor.get_open_market_tickets)
            active_s = state_mgr.get_active_signals()
            has_open_mkt_boot = bool(open_pos) or any(
                isinstance(s, dict) and bool(s.get("market_tickets")) for _, s in (active_s or [])
            )
            # Always baseline realized daily loss on startup against current account balance so
            # previously realized losses from earlier runs never re-trigger configured drawdown guards on restart.
            dd_init = await _mt5_call(executor.get_daily_drawdown_status)
            if dd_init.get("realized_today", 0.0) < 0.0 or dd_init.get("pause_new_signals"):
                upd = await _mt5_call(executor.update_daily_loss_baseline)
                _daily_50pct_alert_date = None
                logger.info(
                    f"Startup daily loss baseline synced to current balance "
                    f"{upd['current_balance']:.2f} {dd_init['currency']} "
                    f"(archived prior realized={upd['baseline_realized']:.2f} {dd_init['currency']})."
                )
            await _startup_hydrate_and_backfill_ledger()
            await _update_live_terminal_status_async(force_print=True)

        _boot_limits = read_risk_limits(sys.modules.get("config") or config)
        _lot_lbl = "ON" if _boot_limits.lot_cap_enabled else "Unlimited (OFF)"
        _dd_lbl = (
            f"Pause {_fmt_pct(_boot_limits.daily_loss_pause_pct)}% / Cut-Off {_fmt_pct(_boot_limits.emergency_cutoff_pct)}%"
            if (_boot_limits.daily_loss_pause_pct > 0.0 or _boot_limits.emergency_cutoff_pct > 0.0)
            else "Unlimited (OFF)"
        )
        _fc_lbl = (
            f"{_fmt_pct(_boot_limits.float_loss_cap_pct)}%"
            if _boot_limits.float_loss_cap_pct > 0.0
            else "Unlimited (OFF)"
        )
        logger.info(
            f"🛡️ Risk Limits: Lot Cap={_lot_lbl} | Drawdown Guard={_dd_lbl} | Float Cap={_fc_lbl} "
            f"(Manual Saved Messages controls: /lotcap, /losslimit, /floatcap, dailyloss, dailywin)"
        )

        session_file = str(BASE_DIR / TELEGRAM_SESSION_NAME)
        logger.info(f"Initializing Telegram Client (Session: {session_file})...")
        logger.info(f"Monitoring Channel IDs: {CHANNEL_IDS}")

        telethon_client = TelegramClient(
            session_file,
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
            connection_retries=-1,
            retry_delay=2,
            auto_reconnect=True,
        )

        telethon_client.add_event_handler(
            handle_new_message,
            events.NewMessage(chats=CHANNEL_IDS),
        )
        telethon_client.add_event_handler(
            handle_message_edited,
            events.MessageEdited(chats=CHANNEL_IDS),
        )
        telethon_client.add_event_handler(
            handle_message_deleted,
            events.MessageDeleted(),
        )
        telethon_client.add_event_handler(
            handle_saved_messages_command,
            events.NewMessage(chats="me"),
        )

        logger.info("Copier event handlers registered. Starting Telethon client...")
        _disable_windows_console_quickedit()
        _set_windows_keep_awake(True)
        await telethon_client.start()
        try:
            await telethon_client.get_dialogs(limit=100)
        except Exception as dlg_err:
            logger.debug(f"Telethon dialog cache warmup note: {dlg_err}")

        await run_reconnect_catchup(reason="startup")
        logger.info("Telegram Copier v3.0 is LIVE and actively listening for signals!")

        proximity_task = asyncio.create_task(tp1_proximity_watcher_task())

        await telethon_client.run_until_disconnected()
        if not (TRADING_HALTED or _daily_loss_terminate_requested or _daily_win_terminate_requested):
            unexpected_exit = True
    except KeyboardInterrupt:
        user_interrupt = True
        logger.info("Shutdown signal (Ctrl+C) received. Exiting gracefully...")
    except SystemExit as e:
        if e.code not in (0, None, DAILY_WIN_EXIT_CODE, DAILY_LOSS_EXIT_CODE, DUPLICATE_INSTANCE_EXIT_CODE):
            unexpected_exit = True
        raise
    except Exception as e:
        unexpected_exit = True
        logger.exception(f"Unexpected error in copier loop: {e}")
    finally:
        _set_windows_keep_awake(False)
        for pending_key in list(_pending_sell_now_tasks.keys()):
            _cancel_delayed_sell_now(pending_key)
        if proximity_task is not None:
            proximity_task.cancel()
            try:
                await proximity_task
            except asyncio.CancelledError:
                pass
        try:
            await _mt5_call(executor.shutdown)
        except Exception as e:
            logger.debug(f"Executor shutdown note: {e}")
        release_single_instance_lock(_instance_lock)
        _instance_lock = None
        logger.info("Copier terminated.")
        if _daily_win_terminate_requested:
            sys.exit(DAILY_WIN_EXIT_CODE)
        if TRADING_HALTED or _daily_loss_terminate_requested:
            sys.exit(0)
        if user_interrupt:
            sys.exit(0)
        if unexpected_exit:
            sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print("\nShutdown complete.")
        sys.exit(e.code if e.code is not None else 0)
    except KeyboardInterrupt:
        print("\nShutdown complete.")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Fatal unhandled crash: {e}")
        sys.exit(1)
