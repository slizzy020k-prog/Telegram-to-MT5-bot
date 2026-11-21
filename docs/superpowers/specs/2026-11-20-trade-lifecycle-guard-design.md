# Trade Lifecycle Guard — Design Spec

**Version:** v3.8.0 (design)
**Date:** 2026-11-20
**Status:** Approved for planning — no code written yet
**Repo:** `telegram-mt5-copier` @ `main`

---

## 1. Problem Statement & Evidence

The bot is a single-symbol (`XAUUSD.s`) Telegram→MT5 copier with a staged exit engine. Its own
ledger (`trades_ledger.jsonl`, 19 live closed signals, `dry_run=false`, GBP) shows the edge leaking
in two places.

### 1.1 Ledger snapshot

Totals: **−£172.46 realized**.

| Signal | Exit code | MFE in favour (cash) | Realized | Giveback | Held |
|---|---|---:|---:|---:|---:|
| SELL 11-19 06:19 | `BROKER_SL` | £55.60 | +£0.56 | 99% | 3.0 h |
| SELL 11-19 07:02 | `BROKER_SL` | £30.90 | +£0.45 | 99% | 2.3 h |
| SELL 11-19 07:52 | `BROKER_SL` | £13.25 | −£25.36 | 291% | 0.3 h |
| BUY 11-19 14:00 | `PROFIT_RATCHET_SL` | £6.95 | −£12.22 | 276% | 1.6 h |
| SELL 11-18 23:39 | `ATR_TRAIL` | £20.15 | £0.00 | 100% | 0.4 h |
| BUY 11-18 00:45 | `GOLD_GUARD` | £13.20 | +£0.85 | 94% | 0.3 h |
| BUY 11-17 20:10 | manual (MT5 app) | — | −£82.19 | — | **21.0 h** |
| BUY 11-17 20:55 | manual (MT5 app) | — | −£23.24 | — | **20.3 h** |

Of the signals with excursion data: **£154.93 earned in MFE → −£33.15 realized; £188.08 given
back — 121% of everything the market gave.**

Two trades held 21.0 h and 20.3 h (−£105.43, **61% of the total loss**); seven more sat 42.8–81.7 h
until one mass close at `2026-11-19T09:18:51` swept them.

### 1.2 What already exists (do not rebuild)

`dynamic_risk.compute_dynamic_atr_trail_stop` implements a BE lock and a forward-only ATR trail,
and `main.py:4342` runs it for **every** signal with open market tickets — i.e. pre-`TP1` as well:

- BE lock armed at `MFE ≥ max(0.75 × ATR, $3.50)` → `SL = entry ± $0.50`
- ATR trail armed at `MFE ≥ 1.00 × ATR` → `SL = peak − clamp(ATR × mult, $2.50, $12.00)`
- Milestone floor from cleared `TPn` stages; candidate picker guarantees forward-only.

The ledger trades date from 2026-11-17 → 2026-11-19, which is the **pre-v3.5.2** engine.
`AUTOMATION_CHANGELOG.md:127-136` already contains the A/B replay proof showing that rewrite moving
Live `£-34.97` → Arm B `£+24.42` over 12 trades.

### 1.3 The two surviving gaps

**Gap A — protection thresholds are ATR-absolute, not risk-relative.**
A $22.00/oz stop and a $2.00 scalp both arm BE at `max(0.75 × ATR, $3.50)`. The wide trade risks
$22 to make $3.50 risk-free — a 6:1 payout ratio against the account. The `-£82.19` trade is
exactly this shape.

**Gap B — there is no time stop at all.**
A repository-wide search for `max_age`, `session_end`, `rollover`, `trade_age`, `expiry`, `stale`
finds **no trade-age or pending-expiry logic in the live code**. `CATCHUP_MARKET_MAX_AGE_SECONDS`
governs *new* signal catch-up, not open trades. Rollover (`21:55–23:05 UTC`) exists only as an
*analysis flag* in `scripts/ab_replay_proof.py`, never as engine behaviour. The 21 h and 20.3 h
trades are reachable with the current code.

---

## 2. Goals

| # | Goal | Success measure |
|---|---|---|
| G1 | Make the profit-protection floor scale with each trade's own risk (R), not with ATR | Arm C ≥ Arm B on net P&L in the replay proof |
| G2 | Stop trades from dying after 20+ hours of unattended exposure | No open signal older than the hard age limit at any observation point |
| G3 | Make every exit attributable and every giveback measurable | Ledger records cash MFE, cash giveback, R and age for every close |
| G4 | Ship with zero behaviour change until explicitly enabled | Both features default to disabled |

## 3. Non-Goals

- No new runtime dependencies.
- **No VPS deployment** (standing directive — deployment is out of scope for this and any follow-on work).
- No changes to entry, parsing, position sizing, or the TP ladder.
- No re-tuning of the v3.7.0 ATR trail multipliers — the R floor sits *beneath* the trail, not in place of it.
- No multi-symbol support, no news calendar, no web dashboard.
- No session/rollover flatten by default (decided 2026-11-20: the 6 h age flatten is the backstop).

---

## 4. Architecture

### 4.1 New module: `risk_lifecycle.py`

Pure, side-effect-free, no MT5 import — same discipline as `risk_controls.py` and `dynamic_risk.py`.
All behaviour is decided here; the executor only carries it out.

```python
@dataclass(frozen=True)
class LifecyclePolicy:
    r_lock_enabled: bool                 # default False
    r_lock_tiers: Tuple[Tuple[float, float], ...]   # ((0.5, 0.00), (1.0, 0.25), (1.5, 0.50), (2.0, 0.75))
    r_lock_min_r_dollars: float          # default 0.50
    lifecycle_enabled: bool              # default False
    derisk_after_seconds: int            # default 7200   (2 h)
    flatten_after_seconds: int           # default 21600  (6 h)
    pending_expire_seconds: int          # default 43200  (12 h)
    session_flatten_enabled: bool        # default False
    session_flatten_at: str              # "23:30" broker server time
    max_close_retries: int               # default 3
```

### 4.2 Public API

```python
def read_lifecycle_policy(cfg_mod: Any) -> LifecyclePolicy: ...

def compute_initial_r(entry_price: float, initial_sl: float) -> Optional[float]:
    """R per ounce = |entry - initial SL|. None when either input is invalid or R <= 0."""

def compute_r_lock_floor(
    direction: str,
    entry_price: float,
    initial_sl: float,
    mfe_dollars: float,
    live_price: float,
    policy: LifecyclePolicy,
    digits: int = 2,
) -> Optional[float]:
    """SL price for the reached R tier, or None when no valid floor exists."""

def evaluate_trade_age(
    signal_data: Dict[str, Any],
    now_utc: datetime,
    policy: LifecyclePolicy,
    server_offset_seconds: int = 0,
) -> Dict[str, Any]:
    """-> {"action": "NONE"|"DERISK"|"FLATTEN"|"EXPIRE_PENDING"|"SESSION_FLATTEN", "reason": str,
           "signal_age_seconds": int, "fill_age_seconds": int|None, "server_time": str}"""

def apply_lifecycle_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]:
    """Saved Messages `/lifecycle on|off|status`."""

def apply_rlock_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]:
    """Saved Messages `/rlock on|off` (status is folded into `/rlock` with no argument)."""
```

`compute_r_lock_floor` semantics:

- `R = |entry − initial_sl|`; `None` if `R <= 0` or `R < policy.r_lock_min_r_dollars`.
- The active fraction is the **greatest lock fraction among the tiers whose `r_threshold × R ≤ mfe_dollars`**. Taking the greatest reached fraction keeps the floor order-independent and monotone — the lock can never loosen as the excursion grows, even if a caller builds `LifecyclePolicy` directly with unsorted tiers. For the default ladder this is exactly "the highest threshold reached".
- Floor price = `entry + sign × (lock_fraction × R)`, rounded to `digits`.
- `None` when no tier is reached (MFE below the first threshold), when the direction is not `BUY`/`SELL`, or **when the floor is not valid against the current market** — for a BUY the floor must stay strictly below `live_price`, for a SELL strictly above it. The floor is derived from the peak excursion, so a retracement can carry price back past it; a stop on the wrong side of the market would be rejected by the broker with retcode `10016`. The consumer's stops-buffer clamp is a second line of defence, not the only one.

### 4.3 Config knobs (all new, all default to safe)

| Knob | Default | Meaning |
|---|---|---|
| `R_LOCK_ENABLED` | `false` | Master switch for the R-relative floor |
| `R_LOCK_TIERS` | `0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75` | `r_threshold:lock_fraction` pairs |
| `R_LOCK_MIN_R_DOLLARS` | `0.50` | Ignore trades whose initial risk per oz is below this |
| `LIFECYCLE_ENABLED` | `false` | Master switch for the age guard |
| `LIFECYCLE_DERISK_SECONDS` | `7200` | Fill age at which pending is cancelled and open layers move to BE |
| `LIFECYCLE_FLATTEN_SECONDS` | `21600` | Fill age at which everything is closed at market |
| `LIFECYCLE_PENDING_EXPIRE_SECONDS` | `43200` | Signal age at which unfilled pending limits are cancelled |
| `LIFECYCLE_SESSION_FLATTEN_ENABLED` | `false` | Session cut (off by default) |
| `LIFECYCLE_SESSION_FLATTEN_AT` | `23:30` | Broker server time-of-day for the session cut |
| `LIFECYCLE_MAX_CLOSE_RETRIES` | `3` | Retries before the guard gives up and alerts |

Parsed in `config.py` at import; validated in `startup_checks.py` (ranges: seconds `0..604800`,
tiers well-formed and ascending, `flatten > derisk`).

### 4.4 Integration — the R floor

`dynamic_risk.compute_dynamic_atr_trail_stop` gains **one additive keyword-only argument**:

```python
def compute_dynamic_atr_trail_stop(
    ..., apply_milestone_floor: bool = True,
    r_lock_floor: Optional[float] = None,      # NEW — default None keeps every existing caller identical
) -> Dict[str, Any]:
```

When `r_lock_floor` is not `None`, it is added to the existing `candidates` list
(`dynamic_risk.py:529-561`) as `(0, "R_LOCK", r_lock_floor)`. Existing candidates shift to
indices 1–3. The tie-break order becomes:

```
R_LOCK  >  MILESTONE_FLOOR  >  ATR_TRAIL  >  BE_LOCK
```

(the picker selects the tightest stop; ties break on the lowest index). The returned dict gains
`"r_lock_floor"` so callers can log it. The forward-only invariant is preserved: the R floor can
only ever be *tighter*, and `should_update` still requires a ≥ `$0.25` improvement over the
current SL.

`mt5_executor.evaluate_dynamic_atr_engine` computes the floor per layer before calling
`compute_dynamic_atr_trail_stop`:

```
mfe        = sign × (peak_price − entry_price)
r_lock_floor = compute_r_lock_floor(direction, entry_price, initial_sl, mfe, policy)
```

`initial_sl` comes from the new per-layer state key `lv["initial_sl"]`, captured once at order
placement. **If it is absent** (a signal opened before the upgrade), the R lock is skipped for
that layer and the skip is logged once — the ATR trail and the age guard continue unaffected.
The current `lv["sl"]` is never used as a fallback, because the trail overwrites it and would
make R ≈ 0.

### 4.5 Integration — the age guard

New per-signal state keys (all additive): `first_fill_at`, `lifecycle_derisk_at`,
`lifecycle_flatten_at`, `lifecycle_pending_expired_at`, `lifecycle_retry_count`.

| Clock | Action | Effect |
|---|---|---|
| Fill age ≥ `LIFECYCLE_DERISK_SECONDS` | `DERISK` | Cancel unfilled pending limits for the signal; move every open layer to `entry ± DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS` (default `$0.50`, i.e. BE) — only when that is *tighter* than the current SL |
| Fill age ≥ `LIFECYCLE_FLATTEN_SECONDS` | `FLATTEN` | Close all open positions at market; cancel all pending limits; mark the signal `CLOSED` |
| Signal age ≥ `LIFECYCLE_PENDING_EXPIRE_SECONDS`, nothing filled | `EXPIRE_PENDING` | Cancel pending limits; mark the signal `CLOSED` |
| Broker server time-of-day ≥ `LIFECYCLE_SESSION_FLATTEN_AT` | `SESSION_FLATTEN` | *(disabled by default)* close all open positions at market; fires at most once per broker day. Requires at least one **open position** — it never fires on a pending-only signal |

Precedence when several conditions hold: `SESSION_FLATTEN` > `FLATTEN` > `DERISK` > `EXPIRE_PENDING`.
`EXPIRE_PENDING` only applies when there are no market tickets, so it can never collide with the
fill-age branches. `SESSION_FLATTEN` requires `LIFECYCLE_SESSION_FLATTEN_ENABLED`; when it fires it
stamps `lifecycle_flatten_at` so it cannot fire twice on the same broker day.

Clocks:

- **fill age** = `now_utc − signal_data["first_fill_at"]` (new stamp set on first fill)
- **signal age** = `now_utc − signal_data["timestamp"]` (the existing signal receipt time)

**Server time** is derived from the existing `get_server_time_offset_seconds()` /
`get_server_date_str()` helpers so the session cut aligns with the broker's trading day, matching
the daily-P&L window logic.

### 4.6 New executor methods (`mt5_executor.py`)

```python
def apply_lifecycle_derisk(self, signal_data) -> Dict[str, Any]:
    """Cancel unfilled pending orders; move open layers to BE (never loosening)."""

def apply_lifecycle_flatten(self, signal_data) -> Dict[str, Any]:
    """Close all open positions at market + cancel pending; mark the signal CLOSED."""

def apply_lifecycle_pending_expire(self, signal_data) -> Dict[str, Any]:
    """Cancel pending limits for a signal that never filled; mark it CLOSED."""
```

Each returns a structured result (`{"moved": [...], "skipped": [...], "closed": [...],
"failed": [...], "pending_cancelled": [...]}`) that is logged and summarised to Saved Messages.

Reuse, not re-implement:

- the **validated close path** already used by `/closeall` (retcode checked, partial fills tolerated);
- the **stop-change validation** already used by the trail path —
  `is_within_freeze_level`, `stops_buffer` distance from live price, and the broker
  `trade_stops_level` clamp.

### 4.7 Watcher integration (`main.py`)

The existing 1-second watcher gains one branch per active signal, evaluated **before** the
existing A/B/C branches so an aged trade is de-risked first:

```python
age_res = evaluate_trade_age(sig, now_utc, lifecycle_policy, server_offset)
if age_res["action"] != "NONE":
    await _mt5_call(executor.apply_lifecycle_action, sig, age_res["action"])
```

`apply_lifecycle_action` dispatches to the three methods above. Each action stamps its state key
so a repeat tick is a no-op.

### 4.8 Ledger instrumentation (`trade_ledger.py`)

New fields on every closed-signal record:

| Field | Type | Meaning |
|---|---|---|
| `mfe_cash` | `float \| null` | `peak_profit_per_oz × total_lots × 100` — the existing `max_favorable_excursion` is **per ounce** and is never converted to cash anywhere today |
| `giveback_cash` | `float \| null` | `mfe_cash − realized_pnl` |
| `initial_r_per_oz` | `float \| null` | `|entry − initial SL|` for the signal |
| `r_at_exit` | `float \| null` | `mfe_cash / (initial_r_per_oz × total_lots × 100)` |
| `exit_age_seconds` | `int \| null` | Fill age at close |
| `lifecycle_action` | `str \| null` | `DERISK` / `FLATTEN` / `EXPIRE_PENDING` / `SESSION_FLATTEN` if the guard fired |

`schema_version` bumps `3` → `4`. Older records are read without the new fields (all consumers
must tolerate absence).

### 4.9 `/stats` command

New Saved Messages command aggregating `trades_ledger.jsonl`:

- today and 7-day realized P&L, wins / losses, win rate
- exit-reason mix (counts by `exit_reason_code`)
- **total giveback in £** (the headline metric for this feature)
- longest trade age observed and count of trades closed by the lifecycle guard

### 4.10 Telegram commands

| Command | Effect |
|---|---|
| `/rlock on\|off` | Toggle `R_LOCK_ENABLED` at runtime |
| `/rlock` | Show the active tiers and the current policy |
| `/lifecycle on\|off` | Toggle `LIFECYCLE_ENABLED` at runtime |
| `/lifecycle` | Show thresholds, next action, and per-signal ages |
| `/stats` | Ledger summary (4.9) |

All four are instant, no restart, and are the kill switches for the live rollout.

---

## 5. Data Flow

```
Telegram signal ──► parser ──► main._handle_new_message_unlocked
                                    │
                                    ▼
                        mt5_executor.execute_4layer_zone / single_price
                                    │
                    ┌───────────────┴────────────────┐
                    ▼                                ▼
        layer state gains initial_sl        signal gains timestamp
                                                (+ first_fill_at on first fill)
                                    │
                    1-second watcher (main.tp1_proximity_watcher_task)
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
   evaluate_trade_age     evaluate_dynamic_atr_engine   (existing A/B/C)
              │                     │
              │            compute_r_lock_floor  ──► r_lock_floor
              │                     │
              │            compute_dynamic_atr_trail_stop(r_lock_floor=…)
              │                     │
              │            candidates: R_LOCK / MILESTONE_FLOOR / ATR_TRAIL / BE_LOCK
              │                     │
              │            modify_position_sltp  (validated: never loosens, never 10016)
              ▼
   apply_lifecycle_derisk / _flatten / _pending_expire
              │
              ▼
   trade_ledger.append_closed_signal(...)  ──►  trades_ledger.jsonl  ──►  /stats
```

---

## 6. Safety Invariants (hard rules — a violation is a defect, not a tuning issue)

1. **Never loosen a broker SL or TP.** The R floor only raises the floor; the candidate picker
   already guarantees forward-only. The age guard's BE move only applies when strictly tighter.
2. **Never emit retcode `10016` (Invalid stops).** Every SL/TP change goes through the existing
   `is_within_freeze_level` + `stops_buffer` + `trade_stops_level` validation used by the trail
   path, and every close goes through the validated market-close path used by `/closeall`.
3. **Idempotent.** `DERISK` / `FLATTEN` / `EXPIRE_PENDING` each stamp a state key. A repeat tick
   is a no-op and never double-closes.
4. **Never fire on a signal with zero open positions**, except the `EXPIRE_PENDING` path (which by
   definition has pending orders and no fills).
5. **No change to entry, sizing, TP ladder, or the v3.7.0 ATR trail multipliers.**
6. **Disabled by default.** `R_LOCK_ENABLED=false` and `LIFECYCLE_ENABLED=false` → a fresh clone
   behaves exactly like v3.7.0.
7. **The age guard never raises risk.** It only cancels, moves to BE, or closes.

---

## 7. Error Handling

| Failure | Behaviour |
|---|---|
| Exception inside the lifecycle branch | Caught, logged, **never** propagates out of the 1-second watcher; the rest of the tick continues |
| Market close returns a failing retcode | Position left open, `lifecycle_retry_count` incremented, retried next tick; after `LIFECYCLE_MAX_CLOSE_RETRIES` (3) the guard stops retrying, sends a Saved-Messages alert, and leaves the position to its broker SL (the safe fallback) |
| `initial_sl` missing on a layer | R lock skipped for that layer, logged once per signal; age guard unaffected |
| `first_fill_at` missing | Fill age falls back to signal age; logged once |
| Broker `trade_stops_level` rejects the BE move | Move skipped, layer left at its existing (already valid) SL, logged |
| Ledger file missing / corrupt | `/stats` reports "no ledger data" instead of raising |

---

## 8. Testing

### 8.1 Unit — `risk_lifecycle.py` (pure)

- `compute_initial_r`: BUY and SELL symmetry; `R <= 0` → `None`; invalid inputs → `None`.
- `compute_r_lock_floor` boundaries: MFE `0.49R` → `None`; `0.50R` → BE; `1.00R` → `entry + 0.25R`;
  `1.50R` → `entry + 0.50R`; `2.00R` → `entry + 0.75R`; `2.01R` → unchanged (no tier above).
- Same ladder on a tight stop ($2.00) and a wide stop ($22.00) — the *fraction* is identical, the
  *dollar* lock scales.
- BUY places the floor above entry, SELL below.
- `R < r_lock_min_r_dollars` → `None`.
- `evaluate_trade_age`: NONE / DERISK / FLATTEN / EXPIRE_PENDING selection; precedence
  `FLATTEN > DERISK > EXPIRE_PENDING`; `EXPIRE_PENDING` only with pending and no market tickets;
  session cut respects the server-time offset.
- `apply_lifecycle_command`: `on` / `off` / status / invalid input.

### 8.2 Integration — dry-run `MT5Executor`

- The broker SL never loosens across a simulated price path with the R floor active.
- `stop_source` attribution: `R_LOCK` wins the tie against `MILESTONE_FLOOR` / `ATR_TRAIL` /
  `BE_LOCK`.
- Age guard: de-risk at 2 h cancels pending and moves to BE; flatten at 6 h closes everything;
  pending expiry at 12 h cancels; three consecutive ticks produce **one** action each.
- Session cut fires at most once per broker day (disabled by default — tested with the flag on).
- No `10016` retcode is produced in any scenario.

### 8.3 Regression

- The full existing suite (**241 tests**) must stay green.
- The `stop_source` tie-break change is the main regression risk; it gets a dedicated test and an
  explicit review of every existing `active_stop_source` assertion.

### 8.4 Replay proof — the acceptance evidence

`scripts/ab_replay_proof.py` gains an **Arm C** = Arm B + R-lock + age guard, run over the same
12 recorded trades. Acceptance:

- Arm C ≥ Arm B on net P&L, **and** Arm C ≤ Arm B on tail loss;
- no replay errors, no `unflagged_rollover_exits`;
- the harness's existing invariant checks still pass for Arms A and B (unchanged behaviour).

This runs **before** either feature is enabled live.

---

## 9. Rollout

1. **Ship disabled.** `R_LOCK_ENABLED=false`, `LIFECYCLE_ENABLED=false`. Tests + Arm C replay
   result included in the same commit.
2. **Validate.** Review the Arm C replay output. If it does not beat Arm B, adjust the tier
   thresholds and re-run — do not enable.
3. **Enable live** from Saved Messages: `/rlock on`, then `/lifecycle on`. Instant, no restart.
4. **Kill switch** at any time: `/rlock off`, `/lifecycle off`. Instant, no restart.
5. **VPS `.env` drift.** `.env` is gitignored, so the new knobs must be added **by hand** on the
   server at deploy time. This is the same failure that left `DYNAMIC_ATR_TRAIL_MULT=1.50` on the
   VPS while the repo said `1.40`. Document the exact knob list in the deploy note.
6. **Doc refresh — a separate change wave, explicitly outside this spec's implementation scope.**
   It is listed here only so it is not lost:
   - `README.md` still documents the Stage-0 `GOLD_GUARD` reversal guard, which was deliberately
     decommissioned (`tests/test_dynamic_risk_management.py:367-389` asserts
     `GOLD_REVERSAL_GUARD_ENABLED`, `STAGE0_PROFIT_LOCK_ENABLED`, `PROFIT_RATCHET_ENABLED` and
     `evaluate_profit_ratchet_tick` no longer exist).
   - `AI_HANDOVER.md` still says v2.2.2 / 3-layer / 72 tests against a v3.7.0 / 4-layer / 241-test
     reality.
   An operator reading either document is being told about protections that do not exist. This is
   documentation-only and carries no code risk, so it ships as its own commit **after** the
   lifecycle guard is validated.

---

## 10. Acceptance Criteria

| # | Criterion |
|---|---|
| A1 | `risk_lifecycle.py` exists, is pure, and has no MT5 import |
| A2 | `compute_dynamic_atr_trail_stop` gains only the additive `r_lock_floor` keyword; every existing caller is unaffected |
| A3 | With both features disabled, the bot's behaviour is identical to v3.7.0 |
| A4 | The broker SL never loosens and no `10016` retcode is emitted in any new path |
| A5 | Each lifecycle action is idempotent across repeated ticks |
| A6 | No signal can remain open past `LIFECYCLE_FLATTEN_SECONDS` once the guard is enabled |
| A7 | Every closed signal records cash MFE, cash giveback, R at exit and exit age |
| A8 | The full test suite passes with **0** failures |
| A9 | Arm C ≥ Arm B on net P&L and ≤ Arm B on tail loss in the replay proof |
| A10 | `/rlock` and `/lifecycle` toggle the features instantly, with no restart |
| A11 | Nothing is deployed to the VPS as part of this work |

---

## 11. Known Risks

| Risk | Mitigation |
|---|---|
| R floor tightens stops too early on wide-stop trades, converting potential runners into scratch exits | Tier fractions are conservative (0.25R at 1R, 0.75R at 2R) and the ATR trail still owns the tight stop; Arm C replay validates before enabling |
| The 6 h flatten cuts a trade that would have reached `TP2`+ | Configurable; `/lifecycle off` is instant; the replay proof reports the trades it would have cut |
| `initial_sl` missing on legacy signals | R lock skipped for those layers, logged once, age guard unaffected |
| `stop_source` tie-break change alters existing attribution | Dedicated test + explicit review of every `active_stop_source` assertion |
| Six-month ledger has only 19 records — thin statistics | The replay proof over 12 recorded trades is the primary evidence, not the ledger; the ledger is instrumentation, not the basis of the decision |

---

## 12. File Map

| File | Change |
|---|---|
| `risk_lifecycle.py` | **New** — policy dataclass, pure functions, command handler |
| `config.py` | 10 new knobs, parsed and validated at import |
| `startup_checks.py` | Range + ordering validation of the new knobs |
| `dynamic_risk.py` | `compute_dynamic_atr_trail_stop(..., r_lock_floor=None)`; `R_LOCK` candidate; `"r_lock_floor"` in the result |
| `mt5_executor.py` | `initial_sl` capture at placement; `apply_lifecycle_derisk` / `_flatten` / `_pending_expire` / `apply_lifecycle_action`; R-floor computation per layer |
| `main.py` | Age-guard branch in the 1-second watcher; `/rlock`, `/lifecycle`, `/stats` commands; `first_fill_at` stamping |
| `trade_ledger.py` | `schema_version` 3 → 4; six new fields |
| `risk_controls.py` | *(unchanged — new command handlers live in `risk_lifecycle.py`)* |
| `tests/test_trade_lifecycle.py` | **New** — unit + integration suites |
| `scripts/ab_replay_proof.py` | Arm C |
| `README.md`, `AI_HANDOVER.md` | **Out of scope** — separate documentation-only change wave (section 9.6) |

---

## 13. Decision Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-11-20 | R-relative floor (A1) over $-per-oz ladder (A2) and knob re-tuning (A3) | Only A1 adapts to each trade's own stop distance; A3 has already broken the tight-TP1 case once (`Msg 13167`) |
| 2026-11-20 | Two-step time stop: de-risk at 2 h, flatten at 6 h | Keeps a winning runner's upside while stopping a stuck trade bleeding |
| 2026-11-20 | Session/rollover flatten **off** by default | Forfeits the Asian-session gold move; the 6 h age flatten is the backstop. Revisit with data |
| 2026-11-20 | Both features ship disabled | Zero behaviour change on upgrade; live enable is one command |
| 2026-11-20 | No VPS deployment | Standing directive |
