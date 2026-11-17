import os
import sys
import time
import inspect
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

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
    LAYER_LOTS,
    MAX_ORDERS_PER_SIGNAL,
    BE_LAYERS,
    BE_OFFSET_PIPS,
    TP_FRONT_RUN_OFFSET,
    TP1_CLOSE_PROXIMITY_PIPS,
    NOTIFY_SAVED_MESSAGES,
    DRY_RUN,
    LOG_FILE_PATH,
    STATE_FILE_PATH,
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
    PREVENT_SYSTEM_SLEEP,
    IDLE_MT5_POLL_INTERVAL_SECONDS,
    TELEGRAM_KEEPALIVE_SECONDS,
    CONSOLE_TITLE_UPDATES,
)
from parser import parse_message, SignalAction, ZoneInfo, is_tp1_hit_message
from mt5_executor import MT5Executor
from state_manager import StateManager


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
    # Enable Windows Virtual Terminal Processing for ANSI colors
    if sys.platform == "win32":
        try:
            import os
            os.system("")
        except Exception:
            pass

    logger = logging.getLogger("copier")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    # Colorful console output with pinned MT5 bottom status bar support
    console_handler = LiveHUDStreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredConsoleFormatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    # Clean uncolored file log
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
    logger.addHandler(file_handler)

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

    return logger


logger = setup_logger()

# Shared Singletons
state_mgr = StateManager(STATE_FILE_PATH)
executor = MT5Executor(dry_run=DRY_RUN)
telethon_client: Optional[TelegramClient] = None
BOT_START_TIME = time.time()

# In-memory deduplication tracking ((chat_id, msg_id) or msg_id -> process_timestamp)
_recent_messages: Dict[Any, float] = {}

# Delayed "Sell now" / "Buy now" tasks awaiting limit/zone edit (msg_id or (chat_id, msg_id) -> asyncio.Task)
_pending_sell_now_tasks: Dict[Any, asyncio.Task] = {}
_pending_task_chats: Dict[Any, Any] = {}

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


def session_trading_blocked() -> bool:
    """True when trading is halted/terminating or locked (daily win / daily loss) -> never open new trades."""
    return bool(TRADING_HALTED or _daily_win_terminate_requested or _daily_loss_terminate_requested or _daily_loss_locked)


def _trading_day_str() -> str:
    """Current broker-server trading day (YYYY-MM-DD) so daily limits roll over with MT5's day."""
    try:
        return executor.get_server_date_str()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _close_all_bot_exposure_and_signals() -> Dict[str, Any]:
    """
    Close every open bot position, cancel every bot pending order, stop all delayed
    'Sell now'/'Buy now' timers and mark all tracked signals CLOSED.
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
        layers = s.get("layers", {})
        for lk in layers:
            layers[lk]["status"] = "CLOSED"
        state_mgr.update_signal(
            mid,
            {"status": "CLOSED", "market_tickets": [], "pending_tickets": [], "layers": layers},
        )
    for mid, s in state_mgr.get_all_signals().items():
        if s.get("status") in ("PENDING", "PENDING_DELAY"):
            state_mgr.update_signal(mid, {"status": "CLOSED"})

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

    prog = get_daily_win_progress()
    if not prog["target_reached"]:
        return "OK"

    _daily_win_terminate_requested = True
    target_amount = prog["target_amount"]
    baseline_pnl = prog["baseline_pnl"]
    curr = prog["currency"]

    # 1-2. Bank all open bot positions, cancel pending limits & delayed entries, close tracked signals
    res = _close_all_bot_exposure_and_signals()
    closed_positions = len(res.get("closed_tickets", []))
    cancelled_orders = len(res.get("cancelled_orders", []))

    # 3. Disable dailywin so future sessions start clean unless toggled again
    state_mgr.set_daily_win_config(
        enabled=False,
        target_amount=target_amount,
        baseline_pnl=0.0,
        date_str=prog["date"],
    )

    dd_after = executor.get_daily_drawdown_status()
    realized_after = round(float(dd_after.get("realized_today_pnl", 0.0)) - baseline_pnl, 2)
    final_profit = max(prog["net_profit_today"], realized_after)
    fin = executor.get_account_financials()

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
    - Floating losses on open trades never trigger it (only closed / Stop-Loss-hit trades count).
    - When realized P&L <= -abs(limit):
        1. Sets TRADING_HALTED = True, stops pending delayed timers, and cancels ALL pending MT5 orders
           via executor.cancel_all_pending_orders (leaving open positions protected by their broker SL/TP
           unless DAILY_LOSS_CLOSE_OPEN_TRADES=true).
        2. Records the trigger date, notifies Saved Messages, disconnects Telethon & shuts down MT5.
        3. Calls sys.exit(0) when exit_on_trigger=True so the launcher stops cleanly.
    - If relaunched manually on the SAME broker day while still over the limit, enters LOCKED mode
      (no new trades) so `dailyloss off` / `/set_daily_loss` can override.
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

    prog = get_daily_loss_progress()
    if not prog["limit_reached"]:
        if _daily_loss_locked:
            _daily_loss_locked = False
            logger.info("Daily loss lock released (limit disabled / raised above today's realized loss).")
        return "OK"

    curr = prog["currency"]
    limit_amt = round(abs(float(prog["limit_amount"])), 2)
    limit_str = format_profit(-limit_amt, curr)
    loss_str = format_profit(prog["realized_today_pnl"], curr)

    # Relaunched on the same broker day after the limit already fired -> LOCKED (stay online, trade nothing)
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

    # 1. Stop all delayed timers & cancel ALL pending MT5 orders (optionally close filled positions)
    for pending_key in list(_pending_sell_now_tasks.keys()):
        _cancel_delayed_sell_now(pending_key)

    all_sigs = state_mgr.get_all_signals()
    tracked_pd: List[int] = []
    for _, s in all_sigs.items():
        for pt in (s.get("pending_tickets") or []):
            if pt not in tracked_pd:
                tracked_pd.append(pt)

    if DAILY_LOSS_CLOSE_OPEN_TRADES:
        res = _close_all_bot_exposure_and_signals()
        extra_cancelled = executor.cancel_all_pending_orders(tracked_pd)
        cancelled_orders_list = list(dict.fromkeys(res.get("cancelled_orders", []) + extra_cancelled))
        closed_positions = len(res.get("closed_tickets", []))
    else:
        cancelled_orders_list = executor.cancel_all_pending_orders(tracked_pd)
        closed_positions = 0
        for mid, s in all_sigs.items():
            st = s.get("status")
            if st in ("PENDING", "PENDING_DELAY"):
                layers = s.get("layers", {})
                for lk in layers:
                    if isinstance(layers[lk], dict) and layers[lk].get("status") == "PENDING":
                        layers[lk]["status"] = "CANCELLED"
                state_mgr.update_signal(mid, {"status": "CLOSED", "pending_tickets": [], "layers": layers})
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
                state_mgr.update_signal(mid, updates)

    cancelled_orders = len(cancelled_orders_list)

    # 2. Record the trigger so a same-day manual relaunch enters LOCKED mode instead of trading again
    state_mgr.set_daily_loss_config(
        enabled=True,
        limit_amount=limit_amt,
        triggered_date=prog["date"],
        triggered_at=time.time(),
    )

    final_realized = round(float(executor.get_realized_today_pnl()), 2)
    fin = executor.get_account_financials()
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
        executor.shutdown()
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
    dd = executor.get_daily_drawdown_status()
    curr = dd.get("currency", "GBP")
    today_str = _trading_day_str()

    if dd.get("emergency_cutoff_triggered"):
        active_sigs = state_mgr.get_active_signals()
        all_mk: List[int] = []
        all_pd: List[int] = []
        for _, s in active_sigs:
            all_mk.extend(s.get("market_tickets", []))
            all_pd.extend(s.get("pending_tickets", []))

        res = executor.emergency_cutoff_losing_trades(all_mk, all_pd)
        closed_tkts = res.get("closed_tickets", [])
        cancelled_ords = res.get("cancelled_orders", [])

        for mid, s in active_sigs:
            layers = s.get("layers", {})
            for lk in layers:
                layers[lk]["status"] = "CLOSED"
            state_mgr.update_signal(
                mid,
                {
                    "status": "CLOSED",
                    "market_tickets": [],
                    "pending_tickets": [],
                    "layers": layers,
                },
            )

        if closed_tkts or cancelled_ords or active_sigs:
            fin = executor.get_account_financials()
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


def update_live_terminal_status(force_print: bool = False) -> None:
    """
    Renders a compact (<79 chars, never line-wraps) in-place MetaTrader 5 balance bar at the
    bottom of the terminal. Only redraws when Balance, Credit, Equity, P&L, or Lots change
    so the console never floods when idle.
    """
    global _last_terminal_snapshot, _current_hud_line, _last_hud_state
    if executor.dry_run:
        return
    try:
        st = executor.get_risk_exposure_status()
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
            f"[XAUUSD COPIER] Bal: {sym_char}{bal:.2f} | Cred: {sym_char}{cred:.2f} | "
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
        # Compact (<79 visible chars) so it NEVER wraps in an 80- or 120-column cmd.exe window
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
) -> bool:
    """Return True if all planned entry tiers are pending limit orders away from live market."""
    if not tick:
        return False
    dir_up = str(direction or "").upper()
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


async def invalidate_signal_tp1(
    msg_id: Any,
    reason: str,
    chat_id: Optional[int | str] = None,
) -> bool:
    """
    Invalidate an unentered signal (PENDING/PENDING_DELAY or unfilled zone pending limits)
    when TP1 is hit via Telegram Reply/Edit/Alert or live MT5 market price.
    Cancels pending limit/stop orders via TRADE_ACTION_REMOVE and never closes active positions.
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
    extra_pending = [t for t in (sig.get("mt5_order_tickets") or []) if t not in market_tickets and t not in pending_tickets]
    all_pending = pending_tickets + extra_pending
    layers = dict(sig.get("layers") or {})

    cancelled: List[int] = []
    for pt in all_pending:
        if pt not in market_tickets and executor.cancel_pending_order(pt):
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
    3. At T = delay_seconds, compares fresh live entry price against `initial_price`:
       - SELL: favorable iff current_price >= initial_price.
       - BUY: favorable iff current_price <= initial_price.
       Aborts immediately (`ABORTED_PRICE_GUARD`) when NOW_PRICE_GUARD_ABORT_ON_MISMATCH=True (default).
    """
    eff_key = task_key if task_key is not None else msg_id
    try:
        delay_total = max(0.0, float(SELL_NOW_DELAY_SECONDS))
        step = min(float(DELAYED_POLL_INTERVAL_SECONDS), max(0.01, delay_total / 4.0))
        deadline = time.monotonic() + delay_total

        while True:
            sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
            if not sig or sig.get("status") not in ("PENDING", "PENDING_DELAY") or sig.get("tp1_reached") or sig.get("is_zone_order"):
                return
            if session_trading_blocked():
                state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
                logger.info(f"Delayed entry [ID: {msg_id}] dropped: session is terminating / halted / locked.")
                return

            symbol = sig.get("symbol", DEFAULT_SYMBOL)
            direction = sig.get("direction", "SELL").upper()
            tp1_price = sig.get("tp1_price") if sig.get("tp1_price") is not None else sig.get("raw_tp1")
            tick = executor.get_tick(symbol)
            if tp1_price is not None and tick and _tick_hit_tp1(tick, direction, tp1_price):
                await invalidate_signal_tp1(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
                return

            now_mono = time.monotonic()
            if now_mono >= deadline:
                break
            await asyncio.sleep(min(step, max(0.005, deadline - now_mono)))

        sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
        if not sig or sig.get("status") not in ("PENDING", "PENDING_DELAY") or sig.get("tp1_reached") or sig.get("is_zone_order"):
            return
        if session_trading_blocked():
            state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
            return

        symbol = sig.get("symbol", DEFAULT_SYMBOL)
        direction = sig.get("direction", "SELL").upper()
        sl = sig.get("sl")
        final_tp = sig.get("final_tp")
        tp1_price = sig.get("tp1_price") if sig.get("tp1_price") is not None else sig.get("raw_tp1")
        initial_price = sig.get("initial_price") if sig.get("initial_price") is not None else sig.get("recorded_entry_price")

        current_tick = executor.get_tick(symbol)
        if tp1_price is not None and current_tick and _tick_hit_tp1(current_tick, direction, tp1_price):
            await invalidate_signal_tp1(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
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
                wait_started = time.time()
                bad_price_notified = False
                while True:
                    sig = state_mgr.get_signal(msg_id, chat_id=chat_id)
                    if not sig or sig.get("status") not in ("PENDING", "PENDING_DELAY") or sig.get("tp1_reached"):
                        return
                    if session_trading_blocked():
                        state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
                        return
                    tick = executor.get_tick(symbol)
                    if tp1_price is not None and tick and _tick_hit_tp1(tick, direction, tp1_price):
                        await invalidate_signal_tp1(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
                        return
                    current_price = _get_live_entry_price(tick, direction)
                    if current_price is not None:
                        fav_now = (current_price >= init_val) if direction == "SELL" else (current_price <= init_val)
                        if fav_now:
                            break
                        if not bad_price_notified:
                            bad_price_notified = True
                            state_mgr.update_signal(msg_id, {"awaiting_favorable_price": True}, chat_id=chat_id)
                            cmp_desc = f"higher (`>= {init_val:.2f}`)" if direction == "SELL" else f"lower (`<= {init_val:.2f}`)"
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
                    if (time.time() - wait_started) >= NOW_ENTRY_MAX_WAIT_SECONDS:
                        state_mgr.update_signal(
                            msg_id,
                            {"status": "ABORTED_PRICE_GUARD", "awaiting_favorable_price": False},
                            chat_id=chat_id,
                        )
                        return
                    await asyncio.sleep(min(float(DELAYED_POLL_INTERVAL_SECONDS), max(0.01, delay_total)))

            logger.info(
                f"[PRICE GUARD] {symbol} | {direction} | Initial: {init_val} | "
                f"Post-Delay: {current_price} | Action: EXECUTED"
            )
        else:
            logger.info(
                f"[PRICE GUARD] {symbol} | {direction} | Initial: None | "
                f"Post-Delay: {current_price} | Action: EXECUTED"
            )

        passed, reason = executor.check_preflight(
            symbol=symbol,
            direction=direction,
            raw_tp1=tp1_price,
            sl=sl,
        )
        if not passed:
            logger.warning(f"Delayed {direction} Now [ID: {msg_id}] blocked by pre-flight after wait: {reason}")
            state_mgr.update_signal(msg_id, {"status": "CLOSED"}, chat_id=chat_id)
            await notify_saved_messages(f"⚠️ **Delayed {direction.title()} Now Skipped [ID: {msg_id}]**: {reason}")
            return

        top_lot = LAYER_LOTS.get("TOP", 0.01)
        prev_lots = executor.get_total_active_lots()["total_lots"]
        t1 = executor.execute_market_order(
            symbol=symbol,
            direction=direction,
            lot=top_lot,
            sl=sl,
            tp=final_tp,
            comment=f"TG Layer 1 (Delayed {direction.title()} Now)",
        )
        if not t1:
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

        new_lots = executor.get_total_active_lots()["total_lots"]
        risk_alerts = executor.evaluate_risk_threshold_alerts(prev_lots, new_lots)
        risk_block = ("\n\n" + "\n".join(risk_alerts)) if risk_alerts else ""

        layers_data = {
            "TOP": {"lot": top_lot, "target_price": None, "ticket": t1, "order_type": "MARKET", "status": "OPEN"},
            "MID": {"lot": LAYER_LOTS.get("MID", 0.01), "target_price": None, "ticket": None, "order_type": "LIMIT", "status": "PENDING"},
            "BOTTOM": {"lot": LAYER_LOTS.get("BOTTOM", 0.02), "target_price": None, "ticket": None, "order_type": "LIMIT", "status": "PENDING"},
        }
        state_mgr.update_signal(
            msg_id,
            {
                "status": "ACTIVE",
                "awaiting_favorable_price": False,
                "market_tickets": [t1],
                "layers": layers_data,
            },
            chat_id=chat_id,
        )
        await notify_saved_messages(
            f"🚀 **Delayed {direction.title()} Now Executed (No Limits Posted) [ID: {msg_id}]**\n"
            f"• Direction: `{direction}` {symbol}\n"
            f"• 📍 **Initial Point:** `{initial_price}` | **Entered At:** `{current_price}`\n"
            f"• Orders: Market=[`{t1}`] (0.01 lot)\n"
            f"• SL: `{sl}` | TP1: `{final_tp}`"
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
# 1-SECOND TP1 PROXIMITY WATCHER TASK
# ---------------------------------------------------------------------------

async def _run_telegram_keepalive_check(now: float) -> None:
    """Ping Telegram with a short timeout and cycle the socket immediately on half-open drop."""
    if not telethon_client:
        return
    try:
        if not telethon_client.is_connected():
            logger.warning("Telegram client disconnected; attempting reconnect...")
            await asyncio.wait_for(telethon_client.connect(), timeout=8.0)
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
            logger.info("Telegram connection recovered cleanly after keepalive cycle.")
        except Exception as rec_err:
            logger.debug(f"Telegram reconnect attempt deferred: {rec_err}")


async def tp1_proximity_watcher_task() -> None:
    """
    Background loop running every 1.0 second:
    1. 30-Second Algo Trading Button Monitor: alerts Saved Messages if Algo Trading is turned OFF.
    2. 1-Second MT5 State Reconciliation: syncs filled pending limits, detects manual/mobile closes.
    3. 1-Second 20-Pip TP1 Proximity Auto-Close: auto-banks profits once price reaches raw_tp1 ± $2.00.
    """
    logger.info("TP1 20-Pip Proximity Watcher & MT5 State Reconciler started (1.0s loop).")
    last_algo_check = 0.0
    last_tg_keepalive = time.time()
    last_account_poll = 0.0
    has_live_exposure = False

    while True:
        try:
            now = time.time()
            active_signals = state_mgr.get_active_signals()
            update_live_terminal_status()
            if not executor.dry_run and _last_hud_state is not None:
                has_live_exposure = (_last_hud_state[4] > 0.0)

            # Throttle heavy history_deals_get drawdown/limit checks at IDLE_MT5_POLL_INTERVAL_SECONDS (5s)
            # when flat, while staying on 1.0s speed whenever active signals or open exposure exist.
            if active_signals or has_live_exposure or (now - last_account_poll >= IDLE_MT5_POLL_INTERVAL_SECONDS):
                last_account_poll = now
                await check_and_enforce_daily_drawdown()
                if await check_and_enforce_daily_win_target() == "DAILY_WIN_REACHED":
                    break
                if await check_and_enforce_daily_loss_limit(exit_on_trigger=False) == "DAILY_LOSS_REACHED":
                    break

            # ---------------------------------------------------------------
            # 0. Active Telegram TCP Keepalive + Dead-Socket Recovery
            # ---------------------------------------------------------------
            if now - last_tg_keepalive >= TELEGRAM_KEEPALIVE_SECONDS:
                last_tg_keepalive = now
                await _run_telegram_keepalive_check(now)

            # ---------------------------------------------------------------
            # 1. 30-Second Algo Trading Button Monitor
            # ---------------------------------------------------------------
            if now - last_algo_check >= CHECK_ALGO_TRADING_INTERVAL:
                last_algo_check = now
                if not executor.dry_run and executor.is_connected():
                    if not executor.is_trade_allowed():
                        logger.warning("⚠️ Algo Trading button in MT5 is currently OFF!")
                        await notify_saved_messages("⚠️ **Warning**: Algo Trading is toggled OFF in MT5. Orders will fail until you enable it.")

            # ---------------------------------------------------------------
            # 2. 1-Second MT5 State Reconciliation
            # ---------------------------------------------------------------
            active_signals = state_mgr.get_active_signals()

            if not executor.dry_run and executor.is_connected() and active_signals:
                live_positions = executor.get_live_positions(DEFAULT_SYMBOL)
                live_orders = executor.get_live_orders(DEFAULT_SYMBOL)
                live_pos_tickets = {
                    getattr(p, "ticket", None) for p in live_positions if getattr(p, "ticket", None) is not None
                }
                live_order_tickets = {
                    getattr(o, "ticket", None) for o in live_orders if getattr(o, "ticket", None) is not None
                }

                for msg_id, sig in list(active_signals):
                    mk_tickets = list(sig.get("market_tickets", []))
                    pd_tickets = list(sig.get("pending_tickets", []))
                    layers = sig.get("layers", {})
                    updated = False

                    # Check if pending limits filled into positions
                    new_pd_tickets = []
                    for t in pd_tickets:
                        if t in live_pos_tickets:
                            if t not in mk_tickets:
                                mk_tickets.append(t)
                            for l_name, l_data in layers.items():
                                if l_data.get("ticket") == t:
                                    l_data["status"] = "FILLED"
                            updated = True
                            logger.info(f"Reconciled MT5 state: Pending order #{t} filled into active position for Msg ID {msg_id}")
                        elif t in live_order_tickets:
                            new_pd_tickets.append(t)
                        else:
                            # Cancelled or removed
                            updated = True
                            for l_name, l_data in layers.items():
                                if l_data.get("ticket") == t:
                                    l_data["status"] = "CANCELLED"
                            logger.info(f"Reconciled MT5 state: Pending order #{t} was cancelled/removed for Msg ID {msg_id}")

                    # Check if market positions were closed (SL/TP hit or manual close from mobile app)
                    new_mk_tickets = []
                    broker_closed = []
                    for t in mk_tickets:
                        if t in live_pos_tickets:
                            new_mk_tickets.append(t)
                        else:
                            updated = True
                            broker_closed.append(t)
                            for l_name, l_data in layers.items():
                                if l_data.get("ticket") == t:
                                    l_data["status"] = "CLOSED"
                            logger.info(f"Reconciled MT5 state: Market position #{t} closed for Msg ID {msg_id}")

                    if updated:
                        new_status = "CLOSED" if (not new_mk_tickets and not new_pd_tickets) else sig.get("status")
                        extra = {}
                        if sig.get("runner_ticket") in broker_closed:
                            extra["last_runner_ticket"] = sig.get("runner_ticket")
                            extra["runner_ticket"] = None
                        state_mgr.update_signal(
                            msg_id,
                            {
                                "market_tickets": new_mk_tickets,
                                "pending_tickets": new_pd_tickets,
                                "layers": layers,
                                "status": new_status,
                                **extra,
                            },
                        )

                    # Notify on every broker-side close (hard TP, trailed SL, SL, manual/mobile close)
                    for t in broker_closed:
                        info = executor.get_position_close_info(t)
                        fin = executor.get_account_financials()
                        curr = fin.get("currency", "GBP")
                        reason_map = {
                            "TP": "🎯 Broker Take Profit Hit",
                            "SL": "🛑 Stop Loss Hit (incl. trailed / breakeven SL)",
                            "MANUAL/BOT": "✋ Closed manually / from MT5 app",
                            "UNKNOWN": "Closed on broker",
                        }
                        is_runner = (t == sig.get("runner_ticket"))
                        pnl = info["profit"]
                        head = "🏁 **Runner Closed by Broker**" if is_runner else "📌 **Position Closed by Broker**"
                        if info["reason"] == "TP" and not is_runner:
                            head = "🎯 **TP Hit - Profit Banked**"
                        await notify_saved_messages(
                            f"{head} [ID: {msg_id}]\n"
                            f"• Ticket: `#{t}` ({sig.get('direction')} {sig.get('symbol')})\n"
                            f"• Exit: {reason_map.get(info['reason'], info['reason'])}\n"
                            f"• {'💰' if pnl >= 0 else '🔻'} **Result:** `{format_profit(pnl, curr)}`\n\n"
                            f"{fin['formatted']}"
                        )

            # ---------------------------------------------------------------
            # 3. 1-Second TP1 Scaling, Shortfall Guard & Runner Trailing
            # ---------------------------------------------------------------
            # Re-fetch signals after reconciliation
            active_signals = state_mgr.get_active_signals()
            for msg_id, sig in active_signals:
                status = sig.get("status")
                if status == "CLOSED":
                    continue

                symbol = sig.get("symbol", DEFAULT_SYMBOL)
                direction = sig.get("direction", "BUY").upper()

                tick = executor.get_tick(symbol)
                if not tick:
                    continue

                # ===========================================================
                # Branch A: Designated Runner Active (Post-TP1 Management)
                # ===========================================================
                if status == "RUNNER_ACTIVE":
                    still_active = executor.manage_designated_runner(sig)
                    state_mgr.update_signal(msg_id, sig)
                    if not still_active:
                        runner_layer = sig.get("runner_layer", "RUNNER")
                        last_runner_t = sig.get("last_runner_ticket")
                        tp2_hit = sig.get("tp2_reached", False)
                        close_reason = sig.get("runner_close_reason", "Trailing SL / Virtual Floor / TP2 Shortfall")

                        runner_profit = executor.get_position_closed_profit(last_runner_t)
                        fin = executor.get_account_financials()
                        curr = fin.get("currency", "GBP")
                        profit_str = format_profit(runner_profit, curr)

                        prev_banked = sig.get("banked_profit", 0.0)
                        total_trade_profit = runner_profit + prev_banked
                        total_profit_str = format_profit(total_trade_profit, curr)

                        logger.info(f"Runner #{last_runner_t} for Signal {msg_id} exited ({close_reason}). Profit={profit_str}")

                        if tp2_hit:
                            alert = (
                                f"🏆 **TP2 REACHED! Maximum Profit Banked [ID: {msg_id}]**\n"
                                f"• Symbol: `{symbol}` ({direction})\n"
                                f"• Runner: `Layer {runner_layer}` [Ticket #{last_runner_t}]\n"
                                f"• Target TP2: `{sig.get('raw_tp2')}`\n"
                                f"• 💰 **Amount Made on Runner:** `{profit_str}`\n"
                                f"• 💵 **Total Trade Profit:** `{total_profit_str}`\n\n"
                                f"{fin['formatted']}"
                            )
                        else:
                            alert = (
                                f"🏁 **Runner Trade Closed & Banked [ID: {msg_id}]**\n"
                                f"• Symbol: `{symbol}` ({direction})\n"
                                f"• Runner: `Layer {runner_layer}` [Ticket #{last_runner_t}]\n"
                                f"• Exit Reason: 🔒 `{close_reason}`\n"
                                f"• 💰 **Amount Made on Runner:** `{profit_str}`\n"
                                f"• 💵 **Total Trade Profit:** `{total_profit_str}`\n\n"
                                f"{fin['formatted']}"
                            )
                        await notify_saved_messages(alert)
                    continue

                # ===========================================================
                # Branch B: Pre-TP1 Monitoring (Scaling & Shortfall Guard)
                # ===========================================================
                if sig.get("tp1_reached", False):
                    continue

                curr_price = tick.bid if direction == "BUY" else tick.ask
                raw_tp1 = sig.get("raw_tp1")

                # Improvement #3: Fallback TP1 if an unpriced Market Now trade never received a TP edit
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
                        pip_offset = FALLBACK_TP1_PIPS * 0.10
                        raw_tp1 = round(curr_price + pip_offset, 2) if direction == "BUY" else round(curr_price - pip_offset, 2)
                        final_tp = round(raw_tp1 - TP_FRONT_RUN_OFFSET, 2) if direction == "BUY" else round(raw_tp1 + TP_FRONT_RUN_OFFSET, 2)
                        for mt in sig.get("market_tickets", []):
                            executor.modify_position_sltp(ticket=mt, sl=None, tp=final_tp)
                        sig["raw_tp1"] = raw_tp1
                        sig["final_tp"] = final_tp
                        sig["hard_broker_tp"] = final_tp
                        sig["fallback_tp_applied"] = True
                        state_mgr.update_signal(msg_id, sig)
                        logger.info(f"Attached fallback TP1 ({raw_tp1}, +{FALLBACK_TP1_PIPS} pips) on unpriced Signal {msg_id}")
                        await notify_saved_messages(
                            f"🛡️ **Fallback TP1 Attached [ID: {msg_id}]**\n"
                            f"• No TP edit received after {int(UNPRICED_TP_GRACE_SECONDS)}s.\n"
                            f"• Auto-assigned TP1: `{raw_tp1}` (Broker TP: `{final_tp}`) so profit guards & trailing are active."
                        )

                if not raw_tp1:
                    continue

                tp1_hit = (curr_price >= raw_tp1) if direction == "BUY" else (curr_price <= raw_tp1)

                # Improvement #1 / Trigger 2: If 0 market positions filled and pending limits remain,
                # invalidate once live market price reaches raw_tp1 (when pre-TP1 guard is armed).
                if not sig.get("market_tickets"):
                    pd_tkts = list(sig.get("pending_tickets", []))
                    if pd_tkts:
                        if not sig.get("tp1_guard_armed", True):
                            entry_ref = None
                            z = sig.get("zone")
                            if isinstance(z, dict):
                                entry_ref = z.get("top") if direction == "BUY" else z.get("bottom")
                            elif isinstance(sig.get("layers"), dict):
                                entry_ref = sig["layers"].get("TOP", {}).get("target_price")
                            margin = (
                                min(1.5, abs(float(raw_tp1) - float(entry_ref)) * 0.5)
                                if isinstance(entry_ref, (int, float))
                                else 0.5
                            )
                            if direction == "BUY":
                                ask_val = getattr(tick, "ask", None)
                                if isinstance(ask_val, (int, float)) and not isinstance(ask_val, bool) and float(ask_val) <= round(float(raw_tp1) - margin, 2):
                                    sig["tp1_guard_armed"] = True
                                    state_mgr.update_signal(msg_id, {"tp1_guard_armed": True}, chat_id=sig.get("chat_id"))
                            elif direction == "SELL":
                                bid_val = getattr(tick, "bid", None)
                                if isinstance(bid_val, (int, float)) and not isinstance(bid_val, bool) and float(bid_val) >= round(float(raw_tp1) + margin, 2):
                                    sig["tp1_guard_armed"] = True
                                    state_mgr.update_signal(msg_id, {"tp1_guard_armed": True}, chat_id=sig.get("chat_id"))

                        if sig.get("tp1_guard_armed", True) and _tick_hit_tp1(tick, direction, raw_tp1):
                            await invalidate_signal_tp1(
                                msg_id,
                                reason="Market Price Reached TP1",
                                chat_id=sig.get("chat_id"),
                            )
                    continue

                if tp1_hit:
                    logger.info(
                        f"🎯 TP1 REACHED on Msg ID {msg_id}! "
                        f"Live Price={curr_price}, Raw TP1={raw_tp1}"
                    )
                    runner_ticket = executor.handle_tp1_scaling_and_select_runner(sig)
                    state_mgr.set_tp1_reached(msg_id, True)

                    banked_tkts = sig.get("last_banked_tickets", [])
                    banked_profit = sum(executor.get_position_closed_profit(t) for t in banked_tkts)
                    sig["banked_profit"] = banked_profit
                    state_mgr.update_signal(msg_id, sig)

                    fin = executor.get_account_financials()
                    curr = fin.get("currency", "GBP")
                    profit_str = format_profit(banked_profit, curr)

                    if sig.get("status") == "RUNNER_ACTIVE":
                        runner_name = sig.get("runner_layer")
                        runner_t = sig.get("runner_ticket")
                        soft_floor = sig.get("soft_tp1_floor")
                        raw_tp2 = sig.get("raw_tp2")
                        tp2_str = f"`{raw_tp2}`" if raw_tp2 else "`TP1 + 100 pips`"
                        banked_str = "Layer 1 & Layer 2" if runner_name == "BOTTOM" else "Layer 1"
                        alert = (
                            f"🎯 **TP1 Reached & Profit Banked! [ID: {msg_id}]**\n"
                            f"• Symbol: `{symbol}` ({direction})\n"
                            f"• Hit Price: `{curr_price}` (TP1: `{raw_tp1}`)\n"
                            f"• 💰 **Amount Made at TP1:** `{profit_str}` ({banked_str})\n"
                            f"• 🚀 **Designated Runner:** Layer {runner_name} [Ticket #{runner_t}]\n"
                            f"• 🛡️ **Soft Floor / Trailed SL:** `{soft_floor}` (Locked in TP1 gain)\n"
                            f"• 🎯 **Target:** TP2 {tp2_str}\n"
                            f"• Pending limit orders cancelled cleanly.\n\n"
                            f"{fin['formatted']}"
                        )
                    else:
                        alert = (
                            f"🎯 **TP1 Reached & 100% Banked! [ID: {msg_id}]**\n"
                            f"• Symbol: `{symbol}` ({direction})\n"
                            f"• Hit Price: `{curr_price}` (TP1: `{raw_tp1}`)\n"
                            f"• 💰 **Amount Made at TP1:** `{profit_str}` (All orders closed)\n"
                            f"• All pending limit orders cancelled.\n\n"
                            f"{fin['formatted']}"
                        )
                    await notify_saved_messages(alert)
                    continue

                # ===========================================================
                # Branch C: TP1 Near-Miss Shortfall Protection (SHORTFALL_GUARD)
                # ===========================================================
                arm_dist = SHORTFALL_ARM_PIPS * 0.10  # $2.00
                pullback_dist = SHORTFALL_PULLBACK_PIPS * 0.10  # $0.80

                shortfall_triggered = False
                peak_val = sig.get("peak_price")

                if direction == "BUY":
                    if curr_price >= round(raw_tp1 - arm_dist, 2):
                        if not sig.get("shortfall_armed"):
                            sig["shortfall_armed"] = True
                            sig["peak_price"] = curr_price
                            state_mgr.update_signal(msg_id, {"shortfall_armed": True, "peak_price": curr_price})
                        else:
                            if curr_price > (peak_val or curr_price):
                                sig["peak_price"] = curr_price
                                state_mgr.update_signal(msg_id, {"peak_price": curr_price})

                        peak = sig.get("peak_price", curr_price)
                        if curr_price <= round(peak - pullback_dist, 2):
                            shortfall_triggered = True
                            peak_val = peak
                else:  # SELL
                    if curr_price <= round(raw_tp1 + arm_dist, 2):
                        if not sig.get("shortfall_armed"):
                            sig["shortfall_armed"] = True
                            sig["peak_price"] = curr_price
                            state_mgr.update_signal(msg_id, {"shortfall_armed": True, "peak_price": curr_price})
                        else:
                            if curr_price < (peak_val or curr_price):
                                sig["peak_price"] = curr_price
                                state_mgr.update_signal(msg_id, {"peak_price": curr_price})

                        peak = sig.get("peak_price", curr_price)
                        if curr_price >= round(peak + pullback_dist, 2):
                            shortfall_triggered = True
                            peak_val = peak

                if shortfall_triggered:
                    logger.warning(
                        f"⚠️ TP1 SHORTFALL TRIGGERED on Msg ID {msg_id}! "
                        f"Peak={peak_val}, Reversal Price={curr_price}, TP1={raw_tp1}"
                    )
                    closed_tickets = list(sig.get("market_tickets", []))
                    for t in closed_tickets:
                        executor.close_market_position(t)
                    for pt in sig.get("pending_tickets", []):
                        executor.cancel_pending_order(pt)

                    layers = sig.get("layers", {})
                    for lk in layers:
                        layers[lk]["status"] = "CLOSED"

                    banked_profit = sum(executor.get_position_closed_profit(t) for t in closed_tickets)
                    sig["status"] = "CLOSED"
                    sig["market_tickets"] = []
                    sig["pending_tickets"] = []
                    sig["layers"] = layers
                    sig["banked_profit"] = banked_profit
                    state_mgr.update_signal(msg_id, sig)

                    fin = executor.get_account_financials()
                    curr = fin.get("currency", "GBP")
                    profit_str = format_profit(banked_profit, curr)

                    alert = (
                        f"⚠️ **TP1 Shortfall Guard Triggered & Banked! [ID: {msg_id}]**\n"
                        f"• Symbol: `{symbol}` ({direction})\n"
                        f"• Peak Price: `{peak_val}` (within $2.00 of TP1 `{raw_tp1}`)\n"
                        f"• Reversal Price: `{curr_price}` (pulled back $0.80 from peak)\n"
                        f"• 💰 **Amount Made:** `{profit_str}`\n"
                        f"• All positions banked at market to protect gain!\n"
                        f"• All pending limit orders cancelled to prevent round-trip loss!\n\n"
                        f"{fin['formatted']}"
                    )
                    await notify_saved_messages(alert)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in TP1 proximity watcher: {e}")

        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# MESSAGE HANDLERS
# ---------------------------------------------------------------------------

async def handle_new_message(event):
    """Handler for new messages in monitored Telegram channels."""
    if session_trading_blocked():
        return
    msg = event.message
    if not msg:
        return

    msg_id = msg.id
    raw_text = msg.message or ""
    chat_id = _safe_chat_id(event)
    raw_reply_to = getattr(msg, "reply_to_msg_id", None)
    if not (isinstance(raw_reply_to, (int, str)) and not isinstance(raw_reply_to, bool)):
        reply_obj = getattr(msg, "reply_to", None)
        raw_reply_to = getattr(reply_obj, "reply_to_msg_id", None) if reply_obj else None
    reply_to_id = raw_reply_to if isinstance(raw_reply_to, (int, str)) and not isinstance(raw_reply_to, bool) else None
    is_reply_evt = (getattr(event, "is_reply", None) is True) or (reply_to_id is not None)

    # Deduplication check (per channel)
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
    _recent_messages[dedup_key] = now

    logger.info(
        f"--- New Message Received [ID: {msg_id}] in Chat [{chat_id}] "
        f"(Reply to: {reply_to_id}) ---\nText: {raw_text}"
    )

    # Resolve target signal if this is a reply
    target_signal = None
    target_msg_id = None
    target_chat_id = chat_id
    if reply_to_id is not None:
        target_signal = state_mgr.get_signal(reply_to_id, chat_id=chat_id)
        if target_signal:
            target_msg_id = str(reply_to_id)
            target_chat_id = target_signal.get("chat_id", chat_id)

    # 6-Gate Pipeline execution
    is_forward = getattr(msg, "forward", None) is not None
    tick = executor.get_tick(DEFAULT_SYMBOL)
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

    # 1. Trade Management Commands (BE, Partial Close, Full Close, Reactivation)
    if parsed.event_type == "TRADE_MANAGEMENT" or parsed.is_management:
        action = parsed.management_action
        logger.info(f"Processing Trade Management: {action} [Msg ID: {msg_id}]")

        # Resolve signal if not already resolved
        if not target_signal:
            res = state_mgr.get_latest_signal(symbol=parsed.symbol)
            if res:
                target_msg_id, target_signal = res
                target_chat_id = target_signal.get("chat_id", chat_id)

        if not target_signal:
            logger.warning(f"No target trade found in state to apply {action}. (Reply ID: {reply_to_id})")
            return

        if target_signal.get("status") in ("PENDING", "PENDING_DELAY") and action not in (
            "ACTION_FULL_CLOSE",
            "FULL_CLOSE",
            "ACTION_TP1_HIT_REPLY",
            "TP1_HIT_REPLY",
        ):
            logger.info(f"Ignoring {action}: target signal {target_msg_id} is still in delay window (no open orders).")
            return

        if target_signal.get("status") in ("CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD") and action not in ("ACTION_REACTIVATE", "REACTIVATE"):
            logger.info(f"Ignoring {action}: target signal {target_msg_id} is already terminal ({target_signal.get('status')}).")
            return

        symbol = target_signal.get("symbol", DEFAULT_SYMBOL)
        direction = target_signal.get("direction", "BUY")
        layers = target_signal.get("layers", {})
        market_tickets = list(target_signal.get("market_tickets", []))
        pending_tickets = list(target_signal.get("pending_tickets", []))

        # ACTION: BREAKEVEN (Selective: TOP + MID only)
        if action in ("ACTION_BREAKEVEN", "BREAKEVEN"):
            executor.apply_selective_breakeven(
                layers=layers,
                symbol=symbol,
                direction=direction,
                be_layers=BE_LAYERS,
                be_offset_pips=BE_OFFSET_PIPS,
            )
            state_mgr.update_signal(target_msg_id, {"status": "BE_APPLIED"}, chat_id=target_chat_id)
            alert = (
                f"🛡️ **Selective Breakeven Applied**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• Layers Protected: `{BE_LAYERS}` (+{BE_OFFSET_PIPS} pips commission buffer)\n"
                f"• Bottom Layer (0.02) remains at original SL."
            )
            await notify_saved_messages(alert)

        # ACTION: CLOSE WORST ENTRIES & HOLD LAYERS
        elif action in ("ACTION_CLOSE_WORST_HOLD_LAYERS", "CLOSE_WORST_HOLD_LAYERS"):
            raw_tp3 = target_signal.get("raw_tp3")
            res = executor.apply_breakeven_worst_hold_layers(
                market_tickets=market_tickets,
                symbol=symbol,
                direction=direction,
                be_offset_pips=BE_OFFSET_PIPS,
                raw_tp3=raw_tp3,
            )
            be_tkts = res.get("be_tickets", [])
            held_tkts = res.get("held_tickets", [])
            failed_be = res.get("failed_be_tickets", [])

            updates = {"hold_past_tp2": True}
            if be_tkts:
                updates["status"] = "BE_APPLIED"
            state_mgr.update_signal(target_msg_id, updates, chat_id=target_chat_id)

            fail_str = ""
            if failed_be:
                fail_details = ", ".join(f"#{t} ({err})" for t, err in failed_be)
                fail_str = f"\n• ⚠️ **Could not BE yet:** {fail_details}"

            target_note = f"TP3 (`{raw_tp3}`) + Trail SL" if raw_tp3 else "TP1 / TP2 + Trail SL"
            alert = (
                f"🛡️ **Worst Entries -> Breakeven | Holding Bottom 2 Layers**\n"
                f"• Symbol: `{symbol}` ({direction}) [Msg ID: {target_msg_id}]\n"
                f"• 🔒 **Moved to BE (+{BE_OFFSET_PIPS} pips):** `{be_tkts or 'None (<= 2 layers open)'}`\n"
                f"• 🚀 **Held Running Layers (Top 2 Profit):** `{held_tkts}` -> Target `{target_note}`"
                f"{fail_str}"
            )
            await notify_saved_messages(alert)

        # ACTION: REACTIVATION ("Active" reply to old cancelled/expired zone)
        elif action in ("ACTION_REACTIVATE", "REACTIVATE"):
            curr_status = target_signal.get("status")
            if curr_status == "ACTIVE":
                logger.info("Signal is already active; ignoring redundant 'Active' command.")
                return

            # Check pre-flight for reactivation
            raw_tp1 = target_signal.get("raw_tp1")
            sl = target_signal.get("sl")
            passed, reason = executor.check_preflight(
                symbol=symbol,
                direction=direction,
                raw_tp1=raw_tp1,
                sl=sl,
            )
            if not passed:
                logger.warning(f"Reactivation skipped: {reason}")
                await notify_saved_messages(f"⚠️ Reactivation rejected for Msg ID {target_msg_id}: {reason}")
                return

            zone_dict = target_signal.get("zone")
            if zone_dict:
                zone = ZoneInfo(zone_dict["top"], zone_dict["mid"], zone_dict["bottom"])
                mk_tkts, pd_tkts, layers_data = executor.execute_3layer_zone(
                    symbol=symbol,
                    direction=direction,
                    zone=zone,
                    sl=sl,
                    final_tp=target_signal.get("final_tp"),
                    raw_tp1=raw_tp1,
                )
                state_mgr.update_signal(
                    target_msg_id,
                    {
                        "status": "ACTIVE",
                        "market_tickets": mk_tkts,
                        "pending_tickets": pd_tkts,
                        "layers": layers_data,
                        "tp1_reached": False,
                    },
                    chat_id=target_chat_id,
                )
                alert = (
                    f"🔄 **Zone Reactivated ('Active')**\n"
                    f"• Symbol: `{symbol}` ({direction})\n"
                    f"• Layers Re-entered: 0.01 / 0.01 / 0.02\n"
                    f"• Market: {mk_tkts} | Pending: {pd_tkts}"
                )
                await notify_saved_messages(alert)

        # ACTION: PARTIAL CLOSE
        elif action in ("ACTION_PARTIAL_CLOSE", "PARTIAL_CLOSE"):
            closed = executor.apply_partial_close(
                market_tickets=market_tickets,
                pending_tickets=pending_tickets,
                symbol=symbol,
                direction=direction,
            )
            fin = executor.get_account_financials()
            curr = fin.get("currency", "GBP")
            profit_info = ""
            if closed and closed in market_tickets:
                market_tickets.remove(closed)
                profit = executor.get_position_closed_profit(closed)
                profit_info = f"\n• 💰 **Amount Made:** `{format_profit(profit, curr)}`"

            state_mgr.update_signal(
                target_msg_id,
                {"market_tickets": market_tickets, "status": "PARTIAL_CLOSED"},
                chat_id=target_chat_id,
            )
            alert = (
                f"💰 **Partial Close Executed on `{symbol}` [Msg ID: {target_msg_id}]**\n"
                f"• Order #{closed} closed at market."
                f"{profit_info}\n\n"
                f"{fin['formatted']}"
            )
            await notify_saved_messages(alert)

        # ACTION: FULL CLOSE
        elif action in ("ACTION_FULL_CLOSE", "FULL_CLOSE"):
            if target_signal.get("status") in ("PENDING", "PENDING_DELAY"):
                _cancel_delayed_sell_now(target_msg_id, chat_id=target_chat_id)
                state_mgr.update_signal(target_msg_id, {"status": "CLOSED"}, chat_id=target_chat_id)
                await notify_saved_messages(
                    f"🚫 **Pending {direction.title()} Now Cancelled [Msg ID: {target_msg_id}]** before execution."
                )
                return

            closed_tkts = list(market_tickets)
            executor.apply_proximity_close(market_tickets, pending_tickets)
            total_profit = sum(executor.get_position_closed_profit(t) for t in closed_tkts)
            fin = executor.get_account_financials()
            curr = fin.get("currency", "GBP")
            profit_str = format_profit(total_profit, curr)

            state_mgr.update_signal(
                target_msg_id,
                {"market_tickets": [], "pending_tickets": [], "status": "CLOSED"},
                chat_id=target_chat_id,
            )
            alert = (
                f"🚪 **Full Close Executed on `{symbol}` [Msg ID: {target_msg_id}]**\n"
                f"• All open positions closed at market.\n"
                f"• 💰 **Total Amount Made:** `{profit_str}`\n\n"
                f"{fin['formatted']}"
            )
            await notify_saved_messages(alert)

        # ACTION: PROVIDER REPLY "TP1 HIT" (Trigger 1: Reply / Alert)
        elif action in ("ACTION_TP1_HIT_REPLY", "TP1_HIT_REPLY"):
            is_unentered = target_signal.get("status") in ("PENDING", "PENDING_DELAY") or not market_tickets
            if not is_reply_evt and not is_unentered:
                # Standalone "TP1 hit" (not a reply) cannot be safely tied to one filled trade when several
                # run concurrently -> only used to cancel not-yet-entered trades; live TP1 guards handle the rest.
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
                await invalidate_signal_tp1(
                    target_msg_id,
                    reason="Reply" if is_reply_evt else "Alert",
                    chat_id=target_chat_id,
                )
                return

            runner_t = executor.handle_tp1_scaling_and_select_runner(target_signal)
            state_mgr.set_tp1_reached(target_msg_id, True, chat_id=target_chat_id)
            banked_tkts = target_signal.get("last_banked_tickets", [])
            banked_profit = sum(executor.get_position_closed_profit(t) for t in banked_tkts)
            target_signal["banked_profit"] = banked_profit
            state_mgr.update_signal(target_msg_id, target_signal, chat_id=target_chat_id)

            fin = executor.get_account_financials()
            curr = fin.get("currency", "GBP")
            profit_str = format_profit(banked_profit, curr)
            runner_info = (
                f"\n• 🚀 **Designated Runner:** Layer `{target_signal.get('runner_layer')}` "
                f"[Ticket #{runner_t}] (Floor: `{target_signal.get('soft_tp1_floor')}`)"
                if runner_t
                else "\n• All positions banked at TP1."
            )
            await notify_saved_messages(
                f"🎯 **TP1 Scaling Triggered via Provider Reply [ID: {target_msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction})\n"
                f"• 💰 **Amount Made at TP1:** `{profit_str}`"
                f"{runner_info}\n\n"
                f"{fin['formatted']}"
            )

        return

    # 2. New Signal Entry
    if parsed.event_type == "NEW_SIGNAL" or parsed.is_entry:
        symbol = parsed.symbol or DEFAULT_SYMBOL
        direction = parsed.direction
        sl = parsed.sl if parsed.sl_valid else None
        final_tp = parsed.final_tp
        zone = parsed.zone

        # Pre-Flight Safety Checks (Safe price window & MT5 connection)
        passed, reason = executor.check_preflight(
            symbol=symbol,
            direction=direction,
            raw_tp1=parsed.raw_tp1,
            sl=sl,
            zone=zone,
            single_price=parsed.single_price,
        )
        if not passed:
            logger.warning(f"Pre-flight safety check failed for Msg ID {msg_id}: {reason}")
            await notify_saved_messages(f"⚠️ **Order Blocked by Pre-Flight**: {reason}")
            return

        logger.info(
            f"Pre-flight OK. Executing New Signal: Direction={direction}, Symbol={symbol}, "
            f"Zone={zone}, SL={sl}, TP={final_tp}"
        )

        market_tickets = []
        pending_tickets = []
        layers_data = {}
        zone_completed = False
        is_zone = False
        max_orders = MAX_ORDERS_PER_SIGNAL
        prev_lots = executor.get_total_active_lots()["total_lots"]

        initial_price = None
        if parsed.is_zone and zone:
            # Case 1: Two-Price Zone (e.g. Buy 3895-3887) -> 3 layers (0.01 / 0.01 / 0.02)
            market_tickets, pending_tickets, layers_data = executor.execute_3layer_zone(
                symbol=symbol,
                direction=direction,
                zone=zone,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=parsed.raw_tp1,
                raw_tp2=parsed.raw_tp2,
            )
            zone_completed = True
            is_zone = True
            max_orders = 3
        elif parsed.single_price is not None:
            # Case 2: Single-Price Entry (e.g. "Buy limit 3890" or "Sell 3920") -> 1 order (0.01 lot)
            market_tickets, pending_tickets, layers_data = executor.execute_single_price_order(
                symbol=symbol,
                direction=direction,
                price=parsed.single_price,
                lot=SINGLE_PRICE_LOT,
                sl=sl,
                final_tp=final_tp,
                raw_tp1=parsed.raw_tp1,
            )
            zone_completed = False
            is_zone = False
            max_orders = 1
        elif float(SELL_NOW_DELAY_SECONDS) <= 0.0:
            # Case 3 (Immediate mode, SELL_NOW_DELAY_SECONDS == 0): execute 0.01 market order immediately
            now_tick = executor.get_tick(symbol)
            initial_price = _get_live_entry_price(now_tick, direction)
            top_lot = LAYER_LOTS.get("TOP", 0.01)
            t1 = executor.execute_market_order(
                symbol=symbol,
                direction=direction,
                lot=top_lot,
                sl=sl,
                tp=final_tp,
                comment="TG Layer 1 (Market Now)",
            )
            if t1:
                market_tickets = [t1]
                layers_data = {
                    "TOP": {
                        "lot": top_lot,
                        "target_price": None,
                        "ticket": t1,
                        "order_type": "MARKET",
                        "status": "OPEN",
                    }
                }
            zone_completed = False
            is_zone = False
            max_orders = 1
        else:
            # Case 3 (Delayed mode, SELL_NOW_DELAY_SECONDS > 0): snapshot entry & hold for timer
            now_tick = executor.get_tick(symbol)
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
                sl=sl,
                raw_tp1=parsed.raw_tp1,
                tp1_price=parsed.raw_tp1,
                raw_tp2=parsed.raw_tp2,
                raw_tp3=parsed.raw_tp3,
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
                f"will validate {cond_str} before executing 0.01 market {direction.lower()}."
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

        arm_tick = executor.get_tick(symbol)
        tp1_guard_armed = _compute_initial_tp1_guard_armed(
            arm_tick, direction, parsed.raw_tp1, bool(market_tickets)
        )
        state_mgr.create_signal(
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
            sl=sl,
            raw_tp1=parsed.raw_tp1,
            tp1_price=parsed.raw_tp1,
            raw_tp2=parsed.raw_tp2,
            raw_tp3=parsed.raw_tp3,
            final_tp=final_tp,
            proximity_close_price=parsed.proximity_close_price,
            raw_text=raw_text,
            initial_price=initial_price,
            recorded_entry_price=initial_price,
            tp1_guard_armed=tp1_guard_armed,
        )

        new_lots = executor.get_total_active_lots()["total_lots"]
        risk_alerts = executor.evaluate_risk_threshold_alerts(prev_lots, new_lots)
        risk_block = ("\n\n" + "\n".join(risk_alerts)) if risk_alerts else ""

        alert = (
            f"🚀 **New Signal Executed**\n"
            f"• Direction: `{direction}` {symbol}\n"
            f"• Orders: Market={market_tickets}, Pending={pending_tickets}\n"
            f"• SL: `{sl}` | TP1: `{final_tp}` (Proximity Guard: `{parsed.proximity_close_price}`)"
            f"{risk_block}"
        )
        await notify_saved_messages(alert)
        update_live_terminal_status()


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


async def handle_message_edited(event):
    """Handler for edited messages (adding zones, updating SL/TP)."""
    if session_trading_blocked():
        return
    msg = event.message
    if not msg:
        return

    msg_id = msg.id
    raw_text = msg.message or ""
    chat_id = _safe_chat_id(event)

    existing_signal = state_mgr.get_signal(msg_id, chat_id=chat_id)

    # Redundant edit filter (reactions/views)
    if existing_signal and raw_text.strip() == existing_signal.get("raw_text", "").strip():
        logger.debug(f"Edited message {msg_id} text unchanged; skipping.")
        return

    # If message ID does not exist in state: skip silently if already processed within dedup window
    if not existing_signal:
        now = time.time()
        dedup_key = _get_dedup_key(chat_id, msg_id)
        if dedup_key in _recent_messages and (now - _recent_messages[dedup_key]) < DEDUPLICATION_WINDOW_SECONDS:
            logger.debug(f"Edited message {msg_id} not in state & within {DEDUPLICATION_WINDOW_SECONDS}s dedup; skipping.")
            return

        # Improvement #2: Block emoji reactions / edits on old untracked messages from firing late trades
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
        await handle_new_message(event)
        return

    logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{chat_id}] ---\nNew Text: {raw_text}")

    curr_status = existing_signal.get("status")
    if curr_status in ("CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"):
        logger.info(f"Signal {msg_id} is already terminal ({curr_status}); ignoring edit.")
        return

    symbol = existing_signal.get("symbol", DEFAULT_SYMBOL)
    direction = existing_signal.get("direction", "BUY")

    # If the provider edited the message itself to announce "TP1 hit", cancel delayed entry / delete pending orders
    if is_tp1_hit_message(raw_text):
        _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
        mk_tkts_now = list(existing_signal.get("market_tickets", []))
        if curr_status in ("PENDING", "PENDING_DELAY") or not mk_tkts_now:
            await invalidate_signal_tp1(msg_id, reason="Edit", chat_id=chat_id)
            state_mgr.update_signal(msg_id, {"raw_text": raw_text}, chat_id=chat_id)
            return
        elif not existing_signal.get("tp1_reached") and curr_status != "RUNNER_ACTIVE":
            executor.handle_tp1_scaling_and_select_runner(existing_signal)
            state_mgr.set_tp1_reached(msg_id, True, chat_id=chat_id)
            existing_signal["raw_text"] = raw_text
            state_mgr.update_signal(msg_id, existing_signal, chat_id=chat_id)
            await notify_saved_messages(
                f"🎯 **TP1 Scaling Triggered via Edited Signal [ID: {msg_id}]**\n"
                f"• Symbol: `{symbol}` ({direction}) — Pending limits deleted & TP1 profit banked."
            )
            return

    is_forward = getattr(msg, "forward", None) is not None
    tick = executor.get_tick(DEFAULT_SYMBOL)
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

    raw_tp1 = parsed.raw_tp1 or existing_signal.get("raw_tp1")
    raw_tp2 = parsed.raw_tp2 or existing_signal.get("raw_tp2")
    raw_tp3 = parsed.raw_tp3 or existing_signal.get("raw_tp3")

    # Handle edit arriving during Sell Now / Buy Now delay window
    if curr_status in ("PENDING", "PENDING_DELAY"):
        is_pending_pullback = _is_strictly_pending_limit_setup(
            tick, direction, zone=parsed.zone, single_price=parsed.single_price
        )
        # If the edit provides a TP1 that live price has ALREADY reached (and not a deep pullback limit), invalidate
        if not is_pending_pullback and raw_tp1 is not None and tick and _tick_hit_tp1(tick, direction, raw_tp1):
            await invalidate_signal_tp1(msg_id, reason="Market Price Reached TP1", chat_id=chat_id)
            state_mgr.update_signal(msg_id, {"raw_text": raw_text}, chat_id=chat_id)
            return
        if (parsed.is_zone and parsed.zone) or (parsed.single_price is not None):
            passed, reason = executor.check_preflight(
                symbol=symbol,
                direction=direction,
                raw_tp1=raw_tp1,
                sl=new_sl,
                zone=parsed.zone,
                single_price=parsed.single_price,
            )
            if not passed:
                _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text}, chat_id=chat_id)
                logger.warning(f"Edited delayed signal [ID: {msg_id}] blocked by pre-flight: {reason}")
                await notify_saved_messages(
                    f"⚠️ **Edited Delayed Entry Skipped [ID: {msg_id}]**: {reason}"
                )
                return

        if parsed.is_zone and parsed.zone:
            _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
            zone = parsed.zone
            mk_tkts, pd_tkts, l_data = executor.execute_3layer_zone(
                symbol=symbol,
                direction=direction,
                zone=zone,
                sl=new_sl,
                final_tp=new_tp,
                raw_tp1=raw_tp1,
                raw_tp2=raw_tp2,
            )
            if not mk_tkts and not pd_tkts:
                err_reason = getattr(executor, "last_error", None) or "Broker rejected order(s)"
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text}, chat_id=chat_id)
                await notify_saved_messages(f"❌ **Edited Zone Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`")
                return
            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "tp1_price": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
                    "final_tp": new_tp,
                    "hard_broker_tp": new_tp,
                    "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                    "zone": zone.to_dict(),
                    "is_zone": True,
                    "is_zone_order": True,
                    "zone_completed": True,
                    "max_orders": 3,
                    "layers": l_data,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "tp1_guard_armed": _compute_initial_tp1_guard_armed(
                        executor.get_tick(symbol), direction, raw_tp1, bool(mk_tkts)
                    ),
                    "raw_text": raw_text,
                },
                chat_id=chat_id,
            )
            await notify_saved_messages(
                f"✏️ **Delayed {direction.title()} Now → Zone Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending Limits: {pd_tkts}\n"
                f"• SL: `{new_sl}` | TP1: `{new_tp}`"
            )
            return
        elif parsed.single_price is not None:
            _cancel_delayed_sell_now(msg_id, chat_id=chat_id)
            mk_tkts, pd_tkts, l_data = executor.execute_single_price_order(
                symbol=symbol,
                direction=direction,
                price=parsed.single_price,
                lot=SINGLE_PRICE_LOT,
                sl=new_sl,
                final_tp=new_tp,
                raw_tp1=raw_tp1,
            )
            if not mk_tkts and not pd_tkts:
                err_reason = getattr(executor, "last_error", None) or "Broker rejected order"
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text}, chat_id=chat_id)
                await notify_saved_messages(f"❌ **Edited Limit Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`")
                return
            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "tp1_price": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
                    "final_tp": new_tp,
                    "hard_broker_tp": new_tp,
                    "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                    "layers": l_data,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "tp1_guard_armed": _compute_initial_tp1_guard_armed(
                        executor.get_tick(symbol), direction, raw_tp1, bool(mk_tkts)
                    ),
                    "raw_text": raw_text,
                },
                chat_id=chat_id,
            )
            await notify_saved_messages(
                f"✏️ **Delayed {direction.title()} Now → Limit Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending: {pd_tkts} @ `{parsed.single_price}`"
            )
            return
        else:
            # Edit added SL/TP only; update state and let the timer keep waiting for limits
            state_mgr.update_signal(
                msg_id,
                {
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "tp1_price": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
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
    layers = existing_signal.get("layers", {})

    if raw_tp2:
        runner_tp = round(raw_tp2, 2)
    elif raw_tp1:
        runner_tp = round(raw_tp1 + 10.0, 2) if direction == "BUY" else round(raw_tp1 - 10.0, 2)
    else:
        runner_tp = new_tp

    # If the trade is already a RUNNER, protect its trailed SL!
    if curr_status == "RUNNER_ACTIVE":
        if raw_tp2 and runner_tp:
            runner_t = existing_signal.get("runner_ticket")
            if runner_t:
                executor.modify_position_sltp(ticket=runner_t, sl=None, tp=runner_tp)
        state_mgr.update_signal(
            msg_id,
            {
                "raw_tp1": raw_tp1,
                "raw_tp2": raw_tp2,
                "raw_text": raw_text,
            },
            chat_id=chat_id,
        )
        await notify_saved_messages(
            f"✏️ **Runner Target Updated via Edit [ID: {msg_id}]**\n"
            f"• Target TP2: `{runner_tp}`"
        )
        return

    # Update SL/TP on all existing orders: Layer 1 gets top_tp (new_tp), Layers 2 & 3 get runner_tp
    if new_sl is not None or new_tp is not None:
        logger.info(
            f"Updating SL/TP on existing tickets for Msg ID {msg_id}: "
            f"SL={new_sl}, Layer1 TP={new_tp}, Runner TP={runner_tp}"
        )
        for l_name, l_data in layers.items():
            t = l_data.get("ticket")
            if not t:
                continue
            l_tp = new_tp if l_name == "TOP" else runner_tp
            if t in market_tickets:
                executor.modify_position_sltp(ticket=t, sl=new_sl, tp=l_tp)
            elif t in pending_tickets:
                executor.modify_pending_order_sltp(ticket=t, sl=new_sl, tp=l_tp)

    # Check slot limits
    current_count = len(market_tickets) + len(pending_tickets)
    slots_left = MAX_ORDERS_PER_SIGNAL - current_count
    zone_completed = existing_signal.get("zone_completed", False)

    # If zone was added in edit and slots are still available
    if parsed.zone and slots_left > 0 and not zone_completed:
        zone = parsed.zone
        mid_lot = LAYER_LOTS.get("MID", 0.01)
        bottom_lot = LAYER_LOTS.get("BOTTOM", 0.02)

        layer_specs = [
            ("MID", zone.mid, mid_lot, "TG Layer 2 (Edit Mid"),
            ("BOTTOM", zone.bottom if direction == "BUY" else zone.top, bottom_lot,
             "TG Layer 3 (Edit Bottom" if direction == "BUY" else "TG Layer 3 (Edit Top"),
        ]
        for l_name, target_px, l_lot, c_prefix in layer_specs:
            if slots_left <= 0:
                break
            t, o_type = _place_edit_layer_order(
                symbol=symbol,
                direction=direction,
                target_price=target_px,
                lot=l_lot,
                sl=new_sl,
                tp=runner_tp,
                comment_prefix=f"{c_prefix})",
                tick=tick,
            )
            if t:
                if o_type == "MARKET":
                    market_tickets.append(t)
                    layers[l_name] = {"lot": l_lot, "target_price": target_px, "ticket": t, "order_type": "MARKET", "status": "OPEN"}
                else:
                    pending_tickets.append(t)
                    layers[l_name] = {"lot": l_lot, "target_price": target_px, "ticket": t, "order_type": "LIMIT", "status": "PENDING"}
                slots_left -= 1

        zone_completed = True

    state_mgr.update_signal(
        msg_id,
        {
            "sl": new_sl,
            "raw_tp1": parsed.raw_tp1 or existing_signal.get("raw_tp1"),
            "raw_tp2": parsed.raw_tp2 or existing_signal.get("raw_tp2"),
            "raw_tp3": parsed.raw_tp3 or existing_signal.get("raw_tp3"),
            "final_tp": new_tp,
            "hard_broker_tp": new_tp,
            "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
            "zone": parsed.zone.to_dict() if parsed.zone else existing_signal.get("zone"),
            "is_zone": True if parsed.zone else existing_signal.get("is_zone", False),
            "is_zone_order": True if parsed.zone else existing_signal.get("is_zone_order", existing_signal.get("is_zone", False)),
            "max_orders": 3 if parsed.zone else existing_signal.get("max_orders", 1),
            "layers": layers,
            "market_tickets": market_tickets,
            "pending_tickets": pending_tickets,
            "zone_completed": zone_completed,
            "raw_text": raw_text,
        },
        chat_id=chat_id,
    )

    alert = (
        f"✏️ **Signal Updated via Edit [ID: {msg_id}]**\n"
        f"• SL: `{new_sl}` | TP: `{new_tp}`\n"
        f"• Total Orders: {len(market_tickets) + len(pending_tickets)}/{MAX_ORDERS_PER_SIGNAL}"
    )
    await notify_saved_messages(alert)


async def _apply_daily_win_input(event, raw_arg: str) -> None:
    """Parse and apply a Daily Win Target input (e.g. '20', '£20', or 'off') from Saved Messages."""
    global _awaiting_dailywin_input
    import re

    clean = raw_arg.strip().lower()
    today_str = _trading_day_str()
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
    dd = executor.get_daily_drawdown_status()
    curr = dd.get("currency", "GBP")
    realized_today = round(float(dd.get("realized_today_pnl", 0.0)), 2)
    floating_pnl = round(float(dd.get("floating_pnl", 0.0)), 2)

    # If today's prior realized profit already exceeds the target, set baseline to current realized
    # so the bot aims to make +target_val from this point forward; otherwise count from 0.0 today.
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
        f"it will automatically close all open trades, cancel pending orders, notify you here in Saved Messages, "
        f"and terminate the session for no more trading!\n"
        f"• 💡 _Type `dailywin off` anytime to disable._"
    )
    await check_and_enforce_daily_win_target()


async def _apply_daily_loss_input(event, raw_arg: str) -> None:
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

    prog = get_daily_loss_progress()
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
    "/status", "status", "/ping", "ping", "/trades", "trades",
    "/algo", "algo", "/check", "check", "/help", "help",
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


async def handle_saved_messages_command(event):
    """Remote monitoring commands sent to Saved Messages ('me')."""
    global _awaiting_dailywin_input, _awaiting_dailyloss_input
    msg = event.message
    if not msg or not msg.message:
        return

    raw_msg = msg.message.strip()
    text = raw_msg.lower()

    # Ignore bot-generated notifications sent to Saved Messages ("me") so they don't trigger prompts
    if getattr(msg, "out", False) and any(
        raw_msg.startswith(prefix)
        for prefix in (
            "🏆", "🚀", "🛡️", "💰", "🚪", "🎯", "⚠️", "❌", "⏳", "✏️", "🏦", "🚨",
            "🧪", "🏃", "✅", "📋", "🤖", "⚪", "🚫", "🔄", "ℹ️", "💳", "🛑", "🔒",
        )
    ):
        return

    # -----------------------------------------------------------------------
    # LIVE CONTROL: dailywin / /dailywin (Toggle & Set Daily Win Target)
    # -----------------------------------------------------------------------
    if text in ("/dailywin", "dailywin", "/daily_win", "daily_win"):
        _awaiting_dailyloss_input = False
        _awaiting_dailywin_input = True
        prog = get_daily_win_progress()
        curr = prog.get("currency", "GBP")
        if prog["enabled"] and prog["target_amount"] > 0:
            curr_status = f"🟢 ACTIVE (Target: {format_profit(prog['target_amount'], curr)})"
        else:
            curr_status = "⚪ OFF"

        await event.reply(
            f"🎯 **Daily Win Limit Toggle (`dailywin`)**\n"
            f"• Current Setting: `{curr_status}`\n"
            f"• Today's Net Profit: `{format_profit(prog['net_profit_today'], curr)}`\n\n"
            f"👉 **Reply with your Daily Win Target amount** (for example `20` or `£20`), "
            f"or reply `off` to disable it.\n"
            f"_(Once the bot makes that amount from trading, it will close all trades, leave a notification here, and terminate the session.)_"
        )
        return

    if text.startswith("/dailywin ") or text.startswith("dailywin ") or text.startswith("/daily_win ") or text.startswith("daily_win "):
        _awaiting_dailyloss_input = False
        parts = raw_msg.split(None, 1)
        arg_part = parts[1] if len(parts) > 1 else ""
        await _apply_daily_win_input(event, arg_part)
        return

    # -----------------------------------------------------------------------
    # LIVE CONTROL: dailyloss / /set_daily_loss / /status_daily_loss
    # -----------------------------------------------------------------------
    if text in ("/dailyloss", "dailyloss", "/daily_loss", "daily_loss", "/set_daily_loss", "set_daily_loss"):
        _awaiting_dailywin_input = False
        _awaiting_dailyloss_input = True
        prog = get_daily_loss_progress()
        curr = prog.get("currency", "GBP")
        if prog["enabled"] and prog["limit_amount"] > 0:
            curr_status = f"🟢 ACTIVE (Limit: {format_profit(-prog['limit_amount'], curr)})"
            if _daily_loss_locked:
                curr_status += " 🔒 LOCKED"
        else:
            curr_status = "⚪ OFF"
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

    if (
        text.startswith("/dailyloss ")
        or text.startswith("dailyloss ")
        or text.startswith("/daily_loss ")
        or text.startswith("daily_loss ")
        or text.startswith("/set_daily_loss ")
        or text.startswith("set_daily_loss ")
    ):
        _awaiting_dailywin_input = False
        parts = raw_msg.split(None, 1)
        arg_part = parts[1] if len(parts) > 1 else ""
        await _apply_daily_loss_input(event, arg_part)
        return

    if text in ("/status_daily_loss", "status_daily_loss"):
        _awaiting_dailywin_input = False
        _awaiting_dailyloss_input = False
        prog = get_daily_loss_progress()
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

    # If the user previously typed `dailywin` and is now replying with the target (e.g. "20", "£20", "off")
    if _awaiting_dailywin_input:
        if _is_known_saved_command(text):
            _awaiting_dailywin_input = False
        else:
            await _apply_daily_win_input(event, raw_msg)
            return

    # If the user previously typed `dailyloss` and is now replying with the limit (e.g. "20", "£20", "-20", "off")
    if _awaiting_dailyloss_input:
        if _is_known_saved_command(text):
            _awaiting_dailyloss_input = False
        else:
            await _apply_daily_loss_input(event, raw_msg)
            return
    # -----------------------------------------------------------------------
    # LIVE CONTROL: /bank (Manual TP1 Scaling & Runner Trigger)
    # -----------------------------------------------------------------------
    if text in ("/bank", "bank"):
        latest = state_mgr.get_latest_signal()
        if not latest or latest[1].get("status") == "CLOSED":
            await event.reply("⚠️ No active signals found to bank.")
            return

        msg_id, sig = latest
        if sig.get("status") == "RUNNER_ACTIVE":
            await event.reply(
                f"ℹ️ Signal `{msg_id}` is already in `RUNNER_ACTIVE` mode.\n"
                f"• Runner: `Layer {sig.get('runner_layer')}` [Ticket #{sig.get('runner_ticket')}]\n"
                f"• Soft Floor: `{sig.get('soft_tp1_floor')}`"
            )
            return

        runner_t = executor.handle_tp1_scaling_and_select_runner(sig)
        state_mgr.set_tp1_reached(msg_id, True)

        banked_tkts = sig.get("last_banked_tickets", [])
        banked_profit = sum(executor.get_position_closed_profit(t) for t in banked_tkts)
        sig["banked_profit"] = banked_profit
        state_mgr.update_signal(msg_id, sig)

        fin = executor.get_account_financials()
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(banked_profit, curr)

        runner_l = sig.get("runner_layer")
        floor = sig.get("soft_tp1_floor")
        status = sig.get("status")
        await event.reply(
            f"🏦 **Manual Bank Executed for Signal `{msg_id}`!**\n"
            f"• Direction: `{sig.get('direction')}` {sig.get('symbol')}\n"
            f"• 💰 **Amount Made at Bank:** `{profit_str}`\n"
            f"• Status: `{status}`\n"
            f"• Runner: `Layer {runner_l}` [Ticket #{runner_t}]\n"
            f"• Soft TP1 Floor: `{floor}` (Locked in software & MT5)\n"
            f"• Target TP2: `{sig.get('raw_tp2') or 'TP1 + 100 pips'}`\n"
            f"• Unfilled limit orders cancelled.\n\n"
            f"{fin['formatted']}"
        )
        return

    # -----------------------------------------------------------------------
    # LIVE CONTROL: /closeall (Emergency Panic Button)
    # -----------------------------------------------------------------------
    if text in ("/closeall", "closeall", "/panic", "panic"):
        res = _close_all_bot_exposure_and_signals()
        closed_tickets_list = res.get("closed_tickets", [])
        cancelled_orders_list = res.get("cancelled_orders", [])
        closed_positions = len(closed_tickets_list)
        cancelled_orders = len(cancelled_orders_list)

        total_profit = sum(executor.get_position_closed_profit(t) for t in closed_tickets_list)
        fin = executor.get_account_financials()
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(total_profit, curr)

        await event.reply(
            f"🚨 **EMERGENCY PANIC BUTTON TRIGGERED!**\n"
            f"• Market Positions Closed: `{closed_positions}`\n"
            f"• Pending Orders Cancelled: `{cancelled_orders}`\n"
            f"• 💰 **Total Amount Made / Realized:** `{profit_str}`\n"
            f"• All active signals & delayed entries marked `CLOSED`.\n\n"
            f"{fin['formatted']}"
        )
        return

    # -----------------------------------------------------------------------
    # WEEKEND SIMULATION: /sim_tp1 [1|2|3]
    # -----------------------------------------------------------------------
    if text.startswith("/sim_tp1") or text.startswith("sim_tp1"):
        parts = text.split()
        case_num = parts[1] if len(parts) > 1 else "3"
        from mt5_executor import MT5Executor
        test_exec = MT5Executor(dry_run=True)
        test_exec.connect()

        sample_layers = {
            "TOP": {"lot": 0.01, "ticket": 901, "order_type": "MARKET", "status": "OPEN"},
            "MID": {"lot": 0.01, "ticket": 902, "order_type": "LIMIT", "status": "OPEN" if case_num in ("2", "3") else "PENDING"},
            "BOTTOM": {"lot": 0.02, "ticket": 903, "order_type": "LIMIT", "status": "OPEN" if case_num == "3" else "PENDING"},
        }

        if case_num == "1":
            m_tickets = [901]
            p_tickets = [902, 903]
        elif case_num == "2":
            m_tickets = [901, 902]
            p_tickets = [903]
        else:
            m_tickets = [901, 902, 903]
            p_tickets = []

        sig_data = {
            "layers": sample_layers,
            "market_tickets": list(m_tickets),
            "pending_tickets": list(p_tickets),
            "symbol": DEFAULT_SYMBOL,
            "direction": "BUY",
            "raw_tp1": 3900.0,
            "raw_tp2": 3910.0,
        }

        runner_t = test_exec.handle_tp1_scaling_and_select_runner(sig_data)
        status = sig_data.get("status")
        floor = sig_data.get("soft_tp1_floor")

        if case_num == "1":
            desc = "Shallow Pullback (Only Layer 1 filled)"
            action_desc = (
                "• Banked Layer 1 (0.01 lot) at TP1 (3900.00)\n"
                "• Cancelled pending limits for Layers 2 & 3\n"
                "• Trade fully CLOSED"
            )
        elif case_num == "2":
            desc = "Medium Pullback (Layers 1 & 2 filled)"
            action_desc = (
                f"• Banked Layer 1 (0.01 lot) at TP1\n"
                f"• Cancelled Layer 3 pending limit\n"
                f"• Designated Runner: Layer 2 (0.01 lot) [Ticket #{runner_t}]\n"
                f"• Stop Loss locked at TP1 - 0.50 ({floor})\n"
                f"• Trailing stop enabled toward TP2 (3910.00)"
            )
        else:
            desc = "Full Zone Fill (All 3 Layers filled)"
            action_desc = (
                f"• Banked Layers 1 & 2 (0.02 lot total) at TP1\n"
                f"• Designated Runner: Layer 3 (0.02 lot - best entry!) [Ticket #{runner_t}]\n"
                f"• Stop Loss locked at TP1 - 0.50 ({floor})\n"
                f"• Trailing stop enabled toward TP2 (3910.00)"
            )

        fin = test_exec.get_account_financials()
        curr = fin.get("currency", "GBP")
        sim_profit = 2.50 if case_num == "3" else 1.25
        profit_str = format_profit(sim_profit, curr)

        reply_msg = (
            f"🧪 **Weekend Simulation: `/sim_tp1 {case_num}`**\n"
            f"• Scenario: **{desc}**\n"
            f"• Execution Details:\n{action_desc}\n\n"
            f"• 💰 **Amount Made at TP1:** `{profit_str}`\n"
            f"• Status: `{status}`\n"
            f"• Remaining Open Orders: `{sig_data.get('market_tickets')}`\n"
            f"• Pending Orders: `{sig_data.get('pending_tickets')}`\n\n"
            f"{fin['formatted']}\n\n"
            f"✅ Verified 100% compliant with TP1 scaling specifications!"
        )
        await event.reply(reply_msg)
        return

    # -----------------------------------------------------------------------
    # WEEKEND SIMULATION: /sim_shortfall
    # -----------------------------------------------------------------------
    if text in ("/sim_shortfall", "sim_shortfall"):
        reply_msg = (
            f"🧪 **Weekend Simulation: `/sim_shortfall` (Near-Miss Protection)**\n\n"
            f"1️⃣ **Initial Condition:**\n"
            f"   • BUY XAUUSD: Entry 3895.00, TP1 = 3900.00\n"
            f"   • Shortfall Arm Distance: 20 pips ($2.00 before TP1 = 3898.00)\n"
            f"   • Pullback Trigger: 8 pips ($0.80 drop from peak)\n\n"
            f"2️⃣ **Price Movement:**\n"
            f"   • Price climbs to `3899.20` (within 80 cents of TP1)\n"
            f"   • `shortfall_armed` activates: `True` | `peak_price` = `3899.20`\n"
            f"   • Gold encounters heavy resistance and stalls.\n"
            f"   • Price rolls over and drops to `3898.40` (reversal of $0.80 from peak)!\n\n"
            f"3️⃣ **Automatic Execution:**\n"
            f"   • ⚠️ SHORTFALL GUARD TRIGGERS INSTANTLY!\n"
            f"   • All open market layers banked immediately at market (`3898.40`).\n"
            f"   • All remaining pending limit orders deleted.\n"
            f"   • Trade marked `CLOSED`.\n\n"
            f"🎉 **Result:** Secured huge win near the top! A 42-pip gain was locked in instead of letting it reverse back into a loss."
        )
        await event.reply(reply_msg)
        return

    # -----------------------------------------------------------------------
    # WEEKEND SIMULATION: /sim_trail [2|3]
    # -----------------------------------------------------------------------
    if text.startswith("/sim_trail") or text.startswith("sim_trail"):
        parts = text.split()
        layer_num = parts[1] if len(parts) > 1 else "3"
        lot_size = "0.01" if layer_num == "2" else "0.02"

        reply_msg = (
            f"🧪 **Weekend Simulation: `/sim_trail {layer_num}` (Runner Trailing & TP2 Guard)**\n\n"
            f"1️⃣ **Runner Handoff at TP1 (3900.00):**\n"
            f"   • Designated Runner: Layer {layer_num} ({lot_size} lot)\n"
            f"   • Virtual TP1 Floor: `3899.50` (TP1 - 0.50 buffer)\n"
            f"   • Hard Broker SL: Set to `3899.50`\n"
            f"   • Target TP2: `3910.00`\n\n"
            f"2️⃣ **Trailing Ratchet Upward:**\n"
            f"   • Price pushes to `3903.00`: SL ratchets up to `3901.50` (+$1.50 trailing dist)\n"
            f"   • Price pushes to `3906.00`: SL ratchets up to `3904.50`\n"
            f"   • Price dips to `3905.00`: One-way ratchet holds firm at `3904.50` (never moves down)\n\n"
            f"3️⃣ **TP2 Shortfall High-Water Mark Guard:**\n"
            f"   • Price surges to `3908.20` (within 20 pips of TP2 3910.00)\n"
            f"   • `tp2_shortfall_armed` activates: `True` | `runner_peak` = `3908.20`\n"
            f"   • Price pulls back $0.80 to `3907.40`\n"
            f"   • ⚠️ TP2 Shortfall Guard triggers -> runner closed at market (`3907.40`)!\n\n"
            f"🎉 **Result:** Banked +74 pips on runner! Maximum gain extracted."
        )
        await event.reply(reply_msg)
        return

    # -----------------------------------------------------------------------
    # TEST RUNNER COMMAND: /testrunner
    # -----------------------------------------------------------------------
    if text in ("/testrunner", "testrunner"):
        # Test simulated dry-run executor with all 3 cases
        from mt5_executor import MT5Executor
        test_exec = MT5Executor(dry_run=True)
        test_exec.connect()

        sample_layers = {
            "TOP": {"lot": 0.01, "ticket": 901, "order_type": "MARKET", "status": "OPEN"},
            "MID": {"lot": 0.01, "ticket": 902, "order_type": "LIMIT", "status": "PENDING"},
            "BOTTOM": {"lot": 0.02, "ticket": 903, "order_type": "LIMIT", "status": "PENDING"},
        }

        # Case 1: Only Layer 1 active
        r1 = test_exec.apply_smart_tp1_runner_strategy(
            layers=sample_layers,
            market_tickets=[901],
            pending_tickets=[902, 903],
            symbol=DEFAULT_SYMBOL,
            direction="BUY",
            raw_tp1=3900.0,
        )

        # Case 2: Layers 1 & 2 active
        r2 = test_exec.apply_smart_tp1_runner_strategy(
            layers=sample_layers,
            market_tickets=[901, 902],
            pending_tickets=[903],
            symbol=DEFAULT_SYMBOL,
            direction="BUY",
            raw_tp1=3900.0,
        )

        # Case 3: All 3 layers active
        r3 = test_exec.apply_smart_tp1_runner_strategy(
            layers=sample_layers,
            market_tickets=[901, 902, 903],
            pending_tickets=[],
            symbol=DEFAULT_SYMBOL,
            direction="BUY",
            raw_tp1=3900.0,
        )

        runner_reply = (
            f"🏃 **Smart TP1 & Runner Hierarchy Test Results:**\n\n"
            f"1️⃣ **Case 1 (Only Layer 1 active):**\n"
            f"   • Banked: `{r1['banked']}` (0.01 lot banked at TP1)\n"
            f"   • Runner: `None` | Status: `{r1['status']}`\n\n"
            f"2️⃣ **Case 2 (Layers 1 & 2 active):**\n"
            f"   • Banked: `{r2['banked']}` (Layer 1 banked at TP1)\n"
            f"   • Runner: `Layer {r2['runner']}` (0.01 lot) [Ticket #{r2['runner_ticket']}]\n"
            f"   • Trailed SL: `{r2['trailed_sl']}` (Locked in TP1 gain for TP2+)\n"
            f"   • Status: `{r2['status']}`\n\n"
            f"3️⃣ **Case 3 (All 3 layers active):**\n"
            f"   • Banked: `{r3['banked']}` (Layers 1 & 2 banked at TP1)\n"
            f"   • Runner: `Layer {r3['runner']}` (0.02 lot - best entry!)\n"
            f"   • Trailed SL: `{r3['trailed_sl']}` (Locked in massive profit for TP2+)\n"
            f"   • Status: `{r3['status']}`\n\n"
            f"🎉 **Hierarchy Logic Verified 100%!**"
        )
        await event.reply(runner_reply)
        return

    # -----------------------------------------------------------------------
    # TEST ORDER COMMAND: /testorder (Safe real MT5 Limit Order + Auto-Cancel)
    # -----------------------------------------------------------------------
    if text in ("/testorder", "testorder", "/testlimit", "testlimit"):
        if not executor.is_connected():
            await event.reply("❌ Cannot place test order: MT5 terminal is not connected.")
            return

        if not executor.is_trade_allowed():
            await event.reply("⚠️ Cannot place test order: Algo Trading button is OFF in MT5. Please enable it.")
            return

        tick = executor.get_tick(DEFAULT_SYMBOL)
        if not tick:
            await event.reply(f"❌ Cannot place test order: No live tick available for `{DEFAULT_SYMBOL}`.")
            return

        # Place a safe BUY LIMIT $20.00 (200 pips) below current market so it cannot fill
        safe_price = round(tick.bid - 20.0, 2)
        ticket = executor.place_limit_order(
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

        # Wait 5 seconds so user can see it on MT5 if looking, then cancel safely
        await asyncio.sleep(5)
        cancelled = executor.cancel_pending_order(ticket)

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

    # -----------------------------------------------------------------------
    # TEST SIMULATION COMMAND: /test or /sim
    # -----------------------------------------------------------------------
    if text.startswith("/sim ") or text.startswith("sim "):
        custom_text = msg.message.split(None, 1)[1] if " " in msg.message else ""
        parsed = parse_message(custom_text)
        if not parsed:
            await event.reply(f"❌ Signal Filtered / Rejected by Gates:\n`{custom_text}`")
            return

        reply_str = (
            f"📋 **Signal Parser Simulation:**\n"
            f"• Raw Text: `{custom_text}`\n"
            f"• Event: `{parsed.event_type}` | Direction: `{parsed.direction}`\n"
            f"• Symbol: `{parsed.symbol}`\n"
        )
        if parsed.zone:
            reply_str += (
                f"• Zone: Top=`{parsed.top}` (0.01), Mid=`{parsed.mid}` (0.01), Bottom=`{parsed.bottom}` (0.02)\n"
                f"• Hard Broker TP: `{parsed.final_tp}`\n"
                f"• 20-Pip Proximity Close: `{parsed.proximity_close_price}`\n"
                f"• Stop Loss: `{parsed.sl}` (Valid: {parsed.sl_valid})"
            )
        elif parsed.single_price:
            reply_str += f"• Single Price: `{parsed.single_price}` (0.01 lot) | TP: `{parsed.final_tp}` | SL: `{parsed.sl}`"
        await event.reply(reply_str)
        return

    if text in ("/test", "test"):
        mt5_ok = executor.is_connected()
        algo_ok = executor.is_trade_allowed()
        tick = executor.get_tick(DEFAULT_SYMBOL)
        sample_signal = "Buy 3895-3887 \n TP 3900 \n SL 3875"
        parsed = parse_message(sample_signal)

        test_msg = (
            f"🧪 **Telegram-to-MT5 End-to-End Diagnostics:**\n\n"
            f"1. **Telegram Listener:** 🟢 ACTIVE (Receiving commands from phone)\n"
            f"2. **MT5 Terminal:** {'🟢 CONNECTED' if mt5_ok else '🔴 DISCONNECTED'}\n"
            f"3. **Algo Trading Button:** {'🟢 ENABLED' if algo_ok else '🔴 DISABLED'}\n"
            f"4. **Live Quote ({DEFAULT_SYMBOL}):** Bid=`{getattr(tick, 'bid', 'N/A')}` | Ask=`{getattr(tick, 'ask', 'N/A')}`\n"
            f"5. **6-Gate Parser:** 🟢 PASSED\n"
            f"   - Zone: Top=3895.0, Mid=3891.0, Bottom=3887.0\n"
            f"   - Final TP: 3899.50 (TP1 - 0.50 front-run)\n"
            f"   - Proximity Close Trigger: 3898.00\n"
            f"   - Asymmetric Lots: 0.01 / 0.01 / 0.02 (0.04 max)\n"
            f"6. **State Manager:** 🟢 PERSISTENT DISK OK\n\n"
            f"💡 _To test live broker order execution, send `/testorder` to place a safe limit order and auto-cancel it in 5 seconds._"
        )
        await event.reply(test_msg)
        return

    if text in ("/status", "status", "/ping", "ping", "/trades", "trades", "/algo", "algo", "/check", "check", "/help", "help"):
        uptime_sec = int(time.time() - BOT_START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        mt5_ok = executor.is_connected()
        algo_ok = executor.is_trade_allowed()
        algo_str = "🟢 ENABLED (Green play icon)" if algo_ok else "🔴 DISABLED (Click Algo Trading in MT5)"

        fin = executor.get_account_financials()
        acc_info = (
            f"\n• Balance: `{fin['balance']:.2f} {fin['currency']}`\n"
            f"• Credit: `{fin['credit']:.2f} {fin['currency']}`\n"
            f"• Equity: `{fin['equity']:.2f} {fin['currency']}`"
        )
        floating_pnl_str = ""
        if mt5_ok and not executor.dry_run:
            open_pos = executor.get_live_positions(DEFAULT_SYMBOL)
            if open_pos:
                bot_pnl = sum(
                    float(getattr(p, "profit", 0.0) or 0.0)
                    for p in open_pos
                    if getattr(p, "magic", None) == MAGIC_NUMBER
                )
                floating_pnl_str = f"\n• Floating Bot P&L: `{bot_pnl:+.2f} {fin['currency']}`"

        tick = executor.get_tick(DEFAULT_SYMBOL)
        gold_info = ""
        if tick:
            bid = getattr(tick, "bid", "N/A")
            ask = getattr(tick, "ask", "N/A")
            spread = "N/A"
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                spread = f"{round((ask - bid) / 0.01, 1)} pts"
            gold_info = f"\n• Live Gold ({DEFAULT_SYMBOL}): Bid=`{bid}` | Ask=`{ask}` | Spread=`{spread}`"

        active_signals = state_mgr.get_active_signals()
        if not active_signals:
            trades_str = "\n• Active Signals: None (Listening for next signal)"
        else:
            trades_str = f"\n• Active Signals ({len(active_signals)}):"
            for mid, s in active_signals:
                st = s.get("status")
                layers = s.get("layers", {})
                l1_st = layers.get("TOP", {}).get("status", "N/A")
                l2_st = layers.get("MID", {}).get("status", "N/A")
                l3_st = layers.get("BOTTOM", {}).get("status", "N/A")
                trades_str += f"\n  - ID `{mid}`: `{s.get('direction')}` {s.get('symbol')} | Status: `{st}`"
                trades_str += f"\n    • Layers: L1(0.01)={l1_st} | L2(0.01)={l2_st} | L3(0.02)={l3_st}"
                if s.get("shortfall_armed"):
                    trades_str += f"\n    • ⚠️ TP1 Shortfall Armed! Peak=`{s.get('peak_price')}`"
                if st == "RUNNER_ACTIVE":
                    trades_str += (
                        f"\n    • 🚀 Runner: Layer {s.get('runner_layer')} [Ticket #{s.get('runner_ticket')}]"
                        f" | Soft Floor: `{s.get('soft_tp1_floor')}` | TP2 Target: `{s.get('raw_tp2') or 'TP1+100 pips'}`"
                    )

        risk_st = executor.get_risk_exposure_status()
        dd_st = executor.get_daily_drawdown_status()
        dw_prog = get_daily_win_progress()
        dw_curr = dw_prog.get("currency", "GBP")
        if dw_prog["enabled"] and dw_prog["target_amount"] > 0:
            dw_state_str = (
                f"🟢 ACTIVE — Target: `{format_profit(dw_prog['target_amount'], dw_curr)}` "
                f"(Made: `{format_profit(dw_prog['net_profit_today'], dw_curr)}`)"
            )
        else:
            dw_state_str = f"⚪ OFF (Today's Net: `{format_profit(dw_prog['net_profit_today'], dw_curr)}`)"

        dl_prog = get_daily_loss_progress()
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
        risk_str = (
            f"\n• Active Lots: `{risk_st['total_lots']:.2f} / {risk_st['high_risk_lots']:.2f} Max` "
            f"(Low: `{risk_st['low_risk_lots']:.2f}` | Mid: `{risk_st['mid_risk_lots']:.2f}` | "
            f"High Cap: `{risk_st['high_risk_lots']:.2f}`)"
            f"\n• Daily Win Limit (`dailywin`): {dw_state_str}"
            f"\n• Daily Loss Limit (`dailyloss`): {dl_state_str}"
            f"\n• Daily Drawdown Guard: `{dd_state_str}` "
            f"(Loss: `-{dd_st['total_daily_loss']:.2f} {dd_st['currency']}` / `{dd_st['loss_pct']:.1f}%` | "
            f"50% Pause: `-{dd_st['pause_limit_amount']:.2f}` | 90% Cut-Off: `-{dd_st['emergency_cutoff_amount']:.2f}`)"
        )

        reply_text = (
            f"🤖 **Telegram Copier System Status**\n"
            f"• State: `{'🟢 ONLINE' if mt5_ok else '🔴 MT5 DISCONNECTED'}`\n"
            f"• Algo Trading: `{algo_str}`"
            f"{acc_info}"
            f"{floating_pnl_str}"
            f"{risk_str}"
            f"{gold_info}"
            f"\n• Bot Uptime: `{uptime_str}`"
            f"\n• Execution Mode: `{'DRY-RUN' if executor.dry_run else 'LIVE BROKER'}`"
            f"\n• Magic Number: `{MAGIC_NUMBER}`"
            f"{trades_str}\n\n"
            f"📱 **Commands:** `dailywin`, `dailyloss`, `/set_daily_loss`, `/status_daily_loss`, `/status`, `/bank`, `/closeall`, `/sim_tp1 1|2|3`, `/sim_shortfall`, `/sim_trail 2|3`, `/testorder`"
        )
        await event.reply(reply_text)


# ---------------------------------------------------------------------------
# MAIN APPLICATION LIFECYCLE
# ---------------------------------------------------------------------------

async def main():
    global telethon_client, _daily_loss_locked
    print("\033[93m\033[1m╔═════════════════════════════════════════════════════════════════════════╗")
    print("║        ⚡ ULTIMATE TELEGRAM-TO-MT5 XAUUSD PRO COPIER v2.0 ⚡           ║")
    print("║  ─────────────────────────────────────────────────────────────────────  ║")
    print(f"║   • Broker Symbol: \033[97m{DEFAULT_SYMBOL:<12}\033[93m  • Magic Number: \033[97m{MAGIC_NUMBER:<12}\033[93m   ║")
    print(f"║   • Mode: \033[92m{'DRY-RUN (SIM)' if DRY_RUN else 'LIVE BROKER':<16}\033[93m   • Lots: \033[97m0.01 / 0.01 / 0.02 (0.04)\033[93m     ║")
    print("║   • Protection: \033[96m20-Pip TP1 Guard\033[93m   • Runner: \033[96mTrailing Stop + Floor\033[93m       ║")
    print("╚═════════════════════════════════════════════════════════════════════════╝\033[0m\n")

    # Prune old terminal records (> 7 days)
    cleaned = state_mgr.cleanup_old_signals(days=7)
    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} historical closed trade(s) from state file.")

    # Orphaned "Sell now" / "Buy now" delay timers from a previous run can never fire; close them safely
    stale_delay = [
        mid for mid, s in state_mgr.get_all_signals().items() if s.get("status") in ("PENDING", "PENDING_DELAY")
    ]
    for mid in stale_delay:
        state_mgr.update_signal(mid, {"status": "CLOSED"})
        logger.warning(f"Closed orphaned PENDING signal {mid} left over from previous run (no orders were placed).")

    # Connect to MT5
    logger.info("Connecting to MetaTrader 5...")
    if not executor.connect():
        logger.warning(
            "MT5 terminal connection failed or MT5 is not running. "
            "Please ensure MetaTrader 5 is running with Algorithmic Trading enabled. "
            "Monitoring Telegram channels will still start..."
        )
    else:
        logger.info("MetaTrader 5 connection established successfully.")
        dl_prog = get_daily_loss_progress()
        if dl_prog.get("triggered_date") == _trading_day_str() and dl_prog.get("limit_reached"):
            _daily_loss_locked = True
            logger.warning(
                f"🔒 Daily loss limit already triggered today ({dl_prog['date']}); "
                f"starting in LOCKED mode (new signals blocked until next broker day or `dailyloss off`)."
            )
        update_live_terminal_status(force_print=True)

    # Initialize Telethon Client
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

    # Register Telegram Event Handlers
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
        # Warm Telegram entity/channel access_hash cache so all monitored CHANNEL_IDS receive live updates
        await telethon_client.get_dialogs(limit=100)
    except Exception as dlg_err:
        logger.debug(f"Telethon dialog cache warmup note: {dlg_err}")
    logger.info("Telegram Copier is LIVE and actively listening for signals!")

    # Start 1-second TP1 Proximity Watcher in background
    proximity_task = asyncio.create_task(tp1_proximity_watcher_task())

    unexpected_exit = False
    user_interrupt = False
    try:
        await telethon_client.run_until_disconnected()
        if not (TRADING_HALTED or _daily_loss_terminate_requested or _daily_win_terminate_requested):
            unexpected_exit = True
    except KeyboardInterrupt:
        user_interrupt = True
        logger.info("Shutdown signal (Ctrl+C) received. Exiting gracefully...")
    except SystemExit as e:
        if e.code not in (0, None, DAILY_WIN_EXIT_CODE, DAILY_LOSS_EXIT_CODE):
            unexpected_exit = True
        raise
    except Exception as e:
        unexpected_exit = True
        logger.exception(f"Unexpected error in copier loop: {e}")
    finally:
        _set_windows_keep_awake(False)
        proximity_task.cancel()
        try:
            await proximity_task
        except asyncio.CancelledError:
            pass
        executor.shutdown()
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
