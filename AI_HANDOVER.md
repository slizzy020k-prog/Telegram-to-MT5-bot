# AI Handover & Architecture Reference (`AI_HANDOVER.md`)

Technical handover for any AI coding agent or developer maintaining the **Telegram-to-MT5 Autonomous XAUUSD Copier — v3.9.0**.

**Verification snapshot:** branch `main`, HEAD `1f1565e` (`feat(replay): controlled trailing A/B experiments (gate_3 + distance)`). Full suite: **`python -m pytest -q` → 536 passed, 0 failed** (~47 s). Ground truth: `AUTOMATION_CHANGELOG.md` (v3.8.0–v3.9.0), `docs/development/phase0-audit-and-plan.md`, and the code itself. This document replaces the stale v2.2.2 handover (3-layer sizing, TP2/TP3 shortfall runner guards, 76-test suite — all obsolete).

---

## 1. System Purpose & Core Invariants

1. **Gold (`XAUUSD.s`) only.** Default symbol `XAUUSD.s` (PU Prime; suffix auto-resolution via `MT5Executor.resolve_symbol`). Non-Gold symbols in `IGNORED_SYMBOLS` are rejected by the parser. Gold pip math: **1 pip = $0.10**; 10 pips = $1.00; contract = 100 oz/lot.
2. **4-layer entries, 0.05 base lots.** `BASE_LAYER_LOTS = [0.01, 0.01, 0.01, 0.02]` (`L1..L4`), `RISK_LOT_MULTIPLIER = 1.0`, `MAX_ORDERS_PER_SIGNAL = 4`. The v2 3-layer TOP/MID/BOTTOM (0.04) sizing is **gone**; `LEGACY_LAYER_MAP = {TOP: L1, MID: L2, BOTTOM: L4}` survives only for backward compatibility.
3. **No clock exits (v3.9.0).** Trades and pending limits are never closed/cancelled by a timer — only by TP, by the (trailed / R-locked / initial) broker SL, or by an explicit provider/user command. All `LIFECYCLE_*` config and `/lifecycle` were deleted.
4. **One exit engine.** The **Pure Dynamic ATR Engine** (`mt5_executor.evaluate_dynamic_atr_engine`, mt5_executor.py:7962) owns BE lock, milestone floors, forward-only ATR trailing SL and momentum-gated trailing TP. It **only modifies broker SL/TP — it never calls `close_market_position`**. All pre-v3.7 virtual exit mechanisms are decommissioned (§6).
5. **Magic-number isolation.** `MAGIC_NUMBER = 777001`; manual trades are never modified by signal operations.
6. **Fail-safe session verification.** `validate_mt5_session` preflight at boot and on reconnect; hard failure ⇒ `TRADING_HALTED` (no orders), Telegram monitoring stays online (§8).
7. **Concurrency model.** All MT5 IPC goes through `_mt5_call = asyncio.to_thread`, serialized by `threading.RLock _mt5_lock`. Domain modules (`dynamic_risk.py`, `risk_lifecycle.py`, `risk_controls.py`) are pure stdlib — no MT5 import, no I/O.

---

## 2. File-by-File Architecture

| File | Responsibility (v3.9.0) |
| :--- | :--- |
| `config.py` | Loads `.env` (safe `_env_bool/int/float/choice` parsers with clamping + fallbacks), exposes all runtime constants. 4-layer lots, Pure Dynamic ATR knobs (36 `DYNAMIC_*`), 3 `R_LOCK_*` knobs, optional `MT5_LOGIN/PASSWORD/SERVER/PATH`. `validate_dynamic_risk_pairs()` swaps inverted min/max pairs at import. **No `LIFECYCLE_*`, no staged-exit/guard knobs.** |
| `parser.py` | Stateless sequential gate pipeline `gate_1..gate_5` (`parse_message`): forward/empty filter → announcement/recap/ignored-symbol filter → trade-management classification (BE / close-worst-hold / reactivate / partial / full close / cancel-pending / TP1-hit reply) → signal extraction (direction, zone/single/instant, TP1–TP5 with monotonic typo sanitizer) → price sanity (wrong-side/out-of-range TP strip, SL caps, fallbacks). |
| `dynamic_risk.py` | **Pure Dynamic ATR Engine domain logic.** `DynamicRiskPolicy` dataclass + `read_dynamic_risk_policy` reader (defensive normalization); `scale_sl_tp_by_atr` (entry SL/TP scaling); `select_progressive_trail_mult` + `trail_zone_clamp` (3-stage compression, forward-only invariant); `compute_dynamic_atr_trail_stop` (tightest-candidate picker: R_LOCK > MILESTONE_FLOOR > ATR_TRAIL > BE_LOCK; R-gated BE arming); `compute_dynamic_tp_step` (momentum-gated TP lifts); `/dynamicrisk` + `/atrtrail` command handlers. |
| `risk_lifecycle.py` | **R-relative profit floor + fill-time telemetry only** (timers removed). `LifecyclePolicy` (r_lock_enabled/tiers/min_r), `parse_r_lock_tiers`, `compute_initial_r`, `compute_r_lock_floor` (peak-derived, market-side-validated), `apply_rlock_command` (`/rlock`), `stamp_fill_times` / `resolve_fill_time` (earliest known fill — used by ledger exit age). |
| `risk_controls.py` | Pure stdlib domain controls for runtime risk limits (`RiskLimitsSnapshot`: lot cap, daily-loss pause / emergency cutoff, floating-loss cap) and Saved Messages `/lotcap`, `/losslimit`, `/floatcap` command handling. |
| `mt5_executor.py` | `MT5Executor`: connection/symbol resolution, 4-layer zone/single-price/market placement (`execute_4layer_zone` / `execute_4layer_single_price` / market), ATR SL/TP application (`apply_dynamic_atr_sl_tp`), **`evaluate_dynamic_atr_engine`** (per-layer trail/BE/TP-lift ticks, R-lock wiring at mt5_executor.py:8297, pending sweeps), `modify_position_sltp` (stops/freeze-validated), breakeven (fast per-channel, stops-level fallback + deferred retry), per-order `check_preflight`, risk-guide lot cap, daily drawdown/realized-P&L queries, `initial_sl` capture at placement. Config whitelist proxies: `_CFG_IMPORT_SNAPSHOT` (:134), `_cfg` monkeypatch precedence (:232), `_get_dynamic_risk_policy` (:1398), `_get_lifecycle_policy`. |
| `state_manager.py` | Thread-safe `StateManager` persisting `signals_state.json` via atomic `.tmp` replacement; `(chat_id, msg_id)` key isolation; `_`-prefixed control keys (`_daily_win`, `_daily_loss`) excluded from signal iterators. |
| `trade_ledger.py` | Append-only JSONL ledger, **schema 4** (`build_trade_record`, `TradeLedger`): cash MFE/giveback with account-currency FX conversion (`_apply_giveback_currency`), `initial_r_per_oz`, `r_at_exit`, `exit_age_seconds` (via `resolve_fill_time`), exit-reason normalization incl. legacy aliases, `get_stats`. |
| `startup_checks.py` | `validate_environment()` (packages, config invariants, writable paths, credential presence — never logs secrets); **`validate_mt5_session()`** read-only MT5 preflight (§8); `SingleInstanceLock` (Win32 named mutex + OS file lock; duplicate instance ⇒ exit 90). |
| `main.py` | Async Telethon listener (`handle_new_message`, `handle_message_edited`, `handle_message_deleted`, `handle_saved_messages_command`), reconnect catch-up with stale-market guards, **`tp1_proximity_watcher_task`** (main.py:4495; 1.0 s loop, 0.5 s when a BE is deferred — main.py:4622): deleted-signal sweep, MT5 health + connection-transition alerts, daily win/loss/drawdown enforcement, live reconciliation, per-signal locked ticks (fast BE sweep → runner tick or pre-TP1 tick). Preflight wiring: `_run_mt5_session_preflight` (:2818), boot call (:8935), reconnect call (:2883). Phone command plane incl. `/rlock` (:7553), `/stats` (:7566). `_MAIN_CFG_SNAPSHOT` (:596) + `_main_cfg` precedence (:664). |
| `auth.py` | One-time Telegram login (creates the `.session`). |
| `scripts/ab_replay_proof.py` | Pinned-tape 12-trade A/B/C replay harness + experimental arms (§10). |
| `tests/` | **536-test** pytest suite (33 modules + `conftest.py` + `fixtures/tape_2026-11-17_19.json`). Key guards: `test_shipped_defaults.py` (.env-independent defaults), `test_config_propagation.py` (whitelist drift guard), `test_replay_reproducibility.py` (tape pins + byte-identical double replay), `test_mt5_preflight.py`, `test_v390_atr_compression.py`, `test_rlock_fraction.py`, `test_trade_lifecycle.py` (R-lock/telemetry kept after timer-test removal), `test_trailing_experiments.py`, `test_dynamic_risk_management.py` (incl. the decommission assertion). |
| `simulate.py` / `test_examples.py` / `check_algo.py` | Isolated 10-step simulation, worked examples, static algo-trading sanity checks. |
| `start_copier.bat` | Windows watchdog: crash auto-restart with capped backoff (5 s → 60 s); **stops** on exit 88 (daily win), 89 (daily loss), 90 (duplicate instance), 0 (clean/halted). Requires CRLF. Banner text still says "v3.1" (cosmetic only). |
| `AUTOMATION_CHANGELOG.md` | Authoritative autonomous-change history (v3.6.0 → v3.9.0). **Never delete.** |
| `docs/development/phase0-audit-and-plan.md` | Read-only audit: architecture, config chain, ranked risks R1–R9, quant findings, phased plan. |
| `ARCHITECTURE_v3.md` / `PARSER_SPEC.md` | Historical specs — partially superseded (§13). |

---

## 3. Configuration Propagation Chain (the silent-no-op hazard)

The executor filters config through an **explicit whitelist**. A new `DYNAMIC_*` knob must be registered at **all 7 points** (phase0 §2; verified complete at HEAD for all 36 `DYNAMIC_*` + 3 `R_LOCK_*` knobs):

1. `config.py` env parse (`_env_*` helper with shipped default).
2. `from config import ...` block in `mt5_executor.py` (proxy fallbacks reference bare globals).
3. `_CFG_IMPORT_SNAPSHOT` (mt5_executor.py:134) — governs `_cfg` monkeypatch precedence (:232).
4. **`_get_dynamic_risk_policy` SimpleNamespace proxy** (mt5_executor.py:1398) — the whitelist gate → `read_dynamic_risk_policy`. *A knob absent here silently falls back to the dataclass default — no error.* (`R_LOCK_*` goes through `_get_lifecycle_policy`.)
5. `DynamicRiskPolicy` field + reader `getattr` in `dynamic_risk.py`.
6. `startup_checks._validate_dynamic_risk_config` + the min/max pair lists (`config.validate_dynamic_risk_pairs`).
7. If `main.py` needs it: main import block + `_MAIN_CFG_SNAPSHOT` (main.py:596) + `_main_cfg`.

**Drift guard:** `tests/test_config_propagation.py` — sentinel-per-knob propagation for every knob on both monkeypatch targets, plus set-equality guards (config knob set == proxy keys == snapshot keys == reader getattr names). The v3.9.0 clamp knobs (`DYNAMIC_ATR_TRAIL_EXTENDED_MIN/MAX_DOLLARS`, `DYNAMIC_ATR_TRAIL_HOMERUN_MIN/MAX_DOLLARS`) were wired through all points in commit `7791a25`.

**Dead / vestigial knobs (parsed but inert — do not "fix" by wiring them without a decision):**
- `STAGE1_ATR_ACTIVATION_PCT` — parsed by config; consumed only by the replay harness.
- `TP2_SHORTFALL_ARM_PIPS`, `TP2_SHORTFALL_PULLBACK_PIPS`, `NO_TP2_SURGE_ARM_PIPS` — dead compatibility constants.
- `TRAIL_STEP_PIPS` — imported/validated but never consumed by trailing (the engine uses `DYNAMIC_ATR_TRAIL_STEP_DOLLARS`).
- Vestigial post-timer-removal: `lifecycle_action` (ledger field + `/stats` "Lifecycle closes" line — always null/0 on new trades), `signal_data["tp2_shortfall_armed"]`.

---

## 4. Entry Architecture (4-layer)

Flow: Telegram → `parser.parse_message` → `main._handle_new_message_unlocked` → `mt5_executor.execute_4layer_zone / single_price / market` (ATR SL/TP via `apply_dynamic_atr_sl_tp`) → `state_mgr` persistence.

- **Zone:** tiers at 0/33/67/100% of the zone; Market for already-reached prices, Limit for deeper levels; `LIMIT_ENTRY_FRONT_RUN_OFFSET = 0.50` shifts pending BUY limits up / SELL limits down $0.50. Zone anchors are shared between fresh signals and edits (`compute_zone_atr_anchors`) so both scale SL/TP identically (v3.7.0 fix).
- **Single price:** 1 consolidated order (`SINGLE_PRICE_CONSOLIDATE_ORDERS=true`), `zone_completed=False` so late zone edits expand L2–L4; duplicate-pending guards (`$0.50` signal-level / `$0.25` order-level).
- **Market now / bare price:** `STAGED_MARKET_ENTRY=true` ⇒ only L1 at market; L2–L4 await the provider's zone edit. `AUTO_MARKET_SLIPPAGE_PIPS=15` activates bare priced signals at market within $1.50. Optional delay guard `SELL_NOW_DELAY_SECONDS=0` (immediate by default).
- **All layers start at TP1:** broker TP = `TP1 ∓ TP_FRONT_RUN_OFFSET ($0.50)`. ATR scaling (ADAPTIVE, M5 ATR(14), $4 baseline, ratio clamp 0.75–1.75; SL 2.5x clamp [$4.50,$35], TP 1.5x clamp [$2.50,$50]); fallbacks when ATR unavailable (`FALLBACK_SL_PIPS=150`, `FALLBACK_TP1_PIPS=50`, `UNPRICED_TP_GRACE_SECONDS=45`).
- **TP1 reached before zone entry ⇒** unfilled pendings invalidated (`INVALIDATED_TP1`, main.py:2405+); filled positions are never touched by invalidation.
- **Edits:** late SL/TP/zone edits apply anytime; untracked-message age guard `MAX_NEW_EDIT_AGE_SECONDS=3600`, edit-event freshness `MAX_EDIT_EVENT_FRESHNESS_SECONDS=180` (emoji reactions on old posts never fire trades). `roles_locked` (set only on BE-lock or TP1-clear) blocks conflicting SL edits.
- **Deleted signal ⇒** instant close + cancel for that signal only (0 ms push + 1.5 s fast sweep).
- **Runner selection:** deepest filled layer runs (`RUNNER_DEEPEST_COUNT_FULL=1`); single fill ⇒ L1 runs; `RUNNER_MAX_TP_STAGE=5` with dynamic `effective_max_stage` capping at the highest valid TP.

---

## 5. Current Exit Architecture — Pure Dynamic ATR Engine

Per-layer tick inside `evaluate_dynamic_atr_engine` (mt5_executor.py:7962), driven by the watcher:

1. **Guards:** engine disabled / invalid tick / spread spike (`ADAPTIVE_SPREAD_CEILING_DOLLARS=0.80`) / ATR <= 0 ⇒ no-op tick (`paused_by_spread` flagged). Hard broker SL/TP remain active during pauses.
2. **3-stage progressive compression** (M5 ATR; MFE from the non-decreasing per-layer peak):

   | Stage | MFE ($/oz) | Multiplier | Clamp | Knobs |
   | :--- | :--- | :--- | :--- | :--- |
   | A `STANDARD` | < $10 | 1.40x | [$2.50, $8.00] | `DYNAMIC_ATR_TRAIL_MULT`, `..._MIN/MAX_DOLLARS` (max 12→8 in v3.9.0) |
   | B `EXTENDED` | $10–$18 | 1.00x | [$2.20, $5.50] | `..._EXTENDED_MFE_DOLLARS/MULT` + **new** `..._EXTENDED_MIN/MAX_DOLLARS` |
   | C `HOME_RUN` | >= $18 | 0.80x | [$2.00, $4.00] | `..._HOMERUN_MFE_DOLLARS/MULT` + **new** `..._HOMERUN_MIN/MAX_DOLLARS` |

   Boundaries inclusive at $10/$18 (1e-9 epsilon). Trail arms at `MFE >= DYNAMIC_ATR_TRAIL_ACTIVATION_MULT (1.00) x ATR`.
3. **Candidates → tightest picker:** `R_LOCK` (if enabled & valid) > `MILESTONE_FLOOR` > `ATR_TRAIL` > `BE_LOCK` (tie-break priority). `stops_buffer = trade_stops_level x point + STOP_LEVEL_SAFETY_BUFFER_DOLLARS ($0.05)` clamps the candidate; current & candidate SL are checked against `trade_freeze_level` (`is_within_freeze_level`) before any modify ⇒ 0 retcode-10016 rejections in replay.
4. **Forward-only:** `should_update` requires improvement >= `DYNAMIC_ATR_TRAIL_STEP_DOLLARS ($0.25)`; monotonic peak + non-increasing multiplier (`min(...)` guard) + monotonically tightening clamps ⇒ the stop never loosens, including across stage transitions and ATR expansion.
5. **BE lock:** flat arming `max(0.75 x ATR, $3.50)` ⇒ level `entry ± $0.50` + `should_sweep_pending` (cancels unfilled lower limits). **R-gated** when R-lock is on and `initial_r` is known: arming `max(0.5R, 0.75 x ATR, $2.50)`; the LEVEL is unchanged, so the R-gated path is never less protective.
6. **Milestone floors:** TP1 cleared ⇒ BE floor; TPm (m>=2) cleared ⇒ `max(TP(m-1), BE floor)` (sign-aware). Not applied to `BANK_TP1` / `LATE_RECOVERY` roles.
7. **Trailing TP:** `compute_dynamic_tp_step` — lift when within `0.50 x ATR` of the next target or >= 75% leg progress, momentum ok (pullback <= 0.25 x ATR); TP1→TP2 additionally needs >= 50% progress or BE-locked (`DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT`, v3.7.0 anti-premature-lift guard).
8. **Execution:** strictly `modify_position_sltp` (broker SL/TP). The engine never market-closes. Market closes happen only via provider commands, phone controls, session guards, or broker TP/SL fills (reconciled by the watcher with deal-level P&L accounting).

### R-lock (`risk_lifecycle.py`) — ships DISABLED
- `R_LOCK_ENABLED=false` in `config.py` / `.env.example` (the local live `.env` activates it — that is a deployment choice, not the shipped default). `/rlock on|off` is in-memory only (resets on restart); bare `/rlock` reports policy.
- Tiers `0.5:0.00, 1.0:0.25, 1.5:0.50, 2.0:0.75` (MFE threshold R : locked fraction R); `R_LOCK_MIN_R_DOLLARS=0.50`. Floor = `entry + sign x fraction x R`, derived from persisted per-layer `mfe_dollars` (peak), validated against live price — it disappears on retracement past the floor (never a wrong-side stop).
- `initial_sl` is captured per layer **at placement** (the stop the broker actually received, incl. ATR/fallback computation); without it the layer logs `[R-LOCK SKIPPED]` and runs ATR-trail-only.
- **Structural wide-stop limitation (measured, phase0 §4):** the picker keeps the tightest stop; ATR_TRAIL tracks MFE ~1:1 with distance capped $8/$5.50/$4 by zone; the R floor is static per tier. R_LOCK wins only while `trail_dist > (t_k - f_k) x R` (margins 0.75R / 1.0R / 1.25R). For R=$25 that needs trail_dist > $18.75 — impossible under any clamp. **Re-tiering cannot fix wide stops** (the `cx` experimental arm tests exactly this). Real niche: narrow/mid R, high-ATR/unarmed windows; many wins are fraction-0 BE-equivalents. Pinned-tape evidence: 7,240 picker calls, 36 R_LOCK wins (0.5%), only trades 9937/9953, max winning R $9.74. The 9953 tail gain came from the R-gated BE arming earlier than flat BE for R < $7, not from the staircase. Trio peaks never reached 0.5R ⇒ R-lock is irrelevant to gate_3.

---

## 6. Decommissioned Mechanisms (do NOT reintroduce)

Asserted absent from `config` / `mt5_executor` / `main` by `tests/test_dynamic_risk_management.py::test_decommission_and_pure_dynamic_atr_defaults`:
`GOLD_REVERSAL_GUARD_ENABLED`, `STAGE0_PROFIT_LOCK_ENABLED`, `CANCEL_PENDING_ON_STAGE0_LOCK_EXIT`, `PROFIT_RATCHET_ENABLED`, `PROFIT_RATCHET_VIRTUAL_CLOSE_ENABLED`, `BANK_HARVEST_ENABLED`, `STAGED_EXIT_ENABLED`, `SHORTFALL_TRIGGER_DISTANCE`, `SHORTFALL_REVERSAL_TICKS`, `RUNNER_PROXIMITY_THRESHOLD_PIPS`, `RUNNER_REVERSAL_CONFIRM_TICKS`, `RUNNER_EARLY_TP_LIFT_PCT`, `RUNNER_PROACTIVE_TP_LIFT`, `RUNNER_CONTINUOUS_TRAIL_ENABLED`, `RUNNER_CLEAN_BREAK_PIPS`, `evaluate_gold_reversal_guard`, `evaluate_profit_ratchet_tick`.

In plain terms, the following are **gone from live code**:
- v2.x 3-layer TOP/MID/BOTTOM sizing + TP2/TP3 shortfall runner guards + virtual TP1 floor.
- v3.5.0 Stage-0 `GOLD_REVERSAL_GUARD` / `SHORTFALL_BASE` pre-TP1 guards and the mutually-exclusive staged M1-ATR exit FSM (Stages 1–4 `fsm_floor`/`atr_stop` table, `ARM_THRESHOLD_PCT`, `GIVEBACK_PCT`, `HARD_GIVEBACK_CEILING_DOLLARS`, `TP1_TRAIL_*`/`TRAIL_TP2..4_*` knobs).
- v3.6.0 profit-ratchet / bank-harvest virtual exits.
- **v3.9.0 timer removal (commit `f0da936`):** `evaluate_trade_age`, de-risk/hard-flatten loops, pending auto-expiry, session-flatten clock, all `LIFECYCLE_*` config (`LIFECYCLE_ENABLED`, `DERISK_SECONDS`, `FLATTEN_SECONDS`, `PENDING_EXPIRE_SECONDS`, `SESSION_FLATTEN_*`, `MAX_CLOSE_RETRIES`), the `/lifecycle` command, and 104 timer tests. **Kept intact:** R-lock (`compute_initial_r`, `compute_r_lock_floor`, `R_LOCK_*`, `/rlock`) and all telemetry (`initial_sl` capture, fill stamps, `resolve_fill_time`, ledger schema 4).

They survive only as: legacy `exit_reason_code` aliases in `trade_ledger.py` (for parsing historical records), historical Arm-A/B parameters in the replay harness, and changelog/spec history.

---

## 7. What Can Close a Trade Now (exit inventory)

| Trigger | Mechanism |
| :--- | :--- |
| Broker TP hit | Initial TP1-front-run TP, lifted runner TPs, ceiling TP |
| Broker SL hit | Initial SL, BE lock, trailed ATR SL, milestone floor, R-lock floor (all broker-executed) |
| Provider command | BE / close / partial / cancel-pending / close-worst-hold / signal deletion |
| Phone control | `/be`, `/sl`, `/partial`, `/bank*`, `/closeall`, `/trade` management |
| Session guard | `dailywin` (exit 88), `dailyloss` realized-only breaker (exit 89, same-day relaunch LOCKED), optional drawdown/float caps (default OFF) |
| **Clock** | **NOTHING — removed in v3.9.0** |

---

## 8. MT5 Startup Preflight (`startup_checks.validate_mt5_session`)

Read-only IPC (`terminal_info` / `account_info` / `symbol_info`) under `_mt5_lock`; **never** calls `mt5.login`, switches accounts, or sends orders. Checks:
- (a) terminal `.connected` and `.trade_allowed`;
- (b) `account.login == int(MT5_LOGIN)` and (when set) `account.server == MT5_SERVER` — **hard fail** on mismatch; live mode with `MT5_LOGIN` unset ⇒ hard fail unless `cfg.MT5_PREFLIGHT_ALLOW_UNVERIFIED_LOGIN=True`;
- (c) `DEFAULT_SYMBOL` exists with sane `volume_min`/`volume_step`; non-FULL `trade_mode` ⇒ warning only.

`DRY_RUN` (cfg or executor) bypasses everything. Returns `(ok, problems)`; hard failures only.
**Wiring:** `main._run_mt5_session_preflight` (main.py:2818) called after `executor.connect` at boot (main.py:8935, before hydration/handlers armed) and on connection recovery (main.py:2883, only when the executor holds a live session). Failure ⇒ `TRADING_HALTED=True` (main.py:295), per-problem logs + Saved Messages alert, Telegram monitoring stays online, no orders. Per-order `check_preflight` remains defense-in-depth.
**Opt-in:** `MT5_PREFLIGHT_ALLOW_UNVERIFIED_LOGIN` is parsed from `.env` by `config.py` (default `False` = hard-halt on unverified identity) and documented in `.env.example`. The recommended live path is configuring `MT5_LOGIN` (and `MT5_SERVER`); the opt-in exists only for deliberate unverified-identity operation. (Audit risk R9 fixed — see §12.)

---

## 9. Ledger Schema 4 & `/stats`

`trade_ledger.py` — one JSON line per closed signal, `schema_version: 4` (upgrade; schema-3 records still read; fresh clones write schema 4 even with R-lock disabled).
- **New fields:** `mfe_cash` (USD price-based: `peak_profit_per_oz x lots x 100`), `giveback_cash` (**account currency**: `mfe_cash x fx_rate_to_account - realized_pnl`; `null` when the rate is unknown — never mixed-currency), `giveback_currency`, `fx_rate_to_account`, `initial_r_per_oz`, `r_at_exit` (USD/USD ratio — currency-neutral), `exit_age_seconds` (earliest known fill via `resolve_fill_time`, never the raw `first_fill_at` key), `lifecycle_action` (vestigial, null on new trades).
- **Retained schema-3 fields:** exit attribution (`exit_reason`/`exit_reason_code`/`exit_reason_detail`, `exit_stage`), `exit_price`, `max_favorable_excursion`, `giveback_from_peak`, `trail_tier_used`, `layer_exits`, tickets/lots/milestones, realized P&L (deal-ticket accounting fix from v3.6.0).
- **`/stats`** (main.py:7566): streams the JSONL, reports today / 7-day / all-time net P&L, counts, win rate, total giveback (labelled from the ledger's own currency), max exit age, "Lifecycle closes" (vestigial 0) and exit-reason mix; degrades to "no ledger data" on missing/corrupt files.

---

## 10. Replay Harness (`scripts/ab_replay_proof.py`)

Deterministic 12-trade Nov 17–19 `XAUUSD.s` M1 replay; every arm runs `DRY_RUN=true` in an isolated subprocess with temp runtime paths; MT5 strictly read-only.
- **Arms:** A = frozen `4544f7f` (v3.5.2) in a temporary git worktree (deliberately NOT fed the engine ATR); B = calibrated working tree, `R_LOCK_ENABLED=false` **pinned**, production ATR engine live (M1→M5 bucket aggregation, closed-bar SMA-TR ATR(14) fed via `_dry_run_atr` — the v3.8.2 fidelity fix); C = B + R-lock ON. Picker trace (`_install_picker_trace`) records every `compute_dynamic_atr_trail_stop` decision incl. Phase-2 `lock_fraction` instrumentation (reporting only).
- **Pinned tape (Phase 1.1):** default input `tests/fixtures/tape_2026-11-17_19.json` (schema `mt5-copier-replay-tape/1`, 3,477 bars, `2026-11-16T23:00Z → 2026-11-19T11:00Z`, sha256 `9fd14005…`); load-time count/range/hash/gap validation. `--live-bars` = explicit opt-in to live fetch (non-reproducible; incompatible with explicit `--bars-json`). `--refresh-tape <new-file>` re-fetches to a NEW file (never overwrites the canonical fixture) + diff summary for human-reviewed promotion. ALL `DYNAMIC_ATR_*` pinned in both arm param sets (.env-independent); GBP rate frozen `0.7589`, server offset `7200` (DST-sensitive).
- **Gates:** `g1–g5`; `g5` = C >= B net AND C tail no worse (sign direction fixed in v3.8.2). v3.9.0 reference: A £+15.62 / B £+49.91 / C £+52.73; tails −£61.99 / −£26.08 / −£21.74; **g5=True**. **gate_3 still False** (pre-existing choke-trio capture gate) ⇒ `all_gates_passed=False`, exit 1 — expected, not a regression.
- **gate_3 anatomy (phase0 §4):** condition = trio GBP > 6.125 AND pts*lot >= 0.090 AND 13153 stage >= 2. v3.9.0: GBP 6.26 (pass), stage 3 (pass), pts*lot 0.0825 (**sole failing leg**). Thresholds inconsistent: 0.090 pts*lot == GBP 6.83 > 6.125. Cause = **stop-distance surrender**, not TP-lift timing: 9934 cleared TP1, milestone floor = BE, ATR trail NEVER armed (ATR > MFE 6.34 at 1.00x activation), exited BE_LOCK_SL at GBP 0.38 (1.0% capture of pot 47.98).
- **Experimental arms — NOT shipped, never defaults, never written to config/.env:** `cx` (re-tiered R-lock ladder `0.5:0,0.75:0.25,1.0:0.50,1.25:0.75` — Phase-2 hypothesis test), `t1` (activation 1.00→0.70x, H1), `t2` (STANDARD clamp $8→$5.50, H2), `t3` (H1+H2), `t4` (clamp $6.50). `--arm trailing` runs A+B+T1..T4; `--arm` choices: a/b/c/cx/t1..t4/both/trailing (default both).
- **Known metric gaps:** replay peak MFE uses avg entry vs engine per-layer (multi-layer mismatch); MAE is not measured anywhere.

---

## 11. Tests & Validation

```powershell
python -m pytest -q          # 536 passed, 0 failed (HEAD 1f1565e)
python check_algo.py         # ALL CHECKS PASSED
python simulate.py           # isolated 10-step simulation
python test_examples.py
```
- **Shipped-default pattern (Phase 1.2, `tests/test_shipped_defaults.py`):** assert `DynamicRiskPolicy()` / `LifecyclePolicy()` dataclass defaults + `read_*_policy(SimpleNamespace())` reader fallbacks (no reload); for config literals, the `shipped_config` fixture (conftest) neutralizes `load_dotenv`, delenvs strategy knobs, reloads config, asserts, undoes. Ambient checks are validity-only, never value-equality.
- **Propagation drift guard (Phase 1.3):** §3. **Replay reproducibility (Phase 1.1):** tape pins + byte-identical double replay. **Preflight (Phase 1.4):** `tests/test_mt5_preflight.py`.
- Timer tests (104) were removed with the feature; every R-lock/telemetry test was kept.

---

## 12. Known Limitations & Risks (phase0 §3 — fixed vs outstanding)

| Risk | Status | Detail |
| :--- | :--- | :--- |
| **R9** MT5 identity/permissions preflight | **PARTIALLY FIXED** (Phase 1.4) | `validate_mt5_session` added at boot + reconnect; fail-safe `TRADING_HALTED`. **Outstanding sub-risks:** `connect()` itself still has no post-login identity assertion (preflight is a separate layer); `order_send` None + filling-mode retry can double-submit; synthetic-ticket fallback could phantom-track. Opt-out knob not parsed from `.env` (§8). |
| **R1** Lost-update selective write-back | **OUTSTANDING** | Executor mutates the signal dict in place off-loop (`broker_sl`/`recorded_entry_price`/`trail_stop`); the watcher BE sweep persists only a fixed field set, so mutations can be dropped each tick (state deep-copied on read). Runner tick uses dirty-diff saves (safer); BE path asymmetric. |
| **R2** No MT5 re-initialize on reconnect | **OUTSTANDING** | Connect only at boot; transition handler alerts + re-runs preflight but never re-initializes the terminal session. |
| **R3** Persistence failures log-only | **OUTSTANDING** | `_save_state_locked` returns False; `update_signal` ignores it. |
| **R4** Watcher catch-all | **OUTSTANDING** | One bad signal aborts the whole tick (no per-signal isolation). |
| **R5** No cumulative-exposure gate | **OUTSTANDING** | `MAX_ORDERS_PER_SIGNAL` is per-signal; pending layers exempt from the lot cap; no re-check at fill time ⇒ N concurrent signals = N x correlated lots. |
| **R6** Manual `/trail` can loosen | **OUTSTANDING** | Pre-TP1 `/trail` non-runner branch can loosen an already-BE'd stop (mt5_executor.py:7888 per audit). |
| **R7** SL/TP leg fallback | **OUTSTANDING** | `modify_position_sltp` leg fallback can lift TP while the SL write fails (unless `require_both`). |
| **R8** Daily-loss fail-open | **OUTSTANDING** | Guards fail-open on disconnected IPC (synthetic financials); restart archives realized losses ⇒ bypasses the pause; dead key `realized_today` vs `realized_today_pnl`; cutoff closes all tracked positions, not just losers. |

**Fixed since the audit (Phase 1, all verified by tests):** 1.1 pinned tape + reproducibility gate; 1.2 .env-independent config-default tests; 1.3 config whitelist drift guard; 1.4 MT5 preflight. Phase 2 added `lock_fraction` instrumentation (reporting only) and the `cx` arm confirming the structural wide-stop finding. Phases 3/4 trailing optimisation: experimental arms built, **decision pending** — no experimental parameter has been shipped.

**Strategy limitations (measured):**
- R-lock wide-stop domination is **structural** (§5) — do not blind re-tier; instrument first, A/B on the pinned tape.
- gate_3 fails on stop-distance surrender (§10); candidate fixes H1/H2 are **hypotheses** (arms t1–t4), requiring A/B evidence incl. winner (9905/9705) non-regression.
- No clock exits ⇒ stale trades persist until TP/SL/command; there is no age-based safety net (deliberate v3.9.0 product decision).
- Risk caps ship disabled; `gate_3` pts*lot/GBP threshold inconsistency needs an owner decision.

---

## 13. Documentation Drift Notes (left in place deliberately)

- **`ARCHITECTURE_v3.md`** — labelled "LOCKED v3.5.0"; its Stage-0 `GOLD_REVERSAL_GUARD`/`SHORTFALL_BASE` and staged M1-ATR trail contracts are **superseded** by the Pure Dynamic ATR Engine. Kept as a historical contract; do not treat as current. (Its 4-layer allocation/volume-safety sections remain broadly accurate.)
- **`PARSER_SPEC.md`** — gate pipeline broadly matches `parser.py` (which implements `gate_1..gate_5`; the spec's "6 gates" counts the output node), but some side-effect descriptions date from the 3-layer era. Partially stale; left in place.
- **Cosmetic version strings:** `main.py` logs "Telegram Copier v3.0 is LIVE"; `start_copier.bat` banner says v3.1. Behavior is v3.9.0; strings not updated (outside docs scope).
- **`.env.example`** pins `MAX_ALLOWED_SPREAD_DOLLARS=0.40` while the `config.py` shipped default is `0.45` (minor drift; config defaults are authoritative and test-pinned).
- **`AUTOMATION_CHANGELOG.md` is authoritative history — never delete or rewrite it.**

---

## 14. Development Rules

1. **Development/validation only** unless explicitly authorised otherwise: no live trades, no restarts of the live instance, no live-account changes. Replays and tests are `DRY_RUN=true` with isolated paths.
2. **Never commit secrets or runtime state:** `.gitignore` blocks `.env`, `*.session*`, `*.log`, `signals_state.json*`, `sim_state.json`, `trades_ledger.jsonl` / `*.jsonl`, `*.lock`, `*.tmp`, `*.bak*`, and `.superpowers/`. Use placeholders in docs. Verify `git status --short` before committing.
3. **Line endings:** `.bat` files require CRLF; `.md`/`.py` in this repo are LF — preserve them.
4. **Every change:** run `python -m pytest -q` (536 baseline) + `check_algo.py`; never silently ignore failures. Strategy adjustments need pinned-tape replay evidence (no single-trade evidence); label measured vs hypothesis; keep experimental params separate from production defaults until approved.
5. **New `DYNAMIC_*` knobs:** register at all 7 points (§3) or the drift guard fails — a missing whitelist entry is a silent no-op, not an error.
6. **Windows watchdog:** stopping `python.exe` alone triggers the `.bat` auto-restart; stop the parent `cmd.exe` first for a full shutdown (exit codes 88/89/90/0 stop the loop by design).
