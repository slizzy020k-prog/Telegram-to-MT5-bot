import os
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from config import STATE_FILE_PATH, LAYER_LOTS

logger = logging.getLogger("copier.state")


class StateManager:
    """
    Persistent JSON database linking Telegram message_id -> tagged layer tickets & MT5 orders.
    Thread-safe with atomic file writes to survive crashes, restarts, and concurrent events.
    """

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = Path(file_path or STATE_FILE_PATH)
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    def _load_state(self) -> None:
        """Load state from JSON file or initialize empty state."""
        with self._lock:
            if not self.file_path.exists():
                self._state = {}
                self._save_state_locked()
                return

            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        self._state = json.loads(content)
                    else:
                        self._state = {}
            except Exception as e:
                logger.error(f"Failed to load state file {self.file_path}: {e}. Creating backup and resetting.")
                backup_path = self.file_path.with_suffix(f".bak.{int(datetime.now().timestamp())}")
                try:
                    self.file_path.rename(backup_path)
                except Exception:
                    pass
                self._state = {}
                self._save_state_locked()

    def _save_state_locked(self) -> None:
        """Atomic write to disk to prevent corrupted JSON files."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.file_path.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
            temp_file.replace(self.file_path)
        except Exception as e:
            logger.error(f"Failed to atomically write state file {self.file_path}: {e}")
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    def get_signal(self, message_id: int | str) -> Optional[Dict[str, Any]]:
        """Retrieve signal state by Telegram message ID."""
        key = str(message_id)
        with self._lock:
            signal = self._state.get(key)
            return dict(signal) if signal else None

    def create_signal(
        self,
        message_id: int | str,
        symbol: str,
        direction: str,
        layers: Optional[Dict[str, Dict[str, Any]]] = None,
        zone: Optional[Dict[str, float]] = None,
        market_tickets: Optional[List[int]] = None,
        pending_tickets: Optional[List[int]] = None,
        zone_completed: bool = False,
        sl: Optional[float] = None,
        raw_tp1: Optional[float] = None,
        raw_tp2: Optional[float] = None,
        raw_tp3: Optional[float] = None,
        final_tp: Optional[float] = None,
        proximity_close_price: Optional[float] = None,
        max_orders: int = 3,
        is_zone: bool = True,
        raw_text: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Create and persist a new layer-tagged signal record."""
        key = str(message_id)
        timestamp = datetime.now(timezone.utc).isoformat()

        # Build default layer structure if not provided
        if layers is None:
            layers = {
                "TOP": {
                    "lot": LAYER_LOTS.get("TOP", 0.01),
                    "target_price": zone.get("top") if zone else None,
                    "ticket": None,
                    "order_type": "MARKET",
                    "status": "PENDING",
                },
                "MID": {
                    "lot": LAYER_LOTS.get("MID", 0.01),
                    "target_price": zone.get("mid") if zone else None,
                    "ticket": None,
                    "order_type": "LIMIT",
                    "status": "PENDING",
                },
                "BOTTOM": {
                    "lot": LAYER_LOTS.get("BOTTOM", 0.02),
                    "target_price": zone.get("bottom") if zone else None,
                    "ticket": None,
                    "order_type": "LIMIT",
                    "status": "PENDING",
                },
            }

        signal_record = {
            "timestamp": timestamp,
            "symbol": symbol,
            "direction": direction,
            "layers": layers,
            "zone": zone,
            "max_orders": max_orders,
            "is_zone": is_zone,
            "market_tickets": market_tickets or [],
            "pending_tickets": pending_tickets or [],
            "zone_completed": zone_completed,
            "sl": sl,
            "raw_tp1": raw_tp1,
            "raw_tp2": raw_tp2,
            "raw_tp3": raw_tp3,
            "hard_broker_tp": final_tp,
            "final_tp": final_tp,
            "proximity_close_price": proximity_close_price,
            "tp1_reached": False,
            "shortfall_armed": False,
            "peak_price": None,
            "runner_layer": None,
            "runner_ticket": None,
            "soft_tp1_floor": None,
            "tp2_shortfall_armed": False,
            "runner_peak_price": None,
            "status": "ACTIVE",
            "raw_text": raw_text,
        }
        signal_record.update(kwargs)

        with self._lock:
            self._state[key] = signal_record
            self._save_state_locked()

        logger.info(f"State saved for message_id={key} (Symbol={symbol}, Direction={direction})")
        return signal_record

    def update_signal(self, message_id: int | str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update fields on an existing signal record."""
        key = str(message_id)
        with self._lock:
            if key not in self._state:
                logger.warning(f"Attempted to update non-existent signal: {key}")
                return None
            self._state[key].update(updates)
            self._save_state_locked()
            return dict(self._state[key])

    def update_layer(
        self,
        message_id: int | str,
        layer_name: str,
        ticket: Optional[int] = None,
        status: Optional[str] = None,
        order_type: Optional[str] = None,
        target_price: Optional[float] = None,
    ) -> None:
        """Update a specific layer (TOP, MID, or BOTTOM) in the signal record."""
        key = str(message_id)
        with self._lock:
            if key in self._state:
                layers = self._state[key].setdefault("layers", {})
                layer = layers.setdefault(layer_name, {})
                if ticket is not None:
                    layer["ticket"] = ticket
                if status is not None:
                    layer["status"] = status
                if order_type is not None:
                    layer["order_type"] = order_type
                if target_price is not None:
                    layer["target_price"] = target_price
                self._save_state_locked()

    def add_market_ticket(self, message_id: int | str, ticket: int) -> None:
        """Add a market ticket to the signal record."""
        key = str(message_id)
        with self._lock:
            if key in self._state:
                tickets = self._state[key].setdefault("market_tickets", [])
                if ticket not in tickets:
                    tickets.append(ticket)
                self._save_state_locked()

    def add_pending_ticket(self, message_id: int | str, ticket: int) -> None:
        """Add a pending ticket to the signal record."""
        key = str(message_id)
        with self._lock:
            if key in self._state:
                tickets = self._state[key].setdefault("pending_tickets", [])
                if ticket not in tickets:
                    tickets.append(ticket)
                self._save_state_locked()

    def remove_pending_ticket(self, message_id: int | str, ticket: int) -> None:
        """Remove a pending ticket from the signal record."""
        key = str(message_id)
        with self._lock:
            if key in self._state:
                tickets = self._state[key].get("pending_tickets", [])
                if ticket in tickets:
                    tickets.remove(ticket)
                    self._save_state_locked()

    def set_tp1_reached(self, message_id: int | str, reached: bool = True) -> None:
        """Mark tp1_reached status for a signal."""
        key = str(message_id)
        with self._lock:
            if key in self._state:
                self._state[key]["tp1_reached"] = reached
                self._save_state_locked()

    def get_active_signals(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Return all signals that are currently active and not fully closed."""
        with self._lock:
            active = []
            for msg_id, data in self._state.items():
                if data.get("status") in ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE"):
                    active.append((msg_id, dict(data)))
            return active

    def count_open_signals(self) -> int:
        """Count active open signals."""
        with self._lock:
            return sum(
                1 for data in self._state.values()
                if data.get("status") in ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE")
            )

    def _sort_key(self, item: Tuple[str, Dict[str, Any]]):
        """Sort helper: parse ISO timestamp robustly with numeric msg_id tie-breaker."""
        msg_id_str, val = item
        ts_str = val.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            ts = datetime.min.replace(tzinfo=timezone.utc)
        numeric_id = int(msg_id_str) if msg_id_str.isdigit() else 0
        return (ts, numeric_id)

    def get_latest_signal(self, symbol: Optional[str] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Get the most recent active signal."""
        with self._lock:
            sorted_items = sorted(
                self._state.items(),
                key=self._sort_key,
                reverse=True,
            )
            for msg_id, data in sorted_items:
                if data.get("status") in ["ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE"]:
                    if symbol is None or data.get("symbol", "").upper() == symbol.upper():
                        return msg_id, dict(data)

            # If no active found, return the latest of any status matching symbol
            for msg_id, data in sorted_items:
                if symbol is None or data.get("symbol", "").upper() == symbol.upper():
                    return msg_id, dict(data)

            return None

    def cleanup_old_signals(self, days: int = 7) -> int:
        """Remove terminal signals older than N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal_statuses = {"CLOSED", "FULL_CLOSED"}
        to_remove = []

        with self._lock:
            for key, val in self._state.items():
                if val.get("status") not in terminal_statuses:
                    continue
                ts_str = val.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    to_remove.append(key)

            for key in to_remove:
                del self._state[key]

            if to_remove:
                self._save_state_locked()
                logger.info(f"cleanup_old_signals: removed {len(to_remove)} old closed signals.")

        return len(to_remove)

    def get_all_signals(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of the full state."""
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}
