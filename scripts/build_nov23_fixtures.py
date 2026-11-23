#!/usr/bin/env python3
"""Build NEW replay fixtures for the T3 vs T3-tight validation (never the canonical tape).

Two modes:
  nov23      Build tests/fixtures/tape_2026-11-23.json + signals_2026-11-23.json from
             .superpowers/sdd/2026-11-20-trade-lifecycle-guard/nov23_raw.json (181 M1 bars),
             trades_ledger.jsonl (exact live fills/exits) and signals_state.json (the
             ATR-scaled SL/TP the production engine actually saw).
  scenarios  Build three deterministic synthetic M1 scenario tapes + signal sets:
             high-ATR wide stop with $2-3 retracements, normal-ATR retracements, and a
             sharp reversal after ~$5 MFE (the Nov-23 msg 13305 failure shape).

Conventions (documented in each fixture):
  * Nov-23 catalog layers = ONLY the layers the live ledger reports as filled, at their
    exact MT5 fill prices, all initialized at open_utc (same convention as the canonical
    Nov 17-19 catalog). Sub-minute fill sequencing is not replayed.
  * trade sl = the common initial_sl of the filled layers in signals_state.json when
    unanimous, else the signal-level dynamic_risk.sl (the ATR-scaled stop live used).
  * tp_levels = signals_state.json dynamic_risk.tp_levels (ATR-scaled, as live).
  * Bars are normalized exactly like ab_replay_proof.fetch_m1_bars (UTC = server-7200,
    spread_dollars = max(0.05, spread_pts*0.01)) and carry the closed-bar ATR(14).

Read-only with respect to live state: this script only READS the ledger/state files.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
SDD_DIR = REPO_ROOT / ".superpowers" / "sdd" / "2026-11-20-trade-lifecycle-guard"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
MT5_SERVER_OFFSET_SEC = 7200

# The 9 bot signals named in the replay-validation brief (msg ids are Telegram ids).
NOV23_MSG_IDS = [10283, 10289, 13305, 10304, 10306, 10310, 10321, 13312, 10328]
SIGNALS_SCHEMA = "mt5-copier-replay-signals/1"


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ab_replay_proof_for_build", REPO_ROOT / "scripts" / "ab_replay_proof.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _utc_z(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_raw_bars(raw_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    for b in raw_bars:
        sp_pts = int(b["spread"])
        bars.append(
            {
                "time_utc": int(b["time"]) - MT5_SERVER_OFFSET_SEC,
                "open": round(float(b["open"]), 2),
                "high": round(float(b["high"]), 2),
                "low": round(float(b["low"]), 2),
                "close": round(float(b["close"]), 2),
                "spread_pts": sp_pts,
                "spread_dollars": round(max(0.05, sp_pts * 0.01), 4),
            }
        )
    bars.sort(key=lambda x: x["time_utc"])
    return bars


def build_nov23() -> int:
    h = _load_harness()
    raw = json.loads((SDD_DIR / "nov23_raw.json").read_text(encoding="utf-8"))
    bars = _normalize_raw_bars(raw["bars"])
    h._compute_rolling_atr(bars, period=h.ATR_PERIOD)
    assert len(bars) == 181, f"expected 181 raw bars, got {len(bars)}"
    assert h._utc_iso(bars[0]["time_utc"]) == "2026-11-22T23:00:00Z"
    assert h._utc_iso(bars[-1]["time_utc"]) == "2026-11-23T02:00:00Z"

    # A read-only copy_rates_range verification fetch (2026-11-23) returned exactly these
    # 181 bars: the broker holds no earlier M1 history (weekend close precedes the window).
    warmup_path = SDD_DIR / "nov23_bars_warmup.json"
    if warmup_path.exists():
        warm = json.loads(warmup_path.read_text(encoding="utf-8"))["bars"]
        assert _normalize_raw_bars(warm) == [
            {k: b[k] for k in ("time_utc", "open", "high", "low", "close", "spread_pts", "spread_dollars")}
            for b in bars
        ], "verification fetch diverges from nov23_raw bars"

    ledger_rows = [
        json.loads(line)
        for line in (REPO_ROOT / "trades_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    state = json.loads((REPO_ROOT / "signals_state.json").read_text(encoding="utf-8"))
    selected = [
        r for r in ledger_rows
        if r.get("message_id") in NOV23_MSG_IDS and str(r.get("opened_at", "")) >= "2026-11-22T22:00"
    ]
    assert len(selected) == 9, f"expected 9 overnight ledger rows, got {len(selected)}"
    by_msg = {int(r["message_id"]): r for r in selected}
    assert set(by_msg) == set(NOV23_MSG_IDS)

    trades: List[Dict[str, Any]] = []
    provenance: Dict[str, Any] = {}
    for mid in NOV23_MSG_IDS:
        led = by_msg[mid]
        sig = state[str(mid)]
        assert sig["direction"] == led["direction"], f"direction mismatch for {mid}"
        dyn = sig.get("dynamic_risk") or {}
        raw_tps = {f"TP{i}": sig.get(f"raw_tp{i}") for i in range(1, 6) if sig.get(f"raw_tp{i}")}
        tp_levels_src = {k: float(v) for k, v in (dyn.get("tp_levels") or {}).items()}
        tp_source = "dynamic_risk.tp_levels (ATR-scaled)"
        if not tp_levels_src:
            # Bare market-now signals (e.g. 10321) keep an empty scaled map; the engine
            # milestones against the provider-edit raw TPs (final_tp = raw_tp1 +/- front-run).
            assert raw_tps.get("TP1"), f"no scaled or raw TP levels for {mid}"
            tp_levels_src = {k: float(v) for k, v in raw_tps.items()}
            tp_source = "signal raw_tp1..5 (provider edit; scaled map empty)"
        assert dyn.get("applied") or tp_levels_src, f"missing dynamic_risk/TP levels for {mid}"
        filled = list(led["layers_filled"])
        layer_exits = {le["layer"]: le for le in led["layer_exits"]}
        layers: List[List[Any]] = []
        init_sls = set()
        for lk in sorted(filled):
            le = layer_exits[lk]
            st_lv = sig["layers"][lk]
            assert abs(float(st_lv["entry_price"]) - float(le["entry_price"])) < 1e-6, f"fill mismatch {mid}/{lk}"
            layers.append([lk, float(le["lot"]), float(le["entry_price"])])
            if st_lv.get("initial_sl"):
                init_sls.add(round(float(st_lv["initial_sl"]), 4))
        sl = float(next(iter(init_sls))) if len(init_sls) == 1 else float(dyn["sl"])
        sl_source = "filled-layer initial_sl (unanimous)" if len(init_sls) == 1 else "dynamic_risk.sl (signal-level)"
        open_utc = _utc_z(led["opened_at"])
        close_utc = _utc_z(led["closed_at"])
        # Window sufficiency: entry bar exists and the live close is inside the tape.
        idx0 = h._find_entry_bar_index(bars, open_utc)
        assert int(datetime.fromisoformat(led["closed_at"]).timestamp()) <= bars[-1]["time_utc"] + 60, (
            f"trade {mid} closes after tape end"
        )
        trades.append(
            {
                "msg_id": str(mid),
                "dir": led["direction"],
                "open_utc": open_utc,
                "close_utc": close_utc,
                "layers": layers,
                "sl": sl,
                "tp_levels": tp_levels_src,
                "actual_mt5_gbp": float(led["realized_pnl"]),
                "actual_exit_reason": f"{led['exit_reason_code']} (live: {led.get('exit_reason_detail') or ''})",
            }
        )
        provenance[str(mid)] = {
            "chat_id": led["chat_id"],
            "entry_type": led["entry_type"],
            "tickets": led["tickets"],
            "filled_lots": led["filled_lots"],
            "layers_filled": filled,
            "sl_used": sl,
            "sl_source": sl_source,
            "tp_source": tp_source,
            "scaled_atr_at_entry": dyn.get("atr"),
            "scaled_vol_ratio": dyn.get("vol_ratio"),
            "ledger_initial_r_per_oz": led.get("initial_r_per_oz"),
            "ledger_exit_price": led.get("exit_price"),
            "ledger_exit_stage": led.get("exit_stage"),
            "ledger_peak_profit_per_oz": (led.get("max_favorable_excursion") or {}).get("peak_profit_per_oz"),
            "entry_bar_index": idx0,
        }

    fetched_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tape_path = FIXTURE_DIR / "tape_2026-11-23.json"
    assert tape_path.resolve() != h.PINNED_TAPE_PATH.resolve()
    meta = h._write_tape_fixture(
        tape_path,
        bars,
        fetched_utc=fetched_utc,
        source=(
            "nov23_raw.json (181 M1 bars, MT5 server UTC+2, captured 2026-11-23 overnight); "
            "read-only copy_rates_range verification returned the identical window (no earlier "
            "bars exist: weekend close). Normalized + ATR(14) recomputed by build_nov23_fixtures.py."
        ),
        canonical=False,
    )
    payload = json.loads(tape_path.read_text(encoding="utf-8"))
    payload["signals_fixture"] = "signals_2026-11-23.json"
    payload["notes"] = (
        "Non-canonical research tape for the T3 vs T3-tight replay validation. Window covers all "
        "9 brief signals' opens and closes (23:55:13 -> 01:11:40 UTC). M5 ATR warmup is partial "
        "for the first ~75 minutes (market opened 23:00 UTC); identical to the data live faced."
    )
    tape_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    signals_path = FIXTURE_DIR / "signals_2026-11-23.json"
    signals_payload = {
        "schema": SIGNALS_SCHEMA,
        "tape_fixture": "tape_2026-11-23.json",
        "built_utc": fetched_utc,
        "source": "trades_ledger.jsonl (fills/exits) + signals_state.json (ATR-scaled SL/TP) for the 9 brief msg ids",
        "conventions": {
            "layers": "only live-filled layers, exact MT5 fill prices, all initialized at open_utc (canonical-catalog convention)",
            "sl": "common filled-layer initial_sl when unanimous, else signal-level dynamic_risk.sl",
            "tp_levels": "signals_state.json dynamic_risk.tp_levels (ATR-scaled, as live)",
            "actual_mt5_gbp": "live ledger realized_pnl (GBP, live fx ~0.7633; replay uses pinned 0.7589)",
        },
        "trades": trades,
        "provenance": provenance,
    }
    signals_path.write_text(json.dumps(signals_payload, indent=2), encoding="utf-8")
    print(f"nov23 tape: {tape_path} bars={meta['bar_count']} sha={meta['bars_sha256']}")
    print(f"nov23 signals: {signals_path} trades={len(trades)}")
    return 0


# ---------------------------------------------------------------------------
# Synthetic scenarios (deterministic; no randomness)
# ---------------------------------------------------------------------------

def _mk_bars(start_epoch: int, closes: List[float], wicks: List[float], spread_pts: int = 32) -> List[Dict[str, Any]]:
    """Build M1 bars from a close path; wicks[i] = (wick_up, wick_down) for bar i.

    open = previous close (first bar opens at its own close). All prices 2dp.
    """
    bars: List[Dict[str, Any]] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        wu, wd = wicks[i]
        bars.append(
            {
                "time_utc": start_epoch + 60 * i,
                "open": round(o, 2),
                "high": round(max(o, c) + wu, 2),
                "low": round(min(o, c) - wd, 2),
                "close": round(c, 2),
                "spread_pts": spread_pts,
                "spread_dollars": round(max(0.05, spread_pts * 0.01), 4),
            }
        )
        prev = c
    return bars


def _osc_warmup(start: float, n: int, up: float, down: float, end: float) -> List[float]:
    closes = [start]
    for i in range(n - 1):
        closes.append(round(closes[-1] + (up if i % 2 == 0 else -down), 2))
    closes[-1] = end
    return closes


def _scenario_bars(kind: str) -> List[Dict[str, Any]]:
    if kind in _SEGMENT_SPECS:
        return _spec_bars(kind)
    epoch = int(datetime(2026, 11, 23, {"high_atr": 3, "normal_retracement": 5, "sharp_reversal": 7}[kind], 0, tzinfo=timezone.utc).timestamp())
    if kind in ("high_atr", "sharp_reversal"):
        # ATR ~10 warmup: |delta| ~4.6-5 plus 2.5+2.5 wicks.
        closes = _osc_warmup(3595.0, 25, 5.0, 4.6, 3600.0)
        wicks = [(2.5, 2.5)] * 25
        closes.append(3600.0)  # bar 25: entry doji
        wicks.append((2.5, 2.5) if kind == "high_atr" else (0.5, 0.5))
        if kind == "high_atr":
            advance = [2.0, 2.0, -1.2, -1.3, 2.7, 2.8, -1.2, -1.3, 2.7, 2.8, -1.2, -1.3, 2.2, 2.3,
                       -1.2, -1.3, 0.8, -0.9, 0.6]
            for d in advance:
                closes.append(round(closes[-1] + d, 2))
            wicks += [(3.5, 3.5)] * len(advance)
            tail = [-0.4, 0.3, -0.3, 0.2, -0.2, 0.1]
            for d in tail:
                closes.append(round(closes[-1] + d, 2))
            wicks += [(1.0, 1.0)] * len(tail)
        else:
            spike = [2.0, 2.0, 1.0]
            for d in spike:
                closes.append(round(closes[-1] + d, 2))
            wicks += [(0.5, 0.5)] * len(spike)  # keep peak MFE ~5.5 so T3 (0.70x ATR ~7) never arms
            crash = [-4.0, -5.0, -6.0, -7.0, -8.0, -7.0, -6.0]
            for d in crash:
                closes.append(round(closes[-1] + d, 2))
            wicks += [(0.5, 3.5)] * len(crash)
            tail = [-1.0, 0.5, -0.5]
            for d in tail:
                closes.append(round(closes[-1] + d, 2))
            wicks += [(1.0, 1.0)] * len(tail)
    else:
        # ATR ~4 warmup: |delta| ~1.4-1.6 plus 1.2+1.2 wicks.
        closes = _osc_warmup(3597.6, 25, 1.6, 1.4, 3600.0)
        wicks = [(1.2, 1.2)] * 25
        closes.append(3600.0)
        wicks.append((1.2, 1.2))
        advance = [1.5, 1.5, -1.1, -1.1, 2.1, 2.1, -1.2, -1.2, 2.7, 2.7, -1.1, -1.1, 1.6, 1.6,
                   -0.8, -0.7, 0.6, -0.5]
        for d in advance:
            closes.append(round(closes[-1] + d, 2))
        wicks += [(1.2, 1.2)] * len(advance)
        tail = [-0.3, 0.2, -0.2, 0.1, -0.1, 0.1]
        for d in tail:
            closes.append(round(closes[-1] + d, 2))
        wicks += [(0.6, 0.6)] * len(tail)
    return _mk_bars(epoch, closes, wicks)


# Spec-driven scenarios for attachment section 9 (deterministic, no randomness).
# Each spec: warmup oscillation + entry doji + scripted (delta-list, wick) segments.
_SEGMENT_SPECS: Dict[str, Dict[str, Any]] = {
    "low_vol": {
        "warmup": (3598.8, 25, 0.8, 0.7, 3600.0, (0.4, 0.4)),
        "doji_wick": (0.4, 0.4),
        "segments": [
            ([0.8, 0.8, 0.9, 0.9], (0.4, 0.4)),
            ([-0.5, -0.4], (0.4, 0.4)),
            ([0.9, 0.9, 0.8], (0.4, 0.4)),
            ([-0.5, -0.4], (0.4, 0.4)),
            ([0.8, 0.7], (0.4, 0.4)),
            ([-0.3, -0.3, 0.2, -0.2], (0.3, 0.3)),
            ([-0.1, 0.1, -0.1, 0.1, -0.1, 0.1], (0.2, 0.2)),
        ],
        "hour": 9,
    },
    "rapid_atr_change": {
        "warmup": (3597.6, 25, 1.5, 1.3, 3600.0, (0.8, 0.8)),
        "doji_wick": (0.8, 0.8),
        "segments": [
            # Straight grind to ~$5.3 MFE with tiny wicks so the pre-expansion
            # ladder reaches tier >= 1.25 before the ATR spike (hysteresis probe).
            ([1.2, 1.3, 1.2, 1.3], (0.3, 0.3)),
            # Volatility expansion: ATR jumps ~3 -> ~10+ mid-trade (hysteresis probe).
            # 15 big bars = 3 full M5 buckets so the closed-bar ATR window is dominated.
            ([7.0, -8.0, 8.0, -7.0, 6.0, -9.0, 7.0, -8.0, 8.0, -7.0, 6.0, -8.0, 7.0, -9.0, 8.0], (4.0, 4.0)),
            ([-2.0, 1.0, -1.0], (2.0, 2.0)),
        ],
        "hour": 10,
    },
    "strong_trend": {
        "warmup": (3597.6, 25, 1.6, 1.4, 3600.0, (1.2, 1.2)),
        "doji_wick": (1.2, 1.2),
        "segments": [
            # Strong trend: shallow $0.4 pullbacks + $0.3 wicks (adverse ~$1.0
            # from peak) so the early ladder tiers' retained giveback absorbs it.
            ([1.8, 1.6, -0.4] * 6, (0.3, 0.3)),
            ([1.5, 1.5], (0.3, 0.3)),
            ([-0.4, 0.3, -0.3, 0.2], (0.3, 0.3)),
        ],
        "hour": 11,
    },
    "multi_entry": {
        "warmup": (3597.6, 25, 1.6, 1.4, 3600.0, (1.2, 1.2)),
        "doji_wick": (1.2, 1.2),
        "segments": [
            ([2.0, 2.0], (1.5, 1.5)),
            ([-1.2, -1.3], (1.5, 1.5)),
            ([2.5, 2.5], (1.5, 1.5)),
            ([-1.2, -1.3], (1.5, 1.5)),
            ([2.0, 2.0], (1.5, 1.5)),
            ([-1.5, -1.5, -1.5], (1.5, 1.5)),
            ([-0.3, 0.2, -0.2, 0.1], (0.6, 0.6)),
        ],
        "hour": 12,
    },
    "auto_be_30pip": {
        "warmup": (3595.0, 25, 5.0, 4.6, 3600.0, (2.5, 2.5)),
        "doji_wick": (0.3, 0.3),
        "segments": [
            # MFE ~3.6: >= auto-BE $3.00 (30 pips x $0.10) but < ladder tier 1 (0.40 x ATR ~10).
            ([1.2, 1.1, 1.0], (0.3, 0.3)),
            ([-4.0, -5.0, -6.0, -4.0], (0.5, 3.5)),
            ([-1.0, 0.5], (1.0, 1.0)),
        ],
        "hour": 13,
    },
    "explicit_be": {
        "warmup": (3595.0, 25, 5.0, 4.6, 3600.0, (2.5, 2.5)),
        "doji_wick": (0.5, 0.5),
        "segments": [
            ([2.0, 2.0, 1.0], (0.5, 0.5)),
            ([0.0], (0.5, 0.5)),  # bar 30: scripted "Set breakeven" command fires here
            ([-5.0, -6.0, -7.0], (0.5, 3.5)),
            ([-1.0, 0.5], (1.0, 1.0)),
        ],
        "hour": 14,
    },
}


def _spec_bars(kind: str) -> List[Dict[str, Any]]:
    spec = _SEGMENT_SPECS[kind]
    start, n, up, down, end, wick = spec["warmup"]
    epoch = int(datetime(2026, 11, 23, spec["hour"], 0, tzinfo=timezone.utc).timestamp())
    closes = _osc_warmup(start, n, up, down, end)
    wicks = [wick] * n
    closes.append(end)
    wicks.append(spec["doji_wick"])
    for deltas, seg_wick in spec["segments"]:
        for d in deltas:
            closes.append(round(closes[-1] + d, 2))
            wicks.append(seg_wick)
    return _mk_bars(epoch, closes, wicks)


SCENARIO_DEFS: Dict[str, Dict[str, Any]] = {
    "high_atr": {
        "sl": 3562.0,
        "tp_levels": {"TP1": 3610.0, "TP2": 3618.0, "TP3": 3628.0, "TP4": 3640.0, "TP5": 3655.0},
        "atr_band": (8.5, 12.5),
        "description": "High ATR (~10), wide stop R=$38, $2-3 retracements on the way to ~$12 MFE; tests early protection vs premature stop-outs.",
    },
    "normal_retracement": {
        "sl": 3585.0,
        "tp_levels": {"TP1": 3606.0, "TP2": 3612.0, "TP3": 3620.0, "TP4": 3630.0, "TP5": 3640.0},
        "atr_band": (3.0, 5.5),
        "description": "Normal ATR (~4), R=$15, $2-3 retracements on the way to ~$9 MFE; tight trails must not stop out normal noise.",
    },
    "sharp_reversal": {
        "sl": 3562.0,
        "tp_levels": {"TP1": 3610.0, "TP2": 3618.0, "TP3": 3628.0, "TP4": 3640.0, "TP5": 3655.0},
        "atr_band": (8.5, 12.5),
        "description": "High ATR (~10), wide stop R=$38, ~$5 MFE spike then sharp reversal through entry to SL (Nov-23 msg 13305 shape).",
    },
    "low_vol": {
        "sl": 3590.0,
        "tp_levels": {"TP1": 3604.0, "TP2": 3607.0, "TP3": 3610.0, "TP4": 3614.0, "TP5": 3618.0},
        "atr_band": (1.0, 2.8),
        "description": "Low volatility (ATR ~1.5), R=$10, gentle $0.8-1 retractions on a +$5.7 grind; ladder tiers arm on small MFE/ATR ratios.",
    },
    "rapid_atr_change": {
        "sl": 3585.0,
        "tp_levels": {"TP1": 3606.0, "TP2": 3612.0, "TP3": 3618.0, "TP4": 3625.0, "TP5": 3635.0},
        "atr_band": (3.0, 12.0),
        "description": "Rapid ATR expansion ~3 -> ~10-13 mid-trade after ~$5.8 MFE; tier hysteresis must not regress when ATR spikes.",
    },
    "strong_trend": {
        "sl": 3588.0,
        "tp_levels": {"TP1": 3605.0, "TP2": 3610.0, "TP3": 3615.0, "TP4": 3620.0, "TP5": 3625.0},
        "atr_band": (3.0, 5.5),
        "description": "Strong directional trend (+$19) with only $0.7 pullbacks; moving TP1-TP4 targets must not reset MFE or the ladder.",
    },
    "multi_entry": {
        "sl": 3580.0,
        "tp_levels": {"TP1": 3606.0, "TP2": 3612.0, "TP3": 3618.0, "TP4": 3625.0, "TP5": 3635.0},
        "atr_band": (3.0, 5.5),
        "layers": [["L1", 0.01, 3600.0], ["L2", 0.01, 3597.0], ["L3", 0.01, 3594.0], ["L4", 0.02, 3591.0]],
        "description": "Multiple entries (4 filled zone layers, 0.05 lot total) with $2.5 retracements; per-position MFE and partial-close paths.",
    },
    "auto_be_30pip": {
        "sl": 3585.0,
        "tp_levels": {"TP1": 3610.0, "TP2": 3618.0, "TP3": 3628.0, "TP4": 3640.0, "TP5": 3655.0},
        "atr_band": (8.5, 12.5),
        "description": "Automatic 30-pip BE: MFE ~$3.6 reaches the $3.00 auto-BE threshold but stays below ladder tier 1 (0.40 x ATR ~10), then reverses to SL.",
    },
    "explicit_be": {
        "sl": 3585.0,
        "tp_levels": {"TP1": 3610.0, "TP2": 3618.0, "TP3": 3628.0, "TP4": 3640.0, "TP5": 3655.0},
        "atr_band": (8.5, 12.5),
        "be_command_bar": 30,
        "be_command_text": "Set breakeven",
        "description": "Explicit Telegram BE command ('Set breakeven') scripted at bar 30 while ~$5.5 in profit, then sharp reversal; drives mt5_executor.apply_signal_breakeven.",
    },
}


def build_scenarios() -> int:
    h = _load_harness()
    fetched_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for kind, defn in SCENARIO_DEFS.items():
        bars = _scenario_bars(kind)
        h._compute_rolling_atr(bars, period=h.ATR_PERIOD)
        # Validate the ATR regime during the trade window (bars 26+).
        m5 = h._compute_m5_atr_by_m1(bars)
        trade_atrs = [a for a in m5[26:] if a > 0.0]
        assert trade_atrs, f"{kind}: no ATR during trade window"
        med = sorted(trade_atrs)[len(trade_atrs) // 2]
        lo, hi = defn["atr_band"]
        assert lo <= med <= hi, f"{kind}: median trade-window M5 ATR {med:.2f} outside band [{lo}, {hi}]"
        if kind == "rapid_atr_change":
            assert max(m5[40:]) > 2.0 * m5[28], f"rapid_atr_change: ATR did not expand (bar28={m5[28]:.2f}, post={max(m5[40:]):.2f})"
        tape_path = FIXTURE_DIR / f"tape_scenario_{kind}.json"
        meta = h._write_tape_fixture(
            tape_path, bars, fetched_utc=fetched_utc,
            source=f"SYNTHETIC deterministic scenario '{kind}' generated by scripts/build_nov23_fixtures.py (no live data)",
            canonical=False,
        )
        payload = json.loads(tape_path.read_text(encoding="utf-8"))
        payload["scenario"] = kind
        payload["scenario_description"] = defn["description"]
        payload["median_trade_window_m5_atr"] = round(med, 4)
        tape_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        open_utc = h._utc_iso(bars[25]["time_utc"])
        trade = {
            "msg_id": f"syn_{kind}",
            "dir": "BUY",
            "open_utc": open_utc,
            "close_utc": h._utc_iso(bars[-1]["time_utc"]),
            "layers": defn.get("layers") or [["L1", 0.01, 3600.0]],
            "sl": defn["sl"],
            "tp_levels": defn["tp_levels"],
            "actual_mt5_gbp": 0.0,
            "actual_exit_reason": "SYNTHETIC scenario (no live counterpart)",
        }
        if defn.get("be_command_bar") is not None:
            trade["be_command_utc"] = h._utc_iso(bars[defn["be_command_bar"]]["time_utc"])
            trade["be_command_text"] = defn.get("be_command_text") or "Set breakeven"
        signals_path = FIXTURE_DIR / f"signals_scenario_{kind}.json"
        signals_path.write_text(json.dumps({
            "schema": SIGNALS_SCHEMA,
            "tape_fixture": tape_path.name,
            "built_utc": fetched_utc,
            "source": f"SYNTHETIC scenario '{kind}': {defn['description']}",
            "conventions": {"layers": "single L1 0.01 lot at 3600.00 for clean per-scenario attribution"},
            "trades": [trade],
            "provenance": {},
        }, indent=2), encoding="utf-8")
        print(f"scenario {kind}: {tape_path.name} bars={meta['bar_count']} medianATR={med:.2f} sha={meta['bars_sha256'][:16]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("nov23", "scenarios", "all"))
    args = parser.parse_args()
    if args.mode in ("nov23", "all"):
        rc = build_nov23()
        if rc:
            return rc
    if args.mode in ("scenarios", "all"):
        return build_scenarios()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
