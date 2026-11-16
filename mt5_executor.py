import time
import logging
from typing import Optional, List, Dict, Any, Tuple
import MetaTrader5 as mt5

from config import (
    LAYER_LOTS,
    MAX_ORDERS_PER_SIGNAL,
    MAGIC_NUMBER,
    BE_LAYERS,
    BE_OFFSET_PIPS,
    FALLBACK_SL_PIPS,
    TP_FRONT_RUN_OFFSET,
    TP1_CLOSE_PROXIMITY_PIPS,
    TP1_LOCK_BUFFER,
    TRAIL_DISTANCE_PIPS,
    TRAIL_STEP_PIPS,
    TP2_SHORTFALL_ARM_PIPS,
    TP2_SHORTFALL_PULLBACK_PIPS,
    NO_TP2_SURGE_ARM_PIPS,
    SHORTFALL_ARM_PIPS,
    SHORTFALL_PULLBACK_PIPS,
    MIN_SL_BUFFER_PIPS,
    ALLOW_BELOW_ZONE_WICK,
    MT5_LOGIN,
    MT5_PASSWORD,
    MT5_SERVER,
    MT5_PATH,
    ENFORCE_HIGH_RISK_LOT_CAP,
    DAILY_LOSS_PAUSE_PCT,
    EMERGENCY_CUTOFF_LOSS_PCT,
)
from parser import ZoneInfo
import math
from datetime import datetime, timezone

logger = logging.getLogger("copier.mt5")


def calculate_risk_guide_thresholds(capital: float) -> Dict[str, float]:
    """
    Professional Risk Guide Calculator for any custom account balance:
      £50   -> Low: 0.01 | Mid: 0.02 | High: 0.05
      £100  -> Low: 0.01 | Mid: 0.05 | High: 0.10
      £200  -> Low: 0.02 | Mid: 0.10 | High: 0.20
      £500  -> Low: 0.05 | Mid: 0.25 | High: 0.50
      £1000 -> Low: 0.10 | Mid: 0.50 | High: 1.00
    """
    cap = max(0.0, float(capital))
    low_risk = max(0.01, round(math.floor((cap + 1e-9) / 100.0) * 0.01, 2))
    mid_risk = max(0.01, round(math.floor((cap + 1e-9) / 20.0) * 0.01, 2))
    high_risk = max(0.01, round(math.floor((cap + 1e-9) / 10.0) * 0.01, 2))
    return {
        "capital": round(cap, 2),
        "low_risk_lots": low_risk,
        "mid_risk_lots": mid_risk,
        "high_risk_lots": high_risk,
    }


class MT5Executor:
    """
    MetaTrader 5 trade executor supporting asymmetric 3-layer zone orders (0.01/0.01/0.02),
    pre-flight safety checks, selective breakeven, and 20-pip TP1 proximity auto-close.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._connected = False
        self._symbol_cache: Dict[str, str] = {}
        self.last_error: Optional[str] = None
        self._dry_run_capital: Optional[float] = None
        self._dry_run_ticket_lots: Dict[int, float] = {}
        self._dry_run_daily_realized_pnl: float = 0.0
        self._dry_run_floating_pnl: float = 0.0

    def connect(self) -> bool:
        """Initialize connection to MetaTrader 5 terminal."""
        if self.dry_run:
            logger.info("MT5Executor running in DRY-RUN mode.")
            self._connected = True
            return True

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
                    f"Connected to MT5 active account: {acc_info.login} on {acc_info.server} "
                    f"(Balance: {acc_info.balance} {acc_info.currency})"
                )
            else:
                logger.info(f"Connected to MT5 terminal: {term_info}")

        self._connected = True
        return True

    def shutdown(self) -> None:
        """Shutdown MT5 connection."""
        if not self.dry_run and self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 connection shut down.")

    def is_connected(self) -> bool:
        """Check if MT5 is initialized and connected to the trade server."""
        if self.dry_run:
            return True
        if not self._connected:
            return False
        try:
            term_info = mt5.terminal_info()
            return term_info is not None and getattr(term_info, "connected", False)
        except Exception:
            return False

    def is_trade_allowed(self) -> bool:
        """Verify if MT5 Algo Trading is enabled in terminal (trade_allowed == True)."""
        if self.dry_run:
            return True
        if not self._connected:
            return False
        try:
            term = mt5.terminal_info()
            return term is not None and getattr(term, "trade_allowed", False)
        except Exception:
            return False

    def _ensure_connected(self) -> bool:
        """Ensure MT5 connection is live; reconnect if dropped."""
        if self.dry_run:
            return True
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
        if self.dry_run or not self._connected:
            return symbol

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

        resolved = self.resolve_symbol(symbol)
        info = mt5.symbol_info(resolved)
        if info is None:
            logger.error(f"Symbol {symbol} (resolved: {resolved}) not found on broker.")
            return False
        if not info.visible:
            if not mt5.symbol_select(resolved, True):
                logger.error(f"Failed to select symbol {resolved} in Market Watch.")
                return False
        return True

    def get_symbol_info(self, symbol: str):
        """Get MT5 symbol information."""
        if self.dry_run:
            class DummySymbolInfo:
                digits = 2
                point = 0.01
                spread = 15
                trade_mode = 4  # Full access
                filling_mode = 2  # IOC
                visible = True
            return DummySymbolInfo()
        resolved = self.resolve_symbol(symbol)
        return mt5.symbol_info(resolved)

    def get_tick(self, symbol: str):
        """Get latest tick for symbol."""
        if self.dry_run:
            class DummyTick:
                ask = 3895.00
                bid = 3894.80
            return DummyTick()
        resolved = self.resolve_symbol(symbol)
        return mt5.symbol_info_tick(resolved)

    def get_pip_size(self, symbol: str) -> float:
        """
        Calculates pip value in price points:
        - Gold (XAUUSD): 1 pip = 0.10 ($0.10)
        - Forex 3/5 digit: point * 10
        - Forex 2/4 digit: point
        """
        upper = symbol.upper()
        if "GOLD" in upper or "XAU" in upper:
            return 0.10

        info = self.get_symbol_info(symbol)
        if not info:
            return 0.0001

        if info.digits in (3, 5):
            return info.point * 10.0
        return info.point

    def check_preflight(
        self,
        symbol: str,
        direction: str,
        raw_tp1: Optional[float] = None,
        sl: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Pre-Flight Safety Checks:
        1. MT5 connection live.
        2. Trade mode full (market open).
        3. Safe Price Window Check:
           - Upper bound: not within 20 pips of TP1 (move not already finished).
           - Lower bound: at least 20 pips away from SL.
        (Spread caps and signal concurrency limits disabled: all signals execute).
        """
        if not self.is_trade_allowed():
            return False, "Algo Trading is OFF in MT5 (trade_allowed=False)"

        dd = self.get_daily_drawdown_status()
        if dd["pause_new_signals"]:
            return (
                False,
                f"{int(DAILY_LOSS_PAUSE_PCT)}% Daily Loss Limit Reached "
                f"(-{dd['total_daily_loss']:.2f} {dd['currency']} / {dd['loss_pct']:.1f}% of "
                f"{dd['reference_balance']:.2f} {dd['currency']} balance) — "
                f"Holding existing open trades to natural SL/TP; new signals paused.",
            )

        if self.dry_run:
            return True, "DRY_RUN: Pre-flight OK"

        if not self._ensure_connected():
            return False, "MT5 terminal not connected"

        resolved = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved):
            return False, f"Symbol {resolved} not accessible"

        info = self.get_symbol_info(resolved)
        if not info:
            return False, f"Failed to retrieve symbol info for {resolved}"

        # 1. Trade mode check (SYMBOL_TRADE_MODE_FULL = 4)
        if hasattr(info, "trade_mode") and info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            return False, f"Trade mode is not FULL (mode={info.trade_mode}) - market may be closed"

        # 2. Check if Algo Trading button is ON in MT5
        if not self.is_trade_allowed():
            return False, "Algo Trading is OFF in MT5 (trade_allowed=False)"

        # 2. Retrieve tick
        tick = self.get_tick(resolved)
        if not tick:
            return False, f"No tick available for {resolved}"

        spread_points = round((tick.ask - tick.bid) / info.point, 1)
        logger.info(f"Pre-flight check: Current spread on {resolved} is {spread_points} points.")

        # 3. Safe Price Window Check
        # Gold pip size is 0.10 => 20 pips = 2.00 points
        pip_20 = TP1_CLOSE_PROXIMITY_PIPS * 0.10
        sl_pip_20 = MIN_SL_BUFFER_PIPS * 0.10

        if direction == "BUY":
            # TP1 guard: must NOT already be within 20 pips of TP1 (ask >= raw_tp1 - 2.00)
            if raw_tp1 is not None and tick.ask >= (raw_tp1 - pip_20):
                return False, f"Price {tick.ask} is within 20 pips of TP1 ({raw_tp1}) - R:R expired"
            # SL guard: must be at least 20 pips above SL
            if sl is not None and tick.ask <= (sl + sl_pip_20):
                return False, f"Price {tick.ask} is too close to SL ({sl}) (< 20 pips)"
        else:  # SELL
            # TP1 guard: must NOT already be within 20 pips of TP1 (bid <= raw_tp1 + 2.00)
            if raw_tp1 is not None and tick.bid <= (raw_tp1 + pip_20):
                return False, f"Price {tick.bid} is within 20 pips of TP1 ({raw_tp1}) - R:R expired"
            # SL guard: must be at least 20 pips below SL
            if sl is not None and tick.bid >= (sl - sl_pip_20):
                return False, f"Price {tick.bid} is too close to SL ({sl}) (< 20 pips)"

        return True, "Pre-flight checks passed"

    def get_filling_modes(self, symbol: str) -> List[int]:
        """Determine supported filling modes for symbol."""
        if self.dry_run:
            return [mt5.ORDER_FILLING_IOC]

        info = self.get_symbol_info(symbol)
        if not info:
            return [mt5.ORDER_FILLING_RETURN]

        modes = []
        if info.filling_mode & 2 != 0:
            modes.append(mt5.ORDER_FILLING_IOC)
        if info.filling_mode & 1 != 0:
            modes.append(mt5.ORDER_FILLING_FOK)
        modes.append(mt5.ORDER_FILLING_RETURN)
        return modes

    def send_order_with_filling_fallback(self, request: dict) -> Tuple[bool, Any]:
        """Send MT5 order, retrying alternative filling modes on invalid fill error."""
        if self.dry_run:
            import random
            dummy_ticket = random.randint(100000, 999999)
            logger.info(f"[DRY-RUN] Order sent successfully: ticket={dummy_ticket}, request={request}")
            class DummyResult:
                retcode = mt5.TRADE_RETCODE_DONE
                order = dummy_ticket
                comment = "Dry run order"
            return True, DummyResult()

        if not self._ensure_connected():
            self.last_error = "MT5 terminal is not connected"
            logger.error("Cannot send order: MT5 terminal is not connected.")
            return False, None

        self.last_error = None
        symbol = request.get("symbol", "")
        filling_modes = self.get_filling_modes(symbol)

        for fill_mode in filling_modes:
            req_copy = request.copy()
            req_copy["type_filling"] = fill_mode
            result = mt5.order_send(req_copy)

            if result is None:
                err = mt5.last_error()
                self.last_error = f"order_send returned None: {err}"
                logger.warning(f"order_send returned None (fill={fill_mode}): {err}")
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.last_error = None
                return True, result

            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                logger.debug(f"Invalid filling mode {fill_mode}, attempting next mode...")
                continue
            else:
                self.last_error = f"retcode={result.retcode} ({result.comment})"
                logger.error(
                    f"Order failed: retcode={result.retcode}, comment='{result.comment}', request={req_copy}"
                )
                return False, result

        return False, None

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
        self.last_error = None
        allowed_lot, block_reason = self.check_and_cap_order_lot(lot)
        if allowed_lot <= 0.0:
            self.last_error = block_reason or "High Risk Lot Cap reached"
            logger.warning(f"Market {direction} blocked by Risk Guide Cap: {self.last_error}")
            return None
        lot = allowed_lot

        resolved_sym = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved_sym):
            self.last_error = f"Symbol {resolved_sym} not accessible"
            return None

        sym_info = self.get_symbol_info(resolved_sym)
        digits = sym_info.digits if sym_info else 2
        pip_size = self.get_pip_size(resolved_sym)
        tick = self.get_tick(resolved_sym)

        if not tick:
            self.last_error = f"No tick available for {resolved_sym}"
            logger.error(f"Cannot get tick for {resolved_sym}")
            return None

        is_buy = direction == "BUY"
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        entry_price = tick.ask if is_buy else tick.bid

        final_sl = sl
        if final_sl is None or final_sl == 0.0:
            if is_buy:
                final_sl = round(entry_price - (FALLBACK_SL_PIPS * pip_size), digits)
            else:
                final_sl = round(entry_price + (FALLBACK_SL_PIPS * pip_size), digits)
            logger.info(f"Attached fallback SL ({FALLBACK_SL_PIPS} pips): {final_sl} on {direction} {resolved_sym}")

        final_tp = round(tp, digits) if tp is not None else 0.0

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": resolved_sym,
            "volume": round(lot, 2),
            "type": order_type,
            "price": entry_price,
            "sl": final_sl,
            "tp": final_tp,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        success, result = self.send_order_with_filling_fallback(request)
        if success and result:
            ticket = getattr(result, "order", None)
            if self.dry_run and ticket is not None:
                self._dry_run_ticket_lots[ticket] = round(lot, 2)
            logger.info(
                f"Opened Market {direction} Order #{ticket} | Symbol={resolved_sym}, "
                f"Lot={lot}, Price={entry_price}, SL={final_sl}, TP={final_tp}"
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
        """Place a BUY LIMIT or SELL LIMIT order at the specified zone price."""
        self.last_error = None
        allowed_lot, block_reason = self.check_and_cap_order_lot(lot)
        if allowed_lot <= 0.0:
            self.last_error = block_reason or "High Risk Lot Cap reached"
            logger.warning(f"Limit {direction} @ {price} blocked by Risk Guide Cap: {self.last_error}")
            return None
        lot = allowed_lot

        resolved_sym = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved_sym):
            self.last_error = f"Symbol {resolved_sym} not accessible"
            return None

        sym_info = self.get_symbol_info(resolved_sym)
        digits = sym_info.digits if sym_info else 2
        is_buy = direction == "BUY"
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT

        pip_size = self.get_pip_size(resolved_sym)
        target_price = round(price, digits)

        final_sl = sl
        if final_sl is None or final_sl == 0.0:
            if is_buy:
                final_sl = round(target_price - (FALLBACK_SL_PIPS * pip_size), digits)
            else:
                final_sl = round(target_price + (FALLBACK_SL_PIPS * pip_size), digits)
            logger.info(f"Attached fallback SL ({FALLBACK_SL_PIPS} pips): {final_sl} on Limit {direction} {resolved_sym}")
        else:
            final_sl = round(final_sl, digits)

        final_tp = round(tp, digits) if tp is not None else 0.0

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": resolved_sym,
            "volume": round(lot, 2),
            "type": order_type,
            "price": target_price,
            "sl": final_sl,
            "tp": final_tp,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        success, result = self.send_order_with_filling_fallback(request)
        if success and result:
            ticket = getattr(result, "order", None)
            if self.dry_run and ticket is not None:
                self._dry_run_ticket_lots[ticket] = round(lot, 2)
            logger.info(
                f"Placed Limit {direction} Order #{ticket} | Symbol={resolved_sym}, "
                f"TargetPrice={target_price}, Lot={lot}, SL={final_sl}, TP={final_tp}"
            )
            return ticket
        return None

    def execute_single_price_order(
        self,
        symbol: str,
        direction: str,
        price: float,
        lot: float = 0.01,
        sl: Optional[float] = None,
        final_tp: Optional[float] = None,
        raw_tp1: Optional[float] = None,
    ) -> Tuple[List[int], List[int], Dict[str, Dict[str, Any]]]:
        """
        Places 1 order (0.01 lot) at the specified single price using Smart Routing:
        - For BUY:
          if ask <= price -> Market Buy
          if ask > price -> BUY LIMIT at price
        - For SELL:
          if bid >= price -> Market Sell
          if bid < price -> SELL LIMIT at price
        Returns (market_tickets, pending_tickets, layer_dict).
        """
        resolved_sym = self.resolve_symbol(symbol)
        if not self.ensure_symbol(resolved_sym):
            return [], [], {}

        tick = self.get_tick(resolved_sym)
        if not tick:
            logger.error(f"Cannot get tick for {resolved_sym}")
            return [], [], {}

        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"Executing Single-Price Order for {direction} {resolved_sym}: Price={price}, Lot={lot}, TP={final_tp}, SL={sl}"
        )

        if direction == "BUY":
            if tick.ask <= price:
                t1 = self.execute_market_order(
                    symbol=resolved_sym, direction="BUY", lot=lot,
                    sl=sl, tp=final_tp, comment="TG Single (Market)",
                )
                if t1:
                    market_tickets.append(t1)
                    layers_data["TOP"] = {"lot": lot, "target_price": price, "ticket": t1, "order_type": "MARKET", "status": "OPEN"}
            else:
                t1 = self.place_limit_order(
                    symbol=resolved_sym, direction="BUY", price=price, lot=lot,
                    sl=sl, tp=final_tp, comment="TG Single (Limit)",
                )
                if t1:
                    pending_tickets.append(t1)
                    layers_data["TOP"] = {"lot": lot, "target_price": price, "ticket": t1, "order_type": "LIMIT", "status": "PENDING"}
        else:  # SELL
            if tick.bid >= price:
                t1 = self.execute_market_order(
                    symbol=resolved_sym, direction="SELL", lot=lot,
                    sl=sl, tp=final_tp, comment="TG Single (Market)",
                )
                if t1:
                    market_tickets.append(t1)
                    layers_data["TOP"] = {"lot": lot, "target_price": price, "ticket": t1, "order_type": "MARKET", "status": "OPEN"}
            else:
                t1 = self.place_limit_order(
                    symbol=resolved_sym, direction="SELL", price=price, lot=lot,
                    sl=sl, tp=final_tp, comment="TG Single (Limit)",
                )
                if t1:
                    pending_tickets.append(t1)
                    layers_data["TOP"] = {"lot": lot, "target_price": price, "ticket": t1, "order_type": "LIMIT", "status": "PENDING"}

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
        Asymmetric 3-Layer Zone Placement (Max 3 orders, 0.04 total max exposure):
        - Layer 1 (TOP): 0.01 lot -> Hard TP at final_tp (TP1 ± 0.50)
        - Layer 2 (MID): 0.01 lot -> Target TP2 (or TP1 + 100 pips if no TP2)
        - Layer 3 (BOTTOM): 0.02 lot -> Target TP2 (or TP1 + 100 pips if no TP2)
        Returns (market_tickets, pending_tickets, layer_dict).
        """
        if not self.ensure_symbol(symbol):
            return [], [], {}

        tick = self.get_tick(symbol)
        if not tick:
            logger.error(f"Cannot get tick for {symbol}")
            return [], [], {}

        sym_info = self.get_symbol_info(symbol)
        digits = sym_info.digits if sym_info else 2

        top_lot = LAYER_LOTS.get("TOP", 0.01)
        mid_lot = LAYER_LOTS.get("MID", 0.01)
        bottom_lot = LAYER_LOTS.get("BOTTOM", 0.02)

        top_price = zone.top
        mid_price = zone.mid
        bottom_price = zone.bottom

        # Layer 1 gets hard TP1 front-run
        top_tp = final_tp

        # Layers 2 and 3 get TP2 (or TP1 + 100 pips / $10 on Gold)
        if raw_tp2:
            runner_tp = round(raw_tp2, digits)
        elif raw_tp1:
            runner_tp = round(raw_tp1 + 10.0, digits) if direction == "BUY" else round(raw_tp1 - 10.0, digits)
        else:
            runner_tp = final_tp

        mid_tp = runner_tp
        bottom_tp = runner_tp

        market_tickets: List[int] = []
        pending_tickets: List[int] = []
        layers_data: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"Executing 3-Layer Zone for {direction} {symbol}: "
            f"Top={top_price} ({top_lot}, TP={top_tp}), Mid={mid_price} ({mid_lot}, TP={mid_tp}), Bottom={bottom_price} ({bottom_lot}, TP={bottom_tp}) | "
            f"SL={sl}"
        )

        if direction == "BUY":
            # Layer 1: TOP (0.01 lot)
            if tick.ask <= top_price:
                t1 = self.execute_market_order(
                    symbol=symbol, direction="BUY", lot=top_lot,
                    sl=sl, tp=top_tp, comment="TG Layer 1 (Top Market)",
                )
                if t1:
                    market_tickets.append(t1)
                    layers_data["TOP"] = {"lot": top_lot, "target_price": top_price, "ticket": t1, "order_type": "MARKET", "status": "OPEN"}
            else:
                t1 = self.place_limit_order(
                    symbol=symbol, direction="BUY", price=top_price, lot=top_lot,
                    sl=sl, tp=top_tp, comment="TG Layer 1 (Top Limit)",
                )
                if t1:
                    pending_tickets.append(t1)
                    layers_data["TOP"] = {"lot": top_lot, "target_price": top_price, "ticket": t1, "order_type": "LIMIT", "status": "PENDING"}

            # Layer 2: MID (0.01 lot)
            if tick.ask <= mid_price:
                t2 = self.execute_market_order(
                    symbol=symbol, direction="BUY", lot=mid_lot,
                    sl=sl, tp=mid_tp, comment="TG Layer 2 (Mid Market)",
                )
                if t2:
                    market_tickets.append(t2)
                    layers_data["MID"] = {"lot": mid_lot, "target_price": mid_price, "ticket": t2, "order_type": "MARKET", "status": "OPEN"}
            else:
                t2 = self.place_limit_order(
                    symbol=symbol, direction="BUY", price=mid_price, lot=mid_lot,
                    sl=sl, tp=mid_tp, comment="TG Layer 2 (Mid Limit)",
                )
                if t2:
                    pending_tickets.append(t2)
                    layers_data["MID"] = {"lot": mid_lot, "target_price": mid_price, "ticket": t2, "order_type": "LIMIT", "status": "PENDING"}

            # Layer 3: BOTTOM (0.02 lot)
            if tick.ask <= bottom_price:
                t3 = self.execute_market_order(
                    symbol=symbol, direction="BUY", lot=bottom_lot,
                    sl=sl, tp=bottom_tp, comment="TG Layer 3 (Bottom Market)",
                )
                if t3:
                    market_tickets.append(t3)
                    layers_data["BOTTOM"] = {"lot": bottom_lot, "target_price": bottom_price, "ticket": t3, "order_type": "MARKET", "status": "OPEN"}
            else:
                t3 = self.place_limit_order(
                    symbol=symbol, direction="BUY", price=bottom_price, lot=bottom_lot,
                    sl=sl, tp=bottom_tp, comment="TG Layer 3 (Bottom Limit)",
                )
                if t3:
                    pending_tickets.append(t3)
                    layers_data["BOTTOM"] = {"lot": bottom_lot, "target_price": bottom_price, "ticket": t3, "order_type": "LIMIT", "status": "PENDING"}

        else:  # SELL
            # Layer 1: BOTTOM entry on SELL (0.01 lot)
            if tick.bid >= bottom_price:
                t1 = self.execute_market_order(
                    symbol=symbol, direction="SELL", lot=top_lot,
                    sl=sl, tp=top_tp, comment="TG Layer 1 (Bottom Market)",
                )
                if t1:
                    market_tickets.append(t1)
                    layers_data["TOP"] = {"lot": top_lot, "target_price": bottom_price, "ticket": t1, "order_type": "MARKET", "status": "OPEN"}
            else:
                t1 = self.place_limit_order(
                    symbol=symbol, direction="SELL", price=bottom_price, lot=top_lot,
                    sl=sl, tp=top_tp, comment="TG Layer 1 (Bottom Limit)",
                )
                if t1:
                    pending_tickets.append(t1)
                    layers_data["TOP"] = {"lot": top_lot, "target_price": bottom_price, "ticket": t1, "order_type": "LIMIT", "status": "PENDING"}

            # Layer 2: MID (0.01 lot)
            if tick.bid >= mid_price:
                t2 = self.execute_market_order(
                    symbol=symbol, direction="SELL", lot=mid_lot,
                    sl=sl, tp=mid_tp, comment="TG Layer 2 (Mid Market)",
                )
                if t2:
                    market_tickets.append(t2)
                    layers_data["MID"] = {"lot": mid_lot, "target_price": mid_price, "ticket": t2, "order_type": "MARKET", "status": "OPEN"}
            else:
                t2 = self.place_limit_order(
                    symbol=symbol, direction="SELL", price=mid_price, lot=mid_lot,
                    sl=sl, tp=mid_tp, comment="TG Layer 2 (Mid Limit)",
                )
                if t2:
                    pending_tickets.append(t2)
                    layers_data["MID"] = {"lot": mid_lot, "target_price": mid_price, "ticket": t2, "order_type": "LIMIT", "status": "PENDING"}

            # Layer 3: TOP (0.02 lot)
            if tick.bid >= top_price:
                t3 = self.execute_market_order(
                    symbol=symbol, direction="SELL", lot=bottom_lot,
                    sl=sl, tp=bottom_tp, comment="TG Layer 3 (Top Market)",
                )
                if t3:
                    market_tickets.append(t3)
                    layers_data["BOTTOM"] = {"lot": bottom_lot, "target_price": top_price, "ticket": t3, "order_type": "MARKET", "status": "OPEN"}
            else:
                t3 = self.place_limit_order(
                    symbol=symbol, direction="SELL", price=top_price, lot=bottom_lot,
                    sl=sl, tp=bottom_tp, comment="TG Layer 3 (Top Limit)",
                )
                if t3:
                    pending_tickets.append(t3)
                    layers_data["BOTTOM"] = {"lot": bottom_lot, "target_price": top_price, "ticket": t3, "order_type": "LIMIT", "status": "PENDING"}

        return market_tickets, pending_tickets, layers_data

    # Compatibility alias for 3tier
    execute_3tier_zone = execute_3layer_zone

    def modify_position_sltp(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        """Modify SL and TP of an open MT5 market position."""
        if self.dry_run:
            logger.info(f"[DRY-RUN] Modified Position #{ticket}: SL={sl}, TP={tp}")
            return True

        if not self._ensure_connected():
            logger.error(f"Cannot modify position #{ticket}: MT5 is not connected.")
            return False

        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            logger.warning(f"Position #{ticket} not found for SL/TP modification.")
            return False
        p = pos[0]

        sym_info = self.get_symbol_info(p.symbol)
        digits = sym_info.digits if sym_info else 2

        new_sl = round(sl, digits) if sl is not None else p.sl
        new_tp = round(tp, digits) if tp is not None else p.tp

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": p.symbol,
            "sl": new_sl,
            "tp": new_tp,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Updated SL/TP on Position #{ticket}: SL={new_sl}, TP={new_tp}")
            return True
        no_changes_code = getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025)
        comment = getattr(result, "comment", "") if result else mt5.last_error()
        if result and (result.retcode == no_changes_code or str(comment).strip().lower() == "no changes"):
            logger.debug(f"Position #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
            return True
        self.last_error = str(comment)
        logger.error(f"Failed to update SL/TP on Position #{ticket}: {comment}")
        return False

    def modify_pending_order_sltp(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        """Modify SL and TP of a pending MT5 limit order."""
        if self.dry_run:
            logger.info(f"[DRY-RUN] Modified Pending Order #{ticket}: SL={sl}, TP={tp}")
            return True

        if not self._ensure_connected():
            logger.error(f"Cannot modify pending order #{ticket}: MT5 is not connected.")
            return False

        orders = mt5.orders_get(ticket=ticket)
        if not orders:
            logger.warning(f"Pending order #{ticket} not found for modification.")
            return False
        order = orders[0]

        sym_info = self.get_symbol_info(order.symbol)
        digits = sym_info.digits if sym_info else 2

        new_sl = round(sl, digits) if sl is not None else order.sl
        new_tp = round(tp, digits) if tp is not None else order.tp

        request = {
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": ticket,
            "price": order.price_open,
            "sl": new_sl,
            "tp": new_tp,
            "type_time": order.type_time,
            "type_filling": order.type_filling,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Updated SL/TP on Pending Order #{ticket}: SL={new_sl}, TP={new_tp}")
            return True
        no_changes_code = getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025)
        comment = getattr(result, "comment", "") if result else mt5.last_error()
        if result and (result.retcode == no_changes_code or str(comment).strip().lower() == "no changes"):
            logger.debug(f"Pending Order #{ticket} SL/TP already at SL={new_sl}, TP={new_tp} (No changes).")
            return True
        self.last_error = str(comment)
        logger.error(f"Failed to update SL/TP on Pending Order #{ticket}: {comment}")
        return False

    def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel an unfilled pending limit order."""
        if self.dry_run:
            self._dry_run_ticket_lots.pop(ticket, None)
            logger.info(f"[DRY-RUN] Cancelled Pending Order #{ticket}")
            return True

        if not self._ensure_connected():
            logger.error(f"Cannot cancel pending order #{ticket}: MT5 is not connected.")
            return False

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Successfully cancelled Pending Order #{ticket}")
            return True
        else:
            comment = getattr(result, "comment", "") if result else mt5.last_error()
            logger.warning(f"Failed or already filled/cancelled for Order #{ticket}: {comment}")
            return False

    def close_market_position(self, ticket: int) -> bool:
        """Close an open MT5 position completely at current market price."""
        if not hasattr(self, "_cached_pos_profits"):
            self._cached_pos_profits = {}

        if self.dry_run:
            self._dry_run_ticket_lots.pop(ticket, None)
            logger.info(f"[DRY-RUN] Closed Market Position #{ticket}")
            self._cached_pos_profits[ticket] = 1.25
            return True

        if not self._ensure_connected():
            logger.error(f"Cannot close position #{ticket}: MT5 is not connected.")
            return False

        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            logger.warning(f"Position #{ticket} not found to close (already closed).")
            return False
        p = pos[0]
        self._cached_pos_profits[ticket] = round(p.profit, 2)

        tick = self.get_tick(p.symbol)
        if not tick:
            return False

        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "price": close_price,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": "TG Copier Close",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        success, result = self.send_order_with_filling_fallback(request)
        if success:
            logger.info(f"Closed Position #{ticket} ({p.symbol} {p.volume} lots at {close_price})")
            return True
        return False

    def get_position_closed_profit(self, ticket: Optional[int]) -> float:
        """
        Retrieve realized net profit for a closed position ticket from MT5 history deals.
        Returns profit in account currency (e.g. GBP).
        """
        if not ticket:
            return 0.0

        if self.dry_run or not self._connected:
            return getattr(self, "_cached_pos_profits", {}).get(ticket, 1.25)

        try:
            import MetaTrader5 as mt5
            deals = mt5.history_deals_get(position=ticket)
            if deals:
                net_profit = sum(
                    getattr(d, "profit", 0.0) + getattr(d, "commission", 0.0) + getattr(d, "swap", 0.0)
                    for d in deals
                )
                return round(net_profit, 2)
        except Exception as e:
            logger.warning(f"Could not retrieve closed profit for #{ticket}: {e}")

        return getattr(self, "_cached_pos_profits", {}).get(ticket, 0.0)

    def get_position_close_info(self, ticket: Optional[int]) -> Dict[str, Any]:
        """
        Returns {"profit": float, "reason": str} for a closed position, using MT5 deal history.
        reason is one of: "TP", "SL", "MANUAL/BOT", "UNKNOWN".
        """
        profit = self.get_position_closed_profit(ticket)
        reason = "UNKNOWN"
        if not ticket or self.dry_run or not self._connected:
            return {"profit": profit, "reason": reason}
        try:
            deals = mt5.history_deals_get(position=ticket) or []
            exit_deals = [d for d in deals if getattr(d, "entry", 0) == getattr(mt5, "DEAL_ENTRY_OUT", 1)]
            if exit_deals:
                d = exit_deals[-1]
                d_reason = getattr(d, "reason", None)
                if d_reason == getattr(mt5, "DEAL_REASON_SL", 4):
                    reason = "SL"
                elif d_reason == getattr(mt5, "DEAL_REASON_TP", 5):
                    reason = "TP"
                else:
                    reason = "MANUAL/BOT"
        except Exception as e:
            logger.warning(f"Could not determine close reason for #{ticket}: {e}")
        return {"profit": profit, "reason": reason}

    def get_account_financials(self) -> Dict[str, Any]:
        """
        Retrieve current live balance, credit, equity, and currency from MT5.
        """
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
            import MetaTrader5 as mt5
            acc = mt5.account_info()
            if acc:
                bal = round(acc.balance, 2)
                cred = round(acc.credit, 2)
                eq = round(acc.equity, 2)
                pnl = round(getattr(acc, "profit", 0.0), 2)
                mgn = round(getattr(acc, "margin", 0.0), 2)
                free_mgn = round(getattr(acc, "margin_free", eq), 2)
                curr = acc.currency
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
        """Return current bot open market lots, pending limit lots, and total committed lots."""
        if self.dry_run or not self._connected:
            total = round(sum(self._dry_run_ticket_lots.values()), 2)
            return {"open_lots": total, "pending_lots": 0.0, "total_lots": total}

        try:
            positions = mt5.positions_get() or []
            orders = mt5.orders_get() or []
            open_lots = round(sum(p.volume for p in positions if getattr(p, "magic", 0) == MAGIC_NUMBER), 2)
            pending_lots = round(sum(o.volume_current for o in orders if getattr(o, "magic", 0) == MAGIC_NUMBER), 2)
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

    def check_and_cap_order_lot(self, requested_lot: float) -> Tuple[float, Optional[str]]:
        """
        Enforces the Professional Risk Guide High-Risk Cap:
        Never allows total active + pending lots to exceed high_risk_lots.
        """
        if not ENFORCE_HIGH_RISK_LOT_CAP:
            return round(requested_lot, 2), None

        # In default dry-run mode without an explicit test capital override, allow normal test execution
        if self.dry_run and self._dry_run_capital is None:
            return round(requested_lot, 2), None

        status = self.get_risk_exposure_status()
        avail = status["available_lots"]
        high_cap = status["high_risk_lots"]
        total_lots = status["total_lots"]
        cap = status["capital"]
        curr = status.get("currency", "GBP")

        if avail < 0.01 - 1e-9:
            reason = (
                f"High Risk Lot Cap Reached ({total_lots:.2f}/{high_cap:.2f} lots "
                f"for {cap:.2f} {curr} account)"
            )
            return 0.0, reason

        allowed = min(round(requested_lot, 2), avail)
        allowed = round(math.floor((allowed + 1e-9) * 100.0) / 100.0, 2)
        if allowed < 0.01:
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

    def get_daily_drawdown_status(self) -> Dict[str, Any]:
        """
        Two-Stage Daily Loss & Account Drawdown Monitor:
        - Stage 1 (DAILY_LOSS_PAUSE_PCT = 50%): Pause NEW signals, keep existing trades running naturally to SL/TP.
        - Stage 2 (EMERGENCY_CUTOFF_LOSS_PCT = 90%): Urgency cut-off immediately closes losing trades.
        """
        fin = self.get_account_financials()
        curr = fin.get("currency", "GBP")

        if self.dry_run or not self._connected:
            curr_bal = self._dry_run_capital if self._dry_run_capital is not None else 200.0
            realized_today = self._dry_run_daily_realized_pnl
            floating_pnl = self._dry_run_floating_pnl
        else:
            curr_bal = fin.get("balance", 0.0)
            realized_today = 0.0
            floating_pnl = 0.0
            try:
                now_dt = datetime.now()
                day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                deals = mt5.history_deals_get(day_start, now_dt) or []
                out_entries = {
                    getattr(mt5, "DEAL_ENTRY_OUT", 1),
                    getattr(mt5, "DEAL_ENTRY_INOUT", 2),
                    getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
                }
                for d in deals:
                    if getattr(d, "entry", 0) in out_entries:
                        realized_today += (
                            getattr(d, "profit", 0.0)
                            + getattr(d, "commission", 0.0)
                            + getattr(d, "swap", 0.0)
                        )
                positions = mt5.positions_get() or []
                for p in positions:
                    floating_pnl += getattr(p, "profit", 0.0) + getattr(p, "swap", 0.0)
            except Exception as e:
                logger.debug(f"Could not compute daily drawdown deals: {e}")

        realized_today = round(realized_today, 2)
        floating_pnl = round(floating_pnl, 2)
        ref_bal = max(curr_bal, round(curr_bal - min(0.0, realized_today), 2))
        floating_loss = max(0.0, round(-floating_pnl, 2))
        realized_loss = max(0.0, round(-realized_today, 2))
        total_daily_loss = round(realized_loss + floating_loss, 2)

        pause_limit = round(ref_bal * (DAILY_LOSS_PAUSE_PCT / 100.0), 2)
        cutoff_limit_ref = round(ref_bal * (EMERGENCY_CUTOFF_LOSS_PCT / 100.0), 2)
        cutoff_limit_curr = round(curr_bal * (EMERGENCY_CUTOFF_LOSS_PCT / 100.0), 2)

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

    def emergency_cutoff_losing_trades(self, tracked_market_tickets: Optional[List[int]] = None, tracked_pending_tickets: Optional[List[int]] = None) -> Dict[str, List[int]]:
        """
        90% Emergency Drawdown Cut-Off:
        Immediately closes all losing open positions at market and cancels all pending limit orders.
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
            return {"closed_tickets": closed_tickets, "cancelled_orders": cancelled_orders}

        try:
            positions = mt5.positions_get() or []
            for p in positions:
                if getattr(p, "magic", 0) == MAGIC_NUMBER or (tracked_market_tickets and p.ticket in tracked_market_tickets):
                    if getattr(p, "profit", 0.0) < 0.0 or tracked_market_tickets:
                        if self.close_market_position(p.ticket):
                            closed_tickets.append(p.ticket)

            orders = mt5.orders_get() or []
            for o in orders:
                if getattr(o, "magic", 0) == MAGIC_NUMBER or (tracked_pending_tickets and o.ticket in tracked_pending_tickets):
                    if self.cancel_pending_order(o.ticket):
                        cancelled_orders.append(o.ticket)
        except Exception as e:
            logger.error(f"Error during 90% emergency cut-off: {e}")

        return {"closed_tickets": closed_tickets, "cancelled_orders": cancelled_orders}

    def apply_selective_breakeven(
        self,
        layers: Dict[str, Dict[str, Any]],
        symbol: str,
        direction: str,
        be_layers: List[str] = BE_LAYERS,
        be_offset_pips: float = BE_OFFSET_PIPS,
    ) -> List[int]:
        """
        Selective Breakeven:
        Only moves SL to entry price on TOP and MID layers (be_layers).
        Leaves BOTTOM layer (0.02) untouched at original SL.
        Pending limit orders remain active.
        Returns list of tickets modified to BE.
        """
        pip_size = self.get_pip_size(symbol)
        offset_value = be_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = sym_info.digits if sym_info else 2

        modified_tickets = []

        for layer_name in be_layers:
            layer = layers.get(layer_name)
            if not layer:
                continue
            ticket = layer.get("ticket")
            if not ticket:
                continue

            if self.dry_run:
                logger.info(
                    f"[DRY-RUN] Moved Layer {layer_name} (Ticket #{ticket}) to Breakeven (+{be_offset_pips} pips offset)"
                )
                modified_tickets.append(ticket)
                continue

            pos = mt5.positions_get(ticket=ticket)
            if pos:
                p = pos[0]
                if p.type == mt5.ORDER_TYPE_BUY:
                    new_sl = round(p.price_open + offset_value, digits)
                else:
                    new_sl = round(p.price_open - offset_value, digits)

                if self.modify_position_sltp(ticket=ticket, sl=new_sl, tp=p.tp):
                    modified_tickets.append(ticket)
                    logger.info(
                        f"Layer {layer_name} (Ticket #{ticket}) moved to BE: OpenPrice={p.price_open}, "
                        f"NewSL={new_sl} (+{be_offset_pips} pips offset)"
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
        Identifies which open positions are NOT making the most money,
        moves those worst-performing position(s) to Breakeven (entry ± offset),
        and leaves the top 2 most profitable / bottom two layers untouched to run
        toward TP1 / TP2 / trailing SL above TP3.
        """
        pip_size = self.get_pip_size(symbol)
        offset_value = be_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = sym_info.digits if sym_info else 2

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
        for ticket in market_tickets:
            pos = mt5.positions_get(ticket=ticket)
            if pos:
                active_positions.append(pos[0])

        if not active_positions:
            return {"be_tickets": [], "held_tickets": [], "failed_be_tickets": []}

        # Sort by current profit descending: index 0 & 1 are making the most money (bottom two layers)
        active_positions.sort(key=lambda p: p.profit, reverse=True)
        held_positions = active_positions[:2]
        worst_positions = active_positions[2:]

        held_tickets = [p.ticket for p in held_positions]

        # Optionally extend TP on held runner positions to TP3 if TP3 is provided and further than current TP
        if raw_tp3:
            for hp in held_positions:
                is_buy = hp.type == mt5.ORDER_TYPE_BUY
                should_extend = (
                    hp.tp == 0.0
                    or (is_buy and raw_tp3 > hp.tp)
                    or (not is_buy and raw_tp3 < hp.tp)
                )
                if should_extend:
                    self.modify_position_sltp(ticket=hp.ticket, sl=hp.sl, tp=round(raw_tp3, digits))

        for wp in worst_positions:
            if wp.type == mt5.ORDER_TYPE_BUY:
                new_sl = round(wp.price_open + offset_value, digits)
            else:
                new_sl = round(wp.price_open - offset_value, digits)

            if self.modify_position_sltp(ticket=wp.ticket, sl=new_sl, tp=wp.tp):
                be_tickets.append(wp.ticket)
                logger.info(
                    f"Worst entry Ticket #{wp.ticket} (profit={wp.profit:.2f}) moved to BE: "
                    f"Open={wp.price_open}, NewSL={new_sl} | Holding top 2 layers {held_tickets}"
                )
            else:
                err = self.last_error or "Broker rejected BE SL"
                failed_be_tickets.append((wp.ticket, err))
                logger.warning(f"Could not move worst entry #{wp.ticket} to BE ({err})")

        return {
            "be_tickets": be_tickets,
            "held_tickets": held_tickets,
            "failed_be_tickets": failed_be_tickets,
        }

    # Compatibility alias for breakeven
    def apply_breakeven(
        self,
        market_tickets: List[int],
        pending_tickets: List[int],
        symbol: str,
        direction: str,
        be_offset_pips: float = BE_OFFSET_PIPS,
        layers: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> bool:
        """
        Applies breakeven. If layer information is provided, uses selective BE on TOP/MID.
        Otherwise falls back to all market tickets.
        """
        if layers:
            self.apply_selective_breakeven(
                layers=layers,
                symbol=symbol,
                direction=direction,
                be_layers=BE_LAYERS,
                be_offset_pips=be_offset_pips,
            )
            return True

        # Fallback without layer dict
        pip_size = self.get_pip_size(symbol)
        offset_value = be_offset_pips * pip_size
        sym_info = self.get_symbol_info(symbol)
        digits = sym_info.digits if sym_info else 2

        for ticket in market_tickets:
            if self.dry_run:
                logger.info(f"[DRY-RUN] Moved SL to Breakeven for Position #{ticket}")
                continue
            pos = mt5.positions_get(ticket=ticket)
            if pos:
                p = pos[0]
                new_sl = round(p.price_open + offset_value, digits) if p.type == mt5.ORDER_TYPE_BUY else round(p.price_open - offset_value, digits)
                self.modify_position_sltp(ticket=ticket, sl=new_sl, tp=p.tp)

        return True

    def get_bot_positions(self, symbol: Optional[str] = None) -> List[Any]:
        """Retrieve all active MT5 market positions placed by this bot (magic == MAGIC_NUMBER)."""
        if self.dry_run or not self._connected:
            return []
        resolved = self.resolve_symbol(symbol) if symbol else None
        positions = mt5.positions_get(symbol=resolved) if resolved else mt5.positions_get()
        if not positions:
            return []
        return [p for p in positions if getattr(p, "magic", 0) == MAGIC_NUMBER]

    def get_bot_pending_orders(self, symbol: Optional[str] = None) -> List[Any]:
        """Retrieve all unfilled MT5 pending orders placed by this bot (magic == MAGIC_NUMBER)."""
        if self.dry_run or not self._connected:
            return []
        resolved = self.resolve_symbol(symbol) if symbol else None
        orders = mt5.orders_get(symbol=resolved) if resolved else mt5.orders_get()
        if not orders:
            return []
        return [o for o in orders if getattr(o, "magic", 0) == MAGIC_NUMBER]

    def apply_proximity_close(
        self,
        market_tickets: List[int],
        pending_tickets: List[int],
        symbol: Optional[str] = None,
    ) -> None:
        """
        20-Pip TP1 Proximity Auto-Close:
        1. Closes all recorded market positions.
        2. Cancels all recorded pending limits.
        3. Safety Sweep: Cross-references MAGIC_NUMBER to guarantee no filled limit order is left orphaned.
        """
        closed_set = set()
        for ticket in list(market_tickets):
            if self.close_market_position(ticket):
                closed_set.add(ticket)

        for ticket in list(pending_tickets):
            self.cancel_pending_order(ticket)

        # Magic Number Safety Sweep
        if not self.dry_run and self._connected:
            bot_positions = self.get_bot_positions(symbol)
            for p in bot_positions:
                if p.ticket not in closed_set:
                    logger.info(f"Safety sweep: Closing unrecorded filled bot position #{p.ticket} ({p.symbol})")
                    self.close_market_position(p.ticket)

            bot_orders = self.get_bot_pending_orders(symbol)
            for o in bot_orders:
                logger.info(f"Safety sweep: Cancelling remaining bot pending order #{o.ticket} ({o.symbol})")
                self.cancel_pending_order(o.ticket)

        logger.info(
            f"Proximity Auto-Close executed: Closed {len(market_tickets)} market positions, "
            f"Cancelled {len(pending_tickets)} pending orders."
        )

    apply_full_close = apply_proximity_close

    def handle_tp1_scaling_and_select_runner(self, signal_data: Dict[str, Any]) -> Optional[int]:
        """
        Implements TP1_SCALING_AND_PHONE_CONTROLS and RUNNER_TRAILING_SPEC Section 5 Step A:
        1. Always closes TOP (0.01) if open.
        2. Cancels all unfilled pending limits (BOTTOM, MID).
        3. If BOTTOM is open: closes MID (if open), sets BOTTOM (0.02) as the designated runner.
           Elif MID is open: sets MID (0.01) as the designated runner.
           Else: no runner (trade finished).
        4. Sets soft_tp1_floor = TP1 - 0.50 (BUY) or TP1 + 0.50 (SELL).
        5. Attempts immediate broker SL lock to floor_price; if rejected, soft_tp1_floor protects in software!
        Returns runner_ticket or None.
        """
        layers = signal_data.get("layers", {})
        direction = signal_data.get("direction", "BUY").upper()
        raw_tp1 = signal_data.get("raw_tp1")
        symbol = signal_data.get("symbol", "XAUUSD.s")
        floor_price = round(raw_tp1 - TP1_LOCK_BUFFER, 2) if direction == "BUY" else round(raw_tp1 + TP1_LOCK_BUFFER, 2)

        market_tickets = list(signal_data.get("market_tickets", []))
        pending_tickets = list(signal_data.get("pending_tickets", []))
        active_set = set(market_tickets)

        # Cancel all unfilled pending limit orders
        for pt in pending_tickets:
            self.cancel_pending_order(pt)
        signal_data["pending_tickets"] = []

        top_ticket = layers.get("TOP", {}).get("ticket") if layers else None
        mid_ticket = layers.get("MID", {}).get("ticket") if layers else None
        bot_ticket = layers.get("BOTTOM", {}).get("ticket") if layers else None

        top_open = top_ticket is not None and top_ticket in active_set
        mid_open = mid_ticket is not None and mid_ticket in active_set
        bot_open = bot_ticket is not None and bot_ticket in active_set

        banked_tickets = []

        # Always close TOP if open
        if top_open and top_ticket:
            self.close_market_position(top_ticket)
            banked_tickets.append(top_ticket)
            if "TOP" in layers:
                layers["TOP"]["status"] = "CLOSED"

        runner_ticket = None
        runner_layer = None

        if bot_open and bot_ticket:
            # All 3 open (or at least BOTTOM open):
            if mid_open and mid_ticket:
                self.close_market_position(mid_ticket)
                banked_tickets.append(mid_ticket)
                if "MID" in layers:
                    layers["MID"]["status"] = "CLOSED"
            runner_ticket = bot_ticket
            runner_layer = "BOTTOM"
            if "BOTTOM" in layers:
                layers["BOTTOM"]["status"] = "RUNNER"
        elif mid_open and mid_ticket:
            # TOP + MID open:
            runner_ticket = mid_ticket
            runner_layer = "MID"
            if "MID" in layers:
                layers["MID"]["status"] = "RUNNER"
        else:
            # Only TOP was open (or single order)
            runner_ticket = None
            runner_layer = None

        signal_data["last_banked_tickets"] = banked_tickets
        signal_data["runner_layer"] = runner_layer
        signal_data["runner_ticket"] = runner_ticket
        signal_data["soft_tp1_floor"] = floor_price
        signal_data["runner_peak_price"] = raw_tp1
        signal_data["tp2_shortfall_armed"] = False
        signal_data["tp1_reached"] = True
        signal_data["market_tickets"] = [runner_ticket] if runner_ticket else []
        signal_data["status"] = "RUNNER_ACTIVE" if runner_ticket else "CLOSED"

        # Attempt immediate broker SL lock; if broker stop level rejects, soft_tp1_floor protects in software
        if runner_ticket:
            self.modify_position_sltp(runner_ticket, sl=floor_price, tp=None)

        return runner_ticket

    def manage_designated_runner(self, signal_data: Dict[str, Any]) -> bool:
        """
        Implements RUNNER_TRAILING_SPEC Section 5 Step B:
        Protects the single designated runner (MID or BOTTOM) using:
        1. Virtual TP1 Floor (instant market close if price drops back to TP1 +/- 0.50)
        2. TP2 Shortfall High-Water Mark Guard (instant market close if surges near TP2 then drops $0.80)
        3. One-Way Hard Broker SL Trailing (ratchets SL upward in $0.50+ steps)
        Returns True if runner is still active, False if runner was closed.
        """
        ticket = signal_data.get("runner_ticket")
        if not ticket:
            return False

        signal_data["last_runner_ticket"] = ticket
        direction = signal_data.get("direction", "BUY").upper()
        symbol = signal_data.get("symbol", "XAUUSD.s")
        floor_price = signal_data.get("soft_tp1_floor")
        raw_tp1 = signal_data.get("raw_tp1")
        raw_tp2 = signal_data.get("raw_tp2")
        raw_tp3 = signal_data.get("raw_tp3")
        using_tp3 = bool(signal_data.get("hold_past_tp2") and raw_tp3)
        runner_target = raw_tp3 if using_tp3 else raw_tp2
        target_label = "TP3" if using_tp3 else "TP2"

        if self.dry_run:
            tick = self.get_tick(symbol)
            if not tick:
                return True
            pos_sl = signal_data.get("soft_tp1_floor", 0.0)
            min_stop_dist = 0.50
        else:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = "Trailed Stop Loss Hit (Locked in Gain)"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False
            pos = positions[0]
            pos_sl = pos.sl
            sym_info = self.get_symbol_info(pos.symbol)
            tick = self.get_tick(pos.symbol)
            if not sym_info or not tick:
                return True
            min_stop_dist = (sym_info.trade_stops_level * sym_info.point) if hasattr(sym_info, "trade_stops_level") else 0.50

        trail_dist = TRAIL_DISTANCE_PIPS * 0.10
        trail_step = TRAIL_STEP_PIPS * 0.10
        tp2_arm_dist = TP2_SHORTFALL_ARM_PIPS * 0.10
        tp2_pullback_dist = TP2_SHORTFALL_PULLBACK_PIPS * 0.10
        no_tp2_arm_dist = NO_TP2_SURGE_ARM_PIPS * 0.10

        if direction == "BUY":
            live_price = tick.bid

            # 1. VIRTUAL TP1 FLOOR CHECK
            if floor_price is not None and live_price <= floor_price:
                logger.info(f"Runner #{ticket}: Virtual TP1 floor hit (live={live_price} <= floor={floor_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"Virtual TP1 Floor Protected ({floor_price})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            # 2. TP2 / TP3 REACHED TARGET CHECK
            if runner_target and live_price >= runner_target:
                logger.info(f"Runner #{ticket}: Reached Target {target_label} ({runner_target})! Closing at market to secure maximum profit.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["tp2_reached"] = True
                signal_data["runner_close_reason"] = f"Target {target_label} Reached ({runner_target})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            # 3. TP2 SHORTFALL HIGH-WATER MARK GUARD
            signal_data["runner_peak_price"] = max(signal_data.get("runner_peak_price") or live_price, live_price)
            peak = signal_data["runner_peak_price"]

            arm_level = round(runner_target - tp2_arm_dist, 2) if runner_target else round(raw_tp1 + no_tp2_arm_dist, 2)
            if live_price >= arm_level:
                signal_data["tp2_shortfall_armed"] = True

            if signal_data.get("tp2_shortfall_armed") and live_price <= round(peak - tp2_pullback_dist, 2):
                logger.info(f"Runner #{ticket}: TP2 Shortfall triggered (peak={peak}, live={live_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"TP2 Shortfall Guard Triggered (Peak: {peak})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            # 4. ONE-WAY HARD BROKER SL TRAILING (Never below floor_price)
            candidate_sl = max(floor_price or 0.0, round(live_price - trail_dist, 2))
            if candidate_sl >= round((pos_sl or 0.0) + trail_step, 2) and (live_price - candidate_sl) >= min_stop_dist:
                logger.info(f"Runner #{ticket}: Trailing SL ratchet -> {candidate_sl} (live={live_price})")
                self.modify_position_sltp(ticket, sl=candidate_sl, tp=None)

        elif direction == "SELL":
            live_price = tick.ask

            # 1. VIRTUAL TP1 FLOOR CHECK
            if floor_price is not None and live_price >= floor_price:
                logger.info(f"Runner #{ticket}: Virtual TP1 floor hit (live={live_price} >= floor={floor_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"Virtual TP1 Floor Protected ({floor_price})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            # 2. TP2 / TP3 REACHED TARGET CHECK
            if runner_target and live_price <= runner_target:
                logger.info(f"Runner #{ticket}: Reached Target {target_label} ({runner_target})! Closing at market to secure maximum profit.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["tp2_reached"] = True
                signal_data["runner_close_reason"] = f"Target {target_label} Reached ({runner_target})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            # 3. TP2 SHORTFALL HIGH-WATER MARK GUARD
            signal_data["runner_peak_price"] = min(signal_data.get("runner_peak_price") or live_price, live_price)
            peak = signal_data["runner_peak_price"]

            arm_level = round(runner_target + tp2_arm_dist, 2) if runner_target else round(raw_tp1 - no_tp2_arm_dist, 2)
            if live_price <= arm_level:
                signal_data["tp2_shortfall_armed"] = True

            if signal_data.get("tp2_shortfall_armed") and live_price >= round(peak + tp2_pullback_dist, 2):
                logger.info(f"Runner #{ticket}: TP2 Shortfall triggered (peak={peak}, live={live_price}). Closing at market.")
                self.close_market_position(ticket)
                signal_data["last_runner_ticket"] = ticket
                signal_data["runner_close_reason"] = f"TP2 Shortfall Guard Triggered (Peak: {peak})"
                signal_data["runner_ticket"] = None
                signal_data["status"] = "CLOSED"
                signal_data["market_tickets"] = []
                return False

            # 4. ONE-WAY HARD BROKER SL TRAILING (Never above floor_price)
            candidate_sl = min(floor_price or 999999.0, round(live_price + trail_dist, 2))
            if (pos_sl == 0.0 or candidate_sl <= round(pos_sl - trail_step, 2)) and (candidate_sl - live_price) >= min_stop_dist:
                logger.info(f"Runner #{ticket}: Trailing SL ratchet -> {candidate_sl} (live={live_price})")
                self.modify_position_sltp(ticket, sl=candidate_sl, tp=None)

        return True

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
            "layers": dict(layers),
            "market_tickets": list(market_tickets),
            "pending_tickets": list(pending_tickets),
            "symbol": symbol,
            "direction": direction,
            "raw_tp1": raw_tp1,
            "raw_tp2": raw_tp2,
        }
        runner_t = self.handle_tp1_scaling_and_select_runner(sig_data)

        banked = []
        if layers.get("TOP", {}).get("ticket") in market_tickets:
            banked.append("TOP")
        if sig_data.get("runner_layer") == "BOTTOM" and layers.get("MID", {}).get("ticket") in market_tickets:
            banked.append("MID")

        return {
            "status": sig_data["status"],
            "banked": banked,
            "runner": sig_data.get("runner_layer"),
            "runner_ticket": runner_t,
            "remaining_market_tickets": sig_data["market_tickets"],
            "trailed_sl": sig_data.get("soft_tp1_floor"),
            "soft_tp1_floor": sig_data.get("soft_tp1_floor"),
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
                logger.info(f"[DRY-RUN] Partial close: closed Order #{closed}, moving rest to BE.")
                return closed
            elif len(market_tickets) == 1:
                logger.info(f"[DRY-RUN] Only 1 order open; moved SL to BE.")
                return None
            return None

        active_positions = []
        for ticket in market_tickets:
            pos = mt5.positions_get(ticket=ticket)
            if pos:
                active_positions.append(pos[0])

        if not active_positions:
            return None

        if len(active_positions) == 1:
            self.apply_breakeven(
                market_tickets=[active_positions[0].ticket],
                pending_tickets=pending_tickets,
                symbol=symbol,
                direction=direction,
            )
            return None

        active_positions.sort(key=lambda p: p.profit, reverse=True)
        order_to_close = active_positions[0]
        closed_ticket = order_to_close.ticket

        if self.close_market_position(closed_ticket):
            remaining = [p.ticket for p in active_positions if p.ticket != closed_ticket]
            if remaining:
                self.apply_breakeven(
                    market_tickets=remaining,
                    pending_tickets=pending_tickets,
                    symbol=symbol,
                    direction=direction,
                )
            return closed_ticket

        return None
