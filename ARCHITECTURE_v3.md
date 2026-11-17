# XAUUSD Copier v3.1 — Architecture & API Contract

**Status:** LOCKED (v3.1 Dynamic Stepped Scaling & Calibrated Anti-Choke Update)  
**Scope:** Local Windows production upgrade (`telegram-mt5-copier`) — 4-Layer All-at-TP1 Entry, Dynamic Deepest-Layer Runner Detection (`1..4` layers), Stepped Momentum Scaling (`TP1 -> TP2 -> TP3 -> TP4`), Strict Activation Zone + Anti-Choke Reversal Guard, Async Concurrency, Ledger & Safety Hardening.

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
