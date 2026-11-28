"""Coverage for the manual GBP daily-loss cutoff and the new lot sizing.

- The hard daily-loss cutoff is seeded from DAILY_LOSS_LIMIT_GBP at startup, and a
  value the operator set from Saved Messages always wins over that seed.
- /losslimit manages the cutoff in account currency; /losslimit off switches it off.
- The high-risk lot cap scales proportionally with the balance and the shipped
  4-layer ladder is [0.02, 0.02, 0.02, 0.03] (0.09 lots).
"""
from __future__ import annotations

from datetime import datetime

import pytest

import config
import main
from mt5_executor import calculate_risk_guide_thresholds, compute_dynamic_layer_lots_for_balance
from state_manager import StateManager
from tests.test_copier_scenarios import MockTelethonEvent
from trade_ledger import TradeLedger


def _reset_main(tmp_path, tag: str, balance: float = 54.13):
    main.state_mgr = StateManager(tmp_path / f"{tag}_state.json")
    main.trade_ledger = TradeLedger(tmp_path / f"{tag}_ledger.jsonl")
    main._daily_loss_locked = False
    main._daily_loss_lock_notified_date = None
    main._daily_loss_terminate_requested = False
    main._daily_50pct_alert_date = None
    return main.state_mgr


# ---------------------------------------------------------------------------
# 1. The startup seed
# ---------------------------------------------------------------------------


def test_the_configured_cutoff_is_seeded_into_the_persisted_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed")

    assert main._seed_daily_loss_limit_from_config() == 30.0
    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is True
    assert cfg["limit_amount"] == 30.0
    assert cfg["source"] == "config"


def test_a_zero_knob_seeds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 0.0)
    _reset_main(tmp_path, "seed_off")

    main._seed_daily_loss_limit_from_config()
    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is False
    assert cfg["limit_amount"] == 0.0


def test_a_user_set_limit_survives_the_startup_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed_user")

    main.state_mgr.set_daily_loss_config(enabled=True, limit_amount=55.0, source="user")
    main._seed_daily_loss_limit_from_config()

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["limit_amount"] == 55.0
    assert cfg["source"] == "user"


def test_a_user_disabled_limit_is_not_re_seeded(tmp_path, monkeypatch):
    """`dailyloss off` must stay off across a restart."""
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed_user_off")

    main.state_mgr.set_daily_loss_config(enabled=False, limit_amount=0.0, source="user")
    main._seed_daily_loss_limit_from_config()

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is False
    assert cfg["limit_amount"] == 0.0


def test_the_seed_preserves_a_limit_that_already_fired_today(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed_triggered")

    main.state_mgr.set_daily_loss_config(
        enabled=True, limit_amount=30.0, triggered_date="2026-11-28", triggered_at=1234.0
    )
    main._seed_daily_loss_limit_from_config()

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["triggered_date"] == "2026-11-28"
    assert cfg["triggered_at"] == 1234.0
    assert cfg["limit_amount"] == 30.0


# ---------------------------------------------------------------------------
# 2. The Saved Messages controls
# ---------------------------------------------------------------------------


def test_the_gbp_argument_parser_accepts_the_currency_forms():
    assert main._parse_gbp_daily_loss_arg(["£30"]) == 30.0
    assert main._parse_gbp_daily_loss_arg(["30gbp"]) == 30.0
    assert main._parse_gbp_daily_loss_arg(["30", "gbp"]) == 30.0
    assert main._parse_gbp_daily_loss_arg(["£12.50"]) == 12.5
    # A percentage / unrelated argument is NOT a GBP request.
    assert main._parse_gbp_daily_loss_arg(["50"]) is None
    assert main._parse_gbp_daily_loss_arg(["off"]) is None
    assert main._parse_gbp_daily_loss_arg([]) is None


@pytest.mark.asyncio
async def test_losslimit_sets_the_manual_cutoff(tmp_path, monkeypatch):
    _reset_main(tmp_path, "losslimit_gbp")

    evt = MockTelethonEvent(msg_id=7001, text="/losslimit £30", chat_id="me")
    await main.handle_saved_messages_command(evt)

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is True
    assert cfg["limit_amount"] == 30.0
    assert cfg["source"] == "user"
    assert any("Hard Daily Loss Cutoff" in r and "30.00" in r for r in evt.replies)


@pytest.mark.asyncio
async def test_losslimit_off_switches_the_manual_cutoff_off(tmp_path, monkeypatch):
    _reset_main(tmp_path, "losslimit_off")
    main.state_mgr.set_daily_loss_config(enabled=True, limit_amount=30.0, source="user")

    evt = MockTelethonEvent(msg_id=7002, text="/losslimit off", chat_id="me")
    await main.handle_saved_messages_command(evt)

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is False
    assert cfg["limit_amount"] == 0.0
    assert cfg["source"] == "user"


@pytest.mark.asyncio
async def test_dailyloss_marks_the_limit_as_user_set(tmp_path, monkeypatch):
    _reset_main(tmp_path, "dailyloss_user")

    evt = MockTelethonEvent(msg_id=7003, text="dailyloss 40", chat_id="me")
    await main.handle_saved_messages_command(evt)

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is True
    assert cfg["limit_amount"] == 40.0
    assert cfg["source"] == "user"


def test_a_fired_limit_is_compared_against_the_broker_trading_day(tmp_path, monkeypatch):
    """The fired date comes from the BROKER, so the seed must use the broker day.

    A local-clock comparison would silently raise the limit on every restart
    that happens while the broker's trading day has rolled over.
    """
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed_broker_day")

    class _DisconnectedExecutor:
        """A disconnected executor: the trading day falls back to the LOCAL day."""

        dry_run = False
        _connected = False

        def get_server_date_str(self):
            return datetime.now().strftime("%Y-%m-%d")

    monkeypatch.setattr(main, "executor", _DisconnectedExecutor())
    main.state_mgr.set_daily_loss_config(
        enabled=True,
        limit_amount=10.0,
        triggered_date="2026-11-29",      # a BROKER day that differs from the local one
        triggered_at=1.0,
        source="config",
    )
    main._seed_daily_loss_limit_from_config()

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["limit_amount"] == 10.0, "a limit that fired on the broker day must not be raised"
    assert cfg["triggered_date"] == "2026-11-29"


def test_a_legacy_limit_without_a_source_marker_is_not_overwritten(tmp_path, monkeypatch):
    """A limit persisted by an older release belongs to the operator."""
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed_legacy")

    # No source marker: this is what the previous release wrote.
    main.state_mgr.set_daily_loss_config(enabled=True, limit_amount=10.0)

    main._seed_daily_loss_limit_from_config()

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["limit_amount"] == 10.0, "an operator-owned limit is never overwritten"
    assert str(cfg.get("source") or "") == "", "the ownership marker is not overwritten either"


def test_a_limit_that_already_fired_today_is_not_raised_by_the_seed(tmp_path, monkeypatch):
    """A fired limit must stay locked: raising it would clear the breach."""
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_GBP", 30.0)
    _reset_main(tmp_path, "seed_fired")

    main.state_mgr.set_daily_loss_config(
        enabled=True,
        limit_amount=10.0,
        triggered_date=main._trading_day_str(),
        triggered_at=1234.0,
        source="config",
    )
    main._seed_daily_loss_limit_from_config()

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["limit_amount"] == 10.0, "the fired amount must not be raised"
    assert cfg["triggered_date"] == main._trading_day_str()


@pytest.mark.asyncio
async def test_the_trigger_keeps_the_user_ownership_marker(tmp_path, monkeypatch):
    """Firing the limit must not lose WHO owns the value (source="user")."""
    _reset_main(tmp_path, "trigger_source")
    main.state_mgr.set_daily_loss_config(enabled=True, limit_amount=10.0, source="user")

    class _FakeExecutor:
        def get_realized_today_pnl(self):
            return -10.0

        def get_account_financials(self):
            return {
                "currency": "GBP",
                "balance": 54.13,
                "credit": 0.0,
                "equity": 44.13,
                "formatted": "£44.13 GBP",
            }

        def get_server_date_str(self):
            return "2026-11-28"

        def cancel_all_pending_orders(self, tickets=None):
            return []

        def get_open_market_tickets(self):
            return []

    main.executor = _FakeExecutor()
    monkeypatch.setattr(main, "_trading_day_str", lambda: "2026-11-28")

    async def _notify(_msg):
        return None

    monkeypatch.setattr(main, "notify_saved_messages", _notify)

    result = await main.check_and_enforce_daily_loss_limit(exit_on_trigger=False)
    assert result == "DAILY_LOSS_REACHED"

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["source"] == "user", "the operator's ownership must survive the trigger"
    assert cfg["limit_amount"] == 10.0
    assert cfg["triggered_date"] == "2026-11-28"


@pytest.mark.asyncio
async def test_a_percentage_losslimit_leaves_the_gbp_cutoff_untouched(tmp_path, monkeypatch):
    _reset_main(tmp_path, "losslimit_pct")
    main.state_mgr.set_daily_loss_config(enabled=True, limit_amount=30.0, source="user")

    evt = MockTelethonEvent(msg_id=7004, text="/losslimit 50", chat_id="me")
    await main.handle_saved_messages_command(evt)

    assert config.DAILY_LOSS_PAUSE_PCT == 50.0
    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is True
    assert cfg["limit_amount"] == 30.0, "a percentage must not change the GBP cutoff"


@pytest.mark.asyncio
async def test_a_zero_percentage_losslimit_does_not_disable_the_gbp_cutoff(tmp_path, monkeypatch):
    """"0" is a PERCENTAGE value, so it must never switch the hard cutoff off."""
    _reset_main(tmp_path, "losslimit_zero")
    main.state_mgr.set_daily_loss_config(enabled=True, limit_amount=30.0, source="user")

    evt = MockTelethonEvent(msg_id=7005, text="/losslimit 0", chat_id="me")
    await main.handle_saved_messages_command(evt)

    cfg = main.state_mgr.get_daily_loss_config()
    assert cfg["enabled"] is True
    assert cfg["limit_amount"] == 30.0, "the GBP cutoff must survive a percentage command"


# ---------------------------------------------------------------------------
# 3. Lot sizing
# ---------------------------------------------------------------------------


def test_the_high_risk_cap_scales_proportionally_with_the_balance():
    # £50 unlocks the full 0.09-lot ladder; the cap then scales with the balance.
    assert calculate_risk_guide_thresholds(50.0)["high_risk_lots"] == 0.09
    assert calculate_risk_guide_thresholds(54.13)["high_risk_lots"] == 0.09
    assert calculate_risk_guide_thresholds(100.0)["high_risk_lots"] == 0.18
    assert calculate_risk_guide_thresholds(200.0)["high_risk_lots"] == 0.36
    assert calculate_risk_guide_thresholds(500.0)["high_risk_lots"] == 0.90
    # Below £50 the cap scales down proportionally (never below one lot step).
    assert calculate_risk_guide_thresholds(25.0)["high_risk_lots"] == 0.04
    # The cap is always a multiple of the broker's 0.01 lot step.
    for bal in (50.0, 54.13, 77.77, 123.45, 999.99):
        cap = calculate_risk_guide_thresholds(bal)["high_risk_lots"]
        assert abs(round(cap / 0.01) * 0.01 - cap) < 1e-9


def test_the_shipped_ladder_is_the_0_09_lot_vector(shipped_config):
    assert config.BASE_LAYER_LOTS == [0.02, 0.02, 0.02, 0.03]
    assert round(sum(config.BASE_LAYER_LOTS), 2) == 0.09
    assert config.LAYER_LOTS["L4"] == 0.03


def test_the_dynamic_ladder_matches_the_shipped_one_at_50():
    assert compute_dynamic_layer_lots_for_balance(50.0, risk_mode="high") == [0.02, 0.02, 0.02, 0.03]


def test_the_configured_cutoff_default_is_thirty_pounds(shipped_config):
    assert config.DAILY_LOSS_LIMIT_GBP == 30.0
