"""
Tests for Multi-User Copy-Trading Architecture & Risk Profiles
==============================================================
Verifies:
1. Individual R-sizing and TP target calculation across Conservative ($100),
   Balanced ($500), and Aggressive ($2,000) accounts.
2. Exchange $100.00 CLOB min_size gate: safe $104 bump vs rejection without error 2094.
3. Target equity achievement and daily drawdown stop-crane auto-pause.
4. Late Join policy protection (skipping trades older than 10m / drifted > 0.15R).
5. Isolated subscriber position management & 2-step confirmation in Telegram UI.
"""

import os
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from eth_account import Account

from services.trader_registry import TraderRegistry, RISK_PROFILES
from services.copy_trading_dispatcher import CopyTradingDispatcher, SubscriberPositionsStore
from tools.nado_link_signer_helper import sign_link_signer_payload


@pytest.fixture
def temp_env(tmp_path):
    traders_file = str(tmp_path / "connected_traders.json")
    positions_file = str(tmp_path / "subscriber_positions.json")
    registry = TraderRegistry(storage_path=traders_file)
    store = SubscriberPositionsStore(storage_path=positions_file)
    return registry, store, traders_file, positions_file


def _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=500.0, **kwargs):
    owner = Account.create()
    session = registry.prepare_pending_session(chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=100)
    success, _ = registry.register_trader(
        chat_id=chat_id,
        owner_address=owner.address,
        subaccount_name="default",
        signer_address=session["signer_address"],
        signer_private_key=session["signer_private_key"],
        signature=sig,
        nonce=100
    )
    assert success
    registry.set_trader_risk_profile(chat_id, risk_mode)
    if kwargs:
        registry.update_trader_risk_settings(chat_id, **kwargs)
    traders = registry.load_traders()
    traders[chat_id]["_mock_equity"] = mock_equity
    registry.save_traders(traders)
    return traders[chat_id]


# -----------------------------------------------------------------------------
# 1. Individual R-sizing and TP targets across 3 accounts
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multi_user_sizing_and_tp_targets(temp_env, monkeypatch):
    registry, store, _, _ = temp_env

    # Setup 3 users: Conservative ($100), Balanced ($500), Aggressive ($2,000)
    user_a = _register_mock_trader(registry, "101", risk_mode="conservative", mock_equity=100.0)
    user_b = _register_mock_trader(registry, "102", risk_mode="balanced", mock_equity=500.0)
    user_c = _register_mock_trader(registry, "103", risk_mode="aggressive", mock_equity=2000.0)

    # Signal params: SOL-USD LONG at $140.00, SL at $137.20 (dist = $2.80, 2.0%)
    entry_price = 140.0
    master_sl = 137.20
    sl_dist_pct = (entry_price - master_sl) / entry_price  # 0.02 (2%)

    # Test pure sizing logic for User B ($500 balanced):
    # Risk = 500 * 1.6% = $8.00. Notional = $8.00 / 0.02 = $400.00
    can_b, notional_b, risk_b, status_b = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=user_b,
        entry_price=entry_price,
        master_sl=master_sl,
        subaccount_equity=500.0,
        available_margin=450.0
    )
    assert can_b is True
    assert round(notional_b, 2) == 400.0
    assert round(risk_b, 2) == 8.0
    assert status_b == "SIZED_OK"

    # Test pure sizing logic for User C ($2,000 aggressive):
    # Risk = 2000 * 2.6% = $52.00. Notional = $52.00 / 0.02 = $2,600.00
    can_c, notional_c, risk_c, status_c = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=user_c,
        entry_price=entry_price,
        master_sl=master_sl,
        subaccount_equity=2000.0,
        available_margin=1800.0
    )
    assert can_c is True
    assert round(notional_c, 2) == 2600.0
    assert round(risk_c, 2) == 52.0

    # Test pure sizing for User A ($100 conservative, 0.9% risk = $0.90):
    # Raw notional = $0.90 / 0.02 = $45.00 (< 103.50).
    # Risk at $104 bump = 104 * 0.02 = $2.08.
    # Max allowed bump risk = $0.90 * 1.45 = $1.305.
    # $2.08 > $1.305 => Safe bump fails, skips trade cleanly!
    can_a, notional_a, risk_a, status_a = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=user_a,
        entry_price=entry_price,
        master_sl=master_sl,
        subaccount_equity=100.0,
        available_margin=90.0
    )
    assert can_a is False
    assert status_a == "BELOW_EXCHANGE_MIN"

    # Verify TP Targets:
    # Conservative TP target: 1.8R => 140 + 2.80 * 1.8 = 145.04
    # Balanced TP target: 3.0R => 140 + 2.80 * 3.0 = 148.40
    # Aggressive TP target: 3.8R => 140 + 2.80 * 3.8 = 150.64
    sl_dist = entry_price - master_sl
    tp_a = entry_price + (sl_dist * user_a["tp_r_target"])
    tp_b = entry_price + (sl_dist * user_b["tp_r_target"])
    tp_c = entry_price + (sl_dist * user_c["tp_r_target"])

    assert round(tp_a, 2) == 145.04
    assert round(tp_b, 2) == 148.40
    assert round(tp_c, 2) == 150.64


# -----------------------------------------------------------------------------
# 2. Exchange $100.00 CLOB min_size gate: Safe $104 bump
# -----------------------------------------------------------------------------
def test_safe_104_bump_success():
    # Subscriber with $300 balance, 1.6% risk = $4.80.
    # Tight stop: entry 100, SL 95 (5% SL distance)
    # Target notional = $4.80 / 0.05 = $96.00 (< 103.50)
    # Risk at $104 = 104 * 0.05 = $5.20.
    # Max allowed bump risk = 4.80 * 1.45 = $6.96.
    # $5.20 <= $6.96 => safely bumped to $104.00!
    sub = {
        "risk_per_trade_pct": 1.6,
        "max_leverage": 8.0
    }
    can_trade, notional, risk, status = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=sub,
        entry_price=100.0,
        master_sl=95.0,
        subaccount_equity=300.0,
        available_margin=100.0
    )
    assert can_trade is True
    assert notional == 104.0
    assert status == "BUMPED_TO_MIN_NOTIONAL"


# -----------------------------------------------------------------------------
# 3. Target equity & daily drawdown auto-pause
# -----------------------------------------------------------------------------
def test_target_equity_and_daily_drawdown_auto_pause(temp_env):
    registry, _, _, _ = temp_env
    chat_id = "555"
    _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=400.0, target_equity_usd=500.0, max_daily_drawdown_pct=4.0)

    # Initial check at $400: neither triggered
    r1 = registry.check_daily_drawdown_and_targets(chat_id, current_equity=400.0)
    assert r1["target_equity_reached"] is False
    assert r1["daily_drawdown_paused"] is False

    # Equity grows to $510 => target reached!
    r2 = registry.check_daily_drawdown_and_targets(chat_id, current_equity=510.0)
    assert r2["target_equity_reached"] is True
    assert r2["just_reached_target"] is True

    # Next check: stays reached, but just_reached is False
    r3 = registry.check_daily_drawdown_and_targets(chat_id, current_equity=520.0)
    assert r3["target_equity_reached"] is True
    assert r3["just_reached_target"] is False

    # Test Drawdown on a new user starting at $1,000
    dd_chat = "666"
    _register_mock_trader(registry, dd_chat, risk_mode="balanced", mock_equity=1000.0, max_daily_drawdown_pct=4.0)
    # Day start equity sets to 1000
    registry.check_daily_drawdown_and_targets(dd_chat, current_equity=1000.0)

    # Drawdown of 2% ($980) => OK
    r_ok = registry.check_daily_drawdown_and_targets(dd_chat, current_equity=980.0)
    assert r_ok["daily_drawdown_paused"] is False

    # Drawdown of 5% ($950) => Limit hit (5% >= 4%)!
    r_hit = registry.check_daily_drawdown_and_targets(dd_chat, current_equity=950.0)
    assert r_hit["daily_drawdown_paused"] is True
    assert r_hit["just_triggered_dd"] is True
    assert r_hit["drawdown_pct"] == 5.0


# -----------------------------------------------------------------------------
# 4. Late Join policy protection
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_late_join_policy_protection(temp_env, monkeypatch):
    registry, store, _, _ = temp_env
    chat_id = "777"
    _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=1000.0, late_join_policy="next_signal_only")

    mock_trading = MagicMock()
    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()

    from core.config import config
    monkeypatch.setattr(config, "COPY_TRADING_ENABLED", True)

    dispatcher = CopyTradingDispatcher(master_trading_service=mock_trading, telegram_service=mock_tg)
    dispatcher.positions_store = store
    monkeypatch.setattr("services.copy_trading_dispatcher.trader_registry", registry)

    # Simulate Master trade opened 15 minutes ago (900 seconds)
    old_master_open = time.time() - 900.0
    entry_price = 100.0
    master_sl = 95.0 # sl_dist = 5.0

    # User connected before master trade was opened
    registry.update_trader_risk_settings(chat_id, connected_at=old_master_open - 60.0)

    # Market price has moved to 103.0 (drift = 3.0 => 0.60R > 0.15R)
    mock_trading.market_data_service.get_last_price.return_value = 103.0

    await dispatcher.dispatch_copy_trade_open(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=entry_price,
        master_sl=master_sl,
        master_tp=115.0,
        master_open_time=old_master_open
    )

    # Trade must be skipped, no position registered in store
    pos = store.get_user_position(chat_id, "SOL-USD")
    assert pos is None
    # Notification sent explaining the skip
    assert mock_tg.send_message.call_count == 1
    call_text = mock_tg.send_message.call_args[1]["text"]
    assert "Защита от опоздания" in call_text or "Пропуск" in call_text


# -----------------------------------------------------------------------------
# 5. Telegram UI: Personal Subscriber Position & 2-Step Confirmation
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_telegram_subscriber_position_and_close(temp_env, monkeypatch):
    registry, store, _, _ = temp_env
    chat_id = "888"
    _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=800.0)

    # Pre-populate an open subscriber position
    store.set_user_position(chat_id, "ETH-USD", {
        "symbol": "ETH-USD",
        "direction": "SHORT",
        "entry_price": 2500.0,
        "notional_usd": 400.0,
        "contracts": 0.16,
        "sl_price": 2550.0,
        "tp_price": 2350.0,
        "initial_sl": 2550.0,
        "initial_tp": 2350.0,
        "r_dist": 50.0,
        "planned_risk_usd": 8.0,
        "tp_r_target": 3.0,
        "be_trigger_r": 0.75,
        "trail_activation_r": 1.35,
        "protection_state": "NATIVE_SL_CONFIRMED",
        "risk_mode": "balanced",
        "leverage": 8.0
    })

    from services.telegram_bot_listener import TelegramBotListener
    mock_trading = MagicMock()
    mock_trading.active_positions = {} # Master has no position
    mock_trading.market_data_service.get_last_price.return_value = 2480.0 # Floating profit (+20)

    bot = TelegramBotListener(trading_service=mock_trading)

    # Patch dispatcher's store in listener
    with patch("services.copy_trading_dispatcher.copy_trading_dispatcher.positions_store", store):
        text, markup = await bot._get_positions_payload(chat_id=chat_id, is_admin=False)
        assert "МОИ СКОПИРОВАННЫЕ ПОЗИЦИИ" in text
        assert "ETH-USD" in text
        assert "Take-Profit" in text and "$2,350.00" in text

        # Check inline buttons for personal position
        buttons = markup["inline_keyboard"]
        flat_buttons = [b for row in buttons for b in row]
        button_callbacks = [b["callback_data"] for b in flat_buttons]

        assert "sub_be:ETH-USD" in button_callbacks
        assert "sub_close_ask:ETH-USD" in button_callbacks

        # Test Manual BE transfer
        from services.copy_trading_dispatcher import copy_trading_dispatcher
        copy_trading_dispatcher.positions_store = store
        succ_be, msg_be = await copy_trading_dispatcher.move_subscriber_sl_to_be(chat_id, "ETH-USD")
        assert succ_be is True
        updated_pos = store.get_user_position(chat_id, "ETH-USD")
        assert updated_pos["protection_state"] == "BREAK_EVEN"

        # Test Manual Subscriber Market Close (leaves Master positions untouched)
        succ_close, msg_close = await copy_trading_dispatcher.close_subscriber_position(chat_id, "ETH-USD")
        assert succ_close is True
        assert store.get_user_position(chat_id, "ETH-USD") is None


# -----------------------------------------------------------------------------
# 6. Telegram Risk Menu Payload & 1-Click Profile Switching
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_telegram_risk_menu_payload(temp_env):
    registry, store, _, _ = temp_env
    chat_id = "999"
    _register_mock_trader(registry, chat_id, risk_mode="balanced")

    from services.telegram_bot_listener import TelegramBotListener
    mock_trading = MagicMock()
    bot = TelegramBotListener(trading_service=mock_trading)

    with patch("services.trader_registry.trader_registry", registry):
        text, markup = bot._get_risk_menu_payload(chat_id)
        assert "ПРОФИЛЬ РИСКА И ЦЕЛИ КОПИ-ТРЕЙДИНГА" in text
        assert "Сбалансированный" in text

        # Test switching to aggressive
        registry.set_trader_risk_profile(chat_id, "aggressive")
        t_agg = registry.get_trader(chat_id)
        assert t_agg["risk_mode"] == "aggressive"
        assert t_agg["risk_per_trade_pct"] == 2.6
        assert t_agg["max_slots"] == 3
        assert t_agg["tp_r_target"] == 3.8
