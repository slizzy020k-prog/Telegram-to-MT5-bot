"""Startup preflight validation and cross-platform single-instance locking."""

from __future__ import annotations

import atexit
import ctypes
import hashlib
import importlib
import importlib.util
import logging
import math
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

logger = logging.getLogger("copier.startup")

DUPLICATE_INSTANCE_EXIT_CODE = 90
REQUIRED_PACKAGES: Tuple[str, ...] = ("telethon", "MetaTrader5", "dotenv")
_PLACEHOLDER_HASHES = {"", "your_telegram_api_hash", "your_api_hash_here"}

_REGISTRY_LOCK = threading.Lock()
_HELD_MUTEX_NAMES: Set[str] = set()
_HELD_LOCK_PATHS: Set[str] = set()
_ACTIVE_LOCKS: List["SingleInstanceLock"] = []


def _default_lock_path() -> Path:
    try:
        import config as cfg

        lock_path = getattr(cfg, "LOCK_FILE_PATH", None)
        if lock_path:
            return Path(lock_path)
        base_dir = getattr(cfg, "BASE_DIR", None)
        if base_dir:
            return Path(base_dir) / "copier.lock"
    except Exception:
        pass
    return Path(__file__).resolve().parent / "copier.lock"


def _resolve_lock_target(
    lock_name_or_path: Optional[Union[str, Path]]
) -> Tuple[Optional[Path], Optional[str]]:
    """Classify an argument into `(lock_path, mutex_name)`."""
    if lock_name_or_path is None:
        return _default_lock_path(), None

    if isinstance(lock_name_or_path, Path):
        return lock_name_or_path, None

    raw = str(lock_name_or_path).strip()
    if not raw:
        return _default_lock_path(), None

    if raw.startswith(("Local\\", "Global\\")):
        return None, raw

    if "/" in raw or "\\" in raw or raw.lower().endswith(".lock"):
        return Path(raw), None

    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    return None, f"Local\\XAUUSD_Copier_{sanitized}"


class SingleInstanceLock:
    """Single-instance guard combining Win32 named mutex (`CreateMutexW`) and OS file lock."""

    def __init__(
        self,
        lock_path: Optional[Union[str, Path]] = None,
        mutex_name: Optional[str] = None,
    ) -> None:
        resolved_path: Optional[Path] = None
        if lock_path is not None:
            resolved_path = Path(lock_path).resolve()
        elif mutex_name is None:
            resolved_path = _default_lock_path().resolve()

        if mutex_name is not None:
            resolved_mutex = str(mutex_name)
        elif resolved_path is not None:
            digest = hashlib.sha256(str(resolved_path).lower().encode("utf-8")).hexdigest()[:24]
            resolved_mutex = f"Local\\XAUUSD_Copier_{digest}"
        else:
            resolved_mutex = "Local\\XAUUSD_Copier_Default"

        if resolved_path is None:
            digest = hashlib.sha256(resolved_mutex.lower().encode("utf-8")).hexdigest()[:16]
            resolved_path = (Path(tempfile.gettempdir()) / f"xauusd_copier_{digest}.lock").resolve()

        self.lock_path: Path = resolved_path
        self.mutex_name: str = resolved_mutex
        self._mutex_handle: Optional[int] = None
        self._file_obj: Optional[Any] = None
        self._acquired: bool = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def _acquire_win32_mutex(self) -> bool:
        if sys.platform != "win32" or not self.mutex_name:
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, True, self.mutex_name)
        err = ctypes.get_last_error()
        if not handle:
            logger.warning("CreateMutexW failed for %s (winerror=%s)", self.mutex_name, err)
            return False

        # ERROR_ALREADY_EXISTS (183): another live handle holds this named mutex.
        if err == 183:
            kernel32.CloseHandle(handle)
            return False

        self._mutex_handle = int(handle)
        return True

    def _release_win32_mutex(self) -> None:
        if sys.platform != "win32" or not self._mutex_handle:
            self._mutex_handle = None
            return
        handle = self._mutex_handle
        self._mutex_handle = None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        except Exception as exc:
            logger.debug("Ignoring mutex release error: %s", exc)

    def _acquire_file_lock(self) -> bool:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            file_obj = open(self.lock_path, "a+b")
        except OSError as exc:
            logger.warning("Failed to open lock file %s: %s", self.lock_path, exc)
            return False

        try:
            if sys.platform == "win32":
                file_obj.seek(0)
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            file_obj.seek(0)
            file_obj.truncate(0)
            file_obj.write(f"{os.getpid()}\n".encode("ascii"))
            file_obj.flush()
            file_obj.seek(0)
            self._file_obj = file_obj
            return True
        except OSError:
            try:
                file_obj.close()
            except OSError:
                pass
            return False

    def _release_file_lock(self) -> None:
        file_obj = self._file_obj
        self._file_obj = None
        if file_obj is None:
            return
        try:
            if sys.platform == "win32":
                file_obj.seek(0)
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                file_obj.close()
            except OSError:
                pass

    def acquire(self) -> bool:
        """Attempt non-blocking acquisition of the single-instance lock."""
        path_key = str(self.lock_path).lower()
        mutex_key = self.mutex_name.lower()

        with _REGISTRY_LOCK:
            if self._acquired:
                return True
            if mutex_key in _HELD_MUTEX_NAMES or path_key in _HELD_LOCK_PATHS:
                return False

            if not self._acquire_win32_mutex():
                return False

            if not self._acquire_file_lock():
                self._release_win32_mutex()
                return False

            _HELD_MUTEX_NAMES.add(mutex_key)
            _HELD_LOCK_PATHS.add(path_key)
            _ACTIVE_LOCKS.append(self)
            self._acquired = True
            return True

    def release(self) -> None:
        """Release both the OS file lock and Win32 named mutex cleanly."""
        path_key = str(self.lock_path).lower()
        mutex_key = self.mutex_name.lower()

        with _REGISTRY_LOCK:
            self._release_file_lock()
            self._release_win32_mutex()
            _HELD_MUTEX_NAMES.discard(mutex_key)
            _HELD_LOCK_PATHS.discard(path_key)
            if self in _ACTIVE_LOCKS:
                _ACTIVE_LOCKS.remove(self)
            self._acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            raise RuntimeError(f"Another instance already holds lock {self.lock_path}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def acquire_single_instance_lock(
    lock_name_or_path: Optional[Union[str, Path]] = None
) -> Optional[SingleInstanceLock]:
    """Acquire the process-wide single-instance lock, returning `None` if already held."""
    lock_path, mutex_name = _resolve_lock_target(lock_name_or_path)
    lock = SingleInstanceLock(lock_path=lock_path, mutex_name=mutex_name)
    if lock.acquire():
        return lock
    return None


def release_single_instance_lock(lock_handle: Optional[SingleInstanceLock] = None) -> None:
    """Release a specific `SingleInstanceLock`, or all active locks when `None`."""
    if lock_handle is not None:
        lock_handle.release()
        return

    with _REGISTRY_LOCK:
        active_copy = list(_ACTIVE_LOCKS)
    for lock in reversed(active_copy):
        lock.release()


atexit.register(release_single_instance_lock)


def _is_finite_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))


def _check_writable_file_path(label: str, raw_path: Any, errors: List[str]) -> None:
    if raw_path is None or not str(raw_path).strip():
        errors.append(f"{label} is not configured.")
        return

    try:
        path = Path(raw_path)
        if path.exists() and path.is_dir():
            errors.append(f"{label} points to a directory instead of a file: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Probe directory writability via a transient file so existing logs/state are never altered.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".copier_probe_", delete=True):
            pass
        if path.exists() and not os.access(path, os.W_OK):
            errors.append(f"{label} exists and is not writable: {path}")
    except OSError as exc:
        errors.append(f"{label} is not writable ({raw_path}): {exc}")


def _validate_channel_ids(cfg: Any, errors: List[str], warnings: List[str]) -> None:
    channel_ids = getattr(cfg, "CHANNEL_IDS", None)
    if not isinstance(channel_ids, (list, tuple, set)) or len(channel_ids) == 0:
        errors.append("CHANNEL_IDS must be a non-empty list of Telegram channel IDs.")
        return

    for ch in channel_ids:
        if isinstance(ch, bool):
            errors.append(f"Invalid boolean entry in CHANNEL_IDS: {ch!r}")
        elif isinstance(ch, int):
            if ch == 0:
                errors.append("CHANNEL_IDS cannot contain 0.")
        elif isinstance(ch, str):
            if not ch.strip() or any(ws in ch.strip() for ws in (" ", "\t", "\n")):
                errors.append(f"Invalid string entry in CHANNEL_IDS: {ch!r}")
        else:
            errors.append(f"Unsupported CHANNEL_IDS entry type: {type(ch).__name__}")

    if list(channel_ids) == [-1001234567890]:
        warnings.append("CHANNEL_IDS is still set to the example placeholder [-1001234567890].")


def _validate_lots_and_orders(cfg: Any, errors: List[str], warnings: List[str]) -> None:
    base_lots = getattr(cfg, "BASE_LAYER_LOTS", None)
    if not isinstance(base_lots, (list, tuple)) or len(base_lots) != 4:
        errors.append("BASE_LAYER_LOTS must be a sequence of exactly 4 positive lot sizes.")
    else:
        for idx, lot in enumerate(base_lots, start=1):
            if not _is_finite_number(lot) or float(lot) <= 0:
                errors.append(f"BASE_LAYER_LOTS[{idx - 1}] (L{idx}) must be > 0, got {lot!r}.")

    mult = getattr(cfg, "RISK_LOT_MULTIPLIER", 1.0)
    if not _is_finite_number(mult) or float(mult) <= 0:
        errors.append(f"RISK_LOT_MULTIPLIER must be > 0, got {mult!r}.")

    layer_lots = getattr(cfg, "LAYER_LOTS", None)
    if not isinstance(layer_lots, dict) or not layer_lots:
        errors.append("LAYER_LOTS must be a non-empty mapping.")
    else:
        for key, lot in layer_lots.items():
            if not _is_finite_number(lot) or float(lot) <= 0:
                errors.append(f"LAYER_LOTS[{key!r}] must be > 0, got {lot!r}.")

    max_orders = getattr(cfg, "MAX_ORDERS_PER_SIGNAL", 4)
    if not isinstance(max_orders, int) or isinstance(max_orders, bool) or max_orders < 1:
        errors.append(f"MAX_ORDERS_PER_SIGNAL must be an integer >= 1, got {max_orders!r}.")
    elif max_orders < 4:
        warnings.append(
            f"MAX_ORDERS_PER_SIGNAL={max_orders} is below 4; 4-layer entries will be capped."
        )

    be_layers = getattr(cfg, "BE_LAYERS", [])
    valid_layer_names = {"L1", "L2", "L3", "L4", "TOP", "MID", "BOTTOM"}
    if not isinstance(be_layers, (list, tuple, set)):
        errors.append(f"BE_LAYERS must be a list/sequence, got {type(be_layers).__name__}.")
    else:
        for item in be_layers:
            if str(item).strip().upper() not in valid_layer_names:
                warnings.append(f"Unrecognized layer in BE_LAYERS: {item!r}")

    default_symbol = getattr(cfg, "DEFAULT_SYMBOL", "")
    if not isinstance(default_symbol, str) or not default_symbol.strip():
        errors.append("DEFAULT_SYMBOL must be a non-empty symbol string.")


def _validate_tp_and_runner_buffers(cfg: Any, errors: List[str], warnings: List[str]) -> None:
    deepest_count = getattr(cfg, "RUNNER_DEEPEST_COUNT_FULL", 1)
    if isinstance(deepest_count, bool) or not isinstance(deepest_count, int) or not (1 <= deepest_count <= 2):
        errors.append(
            f"RUNNER_DEEPEST_COUNT_FULL must be an integer in [1, 2], got {deepest_count!r}."
        )

    max_tp_stage = getattr(cfg, "RUNNER_MAX_TP_STAGE", 4)
    if isinstance(max_tp_stage, bool) or not isinstance(max_tp_stage, int) or not (1 <= max_tp_stage <= 5):
        errors.append(
            f"RUNNER_MAX_TP_STAGE must be an integer in [1, 5], got {max_tp_stage!r}."
        )

    for pos_guard_field in ("MAX_ALLOWED_SPREAD_DOLLARS", "ADAPTIVE_SPREAD_CEILING_DOLLARS"):
        if hasattr(cfg, pos_guard_field):
            p_val = getattr(cfg, pos_guard_field)
            if not _is_finite_number(p_val) or float(p_val) <= 0.0:
                errors.append(f"{pos_guard_field} must be > 0, got {p_val!r}.")

    if hasattr(cfg, "CLOSE_ON_SIGNAL_DELETED"):
        close_on_del = getattr(cfg, "CLOSE_ON_SIGNAL_DELETED")
        if not isinstance(close_on_del, bool):
            errors.append(
                f"CLOSE_ON_SIGNAL_DELETED must be a bool, got {close_on_del!r}."
            )

    non_negative_fields = (
        "TP_FRONT_RUN_OFFSET",
        "TP1_CLOSE_PROXIMITY_PIPS",
        "TP1_LOCK_BUFFER",
        "TRAIL_DISTANCE_PIPS",
        "TRAIL_STEP_PIPS",
        "MIN_SL_BUFFER_PIPS",
        "CATCHUP_WINDOW_SECONDS",
        "CATCHUP_MAX_MESSAGES",
        "CATCHUP_MARKET_MAX_AGE_SECONDS",
        "CATCHUP_MARKET_MAX_SLIPPAGE_PIPS",
        "DAILY_PNL_CACHE_TTL_SECONDS",
        "DELETED_SIGNAL_POLL_INTERVAL_SECONDS",
    )
    for field in non_negative_fields:
        val = getattr(cfg, field, 0.0)
        if not _is_finite_number(val) or float(val) < 0:
            errors.append(f"{field} must be >= 0, got {val!r}.")

    positive_fields = (
        "FALLBACK_SL_PIPS",
        "MAX_SL_PIPS",
        "FALLBACK_TP1_PIPS",
        "DEFAULT_TP_STEP_PIPS",
        "TP2_STEP_PIPS",
        "TP3_STEP_PIPS",
        "TP4_STEP_PIPS",
        "TP5_STEP_PIPS",
    )
    for field in positive_fields:
        val = getattr(cfg, field, 1.0)
        if not _is_finite_number(val) or float(val) <= 0:
            errors.append(f"{field} must be > 0, got {val!r}.")

    fallback_sl = getattr(cfg, "FALLBACK_SL_PIPS", None)
    max_sl = getattr(cfg, "MAX_SL_PIPS", None)
    if _is_finite_number(fallback_sl) and _is_finite_number(max_sl) and float(fallback_sl) > float(max_sl):
        errors.append(
            f"FALLBACK_SL_PIPS ({fallback_sl}) cannot exceed MAX_SL_PIPS ({max_sl})."
        )

    pause_pct = getattr(cfg, "DAILY_LOSS_PAUSE_PCT", 0.0)
    cutoff_pct = getattr(cfg, "EMERGENCY_CUTOFF_LOSS_PCT", 0.0)
    float_cap_pct = getattr(cfg, "FLOAT_LOSS_FORCE_CLOSE_PCT", 0.0)
    if not (_is_finite_number(pause_pct) and 0 <= float(pause_pct) <= 100):
        errors.append(f"DAILY_LOSS_PAUSE_PCT must be in [0, 100], got {pause_pct!r}.")
    if not (_is_finite_number(cutoff_pct) and 0 <= float(cutoff_pct) <= 100):
        errors.append(f"EMERGENCY_CUTOFF_LOSS_PCT must be in [0, 100], got {cutoff_pct!r}.")
    if not (_is_finite_number(float_cap_pct) and 0 <= float(float_cap_pct) <= 100):
        errors.append(f"FLOAT_LOSS_FORCE_CLOSE_PCT must be in [0, 100], got {float_cap_pct!r}.")
    if (
        _is_finite_number(pause_pct)
        and _is_finite_number(cutoff_pct)
        and float(pause_pct) > 0
        and float(cutoff_pct) > 0
        and float(pause_pct) >= float(cutoff_pct)
    ):
        errors.append(
            f"DAILY_LOSS_PAUSE_PCT ({pause_pct}) must be strictly less than EMERGENCY_CUTOFF_LOSS_PCT ({cutoff_pct})."
        )

    trail_dist = getattr(cfg, "TRAIL_DISTANCE_PIPS", 15.0)
    trail_step = getattr(cfg, "TRAIL_STEP_PIPS", 5.0)
    if _is_finite_number(trail_dist) and _is_finite_number(trail_step) and float(trail_step) > float(trail_dist):
        warnings.append(
            f"TRAIL_STEP_PIPS ({trail_step}) exceeds TRAIL_DISTANCE_PIPS ({trail_dist}); broker SL will trail infrequently."
        )


def _validate_dynamic_risk_config(cfg: Any, errors: List[str]) -> None:
    """Validate Pure Dynamic ATR Engine (SL/TP, BE Lock & Trailing SL/TP) fields."""
    for bool_flag in ("DYNAMIC_ATR_SLTP_ENABLED", "DYNAMIC_ATR_TRAIL_ENABLED", "DYNAMIC_ATR_TP_TRAIL_ENABLED"):
        if hasattr(cfg, bool_flag):
            flag_val = getattr(cfg, bool_flag)
            if not isinstance(flag_val, bool):
                errors.append(f"{bool_flag} must be a bool, got {flag_val!r}.")
    valid_modes = ("ADAPTIVE", "FALLBACK_ONLY", "PURE_ATR")
    valid_tfs = ("M1", "M5", "M15", "H1")
    dr_mode = str(getattr(cfg, "DYNAMIC_SLTP_MODE", "ADAPTIVE")).strip().upper()
    if dr_mode not in valid_modes:
        errors.append(f"DYNAMIC_SLTP_MODE must be one of {valid_modes}, got {dr_mode!r}.")
    for tf_field in ("DYNAMIC_ATR_TIMEFRAME", "DYNAMIC_ATR_TRAIL_TIMEFRAME"):
        tf_val = str(getattr(cfg, tf_field, "M5")).strip().upper()
        if tf_val not in valid_tfs:
            errors.append(f"{tf_field} must be one of {valid_tfs}, got {tf_val!r}.")
    atr_period = getattr(cfg, "DYNAMIC_ATR_PERIOD", 14)
    if isinstance(atr_period, bool) or not isinstance(atr_period, int) or not (2 <= atr_period <= 200):
        errors.append(f"DYNAMIC_ATR_PERIOD must be an integer in [2, 200], got {atr_period!r}.")
    for non_neg_field in (
        "DYNAMIC_ATR_TRAIL_ACTIVATION_MULT",
        "DYNAMIC_ATR_BE_ACTIVATION_MULT",
        "DYNAMIC_ATR_BE_MIN_MFE_DOLLARS",
        "DYNAMIC_ATR_BE_LOCK_OFFSET_DOLLARS",
    ):
        val = getattr(cfg, non_neg_field, 0.0)
        if not _is_finite_number(val) or float(val) < 0.0:
            errors.append(f"{non_neg_field} must be >= 0, got {val!r}.")
    for pos_field in (
        "DYNAMIC_ATR_BASELINE_DOLLARS",
        "DYNAMIC_SL_ATR_MULT",
        "DYNAMIC_TP_ATR_MULT",
        "DYNAMIC_TP_STEP_ATR_MULT",
        "DYNAMIC_SL_MIN_DOLLARS",
        "DYNAMIC_SL_MAX_DOLLARS",
        "DYNAMIC_TP_MIN_DOLLARS",
        "DYNAMIC_TP_MAX_DOLLARS",
        "DYNAMIC_SLTP_SCALE_MIN",
        "DYNAMIC_SLTP_SCALE_MAX",
        "DYNAMIC_ATR_TRAIL_MULT",
        "DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS",
        "DYNAMIC_ATR_TRAIL_EXTENDED_MULT",
        "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS",
        "DYNAMIC_ATR_TRAIL_HOMERUN_MULT",
        "DYNAMIC_ATR_TRAIL_MIN_DOLLARS",
        "DYNAMIC_ATR_TRAIL_MAX_DOLLARS",
        "DYNAMIC_ATR_TRAIL_STEP_DOLLARS",
        "DYNAMIC_ATR_TP_LIFT_ATR_MULT",
    ):
        val = getattr(cfg, pos_field, 1.0)
        if not _is_finite_number(val) or float(val) <= 0.0:
            errors.append(f"{pos_field} must be > 0, got {val!r}.")
    for pct_field in ("DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT", "DYNAMIC_ATR_TP1_LIFT_MIN_PROGRESS_PCT"):
        prog_pct = getattr(cfg, pct_field, 0.75 if pct_field == "DYNAMIC_ATR_TP_LIFT_PROGRESS_PCT" else 0.50)
        if not _is_finite_number(prog_pct) or not (0.0 < float(prog_pct) < 1.0):
            errors.append(f"{pct_field} must be in (0, 1), got {prog_pct!r}.")
    for lo_field, hi_field in (
        ("DYNAMIC_SL_MIN_DOLLARS", "DYNAMIC_SL_MAX_DOLLARS"),
        ("DYNAMIC_TP_MIN_DOLLARS", "DYNAMIC_TP_MAX_DOLLARS"),
        ("DYNAMIC_SLTP_SCALE_MIN", "DYNAMIC_SLTP_SCALE_MAX"),
        ("DYNAMIC_ATR_TRAIL_MIN_DOLLARS", "DYNAMIC_ATR_TRAIL_MAX_DOLLARS"),
        ("DYNAMIC_ATR_TRAIL_EXTENDED_MFE_DOLLARS", "DYNAMIC_ATR_TRAIL_HOMERUN_MFE_DOLLARS"),
    ):
        lo_v, hi_v = getattr(cfg, lo_field, 1.0), getattr(cfg, hi_field, 10.0)
        if _is_finite_number(lo_v) and _is_finite_number(hi_v) and float(lo_v) > float(hi_v):
            errors.append(f"{lo_field} ({lo_v}) cannot exceed {hi_field} ({hi_v}).")


def validate_environment(check_packages: bool = True, cfg: Any = None) -> Dict[str, Any]:
    """Validate local packages, trading configuration invariants, and writable paths."""
    errors: List[str] = []
    warnings: List[str] = []

    if check_packages:
        for pkg in REQUIRED_PACKAGES:
            if pkg not in sys.modules and importlib.util.find_spec(pkg) is None:
                errors.append(f"Missing required Python package: {pkg}")

    if cfg is None:
        try:
            cfg = importlib.import_module("config")
        except Exception as exc:
            errors.append(f"Failed to import config module: {exc}")
            return {"ok": False, "errors": errors, "warnings": warnings}

    # Validate credential presence without ever logging or returning secret values.
    api_id = getattr(cfg, "TELEGRAM_API_ID", None)
    if not isinstance(api_id, int) or isinstance(api_id, bool) or api_id <= 0:
        errors.append("TELEGRAM_API_ID must be a positive integer.")
    elif api_id == 12345678:
        warnings.append("TELEGRAM_API_ID is using the example placeholder.")

    api_hash = getattr(cfg, "TELEGRAM_API_HASH", None)
    if not isinstance(api_hash, str) or not api_hash.strip():
        errors.append("TELEGRAM_API_HASH is missing.")
    elif api_hash.strip() in _PLACEHOLDER_HASHES:
        warnings.append("TELEGRAM_API_HASH is using an example placeholder.")

    _validate_channel_ids(cfg, errors, warnings)
    _validate_lots_and_orders(cfg, errors, warnings)
    _validate_tp_and_runner_buffers(cfg, errors, warnings)
    _validate_dynamic_risk_config(cfg, errors)

    _check_writable_file_path("LOG_FILE_PATH", getattr(cfg, "LOG_FILE_PATH", None), errors)
    _check_writable_file_path("STATE_FILE_PATH", getattr(cfg, "STATE_FILE_PATH", None), errors)
    _check_writable_file_path(
        "TRADES_LEDGER_PATH",
        getattr(cfg, "TRADES_LEDGER_PATH", Path(getattr(cfg, "BASE_DIR", Path("."))) / "trades_ledger.jsonl"),
        errors,
    )
    if getattr(cfg, "LOCK_FILE_PATH", None) is not None:
        _check_writable_file_path("LOCK_FILE_PATH", getattr(cfg, "LOCK_FILE_PATH"), errors)

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
