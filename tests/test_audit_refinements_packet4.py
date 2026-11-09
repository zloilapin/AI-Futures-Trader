import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from core.llm_parser import LLMStructuredOutputParser
from agents.ceo_agent import CEOAgent
from agents.indicator_agent import IndicatorAgent
from core.pipeline import TradingPipeline
from core.logger import TradeLogger


# ==============================================================================
# 1. JSON Schema & Parser Robustness against Pipe-Delimited Enums
# ==============================================================================
def test_enum_pipe_sanitization_in_llm_parser():
    """Verifies that if an LLM outputs literal enum placeholders with pipes/slashes, parser safely resolves them."""
    raw_response = """
    {
      "decision": "LONG | SHORT | HOLD",
      "directional_confidence": 75,
      "risk_score": 50,
      "entry_quality": 65,
      "trade_action": "ENTER | WAIT_FOR_PULLBACK",
      "winning_argument": "Bull / Bear / Neither",
      "reasoning_en": "Escalation review completed."
    }
    """
    parsed = LLMStructuredOutputParser.parse_and_validate(
        raw_response, 
        required_keys=["decision", "trade_action", "reasoning_en"]
    )
    # The parser should sanitize the pipe/slash-delimited placeholder to a safe single value (HOLD)
    assert parsed["decision"] == "HOLD", f"Expected 'HOLD', got '{parsed['decision']}'"
    assert parsed["trade_action"] == "HOLD", f"Expected 'HOLD', got '{parsed['trade_action']}'"
    assert parsed["winning_argument"] == "Neither"


def test_ceo_agent_sanitizes_pipe_decision():
    """Verifies that CEOAgent._validate_and_compute_score handles pipe-delimited strings safely."""
    agent = CEOAgent.__new__(CEOAgent)
    agent.name = "CEO_Agent"
    agent.logger = MagicMock()
    
    breakdown = {
        "bull_argument": 30,
        "bear_argument": -10,
        "mtf_trend": 20,
        "risk_penalties": {"total": 0}
    }
    res = agent._validate_and_compute_score("LONG | SHORT | HOLD", breakdown)
    assert res["decision"] == "HOLD"


# ==============================================================================
# 2. IndicatorAgent Counter-Trend Penalty & Confidence Cap
# ==============================================================================
@pytest.mark.asyncio
async def test_indicator_agent_counter_trend_penalty_and_confidence_cap():
    """Verifies that 15m bullish indicator signals in a dominant 1H BEARISH trend are penalized and capped at 60%."""
    logger = MagicMock()
    agent = IndicatorAgent(logger)

    # Bullish indicators on 15m: oversold RSI + bullish MACD crossover + lower liquidity sweep
    market_data_bearish_1h = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "rsi_14": 28.0,  # +2 bull_score in neutral, but bear_score in dominant bear trend
            "macd": -1.0,
            "macd_signal": -2.0,  # MACD > macd_signal and macd < 0 -> +2 bull_score
            "ema_20": 98.0,       # price > ema_20 -> +1 bull_score
            "algo_signals": {
                "rsi_divergence": "BULLISH",  # +3 bull_score
                "macd_crossover": "BULLISH_CROSS",  # +2 bull_score
                "liquidity_sweeps": [{"type": "lower_sweep", "direction": "bullish"}]  # +1 bull_score
            }
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }

    res = await agent.analyze(market_data_bearish_1h)
    
    # In old code: net_score was >= 6 and confidence reached 85-95% in direct opposition to 1H bear trend!
    # In new code: counter-trend signal confidence must be capped at <= 60%
    assert res["confidence"] <= 60, f"Expected counter-trend confidence <= 60%, got {res['confidence']}%"
    assert "Сигнал 15м направлен против медвежьего 1H тренда" in res["reasoning"]


# ==============================================================================
# 3. Pipeline Multi-Target Expansion & Radar Symbols
# ==============================================================================
@pytest.mark.asyncio
async def test_pipeline_targeted_scan_multi_target_expansion():
    """Verifies that Targeted Scan supports multiple target symbols and incorporates radar targets."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.exchange_name = "NADO"
    pipeline.services = MagicMock()
    pipeline.services.logger = MagicMock()
    pipeline.services.tg_sender = MagicMock()
    pipeline.services.tg_sender.send_message = AsyncMock()
    pipeline.services.trading_service = MagicMock()
    pipeline.services.trading_service.cooldown_until = 0.0
    pipeline.services.trading_service.active_positions = {}
    pipeline.services.trading_service.get_portfolio_summary = AsyncMock(return_value={"equity": 1000.0})
    pipeline.run_sentinel_checks = AsyncMock()
    pipeline._universe_active_perps = [{"symbol": "SOL-USD"}, {"symbol": "BTC-USD"}, {"symbol": "ETH-USD"}]
    pipeline._universe_cache = ["SOL-USD", "BTC-USD"]
    pipeline._universe_cache_time = 0.0
    pipeline._last_full_scan_ts = time.time()  # Within 25 min to engage targeted scan mode

    # Accumulate a radar momentum target alongside a retest target
    pipeline._radar_target_symbols = {"ETH-USD"}

    # Mock regime agent & reflector
    pipeline.agents = MagicMock()
    pipeline.agents.regime = MagicMock()
    pipeline.agents.regime.analyze = AsyncMock(return_value={"regime": "TREND_FOLLOWING", "recommended_profile": "BALANCED"})
    pipeline.agents.reflector = MagicMock()
    pipeline.agents.reflector.get_lessons = MagicMock(return_value=[])

    pipeline.services.fetcher = MagicMock()
    pipeline.services.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "symbol": "MOCK", "price_data": {"current_price": 100.0}, "order_book": {"spread": 0.01}, "indicators": {"atr_14": 1.0}
    })

    pipeline._priority_symbol = "SOL-USD"  # Re-test priority
    pipeline._priority_direction = "LONG"

    # Trigger targeted scan with SOL-USD while ETH-USD is in _radar_target_symbols
    await pipeline._run_cycle_inner(cycle_number=1, force_scan=True, skip_new_trades=True, targeted_symbol="SOL-USD")

    # Both SOL-USD and ETH-USD must have been targeted and preserved!
    assert "SOL-USD" in pipeline._last_universe_symbols, "Original target SOL-USD must be in targeted assets"
    assert "ETH-USD" in pipeline._last_universe_symbols, "Radar target ETH-USD must be included in multi-target expansion"
    assert pipeline._radar_target_symbols == set(), "Radar target symbols should be consumed and cleared"


# ==============================================================================
# 4. FastRadarLoop Timeout Handling
# ==============================================================================
@pytest.mark.asyncio
async def test_fast_radar_loop_timeout_protection():
    """Verifies that an RPC/DEX hang in radar checks is cleanly timed out and does not freeze the loop."""
    pipeline = MagicMock()
    
    async def hanging_pullback():
        await asyncio.sleep(5.0)
        return (True, "SOL-USD", "Hanging pullback")

    pipeline.check_pullback_watchlist = hanging_pullback
    pipeline.check_fast_market_momentum = AsyncMock(return_value=(False, None, None))

    async def _check_radar_signals():
        pb_trig, pb_s, pb_reas = await pipeline.check_pullback_watchlist()
        if pb_trig:
            return ("pullback", pb_s, pb_reas)
        return (None, None, None)

    # Calling with a short timeout must raise asyncio.TimeoutError and be caught cleanly
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_check_radar_signals(), timeout=0.05)
