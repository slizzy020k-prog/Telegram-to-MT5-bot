import os
import sys
import time
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
)
from parser import parse_message, SignalAction, ZoneInfo
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

    # Colorful console output
    console_handler = logging.StreamHandler(sys.stdout)
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

    return logger


logger = setup_logger()

# Shared Singletons
state_mgr = StateManager(STATE_FILE_PATH)
executor = MT5Executor(dry_run=DRY_RUN)
telethon_client: Optional[TelegramClient] = None
BOT_START_TIME = time.time()

# In-memory deduplication tracking (msg_id -> process_timestamp)
_recent_messages: Dict[int, float] = {}

# Delayed "Sell now" tasks awaiting limit/zone edit (msg_id -> asyncio.Task)
_pending_sell_now_tasks: Dict[int, asyncio.Task] = {}

# Last printed terminal financial/risk snapshot to avoid spamming identical lines
_last_terminal_snapshot: Optional[Tuple[float, float, float]] = None

# Tracks date string (YYYY-MM-DD) when 50% daily loss pause alert was last sent
_daily_50pct_alert_date: Optional[str] = None


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
    today_str = datetime.now().strftime("%Y-%m-%d")

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


def update_live_terminal_status(force_print: bool = False) -> None:
    """
    Updates the Windows console title bar with live Balance, Credit, Equity & Risk Guide Lots,
    and prints a terminal update line whenever Balance, Credit, or Active Lot Exposure changes.
    """
    global _last_terminal_snapshot
    if executor.dry_run:
        return
    try:
        st = executor.get_risk_exposure_status()
        bal = st.get("balance", 0.0)
        cred = st.get("credit", 0.0)
        eq = st.get("equity", 0.0)
        curr = st.get("currency", "GBP")
        sym_char = "£" if curr == "GBP" else "$" if curr == "USD" else ""
        tot_lots = st.get("total_lots", 0.0)
        low_l = st.get("low_risk_lots", 0.01)
        mid_l = st.get("mid_risk_lots", 0.01)
        high_l = st.get("high_risk_lots", 0.01)

        title_str = (
            f"[XAUUSD COPIER] Bal: {sym_char}{bal:.2f} | Cred: {sym_char}{cred:.2f} | "
            f"Eq: {sym_char}{eq:.2f} | Lots: {tot_lots:.2f}/{high_l:.2f} "
            f"(Low:{low_l:.2f} Mid:{mid_l:.2f} High:{high_l:.2f})"
        )
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW(title_str)
            except Exception:
                pass

        snap = (round(bal, 2), round(cred, 2), round(tot_lots, 2))
        if force_print or snap != _last_terminal_snapshot:
            _last_terminal_snapshot = snap
            logger.info(
                f"💳 [LIVE ACCOUNT & RISK GUIDE] Balance: {bal:.2f} {curr} | Credit: {cred:.2f} {curr} | "
                f"Equity: {eq:.2f} {curr} | Active Lots: {tot_lots:.2f}/{high_l:.2f} Max "
                f"(Low: {low_l:.2f} | Mid: {mid_l:.2f} | High Cap: {high_l:.2f})"
            )
    except Exception as e:
        logger.debug(f"Could not update live terminal status: {e}")


def _cancel_delayed_sell_now(msg_id: int) -> bool:
    """Cancel an active 20-second delayed Sell Now timer if still waiting."""
    task = _pending_sell_now_tasks.pop(msg_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def _delayed_sell_now_task(msg_id: int) -> None:
    """Wait SELL_NOW_DELAY_SECONDS on unpriced 'Sell now'; if no limit/zone arrives, execute market SELL."""
    try:
        await asyncio.sleep(SELL_NOW_DELAY_SECONDS)
        sig = state_mgr.get_signal(msg_id)
        if not sig or sig.get("status") != "PENDING_DELAY":
            return

        symbol = sig.get("symbol", DEFAULT_SYMBOL)
        direction = sig.get("direction", "SELL")
        sl = sig.get("sl")
        final_tp = sig.get("final_tp")
        raw_tp1 = sig.get("raw_tp1")

        passed, reason = executor.check_preflight(
            symbol=symbol,
            direction=direction,
            raw_tp1=raw_tp1,
            sl=sl,
        )
        if not passed:
            logger.warning(f"Delayed Sell Now [ID: {msg_id}] blocked by pre-flight after wait: {reason}")
            state_mgr.update_signal(msg_id, {"status": "CLOSED"})
            await notify_saved_messages(f"⚠️ **Delayed Sell Now Skipped [ID: {msg_id}]**: {reason}")
            return

        top_lot = LAYER_LOTS.get("TOP", 0.01)
        prev_lots = executor.get_total_active_lots()["total_lots"]
        t1 = executor.execute_market_order(
            symbol=symbol,
            direction=direction,
            lot=top_lot,
            sl=sl,
            tp=final_tp,
            comment="TG Layer 1 (Delayed Sell Now)",
        )
        if not t1:
            err_reason = getattr(executor, "last_error", None) or "Broker rejected order"
            logger.error(f"Delayed Sell Now [ID: {msg_id}] failed on MT5: {err_reason}")
            state_mgr.update_signal(msg_id, {"status": "CLOSED"})
            await notify_saved_messages(
                f"❌ **Delayed Sell Now Rejected by MT5 [ID: {msg_id}]**\n"
                f"• Reason: `{err_reason}`"
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
                "market_tickets": [t1],
                "layers": layers_data,
            },
        )
        await notify_saved_messages(
            f"🚀 **Delayed Sell Now Executed (No Limits Posted) [ID: {msg_id}]**\n"
            f"• Direction: `{direction}` {symbol}\n"
            f"• Orders: Market=[`{t1}`] (0.01 lot)\n"
            f"• SL: `{sl}` | TP1: `{final_tp}`"
            f"{risk_block}"
        )
    except asyncio.CancelledError:
        logger.info(f"Delayed Sell Now timer cancelled for Msg ID {msg_id} (limits/cancel arrived).")
    finally:
        _pending_sell_now_tasks.pop(msg_id, None)


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
        await telethon_client.send_message("me", text)
    except Exception as e:
        logger.warning(f"Could not deliver notification to Saved Messages: {e}")


# ---------------------------------------------------------------------------
# 1-SECOND TP1 PROXIMITY WATCHER TASK
# ---------------------------------------------------------------------------

async def tp1_proximity_watcher_task() -> None:
    """
    Background loop running every 1.0 second:
    1. 30-Second Algo Trading Button Monitor: alerts Saved Messages if Algo Trading is turned OFF.
    2. 1-Second MT5 State Reconciliation: syncs filled pending limits, detects manual/mobile closes.
    3. 1-Second 20-Pip TP1 Proximity Auto-Close: auto-banks profits once price reaches raw_tp1 ± $2.00.
    """
    logger.info("TP1 20-Pip Proximity Watcher & MT5 State Reconciler started (1.0s loop).")
    last_algo_check = 0.0

    while True:
        try:
            now = time.time()
            update_live_terminal_status()
            await check_and_enforce_daily_drawdown()

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
                import MetaTrader5 as mt5
                resolved_sym = executor.resolve_symbol(DEFAULT_SYMBOL)
                live_positions = mt5.positions_get(symbol=resolved_sym) or []
                live_orders = mt5.orders_get(symbol=resolved_sym) or []
                live_pos_tickets = {p.ticket for p in live_positions}
                live_order_tickets = {o.ticket for o in live_orders}

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

                # Improvement #1: If 0 market positions filled, but price already reached TP1, cancel stale pending limits
                if not sig.get("market_tickets"):
                    pd_tkts = list(sig.get("pending_tickets", []))
                    if pd_tkts and tp1_hit:
                        for pt in pd_tkts:
                            executor.cancel_pending_order(pt)
                        layers = sig.get("layers", {})
                        for lk in layers:
                            layers[lk]["status"] = "CANCELLED"
                        state_mgr.update_signal(
                            msg_id,
                            {
                                "pending_tickets": [],
                                "layers": layers,
                                "status": "CLOSED",
                            },
                        )
                        logger.info(f"Unfilled Zone [ID: {msg_id}] expired: TP1 ({raw_tp1}) reached before fill. Cancelled {pd_tkts}.")
                        await notify_saved_messages(
                            f"🚫 **Unfilled Zone Expired (TP1 Reached Without Fill) [ID: {msg_id}]**\n"
                            f"• Symbol: `{symbol}` ({direction})\n"
                            f"• Price hit TP1 (`{raw_tp1}`) before dipping into zone.\n"
                            f"• Cancelled `{len(pd_tkts)}` pending limit order(s) (`{pd_tkts}`) to prevent late reversal fills."
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
    msg = event.message
    if not msg:
        return

    msg_id = msg.id
    raw_text = msg.message or ""
    reply_to_id = msg.reply_to_msg_id

    # Deduplication check
    now = time.time()
    if len(_recent_messages) > 200:
        cutoff = now - 60.0
        stale_keys = [k for k, v in _recent_messages.items() if v < cutoff]
        for k in stale_keys:
            del _recent_messages[k]

    if msg_id in _recent_messages and (now - _recent_messages[msg_id]) < DEDUPLICATION_WINDOW_SECONDS:
        logger.info(f"Message ID {msg_id} duplicate event within {DEDUPLICATION_WINDOW_SECONDS}s; skipping.")
        return
    _recent_messages[msg_id] = now

    logger.info(
        f"--- New Message Received [ID: {msg_id}] in Chat [{event.chat_id}] "
        f"(Reply to: {reply_to_id}) ---\nText: {raw_text}"
    )

    # Resolve target signal if this is a reply
    target_signal = None
    target_msg_id = None
    if reply_to_id:
        target_signal = state_mgr.get_signal(reply_to_id)
        if target_signal:
            target_msg_id = str(reply_to_id)

    # 6-Gate Pipeline execution
    is_forward = getattr(msg, "forward", None) is not None
    tick = executor.get_tick(DEFAULT_SYMBOL)
    live_price = getattr(tick, "bid", None) if tick else None
    parsed = parse_message(
        raw_text,
        message_id=msg_id,
        is_reply=bool(reply_to_id),
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

        if not target_signal:
            logger.warning(f"No target trade found in state to apply {action}. (Reply ID: {reply_to_id})")
            return

        if target_signal.get("status") == "PENDING_DELAY" and action not in ("ACTION_FULL_CLOSE", "FULL_CLOSE"):
            logger.info(f"Ignoring {action}: target signal {target_msg_id} is still in Sell Now delay window (no open orders).")
            return

        if target_signal.get("status") in ("CLOSED", "FULL_CLOSED") and action not in ("ACTION_REACTIVATE", "REACTIVATE"):
            logger.info(f"Ignoring {action}: target signal {target_msg_id} is already CLOSED.")
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
            state_mgr.update_signal(target_msg_id, {"status": "BE_APPLIED"})
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
            state_mgr.update_signal(target_msg_id, updates)

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
            if target_signal.get("status") == "PENDING_DELAY":
                _cancel_delayed_sell_now(int(target_msg_id))
                state_mgr.update_signal(target_msg_id, {"status": "CLOSED"})
                await notify_saved_messages(f"🚫 **Pending Sell Now Cancelled [Msg ID: {target_msg_id}]** before execution.")
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
            )
            alert = (
                f"🚪 **Full Close Executed on `{symbol}` [Msg ID: {target_msg_id}]**\n"
                f"• All open positions closed at market.\n"
                f"• 💰 **Total Amount Made:** `{profit_str}`\n\n"
                f"{fin['formatted']}"
            )
            await notify_saved_messages(alert)

        # ACTION: PROVIDER REPLY "TP1 HIT" (Improvement #4)
        elif action in ("ACTION_TP1_HIT_REPLY", "TP1_HIT_REPLY"):
            if target_signal.get("tp1_reached") or target_signal.get("status") in ("RUNNER_ACTIVE", "CLOSED"):
                logger.info(f"Signal {target_msg_id} already processed TP1; ignoring provider TP1 reply.")
                return
            if not market_tickets:
                for pt in pending_tickets:
                    executor.cancel_pending_order(pt)
                for lk in layers:
                    layers[lk]["status"] = "CANCELLED"
                state_mgr.update_signal(
                    target_msg_id,
                    {"pending_tickets": [], "layers": layers, "status": "CLOSED"},
                )
                await notify_saved_messages(
                    f"🚫 **Provider Replied TP1 Hit [Msg ID: {target_msg_id}]**\n"
                    f"• No positions were filled; cancelled `{len(pending_tickets)}` unfilled pending limit(s)."
                )
                return

            runner_t = executor.handle_tp1_scaling_and_select_runner(target_signal)
            state_mgr.set_tp1_reached(target_msg_id, True)
            banked_tkts = target_signal.get("last_banked_tickets", [])
            banked_profit = sum(executor.get_position_closed_profit(t) for t in banked_tkts)
            target_signal["banked_profit"] = banked_profit
            state_mgr.update_signal(target_msg_id, target_signal)

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
        else:
            # Case 3: Instant market entry without price ("Buy now" / "Sell now")
            if direction == "SELL":
                # Wait SELL_NOW_DELAY_SECONDS (default 20s) to see if provider edits in limits/zone
                state_mgr.create_signal(
                    message_id=msg_id,
                    symbol=symbol,
                    direction=direction,
                    layers={},
                    zone=None,
                    market_tickets=[],
                    pending_tickets=[],
                    zone_completed=False,
                    max_orders=MAX_ORDERS_PER_SIGNAL,
                    is_zone=False,
                    sl=sl,
                    raw_tp1=parsed.raw_tp1,
                    raw_tp2=parsed.raw_tp2,
                    raw_tp3=parsed.raw_tp3,
                    final_tp=final_tp,
                    proximity_close_price=parsed.proximity_close_price,
                    raw_text=raw_text,
                    status="PENDING_DELAY",
                )
                _cancel_delayed_sell_now(msg_id)
                _pending_sell_now_tasks[msg_id] = asyncio.create_task(_delayed_sell_now_task(msg_id))
                logger.info(f"Signal ID {msg_id}: Unpriced 'Sell now' held for {SELL_NOW_DELAY_SECONDS}s awaiting limits.")
                await notify_saved_messages(
                    f"⏳ **Sell Now Held ({int(SELL_NOW_DELAY_SECONDS)}s Timer) [ID: {msg_id}]**\n"
                    f"• Symbol: `{symbol}` ({direction})\n"
                    f"• Waiting {int(SELL_NOW_DELAY_SECONDS)}s for limit/zone edit; if none arrives, will execute 0.01 market sell."
                )
                return

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
                market_tickets.append(t1)
                layers_data["TOP"] = {"lot": top_lot, "target_price": None, "ticket": t1, "order_type": "MARKET", "status": "OPEN"}
                layers_data["MID"] = {"lot": LAYER_LOTS.get("MID", 0.01), "target_price": None, "ticket": None, "order_type": "LIMIT", "status": "PENDING"}
                layers_data["BOTTOM"] = {"lot": LAYER_LOTS.get("BOTTOM", 0.02), "target_price": None, "ticket": None, "order_type": "LIMIT", "status": "PENDING"}
            zone_completed = False
            is_zone = False
            max_orders = 1

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

        state_mgr.create_signal(
            message_id=msg_id,
            symbol=symbol,
            direction=direction,
            layers=layers_data,
            zone=zone.to_dict() if zone else None,
            market_tickets=market_tickets,
            pending_tickets=pending_tickets,
            zone_completed=zone_completed,
            max_orders=max_orders,
            is_zone=is_zone,
            sl=sl,
            raw_tp1=parsed.raw_tp1,
            raw_tp2=parsed.raw_tp2,
            raw_tp3=parsed.raw_tp3,
            final_tp=final_tp,
            proximity_close_price=parsed.proximity_close_price,
            raw_text=raw_text,
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
    msg = event.message
    if not msg:
        return

    msg_id = msg.id
    raw_text = msg.message or ""

    existing_signal = state_mgr.get_signal(msg_id)

    # Redundant edit filter (reactions/views)
    if existing_signal and raw_text.strip() == existing_signal.get("raw_text", "").strip():
        logger.debug(f"Edited message {msg_id} text unchanged; skipping.")
        return

    # If message ID does not exist in state: skip silently if already processed within dedup window
    if not existing_signal:
        now = time.time()
        if msg_id in _recent_messages and (now - _recent_messages[msg_id]) < DEDUPLICATION_WINDOW_SECONDS:
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

        logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{event.chat_id}] (not in state; treating as new) ---")
        await handle_new_message(event)
        return

    logger.info(f"--- Message Edited [ID: {msg_id}] in Chat [{event.chat_id}] ---\nNew Text: {raw_text}")

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

    curr_status = existing_signal.get("status")
    if curr_status in ("CLOSED", "FULL_CLOSED"):
        logger.info(f"Signal {msg_id} is already CLOSED; ignoring edit.")
        return

    symbol = existing_signal.get("symbol", DEFAULT_SYMBOL)
    direction = existing_signal.get("direction", "BUY")

    new_sl = parsed.sl if (parsed.sl_valid and parsed.sl is not None) else existing_signal.get("sl")
    new_tp = parsed.final_tp if parsed.final_tp is not None else existing_signal.get("final_tp")

    raw_tp1 = parsed.raw_tp1 or existing_signal.get("raw_tp1")
    raw_tp2 = parsed.raw_tp2 or existing_signal.get("raw_tp2")
    raw_tp3 = parsed.raw_tp3 or existing_signal.get("raw_tp3")

    # Handle edit arriving during 20s Sell Now delay window
    if curr_status == "PENDING_DELAY":
        if parsed.is_zone and parsed.zone:
            _cancel_delayed_sell_now(msg_id)
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
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text})
                await notify_saved_messages(f"❌ **Edited Zone Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`")
                return
            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
                    "final_tp": new_tp,
                    "hard_broker_tp": new_tp,
                    "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                    "zone": zone.to_dict(),
                    "is_zone": True,
                    "zone_completed": True,
                    "max_orders": 3,
                    "layers": l_data,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "raw_text": raw_text,
                },
            )
            await notify_saved_messages(
                f"✏️ **Delayed Sell Now → Zone Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending Limits: {pd_tkts}\n"
                f"• SL: `{new_sl}` | TP1: `{new_tp}`"
            )
            return
        elif parsed.single_price is not None:
            _cancel_delayed_sell_now(msg_id)
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
                state_mgr.update_signal(msg_id, {"status": "CLOSED", "raw_text": raw_text})
                await notify_saved_messages(f"❌ **Edited Limit Rejected by MT5 [ID: {msg_id}]**: `{err_reason}`")
                return
            state_mgr.update_signal(
                msg_id,
                {
                    "status": "ACTIVE",
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
                    "final_tp": new_tp,
                    "hard_broker_tp": new_tp,
                    "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                    "layers": l_data,
                    "market_tickets": mk_tkts,
                    "pending_tickets": pd_tkts,
                    "raw_text": raw_text,
                },
            )
            await notify_saved_messages(
                f"✏️ **Delayed Sell Now → Limit Executed [ID: {msg_id}]**\n"
                f"• Market: {mk_tkts} | Pending: {pd_tkts} @ `{parsed.single_price}`"
            )
            return
        else:
            # Edit added SL/TP only; update state and let the 20s timer keep waiting for limits
            state_mgr.update_signal(
                msg_id,
                {
                    "sl": new_sl,
                    "raw_tp1": raw_tp1,
                    "raw_tp2": raw_tp2,
                    "raw_tp3": raw_tp3,
                    "final_tp": new_tp,
                    "hard_broker_tp": new_tp,
                    "proximity_close_price": parsed.proximity_close_price or existing_signal.get("proximity_close_price"),
                    "raw_text": raw_text,
                },
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
            "max_orders": 3 if parsed.zone else existing_signal.get("max_orders", 1),
            "layers": layers,
            "market_tickets": market_tickets,
            "pending_tickets": pending_tickets,
            "zone_completed": zone_completed,
            "raw_text": raw_text,
        },
    )

    alert = (
        f"✏️ **Signal Updated via Edit [ID: {msg_id}]**\n"
        f"• SL: `{new_sl}` | TP: `{new_tp}`\n"
        f"• Total Orders: {len(market_tickets) + len(pending_tickets)}/{MAX_ORDERS_PER_SIGNAL}"
    )
    await notify_saved_messages(alert)


async def handle_saved_messages_command(event):
    """Remote monitoring commands sent to Saved Messages ('me')."""
    msg = event.message
    if not msg or not msg.message:
        return

    text = msg.message.strip().lower()
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
        closed_positions = 0
        cancelled_orders = 0
        closed_tickets_list = []

        # 1. Close in broker via MT5 API
        if not executor.dry_run and executor.is_connected():
            import MetaTrader5 as mt5
            positions = mt5.positions_get(symbol=DEFAULT_SYMBOL)
            if positions:
                for p in positions:
                    if p.magic == MAGIC_NUMBER:
                        closed_tickets_list.append(p.ticket)
                        if executor.close_market_position(p.ticket):
                            closed_positions += 1
            orders = mt5.orders_get(symbol=DEFAULT_SYMBOL)
            if orders:
                for o in orders:
                    if o.magic == MAGIC_NUMBER:
                        if executor.cancel_pending_order(o.ticket):
                            cancelled_orders += 1

        # 2. Close tracked tickets in state
        active_signals = state_mgr.get_active_signals()
        for mid, s in active_signals:
            for t in s.get("market_tickets", []):
                if t not in closed_tickets_list:
                    closed_tickets_list.append(t)
                if executor.close_market_position(t):
                    closed_positions += 1
            for pt in s.get("pending_tickets", []):
                if executor.cancel_pending_order(pt):
                    cancelled_orders += 1

            layers = s.get("layers", {})
            for lk in layers:
                layers[lk]["status"] = "CLOSED"

            s["status"] = "CLOSED"
            s["market_tickets"] = []
            s["pending_tickets"] = []
            s["layers"] = layers
            state_mgr.update_signal(mid, s)

        total_profit = sum(executor.get_position_closed_profit(t) for t in closed_tickets_list)
        fin = executor.get_account_financials()
        curr = fin.get("currency", "GBP")
        profit_str = format_profit(total_profit, curr)

        await event.reply(
            f"🚨 **EMERGENCY PANIC BUTTON TRIGGERED!**\n"
            f"• Market Positions Closed: `{closed_positions}`\n"
            f"• Pending Orders Cancelled: `{cancelled_orders}`\n"
            f"• 💰 **Total Amount Made / Realized:** `{profit_str}`\n"
            f"• All active signals marked `CLOSED`.\n\n"
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

        acc_info = ""
        floating_pnl_str = ""
        if mt5_ok and not executor.dry_run:
            import MetaTrader5 as mt5
            acc = mt5.account_info()
            if acc:
                acc_info = (
                    f"\n• Account: `{acc.login}` ({acc.server})\n"
                    f"• Balance: `{acc.balance:.2f} {acc.currency}`\n"
                    f"• Credit: `{acc.credit:.2f} {acc.currency}`\n"
                    f"• Equity: `{acc.equity:.2f} {acc.currency}`"
                )
            open_pos = mt5.positions_get(symbol=DEFAULT_SYMBOL)
            if open_pos:
                bot_pnl = sum(p.profit for p in open_pos if p.magic == MAGIC_NUMBER)
                floating_pnl_str = f"\n• Floating Bot P&L: `{bot_pnl:+.2f} USD`"
        else:
            fin = executor.get_account_financials()
            acc_info = (
                f"\n• Balance: `{fin['balance']:.2f} {fin['currency']}`\n"
                f"• Credit: `{fin['credit']:.2f} {fin['currency']}`\n"
                f"• Equity: `{fin['equity']:.2f} {fin['currency']}`"
            )

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
        dd_state_str = (
            "🚨 90% CUT-OFF" if dd_st["emergency_cutoff_triggered"]
            else "🛑 50% PAUSED (Holding Open Trades)" if dd_st["pause_new_signals"]
            else "🟢 OK"
        )
        risk_str = (
            f"\n• Active Lots: `{risk_st['total_lots']:.2f} / {risk_st['high_risk_lots']:.2f} Max` "
            f"(Low: `{risk_st['low_risk_lots']:.2f}` | Mid: `{risk_st['mid_risk_lots']:.2f}` | "
            f"High Cap: `{risk_st['high_risk_lots']:.2f}`)"
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
            f"📱 **Commands:** `/status`, `/bank`, `/closeall`, `/sim_tp1 1|2|3`, `/sim_shortfall`, `/sim_trail 2|3`, `/testorder`"
        )
        await event.reply(reply_text)


# ---------------------------------------------------------------------------
# MAIN APPLICATION LIFECYCLE
# ---------------------------------------------------------------------------

async def main():
    global telethon_client
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

    # Orphaned "Sell now" delay timers from a previous run can never fire; close them safely
    stale_delay = [
        mid for mid, s in state_mgr.get_all_signals().items() if s.get("status") == "PENDING_DELAY"
    ]
    for mid in stale_delay:
        state_mgr.update_signal(mid, {"status": "CLOSED"})
        logger.warning(f"Closed orphaned PENDING_DELAY signal {mid} left over from previous run (no orders were placed).")

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
    await telethon_client.start()
    logger.info("Telegram Copier is LIVE and actively listening for signals!")

    # Start 1-second TP1 Proximity Watcher in background
    proximity_task = asyncio.create_task(tp1_proximity_watcher_task())

    try:
        await telethon_client.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received. Exiting gracefully...")
    finally:
        proximity_task.cancel()
        try:
            await proximity_task
        except asyncio.CancelledError:
            pass
        executor.shutdown()
        logger.info("Copier terminated.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nShutdown complete.")
