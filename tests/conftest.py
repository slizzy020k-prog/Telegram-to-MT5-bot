import atexit
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Allocate an isolated temporary runtime directory BEFORE pytest imports any test module
# that imports `config` or `main` at module scope.
_PYTEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="xauusd_copier_pytest_"))


def _apply_isolated_env() -> None:
    os.environ["DRY_RUN"] = "true"
    os.environ["LOG_FILE_PATH"] = str(_PYTEST_RUNTIME_DIR / "copier_test.log")
    os.environ["STATE_FILE_PATH"] = str(_PYTEST_RUNTIME_DIR / "signals_state_test.json")
    os.environ["TRADES_LEDGER_PATH"] = str(_PYTEST_RUNTIME_DIR / "trades_ledger_test.jsonl")
    os.environ["LOCK_FILE_PATH"] = str(_PYTEST_RUNTIME_DIR / "copier_test.lock")
    os.environ["PREVENT_SYSTEM_SLEEP"] = "false"
    os.environ["CONSOLE_TITLE_UPDATES"] = "false"
    os.environ.setdefault("LIMIT_ENTRY_FRONT_RUN_OFFSET", "0.0")
    # Hermeticity: neutralize the live opt-in T3-upgrade protection knobs so the
    # developer's live .env (which enables them) cannot leak into tests that assume
    # shipped baseline protection. Tests exercising the upgrade enable these explicitly
    # via monkeypatch/policy objects. RELATIVE_SCALE_MULT keeps its shipped default (1.02):
    # it is gated on the ladder and a shipped-default test pins it.
    os.environ["DYNAMIC_ATR_MFE_LOCK_ENABLED"] = "false"
    os.environ["DYNAMIC_ATR_AUTO_BE_ENABLED"] = "false"
    os.environ["DYNAMIC_ATR_TRAIL_ACTIVATION_CEILING_DOLLARS"] = "0.0"
    os.environ["DYNAMIC_ATR_BE_ACTIVATION_CEILING_DOLLARS"] = "0.0"
    os.environ["DYNAMIC_ATR_TRAIL_DISTANCE_TIERS"] = ""


_apply_isolated_env()


def _cleanup_runtime_dir() -> None:
    for logger_name in ("copier", "telethon", "copier.config"):
        lgr = logging.getLogger(logger_name)
        for handler in list(lgr.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
            if isinstance(handler, logging.FileHandler):
                lgr.removeHandler(handler)
    shutil.rmtree(_PYTEST_RUNTIME_DIR, ignore_errors=True)


atexit.register(_cleanup_runtime_dir)


def pytest_configure(config) -> None:
    _apply_isolated_env()


def pytest_sessionfinish(session, exitstatus) -> None:
    _cleanup_runtime_dir()


@pytest.fixture(scope="session", autouse=True)
def isolate_runtime_paths(tmp_path_factory):
    """Guarantee pytest never reads/writes live copier.log, signals_state.json, or trades_ledger.jsonl."""
    session_dir = tmp_path_factory.mktemp("copier_session_runtime")
    _apply_isolated_env()

    import config as cfg

    assert cfg.DRY_RUN is True, "pytest MUST run with DRY_RUN=True"
    live_paths = {
        (cfg.BASE_DIR / "copier.log").resolve(),
        (cfg.BASE_DIR / "signals_state.json").resolve(),
        (cfg.BASE_DIR / "trades_ledger.jsonl").resolve(),
    }
    assert Path(cfg.LOG_FILE_PATH).resolve() not in live_paths
    assert Path(cfg.STATE_FILE_PATH).resolve() not in live_paths
    if hasattr(cfg, "TRADES_LEDGER_PATH"):
        assert Path(cfg.TRADES_LEDGER_PATH).resolve() not in live_paths

    if "main" in sys.modules:
        main_mod = sys.modules["main"]
        main_mod.DRY_RUN = True
        if hasattr(main_mod, "StateManager") and hasattr(main_mod, "state_mgr"):
            main_mod.state_mgr = main_mod.StateManager(session_dir / "signals_state.json")
        if hasattr(main_mod, "trade_ledger") and hasattr(main_mod, "TradeLedger"):
            main_mod.trade_ledger = main_mod.TradeLedger(session_dir / "trades_ledger.jsonl")

    yield session_dir


# Runtime-isolation knobs managed by _apply_isolated_env(). The shipped_config fixture
# keeps these set so a neutralized reload still runs DRY_RUN=True against isolated temp
# paths and never touches live runtime artifacts.
_ISOLATION_ENV_KNOBS = frozenset({
    "DRY_RUN",
    "LOG_FILE_PATH",
    "STATE_FILE_PATH",
    "TRADES_LEDGER_PATH",
    "LOCK_FILE_PATH",
    "PREVENT_SYSTEM_SLEEP",
    "CONSOLE_TITLE_UPDATES",
})


@pytest.fixture
def shipped_config():
    """Reload ``config`` with .env loading neutralized and every strategy knob delenv'd.

    Yields the reloaded config module holding the SHIPPED literal defaults, independent of
    the ambient (live-activated) .env. On teardown the original environment and dotenv
    loader are restored and config is reloaded again, so subsequent tests see the ambient
    values back. Phase 1.2 shipped-defaults pattern (see the phase1-2-3 brief).

    Caveats:
      * We patch ``dotenv.load_dotenv`` (not ``config.load_dotenv``) because reload re-runs
        ``from dotenv import load_dotenv``, which would otherwise re-bind the real loader.
      * Raw ``os.getenv`` knobs (e.g. R_LOCK_TIERS) are asserted via their reader fallback,
        not by raw string equality.
      * reload re-runs ``validate_dynamic_risk_pairs()``; from-config bindings in main /
        mt5_executor do NOT follow the reload, so only assert on the yielded module.
    """
    import importlib

    import dotenv

    import config as cfg

    knob_names = sorted({k for k in vars(cfg) if k.isupper()} - _ISOLATION_ENV_KNOBS)
    saved_env = {k: os.environ.get(k) for k in knob_names}
    saved_loader = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: None
    for k in knob_names:
        os.environ.pop(k, None)
    try:
        importlib.reload(cfg)
        yield cfg
    finally:
        dotenv.load_dotenv = saved_loader
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(cfg)
