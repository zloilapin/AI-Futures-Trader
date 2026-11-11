import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from services.market_data_service import MarketDataService
from agents.oi_funding_agent import OIFundingAgent
from agents.news_agent import NewsAgent
from core.llm_parser import LLMStructuredOutputParser
from core.llm_client import LLMClient, LLMCircuitBreakerException


@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


# ─── 1. Candle Timestamp & Emergency Cache TTL Tests ─────────────────────────

def test_market_data_normalize_candles_preserves_timestamp(mock_logger):
    """
    Verifies that _normalize_candles preserves the timestamp field for both dict
    and list candlestick formats.
    """
    service = MarketDataService(logger=mock_logger)

    raw_dicts = [
        {"open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 1000.0, "timestamp": 1700000000.0},
        {"open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0, "volume": 1200.0, "time": 1700000900.0}
    ]
    norm_dicts = service._normalize_candles(raw_dicts)
    assert len(norm_dicts) == 2
    assert norm_dicts[0]["timestamp"] == 1700000000.0
    assert norm_dicts[1]["timestamp"] == 1700000900.0

    raw_lists = [
        [1700000000.0, 100.0, 105.0, 99.0, 104.0, 1000.0],
        [1700000900.0, 104.0, 106.0, 103.0, 105.0, 1200.0]
    ]
    norm_lists = service._normalize_candles(raw_lists)
    assert len(norm_lists) == 2
    assert norm_lists[0]["timestamp"] == 1700000000.0
    assert norm_lists[1]["timestamp"] == 1700000900.0


@pytest.mark.asyncio
async def test_market_data_fetch_nado_candles_emergency_cache_ttl(mock_logger):
    """
    Verifies that _fetch_nado_candles():
    - Parses and attaches timestamp to candlesticks.
    - On failure, returns emergency cache ONLY if age <= 120s.
    - If cache is older than 120s, returns [] to avoid silent stale data.
    """
    service = MarketDataService(logger=mock_logger)
    service.is_nado = True
    service.nado_client = MagicMock()
    service.product_map = {"SOL": 1}

    cache_key = ("SOL", 15)
    now = time.time()

    # Case A: Fresh emergency cache (60 seconds old)
    fresh_candles = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 50, "timestamp": now - 60}]
    service._candle_cache[cache_key] = (now - 60.0, fresh_candles)

    # Force SDK error to trigger except block
    service.nado_client.market.get_candlesticks.side_effect = Exception("Network timeout")

    res_fresh = await service._fetch_nado_candles("SOL-USD", 15, 10)
    assert len(res_fresh) == 1
    assert res_fresh[0]["timestamp"] == now - 60

    # Case B: Stale emergency cache (180 seconds old > 120s TTL)
    service._candle_cache[cache_key] = (now - 180.0, fresh_candles)
    res_stale = await service._fetch_nado_candles("SOL-USD", 15, 10)
    assert res_stale == []


# ─── 2. OI Change Percentage & RVOL Tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_market_data_oi_change_pct_and_trend(mock_logger):
    """
    Verifies that fetch_oi_funding calculates oi_change_pct and oi_trend:
    - With past_points (>= 30m old history): calculates relative to 1h point.
    - In first 30m (len(history) >= 2): calculates relative to history[0].
    - Guarantees oi_change_pct is always present in output.
    """
    service = MarketDataService(logger=mock_logger)
    service.is_nado = True
    service.nado_client = MagicMock()
    service.product_map = {"SOL": 1}

    now = time.time()
    mock_market = MagicMock()
    mock_prod = MagicMock()
    mock_prod.product_id = 1
    mock_prod.state.open_interest = int(105.0 * 1e18)
    mock_market.perp_products = [mock_prod]
    service.nado_client.market.get_all_engine_markets.return_value = mock_market

    mock_snap_obj = MagicMock()
    mock_snap = MagicMock()
    mock_snap.funding_rates = {"1": int(0.0001 * 1e18)}
    mock_snap.open_interests = {}
    mock_snap_obj.snapshots = [mock_snap]
    service.nado_client.market.get_market_snapshots.return_value = mock_snap_obj

    # Scenario 1: past_points exist (1h ago OI = 100.0, current = 105.0 => +5.0%)
    service._oi_history["SOL-USD"] = [
        {"ts": now - 3600, "oi": 100.0}
    ]
    res1 = await service.fetch_oi_funding("SOL-USD")
    assert res1["open_interest"] == 105.0
    assert res1["oi_change_pct"] == 5.0
    assert res1["open_interest_trend"] == "rising"

    # Scenario 2: First 30 min (history has points within last 15 min: initial 100.0, now 97.0 => -3.0%)
    mock_prod.state.open_interest = int(97.0 * 1e18)
    service._oi_history["SOL-USD"] = [
        {"ts": now - 600, "oi": 100.0}
    ]
    res2 = await service.fetch_oi_funding("SOL-USD")
    assert res2["open_interest"] == 97.0
    assert res2["oi_change_pct"] == -3.0
    assert res2["open_interest_trend"] == "falling"


def test_market_data_indicators_and_fallback_contain_rvol(mock_logger):
    """
    Verifies that fetch_indicators and _create_fallback_indicators provide the 'rvol' key.
    """
    service = MarketDataService(logger=mock_logger)
    fallback = service._create_fallback_indicators("SOL-USD", 100.0)
    assert "rvol" in fallback
    assert fallback["rvol"] == 1.0


# ─── 3. OIFundingAgent candles_20 & Closed Candle Momentum ───────────────────

@pytest.mark.asyncio
async def test_oi_funding_agent_uses_candles_20_and_closed_candle(mock_logger):
    """
    Verifies that OIFundingAgent reads candles from 'candles_20' and evaluates
    price momentum strictly on the completed candle (candles[-2]), ignoring unclosed candles[-1].
    """
    agent = OIFundingAgent(logger=mock_logger, llm_client=MagicMock())

    # candles[-2] is completed GREEN (100 -> 105)
    # candles[-1] is unclosed RED (105 -> 102)
    market_data = {
        "symbol": "SOL-USD",
        "price_data": {
            "current_price": 102.0,
            "candles_20": [
                {"open": 98.0, "high": 100.0, "low": 97.0, "close": 99.0, "volume": 100},
                {"open": 100.0, "high": 106.0, "low": 99.0, "close": 105.0, "volume": 200}, # completed (-2) GREEN
                {"open": 105.0, "high": 105.0, "low": 101.0, "close": 102.0, "volume": 50}   # forming (-1) RED
            ]
        },
        "derivatives_data": {
            "open_interest_trend": "rising",
            "oi_change_pct": 5.0,
            "funding_rate": 0.0001
        },
        "indicators": {}
    }

    res = await agent.analyze(market_data)
    assert res["signal"] == "BULLISH"
    assert "аккумуляция" in res["reasoning"].lower()


# ─── 4. Removal of Synthetic score_breakdown from LLMStructuredOutputParser ──

def test_llm_parser_no_synthetic_score_breakdown():
    """
    Verifies that LLMStructuredOutputParser does NOT inject a synthetic
    bullish score_breakdown when parsing text fallback bullet points.
    If 'score_breakdown' is required and missing, it raises ValueError.
    """
    bullet_text = """
    - trade_action: "SHORT"
    - directional_confidence: 82
    - entry_quality: 78
    - reasoning_en: "Breakdown below structural support with expanding volume."
    """

    # If score_breakdown is required, parser must raise ValueError, not inject fake bullish breakdown
    with pytest.raises(ValueError) as excinfo:
        LLMStructuredOutputParser.parse_and_validate(
            bullet_text,
            required_keys=["trade_action", "directional_confidence", "score_breakdown"]
        )
    assert "Missing required keys" in str(excinfo.value)
    assert "score_breakdown" in str(excinfo.value)

    # When not required, parsed dict must NOT have score_breakdown injected
    parsed = LLMStructuredOutputParser.parse_and_validate(
        bullet_text,
        required_keys=["trade_action", "directional_confidence"]
    )
    assert "score_breakdown" not in parsed
    assert parsed["trade_action"] == "SHORT"
    assert parsed["directional_confidence"] == 82


# ─── 5. NewsAgent Macro-Trend Awareness & LLMClient Provider Rotation ─────────

@pytest.mark.asyncio
async def test_news_agent_payload_and_caching_with_macro_trend(mock_logger):
    """
    Verifies that NewsAgent:
    - Passes mtf_trend_1h and mtf_trend_4h in the LLM payload.
    - Caches results by (round(score), trend_1h), so changing trend_1h invalidates cache.
    """
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value='{"signal": "NEUTRAL", "confidence": 55, "reasoning": "Mixed"}')
    agent = NewsAgent(logger=mock_logger, llm_client=mock_llm)

    market_data = {
        "symbol": "BTC-USD",
        "news_data": {"sentiment_score": 52.3},
        "multi_timeframe": {
            "mtf_trend_1h": "BULLISH",
            "mtf_trend_4h": "BULLISH"
        }
    }

    res1 = await agent.analyze(market_data)
    assert res1["signal"] == "NEUTRAL"
    assert mock_llm.generate.call_count == 1

    # Verify payload passed to LLM
    call_prompt = mock_llm.generate.call_args[0][0]
    assert '"mtf_trend_1h": "BULLISH"' in call_prompt
    assert '"mtf_trend_4h": "BULLISH"' in call_prompt

    # Same score and same 1H trend -> Cache hit (no new LLM call)
    res2 = await agent.analyze(market_data)
    assert res2["signal"] == "NEUTRAL"
    assert mock_llm.generate.call_count == 1

    # Changed 1H trend -> Cache miss (triggers LLM call)
    market_data["multi_timeframe"]["mtf_trend_1h"] = "BEARISH"
    res3 = await agent.analyze(market_data)
    assert mock_llm.generate.call_count == 2


@pytest.mark.asyncio
async def test_news_agent_deterministic_fallback_macro_trend_blocking(mock_logger):
    """
    Verifies that deterministic fallback in NewsAgent blocks counter-trend extremes:
    - score < 40 and trend_1h == "BEARISH" => returns NEUTRAL (no counter-trend long)
    - score > 60 and trend_1h == "BULLISH" => returns NEUTRAL (no counter-trend short)
    - score <= 25 and trend_1h == "NEUTRAL" => returns BULLISH
    - score >= 75 and trend_1h == "NEUTRAL" => returns BEARISH
    """
    # No llm_client triggers deterministic fallback
    agent = NewsAgent(logger=mock_logger, llm_client=None)

    # 1. Fear (score 35) + BEARISH trend => blocked to NEUTRAL
    m1 = {
        "symbol": "BTC-USD",
        "news_data": {"sentiment_score": 35.0},
        "multi_timeframe": {"trend_1h": "BEARISH"}
    }
    r1 = await agent.analyze(m1)
    assert r1["signal"] == "NEUTRAL"
    assert "заблокирован" in r1["reasoning"]

    # 2. Greed (score 68) + BULLISH trend => blocked to NEUTRAL
    m2 = {
        "symbol": "BTC-USD",
        "news_data": {"sentiment_score": 68.0},
        "multi_timeframe": {"trend_1h": "BULLISH"}
    }
    r2 = await agent.analyze(m2)
    assert r2["signal"] == "NEUTRAL"
    assert "заблокирован" in r2["reasoning"]

    # 3. Extreme Fear (score 20) + NEUTRAL trend => allowed contrarian BULLISH
    m3 = {
        "symbol": "BTC-USD",
        "news_data": {"sentiment_score": 20.0},
        "multi_timeframe": {"trend_1h": "NEUTRAL"}
    }
    r3 = await agent.analyze(m3)
    assert r3["signal"] == "BULLISH"

    # 4. Extreme Greed (score 80) + NEUTRAL trend => allowed contrarian BEARISH
    m4 = {
        "symbol": "BTC-USD",
        "news_data": {"sentiment_score": 80.0},
        "multi_timeframe": {"trend_1h": "NEUTRAL"}
    }
    r4 = await agent.analyze(m4)
    assert r4["signal"] == "BEARISH"


def test_llm_client_switch_provider_preserves_rotation():
    """
    Verifies that _switch_provider() does not permanently delete the current provider,
    but appends it to the end of _available_providers for cyclic rotation.
    """
    client = LLMClient.__new__(LLMClient)
    client.provider = "none"
    client.model_name = "none"
    client.circuit_breaker_until = 0.0

    p_groq = {"provider": "groq", "model": "llama-3.1-70b"}
    p_gemini = {"provider": "gemini", "model": "gemini-2.0-flash"}
    p_openrouter = {"provider": "openrouter", "model": "deepseek-chat"}

    client._available_providers = [p_gemini, p_openrouter]
    client._set_provider(p_groq)

    # Initial state: active = groq, queue = [gemini, openrouter]
    assert client.provider == "groq"
    assert len(client._available_providers) == 2

    # Switch 1: groq moves to end, gemini becomes active
    assert client._switch_provider() is True
    assert client.provider == "gemini"
    assert client._available_providers == [p_openrouter, p_groq]

    # Switch 2: gemini moves to end, openrouter becomes active
    assert client._switch_provider() is True
    assert client.provider == "openrouter"
    assert client._available_providers == [p_groq, p_gemini]

    # Switch 3: openrouter moves to end, groq becomes active again (cyclic preservation!)
    assert client._switch_provider() is True
    assert client.provider == "groq"
    assert client._available_providers == [p_gemini, p_openrouter]


@pytest.mark.asyncio
async def test_llm_client_generate_limits_switches_to_unique_providers():
    """
    Verifies that generate() limits provider switches to the count of unique providers,
    preventing infinite recursion and properly raising LLMCircuitBreakerException.
    """
    client = LLMClient.__new__(LLMClient)
    client.circuit_breaker_until = 0.0

    p1 = {"provider": "groq", "model": "llama-3.1-70b"}
    p2 = {"provider": "gemini", "model": "gemini-2.0-flash"}

    client._available_providers = [p2]
    client._set_provider(p1)

    # 2 unique providers: groq and gemini
    assert client.get_unique_providers_count() == 2

    switch_calls = []
    orig_switch = client._switch_provider
    def spy_switch():
        res = orig_switch()
        if res:
            switch_calls.append(client.provider)
        return res
    client._switch_provider = spy_switch

    # Client has neither Groq nor Gemini client initialized -> triggers failure & switch
    client.client = None

    with pytest.raises(LLMCircuitBreakerException):
        await client.generate("test prompt", max_retries=1)

    # Exactly 2 switches (groq -> gemini -> groq), then bounded and stops
    assert len(switch_calls) == 2
    assert switch_calls == ["gemini", "groq"]
    # Queue is still preserved!
    assert len(client._available_providers) == 1

