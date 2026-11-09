# tests/test_audit_alpha_levers_packet6.py
import pytest
from unittest.mock import MagicMock, AsyncMock

from agents.order_book_agent import OrderBookAgent
from agents.oi_funding_agent import OIFundingAgent
from agents.universe_agent import UniverseAgent


@pytest.mark.asyncio
async def test_order_book_agent_lowered_threshold_sensitivity():
    """
    Lever 1: Verify OrderBookAgent wakes up at imbalance >= 0.15 instead of 0.30,
    and scales confidence up to 75-85% on strong dominance (>= 0.28).
    """
    logger = MagicMock()
    ob_agent = OrderBookAgent(logger)

    # 1. Subtle imbalance (0.10) -> Should remain NEUTRAL
    res_subtle = await ob_agent.analyze({"order_book_data": {"imbalance": 0.10, "spread": 0.0001}})
    assert res_subtle["signal"] == "NEUTRAL"

    # 2. Moderate bullish imbalance (0.18) -> Wakes up to BULLISH (60-74% confidence)
    res_mod_bull = await ob_agent.analyze({"order_book_data": {"imbalance": 0.18, "spread": 0.0002}})
    assert res_mod_bull["signal"] == "BULLISH"
    assert 60 <= res_mod_bull["confidence"] <= 74
    assert "Умеренный перевес" in res_mod_bull["reasoning"]

    # 3. Aggressive bullish dominance (0.35) -> BULLISH (>= 75% confidence)
    res_agg_bull = await ob_agent.analyze({"order_book_data": {"imbalance": 0.35, "spread": 0.0001}})
    assert res_agg_bull["signal"] == "BULLISH"
    assert res_agg_bull["confidence"] >= 75
    assert "Агрессивное институциональное доминирование" in res_agg_bull["reasoning"]

    # 4. Moderate bearish imbalance (-0.20) -> Wakes up to BEARISH
    res_mod_bear = await ob_agent.analyze({"order_book_data": {"imbalance": -0.20, "spread": 0.0001}})
    assert res_mod_bear["signal"] == "BEARISH"
    assert 60 <= res_mod_bear["confidence"] <= 74

    # 5. Aggressive bearish dominance (-0.30) -> BEARISH (>= 75% confidence)
    res_agg_bear = await ob_agent.analyze({"order_book_data": {"imbalance": -0.30, "spread": 0.0001}})
    assert res_agg_bear["signal"] == "BEARISH"
    assert res_agg_bear["confidence"] >= 75


@pytest.mark.asyncio
async def test_oi_funding_agent_capital_flow_and_exhaustion():
    """
    Lever 1: Verify OIFundingAgent scores rising/falling OI in confluence with price direction
    during normal funding conditions, instead of returning 0 points / NEUTRAL.
    """
    logger = MagicMock()
    oi_agent = OIFundingAgent(logger)

    # Case A: Price rising + OI rising (Institutional Long Accumulation)
    data_long_acc = {
        "derivatives_data": {"funding_rate": 0.00005, "open_interest_trend": "rising", "open_interest_usd": 2000000},
        "price_data": {"candles": [{"open": 69000, "close": 69500}], "current_price": 69500},
        "indicators": {"ema_20": 69200}
    }
    res_long = await oi_agent.analyze(data_long_acc)
    assert res_long["signal"] == "BULLISH"
    assert res_long["confidence"] >= 75
    assert "аккумуляция" in res_long["reasoning"]

    # Case B: Price falling + OI rising (Institutional Short Buildup)
    data_short_build = {
        "derivatives_data": {"funding_rate": -0.00005, "open_interest_trend": "rising", "open_interest_usd": 2000000},
        "price_data": {"candles": [{"open": 69500, "close": 68800}], "current_price": 68800},
        "indicators": {"ema_20": 69200}
    }
    res_short = await oi_agent.analyze(data_short_build)
    assert res_short["signal"] == "BEARISH"
    assert res_short["confidence"] >= 75
    assert "набор коротких позиций" in res_short["reasoning"]

    # Case C: Price rising + OI falling (Short Covering Exhaustion)
    data_exhaustion = {
        "derivatives_data": {"funding_rate": 0.00005, "open_interest_trend": "falling", "open_interest_usd": 500000},
        "price_data": {"candles": [{"open": 69000, "close": 69500}], "current_price": 69500},
        "indicators": {"ema_20": 69200}
    }
    res_exh = await oi_agent.analyze(data_exhaustion)
    assert res_exh["signal"] == "BEARISH"
    assert "истощение" in res_exh["reasoning"]

    # Case D: Extreme positive funding (> 0.02%/hr) + Long Flush
    data_flush = {
        "derivatives_data": {"funding_rate": 0.00035, "open_interest_trend": "falling", "open_interest_usd": 1500000},
        "price_data": {"candles": [{"open": 69500, "close": 68000}], "current_price": 68000},
        "indicators": {"ema_20": 69200}
    }
    res_flush = await oi_agent.analyze(data_flush)
    # Both extreme positive funding and long flush alert to dump / risk
    assert res_flush["signal"] == "BEARISH"
    assert "риск сквиза вниз" in res_flush["reasoning"]


@pytest.mark.asyncio
async def test_universe_agent_deterministic_selection():
    """
    Lever 2: Verify UniverseAgent operates deterministically by default without LLM,
    selecting Core Majors + Top-4 Altcoins by volume/ATR.
    """
    logger = MagicMock()
    llm = MagicMock()
    u_agent = UniverseAgent(logger, llm)

    broad_data = {
        "trending_perps": [
            {"symbol": "SOL-USD", "volumeQuote": 50000000, "atr_pct": 2.5},
            {"symbol": "DOGE-USD", "volumeQuote": 30000000, "atr_pct": 3.0}, # score = 90m
            {"symbol": "BTC-USD", "volumeQuote": 200000000, "atr_pct": 1.5},
            {"symbol": "ETH-USD", "volumeQuote": 100000000, "atr_pct": 2.0},
            {"symbol": "XRP-USD", "volumeQuote": 40000000, "atr_pct": 1.2},  # score = 48m
            {"symbol": "HYPE-USD", "volumeQuote": 25000000, "atr_pct": 5.0}, # score = 125m
            {"symbol": "AVAX-USD", "volumeQuote": 15000000, "atr_pct": 2.0}, # score = 30m
            {"symbol": "LINK-USD", "volumeQuote": 10000000, "atr_pct": 1.5}, # score = 15m
        ]
    }

    # Deterministic analyze() call (0 LLM calls)
    res = await u_agent.analyze(broad_data)
    assert not llm.generate_json.called, "LLM was called in deterministic mode!"
    
    selected = res["selected_pairs"]
    assert len(selected) == 7
    # 3 Core Majors must be present and anchored first
    assert selected[:3] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    # Next 4 alts ranked by score (vol * atr): HYPE (125m), DOGE (90m), XRP (48m), AVAX (30m)
    assert "HYPE-USD" in selected
    assert "DOGE-USD" in selected
    assert "XRP-USD" in selected
    assert "AVAX-USD" in selected
    assert "LINK-USD" not in selected # ranked 5th alt
    assert "Детерминированный отбор (0 токенов" in res["reasoning"]

