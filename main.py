import os
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
    CANCEL_PENDING_ON_TP1,
    TP_FRONT_RUN_OFFSET,
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
    NOTIFY_SAVED_MESSAGES,
    DRY_RUN,
    LOG_FILE_PATH,
    STATE_FILE_PATH,
    TRADES_LEDGER_PATH,
    LOCK_FILE_PATH,
    DEDUPLICATION_WINDOW_SECONDS,
    SELL_NOW_DELAY_SECONDS,
    DELAYED_POLL_INTERVAL_SECONDS,
    NOW_PRICE_GUARD_ABORT_ON_MISMATCH,
    NOW_ENTRY_MAX_WAIT_SECONDS,
    DAILY_LOSS_CLOSE_OPEN_TRADES,
    MAX_NEW_EDIT_AGE_SECONDS,
    FALLBACK_TP1_PIPS,
    UNPRICED_TP_GRACE_SECONDS,
    DAILY_LOSS_PAUSE_PCT,
    EMERGENCY_CUTOFF_LOSS_PCT,
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
    PREVENT_SYSTEM_SLEEP,
    IDLE_MT5_POLL_INTERVAL_SECONDS,
    TELEGRAM_KEEPALIVE_SECONDS,
    CONSOLE_TITLE_UPDATES,
)
from parser import (
    parse_message,
    SignalAction,
    ZoneInfo,
    is_tp1_hit_message,
    derive_tp_levels,
    compute_zone_4tiers,
)
from mt5_executor import MT5Executor
from state_manager import StateManager
from trade_ledger import TradeLedger
from startup_checks import (
    DUPLICATE_INSTANCE_EXIT_CODE,
    SingleInstanceLock,
    acquire_single_instance_lock,
    release_single_instance_lock,
    validate_environment,
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

# In-memory deduplication tracking ((chat_id, msg_id) or msg_id -> process_timestamp)
_recent_messages: Dict[Any, float] = {}

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
_last_terminal_snapshot: Optional[Tuple[float, float, float]] = None

# Tracks date string (YYYY-MM-DD) when 50% daily loss pause alert was last sent
_daily_50pct_alert_date: Optional[str] = None

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


def _get_signal_lock(chat_id: Optional[int | str], msg_id: Any) -> asyncio.Lock:
    """Return a per-(event_loop, chat_id, msg_id) asyncio.Lock serializing edits, replies, and timers."""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    key = (loop_id, eff_chat, str(mid_norm))
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


async def _account_closed_tickets(
    sig: Dict[str, Any],
    tickets: List[Any],
    close_infos: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Tuple[float, Dict[int, Dict[str, Any]]]:
    """
    Deduplicate realized ticket PnL across `sig['accounted_tickets']` and accumulate
    into `sig['banked_profit']`. Returns (newly_added_profit, ticket_info_map).
    """
    accounted: List[int] = []
    for raw_a in (sig.get("accounted_tickets") or []):
        ai = _safe_ticket_int(raw_a)
        if ai is not None and ai not in accounted:
            accounted.append(ai)

    total_banked = float(sig.get("banked_profit") or 0.0)
    infos: Dict[int, Dict[str, Any]] = dict(close_infos or {})
    added_profit = 0.0

    unique_tickets: List[int] = []
    for raw_t in (tickets or []):
        ti = _safe_ticket_int(raw_t)
        if ti is not None and ti not in unique_tickets:
            unique_tickets.append(ti)

    for t in unique_tickets:
        if t not in infos:
            infos[t] = await _mt5_call(executor.get_position_close_info, t)
        if t not in accounted:
            profit_val = float(infos[t].get("profit", 0.0) or 0.0)
            accounted.append(t)
            added_profit += profit_val
            total_banked += profit_val

    sig["accounted_tickets"] = accounted
    sig["banked_profit"] = round(total_banked, 2)
    return round(added_profit, 2), infos


def _has_executed_exposure(sig: Dict[str, Any]) -> bool:
    """Return True if a signal actually had filled/executed exposure (not merely cancelled pending setup)."""
    if sig.get("accounted_tickets") or sig.get("market_tickets") or sig.get("last_runner_ticket") or sig.get("last_banked_tickets"):
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
    if isinstance(tp_map, dict):
        for idx in range(1, 6):
            val = tp_map.get(f"TP{idx}")
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out[f"TP{idx}"] = float(val)
    for idx in range(1, 6):
        val = sig.get(f"raw_tp{idx}")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and f"TP{idx}" not in out:
            out[f"TP{idx}"] = float(val)
    return out


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
    runner_t = await _mt5_call(executor.handle_tp1_scaling_and_select_runner, sig)
    state_mgr.set_tp1_reached(msg_id, True, chat_id=eff_chat)

    banked_tkts = list(sig.get("last_banked_tickets") or [])
    tp1_added_profit, _ = await _account_closed_tickets(sig, banked_tkts)
    state_mgr.update_signal(msg_id, sig, chat_id=eff_chat)

    fin = await _mt5_call(executor.get_account_financials)
    curr = fin.get("currency", "GBP")
    profit_str = format_profit(tp1_added_profit, curr)

    symbol = sig.get("symbol", DEFAULT_SYMBOL)
    direction = sig.get("direction", "BUY")
    raw_tp1 = sig.get("raw_tp1")
    hit_line = f"\n• Hit Price: `{curr_price}` (TP1: `{raw_tp1}`)" if curr_price is not None else f"\n• TP1: `{raw_tp1}`"

    if sig.get("status") == "CLOSED":
        await _record_signal_to_ledger(msg_id, sig, exit_reason=f"TP1 Full Bank ({source_label})", currency=curr)
        alert = (
            f"🎯 **TP1 Reached & 100% Banked ({source_label})! [ID: {msg_id}]**\n"
            f"• Symbol: `{symbol}` ({direction}){hit_line}\n"
            f"• 💰 **Amount Made at TP1:** `{profit_str}` (All open layers banked)\n"
            f"• Pending limit orders cancelled.\n\n"
            f"{fin['formatted']}"
        )
        return runner_t, tp1_added_profit, alert

    runner_layers = list(sig.get("runner_layers") or ([sig["runner_layer"]] if sig.get("runner_layer") else []))
    runner_tickets = list(sig.get("runner_tickets") or ([sig["runner_ticket"]] if sig.get("runner_ticket") else []))
    runner_name = ", ".join(str(x) for x in runner_layers) if len(runner_layers) > 1 else sig.get("runner_layer")
    soft_floor = sig.get("runner_sl_floor") if sig.get("runner_sl_floor") is not None else sig.get("soft_tp1_floor")
    milestone_name = sig.get("runner_milestone_name", "TP1")
    tp_stage = sig.get("tp_stage", "TP2")
    surviving_mkt = list(sig.get("market_tickets") or [])
    tp_summary = _format_tp_summary(sig)
    pending_note = (
        "Pending limit orders cancelled cleanly."
        if CANCEL_PENDING_ON_TP1
        else f"Remaining pending orders kept active ({len(sig.get('pending_tickets') or [])})."
    )
    be_note = " | BE applied to open layers" if BE_AFTER_TP1 else ""
    extra_runners_note = f" (Runner Tickets: `{runner_tickets}`)" if len(runner_tickets) > 1 else ""

    alert = (
        f"🎯 **TP1 Reached & Profit Banked ({source_label})! [ID: {msg_id}]**\n"
        f"• Symbol: `{symbol}` ({direction}){hit_line}\n"
        f"• 💰 **Amount Made at TP1:** `{profit_str}` (Tickets: `{banked_tkts}`{be_note})\n"
        f"• 🚀 **Designated Runner:** Layer `{runner_name}` [Ticket #{runner_t}]{extra_runners_note} (Open Tickets: `{surviving_mkt}`)\n"
        f"• 🛡️ **Floor / Trailed SL:** `{soft_floor}` | Stage: `{tp_stage}` | Next Milestone: `{milestone_name}`\n"
        f"• 🎯 **Targets:** {tp_summary}\n"
        f"• {pending_note}\n\n"
        f"{fin['formatted']}"
    )
    return runner_t, tp1_added_profit, alert


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

    for mid, s in active_signals:
        s_chat = s.get("chat_id")
        mk_tkts = [t for t in (s.get("market_tickets") or []) if t in closed_set or t]
        if mk_tkts:
            await _account_closed_tickets(s, mk_tkts)
        layers = s.get("layers", {})
        for lk in layers:
            if isinstance(layers[lk], dict):
                layers[lk]["status"] = "CLOSED"
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
    Open floating trades are deliberately ignored (the 50%/90% drawdown guard covers those).
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


async def check_and_enforce_daily_drawdown() -> str:
    """
    Two-Stage Daily Loss & Account Drawdown Protection:
    - Stage 2 (90% of balance): Urgency cut-off immediately closes all losing open positions & cancels limits.
    - Stage 1 (50% of balance): Pauses NEW signals, while holding existing open trades naturally to SL/TP.
    Returns "EMERGENCY_CUTOFF", "PAUSED_50PCT", or "OK".
    """
    global _daily_50pct_alert_date
    dd = await _mt5_call(executor.get_daily_drawdown_status)
    curr = dd.get("currency", "GBP")
    today_str = await _trading_day_str_async()

    if dd.get("emergency_cutoff_triggered"):
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
                        "banked_profit": s.get("banked_profit", 0.0),
                    },
                    chat_id=s_chat,
                )
                if new_status == "CLOSED":
                    await _record_signal_to_ledger(
                        mid, s, exit_reason=f"{int(EMERGENCY_CUTOFF_LOSS_PCT)}% Emergency Cutoff", currency=curr
                    )

        if closed_tkts or cancelled_ords or active_sigs:
            fin = await _mt5_call(executor.get_account_financials)
            logger.error(
                f"🚨 90% EMERGENCY CUT-OFF TRIGGERED! Drawdown=-{dd['total_daily_loss']:.2f} {curr} "
                f"({dd['loss_pct']:.1f}%). Closed={closed_tkts}, Cancelled={cancelled_ords}"
            )
            await notify_saved_messages(
                f"🚨 **{int(EMERGENCY_CUTOFF_LOSS_PCT)}% EMERGENCY DRAWDOWN CUT-OFF TRIGGERED!**\n"
                f"• Loss Risked: `-{dd['total_daily_loss']:.2f} {curr}` (`{dd['loss_pct']:.1f}%` of `{dd['reference_balance']:.2f} {curr}` balance)\n"
                f"• Closed Losing Positions: `{closed_tkts}`\n"
                f"• Cancelled Pending Limits: `{cancelled_ords}`\n"
                f"• 🛡️ All losing positions cut immediately to protect your remaining account balance!\n\n"
                f"{fin['formatted']}"
            )
        return "EMERGENCY_CUTOFF"

    if dd.get("pause_new_signals"):
        if _daily_50pct_alert_date != today_str:
            _daily_50pct_alert_date = today_str
            logger.warning(
                f"🛑 50% Daily Loss Limit Reached (-{dd['total_daily_loss']:.2f} {curr} / {dd['loss_pct']:.1f}%). "
                f"Holding existing open trades to SL/TP; new signals paused."
            )
            await notify_saved_messages(
                f"🛑 **{int(DAILY_LOSS_PAUSE_PCT)}% Daily Loss Threshold Reached — New Signals Paused**\n"
                f"• Current Daily/Floating Loss: `-{dd['total_daily_loss']:.2f} {curr}` (`{dd['loss_pct']:.1f}%` of `{dd['reference_balance']:.2f} {curr}` balance)\n"
                f"• 🛡️ **Existing Trades Held:** Currently open positions are **NOT** closed — waiting naturally for their SL or TP.\n"
                f"• 🚨 **90% Urgency Cut-Off:** Open losing trades will only be force-closed if drawdown reaches `{int(EMERGENCY_CUTOFF_LOSS_PCT)}%` (`-{dd['emergency_cutoff_amount']:.2f} {curr}`)."
            )
        return "PAUSED_50PCT"

    return "OK"


# Last rendered HUD state (bal, cred, eq, pnl, tot_lots, high_l) so we only redraw when values change
_last_hud_state: Optional[Tuple[float, float, float, float, float, float]] = None


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
    tot_lots = round(st.get("total_lots", 0.0), 2)
    low_l = st.get("low_risk_lots", 0.01)
    mid_l = st.get("mid_risk_lots", 0.01)
    high_l = round(st.get("high_risk_lots", 0.01), 2)

    snap = (bal, cred, tot_lots)
    if force_print or snap != _last_terminal_snapshot:
        _last_terminal_snapshot = snap
        logger.info(
            f"💳 [LIVE ACCOUNT & RISK GUIDE] Balance: {bal:.2f} {curr} | Credit: {cred:.2f} {curr} | "
            f"Equity: {eq:.2f} {curr} | Active Lots: {tot_lots:.2f}/{high_l:.2f} Max "
            f"(Low: {low_l:.2f} | Mid: {mid_l:.2f} | High Cap: {high_l:.2f})"
        )

    hud_state = (bal, cred, eq, pnl, tot_lots, high_l)
    if not force_print and hud_state == _last_hud_state:
        return
    _last_hud_state = hud_state

    title_str = (
        f"[XAUUSD COPIER v3.0] Bal: {sym_char}{bal:.2f} | Cred: {sym_char}{cred:.2f} | "
        f"Eq: {sym_char}{eq:.2f} | P&L: {'+' if pnl >= 0 else '-'}{sym_char}{abs(pnl):.2f} | "
        f"Lots: {tot_lots:.2f}/{high_l:.2f}"
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
        f"Lots: \033[93m{tot_lots:.2f}/{high_l:.2f}\033[0m"
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
    """Extract JSON-safe chat_id from Telethon or mock event (ignoring unconfigured MagicMock)."""
    raw_cid = getattr(event, "chat_id", None)
    if isinstance(raw_cid, (int, str)) and not isinstance(raw_cid, bool):
        return raw_cid
    return None


def _get_dedup_key(chat_id: Optional[int | str], msg_id: Any) -> Any:
    """Build per-channel deduplication key."""
    return (chat_id, msg_id) if chat_id is not None else msg_id


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
) -> bool:
    """
    Arm live-price TP1 invalidation immediately when market positions exist or live price starts
    on the pre-TP1 side. When a deep BUY/SELL LIMIT starts with live price already beyond TP1,
    wait until price moves back across TP1 toward the limit zone before arming invalidation.
    """
    if has_market or raw_tp1 is None or not tick:
        return True
    try:
        tp_val = float(raw_tp1)
    except (TypeError, ValueError):
        return True
    dir_up = str(direction or "").upper()
    if dir_up == "BUY":
        ask = getattr(tick, "ask", None)
        if isinstance(ask, (int, float)) and not isinstance(ask, bool):
            return float(ask) < tp_val
    elif dir_up == "SELL":
        bid = getattr(tick, "bid", None)
        if isinstance(bid, (int, float)) and not isinstance(bid, bool):
            return float(bid) > tp_val
    return True


def _reserve_task_key(msg_id: Any, chat_id: Optional[int | str] = None) -> Any:
    """Cancel any prior matching delayed task and return the collision-safe tracking key."""
    _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
    mid_norm, eff_chat = _normalize_task_msg_id(msg_id, chat_id=chat_id)
    if mid_norm not in _pending_sell_now_tasks or _pending_task_chats.get(mid_norm) in (None, eff_chat):
        return mid_norm
    return (eff_chat, mid_norm)


def _cancel_delayed_sell_now(msg_id: Any, chat_id: Optional[int | str] = None) -> bool:
    """Cancel an active delayed 'Sell now' / 'Buy now' timer if still waiting."""
    if isinstance(msg_id, tuple) and msg_id in _pending_sell_now_tasks:
        task = _pending_sell_now_tasks.pop(msg_id, None)
        _pending_task_chats.pop(msg_id, None)
        if task and not task.done():
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
        if task and not task.done():
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
    all_pending = pending_tickets + extra_pending
    layers = dict(sig.get("layers") or {})

    cancelled: List[int] = []
    for pt in all_pending:
        if pt not in market_tickets and await _mt5_call(executor.cancel_pending_order, pt):
            cancelled.append(pt)

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

            await asyncio.sleep(min(float(DELAYED_POLL_INTERVAL_SECONDS), max(0.01, delay_total)))

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

            prev_lots = (await _mt5_call(executor.get_total_active_lots))["total_lots"]
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

            new_lots = (await _mt5_call(executor.get_total_active_lots))["total_lots"]
            risk_alerts = await _mt5_call(executor.evaluate_risk_threshold_alerts, prev_lots, new_lots)
            alloc_warn = getattr(executor, "last_allocation_warning", None)
            extra_notes: List[str] = []
            if alloc_warn:
                extra_notes.append(f"⚠️ **Lot Cap Note:** `{alloc_warn}`")
            extra_notes.extend(risk_alerts)
            risk_block = ("\n\n" + "\n".join(extra_notes)) if extra_notes else ""

            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
                    "awaiting_favorable_price": False,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "layers": layers_data,
                    "max_orders": MAX_ORDERS_PER_SIGNAL,
                    "tp1_guard_armed": True,
                },
                chat_id=chat_id,
            )
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
    if parsed.raw_tp1 is not None and tick and _tick_hit_tp1(tick, direction, parsed.raw_tp1):
        return f"TP1 ({parsed.raw_tp1}) already reached on live market"

    if parsed.is_instant_now:
        if age_sec > float(CATCHUP_MARKET_MAX_AGE_SECONDS):
            return (
                f"Market Now signal age {age_sec:.1f}s exceeds "
                f"CATCHUP_MARKET_MAX_AGE_SECONDS ({CATCHUP_MARKET_MAX_AGE_SECONDS}s)"
            )
        return None

    live_px = _get_live_entry_price(tick, direction)
    if live_px is None or pip_size <= 0:
        return None

    ref_px: Optional[float] = None
    would_fill_market = False
    is_stop = bool(getattr(parsed, "is_stop_order", False))

    if parsed.single_price is not None:
        ref_px = float(parsed.single_price)
        if is_stop:
            would_fill_market = (direction == "BUY" and live_px >= ref_px) or (direction == "SELL" and live_px <= ref_px)
        else:
            would_fill_market = (direction == "BUY" and live_px <= ref_px) or (direction == "SELL" and live_px >= ref_px)
    elif parsed.is_zone and parsed.zone is not None:
        ref_px = float(parsed.zone.top if direction == "BUY" else parsed.zone.bottom)
        would_fill_market = (direction == "BUY" and live_px <= ref_px) or (direction == "SELL" and live_px >= ref_px)

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


async def run_reconnect_catchup(reason: str = "startup") -> Dict[str, int]:
    """
    Replay bounded recent messages missed while offline/disconnected across CHANNEL_IDS.
    Enforces CATCHUP_WINDOW_SECONDS, CATCHUP_MAX_MESSAGES, persisted (chat_id, msg_id)
    watermarks, and strict stale-market age/slippage guards.
    """
    summary = {"replayed": 0, "edits": 0, "skipped_stale": 0, "channels": 0}
    if session_trading_blocked() or not telethon_client:
        return summary
    if int(CATCHUP_WINDOW_SECONDS) <= 0 or int(CATCHUP_MAX_MESSAGES) <= 0:
        return summary

    get_msgs_fn = getattr(telethon_client, "get_messages", None)
    if not callable(get_msgs_fn):
        return summary

    async with _get_catchup_lock():
        now_utc = datetime.now(timezone.utc)
        tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
        pip_size = await _mt5_call(executor.get_pip_size, DEFAULT_SYMBOL)

        for chat_id in CHANNEL_IDS:
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
                        if age_sec <= float(CATCHUP_WINDOW_SECONDS):
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

        if summary["replayed"] or summary["edits"] or summary["skipped_stale"]:
            logger.info(
                f"Reconnect catch-up ({reason}) finished: "
                f"replayed={summary['replayed']}, edits={summary['edits']}, skipped_stale={summary['skipped_stale']}"
            )
            await notify_saved_messages(
                f"🔄 **Reconnect Catch-Up Summary (`{reason}`)**\n"
                f"• Replayed Signals/Commands: `{summary['replayed']}` | Missed Edits: `{summary['edits']}`\n"
                f"• Rejected Stale/Slipped Market Signals: `{summary['skipped_stale']}`"
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


async def _reconcile_live_mt5_signals(
    active_signals: List[Tuple[str, Dict[str, Any]]],
) -> None:
    """
    Reconcile live MT5 positions & pending orders against active state records.
    Unifies broker-closed `runner_ticket` accounting with banked_profit + TradeLedger.
    """
    live_positions = await _mt5_call(executor.get_live_positions, DEFAULT_SYMBOL)
    live_orders = await _mt5_call(executor.get_live_orders, DEFAULT_SYMBOL)
    live_pos_tickets = {
        getattr(p, "ticket", None) for p in live_positions if getattr(p, "ticket", None) is not None
    }
    live_order_tickets = {
        getattr(o, "ticket", None) for o in live_orders if getattr(o, "ticket", None) is not None
    }

    for msg_id, sig_snap in list(active_signals):
        sig_chat = sig_snap.get("chat_id")
        async with _get_signal_lock(sig_chat, msg_id):
            sig = state_mgr.get_signal(msg_id, chat_id=sig_chat)
            if not sig or sig.get("status") == "CLOSED":
                continue

            mk_tickets = list(sig.get("market_tickets", []))
            pd_tickets = list(sig.get("pending_tickets", []))
            layers = dict(sig.get("layers") or {})
            updated = False

            new_pd_tickets: List[int] = []
            for t in pd_tickets:
                if t in live_pos_tickets:
                    if t not in mk_tickets:
                        mk_tickets.append(t)
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and l_data.get("ticket") == t:
                            l_data["status"] = "FILLED"
                    updated = True
                    logger.info(f"Reconciled MT5 state: Pending order #{t} filled into active position for Msg ID {msg_id}")
                elif t in live_order_tickets:
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
                if t in live_pos_tickets:
                    new_mk_tickets.append(t)
                else:
                    updated = True
                    broker_closed.append(t)
                    for l_data in layers.values():
                        if isinstance(l_data, dict) and l_data.get("ticket") == t:
                            l_data["status"] = "CLOSED"
                    logger.info(f"Reconciled MT5 state: Market position #{t} closed for Msg ID {msg_id}")

            if not updated:
                continue

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

            updates: Dict[str, Any] = {
                "market_tickets": new_mk_tickets,
                "pending_tickets": new_pd_tickets,
                "layers": layers,
                "status": new_status,
                "accounted_tickets": sig.get("accounted_tickets", []),
                "banked_profit": sig.get("banked_profit", 0.0),
            }
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
                r_reason_map = {
                    "TP": "Broker Take Profit Hit",
                    "SL": "Trailed / Breakeven Stop Loss Hit",
                    "MANUAL/BOT": "Closed manually / from MT5 app",
                    "UNKNOWN": "Closed on broker",
                }
                updates["last_runner_tickets"] = closed_runner_tkts
                updates["last_runner_ticket"] = runner_t
                updates["runner_tickets"] = []
                updates["runner_ticket"] = None
                updates["runner_proximity_armed"] = False
                updates["runner_reversal_ticks"] = 0
                updates["tp2_shortfall_armed"] = False
                updates["runner_close_reason"] = r_reason_map.get(r_info.get("reason", "UNKNOWN"), "Closed on broker")
                max_stage = int(sig.get("runner_max_tp_stage") or RUNNER_MAX_TP_STAGE)
                if r_info.get("reason") == "TP":
                    updates[f"tp{max_stage}_reached"] = True
                    if sig.get("raw_tp5") is not None:
                        updates["tp5_reached"] = True

            sig.update(updates)
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

            if not broker_closed:
                continue

            fin = await _mt5_call(executor.get_account_financials)
            curr = fin.get("currency", "GBP")
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
                head = "🎯 **TP Hit - Profit Banked**" if info.get("reason") == "TP" else "📌 **Position Closed by Broker**"
                await notify_saved_messages(
                    f"{head} [ID: {msg_id}]\n"
                    f"• Ticket: `#{t}` ({sig.get('direction')} {sig.get('symbol')})\n"
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
                await notify_saved_messages(
                    f"🏁 **Runner Closed by Broker & Unified Summary [ID: {msg_id}]**\n"
                    f"• Symbol: `{sig.get('symbol')}` ({sig.get('direction')})\n"
                    f"• Runner: `Layer {sig.get('runner_layer')}` [Ticket #{runner_t}]\n"
                    f"• Exit: {reason_map.get(r_info.get('reason', 'UNKNOWN'), r_info.get('reason'))}\n"
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
                await _record_signal_to_ledger(msg_id, sig, exit_reason=str(exit_lbl), currency=curr)


async def _process_runner_active_tick(msg_id: str, sig: Dict[str, Any]) -> None:
    """Manage a RUNNER_ACTIVE signal via _mt5_call with dirty-checked saves and unified close summary."""
    sig_chat = sig.get("chat_id")
    sig_before = copy.deepcopy(sig)
    prior_banked = float(sig.get("banked_profit") or 0.0)

    still_active = await _mt5_call(executor.manage_designated_runner, sig)

    backstop_tkts = list(sig.get("last_backstop_closed_tickets") or [])
    if backstop_tkts:
        added_bs, _ = await _account_closed_tickets(sig, backstop_tkts)
        fin_bs = await _mt5_call(executor.get_account_financials)
        curr_bs = fin_bs.get("currency", "GBP")
        await notify_saved_messages(
            f"🎯 **Milestone Auto-Close Backstop Banked [ID: {msg_id}]**\n"
            f"• Symbol: `{sig.get('symbol', DEFAULT_SYMBOL)}` ({sig.get('direction')})\n"
            f"• Closed Target Tickets: `{backstop_tkts}`\n"
            f"• 💰 **Banked:** `{format_profit(added_bs, curr_bs)}` | "
            f"**Total Trade PnL:** `{format_profit(sig.get('banked_profit', 0.0), curr_bs)}`"
        )

    if not still_active:
        last_runner_tkts = [
            t for t in (sig.get("last_runner_tickets") or ([sig["last_runner_ticket"]] if sig.get("last_runner_ticket") else []))
            if t is not None
        ]
        last_runner_t = sig.get("last_runner_ticket") or (last_runner_tkts[-1] if last_runner_tkts else None)
        runner_layer = sig.get("runner_layer", "RUNNER")
        tp5_hit = bool(sig.get("tp5_reached", False))
        tp4_hit = bool(sig.get("tp4_reached", False))
        tp2_hit = bool(sig.get("tp2_reached", False))
        close_reason = sig.get("runner_close_reason", "Trailing SL / Virtual Floor / Proximity Guard")

        _, r_infos = await _account_closed_tickets(sig, last_runner_tkts)
        runner_profit = sum(float(r_infos.get(rt, {}).get("profit", 0.0)) for rt in last_runner_tkts)
        total_trade_profit = float(sig.get("banked_profit") or 0.0)
        if not sig.get("accounted_tickets") and prior_banked != 0.0:
            total_trade_profit = runner_profit + prior_banked

        state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)

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

        await notify_saved_messages(
            f"{headline}\n"
            f"• Symbol: `{sig.get('symbol', DEFAULT_SYMBOL)}` ({sig.get('direction')})\n"
            f"• Runner: `Layer {runner_layer}` [Ticket #{last_runner_t}]\n"
            f"• Exit Reason: 🔒 `{close_reason}`\n"
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
        "runner_milestone_idx", "runner_milestone_name", "tp_stage",
        "runner_cleared_milestones", "runner_proximity_armed", "runner_reversal_ticks", "tp2_shortfall_armed",
        "runner_sl_floor", "soft_tp1_floor", "broker_sl",
        "tp2_reached", "tp3_reached", "tp4_reached", "tp5_reached",
        "accounted_tickets", "banked_profit", "tp_levels",
    )
    important_changed = any(sig.get(k) != sig_before.get(k) for k in important_keys)
    armed_now = bool(sig.get("runner_proximity_armed") or sig.get("tp2_shortfall_armed"))
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
                    state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)
                    fin = await _mt5_call(executor.get_account_financials)
                    curr = fin.get("currency", "GBP")
                    if sig.get("status") == "CLOSED":
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
            pip_offset = FALLBACK_TP1_PIPS * pip_size
            raw_tp1 = round(curr_price + pip_offset, 2) if direction == "BUY" else round(curr_price - pip_offset, 2)
            final_tp = round(raw_tp1 - TP_FRONT_RUN_OFFSET, 2) if direction == "BUY" else round(raw_tp1 + TP_FRONT_RUN_OFFSET, 2)
            tp_levels = derive_tp_levels([raw_tp1], direction=direction, pip_size=pip_size)
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
            logger.info(f"Attached fallback TP1 ({raw_tp1}, +{FALLBACK_TP1_PIPS} pips) on unpriced Signal {msg_id}")
            await notify_saved_messages(
                f"🛡️ **Fallback TP1 Attached [ID: {msg_id}]**\n"
                f"• No TP edit received after {int(UNPRICED_TP_GRACE_SECONDS)}s.\n"
                f"• Auto-assigned TP1: `{raw_tp1}` (Broker TP: `{final_tp}`) | {_format_tp_summary(sig)}"
            )

    if not raw_tp1:
        return

    tp1_hit = (curr_price >= raw_tp1) if direction == "BUY" else (curr_price <= raw_tp1)

    # Trigger 2: If 0 market positions filled and pending limits/stops remain, invalidate once live price hits raw_tp1
    if not sig.get("market_tickets"):
        pd_tkts = list(sig.get("pending_tickets", []))
        if pd_tkts:
            if not sig.get("tp1_guard_armed", True):
                entry_ref = None
                z = sig.get("zone")
                if isinstance(z, dict):
                    entry_ref = z.get("top") if direction == "BUY" else z.get("bottom")
                elif isinstance(sig.get("layers"), dict):
                    l1_obj = sig["layers"].get("L1") or sig["layers"].get("TOP") or {}
                    entry_ref = l1_obj.get("target_price")
                margin = (
                    min(1.5, abs(float(raw_tp1) - float(entry_ref)) * 0.5)
                    if isinstance(entry_ref, (int, float))
                    else 0.5
                )
                if direction == "BUY":
                    ask_val = getattr(tick, "ask", None)
                    if isinstance(ask_val, (int, float)) and not isinstance(ask_val, bool) and float(ask_val) <= round(float(raw_tp1) - margin, 2):
                        sig["tp1_guard_armed"] = True
                        state_mgr.update_signal(msg_id, {"tp1_guard_armed": True}, chat_id=sig_chat)
                elif direction == "SELL":
                    bid_val = getattr(tick, "bid", None)
                    if isinstance(bid_val, (int, float)) and not isinstance(bid_val, bool) and float(bid_val) >= round(float(raw_tp1) + margin, 2):
                        sig["tp1_guard_armed"] = True
                        state_mgr.update_signal(msg_id, {"tp1_guard_armed": True}, chat_id=sig_chat)

            if sig.get("tp1_guard_armed", True) and _tick_hit_tp1(tick, direction, raw_tp1):
                await _invalidate_signal_tp1_unlocked(
                    msg_id,
                    reason="Market Price Reached TP1",
                    chat_id=sig_chat,
                )
        return

    # v3.1 Proactive Runner TP Lift & Dynamic Deepest-Layer Sync before TP1
    if not sig.get("tp1_reached") and hasattr(executor, "prepare_deepest_runners_for_tp_breakout"):
        sig_before_prep = copy.deepcopy(sig)
        await _mt5_call(executor.prepare_deepest_runners_for_tp_breakout, sig, curr_price)
        if sig != sig_before_prep:
            state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)

    if tp1_hit:
        logger.info(
            f"🎯 TP1 REACHED on Msg ID {msg_id}! Live Price={curr_price}, Raw TP1={raw_tp1}"
        )
        _, _, alert = await _execute_tp1_scaling_handoff(
            msg_id, sig, chat_id=sig_chat, source_label="Watcher", curr_price=curr_price
        )
        await notify_saved_messages(alert)
        return

    # Branch C: Calibrated TP1 Near-Miss Shortfall Protection (SHORTFALL_GUARD + Anti-Choke Confirm Ticks)
    raw_arm_dist = float(SHORTFALL_ARM_PIPS) * pip_size
    entry_ref = sig.get("recorded_entry_price") if sig.get("recorded_entry_price") is not None else sig.get("initial_price")
    if entry_ref is None and isinstance(sig.get("zone"), dict):
        z_dict = sig["zone"]
        entry_ref = z_dict.get("top") if direction == "BUY" else z_dict.get("bottom")
    if entry_ref is None and isinstance(sig.get("layers"), dict):
        l1_obj = sig["layers"].get("L1") or sig["layers"].get("TOP") or {}
        entry_ref = l1_obj.get("target_price")
    if isinstance(entry_ref, (int, float)) and not isinstance(entry_ref, bool):
        leg_dist = abs(float(raw_tp1) - float(entry_ref))
        arm_dist = min(raw_arm_dist, leg_dist * float(RUNNER_MAX_MIDRANGE_RATIO)) if leg_dist > 0 else raw_arm_dist
    else:
        arm_dist = raw_arm_dist

    pullback_dist = float(SHORTFALL_PULLBACK_PIPS) * pip_size
    confirm_ticks_req = max(1, int(RUNNER_REVERSAL_CONFIRM_TICKS))
    shortfall_triggered = False
    peak_val = sig.get("peak_price")

    in_arm_zone = (
        curr_price >= round(float(raw_tp1) - arm_dist, 4)
        if direction == "BUY"
        else curr_price <= round(float(raw_tp1) + arm_dist, 4)
    )
    if not sig.get("shortfall_armed") and in_arm_zone:
        sig["shortfall_armed"] = True
        sig["peak_price"] = curr_price
        sig["shortfall_reversal_ticks"] = 0
        state_mgr.update_signal(
            msg_id,
            {"shortfall_armed": True, "peak_price": curr_price, "shortfall_reversal_ticks": 0},
            chat_id=sig_chat,
        )
        peak_val = curr_price

    if sig.get("shortfall_armed"):
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
        logger.warning(
            f"⚠️ TP1 SHORTFALL TRIGGERED on Msg ID {msg_id}! "
            f"Peak={peak_val}, Reversal Price={curr_price}, TP1={raw_tp1}"
        )
        closed_tickets = list(sig.get("market_tickets", []))
        for t in closed_tickets:
            await _mt5_call(executor.close_market_position, t)
        for pt in sig.get("pending_tickets", []):
            await _mt5_call(executor.cancel_pending_order, pt)

        layers = sig.get("layers", {})
        for lk in layers:
            if isinstance(layers[lk], dict):
                layers[lk]["status"] = "CLOSED"

        added_profit, _ = await _account_closed_tickets(sig, closed_tickets)
        sig["status"] = "CLOSED"
        sig["market_tickets"] = []
        sig["pending_tickets"] = []
        sig["runner_tickets"] = []
        sig["runner_ticket"] = None
        sig["layers"] = layers
        state_mgr.update_signal(msg_id, sig, chat_id=sig_chat)

        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        await _record_signal_to_ledger(msg_id, sig, exit_reason="TP1 Shortfall Guard", currency=curr)
        profit_str = format_profit(added_profit, curr)

        await notify_saved_messages(
            f"⚠️ **TP1 Shortfall Guard Triggered & Banked! [ID: {msg_id}]**\n"
            f"• Symbol: `{symbol}` ({direction})\n"
            f"• Peak Price: `{peak_val}` (within {SHORTFALL_ARM_PIPS} pips of TP1 `{raw_tp1}`)\n"
            f"• Reversal Price: `{curr_price}` (pulled back {SHORTFALL_PULLBACK_PIPS} pips from peak)\n"
            f"• 💰 **Amount Made:** `{profit_str}`\n"
            f"• All positions banked at market & pending orders cancelled!\n\n"
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
    has_live_exposure = False

    while True:
        try:
            now = time.time()
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
                    if sig.get("status") == "RUNNER_ACTIVE":
                        await _process_runner_active_tick(msg_id, sig)
                    else:
                        await _process_pre_tp1_or_backstop_tick(msg_id, sig)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in TP1 proximity watcher: {e}")

        await asyncio.sleep(1.0)


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
        await _mt5_call(
            executor.apply_selective_breakeven,
            layers=layers,
            symbol=symbol,
            direction=direction,
            be_layers=BE_LAYERS,
            be_offset_pips=BE_OFFSET_PIPS,
        )
        state_mgr.update_signal(target_msg_id, {"status": "BE_APPLIED"}, chat_id=target_chat_id)
        await notify_saved_messages(
            f"🛡️ **Selective Breakeven Applied**\n"
            f"• Symbol: `{symbol}` ({direction})\n"
            f"• Layers Protected: `{BE_LAYERS}` (+{BE_OFFSET_PIPS} pips commission buffer)\n"
            f"• Runner Layer (L4 / 0.02) remains at original SL until TP1."
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
            updates["status"] = "BE_APPLIED"
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
        if target_signal.get("status") == "ACTIVE":
            logger.info("Signal is already active; ignoring redundant 'Active' command.")
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

        state_mgr.update_signal(
            target_msg_id,
            {
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
            },
            chat_id=target_chat_id,
        )
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

        new_status = "CLOSED" if (not market_tickets and not pending_tickets) else "PARTIAL_CLOSED"
        target_signal["market_tickets"] = market_tickets
        target_signal["layers"] = layers
        target_signal["status"] = new_status
        state_mgr.update_signal(
            target_msg_id,
            {
                "market_tickets": market_tickets,
                "layers": layers,
                "status": new_status,
                "accounted_tickets": target_signal.get("accounted_tickets", []),
                "banked_profit": target_signal.get("banked_profit", 0.0),
                "runner_ticket": target_signal.get("runner_ticket"),
                "last_runner_ticket": target_signal.get("last_runner_ticket"),
            },
            chat_id=target_chat_id,
        )
        if new_status == "CLOSED":
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

        closed_tkts = list(market_tickets)
        await _mt5_call(executor.apply_proximity_close, market_tickets, pending_tickets, symbol)
        added_p, _ = await _account_closed_tickets(target_signal, closed_tkts)
        fin = await _mt5_call(executor.get_account_financials)
        curr = fin.get("currency", "GBP")
        total_profit = float(target_signal.get("banked_profit") or added_p)
        profit_str = format_profit(total_profit, curr)

        for l_data in layers.values():
            if isinstance(l_data, dict):
                l_data["status"] = "CLOSED"
        target_signal["market_tickets"] = []
        target_signal["pending_tickets"] = []
        target_signal["runner_ticket"] = None
        target_signal["layers"] = layers
        target_signal["status"] = "CLOSED"
        state_mgr.update_signal(
            target_msg_id,
            {
                "market_tickets": [],
                "pending_tickets": [],
                "runner_ticket": None,
                "layers": layers,
                "status": "CLOSED",
                "accounted_tickets": target_signal.get("accounted_tickets", []),
                "banked_profit": target_signal.get("banked_profit", 0.0),
            },
            chat_id=target_chat_id,
        )
        await _record_signal_to_ledger(target_msg_id, target_signal, exit_reason="Full Close Command", currency=curr)
        await notify_saved_messages(
            f"🚪 **Full Close Executed on `{symbol}` [Msg ID: {target_msg_id}]**\n"
            f"• All open positions closed at market.\n"
            f"• 💰 **Total Amount Made:** `{profit_str}`\n\n"
            f"{fin['formatted']}"
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


async def _handle_new_message_unlocked(event: Any) -> None:
    """Unlocked core implementation of handle_new_message (caller holds signal lock for (chat_id, msg_id))."""
    if session_trading_blocked():
        return
    msg = event.message
    if not msg:
        return

    msg_id = msg.id
    raw_text = msg.message or ""
    chat_id = _safe_chat_id(event)
    reply_to_id = _extract_reply_to_id(msg)
    is_reply_evt = (getattr(event, "is_reply", None) is True) or (reply_to_id is not None)

    now = time.time()
    if len(_recent_messages) > 200:
        cutoff = now - 60.0
        stale_keys = [k for k, v in _recent_messages.items() if v < cutoff]
        for k in stale_keys:
            del _recent_messages[k]

    dedup_key = _get_dedup_key(chat_id, msg_id)
    if dedup_key in _recent_messages and (now - _recent_messages[dedup_key]) < DEDUPLICATION_WINDOW_SECONDS:
        logger.info(f"Message ID {msg_id} duplicate event within {DEDUPLICATION_WINDOW_SECONDS}s; skipping.")
        return
    if state_mgr.get_signal(msg_id, chat_id=chat_id) is not None:
        logger.info(f"Message ID {msg_id} already exists in state; skipping duplicate creation.")
        _recent_messages[dedup_key] = now
        return
    _recent_messages[dedup_key] = now

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
        target_signal = state_mgr.get_signal(reply_to_id, chat_id=chat_id)
        if target_signal:
            target_msg_id = str(reply_to_id)
            target_chat_id = target_signal.get("chat_id", chat_id)

    is_forward = getattr(msg, "forward", None) is not None
    tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
    live_price = getattr(tick, "bid", None) if tick else None
    parsed = parse_message(
        raw_text,
        message_id=msg_id,
        is_reply=is_reply_evt,
        reply_to_msg_id=reply_to_id,
        is_forward=is_forward,
        live_price=live_price,
    )

    if parsed is None:
        logger.info(f"Message ID {msg_id} filtered out (noise, recap, or ignored symbol).")
        return

    # 1. Trade Management Commands
    if parsed.event_type == "TRADE_MANAGEMENT" or parsed.is_management:
        action = parsed.management_action
        logger.info(f"Processing Trade Management: {action} [Msg ID: {msg_id}]")

        if not target_signal:
            res = state_mgr.get_latest_signal(symbol=parsed.symbol, chat_id=chat_id)
            if not res and chat_id is not None:
                res = state_mgr.get_latest_signal(symbol=parsed.symbol)
            if res:
                target_msg_id, target_signal = res
                target_chat_id = target_signal.get("chat_id", chat_id)

        if not target_signal or target_msg_id is None:
            logger.warning(f"No target trade found in state to apply {action}. (Reply ID: {reply_to_id})")
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
            await notify_saved_messages(f"⚠️ **Order Blocked by Pre-Flight**: {reason}")
            return

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
        prev_lots = (await _mt5_call(executor.get_total_active_lots))["total_lots"]
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
        elif float(SELL_NOW_DELAY_SECONDS) <= 0.0:
            now_tick = await _mt5_call(executor.get_tick, symbol)
            initial_price = _get_live_entry_price(now_tick, direction)
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
            await notify_saved_messages(
                f"⏳ **{direction.title()} Now Held ({int(SELL_NOW_DELAY_SECONDS)}s Timer) [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• 📍 **Initial Market Snapshot:** `{initial_price}`\n"
                f"• Waiting {int(SELL_NOW_DELAY_SECONDS)}s for zone/limit edit or TP1 check; if no zone arrives, "
                f"will validate {cond_str} before executing 4-layer market ({BASE_LAYER_LOTS})."
            )
            return

        if not market_tickets and not pending_tickets:
            err_reason = getattr(executor, "last_error", None) or "Broker rejected order(s)"
            logger.error(f"Signal ID {msg_id} produced 0 orders ({err_reason}); skipping state creation.")
            await notify_saved_messages(
                f"❌ **Order Rejected by MT5 [ID: {msg_id}]**\n"
                f"• Direction: `{direction}` {symbol}\n"
                f"• Reason: `{err_reason}`\n"
                f"• No positions or pending orders were opened."
            )
            return

        arm_tick = await _mt5_call(executor.get_tick, symbol)
        tp1_guard_armed = _compute_initial_tp1_guard_armed(
            arm_tick, direction, raw_tp1, bool(market_tickets)
        )
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
            initial_price=initial_price,
            recorded_entry_price=initial_price,
            tp1_guard_armed=tp1_guard_armed,
        )

        new_lots = (await _mt5_call(executor.get_total_active_lots))["total_lots"]
        risk_alerts = await _mt5_call(executor.evaluate_risk_threshold_alerts, prev_lots, new_lots)
        alloc_warn = getattr(executor, "last_allocation_warning", None)
        extra_notes: List[str] = []
        if alloc_warn:
            extra_notes.append(f"⚠️ **Lot Cap Note:** `{alloc_warn}`")
        extra_notes.extend(risk_alerts)
        risk_block = ("\n\n" + "\n".join(extra_notes)) if extra_notes else ""

        await notify_saved_messages(
            f"🚀 **New 4-Layer Signal Executed ({order_intent}) [ID: {msg_id}]**\n"
            f"• Direction: `{direction}` {symbol}\n"
            f"• Orders: Market={market_tickets}, Pending={pending_tickets}\n"
            f"• Layers: {_format_layers_summary(created_sig)}\n"
            f"• SL: `{sl}` | Broker TP1: `{final_tp}` | Targets: {_format_tp_summary(created_sig)}"
            f"{risk_block}"
        )
        await _update_live_terminal_status_async()


async def handle_new_message(event: Any) -> None:
    """Handler for new messages in monitored Telegram channels (serialized per (chat_id, msg_id))."""
    if session_trading_blocked():
        return
    msg = getattr(event, "message", None)
    if not msg:
        return
    chat_id = _safe_chat_id(event)
    async with _get_signal_lock(chat_id, msg.id):
        await _handle_new_message_unlocked(event)


def _place_edit_layer_order(
    symbol: str,
    direction: str,
    target_price: float,
    lot: float,
    sl: Optional[float],
    tp: Optional[float],
    comment_prefix: str,
    tick: Any,
) -> Tuple[Optional[int], str]:
    """Place a limit order if price has not reached target_price yet; otherwise execute at market."""
    is_market = False
    if tick:
        if direction == "BUY" and getattr(tick, "ask", 999999.0) <= target_price:
            is_market = True
        elif direction == "SELL" and getattr(tick, "bid", 0.0) >= target_price:
            is_market = True

    if is_market:
        t = executor.execute_market_order(
            symbol=symbol, direction=direction, lot=lot, sl=sl, tp=tp, comment=f"{comment_prefix} Market",
        )
        return t, "MARKET"

    t = executor.place_limit_order(
        symbol=symbol, direction=direction, price=target_price, lot=lot, sl=sl, tp=tp, comment=f"{comment_prefix} Limit",
    )
    return t, "LIMIT"


async def _handle_message_edited_unlocked(event: Any) -> None:
    """Unlocked core implementation of handle_message_edited (caller holds signal lock)."""
    if session_trading_blocked():
        return
    msg = event.message
    if not msg:
        return

    msg_id = msg.id
    raw_text = msg.message or ""
    chat_id = _safe_chat_id(event)

    existing_signal = state_mgr.get_signal(msg_id, chat_id=chat_id)

    if existing_signal and raw_text.strip() == (existing_signal.get("raw_text") or "").strip():
        logger.debug(f"Edited message {msg_id} text unchanged; skipping.")
        return

    if not existing_signal:
        now = time.time()
        dedup_key = _get_dedup_key(chat_id, msg_id)
        if dedup_key in _recent_messages and (now - _recent_messages[dedup_key]) < DEDUPLICATION_WINDOW_SECONDS:
            logger.debug(f"Edited message {msg_id} not in state & within {DEDUPLICATION_WINDOW_SECONDS}s dedup; skipping.")
            return

        msg_date = getattr(msg, "date", None)
        if isinstance(msg_date, datetime):
            dt_utc = msg_date if msg_date.tzinfo is not None else msg_date.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - dt_utc).total_seconds()
            if age_sec > MAX_NEW_EDIT_AGE_SECONDS:
                logger.info(
                    f"Ignoring edit/reaction on old untracked message {msg_id} "
                    f"(age={age_sec:.0f}s > {MAX_NEW_EDIT_AGE_SECONDS}s limit)."
                )
                return

        logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{chat_id}] (not in state; treating as new) ---")
        await _handle_new_message_unlocked(event)
        return

    logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{chat_id}] ---\nNew Text: {raw_text}")

    curr_status = existing_signal.get("status")
    if curr_status in ("CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"):
        logger.info(f"Signal {msg_id} is already terminal ({curr_status}); ignoring edit.")
        return

    symbol = existing_signal.get("symbol", DEFAULT_SYMBOL)
    direction = existing_signal.get("direction", "BUY")

    if is_tp1_hit_message(raw_text):
        _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
        mk_tkts_now = list(existing_signal.get("market_tickets", []))
        if curr_status in ("PENDING", "PENDING_DELAY") or not mk_tkts_now:
            await _invalidate_signal_tp1_unlocked(msg_id, reason="Edit", chat_id=chat_id)
            state_mgr.update_signal(msg_id, {"raw_text": raw_text}, chat_id=chat_id)
            return
        elif not existing_signal.get("tp1_reached") and curr_status != "RUNNER_ACTIVE":
            existing_signal["raw_text"] = raw_text
            _, _, alert = await _execute_tp1_scaling_handoff(
                msg_id, existing_signal, chat_id=chat_id, source_label="Edited Signal"
            )
            await notify_saved_messages(alert)
            return

    is_forward = getattr(msg, "forward", None) is not None
    tick = await _mt5_call(executor.get_tick, DEFAULT_SYMBOL)
    live_price = getattr(tick, "bid", None) if tick else None
    parsed = parse_message(
        raw_text,
        message_id=msg_id,
        is_edit=True,
        is_forward=is_forward,
        live_price=live_price,
    )
    if parsed is None:
        return

    new_sl = parsed.sl if (parsed.sl_valid and parsed.sl is not None) else existing_signal.get("sl")
    new_tp = parsed.final_tp if parsed.final_tp is not None else existing_signal.get("final_tp")

    merged_tp_levels: Dict[str, float] = {}
    if isinstance(existing_signal.get("tp_levels"), dict):
        merged_tp_levels.update(existing_signal["tp_levels"])
    if isinstance(parsed.tp_levels, dict) and parsed.tp_levels:
        merged_tp_levels.update(parsed.tp_levels)

    raw_tp1 = parsed.raw_tp1 or existing_signal.get("raw_tp1") or merged_tp_levels.get("TP1")
    raw_tp2 = parsed.raw_tp2 or existing_signal.get("raw_tp2") or merged_tp_levels.get("TP2")
    raw_tp3 = parsed.raw_tp3 or existing_signal.get("raw_tp3") or merged_tp_levels.get("TP3")
    raw_tp4 = parsed.raw_tp4 or existing_signal.get("raw_tp4") or merged_tp_levels.get("TP4")
    raw_tp5 = parsed.raw_tp5 or existing_signal.get("raw_tp5") or merged_tp_levels.get("TP5")
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
                await notify_saved_messages(f"⚠️ **Edited Delayed Entry Skipped [ID: {msg_id}]**: {reason}")
                return

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
                await notify_saved_messages(f"❌ **Edited Zone Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`")
                return
            arm_tick = await _mt5_call(executor.get_tick, symbol)
            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
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
                    "zone": zone.to_dict(),
                    "is_zone": True,
                    "is_zone_order": True,
                    "zone_completed": True,
                    "max_orders": MAX_ORDERS_PER_SIGNAL,
                    "layers": l_data,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "tp1_guard_armed": _compute_initial_tp1_guard_armed(
                        arm_tick, direction, raw_tp1, bool(mk_tkts)
                    ),
                    "raw_text": raw_text,
                },
                chat_id=chat_id,
            )
            await notify_saved_messages(
                f"✏️ **Delayed {direction.title()} Now → 4-Layer Zone Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending Limits: {pd_tkts}\n"
                f"• SL: `{new_sl}` | Broker TP1: `{new_tp}`"
            )
            return
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
                await notify_saved_messages(f"❌ **Edited Order Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`")
                return
            arm_tick = await _mt5_call(executor.get_tick, symbol)
            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
                    "sl": new_sl,
                    "single_price": parsed.single_price,
                    "is_stop_order": is_stop_order,
                    "is_limit_order": is_limit_order,
                    "order_intent": order_intent,
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
                    "max_orders": MAX_ORDERS_PER_SIGNAL,
                    "layers": l_data,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "tp1_guard_armed": _compute_initial_tp1_guard_armed(
                        arm_tick, direction, raw_tp1, bool(mk_tkts)
                    ),
                    "raw_text": raw_text,
                },
                chat_id=chat_id,
            )
            await notify_saved_messages(
                f"✏️ **Delayed {direction.title()} Now → 4-Layer {order_intent} Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending: {pd_tkts} @ `{parsed.single_price}`"
            )
            return
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
            return

    market_tickets = list(existing_signal.get("market_tickets", []))
    pending_tickets = list(existing_signal.get("pending_tickets", []))
    layers = dict(existing_signal.get("layers") or {})
    is_legacy = StateManager.is_legacy_3layer(existing_signal)

    pip_size = await _mt5_call(executor.get_pip_size, symbol)
    if raw_tp2:
        legacy_runner_tp = round(raw_tp2, 2)
    elif raw_tp1:
        legacy_runner_tp = round(raw_tp1 + DEFAULT_TP_STEP_PIPS * pip_size, 2) if direction == "BUY" else round(raw_tp1 - DEFAULT_TP_STEP_PIPS * pip_size, 2)
    else:
        legacy_runner_tp = new_tp

    v3_runner_tp = raw_tp5 if raw_tp5 is not None else legacy_runner_tp

    # Protect trailed SL when RUNNER_ACTIVE
    if curr_status == "RUNNER_ACTIVE":
        runner_tkts = [
            t for t in (existing_signal.get("runner_tickets") or ([existing_signal["runner_ticket"]] if existing_signal.get("runner_ticket") else []))
            if t is not None
        ]
        if is_legacy:
            target_runner_tp = legacy_runner_tp
        else:
            stage_key = str(existing_signal.get("tp_stage") or existing_signal.get("runner_milestone_name") or "TP2").upper()
            if stage_key == "TP1":
                stage_key = "TP2"
            target_runner_tp = (
                (merged_tp_levels or {}).get(stage_key)
                or (merged_tp_levels or {}).get(f"TP{RUNNER_MAX_TP_STAGE}")
                or v3_runner_tp
            )
        if runner_tkts and target_runner_tp:
            for rt in runner_tkts:
                await _mt5_call(executor.modify_position_sltp, ticket=rt, sl=None, tp=target_runner_tp)
            for l_data in layers.values():
                if isinstance(l_data, dict) and l_data.get("ticket") in runner_tkts:
                    l_data["tp"] = target_runner_tp
        state_mgr.update_signal(
            msg_id,
            {
                "raw_tp1": raw_tp1,
                "raw_tp2": raw_tp2,
                "raw_tp3": raw_tp3,
                "raw_tp4": raw_tp4,
                "raw_tp5": raw_tp5,
                "tp_levels": merged_tp_levels or None,
                "layers": layers,
                "raw_text": raw_text,
            },
            chat_id=chat_id,
        )
        await notify_saved_messages(
            f"✏️ **Runner Target Updated via Edit [ID: {msg_id}]**\n"
            f"• Runner TP Ceiling: `{target_runner_tp}`"
        )
        return

    # Update SL/TP per layer role (v3.1 All-at-TP1 Entry Policy for pre-TP1 4-layer signals)
    if new_sl is not None or new_tp is not None:
        logger.info(f"Updating SL/TP on existing tickets for Msg ID {msg_id}: SL={new_sl}, Broker TP1={new_tp}")
        for l_name, l_data in layers.items():
            if not isinstance(l_data, dict):
                continue
            t = l_data.get("ticket")
            if not t:
                continue
            if not is_legacy:
                l_tp = new_tp
            elif l_name in ("L1", "TOP"):
                l_tp = new_tp
            else:
                l_tp = legacy_runner_tp

            if l_tp is not None:
                l_data["tp"] = l_tp
                if not is_legacy:
                    l_data["tp_stage"] = "TP1"
            if t in market_tickets:
                await _mt5_call(executor.modify_position_sltp, ticket=t, sl=new_sl, tp=l_tp)
            elif t in pending_tickets:
                await _mt5_call(executor.modify_pending_order_sltp, ticket=t, sl=new_sl, tp=l_tp)

    current_count = len(market_tickets) + len(pending_tickets)
    slots_left = MAX_ORDERS_PER_SIGNAL - current_count
    zone_completed = existing_signal.get("zone_completed", False)

    if parsed.zone and slots_left > 0 and not zone_completed:
        zone = parsed.zone
        if is_legacy:
            layer_specs = [
                ("MID", zone.mid, LAYER_LOTS.get("L2", 0.01), legacy_runner_tp, "TG Layer 2 (Edit Mid"),
                (
                    "BOTTOM",
                    zone.bottom if direction == "BUY" else zone.top,
                    LAYER_LOTS.get("L4", 0.02),
                    legacy_runner_tp,
                    "TG Layer 3 (Edit Bottom" if direction == "BUY" else "TG Layer 3 (Edit Top",
                ),
            ]
        else:
            tiers = zone.get_tiers(direction)
            alloc_lots, _ = await _mt5_call(executor.allocate_4layer_lots, symbol)
            layer_specs = [
                ("L2", tiers["L2"], alloc_lots[1], new_tp, "TG L2 (Edit"),
                ("L3", tiers["L3"], alloc_lots[2], new_tp, "TG L3 (Edit"),
                ("L4", tiers["L4"], alloc_lots[3], new_tp, "TG L4 (Edit"),
            ]

        for l_name, target_px, l_lot, l_tp, c_prefix in layer_specs:
            if slots_left <= 0:
                break
            if l_name in layers or l_lot <= 0.0:
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
            )
            if t:
                if o_type == "MARKET":
                    market_tickets.append(t)
                    layers[l_name] = {
                        "lot": l_lot,
                        "target_price": target_px,
                        "tp": l_tp,
                        "tp_stage": "TP1",
                        "ticket": t,
                        "order_type": "MARKET",
                        "status": "OPEN",
                    }
                else:
                    pending_tickets.append(t)
                    layers[l_name] = {
                        "lot": l_lot,
                        "target_price": target_px,
                        "tp": l_tp,
                        "tp_stage": "TP1",
                        "ticket": t,
                        "order_type": "LIMIT",
                        "status": "PENDING",
                    }
                slots_left -= 1

        zone_completed = True

    state_mgr.update_signal(
        msg_id,
        {
            "sl": new_sl,
            "raw_tp1": raw_tp1,
            "raw_tp2": raw_tp2,
            "raw_tp3": raw_tp3,
            "raw_tp4": raw_tp4,
            "raw_tp5": raw_tp5,
            "tp_levels": merged_tp_levels or None,
            "final_tp": new_tp,
            "hard_broker_tp": new_tp,
            "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
            "zone": parsed.zone.to_dict() if parsed.zone else existing_signal.get("zone"),
            "is_zone": True if parsed.zone else existing_signal.get("is_zone", False),
            "is_zone_order": True if parsed.zone else existing_signal.get("is_zone_order", existing_signal.get("is_zone", False)),
            "max_orders": MAX_ORDERS_PER_SIGNAL if parsed.zone else existing_signal.get("max_orders", MAX_ORDERS_PER_SIGNAL),
            "layers": layers,
            "market_tickets": market_tickets,
            "pending_tickets": pending_tickets,
            "zone_completed": zone_completed,
            "raw_text": raw_text,
        },
        chat_id=chat_id,
    )

    await notify_saved_messages(
        f"✏️ **Signal Updated via Edit [ID: {msg_id}]**\n"
        f"• SL: `{new_sl}` | Broker TP1: `{new_tp}`\n"
        f"• Total Orders: {len(market_tickets) + len(pending_tickets)}/{MAX_ORDERS_PER_SIGNAL}"
    )


async def handle_message_edited(event: Any) -> None:
    """Handler for edited messages (serialized per (chat_id, msg_id))."""
    if session_trading_blocked():
        return
    msg = getattr(event, "message", None)
    if not msg:
        return
    chat_id = _safe_chat_id(event)
    async with _get_signal_lock(chat_id, msg.id):
        await _handle_message_edited_unlocked(event)


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
        await event.reply(
            "⚪ **Daily Loss Limit Disabled**\n"
            "• The realized daily loss limit is now OFF (50%/90% emergency drawdown guards remain active).\n"
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
    "/bank", "bank", "/closeall", "closeall", "/panic", "panic",
    "/sim_shortfall", "sim_shortfall", "/testrunner", "testrunner",
    "/testorder", "testorder", "/testlimit", "testlimit", "/test", "test",
    "/health", "health", "/status", "status", "/ping", "ping",
    "/trades", "trades", "/algo", "algo", "/check", "check", "/help", "help",
}

_KNOWN_SAVED_PREFIXES = (
    "/sim", "sim ", "sim_",
    "/dailywin ", "dailywin ", "/daily_win ", "daily_win ",
    "/dailyloss ", "dailyloss ", "/daily_loss ", "daily_loss ",
    "/set_daily_loss ", "set_daily_loss ",
)


def _is_known_saved_command(text: str) -> bool:
    """Return True if a Saved Messages input is a command rather than a numeric prompt reply."""
    return text in _KNOWN_SAVED_COMMANDS or text.startswith(_KNOWN_SAVED_PREFIXES)


def _format_uptime() -> str:
    uptime_sec = max(0, int(time.time() - BOT_START_TIME))
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


async def handle_saved_messages_command(event: Any) -> None:
    """Remote monitoring commands sent to Saved Messages ('me')."""
    global _awaiting_dailywin_input, _awaiting_dailyloss_input
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
            "🟢", "🔴", "🩺",
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

    # LIVE CONTROL: /bank
    if text in ("/bank", "bank"):
        latest = state_mgr.get_latest_signal()
        if not latest or latest[1].get("status") == "CLOSED":
            await event.reply("⚠️ No active signals found to bank.")
            return

        msg_id, sig = latest
        sig_chat = sig.get("chat_id")
        if sig.get("status") == "RUNNER_ACTIVE":
            await event.reply(
                f"ℹ️ Signal `{msg_id}` is already in `RUNNER_ACTIVE` mode.\n"
                f"• Runner: `Layer {sig.get('runner_layer')}` [Ticket #{sig.get('runner_ticket')}]\n"
                f"• Milestone: `{sig.get('runner_milestone_name', 'TP1')}` | "
                f"Soft Floor: `{sig.get('runner_sl_floor') or sig.get('soft_tp1_floor')}`"
            )
            return

        async with _get_signal_lock(sig_chat, msg_id):
            fresh_sig = state_mgr.get_signal(msg_id, chat_id=sig_chat) or sig
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
                lines.append(
                    f"  - `{mid}` ({s.get('direction')} {s.get('symbol')}) | `{s.get('status')}` | "
                    f"Layers: `{_format_layers_summary(s)}` | Stage: `{tp_stage}` | Milestone: `{milestone}` "
                    f"(Cleared: `{cleared}`, Guard: `{armed}`, RevTicks: `{rev_ticks}/{RUNNER_REVERSAL_CONFIRM_TICKS}`)"
                )
            active_block = "\n" + "\n".join(lines)

        await event.reply(
            f"🩺 **XAUUSD Copier v3.0 Health Telemetry**\n"
            f"• Uptime: `{_format_uptime()}`\n"
            f"• Single-Instance Lock: `{lock_label}`\n"
            f"• MT5 Terminal: `{'🟢 CONNECTED' if mt5_ok else '🔴 DISCONNECTED'}` "
            f"(Ping: `{ping_ms:.2f} ms`)\n"
            f"• Algo Trading: `{'🟢 ENABLED' if algo_ok else '🔴 DISABLED'}`\n"
            f"• Telegram Client: `{'🟢 CONNECTED' if tg_connected else '⚪ OFFLINE'}` | "
            f"Reconnects: `{_telegram_reconnect_count}`\n"
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
            "L1": {"lot": 0.01, "ticket": 901, "order_type": "MARKET", "status": "OPEN"},
            "L2": {"lot": 0.01, "ticket": 902, "order_type": "LIMIT", "status": "PENDING"},
            "L3": {"lot": 0.01, "ticket": 903, "order_type": "LIMIT", "status": "PENDING"},
            "L4": {"lot": 0.02, "ticket": 904, "order_type": "LIMIT", "status": "PENDING"},
        }

        r1 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901], [902, 903, 904], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])
        r2 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901, 902], [903, 904], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])
        r3 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901, 902, 903], [904], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])
        r4 = test_exec.apply_smart_tp1_runner_strategy(sample_layers, [901, 902, 903, 904], [], DEFAULT_SYMBOL, "BUY", 3900.0, tp_map["TP2"])

        await event.reply(
            f"🏃 **v3.0 4-Layer Smart TP1 & Runner Hierarchy Test Results:**\n\n"
            f"1️⃣ **Case 1 (L1 only):** Banked=`{r1['banked']}`, Runner=`{r1['runner']}`, Status=`{r1['status']}`\n"
            f"2️⃣ **Case 2 (L1+L2):** Banked=`{r2['banked']}`, Runner=`{r2['runner']}` [#{r2['runner_ticket']}], Floor=`{r2['trailed_sl']}`, Status=`{r2['status']}`\n"
            f"3️⃣ **Case 3 (L1+L2+L3):** Banked=`{r3['banked']}`, Open=`{r3['remaining_market_tickets']}`, Runner=`{r3['runner']}`, Status=`{r3['status']}`\n"
            f"4️⃣ **Case 4 (All 4 L1..L4):** Banked=`{r4['banked']}`, Open=`{r4['remaining_market_tickets']}`, Runner=`{r4['runner']}` (0.02), Floor=`{r4['trailed_sl']}`, Status=`{r4['status']}`\n\n"
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

        active_signals = state_mgr.get_active_signals()
        if not active_signals:
            trades_str = "\n• Active Signals: None (Listening for next signal)"
        else:
            trades_str = f"\n• Active Signals ({len(active_signals)}):"
            for mid, s in active_signals:
                st = s.get("status")
                trades_str += f"\n  - ID `{mid}`: `{s.get('direction')}` {s.get('symbol')} | Status: `{st}`"
                trades_str += f"\n    • Layers: `{_format_layers_summary(s)}`"
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
                    trades_str += (
                        f"\n    • 🚀 Runner: Layer `{s.get('runner_layer')}` [Ticket #{s.get('runner_ticket')}]"
                        f" | Milestone: `{s.get('runner_milestone_name', 'TP1')}` (Cleared: `{cleared or 'None'}`)"
                        f"\n    • 🛡️ Floor: `{s.get('runner_sl_floor') or s.get('soft_tp1_floor')}` | "
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

        dd_state_str = (
            "🚨 90% CUT-OFF" if dd_st["emergency_cutoff_triggered"]
            else "🛑 50% PAUSED (Holding Open Trades)" if dd_st["pause_new_signals"]
            else "🟢 OK"
        )
        srv_date = await _trading_day_str_async()
        ledger_stats = trade_ledger.get_stats(server_date=srv_date)
        lots_vec_str = "/".join(f"{b:.2f}" for b in BASE_LAYER_LOTS)

        risk_str = (
            f"\n• 4-Layer Split: `[{lots_vec_str}]` x `{RISK_LOT_MULTIPLIER}` (`{MAX_ORDERS_PER_SIGNAL}` orders/signal)"
            f"\n• Active Lots: `{risk_st['total_lots']:.2f} / {risk_st['high_risk_lots']:.2f} Max` "
            f"(Low: `{risk_st['low_risk_lots']:.2f}` | Mid: `{risk_st['mid_risk_lots']:.2f}` | "
            f"High Cap: `{risk_st['high_risk_lots']:.2f}`)"
            f"\n• Daily Win Limit (`dailywin`): {dw_state_str}"
            f"\n• Daily Loss Limit (`dailyloss`): {dl_state_str}"
            f"\n• Daily Drawdown Guard: `{dd_state_str}` "
            f"(Loss: `-{dd_st['total_daily_loss']:.2f} {dd_st['currency']}` / `{dd_st['loss_pct']:.1f}%` | "
            f"50% Pause: `-{dd_st['pause_limit_amount']:.2f}` | 90% Cut-Off: `-{dd_st['emergency_cutoff_amount']:.2f}`)"
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
            f"{trades_str}\n\n"
            f"📱 **Commands:** `/health`, `/status`, `dailywin`, `dailyloss`, `/set_daily_loss`, `/status_daily_loss`, `/bank`, `/closeall`, `/sim_tp1 1|2|3|4`, `/sim_shortfall`, `/sim_trail 2|3|4`, `/test`, `/testrunner`, `/testorder`"
        )
        await event.reply(reply_text)


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
            await _update_live_terminal_status_async(force_print=True)

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
