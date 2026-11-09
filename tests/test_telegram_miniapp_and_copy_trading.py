import pytest
import asyncio
import json
import time
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from eth_account import Account

from services.trader_registry import TraderRegistry
from tools.nado_link_signer_helper import sign_link_signer_payload, generate_session_key
from services.copy_trading_dispatcher import CopyTradingDispatcher
from services.telegram_bot_listener import TelegramBotListener


@pytest.fixture
def temp_registry():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tmp_path = tf.name
    reg = TraderRegistry(storage_path=tmp_path)
    yield reg
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def test_trader_registration_valid_signature(temp_registry):
    owner = Account.create()
    chat_id = "987654321"
    session = temp_registry.prepare_pending_session(chat_id)
    signer_addr = session["signer_address"]
    nonce = int(time.time() * 1000)

    signature = sign_link_signer_payload(
        owner_account=owner,
        signer_address=signer_addr,
        subaccount_name="default",
        nonce=nonce
    )

    ok, msg = temp_registry.register_trader(
        chat_id=chat_id,
        owner_address=owner.address,
        subaccount_name="default",
        signature=signature,
        nonce=nonce
    )

    assert ok is True
    assert "успешно подключен" in msg

    trader = temp_registry.get_trader(chat_id)
    assert trader is not None
    assert trader["owner_address"] == owner.address.lower()
    assert trader["signer_address"] == signer_addr.lower()
    assert trader["copy_enabled"] is True

    # Test toggle
    ok_tog, new_st = temp_registry.toggle_copy(chat_id)
    assert ok_tog is True
    assert new_st is False
    assert temp_registry.get_trader(chat_id)["copy_enabled"] is False

    # Test disconnect
    assert temp_registry.disconnect_trader(chat_id) is True
    assert temp_registry.get_trader(chat_id) is None


def test_trader_registration_invalid_signature(temp_registry):
    owner = Account.create()
    imposter = Account.create()
    chat_id = "55555555"
    session = temp_registry.prepare_pending_session(chat_id)
    signer_addr = session["signer_address"]
    nonce = int(time.time() * 1000)

    # Forged: signed by imposter instead of owner
    forged_sig = sign_link_signer_payload(
        owner_account=imposter,
        signer_address=signer_addr,
        subaccount_name="default",
        nonce=nonce
    )

    ok, msg = temp_registry.register_trader(
        chat_id=chat_id,
        owner_address=owner.address,
        subaccount_name="default",
        signature=forged_sig,
        nonce=nonce
    )

    assert ok is False
    assert "не совпадает с адресом владельца" in msg
    assert temp_registry.get_trader(chat_id) is None


@pytest.mark.asyncio
async def test_telegram_miniapp_web_app_data_parsing(temp_registry, monkeypatch):
    import services.telegram_bot_listener as tbl_mod
    import services.trader_registry as tr_mod
    monkeypatch.setattr(tbl_mod, "trader_registry", temp_registry)
    monkeypatch.setattr(tr_mod, "trader_registry", temp_registry)

    listener = TelegramBotListener(trading_service=MagicMock())
    replies = []
    listener._send_reply = AsyncMock(side_effect=lambda text, **kwargs: replies.append((text, kwargs)))

    owner = Account.create()
    chat_id = "11223344"
    session = temp_registry.prepare_pending_session(chat_id)
    signer_addr = session["signer_address"]
    nonce = int(time.time() * 1000)

    signature = sign_link_signer_payload(
        owner_account=owner,
        signer_address=signer_addr,
        subaccount_name="default",
        nonce=nonce
    )

    payload_json = json.dumps({
        "owner": owner.address,
        "subaccount": "default",
        "signer": signer_addr,
        "signature": signature,
        "nonce": nonce
    })

    await listener.handle_web_app_data(payload_json, sender_chat_id=chat_id)

    assert len(replies) == 2
    welcome_text, kwargs0 = replies[0]
    assert "Кошелек успешно подключен" in welcome_text
    assert owner.address in welcome_text
    assert temp_registry.get_trader(chat_id) is not None

    wizard_step1_text, kwargs1 = replies[1]
    assert "МАСТЕР НАСТРОЙКИ ТОРГОВЛИ (ШАГ 1/4)" in wizard_step1_text
    assert "Консервативный" in wizard_step1_text
    assert "Сбалансированный" in wizard_step1_text


@pytest.mark.asyncio
async def test_telegram_fallback_link_confirm(temp_registry, monkeypatch):
    import services.telegram_bot_listener as tbl_mod
    import services.trader_registry as tr_mod
    monkeypatch.setattr(tbl_mod, "trader_registry", temp_registry)
    monkeypatch.setattr(tr_mod, "trader_registry", temp_registry)

    listener = TelegramBotListener(trading_service=MagicMock())
    replies = []
    listener._send_reply = AsyncMock(side_effect=lambda text, **kwargs: replies.append((text, kwargs)))

    owner = Account.create()
    chat_id = "99887766"
    session = temp_registry.prepare_pending_session(chat_id)
    signer_addr = session["signer_address"]
    nonce = int(time.time() * 1000)

    signature = sign_link_signer_payload(
        owner_account=owner,
        signer_address=signer_addr,
        subaccount_name="default",
        nonce=nonce
    )

    cmd_text = f"/link_confirm {owner.address} {nonce} {signature}"
    await listener.handle_command(cmd_text, sender_chat_id=chat_id)

    assert len(replies) >= 1
    reply_text, kwargs = replies[0]
    assert "Кошелек успешно подключен" in reply_text
    assert temp_registry.get_trader(chat_id) is not None


@pytest.mark.asyncio
async def test_subscriber_commands_lifecycle(temp_registry, monkeypatch):
    import services.telegram_bot_listener as tbl_mod
    import services.trader_registry as tr_mod
    monkeypatch.setattr(tbl_mod, "trader_registry", temp_registry)
    monkeypatch.setattr(tr_mod, "trader_registry", temp_registry)

    mock_trading = MagicMock()
    mock_trading.client = None
    listener = TelegramBotListener(trading_service=mock_trading)
    replies = []
    listener._send_reply = AsyncMock(side_effect=lambda text, **kwargs: replies.append((text, kwargs)))

    owner = Account.create()
    chat_id = "77665544"

    # 1. Unregistered /my_balance
    await listener.handle_command("/my_balance", sender_chat_id=chat_id)
    assert "еще не подключен" in replies[-1][0]

    # 2. Register
    session = temp_registry.prepare_pending_session(chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=123)
    temp_registry.register_trader(chat_id, owner.address, "default", session["signer_address"], session["signer_private_key"], sig, 123)

    # 3. Registered /my_balance
    await listener.handle_command("/my_balance", sender_chat_id=chat_id)
    assert "ВАШ СУБ-АККАУНТ НА NADO DEX" in replies[-1][0]

    # 4. Toggle
    await listener.handle_command("/toggle", sender_chat_id=chat_id)
    assert "ПОСТАВЛЕНО НА ПАУЗУ" in replies[-1][0]

    # 5. Disconnect
    await listener.handle_command("/disconnect", sender_chat_id=chat_id)
    assert "Кошелек успешно отключен" in replies[-1][0]
    assert temp_registry.get_trader(chat_id) is None


@pytest.mark.asyncio
async def test_copy_trading_dispatcher_min_notional_gate(temp_registry, monkeypatch):
    import services.copy_trading_dispatcher as ctd_mod
    monkeypatch.setattr(ctd_mod, "trader_registry", temp_registry)

    # Enable copy trading
    from core.config import config
    monkeypatch.setattr(config, "COPY_TRADING_ENABLED", True)

    mock_trading = MagicMock()
    # Mock subscriber subaccount with tiny balance: $12.50 assets (< $35.00 min threshold)
    mock_summary = MagicMock()
    health_mock = MagicMock()
    health_mock.assets = int(12.50 * 1e18)
    health_mock.health = int(10.00 * 1e18)
    mock_summary.healths = [health_mock]
    mock_trading.client.subaccount.get_engine_subaccount_summary.return_value = mock_summary

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()

    dispatcher = CopyTradingDispatcher(master_trading_service=mock_trading, telegram_service=mock_tg)

    owner = Account.create()
    chat_id = "12121212"
    session = temp_registry.prepare_pending_session(chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=123)
    temp_registry.register_trader(chat_id, owner.address, "default", session["signer_address"], session["signer_private_key"], sig, 123)

    # Dispatch trade
    await dispatcher.dispatch_open_position(
        symbol="SOL-USD",
        direction="SHORT",
        entry_price=87.42,
        limit_price=87.20,
        notional_usd=104.90,
        tp_price=84.79,
        sl_price=87.11,
        leverage=6.0
    )

    # Check notification sent to subscriber about insufficient balance
    mock_tg.send_message.assert_called_once()
    sent_text = mock_tg.send_message.call_args[1]["text"]
    assert "Пропуск сделки" in sent_text
    assert "12.50" in sent_text
    assert "100.00" in sent_text


@pytest.mark.asyncio
async def test_copy_trading_dispatcher_isolation_on_error(temp_registry, monkeypatch):
    import services.copy_trading_dispatcher as ctd_mod
    monkeypatch.setattr(ctd_mod, "trader_registry", temp_registry)

    from core.config import config
    monkeypatch.setattr(config, "COPY_TRADING_ENABLED", True)

    mock_trading = MagicMock()
    # Simulate RPC explosion when querying subscriber
    mock_trading.client.subaccount.get_engine_subaccount_summary.side_effect = RuntimeError("Sequencer RPC Timeout")

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()

    dispatcher = CopyTradingDispatcher(master_trading_service=mock_trading, telegram_service=mock_tg)

    owner = Account.create()
    chat_id = "34343434"
    session = temp_registry.prepare_pending_session(chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=123)
    temp_registry.register_trader(chat_id, owner.address, "default", session["signer_address"], session["signer_private_key"], sig, 123)

    # Should not raise exception
    try:
        await dispatcher.dispatch_open_position(
            symbol="ETH-USD",
            direction="SHORT",
            entry_price=2133.65,
            limit_price=2130.00,
            notional_usd=104.55,
            tp_price=2069.52,
            sl_price=2151.89,
            leverage=6.0
        )
    except Exception as e:
        pytest.fail(f"Dispatcher allowed subscriber error to bubble up: {e}")


def test_nado_referral_configuration():
    from core.config import config
    assert config.NADO_REFERRAL_CODE == "ptn3l1u"
    assert config.NADO_REFERRAL_URL == "https://app.nado.xyz?join=ptn3l1u"


@pytest.mark.asyncio
async def test_telegram_bot_referral_command():
    mock_trading = MagicMock()
    listener = TelegramBotListener(trading_service=mock_trading)
    sent_replies = []
    listener._send_reply = AsyncMock(side_effect=lambda text, **kwargs: sent_replies.append(text))

    # Showcase guest calls /ref
    await listener.handle_command("/ref", sender_chat_id="777888999")
    assert len(sent_replies) == 1
    assert "https://app.nado.xyz?join=ptn3l1u" in sent_replies[0]
    assert "ptn3l1u" in sent_replies[0]
    assert "скидку 10%" in sent_replies[0]

    # Showcase guest calls /referral
    await listener.handle_command("/referral", sender_chat_id="777888999")
    assert len(sent_replies) == 2
    assert "https://app.nado.xyz?join=ptn3l1u" in sent_replies[1]


@pytest.mark.asyncio
async def test_telegram_bot_connect_includes_referral():
    mock_trading = MagicMock()
    listener = TelegramBotListener(trading_service=mock_trading)
    sent_replies = []
    sent_markups = []
    listener._send_reply = AsyncMock(side_effect=lambda text, reply_markup=None, **kwargs: (sent_replies.append(text), sent_markups.append(reply_markup)))

    await listener.handle_command("/connect", sender_chat_id="666555444")
    assert len(sent_replies) == 1
    assert "https://app.nado.xyz?join=ptn3l1u" in sent_replies[0]
    assert "Шаг 1. Активация скидки 10% на комиссии" in sent_replies[0]

    markup = sent_markups[0]
    assert markup is not None
    # Check web_app URL carries ?join=ptn3l1u
    web_app_url = markup["keyboard"][0][0]["web_app"]["url"]
    assert "join=ptn3l1u" in web_app_url


def test_miniapp_html_contains_referral():
    html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "tg_miniapp_link_signer.html")
    assert os.path.exists(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "https://app.nado.xyz?join=ptn3l1u" in content
    assert "ptn3l1u" in content
    assert "Скидка 10% на комиссии" in content


@pytest.mark.asyncio
async def test_settings_command_unconnected_and_connected(temp_registry, monkeypatch):
    import services.telegram_bot_listener as tbl_mod
    import services.trader_registry as tr_mod
    monkeypatch.setattr(tbl_mod, "trader_registry", temp_registry)
    monkeypatch.setattr(tr_mod, "trader_registry", temp_registry)

    listener = TelegramBotListener(trading_service=MagicMock())
    replies = []
    listener._send_reply = AsyncMock(side_effect=lambda text, **kwargs: replies.append((text, kwargs)))

    guest_chat_id = "554433221"

    # 1. Unconnected user calling /settings
    await listener.handle_command("/settings", sender_chat_id=guest_chat_id)
    assert len(replies) == 1
    guest_text = replies[-1][0]
    assert "Сначала подключите ваш EVM-кошелек" in guest_text
    assert "ПЕРСОНАЛЬНЫЕ НАСТРОЙКИ ТОРГОВЛИ" in guest_text

    # 2. Register trader
    owner = Account.create()
    session = temp_registry.prepare_pending_session(guest_chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=999)
    temp_registry.register_trader(guest_chat_id, owner.address, "default", session["signer_address"], session["signer_private_key"], sig, 999)

    # 3. Connected user calling /settings
    await listener.handle_command("/settings", sender_chat_id=guest_chat_id)
    assert len(replies) == 2
    trader_text = replies[-1][0]
    assert "ВАШИ ИНДИВИДУАЛЬНЫЕ НАСТРОЙКИ ТОРГОВЛИ" in trader_text
    assert "Профиль риска" in trader_text
    assert "Анти-Бот" in trader_text
    markup = replies[-1][1].get("reply_markup")
    assert markup is not None
    assert any("wiz_start" in btn.get("callback_data", "") for row in markup["inline_keyboard"] for btn in row)


@pytest.mark.asyncio
async def test_personal_trading_wizard_full_flow(temp_registry, monkeypatch):
    import services.telegram_bot_listener as tbl_mod
    import services.trader_registry as tr_mod
    monkeypatch.setattr(tbl_mod, "trader_registry", temp_registry)
    monkeypatch.setattr(tr_mod, "trader_registry", temp_registry)

    listener = TelegramBotListener(trading_service=MagicMock())
    edited_messages = []
    listener._edit_message_text = AsyncMock(side_effect=lambda text, reply_markup=None, message_id=None, chat_id=None: edited_messages.append((text, reply_markup, message_id, chat_id)))

    chat_id = "44332211"
    owner = Account.create()
    session = temp_registry.prepare_pending_session(chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=555)
    temp_registry.register_trader(chat_id, owner.address, "default", session["signer_address"], session["signer_private_key"], sig, 555)

    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock):
        # Step 1: Start wizard
        await listener.handle_callback("cb_1", "wiz_start", chat_id, message_id=101)
        assert len(edited_messages) == 1
        assert "ШАГ 1/4" in edited_messages[-1][0]

        # Step 2: Choose Aggressive risk (10x)
        await listener.handle_callback("cb_2", "wiz_risk_AGGRESSIVE", chat_id, message_id=101)
        assert len(edited_messages) == 2
        assert "ШАГ 2/4" in edited_messages[-1][0]

        # Step 3: Choose 3 concurrent positions
        await listener.handle_callback("cb_3", "wiz_pos_3", chat_id, message_id=101)
        assert len(edited_messages) == 3
        assert "ШАГ 3/4" in edited_messages[-1][0]

        # Step 4: Choose Single-Tier TP
        await listener.handle_callback("cb_4", "wiz_tp_SINGLE_TIER", chat_id, message_id=101)
        assert len(edited_messages) == 4
        assert "ШАГ 4/4" in edited_messages[-1][0]

        # Step 5: Disable Anti-bot mode -> Finish wizard and save
        await listener.handle_callback("cb_5", "wiz_ab_false", chat_id, message_id=101)
        assert len(edited_messages) == 5
        summary_text = edited_messages[-1][0]
        assert "ВАШИ ИНДИВИДУАЛЬНЫЕ НАСТРОЙКИ СОХРАНЕНЫ" in summary_text
        assert "AGGRESSIVE" in summary_text
        assert "10x" in summary_text
        assert "3 одновременно" in summary_text
        assert "Single-Tier" in summary_text
        assert "ВЫКЛЮЧЕН" in summary_text

        # Verify saved in registry
        saved = temp_registry.get_trader_settings(chat_id)
        assert saved["risk_profile"] == "AGGRESSIVE"
        assert saved["leverage"] == 10.0
        assert saved["max_concurrent_positions"] == 3
        assert saved["tp_mode"] == "SINGLE_TIER"
        assert saved["anti_bot_enabled"] is False


