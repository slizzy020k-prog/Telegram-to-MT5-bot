import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config as _config

logger = logging.getLogger("copier.ledger")

_DEFAULT_LEDGER_PATH = Path(
    getattr(_config, "TRADES_LEDGER_PATH", Path(__file__).resolve().parent / "trades_ledger.jsonl")
)
_FILLED_LAYER_STATUSES = {"OPEN", "FILLED", "RUNNER", "BANKED"}
_TERMINAL_UNFILLED_STATUSES = {"CANCELLED", "EXPIRED", "ABORTED"}


def _safe_float(val: Any, default: float = 0.0) -> float:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any) -> Optional[int]:
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    try:
        if isinstance(val, str) and val.strip().lstrip("-").isdigit():
            return int(val.strip())
    except (TypeError, ValueError):
        pass
    return None


def _infer_entry_type(signal_data: Dict[str, Any]) -> str:
    if signal_data.get("is_zone") or signal_data.get("is_zone_order"):
        return "ZONE"
    if signal_data.get("is_stop_order") or str(signal_data.get("order_intent", "")).upper() == "STOP":
        return "STOP"
    if signal_data.get("single_price") is not None:
        return "LIMIT"
    return "MARKET"


def _extract_layers_summary(signal_data: Dict[str, Any]) -> Tuple[float, float, List[str], List[int]]:
    """Compute (total_lots, filled_lots, layers_filled, unique_tickets) from signal metadata."""
    layers = signal_data.get("layers") or {}
    market_set = {_safe_int(t) for t in (signal_data.get("market_tickets") or [])} - {None}
    accounted_set = {_safe_int(t) for t in (signal_data.get("accounted_tickets") or [])} - {None}
    known_filled_tickets = market_set | accounted_set

    total_lots = 0.0
    filled_lots = 0.0
    layers_filled: List[str] = []
    tickets: List[int] = []

    def _add_ticket(raw_t: Any) -> Optional[int]:
        t_int = _safe_int(raw_t)
        if t_int is not None and t_int > 0 and t_int not in tickets:
            tickets.append(t_int)
        return t_int

    if isinstance(layers, dict):
        for layer_name, l_data in layers.items():
            if not isinstance(l_data, dict):
                continue
            lot = max(0.0, _safe_float(l_data.get("lot"), 0.0))
            total_lots += lot
            t_int = _add_ticket(l_data.get("ticket"))
            st = str(l_data.get("status") or "").upper()
            order_type = str(l_data.get("order_type") or "").upper()

            is_filled = False
            if st in _TERMINAL_UNFILLED_STATUSES:
                is_filled = False
            elif t_int is not None and t_int in known_filled_tickets:
                is_filled = True
            elif st in _FILLED_LAYER_STATUSES:
                is_filled = True
            elif st == "CLOSED" and t_int is not None:
                is_filled = order_type == "MARKET" or not known_filled_tickets

            if is_filled:
                filled_lots += lot
                layers_filled.append(str(layer_name))

    for src_key in (
        "accounted_tickets",
        "mt5_order_tickets",
        "market_tickets",
        "pending_tickets",
        "runner_tickets",
        "last_runner_tickets",
    ):
        for raw_t in signal_data.get(src_key) or []:
            _add_ticket(raw_t)
    _add_ticket(signal_data.get("runner_ticket"))
    _add_ticket(signal_data.get("last_runner_ticket"))

    return round(total_lots, 4), round(filled_lots, 4), layers_filled, tickets


def build_trade_record(
    signal_key: str,
    signal_data: Dict[str, Any],
    realized_pnl: float,
    exit_reason: str,
    currency: str = "GBP",
    dry_run: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a canonical v3.0 trades_ledger.jsonl record for a closed signal."""
    total_lots, filled_lots, layers_filled, tickets = _extract_layers_summary(signal_data)
    cleared = list(signal_data.get("runner_cleared_milestones") or [])
    if cleared:
        highest_cleared: Optional[str] = str(cleared[-1])
    elif signal_data.get("tp1_reached"):
        highest_cleared = "TP1"
    else:
        highest_cleared = None

    raw_mid = signal_data.get("message_id")
    if raw_mid is None:
        tail = str(signal_key).rsplit(":", 1)[-1]
        raw_mid = int(tail) if tail.lstrip("-").isdigit() else tail

    record: Dict[str, Any] = {
        "schema_version": 3,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "opened_at": signal_data.get("timestamp"),
        "signal_key": str(signal_key),
        "chat_id": signal_data.get("chat_id"),
        "message_id": raw_mid,
        "symbol": signal_data.get("symbol", getattr(_config, "DEFAULT_SYMBOL", "XAUUSD.s")),
        "direction": str(signal_data.get("direction") or "BUY").upper(),
        "entry_type": _infer_entry_type(signal_data),
        "total_lots": total_lots,
        "filled_lots": filled_lots,
        "tickets": tickets,
        "layers_filled": layers_filled,
        "highest_milestone_cleared": highest_cleared,
        "runner_layer": signal_data.get("runner_layer"),
        "runner_layers": list(
            signal_data.get("runner_layers")
            or ([signal_data["runner_layer"]] if signal_data.get("runner_layer") else [])
        ),
        "exit_reason": str(exit_reason or "CLOSED"),
        "realized_pnl": round(_safe_float(realized_pnl, 0.0), 2),
        "currency": str(currency or "GBP"),
        "dry_run": bool(dry_run),
    }
    if extra:
        record.update(extra)
    return record


class TradeLedger:
    """Thread-safe append-only JSONL trade ledger writer & analytics reader for /health."""

    def __init__(self, file_path: Optional[Path] = None):
        configured = getattr(_config, "TRADES_LEDGER_PATH", _DEFAULT_LEDGER_PATH)
        self.file_path = Path(file_path or configured)
        self._lock = threading.Lock()

    def append_trade(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Append a single closed-trade record atomically as one JSON line."""
        entry = dict(record)
        entry.setdefault("schema_version", 3)
        entry.setdefault("closed_at", datetime.now(timezone.utc).isoformat())
        entry["realized_pnl"] = round(_safe_float(entry.get("realized_pnl"), 0.0), 2)

        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        return entry

    def record_closed_signal(
        self,
        signal_key: str,
        signal_data: Dict[str, Any],
        realized_pnl: float,
        exit_reason: str,
        currency: str = "GBP",
        dry_run: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Construct and append a canonical closed-signal ledger entry."""
        rec = build_trade_record(
            signal_key=signal_key,
            signal_data=signal_data,
            realized_pnl=realized_pnl,
            exit_reason=exit_reason,
            currency=currency,
            dry_run=dry_run,
            extra=extra,
        )
        return self.append_trade(rec)

    def read_trades(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Read parsed trade records from the JSONL ledger (skips corrupt lines safely)."""
        with self._lock:
            if not self.file_path.exists():
                return []
            trades: List[Dict[str, Any]] = []
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    for line_no, raw_line in enumerate(f, start=1):
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        try:
                            obj = json.loads(stripped)
                            if isinstance(obj, dict):
                                trades.append(obj)
                        except json.JSONDecodeError as exc:
                            logger.warning(f"Skipping malformed ledger line {line_no} in {self.file_path}: {exc}")
            except Exception as exc:
                logger.error(f"Failed to read trade ledger {self.file_path}: {exc}")
                return []

        if limit is not None and limit > 0:
            return trades[-limit:]
        return trades

    def get_stats(self, server_date: Optional[str] = None) -> Dict[str, Any]:
        """Compute win-rate and realized PnL summary from trades_ledger.jsonl."""
        trades = self.read_trades()
        target_date = server_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        total_trades = len(trades)
        wins = 0
        losses = 0
        breakevens = 0
        total_pnl = 0.0
        today_trades = 0
        today_pnl = 0.0
        pnls: List[float] = []

        for t in trades:
            pnl = round(_safe_float(t.get("realized_pnl"), 0.0), 2)
            pnls.append(pnl)
            total_pnl += pnl
            if pnl > 0.0:
                wins += 1
            elif pnl < 0.0:
                losses += 1
            else:
                breakevens += 1

            rec_date = t.get("server_date")
            if not rec_date:
                closed_at = str(t.get("closed_at") or "")
                rec_date = closed_at[:10] if len(closed_at) >= 10 else ""
            if rec_date == target_date:
                today_trades += 1
                today_pnl += pnl

        win_rate = round((wins / total_trades) * 100.0, 1) if total_trades > 0 else 0.0
        avg_pnl = round(total_pnl / total_trades, 2) if total_trades > 0 else 0.0
        last_trade = trades[-1] if trades else {}

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
            "win_rate_pct": win_rate,
            "total_realized_pnl": round(total_pnl, 2),
            "today_trades": today_trades,
            "today_realized_pnl": round(today_pnl, 2),
            "avg_pnl": avg_pnl,
            "best_trade_pnl": round(max(pnls), 2) if pnls else 0.0,
            "worst_trade_pnl": round(min(pnls), 2) if pnls else 0.0,
            "last_exit_reason": last_trade.get("exit_reason"),
            "last_closed_at": last_trade.get("closed_at"),
        }
