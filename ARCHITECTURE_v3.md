# XAUUSD Copier v3.3 — Architecture & API Contract

**Status:** LOCKED (v3.3 Back-to-Front Audit + Active-Trade SL & TP1/TP2/TP3 Adjust/Trail Prompts)  
**Scope:** Local Windows production upgrade (`telegram-mt5-copier`) — 4-Layer All-at-TP1 Entry, Dynamic Deepest-Layer Runner Detection (`1..4` layers), Stepped Momentum Scaling (`TP1 -> TP2 -> TP3 -> TP4`), Strict Activation Zone + Anti-Choke Reversal Guard, Deleted-Signal Trade Protection, Active-Trade SL/TP1–TP3 Adjust & Trail Prompt Control Plane, Async Concurrency, Ledger & Safety Hardening.

---

## 1. 4-Layer Capital Allocation & Broker Volume Safety

### 1.1 Canonical Layer Keys & Base Distribution
- **Layer Names:** `LAYER_NAMES = ("L1", "L2", "L3", "L4")`
- **Base Distribution:** `BASE_LAYER_LOTS = [0.01, 0.01, 0.01, 0.02]` (total `0.05` base lots).
- **`LAYER_LOTS` Dict:** `{"L1": b[0], "L2": b[1], "L3": b[2], "L4": b[3]}` (only the 4 canonical keys so `sum(LAYER_LOTS.values())` never double-counts).
- **`MAX_ORDERS_PER_SIGNAL`:** `4` across ALL entry types (Zone, Single-Price LIMIT/STOP, Market Now).
- **Legacy Alias Mapping:** `LEGACY_LAYER_MAP = {"TOP": "L1", "MID": "L2", "BOTTOM": "L4"}` (`L1/L2/L3` on `"BE"` = `BE_LAYERS = ["L1", "L2", "L3"]` default, keeping deepest `0.02` `L4` untouched until TP1).

### 1.2 Proportional Scaling & Broker Volume Validation (`mt5_executor.py`)
- **`normalize_lot_to_symbol(lot: float, sym_info: Any = None) -> float`**
  - Reads `volume_min` (default `0.01`), `volume_max` (default `100.0`), `volume_step` (default `0.01`) via safe numeric extraction.
  - If `lot is None or lot <= 0`: returns `0.0`.
  - Clamps upper bound to `volume_max`; if `0 < lot < volume_min`, clamps to `volume_min`.
  - **Floor-to-step** (`math.floor((lot + 1e-9) / volume_step) * volume_step`), rounded to `volume_step` decimal places, clamped to `[volume_min, volume_max]`. (Flooring prevents rounding up past risk caps.)
- **`scale_layer_lots(base_lots: Sequence[float] = BASE_LAYER_LOTS, multiplier: float = 1.0, sym_info: Any = None) -> List[float]`**
  - Validates `base_lots` has 4 positive numbers (falls back to `[0.01, 0.01, 0.01, 0.02]`), `multiplier > 0` (falls back to `1.0`).
  - Returns `[normalize_lot_to_symbol(b * multiplier, sym_info) for b in base_lots]`.
- **`MT5Executor.allocate_4layer_lots(symbol: str, base_lots: Optional[Sequence[float]] = None, multiplier: Optional[float] = None) -> Tuple[List[float], Optional[str]]`**
  - Computes `scaled = scale_layer_lots(base_lots or BASE_LAYER_LOTS, multiplier or RISK_LOT_MULTIPLIER, sym_info)`.
  - If `not ENFORCE_HIGH_RISK_LOT_CAP` (or `self.dry_run and self._dry_run_capital is None`): returns `(scaled, None)`.
  - Reads `status = self.get_risk_exposure_status()` -> `avail = status["available_lots"]`.
  - If `sum(scaled) <= avail + 1e-9`: returns `(scaled, None)`.
  - If `avail < volume_min - 1e-9`: returns `([0.0, 0.0, 0.0, 0.0], "High Risk Lot Cap Reached ...")`.
  - If `0 < avail < sum(scaled)`:
    1. Proportional floor pass: `ratio = avail / sum(scaled)`; compute `floor_to_step(s * ratio)` (values `< volume_min` become `0.0`).
    2. Deterministic remainder distribution (`rem = avail - sum(alloc)`): while `rem >= volume_min - 1e-9`, assign `volume_min` to zeroed layers in `L1 -> L2 -> L3 -> L4` order, then top-up `volume_step` up to `scaled[i]` in `L4 -> L3 -> L2 -> L1` order without ever exceeding `avail`.
    3. Returns `(alloc, f"High-Risk Cap applied: allocated {sum(alloc):.2f}/{sum(scaled):.2f} lots (avail={avail:.2f})")`.
    - Callers skip placing any layer whose allocated lot is `<= 0.0`.

---

## 2. Entry Splitting, Order Types & TP1–TP5 Derivation

### 2.1 Entry Splitting Across 4 Layers (v3.1 All-at-TP1 Entry Policy)
Every signal entry type (Zone, Single-Price LIMIT/STOP, Market Now, Edits, Unpriced Fallback) produces up to 4 orders (`L1 -> L4`) and **ALL active layers MUST initially be placed at `TP1`**:
- Broker hard TP on EVERY layer (`L1`, `L2`, `L3`, `L4`): `tp = final_tp` (`raw_tp1 - TP_FRONT_RUN_OFFSET` for `BUY`, `raw_tp1 + TP_FRONT_RUN_OFFSET` for `SELL`).
- Layer metadata on each placed layer includes `"tp": final_tp` and `"tp_stage": "TP1"`.
- Full `tp_levels` (`TP1..TP5`) and `raw_tp1..raw_tp5` are still extracted/derived and persisted on the signal record for stepped runner scaling (`TP1 -> TP2 -> TP3 -> TP4`, or `TP5` when configured).

Entry price mapping per entry type:
1. **Zone Entry (`is_zone=True`)**: Split `[0%, 33.3%, 66.7%, 100%]` (`[0.0, 1/3, 2/3, 1.0]`), rounded to 2 decimals (`compute_zone_4tiers(top, bottom, direction)`):
   - **`BUY` (`top -> bottom`):** `L1 = top`, `L2 = round(top - span/3, 2)`, `L3 = round(top - 2*span/3, 2)`, `L4 = bottom` (heaviest `0.02` at deepest price).
   - **`SELL` (`bottom -> top`):** `L1 = bottom`, `L2 = round(bottom + span/3, 2)`, `L3 = round(bottom + 2*span/3, 2)`, `L4 = top` (heaviest `0.02` at highest price).
   - Routing per layer: `MARKET` if already crossed favorably (`ask <= price` BUY / `bid >= price` SELL), else `LIMIT` (with `trade_stops_level` market fallback).
2. **Single-Price Entry (`single_price is not None`)**: All 4 layers (`L1..L4`) target `single_price`:
   - If `is_stop_order=True` (`BUY STOP` / `SELL STOP`):
     - `BUY`: `MARKET` if `ask >= single_price`, else `ORDER_TYPE_BUY_STOP` (`place_stop_order`).
     - `SELL`: `MARKET` if `bid <= single_price`, else `ORDER_TYPE_SELL_STOP` (`place_stop_order`).
     - Fallback to `MARKET` if within `trade_stops_level` tolerance.
   - Else (`LIMIT` / unqualified single price):
     - `BUY`: `MARKET` if `ask <= single_price`, else `ORDER_TYPE_BUY_LIMIT`.
     - `SELL`: `MARKET` if `bid >= single_price`, else `ORDER_TYPE_SELL_LIMIT`.
3. **Market Now (`is_instant_now=True`)**:
   - Immediate (`SELL_NOW_DELAY_SECONDS == 0`) or after delay Price Guard passes: all 4 layers (`L1..L4`) execute at `MARKET` (`execute_4layer_market`) with `tp = final_tp` (`tp_stage = "TP1"`).

### 2.2 TP1–TP5 Extraction & Derivation (`parser.py`)
- **`derive_tp_levels(tps: Sequence[float], direction: str = "BUY", pip_size: float = 0.10, step_pips: Optional[Sequence[float]] = None) -> Dict[str, float]`**
  - If `not tps`: returns `{}`.
  - Takes up to 5 extracted TPs (`tps[:5]`) sequentially as `TP1..TPk`.
  - Missing targets up to `TP5` are derived sequentially using `steps = step_pips or [TP2_STEP_PIPS, TP3_STEP_PIPS, TP4_STEP_PIPS, TP5_STEP_PIPS]` (default each `DEFAULT_TP_STEP_PIPS = 100.0` pips = `$10.00` on Gold):
    - `sign = 1.0 if direction.upper() == "BUY" else -1.0`
    - For `idx` from `len(base)` to `4`: `next_tp = round(base[-1] + sign * float(steps[idx - 1]) * pip_size, 2)`
  - Returns `{"TP1": ..., "TP2": ..., "TP3": ..., "TP4": ..., "TP5": ...}`.
- **`ZoneInfo` & `SignalAction` Contract (`parser.py`)**:
  - `compute_zone_4tiers(top: float, bottom: float, direction: str = "BUY", digits: int = 2) -> Dict[str, float]` returning `{"L1": .., "L2": .., "L3": .., "L4": ..}`.
  - `ZoneInfo(top, mid, bottom, direction="BUY")` preserves `.top`, `.mid`, `.bottom`, `.to_dict()`, and exposes `.tiers` (`Dict[str, float]`) + `.get_tiers(direction)`.
  - `SignalAction` fields:
    - `is_stop_order: bool` (`True` on `BUY STOP`/`SELL STOP`; `SL_PATTERN` must NOT treat `Buy stop <price>` as `SL`!)
    - `is_limit_order: bool`
    - `order_intent: str` (`"STOP"` | `"LIMIT"` | `"MARKET"` | `"AUTO"`)
    - `explicit_tps: List[float]` (raw extracted TPs before derivation)
    - `tp_levels: Dict[str, float]` (`{"TP1":..,"TP5":..}` or `{}`)
    - `raw_tp1`..`raw_tp5: Optional[float]` (populated from `tp_levels["TP1"]..["TP5"]`)
    - `tp_stage: str = "TP1"`
    - `max_orders: int = MAX_ORDERS_PER_SIGNAL` (`4` for all entries)
    - `layer_lots: Dict[str, float] = LAYER_LOTS`

---

## 3. Dynamic Deepest-Layer Runners, Stepped Scaling & Anti-Choke Guard

### 3.1 Configuration (`config.py`)
- `RUNNER_DEEPEST_COUNT_FULL = 1` (`int` clamped `1..2`; `1` = `L4` only when all 4 layers active, `2` = `L3 & L4` together)
- `RUNNER_MAX_TP_STAGE = 4` (`int` clamped `1..5`; default `4` scaling up to `TP4`, optionally `5` if configured)
- `RUNNER_PROACTIVE_TP_LIFT = True` (proactively lift runner broker TP to `TP{k+1}` inside tight `TPk` zone when `k < RUNNER_MAX_TP_STAGE`)
- `RUNNER_PROXIMITY_MODE = "PIPS"` (`"PIPS"` | `"PCT"` | `"BOTH"`)
- `RUNNER_PROXIMITY_BUFFER_PIPS = 10.0` (10 pips = `$1.00` tight activation zone on XAUUSD)
- `RUNNER_PROXIMITY_BUFFER_PCT = 0.05` (`0.05%` of current milestone `TPk` price)
- `RUNNER_MAX_MIDRANGE_RATIO = 0.35` (clamped `0.05..0.95`; clamps arming buffer against `|TPk - anchor|` so short targets never arm mid-range)
- `RUNNER_REVERSAL_PULLBACK_PIPS = 12.0` (12 pips = `$1.20` pullback from peak/trough)
- `RUNNER_REVERSAL_PULLBACK_PCT = 0.02` (`0.02%` of peak/trough price)
- `RUNNER_REVERSAL_CONFIRM_TICKS = 2` (`int` clamped `1..10`; consecutive ticks with pullback `>= eff_pullback` inside tight zone; resets to `0` on recovery or new peak)
- `RUNNER_REVERSAL_CANDLE_CONFIRM = False` (optional closed M1 reversal candle check: `close < open` for `BUY`, `close > open` for `SELL`)
- `RUNNER_CLEAN_BREAK_PIPS = 5.0` (5 pips = `$0.50` beyond `TPk` confirms clean breakthrough)
- `SHORTFALL_ARM_PIPS = 10.0`, `SHORTFALL_PULLBACK_PIPS = 12.0` (calibrated pre-TP1 shortfall guard using `RUNNER_MAX_MIDRANGE_RATIO` and `RUNNER_REVERSAL_CONFIRM_TICKS`)
- `CANCEL_PENDING_ON_TP1 = True`, `BE_AFTER_TP1 = True`
- `DEFAULT_TP_STEP_PIPS = 100.0`, `TP2_STEP_PIPS..TP5_STEP_PIPS` (default `100.0`)

### 3.2 Dynamic Deepest-Layer Detection (`mt5_executor.py`)
- **`select_dynamic_runners(layers: Dict[str, Any], market_tickets: Sequence[int], full_runner_count: int = RUNNER_DEEPEST_COUNT_FULL) -> Tuple[List[str], List[str]]`**
  - Evaluates currently open layers in canonical shallow-to-deep order (`"L1", "L2", "L3", "L4"`; a layer is open when `ticket` is present in `market_tickets` and `status` not in `("CLOSED", "CANCELLED")`).
  - Returns `(runner_layers, early_layers)`:
    - `n == 0`: `([], [])`
    - `n == 1` (e.g. Single-Layer or shallow pullback where only `L1` filled): `([only_layer], [])` — closes **0** layers at TP1 so that layer scales dynamically `TP1 -> TP2 -> TP3 -> TP4`!
    - `n == 2`: `([open_layers[-1]], open_layers[:-1])` (e.g. `["L2"], ["L1"]`)
    - `n == 3`: `([open_layers[-1]], open_layers[:-1])` (e.g. `["L3"], ["L1", "L2"]`)
    - `n >= 4`: `count = max(1, min(2, int(full_runner_count or 1)))` -> `(open_layers[-count:], open_layers[:-count])` (`["L4"]` / `["L1", "L2", "L3"]` when `1`; `["L3", "L4"]` / `["L1", "L2"]` when `2`).
- **`MT5Executor.prepare_deepest_runners_for_tp_breakout(signal_data, live_price=None) -> Dict[str, Any]`**
  - Calls `select_dynamic_runners(...)` and syncs `runner_layers`, `runner_tickets`, `runner_layer`, `runner_ticket` on `signal_data`.
  - Always verifies ALL `early_layers` have broker `tp = final_tp` (`tp_stage = "TP1"`), resetting any previously lifted layer back to `final_tp` if a deeper limit subsequently filled before TP1.
  - When `RUNNER_PROACTIVE_TP_LIFT=True`, `RUNNER_MAX_TP_STAGE >= 2`, `tp2` exists, and `live_price` is inside the strict tight `TP1` activation zone: lifts ONLY `runner_layers`' MT5 `tp` to `tp2` (`tp_stage = "TP2"`).

### 3.3 Strict Proximity & Calibrated Anti-Choke Pullback Thresholds
For milestone `TPk` (`k ∈ {1..RUNNER_MAX_TP_STAGE}`) with target price `target = tp_levels[f"TP{k}"]`, using dynamic `pip_size = get_pip_size(symbol)`:
- Anchor price for midrange clamp: for `k == 1`, `entry_ref` (`recorded_entry_price` or open layer `target_price`/`price_open`); for `k >= 2`, `tp_levels[f"TP{k-1}"]` (fallback `entry_ref`).
- `buf_pips = RUNNER_PROXIMITY_BUFFER_PIPS * pip_size` (`$1.00` default on Gold)
- `buf_pct = abs(target) * (RUNNER_PROXIMITY_BUFFER_PCT / 100.0)`
- Raw arming buffer: `"PIPS"` -> `buf_pips`, `"PCT"` -> `buf_pct`, `"BOTH"` -> `min(buf_pips, buf_pct)`.
- **Midrange Clamp (`RUNNER_MAX_MIDRANGE_RATIO = 0.35`):** if `anchor` is known and `leg_dist = abs(target - anchor) > 0`, `eff_buffer = min(raw_buf, leg_dist * RUNNER_MAX_MIDRANGE_RATIO)`.
- Pullback thresholds at `peak`:
  - `pb_pips = RUNNER_REVERSAL_PULLBACK_PIPS * pip_size` (`$1.20` default on Gold)
  - `pb_pct = abs(peak) * (RUNNER_REVERSAL_PULLBACK_PCT / 100.0)`
  - Effective pullback distance: `"PIPS"` -> `pb_pips`, `"PCT"` -> `pb_pct`, `"BOTH"` -> `max(pb_pips, pb_pct)`.
- **Sustained Confirmation (`RUNNER_REVERSAL_CONFIRM_TICKS = 2`):**
  - Track `runner_reversal_ticks` (and `shortfall_reversal_ticks` pre-TP1) in persisted signal state!
  - Reset counter to `0` whenever price sets a new peak/trough OR whenever `pullback < eff_pullback` (recovery).
  - Increment counter `+= 1` only when `pullback >= eff_pullback` (and M1 reversal candle passes if `RUNNER_REVERSAL_CANDLE_CONFIRM=True`).
  - Trigger Bank-It / Shortfall close ONLY when counter `>= RUNNER_REVERSAL_CONFIRM_TICKS`.

### 3.4 Stepped Momentum Scaling FSM (`TP1 -> TP2 -> TP3 -> TP4`)
State fields on signal record:
- `runner_layers: List[str]`, `runner_tickets: List[int]` (`runner_layer = runner_layers[-1]`, `runner_ticket = runner_tickets[-1]`)
- `last_runner_tickets: List[int]`, `last_runner_ticket: Optional[int]`
- `runner_milestone_idx: int` (`1..RUNNER_MAX_TP_STAGE`, starts at `1`), `runner_milestone_name: str` (`"TP1".."TP5"`)
- `runner_cleared_milestones: List[str]`, `runner_proximity_armed: bool`, `runner_reversal_ticks: int`
- `runner_peak_price: Optional[float]`, `runner_sl_floor: Optional[float]` (synced with `soft_tp1_floor`), `tp_stage: str`

Transitions on every 1s tick (`BUY` shown; `SELL` symmetric), applied across **all active `runner_tickets`**:
1. **Virtual Floor Check (`runner_sl_floor` / `soft_tp1_floor`)**:
   If `floor is not None and live_price <= floor`: close ALL `runner_tickets` at market (`"Virtual Floor Protected"`), mark `CLOSED`.
2. **Ceiling Target (`max_stage = RUNNER_MAX_TP_STAGE`, default `4`) Exit**:
   `ceiling_tp = tp_levels.get(f"TP{max_stage}")`
   If `ceiling_tp is not None and live_price >= ceiling_tp`: close ALL `runner_tickets` at market (`f"Target TP{max_stage} Reached"`), set `tp{max_stage}_reached = True`, mark `CLOSED`.
3. **Current Milestone `TPk` Clean Breakthrough (`k < max_stage`)**:
   `clean_break_level = round(target + RUNNER_CLEAN_BREAK_PIPS * pip_size, digits)`
   If `live_price >= clean_break_level`:
   - Mark `TPk` cleared (`runner_cleared_milestones.append(f"TP{k}")`).
   - **Disarm** proximity guard (`runner_proximity_armed = False`, `runner_reversal_ticks = 0`), update `runner_peak_price = live_price`.
   - Advance milestone pointer: `k += 1`, `runner_milestone_idx = k`, `runner_milestone_name = f"TP{k}"`.
   - **Step Broker TP Upward** across all `runner_tickets` to `tp_levels[f"TP{k}"]` (`tp_stage = f"TP{k}"`) via `modify_position_sltp`.
   - **Ratchet floor upward** (`k == 1` cleared -> `max(be_floor, TP1 - TP1_LOCK_BUFFER)`; `k >= 2` cleared -> `max(current_floor, TP_{k-1}, target - TRAIL_DISTANCE_PIPS * pip_size)`) and ratchet hard broker SL across all `runner_tickets`.
4. **Strict Proximity Reversal Guard ("Bank It") at Current `TPk` (`k <= max_stage`)**:
   - If not armed and `(target - live_price) <= eff_buffer`:
     `runner_proximity_armed = True`, `runner_reversal_ticks = 0`, `runner_peak_price = live_price`.
     - **Proactive Broker TP Lift:** if `RUNNER_PROACTIVE_TP_LIFT=True` and `k < max_stage`, lift all `runner_tickets`' broker `tp` to `tp_levels[f"TP{k+1}"]` (`tp_stage = f"TP{k+1}"`) so fast breakout candles never stop early at `TPk`.
   - While armed:
     - Update `runner_peak_price = max(runner_peak_price or live_price, live_price)` (reset `runner_reversal_ticks = 0` on new peak).
     - If `pullback >= eff_pullback` (and optional M1 candle confirms): increment `runner_reversal_ticks += 1`; if `runner_reversal_ticks >= RUNNER_REVERSAL_CONFIRM_TICKS`, **BANK IT**: close ALL `runner_tickets` at market (`reason = f"Bank It Proximity Guard (TP{k}, Peak: {peak})"`), mark `CLOSED`.
     - Else: reset `runner_reversal_ticks = 0`.
5. **Continuous One-Way Broker SL Trail** across all `runner_tickets`:
   `candidate_sl = max(floor_bound, round(live_price - TRAIL_DISTANCE_PIPS * pip_size, digits))` — update MT5 SL when improved by `>= TRAIL_STEP_PIPS * pip_size` and outside `trade_stops_level`.

**TP1 Scaling Handoff (`handle_tp1_scaling_and_select_runner`) & Broker Early-TP Safety:**
1. **4-Layer signals (`L1..L4`):**
   - Call `runner_layers, early_layers = select_dynamic_runners(layers, market_tickets, RUNNER_DEEPEST_COUNT_FULL)`.
   - Close `early_layers` (`[]` when `n == 1`) -> `layers[lk]["status"] = "CLOSED"`. Cancel unfilled `pending_tickets` if `CANCEL_PENDING_ON_TP1=True`.
   - If `RUNNER_MAX_TP_STAGE <= 1`: close `runner_layers` too (`status = "CLOSED"`).
   - Otherwise lift ALL `runner_tickets` to `TP2` (`tp_stage = "TP2"`), apply BE if `BE_AFTER_TP1=True` (using `runner_be_sl` when live price hasn't cleared `tp1_floor_price`), initialize `runner_layers`, `runner_tickets`, `runner_milestone_idx` (`2` if past clean break else `1`), `runner_reversal_ticks = 0`, `status = "RUNNER_ACTIVE"`.
   - **Step 2 Broker TP1 Handoff (`main.py`):** if an `early_layer` (e.g. `L1`) closes on broker with `reason == "TP"` while `tp1_reached == False`, immediately trigger `_execute_tp1_scaling_handoff` so surviving `runner_layers` transition cleanly into `RUNNER_ACTIVE`.
2. **Runner Exit Accounting:**
   - When `RUNNER_ACTIVE` finishes (all `runner_tickets` closed via Bank It / Virtual Floor / `TP{max_stage}` / Broker SL), remove closed tickets from `market_tickets`, clear `runner_tickets = []`, `runner_ticket = None`, `runner_layers = []`, `runner_layer = None` (keeping `last_runner_tickets` / `last_runner_ticket` for `_account_closed_tickets`), and set `status = "CLOSED"` when `not market_tickets and not pending_tickets` (else `"BE_APPLIED"`).
3. **Legacy 3-layer records (`TOP/MID/BOTTOM` without `L1..L4`):**
   - Preserve legacy `TOP`/`MID` close & `BOTTOM` runner behavior toward `TP2`/`TP3`.

---

## 4. Concurrency, Resilience, State & Ledger Contracts

### 4.1 Startup & Single-Instance Mutex (`startup_checks.py`)
- `validate_environment(check_packages: bool = True) -> Dict[str, Any]` -> `{"ok": bool, "errors": List[str], "warnings": List[str]}`
- `SingleInstanceLock(lock_path: Optional[Path] = None, mutex_name: Optional[str] = None)`
- `acquire_single_instance_lock(lock_name_or_path: Optional[str | Path] = None) -> Optional[SingleInstanceLock]`
  - Uses Windows `CreateMutexW` (`ERROR_ALREADY_EXISTS = 183`) derived from resolved `LOCK_FILE_PATH` + OS file lock (`msvcrt.locking` / `fcntl.flock`) + process-local held registry.
  - Duplicate launch returns `None`; `main.py` exits cleanly with `DUPLICATE_INSTANCE_EXIT_CODE = 90` (launchers `.bat` do NOT auto-restart on `88`, `89`, `90`, `0`).
- `release_single_instance_lock(lock_handle: Optional[SingleInstanceLock] = None) -> None`

### 4.2 Asyncio Non-Blocking MT5 IPC & Per-Signal Lock (`main.py`)
- **`_mt5_call(fn, *args, **kwargs)`**: wraps blocking `executor.*` IPC calls with `await asyncio.to_thread(...)` so Telethon event loop never stalls.
- **Per-`(chat_id, msg_id)` `asyncio.Lock`**: `_get_signal_lock(chat_id, msg_id)` serializes `handle_new_message`, `handle_message_edited`, and `_delayed_sell_now_task` for the same signal.

### 4.3 Bounded Reconnect Catch-Up & One-Shot MT5 Alerts (`main.py`, `state_manager.py`)
- Config: `CATCHUP_WINDOW_SECONDS = 1800`, `CATCHUP_MAX_MESSAGES = 25`, `CATCHUP_MARKET_MAX_AGE_SECONDS = 60`, `CATCHUP_MARKET_MAX_SLIPPAGE_PIPS = 15.0`.
- Watermarks persisted under `signals_state.json["_meta"]["catchup_watermarks"][str(chat_id)] = {"last_msg_id": int, "last_msg_ts": str, "updated_at": str}` via monotonic `StateManager.set_catchup_watermark(chat_id, msg_id, msg_ts=None)` & `get_last_seen_msg_id(chat_id)`.
- Stale market signals (`is_instant_now` older than `CATCHUP_MARKET_MAX_AGE_SECONDS` or drifted beyond `CATCHUP_MARKET_MAX_SLIPPAGE_PIPS`) are rejected safely on catch-up.
- **One-shot MT5 disconnect / recovery alerts**: watcher fires ONE Saved Messages disconnect alert on `connected -> disconnected` transition and ONE recovery alert on `disconnected -> connected`.

### 4.4 Dirty-Checked `StateManager` (`state_manager.py`)
- `_save_state_locked(force: bool = False) -> bool` compares canonical serialized JSON against `_last_saved_payload` so unchanged 1s watcher ticks perform **zero disk writes**.
- Composite keys `f"{chat_id}:{msg_id}"` unpack/resolve cleanly across both `(chat_id, msg_id)` tuples and `"chat_id:msg_id"` strings; `_meta`, `_daily_win`, `_daily_loss` are skipped by all signal queries and cleanup.
- Legacy `"TOP"/"MID"/"BOTTOM"` active records load without mutation; `StateManager.is_legacy_3layer(sig)` and `normalize_layer_key(name)` support clean backwards compatibility.

### 4.5 Unified Close Accounting, PnL Cache & `trade_ledger.py`
- **`MT5Executor.get_position_close_info(ticket)`**: single `mt5.history_deals_get(position=ticket)` call returning `{"profit": float, "reason": str}`.
- **`MT5Executor.get_realized_today_pnl()`**: cached for `DAILY_PNL_CACHE_TTL_SECONDS` (`5.0s`), explicitly invalidated via `invalidate_realized_pnl_cache()` on any close.
- **Unified `RUNNER_ACTIVE` broker close**: deduplicates realized ticket PnL in `sig["accounted_tickets"]`, combines `banked_profit + runner_profit`, emits one unified Saved Messages summary, and appends one record to `trades_ledger.jsonl`.
- **`TradeLedger` (`trade_ledger.py`)**:
  - Append-only JSONL at `TRADES_LEDGER_PATH` (`trades_ledger.jsonl`).
  - Methods: `append_trade(record)`, `record_closed_signal(signal_key, signal_data, realized_pnl, exit_reason, currency="GBP", dry_run=False, extra=None)`, `read_trades(limit=None)`, `get_stats(server_date=None)`.
  - Schema v3 fields: `schema_version`, `closed_at`, `opened_at`, `signal_key`, `chat_id`, `message_id`, `symbol`, `direction`, `entry_type`, `total_lots`, `filled_lots`, `tickets`, `layers_filled`, `highest_milestone_cleared`, `runner_layer`, `exit_reason`, `realized_pnl`, `currency`, `dry_run`.
- **`/health` Command (`main.py`)**: reports uptime, single-instance lock status, MT5 connection & ping latency (`ms`), Algo Trading status, Telegram reconnect count, active signals & runner milestone state, and `TradeLedger.get_stats()` summary.

---

## 5. Deleted-Signal Trade Protection (v3.2)

Automatically liquidates open positions and cancels pending orders for any active or pending signal whose source Telegram message is deleted from its monitored channel/chat, so pulled or deleted setups never stay open to hit Stop Loss.

### 5.1 Configuration & Preflight Contracts (`config.py`, `startup_checks.py`)
- `CLOSE_ON_SIGNAL_DELETED: bool = _env_bool("CLOSE_ON_SIGNAL_DELETED", True)` — master toggle for automatic close/cancel on signal deletion.
- `DELETED_SIGNAL_POLL_INTERVAL_SECONDS: float = _env_float("DELETED_SIGNAL_POLL_INTERVAL_SECONDS", 15.0, min_val=0.0, max_val=3600.0)` — periodic Telegram message-existence verification cadence (`0.0` disables periodic polling while preserving real-time `events.MessageDeleted`).
- `startup_checks.validate_environment()` verifies `DELETED_SIGNAL_POLL_INTERVAL_SECONDS >= 0` (`non_negative_fields`) and `isinstance(CLOSE_ON_SIGNAL_DELETED, bool)` when present.

### 5.2 State Lookup & Telegram Channel ID Normalization (`state_manager.py`)
- **`_chat_ids_match(stored_chat: Any, query_chat: Any) -> bool`:**
  - Matches exact string equality AND Telegram channel ID normalization (`-100<id>` <-> `<id>` / `-<id>`) so `UpdateDeleteChannelMessages.channel_id` and `event.chat_id` always resolve against `stored_chat`.
- **`StateManager.get_open_or_pending_signals(self) -> List[Tuple[str, Dict[str, Any]]]`:**
  - Returns deep-copied `(key, data)` for all non-`_`-prefixed dict records with `data.get("status") in (_ACTIVE_STATUSES + _PENDING_STATUSES)`.
- **`StateManager.find_matching_signals_for_delete(self, message_id: int | str, chat_id: Optional[int | str] = None) -> List[Tuple[str, Dict[str, Any]]]`:**
  - Unpacks `base_key, eff_chat = self._unpack_id(message_id, chat_id)`.
  - Scans non-`_`-prefixed dict records whose unpacked message ID equals `base_key` and whose `status` is NOT in `_TERMINAL_STATUSES` (or still holds non-empty `market_tickets` / `pending_tickets`):
    - If `eff_chat is not None`: includes records where `rec.get("chat_id") is None` or `_chat_ids_match(rec.get("chat_id"), eff_chat)`.
    - If `eff_chat is None`: includes all matching non-terminal records for `base_key` so the caller can verify message absence via Telegram `get_messages` before closing.

### 5.3 Surgical Per-Signal Exposure Close (`mt5_executor.py`)
- **`MT5Executor.close_signal_exposure(self, signal_data: Dict[str, Any]) -> Dict[str, List[int]]`:**
  - Collects unique positive integer tickets belonging **strictly** to `signal_data` (`market_tickets`, `runner_tickets`, `runner_ticket`, `pending_tickets`, `mt5_order_tickets`, and `layers[k]["ticket"]`), excluding `accounted_tickets`.
  - Tracks `known_market_set` (`market_tickets`, `runner_tickets`, `runner_ticket`, layers in `OPEN`/`FILLED`/`RUNNER`) and `known_pending_set` (`pending_tickets`, layers in `PENDING`).
  - **Isolation Invariant:** NEVER sweeps unrelated active bot positions/orders by `MAGIC_NUMBER`.
  - **Dry-Run / Disconnected:** closes `known_market_set` via `close_market_position(t)`, then cancels `known_pending_set - closed_tickets - known_market_set` via `cancel_pending_order(t)`.
  - **Live MT5 (`_mt5_lock`):** queries `mt5.positions_get()` and `mt5.orders_get()` for `live_pos_ids` and `live_ord_ids`. For each signal ticket `t`:
    1. If `t in live_pos_ids` (catches pending orders that just filled!): closes via `close_market_position(t)`.
    2. Elif `t in live_ord_ids`: cancels via `cancel_pending_order(t)`; if cancel returns `False`, re-checks `mt5.positions_get(ticket=t)` and closes via `close_market_position(t)` if newly filled.
    3. Else (e.g. mocked unit test where `positions_get`/`orders_get` is empty): falls back to `known_market_set` -> `close_market_position(t)` or `known_pending_set` -> `cancel_pending_order(t)`.
  - Calls `self.invalidate_realized_pnl_cache()` whenever `closed_tickets` is non-empty.
  - Returns `{"closed_tickets": List[int], "cancelled_orders": List[int]}`.

### 5.4 Event Handler, Tombstone Guard, Reconnect/Periodic Sweep & Accounting (`main.py`)
- **`handle_message_deleted(event)`:** registered on `events.MessageDeleted()`. Ignores events outside monitored `CHANNEL_IDS` when `event.chat_id` is set; supports `event.chat_id is None` (basic-group deletes) by verifying candidate `(sig_chat, msg_id)` absence via `telethon_client.get_messages(sig_chat, ids=...)` (treating `None` / empty result as deleted).
- **Out-of-Order Tombstone Guard (`_deleted_messages`):** records deleted `(chat_id, msg_id)` keys (and bare `msg_id` when chat unknown) so delayed/out-of-order `NewMessage`, `MessageEdited`, or catch-up events cannot re-open a deleted signal; cancels any active `_delayed_sell_now_task`.
- **Reconnect & Periodic Sweep (`poll_deleted_active_signals()`):** runs during `run_reconnect_catchup` and periodically inside `tp1_proximity_watcher_task` every `DELETED_SIGNAL_POLL_INTERVAL_SECONDS` (`15.0s`), batch-checking `state_mgr.get_open_or_pending_signals()` against Telegram `get_messages` so deletes missed while offline are caught automatically.
- **Unified Close, Accounting & Ledger:** acquires per-`(chat_id, msg_id)` `asyncio.Lock`, invokes `_mt5_call(executor.close_signal_exposure, sig)`, accumulates closed position PnL into `banked_profit` / `accounted_tickets`, sets `status = "CLOSED"`, clears `market_tickets`, `pending_tickets`, `runner_tickets`, `runner_ticket`, records to `TradeLedger` ONLY when positions actually closed (`closed_tickets` or prior `accounted_tickets` — skipping purely unfilled/pending setups), and alerts Saved Messages.

---

## 6. Active-Trade Prompt Control Plane & Dynamic SL / TP1–TP3 Adjustment & Trailing (v3.3)

Guarantees that active or pending trades (`ACTIVE`, `BE_APPLIED`, `PARTIAL_CLOSED`, `RUNNER_ACTIVE`, `PENDING`) cleanly accept dynamic **SL**, **TP1**, **TP2**, **TP3** (and **TP4/TP5**) adjustments and **trailing / milestone-stepping** commands across **all 4 control paths**: Channel Replies, concise Channel Prompts, Message Edits, and User Prompts in Telegram Saved Messages (`"me"`).

### 6.1 Parser Contracts (`parser.py`)
- **Breakeven Disambiguation (`BREAKEVEN_PATTERNS`):**
  - Replaces bare `r"\bmove\s+sl\b"` with `r"\bmove\s+sl\s+(?:to\s+)?(?:entry|be|break\s*even)\b"` and `r"^\s*move\s+sl\s*[!.]*\s*$"` so `"Move SL to 3885"` is NEVER hijacked as Breakeven, while `"Move SL to BE"`, `"Move SL to entry"`, and bare `"Move SL"` continue matching `ACTION_BREAKEVEN`.
- **Labeled TP Extraction & Active Merge:**
  - **`extract_labeled_tps(text: str) -> Tuple[Dict[str, float], List[float]]`:** scans `TP_PATTERN.finditer(text)`, returning `(labeled_dict, unlabeled_list)` where numbered matches (`TP1..TP5`) map to `{"TP1": p1, ...}` and unnumbered matches (`TP 3905`) preserve order in `unlabeled_list` (keeping `extract_tps(text, direction)` 100% backward-compatible). Gate 4 `NEW_SIGNAL` `SignalAction` populates `"labeled_tps"` and `"unlabeled_tps"`.
  - **`merge_tp_levels_for_active_signal(existing_tps, labeled_tps, unlabeled_tps=(), direction="BUY", pip_size=0.10, step_pips=None) -> Dict[str, float]`:** copies `existing_tps`, applies `labeled_tps` to their exact `TP1..TP5` keys (preserving untouched stages like `TP1` when `"TP2 3918 TP3 3930"` arrives), maps `unlabeled_tps` sequentially into `TP1..TP5` skipping keys explicitly in `labeled_tps`, and re-derives missing or non-monotonic stages above updated stages up to `TP5` using `step_pips` or `[TP2_STEP_PIPS..TP5_STEP_PIPS]`.
- **Milestone Hit Detection (`1..3`):**
  - **`detect_tp_milestone_hit(text: Optional[str]) -> Optional[int]`:** extends negation guard `_TP_NEGATION_RE` to `tp\s*#?\s*[1-5]` / `target\s*#?\s*[1-5]` / `(?:first|1st|second|2nd|third|3rd)`. Returns `2` (`"TP2 hit"`, `"TP 2 smashed"`, `"Target 2 reached"`, `"2nd TP hit"`, `"TP2 ✅"`), `3` (`"TP3 hit"`, `"Target 3 reached"`, `"3rd TP hit"`, `"TP3 ✅"`), `1` (`is_tp1_hit_message`), or `None` (never matching `"TP2 3910"` target-price lines).
- **Gate 2 & Gate 3 Active-Trade Prompt Classification:**
  - Gate 2 permits short `<= 8`-word `detect_tp_milestone_hit(lower) in (1, 2, 3)` alerts (without direction or 4-digit prices) through `VICTORY_RECAP_BLOCKERS`.
  - Gate 3 emits:
    - `"ACTION_TP1_HIT_REPLY"` (stage `1`), `"ACTION_TP2_HIT"` (`tp_hit_stage=2`), `"ACTION_TP3_HIT"` (`tp_hit_stage=3`).
    - `"ACTION_TRAIL_SL"` (without direction verb, on reply/edit or `word_count <= 20`): matches `trail` / `trail sl` / `lock profit` / `lock tp1..4`, extracting `trail_milestone` (`"TP1".."TP4"`), `trail_price` (`>= 500.0`, non-TP price), `trail_pips` (`1..500`), plus optional `labeled_tps` / `unlabeled_tps`.
    - `"ACTION_ADJUST_TARGETS"` (without direction verb, on reply/edit or `word_count <= 20`): extracts `sl = _extract_sl(text)` (Gold sanity check `>= 500.0`) and `labeled_tps, unlabeled_tps = extract_labeled_tps(text)`, returning `SignalAction(event_type="TRADE_MANAGEMENT", management_action="ACTION_ADJUST_TARGETS", sl=adj_sl, sl_valid=(adj_sl is not None and adj_sl >= 500.0), labeled_tps=labeled_tps, unlabeled_tps=unlabeled_tps, explicit_tps=..., cancel_pending_limits=False)`.

### 6.2 Broker-Safe Target Adjust & Trail Methods (`mt5_executor.py`)
- **`MT5Executor.adjust_active_signal_sl_tp(self, signal_data, new_sl=None, labeled_tps=None, unlabeled_tps=None, ratchet_sl_only=False) -> Dict[str, Any]`:**
  - Merges `labeled_tps` / `unlabeled_tps` via `merge_tp_levels_for_active_signal`, syncs `tp_levels`, `raw_tp1..raw_tp5`, `tp1_price`, and recomputes `final_tp` / `hard_broker_tp` when `TP1` changes.
  - **Ratchet vs Explicit Override:** when `ratchet_sl_only=True` (channel edits during `RUNNER_ACTIVE`), enforces one-way protection against `runner_sl_floor` / `soft_tp1_floor` (never lowering/loosening a trailed SL); when `ratchet_sl_only=False` (explicit adjust prompt), updates SL directly.
  - **Stop-Level Validation:** checks broker `min_stop_dist = trade_stops_level * point` against live tick; if SL violates stop distance, records `rejected_sl_reason` while still applying valid TP updates.
  - **Stage-Aware MT5 Updates:**
    - `RUNNER_ACTIVE` / `tp1_reached=True`: updates all `runner_tickets` with `(sl=eff_sl, tp=tp_levels[stage_key])` (minimum `"TP2"` if `max_stage >= 2`), updates any non-runner open `market_tickets` (`L2->TP2`, `L3->TP3`), and disarms `runner_proximity_armed` / `runner_reversal_ticks` if active `TP{milestone_idx}` moved beyond `eff_buffer`.
    - Pre-TP1 (`ACTIVE` / `BE_APPLIED` / `PARTIAL_CLOSED` / `PENDING`): updates `market_tickets` (`modify_position_sltp`) and `pending_tickets` (`modify_pending_order_sltp`) with `TP2` (if `tp_stage == "TP2"`) else `final_tp`, syncing `l_data["tp"]`; resets `shortfall_armed` / `shortfall_reversal_ticks` if `TP1` moved beyond `SHORTFALL_ARM_PIPS * pip_size`.
  - Returns `{"sl", "tp_levels", "final_tp", "tp_stage", "modified_positions", "modified_orders", "rejected_sl_reason"}`.
- **`MT5Executor.trail_active_signal(self, signal_data, milestone=None, trail_pips=None, explicit_sl_price=None) -> Dict[str, Any]`:**
  - **Milestone Stepping (`"TP1".."TP4"`):** executes `handle_tp1_scaling_and_select_runner` first if `not tp1_reached`; marks `TP1..TP{m_idx}` cleared; closes runners at ceiling if `m_idx >= max_stage`; computes locked floor (`TP1 ± lock_buf` or `max/min(cur_floor, TP{m-1}, TP{m} ± trail_dist)`) clamped safely inside `min_stop_dist` from live price; steps to `next_stage = f"TP{min(max_stage, m_idx + 1)}"` via `_step_runner_broker_tp_and_sl(...)`, disarming proximity & resetting `runner_reversal_ticks = 0`.
  - **Pip-Distance / Explicit SL Trail:** computes `cand_sl` from `explicit_sl_price` or `live_px -/+ eff_pips * pip_size` (clamped by `min_stop_dist`), modifies active `runner_tickets` (if `RUNNER_ACTIVE`) or `market_tickets`, and syncs `runner_sl_floor` / `soft_tp1_floor` / `broker_sl` / `sl`.
  - Returns `{"new_sl", "tp_stage", "next_tp", "cleared_milestones", "modified_tickets", "banked_tickets", "status"}`.

### 6.3 Channel & Saved Messages (`"me"`) Control Plane (`main.py`)
- **Channel Replies / Prompts & Message Edits:**
  - `ACTION_ADJUST_TARGETS` invokes `_mt5_call(executor.adjust_active_signal_sl_tp, ..., ratchet_sl_only=False)` under `_get_signal_lock`.
  - `ACTION_TRAIL_SL` applies optional `labeled_tps`/`unlabeled_tps` first, then `_mt5_call(executor.trail_active_signal, ...)`, updating state, accounting closed/banked tickets (`TradeLedger` if ceiling closed), and notifying Saved Messages.
  - `ACTION_TP2_HIT` / `ACTION_TP3_HIT` invoke `trail_active_signal(..., milestone="TP2"|"TP3")` to bank/advance runners cleanly toward `TP3`/`TP4`.
  - `_handle_message_edited_unlocked` delegates live SL/TP edits in both `RUNNER_ACTIVE` and pre-TP1 active states to `adjust_active_signal_sl_tp(..., ratchet_sl_only=True)` so edits adjust `SL`, `TP1`, `TP2`, `TP3`, `TP4`, `TP5` on MT5 while preserving one-way runner SL ratchets.
- **Saved Messages (`"me"`) Prompt Commands:**
  - All active-trade prompts are registered in `_KNOWN_SAVED_COMMANDS` / `_KNOWN_SAVED_PREFIXES` (preventing capture by `_awaiting_dailywin_input` / `_awaiting_dailyloss_input`) and operate on the latest active/pending signal (`state_mgr.get_latest_signal()`) under `_get_signal_lock`:
    - **Adjust SL / TPs:** `/sl <price>`, `sl <price>`, `move sl <price>`, `/tp <...>`, `tp1..tp4 <price>`, `adjust <...>`
    - **Trail / Step Milestones:** `/trail`, `trail`, `/trail <pips|price|tp1..tp4>`, `trail <pips|price|tp1..tp4>`, `lock tp1..tp4`, `tp1 hit`, `tp2 hit`, `tp3 hit`
    - **Active Trade Shortcuts:** `/be`, `be`, `/partial`, `partial`
    - **Manual Signal Execution:** `/trade <signal>`, `/exec <signal>` (runs full 6-gate `parse_message` -> preflight -> 4-layer execution pipeline).

---

## 7. Late-Edit Resilience & Multi-Layer Zone Re-Tiering (v3.3.2)

- **No Elapsed-Time Limit on Active Signals:** Edits arriving 3 minutes, 15 minutes, or hours after an initial signal post always update `SL`, `TP1`–`TP5`, and unfilled/missing layers as long as the trade is non-terminal.
- **Untracked Placeholder Edits vs Emoji Reactions:** `MAX_NEW_EDIT_AGE_SECONDS` defaults to `3600s` (`60m`), and fresh Telethon `msg.edit_date` timestamps (`<= MAX_EDIT_EVENT_FRESHNESS_SECONDS = 180s`) allow genuine text edits on older placeholder posts regardless of original post age while filtering out emoji-reaction / view updates (`edit_date=None` or stale `edit_date`).
- **Gate 2 Risk-Tag Precision:** Soft risk disclaimers (`"High risk"`, `"Low risk"`, `"Manage your risk"`, `"Protect your capital"`, `"VIP"`) no longer reject structured Gold entries or TP/SL edits, while standalone risk/sizing tables and `"how to"` chatter remain blocked.
- **Synthetic Fallback TP Replacement & Zone Re-Tiering:** When a real TP edit arrives after the `45s` `UNPRICED_TP_GRACE_SECONDS` fallback attached, synthetic `tp_levels` and stale `shortfall_armed` state are cleanly discarded and replaced. Unfilled pending limits are re-tiered across edited zone levels (with pending-to-market fill-race promotion), missing layers scale up to `4` (`[0.01, 0.01, 0.01, 0.02]`), and `modify_position_sltp` / `modify_pending_order_sltp` retry `TP` and `SL` independently if MT5 returns `10016 Invalid stops` on a combined modification.
