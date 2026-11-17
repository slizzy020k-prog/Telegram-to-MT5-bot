import pytest
from parser import (
    parse_message,
    gate_1_check_forward_and_empty,
    gate_2_check_announcements_and_recaps,
    gate_3_classify_trade_management,
    gate_4_extract_signal,
    gate_5_validate_price_sanity,
)


def test_spec_test_1_standard_zone_signal():
    """
    Standard Zone Signal:
    "Buy xauusd 3895-3887 \n Tp 3900 \n Tp 3910 \n Sl 3875"
    Expected: XAUUSD BUY, top=3895.0, mid=3891.0, bottom=3887.0,
    final_tp=3899.50, sl=3875.0, sl_valid=True, proximity_close_price=3898.00
    """
    msg = "Buy xauusd 3895-3887 \n Tp 3900 \n Tp 3910 \n Sl 3875"
    res = parse_message(msg)

    assert res is not None
    assert res.event_type == "NEW_SIGNAL"
    assert res.symbol in ("XAUUSD", "XAUUSD.s")
    assert res.direction == "BUY"
    assert res.top == 3895.0
    assert res.mid == 3891.0
    assert res.bottom == 3887.0
    assert res.final_tp == 3899.50
    assert res.raw_tp1 == 3900.0
    assert res.proximity_close_price == 3898.00  # 3900 - 2.00 ($2.00 / 20 pips buffer)
    assert res.sl == 3875.0
    assert res.sl_valid is True
    assert res["max_orders"] == 4
    assert res.is_zone is True
    assert res.tp_levels == {
        "TP1": 3900.0,
        "TP2": 3910.0,
        "TP3": 3920.0,
        "TP4": 3930.0,
        "TP5": 3940.0,
    }
    assert res.zone_tiers == {"L1": 3895.0, "L2": 3892.33, "L3": 3889.67, "L4": 3887.0}


def test_xauusd_default_without_symbol_name():
    """
    XAUUSD-Only Default Execution:
    Valid directional verbs automatically map to XAUUSD even without 'XAUUSD' or 'GOLD' typed.
    """
    # 1. Plain Buy with zone
    res1 = parse_message("Buy 3895-3887 \n Tp 3900 \n Sl 3875")
    assert res1 is not None
    assert res1.symbol in ("XAUUSD", "XAUUSD.s")
    assert res1.direction == "BUY"
    assert res1.top == 3895.0
    assert res1.is_zone is True
    assert res1.max_orders == 4

    # 2. Buy limit with zone
    res2 = parse_message("Buy limit 3895-3887 \n Tp 3900 \n Sl 3875")
    assert res2 is not None
    assert res2.symbol in ("XAUUSD", "XAUUSD.s")
    assert res2.direction == "BUY"
    assert res2.is_zone is True

    # 3. Sell limit single price (v3.0 splits across 4 layers)
    res3 = parse_message("Sell limit 3920 \n Tp 3910 \n Sl 3930")
    assert res3 is not None
    assert res3.symbol in ("XAUUSD", "XAUUSD.s")
    assert res3.direction == "SELL"
    assert res3.single_price == 3920.0
    assert res3.is_zone is False
    assert res3.max_orders == 4
    assert res3.final_tp == 3910.50
    assert res3.proximity_close_price == 3912.00  # 3910 + 2.00 for SELL


def test_ignored_symbols_rejected():
    """Non-Gold tickers from IGNORED_SYMBOLS must be blocked immediately at Gate 2."""
    assert parse_message("Buy EURUSD 1.0850 \n TP 1.0900 \n SL 1.0800") is None
    assert parse_message("Sell GBPUSD 1.2500 \n TP 1.2400 \n SL 1.2600") is None
    assert parse_message("Buy US30 42000 \n TP 42200 \n SL 41800") is None
    assert parse_message("Sell NAS100 20500") is None
    assert parse_message("Buy BTCUSD 68000") is None


def test_spec_test_2_sl_typo_guard():
    """
    SL Typo Guard:
    'Sl 387' instead of 3875 is > 250 pips away and must be flagged as invalid.
    """
    msg = "Buy xauusd 3895-3887 \n Tp 3900 \n Sl 387"
    res = parse_message(msg)

    assert res is not None
    assert res.sl is None
    assert res.sl_valid is False
    assert res.top == 3895.0
    assert res.final_tp == 3899.50


def test_spec_test_3_initial_buy_now():
    """
    Initial 'Buy now' post without price:
    Must default to XAUUSD market entry awaiting subsequent zone edit.
    """
    msg = "Buy now"
    res = parse_message(msg)

    assert res is not None
    assert res.symbol in ("XAUUSD", "XAUUSD.s")
    assert res.direction == "BUY"
    assert res.is_instant_now is True
    assert res.entry_tiers is None
    assert res.sl is None
    assert res.sl_valid is False


def test_spec_test_4_risk_management_noise():
    msg = "£50 account High Risk : 0.05 Mid Risk : 0.02 Low Risk : 0.01"
    assert parse_message(msg) is None


def test_spec_test_5_victory_recap_noise():
    msg = "XAUUSD TP1 HIT +50 PIPS! Buy entry was 3895-3887"
    assert parse_message(msg) is None


def test_gate_1_forward_and_empty():
    assert parse_message("") is None
    assert parse_message("   ") is None
    assert parse_message(None) is None
    assert parse_message("Buy xauusd 3895-3887", is_forward=True) is None


def test_gate_3_trade_management():
    # Breakeven
    for word in ["be", "BE", "breakeven", "break even", "sl to entry", "risk free", "move sl"]:
        res = parse_message(word, message_id=501, reply_to_msg_id=100)
        assert res is not None
        assert res.event_type == "TRADE_MANAGEMENT"
        assert res.management_action == "ACTION_BREAKEVEN"
        assert res.reply_to_msg_id == 100

    # Reactivate ("Active")
    for word in ["Active", "active", "reactivate", "valid again"]:
        res = parse_message(word, message_id=502, reply_to_msg_id=100)
        assert res is not None
        assert res.event_type == "TRADE_MANAGEMENT"
        assert res.management_action == "ACTION_REACTIVATE"
        assert res.reply_to_msg_id == 100

    # Partial Close
    for word in ["close half", "partial", "secure", "book 50", "50%"]:
        res = parse_message(word, message_id=503, reply_to_msg_id=100)
        assert res is not None
        assert res.event_type == "TRADE_MANAGEMENT"
        assert res.management_action == "ACTION_PARTIAL_CLOSE"

    # Full Close
    for word in ["close all", "close now", "exit", "cut", "invalidate", "cancel", "delete limit"]:
        res = parse_message(word, message_id=504, reply_to_msg_id=100)
        assert res is not None
        assert res.event_type == "TRADE_MANAGEMENT"
        assert res.management_action == "ACTION_FULL_CLOSE"


def test_sell_zone_signal():
    msg = "Sell Gold 3925-3920 \n Tp 3900 \n Sl 3935"
    res = parse_message(msg)

    assert res is not None
    assert res.direction == "SELL"
    assert res.symbol in ("XAUUSD", "XAUUSD.s")
    assert res.top == 3925.0
    assert res.mid == 3922.5
    assert res.bottom == 3920.0
    assert res.sl == 3935.0
    assert res.sl_valid is True
    assert res.raw_tp1 == 3900.0
    assert res.final_tp == 3900.50  # 3900.0 + 0.50 for SELL
    assert res.proximity_close_price == 3902.00  # 3900 + 2.00 for SELL


def test_labeled_tps_out_of_order():
    msg = "Buy xauusd 3895-3887 \n Tp2 3915 \n Tp1 3905 \n Sl 3875"
    res = parse_message(msg)

    assert res is not None
    assert res.raw_tp1 == 3905.0
    assert res.final_tp == 3904.50
    assert res.proximity_close_price == 3903.00


def test_edit_bypasses_victory_recap():
    msg = "Buy xauusd 3895-3887, TP 3900, SL 3875 (booked 50 pips)"
    assert parse_message(msg, is_edit=False) is None

    res = parse_message(msg, is_edit=True)
    assert res is not None
    assert res.top == 3895.0
    assert res.final_tp == 3899.50


def test_lot_range_noise_not_treated_as_zone():
    msg = "Buy xauusd now \n Use 0.01-0.02 lot"
    res = parse_message(msg)
    assert res is not None
    assert res.is_instant_now is True
    assert res.entry_tiers is None
