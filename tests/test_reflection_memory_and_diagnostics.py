import os
import json
import asyncio
import tempfile
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime

from agents.reflector_agent import ReflectorAgent
from agents.memory_manager import MemoryManager
from core.diagnostics import DiagnosticTracker
from core.state_store import StateStore
from core.logger import TradeLogger
from core.llm_client import LLMClient

@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger

@pytest.fixture
def mock_llm_client():
    client = MagicMock(spec=LLMClient)
    client.model_name = "test-model"
    client.generate_json = AsyncMock()
    return client

def test_reflector_get_lessons_symbol_prioritization(tmp_path, mock_logger, mock_llm_client):
    """Verifies that ReflectorAgent prioritizes lessons for the requested symbol and normalizes tickers."""
    lessons_file = str(tmp_path / "lessons.json")
    lessons_data = [
        {"symbol": "ETH-USD", "trade_outcome": "LOSS", "actionable_rule": "ETH rule 1"},
        {"symbol": "BTC-USD", "trade_outcome": "LOSS", "actionable_rule": "BTC specific rule"},
        {"symbol": "SOL-USD", "trade_outcome": "LOSS", "actionable_rule": "SOL rule 1"},
        {"symbol": "BTC", "trade_outcome": "LOSS", "actionable_rule": "BTC base rule"},
        {"symbol": "DOGE-USD", "trade_outcome": "WIN", "actionable_rule": "Ignore winning rule"},
    ]
    with open(lessons_file, "w", encoding="utf-8") as f:
        json.dump(lessons_data, f)

    agent = ReflectorAgent(mock_logger, mock_llm_client, lessons_file=lessons_file)
    
    # Query for BTC-USD: should return BTC rules first (BTC base rule, BTC specific rule), then others
    btc_lessons = agent.get_lessons(limit=3, symbol="BTC-USD")
    assert len(btc_lessons) == 3
    assert btc_lessons[0] == "BTC base rule"
    assert btc_lessons[1] == "BTC specific rule"
    assert btc_lessons[2] in ["SOL rule 1", "ETH rule 1"]

    # Query for SOL/USD (slash format) -> matches SOL-USD
    sol_lessons = agent.get_lessons(limit=2, symbol="SOL/USD")
    assert sol_lessons[0] == "SOL rule 1"

def test_reflector_deduplication_and_loss_filtering(tmp_path, mock_logger, mock_llm_client):
    """Verifies that ReflectorAgent deduplicates identical lessons and strictly ignores WIN outcomes."""
    lessons_file = str(tmp_path / "lessons.json")
    lessons_data = [
        {"symbol": "BTC-USD", "trade_outcome": "LOSS", "actionable_rule": "Wait for 1H candle close"},
        {"symbol": "ETH-USD", "trade_outcome": "WIN", "actionable_rule": "Winning trade advice"},
        {"symbol": "SOL-USD", "trade_outcome": "LOSS", "actionable_rule": "Wait for 1H candle close"}, # duplicate rule
        {"symbol": "SOL-USD", "trade_outcome": "LOSS", "actionable_rule": "Avoid high funding trades"}
    ]
    with open(lessons_file, "w", encoding="utf-8") as f:
        json.dump(lessons_data, f)

    agent = ReflectorAgent(mock_logger, mock_llm_client, lessons_file=lessons_file)
    lessons = agent.get_lessons(limit=5)
    
    # "Wait for 1H candle close" appears only once; "Winning trade advice" is excluded
    assert "Winning trade advice" not in lessons
    assert lessons.count("Wait for 1H candle close") == 1
    assert "Avoid high funding trades" in lessons

def test_reflector_clean_context_prunes_heavy_arrays():
    """Verifies that ReflectorAgent._clean_context_for_llm strips heavy candle arrays and order book depth."""
    heavy_context = {
        "ohlcv": [(1700000000 + i * 60, 100, 105, 95, 102, 1000) for i in range(200)],
        "candles": [1, 2, 3],
        "order_book": {
            "bids": [[100, 10]] * 50,
            "asks": [[101, 10]] * 50,
            "best_bid": 100.0,
            "best_ask": 101.0,
            "spread_pct": 0.01,
            "imbalance": 0.15
        },
        "multi_timeframe": {
            "trend_15m": "BULLISH",
            "trend_1h": "NEUTRAL",
            "trend_4h": "BEARISH",
            "mtf_alignment": "MIXED_CHOP",
            "candles_20": [1, 2, 3]
        },
        "indicators": {"rsi_14": 55.0}
    }

    clean = ReflectorAgent._clean_context_for_llm(heavy_context)
    assert "ohlcv" not in clean
    assert "candles" not in clean
    assert "bids" not in clean["order_book"]
    assert clean["order_book"]["best_bid"] == 100.0
    assert clean["multi_timeframe"]["trend_15m"] == "BULLISH"
    assert clean["indicators"]["rsi_14"] == 55.0

def test_reflector_save_and_rotation_limit(tmp_path, mock_logger, mock_llm_client):
    """Verifies that ReflectorAgent._save_lesson limits history to the latest 100 records."""
    lessons_file = str(tmp_path / "lessons.json")
    agent = ReflectorAgent(mock_logger, mock_llm_client, lessons_file=lessons_file)

    for i in range(105):
        agent._save_lesson({
            "symbol": f"SYM_{i}",
            "trade_outcome": "LOSS",
            "actionable_rule": f"Rule {i}"
        })

    saved = StateStore.load(lessons_file)
    assert len(saved) == 100
    assert saved[-1]["actionable_rule"] == "Rule 104"
    assert saved[0]["actionable_rule"] == "Rule 5"

def test_memory_manager_save_and_symbol_context_retrieval(tmp_path, mock_logger):
    """Verifies that MemoryManager saves cycles and prioritizes requested symbols in get_recent_context."""
    storage_dir = str(tmp_path / "memory")
    manager = MemoryManager(mock_logger, storage_path=storage_dir)

    # Save cycles for different symbols
    manager.save_cycle({"symbol": "BTC-USD", "status": "APPROVED", "ceo_decision": {"decision": "LONG", "conviction": 85}})
    manager.save_cycle({"symbol": "ETH-USD", "status": "VETOED", "ceo_decision": {"decision": "SHORT", "conviction": 60}})
    manager.save_cycle({"symbol": "SOL-USD", "status": "APPROVED", "ceo_decision": {"decision": "LONG", "conviction": 90}})
    manager.save_cycle({"symbol": "SOL-USD", "status": "VETOED", "ceo_decision": {"decision": "LONG", "conviction": 65}})

    # Fetch context for SOL-USD
    sol_context = manager.get_recent_context(limit=2, symbol="SOL-USD", compact=True)
    assert len(sol_context) == 2
    for item in sol_context:
        assert item["symbol"] == "SOL-USD"
        assert "ceo_decision" in item
        assert "status" in item

    # Limit=3 should include both SOL cycles and pad with one other symbol
    sol_context_3 = manager.get_recent_context(limit=3, symbol="SOL-USD", compact=True)
    assert len(sol_context_3) == 3
    assert sol_context_3[0]["symbol"] == "SOL-USD"
    assert sol_context_3[1]["symbol"] == "SOL-USD"
    assert sol_context_3[2]["symbol"] in ["BTC-USD", "ETH-USD"]

def test_memory_manager_rotation_limit(tmp_path, mock_logger):
    """Verifies that MemoryManager keeps only the latest 100 cycle logs."""
    storage_dir = str(tmp_path / "memory")
    manager = MemoryManager(mock_logger, storage_path=storage_dir)

    for i in range(105):
        manager.save_cycle({"symbol": "BTC-USD", "iteration": i})

    files = [f for f in os.listdir(manager.storage_path) if f.startswith('cycle_') and f.endswith('.json')]
    assert len(files) == 100

def test_diagnostic_tracker_recording_and_normalization(tmp_path):
    """Verifies DiagnosticTracker normalization of rejection categories and closed trade tracking."""
    diag_file = str(tmp_path / "diagnostics.json")
    tracker = DiagnosticTracker(filepath=diag_file)

    tracker.record_scan()
    tracker.record_scan()
    tracker.record_trade()
    tracker.record_execution_failed()

    # Rejection recording with messy whitespace and spaces
    tracker.record_rejection("ceo hold analyst disagreement")
    tracker.record_rejection("CEO_HOLD_ANALYST_DISAGREEMENT")
    tracker.record_rejection(" market choppy ")

    # Closed trades recording
    tracker.record_closed_trade("WIN", 25.50)
    tracker.record_closed_trade("LOSS", -10.00)
    tracker.record_closed_trade("BREAK_EVEN", 0.00)

    # Reload from disk
    tracker_reloaded = DiagnosticTracker(filepath=diag_file)
    s = tracker_reloaded.stats

    assert s["total_scans"] == 2
    assert s["trades_executed"] == 1
    assert s["execution_failed"] == 1
    assert s["win_trades"] == 1
    assert s["loss_trades"] == 1
    assert s["breakeven_trades"] == 1
    assert s["total_pnl_usd"] == pytest.approx(15.50)
    assert s["rejections"]["CEO_HOLD_ANALYST_DISAGREEMENT"] == 2
    assert s["rejections"]["MARKET_CHOPPY"] == 1

    summary_text = tracker_reloaded.get_summary_text()
    assert "Total Scans: 2" in summary_text
    assert "Realized PnL: $+15.50" in summary_text
    assert "CEO_HOLD_ANALYST_DISAGREEMENT" in summary_text

def test_state_store_atomic_write_and_default_serialization(tmp_path):
    """Verifies StateStore.save atomic writes and non-primitive serialization handling."""
    target_file = str(tmp_path / "test_state.json")
    data_with_complex_types = {
        "timestamp": datetime.now(),
        "tags": {"tag1", "tag2"},
        "rejections": {"VETO": 5}
    }

    # Must save without throwing TypeError on datetime or set
    StateStore.save(target_file, data_with_complex_types)
    assert os.path.exists(target_file)

    loaded = StateStore.load(target_file)
    assert "timestamp" in loaded
    assert "rejections" in loaded
    assert loaded["rejections"]["VETO"] == 5
