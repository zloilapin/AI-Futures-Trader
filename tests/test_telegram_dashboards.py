import pytest
import asyncio
import os
import json
import time
from unittest.mock import MagicMock
from services.telegram_bot_listener import TelegramBotListener
from core.state_store import StateStore

@pytest.mark.asyncio
async def test_debates_payload_fallback_when_empty():
    mock_service = MagicMock()
    listener = TelegramBotListener(trading_service=mock_service)

    text, markup = await listener._get_debates_payload()
    assert "СИНДИКАТ ИИ | ДЕБАТЫ И КОНСЕНСУС CEO" in text
    assert "Данные последнего цикла еще формируются" in text
    assert "inline_keyboard" in markup

@pytest.mark.asyncio
async def test_debates_payload_reads_latest_snapshot():
    mock_service = MagicMock()
    listener = TelegramBotListener(trading_service=mock_service)

    # Write a test snapshot to isolated test state
    snap_path = StateStore._normalize_path("data/memory/decision_snapshots.jsonl")
    os.makedirs(os.path.dirname(snap_path), exist_ok=True)
    test_snapshot = {
        "timestamp": time.time(),
        "iso_time": "2026-11-12T08:00:00+00:00",
        "symbol": "ETH-USD",
        "stage": "EXECUTION",
        "final_action": "ENTER",
        "router": {"strategy_mode": "TREND_FOLLOWING"},
        "ceo": {
            "decision": "SHORT",
            "conviction": 89,
            "directional_confidence": 89,
            "trade_action": "ENTER",
            "reasoning": "Bearish trend confirmed on all timeframes.",
            "risk_breakdown": {"total": -20}
        }
    }
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(test_snapshot) + "\n")

    text, markup = await listener._get_debates_payload()
    assert "СИНДИКАТ ИИ | ДЕБАТЫ И КОНСЕНСУС CEO" in text
    assert "ETH-USD" in text
    assert "SHORT" in text
    assert "89%" in text
    assert "-20 pts" in text
    assert "Bearish trend confirmed on all timeframes." in text
    assert "DeepSeek-V3" in text
    assert "inline_keyboard" in markup
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "dash_debates" in callbacks
    assert "dash_main" in callbacks

@pytest.mark.asyncio
async def test_radar_payload_structure():
    mock_service = MagicMock()
    mock_service.active_positions = {
        "ETH-USD": {
            "symbol": "ETH-USD",
            "side": "SHORT",
            "entry_price": 2047.97,
            "stop_loss": 2065.24
        },
        "ZEC-USD": {
            "symbol": "ZEC-USD",
            "side": "SHORT",
            "entry_price": 986.58,
            "stop_loss": 999.48
        }
    }
    listener = TelegramBotListener(trading_service=mock_service)

    text, markup = await listener._get_radar_payload()
    
    assert "ARANEA AI | КВАНТОВЫЙ РАДАР РЫНКА" in text
    assert "ОТКРЫТЫЕ ПОЗИЦИИ В РЫНКЕ:" in text
    assert "ETH" in text
    assert "ZEC" in text
    assert "МОНИТОРИНГ ВСЕЛЕННОЙ АКТИВОВ:" in text
    assert "СВОДКА СИНДИКАТА:" in text
    assert "Быстрый импульсный радар:" in text
    
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "radar_table" in callbacks
    assert "radar_pullback" in callbacks
    assert "radar_verdicts" in callbacks
    assert "radar_whales" in callbacks
    assert "dash_main" in callbacks


@pytest.mark.asyncio
async def test_close_confirm_triggers_force_close_and_dispatches_notification(monkeypatch):
    from unittest.mock import AsyncMock

    mock_service = MagicMock()
    mock_service.force_close_position = AsyncMock(return_value=(True, 0.7157))
    mock_service.active_positions = {}
    mock_service._last_close_info = {
        "HYPE-USD": {
            "symbol": "HYPE-USD",
            "direction": "SHORT",
            "entry_price": 69.748,
            "exit_price": 69.252,
            "closed_qty": 1.6,
            "gross_pnl_usd": 0.7936,
            "entry_fee_usd": 0.0391,
            "exit_fee_usd": 0.0779,
            "total_fee_usd": 0.0779,
            "net_pnl_usd": 0.7157,
            "realized_r": 0.35,
            "planned_risk_usd": 2.06,
            "pnl_source": "EXACT",
            "close_reason": "MANUAL_TG_CLOSE",
            "triggered_by": "MANUAL_TG_CLOSE",
            "strategy_mode": "TREND_FOLLOWING",
            "is_anti_bot": False
        }
    }

    listener = TelegramBotListener(trading_service=mock_service)
    listener.allowed_chat_ids = ["12345"]

    mock_send_message = AsyncMock(return_value=True)
    mock_broadcast = AsyncMock(return_value=True)
    mock_answer_cb = AsyncMock(return_value=True)
    listener._edit_reply = AsyncMock(return_value=True)

    from services.telegram_service import TelegramService
    monkeypatch.setattr(TelegramService, "send_message", mock_send_message)
    monkeypatch.setattr(TelegramService, "broadcast_to_channel", mock_broadcast)
    monkeypatch.setattr(TelegramService, "answer_callback_query", mock_answer_cb)

    await listener.handle_callback(
        callback_id="cb_999",
        callback_data="close_confirm:HYPE-USD",
        chat_id="12345",
        message_id=456
    )

    # 1. Verification: force_close_position invoked with symbol and exit_reason
    mock_service.force_close_position.assert_awaited_once_with("HYPE-USD", exit_reason="MANUAL_TG_CLOSE")

    # 2. Verification: send_message and broadcast_to_channel were dispatched
    assert mock_send_message.await_count >= 1
    sent_text = mock_send_message.await_args_list[0].args[0]
    assert "ПОЗИЦИЯ ЗАКРЫТА: HYPE-USD (SHORT)" in sent_text
    assert "69.7480" in sent_text
    assert "+0.7157$" in sent_text
    assert "MANUAL_TG_CLOSE" in sent_text
    assert "EXACT" in sent_text

    mock_broadcast.assert_awaited_once()
    broadcast_text = mock_broadcast.await_args[0][0]
    assert "HYPE-USD" in broadcast_text

