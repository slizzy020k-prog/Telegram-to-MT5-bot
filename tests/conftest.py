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
