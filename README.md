# Telegram-to-MT5 Gold (`XAUUSD`) Signal Copier (v3.4)

A fast, automated Python bot on Windows that reads **Gold (`XAUUSD`)** trading signals from your Telegram channels and executes + manages them on **MetaTrader 5** in real time.

---

## How It Works (In Plain English)

### 1. Reading Signals Automatically
- **Gold-Only by Default:** Works even when the provider just writes `"Buy now"` or `"Sell limit 3895-3887"` without typing `"XAUUSD"`. Any non-Gold pair (`EURUSD`, `NAS100`, `BTCUSD`, etc.) or chat noise/recap is automatically ignored.
- **4-Layer Entries (`0.05` Lots Base):** Every valid trade splits into **4 layers** (`[0.01, 0.01, 0.01, 0.02]` lots):
  - **Price Zone (`Buy 3895-3887`):** Spreads `L1`, `L2`, `L3`, `L4` evenly across the zone (`0%`, `33%`, `67%`, `100%`), placing Market orders for any price already reached and Limit orders for deeper levels.
  - **Single Price or `"Buy/Sell now"`:** Opens up to 4 layers (and if `"Buy now"` is edited minutes later with a full zone, SL, and TPs, the bot automatically adds the missing layers and updates SL/TP).

### 2. Taking Profit & Scaling Winners (`TP1 -> TP2 -> TP3 -> TP4`)
- **Start Safe at TP1:** All layers start with their broker Take-Profit set at `TP1 - $0.50` (BUY) or `TP1 + $0.50` (SELL) so spreads don't cause missed fills.
- **Smart Dynamic Runner:** The bot automatically picks the **deepest filled layer** to act as your runner:
  - **4 layers filled:** `L1, L2, L3` bank profit at `TP1`, while **`L4` (`0.02` lot)** runs toward `TP2 -> TP3 -> TP4`.
  - **2–3 layers filled:** Earlier layers bank at `TP1`, deepest filled layer runs.
  - **Only 1 layer filled:** **`L1` automatically becomes the runner** (nothing closes early at `TP1`) so single-fill trades still catch big moves to `TP2–TP4`.
- **Early Broker TP Lift (`70%`):** Once price reaches `70%` of the way to `TP1`, the runner's broker TP lifts early to `TP2` so MT5 never cuts off a fast breakout at `TP1`.

### 3. Gold-Calibrated Reversal Guard ("Bank It" Without Choking Trades)
Normal Gold wicks (`$0.80–$1.40`) won't kick you out of winning trades early:
- **Stays Off Mid-Range:** Only arms on the runner once profit reaches **`85%` of `TP1`** (`ARM_THRESHOLD_PCT = 0.85`).
- **30% Giveback + Gold Noise Floor:** Once armed, it banks the runner at market **only** if open profit drops **`30%` from its peak** (`capped at $4.50` max giveback after `TP1`) **AND** price pulls back at least **`$1.50` (`15 pips`)** (or `0.35 x M1 ATR(14)` when volatility is high).
- **Milestone Hard-SL Locks:** As milestones clear, hard MT5 Stop-Loss steps cleanly (`BE -> TP1 -> TP2 -> TP3`) without dragging a tight trailing stop right behind price.
- **Spread Spike Filter:** Temporarily pauses reversal exits during sudden news/rollover spread spikes (automatically adapts to brokers like PU Prime where normal Gold spread is `~$0.52`).
- **Base-Layer `TP1 Shortfall` (`L1–L3` Only):** If price gets within `$0.80` (`8 pips`) of `TP1` and reverses by `$2.00` (`20 pips` for 3 ticks), **only** `L1–L3` bank early—your deepest runner stays open at Breakeven.

### 4. Instant Channel Commands, Edits & Deleted Messages
- **Fast Breakeven (No Reply Needed, Per-Channel Only):** If a provider posts `"BE"`, `"Go breakeven!"`, or `"break even your entries"` in a channel (even without replying to the signal), the bot moves **all 4 layers (`L1–L4`)** of **that channel's trade only** to Breakeven (`+2 pips`, with instant `0.0p` fallback and `0.5s` auto-retry if a layer is slightly underwater). It never touches trades from other channels.
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
| `BE_OFFSET_PIPS` | `2.0` | Extra `+2 pips` (`$0.20`) added to entry on Breakeven to cover fees |
| `ARM_THRESHOLD_PCT` | `0.85` | Runner Reversal Guard arms at `85%` of `TP1` |
| `RUNNER_EARLY_TP_LIFT_PCT` | `0.70` | Lifts runner's broker TP to `TP2` at `70%` of `TP1` |
| `GIVEBACK_PCT` | `0.30` | Banks runner on a `30%` drop from peak open profit |
| `MIN_GOLD_PULLBACK_DOLLARS` | `1.50` | Minimum `$1.50` (`15 pips`) drop required so Gold wicks are ignored |
| `MAX_GIVEBACK_DOLLARS` | `4.50` | Max dollar pullback allowed after `TP1` (`TP2–TP4` stages) |
| `ADAPTIVE_SPREAD_CEILING_DOLLARS` | `0.80` | Adaptive spread ceiling before pausing reversal exits on spikes |
| `SHORTFALL_ARM_PIPS` / `SHORTFALL_PULLBACK_PIPS` | `8.0` / `20.0` | Base-layer (`L1–L3`) pre-TP1 protection (`$0.80` arm, `$2.00` pullback, `3` ticks) |
| `ENFORCE_HIGH_RISK_LOT_CAP` | `true` | Caps total active lots safely based on live account balance (`£10` per `0.01` lot) |
| `CLOSE_ON_SIGNAL_DELETED` | `true` | Auto-closes/cancels a trade if its Telegram signal is deleted |

---

## Testing Safely (Zero Live Orders)

Run the full isolated test suite and simulator anytime without touching your live account or live state files:

```powershell
$env:DRY_RUN="true"
python -m pytest -q
python simulate.py
python test_examples.py
```

- **`pytest`:** `139` unit & scenario tests covering parsing, 4-layer execution, breakeven, edits, deletes, reconnect catch-up, and the Gold Reversal Guard.
- **`simulate.py`:** `10`-step end-to-end simulation in an isolated temp folder.
- **Deep Technical Specs:** See `ARCHITECTURE_v3.md` for internal state-machine and engineering contracts.
