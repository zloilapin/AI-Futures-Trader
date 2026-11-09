import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.telegram_bot_listener import TelegramBotListener, SHOWCASE_KEYBOARD
from core.config import config


@pytest.fixture
def mock_trading_service():
    service = MagicMock()
    service.active_positions = {
        "ETH-USD": {"direction": "SHORT", "entry_price": 2200.0, "notional_usd": 104.0}
    }
    service.get_portfolio_summary = AsyncMock(return_value={
        "current_balance": 89.50,
        "active_positions_count": 1,
        "spot_balance": 89.50,
        "available_margin": 75.0,
        "margin_used": 14.50,
        "unrealized_pnl": 1.20,
        "unrealized_roi_pct": 8.27,
        "realized_pnl_usd": 0.96,
        "total_pnl_usd": 2.16,
        "total_pnl_pct": 2.45,
        "win_rate_pct": 60.0,
        "win_count": 3,
        "loss_count": 2,
        "peak_balance": 89.50,
        "initial_balance": 87.34
    })
    return service


@pytest.mark.asyncio
async def test_non_admin_restricted_commands(mock_trading_service):
    """Ensure non-admin cannot run sensitive commands (/scan, /pnl, /panic) and gets security message."""
    listener = TelegramBotListener(trading_service=mock_trading_service)
    listener._send_reply = AsyncMock()

    guest_chat_id = "999999999"
    assert str(guest_chat_id) != str(config.TELEGRAM_CHAT_ID)

    # Attempt /scan
    await listener.handle_command("/scan", sender_chat_id=guest_chat_id)
    listener._send_reply.assert_called_once()
    msg, kwargs = listener._send_reply.call_args[0][0], listener._send_reply.call_args[1]
    assert "🔒 Command restricted to sub-account owner" in msg
    assert kwargs.get("reply_markup") == SHOWCASE_KEYBOARD
    assert kwargs.get("target_chat_id") == guest_chat_id

    # Attempt /panic
    listener._send_reply.reset_mock()
    await listener.handle_command("/panic", sender_chat_id=guest_chat_id)
    assert "🔒 Command restricted to sub-account owner" in listener._send_reply.call_args[0][0]

    # Attempt mapped button "🚀 Сканировать рынок"
    listener._send_reply.reset_mock()
    await listener.handle_command("🚀 Сканировать рынок", sender_chat_id=guest_chat_id)
    assert "🔒 Command restricted to sub-account owner" in listener._send_reply.call_args[0][0]


@pytest.mark.asyncio
async def test_non_admin_allowed_showcase_start(mock_trading_service):
    """Ensure non-admin receives the Showcase Mode card on /start."""
    listener = TelegramBotListener(trading_service=mock_trading_service)
    listener._send_reply = AsyncMock()

    guest_chat_id = "999999999"
    await listener.handle_command("/start", sender_chat_id=guest_chat_id)
    listener._send_reply.assert_called_once()
    msg = listener._send_reply.call_args[0][0]
    assert "AI-Futures-Trader on Nado DEX (Ink L2)" in msg
    assert "Sentinel Agent" in msg
    assert "explorer.inkonchain.com" in msg
    assert "Showcase Mode" in msg


@pytest.mark.asyncio
async def test_non_admin_allowed_showcase_regime_and_stats(mock_trading_service):
    """Ensure non-admin can query cached /regime and /stats without using LLM tokens."""
    listener = TelegramBotListener(trading_service=mock_trading_service)
    listener._send_reply = AsyncMock()

    guest_chat_id = "999999999"

    # /regime
    await listener.handle_command("/regime", sender_chat_id=guest_chat_id)
    msg_regime = listener._send_reply.call_args[0][0]
    assert "MARKET REGIME" in msg_regime
    assert "0 TOKENS" in msg_regime or "CACHED" in msg_regime

    # /stats
    listener._send_reply.reset_mock()
    await listener.handle_command("/stats", sender_chat_id=guest_chat_id)
    msg_stats = listener._send_reply.call_args[0][0]
    assert "STATS" in msg_stats
    assert "Public Showcase Mode" in msg_stats

    # /status
    listener._send_reply.reset_mock()
    await listener.handle_command("/status", sender_chat_id=guest_chat_id)
    msg_status = listener._send_reply.call_args[0][0]
    assert "PUBLIC SHOWCASE STATUS" in msg_status
    assert "Sentinel Agent" in msg_status
    assert "0 токенов LLM" in msg_status


@pytest.mark.asyncio
async def test_admin_allowed_all_commands(mock_trading_service):
    """Ensure admin can access all commands."""
    scan_cb = AsyncMock()
    listener = TelegramBotListener(trading_service=mock_trading_service, trigger_scan_callback=scan_cb)
    listener._send_reply = AsyncMock()

    admin_chat_id = str(config.TELEGRAM_CHAT_ID)
    await listener.handle_command("/status", sender_chat_id=admin_chat_id)
    msg_status = listener._send_reply.call_args[0][0]
    assert "AI TRADER STATUS | NADO DEX" in msg_status
    assert "Текущий баланс" in msg_status


def test_start_button_in_all_keyboards():
    from services.telegram_bot_listener import SHOWCASE_KEYBOARD, TRADER_KEYBOARD, REPLY_KEYBOARD
    assert any("Start" in btn.get("text", "") for row in SHOWCASE_KEYBOARD["keyboard"] for btn in row)
    assert any("Start" in btn.get("text", "") for row in TRADER_KEYBOARD["keyboard"] for btn in row)
    assert any("Start" in btn.get("text", "") for row in REPLY_KEYBOARD["keyboard"] for btn in row)


@pytest.mark.asyncio
async def test_start_command_contains_ai_models_and_architecture(mock_trading_service):
    """Ensure /start and mapped start button output the models used and presentation card."""
    listener = TelegramBotListener(trading_service=mock_trading_service)
    sent_messages = []
    listener._send_reply = AsyncMock(side_effect=lambda text, **kwargs: sent_messages.append(text))

    # Test with button text "▶️ Start"
    await listener.handle_command("▶️ Start", sender_chat_id="123456789")
    assert len(sent_messages) == 1
    card = sent_messages[0]

    # Verify model names
    assert "Gemini 2.5 Flash Lite" in card
    assert "DeepSeek-V3" in card
    assert "Llama 3.3 70B" in card
    assert "Sentinel Agent" in card
    assert "Nado DEX" in card
    assert "LinkSigner" in card
    assert "ptn3l1u" in card

    # Test with "старт"
    await listener.handle_command("старт", sender_chat_id="123456789")
    assert len(sent_messages) == 2
    assert "Gemini 2.5 Flash Lite" in sent_messages[1]


