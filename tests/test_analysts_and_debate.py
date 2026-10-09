import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from agents.candle_agent import CandleAgent
from agents.order_book_agent import OrderBookAgent
from agents.oi_funding_agent import OIFundingAgent
from agents.news_agent import NewsAgent
from agents.bull_agent import BullAgent
from agents.bear_agent import BearAgent
from core.logger import TradeLogger


@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


# ==============================================================================
# 1. OI & FUNDING AGENT TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_oi_funding_agent_handles_none_and_empty_data(mock_logger):
    agent = OIFundingAgent(mock_logger)
    
    # Empty market data
    res_empty = await agent.analyze({})
    assert res_empty["signal"] == "NEUTRAL"
    assert res_empty["confidence"] == 50
    
    # derivatives_data is explicitly None
    res_none = await agent.analyze({"derivatives_data": None})
    assert res_none["signal"] == "NEUTRAL"
    assert res_none["confidence"] == 50

    # Fields with None values
    res_none_fields = await agent.analyze({
        "derivatives_data": {
            "funding_rate": None,
            "open_interest_usd": None,
            "open_interest_trend": None
        }
    })
    assert res_none_fields["signal"] == "NEUTRAL"
    assert res_none_fields["confidence"] == 50


@pytest.mark.asyncio
async def test_oi_funding_agent_bullish_and_bearish_signals(mock_logger):
    agent = OIFundingAgent(mock_logger)
    
    # High positive funding -> BEARISH squeeze down risk
    bear_res = await agent.analyze({
        "derivatives_data": {
            "funding_rate": 0.0008,
            "open_interest_usd": 5000000,
            "open_interest_trend": "rising"
        }
    })
    assert bear_res["signal"] == "BEARISH"
    assert bear_res["confidence"] >= 80
    assert "риск сквиза вниз" in bear_res["reasoning"]

    # High negative funding -> BULLISH short squeeze risk
    bull_res = await agent.analyze({
        "derivatives_data": {
            "funding_rate": -0.0008,
            "open_interest_usd": 2000000,
            "open_interest_trend": "rising"
        }
    })
    assert bull_res["signal"] == "BULLISH"
    assert bull_res["confidence"] >= 80
    assert "риск шорт-сквиза" in bull_res["reasoning"]


# ==============================================================================
# 2. ORDER BOOK AGENT TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_order_book_agent_handles_none_and_empty_data(mock_logger):
    agent = OrderBookAgent(mock_logger)

    # Completely missing order_book_data
    res1 = await agent.analyze({})
    assert res1["signal"] == "NEUTRAL"
    assert res1["confidence"] == 50

    # order_book_data is None
    res2 = await agent.analyze({"order_book_data": None})
    assert res2["signal"] == "NEUTRAL"
    assert res2["confidence"] == 50

    # None fields in order_book_data
    res3 = await agent.analyze({
        "order_book_data": {
            "imbalance": None,
            "spread": None
        }
    })
    assert res3["signal"] == "NEUTRAL"
    assert res3["confidence"] == 50


@pytest.mark.asyncio
async def test_order_book_agent_signals(mock_logger):
    agent = OrderBookAgent(mock_logger)

    # Bullish order book (heavy bid depth)
    bull_res = await agent.analyze({
        "order_book_data": {
            "imbalance": 0.65,
            "spread": 0.0002
        }
    })
    assert bull_res["signal"] == "BULLISH"
    assert bull_res["confidence"] >= 75
    assert "покупателей" in bull_res["reasoning"]

    # Bearish order book (heavy ask depth)
    bear_res = await agent.analyze({
        "order_book_data": {
            "imbalance": -0.55,
            "spread": 0.0005
        }
    })
    assert bear_res["signal"] == "BEARISH"
    assert bear_res["confidence"] >= 75
    assert "продавцов" in bear_res["reasoning"]


# ==============================================================================
# 3. NEWS AGENT TESTS (SENTIMENT & FALLBACK)
# ==============================================================================

@pytest.mark.asyncio
async def test_news_agent_fallback_and_none_handling(mock_logger):
    # LLM client is None -> deterministic fallback triggers cleanly
    agent = NewsAgent(mock_logger, llm_client=None)

    # None news_data defaults to neutral 50.0
    res_none = await agent.analyze({"news_data": None})
    assert res_none["signal"] == "NEUTRAL"
    assert res_none["confidence"] == 50

    # Extreme Fear contrarian long (score <= 25)
    agent_fear = NewsAgent(mock_logger, llm_client=None)
    res_fear = await agent_fear.analyze({"news_data": {"sentiment_score": 15.0}})
    assert res_fear["signal"] == "BULLISH"
    assert res_fear["confidence"] == 75
    assert "Extreme Fear" in res_fear["reasoning"]

    # Extreme Greed contrarian short (score >= 75)
    agent_greed = NewsAgent(mock_logger, llm_client=None)
    res_greed = await agent_greed.analyze({"news_data": {"sentiment_score": 85.0}})
    assert res_greed["signal"] == "BEARISH"
    assert res_greed["confidence"] == 75
    assert "Extreme Greed" in res_greed["reasoning"]


@pytest.mark.asyncio
async def test_news_agent_caching(mock_logger):
    agent = NewsAgent(mock_logger, llm_client=None)
    
    res1 = await agent.analyze({"news_data": {"sentiment_score": 20.0}})
    assert res1["signal"] == "BULLISH"
    
    # Subsequent call with identical score should return cached result
    res2 = await agent.analyze({"news_data": {"sentiment_score": 20.0}})
    assert res2["signal"] == "BULLISH"
    assert res2 == res1


# ==============================================================================
# 4. CANDLE AGENT TESTS (CONFIRMED CLOSED VS FORMING)
# ==============================================================================

@pytest.mark.asyncio
async def test_candle_agent_insufficient_data(mock_logger):
    agent = CandleAgent(mock_logger)
    
    res = await agent.analyze({"price_data": {"candles_20": []}})
    assert res["signal"] == "NEUTRAL"
    assert res["confidence"] == 0

    res_single = await agent.analyze({"price_data": {"candles_20": [{"open": 100, "close": 105}]}})
    assert res_single["signal"] == "NEUTRAL"
    assert res_single["confidence"] == 0


@pytest.mark.asyncio
async def test_candle_agent_confirmed_closed_breakout_and_engulfing(mock_logger):
    agent = CandleAgent(mock_logger)

    # Setup: 4 candles
    # c[-4]: base
    # c[-3]: prev closed: high=105, low=98, open=104, close=100 (bearish)
    # c[-2]: last closed: open=99, close=110, high=111, low=99 (bullish breakout & engulfing)
    # c[-1]: live forming: open=110, close=110.5, high=111, low=110
    market_data = {
        "price_data": {
            "candles_20": [
                {"open": 100, "high": 103, "low": 99, "close": 101, "volume": 1000},
                {"open": 104, "high": 105, "low": 98, "close": 100, "volume": 1000},
                {"open": 99, "high": 111, "low": 99, "close": 110, "volume": 2500},
                {"open": 110, "high": 111, "low": 110, "close": 110.5, "volume": 100}
            ]
        },
        "indicators": {"atr_14": 4.0}
    }

    res = await agent.analyze(market_data)
    # Closed candle c[-2] has pierced c[-3] high (110 > 105) and engulfed
    assert res["signal"] == "BULLISH"
    assert res["confidence"] >= 75
    assert "закрытой свече" in res["reasoning"]


@pytest.mark.asyncio
async def test_candle_agent_confirmed_liquidity_sweep(mock_logger):
    agent = CandleAgent(mock_logger)

    # c[-2] has a massive lower wick (Pin bar / hammer) with volume
    # range = 100 - 85 = 15. lower_wick = min(98, 95) - 85 = 10 (which is > 50% of range)
    market_data = {
        "price_data": {
            "candles_20": [
                {"open": 100, "high": 102, "low": 98, "close": 99, "volume": 500},
                {"open": 95, "high": 100, "low": 85, "close": 98, "volume": 2000},
                {"open": 98, "high": 99, "low": 98, "close": 98.5, "volume": 100}
            ]
        },
        "indicators": {"atr_14": 10.0}
    }

    res = await agent.analyze(market_data)
    assert res["signal"] == "BULLISH"
    assert res["pattern_detected"] == "Bullish Liquidity Sweep"
    assert res["confidence"] >= 80


@pytest.mark.asyncio
async def test_candle_agent_live_forming_bar_sweep_when_closed_is_neutral(mock_logger):
    agent = CandleAgent(mock_logger)

    # c[-3] and c[-2] are neutral standard candles
    # c[-1] forming candle is experiencing a severe lower wick sweep with heavy volume
    market_data = {
        "price_data": {
            "candles_20": [
                {"open": 100, "high": 101, "low": 99, "close": 100.2, "volume": 500},
                {"open": 100.2, "high": 101, "low": 99.8, "close": 100.4, "volume": 500},
                {"open": 100.4, "high": 105, "low": 90, "close": 104, "volume": 3000}
            ]
        },
        "indicators": {"atr_14": 5.0}
    }

    res = await agent.analyze(market_data)
    assert res["signal"] == "BULLISH"
    assert res["pattern_detected"] == "Live Bullish Liquidity Sweep"
    assert "формирующейся свече" in res["reasoning"]


# ==============================================================================
# 5. BULL & BEAR AGENT FALLBACKS
# ==============================================================================

@pytest.mark.asyncio
async def test_bull_and_bear_agents_gracefully_fallback_on_llm_error(mock_logger):
    mock_llm = MagicMock()
    # LLM raises an error
    mock_llm.generate = AsyncMock(side_effect=RuntimeError("LLM API Timeout"))

    bull = BullAgent(mock_logger, mock_llm)
    bear = BearAgent(mock_logger, mock_llm)

    payload = {
        "symbol": "BTC-USD",
        "analyst_reports": [],
        "multi_timeframe_context": {}
    }

    bull_verdict = await bull.analyze(payload)
    assert bull_verdict["thesis_score"] == 0
    assert "bullish_arguments" in bull_verdict
    assert "Fallback thesis" in bull_verdict["summary"]

    bear_verdict = await bear.analyze(payload)
    assert bear_verdict["thesis_score"] == 0
    assert "bearish_arguments" in bear_verdict
    assert "Fallback thesis" in bear_verdict["summary"]


# ==============================================================================
# 6. PIPELINE STAGE 4 DEBATE EXCEPTION ISOLATION
# ==============================================================================

@pytest.mark.asyncio
async def test_pipeline_stage4_debate_exception_isolation(mock_logger):
    from core.pipeline import TradingPipeline, AgentRegistry, ServiceRegistry
    from core.strategy_router import StrategyProfile

    risk_mock = MagicMock()
    risk_mock._get_profile_rules.return_value = {"min_conviction": 70}

    dummy_agents = AgentRegistry(
        universe=MagicMock(),
        scanner=MagicMock(),
        candle=MagicMock(),
        orderbook=MagicMock(),
        oi_funding=MagicMock(),
        news=MagicMock(),
        indicator=MagicMock(),
        bull=MagicMock(),
        bear=MagicMock(),
        ceo=MagicMock(),
        sentinel=MagicMock(),
        regime=MagicMock(),
        risk=risk_mock,
        telegram=MagicMock(),
        reflector=MagicMock(),
        memory=MagicMock()
    )

    dummy_services = ServiceRegistry(
        logger=mock_logger,
        fetcher=MagicMock(),
        tg_sender=MagicMock(),
        trading_service=MagicMock()
    )
    dummy_services.tg_sender.send_message = AsyncMock()
    dummy_services.tg_sender.broadcast_to_channel = AsyncMock()
    dummy_services.trading_service.active_positions = {}
    dummy_services.trading_service.cooldown_until = 0.0
    dummy_services.trading_service.sync_with_exchange = AsyncMock()
    dummy_services.trading_service.check_and_update_positions = AsyncMock(return_value=[])
    dummy_services.trading_service.get_portfolio_summary = AsyncMock(return_value={"total_usd": 1000.0, "available_margin": 1000.0})

    pipeline = TradingPipeline(dummy_agents, dummy_services, "nado")

    # Setup pipeline mocks
    pipeline.data_guard.validate = MagicMock(return_value=(True, "OK"))
    dummy_agents.sentinel.run_pre_flight_checks = AsyncMock(return_value=True)
    dummy_agents.regime.analyze = AsyncMock(return_value={"regime": "TRENDING_BULL", "recommended_profile": "BALANCED"})
    dummy_agents.universe.analyze = AsyncMock(return_value={"selected_pairs": ["BTC-USD"]})
    dummy_services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "BTC-USD", "volumeQuote": 1000}])
    dummy_services.trading_service.get_market_limits = AsyncMock(return_value={"size_increment": 0.001})
    dummy_agents.reflector.get_lessons = MagicMock(return_value=[])
    dummy_agents.memory.get_recent_context = MagicMock(return_value=[])
    dummy_agents.scanner.analyze = AsyncMock(return_value={"proceed": True})

    # Analysts return valid reports
    for agent, name in [
        (dummy_agents.candle, "Candle_Agent"),
        (dummy_agents.orderbook, "Order_Book_Agent"),
        (dummy_agents.oi_funding, "OI_Funding_Agent"),
        (dummy_agents.news, "News_Agent"),
        (dummy_agents.indicator, "Indicator_Agent")
    ]:
        agent.analyze = AsyncMock(return_value={"signal": "BULLISH", "confidence": 75, "reasoning": "Positive momentum"})
        agent.name = name

    # Bull agent raises an unhandled exception during debate
    dummy_agents.bull.analyze = AsyncMock(side_effect=RuntimeError("Unhandled network timeout in BullAgent"))
    # Bear agent returns thesis normally
    dummy_agents.bear.analyze = AsyncMock(return_value={"thesis_score": 10, "bearish_arguments": ["Overbought"], "summary": "Bearish resistance"})

    # CEO agent returns HOLD
    ceo_called_payload = {}
    async def capture_ceo(payload):
        nonlocal ceo_called_payload
        ceo_called_payload = payload
        return {"decision": "HOLD", "conviction": 50, "trade_action": "HOLD", "hold_category": "CEO_HOLD"}
    dummy_agents.ceo.analyze = AsyncMock(side_effect=capture_ceo)

    dummy_services.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "symbol": "BTC-USD",
        "price_data": {"current_price": 50000.0, "candles_20": [{"open": 49000, "close": 50000}]},
        "indicators": {"rsi_14": 55.0, "atr_14": 200.0},
        "multi_timeframe": {"trend_1h": "BULLISH", "trend_4h": "BULLISH", "mtf_alignment": "FULL_ALIGNMENT"}
    })

    # Execute cycle
    await pipeline.run_cycle(cycle_number=1, force_scan=True)

    # Verify that pipeline did NOT crash
    assert dummy_agents.ceo.analyze.called
    # Verify that CEO received a valid fallback bull thesis rather than crashing
    assert "bull_thesis" in ceo_called_payload
    assert ceo_called_payload["bull_thesis"]["thesis_score"] == 0
    assert "Fallback bull thesis" in ceo_called_payload["bull_thesis"]["summary"]

