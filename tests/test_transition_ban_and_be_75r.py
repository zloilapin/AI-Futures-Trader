import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from agents.risk_manager import RiskManager
from agents.sentinel_agent import SentinelAgent
from services.partial_tp_calculator import PartialTPCalculator
from core.logger import TradeLogger


@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


@pytest.fixture
def mock_llm():
    return MagicMock()


def test_risk_manager_vetoes_transition_breakout(mock_logger, mock_llm):
    """
    Verifies that RiskManager strictly vetoes BREAKOUT in TRANSITION regime
    due to high risk of false breakout wick traps.
    """
    rm = RiskManager(mock_logger, mock_llm)
    candidate = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "trade_action": "ENTER",
        "direction": "LONG",
        "conviction": 85,
        "entry_price": 81000.0,
        "stop_loss_price": 80200.0,
        "take_profit_price": 83000.0,
        "strategy_mode": "BREAKOUT",
        "regime": "TRANSITION"
    }
    market_data = {
        "price_data": {"current_price": 81000.0, "ohlcv_1h": []},
        "order_book_data": {"spread_pct": 0.01},
        "indicators": {"atr_14": 500.0}
    }
    portfolio_data = {
        "total_usd": 100.0,
        "current_balance": 100.0,
        "available_margin": 100.0,
        "total_equity_usd": 100.0,
        "available_margin_usd": 100.0,
        "active_positions": {},
        "win_count": 10,
        "loss_count": 5,
        "streak_history_by_tag": {}
    }

    res = asyncio.run(rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        macro_regime="TRANSITION",
        strategy_mode="BREAKOUT"
    ))

    assert res["approved"] is False
    assert res["veto_category"] == "REGIME_STRATEGY_MISMATCH"
    assert "BREAKOUT is prohibited in TRANSITION" in res["reasoning"]


def test_risk_manager_permits_transition_trend_following(mock_logger, mock_llm):
    """
    Verifies that RiskManager permits TREND_FOLLOWING in TRANSITION regime
    when Bayes prior is clean Beta(4.0, 4.0) (posterior prob >= 0.42).
    """
    rm = RiskManager(mock_logger, mock_llm)
    candidate = {
        "symbol": "ETH-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "direction": "SHORT",
        "conviction": 85,
        "entry_price": 2320.0,
        "stop_loss_price": 2345.0,
        "take_profit_price": 2260.0,
        "strategy_mode": "TREND_FOLLOWING",
        "regime": "TRANSITION"
    }
    market_data = {
        "price_data": {"current_price": 2320.0, "ohlcv_1h": [{"volume": 100} for _ in range(12)]},
        "order_book_data": {"spread_pct": 0.01},
        "indicators": {"atr_14": 25.0}
    }
    portfolio_data = {
        "total_usd": 100.0,
        "current_balance": 100.0,
        "available_margin": 100.0,
        "total_equity_usd": 100.0,
        "available_margin_usd": 100.0,
        "active_positions": {},
        "win_count": 10,
        "loss_count": 5,
        "streak_history_by_tag": {
            "TRANSITION:TREND_FOLLOWING": {"wins": 1.0, "losses": 0.0, "total": 1}
        }
    }

    res = asyncio.run(rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        macro_regime="TRANSITION",
        strategy_mode="TREND_FOLLOWING"
    ))

    assert res["approved"] is True
    assert res["bayes_strategy_multiplier"] >= 1.0


def test_strategy_router_strictly_rejects_trend_following_low_adx_in_transition():
    """
    Verifies that StrategyRouter strictly rejects TREND_FOLLOWING when regime or MTF
    is TRANSITION and ADX < 22.0 (e.g. ADX=21.1), preventing deadlock and saving LLM tokens.
    """
    from core.strategy_router import StrategyRouter
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "TRANSITION",
            "trend_1h": "BEARISH"
        },
        "indicators": {
            "adx_14": 21.1,  # < 22.0
            "bb_position_pct": 50.0,
            "volume_spike_pct": 50.0
        },
        "price_data": {"current_price": 100.0}
    }
    reports = [
        {"agent_name": "Trend_Agent", "signal": "BEARISH"},
        {"agent_name": "Candle_Agent", "signal": "BEARISH"}
    ]
    profile = StrategyRouter.evaluate(
        "BTC-USD", market_data, reports, {}, detected_regime="TRANSITION"
    )
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "TREND_FOLLOWING"
    assert "ADX=21.1 < 22.0 в фазе TRANSITION" in profile.reasoning


def test_risk_manager_vetoes_negative_bayesian_probability_below_40_pct(mock_logger, mock_llm):
    """
    Verifies that RiskManager vetoes any regime:strategy bucket where posterior win probability
    falls below 40% (p_bayes < 0.40) with at least 1.5 losses / 2 trades.
    """
    rm = RiskManager(mock_logger, mock_llm)
    candidate = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "trade_action": "ENTER",
        "direction": "LONG",
        "conviction": 85,
        "entry_price": 100.0,
        "stop_loss_price": 98.0,
        "take_profit_price": 105.0,
        "strategy_mode": "TREND_FOLLOWING",
        "regime": "TRENDING"
    }
    market_data = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": []},
        "order_book_data": {"spread_pct": 0.01},
        "indicators": {"atr_14": 2.0}
    }
    # Bucket with 0 wins and 3 losses: Beta(4, 7) -> 4 / 11 = 36.4% < 40.0%
    portfolio_data = {
        "total_usd": 100.0,
        "current_balance": 100.0,
        "available_margin": 100.0,
        "total_equity_usd": 100.0,
        "available_margin_usd": 100.0,
        "active_positions": {},
        "win_count": 10,
        "loss_count": 5,
        "streak_history_by_tag": {
            "TRENDING:TREND_FOLLOWING": {"wins": 0.0, "losses": 10.0, "total": 10}
        }
    }

    res = asyncio.run(rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        macro_regime="TRENDING",
        strategy_mode="TREND_FOLLOWING"
    ))

    assert res["approved"] is False
    assert res["veto_category"] == "NEGATIVE_BAYESIAN_EXPECTANCY"
    assert "< 40.0%" in res["reasoning"]


def test_partial_tp_calculator_anchors_tier1_to_75r():
    """
    Verifies that PartialTPCalculator anchors Tier 1 TP target to 0.75R (be_activation_r)
    when sl_price is provided and tp1_r_mult=0.75 in 2-tier mode.
    """
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.01,
        "tp1_r_mult": 0.75
    }
    # LONG: Entry 100.0, SL 98.0 (1R = 2.0). 0.75R = 1.50.
    # Final TP: 106.0 (Delta TP = 6.0). 0.75R target is 101.50 (fraction = 1.50 / 6.0 = 0.25).
    plan_long = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=106.0,
        total_contracts=1.0,
        market_params=market_params,
        preferred_mode="2_TIER",
        sl_price=98.0
    )

    assert plan_long.mode == "2_TIER"
    assert len(plan_long.tiers) == 2
    t1 = plan_long.tiers[0]
    assert t1.target_price == pytest.approx(101.50)
    assert t1.action == "PROTECT_BREAKEVEN"

    # SHORT: Entry 100.0, SL 102.0 (1R = 2.0). 0.75R = 1.50.
    # Final TP: 94.0. 0.75R target is 98.50.
    plan_short = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="SHORT",
        entry_price=100.0,
        tp_final_price=94.0,
        total_contracts=1.0,
        market_params=market_params,
        preferred_mode="2_TIER",
        sl_price=102.0
    )
    assert plan_short.tiers[0].target_price == pytest.approx(98.50)


def test_sentinel_triggers_be_at_75r(mock_logger):
    """
    Verifies that SentinelAgent triggers Break-Even when price reaches +0.75R.
    """
    agent = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "be_activation_r": 0.75,
        "sentinel_trail_activation_atr": 2.5,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25
    }
    eval_atr = 100.0

    # Entry 10000, initial SL 9800 (1R = 200). 0.75R = 150.
    # At price 10150 (+150 pts = +0.75R), Break-Even MUST trigger!
    pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "sl_price": 9800.0,
        "highest_price": 10150.0,
        "protection_state": "PROTECTED"
    }
    market_data = {"price_data": {"current_price": 10150.0}}

    res = asyncio.run(agent.analyze(pos, market_data, profile_rules, eval_atr))
    assert res["state"] == "BREAK_EVEN"
    assert res["new_sl"] == pytest.approx(10020.0)
