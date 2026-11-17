import os
import copy
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
                        loaded = json.loads(content)
                        self._state = loaded if isinstance(loaded, dict) else {}
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
        """Retrieve signal state by Telegram message ID (ignores internal _ metadata keys)."""
        key = str(message_id)
        if key.startswith("_"):
            return None
        with self._lock:
            signal = self._state.get(key)
            return copy.deepcopy(signal) if isinstance(signal, dict) else None

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
        return copy.deepcopy(signal_record)

    def update_signal(self, message_id: int | str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update fields on an existing signal record."""
        key = str(message_id)
        if key.startswith("_"):
            return None
        with self._lock:
            existing = self._state.get(key)
            if not isinstance(existing, dict):
                logger.warning(f"Attempted to update non-existent signal: {key}")
                return None
            existing.update(updates)
            self._save_state_locked()
            return copy.deepcopy(existing)

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
        if key.startswith("_"):
            return
        with self._lock:
            rec = self._state.get(key)
            if isinstance(rec, dict):
                layers = rec.setdefault("layers", {})
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
        if key.startswith("_"):
            return
        with self._lock:
            rec = self._state.get(key)
            if isinstance(rec, dict):
                tickets = rec.setdefault("market_tickets", [])
                if ticket not in tickets:
                    tickets.append(ticket)
                self._save_state_locked()

    def add_pending_ticket(self, message_id: int | str, ticket: int) -> None:
        """Add a pending ticket to the signal record."""
        key = str(message_id)
        if key.startswith("_"):
            return
        with self._lock:
            rec = self._state.get(key)
            if isinstance(rec, dict):
                tickets = rec.setdefault("pending_tickets", [])
                if ticket not in tickets:
                    tickets.append(ticket)
                self._save_state_locked()

    def remove_pending_ticket(self, message_id: int | str, ticket: int) -> None:
        """Remove a pending ticket from the signal record."""
        key = str(message_id)
        if key.startswith("_"):
            return
        with self._lock:
            rec = self._state.get(key)
            if isinstance(rec, dict):
                tickets = rec.get("pending_tickets", [])
                if ticket in tickets:
                    tickets.remove(ticket)
                    self._save_state_locked()

    def set_tp1_reached(self, message_id: int | str, reached: bool = True) -> None:
        """Mark tp1_reached status for a signal."""
        key = str(message_id)
        if key.startswith("_"):
            return
        with self._lock:
            rec = self._state.get(key)
            if isinstance(rec, dict):
                rec["tp1_reached"] = reached
                self._save_state_locked()

    def get_active_signals(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Return all signals that are currently active and not fully closed (skips _ metadata keys)."""
        with self._lock:
            active = []
            for msg_id, data in self._state.items():
                if str(msg_id).startswith("_") or not isinstance(data, dict):
                    continue
                if data.get("status") in ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE"):
                    active.append((msg_id, copy.deepcopy(data)))
            return active

    def count_open_signals(self) -> int:
        """Count active open signals (skips _ metadata keys and non-dict entries)."""
        with self._lock:
            return sum(
                1 for k, data in self._state.items()
                if not str(k).startswith("_")
                and isinstance(data, dict)
                and data.get("status") in ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE")
            )

    def _sort_key(self, item: Tuple[str, Dict[str, Any]]):
        """Sort helper: parse ISO timestamp robustly (UTC-normalized) with numeric msg_id tie-breaker."""
        msg_id_str, val = item
        ts_str = val.get("timestamp", "") if isinstance(val, dict) else ""
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            ts = datetime.min.replace(tzinfo=timezone.utc)
        key_str = str(msg_id_str)
        numeric_id = int(key_str) if key_str.isdigit() else 0
        return (ts, numeric_id)

    def get_latest_signal(self, symbol: Optional[str] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Get the most recent active signal (or latest signal if none active), skipping _ metadata keys."""
        target_sym = symbol.upper() if symbol else None
        with self._lock:
            signal_items = [
                (k, v) for k, v in self._state.items()
                if not str(k).startswith("_") and isinstance(v, dict)
            ]
            sorted_items = sorted(
                signal_items,
                key=self._sort_key,
                reverse=True,
            )
            for msg_id, data in sorted_items:
                if data.get("status") in ["ACTIVE", "PENDING_DELAY", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE"]:
                    if target_sym is None or (data.get("symbol") or "").upper() == target_sym:
                        return msg_id, copy.deepcopy(data)

            # If no active found, return the latest of any status matching symbol
            for msg_id, data in sorted_items:
                if target_sym is None or (data.get("symbol") or "").upper() == target_sym:
                    return msg_id, copy.deepcopy(data)

            return None

    def delete_signal(self, message_id: int | str) -> bool:
        """Delete a signal record from state (e.g. if unentered delayed order was rejected by broker)."""
        key = str(message_id)
        if key.startswith("_"):
            return False
        with self._lock:
            if key in self._state:
                del self._state[key]
                self._save_state_locked()
                return True
            return False

    def cleanup_old_signals(self, days: int = 7) -> int:
        """Remove terminal signals older than N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal_statuses = {"CLOSED", "FULL_CLOSED"}
        to_remove = []

        with self._lock:
            for key, val in self._state.items():
                if str(key).startswith("_") or not isinstance(val, dict):
                    continue
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
        """Return a copy of the full state (excluding internal _ metadata keys and non-dict entries)."""
        with self._lock:
            return {
                k: copy.deepcopy(v) for k, v in self._state.items()
                if not str(k).startswith("_") and isinstance(v, dict)
            }

    def get_daily_win_config(self) -> Dict[str, Any]:
        """Return the persistent Daily Win Target configuration."""
        with self._lock:
            defaults = {
                "enabled": False,
                "target_amount": 0.0,
                "baseline_pnl": 0.0,
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
            cfg = self._state.get("_daily_win")
            if isinstance(cfg, dict):
                defaults.update(cfg)
                defaults["enabled"] = bool(defaults["enabled"])
                defaults["target_amount"] = round(float(defaults["target_amount"] or 0.0), 2)
                defaults["baseline_pnl"] = round(float(defaults["baseline_pnl"] or 0.0), 2)
                if not defaults.get("date"):
                    defaults["date"] = datetime.now().strftime("%Y-%m-%d")
            return defaults

    def set_daily_win_config(
        self,
        enabled: bool,
        target_amount: float = 0.0,
        baseline_pnl: float = 0.0,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist the Daily Win Target configuration in state file."""
        with self._lock:
            cfg = {
                "enabled": bool(enabled),
                "target_amount": round(float(target_amount or 0.0), 2),
                "baseline_pnl": round(float(baseline_pnl or 0.0), 2),
                "date": date_str or datetime.now().strftime("%Y-%m-%d"),
            }
            self._state["_daily_win"] = cfg
            self._save_state_locked()
            return dict(cfg)

    def get_daily_loss_config(self) -> Dict[str, Any]:
        """Return the persistent Daily Loss Limit configuration (realized closed losses only)."""
        with self._lock:
            defaults = {
                "enabled": False,
                "limit_amount": 0.0,
                "triggered_date": None,
                "triggered_at": 0.0,
            }
            cfg = self._state.get("_daily_loss")
            if isinstance(cfg, dict):
                defaults.update(cfg)
                defaults["enabled"] = bool(defaults["enabled"])
                defaults["limit_amount"] = round(abs(float(defaults["limit_amount"] or 0.0)), 2)
                defaults["triggered_at"] = float(defaults["triggered_at"] or 0.0)
            return defaults

    def set_daily_loss_config(
        self,
        enabled: bool,
        limit_amount: float = 0.0,
        triggered_date: Optional[str] = None,
        triggered_at: float = 0.0,
    ) -> Dict[str, Any]:
        """Persist the Daily Loss Limit configuration. Pass triggered_date=None to clear any previous trigger."""
        with self._lock:
            cfg = {
                "enabled": bool(enabled),
                "limit_amount": round(abs(float(limit_amount or 0.0)), 2),
                "triggered_date": triggered_date,
                "triggered_at": float(triggered_at or 0.0),
            }
            self._state["_daily_loss"] = cfg
            self._save_state_locked()
            return dict(cfg)
