"""Domain controls for runtime risk limits and Saved Messages command handling."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Optional, Sequence, Tuple

_OFF_TOKENS = frozenset({"off", "0", "0%", "0.0", "0.0%", "none", "disable", "disabled", "false", "unlimited"})
_LOT_CAP_OFF_TOKENS = frozenset({"off", "false", "0", "none", "disable", "disabled", "unlimited"})
_ON_TOKENS = frozenset({"on", "true", "1", "enable", "enabled"})

_FLOAT_CAP_USAGE = "⚠️ **Invalid value.** Usage: `/floatcap <1-100|off>`\nExamples: `/floatcap 60` (enable at -60%) or `/floatcap off` (unlimited)."
_LOSS_LIMIT_USAGE = (
    "⚠️ **Invalid value.** Usage: `/losslimit <pause_pct|off> [cutoff_pct|off]`\n"
    "Examples: `/losslimit 50 90`, `/losslimit 50 off`, or `/losslimit off` (unlimited). "
    "Values must be `1-100`, with `cutoff_pct > pause_pct` when both are enabled."
)
_LOT_CAP_USAGE = "⚠️ **Invalid value.** Usage: `/lotcap <on|off>`\nExamples: `/lotcap on` (enable Risk Guide lot cap) or `/lotcap off` (unlimited)."


@dataclass(frozen=True)
class RiskLimitsSnapshot:
    """Immutable snapshot of runtime lot-cap and drawdown-limit settings."""

    lot_cap_enabled: bool
    daily_loss_pause_pct: float
    emergency_cutoff_pct: float
    float_loss_cap_pct: float


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _parse_pct(raw: str) -> Optional[float]:
    try:
        parsed = float(raw.strip().rstrip("%"))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and 1.0 <= parsed <= 100.0 else None


def _fmt_pct(pct: float) -> str:
    return f"{pct:.0f}%" if abs(pct - round(pct)) < 1e-9 else f"{pct:.1f}%"


def _pct_amount(balance: float, pct: float) -> float:
    return round(max(0.0, _safe_float(balance)) * (pct / 100.0), 2)


def read_risk_limits(cfg_mod: Any) -> RiskLimitsSnapshot:
    """Read active runtime risk and drawdown limits from the config module."""
    return RiskLimitsSnapshot(
        lot_cap_enabled=bool(getattr(cfg_mod, "ENFORCE_HIGH_RISK_LOT_CAP", False)),
        daily_loss_pause_pct=max(0.0, _safe_float(getattr(cfg_mod, "DAILY_LOSS_PAUSE_PCT", 0.0))),
        emergency_cutoff_pct=max(0.0, _safe_float(getattr(cfg_mod, "EMERGENCY_CUTOFF_LOSS_PCT", 0.0))),
        float_loss_cap_pct=max(0.0, _safe_float(getattr(cfg_mod, "FLOAT_LOSS_FORCE_CLOSE_PCT", 0.0))),
    )


def apply_float_cap_command(cfg_mod: Any, raw_arg: str, balance: float, currency: str = "GBP") -> Tuple[bool, str]:
    """Query or update FLOAT_LOSS_FORCE_CLOSE_PCT (0.0 = unlimited, 1..100 = enabled)."""
    token, bal = (raw_arg or "").strip(), max(0.0, _safe_float(balance))
    if not token:
        cur = read_risk_limits(cfg_mod).float_loss_cap_pct
        if cur <= 0.0:
            return False, "⚪ **Floating Loss Cap:** `Unlimited (OFF)`\n• No automatic force-close on floating drawdown.\n• Enable: `/floatcap <1-100>` | Disable: `/floatcap off`"
        return False, (
            f"🔴 **Floating Loss Hard Cap:** `{_fmt_pct(cur)}` (≈ `-{_pct_amount(bal, cur):.2f} {currency}`)\n"
            "• Force-closes ALL open trades if floating loss hits this level.\n• Change: `/floatcap <1-100>` | Disable: `/floatcap off`"
        )
    if token.lower() in _OFF_TOKENS:
        if cfg_mod is not None:
            setattr(cfg_mod, "FLOAT_LOSS_FORCE_CLOSE_PCT", 0.0)
        return True, "⚪ **Floating Loss Cap Disabled → `Unlimited (OFF)`**\n• No automatic force-close on floating drawdown.\n• Re-enable anytime: `/floatcap <1-100>` (e.g. `/floatcap 60`)"
    new_pct = _parse_pct(token)
    if new_pct is None:
        return False, _FLOAT_CAP_USAGE
    if cfg_mod is not None:
        setattr(cfg_mod, "FLOAT_LOSS_FORCE_CLOSE_PCT", new_pct)
    cap_amt = _pct_amount(bal, new_pct)
    return True, (
        f"🔴 **Floating Loss Hard Cap Updated → `{_fmt_pct(new_pct)}`**\n"
        f"• Force-closes ALL open trades if floating loss breaches `-{cap_amt:.2f} {currency}` (`{_fmt_pct(new_pct)}` of `{bal:.2f} {currency}`).\n"
        "• Change: `/floatcap <1-100>` | Disable: `/floatcap off`"
    )


def _parse_loss_limit_args(cleaned: Sequence[str], current_cutoff: float) -> Optional[Tuple[float, float]]:
    if len(cleaned) == 1:
        if cleaned[0].lower() in _OFF_TOKENS:
            return 0.0, 0.0
        pause_pct = _parse_pct(cleaned[0])
        if pause_pct is None:
            return None
        return pause_pct, (current_cutoff if current_cutoff > pause_pct else 0.0)
    if len(cleaned) != 2:
        return None
    pause_pct = 0.0 if cleaned[0].lower() in _OFF_TOKENS else _parse_pct(cleaned[0])
    cutoff_pct = 0.0 if cleaned[1].lower() in _OFF_TOKENS else _parse_pct(cleaned[1])
    if pause_pct is None or cutoff_pct is None:
        return None
    if pause_pct > 0.0 and cutoff_pct > 0.0 and cutoff_pct <= pause_pct:
        return None
    return pause_pct, cutoff_pct


def _loss_limit_details(pause_pct: float, cutoff_pct: float, bal: float, currency: str) -> str:
    p_txt = "`Unlimited (OFF)`" if pause_pct <= 0.0 else f"`{_fmt_pct(pause_pct)}` (≈ `-{_pct_amount(bal, pause_pct):.2f} {currency}`)"
    c_txt = "`Unlimited (OFF)`" if cutoff_pct <= 0.0 else f"`{_fmt_pct(cutoff_pct)}` (≈ `-{_pct_amount(bal, cutoff_pct):.2f} {currency}`)"
    return f"• Pause New Signals: {p_txt}\n• Emergency Cut-Off: {c_txt}"


def apply_loss_limit_command(cfg_mod: Any, args: Sequence[str], balance: float, currency: str = "GBP") -> Tuple[bool, str]:
    """Query or update DAILY_LOSS_PAUSE_PCT and EMERGENCY_CUTOFF_LOSS_PCT."""
    cleaned = [str(a).strip() for a in (args or []) if str(a).strip()]
    bal, snap = max(0.0, _safe_float(balance)), read_risk_limits(cfg_mod)
    if not cleaned:
        is_off = snap.daily_loss_pause_pct <= 0.0 and snap.emergency_cutoff_pct <= 0.0
        header = "⚪ **Daily Loss Limits:** `Unlimited (OFF)`" if is_off else "🛡️ **Daily Loss Limits:** `ACTIVE`"
        body = _loss_limit_details(snap.daily_loss_pause_pct, snap.emergency_cutoff_pct, bal, currency)
        return False, f"{header}\n{body}\n• Change: `/losslimit <pause_pct> [cutoff_pct]` | Disable: `/losslimit off`"
    parsed = _parse_loss_limit_args(cleaned, snap.emergency_cutoff_pct)
    if parsed is None:
        return False, _LOSS_LIMIT_USAGE
    pause_pct, cutoff_pct = parsed
    if cfg_mod is not None:
        setattr(cfg_mod, "DAILY_LOSS_PAUSE_PCT", pause_pct)
        setattr(cfg_mod, "EMERGENCY_CUTOFF_LOSS_PCT", cutoff_pct)
    body = _loss_limit_details(pause_pct, cutoff_pct, bal, currency)
    header = "⚪ **Daily Loss Limits Disabled → `Unlimited (OFF)`**" if pause_pct <= 0.0 and cutoff_pct <= 0.0 else "🛡️ **Daily Loss Limits Updated**"
    return True, f"{header}\n{body}\n• Disable anytime: `/losslimit off`"


def apply_lot_cap_command(cfg_mod: Any, raw_arg: str, risk_status: Dict[str, Any]) -> Tuple[bool, str]:
    """Query or toggle ENFORCE_HIGH_RISK_LOT_CAP."""
    token, rs = (raw_arg or "").strip().lower(), (risk_status or {})
    open_l, high_l = _safe_float(rs.get("open_lots")), _safe_float(rs.get("high_risk_lots"))
    cap, curr = _safe_float(rs.get("capital")), str(rs.get("currency", "GBP") or "GBP")
    if not token:
        if read_risk_limits(cfg_mod).lot_cap_enabled:
            return False, f"🛡️ **High-Risk Lot Cap:** `ON` (Max `{high_l:.2f}` active lots for `{cap:.2f} {curr}` | Open: `{open_l:.2f}`)\n• Disable: `/lotcap off`"
        return False, f"⚪ **High-Risk Lot Cap:** `Unlimited (OFF)` (Open: `{open_l:.2f}` lots)\n• Enable anytime: `/lotcap on`"
    if token in _ON_TOKENS:
        if cfg_mod is not None:
            setattr(cfg_mod, "ENFORCE_HIGH_RISK_LOT_CAP", True)
        return True, f"🛡️ **High-Risk Lot Cap Enabled → `ON`**\n• Active open lots capped at `{high_l:.2f}` for `{cap:.2f} {curr}` (Open: `{open_l:.2f}`).\n• Disable: `/lotcap off`"
    if token in _LOT_CAP_OFF_TOKENS:
        if cfg_mod is not None:
            setattr(cfg_mod, "ENFORCE_HIGH_RISK_LOT_CAP", False)
        return True, f"⚪ **High-Risk Lot Cap Disabled → `Unlimited (OFF)`**\n• Active open lots are now uncapped (Open: `{open_l:.2f}` lots).\n• Re-enable: `/lotcap on`"
    return False, _LOT_CAP_USAGE


def format_limits_status_lines(cfg_mod: Any, dd_status: Dict[str, Any], risk_status: Dict[str, Any]) -> Dict[str, str]:
    """Build status summary strings for active lots, drawdown guard, and floating-loss cap."""
    snap, dd, rs = read_risk_limits(cfg_mod), (dd_status or {}), (risk_status or {})
    open_l, pend_l = _safe_float(rs.get("open_lots")), _safe_float(rs.get("pending_lots"))
    if snap.lot_cap_enabled:
        low_l, mid_l, high_l = _safe_float(rs.get("low_risk_lots")), _safe_float(rs.get("mid_risk_lots")), _safe_float(rs.get("high_risk_lots"))
        lots_label = f"`{open_l:.2f} / {high_l:.2f} Max` | Pending: `{pend_l:.2f}` (Low: `{low_l:.2f}` | Mid: `{mid_l:.2f}` | High Cap: `{high_l:.2f}`)"
    else:
        lots_label = f"`{open_l:.2f}` (`Unlimited (OFF)`) | Pending: `{pend_l:.2f}`"
    curr, total_loss, loss_pct = str(dd.get("currency", "GBP") or "GBP"), _safe_float(dd.get("total_daily_loss")), _safe_float(dd.get("loss_pct"))
    if snap.daily_loss_pause_pct <= 0.0 and snap.emergency_cutoff_pct <= 0.0:
        dd_line = f"• Daily Drawdown Guard (`/losslimit`): `⚪ Unlimited (OFF)` (Loss: `-{total_loss:.2f} {curr}` / `{loss_pct:.1f}%`)"
    else:
        dd_state = (
            f"🚨 {_fmt_pct(snap.emergency_cutoff_pct)} CUT-OFF" if dd.get("emergency_cutoff_triggered")
            else f"🛑 {_fmt_pct(snap.daily_loss_pause_pct)} PAUSED (Holding Open Trades)" if dd.get("pause_new_signals")
            else "🟢 OK"
        )
        p_part = f"{_fmt_pct(snap.daily_loss_pause_pct)} Pause: `-{_safe_float(dd.get('pause_limit_amount')):.2f}`" if snap.daily_loss_pause_pct > 0 else "Pause: `OFF`"
        c_part = f"{_fmt_pct(snap.emergency_cutoff_pct)} Cut-Off: `-{_safe_float(dd.get('emergency_cutoff_amount')):.2f}`" if snap.emergency_cutoff_pct > 0 else "Cut-Off: `OFF`"
        dd_line = f"• Daily Drawdown Guard (`/losslimit`): `{dd_state}` (Loss: `-{total_loss:.2f} {curr}` / `{loss_pct:.1f}%` | {p_part} | {c_part})"
    float_loss = _safe_float(dd.get("floating_loss"))
    if snap.float_loss_cap_pct <= 0.0:
        fc_line = f"• Floating Loss Cap (`/floatcap`): `⚪ Unlimited (OFF)` (Floating Loss: `-{float_loss:.2f} {curr}`)"
    else:
        f_state = "🔴 TRIGGERED" if dd.get("float_force_close_triggered") else "🟢 ACTIVE"
        fc_line = f"• Floating Loss Cap (`/floatcap`): `{f_state}` — `{_fmt_pct(snap.float_loss_cap_pct)}` (`-{_safe_float(dd.get('float_cap_limit')):.2f} {curr}` | Current: `-{float_loss:.2f} {curr}`)"
    return {"active_lots_label": lots_label, "drawdown_guard_line": dd_line, "float_cap_line": fc_line}


def format_protection_profile_line(cfg_mod: Any) -> str:
    """Display-only one-line summary of the active protection profile.

    Reads the live config module without mutating it: TRAILING_PROFILE,
    R-Lock (via risk_lifecycle), High-Risk Lot Cap and the daily-loss pause
    guard. Used by the startup log, /status and the launcher banners.
    """
    prof = str(getattr(cfg_mod, "TRAILING_PROFILE", "baseline"))
    snap = read_risk_limits(cfg_mod)
    lot_txt = "ON" if snap.lot_cap_enabled else "OFF"
    # _fmt_pct already carries the '%' suffix (80.0 -> '80%'), so no extra '%'.
    pause_txt = _fmt_pct(snap.daily_loss_pause_pct) if snap.daily_loss_pause_pct > 0 else "OFF"
    try:
        from risk_lifecycle import read_lifecycle_policy  # lazy import: avoid circularity

        r_lock_on = bool(read_lifecycle_policy(cfg_mod).r_lock_enabled)
    except Exception:
        r_lock_on = bool(getattr(cfg_mod, "R_LOCK_ENABLED", False))
    r_txt = "ON" if r_lock_on else "OFF"
    base = (
        f"• Protection Profile: TRAILING_PROFILE=`{prof}` | R-Lock: `{r_txt}` "
        f"| Lot Cap: `{lot_txt}` | Daily Loss Pause: `{pause_txt}`"
    )
    # T3 ATR-relative upgrade announcement (opt-in). Absent when the features are
    # disabled, so the shipped/baseline line stays byte-for-byte unchanged.
    mfe_on = bool(getattr(cfg_mod, "DYNAMIC_ATR_MFE_LOCK_ENABLED", False))
    auto_be_on = bool(getattr(cfg_mod, "DYNAMIC_ATR_AUTO_BE_ENABLED", False))
    if mfe_on or auto_be_on:
        rel_mult = _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_RELATIVE_SCALE_MULT", 1.0), 1.0)
        be_pips = _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_AUTO_BE_PIPS", 0.0), 0.0)
        trail_ceiling = _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS", 0.0), 0.0)
        be_ceiling = _safe_float(getattr(cfg_mod, "DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS", 0.0), 0.0)
        parts = []
        if mfe_on:
            parts.append("MFE-lock ladder `ON` (ATR-relative)")
        if auto_be_on:
            parts.append(f"Auto-BE `{be_pips:.0f} pips`")
        parts.append(f"Relative ATR `x{rel_mult:.2f}`")
        if trail_ceiling > 0.0:
            parts.append(f"Trail ceiling `${trail_ceiling:.2f}`")
        if be_ceiling > 0.0:
            parts.append(f"BE ceiling `${be_ceiling:.2f}`")
        base += " | T3 upgrade: " + " | ".join(parts)
    return base
