# Phase 0 — Read-Only Audit & Implementation Plan

Repo: telegram-mt5-copier @ e70b388 (checkpoint tag v3.9.0-dev-baseline).
Task: autonomous trading-team development directive. DEVELOPMENT/VALIDATION ONLY —
no live trades, no restart, no live-account changes. Four read-only specialist audits
(architect/backend, quant/replay, risk/MT5, QA) were run; nothing was edited in Phase 0.

## 1. Architecture overview

Telegram -> parser.parse_message -> main._handle_new_message_unlocked ->
mt5_executor.execute_4layer_zone/single_price/market (ATR SL/TP via apply_dynamic_atr_sl_tp)
-> state_mgr persistence. All MT5 IPC goes through _mt5_call = asyncio.to_thread, serialized
by threading.RLock _mt5_lock. Watcher tp1_proximity_watcher_task (main.py:4464) runs a 1.0s loop
(0.5s when BE deferred): deleted-signal sweep, MT5 health, daily win/loss/drawdown guards,
_reconcile_live_mt5_signals, then per-signal locked ticks (fast BE sweep -> runner tick or
pre-TP1/backstop tick). R-lock floor joins the trail candidate picker (mt5_executor.py:8297).
Domain modules (dynamic_risk, risk_lifecycle) are pure stdlib.

## 2. Configuration propagation chain (the silent-no-op hazard)

A new DYNAMIC_* knob must be registered at ALL of:
1. config.py env parse;
2. from config import ... in mt5_executor.py (proxy fallbacks reference bare globals);
3. _CFG_IMPORT_SNAPSHOT tuple (governs _cfg monkeypatch precedence);
4. _get_dynamic_risk_policy SimpleNamespace proxy (the whitelist gate) -> read_dynamic_risk_policy;
5. DynamicRiskPolicy field + reader getattr;
6. startup_checks._validate_dynamic_risk_config + pair-swap lists;
7. if main needs it: main import block + _MAIN_CFG_SNAPSHOT + _main_cfg.
A knob absent from step 4 silently falls back to the dataclass default (no error).
Verified: all 36 DYNAMIC_* + 3 R_LOCK knobs ARE registered at HEAD. Dead/silent-no-op knobs:
STAGE1_ATR_ACTIVATION_PCT (parsed, read only by the replay), TP2_SHORTFALL_ARM_PIPS /
TP2_SHORTFALL_PULLBACK_PIPS / NO_TP2_SURGE_ARM_PIPS (dead), TRAIL_STEP_PIPS (imported/validated
but never consumed by trailing). Vestigial post-timer-removal: lifecycle_action (ledger/stats
still reference it), signal_data[tp2_shortfall_armed].

## 3. Ranked technical risks

R1 Lost-update on selective write-back: executor mutates the signal dict in place off-loop
   (broker_sl/recorded_entry_price/trail_stop); the watcher BE sweep persists only a fixed field
   set, so those mutations can be dropped each tick (state deep-copied on read). Runner tick uses
   dirty-diff saves (safer); BE path is asymmetric.
R2 No MT5 re-initialize on reconnect; connect only at boot; transition handler only alerts.
R3 Persistence failures log-only (_save_state_locked returns False; update_signal ignores it).
R4 Watcher catch-all aborts the whole tick on one bad signal (no per-signal isolation).
R5 No cumulative-exposure gate: MAX_ORDERS_PER_SIGNAL is per-signal; pending layers exempt from
   the lot cap; no re-check at fill time -> N concurrent signals = N x correlated lots.
R6 Pre-TP1 /trail non-runner branch can loosen an already-BE'd stop (mt5_executor.py:7888).
R7 modify_position_sltp leg fallback can lift TP while the SL write fails (unless require_both).
R8 Daily-loss guards fail-open on disconnected IPC (synthetic financials); restart archives
   realized losses -> bypasses the pause; dead key realized_today vs realized_today_pnl;
   cutoff closes all tracked positions, not just losers.
R9 No MT5 identity/permissions preflight; connect() has no post-login identity assertion;
   .env has no MT5_PASSWORD; order_send None + filling retry can double-submit; synthetic-ticket
   fallback could phantom-track.

## 4. Quant findings (measured from the saved v3.9.0 replay; hypotheses labelled)

R-lock: 7240 picker calls, 36 R_LOCK wins (0.5%), only trades 9937/9953, max winning R=$9.74.
STRUCTURAL: the picker keeps the tightest stop; ATR_TRAIL tracks MFE 1:1 with distance capped
$8/$5.5/$4 by zone; the R floor is static per tier. R_LOCK wins only while trail_dist > (t_k-f_k)*R
(0.75R/1.0R/1.25R). For R=$25 that needs trail_dist>$18.75 — impossible under any clamp. So
wide-stop domination is STRUCTURAL, not a BE-gate fraction problem. Re-tiering R_LOCK will NOT
make it win on wide stops (corrects the earlier hypothesis). Its real niche: high-ATR/unarmed
windows at narrow R; many wins are fraction-0 BE-equivalents. The 9953 tail gain came from the
R-gated BE arming earlier than flat BE for R<$7, not the staircase. Trio peaks never reached 0.5R
-> R-lock is irrelevant to gate_3.

gate_3: condition = trio GBP>6.125 AND pts*lot>=0.090 AND 13153 stage>=2. v3.9.0: GBP 6.26 (pass),
stage 3 (pass), pts*lot 0.0825 (FAIL — the sole failing leg). Thresholds inconsistent: 0.090 pts*lot
== GBP 6.83, stricter than 6.125. Cause is STOP-DISTANCE SURRENDER, not TP-lift timing:
13153 giveback $5.25/oz (capture 20.1%); 13167 $6.44/oz (24%); 9934 $5.84/oz — TP1 cleared,
milestone floor=BE, ATR trail NEVER armed (ATR>MFE 6.34, activation 1.0xATR), exit BE_LOCK_SL
GBP 0.38, capture 1.0% of pot 47.98. 9934 is the big surrender.

MFE/MAE: replay peak uses avg entry vs engine per-layer (metric mismatch on multi-layer).
MAE is not measured anywhere.

Candidate adjustments (ALL HYPOTHESIS, must A/B on pinned data):
H1 lower trail_activation_atr_mult 1.00->~0.7 so the trail arms on high-ATR trades (9934);
   alone insufficient (trail_dist 8 looser than BE at MFE 6.34) — pair with H2.
H2 tighten STANDARD max clamp $8->~$5-6, or a post-TP1 progressive floor (lock a fraction of
   MFE-at-clear instead of flat BE). Targets 9934/13167 surrender; risks premature stops on
   winners 9905/9705 — must A/B.
H3 do NOT re-tier R_LOCK for wide stops (structural); instrument lock_fraction first.
H4 align gate_3 pts*lot/GBP thresholds (0.090==6.83 vs 6.125) or justify the stricter leg.

## 5. Replay reproducibility (two holes)

Hole 1: fetch_m1_bars calls mt5.copy_rates_range LIVE every run unless --bars-json; broker
history resync/revisions + spread fields drift. Saved tape replay_bars_v381.json (3477 bars,
Nov 16 23:00 -> Nov 19 11:00 UTC) matches v3.9.0 bars_meta.
Hole 2: ARM_B_CALIBRATED_PARAMS does NOT pin DYNAMIC_ATR_* — those read ambient .env, so trail
clamps silently vary between runs.
Design: canonical fixture (tests/fixtures/tape_2026-11-17_19.json) with sha256 of bars, fetched_utc,
terminal build/server, symbol digits/stops/freeze, spread policy; harness defaults to the fixture,
live fetch only via explicit flag; load-time validation (count/range/hash/gaps); refresh = explicit
re-fetch to a NEW file + human-reviewed diff before promotion; pin ALL DYNAMIC_ATR_* in arm params;
freeze GBP rate 0.7589 and server offset 7200 (DST-sensitive).

## 6. QA findings (measured: 391 passed, 0 failed, 0 skipped)

.env-dependent fragile tests (assert literal shipped defaults against ambient config):
test_dynamic_risk_management.py::test_decommission_and_pure_dynamic_atr_defaults;
test_v352_production_suite.py::test_12_v352_config_defaults_and_safety_invariants (also a bare
importlib.reload(config)); test_unlimited_defaults_and_saved_controls.py::test_default_config_values_are_all_unlimited_off
and ::test_startup_checks_allows_zero_limits_and_validates_ordering;
test_fast_channel_breakeven_all_layers.py::test_default_be_layers_includes_all_four_layers;
test_deleted_signal_protection.py::test_9_close_on_signal_deleted_config_toggle_and_startup_validation.
Stale pins: _pin_dynamic_defaults (TRAIL_MULT=1.50/TRAIL_MAX=12.00 vs shipped 1.40/8.00; omits the
8 EXTENDED/HOMERUN knobs + TP1_LIFT_MIN_PROGRESS_PCT); three test_v344_float_cap tests pin MAX=12.00;
test_trade_lifecycle.py::_atr_policy pins trail_max_dollars=12.00.
Robust shipped-defaults pattern: assert DynamicRiskPolicy() dataclass defaults +
read_dynamic_risk_policy(SimpleNamespace()) reader fallbacks (no reload); for config-module literals,
a fixture that monkeypatches config.load_dotenv to no-op + delenv + importlib.reload(config), assert,
undo+reload (caveat: raw os.getenv knobs like R_LOCK_TIERS become empty-string; from-config bindings
in main/mt5_executor do not follow reload). Keep ambient checks validity-only, never value equality.
Propagation: 36 DYNAMIC_* knobs all registered, but only 2 have a propagation test; no drift guard
(config knob set == proxy keys == snapshot keys).
Trailing gaps: executor-level evaluate_dynamic_atr_engine tested BUY-only (SELL only at pure-function
level); no test that a broker-rejected trail modify leaves peak/broker_sl unadvanced; manual /trail
no-loosen unverified; Stage-A cap 8.00 only via ambient; trail-peak hydration across restart unverified.

## 7. Implementation plan (prioritised phases)

Phase 1 — RELIABILITY (must pass before any strategy calibration):
 1.1 Pin the replay tape: canonical fixture + sha256/meta + load validation + explicit refresh;
     harness defaults to fixture; pin ALL DYNAMIC_ATR_* in ARM_B/ARM_C params; freeze GBP/offset.
     File: scripts/ab_replay_proof.py, tests/fixtures/tape_*.json, tests/test_replay_reproducibility.py.
 1.2 .env-independent config-default tests: new tests/test_shipped_defaults.py (dataclass + empty-reader
     defaults + neutralized-load_dotenv reload fixture); convert the fragile tests; repair stale pins.
 1.3 Config whitelist guard + propagation: parametrized sentinel-per-knob -> _get_dynamic_risk_policy /
     _get_lifecycle_policy; set-equality drift guard config<->proxy<->snapshot; retire dead knobs.
     Files: tests/test_config_propagation.py (+ optional startup assertion).
 1.4 MT5 startup preflight: validate_mt5_session(executor,cfg) in startup_checks.py; hook after
     executor.connect (main.py:8891) + on reconnect; checks terminal connected/trade_allowed,
     account login/server match, symbol sane; fail-safe TRADING_HALTED on mismatch; DRY_RUN bypass.
     Files: startup_checks.py, main.py, tests/test_mt5_preflight.py.
Phase 2 — R-lock: instrument lock_fraction; confirm the structural wide-stop finding; decide keep
     staircase vs fold into BE gate. NO blind re-tiering. A/B on pinned tape.
Phase 3 — gate_3: A/B B vs B+H1 vs B+H2 vs B+H1+H2 on pinned tape; metrics trio pts*lot, per-trade
     giveback, 9934 exit source, net/tail guards, winner (9905/9705) non-regression.
Phase 4 — trailing-distance optimisation: A/B current vs tighter vs adaptive vs R-based vs milestone
     on pinned tape; full metric set; select strongest across conditions, not max-MFE on a small sample.
Independent reviewer after each phase; final development dashboard.

## 8. Guardrails

No live trades / restart / live-account changes. Experimental params separate from production defaults
until approved. Every adjustment needs replay evidence; no single-trade evidence; label measured vs
hypothesis. Version checkpoint v3.9.0-dev-baseline preserved. Tests run after each phase; never silently
ignore failures; no concurrent same-file edits.