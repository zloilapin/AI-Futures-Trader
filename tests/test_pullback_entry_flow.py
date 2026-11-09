import os
import sys
import asyncio
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../python')))

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
    
    # 1.1 Strategy mode is BREAKOUT with confirmed Donchian break & volume -> should override to ENTER
    market_data_breakout_long = {
        "price_data": {"current_price": 2720.0},
        "indicators": {
            "donchian_high": 2700.0,
            "volume_spike_pct": 140.0
        }
    }
    res = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=risk_mgr,
        market_data=market_data_breakout_long,
        symbol="ETH-USD"
    )
    assert res.decision == "LONG"
    assert res.trade_action == "ENTER", f"Expected ENTER, got {res.trade_action}"
    assert res.is_actionable is True, f"Expected actionable True, got {res.is_actionable}"
    print("✅ Confirmed BREAKOUT overrides WAIT_FOR_PULLBACK to ENTER successfully.")

    # 1.2 Strategy mode is VOLATILITY_MOMENTUM with confirmed Donchian breakdown + volume surge -> overrides to ENTER
    vm_profile = StrategyProfile(True, "VOLATILITY_MOMENTUM", "SHORT", "Volatility momentum test")
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
        strategy_profile=vm_profile,
        ceo_proposal=dict(short_proposal),
        profile="BALANCED",
        risk_manager=risk_mgr,
        market_data=market_data_breakout,
        symbol="BTC-USD"
    )
    assert res2.decision == "SHORT"
    assert res2.trade_action == "ENTER", f"Expected ENTER on confirmed Donchian break for VOLATILITY_MOMENTUM, got {res2.trade_action}"
    assert res2.is_actionable is True
    print("✅ Confirmed breakdown via indicators overrides WAIT_FOR_PULLBACK to ENTER for VOLATILITY_MOMENTUM.")

    # 1.3 Strategy mode is TREND_FOLLOWING: must PRESERVE WAIT_FOR_PULLBACK even if Donchian channel is broken (No FOMO top-buying)
    tf_profile = StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Trend following test")
    res_tf = DeterministicGuard.evaluate(
        strategy_profile=tf_profile,
        ceo_proposal=dict(short_proposal),
        profile="BALANCED",
        risk_manager=risk_mgr,
        market_data=market_data_breakout,
        symbol="BTC-USD"
    )
    assert res_tf.decision == "SHORT"
    assert res_tf.trade_action == "WAIT_FOR_PULLBACK", f"Expected WAIT_FOR_PULLBACK preserved for TREND_FOLLOWING, got {res_tf.trade_action}"
    assert res_tf.is_actionable is False
    assert res_tf.guard_status == "PULLBACK_WATCHLIST"
    print("✅ Confirmed TREND_FOLLOWING strictly preserves WAIT_FOR_PULLBACK to prevent entering at extremes.")

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
        "bull_argument": 45.0,
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

    print("\n=== TEST 5: Bug Fixes Verification (80% Distance, Retest Promotion & Direction Retention) ===")
    # 5.1 SHORT pullback 80% distance test
    market_data_short = {"indicators": {"ema_20": 2406.70}}
    pipeline.register_pullback_candidate(
        symbol="ETH-SHORT",
        decision="SHORT",
        current_price=2399.50,
        directional_conf=81,
        conviction=61,
        strategy_mode="TREND_FOLLOWING",
        market_data=market_data_short,
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=600
    )
    target_price = pipeline._pullback_watchlist["ETH-SHORT"]["target_pullback_price"]
    signal_price = pipeline._pullback_watchlist["ETH-SHORT"]["signal_price"]
    dist = target_price - signal_price
    threshold_80 = target_price - (0.20 * dist)

    # At price below 80% distance, it should NOT trigger!
    fetcher.price = threshold_80 - 1.0
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert not trig, f"Price at {fetcher.price:.2f} should not trigger premature pullback retest (< {threshold_80:.2f})"
    assert "ETH-SHORT" in pipeline._pullback_watchlist
    print(f"✅ Premature pullback trigger prevented ({fetcher.price:.2f} < {threshold_80:.2f} threshold).")

    # At price >= 80% distance, it SHOULD trigger!
    fetcher.price = threshold_80 + 0.5
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert trig, f"Price at {fetcher.price:.2f} must trigger pullback retest (>= {threshold_80:.2f})"
    assert sym == "ETH-SHORT"
    assert pipeline._priority_symbol == "ETH-SHORT"
    assert pipeline._priority_direction == "SHORT"
    print(f"✅ Pullback triggered at 80% distance threshold ({fetcher.price:.2f} >= {threshold_80:.2f}) with correct direction (SHORT).")

    # 5.2 DeterministicGuard Pullback Retest Promotion: Cycle #2 HYPE scenario (DirConf=77%, raw EQ=52% -> effective EQ=67% -> REDUCE_SIZE)
    retest_proposal = {
        "decision": "SHORT",
        "conviction": 52,
        "directional_confidence": 77,
        "entry_quality": 52,
        "trade_action": "WAIT_FOR_PULLBACK",
        "is_pullback_retest": True
    }
    short_profile = StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Short retest")
    guard_retest_res = DeterministicGuard.evaluate(
        strategy_profile=short_profile,
        ceo_proposal=retest_proposal,
        profile="BALANCED",
        risk_manager=dummy_risk,
        market_data={"is_pullback_retest": True},
        symbol="HYPE-USD"
    )
    assert guard_retest_res.trade_action == "REDUCE_SIZE", f"Expected REDUCE_SIZE for EQ=67%, got {guard_retest_res.trade_action}"
    assert guard_retest_res.conviction >= guard_retest_res.min_conviction, f"Expected conviction lifted to at least min_conv ({guard_retest_res.min_conviction}), got {guard_retest_res.conviction}"
    assert guard_retest_res.is_actionable is True, "Retest with DirConf=77% and effective EQ=67% must be actionable"
    print("✅ Retest setup promoted to REDUCE_SIZE with lifted conviction (Cycle #2 HYPE scenario resolved).")

    # 5.3 StrategyRouter honors pullback_retest_direction even when short-term indicators are bullish
    from core.strategy_router import StrategyRouter
    retest_market_data = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "SHORT",
        "multi_timeframe": {"trend_1h": "BEARISH"},
        "indicators": {"bb_position_pct": 60.0, "volume_spike_pct": 20.0},
        "price_data": {"current_price": 78.24}
    }
    # Analyst reports have tech_bulls > tech_bears (2 bulls vs 1 bear) due to upward pullback bounce
    bullish_analyst_reports = [
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"},
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "Trend_Agent", "signal": "BEARISH"}
    ]
    routed_profile = StrategyRouter.evaluate(
        "HYPE-USD", retest_market_data, bullish_analyst_reports, {}, detected_regime="TRENDING_BEAR"
    )
    assert routed_profile.direction_bias == "SHORT", f"Expected SHORT, got {routed_profile.direction_bias}"
    assert "PULLBACK_RETEST SHORT" in routed_profile.reasoning
    print("✅ StrategyRouter correctly preserved SHORT direction despite bullish 15m oscillators.")

    print("\n=== TEST 6: Watchlist Deduplication & Directional Cluster Cap Protection ===")
    # 6.1 remove_pullback_candidate by base ticker or full ticker
    pipeline._pullback_watchlist.clear()
    pipeline.register_pullback_candidate(
        symbol="HYPE-USD",
        decision="SHORT",
        current_price=75.40,
        directional_conf=80,
        conviction=65,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 76.50}},
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=600
    )
    assert "HYPE-USD" in pipeline._pullback_watchlist
    # Remove by base ticker "HYPE"
    removed = pipeline.remove_pullback_candidate("HYPE", reason="order filled")
    assert removed is True
    assert "HYPE-USD" not in pipeline._pullback_watchlist
    print("✅ remove_pullback_candidate successfully cleared HYPE-USD by base ticker 'HYPE'.")

    # 6.2 Active positions auto-cleanup in check_pullback_watchlist
    pipeline.register_pullback_candidate(
        symbol="SOL-USD",
        decision="SHORT",
        current_price=100.0,
        directional_conf=80,
        conviction=65,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 102.0}},
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=600
    )
    # Simulate active position on SOL
    pipeline.services.trading_service.active_positions["SOL"] = {"symbol": "SOL-USD", "direction": "SHORT"}
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert trig is False
    assert "SOL-USD" not in pipeline._pullback_watchlist
    print("✅ check_pullback_watchlist automatically cleans up candidates that already have active positions.")

    # 6.3 Directional cluster cap (2/2 SHORT) prevents wake-up
    pipeline.services.trading_service.active_positions.clear()
    pipeline.services.trading_service.active_positions["BTC"] = {"symbol": "BTC-USD", "direction": "SHORT"}
    pipeline.services.trading_service.active_positions["ETH"] = {"symbol": "ETH-USD", "direction": "SHORT"}
    pipeline.register_pullback_candidate(
        symbol="AVAX-USD",
        decision="SHORT",
        current_price=20.0,
        directional_conf=80,
        conviction=65,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 20.5}},
        ceo_verdict={},
        strategy_profile=None,
        ttl_seconds=600
    )
    # Even if target price reached, 2/2 SHORT is full so pipeline should NOT wake up
    fetcher.price = 20.5
    trig, sym, reason = await pipeline.check_pullback_watchlist()
    assert trig is False, "Directional cluster cap (2/2 SHORT) must block wake-up"
    assert "AVAX-USD" in pipeline._pullback_watchlist, "Watchlist item should be kept while cap is temporarily full"
    print("✅ Directional cluster cap (2/2 SHORT) successfully prevents waking the pipeline.")

    pipeline.services.trading_service.active_positions.clear()
    pipeline._pullback_watchlist.clear()

    print("\n=== TEST 7: FastRadar Volume Filter, Cluster Cap & Macro Leader Guard ===")
    # 7.1 FastRadar volume confirmation (vol_ratio >= 1.3 or |delta_pct| >= 1.2%)
    class RadarFetcher:
        def __init__(self, price=99.4, volume=10.0, avg_vol=10.0, delta_pct=-0.6):
            self.price = price
            self.volume = volume
            self.avg_vol = avg_vol
            self.delta_pct = delta_pct
        async def fetch_ohlcv(self, symbol):
            # open price calculated from delta_pct
            open_p = 100.0
            candles = []
            # 20 history candles around 100.0 (lows at 99.8)
            for _ in range(20):
                candles.append({"open": 100.0, "close": 100.0, "high": 100.5, "low": 99.8, "volume": self.avg_vol})
            # Current candle (last) with close = self.price (< donchian_low 99.8)
            candles.append({"open": open_p, "close": self.price, "high": open_p, "low": self.price, "volume": self.volume})
            return {"current_price": self.price, "candles_20": candles}

    # Low volume breakdown (Δ=-0.6%, Vol=0.8x) -> Must be rejected by FastRadar
    pipeline.services.fetcher = RadarFetcher(price=99.4, volume=8.0, avg_vol=10.0, delta_pct=-0.6)
    pipeline._fast_radar_cooldowns.clear()
    pipeline._last_universe_symbols = ["ZEC-USD"]
    trig, sym, reason = await pipeline.check_fast_market_momentum()
    assert trig is False, "Low volume breakdown (< 1.3x) must NOT trigger FastRadar"
    print("✅ FastRadar successfully filtered out low volume breakdown (Vol=0.8x < 1.3x).")

    # Confirmed volume breakdown (Δ=-0.6%, Vol=1.5x) -> Must trigger FastRadar
    pipeline.services.fetcher = RadarFetcher(price=99.4, volume=15.0, avg_vol=10.0, delta_pct=-0.6)
    pipeline._fast_radar_cooldowns.clear()
    trig, sym, reason = await pipeline.check_fast_market_momentum()
    assert trig is True and sym == "BTC-USD"
    print("✅ FastRadar successfully triggered on confirmed volume breakdown (Vol=1.5x >= 1.3x).")

    # FastRadar cluster cap: when 2/2 SHORT slots full, do not trigger SHORT breakdown
    pipeline.services.trading_service.active_positions["POS1"] = {"symbol": "BTC-USD", "direction": "SHORT"}
    pipeline.services.trading_service.active_positions["POS2"] = {"symbol": "ETH-USD", "direction": "SHORT"}
    pipeline._fast_radar_cooldowns.clear()
    trig, sym, reason = await pipeline.check_fast_market_momentum()
    assert trig is False, "FastRadar must NOT trigger breakdown when 2/2 SHORT positions are active"
    print("✅ FastRadar directional cluster cap (2/2 SHORT) successfully blocked waking.")
    pipeline.services.trading_service.active_positions.clear()

    # 7.2 Macro Leader Guard: can_apply_profile_uplift
    # Case A: Altcoin LONG with BTC/ETH BEARISH -> Blocked!
    leaders_state = {"BTC": "BEARISH", "ETH": "BEARISH"}
    can_up, up_msg = pipeline.can_apply_profile_uplift("BNB-USD", "LONG", leaders_state)
    assert can_up is False
    assert "Поводыри (BTC, ETH) в медвежьем тренде" in up_msg
    print("✅ Asset Profile Uplift correctly blocked for altcoin LONG when BTC/ETH are BEARISH.")

    # Case B: Altcoin SHORT with BTC BEARISH -> Allowed!
    can_up_short, up_msg_short = pipeline.can_apply_profile_uplift("SOL-USD", "SHORT", leaders_state)
    assert can_up_short is True
    print("✅ Asset Profile Uplift allowed for altcoin SHORT aligned with BEARISH leaders.")

    # Case C: Leader itself (BTC/ETH) or uncorrelated gold (XAUT) -> Always allowed
    can_up_btc, _ = pipeline.can_apply_profile_uplift("BTC-USD", "LONG", leaders_state)
    assert can_up_btc is True
    can_up_gold, _ = pipeline.can_apply_profile_uplift("XAUT-USD", "LONG", leaders_state)
    assert can_up_gold is True
    print("✅ Leaders (BTC/ETH) and uncorrelated assets (XAUT) exempt from leader guard.")

    print("\n=== TEST 8: Patches 1-4 Verification (Range Chop Uplift, Pre-Debate Anti-Chop, CEO Retest Memory) ===")
    # 8.1 Uplift blocked in RANGE_CHOPPY
    can_up_chop, chop_msg = pipeline.can_apply_profile_uplift("BTC-USD", "LONG", leaders_state, macro_regime="RANGE_CHOPPY")
    assert can_up_chop is False
    assert "RANGE_CHOPPY" in chop_msg
    print("✅ Asset Profile Uplift strictly blocked in RANGE_CHOPPY macro regime.")

    # 8.2 Pre-Debate Anti-Chop in StrategyRouter
    from core.strategy_router import StrategyRouter
    market_data_overbought = {
        "multi_timeframe": {"mtf_alignment": "TRANSITION", "trend_1h": "BULLISH"},
        "indicators": {"bb_position_pct": 91.5, "rsi_14": 68.0},
        "price_data": {"current_price": 100.0}
    }
    router_prof = StrategyRouter.evaluate(
        market_data=market_data_overbought,
        valid_reports=[{"agent_name": "Trend_Agent", "signal": "BULLISH"}],
        detected_regime="RANGE_CHOPPY",
        symbol="HYPE-USD",
        macro_cache={}
    )
    assert router_prof.has_directional_signal is False
    assert "Анти-распил" in router_prof.reasoning
    print(f"✅ Pre-Debate Anti-Chop blocked overextended LONG before debate: {router_prof.reasoning}")

    # 8.3 CEOAgent Retest Memory and Approval of Counter-Trend Pullback
    from agents.ceo_agent import CEOAgent
    class LocalDummyLogger:
        def info(self, *a, **k): pass
        def debug(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
    dummy_ceo = CEOAgent(logger=LocalDummyLogger(), primary_llm=None, escalation_llm=None)
    # Simulate LLM returning HOLD due to 15m red pullback candle:
    breakdown_neutral = {
        "technical_structure_weight": 0,
        "derivatives_flow_weight": 0,
        "risk_penalty": 0
    }
    retest_ctx = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "LONG",
        "pullback_context": {
            "direction": "LONG",
            "signal_price": 1208.0,
            "target_pullback_price": 1201.0,
            "invalidation_price": 1192.0,
            "directional_confidence": 86
        }
    }
    score_res = dummy_ceo._validate_and_compute_score("HOLD", breakdown_neutral, market_context=retest_ctx)
    assert score_res["decision"] == "LONG"
    assert score_res["trade_action"] in ["ENTER", "REDUCE_SIZE"]
    assert score_res["directional_confidence"] >= 75
    print(f"✅ CEO Retest Memory restored LONG thesis from HOLD: Decision={score_res['decision']}, Action={score_res['trade_action']}, Conf={score_res['directional_confidence']}%.")

    print("\nALL TESTS PASSED SUCCESSFULLY! 🎯")

if __name__ == "__main__":
    asyncio.run(run_tests())
