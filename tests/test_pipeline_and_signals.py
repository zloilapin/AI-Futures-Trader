import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from core.models import FinalTradeDecision, FinalRiskDecision, ExecutionResult
from core.strategy_router import StrategyProfile
from core.deterministic_guard import DeterministicGuard
from agents.ceo_agent import CEOAgent
from agents.scanner_agent import ScannerAgent
from agents.risk_manager import RiskManager
from core.logger import TradeLogger
from core.pipeline import TradingPipeline, AgentRegistry, ServiceRegistry

@pytest.fixture
def dummy_agents():
    risk_mock = MagicMock()
    risk_mock._get_profile_rules.return_value = {"min_conviction": 70}
    return AgentRegistry(
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

import time

@pytest.fixture
def dummy_services():
    logger = TradeLogger()
    fetcher = MagicMock()
    tg_sender = MagicMock()
    tg_sender.send_message = AsyncMock()
    tg_sender.broadcast_to_channel = AsyncMock()
    trading_service = MagicMock()
    trading_service.active_positions = {}
    trading_service.cooldown_until = 0.0
    trading_service.sync_with_exchange = AsyncMock()
    trading_service.check_and_update_positions = AsyncMock(return_value=[])
    trading_service.get_portfolio_summary = AsyncMock(return_value={"total_usd": 1000.0, "available_margin": 1000.0})
    return ServiceRegistry(
        logger=logger,
        fetcher=fetcher,
        tg_sender=tg_sender,
        trading_service=trading_service
    )

@pytest.mark.asyncio
async def test_pipeline_stage1_universe_fallback_on_error(dummy_agents, dummy_services):
    """Verifies that an error in UniverseAgent does not crash run_cycle and falls back to active perps."""
    pipeline = TradingPipeline(dummy_agents, dummy_services, "NADO")
    
    # Mock Regime Agent
    now = time.time()
    dummy_services.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "price_data": {"current_price": 60000.0, "timestamp": now},
        "order_book_data": {"spread_pct": 0.05, "best_bid": 59990.0, "best_ask": 60010.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {"open_interest": 100.0, "funding_rate": 0.0001}
    })
    dummy_agents.regime.analyze = AsyncMock(return_value={"regime": "RANGE_CHOPPY", "recommended_profile": "BALANCED"})
    dummy_agents.reflector.get_lessons = MagicMock(return_value=[])
    
    # Mock Universe Agent throwing an exception
    dummy_services.fetcher.fetch_active_perps = AsyncMock(return_value=[
        {"symbol": "BTC-USD", "volumeQuote": 1000000},
        {"symbol": "ETH-USD", "volumeQuote": 500000}
    ])
    dummy_agents.universe.analyze = AsyncMock(side_effect=RuntimeError("LLM API 503 Overloaded"))
    
    # Mock positions cap
    dummy_services.trading_service.active_positions = {}
    
    # Run cycle without crash
    await pipeline.run_cycle(cycle_number=1, force_scan=True)
    
    # Universe Agent error should be logged and active_perps fallback used
    assert dummy_agents.universe.analyze.called

@pytest.mark.asyncio
async def test_pipeline_per_symbol_error_isolation(dummy_agents, dummy_services):
    """Verifies that an unhandled exception on symbol A does not prevent symbol B from being processed."""
    pipeline = TradingPipeline(dummy_agents, dummy_services, "NADO")
    
    dummy_services.fetcher.fetch_all_market_data = AsyncMock(return_value={"price_data": {"current_price": 60000.0}})
    dummy_agents.regime.analyze = AsyncMock(return_value={"regime": "RANGE_CHOPPY", "recommended_profile": "BALANCED"})
    dummy_agents.reflector.get_lessons = MagicMock(return_value=[])
    dummy_agents.universe.analyze = AsyncMock(return_value={"selected_pairs": ["BAD-USD", "GOOD-USD"]})
    dummy_services.fetcher.fetch_active_perps = AsyncMock(return_value=[
        {"symbol": "BAD-USD", "volumeQuote": 1000},
        {"symbol": "GOOD-USD", "volumeQuote": 1000}
    ])
    
    # Symbol BAD-USD throws error on get_market_limits
    async def mock_market_limits(sym):
        if sym == "BAD-USD":
            raise ValueError("Corrupt market limit RPC")
        return {"size_increment": 0.001}
    dummy_services.trading_service.get_market_limits = AsyncMock(side_effect=mock_market_limits)
    
    # Scanner Agent succeeds for GOOD-USD
    dummy_agents.scanner.analyze = AsyncMock(return_value={"proceed": False, "reasoning": "Filter test passed for GOOD-USD"})
    
    scan_summaries = []
    # Capture scan_summaries by intercepting send_message
    async def mock_send(msg, **kwargs):
        scan_summaries.append(msg)
    dummy_services.tg_sender.send_message = AsyncMock(side_effect=mock_send)
    
    await pipeline.run_cycle(cycle_number=1, force_scan=True)
    
    # Cycle must complete and scan report must be sent
    assert len(scan_summaries) > 0
    report_text = scan_summaries[0]
    assert "BAD-USD" in report_text
    assert "GOOD-USD" in report_text

@pytest.mark.asyncio
async def test_pipeline_execution_failure_no_phantom_broadcast(dummy_agents, dummy_services):
    """Verifies that when open_position fails, no phantom trade card is broadcast to the channel."""
    pipeline = TradingPipeline(dummy_agents, dummy_services, "NADO")
    pipeline.data_guard.validate = MagicMock(return_value=(True, "OK"))
    
    dummy_services.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "price_data": {"current_price": 2000.0, "timestamp": time.time()},
        "order_book_data": {"best_bid": 1999.0, "best_ask": 2001.0, "spread_pct": 0.1},
        "indicators": {"atr_14": 20.0, "ema_20": 1980.0},
        "derivatives_data": {"open_interest": 100.0, "funding_rate": 0.0001}
    })
    dummy_agents.regime.analyze = AsyncMock(return_value={"regime": "BULL_TREND", "recommended_profile": "BALANCED"})
    dummy_agents.reflector.get_lessons = MagicMock(return_value=[])
    dummy_agents.universe.analyze = AsyncMock(return_value={"selected_pairs": ["ETH-USD"]})
    dummy_services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "ETH-USD", "volumeQuote": 1000}])
    dummy_services.trading_service.get_market_limits = AsyncMock(return_value={"size_increment": 0.001})
    dummy_agents.scanner.analyze = AsyncMock(return_value={"proceed": True, "reasoning": "Valid"})
    
    # Analysts return consensus
    for agent in [dummy_agents.candle, dummy_agents.orderbook, dummy_agents.oi_funding, dummy_agents.news, dummy_agents.indicator]:
        agent.analyze = AsyncMock(return_value={"signal": "BULLISH", "confidence": 80})
        agent.name = "TestAnalyst"
        
    dummy_agents.bull.analyze = AsyncMock(return_value={"summary": "Bull thesis"})
    dummy_agents.bear.analyze = AsyncMock(return_value={"summary": "Bear thesis"})
    dummy_agents.ceo.analyze = AsyncMock(return_value={
        "decision": "LONG",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 85,
        "trade_action": "ENTER",
        "reasoning_en": "Strong setup"
    })
    dummy_agents.memory.get_recent_context = MagicMock(return_value=[])
    
    # Risk manager approves trade
    dummy_agents.risk.analyze = AsyncMock(return_value={
        "approved": True,
        "notional_size_usd": 200.0,
        "position_size_pct": 20.0,
        "take_profit_price": 2100.0,
        "take_profit_pct": 5.0,
        "stop_loss_price": 1950.0,
        "leverage": 10,
        "contracts": 0.1,
        "reasoning": "Risk approved"
    })
    
    # But trading service FAILS to open position (e.g. sequencer error)
    dummy_services.trading_service.open_position = AsyncMock(return_value=False)
    dummy_services.tg_sender.send_message = AsyncMock()
    dummy_services.tg_sender.broadcast_to_channel = AsyncMock()
    
    await pipeline.run_cycle(cycle_number=1, force_scan=False)
    
    # broadcast_to_channel must NEVER be called for failed execution!
    assert not dummy_services.tg_sender.broadcast_to_channel.called
    # But operator must receive an error alert via send_message
    assert dummy_services.tg_sender.send_message.called
    alert_msg = dummy_services.tg_sender.send_message.call_args[0][0]
    assert "ОШИБКА ИСПОЛНЕНИЯ" in alert_msg or "EXCHANGE ERROR" in alert_msg

def test_deterministic_guard_pullback_retest_promotion():
    """Verifies that DeterministicGuard promotes WAIT_FOR_PULLBACK to ENTER on confirmed pullback retest."""
    profile = StrategyProfile(True, "TREND_FOLLOWING", "LONG", "Trend test")
    risk_mgr = MagicMock()
    risk_mgr._get_profile_rules.return_value = {"min_conviction": 70}
    
    # CEO proposes WAIT_FOR_PULLBACK, but market_data confirms is_pullback_retest
    ceo_verdict = {
        "decision": "LONG",
        "conviction": 75,
        "directional_confidence": 80,
        "entry_quality": 75,
        "trade_action": "WAIT_FOR_PULLBACK",
        "reasoning_en": "Retest at EMA"
    }
    market_data = {
        "is_pullback_retest": True,
        "price_data": {"current_price": 2600.0}
    }
    
    res = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=ceo_verdict,
        profile="BALANCED",
        risk_manager=risk_mgr,
        market_data=market_data,
        symbol="ETH-USD"
    )
    
    assert res.decision == "LONG"
    assert res.trade_action == "ENTER", f"Expected ENTER, got {res.trade_action}"
    assert res.is_actionable is True

def test_ceo_agent_penalizes_overextension_when_price_data_provided():
    """Verifies that CEOAgent calculates late entry penalty when price_data is passed."""
    logger = TradeLogger()
    ceo = CEOAgent(logger=logger, primary_llm=None, escalation_llm=None)
    
    context_overextended = {
        "price_data": {"current_price": 2800.0},
        "indicators": {
            "ema_20": 2600.0, # 7.6% above EMA-20
            "atr_pct": 1.0,
            "rsi_14": 70.0,
            "bb_position_pct": 95.0
        }
    }
    breakdown = {
        "bull_argument": 45.0,
        "bear_argument": 5.0,
        "mtf_trend": 35.0
    }
    
    score = ceo._validate_and_compute_score("LONG", breakdown, market_context=context_overextended)
    assert score["decision"] == "LONG"
    assert score["directional_confidence"] >= 75
    # Overextension (-15) + BB extreme (-8) must reduce entry quality
    assert score["entry_quality"] < 65
    assert score["trade_action"] == "WAIT_FOR_PULLBACK"

@pytest.mark.asyncio
async def test_scanner_agent_handles_none_spread_pct():
    """Verifies that ScannerAgent does not crash with TypeError when spread_pct is None."""
    logger = TradeLogger()
    scanner = ScannerAgent(logger=logger, llm_client=None)
    
    asset_data = {
        "price_data": {"current_price": 2500.0},
        "order_book_data": {
            "spread": 0.5,
            "spread_pct": None  # Edge case: None from API
        },
        "indicators": {
            "atr_14": 25.0
        }
    }
    
    result = await scanner.analyze(asset_data)
    assert "proceed" in result
    assert result["proceed"] is True
