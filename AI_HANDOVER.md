# AI Handover & Architecture Reference (`AI_HANDOVER.md`)

This document provides a complete technical handover for any AI coding agent or developer maintaining or extending the **Telegram-to-MT5 Autonomous XAUUSD Copier Pro (v2.1)**.

---

## 1. System Purpose & Core Invariants

1. **Gold (`XAUUSD.s`) Only:**
   - Default broker symbol is `XAUUSD.s` (PU Prime; auto-resolves common suffixes like `.s`, `.m`, `.pro`, `XAUUSD`, `GOLD` via `MT5Executor.resolve_symbol`).
   - Any message mentioning a non-Gold symbol in `IGNORED_SYMBOLS` (`EURUSD`, `GBPUSD`, `USDJPY`, `GBPJPY`, `US30`, `NAS100`, `USTEC`, `BTCUSD`) is rejected in Gate 2.
   - Gold pip math: **`1 pip = $0.10` (`0.10` price points)**; `10 pips = $1.00`; `20 pips = $2.00`; `150 pips = $15.00`.
2. **Asymmetric 3-Layer Zone Sizing (`0.04` Lots Max per Signal):**
   - `TOP` (Layer 1): `0.01` lot — targets `final_tp` (`raw_tp1 - 0.50` on `BUY`, `raw_tp1 + 0.50` on `SELL`).
   - `MID` (Layer 2): `0.01` lot — targets `runner_tp` (`raw_tp2`, or `raw_tp1 ± 10.0` if no TP2).
   - `BOTTOM` (Layer 3): `0.02` lot (best price in the zone: lowest price on `BUY`, highest price on `SELL`) — targets `runner_tp`.
   - Single-price signals (e.g. `Buy limit 3890`) place **1 order** (`SINGLE_PRICE_LOT = 0.01`) using price-aware routing (`Market` if live price is already at/better than entry, `Limit` otherwise).
3. **Concurrent Multi-Channel Signals + Dynamic High-Risk Lot Cap:**
   - Multiple signals can run simultaneously across monitored channels, bounded by the **Professional Risk Guide High-Risk Lot Cap** (`ENFORCE_HIGH_RISK_LOT_CAP = true`).
4. **Magic Number Isolation:**
   - All orders use `MAGIC_NUMBER = 777001`. Manual trades without this magic number are not modified by normal signal operations.

---

## 2. File-by-File Architecture

| File | Responsibility |
| :--- | :--- |
| `config.py` | Loads `.env` and exposes all runtime constants, risk parameters, lot sizes, and file paths. |
| `parser.py` | Stateless **6-Gate Sequential Pipeline** (`parse_message`) returning a `SignalAction` dict/object or `None`. |
| `mt5_executor.py` | `MT5Executor` class + `calculate_risk_guide_thresholds()`: handles MT5 connection, symbol resolution, pre-flight checks, 3-layer order placement, TP1 scaling, runner trailing stop, breakeven, profit history lookup, risk guide lot capping, and 50%/90% daily drawdown checks. |
| `state_manager.py` | Thread-safe `StateManager` persisting `signals_state.json` via atomic `.tmp` file replacement (`_save_state_locked`). |
| `main.py` | Async Telethon listener (`handle_new_message`, `handle_message_edited`, `handle_saved_messages_command`), 20s `"Sell now"` timer (`_delayed_sell_now_task`), live console title/log monitor (`update_live_terminal_status`), and 1-second background watcher (`tp1_proximity_watcher_task`). |
| `start_copier.bat` | Windows batch supervisor with `color 0E` styling and a 5-second auto-restart loop (requires CRLF `\r\n` line endings and no unescaped `|` characters). |
| `tests/` | 57-test `pytest` suite (`pytest tests -o pythonpath=. -v`) covering parser gates, 3-layer execution, edits, TP1 scaling, runner trailing, notifications, risk guide caps, and daily drawdown cut-offs. |

---

## 3. Detailed Functional Flows

### A. 6-Gate Parser Pipeline (`parser.py`)
1. **Gate 1 (`gate_1_check_forward_and_empty`):** Drops empty messages and forwarded messages (`ALLOW_FORWARDS = false`).
2. **Gate 2 (`gate_2_check_announcements_and_recaps`):** Drops `IGNORED_SYMBOLS`, account risk sizing tables (`ACCOUNT_RISK_BLOCKERS`), and past-tense victory recaps (`VICTORY_RECAP_BLOCKERS`, unless `is_reply=True` or `is_edit=True`).
3. **Gate 3 (`gate_3_classify_trade_management`):**
   - Non-reply chatter guard: ignores non-reply messages that look like priced entry signals or exceed 10 words; ambiguous single words (`be`, `secure`, `exit`, `cut`, `cancel`, `invalidate`) only match on replies or 1–2 word standalone commands.
   - Classification priority:
     1. `ACTION_BREAKEVEN` (`be`, `breakeven`, `sl to entry`, `risk free`, `move sl`)
     2. `ACTION_CLOSE_WORST_HOLD_LAYERS` (`close worst`, `worst entry/entries`, `hold layers`, `close bad entries`)
     3. `ACTION_REACTIVATE` (`active`, `reactivate`, `valid again` — **only** when `reply_to_msg_id` is set or as a standalone command)
     4. `ACTION_PARTIAL_CLOSE` (`close half`, `partial`, `partials`, `secure profit`, `50%`, `bank profit`)
     5. `ACTION_FULL_CLOSE` (standalone `close`, `close all`, `close now`, `exit`, `cut`, `invalidate`, `cancel`, `delete limit`)
     6. `ACTION_TP1_HIT_REPLY` (`tp1 hit`, `tp1 was hit`, `tp1 smashed` — **only** when `is_reply=True`)
4. **Gate 4 (`gate_4_extract_signal`):** Extracts `BUY`/`SELL` direction, 3-tier zone (`top`, `mid`, `bottom`) or `single_price` or `is_instant_now`, `raw_tp1`, `raw_tp2`, `raw_tp3`, `final_tp`, `proximity_close_price`, and `sl`.
   - `extract_tps()` merges labeled (`TP1`, `TP2`) and unlabeled (`TP 3900`) targets sequentially so unlabeled `TP1` is never lost when `TP2` is labeled.
5. **Gate 5 (`gate_5_validate_price_sanity`):** Rejects missing-digit typos (`SL < 1000` on Gold), SLs exceeding `MAX_SL_PIPS` (`350` pips / `$35.00`), or directionally inverted SLs/TPs (`sl_valid = False` -> falls back to `FALLBACK_SL_PIPS = 150` pips).

---

### B. `"Buy now"` vs `"Sell now"` (20-Second Grace Window)
- **`"Buy now"` (unpriced):** Executes Layer 1 (`0.01` lot) at market immediately with fallback SL (`150` pips), saving state as `ACTIVE` and awaiting message edits for Layers 2 & 3.
- **`"Sell now"` (unpriced):**
  - Saves signal with `status = "PENDING_DELAY"` (`market_tickets = []`, `pending_tickets = []`) and starts `_delayed_sell_now_task(msg_id)` for `SELL_NOW_DELAY_SECONDS` (`20s`).
  - **If edited with a zone or limit price within 20s:** Cancels the timer via `_cancel_delayed_sell_now(msg_id)` and places the 3-layer zone or single limit order (`status = "ACTIVE"`).
  - **If edited with SL/TP only within 20s:** Updates `sl` / `final_tp` in state while the 20s timer continues.
  - **If `close` / `cancel` arrives within 20s:** Cancels the timer and marks `status = "CLOSED"` with zero orders placed. (`BE` commands during `PENDING_DELAY` are ignored).
  - **If 20s expires with no limits:** Re-runs `executor.check_preflight()` and executes `0.01` market sell (`status = "ACTIVE"`).
  - On startup in `main()`, any orphaned `PENDING_DELAY` records from a prior run are closed safely.

---

### C. Message Edits & Old Emoji Reaction Protection (`handle_message_edited`)
- **Untracked Message Age Guard (`MAX_NEW_EDIT_AGE_SECONDS = 180`):**
  - Telegram fires `MessageEdited` when users add emoji reactions to old channel posts. If `msg_id` is not in `signals_state.json` and `(now_utc - msg.date).total_seconds() > 180`, the event is ignored so old signals never fire late trades.
- **Active Runner Protection (`curr_status == "RUNNER_ACTIVE"`):**
  - Edits never overwrite an active runner's trailed SL — they only update `tp` (`runner_tp`) if `raw_tp2` changed.
- **Price-Aware Zone Completion (`_place_edit_layer_order`):**
  - When an edit adds a zone to an existing `Market Now` trade, Layers 2 (`MID`) and 3 (`BOTTOM`) check live `tick.ask` / `tick.bid`. If live price already crossed a layer's price, it executes at market instead of sending an invalid limit price to MT5.

---

### D. 1-Second Background Watcher (`tp1_proximity_watcher_task`)
Runs every `1.0s` in `main.py`:
1. **Live Terminal & Console Title Update (`update_live_terminal_status`):**
   - Updates the Windows console title bar every second and logs `[LIVE ACCOUNT & RISK GUIDE]` whenever `Balance`, `Credit`, or `Active Lots` changes.
2. **Two-Stage Daily Drawdown Check (`check_and_enforce_daily_drawdown`):**
   - Evaluates `executor.get_daily_drawdown_status()` (see Section 4).
3. **30-Second Algo Trading Check:** Warns Saved Messages if `mt5.terminal_info().trade_allowed` is toggled OFF.
4. **1-Second MT5 State Reconciliation:**
   - Detects pending limits that filled into live positions (`status = "FILLED"`).
   - Detects positions closed on the broker side (hard TP hit, trailed/BE SL hit, or manual close in the MT5 mobile/desktop app), queries `executor.get_position_close_info(ticket)` (`DEAL_REASON_TP`, `DEAL_REASON_SL`, `MANUAL/BOT`), and sends a profit + `Balance`/`Credit`/`Equity` alert to Saved Messages.
5. **Signal Branches (for each active signal):**
   - **Branch A (`status == "RUNNER_ACTIVE"`):** Calls `executor.manage_designated_runner(sig)`:
     1. **Virtual TP1 Floor:** Closes runner at market if price pulls back to `soft_tp1_floor` (`raw_tp1 - 0.50` on `BUY`, `raw_tp1 + 0.50` on `SELL`).
     2. **Target Reached (`TP2` or `TP3`):** Uses `raw_tp3` if `sig.get("hold_past_tp2")` is set (from `"close worst entries and hold layers"`), otherwise `raw_tp2`. Closes at market when reached (`tp2_reached = True`).
     3. **TP2/TP3 Shortfall Guard:** Arms `20` pips (`$2.00`) before target (or `40` pips past `TP1` if no `TP2`); closes at market if price pulls back `8` pips (`$0.80`) from `runner_peak_price`.
     4. **One-Way Broker SL Trailing:** Ratchets hard broker SL `15` pips (`$1.50`) behind live price in `>= 5`-pip (`$0.50`) steps, never below/above `soft_tp1_floor`.
   - **Branch B (Pre-TP1 Monitoring):**
     - **Fallback TP1 (`UNPRICED_TP_GRACE_SECONDS = 45`):** If an open market trade still has `raw_tp1 = None` after `45s`, attaches a `50-pip` (`$5.00`) fallback `raw_tp1` and updates the broker TP.
     - **Unfilled Zone Expiry:** If `market_tickets` is empty, `pending_tickets` is non-empty, and live price reaches `raw_tp1`, cancels all unfilled pending limits and closes the signal.
     - **TP1 Hit (`handle_tp1_scaling_and_select_runner`):**
       - Cancels all unfilled pending limit orders.
       - If only Layer 1 (`TOP`) is open: closes Layer 1 (`100%` banked, `status = "CLOSED"`).
       - If Layers 1 & 2 (`TOP`, `MID`) are open: closes Layer 1, designates `MID` (`0.01`) as runner (`status = "RUNNER_ACTIVE"`).
       - If all 3 layers are open: closes Layers 1 & 2 (`0.02` banked), designates `BOTTOM` (`0.02`, best entry) as runner (`status = "RUNNER_ACTIVE"`).
       - Locks `soft_tp1_floor` in state and on MT5, queries realized profit via `get_position_closed_profit()`, and alerts Saved Messages.
   - **Branch C (TP1 Shortfall Guard):**
     - Arms `20` pips (`$2.00`) before `raw_tp1`. Tracks `peak_price` and closes all open positions + cancels pending limits if price pulls back `8` pips (`$0.80`) from peak before touching `raw_tp1`.

---

### E. `"close worst entries now and hold layers"` (`ACTION_CLOSE_WORST_HOLD_LAYERS`)
- Handled by `executor.apply_breakeven_worst_hold_layers()`:
  - Queries open positions for the signal and sorts by `p.profit` **descending**.
  - Keeps the **top 2 most profitable positions (bottom two layers)** untouched to run.
  - Moves SL to Breakeven (`price_open ± 2.0 pips`) on the remaining worst-performing position(s).
  - Sets `hold_past_tp2 = True` on the signal and extends held positions' broker TP to `raw_tp3` (if present) so the runner trails SL toward/above `TP3`.

---

## 4. Capital Protection: Risk Guide Lot Cap & 50%/90% Drawdown Guard

### A. Professional Risk Guide Calculator (`calculate_risk_guide_thresholds`)
For any account capital $C$ (`Balance`, or `Balance + Credit` if `Balance < 30`):
- $\text{Low Risk Lots} = \max(0.01,\ \lfloor C / 100 \rfloor \times 0.01)$
- $\text{Mid Risk Lots} = \max(0.01,\ \lfloor C / 20 \rfloor \times 0.01)$
- $\text{High Risk Cap} = \max(0.01,\ \lfloor C / 10 \rfloor \times 0.01)$

Reproduces the benchmark table (`£50`→`0.01/0.02/0.05`, `£100`→`0.01/0.05/0.10`, `£200`→`0.02/0.10/0.20`, `£500`→`0.05/0.25/0.50`, `£1000`→`0.10/0.50/1.00`) and scales linearly for custom balances (e.g. `£120`→`0.01/0.06/0.12`).
- **Low & Mid Risk:** Alert-only (`evaluate_risk_threshold_alerts` appends threshold notifications when crossed).
- **High Risk Cap:** Enforced in `check_and_cap_order_lot()` inside `execute_market_order` and `place_limit_order` — total open + pending bot lots can never exceed `high_risk_lots`.
- **Dry-Run Testing Note:** In `dry_run=True` mode, `check_and_cap_order_lot` only enforces the cap when `executor._dry_run_capital` is explicitly set (defaulting to `200.0` otherwise so standard dry-run scenario tests run unthrottled).

### B. Two-Stage Daily Loss & Account Drawdown Guard (`get_daily_drawdown_status`)
- Computes `reference_balance = max(current_balance, current_balance - min(0, realized_today_pnl))` and `total_daily_loss = realized_loss_today + floating_loss`.
- **Stage 1 — `50%` Daily Loss (`DAILY_LOSS_PAUSE_PCT = 50.0`):**
  - Blocks **new** signals in `check_preflight()`.
  - **Does NOT close existing open trades** — open positions are held naturally to their SL or TP.
- **Stage 2 — `90%` Urgency Cut-Off (`EMERGENCY_CUTOFF_LOSS_PCT = 90.0`):**
  - Checked every 1 second in `check_and_enforce_daily_drawdown()`.
  - Immediately closes all losing open positions at market and cancels all pending limit orders via `executor.emergency_cutoff_losing_trades()`.

---

## 5. Development, Testing & Git Rules

1. **Run Test Suite (57 Tests):**
   ```powershell
   python -m pytest tests -o pythonpath=. -v
   ```
2. **Never Commit Secrets or Runtime State:**
   - `.gitignore` blocks `.env`, `*.session`, `*.session-journal`, `*.log`, `signals_state.json`, `sim_state.json`, `*.tmp`, and `*.bak*`.
   - Before pushing to `https://github.com/slizzy020k-prog/Telegram-to-MT5-bot.git`, verify `git status --short` contains no credentials, session files, or logs.
3. **Restarting the Live Instance on Windows:**
   - When `Launch_XAUUSD_Copier.bat` / `start_copier.bat` is running, stopping `python.exe` alone triggers the `.bat` 5-second auto-restart loop (which reloads updated code automatically).
   - To shut down completely without restarting, stop the parent `cmd.exe` running `Launch_XAUUSD_Copier.bat` first, then stop `python.exe`.
