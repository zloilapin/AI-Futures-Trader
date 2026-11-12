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
