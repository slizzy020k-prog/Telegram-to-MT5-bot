#!/usr/bin/env python3
"""
Deterministic 12-Trade Nov 17-19 XAUUSD.s M1-Bar A/B Replay Proof
=================================================================

Compares:
  - Arm A (`4544f7f` v3.5.2 baseline): executed inside an isolated temporary
    git worktree at commit `4544f7f` in a dedicated Python subprocess.
  - Arm B (`v3.6.0` calibrated working tree): executed in a separate Python
    subprocess against the working repository with calibrated v3.6.0 defaults.

Both arms run strictly in `DRY_RUN=true` with isolated temporary runtime paths
(`STATE_FILE_PATH`, `LOG_FILE_PATH`, `TRADES_LEDGER_PATH`, `LOCK_FILE_PATH`)
set in `os.environ` prior to importing `config` or `main`.

MT5 usage is strictly read-only (`mt5.copy_rates_range` only; zero orders).
Multi-layer trades initialize all filled MT5 layers (`L1..Lk`) at `open_utc`
using exact MT5 fill prices, with legacy `TOP -> L1` normalized so all 12 trades
exercise the 4-layer / Profit Ratchet FSM uniformly.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "4544f7f"
SYMBOL = "XAUUSD.s"
GBP_PER_PT_001_LOT = 0.7589
MT5_SERVER_OFFSET_SEC = 7200  # MT5 UTC+2 -> UTC/GMT
ATR_PERIOD = 14
WARMUP_START_MT5 = datetime(2026, 11, 17, 0, 0)
WARMUP_END_MT5 = datetime(2026, 11, 19, 13, 0)
CHAT_ID = -1001234567890

# Calibrated v3.6.0 parameters (M1 Swarm Consensus + 9 canonical Rev 2 defaults)
ARM_B_CALIBRATED_PARAMS: Dict[str, Any] = {
    "PROFIT_RATCHET_ENABLED": True,
    "BANK_HARVEST_ENABLED": True,
    "TIERED_STAGE_BANKING_ENABLED": True,
    "STAGE1_ATR_ACTIVATION_PCT": 0.50,
    "TP1_TRAIL_ATR_MULT": 1.55,
    "TP1_TRAIL_MIN_DOLLARS": 2.50,
    "ARM_THRESHOLD_PCT": 0.90,
    "GIVEBACK_PCT": 0.30,
    "REVERSAL_CONFIRM_TICKS": 2,
    "RUNNER_REVERSAL_CONFIRM_TICKS": 2,
    "MAX_ALLOWED_SPREAD_DOLLARS": 0.45,
    "RATCHET_TIER2_MIN_LOCK_DOLLARS": 1.50,
    "RATCHET_TIER3_RETENTION_PCT": 0.55,
    "RATCHET_TIER4_RETENTION_PCT": 0.68,
    "RATCHET_TIER4_MIN_CUSHION": 3.75,
    "RATCHET_TIER5_RETENTION_PCT": 0.78,
    "RATCHET_STAGE4_MIN_CUSHION": 3.00,
    "RATCHET_STAGE4_MAX_CUSHION": 4.50,
    "PEAK_HARVEST_MIN_VIRT_CUSHION": 2.25,
    "BANK_HARVEST_ALT_ARM_PCT": 0.80,
}

# Arm C params: Arm B plus the R-relative profit floor and the trade-age guard.
ARM_C_CALIBRATED_PARAMS: Dict[str, Any] = {
    "R_LOCK_ENABLED": True,
    "LIFECYCLE_ENABLED": True,
    "LIFECYCLE_DERISK_SECONDS": 14400,
    "LIFECYCLE_FLATTEN_SECONDS": 28800,
    "LIFECYCLE_PENDING_EXPIRE_SECONDS": 43200,
    "LIFECYCLE_SESSION_FLATTEN_ENABLED": False,
}

# Exact 12 filled Nov 17-19 XAUUSD.s trades (verified against MT5 deals, signals_state.json, copier.log)
TRADE_CATALOG: List[Dict[str, Any]] = [
    {
        "msg_id": "12973",
        "dir": "SELL",
        "open_utc": "2026-11-17T02:36:32Z",
        "close_utc": "2026-11-17T02:44:35Z",
        "layers": [("L1", 0.01, 3865.00)],  # normalized TOP -> L1
        "sl": 3890.0,
        "tp_levels": {"TP1": 3862.0, "TP2": 3852.0, "TP3": 3845.0, "TP4": 3835.0, "TP5": 3825.0},
        "actual_mt5_gbp": 0.92,
        "actual_exit_reason": "TP1 SHORTFALL (Peak 3862.88, Close 3863.79)",
    },
    {
        "msg_id": "9699",
        "dir": "BUY",
        "open_utc": "2026-11-17T11:13:03Z",
        "close_utc": "2026-11-17T11:44:25Z",
        "layers": [("L1", 0.01, 3858.00)],  # normalized TOP -> L1
        "sl": 3850.0,
        "tp_levels": {"TP1": 3865.0, "TP2": 3880.0, "TP3": 3900.0, "TP4": 3910.0, "TP5": 3920.0},
        "actual_mt5_gbp": 3.80,
        "actual_exit_reason": "TP1 SHORTFALL (Peak 3863.82, Close 3863.01; BE 9702)",
    },
    {
        "msg_id": "9705",
        "dir": "BUY",
        "open_utc": "2026-11-17T13:38:40Z",
        "close_utc": "2026-11-17T13:46:59Z",
        "layers": [("L1", 0.01, 3844.00)],  # normalized TOP -> L1
        "sl": 3820.0,
        "tp_levels": {"TP1": 3849.0, "TP2": 3857.0, "TP3": 3870.0, "TP4": 3880.0, "TP5": 3890.0},
        "actual_mt5_gbp": 0.85,
        "actual_exit_reason": "TG Copier Close @ 3845.12 on Msg 9712 TP1 reply",
    },
    {
        "msg_id": "13030",
        "dir": "SELL",
        "open_utc": "2026-11-17T15:42:55Z",
        "close_utc": "2026-11-17T15:44:31Z",
        "layers": [("L1", 0.01, 3885.00), ("L2", 0.01, 3888.33)],
        "sl": 3910.0,
        "tp_levels": {"TP1": 3883.0, "TP2": 3875.0, "TP3": 3873.0, "TP4": 3865.0, "TP5": 3855.0},
        "actual_mt5_gbp": 4.65,
        "actual_exit_reason": "Broker Take Profit Hit (TP1 @ 3883.50/3883.70)",
    },
    {
        "msg_id": "13078",
        "dir": "BUY",
        "open_utc": "2026-11-18T00:23:35Z",
        "close_utc": "2026-11-18T01:10:44Z",
        "layers": [("L1", 0.01, 3833.54), ("L2", 0.01, 3831.33), ("L3", 0.01, 3825.67)],
        "sl": 3813.0,
        "tp_levels": {"TP1": 3840.0, "TP2": 3847.0, "TP3": 3850.0, "TP4": 3857.0, "TP5": 3867.0},
        "actual_mt5_gbp": -23.24,
        "actual_exit_reason": "MT5 Stop-Out after SL 3813",
    },
    {
        "msg_id": "13153",
        "dir": "BUY",
        "open_utc": "2026-11-18T17:52:42Z",
        "close_utc": "2026-11-18T17:53:53Z",
        "layers": [("L1", 0.01, 3730.00)],
        "sl": 3705.0,
        "tp_levels": {"TP1": 3733.0, "TP2": 3740.0, "TP3": 3743.0, "TP4": 3750.0, "TP5": 3760.0},
        "actual_mt5_gbp": 0.44,
        "actual_exit_reason": "GOLD_GUARD (Peak 3732.07, Close 3730.58)",
    },
    {
        "msg_id": "9905",
        "dir": "SELL",
        "open_utc": "2026-11-18T18:22:06Z",
        "close_utc": "2026-11-18T18:23:42Z",
        "layers": [("L1", 0.01, 3739.18), ("L2", 0.01, 3739.18), ("L3", 0.01, 3739.17), ("L4", 0.02, 3739.20)],
        "sl": 3760.0,
        "tp_levels": {"TP1": 3733.0, "TP2": 3725.0, "TP3": 3715.0, "TP4": 3705.0, "TP5": 3695.0},
        "actual_mt5_gbp": -0.05,
        "actual_exit_reason": "Manual close from MT5 app",
    },
    {
        "msg_id": "9934",
        "dir": "SELL",
        "open_utc": "2026-11-18T23:39:39Z",
        "close_utc": "2026-11-19T00:03:35Z",
        "layers": [("L1", 0.01, 3741.70)],
        "sl": 3767.0,
        "tp_levels": {"TP1": 3737.0, "TP2": 3730.0, "TP3": 3715.0, "TP4": 3700.0, "TP5": 3650.0},
        "actual_mt5_gbp": 1.16,
        "actual_exit_reason": "ATR_TRAIL Stg1 (Peak 3737.67, Close 3740.17; ledger £0.00 bug)",
    },
    {
        "msg_id": "13167",
        "dir": "BUY",
        "open_utc": "2026-11-19T01:02:08Z",
        "close_utc": "2026-11-19T01:02:53Z",
        "layers": [("L1", 0.01, 3750.00)],
        "sl": 3725.0,
        "tp_levels": {"TP1": 3753.0, "TP2": 3760.0, "TP3": 3765.0, "TP4": 3770.0, "TP5": 3780.0},
        "actual_mt5_gbp": 0.85,
        "actual_exit_reason": "GOLD_GUARD (Peak 3752.64, Close 3751.12)",
    },
    {
        "msg_id": "9937",
        "dir": "SELL",
        "open_utc": "2026-11-19T06:19:10Z",
        "close_utc": "2026-11-19T07:02:30Z",
        "layers": [("L1", 0.01, 3737.43), ("L2", 0.01, 3740.26)],
        "sl": 3750.0,
        "tp_levels": {"TP1": 3725.0, "TP2": 3710.0, "TP3": 3680.0, "TP4": 3670.0, "TP5": 3660.0},
        "actual_mt5_gbp": 0.56,
        "actual_exit_reason": "BROKER_SL BE (3738.48 after Msg 9939)",
    },
    {
        "msg_id": "9943",
        "dir": "SELL",
        "open_utc": "2026-11-19T07:02:34Z",
        "close_utc": "2026-11-19T07:17:58Z",
        "layers": [("L1", 0.01, 3739.78), ("L2", 0.01, 3739.76)],
        "sl": 3755.0,
        "tp_levels": {"TP1": 3730.0, "TP2": 3725.0, "TP3": 3710.0, "TP4": 3680.0, "TP5": 3670.0},
        "actual_mt5_gbp": 0.45,
        "actual_exit_reason": "BROKER_SL BE (3739.47 after Msg 9947)",
    },
    {
        "msg_id": "9953",
        "dir": "SELL",
        "open_utc": "2026-11-19T07:52:46Z",
        "close_utc": "2026-11-19T08:12:43Z",
        "layers": [("L1", 0.01, 3730.86), ("L2", 0.01, 3732.67), ("L3", 0.01, 3734.33), ("L4", 0.02, 3736.00)],
        "sl": 3740.0,
        "tp_levels": {"TP1": 3700.0, "TP2": 3680.0, "TP3": 3650.0, "TP4": 3640.0, "TP5": 3630.0},
        "actual_mt5_gbp": -25.36,
        "actual_exit_reason": "BROKER_SL (SL=3740.0, all 4 layers)",
    },
]


def _parse_utc(ts_str: str) -> datetime:
    s = ts_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_iso(epoch_sec: int) -> str:
    return datetime.fromtimestamp(int(epoch_sec), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_rollover_utc(iso_str: Optional[str]) -> bool:
    """True if UTC timestamp falls inside rollover window 21:55-23:05 GMT."""
    if not iso_str:
        return False
    dt = _parse_utc(iso_str)
    mins = dt.hour * 60 + dt.minute
    return (21 * 60 + 55) <= mins <= (23 * 60 + 5)


def _compute_rolling_atr(bars: List[Dict[str, Any]], period: int = ATR_PERIOD) -> None:
    """
    Populate `bar['atr14']` on each bar using the exact closed-bar SMA-TR formula
    from `mt5_executor.py:930-949` (`eff_period + 1` preceding closed bars).
    """
    n = len(bars)
    for i in range(n):
        if i < 2:
            bars[i]["atr14"] = 0.0
            continue
        start_idx = max(0, i - (period + 1))
        window = bars[start_idx:i]
        if len(window) < 2:
            bars[i]["atr14"] = 0.0
            continue
        trs: List[float] = []
        prev_close = float(window[0]["close"])
        for j in range(1, len(window)):
            b = window[j]
            h = float(b["high"])
            l = float(b["low"])
            c = float(b["close"])
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            if tr >= 0.0:
                trs.append(tr)
            prev_close = c
        bars[i]["atr14"] = round(sum(trs) / len(trs), 4) if trs else 0.0


def fetch_m1_bars(
    bars_json_path: Optional[Path] = None,
    save_bars_path: Optional[Path] = None,
    spread_override: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Load M1 bars from JSON or fetch read-only from MT5 (`copy_rates_range`),
    normalize server timestamps (`-7200s` -> UTC), compute spread and ATR(14).
    """
    bars: List[Dict[str, Any]] = []
    if bars_json_path is not None:
        payload = json.loads(bars_json_path.read_text(encoding="utf-8"))
        raw_bars = payload["bars"] if isinstance(payload, dict) else payload
        for b in raw_bars:
            bars.append(
                {
                    "time_utc": int(b["time_utc"]),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "spread_pts": int(b.get("spread_pts", 32)),
                    "spread_dollars": round(
                        float(spread_override)
                        if spread_override is not None
                        else max(0.05, float(b.get("spread_dollars", int(b.get("spread_pts", 32)) * 0.01))),
                        4,
                    ),
                }
            )
    else:
        import MetaTrader5 as mt5

        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        try:
            rates = mt5.copy_rates_range(
                SYMBOL,
                mt5.TIMEFRAME_M1,
                WARMUP_START_MT5,
                WARMUP_END_MT5,
            )
            if rates is None or len(rates) == 0:
                raise RuntimeError(f"mt5.copy_rates_range returned no bars: {mt5.last_error()}")
            for r in rates:
                t_utc = int(r["time"]) - MT5_SERVER_OFFSET_SEC
                sp_pts = int(r["spread"])
                sp_dlr = (
                    round(float(spread_override), 4)
                    if spread_override is not None
                    else round(max(0.05, sp_pts * 0.01), 4)
                )
                bars.append(
                    {
                        "time_utc": t_utc,
                        "open": round(float(r["open"]), 2),
                        "high": round(float(r["high"]), 2),
                        "low": round(float(r["low"]), 2),
                        "close": round(float(r["close"]), 2),
                        "spread_pts": sp_pts,
                        "spread_dollars": sp_dlr,
                    }
                )
        finally:
            mt5.shutdown()

    bars.sort(key=lambda x: x["time_utc"])
    _compute_rolling_atr(bars, period=ATR_PERIOD)

    if save_bars_path is not None:
        save_bars_path.parent.mkdir(parents=True, exist_ok=True)
        out_payload = {
            "symbol": SYMBOL,
            "timeframe": "M1",
            "server_offset_sec": MT5_SERVER_OFFSET_SEC,
            "atr_period": ATR_PERIOD,
            "bar_count": len(bars),
            "first_utc": _utc_iso(bars[0]["time_utc"]) if bars else None,
            "last_utc": _utc_iso(bars[-1]["time_utc"]) if bars else None,
            "bars": bars,
        }
        save_bars_path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")

    return bars


def _find_entry_bar_index(bars: List[Dict[str, Any]], open_utc: str) -> int:
    """Return index of the M1 bar containing `open_utc` (`time_utc <= open_ts < time_utc + 60`)."""
    open_ts = int(_parse_utc(open_utc).timestamp())
    for idx, b in enumerate(bars):
        bt = int(b["time_utc"])
        if bt <= open_ts < bt + 60:
            return idx
    for idx, b in enumerate(bars):
        if int(b["time_utc"]) >= open_ts:
            return idx
    raise RuntimeError(f"No M1 bar found for open_utc={open_utc}")


def _compute_potential_mfe_before_sl(
    trade: Dict[str, Any],
    bars: List[Dict[str, Any]],
    idx0: int,
) -> float:
    """Measure potential favorable excursion ($/oz from avg entry) before initial SL is hit."""
    is_buy = trade["dir"] == "BUY"
    sl = float(trade["sl"])
    total_lot = sum(l[1] for l in trade["layers"])
    avg_entry = sum(l[1] * l[2] for l in trade["layers"]) / total_lot
    max_fav = 0.0
    for b in bars[idx0 + 1 :]:
        h = float(b["high"])
        l = float(b["low"])
        sp = float(b["spread_dollars"])
        if is_buy:
            fav = h - avg_entry
            if fav > max_fav:
                max_fav = fav
            if l <= sl + 1e-9:
                break
        else:
            fav = avg_entry - (l + sp)
            if fav > max_fav:
                max_fav = fav
            if (h + sp) >= sl - 1e-9:
                break
    return round(max(0.0, max_fav), 2)


def _bar_tick_sequence(bar: Dict[str, Any]) -> List[Tuple[float, float]]:
    """
    Deterministic intra-minute tick path:
      - Bull (`C >= O`): O -> L -> H -> C
      - Bear (`C < O`):  O -> H -> L -> C
    Duplicated 2x per point so `REVERSAL_CONFIRM_TICKS=2` triggers deterministically.
    Returns list of `(bid, ask)` tuples.
    """
    o = float(bar["open"])
    h = float(bar["high"])
    l = float(bar["low"])
    c = float(bar["close"])
    sp = float(bar["spread_dollars"])
    pts = [o, l, h, c] if c >= o else [o, h, l, c]
    ticks: List[Tuple[float, float]] = []
    for p in pts:
        bid = round(p, 2)
        ask = round(p + sp, 2)
        ticks.append((bid, ask))
        ticks.append((bid, ask))
    return ticks


# ---------------------------------------------------------------------------
# ISOLATED WORKER IMPLEMENTATION (executed inside subprocess per Arm)
# ---------------------------------------------------------------------------

def _shutdown_copier_loggers() -> None:
    for name in ("copier", "telethon"):
        log_obj = logging.getLogger(name)
        for h in list(log_obj.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    h.close()
                except Exception:
                    pass
                log_obj.removeHandler(h)


class BrokerSpy:
    """
    Simulates real MT5 broker SL/TP state, partial closes, full closes, and
    deal-level GBP PnL (`pts * (lot / 0.01) * 0.7589`) on top of `DRY_RUN=true`.
    """

    def __init__(self, executor: Any, is_buy: bool) -> None:
        self.executor = executor
        self.is_buy = is_buy
        self.positions: Dict[int, Dict[str, Any]] = {}
        self.closed_events: List[Dict[str, Any]] = []
        self.partial_deals_by_ticket: Dict[int, List[Dict[str, Any]]] = {}
        self.closed_profit_by_ticket: Dict[int, float] = {}
        self.last_exit_price_by_ticket: Dict[int, float] = {}
        self.last_close_reason_by_ticket: Dict[int, str] = {}
        self.current_tick: Optional[SimpleNamespace] = None
        self.current_utc: str = ""
        self._deal_seq: int = 900000

    def register_position(
        self,
        ticket: int,
        layer_key: str,
        lot: float,
        entry_price: float,
        sl: Optional[float],
        tp: Optional[float],
    ) -> None:
        self.positions[int(ticket)] = {
            "ticket": int(ticket),
            "layer": layer_key,
            "lot": round(float(lot), 4),
            "initial_lot": round(float(lot), 4),
            "entry_price": float(entry_price),
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
            "open": True,
        }

    def _next_deal_id(self) -> int:
        self._deal_seq += 1
        return self._deal_seq

    def _calc_gbp(self, entry_px: float, exit_px: float, closed_lot: float) -> Tuple[float, float]:
        pts = (exit_px - entry_px) if self.is_buy else (entry_px - exit_px)
        gbp = pts * (closed_lot / 0.01) * GBP_PER_PT_001_LOT
        return round(pts, 4), round(gbp, 2)

    def _current_exec_exit_price(self) -> float:
        assert self.current_tick is not None
        return float(self.current_tick.bid) if self.is_buy else float(self.current_tick.ask)

    def record_close(
        self,
        ticket: int,
        closed_lot: float,
        exit_price: float,
        reason: str,
        is_partial: bool = False,
    ) -> float:
        t_int = int(ticket)
        pos = self.positions.get(t_int)
        if not pos or not pos["open"] or pos["lot"] <= 0.0:
            return 0.0
        eff_lot = round(min(float(closed_lot), pos["lot"]), 4)
        pts, gbp = self._calc_gbp(pos["entry_price"], exit_price, eff_lot)
        did = self._next_deal_id()
        deal_rec = {
            "deal": did,
            "deal_ticket": did,
            "profit": gbp,
            "volume": eff_lot,
            "price": round(exit_price, 4),
            "reason": reason,
        }
        self.partial_deals_by_ticket.setdefault(t_int, []).append(deal_rec)
        self.closed_profit_by_ticket[t_int] = round(self.closed_profit_by_ticket.get(t_int, 0.0) + gbp, 2)
        self.last_exit_price_by_ticket[t_int] = round(exit_price, 4)
        self.last_close_reason_by_ticket[t_int] = reason
        self.closed_events.append(
            {
                "ticket": t_int,
                "layer": pos["layer"],
                "lot": eff_lot,
                "entry_price": pos["entry_price"],
                "exit_price": round(exit_price, 4),
                "pts": pts,
                "gbp": gbp,
                "reason": reason,
                "is_partial": is_partial,
                "exit_utc": self.current_utc,
            }
        )
        rem = round(pos["lot"] - eff_lot, 4)
        pos["lot"] = rem
        if rem <= 1e-6:
            pos["lot"] = 0.0
            pos["open"] = False
            self.executor._dry_run_ticket_lots.pop(t_int, None)
            self.executor._bot_closed_tickets.add(t_int)
        else:
            self.executor._dry_run_ticket_lots[t_int] = rem
        self.executor._cached_pos_profits[t_int] = self.closed_profit_by_ticket[t_int]
        return gbp

    def total_gbp(self) -> float:
        return round(sum(e["gbp"] for e in self.closed_events), 2)

    def total_pts_lot(self) -> float:
        return round(sum(e["pts"] * e["lot"] for e in self.closed_events), 6)

    def last_exit_price(self) -> Optional[float]:
        return self.closed_events[-1]["exit_price"] if self.closed_events else None

    def last_exit_utc(self) -> Optional[str]:
        return self.closed_events[-1]["exit_utc"] if self.closed_events else None


def _install_broker_spy(executor: Any, spy: BrokerSpy) -> None:
    orig_modify = executor.modify_position_sltp
    orig_close = executor.close_market_position
    orig_partial = getattr(executor, "close_partial_volume", None)

    def _spy_modify_position_sltp(
        ticket: int, sl: Optional[float] = None, tp: Optional[float] = None, **kwargs: Any
    ) -> bool:
        ok = orig_modify(ticket, sl, tp, **kwargs)
        if ok:
            pos = spy.positions.get(int(ticket))
            if pos and pos["open"]:
                if sl is not None and float(sl) > 0.0:
                    pos["sl"] = float(sl)
                if tp is not None and float(tp) > 0.0:
                    pos["tp"] = float(tp)
        return ok

    def _spy_close_market_position(ticket: int) -> bool:
        t_int = int(ticket)
        pos = spy.positions.get(t_int)
        rem_lot = pos["lot"] if (pos and pos["open"]) else 0.0
        ok = orig_close(ticket)
        if ok and rem_lot > 0.0:
            exit_px = spy._current_exec_exit_price()
            spy.record_close(t_int, rem_lot, exit_px, reason="MANUAL/BOT", is_partial=False)
        return ok

    def _spy_close_partial_volume(
        ticket: Optional[int],
        close_volume: float,
        current_volume: Optional[float] = None,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        res = orig_partial(
            ticket,
            close_volume,
            current_volume=current_volume,
            symbol=symbol,
            direction=direction,
        )
        if isinstance(res, dict) and res.get("ok") and ticket:
            c_vol = float(res.get("closed_volume") or close_volume)
            exit_px = spy._current_exec_exit_price()
            spy.record_close(int(ticket), c_vol, exit_px, reason="MANUAL/BOT", is_partial=True)
        return res

    def _spy_get_position_close_info(ticket: Optional[int]) -> Dict[str, Any]:
        if not ticket:
            return {"profit": 0.0, "reason": "UNKNOWN"}
        t_int = int(ticket)
        deals = list(spy.partial_deals_by_ticket.get(t_int) or [])
        total_p = round(spy.closed_profit_by_ticket.get(t_int, 0.0), 2)
        reason = spy.last_close_reason_by_ticket.get(t_int, "MANUAL/BOT")
        exit_px = spy.last_exit_price_by_ticket.get(t_int)
        pos = spy.positions.get(t_int)
        entry_px = pos["entry_price"] if pos else None
        rich = {
            "profit": total_p,
            "reason": reason,
            "deals_settled": True,
            "exit_price": exit_px,
            "entry_price": entry_px,
            "exit_deal_tickets": [d["deal_ticket"] for d in deals],
            "exit_deals": deals,
        }
        if hasattr(executor, "_last_close_details") and isinstance(executor._last_close_details, dict):
            executor._last_close_details[t_int] = rich
        return {"profit": total_p, "reason": reason, "exit_deals": deals}

    executor.modify_position_sltp = _spy_modify_position_sltp
    executor.close_market_position = _spy_close_market_position
    if orig_partial is not None:
        executor.close_partial_volume = _spy_close_partial_volume
    executor.get_position_close_info = _spy_get_position_close_info


def _derive_max_stage(sig: Dict[str, Any], main_mod: Any) -> int:
    stage = 0
    if sig.get("tp1_reached"):
        stage = max(stage, 1)
    if sig.get("tp2_reached"):
        stage = max(stage, 2)
    if sig.get("tp3_reached"):
        stage = max(stage, 3)
    if sig.get("tp4_reached"):
        stage = max(stage, 4)
    if sig.get("tp5_reached"):
        stage = max(stage, 5)
    for ms in (sig.get("runner_cleared_milestones") or []):
        s_ms = str(ms).strip().upper()
        if s_ms.startswith("TP") and s_ms[2:].isdigit():
            stage = max(stage, int(s_ms[2:]))
    if hasattr(main_mod, "_get_signal_stage"):
        try:
            stage = max(stage, int(main_mod._get_signal_stage(sig)))
        except Exception:
            pass
    es = sig.get("exit_stage")
    if isinstance(es, int) and not isinstance(es, bool):
        stage = max(stage, es)
    return stage


def _build_signal_record(
    main_mod: Any,
    trade: Dict[str, Any],
    base_ticket: int,
    spy: BrokerSpy,
) -> Dict[str, Any]:
    is_buy = trade["dir"] == "BUY"
    raw_tp1 = float(trade["tp_levels"]["TP1"])
    final_tp = round(raw_tp1 - 0.50, 2) if is_buy else round(raw_tp1 + 0.50, 2)
    sl = float(trade["sl"])

    layers: Dict[str, Dict[str, Any]] = {}
    market_tickets: List[int] = []
    for idx, (lk, lot, fill_px) in enumerate(trade["layers"]):
        tkt = base_ticket + idx
        market_tickets.append(tkt)
        layers[lk] = {
            "lot": float(lot),
            "target_price": float(fill_px),
            "fill_price": float(fill_px),
            "entry_price": float(fill_px),
            "sl": sl,
            # The live path captures the stop the order was placed with as
            # `initial_sl` (mt5_executor); the R-relative profit floor reads
            # it to compute R. Without it the R lock is silently skipped and
            # Arm C would not exercise the feature at all.
            "initial_sl": sl,
            "tp": final_tp,
            "tp_stage": "TP1",
            "ticket": tkt,
            "order_type": "MARKET",
            "status": "OPEN",
        }
        main_mod.executor._dry_run_ticket_lots[tkt] = float(lot)
        spy.register_position(tkt, lk, float(lot), float(fill_px), sl=sl, tp=final_tp)

    tp_levels = dict(trade["tp_levels"])
    sig = main_mod.state_mgr.create_signal(
        message_id=trade["msg_id"],
        symbol=SYMBOL,
        direction=trade["dir"],
        layers=layers,
        zone=None,
        market_tickets=market_tickets,
        pending_tickets=[],
        zone_completed=True,
        sl=sl,
        raw_tp1=raw_tp1,
        raw_tp2=tp_levels.get("TP2"),
        raw_tp3=tp_levels.get("TP3"),
        raw_tp4=tp_levels.get("TP4"),
        raw_tp5=tp_levels.get("TP5"),
        tp_levels=tp_levels,
        final_tp=final_tp,
        max_orders=len(trade["layers"]),
        is_zone=len(trade["layers"]) > 1,
        recorded_entry_price=float(trade["layers"][0][2]),
        initial_price=float(trade["layers"][0][2]),
        chat_id=CHAT_ID,
    )
    sig["status"] = "ACTIVE"
    sig["broker_sl"] = sl
    sig["runner_max_tp_stage"] = 5
    sig["timestamp"] = trade["open_utc"]
    main_mod.state_mgr.update_signal(trade["msg_id"], sig, chat_id=CHAT_ID)
    return main_mod.state_mgr.get_signal(trade["msg_id"], chat_id=CHAT_ID)


async def _simulate_broker_stops(
    main_mod: Any,
    sig: Dict[str, Any],
    spy: BrokerSpy,
    tick: SimpleNamespace,
    check_sl: bool,
    check_tp: bool,
) -> Optional[str]:
    is_buy = spy.is_buy
    exec_px = float(tick.bid) if is_buy else float(tick.ask)
    layers = sig.get("layers") or {}
    closed_any: List[Tuple[int, str, float, str]] = []

    for tkt, pos in list(spy.positions.items()):
        if not pos["open"] or pos["lot"] <= 0.0:
            continue
        if check_sl and pos["sl"] is not None and pos["sl"] > 0.0:
            sl_px = float(pos["sl"])
            hit_sl = (exec_px <= sl_px + 1e-9) if is_buy else (exec_px >= sl_px - 1e-9)
            if hit_sl:
                in_profit = (sl_px > pos["entry_price"] + 1e-6) if is_buy else (sl_px < pos["entry_price"] - 1e-6)
                lv_obj = layers.get(pos["layer"]) if isinstance(layers, dict) else None
                if not in_profit:
                    reason = "BROKER_SL"
                elif isinstance(lv_obj, dict) and lv_obj.get("ratchet_tier"):
                    reason = "PROFIT_RATCHET_SL"
                elif sig.get("tp1_reached"):
                    src = str(sig.get("active_stop_source") or "").strip().upper()
                    reason = f"{src}_SL" if src else "RUNNER_TRAIL_SL"
                elif sig.get("stage0_profit_lock_active"):
                    reason = "STAGE0_PROFIT_LOCK_SL"
                else:
                    reason = "PROFIT_LOCK_SL"
                spy.record_close(tkt, pos["lot"], sl_px, reason="SL", is_partial=False)
                closed_any.append((tkt, pos["layer"], sl_px, reason))
                continue
        if check_tp and pos["tp"] is not None and pos["tp"] > 0.0:
            tp_px = float(pos["tp"])
            hit_tp = (exec_px >= tp_px - 1e-9) if is_buy else (exec_px <= tp_px + 1e-9)
            if hit_tp:
                spy.record_close(tkt, pos["lot"], tp_px, reason="TP", is_partial=False)
                closed_any.append((tkt, pos["layer"], tp_px, "BROKER_TP"))

    if not closed_any:
        return None

    closed_tkts = [c[0] for c in closed_any]
    closed_set = set(closed_tkts)
    for tkt, lk, exit_px, reason_code in closed_any:
        lv = layers.get(lk)
        if isinstance(lv, dict):
            lv["status"] = "CLOSED"
            lv["exit_price"] = round(exit_px, 4)
            lv["exit_reason_code"] = reason_code

    sig["market_tickets"] = [t for t in (sig.get("market_tickets") or []) if int(t) not in closed_set]
    if sig.get("runner_tickets"):
        sig["runner_tickets"] = [t for t in sig["runner_tickets"] if int(t) not in closed_set]
        sig["runner_ticket"] = sig["runner_tickets"][-1] if sig["runner_tickets"] else None

    await main_mod._account_closed_tickets(sig, closed_tkts)

    if not sig.get("market_tickets"):
        last_px = closed_any[-1][2]
        last_reason = closed_any[-1][3]
        sig["status"] = "CLOSED"
        sig["exit_price"] = round(last_px, 4)
        sig["exit_reason_code"] = last_reason
        sig["runner_close_reason"] = last_reason
        main_mod.state_mgr.update_signal(sig.get("msg_id") or closed_any[-1][0], sig, chat_id=CHAT_ID)
        return last_reason
    return None


def _pin_arm_b_params(config_mod: Any, executor_mod: Any, main_mod: Any) -> None:
    """Explicitly pin and verify calibrated v3.6.0 parameters across config, executor, and main."""
    for key, val in ARM_B_CALIBRATED_PARAMS.items():
        setattr(config_mod, key, val)
        if hasattr(executor_mod, key):
            setattr(executor_mod, key, val)
        if hasattr(main_mod, key):
            setattr(main_mod, key, val)
    if hasattr(config_mod, "validate_ratchet_cushion_pairs"):
        config_mod.validate_ratchet_cushion_pairs()
    if hasattr(main_mod, "_MAIN_CFG_SNAPSHOT") and isinstance(main_mod._MAIN_CFG_SNAPSHOT, dict):
        for k in list(main_mod._MAIN_CFG_SNAPSHOT.keys()):
            if k in ARM_B_CALIBRATED_PARAMS:
                main_mod._MAIN_CFG_SNAPSHOT[k] = ARM_B_CALIBRATED_PARAMS[k]

    for key, expected in ARM_B_CALIBRATED_PARAMS.items():
        actual = getattr(config_mod, key)
        if isinstance(expected, float):
            if abs(float(actual) - expected) > 1e-9:
                raise RuntimeError(f"Arm B calibration mismatch for {key}: {actual} != {expected}")
        elif actual != expected:
            raise RuntimeError(f"Arm B calibration mismatch for {key}: {actual!r} != {expected!r}")


def _pin_arm_c_params(config_mod: Any, executor_mod: Any, main_mod: Any) -> None:
    """Explicitly pin and verify the Arm C lifecycle parameters across config, executor, and main."""
    targets = (config_mod, executor_mod, main_mod)
    for key, expected in ARM_C_CALIBRATED_PARAMS.items():
        for mod in targets:
            if hasattr(mod, key):
                setattr(mod, key, expected)
        actual = getattr(config_mod, key, None)
        if isinstance(expected, float):
            if abs(float(actual or 0.0) - expected) > 1e-9:
                raise RuntimeError(f"Arm C calibration mismatch for {key}: {actual} != {expected}")
        elif actual != expected:
            raise RuntimeError(f"Arm C calibration mismatch for {key}: {actual!r} != {expected!r}")


class _ReplayClock:
    """The simulated UTC clock of a replay worker.

    The trade-age guard reads the wall clock (datetime.now(timezone.utc)),
    but the replay simulates the RECORDED Nov 17-19 week. Without a simulated
    clock every recorded trade would look days old and the guard would flatten
    it on its very first tick, which is not a replay of the recorded week at
    all. The clock therefore follows the M1 bars, and Arm A keeps the wall
    clock because it is the frozen historical baseline.
    """

    def __init__(self) -> None:
        self._now: Optional[datetime] = None

    def set_epoch(self, epoch_sec: int) -> None:
        """Move the simulated clock to the given UTC epoch second."""
        self._now = datetime.fromtimestamp(int(epoch_sec), tz=timezone.utc)

    def now(self, tz: Optional[Any] = None) -> datetime:
        """Return the simulated time (the wall clock when no bar was seen yet)."""
        base = self._now or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        if tz is None:
            return base.replace(tzinfo=None)
        return base.astimezone(tz)


_REPLAY_CLOCK = _ReplayClock()


def _install_replay_clock(main_mod: Any) -> None:
    """Make main read the simulated replay clock instead of the wall clock."""
    real_datetime = datetime

    class _SimulatedDatetime(real_datetime):
        @classmethod
        def now(cls, tz: Optional[Any] = None) -> datetime:
            return _REPLAY_CLOCK.now(tz)

    main_mod.datetime = _SimulatedDatetime


async def _run_single_trade_worker(
    main_mod: Any,
    state_cls: Any,
    ledger_cls: Any,
    exec_cls: Any,
    runtime_dir: Path,
    trade: Dict[str, Any],
    bars: List[Dict[str, Any]],
    trade_idx: int,
) -> Dict[str, Any]:
    msg_id = trade["msg_id"]
    is_buy = trade["dir"] == "BUY"
    total_lot = round(sum(l[1] for l in trade["layers"]), 4)
    avg_entry = round(sum(l[1] * l[2] for l in trade["layers"]) / total_lot, 4)

    # Fresh isolated runtime state per trade
    state_path = runtime_dir / f"state_{msg_id}.json"
    ledger_path = runtime_dir / f"ledger_{msg_id}.jsonl"
    if state_path.exists():
        state_path.unlink()
    if ledger_path.exists():
        ledger_path.unlink()

    main_mod.state_mgr = state_cls(state_path)
    main_mod.trade_ledger = ledger_cls(ledger_path)
    main_mod.executor = exec_cls(dry_run=True)
    main_mod.executor.connect()
    if hasattr(main_mod, "_runner_save_throttle_ts"):
        main_mod._runner_save_throttle_ts.clear()

    spy = BrokerSpy(main_mod.executor, is_buy=is_buy)
    _install_broker_spy(main_mod.executor, spy)

    idx0 = _find_entry_bar_index(bars, trade["open_utc"])
    pot_mfe = _compute_potential_mfe_before_sl(trade, bars, idx0)

    # The simulated clock starts at the entry bar and follows the M1 bars,
    # so the trade-age guard measures the age of the RECORDED trade.
    _REPLAY_CLOCK.set_epoch(int(bars[idx0]["time_utc"]))

    sig = _build_signal_record(main_mod, trade, base_ticket=100000 + trade_idx * 100, spy=spy)
    sig["msg_id"] = msg_id

    peak_mfe = 0.0
    max_stage = 0
    exit_reason: Optional[str] = None
    exit_utc: Optional[str] = None
    error_msg: Optional[str] = None

    try:
        for b_idx in range(idx0 + 1, len(bars)):
            bar = bars[b_idx]
            prev_bar = bars[b_idx - 1]
            bar_utc = _utc_iso(bar["time_utc"])
            _REPLAY_CLOCK.set_epoch(int(bar["time_utc"]))
            main_mod.executor._dry_run_m1_atr = float(bar.get("atr14") or 0.0)
            main_mod.executor._dry_run_last_m1_candle = {
                "open": float(prev_bar["open"]),
                "high": float(prev_bar["high"]),
                "low": float(prev_bar["low"]),
                "close": float(prev_bar["close"]),
                "time": float(prev_bar["time_utc"]),
            }

            for bid, ask in _bar_tick_sequence(bar):
                tick = SimpleNamespace(bid=bid, ask=ask, time=bar["time_utc"])
                spy.current_tick = tick
                spy.current_utc = bar_utc
                main_mod.executor.get_tick = lambda _sym, _t=tick: _t

                exec_px = bid if is_buy else ask
                cur_fav = (exec_px - avg_entry) if is_buy else (avg_entry - exec_px)
                if cur_fav > peak_mfe:
                    peak_mfe = cur_fav

                # 1) Check broker SL before FSM tick
                sl_closed_reason = await _simulate_broker_stops(
                    main_mod, sig, spy, tick, check_sl=True, check_tp=False
                )
                if sl_closed_reason is not None:
                    exit_reason = sl_closed_reason
                    exit_utc = bar_utc
                    break

                # 2) Drive real main.py FSM tick handler
                if sig.get("status") == "RUNNER_ACTIVE" or (sig.get("tp1_reached") and sig.get("runner_ticket")):
                    await main_mod._process_runner_active_tick(msg_id, sig)
                else:
                    await main_mod._process_pre_tp1_or_backstop_tick(msg_id, sig)

                max_stage = max(max_stage, _derive_max_stage(sig, main_mod))

                # 3) Check if FSM closed all positions
                if sig.get("status") == "CLOSED" or not sig.get("market_tickets"):
                    exit_reason = str(
                        sig.get("exit_reason_code")
                        or sig.get("runner_close_reason")
                        or "FSM_CLOSED"
                    )
                    exit_utc = bar_utc
                    break

                # 4) Check broker TP after FSM tick (allows TP1->TP2 handoff or lift on crossing tick)
                tp_closed_reason = await _simulate_broker_stops(
                    main_mod, sig, spy, tick, check_sl=False, check_tp=True
                )
                if tp_closed_reason is not None:
                    exit_reason = tp_closed_reason
                    exit_utc = bar_utc
                    break

            if exit_reason is not None:
                break

        # Mark-to-market fallback if data ends while still open
        if exit_reason is None and sig.get("market_tickets"):
            last_bar = bars[-1]
            last_utc = _utc_iso(last_bar["time_utc"])
            last_tick = SimpleNamespace(
                bid=float(last_bar["close"]),
                ask=round(float(last_bar["close"]) + float(last_bar["spread_dollars"]), 2),
                time=last_bar["time_utc"],
            )
            spy.current_tick = last_tick
            spy.current_utc = last_utc
            exit_px = float(last_tick.bid) if is_buy else float(last_tick.ask)
            for tkt, pos in list(spy.positions.items()):
                if pos["open"] and pos["lot"] > 0.0:
                    spy.record_close(tkt, pos["lot"], exit_px, reason="EOD_MTM", is_partial=False)
            sig["market_tickets"] = []
            sig["status"] = "CLOSED"
            exit_reason = "EOD_MTM"
            exit_utc = last_utc
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

    net_gbp = spy.total_gbp()
    pts_lot = spy.total_pts_lot()
    realized_pts_per_oz = round(pts_lot / total_lot, 2) if total_lot > 0.0 else 0.0
    mfe_capture_pct = round((realized_pts_per_oz / pot_mfe) * 100.0, 1) if pot_mfe > 0.0 else None
    final_exit_px = spy.last_exit_price()
    final_exit_utc = exit_utc or spy.last_exit_utc()
    rollover_flag = _is_rollover_utc(final_exit_utc)

    return {
        "msg_id": msg_id,
        "dir": trade["dir"],
        "open_utc": trade["open_utc"],
        "actual_close_utc": trade["close_utc"],
        "total_lot": total_lot,
        "avg_entry": avg_entry,
        "sl": trade["sl"],
        "tp1": trade["tp_levels"]["TP1"],
        "actual_mt5_gbp": trade["actual_mt5_gbp"],
        "actual_exit_reason": trade["actual_exit_reason"],
        "sim_gbp": net_gbp,
        "sim_pts_lot": pts_lot,
        "realized_pts_per_oz": realized_pts_per_oz,
        "peak_mfe_dollars": round(peak_mfe, 2),
        "potential_mfe_before_sl": pot_mfe,
        "mfe_capture_pct": mfe_capture_pct,
        "max_stage": max_stage,
        "exit_price": final_exit_px,
        "exit_reason": exit_reason or "UNKNOWN",
        "exit_utc": final_exit_utc,
        "rollover_flag": rollover_flag,
        "banked_profit_tracked": round(float(sig.get("banked_profit") or 0.0), 2),
        "ratchet_tier_final": (
            (sig.get("layers") or {}).get("L1", {}).get("ratchet_tier")
            or next(
                (
                    lv.get("ratchet_tier")
                    for lv in (sig.get("layers") or {}).values()
                    if isinstance(lv, dict) and lv.get("ratchet_tier")
                ),
                None,
            )
        ),
        "error": error_msg,
    }


def _run_worker_main(arm_mode: str, bars_path: Path, out_path: Path) -> int:
    runtime_tmp = tempfile.TemporaryDirectory(prefix=f"ab_replay_{arm_mode}_")
    try:
        rdir = Path(runtime_tmp.name)
        os.environ["DRY_RUN"] = "true"
        os.environ["STAGED_EXIT_ENABLED"] = "true"
        os.environ["NOTIFY_SAVED_MESSAGES"] = "false"
        os.environ["STATE_FILE_PATH"] = str(rdir / "signals_state.json")
        os.environ["LOG_FILE_PATH"] = str(rdir / "copier.log")
        os.environ["TRADES_LEDGER_PATH"] = str(rdir / "trades_ledger.jsonl")
        os.environ["LOCK_FILE_PATH"] = str(rdir / "copier.lock")

        if arm_mode in ("b", "c"):
            for k, v in ARM_B_CALIBRATED_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        if arm_mode == "c":
            for k, v in ARM_C_CALIBRATED_PARAMS.items():
                os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)

        import config  # noqa: E402
        import main  # noqa: E402
        import mt5_executor  # noqa: E402
        from state_manager import StateManager  # noqa: E402
        from trade_ledger import TradeLedger  # noqa: E402

        assert config.DRY_RUN is True, "DRY_RUN must be True in replay worker"
        assert Path(config.STATE_FILE_PATH).parent == rdir, "STATE_FILE_PATH isolation failed"

        if arm_mode == "a":
            if hasattr(config, "PROFIT_RATCHET_ENABLED"):
                raise RuntimeError("Arm A contamination: PROFIT_RATCHET_ENABLED found in 4544f7f worktree!")
        elif arm_mode == "b":
            _pin_arm_b_params(config, mt5_executor, main)
        else:
            _pin_arm_b_params(config, mt5_executor, main)
            _pin_arm_c_params(config, mt5_executor, main)

        # Arms B and C run on the simulated replay clock: the trade-age guard
        # reads the wall clock, and the replay simulates the RECORDED week.
        if arm_mode in ("b", "c"):
            _install_replay_clock(main)

        async def _no_notify(*_args: Any, **_kwargs: Any) -> None:
            return None

        main.notify_saved_messages = _no_notify

        bars_payload = json.loads(bars_path.read_text(encoding="utf-8"))
        bars = bars_payload["bars"] if isinstance(bars_payload, dict) else bars_payload

        async def _run_all() -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for idx, trade in enumerate(TRADE_CATALOG):
                res = await _run_single_trade_worker(
                    main,
                    StateManager,
                    TradeLedger,
                    mt5_executor.MT5Executor,
                    rdir,
                    trade,
                    bars,
                    idx,
                )
                out.append(res)
            return out

        results = asyncio.run(_run_all())
        agg = _summarize_arm(results, arm_mode)
        payload = {
            "arm": arm_mode,
            "code_root": str(Path(main.__file__).resolve().parent),
            "dry_run": bool(config.DRY_RUN),
            "results": results,
            "summary": agg,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0
    finally:
        _shutdown_copier_loggers()
        try:
            runtime_tmp.cleanup()
        except Exception:
            pass


def _summarize_arm(results: List[Dict[str, Any]], arm_mode: str) -> Dict[str, Any]:
    net_gbp = round(sum(float(r["sim_gbp"]) for r in results), 2)
    net_pts_lot = round(sum(float(r["sim_pts_lot"]) for r in results), 6)
    tail_ids = {"13078", "9953"}
    tail_gbp = round(sum(float(r["sim_gbp"]) for r in results if r["msg_id"] in tail_ids), 2)
    trio_ids = {"13153", "9934", "13167"}
    trio_gbp = round(sum(float(r["sim_gbp"]) for r in results if r["msg_id"] in trio_ids), 2)
    trio_pts_lot = round(sum(float(r["sim_pts_lot"]) for r in results if r["msg_id"] in trio_ids), 6)
    r_13153 = next((r for r in results if r["msg_id"] == "13153"), {})
    r_9934 = next((r for r in results if r["msg_id"] == "9934"), {})
    rollover_trades = [r["msg_id"] for r in results if r.get("rollover_flag")]
    unflagged_rollover = [
        r["msg_id"] for r in results if _is_rollover_utc(r.get("exit_utc")) and not r.get("rollover_flag")
    ]
    errors = [{"msg_id": r["msg_id"], "error": r["error"]} for r in results if r.get("error")]
    avg_mfe_cap_vals = [float(r["mfe_capture_pct"]) for r in results if r.get("mfe_capture_pct") is not None and float(r["sim_gbp"]) > 0]
    avg_win_mfe_cap = round(sum(avg_mfe_cap_vals) / len(avg_mfe_cap_vals), 1) if avg_mfe_cap_vals else 0.0

    return {
        "arm": arm_mode,
        "trade_count": len(results),
        "net_gbp": net_gbp,
        "net_pts_lot": net_pts_lot,
        "actual_live_gbp": round(sum(float(r["actual_mt5_gbp"]) for r in results), 2),
        "tail_loss_gbp_13078_9953": tail_gbp,
        "choke_trio_gbp_13153_9934_13167": trio_gbp,
        "choke_trio_pts_lot": trio_pts_lot,
        "trade_13153_max_stage": int(r_13153.get("max_stage") or 0),
        "trade_9934_banked_tracked": float(r_9934.get("banked_profit_tracked") or 0.0),
        "avg_winner_mfe_capture_pct": avg_win_mfe_cap,
        "rollover_exits": rollover_trades,
        "unflagged_rollover_exits": unflagged_rollover,
        "error_count": len(errors),
        "errors": errors,
    }


def _run_arm_subprocess(arm_mode: str, code_root: Path, bars_path: Path, out_path: Path) -> Dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(code_root)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--arm-mode",
        arm_mode,
        "--worker-bars",
        str(bars_path),
        "--worker-out",
        str(out_path),
    ]
    proc = subprocess.run(cmd, cwd=str(code_root), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Arm {arm_mode.upper()} worker failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(out_path.read_text(encoding="utf-8"))


def _run_arm_a_isolated(bars_path: Path, out_path: Path) -> Dict[str, Any]:
    wt_parent = Path(tempfile.mkdtemp(prefix="ab_replay_wt_4544f7f_"))
    wt_dir = wt_parent / "wt"
    try:
        add_res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "add", "--detach", str(wt_dir), BASELINE_COMMIT],
            capture_output=True,
            text=True,
        )
        if add_res.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {add_res.stderr.strip()}")
        return _run_arm_subprocess("a", wt_dir, bars_path, out_path)
    finally:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(wt_dir)],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "prune"],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(wt_parent, ignore_errors=True)


def _evaluate_gates(
    arm_a: Optional[Dict[str, Any]],
    arm_b: Optional[Dict[str, Any]],
    arm_c: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Arm C is compared against ARM B (not against Arm A): the R lock and the
    # trade-age guard are an increment on Arm B, and the gate is absent when
    # Arm C was not run.
    if arm_c and arm_b:
        sc = arm_c["summary"]
        sb2 = arm_b["summary"]
        g5 = (
            sc["net_gbp"] >= sb2["net_gbp"]
            and sc["tail_loss_gbp_13078_9953"] <= sb2["tail_loss_gbp_13078_9953"]
            and sc["error_count"] == 0
        )
    else:
        g5 = None

    if arm_a and arm_b:
        sa = arm_a["summary"]
        sb = arm_b["summary"]
        g1 = sb["net_gbp"] > sa["net_gbp"]
        g2 = sb["tail_loss_gbp_13078_9953"] > sa["tail_loss_gbp_13078_9953"]
        g3 = (
            sb["choke_trio_gbp_13153_9934_13167"] > 2.45 * 2.5
            and sb["choke_trio_pts_lot"] >= 0.090
            and sb["trade_13153_max_stage"] >= 2
        )
        g4 = (
            sa["error_count"] == 0
            and sb["error_count"] == 0
            and sb["trade_9934_banked_tracked"] > 0.0
            and len(sa["unflagged_rollover_exits"]) == 0
            and len(sb["unflagged_rollover_exits"]) == 0
        )
        return {
            "mode": "both",
            "gate_1_net_profit": g1,
            "gate_2_tail_protection": g2,
            "gate_3_mfe_capture_and_13153_stage2": g3,
            "gate_4_zero_regression": g4,
            "g5": g5,
            "all_gates_passed": bool(g1 and g2 and g3 and g4 and g5 is not False),
        }

    single = arm_a or arm_b or arm_c
    assert single is not None
    ss = single["summary"]
    g4_single = ss["error_count"] == 0 and len(ss["unflagged_rollover_exits"]) == 0
    return {
        "mode": f"single_{single['arm']}",
        "gate_4_zero_regression": g4_single,
        "g5": g5,
        "all_gates_passed": bool(g4_single and g5 is not False),
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    bars_meta = report["bars_meta"]
    arm_a = report.get("arm_a")
    arm_b = report.get("arm_b")
    arm_c = report.get("arm_c")
    gates = report["gates"]

    lines: List[str] = [
        "# 12-Trade XAUUSD.s M1-Bar A/B Replay Proof",
        "",
        f"- **Generated UTC:** `{report['generated_utc']}`",
        f"- **Bars Loaded:** `{bars_meta['count']}` M1 bars (`{bars_meta['first_utc']}` -> `{bars_meta['last_utc']}`, spread_override=`{bars_meta['spread_override']}`)",
        f"- **GBP Conversion Rate:** `£{GBP_PER_PT_001_LOT:.4f}` per `($1.00/oz × 0.01 lot)`",
        f"- **Rollover Window Flagged:** `21:55–23:05 UTC`",
        "",
    ]

    if arm_a and arm_b:
        sa = arm_a["summary"]
        sb = arm_b["summary"]
        by_id_a = {r["msg_id"]: r for r in arm_a["results"]}
        by_id_b = {r["msg_id"]: r for r in arm_b["results"]}
        lines += [
            "## Aggregate A/B Comparison",
            "",
            "| Metric | Actual Live MT5 | Arm A (`4544f7f`) | Arm B (`v3.6.0` Calibrated) | Delta (B - A) |",
            "|---|---:|---:|---:|---:|",
            f"| **Net PnL (GBP)** | `£{sa['actual_live_gbp']:+.2f}` | `£{sa['net_gbp']:+.2f}` | `£{sb['net_gbp']:+.2f}` | `£{sb['net_gbp'] - sa['net_gbp']:+.2f}` |",
            f"| **Tail Loss (`13078+9953`)** | `-£48.60` | `£{sa['tail_loss_gbp_13078_9953']:+.2f}` | `£{sb['tail_loss_gbp_13078_9953']:+.2f}` | `£{sb['tail_loss_gbp_13078_9953'] - sa['tail_loss_gbp_13078_9953']:+.2f}` |",
            f"| **Choke Trio (`13153+9934+13167`)** | `+£2.45` | `£{sa['choke_trio_gbp_13153_9934_13167']:+.2f}` | `£{sb['choke_trio_gbp_13153_9934_13167']:+.2f}` | `£{sb['choke_trio_gbp_13153_9934_13167'] - sa['choke_trio_gbp_13153_9934_13167']:+.2f}` |",
            f"| **`13153` Max Stage** | `0` | `{sa['trade_13153_max_stage']}` | `{sb['trade_13153_max_stage']}` | `+{sb['trade_13153_max_stage'] - sa['trade_13153_max_stage']}` |",
            f"| **Rollover Exits (`21:55-23:05Z`)** | `0` | `{len(sa['rollover_exits'])}` | `{len(sb['rollover_exits'])}` | `0` |",
            "",
            "## Per-Trade A/B Table",
            "",
            "| Msg ID | Dir | Entry | Pot. MFE | Live (£) | Arm A (£) | Arm A Exit (Stage) | Arm B (£) | Arm B Exit (Stage) | Arm B MFE Cap % | Rollover |",
            "|---|---|---:|---:|---:|---:|---|---:|---|---:|---|",
        ]
        for t in TRADE_CATALOG:
            mid = t["msg_id"]
            ra = by_id_a[mid]
            rb = by_id_b[mid]
            ro = "ROLLOVER" if (ra["rollover_flag"] or rb["rollover_flag"]) else "OK"
            cap_b = f"{rb['mfe_capture_pct']:.1f}%" if rb["mfe_capture_pct"] is not None else "N/A"
            lines.append(
                f"| `{mid}` | {ra['dir']} | `{ra['avg_entry']:.2f}` | `${ra['potential_mfe_before_sl']:.2f}` | "
                f"`£{ra['actual_mt5_gbp']:+.2f}` | `£{ra['sim_gbp']:+.2f}` | `{ra['exit_reason']}` (S{ra['max_stage']}) | "
                f"`£{rb['sim_gbp']:+.2f}` | `{rb['exit_reason']}` (S{rb['max_stage']}) | `{cap_b}` | `{ro}` |"
            )
    else:
        single = arm_a or arm_b or arm_c
        assert single is not None
        ss = single["summary"]
        label = {
            "a": "Arm A (`4544f7f`)",
            "b": "Arm B (`v3.6.0` Calibrated)",
            "c": "Arm C (v3.8.0 Trade Lifecycle Guard)",
        }.get(single["arm"], "Arm " + str(single["arm"]))
        lines += [
            f"## {label} Summary",
            "",
            f"- **Net Simulated PnL:** `£{ss['net_gbp']:+.2f}` (pts*lot=`{ss['net_pts_lot']:+.4f}`) vs Actual Live `£{ss['actual_live_gbp']:+.2f}`",
            f"- **Tail Loss (`13078+9953`):** `£{ss['tail_loss_gbp_13078_9953']:+.2f}`",
            f"- **Choke Trio (`13153+9934+13167`):** `£{ss['choke_trio_gbp_13153_9934_13167']:+.2f}` (pts*lot=`{ss['choke_trio_pts_lot']:+.4f}`)",
            f"- **`13153` Max Stage:** `{ss['trade_13153_max_stage']}`",
            f"- **Rollover Exits (`21:55-23:05Z`):** `{ss['rollover_exits'] or 'None'}` | Errors: `{ss['error_count']}`",
            "",
            "| Msg ID | Dir | Lot | Avg Entry | Peak MFE | Pot. MFE | Live (£) | Sim (£) | Exit Price | Exit Reason | Stage | MFE Cap % | Rollover |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|",
        ]
        for r in single["results"]:
            ro = "ROLLOVER" if r["rollover_flag"] else "OK"
            cap = f"{r['mfe_capture_pct']:.1f}%" if r["mfe_capture_pct"] is not None else "N/A"
            epx = f"{r['exit_price']:.2f}" if r["exit_price"] is not None else "N/A"
            lines.append(
                f"| `{r['msg_id']}` | {r['dir']} | `{r['total_lot']:.2f}` | `{r['avg_entry']:.2f}` | "
                f"`${r['peak_mfe_dollars']:.2f}` | `${r['potential_mfe_before_sl']:.2f}` | "
                f"`£{r['actual_mt5_gbp']:+.2f}` | `£{r['sim_gbp']:+.2f}` | `{epx}` | "
                f"`{r['exit_reason']}` | `S{r['max_stage']}` | `{cap}` | `{ro}` |"
            )

    if arm_c:
        sc = arm_c["summary"]
        sb_c = arm_b["summary"] if arm_b else None
        net_b = f"`£{sb_c['net_gbp']}`" if sb_c else "`n/a`"
        tail_b = f"`£{sb_c['tail_loss_gbp_13078_9953']}`" if sb_c else "`n/a`"
        lines += [
            "",
            "### Arm C (v3.8.0 Trade Lifecycle Guard)",
            "",
            f"- **Net PnL:** `£{sc['net_gbp']}` (Arm B: {net_b})",
            f"- **Tail Loss (13078 + 9953):** `£{sc['tail_loss_gbp_13078_9953']}` "
            f"(Arm B: {tail_b})",
            f"- **Trade Count:** `{sc['trade_count']}` | Errors: `{sc['error_count']}`",
        ]

    lines += ["", "## Gates Evaluation", ""]
    for k, v in gates.items():
        lines.append(f"- **{k}:** `{v}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="12-Trade XAUUSD.s M1-Bar A/B Replay Proof")
    parser.add_argument("--arm", choices=("a", "b", "c", "both"), default="both")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    parser.add_argument("--save-bars", type=Path, default=None)
    parser.add_argument("--bars-json", type=Path, default=None)
    parser.add_argument("--spread-override", type=float, default=None)
    # Internal worker args
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--arm-mode", choices=("a", "b", "c"), default="a", help=argparse.SUPPRESS)
    parser.add_argument("--worker-bars", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        assert args.worker_bars is not None and args.worker_out is not None
        return _run_worker_main(args.arm_mode, args.worker_bars, args.worker_out)

    bars = fetch_m1_bars(
        bars_json_path=args.bars_json,
        save_bars_path=args.save_bars,
        spread_override=args.spread_override,
    )

    with tempfile.TemporaryDirectory(prefix="ab_replay_io_") as io_tmp:
        io_dir = Path(io_tmp)
        bars_file = io_dir / "bars.json"
        bars_file.write_text(json.dumps({"bars": bars}), encoding="utf-8")

        arm_a_res: Optional[Dict[str, Any]] = None
        arm_b_res: Optional[Dict[str, Any]] = None
        arm_c_res: Optional[Dict[str, Any]] = None

        if args.arm in ("a", "both"):
            arm_a_res = _run_arm_a_isolated(bars_file, io_dir / "arm_a.json")
        if args.arm in ("b", "both"):
            arm_b_res = _run_arm_subprocess("b", REPO_ROOT, bars_file, io_dir / "arm_b.json")
        if args.arm in ("c", "both"):
            arm_c_res = _run_arm_subprocess("c", REPO_ROOT, bars_file, io_dir / "arm_c.json")

    gates = _evaluate_gates(arm_a_res, arm_b_res, arm_c_res)
    report: Dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": SYMBOL,
        "gbp_rate_per_pt_001_lot": GBP_PER_PT_001_LOT,
        "bars_meta": {
            "count": len(bars),
            "first_utc": _utc_iso(bars[0]["time_utc"]) if bars else None,
            "last_utc": _utc_iso(bars[-1]["time_utc"]) if bars else None,
            "spread_override": args.spread_override,
        },
        "arm_a": arm_a_res,
        "arm_b": arm_b_res,
        "arm_c": arm_c_res,
        "gates": gates,
    }
    md_text = _render_markdown(report)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.md_out is not None:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(md_text, encoding="utf-8")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(md_text)
    return 0 if gates.get("all_gates_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
