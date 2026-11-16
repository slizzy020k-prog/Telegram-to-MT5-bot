import re
import logging
from typing import Optional, List, Dict, Any, Tuple
from config import (
    DEFAULT_SYMBOL,
    IGNORED_SYMBOLS,
    SINGLE_PRICE_LOT,
    TP_FRONT_RUN_OFFSET,
    TP1_CLOSE_PROXIMITY_PIPS,
    MAX_SL_PIPS,
    ALLOW_FORWARDS,
    LAYER_LOTS,
    MAX_ORDERS_PER_SIGNAL,
)

logger = logging.getLogger("copier.parser")

# ---------------------------------------------------------------------------
# GATE 2 KEYWORDS & PATTERNS
# ---------------------------------------------------------------------------

ACCOUNT_RISK_BLOCKERS = [
    r"manage\s+your\s+risk",
    r"protect\s+your\s+capital",
    r"high\s+risk",
    r"mid\s+risk",
    r"low\s+risk",
    r"[£$€]?\s*\d+\s*account",
    r"account\s+size",
    r"lot\s+size",
]

VICTORY_RECAP_BLOCKERS = [
    r"\btp\d*\s+hit\b",                           # "TP1 hit", "TP hit"
    r"\bsmashed\b",                               # "TP smashed"
    r"\bpips\s+(?:secured|booked|smashed|profit)\b",  # "50 pips secured"
    r"\bprofit\s+(?:secured|booked|banked|taken)\b",   # "profit booked"
    r"\bbooked\s+\d",                             # "booked 50 pips"
    r"\bsecured\s+\d",                            # "secured 100 pips"
    r"\bboom\b",                                  # "BOOM 🚀"
    r"\bvip\b",                                   # VIP announcement posts
    r"\bentry\s+was\b",                           # "Buy entry was 3895"
    r"\bwas\s+(?:a\s+)?(?:great|good|perfect|beautiful)\b",
    r"\+\s*\d+\s*pips?\b",                        # "+50 pips"
]

# ---------------------------------------------------------------------------
# GATE 3 TRADE MANAGEMENT INTENT PATTERNS
# ---------------------------------------------------------------------------

BREAKEVEN_PATTERNS = [
    r"\bbe\b",
    r"\bbreakeven\b",
    r"\bbreak\s+even\b",
    r"\bsl\s+to\s+entry\b",
    r"\bsl\s+to\s+be\b",
    r"\brisk\s+free\b",
    r"\bmove\s+sl\b",
    r"\bset\s+sl\s+to\s+entry\b",
]

WORST_HOLD_PATTERNS = [
    r"\bclose\s+worst\b",
    r"\bworst\s+entr(?:y|ies)\b",
    r"\bhold\s+layers?\b",
    r"\bclose\s+bad\s+entr(?:y|ies)\b",
    r"\bbreakeven\s+worst\b",
]

PARTIAL_CLOSE_PATTERNS = [
    r"\bclose\s+half\b",
    r"\bpartial\b",
    r"\bpartials\b",
    r"\bsecure\b",
    r"\bsecure\s+profit\b",
    r"\bbook\s+50\b",
    r"\b50\s*%",
    r"\btake\s+partials\b",
    r"\bclose\s+50\s*%",
    r"\bbank\s+profit\b",
]

FULL_CLOSE_PATTERNS = [
    r"^\s*close\s*[!.]*\s*$",                     # bare "close" (whole message only)
    r"\bclose\s+all\b",
    r"\bclose\s+now\b",
    r"\bexit\b",
    r"\bcut\b",
    r"\binvalidate\b",
    r"\bcancel\b",
    r"\bdelete\s+limit\b",
    r"\bclose\s+trade\b",
    r"\bclose\s+position\b",
]

REACTIVATE_PATTERNS = [
    r"\bactive\b",
    r"\breactivate\b",
    r"\bvalid\s+again\b",
]

# ---------------------------------------------------------------------------
# GATE 4 SIGNAL EXTRACTION PATTERNS
# ---------------------------------------------------------------------------

# Expanded direction verbs supporting limits and stops
DIRECTION_PATTERN = re.compile(
    r"\b(buy\s+limit|sell\s+limit|buy\s+stop|sell\s+stop|buy|sell|long|short)\b",
    re.IGNORECASE,
)

# Zone pattern: 3895-3887, 3895 - 3887, 3895 to 3887, 3895/3887
ZONE_PATTERN = re.compile(
    r"(\d{2,6}(?:\.\d+)?)\s*(?:-|–|—|/|\bto\b)\s*(\d{2,6}(?:\.\d+)?)",
    re.IGNORECASE,
)

# Single price entry (e.g. "Buy limit 3920" or "Sell 3920")
SINGLE_PRICE_PATTERN = re.compile(
    r"\b(?:buy\s+limit|sell\s+limit|buy\s+stop|sell\s+stop|buy|sell|long|short|entry|price)\s*[@:=]?\s*(\d{2,6}(?:\.\d+)?)",
    re.IGNORECASE,
)

# Take Profit pattern: TP1, TP#1, Target 1, etc.
TP_PATTERN = re.compile(
    r"\b(?:tp|take\s*profit|target)(?:#?([1-9])(?:\s*[:=@\-\.]\s*|\s+)|\s+([1-9])\s*[:=@\-\.]\s*|\s*[:=@\-]?\s*)(\d{2,6}(?:\.\d+)?)",
    re.IGNORECASE,
)

# Stop Loss pattern
SL_PATTERN = re.compile(
    r"\b(?:sl|stop\s*loss|stoploss|stop)\s*[:=@\-]?\s*(\d{2,6}(?:\.\d+)?)",
    re.IGNORECASE,
)


class ZoneInfo:
    """Helper representing a 3-tier price entry zone."""

    def __init__(self, top: float, mid: float, bottom: float):
        self.top = round(float(top), 2)
        self.mid = round(float(mid), 2)
        self.bottom = round(float(bottom), 2)

    def to_dict(self) -> Dict[str, float]:
        return {
            "top": self.top,
            "mid": self.mid,
            "bottom": self.bottom,
        }

    def __repr__(self):
        return f"ZoneInfo(top={self.top}, mid={self.mid}, bottom={self.bottom})"


class SignalAction(dict):
    """
    Standardized Action Object implementing PARSER_SPEC JSON contract.
    Inherits from dict while exposing attribute access for seamless dot notation.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            if name in ("top", "mid", "bottom"):
                tiers = self.get("entry_tiers")
                if tiers and isinstance(tiers, dict):
                    return tiers.get(name)
                if self.get("single_price") is not None:
                    return self.get("single_price")
            if name == "single_price":
                return self.get("single_price")
            if name == "is_zone":
                return bool(self.get("is_zone", False))
            if name == "is_entry":
                return self.get("event_type") == "NEW_SIGNAL"
            if name == "is_management":
                return self.get("event_type") == "TRADE_MANAGEMENT"
            if name == "is_market":
                return bool(self.get("is_instant_now", False))
            if name == "broker_symbol":
                return self.get("symbol")
            if name == "zone":
                tiers = self.get("entry_tiers")
                if tiers:
                    return ZoneInfo(top=tiers["top"], mid=tiers["mid"], bottom=tiers["bottom"])
                return None
            if name == "is_noise":
                return False
            return None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


# ---------------------------------------------------------------------------
# GATE IMPLEMENTATIONS
# ---------------------------------------------------------------------------

def gate_1_check_forward_and_empty(
    text: Optional[str],
    is_forward: bool = False,
    allow_forwards: bool = ALLOW_FORWARDS,
) -> bool:
    """Gate 1: Forward & Empty Message Check."""
    if not text or not text.strip():
        logger.debug("Gate 1 Reject: Message is empty or media-only.")
        return False

    if is_forward and not allow_forwards:
        logger.info("Gate 1 Reject: Forwarded messages are disabled (ALLOW_FORWARDS=False).")
        return False

    return True


def gate_2_check_announcements_and_recaps(
    text: str,
    is_reply: bool = False,
    is_edit: bool = False,
) -> bool:
    """
    Gate 2: Announcement & Noise Filter + Ignored Symbols check.
    Blocks:
    1. Messages mentioning non-Gold tickers (from IGNORED_SYMBOLS).
    2. Account / risk sizing tables.
    3. Past-tense victory recaps (unless is_reply or is_edit).
    """
    upper = text.upper()
    lower = text.lower()

    # 1. Ignored Symbols Blocker
    for sym in IGNORED_SYMBOLS:
        if re.search(rf"\b{re.escape(sym.upper())}\b", upper):
            logger.info(f"Gate 2 Reject (Non-Gold Symbol): Ignored pair '{sym}' detected in text.")
            return False

    # 2. Account / Risk Table Blocker
    for pattern in ACCOUNT_RISK_BLOCKERS:
        if re.search(pattern, lower):
            logger.info(f"Gate 2 Reject (Account/Risk Table): Matched pattern '{pattern}'")
            return False

    # 3. Past-Tense Victory Recap Blocker
    if not is_reply and not is_edit:
        for pattern in VICTORY_RECAP_BLOCKERS:
            if re.search(pattern, lower):
                logger.info(f"Gate 2 Reject (Victory Recap): Matched pattern '{pattern}'")
                return False

    return True


def gate_3_classify_trade_management(
    text: str,
    message_id: int = 0,
    reply_to_msg_id: Optional[int] = None,
) -> Optional[SignalAction]:
    """
    Gate 3: Trade Management & Reply Classifier.
    Detects Breakeven, Partial Close, Full Close, and 'Active' Reactivation.
    """
    lower = text.lower()

    action = None
    cancel_limits = False

    is_reply = reply_to_msg_id is not None
    word_count = len(lower.split())

    if not is_reply:
        # Non-reply chatter must never act on a live trade by accident:
        # 1) Anything that looks like a priced entry signal is NOT a management command.
        if DIRECTION_PATTERN.search(lower) and re.search(r"\b\d{4}(?:\.\d+)?\b", lower):
            return None
        # 2) Long messages are conversation, not commands.
        if word_count > 10:
            return None

    # Ambiguous single words (e.g. "be careful", "cut your losses", "exit early") are only
    # accepted as commands on replies or when the whole message is a 1-2 word command.
    ambiguous = {r"\bbe\b", r"\bsecure\b", r"\bexit\b", r"\bcut\b", r"\bcancel\b", r"\binvalidate\b"}

    def _hit(patterns: List[str]) -> bool:
        for p in patterns:
            if re.search(p, lower):
                if not is_reply and p in ambiguous and word_count > 2:
                    continue
                return True
        return False

    # 1. Breakeven
    if _hit(BREAKEVEN_PATTERNS):
        action = "ACTION_BREAKEVEN"
        cancel_limits = True

    # 2. Close Worst Entries & Hold Layers (must check before generic Partial/Full Close)
    if not action and _hit(WORST_HOLD_PATTERNS):
        action = "ACTION_CLOSE_WORST_HOLD_LAYERS"
        cancel_limits = False

    # 3. Reactivate ("Active") - only on replies or standalone command to avoid chat noise
    if not action:
        is_standalone_reactivate = bool(
            re.fullmatch(r"(?:re-?activate|active(?:\s+now)?|valid\s+again)[!.\s]*", lower.strip())
        )
        if is_reply or is_standalone_reactivate:
            if _hit(REACTIVATE_PATTERNS):
                action = "ACTION_REACTIVATE"

    # 4. Partial Close
    if not action and _hit(PARTIAL_CLOSE_PATTERNS):
        action = "ACTION_PARTIAL_CLOSE"

    # 5. Full Close / Invalidate / Cancel
    if not action and _hit(FULL_CLOSE_PATTERNS):
        action = "ACTION_FULL_CLOSE"
        cancel_limits = True

    if action:
        mgmt_obj = SignalAction({
            "event_type": "TRADE_MANAGEMENT",
            "management_action": action,
            "message_id": message_id,
            "reply_to_msg_id": reply_to_msg_id,
            "cancel_pending_limits": cancel_limits,
            "symbol": DEFAULT_SYMBOL,
            "raw_text": text,
        })
        logger.info(f"Gate 3: Classified trade management command: {action} (ReplyTo: {reply_to_msg_id})")
        return mgmt_obj

    return None


def extract_tps(text: str, direction: str = "BUY") -> List[float]:
    """
    Extract Take Profit prices from text.
    Sorts labeled TPs (TP1, TP2, ...) and seamlessly integrates unlabeled TPs
    (e.g. 'TP 3900' followed by 'TP2 3920') so TP1 is guaranteed first.
    """
    labeled: Dict[int, float] = {}
    unlabeled: List[float] = []

    for m in TP_PATTERN.finditer(text):
        idx_str = m.group(1) or m.group(2)
        price = float(m.group(3))
        if idx_str:
            idx = int(idx_str)
            if idx not in labeled:
                labeled[idx] = price
        else:
            if price not in unlabeled:
                unlabeled.append(price)

    if labeled and unlabeled:
        # Fill missing numeric keys with unlabeled TPs in occurrence order
        k = 1
        for p in unlabeled:
            while k in labeled:
                k += 1
            labeled[k] = p

    if labeled:
        return [labeled[k] for k in sorted(labeled)]

    return unlabeled


def gate_4_extract_signal(
    text: str,
    message_id: int = 0,
    is_edit: bool = False,
) -> Optional[SignalAction]:
    """
    Gate 4: New Signal Extraction.
    Defaults to DEFAULT_SYMBOL (XAUUSD) for any valid directional trigger verb.
    Extracts 3-tier zones (top, mid, bottom), SL, and TP1 front-run levels.
    """
    # 1. Direction Verb Detection
    dir_match = DIRECTION_PATTERN.search(text)
    if not dir_match:
        logger.debug("Gate 4 Reject: No valid directional verb detected.")
        return None

    matched_verb = dir_match.group(1).upper()
    if any(buy_kw in matched_verb for buy_kw in ("BUY", "LONG")):
        direction = "BUY"
    else:
        direction = "SELL"

    # 2. Symbol resolution - defaults straight to XAUUSD
    symbol = DEFAULT_SYMBOL

    # 3. Entry Zone & Price Split
    clean_lines = [
        line.strip()
        for line in text.splitlines()
        if not re.match(r"^(?:tp\d*|sl|stop|take)\b", line.strip(), re.IGNORECASE)
    ]
    zone_search_text = " \n ".join(clean_lines)

    zone_match = ZONE_PATTERN.search(zone_search_text)
    entry_tiers = None
    single_price = None
    is_zone = False
    is_instant_now = False
    max_orders = MAX_ORDERS_PER_SIGNAL

    if zone_match:
        p1 = float(zone_match.group(1))
        p2 = float(zone_match.group(2))
        # Guard: Reject lot-range noise like 0.01-0.02
        if max(p1, p2) >= 10.0:
            top_price = round(max(p1, p2), 2)
            bottom_price = round(min(p1, p2), 2)
            mid_price = round((top_price + bottom_price) / 2.0, 2)
            entry_tiers = {
                "top": top_price,
                "mid": mid_price,
                "bottom": bottom_price,
            }
            is_zone = True
            max_orders = 3
        else:
            zone_match = None

    if not is_zone:
        # Check for single-price entry limit (e.g. "Sell limit 3920" or "Buy 3895")
        single_match = SINGLE_PRICE_PATTERN.search(zone_search_text)
        if single_match:
            price = float(single_match.group(1))
            if price >= 10.0:
                single_price = round(price, 2)
                is_zone = False
                max_orders = 1  # Locked to 1 order for single-price entries
        else:
            # No zone or single price found -> instant market entry ("Buy now")
            is_instant_now = True
            max_orders = 1

    # 4. Take Profit & Proximity Trigger (Gold: 1 pip = $0.10, 20 pips = $2.00)
    tps = extract_tps(text, direction=direction)
    raw_tp1 = None
    raw_tp2 = None
    raw_tp3 = None
    final_tp = None
    proximity_close_price = None

    if tps:
        raw_tp1 = tps[0]
        if len(tps) > 1:
            raw_tp2 = tps[1]
        if len(tps) > 2:
            raw_tp3 = tps[2]
        pip_distance_20 = TP1_CLOSE_PROXIMITY_PIPS * 0.10  # 2.00 points
        if direction == "BUY":
            final_tp = round(raw_tp1 - TP_FRONT_RUN_OFFSET, 2)
            proximity_close_price = round(raw_tp1 - pip_distance_20, 2)
        else:
            final_tp = round(raw_tp1 + TP_FRONT_RUN_OFFSET, 2)
            proximity_close_price = round(raw_tp1 + pip_distance_20, 2)

    # 5. Stop Loss
    sl_match = SL_PATTERN.search(text)
    raw_sl = float(sl_match.group(1)) if sl_match else None

    action = SignalAction({
        "event_type": "NEW_SIGNAL",
        "message_id": message_id,
        "is_edit": is_edit,
        "symbol": symbol,
        "direction": direction,
        "is_zone": is_zone,
        "is_instant_now": is_instant_now,
        "single_price": single_price,
        "layer_lots": LAYER_LOTS,
        "max_orders": max_orders,
        "entry_tiers": entry_tiers,
        "sl": raw_sl,
        "sl_valid": False,
        "raw_tp1": raw_tp1,
        "raw_tp2": raw_tp2,
        "raw_tp3": raw_tp3,
        "final_tp": final_tp,
        "proximity_close_price": proximity_close_price,
        "raw_text": text,
    })

    return action


def gate_5_validate_price_sanity(
    action: SignalAction,
    live_price: Optional[float] = None,
    max_sl_pips: float = MAX_SL_PIPS,
) -> SignalAction:
    """
    Gate 5: Price & Typo Sanity Validation.
    Prevents missing-digit typos (e.g. SL 387 vs 3875) from wiping accounts.
    """
    direction = action.get("direction", "BUY")
    raw_sl = action.get("sl")
    final_tp = action.get("final_tp")
    raw_tp1 = action.get("raw_tp1")
    tiers = action.get("entry_tiers")
    single_p = action.get("single_price")

    if raw_sl is None:
        action["sl_valid"] = False
        return action

    sl_ref = None
    tp_ref = None
    if tiers:
        sl_ref = tiers["bottom"] if direction == "BUY" else tiers["top"]
        tp_ref = tiers["top"] if direction == "BUY" else tiers["bottom"]
    elif single_p is not None:
        sl_ref = single_p
        tp_ref = single_p
    elif live_price is not None:
        sl_ref = live_price
        tp_ref = live_price

    if sl_ref is None:
        # No entry price available (e.g. "Buy now" without zone).
        # Reject obvious missing-digit typos on Gold (< 1000.0)
        if raw_sl is not None and raw_sl < 1000.0:
            logger.warning(f"Gate 5 Sanity Check: Rejected anomalous SL {raw_sl} (< 1000.0 on Gold). Possible typo!")
            action["sl"] = None
            action["sl_valid"] = False
            return action

        # If SL and TP1 are both present, validate distance and direction
        if raw_sl is not None and raw_tp1 is not None:
            diff_tp_sl = abs(raw_tp1 - raw_sl)
            if diff_tp_sl > (max_sl_pips * 0.10 * 1.5):
                logger.warning(
                    f"Gate 5 Sanity Check: Distance between TP1 ({raw_tp1}) and SL ({raw_sl}) "
                    f"is {diff_tp_sl:.2f} points, exceeding safe limits. Possible typo!"
                )
                action["sl"] = None
                action["sl_valid"] = False
                return action

            if direction == "BUY" and raw_sl < raw_tp1:
                action["sl_valid"] = True
            elif direction == "SELL" and raw_sl > raw_tp1:
                action["sl_valid"] = True
            else:
                action["sl"] = None
                action["sl_valid"] = False
        else:
            action["sl_valid"] = raw_sl is not None  # Accept if present, no ref to validate against
        return action

    # Gold pip size is 0.10. 350 pips = 35.00 points
    pip_size = 0.10
    max_distance = max_sl_pips * pip_size

    diff = abs(sl_ref - raw_sl)

    # Distance sanity cap
    if diff > max_distance:
        logger.warning(
            f"Gate 5 Sanity Check: Rejected anomalous SL {raw_sl}. "
            f"Distance {diff:.2f} exceeds cap {max_distance:.2f} ({max_sl_pips} pips). Possible typo!"
        )
        action["sl"] = None
        action["sl_valid"] = False
        return action

    # Directional sanity check
    if direction == "BUY":
        if raw_sl >= sl_ref:
            logger.warning(f"Gate 5 Sanity Check: SL {raw_sl} cannot be >= bottom entry {sl_ref} on BUY.")
            action["sl"] = None
            action["sl_valid"] = False
            return action
        if final_tp is not None and tp_ref is not None and final_tp <= tp_ref:
            logger.warning(f"Gate 5 Sanity Check: TP {final_tp} cannot be <= top entry {tp_ref} on BUY.")
            action["final_tp"] = None
    else:  # SELL
        if raw_sl <= sl_ref:
            logger.warning(f"Gate 5 Sanity Check: SL {raw_sl} cannot be <= top entry {sl_ref} on SELL.")
            action["sl"] = None
            action["sl_valid"] = False
            return action
        if final_tp is not None and tp_ref is not None and final_tp >= tp_ref:
            logger.warning(f"Gate 5 Sanity Check: TP {final_tp} cannot be >= bottom entry {tp_ref} on SELL.")
            action["final_tp"] = None

    action["sl_valid"] = True
    return action


# ---------------------------------------------------------------------------
# MAIN PIPELINE ENTRY POINT
# ---------------------------------------------------------------------------

def parse_message(
    text: Optional[str],
    message_id: int = 0,
    is_edit: bool = False,
    is_reply: bool = False,
    reply_to_msg_id: Optional[int] = None,
    is_forward: bool = False,
    live_price: Optional[float] = None,
) -> Optional[SignalAction]:
    """
    Main 6-Gate Sequential Pipeline conforming to PARSER_SPEC.md.
    Gate 1: Forward & Empty Message Check
    Gate 2: Announcement, Recap & Ignored Symbol Filter
    Gate 3: Trade Management & Reply Classifier (BE, Close, Reactivate)
    Gate 4: New Signal Extraction (Defaults to XAUUSD)
    Gate 5: Price & Typo Sanity Validation
    """
    if not gate_1_check_forward_and_empty(text, is_forward=is_forward):
        return None

    clean_text = text.strip()

    # Gate 2: Announcement & Ignored Symbol Filter
    if not gate_2_check_announcements_and_recaps(clean_text, is_reply=is_reply, is_edit=is_edit):
        return None

    # Gate 3: Trade Management (BE, Close, Reactivate)
    mgmt_action = gate_3_classify_trade_management(
        clean_text,
        message_id=message_id,
        reply_to_msg_id=reply_to_msg_id,
    )
    if mgmt_action:
        return mgmt_action

    # Gate 4: Signal Extraction
    signal_action = gate_4_extract_signal(
        clean_text,
        message_id=message_id,
        is_edit=is_edit,
    )
    if not signal_action:
        return None

    # Gate 5: Price Sanity Validation
    validated_action = gate_5_validate_price_sanity(
        signal_action,
        live_price=live_price,
    )

    logger.info(
        f"Pipeline Output: {validated_action['event_type']} | {validated_action['direction']} "
        f"{validated_action['symbol']} | Tiers={validated_action['entry_tiers']} | "
        f"SL={validated_action['sl']} (valid={validated_action['sl_valid']}) | "
        f"FinalTP={validated_action['final_tp']} | ProximityClose={validated_action['proximity_close_price']}"
    )

    return validated_action


# Compatibility aliases
SignalParseResult = SignalAction
parse_telegram_message = parse_message
