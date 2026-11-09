import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch

from services.telegram_bot_listener import TelegramBotListener, MAIN_REPLY_KEYBOARD
from services.trader_registry import trader_registry
from core.config import config


@pytest.fixture
def mock_trading():
    svc = MagicMock()
    svc.active_positions = {
        "SOL-USD": {
            "direction": "SHORT",
            "entry_price": 87.42,
            "sl_price": 87.114,
            "tp_price": 84.797,
            "notional_usd": 104.90,
            "contracts": 1.2,
            "margin_used": 17.48,
            "planned_risk_usd": 1.6,
            "protection_state": "TIME_STOP_BE"
        }
    }
    svc.get_portfolio_summary = AsyncMock(return_value={
        "current_balance": 89.06,
        "available_margin": 71.58,
        "win_rate_pct": 65.0,
        "total_pnl_usd": 12.50,
        "total_r_multiple": 2.4,
        "win_count": 8,
        "loss_count": 4
    })
    svc.update_stop_loss_to_be = AsyncMock(return_value=True)
    svc.force_close_position = AsyncMock(return_value=(True, "ORDER_FILLED"))
    svc.entries_paused = False
    return svc


def test_main_reply_keyboard_structure():
    """Verify that MAIN_REPLY_KEYBOARD has exactly 4 buttons in 2 rows."""
    kb = MAIN_REPLY_KEYBOARD["keyboard"]
    assert len(kb) == 2
    assert len(kb[0]) == 2
    assert len(kb[1]) == 2
    assert kb[0][0]["text"] == "📊 Терминал"
    assert kb[0][1]["text"] == "💼 Позиции и SL"
    assert kb[1][0]["text"] == "🧠 AI-Радар"
    assert kb[1][1]["text"] == "🔗 Кошелек"


@pytest.mark.asyncio
async def test_start_attaches_main_reply_keyboard(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()

    await listener.handle_command("/start", sender_chat_id="111222333")
    assert listener._send_reply.call_count == 1
    _, kwargs = listener._send_reply.call_args
    assert kwargs.get("reply_markup") == MAIN_REPLY_KEYBOARD


@pytest.mark.asyncio
async def test_terminal_dashboard_and_entries_pause(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()
    admin_id = str(config.TELEGRAM_CHAT_ID or "12345")
    listener.chat_id = admin_id

    # Test /terminal command
    await listener.handle_command("/terminal", sender_chat_id=admin_id)
    assert listener._send_reply.call_count == 1
    text, kwargs = listener._send_reply.call_args[0][0], listener._send_reply.call_args[1]
    assert "AI TRADING TERMINAL | ДАШБОРД" in text
    assert "$89.06" in text
    assert "ВХОДЫ АКТИВНЫ" in text

    # Test pause entries callback
    await listener.handle_callback("cb_pause", "dash_pause_entries", admin_id, message_id=42)
    assert mock_trading.entries_paused is True
    assert listener._edit_reply.call_count == 1
    edit_text = listener._edit_reply.call_args[0][0]
    assert "ВХОДЫ НА ПАУЗЕ" in edit_text

    # Test resume entries callback
    await listener.handle_callback("cb_resume", "dash_resume_entries", admin_id, message_id=42)
    assert mock_trading.entries_paused is False
    assert listener._edit_reply.call_count == 2
    edit_text2 = listener._edit_reply.call_args[0][0]
    assert "ВХОДЫ АКТИВНЫ" in edit_text2


@pytest.mark.asyncio
async def test_positions_dashboard_be_and_close(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()
    admin_id = str(config.TELEGRAM_CHAT_ID or "12345")
    listener.chat_id = admin_id

    # Test /positions
    await listener.handle_command("/positions", sender_chat_id=admin_id)
    assert listener._send_reply.call_count == 1
    text = listener._send_reply.call_args[0][0]
    assert "SOL-USD" in text
    assert "SHORT" in text
    assert "Free Ride" in text or "Sentinel" in text

    # Test manual BE ask callback
    await listener.handle_callback("cb_be", "be_ask:SOL-USD", admin_id, message_id=43)
    mock_trading.update_stop_loss_to_be.assert_called_with("SOL-USD")

    # Test emergency close ask (2-step confirmation)
    await listener.handle_callback("cb_close", "close_ask:SOL-USD", admin_id, message_id=43)
    edit_text = listener._edit_reply.call_args[0][0]
    markup = listener._edit_reply.call_args[1]["reply_markup"]
    assert "ПОДТВЕРЖДЕНИЕ ЭКСТРЕННОГО ЗАКРЫТИЯ" in edit_text
    assert any("close_confirm:SOL-USD" in btn.get("callback_data", "") for row in markup["inline_keyboard"] for btn in row)

    # Test emergency close confirm
    await listener.handle_callback("cb_close_conf", "close_confirm:SOL-USD", admin_id, message_id=43)
    mock_trading.force_close_position.assert_called_with("SOL-USD", exit_reason="MANUAL_TG_CLOSE")


@pytest.mark.asyncio
async def test_radar_dashboard_and_subtabs(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()

    await listener.handle_command("/radar", sender_chat_id="777")
    assert listener._send_reply.call_count == 1
    text = listener._send_reply.call_args[0][0]
    assert "AI-РАДАР И СКАНЕР СИНДИКАТА" in text
    assert "BTC-USD" in text

    # Subtab radar_table
    await listener.handle_callback("cb_tab", "radar_table", "777", message_id=50)
    assert listener._edit_reply.call_count == 1
    assert "СВОДНАЯ ТАБЛИЦА 7 ПАР" in listener._edit_reply.call_args[0][0]


@pytest.mark.asyncio
async def test_wallet_watch_only_registration(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()
    guest_id = "555666777"

    # User clicks Watch-Only
    await listener.handle_callback("cb_wo", "wallet_watch_only", guest_id, message_id=60)
    assert listener._waiting_input.get(guest_id) == "watch_only_address"

    # User sends address
    valid_address = "0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa"
    await listener.handle_command(valid_address, sender_chat_id=guest_id)
    assert guest_id not in listener._waiting_input

    # Check registered in trader_registry
    trader = trader_registry.get_trader(guest_id)
    assert trader is not None
    assert trader.get("watch_only") is True
    assert trader.get("owner_address") == valid_address.lower()

    # Active subscribers should exclude watch_only
    subs = trader_registry.get_active_subscribers()
    assert not any(s.get("chat_id") == guest_id for s in subs)

    # Clean up test trader
    trader_registry.disconnect_trader(guest_id)

