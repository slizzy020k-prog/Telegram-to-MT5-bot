import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config as _config
from risk_lifecycle import resolve_fill_time

logger = logging.getLogger("copier.ledger")

_DEFAULT_LEDGER_PATH = Path(
    getattr(_config, "TRADES_LEDGER_PATH", Path(__file__).resolve().parent / "trades_ledger.jsonl")
)
_FILLED_LAYER_STATUSES = {"OPEN", "FILLED", "RUNNER", "BANKED"}
_TERMINAL_UNFILLED_STATUSES = {"CANCELLED", "EXPIRED", "ABORTED"}
VALID_EXIT_REASON_CODES = {
    "DYNAMIC_ATR_TRAIL_SL",
    "DYNAMIC_ATR_TP",
    "BE_SL",
    "INITIAL_SL",
    "TP1_BANK",
    "STAGE2_BANK_TP",
    "TP_CEILING",
    "BROKER_SL",
    "MANUAL",
    "EA_CLOSE",
    "PARTIAL_BANK",
    "ATR_TRAIL",
    "FSM_FLOOR",
    "TRAILED_SL",
    # Legacy aliases preserved for parsing historical ledger entries
    "GOLD_GUARD",
    "SHORTFALL_BASE",
    "BANK_HARVEST",
    "BANK_HARVEST_SL",
    "PROFIT_RATCHET_SL",
    "STAGE0_PROFIT_LOCK",
}
_CANONICAL_EXIT_REASON_CODES = VALID_EXIT_REASON_CODES


def _is_num(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


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


def _infer_exit_stage(signal_data: Dict[str, Any]) -> int:
    """Derive canonical v3.5.0 exit stage (0..4) from signal state."""
    if not isinstance(signal_data, dict):
        return 0
    raw_stage = signal_data.get("exit_stage")
    if _is_num(raw_stage):
        return max(0, min(4, int(raw_stage)))

    status = str(signal_data.get("status") or "").strip().upper()
    if status in ("PENDING", "PENDING_DELAY", "INVALIDATED_TP1", "ABORTED_PRICE_GUARD"):
        return 0

    cleared_stage = 0
    for x in signal_data.get("runner_cleared_milestones") or []:
        s = str(x).strip().upper()
        if s.startswith("TP") and s[2:].isdigit():
            cleared_stage = max(cleared_stage, int(s[2:]))

    raw_idx = signal_data.get("runner_milestone_idx")
    idx_stage = (
        (int(raw_idx) - 1)
        if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) and int(raw_idx) > 1
        else 0
    )
    in_runner_phase = bool(
        signal_data.get("tp1_reached")
        or status == "RUNNER_ACTIVE"
        or cleared_stage >= 1
        or idx_stage >= 1
    )
    if not in_runner_phase:
        return 0
    return min(4, max(1, cleared_stage, idx_stage))


def normalize_exit_reason_code(
    exit_reason: Any,
    signal_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve canonical exit_reason_code (preserving legacy codes for older ledger entries)."""
    sig = signal_data if isinstance(signal_data, dict) else {}
    raw_code = sig.get("exit_reason_code")
    if isinstance(raw_code, str) and raw_code.strip():
        up_code = raw_code.strip().upper()
        if up_code in VALID_EXIT_REASON_CODES:
            return up_code

    text = f"{exit_reason or ''} {sig.get('runner_close_reason') or ''}".strip()
    low = text.lower()
    up = str(exit_reason or "").strip().upper()

    if up in VALID_EXIT_REASON_CODES:
        return up
    if "signal deleted" in low or sig.get("deleted_from_chat"):
        return str(exit_reason or "Signal Deleted")
    if (
        "dynamic_atr_trail_sl" in low
        or "dynamic atr trail sl" in low
        or "dynamic atr trailing" in low
        or "dynamic_atr_trail" in low
        or "dynamic atr trail" in low
    ):
        return "DYNAMIC_ATR_TRAIL_SL"
    if (
        "dynamic_atr_tp" in low
        or "dynamic atr tp" in low
        or "dynamic trailing tp" in low
    ):
        return "DYNAMIC_ATR_TP"
    if "bank_harvest_sl" in low or "bank harvest sl" in low:
        return "BANK_HARVEST_SL"
    if "bank_harvest" in low or "bank harvest" in low:
        return "BANK_HARVEST"
    if "profit_ratchet" in low or "profit ratchet" in low:
        return "PROFIT_RATCHET_SL"
    if "stage0_profit_lock" in low or "stage 0 profit-lock" in low or "stage 0 profit lock" in low:
        return "STAGE0_PROFIT_LOCK"
    if "stage2_bank_tp" in low or "stage 2 bank" in low:
        return "STAGE2_BANK_TP"
    if "tp1_bank" in low or "tp1 bank" in low:
        return "TP1_BANK"
    if "partial_bank" in low or "partial bank" in low:
        return "PARTIAL_BANK"
    if "be_sl" in low or "be_lock" in low or "breakeven stop" in low or "breakeven sl" in low:
        return "BE_SL"
    if "initial_sl" in low or "initial sl" in low:
        return "INITIAL_SL"
    if "gold reversal guard" in low or "gold_guard" in low:
        return "GOLD_GUARD"
    if "shortfall" in low:
        return "SHORTFALL_BASE"
    if (
        "fsm_floor" in low
        or "fsm floor" in low
        or "structural floor" in low
        or "virtual tp" in low
        or "floor hit" in low
    ):
        return "FSM_FLOOR"
    if "atr_trail" in low or "atr trail" in low or "trailing stop" in low or "trailed sl" in low:
        return "ATR_TRAIL"
    if (
        "tp_ceiling" in low
        or "ceiling" in low
        or "tp5 reached" in low
        or "tp4 reached" in low
        or "tp3 reached" in low
        or "tp2 reached" in low
        or "tp1 full bank" in low
        or "broker take profit" in low
        or up in ("TP", "TP1", "TP2", "TP3", "TP4", "TP5")
    ):
        return "TP_CEILING"
    if "broker_sl" in low or "stop loss" in low or up == "SL":
        return "BROKER_SL"
    if "ea_close" in low or "closed by the copier" in low or "closed by the bot" in low:
        return "EA_CLOSE"
    if (
        "manual" in low
        or "/bank" in low
        or "panic" in low
        or "full close" in low
        or "partial close" in low
    ):
        return "MANUAL"
    if isinstance(raw_code, str) and raw_code.strip():
        return raw_code.strip()
    return str(exit_reason or "CLOSED")


def _infer_exit_reason_code(signal_data: Dict[str, Any], exit_reason: str) -> str:
    """Resolve canonical exit_reason_code or preserve descriptive unmapped reason."""
    return normalize_exit_reason_code(exit_reason, signal_data=signal_data)


def _resolve_ref_entry(signal_data: Dict[str, Any], is_buy: bool) -> Optional[float]:
    """Resolve best available reference entry price for MFE computation."""
    layers = signal_data.get("layers")
    r_layers = list(
        signal_data.get("runner_layers")
        or signal_data.get("last_runner_layers")
        or []
    )
    runner_layer = (
        signal_data.get("runner_layer")
        or signal_data.get("last_runner_layer")
        or (r_layers[-1] if r_layers else None)
    )
    if runner_layer and isinstance(layers, dict):
        r_obj = layers.get(runner_layer)
        if isinstance(r_obj, dict):
            for k in ("entry_price", "entry", "price_open", "target_price"):
                if _is_num(r_obj.get(k)) and float(r_obj[k]) > 0:
                    return float(r_obj[k])

    for k in ("recorded_entry_price", "initial_price", "single_price"):
        if _is_num(signal_data.get(k)) and float(signal_data[k]) > 0:
            return float(signal_data[k])

    zone = signal_data.get("zone")
    if isinstance(zone, dict):
        z_px = zone.get("top") if is_buy else zone.get("bottom")
        if _is_num(z_px) and float(z_px) > 0:
            return float(z_px)

    if isinstance(layers, dict):
        for lk in ("L4", "L3", "L2", "L1", "BOTTOM", "MID", "TOP"):
            l_obj = layers.get(lk)
            if isinstance(l_obj, dict):
                for k in ("entry_price", "entry", "price_open", "target_price"):
                    if _is_num(l_obj.get(k)) and float(l_obj[k]) > 0:
                        return float(l_obj[k])
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


def _apply_giveback_currency(record: Dict[str, Any], profit_currency_rate: Optional[float]) -> None:
    """Denominate `giveback_cash` in the ACCOUNT currency -- or leave it unknown.

    `mfe_cash` is a price-based cash amount in the symbol's PROFIT currency
    (USD for XAUUSD), while `realized_pnl` comes from the broker in the ACCOUNT
    currency (GBP). The two can only be subtracted once the MFE has been
    converted, so the giveback is `mfe_cash * rate - realized_pnl`, denominated
    in the account currency.

    Without a known rate the giveback stays `None`: a mixed-currency number is
    never written. `mfe_cash` itself is left untouched, so `r_at_exit` -- a
    ratio of two price-based amounts -- stays currency-neutral.
    """
    if record.get("fx_rate_to_account") is not None:
        return  # already denominated
    rate = _safe_float(profit_currency_rate, 0.0) if profit_currency_rate is not None else 0.0
    if rate <= 0.0:
        return
    mfe_cash = record.get("mfe_cash")
    if not _is_num(mfe_cash):
        return
    realized = _safe_float(record.get("realized_pnl"), 0.0)
    record["giveback_cash"] = round(float(mfe_cash) * rate - realized, 2)
    record["giveback_currency"] = str(record.get("currency") or "GBP")
    record["fx_rate_to_account"] = round(rate, 8)


def build_trade_record(
    signal_key: str,
    signal_data: Dict[str, Any],
    realized_pnl: float,
    exit_reason: str,
    currency: str = "GBP",
    dry_run: bool = False,
    extra: Optional[Dict[str, Any]] = None,
    profit_currency_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a canonical trades_ledger.jsonl record for a closed signal (schema_version=4)."""
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

    direction = str(signal_data.get("direction") or "BUY").upper()
    is_buy = direction == "BUY"

    raw_explicit_code = signal_data.get("exit_reason_code")
    explicit_code = (
        raw_explicit_code.strip().upper()
        if isinstance(raw_explicit_code, str)
        and raw_explicit_code.strip().upper() in _CANONICAL_EXIT_REASON_CODES
        else None
    )
    verbatim_reason = str(exit_reason or signal_data.get("runner_close_reason") or "CLOSED")
    if explicit_code is not None:
        resolved_exit_reason = explicit_code
        resolved_exit_detail = str(exit_reason or signal_data.get("runner_close_reason") or "")
        resolved_exit_code = explicit_code
    else:
        resolved_exit_reason = verbatim_reason
        resolved_exit_detail = verbatim_reason
        resolved_exit_code = _infer_exit_reason_code(signal_data, verbatim_reason)

    exit_stage = _infer_exit_stage(signal_data)
    raw_exit_px = signal_data.get("exit_price")
    exit_price: Optional[float] = round(float(raw_exit_px), 4) if _is_num(raw_exit_px) else None

    raw_mfe = signal_data.get("max_favorable_excursion")
    raw_pk = raw_mfe.get("peak_price") if isinstance(raw_mfe, dict) else None
    raw_pp_oz = raw_mfe.get("peak_profit_per_oz") if isinstance(raw_mfe, dict) else None

    if not _is_num(raw_pk):
        for pk_key in ("trail_peak", "runner_peak_price", "peak_price", "reversal_guard_peak_price"):
            if _is_num(signal_data.get(pk_key)):
                raw_pk = signal_data[pk_key]
                break

    peak_price: Optional[float] = round(float(raw_pk), 4) if _is_num(raw_pk) else None
    if _is_num(raw_pp_oz):
        peak_profit_per_oz: Optional[float] = round(max(0.0, float(raw_pp_oz)), 4)
    else:
        ref_entry = _resolve_ref_entry(signal_data, is_buy)
        if peak_price is not None and ref_entry is not None:
            raw_exc = (peak_price - ref_entry) if is_buy else (ref_entry - peak_price)
            peak_profit_per_oz = round(max(0.0, raw_exc), 4)
        else:
            fallback_pp = (
                signal_data.get("reversal_guard_peak_profit")
                if _is_num(signal_data.get("reversal_guard_peak_profit"))
                else signal_data.get("peak_profit")
            )
            peak_profit_per_oz = round(max(0.0, float(fallback_pp)), 4) if _is_num(fallback_pp) else None

    mfe_dict: Dict[str, Optional[float]] = {
        "peak_price": peak_price,
        "peak_profit_per_oz": peak_profit_per_oz,
    }

    raw_gb = signal_data.get("giveback_from_peak")
    if _is_num(raw_gb):
        giveback_from_peak: Optional[float] = round(max(0.0, float(raw_gb)), 4)
    elif resolved_exit_code == "TP_CEILING" and (peak_price is not None or exit_price is not None):
        giveback_from_peak = 0.0
    elif peak_price is not None and exit_price is not None:
        raw_drop = (peak_price - exit_price) if is_buy else (exit_price - peak_price)
        giveback_from_peak = round(max(0.0, raw_drop), 4)
    else:
        giveback_from_peak = None

    raw_tier = signal_data.get("trail_tier_used")
    trail_tier_used: Optional[Dict[str, Any]] = dict(raw_tier) if isinstance(raw_tier, dict) else None

    layers = signal_data.get("layers")
    resolved_r_layers = [
        str(x)
        for x in (
            signal_data.get("runner_layers")
            or signal_data.get("last_runner_layers")
            or ([signal_data["runner_layer"]] if signal_data.get("runner_layer") else [])
            or ([signal_data["last_runner_layer"]] if signal_data.get("last_runner_layer") else [])
        )
        if x
    ]
    if not resolved_r_layers:
        r_tkts = [
            _safe_int(t)
            for t in (signal_data.get("last_runner_tickets") or ([signal_data.get("last_runner_ticket")] if signal_data.get("last_runner_ticket") else []))
            if _safe_int(t) is not None
        ]
        if r_tkts and isinstance(layers, dict):
            for lk, lv in layers.items():
                if isinstance(lv, dict) and _safe_int(lv.get("ticket")) in r_tkts:
                    resolved_r_layers.append(str(lk))
        elif layers_filled:
            resolved_r_layers = [str(layers_filled[-1])]
    resolved_r_layer = (
        signal_data.get("runner_layer")
        or signal_data.get("last_runner_layer")
        or (resolved_r_layers[-1] if resolved_r_layers else None)
    )

    layer_exits: List[Dict[str, Any]] = []
    if isinstance(layers, dict):
        for lk, lv in layers.items():
            if not isinstance(lv, dict):
                continue
            st_lv = str(lv.get("status") or "").strip().upper()
            if st_lv in _TERMINAL_UNFILLED_STATUSES and not lv.get("exit_deal_tickets") and lv.get("realized_pnl") is None:
                continue
            if st_lv == "PENDING" and not lv.get("exit_price"):
                continue
            l_entry = lv.get("entry_price") or lv.get("entry") or lv.get("price_open") or lv.get("target_price")
            layer_exits.append({
                "layer": str(lk),
                "ticket": _safe_int(lv.get("ticket")),
                "role": lv.get("role"),
                "lot": round(_safe_float(lv.get("lot"), 0.0), 4),
                "remaining_lot": round(_safe_float(lv.get("remaining_lot"), _safe_float(lv.get("lot"), 0.0)), 4),
                "entry_price": round(float(l_entry), 4) if _is_num(l_entry) else None,
                "mfe_peak_price": round(float(lv["mfe_peak_price"]), 4) if _is_num(lv.get("mfe_peak_price")) else None,
                "mfe_dollars": round(float(lv["mfe_dollars"]), 4) if _is_num(lv.get("mfe_dollars")) else None,
                "ratchet_tier": lv.get("ratchet_tier"),
                "exit_price": round(float(lv["exit_price"]), 4) if _is_num(lv.get("exit_price")) else None,
                "exit_reason_code": lv.get("exit_reason_code"),
                "realized_pnl": round(float(lv["realized_pnl"]), 2) if _is_num(lv.get("realized_pnl")) else None,
                "exit_deal_tickets": list(lv.get("exit_deal_tickets") or []),
            })

    # --- Schema 4 telemetry: cash excursion, giveback, R and exit age -------
    # `max_favorable_excursion.peak_profit_per_oz` is a PER-OUNCE figure; it is
    # converted to cash here (contract = 100 oz per lot) so giveback is finally
    # measurable.
    CONTRACT_OZ_PER_LOT = 100.0
    mfe_per_oz = _safe_float((mfe_dict or {}).get("peak_profit_per_oz"), 0.0) if mfe_dict else 0.0
    lots_for_mfe = _safe_float(signal_data.get("total_lots"), 0.0)
    if lots_for_mfe <= 0.0:
        # Fall back to the layer-derived total when the signal carries no size.
        lots_for_mfe = _safe_float(total_lots, 0.0)
    mfe_cash: Optional[float] = None
    if mfe_per_oz > 0.0 and lots_for_mfe > 0.0:
        mfe_cash = round(mfe_per_oz * lots_for_mfe * CONTRACT_OZ_PER_LOT, 2)

    # The giveback can only be computed once the MFE -- a price-based amount in
    # the symbol's PROFIT currency -- has been converted into the ACCOUNT
    # currency of `realized_pnl`. The conversion is applied to the finished
    # record below; without a known rate the giveback stays UNKNOWN (None).
    giveback_cash: Optional[float] = None

    initial_r = _safe_float(signal_data.get("initial_r_per_oz"), 0.0)
    if initial_r <= 0.0:
        # Fallback: derive the signal's initial risk per ounce from its layers.
        _layer_risks = []
        for _lv in (signal_data.get("layers") or {}).values():
            if not isinstance(_lv, dict):
                continue
            _e = _safe_float(_lv.get("entry_price") or _lv.get("entry") or _lv.get("target_price"), 0.0)
            _s = _safe_float(_lv.get("initial_sl"), 0.0)
            if _e > 0.0 and _s > 0.0:
                _layer_risks.append(abs(_e - _s))
        if _layer_risks:
            initial_r = sum(_layer_risks) / len(_layer_risks)
    initial_r_per_oz: Optional[float] = round(initial_r, 4) if initial_r > 0.0 else None
    r_at_exit: Optional[float] = None
    if initial_r_per_oz and mfe_cash is not None and initial_r_per_oz > 0.0:
        risk_cash = initial_r_per_oz * lots_for_mfe * CONTRACT_OZ_PER_LOT
        if risk_cash > 0.0:
            r_at_exit = round(mfe_cash / risk_cash, 4)

    exit_age_seconds: Optional[int] = None
    # The RESOLVED fill time -- never the raw `first_fill_at` key. For a normal
    # zone signal L1 fills at MARKET (stamped on the LAYER) and L2-L4 later as
    # limits (stamped on the SIGNAL), so the raw signal-level stamp can be
    # LATER than the true first fill. `risk_lifecycle.resolve_fill_time`
    # returns the EARLIEST known fill.
    _filled_at = resolve_fill_time(signal_data)
    _closed_at = signal_data.get("closed_at") or datetime.now(timezone.utc).isoformat()
    if _filled_at and _closed_at:
        try:
            _f = _filled_at if isinstance(_filled_at, datetime) else datetime.fromisoformat(str(_filled_at).replace("Z", "+00:00"))
            _c = datetime.fromisoformat(str(_closed_at).replace("Z", "+00:00"))
            if _f.tzinfo is None:
                _f = _f.replace(tzinfo=timezone.utc)
            if _c.tzinfo is None:
                _c = _c.replace(tzinfo=timezone.utc)
            exit_age_seconds = int((_c - _f).total_seconds())
        except (TypeError, ValueError):
            exit_age_seconds = None

    lifecycle_action = signal_data.get("lifecycle_action")

    # --- Reconcile the realised P&L against the broker's exit deals -------------
    # The deal profit is the only authority. The signal-level figure is the sum the
    # copier accumulated while the signal was alive; when the two disagree (a
    # partially booked layer, a close that landed outside the accounting window)
    # the DEAL sum wins and the variance is recorded so the gap stays auditable.
    deal_pnl = 0.0
    deal_layers = 0
    deal_bearing = 0
    for _le in layer_exits:
        if _le.get("exit_deal_tickets"):
            deal_bearing += 1
            if _is_num(_le.get("realized_pnl")):
                deal_pnl += float(_le["realized_pnl"])
                deal_layers += 1
    realized_pnl_variance: Optional[float] = None
    if deal_bearing > 0 and deal_layers == deal_bearing:
        _variance = round(_safe_float(realized_pnl, 0.0) - deal_pnl, 2)
        if abs(_variance) > 0.01:
            realized_pnl_variance = _variance
            realized_pnl = deal_pnl

    record: Dict[str, Any] = {
        "schema_version": 4,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "opened_at": signal_data.get("timestamp"),
        "signal_key": str(signal_key),
        "chat_id": signal_data.get("chat_id"),
        "message_id": raw_mid,
        "symbol": signal_data.get("symbol", getattr(_config, "DEFAULT_SYMBOL", "XAUUSD.s")),
        "direction": direction,
        "entry_type": _infer_entry_type(signal_data),
        "total_lots": total_lots,
        "filled_lots": filled_lots,
        "tickets": tickets,
        "layers_filled": layers_filled,
        "highest_milestone_cleared": highest_cleared,
        "runner_layer": resolved_r_layer,
        "runner_layers": resolved_r_layers,
        "exit_reason": resolved_exit_reason,
        "exit_reason_code": resolved_exit_code,
        "exit_reason_detail": resolved_exit_detail,
        "exit_stage": exit_stage,
        "exit_price": exit_price,
        "max_favorable_excursion": mfe_dict,
        "giveback_from_peak": giveback_from_peak,
        "mfe_cash": mfe_cash,
        "giveback_cash": giveback_cash,
        "giveback_currency": str(currency or "GBP"),
        "fx_rate_to_account": None,
        "initial_r_per_oz": initial_r_per_oz,
        "r_at_exit": r_at_exit,
        "exit_age_seconds": exit_age_seconds,
        "lifecycle_action": (str(lifecycle_action) if lifecycle_action else None),
        "trail_tier_used": trail_tier_used,
        "layer_exits": layer_exits,
        "realized_pnl": round(_safe_float(realized_pnl, 0.0), 2),
        "realized_pnl_variance": realized_pnl_variance,
        "realized_pnl_layers_counted": deal_layers,
        "currency": str(currency or "GBP"),
        "dry_run": bool(dry_run),
    }
    if extra:
        record.update(extra)
    _apply_giveback_currency(record, profit_currency_rate)
    return record


class TradeLedger:
    """Thread-safe append-only JSONL trade ledger writer & analytics reader for /health."""

    def __init__(self, file_path: Optional[Path] = None):
        configured = getattr(_config, "TRADES_LEDGER_PATH", _DEFAULT_LEDGER_PATH)
        self.file_path = Path(file_path or configured)
        self._lock = threading.Lock()

    def append_trade(
        self,
        record: Dict[str, Any],
        profit_currency_rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Append a single closed-trade record atomically as one JSON line."""
        entry = dict(record)
        entry.setdefault("schema_version", 4)
        entry.setdefault("closed_at", datetime.now(timezone.utc).isoformat())
        entry["realized_pnl"] = round(_safe_float(entry.get("realized_pnl"), 0.0), 2)
        # Every appended record carries the giveback contract, even when the
        # giveback is unknown (no rate) -- an absent key is not the same thing
        # as a known zero.
        entry.setdefault("giveback_cash", None)
        entry.setdefault("giveback_currency", str(record.get("currency") or "GBP"))
        entry.setdefault("fx_rate_to_account", None)
        _apply_giveback_currency(entry, profit_currency_rate)

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
        profit_currency_rate: Optional[float] = None,
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
            profit_currency_rate=profit_currency_rate,
        )
        return self.append_trade(rec, profit_currency_rate=profit_currency_rate)

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
        mfe_surrendered = 0.0
        today_mfe_surrendered = 0.0
        r_values: List[float] = []
        reached_half_r = 0
        reached_one_r = 0

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

            # Cash surrendered after the peak excursion: the number that shows whether
            # the protection engine actually keeps what it finds.
            gb = t.get("giveback_cash")
            gb_val = _safe_float(gb, 0.0) if _is_num(gb) else 0.0
            mfe_surrendered += gb_val

            # R-at-exit distribution: if nothing ever reaches 0.5R the R-relative
            # ladders can never engage, and the account is running on luck.
            r_val = t.get("r_at_exit")
            if _is_num(r_val):
                r_f = float(r_val)
                r_values.append(r_f)
                if r_f >= 0.5:
                    reached_half_r += 1
                if r_f >= 1.0:
                    reached_one_r += 1

            rec_date = t.get("server_date")
            if not rec_date:
                closed_at = str(t.get("closed_at") or "")
                rec_date = closed_at[:10] if len(closed_at) >= 10 else ""
            if rec_date == target_date:
                today_trades += 1
                today_pnl += pnl
                today_mfe_surrendered += gb_val

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
            "mfe_surrendered_cash": round(mfe_surrendered, 2),
            "today_mfe_surrendered_cash": round(today_mfe_surrendered, 2),
            "r_at_exit_max": round(max(r_values), 4) if r_values else None,
            "r_at_exit_avg": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "trades_reached_0_5r": reached_half_r,
            "trades_reached_1r": reached_one_r,
        }
