import asyncio
import time
from core.models import FinalTradeDecision
from core.strategy_router import StrategyProfile
from core.deterministic_guard import DeterministicGuard

class DummyRiskManager:
    def _get_profile_rules(self, profile, strategy_mode):
        return {"min_conviction": 65}

async def run_tests():
    print("=== TEST 1: Breakout Override on Strong Momentum ===")
    profile = StrategyProfile(True, "BREAKOUT", "LONG", "Breakout test")
    ceo_proposal = {
        "decision": "LONG",
        "conviction": 68,
        "directional_confidence": 85,
        "entry_quality": 68,
        "trade_action": "WAIT_FOR_PULLBACK"
    }
    risk_mgr = DummyRiskManager()
    
    # 1.1 Strategy mode is BREAKOUT -> should override to ENTER
    res = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=risk_mgr,
        symbol="ETH-USD"
    )
    assert res.decision == "LONG"
    assert res.trade_action == "ENTER", f"Expected ENTER, got {res.trade_action}"
    assert res.is_actionable is True, f"Expected actionable True, got {res.is_actionable}"
    print("✅ Strategy BREAKOUT overrides WAIT_FOR_PULLBACK to ENTER successfully.")

    # 1.2 Strategy mode is TREND_FOLLOWING but indicators have confirmed Donchian breakout + volume surge
    tf_profile = StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Trend following test")
    market_data_breakout = {
        "price_data": {"current_price": 2500.0},
        "indicators": {
            "donchian_low": 2520.0,
            "volume_spike_pct": 150.0
        }
    }
    short_proposal = {
        "decision": "SHORT",
        "conviction": 70,
        "directional_confidence": 85,
        "entry_quality": 65,
        "trade_action": "WAIT_FOR_PULLBACK"
    }
    res2 = DeterministicGuard.evaluate(
        strategy_profile=tf_profile,
        ceo_proposal=dict(short_proposal),
        profile="BALANCED",
        risk_manager=risk_mgr,
        market_data=market_data_breakout,
        symbol="BTC-USD"
    )
    assert res2.decision == "SHORT"
    assert res2.trade_action == "ENTER", f"Expected ENTER on confirmed Donchian break, got {res2.trade_action}"
    assert res2.is_actionable is True
    print("✅ Confirmed breakdown via indicators overrides WAIT_FOR_PULLBACK to ENTER successfully.")

    print("\n=== TEST 2: No Breakout Confirmation -> Keep Monitoring (Not Actionable) ===")
    market_data_normal = {
        "price_data": {"current_price": 2600.0},
        "indicators": {
            "donchian_low": 2520.0,
            "donchian_high": 2680.0,
            "volume_spike_pct": 20.0,
            "ema_20": 2570.0
        }
    }
    res3 = DeterministicGuard.evaluate(
        strategy_profile=tf_profile,
        ceo_proposal=dict(short_proposal),
        profile="BALANCED",
        risk_manager=risk_mgr,
        market_data=market_data_normal,
        symbol="ETH-USD"
    )
    assert res3.decision == "SHORT"
    assert res3.trade_action == "WAIT_FOR_PULLBACK"
    assert res3.is_actionable is False, f"Expected is_actionable=False, got {res3.is_actionable}"
    assert res3.guard_status == "PULLBACK_WATCHLIST"
    print("✅ Without breakout confirmation, WAIT_FOR_PULLBACK does NOT enter automatically.")

    print("\n=== TEST 3: Pullback Watchlist Lifecycle ===")
    from core.pipeline import TradingPipeline

    class DummyFetcher:
        def __init__(self):
            self.price = 2650.0
        async def fetch_ohlcv(self, symbol):
            return {"current_price": self.price, "candles_20": [{"close": self.price, "open": self.price, "volume": 100, "high": self.price+10, "low": self.price-10}]}

    class DummyServices:
        def __init__(self, fetcher):
            self.fetcher = fetcher
            self.logger = type("DummyLogger", (), {"info": lambda *a, **kw: None, "debug": lambda *a, **kw: None, "error": lambda *a, **kw: None, "warning": lambda *a, **kw: None})()
            self.trading_service = type("DummyTS", (), {"active_positions": {}})()

    fetcher = DummyFetcher()
    services = DummyServices(fetcher)
    pipeline = TradingPipeline(agents=None, services=services, exchange_name="NADO")

    # 3.1 Register LONG candidate at 2650 with target EMA 2600
    market_data_long = {
        "indicators": {"ema_20": 2600.0}
    }
    pipeline.register_pullback_candidate(
        symbol="ETH-USD",
        decision="LONG",
        current_price=2650.0,
        directional_conf=80,
        conviction=65,
        strategy_mode="TREND_FOLLOWING",
        market_data=market_data_long,
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=60
    )
    assert "ETH-USD" in pipeline._pullback_watchlist
    entry = pipeline._pullback_watchlist["ETH-USD"]
    assert entry["target_pullback_price"] == 2600.0
    print("✅ Pullback candidate registered correctly with target price.")

    # 3.2 Price still high (2650) -> does not trigger
    fetcher.price = 2640.0
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert not trig, "Should not trigger while price is still above target"
    assert "ETH-USD" in pipeline._pullback_watchlist
    print("✅ High price does not trigger prematurely.")

    # 3.3 Price pulls back to target (2600) -> triggers PULLBACK_RETEST
    fetcher.price = 2602.0  # within 1.002 of 2600
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert trig, "Should trigger when pullback hits target zone"
    assert sym == "ETH-USD"
    assert pipeline._priority_symbol == "ETH-USD"
    assert "ETH-USD" not in pipeline._pullback_watchlist
    print(f"✅ Pullback hit correctly triggered: {reason}, priority symbol set: {pipeline._priority_symbol}")

    # 3.4 Test Stale Expiration
    pipeline.register_pullback_candidate(
        symbol="SOL-USD",
        decision="LONG",
        current_price=150.0,
        directional_conf=80,
        conviction=65,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 140.0}},
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=1  # 1 second TTL
    )
    assert "SOL-USD" in pipeline._pullback_watchlist
    await asyncio.sleep(1.2)  # Wait for TTL to expire
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert not trig
    assert "SOL-USD" not in pipeline._pullback_watchlist
    print("✅ Stale signal expired and cleaned up from watchlist.")

    # 3.5 Test Invalidation Level (Crash through invalidation)
    pipeline.register_pullback_candidate(
        symbol="BTC-USD",
        decision="LONG",
        current_price=65000.0,
        directional_conf=80,
        conviction=65,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 64000.0}},
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=600
    )
    inv_price = pipeline._pullback_watchlist["BTC-USD"]["invalidation_price"]
    # Drop below invalidation price
    fetcher.price = inv_price - 100.0
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert not trig
    assert "BTC-USD" not in pipeline._pullback_watchlist
    print("✅ Invalidated signal discarded from watchlist.")

    print("\n=== TEST 4: Entry Quality Floor (< 60%) & Late Entry Protection ===")
    # 4.1 DeterministicGuard blocks any entry with Entry Quality < 60%
    poor_entry_proposal = {
        "decision": "LONG",
        "conviction": 55,
        "directional_confidence": 75,
        "entry_quality": 55,
        "trade_action": "ENTER"
    }
    dummy_risk = DummyRiskManager()
    dummy_profile = StrategyProfile(True, "TREND_FOLLOWING", "LONG", "Trend test")
    guard_res = DeterministicGuard.evaluate(
        strategy_profile=dummy_profile,
        ceo_proposal=poor_entry_proposal,
        profile="AGGRESSIVE",
        risk_manager=dummy_risk,
        symbol="ETH-USD"
    )
    assert guard_res.is_actionable is False, "Entry quality < 60 must be blocked"
    assert guard_res.rejection_tag == "POOR_ENTRY_QUALITY"
    print("✅ DeterministicGuard strictly blocks Entry Quality < 60% (rejection: POOR_ENTRY_QUALITY).")

    # 4.2 CEOAgent score validation: overextended price reduces Entry Quality and triggers WAIT_FOR_PULLBACK
    from agents.ceo_agent import CEOAgent
    ceo = CEOAgent(logger=services.logger, primary_llm=None, escalation_llm=None)
    overextended_context = {
        "price_data": {"current_price": 2700.0},
        "indicators": {
            "ema_20": 2640.0,  # ~2.2% overextended from EMA-20
            "atr_pct": 1.0,
            "rsi_14": 68.0,
            "bb_position_pct": 95.0
        }
    }
    breakdown_strong_bull = {
        "bull_argument": 40.0,
        "bear_argument": 5.0,
        "mtf_trend": 35.0
    }
    score_res = ceo._validate_and_compute_score("LONG", breakdown_strong_bull, market_context=overextended_context)
    assert score_res["decision"] == "LONG"
    assert score_res["directional_confidence"] >= 75
    # Overextension (-15) + BB exhaustion (-8) reduces entry_quality
    assert score_res["entry_quality"] < score_res["directional_confidence"]
    assert score_res["trade_action"] == "WAIT_FOR_PULLBACK", f"Expected WAIT_FOR_PULLBACK for overextended entry, got {score_res['trade_action']}"
    print(f"✅ Late entry detected: DirConf={score_res['directional_confidence']}%, EntryQuality={score_res['entry_quality']}%, Action={score_res['trade_action']}.")

    print("\nALL TESTS PASSED SUCCESSFULLY! 🎯")

if __name__ == "__main__":
    asyncio.run(run_tests())
