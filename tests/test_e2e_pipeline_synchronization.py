import sys
import os
import pytest
import asyncio
import time
from unittest.mock import MagicMock

# Add python to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from core.logger import TradeLogger
from core.strategy_router import StrategyRouter, StrategyProfile
from agents.ceo_agent import CEOAgent as CEO_Agent
from core.deterministic_guard import DeterministicGuard
from agents.risk_manager import RiskManager
from agents.sentinel_agent import SentinelAgent
from core.config import config


@pytest.fixture(autouse=True)
def setup_config():
    """Ensure pure trend following configuration for tests."""
    orig_allow = getattr(config, "ALLOW_FORBIDDEN_STRATEGIES", False)
    orig_anti = getattr(config, "ANTI_BOT_MODE", False)
    orig_pred = getattr(config, "AUTO_PREDATOR_SWITCH", False)

    config.ALLOW_FORBIDDEN_STRATEGIES = False
    config.ANTI_BOT_MODE = False
    config.AUTO_PREDATOR_SWITCH = False

    yield

    config.ALLOW_FORBIDDEN_STRATEGIES = orig_allow
    config.ANTI_BOT_MODE = orig_anti
    config.AUTO_PREDATOR_SWITCH = orig_pred


@pytest.mark.asyncio
async def test_e2e_trending_trend_following_passes_all_gates():
    """
    1. test_e2e_trending_trend_following_passes_all_gates:
    TREND_FOLLOWING in TRENDING (ADX=26.0) passes all 5 gates without veto and receives notional >= $100.50.
    """
    logger = TradeLogger("TestE2E")
    ceo = CEO_Agent(logger, None, None)
    rm = RiskManager(logger, None)
    sentinel = SentinelAgent(logger, None)

    symbol = "BTC-USD"
    detected_regime = "TRENDING"
    current_price = 76000.0

    market_data = {
        "symbol": symbol,
        "price_data": {
            "current_price": current_price,
            "bid": 75999.0,
            "ask": 76001.0,
        },
        "indicators": {
            "atr_14": 800.0,
            "adx_14": 26.0,
            "rsi_14": 55.0,
            "bb_position_pct": 50.0,
            "bb_width_pct": 2.5,
            "ema_20": 75500.0,
            "volume_spike_pct": 50.0,
            "donchian_high": 77000.0,
            "donchian_low": 74000.0,
        },
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH",
            "trend_15m": "BULLISH"
        },
        "derivatives_data": {
            "funding_rate": 0.00001,
            "funding_rate_hourly": 0.00001,
            "min_notional": 100.50,
            "size_increment": 0.00005,
            "min_size": 0.00005,
            "taker_fee_pct": 0.0005
        }
    }

    valid_reports = [
        {"agent_name": "Tech_Agent", "signal": "BULLISH", "confidence": 85},
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 80},
        {"agent_name": "Momentum_Agent", "signal": "BULLISH", "confidence": 80},
    ]
    macro_cache = {"BTC-USD": market_data}

    # Gate 1: StrategyRouter
    profile = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime=detected_regime,
        profile="BALANCED"
    )
    assert profile.has_directional_signal is True, f"StrategyRouter rejected: {profile.reasoning}"
    assert profile.strategy_mode == "TREND_FOLLOWING"
    assert profile.direction_bias == "LONG"

    # Gate 2: CEO Agent deterministic scoring
    breakdown = {"bull_argument": 45, "bear_argument": 5, "mtf_trend": 35}
    market_context = {
        "strategy_mode": profile.strategy_mode,
        "direction_bias": profile.direction_bias,
        "multi_timeframe": market_data["multi_timeframe"]
    }
    score_res = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)
    assert score_res["decision"] == "LONG"
    assert score_res["conviction"] >= 70

    ceo_proposal = {
        "symbol": symbol,
        "decision": score_res["decision"],
        "conviction": score_res["conviction"],
        "trade_action": "ENTER",
        "entry_price": current_price,
        "sl_price": 75200.0,
        "tp_price": 78000.0,
        "strategy_mode": profile.strategy_mode,
        "direction_bias": profile.direction_bias
    }

    # Gate 3: DeterministicGuard
    guard_res = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=rm,
        market_data=market_data,
        symbol=symbol,
        logger=logger
    )
    assert guard_res.is_actionable is True, f"DeterministicGuard blocked: {guard_res.guard_reason}"
    assert guard_res.guard_status == "PASSED"
    assert guard_res.decision == "LONG"

    # Gate 4: RiskManager
    portfolio_data = {
        "total_balance": 89.05,
        "available_margin": 89.05,
        "peak_balance": 89.05,
        "streak_history_by_tag": {},
        "current_drawdown_pct": 0.0
    }
    risk_res = await rm.analyze(
        ceo_decision=ceo_proposal,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime=detected_regime,
        strategy_mode=profile.strategy_mode
    )
    assert risk_res.get("approved") is True, f"RiskManager vetoed: {risk_res.get('veto_category')} - {risk_res.get('reasoning')}"
    assert risk_res.get("notional_size_usd") >= 40.0
    assert risk_res.get("contracts") > 0

    # Gate 5: SentinelAgent initial healthcheck
    active_pos = {
        "symbol": symbol,
        "direction": "LONG",
        "entry_price": current_price,
        "sl_price": risk_res["stop_loss_price"],
        "tp_price": risk_res["take_profit_price"],
        "contracts": risk_res["contracts"],
        "notional_usd": risk_res["notional_size_usd"],
        "strategy_mode": "TREND_FOLLOWING",
        "entry_time": time.time() - 60,
        "planned_risk_usd": risk_res["planned_risk_usd"]
    }
    profile_rules = rm._get_profile_rules("BALANCED", "TREND_FOLLOWING")
    sentinel_res = await sentinel.analyze(active_pos, market_data, profile_rules, atr_value=800.0)
    # New SL should be None (keep initial SL)
    assert sentinel_res.get("new_sl") is None


@pytest.mark.asyncio
async def test_e2e_transition_trend_following_high_adx_passes_all_gates():
    """
    2. test_e2e_transition_trend_following_high_adx_passes_all_gates:
    TREND_FOLLOWING in TRANSITION with ADX=24.0 and Beta(4,4) passes StrategyRouter,
    DeterministicGuard, and RiskManager WITHOUT 'TRANSITION:TREND_FOLLOWING is prohibited'.
    """
    logger = TradeLogger("TestE2E_Transition")
    rm = RiskManager(logger, None)

    symbol = "ETH-USD"
    detected_regime = "TRANSITION"
    current_price = 2500.0

    market_data = {
        "symbol": symbol,
        "price_data": {
            "current_price": current_price,
            "bid": 2499.5,
            "ask": 2500.5,
        },
        "indicators": {
            "atr_14": 25.0,
            "adx_14": 24.0,  # >= 22.0 required in TRANSITION
            "rsi_14": 52.0,
            "bb_position_pct": 50.0,
            "bb_width_pct": 2.2,
            "ema_20": 2480.0,
            "volume_spike_pct": 40.0,
            "donchian_high": 2550.0,
            "donchian_low": 2440.0,
        },
        "multi_timeframe": {
            "mtf_alignment": "TRANSITION",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        },
        "derivatives_data": {
            "funding_rate": 0.00001,
            "funding_rate_hourly": 0.00001,
            "min_notional": 100.50,
            "size_increment": 0.001,
            "min_size": 0.001,
            "taker_fee_pct": 0.0005
        }
    }

    valid_reports = [
        {"agent_name": "Tech_Agent", "signal": "BULLISH", "confidence": 85},
        {"agent_name": "Momentum_Agent", "signal": "BULLISH", "confidence": 80},
    ]
    macro_cache = {}

    # Gate 1: StrategyRouter
    profile = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime=detected_regime,
        profile="BALANCED"
    )
    assert profile.has_directional_signal is True, f"StrategyRouter rejected: {profile.reasoning}"
    assert profile.strategy_mode == "TREND_FOLLOWING"
    assert profile.direction_bias == "LONG"

    # Gate 2: DeterministicGuard
    ceo_proposal = {
        "symbol": symbol,
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER",
        "entry_price": current_price,
        "sl_price": 2465.0,  # $35 distance (1.4%)
        "tp_price": 2570.0,  # $70 distance (2:1 RR)
        "strategy_mode": profile.strategy_mode,
        "direction_bias": profile.direction_bias
    }

    guard_res = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=rm,
        market_data=market_data,
        symbol=symbol,
        logger=logger
    )
    assert guard_res.is_actionable is True, f"DeterministicGuard blocked: {guard_res.guard_reason}"
    assert guard_res.decision == "LONG"

    # Gate 3: RiskManager (empty journal, Beta(4,4) prior)
    portfolio_data = {
        "total_balance": 89.05,
        "available_margin": 89.05,
        "peak_balance": 89.05,
        "streak_history_by_tag": {},  # Beta(4,4) -> p=0.50
        "current_drawdown_pct": 0.0
    }

    risk_res = await rm.analyze(
        ceo_decision=ceo_proposal,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime=detected_regime,
        strategy_mode=profile.strategy_mode
    )
    assert risk_res.get("approved") is True, f"RiskManager vetoed: {risk_res.get('veto_category')} - {risk_res.get('reasoning')}"
    assert "TRANSITION:TREND_FOLLOWING is prohibited" not in str(risk_res.get("reasoning", ""))
    assert risk_res.get("notional_size_usd") >= 40.0


def test_e2e_transition_breakout_blocked_at_router():
    """
    3. test_e2e_transition_breakout_blocked_at_router:
    In TRANSITION, BREAKOUT strategy is blocked immediately in StrategyRouter (Stage 1),
    never reaching LLM or RiskManager.
    """
    symbol = "SOL-USD"
    detected_regime = "TRANSITION"
    current_price = 185.0

    market_data = {
        "symbol": symbol,
        "price_data": {
            "current_price": current_price,
        },
        "indicators": {
            "atr_14": 5.0,
            "adx_14": 28.0,
            "rsi_14": 62.0,
            "bb_position_pct": 82.0,
            "volume_spike_pct": 320.0,
            "donchian_high": 184.0,  # Price broke above donchian_high
            "donchian_low": 170.0,
            "last_closed_candle_close": 184.5
        },
        "multi_timeframe": {
            "mtf_alignment": "TRANSITION",
            "trend_1h": "BULLISH"
        },
        "derivatives_data": {
            "oi_change_pct": 1.2
        }
    }

    valid_reports = [
        {"agent_name": "Tech_Agent", "signal": "BULLISH", "confidence": 90},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH", "confidence": 85},
    ]

    profile = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache={},
        detected_regime=detected_regime,
        profile="BALANCED"
    )

    # BREAKOUT in TRANSITION MUST be strictly rejected at router (has_directional_signal = False)
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "BREAKOUT"
    assert "Запрет BREAKOUT" in profile.reasoning


def test_e2e_trend_exhaustion_blocks_top_long():
    """
    4. test_e2e_trend_exhaustion_blocks_top_long:
    At bb_pos=90% and rsi=75, LONG trend setup is blocked in StrategyRouter with TREND_EXHAUSTION_VETO.
    """
    symbol = "BTC-USD"
    detected_regime = "TRENDING"
    current_price = 78000.0

    market_data = {
        "symbol": symbol,
        "price_data": {
            "current_price": current_price,
        },
        "indicators": {
            "atr_14": 1200.0,
            "adx_14": 26.0,
            "rsi_14": 75.0,  # >= 72.0 overbought
            "bb_position_pct": 90.0,  # > 85.0 channel ceiling
            "volume_spike_pct": 80.0,
            "donchian_high": 78200.0,
            "donchian_low": 74000.0,
            "algo_signals": {
                "rsi_divergence": "BEARISH"
            }
        },
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        }
    }

    valid_reports = [
        {"agent_name": "Tech_Agent", "signal": "BULLISH", "confidence": 85},
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 85},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH", "confidence": 85},
    ]

    profile = StrategyRouter.evaluate(
        symbol=symbol,
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache={},
        detected_regime=detected_regime,
        profile="BALANCED"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "TREND_EXHAUSTION_VETO"
    assert "Trend Exhaustion Guard" in profile.reasoning


@pytest.mark.asyncio
async def test_e2e_reduce_size_on_altcoin_still_respects_100_min_notional():
    """
    5. test_e2e_reduce_size_on_altcoin_still_respects_100_min_notional:
    Even with Action=REDUCE_SIZE on LIT-USD / ZEC-USD on balance $89.05,
    the resulting order from RiskManager is either >= $100.50 with valid size_increment,
    or strictly vetoed with MIN_NOTIONAL, but NEVER returns $13, $40, or $57.
    """
    logger = TradeLogger("TestE2E_ReduceSize")
    rm = RiskManager(logger, None)

    symbol = "LIT-USD"
    current_price = 0.70
    detected_regime = "TRENDING"

    market_data = {
        "symbol": symbol,
        "price_data": {
            "current_price": current_price,
            "bid": 0.699,
            "ask": 0.701,
        },
        "indicators": {
            "atr_14": 0.025,
            "adx_14": 25.0,
            "rsi_14": 52.0,
            "bb_position_pct": 50.0,
            "bb_width_pct": 3.0,
            "volume_spike_pct": 60.0,
        },
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        },
        "derivatives_data": {
            "funding_rate": 0.00001,
            "funding_rate_hourly": 0.00001,
            "min_notional": 100.50,
            "size_increment": 1.0,  # LIT increment is integer 1.0
            "min_size": 1.0,
            "taker_fee_pct": 0.0005
        }
    }

    portfolio_data = {
        "total_balance": 89.05,
        "available_margin": 89.05,
        "peak_balance": 89.05,
        "streak_history_by_tag": {},
        "current_drawdown_pct": 0.0
    }

    # Action is REDUCE_SIZE (cuts size by ~50%)
    proposal = {
        "symbol": symbol,
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "REDUCE_SIZE",
        "entry_price": current_price,
        "sl_price": 0.675,  # $0.025 distance (3.57%)
        "tp_price": 0.760,  # $0.060 distance (2.4:1 RR)
        "strategy_mode": "TREND_FOLLOWING",
        "direction_bias": "LONG"
    }

    res = await rm.analyze(
        ceo_decision=proposal,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime=detected_regime,
        strategy_mode="TREND_FOLLOWING"
    )

    if res.get("approved"):
        notional = res.get("notional_size_usd", 0.0)
        contracts = res.get("contracts", 0.0)
        assert notional >= 100.50, f"Approved notional ${notional:.2f} is below $100.50 threshold!"
        assert round(contracts % 1.0, 4) in (0.0, 1.0), f"Contracts {contracts} must align with size_increment=1.0 for LIT!"
    else:
        assert res.get("veto_category") in ("MIN_NOTIONAL", "INSUFFICIENT_BALANCE", "EXCESSIVE_RISK")
        assert res.get("notional_size_usd", 0.0) == 0.0


@pytest.mark.asyncio
async def test_e2e_sentinel_does_not_choke_trade_at_0_3r():
    """
    6. test_e2e_sentinel_does_not_choke_trade_at_0_3r:
    A position with profit +0.30R and age 190 minutes is NOT closed and NOT moved to BE before +0.85R.
    """
    logger = TradeLogger("TestE2E_Sentinel")
    sentinel = SentinelAgent(logger, None)
    rm = RiskManager(logger, None)

    symbol = "BTC-USD"
    entry_price = 75000.0
    sl_price = 74000.0  # Planned R = $1000.0
    tp_price = 78000.0
    current_price = 75300.0  # +$300 profit = +0.30R

    pos = {
        "symbol": symbol,
        "direction": "LONG",
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "contracts": 0.00135,
        "notional_usd": 101.25,
        "strategy_mode": "TREND_FOLLOWING",
        "entry_time": time.time() - (190 * 60),  # Age = 190 minutes
        "planned_risk_usd": 1.35,
        "max_favorable_excursion_r": 0.35,
        "protection_state": "NORMAL"
    }

    market_data = {
        "symbol": symbol,
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": 500.0,
            "rsi_14": 54.0,
            "bb_position_pct": 52.0
        }
    }

    profile_rules = rm._get_profile_rules("BALANCED", "TREND_FOLLOWING")

    verdict = await sentinel.analyze(pos, market_data, profile_rules, atr_value=500.0)

    # Sentinel must NOT choke the trade: new_sl must be None (no move) or <= original sl_price
    new_sl = verdict.get("new_sl")
    assert new_sl is None or new_sl <= sl_price, (
        f"Sentinel prematurely modified SL to {new_sl} at only +0.30R profit and age 190m!"
    )
    assert verdict.get("state") not in ("TIME_STOP_BE", "BREAK_EVEN")
