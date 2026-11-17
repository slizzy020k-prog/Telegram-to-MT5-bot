# Ultimate Telegram-to-MT5 XAUUSD Python Signal Copier

## 1. Project Overview
A zero-cost, self-hosted Python bridge connecting **Telegram** (via `Telethon`) to **MetaTrader 5** (via the official MetaQuotes `MetaTrader5` Python library on Windows). Built exclusively for **Gold (`XAUUSD`)** signal providers who post rapid `"Buy now"` or `"Buy limit 3895-3887"` entries without always typing `"XAUUSD"`, edit messages with zones/SL/TPs, manage active trades via replies, and reactivate old zones using `"Active"`.

### Core Capabilities
* **XAUUSD-Only Default Execution (No Symbol Name Required):** Defaults every valid directional signal (`BUY`, `SELL`, `BUY LIMIT`, `SELL LIMIT`, `BUY STOP`, `SELL STOP`, `LONG`, `SHORT`) straight to **`XAUUSD`**—even if the provider never writes `"XAUUSD"` or `"GOLD"` in the message—while automatically rejecting signals that explicitly name other pairs (`EURUSD`, `GBPUSD`, `US30`, `NAS100`, etc.).
* **Asymmetric 3-Layer Zone Scaling (`0.01` / `0.01` / `0.02` = `0.04` Max Exposure):** Splits zone signals into **3 distinct layers** weighted toward the best entry price—never exceeding 3 orders per signal:
  * **Layer 1 — Top / Entry Price (`0.01` lot):** e.g., `3895.00`
  * **Layer 2 — Midpoint (`0.01` lot):** e.g., `3891.00`
  * **Layer 3 — Lowest / Bottom Layer (`0.02` lot):** e.g., `3887.00`
* **Uniform Take Profit (`TP1 ± 0.50` Front-Run Buffer):** All 3 layers carry a hard MT5 Take Profit at `TP1 - 0.50` (on `BUY`) or `TP1 + 0.50` (on `SELL`).
* **20-Pip TP1 Proximity Auto-Close & Guard (`TP1_CLOSE_PROXIMITY_PIPS = 20`):**
  * **Active Trade Auto-Close:** If live price comes within **20 pips (`$2.00` on Gold)** of `raw_tp1` (e.g., reaching `3898.00` when `TP1 = 3900.00`), the bot immediately closes all open positions for that signal to bank profit and deletes any unfilled `MID` or `BOTTOM` pending limit orders.
  * **Late Entry / Reactivation Block:** If a signal or `"Active"` reply arrives when price is already within 20 pips of `raw_tp1` (or past it), the bot skips opening new orders because the risk-to-reward is gone.
* **Selective Breakeven (`BE` on Top & Middle Layers Only):** When `"BE"` is called, the bot moves Stop Loss to entry price **only on Layer 1 (`0.01` Top) and Layer 2 (`0.01` Middle)**, leaving **Layer 3 (`0.02` Bottom)** untouched at the original Stop Loss so the `0.02` position isn't stopped out early by minor pullbacks.
* **Wick-Tolerant Entry Protection:** Allows entries even if price temporarily wicks below the lowest layer (`3887`), provided price is still at least `20` pips away from Stop Loss and more than `20` pips away from `raw_tp1`.
* **Edit-Aware State Machine:** Tracks Telegram `message_id`s in persistent JSON storage. When `"Buy now"` is edited seconds later to add `3895-3887`, `SL`, and `TP`, the bot updates Layer 1 (`0.01`) and places Layer 2 (`0.01` Mid) and Layer 3 (`0.02` Bottom)—strictly capping total orders at `3`.
* **Instant & Old-Zone `"Active"` Reactivation:** New signals execute immediately without waiting for `"Active"`. Replying `"Active"` to an active zone ignores duplicates; replying `"Active"` to an **old cancelled/expired zone** fetches the original message and re-enters fresh `0.01 / 0.01 / 0.02` layers (unless price is within 20 pips of TP1).
* **Multiple Concurrent Signals & High-Spread Trading:** Takes all valid incoming signals concurrently without artificial signal count limits, and allows trading regardless of spread spikes to maximize execution and profit potential.
* **Smart Limit-vs-Market Layer Routing:** Evaluates each layer individually against live broker Ask/Bid at the exact millisecond of placement (e.g. if ask <= target on BUY, executes Market Buy; otherwise places BUY LIMIT).
* **Single-Price vs. Zone Differentiation:** Automatically detects two-price zones (`3895-3887`) and splits into 3 layers (`0.01/0.01/0.02`), while single-price entries (`Buy 3890`) place exactly 1 order (`0.01` lot) with `max_orders = 1`.
* **Live Algo Trading Button Monitor (Every 30s & Pre-Order):** Verifies MT5 `trade_allowed` state continuously. If Algo Trading is disabled in the terminal, blocks orders cleanly with a direct Telegram Saved Messages alert before MT5 returns error `10027`.
* **1-Second MT5 State Reconciliation:** Synchronizes `signals_state.json` against live MT5 positions and orders every second: automatically moves filled limit orders to market positions, and marks trades `CLOSED` if exited manually in the MT5 mobile app or closed via SL/TP.
* **Telegram Saved Messages Notifications & Remote Commands:** Delivers execution receipts, modifications, TP proximity triggers, and risk alerts straight to your personal Telegram `"Saved Messages"` chat. You can also send `/status`, `/trades`, or `/ping` to your Saved Messages to get a real-time health check on the bot from your phone.
* **Magic-Number Safety Sweep:** When proximity TP or full close triggers, cross-references `MAGIC_NUMBER = 777001` across all open positions so no filled limit order is ever orphaned.

---

## 2. System Architecture & File Structure

```text
telegram-mt5-copier/
├── main.py                 # Telethon event loop, remote status & 1s TP1 proximity watcher
├── config.py               # User settings: API keys, DEFAULT_SYMBOL="XAUUSD", 0.01/0.01/0.02 lots
├── parser.py               # XAUUSD-default regex parser: noise/other-pair blocker, zones, & reply commands
├── mt5_executor.py         # MetaTrader 5 3-layer execution, selective BE, proximity close, & cleanup
├── state_manager.py        # Thread-safe persistent JSON database linking message_id -> tagged layer tickets
├── auth.py                 # Interactive Telegram login script for generating session file
├── start_copier.bat        # Windows 24/7 auto-restart watchdog supervisor
├── simulate.py             # Instant zero-risk 6-step pipeline test simulator
├── signals_state.json      # Auto-generated state file (tracks which ticket belongs to Layer 1, 2, or 3)
├── copier.log              # Rotating log file (capped at 5 MB x 5 backups)
├── requirements.txt        # Python dependencies
├── .env                    # Active credentials and configuration settings (gitignored)
├── .env.example            # Template configuration with placeholder values
├── AI_HANDOVER.md          # Complete technical handover & architecture guide for AI agents/devs
└── tests/                  # Pytest verification suite (66 unit & integration tests)
    ├── test_parser.py
    ├── test_state_manager.py
    ├── test_copier_scenarios.py
    ├── test_tp1_runner_and_phone_controls.py
    ├── test_profit_notifications.py
    └── test_production_edge_cases.py
```

---

## 3. Configuration Parameters (`config.py` & `.env`)

| Parameter | Default / Example | Description |
| :--- | :--- | :--- |
| `TELEGRAM_API_ID` | `12345678` | From `https://my.telegram.org` |
| `TELEGRAM_API_HASH` | `"your_api_hash"` | From `https://my.telegram.org` |
| `TELEGRAM_SESSION_NAME` | `"copier_session"` | File name of the Telethon SQLite session |
| `ALLOW_FORWARDS` | `false` | When `false`, Gate 1 drops all forwarded messages |
| `CHANNEL_IDS` | `[-1001234567890]` | List of target Telegram channel/group IDs to monitor |
| `MAGIC_NUMBER` | `777001` | Unique ID tagging bot trades so manual MT5 trades are never touched |
| `DEFAULT_SYMBOL` | `"XAUUSD.s"` | Exact broker symbol used for all signals (PU Prime default: `XAUUSD.s`) |
| `SINGLE_PRICE_LOT` | `0.01` | Lot size used when provider gives a single price instead of a zone range |
| `CHECK_ALGO_TRADING_INTERVAL` | `30` | Seconds between live checks of `mt5.terminal_info().trade_allowed` |
| `IGNORED_SYMBOLS` | `["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "US30", "NAS100", "USTEC", "BTCUSD"]` | Rejects any message explicitly naming a non-Gold pair |
| `LAYER_LOTS` | `{"TOP": 0.01, "MID": 0.01, "BOTTOM": 0.02}` | Exact lot size per layer (`0.04` max total exposure per signal) |
| `MAX_ORDERS_PER_SIGNAL` | `3` | Hard cap preventing any signal, edit, or `"Active"` reply from exceeding 3 trades |
| `TP_FRONT_RUN_OFFSET` | `0.50` | Hard broker TP set to `raw_tp1 - 0.50` on `BUY` (`3899.50`) or `raw_tp1 + 0.50` on `SELL` |
| `TP1_CLOSE_PROXIMITY_PIPS` | `20` | Closes all open layers & cancels pending limits once price is within 20 pips (`$2.00`) of `raw_tp1` |
| `BE_LAYERS` | `["TOP", "MID"]` | Only moves `TOP` (`0.01`) and `MID` (`0.01`) to Breakeven; leaves `BOTTOM` (`0.02`) untouched |
| `BE_OFFSET_PIPS` | `2.0` | Extra buffer (+$0.20 on Gold) when moving Top/Mid layers to BE to cover commission |
| `ALLOW_BELOW_ZONE_WICK` | `True` | Allows entry if price dips below the bottom layer, as long as price is safely inside SL |
| `MIN_SL_BUFFER_PIPS` | `20` | Minimum distance (`$2.00` on Gold) required between live price and SL when entering |
| `FALLBACK_SL_PIPS` | `150` | Emergency SL attached to `"Buy now"` orders until provider edits real SL |
| `MAX_SL_PIPS` | `250` | Safety cap: rejects any parsed SL > 250 pips away (blocks typos like `Sl 387`) |
| `NOTIFY_SAVED_MESSAGES` | `True` | Sends instant trade, edit, BE, proximity-close, and warning alerts to Saved Messages (`"me"`) |
| `DEDUPLICATION_WINDOW_SECONDS` | `5` | Suppresses duplicate events received within N seconds |
| `SELL_NOW_DELAY_SECONDS` | `20` | Seconds to hold unpriced `"Sell now"` / `"Buy now"` waiting for a zone/limit edit or `"TP1 hit"` before checking the recorded market point |
| `NOW_ENTRY_MAX_WAIT_SECONDS` | `180` | Max seconds after the delay to wait for price to return to a favorable level vs the recorded entry point before expiring |
| `DAILY_LOSS_CLOSE_OPEN_TRADES` | `true` | When `dailyloss` (realized loss limit) triggers, also close any open bot positions before terminating (or set `false` to leave open positions on their broker SL/TP) |
| `MAX_NEW_EDIT_AGE_SECONDS` | `180` | Max age (seconds) for an untracked message edit to be treated as a new signal (blocks old emoji reactions) |
| `FALLBACK_TP1_PIPS` | `50` | Fallback TP1 distance (`$5.00` on Gold) attached if an unpriced market trade gets no TP edit |
| `UNPRICED_TP_GRACE_SECONDS` | `45` | Seconds to wait before attaching `FALLBACK_TP1_PIPS` to an unpriced market order |
| `ENFORCE_HIGH_RISK_LOT_CAP` | `true` | Enforces the Professional Risk Guide High-Risk total lot cap (`floor(Balance/10)*0.01`) |
| `DAILY_LOSS_PAUSE_PCT` | `50.0` | At 50% daily/floating loss, pauses **new** signals while holding open trades naturally to SL/TP |
| `EMERGENCY_CUTOFF_LOSS_PCT` | `90.0` | At 90% loss of account balance, immediately cuts losing positions and cancels pending limits |
| `DRY_RUN` | `false` | When `true`, simulates MT5 execution without live broker orders |

---

## 4. 6-Gate Sequential Processing Pipeline (`parser.py`)

Every message passes through 6 sequential gates before touching MetaTrader 5:

```text
Raw Telegram Message
   │
   ├─► Gate 1: Forward & Empty Message Check (discards empty / forwards)
   ├─► Gate 2: Announcement, Recap & Ignored Symbols Blocker (EURUSD/GBPUSD/noise)
   ├─► Gate 3: Trade Management & Reply Classifier (BE / Close / "Active")
   ├─► Gate 4: XAUUSD-Default Direction & 3-Layer Zone Extraction (0.01/0.01/0.02)
   ├─► Gate 5: Price & Typo Sanity Validation (SL distance cap & directional logic)
   └─► Output: Clean Structured SignalAction Object -> mt5_executor.py
```

### Gate Rules & Behaviors
1. **Gate 1 (Forward & Empty):** Discards empty texts, images without captions, and forwarded messages if `ALLOW_FORWARDS=False`.
2. **Gate 2 (Noise & Ignored Symbols):**
   - Blocks any message containing an ignored pair from `IGNORED_SYMBOLS`.
   - Blocks risk-sizing noise: `["manage your risk", "high risk", "mid risk", "low risk", "£50 account"]`.
   - Blocks victory recaps: `["tp hit", "smashed", "pips secured", "profit booked", "boom"]` (bypassed on edits or replies).
3. **Gate 3 (Trade Management):**
   - Breakeven: `["be", "breakeven", "sl to entry", "risk free"]` -> `ACTION_BREAKEVEN`.
   - Reactivation: `["active", "reactivate", "valid again"]` -> `ACTION_REACTIVATE`.
   - Partial Close: `["close half", "partial", "secure profit", "50%"]` -> `ACTION_PARTIAL_CLOSE`.
   - Full Close: `["close all", "close now", "cancel", "delete limit"]` -> `ACTION_FULL_CLOSE`.
4. **Gate 4 (Direction & Zone Extraction):**
   - Recognizes `BUY`, `SELL`, `BUY LIMIT`, `SELL LIMIT`, `BUY STOP`, `SELL STOP`, `LONG`, `SHORT`.
   - Sets symbol to `DEFAULT_SYMBOL` (`XAUUSD`) automatically.
   - Extracts 3-layer zone: `3895-3887` -> `top=3895.00, mid=3891.00, bottom=3887.00`.
   - Extracts TP1 with `- 0.50` front-run offset and calculates proximity price `raw_tp1 ± $2.00`.
5. **Gate 5 (Price Sanity):**
   - Ensures `abs(reference_price - SL) <= 25.00` points (250 pips). Prevents typos like `Sl 387` from wiping capital.
   - Verifies directional logic (`SL < price < TP` for BUY; `TP < price < SL` for SELL).

---

## 5. Execution Rules & Risk Controls

### A. 3-Layer Zone Placement (`0.01` / `0.01` / `0.02`)
For a `BUY` zone `3895-3887` with `SL = 3875` and `TP1 = 3900`:
* **Pre-Flight Checks:**
  1. MT5 connected & symbol trade mode `FULL`.
  2. Safe Price Window Check: verifies price is not already within 20 pips of TP1 (`ask < raw_tp1 - 2.00`) and is above SL (`ask > sl + 2.00`).
  *(Spread caps and signal limits disabled to take all opportunities).*
* **Layer 1 — Top (`3895.00`, `0.01` lot):** Market Buy if `ask <= 3895.00`, else `BUY LIMIT` at `3895.00`.
* **Layer 2 — Midpoint (`3891.00`, `0.01` lot):** `BUY LIMIT` at `3891.00` (or Market Buy if `ask <= 3891.00`).
* **Layer 3 — Bottom (`3887.00`, `0.02` lot):** `BUY LIMIT` at `3887.00` (or Market Buy if `ask <= 3887.00`).

### B. 20-Pip TP1 Proximity Auto-Close
The 1-second background tick watcher monitors live prices on all active signals:
* **`BUY` Trigger:** `bid >= (raw_tp1 - 2.00)` (e.g., `3898.00` when `TP1 = 3900.00`).
* **`SELL` Trigger:** `ask <= (raw_tp1 + 2.00)` (e.g., `3902.00` when `TP1 = 3900.00`).
* **Action:** Instantly closes all open market positions, cancels all pending limit orders, sets `tp1_reached = True`, and sends a notification.

### C. Selective Breakeven
* Modifies Stop Loss to `price_open ± 0.20` on **Layer 1 (`0.01` Top)** and **Layer 2 (`0.01` Mid)** only.
* **Layer 3 (`0.02` Bottom)** retains its original Stop Loss.
* Pending limit orders remain active until filled or cancelled by the TP1 proximity trigger.

### D. Message Edits & `"Active"` Reactivation
* **Scenario B Edits:** `"Buy now"` opens Layer 1 (`0.01` Market). When edited to add `3895-3887, TP 3900, SL 3875`, the bot updates Layer 1 SL/TP and places Layer 2 (`0.01` Mid Limit) and Layer 3 (`0.02` Bottom Limit)—strictly capped at 3 orders total.
* **"Active" Reactivation:** Replying `"Active"` to an old closed or expired zone re-fetches the signal, validates the safe price window, and places fresh 3-layer orders (`0.01 / 0.01 / 0.02`).

---

## 6. State Machine Schema (`signals_state.json`)

The state database maintains structured tracking of each layer:

```json
{
  "1002": {
    "timestamp": "2026-11-13T19:48:00+00:00",
    "symbol": "XAUUSD",
    "direction": "BUY",
    "layers": {
      "TOP": {
        "lot": 0.01,
        "target_price": 3895.0,
        "ticket": 4829101,
        "order_type": "MARKET",
        "status": "OPEN"
      },
      "MID": {
        "lot": 0.01,
        "target_price": 3891.0,
        "ticket": 4829102,
        "order_type": "LIMIT",
        "status": "PENDING"
      },
      "BOTTOM": {
        "lot": 0.02,
        "target_price": 3887.0,
        "ticket": 4829103,
        "order_type": "LIMIT",
        "status": "PENDING"
      }
    },
    "zone": {
      "top": 3895.0,
      "mid": 3891.0,
      "bottom": 3887.0
    },
    "market_tickets": [4829101],
    "pending_tickets": [4829102, 4829103],
    "zone_completed": true,
    "sl": 3875.0,
    "raw_tp1": 3900.0,
    "hard_broker_tp": 3899.50,
    "final_tp": 3899.50,
    "proximity_close_price": 3898.00,
    "tp1_reached": false,
    "status": "ACTIVE",
    "raw_text": "Buy 3895-3887, TP 3900, SL 3875"
  }
}
```

---

## 7. Verification & Testing

The project includes 22 automated tests covering parser gates, state persistence, 3-layer asymmetric zone placement, selective breakeven, proximity auto-close, edit workflows, and "Active" reactivation.

### Running Pytest
```powershell
cd C:\Users\kambo\Code\telegram-mt5-copier
python -m pytest -v -o pythonpath=.
```

**Expected Result:**
```text
tests/test_copier_scenarios.py::test_scenario_a_asymmetric_zone_execution PASSED
tests/test_copier_scenarios.py::test_scenario_b_edit_workflow PASSED
tests/test_copier_scenarios.py::test_selective_breakeven PASSED
tests/test_copier_scenarios.py::test_proximity_close_rule PASSED
tests/test_copier_scenarios.py::test_end_to_end_event_handlers PASSED
tests/test_parser.py::test_spec_test_1_standard_zone_signal PASSED
tests/test_parser.py::test_xauusd_default_without_symbol_name PASSED
tests/test_parser.py::test_ignored_symbols_rejected PASSED
tests/test_parser.py::test_spec_test_2_sl_typo_guard PASSED
tests/test_parser.py::test_spec_test_3_initial_buy_now PASSED
tests/test_parser.py::test_spec_test_4_risk_management_noise PASSED
tests/test_parser.py::test_spec_test_5_victory_recap_noise PASSED
tests/test_parser.py::test_gate_1_forward_and_empty PASSED
tests/test_parser.py::test_gate_3_trade_management PASSED
tests/test_parser.py::test_sell_zone_signal PASSED
tests/test_parser.py::test_labeled_tps_out_of_order PASSED
tests/test_parser.py::test_edit_bypasses_victory_recap PASSED
tests/test_parser.py::test_lot_range_noise_not_treated_as_zone PASSED
telegram-mt5-copier/tests/test_copier_scenarios.py::test_single_price_smart_routing_and_execution PASSED
telegram-mt5-copier/tests/test_copier_scenarios.py::test_algo_trading_guard PASSED
telegram-mt5-copier/tests/test_copier_scenarios.py::test_reconciliation_logic PASSED
tests/test_state_manager.py::test_state_manager_create_and_load PASSED
tests/test_state_manager.py::test_state_manager_layer_updates PASSED
tests/test_state_manager.py::test_state_manager_active_and_counts PASSED
tests/test_state_manager.py::test_state_manager_cleanup_old_signals PASSED

============================= 25 passed in 1.04s =============================
```

---

## 8. Setup & Deployment Guide

### Step 1: Prerequisites
1. **Windows 10/11** with **MetaTrader 5** installed, logged into your broker account, and running.
2. In MT5, go to **Tools -> Options -> Expert Advisors** and enable:
   - `Allow Algorithmic Trading`
   - `Allow DLL imports`
3. Make sure the **Algo Trading** button in the top MT5 toolbar is **Green (Enabled)**.
4. **Python 3.10+** installed (check **"Add Python to PATH"** during installation).

### Step 2: Clone & Install Dependencies
```powershell
git clone https://github.com/slizzy020k-prog/Telegram-to-MT5-bot.git
cd Telegram-to-MT5-bot
pip install -r requirements.txt
```

### Step 3: Get Your Telegram API Credentials & Channel IDs
1. Go to **[my.telegram.org](https://my.telegram.org)** -> log in with your phone number -> open **API development tools** -> create an app to get your `API ID` and `API Hash`.
2. Find your signal channel ID(s) (e.g. forward a message from the channel to `@userinfobot` or `@RawDataBot` on Telegram; supergroup/channel IDs look like `-1001234567890`).

### Step 4: Configure `.env`
Copy the example template and fill in your own details (`.env` is gitignored so your secrets stay local):
```powershell
copy .env.example .env
```
Edit `.env`:
```ini
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_telegram_api_hash_here
CHANNEL_IDS=-1001234567890
DEFAULT_SYMBOL=XAUUSD.s
MAGIC_NUMBER=777001
DRY_RUN=false
```
> **Note:** Check your broker's exact Gold symbol in MT5 Market Watch (e.g. `XAUUSD`, `XAUUSD.s`, `GOLD`).

### Step 5: Authenticate Telegram Session (One-Time)
Run the auth helper to log in to your Telegram account and create your local `.session` file:
```powershell
python auth.py
```
Enter your phone number and the verification code sent to your Telegram app.

### Step 6: Verify MT5 Connection & Health
With MetaTrader 5 open and Algo Trading enabled, run:
```powershell
python check_algo.py
```
Confirm all checks pass (`MT5 Terminal: CONNECTED`, `Algo Trading Button: ENABLED`, `Live Quote Available`).

### Step 7: Run the Copier

**Option A (24/7 Watchdog Supervisor with Auto-Restart):**
Double-click `start_copier.bat` or run:
```cmd
start_copier.bat
```

**Option B (Standard Direct Launch):**
```powershell
python main.py
```
The console will display:
```text
=================================================================
  Ultimate Telegram-to-MT5 XAUUSD Python Signal Copier v2
=================================================================
[copier] Connecting to MetaTrader 5...
[copier] MetaTrader 5 connection established successfully.
[copier] Initializing Telegram Client (Session: copier_session)...
[copier] Monitoring Channel IDs: [-1001234567890]
[copier] Copier event handlers registered. Starting Telethon client...
[copier] Telegram Copier is LIVE and actively listening for signals!
[copier.mt5] TP1 20-Pip Proximity Watcher started (1.0s loop).
```
Once running, send `/status` or `/testorder` to your **Saved Messages** in Telegram to verify live end-to-end execution.

---

## 9. Phone Commands & Remote Control (Telegram Saved Messages)

Send any of the following commands directly to your **Saved Messages** (`"me"`) in Telegram:

| Command | Category | Description |
| :--- | :--- | :--- |
| `dailywin` / `/dailywin` | Profit Target | Interactive toggle to set a Daily Win Limit (e.g. reply `£20` or type `dailywin 20`, or `dailywin off`). Once the bot makes that profit from trading today, it closes all open trades, cancels pending limits, sends a Saved Messages notification, and terminates the session for no more trading. |
| `dailyloss` / `/dailyloss` | Loss Limit | Interactive toggle to set a **realized-only** Daily Loss Limit (e.g. reply `£20`, `dailyloss 20`, or `dailyloss off`). Counted **strictly from closed / Stop-Loss trades** leaving a negative balance/P&L for the broker trading day (open floating trades do **not** trigger it). Once reached, cancels pending limits, notifies Saved Messages, and terminates the session (`exit 89`). |
| `/status` | Diagnostics | Returns real-time health check: MT5 connection, Algo Trading button, floating bot P&L, Daily Win Limit & Daily Loss Limit status, layer states (`OPEN`/`PENDING`/`CLOSED`), shortfall guard arming, and active runner details. |
| `/bank` | Live Control | Manually triggers the TP1 profit banking and runner scaling rule on the latest active trade. |
| `/closeall` / `/panic` | Emergency | Emergency panic button: instantly closes all open bot positions (`MAGIC_NUMBER = 777001`) at market and deletes all pending limit orders. |
| `/sim_tp1 1` | Simulation | Simulates TP1 hit for **Shallow Pullback** (only Layer 1 active) -> banks Layer 1 and cancels Layers 2 & 3 limits. |
| `/sim_tp1 2` | Simulation | Simulates TP1 hit for **Medium Pullback** (Layers 1 & 2 active) -> banks Layer 1 and designates Layer 2 (`0.01`) as runner with Stop Loss at `TP1 - 0.50`. |
| `/sim_tp1 3` | Simulation | Simulates TP1 hit for **Full Zone Fill** (all 3 layers active) -> banks Layers 1 & 2 (`0.02` total) and leaves Layer 3 (`0.02` bottom entry) running toward TP2. |
| `/sim_shortfall` | Simulation | Simulates price peaking at `3899.20` (within 20 pips of `3900.00` TP1) and dropping to `3898.40` -> verifies instant market bank of all open layers before reversal. |
| `/sim_trail 2` / `3` | Simulation | Simulates runner one-way trailing stop ratchets and TP2 near-miss shortfall guard. |
| `/testorder` | Broker Test | Places a safe `BUY LIMIT` $20 below current market on MT5 and auto-cancels it after 5 seconds to test broker connectivity. |


---

## 10. Latest Behaviour Updates (v2.2)

### Recorded Market Point, Bad-Position Entry Guard & Delay/Zone TP1 Cancellation
- Unpriced **`"Sell now"`** and **`"Buy now"`** record the live market entry price (`bid` for SELL, `ask` for BUY) at signal arrival and wait `SELL_NOW_DELAY_SECONDS` (`20s`) in `PENDING_DELAY`.
- **Zone / Limit Edit within the Delay:** Cancels the timer and runs pre-flight checks before placing the 3-layer zone or single limit order.
- **No Zone Edit After Delay (Bad-Position Guard):**
  - **SELL:** Only activates `Sell now` when live `bid >= recorded_entry_price` (price is higher/better than when `"Sell now"` was posted). If price already dropped lower, it holds off up to `NOW_ENTRY_MAX_WAIT_SECONDS` (`180s`) instead of selling into a bad position at the bottom.
  - **BUY (Opposite):** Only activates `Buy now` when live `ask <= recorded_entry_price` (price is lower/better than when `"Buy now"` was posted).
- **TP1 Hit Cancellation (Delay & Zone):** If the channel posts `"TP1 hit"` / `"TP1 ✅"` (via **reply**, **edit**, or a **short standalone alert**) OR live MT5 price reaches `raw_tp1` before entry, the copier cancels the pending timer / deletes all unfilled zone pending limits and marks the signal `CLOSED` with zero entries. (If some zone layers already filled on a reply/edit/live TP1 hit, `handle_tp1_scaling_and_select_runner()` cancels all remaining pending limits and banks TP1.)

### `"close worst entries now and hold layers"`
- Ranks open positions by live profit, moves the **worst** one(s) to breakeven (+`BE_OFFSET_PIPS`) and leaves the **top two (bottom layers)** running.
- Sets `hold_past_tp2`: the runner then targets **TP3** (if provided) and trails SL instead of closing at TP2.

### Safety fixes
- Orders rejected by the broker (e.g. `No money`) never create an `ACTIVE` signal; an `Order Rejected by MT5` alert with the reason is sent instead.
- Broker-side closes (hard TP, trailed/BE SL, manual close in the MT5 app) now send a profit + Balance/Credit/Equity alert.
- `Active` only reactivates on a reply or a standalone `Active` message.
- Non-reply chatter can't trigger management: signal-like messages and messages > 10 words are ignored, and ambiguous words (`be`, `cut`, `exit`, `secure`, `cancel`) only count in 1-2 word messages or replies.
- Edited zones place a market order for any layer price has already crossed (avoids `Invalid price` rejections).
- MT5 `No changes` on SL/TP modify is treated as success.
- **Professional Risk Guide & Live Terminal Balance Monitor:**
  - Live console window title bar and terminal log update in real time with `Balance`, `Credit`, `Equity`, and `Active Lots / High-Risk Cap`.
  - Dynamic thresholds for any custom account balance:
    - `Low Risk` = `max(0.01, floor(Balance / 100) * 0.01)` (`£50`→`0.01`, `£100`→`0.01`, `£200`→`0.02`, `£500`→`0.05`, `£1000`→`0.10`)
    - `Mid Risk` = `max(0.01, floor(Balance / 20) * 0.01)` (`£50`→`0.02`, `£100`→`0.05`, `£200`→`0.10`, `£500`→`0.25`, `£1000`→`0.50`)
    - `High Risk Cap` = `max(0.01, floor(Balance / 10) * 0.01)` (`£50`→`0.05`, `£100`→`0.10`, `£120`→`0.12`, `£200`→`0.20`, `£500`→`0.50`, `£1000`→`1.00`)
  - Sends alerts when total active lots cross **Low Risk** or **Mid Risk**, and **hard-caps** total active + pending lots at **High Risk** (`ENFORCE_HIGH_RISK_LOT_CAP=true`).
- **Two-Stage Daily Loss & Account Drawdown Protection (`DAILY_LOSS_PAUSE_PCT=50.0`, `EMERGENCY_CUTOFF_LOSS_PCT=90.0`):**
  - **At 50% loss of account balance:** Pauses **new** signals for the day while **holding existing open trades naturally** to their SL or TP.
  - **At 90% loss of account balance:** Urgency cut-off immediately closes all losing open positions at market and cancels all pending limit orders to save remaining capital.
- **Unfilled Zone Expiry at TP1:** If price hits `TP1` before any pending limit order fills, all pending limits for that zone are automatically cancelled so old limits cannot fill hours later on a reversal.
- **Old Message Reaction / Edit Filter:** Untracked messages older than `MAX_NEW_EDIT_AGE_SECONDS` (`180s`) are ignored if edited or reacted to later.
- **Fallback TP1 on Unpriced Market Entries:** If a `"Buy now"` / `"Sell now"` trade still has no `TP` after `UNPRICED_TP_GRACE_SECONDS` (`45s`), a fallback `TP1` (`FALLBACK_TP1_PIPS = 50` pips / `$5.00`) is attached automatically.
- **Provider Reply `"TP1 hit"` Scaling:** If the provider replies `"TP1 hit"` / `"Tp1 was hit on this"` to an active signal, TP1 scaling & runner lock trigger immediately.
- **Daily Win Limit Toggle (`dailywin` in Saved Messages):** Type `dailywin` in Saved Messages and reply with your daily profit target (e.g. `20` or `£20`, or one-step `dailywin 20` / `dailywin off`). Once today's net trading profit (`realized + floating`) reaches the target, the copier automatically banks all open positions, cancels pending limit orders, posts a completion summary to Saved Messages, and terminates the session (exit code `88`, stopping the `.bat` supervisor loop so no more trades are taken).
- **Realized-Only Daily Loss Limit (`dailyloss` in Saved Messages):** Type `dailyloss` (2-step prompt) or `dailyloss 20` / `dailyloss £20` / `dailyloss off` in Saved Messages. Uses `get_realized_today_pnl()` (broker-server trading day closed `BUY`/`SELL` deals only — excluding balance/credit deals and **never** triggered by open floating drawdown). Once Stop-Loss / closed losses reach `-limit`, cancels pending limits & delayed entries, closes open bot exposure (if `DAILY_LOSS_CLOSE_OPEN_TRADES=true`), notifies Saved Messages, and exits with code `89` (stopping the `.bat` supervisor loop). Relaunching on the same broker day enters `LOCKED` mode until next trading day or `dailyloss off`.

### Tests
`python -m pytest tests -o pythonpath=. -q` -> 66 tests.
