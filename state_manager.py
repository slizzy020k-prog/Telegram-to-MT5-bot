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

_ACTIVE_STATUSES = ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE")
_PENDING_STATUSES = ("PENDING", "PENDING_DELAY")
_TERMINAL_STATUSES = {"CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"}


def _sync_mt5_order_tickets(rec: Dict[str, Any]) -> List[int]:
    """Keep combined unique mt5_order_tickets synced with market_tickets + pending_tickets."""
    combined: List[int] = []
    for t in list(rec.get("market_tickets") or []) + list(rec.get("pending_tickets") or []):
        if t is not None and t not in combined:
            combined.append(t)
    rec["mt5_order_tickets"] = combined
    return combined


class StateManager:
    """
    Persistent JSON database linking Telegram (chat_id, message_id) -> tagged layer tickets & MT5 orders.
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

    def _unpack_id(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
    ) -> Tuple[str, Optional[int | str]]:
        """Unpack optional (chat_id, msg_id) tuple keys into (str(msg_id), chat_id)."""
        if isinstance(message_id, tuple) and len(message_id) == 2:
            tup_chat, tup_mid = message_id
            if chat_id is None:
                chat_id = tup_chat
            message_id = tup_mid
        return str(message_id), chat_id

    def _resolve_key(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
        for_create: bool = False,
    ) -> str:
        """
        Resolve storage key under self._lock, supporting (chat_id, msg_id) tuples and
        multi-channel collision avoidance (`f"{chat_id}:{msg_id}"` when another chat_id owns `str(msg_id)`).
        """
        base_key, eff_chat = self._unpack_id(message_id, chat_id)
        if base_key.startswith("_"):
            return base_key

        if not for_create and ":" in base_key and isinstance(self._state.get(base_key), dict):
            return base_key

        if for_create:
            if eff_chat is None:
                return base_key
            existing = self._state.get(base_key)
            if not isinstance(existing, dict):
                return base_key
            ex_chat = existing.get("chat_id")
            if ex_chat is None or str(ex_chat) == str(eff_chat):
                return base_key
            return f"{eff_chat}:{base_key}"

        if eff_chat is not None:
            comp_key = f"{eff_chat}:{base_key}"
            if isinstance(self._state.get(comp_key), dict):
                return comp_key
            existing = self._state.get(base_key)
            if isinstance(existing, dict):
                ex_chat = existing.get("chat_id")
                if ex_chat is None or str(ex_chat) == str(eff_chat):
                    return base_key
            return comp_key

        return base_key

    def get_signal(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve signal state by Telegram message ID or (chat_id, msg_id) (ignores _ metadata keys)."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return None
            signal = self._state.get(key)
            return copy.deepcopy(signal) if isinstance(signal, dict) else None

    def create_signal(
        self,
        message_id: int | str | Tuple[Any, Any],
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
        chat_id: Optional[int | str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Create and persist a new layer-tagged signal record."""
        _, eff_chat = self._unpack_id(message_id, chat_id)
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

        initial_price = kwargs.get("initial_price", kwargs.get("recorded_entry_price"))
        recorded_entry_price = kwargs.get("recorded_entry_price", initial_price)
        tp1_price = kwargs.get("tp1_price", raw_tp1)
        eff_raw_tp1 = raw_tp1 if raw_tp1 is not None else tp1_price
        is_zone_order = bool(kwargs.get("is_zone_order", bool(is_zone)))

        signal_record = {
            "timestamp": timestamp,
            "chat_id": eff_chat,
            "symbol": symbol,
            "direction": direction,
            "layers": layers,
            "zone": zone,
            "max_orders": max_orders,
            "is_zone": bool(is_zone),
            "is_zone_order": is_zone_order,
            "market_tickets": list(market_tickets or []),
            "pending_tickets": list(pending_tickets or []),
            "mt5_order_tickets": [],
            "zone_completed": zone_completed,
            "sl": sl,
            "raw_tp1": eff_raw_tp1,
            "tp1_price": tp1_price,
            "raw_tp2": raw_tp2,
            "raw_tp3": raw_tp3,
            "hard_broker_tp": final_tp,
            "final_tp": final_tp,
            "proximity_close_price": proximity_close_price,
            "initial_price": initial_price,
            "recorded_entry_price": recorded_entry_price,
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
        if signal_record.get("chat_id") is None and eff_chat is not None:
            signal_record["chat_id"] = eff_chat
        _sync_mt5_order_tickets(signal_record)

        with self._lock:
            key = self._resolve_key(message_id, chat_id=eff_chat, for_create=True)
            self._state[key] = signal_record
            self._save_state_locked()

        logger.info(f"State saved for message_id={key} (Symbol={symbol}, Direction={direction})")
        return copy.deepcopy(signal_record)

    def update_signal(
        self,
        message_id: int | str | Tuple[Any, Any],
        updates: Dict[str, Any],
        chat_id: Optional[int | str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update fields on an existing signal record while keeping alias fields & mt5_order_tickets synced."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return None
            existing = self._state.get(key)
            if not isinstance(existing, dict):
                logger.warning(f"Attempted to update non-existent signal: {key}")
                return None
            existing.update(updates)
            if "recorded_entry_price" in updates and "initial_price" not in updates:
                existing["initial_price"] = updates["recorded_entry_price"]
            elif "initial_price" in updates and "recorded_entry_price" not in updates:
                existing["recorded_entry_price"] = updates["initial_price"]
            if "raw_tp1" in updates and "tp1_price" not in updates:
                existing["tp1_price"] = updates["raw_tp1"]
            elif "tp1_price" in updates and "raw_tp1" not in updates:
                existing["raw_tp1"] = updates["tp1_price"]
            if "is_zone" in updates and "is_zone_order" not in updates:
                existing["is_zone_order"] = bool(updates["is_zone"])
            elif "is_zone_order" in updates and "is_zone" not in updates:
                existing["is_zone"] = bool(updates["is_zone_order"])
            _sync_mt5_order_tickets(existing)
            self._save_state_locked()
            return copy.deepcopy(existing)

    def update_layer(
        self,
        message_id: int | str | Tuple[Any, Any],
        layer_name: str,
        ticket: Optional[int] = None,
        status: Optional[str] = None,
        order_type: Optional[str] = None,
        target_price: Optional[float] = None,
        chat_id: Optional[int | str] = None,
    ) -> None:
        """Update a specific layer (TOP, MID, or BOTTOM) in the signal record."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return
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

    def add_market_ticket(
        self,
        message_id: int | str | Tuple[Any, Any],
        ticket: int,
        chat_id: Optional[int | str] = None,
    ) -> None:
        """Add a market ticket to the signal record and sync mt5_order_tickets."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return
            rec = self._state.get(key)
            if isinstance(rec, dict):
                tickets = rec.setdefault("market_tickets", [])
                if ticket not in tickets:
                    tickets.append(ticket)
                _sync_mt5_order_tickets(rec)
                self._save_state_locked()

    def add_pending_ticket(
        self,
        message_id: int | str | Tuple[Any, Any],
        ticket: int,
        chat_id: Optional[int | str] = None,
    ) -> None:
        """Add a pending ticket to the signal record and sync mt5_order_tickets."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return
            rec = self._state.get(key)
            if isinstance(rec, dict):
                tickets = rec.setdefault("pending_tickets", [])
                if ticket not in tickets:
                    tickets.append(ticket)
                _sync_mt5_order_tickets(rec)
                self._save_state_locked()

    def remove_pending_ticket(
        self,
        message_id: int | str | Tuple[Any, Any],
        ticket: int,
        chat_id: Optional[int | str] = None,
    ) -> None:
        """Remove a pending ticket from the signal record and sync mt5_order_tickets."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return
            rec = self._state.get(key)
            if isinstance(rec, dict):
                tickets = rec.get("pending_tickets", [])
                if ticket in tickets:
                    tickets.remove(ticket)
                    _sync_mt5_order_tickets(rec)
                    self._save_state_locked()

    def set_tp1_reached(
        self,
        message_id: int | str | Tuple[Any, Any],
        reached: bool = True,
        chat_id: Optional[int | str] = None,
    ) -> None:
        """Mark tp1_reached status for a signal."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return
            rec = self._state.get(key)
            if isinstance(rec, dict):
                rec["tp1_reached"] = reached
                self._save_state_locked()

    def get_active_signals(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Return all signals that are currently active and not terminal/closed (skips _ metadata keys)."""
        with self._lock:
            active = []
            for msg_id, data in self._state.items():
                if str(msg_id).startswith("_") or not isinstance(data, dict):
                    continue
                if data.get("status") in _ACTIVE_STATUSES:
                    active.append((msg_id, copy.deepcopy(data)))
            return active

    def count_open_signals(self) -> int:
        """Count active open signals (skips _ metadata keys and non-dict entries)."""
        with self._lock:
            return sum(
                1 for k, data in self._state.items()
                if not str(k).startswith("_")
                and isinstance(data, dict)
                and data.get("status") in _ACTIVE_STATUSES
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
        tail = str(msg_id_str).rsplit(":", 1)[-1]
        numeric_id = int(tail) if tail.isdigit() else 0
        return (ts, numeric_id)

    def get_latest_signal(self, symbol: Optional[str] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Get the most recent active or pending signal (or latest signal if none active), skipping _ metadata keys."""
        target_sym = symbol.upper() if symbol else None
        priority_statuses = _ACTIVE_STATUSES + _PENDING_STATUSES
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
                if data.get("status") in priority_statuses:
                    if target_sym is None or (data.get("symbol") or "").upper() == target_sym:
                        return msg_id, copy.deepcopy(data)

            # If no active found, return the latest of any status matching symbol
            for msg_id, data in sorted_items:
                if target_sym is None or (data.get("symbol") or "").upper() == target_sym:
                    return msg_id, copy.deepcopy(data)

            return None

    def delete_signal(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
    ) -> bool:
        """Delete a signal record from state (e.g. if unentered delayed order was rejected by broker)."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return False
            if key in self._state:
                del self._state[key]
                self._save_state_locked()
                return True
            return False

    def cleanup_old_signals(self, days: int = 7) -> int:
        """Remove terminal signals older than N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        to_remove = []

        with self._lock:
            for key, val in self._state.items():
                if str(key).startswith("_") or not isinstance(val, dict):
                    continue
                if val.get("status") not in _TERMINAL_STATUSES:
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
                "daily_loss_limit": 0.0,
                "triggered_date": None,
                "triggered_at": 0.0,
            }
            cfg = self._state.get("_daily_loss")
            if isinstance(cfg, dict):
                defaults.update(cfg)
                defaults["enabled"] = bool(defaults["enabled"])
                raw_lim = cfg.get("limit_amount") if "limit_amount" in cfg else cfg.get("daily_loss_limit")
                lim = round(abs(float(raw_lim or 0.0)), 2)
                defaults["limit_amount"] = lim
                defaults["daily_loss_limit"] = lim
                defaults["triggered_at"] = float(defaults["triggered_at"] or 0.0)
            return defaults

    def set_daily_loss_config(
        self,
        enabled: bool,
        limit_amount: float = 0.0,
        triggered_date: Optional[str] = None,
        triggered_at: float = 0.0,
        daily_loss_limit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Persist the Daily Loss Limit configuration, keeping limit_amount and daily_loss_limit synced."""
        with self._lock:
            raw_lim = daily_loss_limit if (daily_loss_limit is not None and limit_amount == 0.0) else limit_amount
            lim = round(abs(float(raw_lim or 0.0)), 2)
            cfg = {
                "enabled": bool(enabled),
                "limit_amount": lim,
                "daily_loss_limit": lim,
                "triggered_date": triggered_date,
                "triggered_at": float(triggered_at or 0.0),
            }
            self._state["_daily_loss"] = cfg
            self._save_state_locked()
            return dict(cfg)
