import copy
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import config as _config

logger = logging.getLogger("copier.state")

_ACTIVE_STATUSES = ("ACTIVE", "BE_APPLIED", "PARTIAL_CLOSED", "RUNNER_ACTIVE")
_PENDING_STATUSES = ("PENDING", "PENDING_DELAY")
_TERMINAL_STATUSES = {"CLOSED", "FULL_CLOSED", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"}

LEGACY_TO_V3_LAYER: Dict[str, str] = {"TOP": "L1", "MID": "L2", "BOTTOM": "L4"}
V3_TO_LEGACY_LAYER: Dict[str, str] = {"L1": "TOP", "L2": "MID", "L4": "BOTTOM"}
_DEFAULT_BASE_LOTS: List[float] = [0.01, 0.01, 0.01, 0.02]


def _normalize_chat_id_variants(chat_id: Any) -> Set[str]:
    """Build canonical string variants for a Telegram chat/channel ID (-100<id>, -<id>, <id>)."""
    if chat_id is None or isinstance(chat_id, bool):
        return set()
    raw = str(chat_id).strip()
    if not raw:
        return set()
    variants: Set[str] = {raw}
    digits = raw.lstrip("-")
    if digits.isdigit():
        variants.add(digits)
        variants.add(f"-{digits}")
        variants.add(f"-100{digits}")
        if digits.startswith("100") and len(digits) > 3:
            bare = digits[3:]
            variants.add(bare)
            variants.add(f"-{bare}")
            variants.add(f"-100{bare}")
    return variants


def _chat_ids_match(stored_chat: Any, query_chat: Any) -> bool:
    """Return True if stored_chat and query_chat refer to the same Telegram chat/channel."""
    if stored_chat is None or query_chat is None:
        return False
    return bool(_normalize_chat_id_variants(stored_chat) & _normalize_chat_id_variants(query_chat))


def _default_layer_lots() -> Dict[str, float]:
    """Return canonical 4-layer lot mapping from config, falling back safely."""
    base = getattr(_config, "BASE_LAYER_LOTS", None)
    if isinstance(base, (list, tuple)) and len(base) == 4:
        return {f"L{i + 1}": float(base[i]) for i in range(4)}
    cfg_layers = getattr(_config, "LAYER_LOTS", None)
    if isinstance(cfg_layers, dict) and all(f"L{i}" in cfg_layers for i in range(1, 5)):
        return {f"L{i}": float(cfg_layers[f"L{i}"]) for i in range(1, 5)}
    return {f"L{i + 1}": _DEFAULT_BASE_LOTS[i] for i in range(4)}


def _sync_mt5_order_tickets(rec: Dict[str, Any]) -> List[int]:
    """Keep combined unique mt5_order_tickets synced and maintain append-only all_placed_tickets."""
    combined: List[int] = []
    for t in list(rec.get("market_tickets") or []) + list(rec.get("pending_tickets") or []):
        if t is not None and t not in combined:
            combined.append(t)
    rec["mt5_order_tickets"] = combined

    all_placed: List[int] = []
    for t in list(rec.get("all_placed_tickets") or []) + combined:
        if t is not None and t not in all_placed:
            all_placed.append(t)
    layers = rec.get("layers")
    if isinstance(layers, dict):
        for l_data in layers.values():
            if isinstance(l_data, dict):
                lt = l_data.get("ticket")
                if lt is not None and lt not in all_placed:
                    all_placed.append(lt)
    rec["all_placed_tickets"] = all_placed
    return combined


def _sync_signal_aliases(rec: Dict[str, Any], updates: Optional[Dict[str, Any]] = None) -> None:
    """Keep dual-named v2/v3 fields consistent after create or update."""
    src = updates if updates is not None else rec

    if "recorded_entry_price" in src and "initial_price" not in src:
        rec["initial_price"] = rec.get("recorded_entry_price")
    elif "initial_price" in src and "recorded_entry_price" not in src:
        rec["recorded_entry_price"] = rec.get("initial_price")

    if "raw_tp1" in src and "tp1_price" not in src:
        rec["tp1_price"] = rec.get("raw_tp1")
    elif "tp1_price" in src and "raw_tp1" not in src:
        rec["raw_tp1"] = rec.get("tp1_price")

    if "is_zone" in src and "is_zone_order" not in src:
        rec["is_zone_order"] = bool(rec.get("is_zone"))
    elif "is_zone_order" in src and "is_zone" not in src:
        rec["is_zone"] = bool(rec.get("is_zone_order"))

    if "soft_tp1_floor" in src and "runner_sl_floor" not in src:
        rec["runner_sl_floor"] = rec.get("soft_tp1_floor")
    elif "runner_sl_floor" in src and "soft_tp1_floor" not in src:
        rec["soft_tp1_floor"] = rec.get("runner_sl_floor")

    if "runner_tickets" in src and (
        ("runner_ticket" not in src) or (updates is None and rec.get("runner_ticket") is None)
    ):
        t_list = list(rec.get("runner_tickets") or [])
        rec["runner_ticket"] = t_list[-1] if t_list else None
    elif "runner_ticket" in src and (
        (updates is not None and "runner_tickets" not in updates) or not rec.get("runner_tickets")
    ):
        rt = rec.get("runner_ticket")
        rec["runner_tickets"] = [rt] if rt else []

    if "runner_layers" in src and (
        ("runner_layer" not in src) or (updates is None and rec.get("runner_layer") is None)
    ):
        l_list = list(rec.get("runner_layers") or [])
        rec["runner_layer"] = l_list[-1] if l_list else None
    elif "runner_layer" in src and (
        (updates is not None and "runner_layers" not in updates) or not rec.get("runner_layers")
    ):
        rl = rec.get("runner_layer")
        rec["runner_layers"] = [rl] if rl else []

    if "tp_levels" in src and isinstance(rec.get("tp_levels"), dict):
        tp_map = rec["tp_levels"]
        for i in range(1, 6):
            k_tp = f"TP{i}"
            k_raw = f"raw_tp{i}"
            explicitly_set = updates is not None and k_raw in updates and updates[k_raw] is not None
            if k_tp in tp_map and not explicitly_set and (updates is not None or rec.get(k_raw) is None):
                rec[k_raw] = tp_map[k_tp]
        if rec.get("raw_tp1") is not None and (updates is None or "tp1_price" not in updates):
            rec["tp1_price"] = rec["raw_tp1"]
    else:
        existing_tps = rec.get("tp_levels") if isinstance(rec.get("tp_levels"), dict) else {}
        merged_tps = dict(existing_tps)
        for i in range(1, 6):
            val = rec.get(f"raw_tp{i}")
            if val is not None:
                merged_tps[f"TP{i}"] = val
        if merged_tps:
            rec["tp_levels"] = merged_tps

    layers_map = rec.get("layers")
    if isinstance(layers_map, dict):
        for l_data in layers_map.values():
            if not isinstance(l_data, dict):
                continue
            if l_data.get("entry_price") is not None and l_data.get("entry") is None:
                l_data["entry"] = l_data["entry_price"]
            elif l_data.get("entry") is not None and l_data.get("entry_price") is None:
                l_data["entry_price"] = l_data["entry"]

    _sync_mt5_order_tickets(rec)


def _compute_default_zone_targets(zone: Optional[Dict[str, float]], direction: str) -> Dict[str, Optional[float]]:
    if not isinstance(zone, dict) or "top" not in zone or "bottom" not in zone:
        return {"L1": None, "L2": None, "L3": None, "L4": None}
    top = float(zone["top"])
    bottom = float(zone["bottom"])
    span = top - bottom
    if str(direction).upper() == "SELL":
        return {
            "L1": round(bottom, 2),
            "L2": round(bottom + span / 3.0, 2),
            "L3": round(bottom + (2.0 * span) / 3.0, 2),
            "L4": round(top, 2),
        }
    return {
        "L1": round(top, 2),
        "L2": round(top - span / 3.0, 2),
        "L3": round(top - (2.0 * span) / 3.0, 2),
        "L4": round(bottom, 2),
    }


class StateManager:
    """
    Persistent JSON database linking Telegram (chat_id, message_id) -> tagged layer tickets & MT5 orders.
    Thread-safe with dirty-checked atomic file writes, composite chat_id key resolution,
    legacy 3-layer compatibility, and _meta catch-up watermarks.
    """

    def __init__(self, file_path: Optional[Path] = None):
        configured = getattr(_config, "STATE_FILE_PATH", Path(__file__).resolve().parent / "signals_state.json")
        self.file_path = Path(file_path or configured)
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}
        self._last_saved_payload: Optional[str] = None
        self._save_count: int = 0
        self._load_state()

    @property
    def save_count(self) -> int:
        """Number of physical disk writes performed (for dirty-check verification)."""
        with self._lock:
            return self._save_count

    @staticmethod
    def is_legacy_3layer(signal_record: Optional[Dict[str, Any]]) -> bool:
        """Return True if a stored signal uses legacy TOP/MID/BOTTOM layer keys without L1..L4."""
        if not isinstance(signal_record, dict):
            return False
        layers = signal_record.get("layers")
        if not isinstance(layers, dict):
            return False
        has_legacy = any(k in layers for k in LEGACY_TO_V3_LAYER)
        has_v3 = any(f"L{i}" in layers for i in range(1, 5))
        return has_legacy and not has_v3

    @staticmethod
    def normalize_layer_name(layer_name: str) -> str:
        """Map legacy TOP/MID/BOTTOM names to canonical L1/L2/L4 while leaving others intact."""
        upper = str(layer_name or "").strip().upper()
        return LEGACY_TO_V3_LAYER.get(upper, upper)

    @staticmethod
    def get_layer(signal_record: Optional[Dict[str, Any]], layer_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve a layer dict by canonical or legacy alias name."""
        if not isinstance(signal_record, dict):
            return None
        layers = signal_record.get("layers")
        if not isinstance(layers, dict):
            return None
        if layer_name in layers and isinstance(layers[layer_name], dict):
            return layers[layer_name]
        upper = str(layer_name or "").strip().upper()
        alias = V3_TO_LEGACY_LAYER.get(upper) or LEGACY_TO_V3_LAYER.get(upper)
        if alias and alias in layers and isinstance(layers[alias], dict):
            return layers[alias]
        return None

    def _serialize_state_locked(self) -> str:
        return json.dumps(self._state, indent=2, sort_keys=True, default=str)

    def _load_state(self) -> None:
        """Load state from JSON file or initialize empty state."""
        with self._lock:
            if not self.file_path.exists():
                self._state = {}
                self._save_state_locked(force=True)
                return

            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        loaded = json.loads(content)
                        self._state = loaded if isinstance(loaded, dict) else {}
                    else:
                        self._state = {}
                self._last_saved_payload = self._serialize_state_locked()
            except Exception as e:
                logger.error(f"Failed to load state file {self.file_path}: {e}. Creating backup and resetting.")
                backup_path = self.file_path.with_suffix(f".bak.{int(datetime.now().timestamp())}")
                try:
                    self.file_path.rename(backup_path)
                except Exception:
                    pass
                self._state = {}
                self._save_state_locked(force=True)

    def _save_state_locked(self, force: bool = False) -> bool:
        """Dirty-checked atomic write to disk; skips I/O when serialized state is unchanged."""
        payload = self._serialize_state_locked()
        if not force and self._last_saved_payload is not None and payload == self._last_saved_payload:
            return False

        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.file_path.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(payload)
            temp_file.replace(self.file_path)
            self._last_saved_payload = payload
            self._save_count += 1
            return True
        except Exception as e:
            logger.error(f"Failed to atomically write state file {self.file_path}: {e}")
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            return False

    def _unpack_id(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
    ) -> Tuple[str, Optional[int | str]]:
        """Unpack (chat_id, msg_id) tuples or 'chat_id:msg_id' strings into (str(msg_id), chat_id)."""
        if isinstance(message_id, tuple) and len(message_id) == 2:
            tup_chat, tup_mid = message_id
            if chat_id is None:
                chat_id = tup_chat
            message_id = tup_mid
        raw_str = str(message_id)
        if not raw_str.startswith("_") and ":" in raw_str:
            c_part, m_part = raw_str.rsplit(":", 1)
            if c_part and m_part:
                if chat_id is None:
                    chat_id = int(c_part) if c_part.lstrip("-").isdigit() else c_part
                raw_str = m_part
        return raw_str, chat_id

    def _resolve_key(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
        for_create: bool = False,
    ) -> str:
        """
        Resolve storage key under self._lock, supporting (chat_id, msg_id) tuples,
        'chat_id:msg_id' composite strings, and multi-channel collision avoidance.
        """
        raw_input = str(message_id) if not isinstance(message_id, tuple) else None
        if (
            not for_create
            and raw_input is not None
            and not raw_input.startswith("_")
            and ":" in raw_input
            and isinstance(self._state.get(raw_input), dict)
        ):
            rec = self._state[raw_input]
            if chat_id is None or rec.get("chat_id") is None or _chat_ids_match(rec.get("chat_id"), chat_id):
                return raw_input

        base_key, eff_chat = self._unpack_id(message_id, chat_id)
        if base_key.startswith("_"):
            return base_key

        if for_create:
            if eff_chat is None:
                return base_key
            comp_key = f"{eff_chat}:{base_key}"
            if isinstance(self._state.get(comp_key), dict):
                return comp_key
            existing = self._state.get(base_key)
            if not isinstance(existing, dict):
                return base_key
            ex_chat = existing.get("chat_id")
            if ex_chat is None or _chat_ids_match(ex_chat, eff_chat):
                return base_key
            return comp_key

        if eff_chat is not None:
            comp_key = f"{eff_chat}:{base_key}"
            if isinstance(self._state.get(comp_key), dict):
                return comp_key
            for v_chat in _normalize_chat_id_variants(eff_chat):
                alt_comp = f"{v_chat}:{base_key}"
                if isinstance(self._state.get(alt_comp), dict):
                    return alt_comp
            existing = self._state.get(base_key)
            if isinstance(existing, dict):
                ex_chat = existing.get("chat_id")
                if ex_chat is None or _chat_ids_match(ex_chat, eff_chat):
                    return base_key
            return comp_key

        if isinstance(self._state.get(base_key), dict):
            return base_key

        suffix = f":{base_key}"
        matches = [
            k for k, v in self._state.items()
            if not str(k).startswith("_") and str(k).endswith(suffix) and isinstance(v, dict)
        ]
        if len(matches) == 1:
            return matches[0]
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
        max_orders: Optional[int] = None,
        is_zone: bool = True,
        raw_text: str = "",
        chat_id: Optional[int | str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Create and persist a new layer-tagged signal record (v3.0 4-layer defaults)."""
        _, eff_chat = self._unpack_id(message_id, chat_id)
        timestamp = datetime.now(timezone.utc).isoformat()
        eff_max_orders = (
            int(max_orders)
            if max_orders is not None
            else int(getattr(_config, "MAX_ORDERS_PER_SIGNAL", 4))
        )

        if layers is None:
            lots = _default_layer_lots()
            zone_targets = _compute_default_zone_targets(zone, direction)
            layers = {
                f"L{i}": {
                    "lot": lots[f"L{i}"],
                    "target_price": zone_targets[f"L{i}"],
                    "tp": final_tp,
                    "tp_stage": "TP1",
                    "ticket": None,
                    "order_type": "MARKET" if i == 1 and not zone else "LIMIT",
                    "status": "PENDING",
                }
                for i in range(1, 5)
            }

        initial_price = kwargs.get("initial_price", kwargs.get("recorded_entry_price"))
        recorded_entry_price = kwargs.get("recorded_entry_price", initial_price)
        tp1_price = kwargs.get("tp1_price", raw_tp1)
        eff_raw_tp1 = raw_tp1 if raw_tp1 is not None else tp1_price
        is_zone_order = bool(kwargs.get("is_zone_order", bool(is_zone)))
        soft_floor = kwargs.get("soft_tp1_floor", kwargs.get("runner_sl_floor"))

        signal_record: Dict[str, Any] = {
            "timestamp": timestamp,
            "chat_id": eff_chat,
            "symbol": symbol,
            "direction": direction,
            "layers": layers,
            "zone": zone,
            "max_orders": eff_max_orders,
            "is_zone": bool(is_zone),
            "is_zone_order": is_zone_order,
            "is_stop_order": bool(kwargs.get("is_stop_order", False)),
            "is_limit_order": bool(kwargs.get("is_limit_order", False)),
            "order_intent": kwargs.get("order_intent", "AUTO"),
            "market_tickets": list(market_tickets or []),
            "pending_tickets": list(pending_tickets or []),
            "mt5_order_tickets": [],
            "accounted_tickets": list(kwargs.get("accounted_tickets") or []),
            "accounted_deal_tickets": list(kwargs.get("accounted_deal_tickets") or []),
            "pending_pnl_tickets": copy.deepcopy(kwargs.get("pending_pnl_tickets")) if kwargs.get("pending_pnl_tickets") else {},
            "banked_profit": float(kwargs.get("banked_profit") or 0.0),
            "roles_locked": bool(kwargs.get("roles_locked", False)),
            "bank_harvest_layer": kwargs.get("bank_harvest_layer"),
            "zone_completed": zone_completed,
            "sl": sl,
            "raw_tp1": eff_raw_tp1,
            "tp1_price": tp1_price,
            "raw_tp2": raw_tp2,
            "raw_tp3": raw_tp3,
            "raw_tp4": kwargs.get("raw_tp4"),
            "raw_tp5": kwargs.get("raw_tp5"),
            "tp_levels": copy.deepcopy(kwargs.get("tp_levels")) if kwargs.get("tp_levels") else None,
            "tp_stage": kwargs.get("tp_stage", "TP1"),
            "hard_broker_tp": final_tp,
            "final_tp": final_tp,
            "proximity_close_price": proximity_close_price,
            "initial_price": initial_price,
            "recorded_entry_price": recorded_entry_price,
            "tp1_reached": False,
            "shortfall_armed": False,
            "shortfall_reversal_ticks": 0,
            "peak_price": None,
            "runner_layer": None,
            "runner_layers": [],
            "runner_ticket": None,
            "runner_tickets": [],
            "last_runner_tickets": [],
            "soft_tp1_floor": soft_floor,
            "runner_sl_floor": soft_floor,
            "runner_milestone_idx": 1,
            "runner_milestone_name": "TP1",
            "runner_max_tp_stage": int(getattr(_config, "RUNNER_MAX_TP_STAGE", 4)),
            "runner_cleared_milestones": [],
            "runner_proximity_armed": False,
            "runner_reversal_ticks": 0,
            "tp2_shortfall_armed": False,
            "runner_peak_price": None,
            "ledger_recorded": False,
            "status": "ACTIVE",
            "raw_text": raw_text,
        }
        signal_record.update(kwargs)
        if signal_record.get("chat_id") is None and eff_chat is not None:
            signal_record["chat_id"] = eff_chat
        _sync_signal_aliases(signal_record)

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
        """Update fields on an existing signal record with dirty-checked disk persistence."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return None
            existing = self._state.get(key)
            if not isinstance(existing, dict):
                logger.warning(f"Attempted to update non-existent signal: {key}")
                return None
            existing.update(copy.deepcopy(updates))
            _sync_signal_aliases(existing, updates=updates)
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
        tp: Optional[float] = None,
        lot: Optional[float] = None,
        tp_stage: Optional[str] = None,
    ) -> None:
        """Update a specific layer (L1..L4 or legacy TOP/MID/BOTTOM) in the signal record."""
        with self._lock:
            key = self._resolve_key(message_id, chat_id=chat_id, for_create=False)
            if key.startswith("_"):
                return
            rec = self._state.get(key)
            if isinstance(rec, dict):
                layers = rec.setdefault("layers", {})
                target_key = layer_name
                if target_key not in layers:
                    alias = V3_TO_LEGACY_LAYER.get(str(layer_name).upper())
                    if alias and alias in layers:
                        target_key = alias
                layer = layers.setdefault(target_key, {})
                if ticket is not None:
                    layer["ticket"] = ticket
                if status is not None:
                    layer["status"] = status
                if order_type is not None:
                    layer["order_type"] = order_type
                if target_price is not None:
                    layer["target_price"] = target_price
                if tp is not None:
                    layer["tp"] = tp
                if lot is not None:
                    layer["lot"] = lot
                if tp_stage is not None:
                    layer["tp_stage"] = tp_stage
                _sync_mt5_order_tickets(rec)
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
            if isinstance(rec, dict) and rec.get("tp1_reached") != reached:
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

    def get_open_or_pending_signals(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Return all active or pending non-terminal signals (skips _ metadata keys)."""
        open_statuses = set(_ACTIVE_STATUSES) | set(_PENDING_STATUSES)
        with self._lock:
            results: List[Tuple[str, Dict[str, Any]]] = []
            for key, data in self._state.items():
                if str(key).startswith("_") or not isinstance(data, dict):
                    continue
                if data.get("status") in open_statuses:
                    results.append((key, copy.deepcopy(data)))
            return results

    def find_matching_signals_for_delete(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Find non-terminal signal records matching a deleted Telegram message_id (and optional chat_id).
        Handles -100<id> <-> bare channel_id normalization and basic-group chat_id=None events.
        """
        base_key, eff_chat = self._unpack_id(message_id, chat_id)
        if base_key.startswith("_"):
            return []

        with self._lock:
            matches: List[Tuple[str, Dict[str, Any]]] = []
            for key, rec in self._state.items():
                if str(key).startswith("_") or not isinstance(rec, dict):
                    continue
                rec_base, rec_key_chat = self._unpack_id(key, rec.get("chat_id"))
                if rec_base != base_key:
                    continue
                has_open_tickets = bool(rec.get("market_tickets") or rec.get("pending_tickets"))
                if rec.get("status") in _TERMINAL_STATUSES and not has_open_tickets:
                    continue
                rec_chat = rec.get("chat_id") if rec.get("chat_id") is not None else rec_key_chat
                if eff_chat is not None:
                    if rec_chat is None or _chat_ids_match(rec_chat, eff_chat):
                        matches.append((key, copy.deepcopy(rec)))
                else:
                    matches.append((key, copy.deepcopy(rec)))
            return matches

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

    def get_latest_signal(
        self,
        symbol: Optional[str] = None,
        chat_id: Optional[int | str] = None,
        prefer_open_positions: bool = False,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Get the most recent active/pending signal (strictly scoped to chat_id when provided)."""
        target_sym = symbol.upper() if symbol else None
        target_base_sym = target_sym.split(".", 1)[0].split("_", 1)[0] if target_sym else None
        priority_statuses = _ACTIVE_STATUSES + _PENDING_STATUSES

        def _matches(data: Dict[str, Any], allow_null_chat: bool = True) -> bool:
            if target_sym is not None:
                rec_sym = (data.get("symbol") or "").upper()
                rec_base_sym = rec_sym.split(".", 1)[0].split("_", 1)[0] if rec_sym else ""
                if rec_sym != target_sym and (not rec_base_sym or rec_base_sym != target_base_sym):
                    return False
            if chat_id is not None:
                rec_chat = data.get("chat_id")
                if rec_chat is None:
                    return allow_null_chat
                return _chat_ids_match(rec_chat, chat_id)
            return True

        def _has_open_positions(data: Dict[str, Any]) -> bool:
            return bool(data.get("market_tickets") or data.get("runner_tickets") or data.get("runner_ticket"))

        with self._lock:
            signal_items = [
                (k, v) for k, v in self._state.items()
                if not str(k).startswith("_") and isinstance(v, dict)
            ]
            sorted_items = sorted(signal_items, key=self._sort_key, reverse=True)
            if prefer_open_positions:
                if chat_id is not None:
                    for msg_id, data in sorted_items:
                        if data.get("status") in _ACTIVE_STATUSES and _has_open_positions(data) and _matches(data, allow_null_chat=False):
                            return msg_id, copy.deepcopy(data)
                for msg_id, data in sorted_items:
                    if data.get("status") in _ACTIVE_STATUSES and _has_open_positions(data) and _matches(data, allow_null_chat=True):
                        return msg_id, copy.deepcopy(data)

            if chat_id is not None:
                for msg_id, data in sorted_items:
                    if data.get("status") in priority_statuses and _matches(data, allow_null_chat=False):
                        return msg_id, copy.deepcopy(data)
            for msg_id, data in sorted_items:
                if data.get("status") in priority_statuses and _matches(data, allow_null_chat=True):
                    return msg_id, copy.deepcopy(data)

            if chat_id is not None:
                for msg_id, data in sorted_items:
                    if _matches(data, allow_null_chat=False):
                        return msg_id, copy.deepcopy(data)
            for msg_id, data in sorted_items:
                if _matches(data, allow_null_chat=True):
                    return msg_id, copy.deepcopy(data)

            return None

    def delete_signal(
        self,
        message_id: int | str | Tuple[Any, Any],
        chat_id: Optional[int | str] = None,
    ) -> bool:
        """Delete a signal record from state."""
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

    # -----------------------------------------------------------------------
    # _META & CATCH-UP WATERMARKS PER CHAT_ID
    # -----------------------------------------------------------------------

    def _ensure_meta_locked(self) -> Dict[str, Any]:
        meta = self._state.get("_meta")
        if not isinstance(meta, dict):
            meta = {"schema_version": 3, "catchup_watermarks": {}}
            self._state["_meta"] = meta
        meta.setdefault("schema_version", 3)
        if not isinstance(meta.get("catchup_watermarks"), dict):
            meta["catchup_watermarks"] = {}
        return meta

    def get_meta(self) -> Dict[str, Any]:
        """Return a copy of _meta metadata (schema_version, catchup_watermarks)."""
        with self._lock:
            meta = self._state.get("_meta")
            if isinstance(meta, dict):
                out = copy.deepcopy(meta)
                out.setdefault("schema_version", 3)
                out.setdefault("catchup_watermarks", {})
                return out
            return {"schema_version": 3, "catchup_watermarks": {}}

    def get_catchup_watermark(self, chat_id: int | str) -> Optional[Dict[str, Any]]:
        """Return persisted catch-up watermark dict for chat_id (including supergroup ID variants), or None."""
        with self._lock:
            meta = self._state.get("_meta")
            if not isinstance(meta, dict):
                return None
            wms = meta.get("catchup_watermarks")
            if not isinstance(wms, dict):
                return None
            entry = wms.get(str(chat_id))
            best_entry: Optional[Dict[str, Any]] = entry if isinstance(entry, dict) else None
            best_id = -1
            if best_entry is not None:
                try:
                    best_id = int(best_entry.get("last_msg_id") or -1)
                except (TypeError, ValueError):
                    best_id = -1
            for alt in _normalize_chat_id_variants(chat_id):
                cand = wms.get(str(alt))
                if isinstance(cand, dict):
                    try:
                        cid = int(cand.get("last_msg_id") or -1)
                    except (TypeError, ValueError):
                        cid = -1
                    if best_entry is None or cid > best_id:
                        best_entry = cand
                        best_id = cid
            return copy.deepcopy(best_entry) if isinstance(best_entry, dict) else None

    def get_last_seen_msg_id(self, chat_id: int | str) -> Optional[int]:
        """Return the highest persisted message_id seen for chat_id, or None."""
        wm = self.get_catchup_watermark(chat_id)
        if not wm:
            return None
        val = wm.get("last_msg_id")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def set_catchup_watermark(
        self,
        chat_id: int | str,
        msg_id: int,
        msg_ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Monotonically advance and persist the last-seen message_id watermark for chat_id under _meta."""
        new_id = int(msg_id)
        chat_key = str(chat_id)
        with self._lock:
            meta = self._ensure_meta_locked()
            wms = meta["catchup_watermarks"]
            existing = wms.get(chat_key)
            if isinstance(existing, dict) and existing.get("last_msg_id") is not None:
                try:
                    if new_id <= int(existing["last_msg_id"]):
                        return copy.deepcopy(existing)
                except (TypeError, ValueError):
                    pass

            entry = {
                "last_msg_id": new_id,
                "last_msg_ts": msg_ts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            wms[chat_key] = entry
            self._save_state_locked()
            return copy.deepcopy(entry)

    def get_all_catchup_watermarks(self) -> Dict[str, Dict[str, Any]]:
        """Return all per-channel catch-up watermarks."""
        return copy.deepcopy(self.get_meta().get("catchup_watermarks", {}))

    # -----------------------------------------------------------------------
    # DAILY WIN / DAILY LOSS PERSISTENT CONFIGS
    # -----------------------------------------------------------------------

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
                # "config" = seeded from DAILY_LOSS_LIMIT_GBP, "user" = set by the
                # operator from Telegram (always wins over the config seed).
                "source": "",
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
        source: str = "",
    ) -> Dict[str, Any]:
        """Persist the Daily Loss Limit configuration, keeping limit_amount and daily_loss_limit synced.

        The "source" field records who owns the value: "config" (seeded from the
        DAILY_LOSS_LIMIT_GBP knob) or "user" (set from Saved Messages). A "user"
        value is never overwritten by the config seed on restart.
        """
        with self._lock:
            raw_lim = daily_loss_limit if (daily_loss_limit is not None and limit_amount == 0.0) else limit_amount
            lim = round(abs(float(raw_lim or 0.0)), 2)
            cfg = {
                "enabled": bool(enabled),
                "limit_amount": lim,
                "daily_loss_limit": lim,
                "triggered_date": triggered_date,
                "triggered_at": float(triggered_at or 0.0),
                "source": str(source or ""),
            }
            self._state["_daily_loss"] = cfg
            self._save_state_locked()
            return dict(cfg)
