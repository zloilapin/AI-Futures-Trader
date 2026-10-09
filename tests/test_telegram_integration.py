import os
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from services.telegram_service import TelegramService
from agents.telegram_agent import TelegramAgent
from services.telegram_bot_listener import TelegramBotListener
from core.config import config


@pytest.fixture
def mock_session_ctx():
    def _create_mock_session(status_codes=[200], custom_texts=None):
        responses = []
        for idx, code in enumerate(status_codes):
            resp = MagicMock()
            resp.status = code
            if custom_texts and idx < len(custom_texts):
                err_text = custom_texts[idx]
            elif code == 400:
                err_text = "Bad Request: can't parse entities in message"
            elif code != 200:
                err_text = "error text"
            else:
                err_text = "ok"
            resp.text = AsyncMock(return_value=err_text)
            resp.json = AsyncMock(return_value={"parameters": {"retry_after": 0.01}} if code == 429 else {"ok": True})
            ctx = AsyncMock()
            ctx.__aenter__.return_value = resp
            ctx.__aexit__.return_value = None
            responses.append(ctx)

        session = MagicMock()
        session.post.side_effect = responses
        session.get.side_effect = responses
        return session
    return _create_mock_session


# -------------------------------------------------------------
# 1. TelegramService Credential & Truncation Safety
# -------------------------------------------------------------

def test_telegram_credential_validation():
    assert TelegramService._is_valid_credential("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11") is True
    assert TelegramService._is_valid_credential("-1001234567890") is True
    assert TelegramService._is_valid_credential(None) is False
    assert TelegramService._is_valid_credential("") is False
    assert TelegramService._is_valid_credential("   ") is False
    assert TelegramService._is_valid_credential("your_telegram_bot_token") is False
    assert TelegramService._is_valid_credential("PLACEHOLDER_TOKEN") is False


def test_telegram_message_truncation():
    short_msg = "Hello World"
    assert TelegramService._safe_truncate(short_msg) == short_msg

    long_msg = "X" * 5000
    truncated = TelegramService._safe_truncate(long_msg)
    assert len(truncated) <= 4000
    assert truncated.endswith("...[TRUNCATED]")
    assert TelegramService._safe_truncate("") == ""


# -------------------------------------------------------------
# 2. TelegramService Dispatch, Fallback & Rate Limiting
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_send_message_success(mock_session_ctx):
    tg = TelegramService()
    tg.bot_token = "valid_token_123"
    tg.chat_id = "valid_chat_456"
    tg.api_url = f"https://api.telegram.org/bot{tg.bot_token}/sendMessage"

    mock_session = mock_session_ctx([200])
    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        res = await tg.send_message("Test alert")
        assert res is True
        assert mock_session.post.call_count == 1
        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["text"] == "Test alert"
        assert call_kwargs["json"]["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_telegram_send_message_markdown_fallback_on_400(mock_session_ctx):
    tg = TelegramService()
    tg.bot_token = "valid_token_123"
    tg.chat_id = "valid_chat_456"
    tg.api_url = f"https://api.telegram.org/bot{tg.bot_token}/sendMessage"

    # First call returns 400 (Bad Request: can't parse entities), second returns 200 (plain text)
    mock_session = mock_session_ctx([400, 200])
    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        res = await tg.send_message("Faulty *markdown [unclosed")
        assert res is True
        assert mock_session.post.call_count == 2
        # First call had parse_mode, second call stripped parse_mode
        first_payload = mock_session.post.call_args_list[0][1]["json"]
        second_payload = mock_session.post.call_args_list[1][1]["json"]
        assert "parse_mode" in first_payload
        assert "parse_mode" not in second_payload


@pytest.mark.asyncio
async def test_telegram_send_message_rate_limit_retry(mock_session_ctx):
    tg = TelegramService()
    tg.bot_token = "valid_token_123"
    tg.chat_id = "valid_chat_456"
    tg.api_url = f"https://api.telegram.org/bot{tg.bot_token}/sendMessage"

    # First returns 429, retry returns 200
    mock_session = mock_session_ctx([429, 200])
    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        res = await tg.send_message("Fast alert")
        assert res is True
        assert mock_session.post.call_count == 2


@pytest.mark.asyncio
async def test_telegram_send_message_placeholder_guard():
    tg = TelegramService()
    tg.bot_token = "your_telegram_bot_token"
    tg.chat_id = "your_telegram_chat_id"
    # Should safely return False without network attempt
    res = await tg.send_message("Alert")
    assert res is False


# -------------------------------------------------------------
# 3. TelegramAgent Signal Formatting & Defensive Parsing
# -------------------------------------------------------------

def test_telegram_agent_format_signal_reasoning_fallback():
    agent = TelegramAgent(logger=MagicMock(), llm_client=MagicMock())
    
    # CEO output has 'reasoning' instead of 'reasoning_en'
    trade_data = {
        "symbol": "BTC-USD",
        "ceo_verdict": {
            "decision": "LONG",
            "conviction": 85,
            "reasoning": "Strong bullish breakout verified across MTF."
        },
        "risk_verdict": {
            "entry_price": 60000.0,
            "stop_loss_price": 59000.0,
            "take_profit_price": 62500.0,
            "risk_reward_ratio": 2.5,
            "notional_size_usd": 500.0,
            "position_size_pct": 5.0,
            "stop_loss_pct": 1.67,
            "take_profit_pct": 4.17
        }
    }

    signal_text = agent.format_signal(trade_data)
    assert "BTC-USD" in signal_text
    assert "Strong bullish breakout verified across MTF" in signal_text
    assert "60,000.00" in signal_text
    assert "59,000.00" in signal_text


def test_telegram_agent_format_signal_defensive_types():
    agent = TelegramAgent(logger=MagicMock(), llm_client=MagicMock())
    
    trade_data = {
        "symbol": "ETH-USD",
        "ceo_verdict": {
            "decision": "SHORT",
            "conviction": "invalid_conviction",
            "reasoning": "A" * 1000  # Extra long reasoning
        },
        "risk_verdict": {
            "entry_price": "3000.5",
            "stop_loss_price": None,
            "take_profit_price": "bad_float",
            "risk_reward_ratio": "2.0",
            "notional_size_usd": 150.0,
            "position_size_pct": 2.0,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 3.0
        }
    }

    signal_text = agent.format_signal(trade_data)
    assert "ETH-USD" in signal_text
    assert "3,000.50" in signal_text
    # Long reasoning should be safely bounded (< 600 chars in snippet)
    assert len(signal_text) < 2000


# -------------------------------------------------------------
# 4. TelegramBotListener Command Input Safety
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_bot_listener_empty_and_unknown_commands():
    trading_mock = MagicMock()
    listener = TelegramBotListener(trading_service=trading_mock)
    listener.send_url = "http://fake.api"
    listener.chat_id = "12345"

    with patch.object(listener, "_send_reply", AsyncMock()) as mock_reply:
        # None and empty commands
        await listener.handle_command("")
        await listener.handle_command("   ")
        assert mock_reply.call_count == 0

        # /help command
        await listener.handle_command("/help")
        assert mock_reply.call_count == 1
        assert "ГЛАВНОЕ МЕНЮ БОТА" in mock_reply.call_args[0][0]


@pytest.mark.asyncio
async def test_telegram_bot_listener_deposit_withdraw_ledger():
    trading_mock = MagicMock()
    trading_mock.adjust_ledger = MagicMock()
    trading_mock.reset_ledger = MagicMock()

    listener = TelegramBotListener(trading_service=trading_mock)
    listener.send_url = "http://fake.api"
    listener.chat_id = "12345"

    with patch.object(listener, "_send_reply", AsyncMock()) as mock_reply:
        # Invalid /deposit argument
        await listener.handle_command("/deposit not_a_number")
        assert "Некорректный формат суммы" in mock_reply.call_args[0][0]

        # Negative /deposit argument
        await listener.handle_command("/deposit -50")
        assert "> 0" in mock_reply.call_args[0][0]

        # Valid /deposit
        await listener.handle_command("/deposit 150.50")
        trading_mock.adjust_ledger.assert_called_with(150.50)
        assert "150.50" in mock_reply.call_args[0][0]

        # Valid /withdraw
        await listener.handle_command("/withdraw 50.00")
        trading_mock.adjust_ledger.assert_called_with(-50.00)

        # /reset_ledger
        await listener.handle_command("/reset_ledger")
        trading_mock.reset_ledger.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_bot_listener_positions_command():
    trading_mock = MagicMock()
    trading_mock.active_positions = {
        "BTC-USD": {
            "direction": "LONG",
            "is_virtual": False,
            "entry_price": 62000.0,
            "notional_usd": 1000.0,
            "leverage": 5.0,
            "margin_usd": 200.0
        }
    }

    listener = TelegramBotListener(trading_service=trading_mock)
    listener.send_url = "http://fake.api"
    listener.chat_id = "12345"

    with patch.object(listener, "_send_reply", AsyncMock()) as mock_reply:
        await listener.handle_command("/positions")
        assert mock_reply.call_count == 1
        reply_text = mock_reply.call_args[0][0]
        assert "BTC-USD" in reply_text
        assert "LONG" in reply_text
        assert "62,000.00" in reply_text


# -------------------------------------------------------------
# 5. TelegramBotListener Callback Query Safety
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_bot_listener_setrisk_callback():
    trading_mock = MagicMock()
    listener = TelegramBotListener(trading_service=trading_mock)
    listener.send_url = "http://fake.api"
    listener.chat_id = "12345"

    with patch("services.telegram_service.TelegramService.answer_callback_query", AsyncMock()) as mock_ans, \
         patch("services.telegram_service.TelegramService.send_message", AsyncMock()) as mock_send:
        # Valid profile change
        await listener.handle_callback("cb_1", "setrisk_AGGRESSIVE", "12345")
        assert config.TRADING_PROFILE == "AGGRESSIVE"
        mock_ans.assert_called_with("cb_1", "Профиль AGGRESSIVE установлен ✅")

        # Invalid profile change
        await listener.handle_callback("cb_2", "setrisk_EXTREME_GAMBLE", "12345")
        mock_ans.assert_called_with("cb_2", "Неизвестный профиль: EXTREME_GAMBLE")


@pytest.mark.asyncio
async def test_telegram_bot_listener_expired_trade_callback():
    trading_mock = MagicMock()
    # Trade created 600 seconds ago (> 300s timeout)
    trading_mock.pending_trades = {
        "trade_1": {
            "symbol": "BTC-USD",
            "created_at": time.time() - 600,
            "direction": "LONG"
        }
    }
    listener = TelegramBotListener(trading_service=trading_mock)
    listener.chat_id = "12345"

    with patch("services.telegram_service.TelegramService.answer_callback_query", AsyncMock()) as mock_ans, \
         patch("services.telegram_service.TelegramService.send_message", AsyncMock()) as mock_send:
        await listener.handle_callback("cb_trade", "approve_trade_1", "12345")
        mock_ans.assert_called_with("cb_trade", "Сделка просрочена (>5 мин) ⏳")
        assert "trade_1" not in trading_mock.pending_trades


# -------------------------------------------------------------
# 6. Release Gate Extra Tests: NaN/Inf, Auth, Non-Parse 400, Memory Uniqueness
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_send_message_400_non_markdown_no_fallback(mock_session_ctx):
    tg = TelegramService()
    tg.bot_token = "valid_token_123"
    tg.chat_id = "valid_chat_456"
    tg.api_url = f"https://api.telegram.org/bot{tg.bot_token}/sendMessage"

    # 400 error because chat was deleted, NOT a markdown parse error
    mock_session = mock_session_ctx([400], custom_texts=["Bad Request: chat not found"])
    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        res = await tg.send_message("Normal text")
        assert res is False
        # Should NOT blindly retry without markdown, exactly 1 call
        assert mock_session.post.call_count == 1


@pytest.mark.asyncio
async def test_telegram_bot_listener_deposit_withdraw_nan_inf_overflow():
    trading_mock = MagicMock()
    trading_mock.adjust_ledger = MagicMock()
    listener = TelegramBotListener(trading_service=trading_mock)
    listener.send_url = "http://fake.api"
    listener.chat_id = "12345"

    with patch.object(listener, "_send_reply", AsyncMock()) as mock_reply:
        # Test NaN
        await listener.handle_command("/deposit nan")
        assert "конечным положительным числом" in mock_reply.call_args[0][0]
        trading_mock.adjust_ledger.assert_not_called()

        # Test Inf
        await listener.handle_command("/deposit inf")
        assert "конечным положительным числом" in mock_reply.call_args[0][0]
        trading_mock.adjust_ledger.assert_not_called()

        # Test -Inf
        await listener.handle_command("/withdraw -inf")
        assert "конечным положительным числом" in mock_reply.call_args[0][0]
        trading_mock.adjust_ledger.assert_not_called()

        # Test Overflow (> 1B)
        await listener.handle_command("/deposit 2000000000")
        assert "конечным положительным числом" in mock_reply.call_args[0][0]
        trading_mock.adjust_ledger.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_bot_listener_authorization_guard():
    trading_mock = MagicMock()
    trading_mock.adjust_ledger = MagicMock()
    listener = TelegramBotListener(trading_service=trading_mock)
    listener.send_url = "http://fake.api"
    listener.chat_id = "12345"

    with patch.object(listener, "_send_reply", AsyncMock()) as mock_reply:
        # Command from unauthorized chat
        await listener.handle_command("/deposit 500", sender_chat_id="99999")
        trading_mock.adjust_ledger.assert_not_called()
        assert mock_reply.call_count == 0

    with patch("services.telegram_service.TelegramService.answer_callback_query", AsyncMock()) as mock_ans:
        # Callback from unauthorized chat
        await listener.handle_callback("cb_hack", "setrisk_AGGRESSIVE", "99999")
        mock_ans.assert_called_with("cb_hack", "⛔ Доступ запрещен")


def test_memory_manager_cross_process_and_restart_uniqueness(tmp_path):
    from agents.memory_manager import MemoryManager
    mock_logger = MagicMock()
    storage_dir = str(tmp_path / "memory_unique")

    manager1 = MemoryManager(mock_logger, storage_path=storage_dir)
    # Simulate first process
    with patch("os.getpid", return_value=1001):
        for i in range(10):
            manager1.save_cycle({"symbol": "BTC-USD", "iteration": i})

    # Simulate restart with new instance and different PID (simulating process restart)
    MemoryManager._seq = 0  # reset in-memory counter
    manager2 = MemoryManager(mock_logger, storage_path=storage_dir)
    with patch("os.getpid", return_value=1002):
        for i in range(10):
            manager2.save_cycle({"symbol": "BTC-USD", "iteration": i + 10})

    files = [f for f in os.listdir(manager2.storage_path) if f.startswith('cycle_') and f.endswith('.json')]
    # All 20 cycles must be strictly preserved without collisions
    assert len(files) == 20

