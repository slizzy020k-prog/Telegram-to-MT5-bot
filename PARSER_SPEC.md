# Python Signal Parser & Safety Filter Specification (`PARSER_SPEC.md`)

## 1. Purpose
This document defines the exact text-processing, filtering, and validation pipeline for `parser.py`. Its job is to convert raw, messy, or edited Telegram messages into structured JSON trade instructions while aggressively blocking false triggers from announcements, victory recaps, forwards, and provider typos.

---

## 2. Processing Pipeline (Execution Order)
Every incoming `NewMessage` or `MessageEdited` event must pass through these **6 sequential gates** before touching MetaTrader 5:

```text
Raw Telegram Message
   │
   ├─► Gate 1: Forward & Empty Message Check
   ├─► Gate 2: Announcement & Victory Recap Filter (Noise Blocker)
   ├─► Gate 3: Trade Management & Reply Classifier (BE / Close / Cancel)
   ├─► Gate 4: New Signal Extraction (Symbol, Direction, Zone, SL, TP1)
   ├─► Gate 5: Price & Typo Sanity Validation (e.g., Sl 387 vs 3875)
   └─► Output: Clean Structured Action Object -> Sent to mt5_executor.py
```

---

## 3. Detailed Gate Rules

### Gate 1: Forward & Empty Check
* **Empty/Media-Only:** If `event.raw_text` is empty or contains only an image with no caption, discard immediately.
* **Forwarded Messages:** If `event.forward` is not `None` and `ALLOW_FORWARDS = False`, discard immediately so forwarded motivational posts or old signals never execute.

### Gate 2: Announcement & Victory Recap Filter
Blocks non-actionable messages even if they contain trading symbols or numbers (such as lot-size tables or trade recaps).
* **Account/Risk Table Blocker:** Discard if message contains any of:
  `["manage your risk", "protect your capital", "high risk", "mid risk", "low risk", "£50 account", "£100 account", "account size", "lot size"]`
* **Past-Tense Victory Recap Blocker:** Providers often post *"XAUUSD TP1 HIT +50 PIPS! Buy entry was 3895-3887"*. Discard if message contains any of:
  `["hit", "smashed", "running", "was", "pips", "profit", "booked", "secured", "boom", "vip"]` *(unless it is a direct reply `event.reply_to_msg_id` to an active signal, which routes to Gate 3)*.

### Gate 3: Trade Management & Reply Classifier
Evaluates whether the message is an instruction to modify an existing trade rather than open a new one.
* **Priority Link:** Check `replied_id = event.reply_to.reply_to_msg_id` against `signals_state.json`.
* **Intent Matching:**
  * **`ACTION_BREAKEVEN`:** Matches `["be", "breakeven", "break even", "sl to entry", "risk free", "move sl"]`.
    * *Side Effect:* Also flags `cancel_pending_limits = True` so unfilled mid/bottom `0.01` limit orders are deleted once the active trade moves to BE.
  * **`ACTION_PARTIAL_CLOSE`:** Matches `["close half", "partial", "secure", "book 50", "50%"]`.
    * *0.01 Lot Rule:* Closes 1 open `0.01` position (if multiple are open) and moves remaining open position(s) to BE.
  * **`ACTION_FULL_CLOSE`:** Matches `["close all", "close now", "exit", "cut", "invalidate", "cancel", "delete limit"]`.

### Gate 4: New Signal Extraction (Regex Patterns)
Triggered only if the message contains **both** a valid symbol and a direction verb.

* **1. Direction:**
  * Pattern: `\b(buy|long|sell|short)\b` (case-insensitive)
* **2. Symbol Normalization:**
  * Pattern: `\b(xauusd|gold|eurusd|gbpusd|us30|nas100|ustec)\b`
  * Mapped via `SYMBOL_MAP` (e.g., `"GOLD"` -> `"XAUUSD"`).
* **3. Entry Zone & 3-Tier Price Split (`0.01` x 3 max):**
  * Pattern: `(\d{2,6}(?:\.\d+)?)\s*[-–to]+\s*(\d{2,6}(?:\.\d+)?)`
  * Extracts `p1` and `p2`:
    * `top_price = max(p1, p2)` (e.g., `3895.00`)
    * `bottom_price = min(p1, p2)` (e.g., `3887.00`)
    * `mid_price = round((top_price + bottom_price) / 2, 2)` (e.g., `3891.00`)
  * **No-Zone Instant Entry (`"Buy xauusd now"`):** If `"now"` is detected and no range exists yet, sets `is_instant_now = True` to open Order #1 (`0.01` Market) immediately while awaiting the message edit.
* **4. Take Profit (`TP1 ± 0.50` Rule):**
  * Pattern: `tp\s*\d*\s*[:=@-]?\s*(\d+(?:\.\d+)?)`
  * Extracts all TPs into a sorted list, takes `tp1 = tps[0]`, and applies the front-run offset:
    * `BUY`: `final_tp = round(tp1 - 0.50, 2)`
    * `SELL`: `final_tp = round(tp1 + 0.50, 2)`
* **5. Stop Loss:**
  * Pattern: `sl\s*[:=@-]?\s*(\d+(?:\.\d+)?)`

### Gate 5: Price & Typo Sanity Validation
Protects your account against missing-digit typos (such as `Sl 387` instead of `3875` on Gold).
* **Reference Price:** Uses `top_price` (for BUY), `bottom_price` (for SELL), or current live MT5 market price (for `"Buy now"`).
* **Validation Checks:**
  1. **Directional Logic:** For `BUY`, `sl` must be strictly `< reference_price` and `final_tp` must be strictly `> reference_price`. (Reverse for `SELL`).
  2. **Max Distance Cap (`MAX_SL_PIPS = 250`):** If `abs(reference_price - sl)` exceeds `25.00` points ($25 on Gold), the parser flags `sl_valid = False`, logs a warning (`"Rejected anomalous SL: 387"`), and uses `FALLBACK_SL_PIPS` (`150` pips) until the provider edits the typo.

---

## 4. Parser Output Schema (JSON Contract)
Every valid parse returns a standardised dictionary passed cleanly to `mt5_executor.py`:

```json
{
  "event_type": "NEW_SIGNAL",
  "message_id": 94821,
  "is_edit": false,
  "symbol": "XAUUSD",
  "direction": "BUY",
  "is_instant_now": false,
  "lot_per_order": 0.01,
  "max_orders": 3,
  "entry_tiers": {
    "top": 3895.00,
    "mid": 3891.00,
    "bottom": 3887.00
  },
  "sl": 3875.00,
  "sl_valid": true,
  "raw_tp1": 3900.00,
  "final_tp": 3899.50
}
```

---

## 5. Unit Test Cases for `parser.py`
Before connecting to MT5, verify `parser.py` against these 5 exact test strings:

1. **Test 1 (Standard Zone Signal):**
   * *Input:* `"Buy xauusd 3895-3887 \n Tp 3900 \n Tp 3910 \n Sl 3875"`
   * *Expected:* `top=3895.0, mid=3891.0, bottom=3887.0, final_tp=3899.50, sl=3875.0, sl_valid=True`
2. **Test 2 (SL Typo Guard):**
   * *Input:* `"Buy xauusd 3895-3887 \n Tp 3900 \n Sl 387"`
   * *Expected:* `sl=None, sl_valid=False` (Triggers fallback SL instead of 3,500-pip exposure)
3. **Test 3 (Initial "Buy Now" Post):**
   * *Input:* `"Buy xauusd now"`
   * *Expected:* `is_instant_now=True, entry_tiers=None, sl_valid=False` (Opens 1x `0.01` market order with fallback SL)
4. **Test 4 (Risk Management Table Noise):**
   * *Input:* `"£50 account High Risk : 0.05 Mid Risk : 0.02 Low Risk : 0.01"`
   * *Expected:* `None` (Ignored at Gate 2)
5. **Test 5 (Victory Recap Noise):**
   * *Input:* `"XAUUSD TP1 HIT +50 PIPS! Buy entry was 3895-3887"`
   * *Expected:* `None` (Ignored at Gate 2)
