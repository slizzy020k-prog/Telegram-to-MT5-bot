# Telegram-to-MT5 Gold (`XAUUSD`) Signal Copier (v3.9.0)

A fast, automated Python bot on Windows that reads **Gold (`XAUUSD`)** trading signals from your Telegram channels and executes + manages them on **MetaTrader 5** in real time.

> **v3.9.0 in one paragraph:** 4-layer entries, a **Pure Dynamic ATR Engine** with 3-stage progressive trailing compression, an optional **R-relative profit floor** (R-lock, shipped **disabled**), **no time-based exits at all** (the lifecycle timers were removed), an **MT5 startup preflight** that halts trading on an unverified session, **schema-4 ledger telemetry** with `/stats`, and a **pinned-tape A/B replay harness** for evidence-based calibration.

### What changed since the old v3.5.0 README
- **Decommissioned:** the Stage-0 `GOLD_REVERSAL_GUARD` / `SHORTFALL_BASE` pre-TP1 guards, the staged M1-ATR exit FSM (`STAGED_EXIT_ENABLED`), and the v3.6 profit-ratchet / bank-harvest virtual exits. One engine replaces them all: the **Pure Dynamic ATR Engine** (broker-executed SL/TP only).
- **Removed in v3.9.0:** every trade-age / pending-expiry / session-flatten timer (`LIFECYCLE_*` config, `/lifecycle` command). **Trades and pending limits are never closed by a clock.**
- **Added:** 3-stage M5-ATR trailing compression with per-stage clamps, R-lock (`/rlock`), MT5 session preflight, ledger schema 4 + `/stats`, pinned-tape replay, .env-independent default tests and a config-propagation drift guard.

---

## How It Works (In Plain English)

### 1. Reading Signals Automatically
- **Strict Signal Intent Whitelist (Gold-Only by Default):** Works even when the provider writes `"Buy now"`, `"Sell with limit 3890"`, or `"Sell limit 3895-3887"` without typing `"XAUUSD"`. Only explicit `Buy/Sell now` (or market commands), `Buy/Sell limit/stop/with limit` + price/zone, or `Buy/Sell` + a real Gold price (`>= 500`) are accepted. Casual chat, victory recaps, conflicting directions, and non-Gold pairs (`EURUSD`, `NAS100`, `BTCUSD`, etc.) are rejected with a `[PARSER IGNORED]` log entry.
- **4-Layer Entries (`0.05` Lots Base):** Complete signals split into **4 layers** `[0.01, 0.01, 0.01, 0.02]` (`L1..L4`), scaled proportionally by `RISK_LOT_MULTIPLIER`, hard-capped at `MAX_ORDERS_PER_SIGNAL = 4` live orders:
  - **Price Zone (`Buy 3895-3887`):** Spreads `L1..L4` evenly across the zone (`0%`, `33%`, `67%`, `100%`), placing Market orders for any price already reached and Limit orders for deeper levels (pending limits front-run by `+$0.50` so spread/round-number bounces still fill). If live price reaches `TP1` **before** the zone fills, the unfilled pending limits are invalidated (cancelled) instead of filling late.
  - **Single-Price Signals:** Consolidated to **1 order** (`SINGLE_PRICE_CONSOLIDATE_ORDERS=true`) with price-aware Market/Limit routing; a later zone edit expands `L2..L4` cleanly.
  - **Market Now / Bare Price (`STAGED_MARKET_ENTRY=true`):** An unpriced `"Buy/Sell now"` (or bare `"BUY 3728"` within `AUTO_MARKET_SLIPPAGE_PIPS = 15` / `$1.50` of live price) executes **only `L1` (`0.01`) at market**, then waits for the provider's zone/limit edit before placing `L2..L4` — no stacking 4 layers at one price. An edit adding only `SL`/`TP` updates targets without duplicate orders.
- **ATR-Scaled SL/TP at Entry:** Provider SL/TP levels are scaled by live **M5 ATR(14)** volatility (ADAPTIVE mode, ratio clamped `0.75x–1.75x` of a `$4.00` baseline; SL clamp `$4.50–$35.00`, TP clamp `$2.50–$50.00`). Missing stops/targets are computed from ATR, with fixed fallbacks (`150`-pip SL / `50`-pip TP1) when ATR is unavailable.

### 2. Taking Profit & Scaling Winners (`TP1 -> TP2 -> TP3 -> TP4 -> TP5`)
- **Start Safe at TP1:** Every layer's broker Take-Profit starts at `TP1 - $0.50` (BUY) or `TP1 + $0.50` (SELL) so spreads don't cause missed fills.
- **Smart Dynamic Runner:** The **deepest filled layer** becomes the runner; earlier layers bank at `TP1`. If only one layer filled, **`L1` itself becomes the runner** — single-fill trades still catch big moves.
- **Momentum-Gated TP Lifts:** The runner's broker TP steps toward the next milestone when price is within `0.50 x ATR` of the target **or** has covered `>= 75%` of the leg, with pullback `<= 0.25 x ATR`. The `TP1 -> TP2` lift additionally requires `>= 50%` leg progress (or an already BE-locked layer) so fast wicks don't lift prematurely.
- **Dynamic Ceiling:** `RUNNER_MAX_TP_STAGE = 5` (`TP5`) is the default ceiling; if a signal only defines `TP1..TP4`, the runner caps at the highest valid TP without fabricating targets.

### 3. Pure Dynamic ATR Engine — 3-Stage Progressive Trailing Compression
One unified engine manages the **BE lock, milestone floors, forward-only trailing SL and trailing TP** for every open layer. It only ever modifies **broker SL/TP** — it never market-closes a position on its own.

The trailing distance compresses in three stages as the trade's **peak** favourable excursion (MFE, $/oz) grows:

| Stage | MFE ($/oz from entry) | Trail Distance | Clamp (min–max) |
| :--- | :--- | :--- | :--- |
| **A — early breathing room** (`STANDARD`) | `< $10` | `1.40 x M5_ATR` | `$2.50 – $8.00` |
| **B — profit-lock tightening** (`EXTENDED`) | `$10 – $18` | `1.00 x M5_ATR` | `$2.20 – $5.50` |
| **C — peak harvest hug** (`HOME_RUN`) | `>= $18` | `0.80 x M5_ATR` | `$2.00 – $4.00` |

- The `$2.50` Stage-A floor absorbs spread + normal retest wicks so runners survive `TP1 -> TP2`; the tighter Stage-B/C clamps bank extended moves instead of surrendering `$8–$10/oz` on reversal. Boundaries are inclusive at `$10` / `$18`.
- **Trail activation:** the ATR trail arms once `MFE >= 1.00 x ATR`.
- **Forward-only, never loosens:** MFE is measured from a monotonically non-decreasing peak; the multiplier is non-increasing, the per-stage clamps tighten monotonically, and a stop is only sent when it improves by at least **`$0.25`** (`DYNAMIC_ATR_TRAIL_STEP_DOLLARS`).
- **Tightest-candidate picker:** `R_LOCK` > `MILESTONE_FLOOR` > `ATR_TRAIL` > `BE_LOCK` (tie-break order). Every modify is validated against the broker's **`STOP_LEVEL`** (plus a `$0.05` safety buffer) and **`FREEZE_LEVEL`** — no `retcode 10016 (Invalid stops)` spam.
- **BE Lock:** arms at `max(0.75 x ATR, $3.50)` of MFE, locks the broker SL at **entry `± $0.50`** and sweeps unfilled lower pending limits. With R-lock enabled and a known initial stop, arming becomes **R-gated** (see below) — the lock level never changes.
- **Milestone Floors:** once `TP1` is cleared the floor is breakeven; once `TPm` (`m >= 2`) is cleared the floor is `TP(m-1)`.
- **Spread-Spike Pause:** above the adaptive spread ceiling (`$0.80`) the engine pauses internal updates for that tick; the hard broker SL/TP already in MT5 remain active.

### 4. R-Relative Profit Floor (R-Lock) — Ships DISABLED
R-lock scales protection to each trade's own initial risk **`R = |entry - initial SL|`** (captured per layer at placement — the stop the broker actually received):

| MFE Reached | Floor Locked |
| :--- | :--- |
| `0.5R` | Breakeven (entry) — via the R-gated BE lock at `max(0.5R, 0.75 x ATR, $2.50)`, level `entry ± $0.50`, + pending sweep |
| `1.0R` | `entry + 0.25R` |
| `1.5R` | `entry + 0.50R` |
| `2.0R` | `entry + 0.75R` |

- **Shipped default: `R_LOCK_ENABLED=false`.** Enable persistently in `.env`, or at runtime with `/rlock on` (in-memory only — resets on restart). `/rlock` alone reports the current policy.
- The floor joins the same tightest-candidate picker, is derived from the persisted peak MFE, and disappears if price retraces past it — it can never place a stop on the wrong side of the market.
- **Known limitation (structural):** because the picker keeps the *tightest* stop and the ATR trail distance is capped (`$8 / $5.50 / $4` by stage), the R floor can only win on **narrow/mid R** trades. On wide stops (e.g. `R = $25`) the staircase would need a trail distance `> $18.75` to win — impossible under any clamp. **Re-tiering the ladder does not fix wide stops** (measured on the pinned tape; see `AI_HANDOVER.md` and the changelog).

### 5. No Timer Exits (v3.9.0)
**Trades and pending zone limits are NEVER closed or cancelled by a clock.** The only ways a position closes or a pending order is removed:
1. **Take-Profit hit** (broker TP, including lifted runner TPs),
2. **Stop-Loss hit** (initial, trailed, BE/R-locked — broker-executed),
3. **An explicit provider command** (close / BE / cancel / delete-limit / signal deletion),
4. **Your phone controls or session guards** (`/closeall`, `/partial`, `dailywin`, `dailyloss`, etc.).

The v3.8.x trade-age de-risk / hard-flatten timers, pending auto-expiry and session-flatten clock were **deleted** in v3.9.0, along with all `LIFECYCLE_*` config and the `/lifecycle` command. **Implication:** a trade that never reaches TP or SL stays open until you or the provider act — there is no age-based safety net.

### 6. MT5 Startup Preflight (Fail-Safe)
After `executor.connect()` at boot — and again when the MT5 connection recovers — `startup_checks.validate_mt5_session()` verifies, using **read-only** IPC only:
- terminal **connected** and **Algorithmic Trading allowed** (`trade_allowed`);
- **account identity**: `account.login == MT5_LOGIN` and (when configured) `account.server == MT5_SERVER`;
- **symbol sanity**: `DEFAULT_SYMBOL` exists with valid `volume_min` / `volume_step` (a non-FULL trade mode is a warning, not a block).

On any hard failure the bot sets **`TRADING_HALTED`**: no orders can be placed, each problem is logged and alerted to Saved Messages, and **Telegram monitoring stays online**. In live mode `MT5_LOGIN` must be configured (unverified identity is a hard fail). `DRY_RUN=true` bypasses the preflight. The preflight never logs in, switches accounts, or sends orders; per-order preflight checks remain as defense-in-depth.

### 7. Instant Channel Commands, Edits & Deleted Messages
- **Fast Breakeven (Per-Channel Only):** A provider `"BE"` / `"Go breakeven!"` moves **all 4 layers of that channel's trade only** to breakeven (`+$0.50`, with instant `0.0p` fallback and `0.5s` auto-retry when a layer is slightly underwater or inside the broker stops level). Other channels are never touched.
- **Late Edits Work Anytime:** Edits update `SL`, `TP1–TP5` and zone layers automatically (untracked-message age guard `MAX_NEW_EDIT_AGE_SECONDS = 3600`; edit-event freshness `180s` so emoji reactions on old posts never fire trades).
- **Deleted Signal = Instant Exit:** Deleting a signal message closes its open positions and cancels its pending orders immediately (0ms push + `1.5s` fast sweep), for that signal only.
- **Reconnect Catch-Up:** On startup/reconnect the bot replays up to `25` missed messages from a `30`-minute window with stale-market guards (market entries older than `60s` or `> 15` pips of slippage are skipped).

### 8. Capital Protection (Defaults OFF — Enable Deliberately)
Risk caps ship **disabled (unlimited)**: `ENFORCE_HIGH_RISK_LOT_CAP=false`, `DAILY_LOSS_PAUSE_PCT=0`, `EMERGENCY_CUTOFF_LOSS_PCT=0`, `FLOAT_LOSS_FORCE_CLOSE_PCT=0`. Toggle them live from Saved Messages (`/lotcap`, `/losslimit`, `/floatcap`, `dailyloss`) or in `.env`.
- **`dailywin 20`:** once net profit for the day reaches the target, everything is banked, pendings cancelled, and the session terminates (exit code `88` — the watchdog does **not** restart).
- **`dailyloss 50`:** realized-only daily loss limit (broker-server trading day). On breach: trading halted, pending orders cancelled, session terminates (exit code `89`). A same-day relaunch starts **LOCKED** (no new trades) until the next broker day or `dailyloss off`. Open filled trades are left on their broker SL/TP by default (`DAILY_LOSS_CLOSE_OPEN_TRADES=false`).

---

## Phone Controls (Telegram "Saved Messages")

Send any of these to your own **Saved Messages** chat from your phone:

| Command | What It Does |
| :--- | :--- |
| `/status` or `/health` | Live check: Balance/Equity, open trades, layers, runner stage, MT5 ping, Dynamic ATR engine status |
| `/stats` | Ledger analytics: today / 7-day / all-time P&L, win rate, cash giveback, longest trade age, exit-reason mix |
| `/be` | Move all layers of the latest active trade to Breakeven |
| `/sl 3880` | Move Stop-Loss on the active trade to `3880` |
| `/tp TP2 3915 TP3 3930` | Update `TP1–TP5` targets on the active trade |
| `/trail` | Manually trail / step the active trade's stop |
| `/partial` | Close half / one layer to lock partial profit |
| `/bank`, `/bank_tp1`, `/bankall` | Manual profit banking (close layers at market to lock profit) |
| `/trade Buy 3890-3885 TP 3900 SL 3875` | Manually execute a 4-layer signal from Saved Messages |
| `/closeall` (or `/panic`) | **Emergency Panic Button:** close all bot positions, cancel all pending orders |
| `/testorder` | Place and auto-cancel a safe far-away limit order to test the MT5 connection |
| `/rlock` / `/rlock on` / `/rlock off` | Report / toggle the R-relative profit floor (in-memory; resets on restart) |
| `/dynamicrisk` | Query/configure ATR-scaled SL/TP (`on`, `off`, `adaptive`, `fallback`, `pure`, `<sl_mult> <tp_mult> [M1|M5|M15|H1]`) |
| `/atrtrail` | Query/configure the trailing engine (`on`, `off`, `tp on`, `tp off`, `<mult> [activation_mult] [timeframe]`) |
| `/lotcap` | Toggle/configure the high-risk lot cap |
| `/losslimit` (or `/drawdown`, `/ddcap`) | Toggle/configure daily drawdown pause / emergency cutoff |
| `/floatcap` | Toggle/configure the floating-loss force-close cap |
| `dailywin 20` / `dailywin off` | Auto-bank everything and stop for the day at `£20` net profit |
| `dailyloss 50` / `dailyloss off` | Stop trading for the day at `-£50` realized loss |
| `/help` | Command overview |

> `/lifecycle` was **removed in v3.9.0** — there are no timer exits left to control.

---

## Quick Setup Guide

1. **Open MetaTrader 5 (Windows):**
   - Log into your broker account.
   - Enable **Tools -> Options -> Expert Advisors -> Allow Algorithmic Trading**.
   - Make sure the top **Algo Trading** button is **Green**.
2. **Install Python Packages** (Python 3 on Windows; the `MetaTrader5` package is Windows-only):
   ```powershell
   pip install -r requirements.txt
   ```
3. **Configure `.env`:** Copy `.env.example` to `.env` and fill in (placeholders shown — never commit real credentials):
   ```dotenv
   TELEGRAM_API_ID=<your_api_id>            # from https://my.telegram.org
   TELEGRAM_API_HASH=<your_api_hash>
   CHANNEL_IDS=[<your_channel_id>]          # monitored signal channel(s)
   DEFAULT_SYMBOL=XAUUSD.s                  # your broker's Gold symbol in Market Watch
   MT5_LOGIN=<your_mt5_account_number>      # REQUIRED in live mode for the session preflight
   MT5_SERVER=<your_broker_server_name>     # optional; verified against the terminal when set
   DRY_RUN=false                            # true = paper mode (preflight bypassed, no real orders)
   R_LOCK_ENABLED=false                     # persistent R-lock switch (shipped disabled)
   ```
   `MT5_PASSWORD` / `MT5_PATH` are optional direct-login overrides. `.env` is gitignored.
4. **Log In to Telegram (Once):**
   ```powershell
   python auth.py
   ```
5. **Start the Bot (Auto-Restart Watchdog):**
   Double-click `start_copier.bat`, or run:
   ```powershell
   python main.py
   ```
   The watchdog restarts on crashes with capped backoff (`5s -> 60s`) but **stops** on clean exits: `88` (daily win), `89` (daily loss), `90` (duplicate instance — a mutex + file lock allow only one copier), `0` (halted/clean shutdown).

---

## Key Settings (`.env` / `config.py`)

All values below are the **shipped defaults** (verified against `config.py` at v3.9.0). Every knob can be overridden in `.env`.

### Layers & Entries
| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `BASE_LAYER_LOTS` | `[0.01, 0.01, 0.01, 0.02]` | Lot sizes for `L1..L4` (`0.05` total) |
| `RISK_LOT_MULTIPLIER` | `1.0` | Scales all 4 layers proportionally |
| `MAX_ORDERS_PER_SIGNAL` | `4` | Hard cap on live orders per signal |
| `STAGED_MARKET_ENTRY` | `true` | Bare market signals open only `L1`; `L2..L4` wait for the zone edit |
| `SINGLE_PRICE_CONSOLIDATE_ORDERS` | `true` | Single-price entries place 1 consolidated order |
| `TP_FRONT_RUN_OFFSET` | `0.50` | Broker TP starts `$0.50` in front of `TP1` |
| `LIMIT_ENTRY_FRONT_RUN_OFFSET` | `0.50` | Pending limit entries front-run `$0.50` toward live price |
| `AUTO_MARKET_SLIPPAGE_PIPS` | `15.0` | Bare `"BUY/SELL <price>"` activates at market within `$1.50` |
| `BE_LAYERS` / `BE_OFFSET_PIPS` | `["L1","L2","L3","L4"]` / `5.0` | Layers moved to BE on `"BE"`; `+$0.50` offset |
| `RUNNER_MAX_TP_STAGE` | `5` | Runner ceiling (`TP5`; dynamically capped at the highest valid TP) |

### Pure Dynamic ATR Engine — SL/TP at Entry
| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `DYNAMIC_ATR_SLTP_ENABLED` | `true` | ATR-scaled SL/TP on entries |
| `DYNAMIC_SLTP_MODE` | `ADAPTIVE` | `ADAPTIVE` / `FALLBACK_ONLY` / `PURE_ATR` |
| `DYNAMIC_ATR_TIMEFRAME` / `DYNAMIC_ATR_PERIOD` | `M5` / `14` | ATR source for SL/TP scaling |
| `DYNAMIC_ATR_BASELINE_DOLLARS` | `4.00` | Baseline for the volatility ratio |
| `DYNAMIC_SL_ATR_MULT` / `DYNAMIC_TP_ATR_MULT` | `2.50` / `1.50` | ATR fallback SL / TP1 multipliers |
| `DYNAMIC_SL_MIN_DOLLARS` / `DYNAMIC_SL_MAX_DOLLARS` | `4.50` / `35.00` | SL distance clamp |
| `DYNAMIC_TP_MIN_DOLLARS` / `DYNAMIC_TP_MAX_DOLLARS` | `2.50` / `50.00` | TP distance clamp |
| `DYNAMIC_SLTP_SCALE_MIN` / `DYNAMIC_SLTP_SCALE_MAX` | `0.75` / `1.75` | Volatility-ratio clamp |

### Pure Dynamic ATR Engine — Trailing (v3.9.0 per-stage clamps)
| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `DYNAMIC_ATR_TRAIL_ENABLED` | `true` | Master switch for the trailing engine |
| `DYNAMIC_ATR_TRAIL_TIMEFRAME` | `M5` | ATR timeframe for the trail |
| `DYNAMIC_ATR_TRAIL_MULT` | `1.40` | Stage A multiplier (MFE `< $10`) |
| `DYNAMIC_ATR_TRAIL_MIN_DOLLARS` / `DYNAMIC_ATR_TRAIL_MAX_DOLLARS` | `2.50` / **`8.00`** | Stage A clamp (**max tightened `12.00 -> 8.00` in v3.9.0**) |
| `DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS` / `DYNAMIC_ATR_TRAIL_EXTENDED_MULT` | `10.00` / `1.00` | Stage B threshold / multiplier |
| **`DYNAMIC_ATR_TRAIL_EXTENDED_MIN_DOLLARS`** / **`DYNAMIC_ATR_TRAIL_EXTENDED_MAX_DOLLARS`** | **`2.20`** / **`5.50`** | **New in v3.9.0:** Stage B clamp |
| `DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS` / `DYNAMIC_ATR_TRAIL_HOMERUN_MULT` | `18.00` / `0.80` | Stage C threshold / multiplier |
| **`DYNAMIC_ATR_TRAIL_HOMERUN_MIN_DOLLARS`** / **`DYNAMIC_ATR_TRAIL_HOMERUN_MAX_DOLLARS`** | **`2.00`** / **`4.00`** | **New in v3.9.0:** Stage C clamp |
| `DYNAMIC_ATR_TRAIL_ACTIVATION_MULT` | `1.00` | Trail arms at `MFE >= 1.00 x ATR` |
| `DYNAMIC_ATR_TRAIL_STEP_DOLLARS` | `0.25` | Minimum stop improvement before an MT5 update |
| `DYNAMIC_ATR_BE_ACTIVATION_MULT` / `DYNAMIC_ATR_BE_MIN_MFE_DOLLARS` | `0.75` / `3.50` | Flat BE-lock arming: `max(0.75 x ATR, $3.50)` |
| `DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS` | `0.50` | BE lock level: `entry ± $0.50` |
| `DYNAMIC_ATR_TP_TRAIL_ENABLED` | `true` | Momentum-gated trailing TP lifts |
| `DYNAMIC_ATR_TP_LIFT_ATR_MULT` / `DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT` | `0.50` / `0.75` | Lift nearness (`<= 0.50 x ATR`) or leg progress (`>= 75%`) |
| `DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT` | `0.50` | Extra `TP1 -> TP2` gate: `>= 50%` leg progress (or BE-locked) |

### R-Lock (shipped disabled)
| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `R_LOCK_ENABLED` | **`false`** | R-relative profit floor master switch (`/rlock on` toggles in-memory) |
| `R_LOCK_TIERS` | `0.5:0.00,1.0:0.25,1.5:0.50,2.0:0.75` | `MFE threshold (R) : locked fraction (R)` ladder |
| `R_LOCK_MIN_R_DOLLARS` | `0.50` | Minimum initial risk per oz for the floor to engage |

### Safety, Spread & Session Guards
| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `MAX_ALLOWED_SPREAD_DOLLARS` | `0.45` | Normal spread ceiling |
| `ADAPTIVE_SPREAD_CEILING_DOLLARS` | `0.80` | Engine pauses internal updates above this spread |
| `STOP_LEVEL_SAFETY_BUFFER_DOLLARS` | `0.05` | Extra buffer on top of broker `trade_stops_level` |
| `ENFORCE_HIGH_RISK_LOT_CAP` | **`false`** | Balance-based lot cap (enable via `/lotcap` or `.env`) |
| `DAILY_LOSS_PAUSE_PCT` / `EMERGENCY_CUTOFF_LOSS_PCT` | `0.0` / `0.0` | Disabled; daily drawdown pause / cutoff (`/losslimit`) |
| `FLOAT_LOSS_FORCE_CLOSE_PCT` | `0.0` | Disabled; floating-loss hard cap (`/floatcap`) |
| `CLOSE_ON_SIGNAL_DELETED` | `true` | Auto-close/cancel when the provider deletes a signal |
| `DRY_RUN` | `false` | Paper mode: no real orders, preflight bypassed |
| `MAGIC_NUMBER` | `777001` | Bot order tag; manual trades are never touched |

**`.env` hygiene for v3.9.0:**
- **Remove any `LIFECYCLE_*` keys** from old `.env` files — they no longer exist and do nothing.
- Runtime toggles (`/rlock`, `/atrtrail`, `/dynamicrisk`, `/lotcap`, …) are **in-memory only** and reset on restart; `.env` is the persistent switch.
- In **live mode set `MT5_LOGIN`** (and optionally `MT5_SERVER`) or the session preflight halts trading. The opt-in `MT5_PREFLIGHT_ALLOW_UNVERIFIED_LOGIN` (default `false`) is parsed from `.env` for deliberate unverified-identity operation; configuring `MT5_LOGIN` is the recommended path.
- Config-default tests are **.env-independent**: your local activations cannot break the shipped-default suite.

---

## Trade Ledger Telemetry (`trades_ledger.jsonl`, Schema 4)

Every closed signal appends one JSON line with `schema_version: 4`. Schema 4 is an **upgrade** — all schema-3 fields are retained and old records still read.

New in schema 4:

| Field | Description |
| :--- | :--- |
| `mfe_cash` | Peak favourable excursion in cash (USD price-based: `peak_profit_per_oz x lots x 100 oz`) |
| `giveback_cash` | Giveback in the **account currency**: `mfe_cash x fx_rate_to_account - realized_pnl`. **`null` when the FX rate is unknown — never a mixed-currency number** |
| `giveback_currency` / `fx_rate_to_account` | Account currency label and the conversion rate used |
| `initial_r_per_oz` | Initial risk per ounce (`|entry - initial SL|`, averaged across layers when needed) |
| `r_at_exit` | `mfe_cash / risk_cash` — currency-neutral R multiple at exit |
| `exit_age_seconds` | Age from the **earliest known fill** (market `L1` fill beats later limit fills) to close |
| `lifecycle_action` | Vestigial after the timer removal — always `null` on new trades |

Exit attribution (`exit_reason_code`) uses current engine codes (`DYNAMIC_ATR_TRAIL_SL`, `DYNAMIC_ATR_TP`, `BE_SL`, `INITIAL_SL`, `TP1_BANK`, `TP_CEILING`, `BROKER_SL`, `MANUAL`, `PARTIAL_BANK`, `ATR_TRAIL`, `FSM_FLOOR`, `TRAILED_SL`, …). `GOLD_GUARD` / `SHORTFALL_BASE` / `BANK_HARVEST` / `PROFIT_RATCHET_SL` survive **only as legacy aliases for parsing historical records** — those mechanisms no longer exist.

**`/stats`** (Saved Messages) reports today / 7-day / all-time net P&L, trade counts, win rate, total cash giveback (labelled in the ledger's account currency), longest trade age and the exit-reason mix. It degrades gracefully to "no ledger data" on a missing or corrupt ledger.

---

## Replay Proof (Pinned Tape, Reproducible)

`scripts/ab_replay_proof.py` replays the recorded **12-trade Nov 17–19 `XAUUSD.s` M1 tape** through isolated, strictly `DRY_RUN=true` subprocesses (MT5 is read-only; zero orders):

- **Arm A** — frozen `4544f7f` (v3.5.2) baseline, executed in a temporary git worktree.
- **Arm B** — calibrated working tree, **R-lock OFF** (explicitly pinned so the ambient `.env` cannot contaminate it), driving the **production ATR engine** (M1 tape aggregated into M5 ATR).
- **Arm C** — Arm B + **R-lock ON**. A picker trace records every production stop decision, evidencing R-lock precedence per trade.

```powershell
python scripts/ab_replay_proof.py                 # default --arm both (A + B)
python scripts/ab_replay_proof.py --arm c         # A + B + C (R-lock comparison)
python scripts/ab_replay_proof.py --arm trailing  # EXPERIMENTAL: A + B + T1..T4
```

**Tape handling (reproducibility first):**
- The default input is the **canonical pinned fixture** `tests/fixtures/tape_2026-11-17_19.json` (3,477 M1 bars, `2026-11-16 23:00 -> 2026-11-19 11:00 UTC`, sha256/count/range/gap-validated on load). Two runs on the fixture are **byte-identical** (regression-tested).
- **`--live-bars`** — explicit opt-in to a live broker fetch (**non-reproducible**; baselines drift). Cannot be combined with an explicit `--bars-json`.
- **`--refresh-tape <new-file>`** — re-fetches live bars to a **NEW** file (never overwrites the canonical fixture) and prints a diff summary for human-reviewed promotion.
- All `DYNAMIC_ATR_*` knobs are pinned in both arm parameter sets; the GBP conversion rate (`0.7589`) and server offset (`7200s`, DST-sensitive) are frozen.

**Gates:** `g1–g5` are evaluated; `g5` = Arm C `>=` Arm B on net P&L **and** tail loss no worse. On the v3.9.0 configuration **`g5` passes** (reference: A `£+15.62`, B `£+49.91`, C `£+52.73`; tails `-£61.99 / -£26.08 / -£21.74`). **`gate_3`** (the pre-existing choke-trio MFE-capture gate) **still fails**, so `all_gates_passed=False` and the script exits `1` — **expected, not a regression**.

**Experimental arms are NOT shipped:** `cx` (re-tiered R-lock ladder, Phase-2 hypothesis test) and `t1–t4` (trailing activation/clamp deltas, Phase-3/4 evidence) are decision instruments only — never defaults, never written to `config.py` / `.env`.

---

## Testing Safely (Zero Live Orders)

```powershell
python -m pytest -q        # full suite: 536 passed, 0 failed (v3.9.0, HEAD 1f1565e)
python simulate.py         # 10-step end-to-end simulation in an isolated temp folder
python test_examples.py    # worked parser/execution examples
python check_algo.py       # static algo-trading sanity checks
```

- The suite covers parsing, 4-layer execution, breakeven, edits, deletes, reconnect catch-up, the Pure Dynamic ATR Engine (3-stage compression, forward-only ratchet, picker), R-lock, MT5 preflight, ledger schema 4, and replay reproducibility.
- **Shipped-default tests are .env-independent** (`tests/test_shipped_defaults.py`: dataclass defaults + empty-reader fallbacks + a neutralized-`load_dotenv` reload fixture), so a live-activated local `.env` cannot break them.
- **`tests/test_config_propagation.py`** is the drift guard: every `DYNAMIC_*` / `R_LOCK_*` knob must propagate through the executor whitelist (see `AI_HANDOVER.md` for the 7 registration points).
- **`tests/test_replay_reproducibility.py`** pins the tape hash and asserts two dry-run replays are byte-identical.
- For manual dry runs set `DRY_RUN=true`; tests isolate state/log/ledger/lock paths automatically.

---

## Known Limitations

1. **R-lock wide-stop domination is structural** — the floor engages narrow/mid R only; re-tiering will not fix wide stops (measured, see `AI_HANDOVER.md`).
2. **No clock exits** — a trade that never hits TP/SL stays open indefinitely until an explicit command. There is no age-based de-risk or flatten.
3. **Risk caps ship disabled** — lot cap, daily drawdown and floating-loss caps are unlimited until you enable them.
4. **Live preflight requires `MT5_LOGIN`** — an unverified identity halts trading (fail-safe). The `.env` opt-out knob is not wired through `config.py` (see above).
5. **`gate_3` replay gate still fails** — stop-distance surrender on the choke trio (`13153/13167/9934`); trailing-distance optimisation is open research (experimental arms `t1–t4`).
6. **Outstanding reliability risks** (from the Phase-0 audit): lost-update state write-back on the BE sweep path, no cumulative-exposure gate across signals, daily-loss guards fail-open on disconnected MT5 IPC, and more — tracked with fixed/outstanding labels in `AI_HANDOVER.md`.
7. **`ARCHITECTURE_v3.md` is a historical v3.5.0 contract** — its Stage-0 / staged-trail sections are superseded by the Pure Dynamic ATR Engine. `PARSER_SPEC.md` is broadly current but partially stale. Treat this README + `AI_HANDOVER.md` + `AUTOMATION_CHANGELOG.md` as authoritative.

---

## More Documentation
- **`AI_HANDOVER.md`** — current (v3.9.0) technical handover: architecture, config propagation chain, exit engine, risks.
- **`AUTOMATION_CHANGELOG.md`** — authoritative version history (v3.6.0 → v3.9.0).
- **`docs/development/phase0-audit-and-plan.md`** — read-only audit, ranked risks and phased plan.
- **`ARCHITECTURE_v3.md` / `PARSER_SPEC.md`** — historical specs (partially superseded; noted above).
