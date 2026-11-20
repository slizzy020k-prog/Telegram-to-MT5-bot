import logging
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import (
    ALLOW_FORWARDS,
    DEFAULT_SYMBOL,
    DEFAULT_TP_STEP_PIPS,
    IGNORED_SYMBOLS,
    LAYER_LOTS,
    MAX_ORDERS_PER_SIGNAL,
    MAX_SL_PIPS,
    MAX_TP_DISTANCE_DOLLARS,
    SINGLE_PRICE_LOT,
    TP2_STEP_PIPS,
    TP3_STEP_PIPS,
    TP4_STEP_PIPS,
    TP5_STEP_PIPS,
    TP1_CLOSE_PROXIMITY_PIPS,
    TP_FRONT_RUN_OFFSET,
)

logger = logging.getLogger("copier.parser")

# ---------------------------------------------------------------------------
# GATE 2 KEYWORDS & PATTERNS
# ---------------------------------------------------------------------------

_HARD_ACCOUNT_BLOCKERS = [
    r"[£$€]?\s*\d+\s*account",
    r"account\s+[£$€]?\s*\d+",
    r"[£$€]\s*\d+\s*(?:to|-|–|—|->)\s*[£$€]?\s*\d+",
    r"account\s+size",
    r"lot\s+size",
    r"\bhow\s+to\b",
]

_SOFT_RISK_TAG_BLOCKERS = [
    r"manage\s+your\s+risk",
    r"protect\s+your\s+capital",
    r"high\s+risk",
    r"mid\s+risk",
    r"low\s+risk",
    r"\bvip\b",
]

ACCOUNT_RISK_BLOCKERS = _HARD_ACCOUNT_BLOCKERS + _SOFT_RISK_TAG_BLOCKERS

VICTORY_RECAP_BLOCKERS = [
    r"\btp\d*\s+hit\b",                              # "TP1 hit", "TP hit"
    r"\bsmashed\b",                                  # "TP smashed"
    r"\bpips\s+(?:secured|booked|smashed|profit)\b", # "50 pips secured"
    r"\bprofit\s+(?:secured|booked|banked|taken)\b", # "profit booked"
    r"\bbooked\s+\d",                                # "booked 50 pips"
    r"\bsecured\s+\d",                               # "secured 100 pips"
    r"\bboom\b",                                     # "BOOM 🚀"
    r"\bentry\s+was\b",                              # "Buy entry was 3895"
    r"\bwas\s+(?:a\s+)?(?:great|good|perfect|beautiful)\b",
    r"\+\s*\d+\s*pips?\b",                           # "+50 pips"
    r"\blast\s+night\b",                             # "1 sell from last night"
    r"\byesterday\b",                                # past-tense recap
    r"\bhis\s+money\b",                              # promo / testimonial chatter
]

# ---------------------------------------------------------------------------
# GATE 3 TRADE MANAGEMENT INTENT PATTERNS
# ---------------------------------------------------------------------------

_UNAMBIGUOUS_BE_PATTERNS = [
    r"\bbreakevens?\b",
    r"\bbreak[\s\-]+evens?\b",
    r"\brisk[\s\-]+free\b",
    r"\bsl\s+(?:to|at)\s+entry\b",
    r"\bstops?\s+(?:to|at)\s+entry\b",
    r"\bsl\s+(?:to|at)\s+be\b",
    r"\b(?:go|set|move|put|shift|lock|bring|all|layers?|entries|entry|stops?|sl)\s+(?:your\s+|all\s+|the\s+)?(?:sl\s+|stops?\s+|entries\s+|layers?\s+)?(?:to\s+|at\s+|on\s+)?be\b",
    r"\bbe\s+(?:now|all|layers?|entries|your\s+entries|on\s+all|on\s+layers?|everyone|guys|lads|team|people)\b",
    r"\b(?:move|set|put)\s+(?:your\s+)?(?:sl|stop\s*loss|stops?)\s+(?:to\s+|at\s+)?(?:entry|be|break[\s\-]*even)\b",
    r"^\s*move\s+sl\s*[!.]*\s*$",
]

BREAKEVEN_PATTERNS = [r"\bbe\b"] + _UNAMBIGUOUS_BE_PATTERNS

_BE_NON_COMMAND_RE = re.compile(
    r"(?:"
    # Conditionals: "If we break even enter again.", "When price gets stopped at BE..."
    r"\b(?:if|when|whenever|in\s+case|should|once)\s+(?:we|it|you|price|gold|this|trade|trades|sells?|buys?|entries?)?\s*"
    r"(?:get\s+|got\s+|gets\s+|hit\s+|hits\s+|are\s+|is\s+)?"
    r"(?:stopped\s+(?:out\s+)?(?:at\s+|on\s+)?|closed\s+(?:at\s+|on\s+)?|taken\s+out\s+(?:at\s+|on\s+)?)?"
    r"(?:breakevens?|break[\s\-]+evens?|be)\b"
    r"|"
    # Past-tense / passive BE-hit recaps (scoped strictly to BE so "TP1 hit, go breakeven!" passes)
    r"\b(?:breakevens?|break[\s\-]+evens?|be)\s+(?:was\s+|is\s+|got\s+|just\s+|has\s+been\s+)?"
    r"(?:hit|hits|hitted|triggered|tagged|touched|smashed|stopped|wicked|taken\s+out|closed)\b"
    r"|"
    r"\b(?:hit|hits|hitted|triggered|tagged|touched|stopped(?:\s+out)?|wicked(?:\s+out)?|taken\s+out|closed(?:\s+out)?|out)\s+"
    r"(?:at\s+|on\s+|by\s+)?(?:our\s+|my\s+|the\s+)?(?:breakevens?|break[\s\-]+evens?|be)\b"
    r"|"
    # Negations: "don't breakeven", "wait to move sl to BE"
    r"\b(?:don'?t|do\s+not|no|not|never|avoid|hold\s+off(?:\s+on)?|wait(?:\s+for|\s+before|\s+to)?)\s+"
    r"(?:set\s+|go\s+|move\s+(?:sl\s+)?(?:to\s+)?|put\s+(?:sl\s+)?(?:to\s+)?)?"
    r"(?:breakevens?|break[\s\-]+evens?|be)\b"
    r"|"
    # Discretionary / optional chatter: "move sl to BE if you want", "if you'd like", "up to you"
    r"\b(?:if\s+(?:you|u)\s+(?:want(?:\s+to)?|would\s+like(?:\s+to)?|'d\s+like(?:\s+to)?|prefer|wish|choose)|"
    r"(?:it'?s\s+)?up\s+to\s+(?:you|u)|at\s+your\s+(?:own\s+)?discretion|optional(?:ly)?)\b"
    r")",
    re.IGNORECASE,
)

WORST_HOLD_PATTERNS = [
    r"\bclose\s+worst\b",
    r"\bworst\s+entr(?:y|ies)\b",
    r"\bhold\s+layers?\b",
    r"\bclose\s+bad\s+entr(?:y|ies)\b",
    r"\bbreakeven\s+worst\b",
]

_FUTURE_TENSE_CHATTER_RE = re.compile(
    r"\b(?:gonna|going\s+to|soon|about\s+to|might|will\s+soon)\b",
    re.IGNORECASE,
)

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

CANCEL_PENDING_PATTERNS = [
    r"\b(?:delete|cancel|remove|clear|drop|kill|void)\s+(?:all\s+|the\s+|my\s+|our\s+|this\s+|these\s+|remaining\s+|unfilled\s+)*(?:pending\s+)?(?:(?:buy|sell)(?:\s*(?:/|and|&|,)\s*(?:buy|sell))?\s+)+(?:(?:xauusd(?:\.[a-z0-9]+)?|gold|xau)\s+)?(?:pending\s+)?(?:limits?|stops?|orders?)\b",
    r"\b(?:delete|cancel|remove|clear|drop|kill|void)\s+(?:all\s+|the\s+|my\s+|our\s+|this\s+|these\s+|remaining\s+|unfilled\s+)*(?:pending\s+(?:limits?|stops?|orders?)|(?:buy|sell)\s+pending|limits|stops)\b",
    r"\b(?:delete|cancel|remove|clear|drop|kill|void)\s+(?:all\s+|the\s+|my\s+|our\s+|this\s+|these\s+|remaining\s+|unfilled\s+)*limit\b\s+\w+",
    r"\b(?:delete|cancel|remove|clear|drop|kill|void)\s+(?:the\s+|my\s+|our\s+|this\s+|these\s+|all\s+|remaining\s+|unfilled\s+)+limit\b",
    r"\b(?:delete|cancel|remove|clear|drop|kill|void)\s+pending\b",
]
_CANCEL_PENDING_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|didn'?t|not|never|no\s+need\s+to)\s+(?:delete|cancel|remove|clear|drop|kill|void)\b",
    re.IGNORECASE,
)

FULL_CLOSE_PATTERNS = [
    r"^\s*close\s*[!.]*\s*$",                        # bare "close" (whole message only)
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

_TP1_TARGET_RE = r"(?:tp(?:\s*#?\s*1)?|target\s*#?\s*1|take\s*profit\s*#?\s*1|(?:first|1st)\s+(?:tp|target|take\s*profit))"
_TP2_TARGET_RE = r"(?:tp\s*#?\s*2|target\s*#?\s*2|take\s*profit\s*#?\s*2|(?:second|2nd)\s+(?:tp|target|take\s*profit))"
_TP3_TARGET_RE = r"(?:tp\s*#?\s*3|target\s*#?\s*3|take\s*profit\s*#?\s*3|(?:third|3rd)\s+(?:tp|target|take\s*profit))"
_TP1_VERB_RE = r"(?:hit|hits|hitted|smashed|reached|done|cleared|achieved|completed|complete|taken|banked|secured|booked|touched|tagged|crushed)"
_TP1_AUX_RE = r"(?:(?:was|is|got|just|has\s+been|have\s+been)\s+)?"
_TP1_NOT_PRICE_AFTER = r"(?!\s*(?:[:=@\-]|[\u2600-\U0001FAFF])?\s*\d{3,6}(?:\.\d+)?\b(?!\s*pips?\b))"
_TP1_EMOJI_RE = r"[✅✔️☑️💥🔥🚀🎯💰👏🎉🏆💵🤑]+"


def _build_tp_hit_patterns(target_re: str) -> List[str]:
    return [
        rf"(?i)\b{target_re}\b\s*{_TP1_AUX_RE}{_TP1_VERB_RE}\b",
        rf"(?i)\b(?:hit|hits|smashed|reached|cleared|achieved|touched|tagged|crushed|booked|banked|secured|took)\s+{target_re}\b",
        rf"(?i)\b{target_re}\b{_TP1_NOT_PRICE_AFTER}\s*{_TP1_EMOJI_RE}\s*(?:\+?\s*\d{{1,4}}\s*pips?\b)?",
        rf"(?i){_TP1_EMOJI_RE}\s*{target_re}\b{_TP1_NOT_PRICE_AFTER}",
        rf"(?i)\b{target_re}\b\s*\+?\s*\d{{1,4}}\s*pips?\b",
    ]


TP1_HIT_REPLY_PATTERNS = _build_tp_hit_patterns(_TP1_TARGET_RE)
TP2_HIT_PATTERNS = _build_tp_hit_patterns(_TP2_TARGET_RE)
TP3_HIT_PATTERNS = _build_tp_hit_patterns(_TP3_TARGET_RE)

_TP_NEGATION_RE = re.compile(
    r"\b(?:not|didn'?t|did\s+not|hasn'?t|has\s+not|haven'?t|have\s+not|wasn'?t|isn'?t|won'?t|never|almost|nearly|before)\s+"
    r"(?:been\s+)?(?:hit|hits|smashed|reached|done|tp(?:\s*#?\s*[1-5])?|target\s*#?\s*[1-5]|(?:first|1st|second|2nd|third|3rd)\s+(?:tp|target))\b",
    re.IGNORECASE,
)
_TP1_NEGATION_RE = _TP_NEGATION_RE


def is_tp1_hit_message(text: Optional[str]) -> bool:
    """Return True if text indicates TP1 was hit/reached (never matching normal 'TP1 3900' entry lines)."""
    if not text:
        return False
    lower = text.lower()
    if _TP_NEGATION_RE.search(lower):
        return False
    return any(re.search(p, lower) for p in TP1_HIT_REPLY_PATTERNS)


def detect_tp_milestone_hit(text: Optional[str]) -> Optional[int]:
    """Return 1, 2, or 3 if text indicates TP1/TP2/TP3 was hit/reached; None otherwise."""
    if not text:
        return None
    lower = text.lower()
    if _TP_NEGATION_RE.search(lower):
        return None
    if any(re.search(p, lower) for p in TP3_HIT_PATTERNS):
        return 3
    if any(re.search(p, lower) for p in TP2_HIT_PATTERNS):
        return 2
    if any(re.search(p, lower) for p in TP1_HIT_REPLY_PATTERNS):
        return 1
    return None


_TRAIL_SL_INTENT_RE = re.compile(
    r"\b(?:trail(?:\s+sl|\s+stop(?:\s*loss)?)?|lock(?:\s+in)?(?:\s+profits?|\s+tp\s*#?\s*[1-4]|\s+target\s*#?\s*[1-4]))\b"
    r"|^\s*lock\s+(?:tp|target)\s*#?\s*[1-4]\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# GATE 4 SIGNAL EXTRACTION PATTERNS
# ---------------------------------------------------------------------------

# Expanded direction verbs supporting singular/plural limits and stops
DIRECTION_PATTERN = re.compile(
    r"\b(buy\s+(?:with\s+)?limits?|sell\s+(?:with\s+)?limits?|buy\s+(?:with\s+)?stops?|sell\s+(?:with\s+)?stops?|buy|sell|long|short)\b",
    re.IGNORECASE,
)

# Order-type detection patterns (distinguishing STOP orders from LIMIT orders)
_STOP_ORDER_PATTERN = re.compile(
    r"\b(?:(?:buy|sell|long|short)(?:\s+(?:xauusd(?:\.[a-z0-9]+)?|xau|gold|usd))?(?:\s+with)?\s+stops?|stop\s+(?:order|entry))\b",
    re.IGNORECASE,
)
_LIMIT_ORDER_PATTERN = re.compile(
    r"\b(?:limits?|(?:buy|sell|long|short)(?:\s+(?:xauusd(?:\.[a-z0-9]+)?|xau|gold|usd))?(?:\s+with)?\s+limits?)\b",
    re.IGNORECASE,
)

# Explicit immediate market execution intent ("Buy now", "Sell xauusd now", "Buy at market", "Market buy")
_MARKET_NOW_PATTERN = re.compile(
    r"\b(?:"
    r"(?:buy|sell|long|short)(?:\s+(?:xauusd(?:\.[a-z0-9]+)?|xau|gold|usd))?"
    r"(?:\s+(?:right|at|@))?\s+(?:now|market(?:\s+(?:price|order|execution))?|instant(?:ly)?|cmp|current\s+market(?:\s+price)?)"
    r"|(?:market|instant)\s+(?:buy|sell|long|short)"
    r")\b",
    re.IGNORECASE,
)

# Conversational / past-tense chatter phrases that must never trigger un-targeted trade entries
_PROSE_CHATTER_PATTERN = re.compile(
    r"\b(?:"
    r"when\s+(?:u|you)\s+sent"
    r"|i\s+(?:went|bought|brought|sold|took|held|entered|closed)"
    r"|(?:sold|bought|brought)\s+it\b"
    r"|from\s+(?:last\s+night|earlier|yesterday)"
    r"|was\s+able\s+to\s+(?:buy|sell)"
    r"|buy\s+my\b"
    r"|sell\s+straight(?:\s+into)?"
    r"|holding\s+(?:sells|buys)"
    r"|(?:sells|buys)\s+running"
    r")\b",
    re.IGNORECASE,
)

# Zone pattern: 3895-3887, 3895 - 3887, 3895 to 3887, 3895/3887
ZONE_PATTERN = re.compile(
    r"(\d{2,6}(?:\.\d+)?)\s*(?:-|–|—|/|\bto\b)\s*(\d{2,6}(?:\.\d+)?)",
    re.IGNORECASE,
)

# Single price entry (e.g. "Buy limit 3920", "Buy xauusd stop 3900", "Sell limits @ 3860", "sell with limit 3890")
SINGLE_PRICE_PATTERN = re.compile(
    r"\b(?:(?:buy|sell|long|short)(?:\s+(?:xauusd(?:\.[a-z0-9]+)?|xau|gold|usd))?(?:\s+with)?(?:\s+(?:limits?|stops?))?|(?:limits?|stops?|entry|entries|zone|price))\s*(?:(?:at|around|near|zone|area|@|:|=|-)\s*)*(\d{2,6}(?:\.\d+)?)\b(?!\s*(?:pips?|%|lots?)\b)",
    re.IGNORECASE,
)

# Take Profit pattern: TP1..TP9, TP 1, TP#1, Target 1, etc. (never matching 'TP1💥50 PIPs')
TP_PATTERN = re.compile(
    r"\b(?:tp|take\s*profit|target)(?:#?([1-9])(?:\s*[:=@\-\.]\s*|\s*->\s*|\s+)|\s+([1-9])(?:\s*[:=@\-\.]\s*|\s*->\s*|\s+)|\s*[:=@\-]?\s*(?:->\s*)?)(?:(?:to|at|is)\s+)?(?:[\u2600-\U0001FAFF]\s*)?(\d{2,6}(?:\.\d+)?)\b(?!\s*pips?\b)",
    re.IGNORECASE,
)

# Context-gated "FP" typo for "TP" at start of line (e.g. "FP 3685" in a real Gold setup with TP/SL anchor)
_FP_TYPO_LINE_PATTERN = re.compile(
    r"^\s*fp(?:#?([1-9])(?:\s*[:=@\-\.]\s*|\s*->\s*|\s+)|\s+([1-9])(?:\s*[:=@\-\.]\s*|\s*->\s*|\s+)|\s*[:=@\-]?\s*(?:->\s*)?)(?:(?:to|at|is)\s+)?(?:[\u2600-\U0001FAFF]\s*)?(\d{2,6}(?:\.\d+)?)\b(?!\s*pips?\b)",
    re.IGNORECASE | re.MULTILINE,
)

# Stop Loss patterns (explicit SL/Stoploss + standalone Stop excluding "Buy stop <price>")
_EXPLICIT_SL_PATTERN = re.compile(
    r"\b(?:sl|stop\s*loss|stoploss)\s*(?:[:=@\-]|->|\b(?:to|at|is)\b)?\s*(\d{2,6}(?:\.\d+)?)\b(?!\s*pips?\b)",
    re.IGNORECASE,
)
_STANDALONE_STOP_SL_PATTERN = re.compile(
    r"\bstop\s*(?:[:=@\-]|->|\b(?:to|at|is)\b)?\s*(\d{2,6}(?:\.\d+)?)\b(?!\s*pips?\b)",
    re.IGNORECASE,
)
SL_PATTERN = re.compile(
    r"\b(?:sl|stop\s*loss|stoploss|(?<!buy\s)(?<!sell\s)(?<!long\s)(?<!short\s)stop)\s*(?:[:=@\-]|->|\b(?:to|at|is)\b)?\s*(\d{2,6}(?:\.\d+)?)\b(?!\s*pips?\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 4-TIER ZONE & TP1-TP5 DERIVATION HELPERS
# ---------------------------------------------------------------------------

def _decimals_for_pip(pip_size: float) -> int:
    """Return rounding decimals appropriate for pip_size (minimum 2 for Gold)."""
    if pip_size <= 0:
        return 2
    formatted = f"{pip_size:.8f}".rstrip("0").rstrip(".")
    return max(2, len(formatted.split(".")[1]) if "." in formatted else 0)


def compute_zone_4tiers(
    top: float,
    bottom: float,
    direction: str = "BUY",
    digits: int = 2,
) -> Dict[str, float]:
    """
    Split a price zone across [0%, 33.3%, 66.7%, 100%] into 4 ordered layer entries:
      - BUY  (top -> bottom): L1=top, L2=top-1/3, L3=top-2/3, L4=bottom
      - SELL (bottom -> top): L1=bottom, L2=bottom+1/3, L3=bottom+2/3, L4=top
    """
    hi = round(max(float(top), float(bottom)), digits)
    lo = round(min(float(top), float(bottom)), digits)
    span = hi - lo
    is_buy = (direction or "BUY").upper() == "BUY"
    weights = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)
    if is_buy:
        prices = [round(hi - w * span, digits) for w in weights]
    else:
        prices = [round(lo + w * span, digits) for w in weights]
    return {"L1": prices[0], "L2": prices[1], "L3": prices[2], "L4": prices[3]}


def _longest_monotonic_indices(
    tps: Sequence[float],
    direction: str = "BUY",
) -> Tuple[List[int], List[int]]:
    """
    Compute kept and dropped indices using exact Longest Strictly Monotonic Subsequence DP
    (strictly ascending for BUY, strictly descending for SELL).
    Primary score: maximum subsequence length.
    Tie-breaker 1: minimum total span abs(seq[-1] - seq[0]).
    Tie-breaker 2: minimum max adjacent gap max(abs(seq[i] - seq[i-1])).
    Tie-breaker 3: earliest index tuple (keeps valid TP1).
    """
    if not tps:
        return [], []
    vals = [float(v) for v in tps]
    n = len(vals)
    if n <= 1:
        return [0], []

    is_buy = (direction or "BUY").upper() == "BUY"

    def _is_step_valid(prev: float, curr: float) -> bool:
        return (curr > prev) if is_buy else (curr < prev)

    # L[i] = length of longest strictly monotonic subsequence ending at index i
    L = [1] * n
    for i in range(n):
        for j in range(i):
            if _is_step_valid(vals[j], vals[i]):
                if L[j] + 1 > L[i]:
                    L[i] = L[j] + 1

    max_len = max(L)
    if max_len == n:
        return list(range(n)), []

    all_paths: List[List[int]] = []

    def _backtrack(curr_idx: int, rem_len: int, current_path: List[int]) -> None:
        if rem_len == 1:
            all_paths.append([curr_idx] + current_path)
            return
        curr_val = vals[curr_idx]
        for prev_idx in range(curr_idx):
            if L[prev_idx] == rem_len - 1 and _is_step_valid(vals[prev_idx], curr_val):
                _backtrack(prev_idx, rem_len - 1, [curr_idx] + current_path)

    for i in range(n):
        if L[i] == max_len:
            _backtrack(i, max_len, [])

    if not all_paths:
        return list(range(n)), []

    def _score_path(p: List[int]) -> Tuple[float, float, Tuple[int, ...]]:
        subseq = [vals[idx] for idx in p]
        span = abs(subseq[-1] - subseq[0])
        max_gap = max((abs(subseq[k] - subseq[k - 1]) for k in range(1, len(subseq))), default=0.0)
        return (span, max_gap, tuple(p))

    best_path = min(all_paths, key=_score_path)
    kept_set = set(best_path)
    kept_indices = [i for i in range(n) if i in kept_set]
    dropped_indices = [i for i in range(n) if i not in kept_set]
    return kept_indices, dropped_indices


def sanitize_monotonic_tps(
    tps: Sequence[float],
    direction: str = "BUY",
) -> Tuple[List[float], List[float]]:
    """
    Sanitize Take Profit sequence using exact Longest Strictly Monotonic Subsequence DP
    (strictly ascending for BUY, strictly descending for SELL).
    Primary score: maximum subsequence length.
    Tie-breaker 1: minimum total span abs(seq[-1] - seq[0]).
    Tie-breaker 2: minimum max adjacent gap max(abs(seq[i] - seq[i-1])).
    Tie-breaker 3: earliest index tuple (keeps valid TP1).
    Returns (kept_tps, dropped_tps) in original order.
    """
    if not tps:
        return [], []
    vals = [float(v) for v in tps]
    kept_indices, dropped_indices = _longest_monotonic_indices(vals, direction=direction)
    return [vals[i] for i in kept_indices], [vals[i] for i in dropped_indices]


def _sanitize_labeled_tps_monotonic(
    labeled_tps: Dict[str, float],
    direction: str = "BUY",
) -> Tuple[Dict[str, float], List[float]]:
    """
    Sanitize labeled TP dictionary using exact Longest Strictly Monotonic Subsequence DP
    over stages ordered by TP1..TP5. Drops out-of-order typo stages and cleanly re-numbers/keeps valid stages.
    """
    if not labeled_tps:
        return {}, []

    parsed_items: List[Tuple[int, str, float]] = []
    for k, v in labeled_tps.items():
        if v is None:
            continue
        k_str = str(k).strip().upper()
        if k_str.startswith("TP") and len(k_str) == 3 and k_str[2].isdigit():
            idx = int(k_str[2])
        elif k_str.isdigit():
            idx = int(k_str)
        else:
            continue
        if 1 <= idx <= 5:
            try:
                fv = float(v)
                if fv > 0:
                    parsed_items.append((idx, f"TP{idx}", fv))
            except (TypeError, ValueError):
                pass

    if not parsed_items:
        return {}, []

    parsed_items.sort(key=lambda x: x[0])
    raw_prices = [item[2] for item in parsed_items]
    kept_idx, dropped_idx = _longest_monotonic_indices(raw_prices, direction=direction)

    if not dropped_idx:
        return {item[1]: item[2] for item in parsed_items}, []

    dropped_prices = [raw_prices[i] for i in dropped_idx]
    kept_prices = [raw_prices[i] for i in kept_idx]

    starts_at_tp1 = (parsed_items[0][0] == 1)
    if starts_at_tp1:
        clean_labeled = {f"TP{i + 1}": kept_prices[i] for i in range(len(kept_prices))}
    else:
        clean_labeled = {parsed_items[i][1]: parsed_items[i][2] for i in kept_idx}

    return clean_labeled, dropped_prices


def derive_tp_levels(
    tps: Sequence[float],
    direction: str = "BUY",
    pip_size: float = 0.10,
    step_pips: Optional[Sequence[float]] = None,
) -> Dict[str, float]:
    """
    Build deterministic TP1..TP5 target dictionary from extracted TPs.
    Returns {} if tps is empty; otherwise fills missing targets up to TP5 sequentially.
    """
    if not tps:
        return {}
    sanitized_tps, _ = sanitize_monotonic_tps(tps, direction=direction)
    digits = _decimals_for_pip(pip_size)
    source_tps = sanitized_tps if sanitized_tps else list(tps)
    base = [round(float(val), digits) for val in source_tps[:5]]

    steps = (
        [float(s) for s in step_pips]
        if step_pips is not None
        else [float(TP2_STEP_PIPS), float(TP3_STEP_PIPS), float(TP4_STEP_PIPS), float(TP5_STEP_PIPS)]
    )
    while len(steps) < 4:
        steps.append(float(DEFAULT_TP_STEP_PIPS))

    sign = 1.0 if (direction or "BUY").upper() == "BUY" else -1.0
    for idx in range(len(base), 5):
        step = steps[idx - 1]
        base.append(round(base[-1] + sign * step * pip_size, digits))

    return {f"TP{i + 1}": base[i] for i in range(5)}


def merge_tp_levels_for_active_signal(
    existing_tps: Optional[Dict[str, float]],
    labeled_tps: Optional[Dict[str, float]] = None,
    unlabeled_tps: Sequence[float] = (),
    direction: str = "BUY",
    pip_size: float = 0.10,
    step_pips: Optional[Sequence[float]] = None,
) -> Dict[str, float]:
    """
    Merge labeled_tps ('TP1'..'TP5') and unlabeled_tps into existing_tps for an active signal.
    Preserves untouched stages (e.g. TP1 stays intact when 'TP2 3918 TP3 3930' arrives)
    and re-derives missing or non-monotonic stages above updated stages up to TP5.
    """
    digits = _decimals_for_pip(pip_size)
    merged: Dict[str, float] = {}
    if isinstance(existing_tps, dict):
        for i in range(1, 6):
            k = f"TP{i}"
            v = existing_tps.get(k)
            if v is not None:
                try:
                    fv = float(v)
                    if fv > 0:
                        merged[k] = round(fv, digits)
                except (TypeError, ValueError):
                    pass

    explicit_labeled: Dict[str, float] = {}
    updated_stages: List[int] = []
    if isinstance(labeled_tps, dict):
        for raw_k, raw_v in labeled_tps.items():
            if raw_v is None:
                continue
            k_str = str(raw_k).strip().upper()
            if k_str.startswith("TP") and len(k_str) == 3 and k_str[2].isdigit():
                idx = int(k_str[2])
            elif k_str.isdigit():
                idx = int(k_str)
            else:
                continue
            if 1 <= idx <= 5:
                try:
                    fv = round(float(raw_v), digits)
                    if fv > 0:
                        key = f"TP{idx}"
                        explicit_labeled[key] = fv
                        merged[key] = fv
                        updated_stages.append(idx)
                except (TypeError, ValueError):
                    pass

    if unlabeled_tps:
        stage_cursor = 1
        for raw_v in unlabeled_tps:
            if raw_v is None:
                continue
            try:
                fv = round(float(raw_v), digits)
            except (TypeError, ValueError):
                continue
            if fv <= 0:
                continue
            while stage_cursor <= 5 and f"TP{stage_cursor}" in explicit_labeled:
                stage_cursor += 1
            if stage_cursor > 5:
                break
            key = f"TP{stage_cursor}"
            merged[key] = fv
            updated_stages.append(stage_cursor)
            stage_cursor += 1

    if not merged:
        return {}

    # Enforce strict monotonicity across populated stages before re-deriving missing slots
    present_stage_keys = [f"TP{i}" for i in range(1, 6) if f"TP{i}" in merged]
    if len(present_stage_keys) >= 2:
        stage_prices = [merged[k] for k in present_stage_keys]
        kept_idx, dropped_idx = _longest_monotonic_indices(stage_prices, direction=direction)
        for idx in dropped_idx:
            k = present_stage_keys[idx]
            del merged[k]
            if k in explicit_labeled:
                del explicit_labeled[k]
            idx_num = int(k[2])
            if idx_num in updated_stages:
                updated_stages.remove(idx_num)

    steps = (
        [float(s) for s in step_pips]
        if step_pips is not None
        else [float(TP2_STEP_PIPS), float(TP3_STEP_PIPS), float(TP4_STEP_PIPS), float(TP5_STEP_PIPS)]
    )
    while len(steps) < 4:
        steps.append(float(DEFAULT_TP_STEP_PIPS))

    is_buy = (direction or "BUY").upper() == "BUY"
    sign = 1.0 if is_buy else -1.0

    # If TP1 is missing (e.g. existing_tps was empty and only TP2+ was supplied), back-fill down to TP1
    if "TP1" not in merged:
        first_present = min(int(k[2]) for k in merged if k.startswith("TP") and k[2:].isdigit())
        for idx in range(first_present - 1, 0, -1):
            step = steps[idx - 1]
            merged[f"TP{idx}"] = round(merged[f"TP{idx + 1}"] - sign * step * pip_size, digits)

    for idx in range(2, 6):
        key = f"TP{idx}"
        prev_val = merged[f"TP{idx - 1}"]
        step = steps[idx - 2]
        if key in explicit_labeled or idx in updated_stages:
            continue
        cur_val = merged.get(key)
        is_non_monotonic = (
            cur_val is None
            or (is_buy and cur_val <= prev_val)
            or (not is_buy and cur_val >= prev_val)
        )
        if is_non_monotonic:
            merged[key] = round(prev_val + sign * step * pip_size, digits)

    return {f"TP{i}": merged[f"TP{i}"] for i in range(1, 6) if f"TP{i}" in merged}


def _extract_sl(text: str) -> Optional[float]:
    """Extract stop-loss price without misclassifying 'Buy stop <price>' as SL."""
    explicit_match = _EXPLICIT_SL_PATTERN.search(text)
    if explicit_match:
        return float(explicit_match.group(1))
    scrubbed = _STOP_ORDER_PATTERN.sub(" ", text)
    stop_match = _STANDALONE_STOP_SL_PATTERN.search(scrubbed)
    if stop_match:
        return float(stop_match.group(1))
    return None


class ZoneInfo:
    """Helper representing an entry zone with legacy (top/mid/bottom) and v3.0 4-tier (L1..L4) access."""

    def __init__(self, top: float, mid: float, bottom: float, direction: str = "BUY"):
        hi = round(max(float(top), float(bottom)), 2)
        lo = round(min(float(top), float(bottom)), 2)
        self.top = hi
        self.mid = round(float(mid), 2)
        self.bottom = lo
        self.direction = (direction or "BUY").upper()
        self.tiers: Dict[str, float] = compute_zone_4tiers(self.top, self.bottom, self.direction)

    def get_tiers(self, direction: Optional[str] = None) -> Dict[str, float]:
        """Return 4-layer entry prices ordered L1..L4 for the given direction."""
        return compute_zone_4tiers(self.top, self.bottom, direction or self.direction)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "top": self.top,
            "mid": self.mid,
            "bottom": self.bottom,
            "tiers": dict(self.tiers),
        }

    def __repr__(self) -> str:
        return f"ZoneInfo(top={self.top}, mid={self.mid}, bottom={self.bottom}, tiers={self.tiers})"


class SignalAction(dict):
    """
    Standardized Action Object implementing PARSER_SPEC & v3.0 JSON contract.
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
            if name == "tiers":
                zone_tiers = self.get("zone_tiers")
                if zone_tiers and isinstance(zone_tiers, dict):
                    return zone_tiers
                entry_tiers = self.get("entry_tiers")
                if entry_tiers and isinstance(entry_tiers, dict):
                    return compute_zone_4tiers(
                        entry_tiers["top"],
                        entry_tiers["bottom"],
                        self.get("direction", "BUY"),
                    )
                return None
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
            if name == "is_limit_order":
                return bool(self.get("is_limit_order", False))
            if name == "is_stop_order":
                return bool(self.get("is_stop_order", False))
            if name == "order_intent":
                if self.get("is_stop_order"):
                    return "STOP"
                if self.get("is_limit_order"):
                    return "LIMIT"
                if self.get("is_instant_now"):
                    return "MARKET"
                return "AUTO"
            if name == "tp_levels":
                return self.get("tp_levels") or {}
            if name == "explicit_tps":
                return self.get("explicit_tps") or []
            if name == "labeled_tps":
                return self.get("labeled_tps") or {}
            if name == "unlabeled_tps":
                return self.get("unlabeled_tps") or []
            if name == "broker_symbol":
                return self.get("symbol")
            if name == "zone":
                tiers = self.get("entry_tiers")
                if tiers:
                    return ZoneInfo(
                        top=tiers["top"],
                        mid=tiers["mid"],
                        bottom=tiers["bottom"],
                        direction=self.get("direction", "BUY"),
                    )
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


def _has_real_trade_structure(text: str) -> bool:
    """
    Return True when text contains structured Gold trade levels (explicit TP >= 500,
    explicit SL >= 500, or a directional verb with a Gold entry price/zone >= 500),
    so risk-tag disclaimers ('High risk', 'Manage your risk', 'VIP') on real signals
    or late edits never cause false Gate 2 rejections.
    """
    for m in TP_PATTERN.finditer(text):
        try:
            if 500.0 <= float(m.group(3)) < 100000.0:
                return True
        except (TypeError, ValueError):
            pass

    sl_m = _EXPLICIT_SL_PATTERN.search(text)
    if sl_m:
        try:
            if 500.0 <= float(sl_m.group(1)) < 100000.0:
                return True
        except (TypeError, ValueError):
            pass

    if DIRECTION_PATTERN.search(text):
        # Actionable directional market execution ("Sell xauusd now", "Buy gold now", "Sell now", etc.)
        # even without explicit prices yet must not be blocked by soft risk-tag blockers.
        if re.search(r"\b(?:now|market|instant|instantly|gold|xauusd)\b", text, re.IGNORECASE):
            return True

        entry_segment = _strip_entry_text(text)
        for pm in re.finditer(
            r"(?<![.\d])(\d{3,6}(?:\.\d+)?)(?![.\d])(?!\s*(?:pips?|%|lots?|x)\b)",
            entry_segment,
            re.IGNORECASE,
        ):
            try:
                if 500.0 <= float(pm.group(1)) < 100000.0:
                    return True
            except (TypeError, ValueError):
                pass

    return False


def _is_unambiguous_breakeven_cmd(lower: str) -> bool:
    """
    Return True when `lower` contains an explicit breakeven command (without needing a reply),
    excluding priced new-signal entries, past-tense recaps, conditionals, and negations.
    """
    words = lower.split()
    if not words or len(words) > 30:
        return False
    if DIRECTION_PATTERN.search(lower) and re.search(r"\b\d{4}(?:\.\d+)?\b", lower):
        return False
    if re.search(r"\bentry\s+was\b|\blast\s+night\b|\byesterday\b", lower):
        return False
    if _BE_NON_COMMAND_RE.search(lower):
        return False
    return any(re.search(p, lower) for p in _UNAMBIGUOUS_BE_PATTERNS)


def gate_2_check_announcements_and_recaps(
    text: str,
    is_reply: bool = False,
    is_edit: bool = False,
) -> bool:
    """
    Gate 2: Announcement & Noise Filter + Ignored Symbols check.
    Blocks:
    1. Messages mentioning non-Gold tickers (from IGNORED_SYMBOLS).
    2. Hard account / lot sizing tables & 'how to' questions.
    3. Standalone risk disclaimers ('high risk', 'manage your risk', 'vip') ONLY when
       no real Gold trade/target structure is present.
    4. Past-tense victory recaps (unless is_reply, is_edit, short TP milestone, or explicit BE command).
    """
    upper = text.upper()
    lower = text.lower()

    # 1. Ignored Symbols Blocker
    for sym in IGNORED_SYMBOLS:
        if re.search(rf"\b{re.escape(sym.upper())}\b", upper):
            logger.info(f"Gate 2 Reject (Non-Gold Symbol): Ignored pair '{sym}' detected in text.")
            return False

    # 2a. Hard Account / Sizing / 'How To' Blocker
    for pattern in _HARD_ACCOUNT_BLOCKERS:
        if re.search(pattern, lower):
            logger.info(f"Gate 2 Reject (Account/Risk Table): Matched pattern '{pattern}'")
            return False

    # 2b. Soft Risk-Tag Disclaimers (allowed when part of a genuine priced signal or TP/SL edit)
    if not _has_real_trade_structure(text):
        for pattern in _SOFT_RISK_TAG_BLOCKERS:
            if re.search(pattern, lower):
                logger.info(f"Gate 2 Reject (Account/Risk Table): Matched pattern '{pattern}'")
                return False

    # 3. Past-Tense Victory Recap Blocker
    if not is_reply and not is_edit:
        is_short_tp_milestone_cmd = (
            len(lower.split()) <= 8
            and not DIRECTION_PATTERN.search(lower)
            and not re.search(r"\b\d{4}(?:\.\d+)?\b", lower)
            and detect_tp_milestone_hit(lower) in (1, 2, 3)
        )
        if not is_short_tp_milestone_cmd and not _is_unambiguous_breakeven_cmd(lower):
            for pattern in VICTORY_RECAP_BLOCKERS:
                if re.search(pattern, lower):
                    logger.info(f"Gate 2 Reject (Victory Recap): Matched pattern '{pattern}'")
                    return False

    return True


def gate_3_classify_trade_management(
    text: str,
    message_id: int = 0,
    reply_to_msg_id: Optional[int] = None,
    is_reply: Optional[bool] = None,
    is_edit: bool = False,
) -> Optional[SignalAction]:
    """
    Gate 3: Trade Management & Reply Classifier.
    Detects Breakeven, Close Worst/Hold Layers, Partial Close, Full Close,
    'Active' Reactivation, TP1..TP3-Hit alerts/replies, Trail SL / Lock TP,
    and active-trade SL / TP1..TP5 target adjustments.
    """
    lower = text.lower()

    action = None
    cancel_limits = False
    tp_hit_stage: Optional[int] = None

    effective_is_reply = (
        (reply_to_msg_id is not None)
        if is_reply is None
        else (bool(is_reply) or reply_to_msg_id is not None)
    )
    word_count = len(lower.split())
    has_direction = bool(DIRECTION_PATTERN.search(lower))
    has_4digit_price = bool(re.search(r"\b\d{4}(?:\.\d+)?\b", lower))
    unambig_be = _is_unambiguous_breakeven_cmd(lower)

    _matches_cancel_pending_phrase = any(re.search(p, lower) for p in CANCEL_PENDING_PATTERNS)
    if _matches_cancel_pending_phrase and _CANCEL_PENDING_NEGATION_RE.search(lower):
        return None
    _is_cancel_pending = (
        _matches_cancel_pending_phrase
        and "?" not in lower
        and not _MARKET_NOW_PATTERN.search(lower)
        and not re.search(r"\b(?:sl|tp\d*|stop\s*loss|take\s*profit)\b", lower)
    )

    if not effective_is_reply:
        # Non-reply chatter must never act on a live trade by accident:
        # 1) Anything that looks like a priced entry signal is NOT a management command.
        if has_direction and has_4digit_price and not _is_cancel_pending:
            return None
        # 2) Long messages are conversation, not commands (except explicit TP-milestone-hit edits or unambiguous BE commands).
        if word_count > 10:
            edit_hit_stage = detect_tp_milestone_hit(lower) if is_edit else None
            if edit_hit_stage == 1:
                action = "ACTION_TP1_HIT_REPLY"
                cancel_limits = True
                tp_hit_stage = 1
            elif edit_hit_stage == 2:
                action = "ACTION_TP2_HIT"
                cancel_limits = False
                tp_hit_stage = 2
            elif edit_hit_stage == 3:
                action = "ACTION_TP3_HIT"
                cancel_limits = False
                tp_hit_stage = 3
            elif not unambig_be and not (_is_cancel_pending and word_count <= 20):
                return None

    # Ambiguous single words (e.g. "be careful", "cut your losses", "exit early") are only
    # accepted as commands on replies or when the whole message is a 1-2 word command.
    ambiguous = {r"\bbe\b", r"\bsecure\b", r"\bexit\b", r"\bcut\b", r"\bcancel\b", r"\binvalidate\b"}

    def _hit(patterns: List[str]) -> bool:
        for p in patterns:
            if re.search(p, lower):
                if not effective_is_reply and p in ambiguous and word_count > 2:
                    continue
                return True
        return False

    # Reject future-tense conversational chatter before trade management classification
    if _FUTURE_TENSE_CHATTER_RE.search(lower):
        logger.info(f"Gate 3: Rejected future-tense chatter message | text={text!r}")
        return None

    # 1. Close Worst Entries & Hold Layers (must check before generic Partial/Full Close)
    if not action and _hit(WORST_HOLD_PATTERNS):
        action = "ACTION_CLOSE_WORST_HOLD_LAYERS"
        cancel_limits = False

    # 1b. Cancel Pending Limit/Stop Orders (preserving bare "delete limit" -> ACTION_FULL_CLOSE)
    cancel_direction: Optional[str] = None
    if not action and _is_cancel_pending:
        action = "ACTION_CANCEL_PENDING"
        cancel_limits = True
        _has_buy_kw = bool(re.search(r"\bbuy\b", lower))
        _has_sell_kw = bool(re.search(r"\bsell\b", lower))
        if _has_buy_kw and not _has_sell_kw:
            cancel_direction = "BUY"
        elif _has_sell_kw and not _has_buy_kw:
            cancel_direction = "SELL"

    # 2. Full Close / Invalidate / Cancel
    if not action and _hit(FULL_CLOSE_PATTERNS):
        action = "ACTION_FULL_CLOSE"
        cancel_limits = True

    # 3. Provider TP1..TP3 hit alerts (replies, edits, or standalone <= 8 words without dir/4-digit price)
    is_tp1_eligible = (
        effective_is_reply
        or is_edit
        or (word_count <= 8 and not has_direction and not has_4digit_price)
    )
    if not action and is_tp1_eligible:
        hit_stage = detect_tp_milestone_hit(lower)
        if hit_stage == 1 and not unambig_be:
            action = "ACTION_TP1_HIT_REPLY"
            cancel_limits = True
            tp_hit_stage = 1
        elif hit_stage == 2:
            action = "ACTION_TP2_HIT"
            cancel_limits = False
            tp_hit_stage = 2
        elif hit_stage == 3:
            action = "ACTION_TP3_HIT"
            cancel_limits = False
            tp_hit_stage = 3

    # 4. Breakeven
    if not action and not _BE_NON_COMMAND_RE.search(lower) and _hit(BREAKEVEN_PATTERNS):
        action = "ACTION_BREAKEVEN"
        cancel_limits = True

    # 5. Reactivate ("Active") - only on replies or standalone command to avoid chat noise
    if not action:
        is_standalone_reactivate = bool(
            re.fullmatch(r"(?:re-?activate|active(?:\s+now)?|valid\s+again)[!.\s]*", lower.strip())
        )
        if effective_is_reply or is_standalone_reactivate:
            if _hit(REACTIVATE_PATTERNS):
                action = "ACTION_REACTIVATE"

    # 6. Partial Close
    if not action and _hit(PARTIAL_CLOSE_PATTERNS):
        action = "ACTION_PARTIAL_CLOSE"

    # 7. Active-Trade Trail SL / Lock TP1..TP4 & 8. Adjust SL / TP1..TP5 (no directional verb)
    if not action and not has_direction and (effective_is_reply or is_edit or word_count <= 20):
        # 7a. Trail SL / Lock TP1..TP4 ("Trail SL", "Trail 15", "Trail TP2", "Lock TP2", "Trail SL to 3895")
        if _TRAIL_SL_INTENT_RE.search(lower):
            ms_match = re.search(r"\b(?:tp|target|take\s*profit)\s*#?\s*([1-4])\b", lower)
            trail_milestone = f"TP{ms_match.group(1)}" if ms_match else None

            raw_labeled, raw_unlabeled = extract_labeled_tps(text)
            labeled_tps = {k: v for k, v in raw_labeled.items() if v >= 500.0}
            unlabeled_tps = [v for v in raw_unlabeled if v >= 500.0]
            tp_prices_set = set(labeled_tps.values()) | set(unlabeled_tps)

            trail_price: Optional[float] = None
            for pm in re.finditer(
                r"\b(?:to|@|=|:|sl|stop\s*loss|stoploss|trail)\s*(?:[:=@\-]|->|\b(?:to|at|is)\b)?\s*(\d{3,6}(?:\.\d+)?)\b(?!\s*pips?\b)",
                lower,
            ):
                cand_p = round(float(pm.group(1)), 2)
                if 500.0 <= cand_p < 100000.0 and cand_p not in tp_prices_set:
                    trail_price = cand_p
                    break

            trail_pips: Optional[float] = None
            pips_m = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*pips?\b", lower)
            if pips_m:
                pv = float(pips_m.group(1))
                if 1.0 <= pv <= 500.0:
                    trail_pips = pv
            elif trail_milestone is None and trail_price is None:
                bare_m = re.search(
                    r"\btrail(?:\s+sl|\s+stop(?:\s*loss)?)?(?:\s+by)?\s*[:=@\-]?\s*(\d{1,3}(?:\.\d+)?)\b",
                    lower,
                )
                if bare_m:
                    pv = float(bare_m.group(1))
                    if 1.0 <= pv < 300.0:
                        trail_pips = pv

            mgmt_obj = SignalAction({
                "event_type": "TRADE_MANAGEMENT",
                "management_action": "ACTION_TRAIL_SL",
                "message_id": message_id,
                "reply_to_msg_id": reply_to_msg_id,
                "cancel_pending_limits": False,
                "symbol": DEFAULT_SYMBOL,
                "trail_milestone": trail_milestone,
                "trail_price": trail_price,
                "trail_pips": trail_pips,
                "sl": trail_price,
                "sl_valid": bool(trail_price is not None and trail_price >= 500.0),
                "labeled_tps": labeled_tps,
                "unlabeled_tps": unlabeled_tps,
                "raw_text": text,
            })
            logger.info(
                f"Gate 3: Classified trade management command: ACTION_TRAIL_SL "
                f"(milestone={trail_milestone}, price={trail_price}, pips={trail_pips}, ReplyTo={reply_to_msg_id})"
            )
            return mgmt_obj

        # 7b. Adjust SL / TP1..TP5 ("SL 3885", "Move SL to 3885", "TP2 3918 TP3 3930", "Adjust TP2 3918 TP3 3928")
        if not effective_is_reply and _infer_geometric_signal(text, is_reply=False) is not None:
            return None
        raw_adj_sl = _extract_sl(text)
        adj_sl = round(raw_adj_sl, 2) if (raw_adj_sl is not None and raw_adj_sl >= 500.0) else None
        raw_labeled, raw_unlabeled = extract_labeled_tps(text)
        labeled_tps = {k: v for k, v in raw_labeled.items() if v >= 500.0}
        unlabeled_tps = [v for v in raw_unlabeled if v >= 500.0]

        if adj_sl is not None or labeled_tps or unlabeled_tps:
            explicit_tps = [v for v in extract_tps(text) if v >= 500.0]
            mgmt_obj = SignalAction({
                "event_type": "TRADE_MANAGEMENT",
                "management_action": "ACTION_ADJUST_TARGETS",
                "message_id": message_id,
                "reply_to_msg_id": reply_to_msg_id,
                "cancel_pending_limits": False,
                "symbol": DEFAULT_SYMBOL,
                "sl": adj_sl,
                "sl_valid": bool(adj_sl is not None and adj_sl >= 500.0),
                "labeled_tps": labeled_tps,
                "unlabeled_tps": unlabeled_tps,
                "explicit_tps": explicit_tps,
                "raw_text": text,
            })
            logger.info(
                f"Gate 3: Classified trade management command: ACTION_ADJUST_TARGETS "
                f"(SL={adj_sl}, labeled_tps={labeled_tps}, unlabeled_tps={unlabeled_tps}, ReplyTo={reply_to_msg_id})"
            )
            return mgmt_obj

    if action:
        payload: Dict[str, Any] = {
            "event_type": "TRADE_MANAGEMENT",
            "management_action": action,
            "message_id": message_id,
            "reply_to_msg_id": reply_to_msg_id,
            "cancel_pending_limits": cancel_limits,
            "symbol": DEFAULT_SYMBOL,
            "raw_text": text,
        }
        if tp_hit_stage is not None:
            payload["tp_hit_stage"] = tp_hit_stage
        if action == "ACTION_CANCEL_PENDING":
            payload["direction"] = cancel_direction
            payload["cancel_direction"] = cancel_direction
        mgmt_obj = SignalAction(payload)
        logger.info(f"Gate 3: Classified trade management command: {action} (ReplyTo: {reply_to_msg_id})")
        return mgmt_obj

    return None


def _has_fp_context_anchor(text: str) -> bool:
    """Allow line-start 'FP' TP typo only when text already has a valid TP or Gold SL anchor."""
    if not text:
        return False
    if TP_PATTERN.search(text):
        return True
    for m in _EXPLICIT_SL_PATTERN.finditer(text):
        try:
            if 500.0 <= float(m.group(1)) < 100000.0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _iter_tp_matches_in_order(text: str) -> List[re.Match]:
    """Return TP_PATTERN matches (plus context-gated _FP_TYPO_LINE_PATTERN matches) in document order."""
    matches = list(TP_PATTERN.finditer(text))
    if _has_fp_context_anchor(text):
        matches.extend(_FP_TYPO_LINE_PATTERN.finditer(text))
        matches.sort(key=lambda m: m.start())
    return matches


def extract_labeled_tps(text: str) -> Tuple[Dict[str, float], List[float]]:
    """
    Extract explicitly labeled TPs ({'TP1': 3905.0, 'TP2': 3915.0, ...}) and
    unlabeled TPs ([3905.0, ...]) from text without shifting stage indices.
    """
    labeled: Dict[str, float] = {}
    unlabeled: List[float] = []
    if not text:
        return labeled, unlabeled

    for m in _iter_tp_matches_in_order(text):
        idx_str = m.group(1) or m.group(2)
        price = round(float(m.group(3)), 2)
        if idx_str:
            idx = int(idx_str)
            if 1 <= idx <= 5:
                key = f"TP{idx}"
                if key not in labeled:
                    labeled[key] = price
        else:
            if price not in unlabeled:
                unlabeled.append(price)

    return labeled, unlabeled


def extract_tps(text: str, direction: str = "BUY") -> List[float]:
    """
    Extract Take Profit prices from text.
    Sorts labeled TPs (TP1, TP2, ...) and seamlessly integrates unlabeled TPs
    (e.g. 'TP 3900' followed by 'TP2 3920') so TP1 is guaranteed first.
    """
    labeled: Dict[int, float] = {}
    unlabeled: List[float] = []

    for m in _iter_tp_matches_in_order(text):
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


def _strip_entry_text(text: str) -> str:
    """Extract only entry-relevant segments before TP/SL clauses for zone/price parsing."""
    clean_lines = []
    seen_targets = False
    allow_fp = _has_fp_context_anchor(text)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(
            r"^(?:tp\d*|sl|stop\s*loss|stoploss|stop|take\s*profit|target\d*)\b",
            stripped,
            re.IGNORECASE,
        ) or (allow_fp and _FP_TYPO_LINE_PATTERN.match(stripped)):
            seen_targets = True
            continue
        has_inline_target = bool(
            re.search(
                r"\b(?:tp\d*|take\s*profit\d*|target\d*|sl|stop\s*loss|stoploss)\b",
                stripped,
                re.IGNORECASE,
            )
        )
        entry_part = re.split(
            r"\b(?:tp\d*|take\s*profit\d*|target\d*|sl|stop\s*loss|stoploss)\b",
            stripped,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        if entry_part:
            if seen_targets and not DIRECTION_PATTERN.search(entry_part):
                continue
            clean_lines.append(entry_part)
        if has_inline_target:
            seen_targets = True
    return " \n ".join(clean_lines)


_NON_GOLD_GEO_TICKER_RE = re.compile(
    r"\b(?:ETHUSD|BTCUSD|SOLUSD|XRPUSD|LTCUSD|BNBUSD|ADAUSD|DOGEUSD|"
    r"SPX500|SP500|US500|NAS100|USTEC|NDX|US30|DJ30|UK100|FTSE|GER40|DE40|DAX|"
    r"XAGUSD|XPTUSD|XPDUSD|WTI|BRENT|USOIL|UKOIL|"
    r"EURUSD|GBPUSD|USDJPY|USDCHF|USDCAD|AUDUSD|NZDUSD|EURJPY|GBPJPY|EURGBP)\b",
    re.IGNORECASE,
)

_GEO_HEADER_ALLOWLIST = {
    "xau", "xauusd", "gold", "limit", "limits", "stop", "stops",
    "entry", "entries", "zone", "area", "price", "to", "at", "around",
    "near", "now", "signal", "setup", "order", "orders", "high", "mid",
    "low", "risk", "scalp", "swing", "vip",
}


def _try_repair_zone_typo(
    raw_p1: str,
    raw_p2: str,
    raw_sl: Optional[float],
    raw_tp1: Optional[float],
    direction: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    """
    Repair a single-digit thousands typo in an entry zone (e.g. '3750-4730' -> 3750.0-3730.0)
    strictly anchored by BOTH explicit SL and explicit TP1 geometry.
    Returns (top_price, bottom_price, note) iff exactly 1 valid candidate exists.
    """
    if raw_sl is None or raw_tp1 is None:
        return None
    if not (1000.0 <= float(raw_sl) < 10000.0 and 1000.0 <= float(raw_tp1) < 10000.0):
        return None
    if abs(float(raw_sl) - float(raw_tp1)) < 1e-6:
        return None

    eff_dir = (direction or "").strip().upper()
    if eff_dir not in ("BUY", "SELL"):
        eff_dir = "BUY" if float(raw_sl) < float(raw_tp1) else "SELL"

    int1 = raw_p1.split(".", 1)[0]
    int2 = raw_p2.split(".", 1)[0]
    if len(int1) != 4 or len(int2) != 4 or int1[0] == int2[0]:
        return None

    try:
        p1 = float(raw_p1)
        p2 = float(raw_p2)
        cand_a = (p1, float(int1[0] + raw_p2[1:]))
        cand_b = (float(int2[0] + raw_p1[1:]), p2)
    except (TypeError, ValueError):
        return None

    max_sl_dist = float(MAX_SL_PIPS) * 0.10
    valid: List[Tuple[float, float, str]] = []
    for a, b in (cand_a, cand_b):
        if not (1000.0 <= a < 10000.0 and 1000.0 <= b < 10000.0):
            continue
        top_p = round(max(a, b), 2)
        bot_p = round(min(a, b), 2)
        span = top_p - bot_p
        if span <= 0.0 or span > 50.0:
            continue
        sl_f = float(raw_sl)
        tp1_f = float(raw_tp1)
        if eff_dir == "BUY":
            if sl_f < bot_p <= top_p < tp1_f and (bot_p - sl_f) <= max_sl_dist and (tp1_f - top_p) <= 250.0:
                valid.append((top_p, bot_p, f"{raw_p1}-{raw_p2} -> {top_p}-{bot_p}"))
        else:
            if tp1_f < bot_p <= top_p < sl_f and (sl_f - top_p) <= max_sl_dist and (bot_p - tp1_f) <= 250.0:
                valid.append((top_p, bot_p, f"{raw_p1}-{raw_p2} -> {top_p}-{bot_p}"))

    if len(valid) == 1:
        return valid[0]
    return None


def _extract_zone_and_single_entry(
    zone_search_text: str,
    direction: str,
    raw_sl: Optional[float] = None,
    explicit_tps: Optional[Sequence[float]] = None,
) -> Tuple[Optional[Dict[str, float]], Optional[Dict[str, float]], Optional[float], bool, List[float], Optional[str]]:
    """
    Shared extractor for entry zones, single-digit typo-repaired zones, multi-line levels,
    and single-price entries. Returns (entry_tiers, zone_tiers, single_price, is_zone, distinct_levels, repair_note).
    """
    entry_tiers = None
    zone_tiers = None
    single_price = None
    is_zone = False
    repair_note: Optional[str] = None
    raw_tp1 = float(explicit_tps[0]) if explicit_tps else None

    failed_pairs: List[Tuple[str, str, float, float]] = []
    for zm in ZONE_PATTERN.finditer(zone_search_text):
        r1, r2 = zm.group(1), zm.group(2)
        p1, p2 = float(r1), float(r2)
        if 500.0 <= p1 < 100000.0 and 500.0 <= p2 < 100000.0 and abs(p1 - p2) <= 250.0:
            top_price = round(max(p1, p2), 2)
            bottom_price = round(min(p1, p2), 2)
            mid_price = round((top_price + bottom_price) / 2.0, 2)
            entry_tiers = {"top": top_price, "mid": mid_price, "bottom": bottom_price}
            zone_tiers = compute_zone_4tiers(top_price, bottom_price, direction)
            is_zone = True
            break
        failed_pairs.append((r1, r2, p1, p2))

    if not is_zone and failed_pairs:
        for r1, r2, p1, p2 in failed_pairs:
            if abs(p1 - p2) > 250.0:
                repaired = _try_repair_zone_typo(r1, r2, raw_sl, raw_tp1, direction=direction)
                if repaired is not None:
                    top_price, bottom_price, repair_note = repaired
                    mid_price = round((top_price + bottom_price) / 2.0, 2)
                    entry_tiers = {"top": top_price, "mid": mid_price, "bottom": bottom_price}
                    zone_tiers = compute_zone_4tiers(top_price, bottom_price, direction)
                    is_zone = True
                    logger.warning(
                        f"[ZONE TYPO REPAIR] {repair_note} ({direction}, SL={raw_sl}, TP1={raw_tp1})"
                    )
                    break

    distinct_levels: List[float] = []
    if not is_zone:
        for num_m in re.finditer(
            r"(?<![.\d])(\d{3,6}(?:\.\d+)?)(?![.\d])(?!\s*(?:pips?|%|lots?|x)\b)",
            zone_search_text,
            re.IGNORECASE,
        ):
            val = round(float(num_m.group(1)), 2)
            if 500.0 <= val < 100000.0 and val not in distinct_levels:
                distinct_levels.append(val)
        if len(distinct_levels) >= 2 and (max(distinct_levels) - min(distinct_levels)) <= 100.0:
            top_price = max(distinct_levels)
            bottom_price = min(distinct_levels)
            mid_price = round((top_price + bottom_price) / 2.0, 2)
            entry_tiers = {"top": top_price, "mid": mid_price, "bottom": bottom_price}
            zone_tiers = compute_zone_4tiers(top_price, bottom_price, direction)
            is_zone = True

    if not is_zone:
        single_match = SINGLE_PRICE_PATTERN.search(zone_search_text)
        if single_match:
            price = float(single_match.group(1))
            if 500.0 <= price < 100000.0:
                single_price = round(price, 2)
                is_zone = False
        if single_price is None and len(distinct_levels) == 1 and (raw_sl is not None or raw_tp1 is not None):
            single_price = distinct_levels[0]
            is_zone = False

    return entry_tiers, zone_tiers, single_price, is_zone, distinct_levels, repair_note


def _infer_geometric_signal(text: str, is_reply: bool = False) -> Optional[str]:
    """
    When explicit BUY/SELL keywords are omitted (e.g. '3750-3730\\nSL 3725\\nTP 3753...'),
    safely infer direction strictly from unambiguous Gold SL/Entry/TP geometry.
    Returns 'BUY', 'SELL', or None.
    """
    if is_reply or "?" in text or DIRECTION_PATTERN.search(text):
        return None
    if _NON_GOLD_GEO_TICKER_RE.search(text):
        return None
    if _PROSE_CHATTER_PATTERN.search(text):
        return None
    lower = text.lower()
    for pat in VICTORY_RECAP_BLOCKERS:
        if re.search(pat, lower):
            return None

    zone_search_text = _strip_entry_text(text).strip()
    if not zone_search_text:
        return None

    # Clean header allowlist: strip emojis, connectors, and punctuation before checking tokens
    hdr_norm = re.sub(r"\bxau\s*/?\s*usd(?:\.[a-z0-9]+)?\b", " xauusd ", zone_search_text, flags=re.IGNORECASE)
    hdr_norm = re.sub(r"[^\x00-\x7F]+", " ", hdr_norm)
    hdr_norm = re.sub(r"[-–—/:@,|•()\[\]*_~`!]+", " ", hdr_norm)
    hdr_tokens = [tok.lower() for tok in hdr_norm.split() if tok]
    if not hdr_tokens or len(hdr_tokens) > 6:
        return None
    for tok in hdr_tokens:
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            continue
        if tok not in _GEO_HEADER_ALLOWLIST:
            return None

    raw_sl = _extract_sl(text)
    explicit_tps = [v for v in extract_tps(text) if 500.0 <= v < 100000.0]
    if raw_sl is None or not explicit_tps:
        return None
    raw_tp1 = float(explicit_tps[0])
    sl_f = float(raw_sl)
    if not (1000.0 <= sl_f < 10000.0 and 1000.0 <= raw_tp1 < 10000.0) or abs(sl_f - raw_tp1) < 1e-6:
        return None

    inferred_dir = "BUY" if sl_f < raw_tp1 else "SELL"
    entry_tiers, _, single_price, is_zone, distinct_levels, _ = _extract_zone_and_single_entry(
        zone_search_text, inferred_dir, raw_sl=sl_f, explicit_tps=explicit_tps
    )

    if is_zone and entry_tiers:
        entry_top = float(entry_tiers["top"])
        entry_bot = float(entry_tiers["bottom"])
        if (entry_top - entry_bot) > 100.0:
            return None
    elif single_price is not None:
        entry_top = float(single_price)
        entry_bot = float(single_price)
    elif len(distinct_levels) == 1:
        entry_top = float(distinct_levels[0])
        entry_bot = float(distinct_levels[0])
    else:
        return None

    if not (1000.0 <= entry_bot <= entry_top < 10000.0):
        return None

    max_sl_dist = float(MAX_SL_PIPS) * 0.10
    if inferred_dir == "BUY":
        if (
            sl_f < entry_bot
            and entry_top < raw_tp1
            and (entry_bot - sl_f) <= max_sl_dist
            and (raw_tp1 - entry_top) <= 250.0
            and all(float(tp) > entry_top for tp in explicit_tps)
        ):
            return "BUY"
    else:
        if (
            raw_tp1 < entry_bot
            and entry_top < sl_f
            and (sl_f - entry_top) <= max_sl_dist
            and (entry_bot - raw_tp1) <= 250.0
            and all(float(tp) < entry_bot for tp in explicit_tps)
        ):
            return "SELL"

    return None


def gate_4_extract_signal(
    text: str,
    message_id: int = 0,
    is_edit: bool = False,
    is_reply: bool = False,
) -> Optional[SignalAction]:
    """
    Gate 4: New Signal Extraction (v3.0 4-Layer + TP1-TP5 + STOP/LIMIT).
    Defaults to DEFAULT_SYMBOL (XAUUSD) for any valid directional trigger verb
    or unambiguous geometric Gold entry/SL/TP layout.
    """
    # 1. Direction Verb Detection (or Unambiguous Geometric Direction Inference)
    dir_match = DIRECTION_PATTERN.search(text)
    inferred_direction = None
    if dir_match:
        matched_verb = dir_match.group(1).upper()
        direction = "BUY" if any(kw in matched_verb for kw in ("BUY", "LONG")) else "SELL"
    else:
        inferred_direction = _infer_geometric_signal(text, is_reply=is_reply)
        if not inferred_direction:
            logger.info(f"[PARSER IGNORED] No valid directional verb detected | text={text!r}")
            return None
        direction = inferred_direction
        logger.info(f"[PARSER GEOMETRIC] Inferred direction={direction} from unambiguous Gold entry/SL/TP geometry")

    # 2. Symbol resolution - defaults straight to XAUUSD
    symbol = DEFAULT_SYMBOL

    # 3. Entry Zone, Price & Order Intent Detection
    zone_search_text = _strip_entry_text(text)

    # Guard: Reject conflicting directions (e.g. "lmao + 410 pips sell straight into a TP1 buy")
    has_buy_full = bool(re.search(r"\bbuy\b", text, re.IGNORECASE))
    has_sell_full = bool(re.search(r"\bsell\b", text, re.IGNORECASE))
    has_buy_entry = bool(re.search(r"\b(?:buy|long)\b", zone_search_text, re.IGNORECASE))
    has_sell_entry = bool(re.search(r"\b(?:sell|short)\b", zone_search_text, re.IGNORECASE))
    if (has_buy_full and has_sell_full) or (has_buy_entry and has_sell_entry):
        logger.info(f"[PARSER IGNORED] Conflicting buy/sell directions detected | text={text!r}")
        return None

    # 4. Pre-extract TP & SL so stub vs complete-signal validation knows whether targets exist
    labeled_tps, unlabeled_tps = extract_labeled_tps(text)
    explicit_tps = [v for v in extract_tps(text, direction=direction) if 500.0 <= v < 100000.0]

    # Monotonic TP typo sanitization
    all_monotonic_dropped: List[float] = []

    if explicit_tps:
        sanitized_explicit, dropped_exp = sanitize_monotonic_tps(explicit_tps, direction=direction)
        if dropped_exp:
            logger.warning(
                f"Gate 4: Dropped non-monotonic explicit TP typos on {direction}: {dropped_exp} from {explicit_tps}"
            )
            explicit_tps = sanitized_explicit
            all_monotonic_dropped.extend(dropped_exp)

    if unlabeled_tps:
        sanitized_unlabeled, dropped_unl = sanitize_monotonic_tps(unlabeled_tps, direction=direction)
        if dropped_unl:
            logger.warning(
                f"Gate 4: Dropped non-monotonic unlabeled TP typos on {direction}: {dropped_unl} from {unlabeled_tps}"
            )
            unlabeled_tps = sanitized_unlabeled
            for d in dropped_unl:
                if d not in all_monotonic_dropped:
                    all_monotonic_dropped.append(d)

    if labeled_tps:
        sanitized_labeled, dropped_lab = _sanitize_labeled_tps_monotonic(labeled_tps, direction=direction)
        if dropped_lab:
            logger.warning(
                f"Gate 4: Dropped non-monotonic labeled TP typos on {direction}: {dropped_lab} from {labeled_tps}"
            )
            labeled_tps = sanitized_labeled
            for d in dropped_lab:
                if d not in all_monotonic_dropped:
                    all_monotonic_dropped.append(d)

    raw_sl = _extract_sl(text)
    has_valid_targets = bool(explicit_tps) or (raw_sl is not None and 500.0 <= raw_sl < 100000.0)

    # Guard: Reject conversational / past-tense chatter when no complete SL+TP target set is present
    if not (bool(explicit_tps) and raw_sl is not None and 500.0 <= raw_sl < 100000.0):
        if _PROSE_CHATTER_PATTERN.search(text):
            logger.info(f"[PARSER IGNORED] Conversational chatter phrase detected | text={text!r}")
            return None

    is_stop_order = bool(_STOP_ORDER_PATTERN.search(zone_search_text))
    is_limit_order = False if is_stop_order else bool(_LIMIT_ORDER_PATTERN.search(zone_search_text))

    is_instant_now = False
    max_orders = MAX_ORDERS_PER_SIGNAL
    entry_tiers, zone_tiers, single_price, is_zone, _, repair_note = _extract_zone_and_single_entry(
        zone_search_text, direction, raw_sl=raw_sl, explicit_tps=explicit_tps
    )

    if not is_zone and single_price is None:
        if is_limit_order or is_stop_order:
            # Explicit "buy limit" / "sell stop" etc. WITHOUT any valid Gold price is incomplete/chatter -> never execute as Market Now!
            logger.info(
                f"[PARSER IGNORED] LIMIT/STOP keyword detected without an entry price or zone (>=500) | text={text!r}"
            )
            return None
        if _MARKET_NOW_PATTERN.search(zone_search_text):
            # Explicit market entry ("Buy now", "Sell xauusd now", "Buy at market")
            is_instant_now = True
        else:
            logger.info(
                f"[PARSER IGNORED] Bare directional word without valid Gold price/zone (>=500) or explicit 'now' keyword | text={text!r}"
            )
            return None

    # Guard: Unstructured prose / question without SL and without TP
    if not has_valid_targets:
        if "?" in zone_search_text or (not is_zone and len(zone_search_text.split()) > 8):
            logger.info(
                f"[PARSER IGNORED] Unstructured message without SL/TP exceeds stub length or is a question | text={text!r}"
            )
            return None
        if is_reply and not is_zone and single_price is None and not _MARKET_NOW_PATTERN.search(zone_search_text):
            logger.info(
                f"[PARSER IGNORED] Unstructured reply without valid Gold price/zone or explicit 'now' | text={text!r}"
            )
            return None

    if is_stop_order:
        order_intent = "STOP"
    elif is_limit_order:
        order_intent = "LIMIT"
    elif is_instant_now:
        order_intent = "MARKET"
    else:
        order_intent = "AUTO"

    # 5. Take Profit (TP1..TP5) & Proximity Trigger (Gold: 1 pip = $0.10)
    tp_levels = derive_tp_levels(explicit_tps, direction=direction)
    raw_tp1 = tp_levels.get("TP1")
    raw_tp2 = tp_levels.get("TP2")
    raw_tp3 = tp_levels.get("TP3")
    raw_tp4 = tp_levels.get("TP4")
    raw_tp5 = tp_levels.get("TP5")
    final_tp = None
    proximity_close_price = None

    if raw_tp1 is not None:
        pip_distance_20 = TP1_CLOSE_PROXIMITY_PIPS * 0.10  # 2.00 points on XAUUSD
        if direction == "BUY":
            final_tp = round(raw_tp1 - TP_FRONT_RUN_OFFSET, 2)
            proximity_close_price = round(raw_tp1 - pip_distance_20, 2)
        else:
            final_tp = round(raw_tp1 + TP_FRONT_RUN_OFFSET, 2)
            proximity_close_price = round(raw_tp1 + pip_distance_20, 2)

    action_payload: Dict[str, Any] = {
        "event_type": "NEW_SIGNAL",
        "message_id": message_id,
        "is_edit": is_edit,
        "symbol": symbol,
        "direction": direction,
        "is_zone": is_zone,
        "is_instant_now": is_instant_now,
        "is_limit_order": is_limit_order,
        "is_stop_order": is_stop_order,
        "order_intent": order_intent,
        "single_price": single_price,
        "layer_lots": dict(LAYER_LOTS),
        "max_orders": max_orders,
        "entry_tiers": entry_tiers,
        "zone_tiers": zone_tiers,
        "sl": raw_sl,
        "sl_valid": False,
        "explicit_tps": explicit_tps,
        "labeled_tps": labeled_tps,
        "unlabeled_tps": unlabeled_tps,
        "tp_levels": tp_levels,
        "raw_tp1": raw_tp1,
        "raw_tp2": raw_tp2,
        "raw_tp3": raw_tp3,
        "raw_tp4": raw_tp4,
        "raw_tp5": raw_tp5,
        "final_tp": final_tp,
        "tp_stage": "TP1",
        "proximity_close_price": proximity_close_price,
        "direction_inferred": bool(inferred_direction),
        "zone_typo_repaired": bool(repair_note),
        "raw_text": text,
    }
    if all_monotonic_dropped:
        action_payload["monotonic_dropped_tps"] = all_monotonic_dropped

    return SignalAction(action_payload)


def _is_valid_tp_vs_ref(
    val: Any,
    direction: str,
    tp_ref: Optional[float],
    max_dist: float,
) -> bool:
    """Validate a single TP candidate against Gold range, direction, and max distance."""
    try:
        fv = float(val)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(fv):
        return False
    if fv < 500.0 or fv >= 100000.0:
        return False
    if tp_ref is None:
        return fv >= 1000.0
    if (direction or "BUY").upper() == "BUY":
        return fv > tp_ref and (fv - tp_ref) <= max_dist
    return fv < tp_ref and (tp_ref - fv) <= max_dist


def gate_5_validate_price_sanity(
    action: SignalAction,
    live_price: Optional[float] = None,
    max_sl_pips: float = MAX_SL_PIPS,
) -> SignalAction:
    """
    Gate 5: Price & Typo Sanity Validation.
    Prevents missing-digit typos (e.g. SL 387 vs 3875) and wrong-side/out-of-range TPs
    from wiping accounts or triggering 10016 Invalid stops errors.
    """
    direction = (action.get("direction") or "BUY").upper()
    raw_sl = action.get("sl")
    tiers = action.get("entry_tiers")
    single_p = action.get("single_price")

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

    # Validate & sanitize ALL TP fields before checking SL (runs even when raw_sl is None)
    max_tp_dist = float(MAX_TP_DISTANCE_DOLLARS)
    raw_labeled_tps = dict(action.get("labeled_tps") or {})
    raw_unlabeled_tps = list(action.get("unlabeled_tps") or [])
    raw_explicit_tps = list(action.get("explicit_tps") or [])
    raw_tp_levels = dict(action.get("tp_levels") or {})

    valid_labeled: Dict[str, float] = {}
    any_tp_stripped = False
    for k, v in raw_labeled_tps.items():
        if _is_valid_tp_vs_ref(v, direction, tp_ref, max_tp_dist):
            valid_labeled[k] = round(float(v), 2)
        else:
            any_tp_stripped = True
            logger.warning(
                f"Gate 5 Sanity Check: Stripped invalid {direction} {k}={v} "
                f"(tp_ref={tp_ref}, max_dist={max_tp_dist})."
            )

    valid_unlabeled: List[float] = []
    for v in raw_unlabeled_tps:
        if _is_valid_tp_vs_ref(v, direction, tp_ref, max_tp_dist):
            valid_unlabeled.append(round(float(v), 2))
        else:
            any_tp_stripped = True
            logger.warning(
                f"Gate 5 Sanity Check: Stripped invalid {direction} unlabeled TP={v} "
                f"(tp_ref={tp_ref}, max_dist={max_tp_dist})."
            )

    valid_explicit: List[float] = []
    for v in raw_explicit_tps:
        if _is_valid_tp_vs_ref(v, direction, tp_ref, max_tp_dist):
            valid_explicit.append(round(float(v), 2))
        else:
            any_tp_stripped = True
            logger.warning(
                f"Gate 5 Sanity Check: Stripped invalid {direction} explicit TP={v} "
                f"(tp_ref={tp_ref}, max_dist={max_tp_dist})."
            )

    if not raw_explicit_tps and (raw_tp_levels or any(action.get(f"raw_tp{i}") is not None for i in range(1, 6))):
        fallback_vals: List[Any] = [raw_tp_levels.get(f"TP{i}") for i in range(1, 6)]
        if not any(v is not None for v in fallback_vals):
            fallback_vals = [action.get(f"raw_tp{i}") for i in range(1, 6)]
        for v in fallback_vals:
            if v is None:
                continue
            if _is_valid_tp_vs_ref(v, direction, tp_ref, max_tp_dist):
                valid_explicit.append(round(float(v), 2))
            else:
                any_tp_stripped = True
                logger.warning(
                    f"Gate 5 Sanity Check: Stripped invalid {direction} fallback TP={v} "
                    f"(tp_ref={tp_ref}, max_dist={max_tp_dist})."
                )

    mono_dropped_g5: List[float] = []
    if valid_explicit:
        san_exp5, d_exp5 = sanitize_monotonic_tps(valid_explicit, direction=direction)
        if d_exp5:
            valid_explicit = san_exp5
            any_tp_stripped = True
            mono_dropped_g5.extend(d_exp5)
            logger.warning(f"Gate 5 Sanity Check: Stripped non-monotonic explicit TPs on {direction}: {d_exp5}")

    if valid_unlabeled:
        san_unl5, d_unl5 = sanitize_monotonic_tps(valid_unlabeled, direction=direction)
        if d_unl5:
            valid_unlabeled = san_unl5
            any_tp_stripped = True
            mono_dropped_g5.extend(d_unl5)
            logger.warning(f"Gate 5 Sanity Check: Stripped non-monotonic unlabeled TPs on {direction}: {d_unl5}")

    if valid_labeled:
        san_lab5, d_lab5 = _sanitize_labeled_tps_monotonic(valid_labeled, direction=direction)
        if d_lab5:
            valid_labeled = san_lab5
            any_tp_stripped = True
            mono_dropped_g5.extend(d_lab5)
            logger.warning(f"Gate 5 Sanity Check: Stripped non-monotonic labeled TPs on {direction}: {d_lab5}")

    if mono_dropped_g5:
        existing_mono = list(action.get("monotonic_dropped_tps") or [])
        for d in mono_dropped_g5:
            if d not in existing_mono:
                existing_mono.append(d)
        action["monotonic_dropped_tps"] = existing_mono

    if any_tp_stripped:
        action["invalid_explicit_tps"] = list(raw_explicit_tps or raw_unlabeled_tps or raw_labeled_tps.values())
        action["labeled_tps"] = valid_labeled
        action["unlabeled_tps"] = valid_unlabeled
        action["explicit_tps"] = valid_explicit
        rebuilt_tp_levels = derive_tp_levels(valid_explicit, direction=direction) if valid_explicit else {}
        action["tp_levels"] = rebuilt_tp_levels
        for i in range(1, 6):
            action[f"raw_tp{i}"] = rebuilt_tp_levels.get(f"TP{i}")
        rebuilt_tp1 = action.get("raw_tp1")
        if rebuilt_tp1 is not None:
            pip_distance_20 = TP1_CLOSE_PROXIMITY_PIPS * 0.10
            if direction == "BUY":
                action["final_tp"] = round(rebuilt_tp1 - TP_FRONT_RUN_OFFSET, 2)
                action["proximity_close_price"] = round(rebuilt_tp1 - pip_distance_20, 2)
            else:
                action["final_tp"] = round(rebuilt_tp1 + TP_FRONT_RUN_OFFSET, 2)
                action["proximity_close_price"] = round(rebuilt_tp1 + pip_distance_20, 2)
        else:
            action["final_tp"] = None
            action["proximity_close_price"] = None

    final_tp = action.get("final_tp")
    if final_tp is not None and not _is_valid_tp_vs_ref(final_tp, direction, tp_ref, max_tp_dist):
        logger.warning(
            f"Gate 5 Sanity Check: Rejected final_tp={final_tp} on {direction} "
            f"(tp_ref={tp_ref}, max_dist={max_tp_dist})."
        )
        action["final_tp"] = None
        action["proximity_close_price"] = None
        any_tp_stripped = True

    action["tp_sanity_rejected"] = bool(any_tp_stripped)
    action["tp_valid"] = not (bool(any_tp_stripped) and not bool(action.get("tp_levels")))
    final_tp = action.get("final_tp")
    raw_tp1 = action.get("raw_tp1")

    if raw_sl is None:
        action["sl_valid"] = False
        return action

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

def finalize_signal_sanity(
    signal_action: SignalAction,
    live_price: Optional[float] = None,
    log_output: bool = True,
) -> SignalAction:
    """Run Gate 5 price sanity validation on an extracted NEW_SIGNAL and emit Pipeline Output log once."""
    validated_action = gate_5_validate_price_sanity(
        signal_action,
        live_price=live_price,
    )
    if log_output:
        logger.info(
            f"Pipeline Output: {validated_action['event_type']} | {validated_action['direction']} "
            f"{validated_action['symbol']} ({validated_action['order_intent']}) | "
            f"Tiers={validated_action['zone_tiers'] or validated_action['entry_tiers']} | "
            f"SL={validated_action['sl']} (valid={validated_action['sl_valid']}) | "
            f"TPs={validated_action['tp_levels']} | FinalTP={validated_action['final_tp']} | "
            f"ProximityClose={validated_action['proximity_close_price']}"
        )
    return validated_action


def parse_message(
    text: Optional[str],
    message_id: int = 0,
    is_edit: bool = False,
    is_reply: bool = False,
    reply_to_msg_id: Optional[int] = None,
    is_forward: bool = False,
    live_price: Optional[float] = None,
    validate_sanity: bool = True,
    log_output: bool = True,
) -> Optional[SignalAction]:
    """
    Main 6-Gate Sequential Pipeline conforming to PARSER_SPEC.md and ARCHITECTURE_v3.md.
    Gate 1: Forward & Empty Message Check
    Gate 2: Announcement, Recap & Ignored Symbol Filter
    Gate 3: Trade Management & Reply Classifier (BE, Close, Reactivate, TP1 Hit)
    Gate 4: New Signal Extraction (Defaults to XAUUSD, 4-Layer & TP1-TP5)
    Gate 5: Price & Typo Sanity Validation
    """
    if not gate_1_check_forward_and_empty(text, is_forward=is_forward):
        return None

    clean_text = text.strip()
    effective_is_reply = bool(is_reply) or (reply_to_msg_id is not None)

    # Gate 2: Announcement & Ignored Symbol Filter
    if not gate_2_check_announcements_and_recaps(clean_text, is_reply=effective_is_reply, is_edit=is_edit):
        return None

    # Gate 3: Trade Management (BE, Close, Reactivate, TP1 Hit)
    mgmt_action = gate_3_classify_trade_management(
        clean_text,
        message_id=message_id,
        reply_to_msg_id=reply_to_msg_id,
        is_reply=effective_is_reply,
        is_edit=is_edit,
    )
    if mgmt_action:
        return mgmt_action

    # Reply Hygiene before Gate 4: a reply that did not match any Gate 3 trade-management command
    # must still be blocked by VICTORY_RECAP_BLOCKERS unless it contains real Gold trade structure.
    if effective_is_reply and not is_edit and not _has_real_trade_structure(clean_text):
        lower_clean = clean_text.lower()
        for pattern in VICTORY_RECAP_BLOCKERS:
            if re.search(pattern, lower_clean):
                logger.info(
                    f"[PARSER IGNORED] Reply matched victory recap blocker '{pattern}' without trade structure | text={clean_text!r}"
                )
                return None

    # Gate 4: Signal Extraction
    signal_action = gate_4_extract_signal(
        clean_text,
        message_id=message_id,
        is_edit=is_edit,
        is_reply=effective_is_reply,
    )
    if not signal_action:
        return None

    if not validate_sanity:
        return signal_action

    # Gate 5: Price Sanity Validation
    return finalize_signal_sanity(signal_action, live_price=live_price, log_output=log_output)


# Compatibility aliases
SignalParseResult = SignalAction
parse_telegram_message = parse_message
