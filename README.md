# Telegram-to-MT5 Gold (`XAUUSD`) Signal Copier (v3.5.0)

A fast, automated Python bot on Windows that reads **Gold (`XAUUSD`)** trading signals from your Telegram channels and executes + manages them on **MetaTrader 5** in real time.

---

## How It Works (In Plain English)

### 1. Reading Signals Automatically
- **Strict Signal Intent Whitelist (Gold-Only by Default):** Works even when the provider writes `"Buy now"`, `"Sell with limit 3890"`, or `"Sell limit 3895-3887"` without typing `"XAUUSD"`. Only explicit `Buy/Sell now` (or market commands), `Buy/Sell limit/stop/with limit` + price/zone, or `Buy/Sell` + a real Gold price (`>= 500`) are accepted. Casual chat (`"sell straight"`), victory recaps, conflicting directions, and non-Gold pairs (`EURUSD`, `NAS100`, `BTCUSD`, etc.) are rejected with a `[PARSER IGNORED]` log entry.
- **4-Layer Entries (`0.05` Lots Base):** Complete signals split into **4 layers** (`[0.01, 0.01, 0.01, 0.02]` lots):
  - **Price Zone (`Buy 3895-3887`):** Spreads `L1`, `L2`, `L3`, `L4` evenly across the zone (`0%`, `33%`, `67%`, `100%`), placing Market orders for any price already reached and Limit orders for deeper levels. Pending limits are **never** cancelled for hitting `TP1` before price actually enters the zone.
  - **Stub `L1`-Only & Zone Edit Distribution:** An unpriced `"Buy/Sell now"` or single-price stub posted without `SL` or `TP` opens **only `L1` (`0.01` lot)**. When edited later to add a zone, the bot keeps `L1` and distributes `L2–L4` as pending limits across the zone (strictly capped at 4 live orders total). An edit adding only `SL`/`TP` updates targets without stacking duplicate orders.

### 2. Taking Profit & Scaling Winners (`TP1 -> TP2 -> TP3 -> TP4 -> TP5`)
- **Start Safe at TP1:** All layers start with their broker Take-Profit set at `TP1 - $0.50` (BUY) or `TP1 + $0.50` (SELL) so spreads don't cause missed fills.
- **Smart Dynamic Runner:** The bot automatically picks the **deepest filled layer** to act as your runner:
  - **4 layers filled:** `L1, L2, L3` bank profit at `TP1`, while **`L4` (`0.02` lot)** runs toward `TP2 -> TP3 -> TP4 -> TP5`.
  - **2–3 layers filled:** Earlier layers bank at `TP1`, deepest filled layer runs.
  - **Only 1 layer filled:** **`L1` automatically becomes the runner** (nothing closes early at `TP1`) so single-fill trades still catch big moves to `TP2–TP5`.
- **Early Broker TP Lift (`70%`):** Once price reaches `70%` of the current milestone leg (`TP1 -> TP2 -> TP3 -> TP4 -> TP5`), the runner's broker TP lifts early to the next milestone so MT5 never cuts off a fast breakout candle.
- **Dynamic `effective_max_stage` Cap:** While `RUNNER_MAX_TP_STAGE=5` (`TP5` ceiling) is the default, if an explicit multi-TP signal only defines `TP1..TP4` (or sets higher TPs to `None`), the runner dynamically caps `effective_max_stage` at the highest valid TP (`TP4`) without fabricating missing targets or crashing.

### 3. Staged Gold Runner Exit Architecture (`Stage 0` vs `Stages 1–4`, v3.5.0)
- **Strict Stage Mutual Exclusivity (`STAGED_EXIT_ENABLED = true`):**
  - **Stage 0 (Pre-`TP1` Only):**
    - **`GOLD_REVERSAL_GUARD` (`GOLD_GUARD`):** Stays off mid-range and only arms once open profit reaches **`85%` of `TP1`** (`ARM_THRESHOLD_PCT = 0.85`; `80%` on tight scalps `<= $4.00`). Once armed, banks at market if open profit drops **`30%` from peak** **AND** price pulls back at least **`$1.50` (`15 pips`)** (or `0.35 x M1_ATR_14`), **OR** if dollar giveback hits **`$3.50`** (`HARD_GIVEBACK_CEILING_DOLLARS = 3.50`).
    - **`SHORTFALL_BASE` (`L1–L3` Base Layers Only):** If price gets within `$0.80` (`8 pips`) of `TP1` and reverses by `$2.00` (`20 pips` for `3` ticks), **only** `L1–L3` bank early—your deepest runner stays open.
    - **Hard Cutoff at `TP1`:** The moment `TP1` is reached (`Stage >= 1`), Stage 0 guards (including the fixed `$3.50` giveback ceiling and legacy post-TP1 proximity/shortfall guards) are **hard-disabled** so multi-milestone trends are never choked early.
  - **Stages 1–4 (Post-`TP1` Active Runner):**
    - Governed exclusively by **4 coordinated mechanisms**: **(1) Pure Structural Stage Floor (`fsm_floor`)**, **(2) Proactive `70%` Broker TP Lift**, **(3) Staged M1-ATR Trailing Stop (`atr_stop` / `trail_stop`)**, and **(4) Ceiling Target Exit (`TP_CEILING` at `TP{effective_max_stage}`, default `TP5` with `RUNNER_MAX_TP_STAGE=5`, or `TP4` when a signal only defines `TP1..TP4`)**.

#### Staged ATR Trail & Structural Floor Table (`Stages 1–4`)

| Stage | Milestone State | Structural Floor (`fsm_floor`) | ATR Trail Formula (`M1_ATR_14`) | Min / Max Clamp (`$`) | Fallback (`ATR <= 0`) | Next Broker TP |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Stage 1** | `TP1` Reached / Cleared | `BE + $0.50` (`+5.0 pips`) | `1.5 x M1_ATR_14` | `$2.50 – $3.50` | `$3.00` | `TP2` |
| **Stage 2** | `TP2` Cleared (`+5.0 pips`) | `TP1` | `2.0 x M1_ATR_14` | `$3.00 – $4.50` | `$3.75` | `TP3` |
| **Stage 3** | `TP3` Cleared (`+5.0 pips`) | `TP2` | `2.5 x M1_ATR_14` | `$4.00 – $6.00` | `$5.00` | `TP4` (or Ceiling if `effective_max_stage=4`) |
| **Stage 4** | `TP4` Cleared (`+5.0 pips`) | `TP3` | `3.0 x M1_ATR_14` | `$5.00 – $8.00` | `$6.50` | `TP5` (Default Ceiling, `RUNNER_MAX_TP_STAGE=5`) |

- **Monotonic Ratchet Rule & Tie-Breaker:**
  - On every tick in `Stages 1–4`, the active runner stop is `trail_stop = max(fsm_floor, atr_stop, prev_trail_stop)` for `BUY` (`min(...)` for `SELL`).
  - `trail_stop` **never loosens** when transitioning to a wider ATR stage tier or when M1 ATR expands.
  - `active_stop_source` is attributed as `"FSM_FLOOR"` when `fsm_floor` dominates or ties (`fsm_floor == atr_stop`), and `"ATR_TRAIL"` when the ATR trail sits above the structural floor.
  - Hard MT5 `SL` pushes immediately on stage transitions (`force_sl_push=True`) and throttles by `TP1_TRAIL_STEP_DOLLARS = $0.25` within the same stage.
- **Spread-Spike Internal Exit Pause:** During momentary rollover/news spread spikes, internal software `trail_stop` market exits pause for that tick while the hard broker `SL` (`>= fsm_floor`) remains active in MT5.
- **Instant Rollback (`STAGED_EXIT_ENABLED=false`):** Setting `STAGED_EXIT_ENABLED=false` in `.env` immediately restores legacy v3.4.2 post-`TP1` exit behavior.

### 4. Instant Channel Commands, Edits & Deleted Messages
- **Fast Breakeven (No Reply Needed, Per-Channel Only):** If a provider posts `"BE"`, `"Go breakeven!"`, or `"break even your entries"` in a channel (even without replying to the signal), the bot moves **all 4 layers (`L1–L4`)** of **that channel's trade only** to Breakeven (`+5 pips` / `$0.50`, with instant `0.0p` fallback and `0.5s` auto-retry if a layer is slightly underwater). It never touches trades from other channels.
- **Late Edits Work Anytime:** Whether a provider edits a signal 10 seconds or 10 minutes later, the bot updates `SL`, `TP1–TP5`, and zone layers automatically.
- **Deleted Signal = Instant Exit:** If a provider deletes a signal message in Telegram, the bot immediately closes open positions and cancels pending orders for **that signal only**.

---

## Phone Controls (Telegram `"Saved Messages"`)

Send any of these messages to your own **Saved Messages** chat in Telegram from your phone:

| Command | What It Does |
| :--- | :--- |
| `/status` or `/health` | Live check: Balance, Equity, open trades, active layers, runner stage, and MT5 ping |
| `/be` | Move all layers of the latest active trade to Breakeven |
| `/sl 3880` | Move Stop-Loss on the active trade to `3880` |
| `/tp TP2 3915 TP3 3930` | Update `TP1`–`TP5` targets on the active trade |
| `/trail` or `/trail tp2` | Trail Stop-Loss or step the runner to the next TP milestone |
| `/partial` | Close half / one layer to lock partial profit |
| `/trade Buy 3890-3885 TP 3900 SL 3875` | Manually execute a 4-layer signal straight from Saved Messages |
| `dailywin 20` / `dailywin off` | Auto-bank everything and stop for the day once you make `£20` net profit |
| `dailyloss 50` / `dailyloss off` | Stop trading for the day if **closed** realized losses reach `-£50` |
| `/closeall` | **Emergency Panic Button:** Close all bot positions and cancel all pending orders immediately |
| `/testorder` | Place and auto-cancel a safe far-away limit order to test MT5 connection |

---

## Quick Setup Guide

1. **Open MetaTrader 5 (Windows):**
   - Log into your broker account.
   - Enable **Tools -> Options -> Expert Advisors -> Allow Algorithmic Trading**.
   - Make sure the top **Algo Trading** button is **Green**.
2. **Install Python Packages:**
   ```powershell
   pip install -r requirements.txt
   ```
3. **Configure `.env`:**
   Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_API_ID` & `TELEGRAM_API_HASH` (from [my.telegram.org](https://my.telegram.org))
   - `CHANNEL_IDS` (your Telegram signal channel IDs)
   - `DEFAULT_SYMBOL` (your broker's Gold symbol in MT5 Market Watch, e.g. `XAUUSD.s` or `XAUUSD`)
4. **Log In to Telegram (Once):**
   ```powershell
   python auth.py
   ```
5. **Start the Bot (24/7 Auto-Restart Watchdog):**
   Double-click `start_copier.bat` (or your desktop launcher `.bat`), or run:
   ```powershell
   python main.py
   ```

---

## Key Settings (`.env` / `config.py`)

| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `BASE_LAYER_LOTS` | `[0.01, 0.01, 0.01, 0.02]` | Lot sizes for `L1, L2, L3, L4` (`0.05` total) |
| `RISK_LOT_MULTIPLIER` | `1.0` | Scales all 4 layers proportionally (e.g. `2.0` -> `0.10` total) |
| `BE_LAYERS` | `["L1", "L2", "L3", "L4"]` | Layers moved to Breakeven when `"BE"` is called |
| `BE_OFFSET_PIPS` | `5.0` | Extra `+5 pips` (`$0.50`) added to entry on Breakeven and Stage 1 structural floor |
| `STAGED_EXIT_ENABLED` | `true` | Enables v3.5.0 mutually-exclusive `Stage 0` vs `Stage 1–4` runner exit engine (`false` = instant v3.4.2 rollback) |
| `RUNNER_MAX_TP_STAGE` | `5` | Default runner ceiling stage (`TP5`; dynamically caps via `effective_max_stage` at the highest valid TP if higher TPs are omitted/`None`) |
| `ARM_THRESHOLD_PCT` | `0.85` | Stage 0 Gold Reversal Guard arms at `85%` of `TP1` (`80%` on legs `<= $4.00`) |
| `RUNNER_EARLY_TP_LIFT_PCT` | `0.70` | Proactively lifts runner's broker TP to next milestone at `70%` of current leg |
| `GIVEBACK_PCT` | `0.30` | Stage 0: banks on a `30%` drop from peak open profit |
| `MIN_GOLD_PULLBACK_DOLLARS` | `1.50` | Stage 0: minimum `$1.50` (`15 pips`) pullback required so Gold wicks are ignored |
| `HARD_GIVEBACK_CEILING_DOLLARS` | `3.50` | Stage 0 only: hard maximum dollar giveback from peak before forced pre-`TP1` exit |
| `TP1_TRAIL_ENABLED` | `true` | Enables staged M1-ATR trailing stop once `TP1` is reached (`Stage 1–4`) |
| `TP1_TRAIL_ATR_MULT` | `1.5` | Stage 1 multiplier applied to `M1_ATR_14` (`BE + $0.50` floor) |
| `TP1_TRAIL_MIN_DOLLARS` / `TP1_TRAIL_MAX_DOLLARS` | `2.50` / `3.50` | Stage 1 clamped ATR trail range (`$3.00` midpoint fallback if ATR `<= 0`) |
| `TP1_TRAIL_STEP_DOLLARS` | `0.25` | Minimum stop improvement (`$0.25`) before sending an intra-stage MT5 `SL` update |
| `TRAIL_TP2_ATR_MULT` / `TRAIL_TP2_MIN` / `TRAIL_TP2_MAX` | `2.0` / `3.00` / `4.50` | Stage 2 (`TP2` cleared, `TP1` floor) ATR multiplier and `$3.00–$4.50` clamp (`$3.75` midpoint) |
| `TRAIL_TP3_ATR_MULT` / `TRAIL_TP3_MIN` / `TRAIL_TP3_MAX` | `2.5` / `4.00` / `6.00` | Stage 3 (`TP3` cleared, `TP2` floor) ATR multiplier and `$4.00–$6.00` clamp (`$5.00` midpoint) |
| `TRAIL_TP4_ATR_MULT` / `TRAIL_TP4_MIN` / `TRAIL_TP4_MAX` | `3.0` / `5.00` / `8.00` | Stage 4 (`TP4` cleared -> `TP5` leg, `TP3` floor) ATR multiplier and `$5.00–$8.00` clamp (`$6.50` midpoint) |
| `TP1_TOUCH_BANK_ENABLED` / `TP1_TOUCH_BANK_BUFFER_PIPS` | `true` / `12.0` | Legacy v3.4.1 TP1-touch fallback bank settings (used only when `STAGED_EXIT_ENABLED=false`) |
| `AUTO_MARKET_SLIPPAGE_PIPS` | `15.0` | Activates bare `"BUY/SELL <price>"` signals at market immediately if within `15 pips` (`$1.50`) |
| `ADAPTIVE_SPREAD_CEILING_DOLLARS` | `0.80` | Adaptive spread ceiling before pausing internal reversal / ATR-trail software exits on spikes |
| `SHORTFALL_ARM_PIPS` / `SHORTFALL_PULLBACK_PIPS` | `8.0` / `20.0` | Stage 0 base-layer (`L1–L3`) pre-`TP1` protection (`$0.80` arm, `$2.00` pullback, `3` ticks) |
| `ENFORCE_HIGH_RISK_LOT_CAP` | `true` | Caps total active lots safely based on live account balance (`£10` per `0.01` lot) |
| `CLOSE_ON_SIGNAL_DELETED` | `true` | Auto-closes/cancels a trade if its Telegram signal is deleted |

---

## Trade Ledger Telemetry (`trades_ledger.jsonl`, v3.5.0)

Every closed signal appends a structured JSON record (`schema_version: 3`) to `trades_ledger.jsonl` including full v3.5.0 exit attribution:

| Field | Type | Description |
| :--- | :--- | :--- |
| `exit_reason_code` | `str` | Canonical enum: `"GOLD_GUARD"` \| `"SHORTFALL_BASE"` \| `"FSM_FLOOR"` \| `"ATR_TRAIL"` \| `"TP_CEILING"` \| `"BROKER_SL"` \| `"MANUAL"` |
| `exit_reason` | `str` | Canonical `exit_reason_code` when explicitly set by the v3.5.0 exit engine (or legacy exit reason string on uncoded closes such as `"Signal Deleted (...)"`, with verbose text stored in `exit_reason_detail`) |
| `exit_stage` | `int` | FSM stage at close (`0` = pre-`TP1`, `1` = `TP1` reached/cleared, `2` = `TP2` cleared, `3` = `TP3` cleared, `4` = `TP4` cleared / ceiling) |
| `exit_price` | `float \| null` | Live `bid` (`BUY`) or `ask` (`SELL`) at close (or broker deal price when available) |
| `max_favorable_excursion` | `object` | `{"peak_price": float \| null, "peak_profit_per_oz": float \| null}` measuring peak favorable excursion per ounce from entry |
| `giveback_from_peak` | `float \| null` | Dollar giveback from `peak_price` to `exit_price` (`0.0` on `TP_CEILING`, `null` if unavailable) |
| `trail_tier_used` | `object \| null` | Active tier snapshot at exit (`{"stage", "atr_mult", "min_dollars", "max_dollars", "m1_atr_14", "trail_dist"}`; `null` at `Stage 0`) |

---

## Testing Safely (Zero Live Orders)

Run the full isolated test suite and simulator anytime without touching your live account or live state files:

```powershell
$env:DRY_RUN="true"
python -m pytest -q
python simulate.py
python test_examples.py
```

- **`pytest`:** Full unit & scenario test suite covering parsing, 4-layer execution, breakeven, edits, deletes, reconnect catch-up, Stage 0 Gold Reversal Guard, and the v3.5.0 Staged Gold Runner Exit Architecture (`Stages 1–4`).
- **`simulate.py`:** `10`-step end-to-end simulation in an isolated temp folder.
- **Deep Technical Specs:** See `ARCHITECTURE_v3.md` for internal state-machine and engineering contracts.
