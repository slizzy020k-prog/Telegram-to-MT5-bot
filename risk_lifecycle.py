"""R-relative profit floor (R lock) and fill-time telemetry.

Pure domain module (standard library only, no MT5 import, no I/O), following the
discipline of `dynamic_risk.py` and `risk_controls.py`. It decides *what* should
happen to a signal; `mt5_executor.py` carries the decision out against the broker.

Why the R floor exists
-----------------------
The ATR engine arms its breakeven lock at ``max(0.50 * ATR, $0.60, $1.20 + $0.60)``
of favourable excursion regardless of how far the trade can lose. A $22/oz stop and a
$2.00 scalp therefore both become risk-free after the same ~$1.80 -- the wide trade
risks $22 to make $1.80 risk-free. The R floor scales the lock to each trade's own initial risk
(``R = |entry - initial SL|``), so a wide trade waits until it has earned a fair
multiple before it is protected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional, Sequence, Tuple



__all__ = [
    "DEFAULT_R_LOCK_TIERS",
    "LifecyclePolicy",
    "apply_rlock_command",
    "compute_initial_r",
    "compute_r_lock_floor",
    "parse_r_lock_tiers",
    "read_lifecycle_policy",
    "resolve_fill_time",
    "stamp_fill_times",
]


#: ``(r_threshold, lock_fraction)`` pairs. The floor locks ``lock_fraction * R``
#: above entry once the trade has travelled ``r_threshold * R`` in its favour.
DEFAULT_R_LOCK_TIERS: Tuple[Tuple[float, float], ...] = (
    (0.50, 0.00),
    (1.00, 0.25),
    (1.50, 0.50),
    (2.00, 0.75),
)

_ON_TOKENS = frozenset({"on", "true", "1", "enable", "enabled"})
_OFF_TOKENS = frozenset({"off", "false", "0", "disable", "disabled", "none"})

_RLOCK_USAGE = (
    "⚠️ **Invalid value.** Usage: `/rlock on|off`\n"
    "Examples: `/rlock on` or `/rlock off`."
)


def _safe_float(value: Any, default: float) -> float:
    """Return ``float(value)`` when it is a finite real number, else ``default``."""
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int) -> int:
    """Return ``int(value)`` when it is convertible, else ``default``."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool) -> bool:
    """Return a bool from a real bool, a numeric flag, or a known token."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return bool(value)
    token = str(value or "").strip().lower()
    if token in _ON_TOKENS:
        return True
    if token in _OFF_TOKENS:
        return False
    return default


@dataclass(frozen=True)
class LifecyclePolicy:
    """Immutable snapshot of the R-lock settings."""

    r_lock_enabled: bool = False
    r_lock_tiers: Tuple[Tuple[float, float], ...] = DEFAULT_R_LOCK_TIERS
    r_lock_min_r_dollars: float = 0.50


def parse_r_lock_tiers(raw: Any) -> Tuple[Tuple[float, float], ...]:
    """Parse ``"0.5:0.00,1.0:0.25"`` into ascending ``((r_threshold, lock_fraction), ...)``.

    Malformed entries are skipped rather than raising, so a typo in `.env` can
    never stop the bot from booting. The result is always sorted ascending by
    ``r_threshold`` so "the highest reached tier" is well defined.
    """
    if isinstance(raw, (list, tuple)):
        items: Sequence[Any] = list(raw)
    else:
        items = str(raw or "").split(",")

    parsed = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            r_thr = _safe_float(item[0], -1.0)
            frac = _safe_float(item[1], -1.0)
        else:
            text = str(item or "").strip()
            if not text:
                continue
            parts = text.split(":")
            if len(parts) != 2:
                continue
            r_thr = _safe_float(parts[0], -1.0)
            frac = _safe_float(parts[1], -1.0)
        if r_thr <= 0.0 or frac < 0.0 or frac > 1.0:
            continue
        parsed.append((r_thr, frac))
    parsed.sort(key=lambda pair: pair[0])
    return tuple(parsed)


def read_lifecycle_policy(cfg_mod: Any) -> LifecyclePolicy:
    """Read the R-lock policy from a config-like module.

    Every value is normalised defensively: a malformed setting is repaired
    rather than rejected, so the bot always boots.
    """
    tiers = parse_r_lock_tiers(getattr(cfg_mod, "R_LOCK_TIERS", DEFAULT_R_LOCK_TIERS))
    if not tiers:
        tiers = DEFAULT_R_LOCK_TIERS

    return LifecyclePolicy(
        r_lock_enabled=_safe_bool(getattr(cfg_mod, "R_LOCK_ENABLED", False), False),
        r_lock_tiers=tiers,
        r_lock_min_r_dollars=max(0.0, _safe_float(getattr(cfg_mod, "R_LOCK_MIN_R_DOLLARS", 0.50), 0.50)),
    )


def compute_initial_r(entry_price: float, initial_sl: float) -> Optional[float]:
    """Return the initial risk per ounce (``|entry - initial SL|``) or ``None``."""
    entry = _safe_float(entry_price, 0.0)
    stop = _safe_float(initial_sl, 0.0)
    if entry <= 0.0 or stop <= 0.0:
        return None
    r = abs(entry - stop)
    return r if r > 1e-9 else None


def compute_r_lock_floor(
    direction: str,
    entry_price: float,
    initial_sl: float,
    mfe_dollars: float,
    live_price: float,
    policy: LifecyclePolicy,
    digits: int = 2,
) -> Optional[float]:
    """Return the R-relative stop floor for the current favourable excursion.

    The active fraction is the **greatest lock fraction among the tiers whose
    ``r_threshold * R`` has been reached**. Taking the greatest reached fraction
    (rather than "the highest threshold reached") keeps the floor order-
    independent and monotone: the lock can never loosen as the excursion grows,
    even if a caller builds ``LifecyclePolicy`` directly with unsorted tiers.
    For the default ladder the two rules agree exactly:
    ``0.5R -> entry``, ``1.0R -> +0.25R``, ``1.5R -> +0.50R``, ``2.0R -> +0.75R``.

    The floor is ``entry + sign * (lock_fraction * R)`` -- never below entry for
    a BUY and never above entry for a SELL.

    Returns ``None`` when:
    - the R lock is disabled, the direction is not ``BUY``/``SELL``, or the
      initial risk is unknown / below ``r_lock_min_r_dollars``;
    - no tier has been reached yet;
    - the live price is unusable (missing, non-numeric or non-positive), so
      no floor can be validated against the market;
    - the floor is not valid against the current market (for a BUY the floor
      must stay strictly below ``live_price``, for a SELL strictly above it).
      The floor is derived from the peak excursion, so a retracement can carry
      price back past it; submitting a stop on the wrong side of the market
      would be rejected by the broker with retcode 10016. The consumer's own
      stops-buffer clamp is a second line of defence, not the only one.

    Note: while the R lock is enabled the ``BE_LOCK`` candidate's THRESHOLD is
    R-gated (it arms at ``max(0.25 * R, 0.50 * ATR, $0.60, $1.20 + $0.60)``), but its
    LEVEL stays the breakeven offset (``entry +/- $1.20``), so the R-gated BE
    lock is never less protective than the flat BE lock it replaced. The
    candidate picker keeps the tightest of the two: ``BE_LOCK`` wins at the
    0.5R tier (the R floor is only the entry) and ``R_LOCK`` wins from the
    1.0R tier upward (the R floor is ``entry + 0.25 * R``).
    """
    if not policy.r_lock_enabled:
        return None

    dir_token = str(direction or "").strip().upper()
    if dir_token not in ("BUY", "SELL"):
        return None
    sign = 1.0 if dir_token == "BUY" else -1.0

    entry = _safe_float(entry_price, 0.0)
    if entry <= 0.0:
        return None

    r = compute_initial_r(entry, initial_sl)
    if r is None or r < policy.r_lock_min_r_dollars:
        return None

    mfe = _safe_float(mfe_dollars, 0.0)
    if mfe <= 0.0:
        return None

    reached = [
        fraction
        for r_threshold, fraction in policy.r_lock_tiers
        if mfe + 1e-9 >= r_threshold * r
    ]
    if not reached:
        return None
    lock_fraction = max(reached)

    floor_px = round(entry + sign * (lock_fraction * r), digits)

    live = _safe_float(live_price, 0.0)
    if live <= 0.0:
        return None
    if sign > 0 and floor_px >= live:
        return None
    if sign < 0 and floor_px <= live:
        return None

    return floor_px


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or ``None``."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def stamp_fill_times(
    signal_data: Optional[Dict[str, Any]],
    layer_data: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Record a fill timestamp on a layer and on its signal.

    The layer always gets ``filled_at``. The signal gets ``first_fill_at`` only when it is
    unset, so the first fill of the trade wins. Returns True when something changed.
    """
    changed = False
    ts = (now or datetime.now(timezone.utc)).isoformat()
    if isinstance(layer_data, dict) and not layer_data.get("filled_at"):
        layer_data["filled_at"] = ts
        changed = True
    if isinstance(signal_data, dict) and not signal_data.get("first_fill_at"):
        signal_data["first_fill_at"] = ts
        changed = True
    return changed


def resolve_fill_time(signal_data: Dict[str, Any]) -> Optional[datetime]:
    """Return the EARLIEST known fill time for a signal, or None.

    A zone signal normally fills L1 at market and L2-L4 later as limits, so the
    trade age must be measured from the FIRST fill -- never from the signal
    timestamp, and never from the latest fill.
    """
    sig = signal_data if isinstance(signal_data, dict) else {}
    best = _parse_iso_utc(sig.get("first_fill_at"))
    layers = sig.get("layers")
    if isinstance(layers, dict):
        for lv in layers.values():
            if not isinstance(lv, dict):
                continue
            ldt = _parse_iso_utc(lv.get("filled_at"))
            if ldt is not None and (best is None or ldt < best):
                best = ldt
    return best


def _format_tiers(policy: LifecyclePolicy) -> str:
    parts = []
    for r_threshold, fraction in policy.r_lock_tiers:
        label = "BE" if fraction <= 0.0 else f"{fraction:.0%}"
        parts.append(f"{r_threshold:g}R -> {label}")
    return ", ".join(parts)


def apply_rlock_command(cfg_mod: Any, args: Sequence[str]) -> Tuple[bool, str]:
    """Query or toggle the R-relative profit floor via Saved Messages (`/rlock`)."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    policy = read_lifecycle_policy(cfg_mod)
    if not cleaned:
        badge = "🟢 ON" if policy.r_lock_enabled else "⚪ OFF"
        return False, (
            f"📐 **R-Relative Profit Floor (`/rlock`): {badge}**\n"
            f"• Tiers: `{_format_tiers(policy)}`\n"
            f"• Minimum R: `${policy.r_lock_min_r_dollars:.2f}` per ounce\n"
            "• Toggles `R_LOCK_ENABLED`\n"
            "• Change: `/rlock on` | `/rlock off`\n"
            "• The floor is a peak-derived level: it disappears if price retraces past it,"
            " so it can never place a stop on the wrong side of the market."
        )
    token = cleaned[0].lower()
    if token in _ON_TOKENS:
        setattr(cfg_mod, "R_LOCK_ENABLED", True)
        return True, (
            f"📐 **R-Relative Profit Floor Enabled → `ON`**\n"
            f"• Tiers: `{_format_tiers(policy)}`\n"
            "• The floor only ever raises the stop, and it disappears if price retraces past it."
        )
    if token in _OFF_TOKENS:
        setattr(cfg_mod, "R_LOCK_ENABLED", False)
        return True, "⚪ **R-Relative Profit Floor Disabled → `OFF`** (ATR trail only)."
    return False, _RLOCK_USAGE
