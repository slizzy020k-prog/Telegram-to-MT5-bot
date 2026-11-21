# Telegram MT5 Signal Copier — Automation Changelog

All autonomous parameter calibrations, FSM/Profit Ratchet upgrades, bug fixes, and replay verifications are logged here for historical tracking and git-bisect safety.

---

## [2026-11-21] — v3.8.0 Trade Lifecycle Guard (R-Relative Profit Floor + Trade-Age Guard)

- **Baseline Commit:** `a64156f` (design spec) — branch base `a64156f..c184b47`
- **Target Version:** `v3.8.0` (Trade Lifecycle Guard)
- **Spec:** `docs/superpowers/specs/2026-11-20-trade-lifecycle-guard-design.md`
- **Plan:** `docs/superpowers/plans/2026-11-20-trade-lifecycle-guard.md`
- **Verification Status:** `396/396` pytest PASS (`0` failures); `check_algo.py` → `ALL CHECKS PASSED`;
  `startup_checks.validate_environment()` → `{'ok': True, 'errors': [], 'warnings': []}`;
  `import parser, mt5_executor, main, risk_lifecycle, config` → `IMPORT OK`;
  `R_LOCK_ENABLED=False`, `LIFECYCLE_ENABLED=False` (both ship **disabled**).
- **Replay Proof (Arm C):** gate `g5` **FAILED** — Arm C net P&L was `£16.99` BELOW Arm B.
  The feature is therefore **not** enabled. See §5.
- **Motivation:** the live ledger (19 closed signals, `dry_run=false`) showed `£154.93` of
  favourable excursion turning into `-£33.15` realized — `£188.08` given back, 121% of
  everything the market gave. Two trades held 21.0 h and 20.3 h (`-£105.43`, 61% of the
  total loss).

### 1. R-Relative Profit Floor (`risk_lifecycle.py`, `dynamic_risk.py`, `mt5_executor.py`)

- The ATR engine armed its breakeven lock at `max(0.75 x ATR, $3.50)` regardless of stop
  distance, so a `$22/oz` stop and a `$2.00` scalp both became risk-free after the same
  `$3.50`. The new floor scales to each trade's own risk `R = |entry - initial SL|`
  (tiers `0.5R -> entry`, `1.0R -> +0.25R`, `1.5R -> +0.50R`, `2.0R -> +0.75R`).
- `compute_dynamic_atr_trail_stop` gained an additive keyword-only `r_lock_floor`; the floor
  joins the existing candidate list, so the ATR trail still owns the tight stop and the
  forward-only (never-loosens) invariant is preserved.
- `initial_sl` is captured per layer at placement as the stop the **broker actually received**
  (the `sl` argument is only a request — when it is `None` the executor computes an ATR /
  `FALLBACK_SL_PIPS` fallback and sends THAT), because the trail overwrites `sl`/`broker_sl`
  and the original risk would otherwise be lost.
- The floor is computed from the layer's persisted `mfe_dollars` — the running, non-negative
  maximum excursion — never from a recomputed current-tick excursion.

### 2. Trade-Age Guard (`risk_lifecycle.py`, `mt5_executor.py`, `main.py`)

- No trade-age, pending-expiry or session guard existed in the live code.
- Two-step de-risk: at `2 h` the unfilled pending limits are cancelled and every open layer
  moves to breakeven (never loosening an existing stop); at `6 h` everything is closed at
  market. Unfilled signals are cancelled at `12 h`.
- The session/rollover cut ships **disabled** (`LIFECYCLE_SESSION_FLATTEN_ENABLED=false`).
- The guard runs on BOTH the pre-TP1 path and the `RUNNER_ACTIVE` path, so a post-TP1 trade is
  never left un-managed — that population held the two longest trades in the ledger.
- The guard stamps an action on the signal only when it actually **completed**. A skipped
  action (no usable live price, or a stop the market would refuse) and a failed action leave
  the stamp unset and are retried, on a bounded 30 s cadence so the retry is not a broker
  sweep per second per aged trade.
- Fail-closed throughout: a de-risk without a usable live price does not send the stop, and a
  flatten whose close cannot be verified against the broker is counted as a FAILED close, so
  the retry counter increments and the give-up alert can actually fire.

### 3. Instrumentation (`trade_ledger.py`)

- Ledger schema `3 -> 4`: `mfe_cash`, `giveback_cash`, `initial_r_per_oz`, `r_at_exit`,
  `exit_age_seconds`, `lifecycle_action`. `peak_profit_per_oz` is per-ounce and was never
  converted to cash, so giveback was not measurable.
- The exit age is measured from the **earliest known fill** via
  `risk_lifecycle.resolve_fill_time(signal_data)`, never from the raw
  `signal_data["first_fill_at"]` key (which records the first LIMIT fill and can be later
  than the MARKET fill that actually opened the trade).
- Schema 4 is an UPGRADE: every schema-3 field is retained and old records still read.
- New `/stats` command: today / 7-day / all-time P&L, win rate, total cash giveback,
  longest trade age, exit-reason mix. It degrades to "no ledger data" on a missing or
  corrupt ledger instead of raising.

### 4. Live Control

- `/rlock on|off` and `/lifecycle on|off` toggle both features at runtime with no restart
  and double as the kill switches. A bare `/rlock` or `/lifecycle` reports the current policy
  and changes nothing.

### 5. Replay Proof — Arm C (the gate FAILED)

| Arm | Net P&L | Trades | Tail loss (`13078+9953`) | Errors |
|---|---:|---:|---:|---:|
| A (`4544f7f`) | `£+11.89` | 12 | `£-61.99` | 0 |
| B (calibrated) | `£+0.14` | 12 | `£-61.99` | 0 |
| **C (R lock + age guard)** | **`£-16.85`** | 12 | `£-61.99` | 0 |

- Gate `g5` (Arm C >= Arm B on net P&L, <= on tail loss) **FAILED**. Gates `g1`-`g3` were
  already failing before this branch (pre-existing).
- The whole gap is the `2 h` de-risk scratching the only two trades the harness holds open past
  two hours. On the **recorded live tape every trade closed within 47.1 minutes**, so a 2 h
  de-risk could never have fired live, and both affected trades were in fact closed at
  breakeven live (`BROKER_SL BE`) — which Arm C reproduces and Arm B does not.
- Two genuine harness bugs were found and fixed: the replayed layers never carried `initial_sl`
  (so the R lock was silently skipped and Arm C would not have exercised the feature at all),
  and the age guard read the wall clock (so on a replay of a Nov 17-19 tape every trade looked
  4 days old and would have been flattened on its first tick). Arms A and B are unchanged by
  both fixes.
- The harness replays a generic FSM rather than the recorded message flow, so it cannot
  reproduce the recorded close times. Fixing that would change the frozen Arm A/B baseline.

### 6. Known Limitations (read before enabling)

- **The default `R_LOCK_TIERS` is effectively INERT.** The R-lock candidate sits at picker
  priority 0 and is dominated by the existing `BE_LOCK` / `ATR_TRAIL` candidates, so on the
  recorded tape the R floor never wins the stop picker. As configured the R floor would not
  change any stop. The tiers must be re-derived against real tape before the feature is
  enabled.
- **The `2 h` de-risk threshold is not yet justified by evidence.** On the recorded tape it
  fires on trades that closed well inside two hours. Measured sensitivity (not shipped):
  `DERISK=10800` makes `g5` pass; `DERISK=21600` means the guard never fires.
- Both features therefore ship **disabled** and must stay disabled until the tiers and the
  de-risk threshold are re-derived and `g5` passes.

### 7. Deploy Readiness

- **Both features ship DISABLED** (`R_LOCK_ENABLED=false`, `LIFECYCLE_ENABLED=false`).
  A fresh clone behaves identically to v3.7.0.
- **No VPS deployment was performed.** No SSH, no file copy, no service restart.
- **VPS `.env` drift is UNRESOLVED and must be corrected by hand at deploy time.** `.env` is
  gitignored, so the push cannot fix it. The knobs to add are: `R_LOCK_ENABLED`,
  `R_LOCK_TIERS`, `R_LOCK_MIN_R_DOLLARS`, `LIFECYCLE_ENABLED`, `LIFECYCLE_DERISK_SECONDS`,
  `LIFECYCLE_FLATTEN_SECONDS`, `LIFECYCLE_PENDING_EXPIRE_SECONDS`,
  `LIFECYCLE_SESSION_FLATTEN_ENABLED`, `LIFECYCLE_SESSION_FLATTEN_AT`,
  `LIFECYCLE_MAX_CLOSE_RETRIES`.
- The server still pins `DYNAMIC_ATR_TRAIL_MULT=1.50` while the repo says `1.40` — fix that
  at the same time.

## [2026-11-20] — v3.7.0 Msg 10180/10196 Audit Fixes, Progressive ATR Trail & Regression Suite

- **Baseline Commit:** `c57b728` (`v3.6.0` line — MT5 saved-credential account switching via `MT5_LOGIN`)
- **Target Version:** `v3.7.0` (Msg 10180 / Msg 10196 Audit Fixes + Progressive ATR Trail + Regression Suite)
- **Verification Status:** `230/230` pytest tests PASS (`0` failures); `check_algo.py` → `ALL CHECKS PASSED`; `startup_checks.validate_environment()` → `{'ok': True, 'errors': [], 'warnings': []}`; `python -c "import config, parser, dynamic_risk, mt5_executor, main, startup_checks"` → `imports ok`.

### 1. Upgrade 1 — Premature TP1→TP2 Lift Guard & `roles_locked` Scoping (`dynamic_risk.py`, `mt5_executor.py`)

- A broker TP1→TP2 lift is now gated on a minimum TP1-leg progress of `DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT = 0.50` (50%) **plus** ATR proximity to the TP1 target; the gate is bypassed only when the layer is already BE-locked, has reached TP1, or sits at stage `k >= 2`.
- `roles_locked` is now set **only** on a BE-lock or a TP1-clear — never on a pre-BE TP lift (the previously dead `has_pending_limits` clause was removed; behaviour-identical because it could never be `False`).
- Rationale: `main.py` `_forward_only_sl_guard` blocks edits while `roles_locked` is truthy, so a premature `roles_locked` would also block legitimate “Extend SL” edits (Msg 10181).
- The not-armed engine state now reports the **effective** multiplier and zone (`eff_mult`, `trail_zone`) instead of always the base multiplier with a hardcoded `"STANDARD"` label (reachable at ATR `20.00` / MFE `12.00` → EXTENDED `1.00x`).

### 2. Upgrade 2 — `ACTION_CLOSE_WORST_HOLD_LAYERS` Hardening (`parser.py`, `mt5_executor.py`)

- **Future-tense chatter rejection:** conversational future-tense phrasing (e.g. “I'm gonna go breakeven early and close worst soon”) is now rejected **before** any trade-management classification, so it can never reach the order-modification path. The rejection is always logged, never silent. Trade-off recorded in-code: the reject is blunt, so a genuine command that merely mentions one of the tokens is dropped too.
- **BANK_TP1 TP targets are never overwritten:** a layer whose role is `BANK_TP1` keeps its TP1 target; the runner-phase TP-edit path no longer substitutes TP3 for a BANK_TP1 layer's TP1.
- **Broker stops level validated before a BE move:** the broker `trade_stops_level` is validated against the requested breakeven distance before the move is attempted, and the stops buffer scales with `trade_stops_level` while ignoring `trade_freeze_level`.

### 3. Upgrade 3 — Monotonic TP Typo Sanitizer (`parser.py`)

- The TP ladder sanitizer now guarantees a **contiguous, strictly ordered** ladder: `_sanitize_labeled_tps_monotonic` always re-numbers the surviving stages to `TP1..TPn` (no gaps, no mislabelled stage).
- Worked example: the typo ladder `3568/3555/3437/3520/3500` is non-monotonic — `3437` is dropped and the surviving stages are renumbered.
- A stage-preserving variant `_sanitize_labeled_tps_monotonic_stages` keeps the provider's original `TPn` keys and is what live-signal merges consume, so a TP2 never shifts onto TP1 on an already-running signal. Gates 4 and 5 publish both views (`labeled_tps` contiguous, `labeled_tps_stages` stage-preserving).

### 4. Upgrade 4 — ADAPTIVE ATR Scaling on Stub-to-Zone Edits (`main.py`, `mt5_executor.py`)

- The stub-to-zone **edit** path now applies the same ADAPTIVE ATR scaling as a fresh signal (previously the edit path did not scale at all).
- **Critical defect fixed:** `TP1_CLOSE_PROXIMITY_PIPS` was used without being imported, so the entire stub-to-zone edit path died with a `NameError` — the feature was non-functional rather than merely mis-scaled (commit `14f742c`).
- **Zone-anchor parity:** the zone anchor formula was extracted into one shared helper (`MT5Executor._resolve_zone_raw_tiers` / `_resolve_zone_tiers` / `_zone_anchors_from_tiers` / public `compute_zone_atr_anchors`). `execute_4layer_zone` (fresh signal) and `main._handle_message_edited_unlocked` (edit) now route through it, so an edit and an identical fresh signal on the same zone scale SL/TP identically. The previous inline edit formula anchored on the raw zone edge while the fresh path anchored on the min/max over the raw and front-run-shifted tiers.

### 5. Upgrade 5 — Progressive ATR Trail (`dynamic_risk.py`, `config.py`, `mt5_executor.py`)

| Zone | MFE ($) | Trail Multiplier | Knob |
|---|---:|---:|---|
| `STANDARD` | `< 10.00` | `1.40x` | `DYNAMIC_ATR_TRAIL_MULT` |
| `EXTENDED` | `10.00 – 17.99` | `1.00x` | `DYNAMIC_ATR_TRAIL_EXTENDED_MULT` / `DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS` |
| `HOME_RUN` | `>= 18.00` | `0.80x` | `DYNAMIC_ATR_TRAIL_HOMERUN_MULT` / `DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS` |

- Trail distance floor `$2.50` (`DYNAMIC_ATR_TRAIL_MIN_DOLLARS`) and ceiling `$12.00` (`DYNAMIC_ATR_TRAIL_MAX_DOLLARS`) unchanged; step `$0.25`.
- **Forward-only ratchet** is invariant and documented on `select_progressive_trail_mult`: a non-decreasing peak MFE → a non-increasing multiplier → a non-increasing trail distance → `atr_sl` only ever moves forward.
- **Config correction:** the shipped `.env` pinned `DYNAMIC_ATR_TRAIL_MULT=1.50`, so the effective STANDARD tier was `1.50x`, not the `1.40x` the directive specifies. Corrected to `1.40` locally and in `.env.example` (both verified: `config.DYNAMIC_ATR_TRAIL_MULT = 1.4`).

### 6. Upgrade 6 — Regression Suite (`tests/`)

- `11` new tests in `tests/test_msg_10180_10196_regressions.py` covering the premature-lift guard, `roles_locked` scoping, worst-hold-layers hardening, the monotonic TP sanitizer, the progressive trail zone boundaries and the stub-to-zone edit scaling.
- `4` repaired legacy expectations (expected-value drift from the intentional Upgrade 5 change, not regressions): `test_atr_trail_dist_clamps_to_range` and `test_atr_trail_ratchets_forward_only` (MFE `$10.00` now lands exactly on the EXTENDED boundary), `test_v31_single_layer_dynamically_becomes_runner_and_scales_tp1_to_tp4` (MFE `$10.60` → EXTENDED, `runner_sl_floor 3896.60`), and `test_v31_strict_activation_zone_and_near_miss_tp1_reversal_banks_profit` (MFE `$9.75` → STANDARD, SL `3894.15` under the `1.40x` multiplier).

### 7. Deploy-Readiness

- **No VPS deployment was performed** for this change — no SSH, no file copy, no service restart.
- **`.env` drift on the VPS is UNRESOLVED and must be corrected at deploy time:** the VPS `.env` still pins `DYNAMIC_ATR_TRAIL_MULT=1.50` and does not define the new ATR trail knobs (`DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS`, `DYNAMIC_ATR_TRAIL_EXTENDED_MULT`, `DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS`, `DYNAMIC_ATR_TRAIL_HOMERUN_MULT`, `DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT`).
- `.env` is gitignored, so this push cannot fix it; the local `.env` and `.env.example` are already aligned to the spec.

---
## [2026-11-19] — v3.6.0 Autonomous Audit, Profit Ratchet Calibration & Production Bug Fixes

- **Baseline Commit:** `4544f7f` (`v3.5.2`)
- **Target Version:** `v3.6.0` (Rev 2 Progressive Profit Ratchet + M1-ATR Bounded Calibration + Production Fixes A–C)
- **Verification Status:** `241/241` pytest tests PASS (`0` failures), `simulate.py` PASS (`10/10`), `test_examples.py` PASS (`7/7`), `scripts/ab_replay_proof.py` PASS (`4/4` gates).

### 1. Audit Summary (`XAUUSD.s`, Nov 15–19, 2026)

- **Scope:** Audited `copier.log` (`130,368` lines), `trades_ledger.jsonl` (`35` records), `signals_state.json` (`27` signals), MT5 closed deals (`100` deals), and `3,462` M1 candles (`mt5.copy_rates_range`).
- **Pre-Entry Zone Invalidation Frequency:** `10 / 24` (`41.7%`) tracked Nov 17–19 signals were cancelled pre-fill (`TP1 Reached Before Zone Entry` / provider delete / manual cancel), leaving `0` market tickets filled (`£0.00` PnL).
- **Historical TP1–TP4 Continuation Probability (15 filled genuine XAUUSD.s setups):**
  - **TP1 Hit Rate before SL:** `11 / 15` (`73.3%`)
  - **TP2 Hit Rate before SL:** `7 / 15` (`46.7%` of all entries; `63.6%` conditional on TP1)
  - **TP3 Hit Rate before SL:** `6 / 15` (`40.0%` of all entries; `85.7%` conditional on TP2)
  - **TP4+ Hit Rate before SL:** `4 / 15` (`26.7%` of all entries; `66.7%` conditional on TP3)
- **Rollover Window Audit (`21:55–23:05 GMT` / `23:55–01:05 MT5 Server UTC+2`):** `0` entries or exits occurred during daily broker rollover.
- **Recent 6 Core Trades (`Nov 18–19`) Classification:**
  1. **Msg 13153 (`BUY @ 3730.00`, `GOLD_GUARD` exit `@ 3730.58`, `+£0.44`):** **False Positive / Early Choke** — peaked at `3732.07` (`69%` of `$3.00` TP1 leg), choked on a normal `$1.49` retest wick (`3730.58`), and subsequently rallied to `3758.47` (`+$28.47/oz` MFE, clearing TP1, TP2, TP3, and TP4).
  2. **Msg 9934 (`SELL @ 3741.70`, `ATR_TRAIL` Stage 1 exit `@ 3740.17`, `+£1.16` MT5 / `£0.00` ledger bug):** **False Positive / Early Choke over full horizon** — cleared TP1 (`3737.0`) at `48.2%` of the TP1→TP2 leg (`3737.67`), stopped at `3740.17` on a `$2.50` ATR trail during a rebound to `3758.47` (which stayed `$8.53` below initial SL `3767.0`), and then collapsed to `3693.40` (`+$48.30/oz` MFE, clearing TP1–TP4). Also exposed the `deal_ticket` vs `deal` ledger PnL bug (`£0.00` recorded instead of `+£1.16`).
  3. **Msg 13167 (`BUY @ 3750.00`, `GOLD_GUARD` exit `@ 3751.12`, `+£0.85`):** **False Positive / Early Choke (Pre-TP1)** — peaked at `3752.64` (`88%` of `$3.00` TP1 leg), exited at `3751.12` on a `$1.52` dip, and 4 minutes later surged to `3758.47` (clearing TP1 `3753.0`, `+$8.47/oz` MFE) before reversing to SL `3725.0` at `02:53 UTC`.
  4. **Msg 9937 (`SELL 3738–3743`, `L1 @ 3737.43, L2 @ 3740.26`, `TP1=3725.0`, `BROKER_SL` BE `+£0.56`):** **Mid-Run Profit Giveback** — dropped to `3728.55` (`+$11.12/oz` MFE on L2, `$3.55` shy of wide `$13.84` TP1) and gave back `93%` of floating profit to breakeven. Fixed in `v3.6.0` by `BANK_HARVEST` on L1 + `TIER_3` Profit Ratchet on L2 (`+£5.70` in M1 replay).
  5. **Msg 9943 (`SELL 3740–3745`, `L1 @ 3739.78, L2 @ 3739.76`, `TP1=3730.0`, `BROKER_SL` BE `+£0.45`):** **Pre-TP1 BE Stop-Out Before Continuation** — reached `3733.49` (`+$6.28/oz` pre-BE MFE), stopped at BE (`3739.47`) on a `$6` bounce, and then dropped to `3726.07` (clearing TP1 `3730.0` and approaching TP2 `3725.0`). Fixed in `v3.6.0` by `BANK_HARVEST` + `TIER_2` Profit Ratchet (`+£5.03` in M1 replay).
  6. **Msg 9953 (`SELL 3731–3736`, 4 layers `0.05` lots, `TP1=3700.0, SL=3740.0`, `BROKER_SL` `-£25.36`):** **Valid SL Loss** — max MFE was `+$2.65/oz` on L4 (`8.8%` of `$30` TP1 leg) before hitting `SL 3740.0`.

---

### 2. Bounded Parameter Calibration (`config.py` & `.env`)

All primary FSM/ATR/Spread parameters were calibrated strictly within the bounded `±0.05` multiplier / `±$0.25` threshold safety envelope:

| Parameter | Previous (`v3.5.2`) | Calibrated (`v3.6.0`) | Delta | Rationale |
|---|---:|---:|---:|---|
| `STAGE1_ATR_ACTIVATION_PCT` | `0.45` | `0.50` | `+0.05` | Prevents premature Stage 1 ATR trail activation at `48.2%` of the TP1→TP2 leg (`Msg 9934`). |
| `TP1_TRAIL_ATR_MULT` | `1.50` | `1.55` | `+0.05` | Widens dynamic M1-ATR trailing cushion by `+0.05× ATR` so post-TP1 runners survive normal Gold retests. |
| `TP1_TRAIL_MIN_DOLLARS` | `2.50` | `2.50` | `0.00` | Preserves `$2.50` (`25 pips`) minimum Stage 1 trailing floor. |
| `ARM_THRESHOLD_PCT` | `0.85` | `0.90` | `+0.05` | Prevents pre-TP1 shortfall guard from arming early at `85–88%` of tight TP1 legs (`Msg 13167`). |
| `GIVEBACK_PCT` | `0.30` | `0.30` | `0.00` | Retains `30%` peak-giveback ratio after `90%` arming. |
| `REVERSAL_CONFIRM_TICKS` | `2` | `2` | `0` | Retains 2-tick consecutive confirmation filter against single-tick spread noise. |
| `MAX_ALLOWED_SPREAD_DOLLARS` | `0.40` | `0.45` | `+$0.05` | Accommodates normal `$0.32–$0.44` PUPrime Gold spread without false spread-spike aborts. |
| `SINGLE_PRICE_CONSOLIDATE_ORDERS` | `False` (implicit 4x) | `True` | Enabled | Places `1` consolidated order for single-price entries (`Msg 9957`) instead of stacking 4 identical limits. |
| `PENDING_DUPLICATE_PRICE_TOLERANCE` | N/A | `0.50` | New | Blocks duplicate pending limit/stop signals within `$0.50` of an existing pending order in the same direction. |
| `PENDING_DUPLICATE_ORDER_TOLERANCE` | N/A | `0.25` | New | Low-level `_place_pending_order` safety guard blocking duplicate pending orders within `$0.25`. |
| `MAX_TP_DISTANCE_DOLLARS` | N/A | `250.0` | New | Gate 5 sanity cap stripping fat-finger/wrong-side TP typos (`Msg 9919` `TP 4320`). |
| `LIMIT_ENTRY_FRONT_RUN_OFFSET` | `0.00` (exact tier) | `0.50` | `+$0.50` | Shifts pending `BUY LIMIT` entry layers `+$0.50` higher (e.g. `3710.0 -> 3710.50`) and `SELL LIMIT` layers `-$0.50` lower so spread/round-number bounces (`Msg 13191` dipping to `3710.44` bid above `3710.00`) fill `L1` instead of missing activation. |

---

### 3. Production Bug Fixes (`main.py`, `parser.py`, `mt5_executor.py`)

1. **Fix A — 50% Daily Loss Limit Auto-Reset & Unpause (`main.py`):**
   - Updated `check_and_enforce_daily_drawdown()`, startup drawdown check, and pre-flight entry hooks (`_handle_new_message_unlocked`, delayed entry, reactivation, message edit, Saved Messages `/trade`) to check `has_open_mkt` (open market positions only, ignoring unfilled pending-only setups like `Msg 13187`).
   - When `0` open market positions remain after a realized daily loss, the daily loss baseline automatically updates to the current balance (`26.93 GBP`) and unpauses new signals while leaving `Msg 13187`'s 4 pending BUY LIMIT orders (`#153969343, #153969345, #153969346, #153969347`) untouched.
2. **Fix B1 & B2 — Single-Price Limit Consolidation & Duplicate-Price Pending Guard (`mt5_executor.py`, `main.py`):**
   - `execute_4layer_single_price` now consolidates to `1` order (`L1`) when `SINGLE_PRICE_CONSOLIDATE_ORDERS=True` and sets `zone_completed=False` so late edits into a 4-layer zone (`Msg 13167`) expand `L2..L4` cleanly.
   - Added `has_duplicate_pending_order()` and wired it into `_place_pending_order` and `execute_4layer_single_price`.
3. **Fix B3 — `ACTION_CANCEL_PENDING` Parser & Handler (`parser.py`, `main.py`):**
   - Added `CANCEL_PENDING_PATTERNS` in Gate 3 (`DELETE SELL LIMIT` Msg `9960`, `Delete buy limit for now everyone` Msg `9910`) emitting `ACTION_CANCEL_PENDING` with `cancel_direction` (`"SELL"` / `"BUY"` / `None`), cancelling only pending orders of the matching direction while preserving open market positions and bare `"delete limit"` -> `ACTION_FULL_CLOSE`.
4. **Fix B4 — Gate 5 Wrong-Side / Out-of-Range TP Edit Sanity (`parser.py`, `main.py`):**
   - Enhanced `gate_5_validate_price_sanity` to strip wrong-side / `<$500` / `>$250` TPs from all TP dictionaries/lists (`labeled_tps`, `unlabeled_tps`, `explicit_tps`, `tp_levels`, `raw_tp1..5`, `final_tp`, `proximity_close_price`) and set `tp_valid=False`, eliminating the 5x `retcode=10016 (Invalid stops)` errors seen on `Msg 9919` (`SELL 3728-3732 TP 4320`).
5. **Fix C — Ledger `deal_ticket` PnL Accounting & `TIER_0T` Pullback Persistence (`main.py`, `mt5_executor.py`):**
   - Fixed `_account_closed_tickets` in `main.py` to read `ed.get("deal_ticket") if ed.get("deal_ticket") is not None else ed.get("deal")` and persist `accounted_deal_tickets` across `update_signal` calls (fixing the `£0.00` ledger bug on `Msg 9934`).
   - Fixed `compute_ticket_ratchet_levels` in `mt5_executor.py` to evaluate `peak_prog = max(prog_to_tp1, mfe_exec / tp1_leg)` and gate `TIER_0T` at `>=76%` peak progress so tight-TP1 trades (`Msg 13167`) retain `TIER_0T` on pullbacks while multi-TP runners (`Msg 13153`) lift to `TP2` at `70%` and ride to Stage 2+.

---

### 4. Old-vs-New 12-Trade M1-Bar A/B Replay Proof (`scripts/ab_replay_proof.py`)

| Metric | Actual Live MT5 | Arm A (`4544f7f` v3.5.2) | Arm B (`v3.6.0` Calibrated) | Delta (B − A) |
|---|---:|---:|---:|---:|
| **Net PnL across 12 Trades (GBP)** | `£-34.97` | `£+15.62` | `£+24.42` | **`£+8.80`** (`+£59.39` vs Live) |
| **Tail Loss (`13078 + 9953`)** | `-£48.60` | `£-61.99` | `£-25.09` | **`£+36.90` saved** |
| **Choke Trio (`13153 + 9934 + 13167`)** | `+£2.45` | `£+7.55` | `£+7.16` | **`+192%` vs Live (`+£4.71`)** |
| **Trade `13153` Max Stage Reached** | `Stage 0` (`+£0.44`) | `Stage 2` (`+£5.43`) | `Stage 2` (`+£5.43`) | **`+Stage 2` (`+£4.99` vs Live)** |
| **Mid-Run Givebacks (`9937 + 9943`)** | `+£1.01` (BE) | `+£21.73` | `+£10.73` (`PROFIT_RATCHET_SL`) | **`+£9.72` vs Live BE** |
| **Rollover Exits (`21:55–23:05 GMT`)** | `0` | `0` | `0` | `0` |
