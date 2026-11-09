import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from core.config import config
from services.telegram_bot_listener import TelegramBotListener, REPLY_KEYBOARD, BUTTON_COMMAND_MAP
from core.strategy_router import StrategyRouter


@pytest.fixture(autouse=True)
def reset_config_state():
    orig_antibot = getattr(config, "ANTI_BOT_MODE", False)
    orig_shadow = getattr(config, "SHADOW_MODE", True)
    orig_profile = getattr(config, "TRADING_PROFILE", "BALANCED")
    yield
    config.ANTI_BOT_MODE = orig_antibot
    config.SHADOW_MODE = orig_shadow
    config.TRADING_PROFILE = orig_profile


def test_reply_keyboard_and_button_command_mapping():
    """Verify that '🚀 Сканировать рынок' is present on ReplyKeyboard and mapped in BUTTON_COMMAND_MAP."""
    # 1. Check ReplyKeyboardMarkup structure
    keyboard = REPLY_KEYBOARD.get("keyboard", [])
    all_btn_texts = [btn["text"] for row in keyboard for btn in row]
    assert "🚀 Сканировать рынок" in all_btn_texts, "Button '🚀 Сканировать рынок' must be on ReplyKeyboard!"
    assert "📊 Статус & PnL" in all_btn_texts
    assert "📂 Позиции" in all_btn_texts
    assert "🎛 Пульт Режимов" in all_btn_texts
    assert "🦈 Анти-Бот (Вкл/Выкл)" in all_btn_texts
    assert "👻 Shadow Mode (Вкл/Выкл)" in all_btn_texts

    # 2. Check BUTTON_COMMAND_MAP variations
    assert BUTTON_COMMAND_MAP["🚀 Сканировать рынок"] == "/scan"
    assert BUTTON_COMMAND_MAP["🚀 сканировать рынок"] == "/scan"
    assert BUTTON_COMMAND_MAP["🚀 Сканировать"] == "/scan"
    assert BUTTON_COMMAND_MAP["сканировать"] == "/scan"
    assert BUTTON_COMMAND_MAP["scan"] == "/scan"


@pytest.mark.asyncio
async def test_manual_scan_trigger_from_text_command():
    """Verify that sending '/scan' or clicking '🚀 Сканировать рынок' invokes trigger_scan_callback."""
    scan_called = asyncio.Event()

    async def mock_scan_callback():
        scan_called.set()

    mock_trading_service = MagicMock()
    listener = TelegramBotListener(trading_service=mock_trading_service, trigger_scan_callback=mock_scan_callback)
    listener._send_reply = AsyncMock()

    # Trigger via text mapped from reply keyboard button
    await listener.handle_command("🚀 Сканировать рынок", sender_chat_id="6455910859")

    # Wait for the async scan background task
    await asyncio.wait_for(scan_called.wait(), timeout=2.0)
    assert scan_called.is_set(), "Scan callback must be triggered!"
    assert listener.is_scanning is False, "is_scanning must be False after scan completes"
    listener._send_reply.assert_called()


@pytest.mark.asyncio
async def test_manual_scan_trigger_from_inline_callback():
    """Verify that clicking the inline button 'cmd_scan' invokes trigger_scan_callback."""
    scan_called = asyncio.Event()

    async def mock_scan_callback():
        scan_called.set()

    mock_trading_service = MagicMock()
    listener = TelegramBotListener(trading_service=mock_trading_service, trigger_scan_callback=mock_scan_callback)
    listener.chat_id = "6455910859"
    listener.allowed_chat_ids = ["6455910859"]
    listener._send_reply = AsyncMock()

    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock) as mock_ans:
        await listener.handle_callback(
            callback_id="cb_123",
            callback_data="cmd_scan",
            chat_id="6455910859",
            message_id=999
        )
        mock_ans.assert_called_once()

    await asyncio.wait_for(scan_called.wait(), timeout=2.0)
    assert scan_called.is_set()
    assert listener.is_scanning is False


@pytest.mark.asyncio
async def test_manual_scan_matrix_all_four_modes():
    """
    Test manual scan across all 4 operational mode combinations:
    1. Standard + Shadow (ANTI_BOT_MODE=False, SHADOW_MODE=True)
    2. Predator + Shadow (ANTI_BOT_MODE=True, SHADOW_MODE=True)
    3. Standard + Live Real Money (ANTI_BOT_MODE=False, SHADOW_MODE=False)
    4. Predator + Live Real Money (ANTI_BOT_MODE=True, SHADOW_MODE=False)
    """
    symbol = "ETH-USD"
    market_data = {
        "price_data": {"current_price": 3000.0},
        "indicators": {
            "current_price": 3000.0,
            "bb_upper": 3050.0,
            "bb_lower": 2950.0,
            "bb_position_pct": 92.0,
            "bb_width_pct": 3.33,
            "rsi_14": 72.0,
            "adx_14": 22.0,
            "donchian_high": 3010.0,
            "donchian_low": 2900.0,
            "volume_spike_pct": 50.0,
            "atr_pct": 1.2,
            "liquidity_sweeps": ["upper_sweep"]
        },
        "multi_timeframe": {
            "trend_15m": "BULLISH",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH",
            "mtf_alignment": "FULL_ALIGNMENT_BULL"
        }
    }
    valid_reports = [
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"},
        {"agent_name": "Bull_Agent", "signal": "BULLISH"},
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"},
    ]
    macro_cache = {}
    detected_regime = "TRENDING_BULL"

    # --- Mode 1: Standard + Shadow ---
    config.ANTI_BOT_MODE = False
    config.SHADOW_MODE = True
    strat_std_shadow = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime=detected_regime,
        profile="BALANCED"
    )
    # In standard mode with FULL_ALIGNMENT_BULL and bullish technicals -> TREND_FOLLOWING LONG
    assert strat_std_shadow.strategy_mode == "TREND_FOLLOWING"
    assert strat_std_shadow.direction_bias == "LONG"
    assert config.SHADOW_MODE is True

    # --- Mode 2: Predator (Anti-Bot) + Shadow ---
    config.ANTI_BOT_MODE = True
    config.SHADOW_MODE = True
    strat_pred_shadow = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime=detected_regime,
        profile="BALANCED"
    )
    # In predator mode with bb_pos=92 and rsi=72 -> Bull Trap -> MEAN_REVERSION SHORT
    assert strat_pred_shadow.strategy_mode == "MEAN_REVERSION"
    assert strat_pred_shadow.direction_bias == "SHORT"
    assert "ANTI-BOT" in strat_pred_shadow.reasoning
    assert config.SHADOW_MODE is True

    # --- Mode 3: Standard + Live Real Money ---
    config.ANTI_BOT_MODE = False
    config.SHADOW_MODE = False
    strat_std_live = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime=detected_regime,
        profile="BALANCED"
    )
    assert strat_std_live.strategy_mode == "TREND_FOLLOWING"
    assert strat_std_live.direction_bias == "LONG"
    assert config.SHADOW_MODE is False

    # --- Mode 4: Predator (Anti-Bot) + Live Real Money ---
    config.ANTI_BOT_MODE = True
    config.SHADOW_MODE = False
    strat_pred_live = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime=detected_regime,
        profile="BALANCED"
    )
    assert strat_pred_live.strategy_mode == "MEAN_REVERSION"
    assert strat_pred_live.direction_bias == "SHORT"
    assert "ANTI-BOT" in strat_pred_live.reasoning
    assert config.SHADOW_MODE is False


@pytest.mark.asyncio
async def test_mode_toggle_buttons_affect_manual_scan_immediately():
    """Verify that toggling modes via inline callbacks immediately alters mode for manual scan."""
    mock_trading_service = MagicMock()
    listener = TelegramBotListener(trading_service=mock_trading_service)
    listener.chat_id = "6455910859"
    listener.allowed_chat_ids = ["6455910859"]
    listener._edit_message_text = AsyncMock()

    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock):
        # 1. Start: ANTI_BOT = False, SHADOW = True
        config.ANTI_BOT_MODE = False
        config.SHADOW_MODE = True

        # Toggle Predator Mode ON
        await listener.handle_callback("cb_1", "toggle_predator", "6455910859", message_id=101)
        assert config.ANTI_BOT_MODE is True, "ANTI_BOT_MODE should be True after toggle"

        # Toggle Shadow Mode OFF -> Live Real Money
        await listener.handle_callback("cb_2", "toggle_shadow", "6455910859", message_id=101)
        assert config.SHADOW_MODE is False, "SHADOW_MODE should be False after toggle (Live Mode)"

        # Toggle Predator Mode OFF -> Standard Mode
        await listener.handle_callback("cb_3", "toggle_predator", "6455910859", message_id=101)
        assert config.ANTI_BOT_MODE is False, "ANTI_BOT_MODE should be False after second toggle"

        # Toggle Shadow Mode ON -> Shadow Mode
        await listener.handle_callback("cb_4", "toggle_shadow", "6455910859", message_id=101)
        assert config.SHADOW_MODE is True, "SHADOW_MODE should be True after second toggle"


@pytest.mark.asyncio
async def test_concurrent_scan_protection_and_cleanup():
    """Verify that duplicate manual scan while a scan is running is rejected and state resets on failure."""
    scan_in_progress = asyncio.Event()
    continue_scan = asyncio.Event()

    async def long_running_scan():
        scan_in_progress.set()
        await continue_scan.wait()

    mock_trading_service = MagicMock()
    listener = TelegramBotListener(trading_service=mock_trading_service, trigger_scan_callback=long_running_scan)
    listener.chat_id = "6455910859"
    listener.allowed_chat_ids = ["6455910859"]
    listener._send_reply = AsyncMock()

    # Trigger first scan
    await listener.handle_command("/scan", sender_chat_id="6455910859")
    await asyncio.wait_for(scan_in_progress.wait(), timeout=1.0)
    assert listener.is_scanning is True

    # Attempt second scan concurrently
    await listener.handle_command("/scan", sender_chat_id="6455910859")
    # Verify rejection reply was sent
    sent_texts = [call.args[0] for call in listener._send_reply.call_args_list]
    assert any("уже выполняется" in t for t in sent_texts), "Duplicate scan must be warned!"

    # Unblock first scan
    continue_scan.set()
    await asyncio.sleep(0.05)
    assert listener.is_scanning is False

    # Now verify exception cleanup in wrapped_scan
    async def failing_scan():
        raise RuntimeError("RPC Connection Dropped")

    listener_failing = TelegramBotListener(trading_service=mock_trading_service, trigger_scan_callback=failing_scan)
    listener_failing.chat_id = "6455910859"
    listener_failing.allowed_chat_ids = ["6455910859"]
    listener_failing._send_reply = AsyncMock()

    await listener_failing.handle_command("/scan", sender_chat_id="6455910859")
    await asyncio.sleep(0.05)
    assert listener_failing.is_scanning is False, "is_scanning must reset to False even on scan exception!"
