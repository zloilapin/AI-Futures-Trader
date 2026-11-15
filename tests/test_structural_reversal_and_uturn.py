import pytest
import time
import math
from unittest.mock import MagicMock, AsyncMock, patch

from core.models import FinalTradeDecision
from core.strategy_router import StrategyRouter, StrategyProfile
from core.deterministic_guard import DeterministicGuard, evaluate_structural_reversal_state
from agents.risk_manager import RiskManager
from core.pipeline import Pipeline, check_cross_asset_reversal_breadth


# ═════════════════════════════════════════════════════════════════════════════
# MODULE A: Deterministic Structural Reversal (CHoCH / U-Turn) Detector Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_bullish_choch_reversal_detection_during_lagging_bearish_macro():
    """
    Module A: Bullish CHoCH detection when lagging 4H trend is still BEARISH.
    Condition 1: Dynamic mean reclaim (cur_px > ema_20_15m and cur_px > ema_20_1h / vwap).
    Condition 2: Structure shift (Higher Low or CHoCH break above 15m swing high).
    Condition 3: Technical consensus (>= 2 technical agents BULLISH with >= 58% conf).
    Result: is_bullish_reversal = True, reversal_direction = 'LONG'.
    """
    symbol_data = {
        "price_data": {
            "current_price": 105.0,
            "candles_20": [
                {"open": 98.0, "high": 100.0, "low": 96.0, "close": 97.0},
                {"open": 97.0, "high": 102.0, "low": 96.5, "close": 101.0}, # Higher Low (96.5 > 96.0)
                {"open": 101.0, "high": 106.0, "low": 100.5, "close": 105.0} # Higher Low (100.5 > 96.5)
            ]
        },
        "indicators": {
            "current_price": 105.0,
            "ema_20_15m": 101.0,
            "ema_20_1h": 103.0,
            "vwap": 102.0,
            "swing_high_15m": 104.0, # Price 105.0 broke above 104.0 (CHoCH BOS)
            "macro_box_pos_pct": 30.0,
            "rsi_15m": 58.0,
            "plus_di_14": 28.0,
            "minus_di_14": 16.0
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT",
            "tf_15m": {"trend": "BULLISH", "ema_20": 101.0},
            "tf_1h": {"trend": "BEARISH", "ema_20": 103.0}
        }
    }

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 65.0},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 62.0},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL", "confidence": 50.0},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH", "confidence": 52.0}
    ]

    rev_state = evaluate_structural_reversal_state(
        symbol_data=symbol_data,
        analyst_reports=analyst_reports,
        macro_trend="BEARISH",
        macro_regime="TRANSITION"
    )

    assert rev_state["is_bullish_reversal"] is True
    assert rev_state["is_bearish_reversal"] is False
    assert rev_state["reversal_direction"] == "LONG"
    assert rev_state["conditions_met"]["bullish"]["mean_reclaim"] is True
    assert rev_state["conditions_met"]["bullish"]["structure_shift"] is True
    assert rev_state["conditions_met"]["bullish"]["analyst_consensus"] is True
    assert rev_state["conditions_met"]["bullish"]["count"] == 3


def test_bearish_choch_reversal_detection_during_lagging_bullish_macro():
    """
    Module A: Symmetric Bearish CHoCH detection when lagging 4H trend is still BULLISH.
    Condition 1: Dynamic mean breakdown (cur_px < ema_20_15m and cur_px < ema_20_1h / vwap).
    Condition 2: Structure shift (Lower High or CHoCH break below 15m swing low).
    Condition 3: Technical consensus (>= 2 technical agents BEARISH with >= 58% conf).
    Result: is_bearish_reversal = True, reversal_direction = 'SHORT'.
    """
    symbol_data = {
        "price_data": {
            "current_price": 95.0,
            "candles_20": [
                {"open": 102.0, "high": 105.0, "low": 100.0, "close": 103.0},
                {"open": 103.0, "high": 104.0, "low": 98.0, "close": 99.0}, # Lower High (104.0 < 105.0)
                {"open": 99.0, "high": 100.0, "low": 94.0, "close": 95.0}  # Lower High (100.0 < 104.0)
            ]
        },
        "indicators": {
            "current_price": 95.0,
            "ema_20_15m": 99.0,
            "ema_20_1h": 98.0,
            "vwap": 97.5,
            "swing_low_15m": 96.0, # Price 95.0 broke below swing low 96.0 (CHoCH breakdown)
            "macro_box_pos_pct": 75.0,
            "rsi_15m": 42.0,
            "plus_di_14": 15.0,
            "minus_di_14": 29.0
        },
        "multi_timeframe": {
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH",
            "mtf_alignment": "FULL_ALIGNMENT"
        }
    }

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH", "confidence": 68.0},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH", "confidence": 64.0},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH", "confidence": 60.0},
        {"agent_name": "OI_Funding_Agent", "signal": "BULLISH", "confidence": 51.0}
    ]

    rev_state = evaluate_structural_reversal_state(
        symbol_data=symbol_data,
        analyst_reports=analyst_reports,
        macro_trend="BULLISH",
        macro_regime="TRENDING"
    )

    assert rev_state["is_bearish_reversal"] is True
    assert rev_state["is_bullish_reversal"] is False
    assert rev_state["reversal_direction"] == "SHORT"
    assert rev_state["conditions_met"]["bearish"]["count"] >= 2


# ═════════════════════════════════════════════════════════════════════════════
# MODULE B: StrategyRouter & DeterministicGuard Reversal Flip & Veto Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_late_cycle_short_vetoed_by_structural_reversal_veto():
    """
    Module B: When is_bullish_reversal == True, late-cycle continuation SHORT setups
    must be strictly VETOED by STRUCTURAL_REVERSAL_VETO.
    """
    market_data = {
        "price_data": {"current_price": 105.0},
        "indicators": {
            "current_price": 105.0,
            "ema_20_15m": 101.0,
            "ema_20_1h": 103.0,
            "vwap": 102.0,
            "swing_high_15m": 104.0,
            "macro_box_pos_pct": 35.0,
            "rsi_15m": 56.0,
            "higher_low": True
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT"
        }
    }

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 65.0},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 60.0},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL", "confidence": 50.0},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH", "confidence": 55.0}
    ]

    macro_cache = {
        "BTC-USD": {"multi_timeframe": {"mtf_alignment": "COUNTER_TREND_WARNING", "trend_1h": "BEARISH"}},
        "ETH-USD": {"multi_timeframe": {"mtf_alignment": "COUNTER_TREND_WARNING", "trend_1h": "BEARISH"}},
        "regime_verdict": {"range_score": 45.0, "vol_score": 15.0}
    }

    # 1. Test in StrategyRouter._evaluate_raw directly
    profile = StrategyRouter.evaluate(
        symbol="ZEC-USD",
        market_data=market_data,
        valid_reports=analyst_reports,
        macro_cache=macro_cache,
        detected_regime="TRANSITION",
        profile="BALANCED"
    )

    # Because bullish flip is eligible, it flips to LONG with REVERSAL_TREND_SHIFT rather than a SHORT!
    assert profile.has_directional_signal is True
    assert profile.direction_bias == "LONG"
    assert profile.strategy_mode == "REVERSAL_TREND_SHIFT"

    # 2. Test PullbackWatchlist retest into an active reversal:
    market_data_retest = dict(market_data)
    market_data_retest["is_pullback_retest"] = True
    market_data_retest["pullback_retest_direction"] = "SHORT"

    retest_profile = StrategyRouter.evaluate(
        symbol="ZEC-USD",
        market_data=market_data_retest,
        valid_reports=analyst_reports,
        macro_cache=macro_cache,
        detected_regime="RANGE_CHOPPY",
        profile="BALANCED"
    )

    assert retest_profile.has_directional_signal is False
    assert retest_profile.strategy_mode == "STRUCTURAL_REVERSAL_VETO"
    assert "STRUCTURAL_REVERSAL_VETO" in retest_profile.reasoning

    # 3. Test DeterministicGuard vetoing a rogue CEO SHORT proposal during bullish reversal
    rm_mock = MagicMock()
    rm_mock._get_profile_rules.return_value = {"min_conviction": 70, "min_rr": 1.25}

    ceo_proposal = {
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 80,
        "reasoning": "Rogue proposal against reversal"
    }

    decision = DeterministicGuard.evaluate(
        strategy_profile=StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Test"),
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=rm_mock,
        market_data=market_data,
        symbol="ZEC-USD",
        analyst_reports=analyst_reports
    )

    assert decision.decision == "HOLD"
    assert decision.guard_status == "BLOCKED"
    assert decision.rejection_tag == "STRUCTURAL_REVERSAL_VETO"


@pytest.mark.asyncio
async def test_reversal_flip_unlocks_long_candidate_with_reduced_size():
    """
    Module B: When is_bullish_reversal == True and at least 2 technical analysts are BULLISH >= 60%:
    Bypasses 'Попытка торговли против глобального макро-тренда', unlocks LONG setup with
    REVERSAL_TREND_SHIFT, and RiskManager caps dollar risk at hard_max_risk_usd <= $1.35.
    """
    market_data = {
        "price_data": {"current_price": 3.10},
        "indicators": {
            "current_price": 3.10,
            "ema_20_15m": 3.02,
            "ema_20_1h": 3.05,
            "vwap": 3.04,
            "swing_high_15m": 3.08,
            "macro_box_pos_pct": 28.0,
            "rsi_15m": 57.0,
            "higher_low": True,
            "atr_14": 0.08
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT"
        },
        "derivatives_data": {
            "min_notional": 100.0,
            "taker_fee_pct": 0.0005,
            "step_size": 0.1
        },
        "order_book_data": {
            "spread_pct": 0.0002
        }
    }

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 64.0},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 61.0},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH", "confidence": 59.0},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH", "confidence": 52.0}
    ]

    macro_cache = {
        "regime_verdict": {"range_score": 40.0, "vol_score": 15.0}
    }

    # 1. StrategyRouter evaluates candidate
    profile = StrategyRouter.evaluate(
        symbol="LIT-USD",
        market_data=market_data,
        valid_reports=analyst_reports,
        macro_cache=macro_cache,
        detected_regime="TRANSITION",
        profile="BALANCED"
    )

    assert profile.has_directional_signal is True
    assert profile.direction_bias == "LONG"
    assert profile.strategy_mode == "REVERSAL_TREND_SHIFT"
    assert profile.action_size_mult == 0.70
    assert "REVERSAL_FLIP" in profile.reasoning

    # 2. RiskManager evaluates sizing: must enforce REDUCE_SIZE ($1.35 hard ceiling)
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    total_balance = 88.0

    ceo_decision = {
        "action": "ENTER",
        "trade_action": "REDUCE_SIZE",
        "decision": "LONG",
        "conviction": 75,
        "entry_price": 3.10,
        "sl_price": 3.03, # 2.25% stop
        "tp_price": 3.25,
        "strategy_mode": "REVERSAL_TREND_SHIFT",
        "symbol": "LIT-USD"
    }

    portfolio_data = {
        "total_balance": total_balance,
        "available_margin": 70.0,
        "active_positions": {},
        "recent_streak": ["WIN"],
        "daily_drawdown_pct": 0.0,
        "win_rate": 60.0,
        "payoff_ratio": 1.5,
        "total_trades": 5
    }

    risk_res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRANSITION"
    )
    if risk_res["approved"]:
        assert risk_res["risk_usd"] <= 1.35


@pytest.mark.asyncio
async def test_reversal_trend_shift_risk_cap_enforced_by_risk_manager():
    """
    Ensure RiskManager enforces hard_max_risk_usd <= 1.35 for REVERSAL_TREND_SHIFT setups.
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    total_balance = 90.0

    # At $90 total balance, normal ENTER cap is $1.935 ($2.00 max).
    # For REVERSAL_TREND_SHIFT, the cap MUST be min(0.0150 * 90, 1.35) = $1.35!
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {"atr_14": 1.2},
        "derivatives_data": {
            "min_notional": 100.0,
            "taker_fee_pct": 0.0005,
            "step_size": 0.01
        },
        "order_book_data": {"spread_pct": 0.0002}
    }

    ceo_decision = {
        "action": "ENTER",
        "decision": "LONG",
        "trade_action": "ENTER", # Even if CEO proposed ENTER, mode REVERSAL_TREND_SHIFT enforces $1.35 cap
        "conviction": 75,
        "entry_price": 100.0,
        "sl_price": 99.0, # 1.0% tight stop
        "tp_price": 102.5,
        "strategy_mode": "REVERSAL_TREND_SHIFT",
        "symbol": "SOL-USD"
    }

    portfolio_data = {
        "total_balance": total_balance,
        "available_margin": 80.0,
        "active_positions": {},
        "recent_streak": ["WIN"],
        "daily_drawdown_pct": 0.0,
        "win_rate": 65.0,
        "payoff_ratio": 1.5,
        "total_trades": 8
    }

    res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRANSITION"
    )

    if res["approved"]:
        # Sized risk must strictly adhere to <= $1.35
        assert res["risk_usd"] <= 1.35
    else:
        # If routed to pullback due to wide stop, verify maximum risk budget passed to P* is <= 1.35
        assert res["trade_action"] == "WAIT_FOR_PULLBACK"


# ═════════════════════════════════════════════════════════════════════════════
# MODULE C: Cross-Asset Breadth Detector & Watchlist Purge Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_cross_asset_breadth_recovery_and_flush_detection():
    """
    Module C: Cross-asset breadth detector check across 7 monitored pairs.
    - BULLISH_RECOVERY when BTC + ETH reverse or >= 4/7 pairs reverse bullish.
    - BEARISH_FLUSH when BTC + ETH reverse or >= 4/7 pairs reverse bearish.
    """
    def make_mock_sym_data(cur_px, ema_15m, ema_1h, vwap, rsi, is_bull_candle=True):
        return {
            "price_data": {"current_price": cur_px},
            "indicators": {
                "current_price": cur_px,
                "ema_20_15m": ema_15m,
                "ema_20_1h": ema_1h,
                "vwap": vwap,
                "rsi_15m": rsi,
                "last_closed_candle_close": cur_px + (0.5 if is_bull_candle else -0.5),
                "candle_open": cur_px
            },
            "analyst_reports": [
                {"agent_name": "Candle_Agent", "signal": "BULLISH" if is_bull_candle else "BEARISH", "confidence": 65.0},
                {"agent_name": "Indicator_Agent", "signal": "BULLISH" if is_bull_candle else "BEARISH", "confidence": 62.0}
            ]
        }

    # Case 1: BTC and ETH both reclaim mean with bullish momentum -> BULLISH_RECOVERY
    universe_data_bullish_leaders = {
        "BTC-USD": make_mock_sym_data(68000.0, 67200.0, 67500.0, 67300.0, 56.0, is_bull_candle=True),
        "ETH-USD": make_mock_sym_data(2550.0, 2500.0, 2520.0, 2510.0, 55.0, is_bull_candle=True),
        "SOL-USD": make_mock_sym_data(180.0, 185.0, 186.0, 184.0, 45.0, is_bull_candle=False),
        "BNB-USD": make_mock_sym_data(620.0, 625.0, 626.0, 624.0, 44.0, is_bull_candle=False),
        "ZEC-USD": make_mock_sym_data(840.0, 850.0, 855.0, 852.0, 42.0, is_bull_candle=False),
        "HYPE-USD": make_mock_sym_data(28.0, 29.0, 29.5, 29.2, 40.0, is_bull_candle=False),
        "LIT-USD": make_mock_sym_data(3.05, 3.12, 3.15, 3.14, 43.0, is_bull_candle=False),
    }

    shift_leaders = Pipeline.check_cross_asset_reversal_breadth(universe_data_bullish_leaders, macro_regime="TRANSITION")
    assert shift_leaders == "BULLISH_RECOVERY"

    # Case 2: >= 4/7 pairs reverse bullish even if one leader is delayed
    universe_data_breadth_4_of_7 = {
        "BTC-USD": make_mock_sym_data(67000.0, 67200.0, 67500.0, 67300.0, 48.0, is_bull_candle=False),
        "ETH-USD": make_mock_sym_data(2550.0, 2500.0, 2520.0, 2510.0, 56.0, is_bull_candle=True),
        "SOL-USD": make_mock_sym_data(190.0, 185.0, 186.0, 184.0, 58.0, is_bull_candle=True),
        "BNB-USD": make_mock_sym_data(630.0, 625.0, 626.0, 624.0, 57.0, is_bull_candle=True),
        "ZEC-USD": make_mock_sym_data(860.0, 850.0, 852.0, 851.0, 59.0, is_bull_candle=True),
        "HYPE-USD": make_mock_sym_data(28.0, 29.0, 29.5, 29.2, 40.0, is_bull_candle=False),
        "LIT-USD": make_mock_sym_data(3.05, 3.12, 3.15, 3.14, 43.0, is_bull_candle=False),
    }

    shift_breadth = Pipeline.check_cross_asset_reversal_breadth(universe_data_breadth_4_of_7, macro_regime="TRANSITION")
    assert shift_breadth == "BULLISH_RECOVERY"

    # Case 3: Symmetric BEARISH_FLUSH when BTC + ETH break down
    universe_data_bearish_leaders = {
        "BTC-USD": make_mock_sym_data(66000.0, 67200.0, 67500.0, 67300.0, 42.0, is_bull_candle=False),
        "ETH-USD": make_mock_sym_data(2450.0, 2500.0, 2520.0, 2510.0, 43.0, is_bull_candle=False),
        "SOL-USD": make_mock_sym_data(190.0, 185.0, 186.0, 184.0, 58.0, is_bull_candle=True),
        "BNB-USD": make_mock_sym_data(630.0, 625.0, 626.0, 624.0, 57.0, is_bull_candle=True),
        "ZEC-USD": make_mock_sym_data(860.0, 850.0, 852.0, 851.0, 59.0, is_bull_candle=True),
        "HYPE-USD": make_mock_sym_data(30.0, 29.0, 29.5, 29.2, 55.0, is_bull_candle=True),
        "LIT-USD": make_mock_sym_data(3.20, 3.12, 3.15, 3.14, 56.0, is_bull_candle=True),
    }

    shift_flush = Pipeline.check_cross_asset_reversal_breadth(universe_data_bearish_leaders, macro_regime="TRANSITION")
    assert shift_flush == "BEARISH_FLUSH"


def test_watchlist_automatic_directional_purge_on_breadth_shift():
    """
    Module C: Automatic purge of pending PullbackWatchlist setups on breadth shift:
    - BULLISH_RECOVERY purges all pending SHORT setups (preserving LONGs).
    - BEARISH_FLUSH purges all pending LONG setups (preserving SHORTs).
    """
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.services = MagicMock()
    pipeline.services.logger = MagicMock()
    pipeline._save_pullback_watchlist = MagicMock()

    # Pre-populate watchlist with 2 SHORTs and 1 LONG
    pipeline._pullback_watchlist = {
        "ZEC-USD": {"direction": "SHORT", "target_pullback_price": 860.0},
        "LIT-USD": {"direction": "SHORT", "target_pullback_price": 3.15},
        "SOL-USD": {"direction": "LONG", "target_pullback_price": 182.0}
    }

    # Breadth shifts to BULLISH_RECOVERY -> All SHORTs must be purged
    purged = pipeline.purge_watchlist_on_breadth_shift("BULLISH_RECOVERY")

    assert set(purged) == {"ZEC-USD", "LIT-USD"}
    assert "ZEC-USD" not in pipeline._pullback_watchlist
    assert "LIT-USD" not in pipeline._pullback_watchlist
    assert "SOL-USD" in pipeline._pullback_watchlist
    pipeline._save_pullback_watchlist.assert_called()

    # Breadth shifts to BEARISH_FLUSH -> All LONGs must be purged
    purged_flush = pipeline.purge_watchlist_on_breadth_shift("BEARISH_FLUSH")

    assert purged_flush == ["SOL-USD"]
    assert "SOL-USD" not in pipeline._pullback_watchlist
    assert len(pipeline._pullback_watchlist) == 0


@pytest.mark.asyncio
async def test_watchlist_individual_symbol_choch_invalidation():
    """
    Module C: Invalidate any individual PullbackWatchlist entry if that specific
    symbol triggers is_bullish_reversal (for a watched SHORT) or is_bearish_reversal
    (for a watched LONG) at the moment the watchlist radar evaluates it.
    """
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.services = MagicMock()
    pipeline.services.logger = MagicMock()
    pipeline.services.trading_service.active_positions = {}
    pipeline._save_pullback_watchlist = MagicMock()
    pipeline._cycle_running = False

    # Symbol with watched SHORT setup
    pipeline._pullback_watchlist = {
        "ZEC-USD": {
            "symbol": "ZEC-USD",
            "direction": "SHORT",
            "target_pullback_price": 860.0,
            "invalidation_price": 890.0,
            "signal_price": 840.0,
            "created_at": time.time(),
            "expires_at": time.time() + 2700,
            "ttl_seconds": 2700
        }
    }

    # Mock fetcher returns price that broke out with Higher Lows and reclaimed 15m EMA-20
    ohlcv_mock = {
        "current_price": 855.0,
        "candles_20": [
            {"open": 830.0, "high": 845.0, "low": 828.0, "close": 840.0},
            {"open": 840.0, "high": 852.0, "low": 838.0, "close": 850.0}, # Higher Low (838 > 828)
            {"open": 850.0, "high": 860.0, "low": 848.0, "close": 855.0}  # Higher Low (848 > 838)
        ]
    }
    pipeline.services.fetcher.fetch_ohlcv = AsyncMock(return_value=ohlcv_mock)

    # Patch evaluate_structural_reversal_state to simulate individual Bullish CHoCH reversal
    with patch.object(
        DeterministicGuard,
        "evaluate_structural_reversal_state",
        return_value={
            "is_bullish_reversal": True,
            "is_bearish_reversal": False,
            "reversal_direction": "LONG",
            "reversal_reason": "Individual Bullish CHoCH confirmed"
        }
    ):
        triggered, sym, reas = await pipeline.check_pullback_watchlist()

        # Must NOT trigger entry retest; must be deleted from watchlist
        assert triggered is False
        assert "ZEC-USD" not in pipeline._pullback_watchlist
        pipeline._save_pullback_watchlist.assert_called()


# ═════════════════════════════════════════════════════════════════════════════
# CONTRACTS 1 & 2: Timeframe Phase-Lag Conditioning & SSOT Risk Cap Tests
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ceo_agent_phase_lag_conditioning_for_reversal_trend_shift():
    """
    Contract 1: CEO_Agent evaluates REVERSAL_TREND_SHIFT via 15m/1H structure
    and analyst consensus without applying lagging 4H counter-trend penalty.
    Conviction / Directional Confidence must NOT collapse to 7-25%.
    """
    from agents.ceo_agent import CEO_Agent

    logger_mock = MagicMock()
    llm_mock = MagicMock()
    llm_mock.model_name = "MockGemini"

    # LLM simulates outputting a breakdown with strong bull argument and mtf trend
    llm_response = {
        "decision": "LONG",
        "score_breakdown": {
            "bull_argument": 40.0,
            "bear_argument": -15.0,
            "mtf_trend": 25.0,
            "risk_penalties": {
                "market_structure": -5.0
            }
        },
        "reasoning_en": "Bullish CHoCH reversal with 15m/1H reclaim confirmed."
    }

    ceo = CEO_Agent(logger=logger_mock, primary_llm=llm_mock, escalation_llm=None)
    ceo.generate_json = AsyncMock(return_value=llm_response)

    # Market context with lagging BEARISH 4H trend, but BULLISH 15m and 1H structure
    data = {
        "symbol": "BTC-USD",
        "strategy_mode": "REVERSAL_TREND_SHIFT",
        "direction_bias": "LONG",
        "router_reasoning": "REVERSAL_FLIP: Bullish CHoCH",
        "macro_regime": "TRANSITION",
        "multi_timeframe_context": {
            "tf_15m": {"trend": "BULLISH", "ema_20": 67000.0},
            "tf_1h": {"trend": "BULLISH", "ema_20": 66800.0},
            "tf_4h": {"trend": "BEARISH", "ema_20": 68000.0}  # Lagging 4H trend
        },
        "subordinate_analyst_reports": [
            {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 70.0},
            {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 68.0},
            {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL", "confidence": 50.0}
        ],
        "market_data": {
            "structural_reversal": {
                "is_reversal": True,
                "is_bullish_reversal": True,
                "reversal_direction": "LONG",
                "reversal_reason": "Bullish CHoCH confirmed"
            }
        }
    }

    res = await ceo.analyze(data)

    assert res["decision"] == "LONG"
    # Directional confidence must be healthy (>= 60%), NOT crushed to 7%-25%!
    assert res["directional_confidence"] >= 65
    assert res["conviction"] >= 55
    assert res["trade_action"] in ("ENTER", "REDUCE_SIZE", "WAIT_FOR_PULLBACK")


def test_ssot_effective_max_risk_usd_contract():
    """
    Contract 2: RiskManager.get_effective_max_risk_usd is the SSOT for 1R cap.
    - ENTER: min(0.0215 * equity, 2.00)
    - REDUCE_SIZE / REVERSAL_TREND_SHIFT: min(0.0150 * equity, 1.35)
    """
    # At $88.00 equity:
    # ENTER cap: min(0.0215 * 88, 2.00) = 1.892
    enter_cap = RiskManager.get_effective_max_risk_usd(88.0, trade_action="ENTER", strategy_mode="TREND_FOLLOWING")
    assert round(enter_cap, 2) == 1.89

    # REDUCE_SIZE cap: min(0.0150 * 88, 1.35) = 1.32
    red_cap = RiskManager.get_effective_max_risk_usd(88.0, trade_action="REDUCE_SIZE", strategy_mode="TREND_FOLLOWING")
    assert round(red_cap, 2) == 1.32

    # REVERSAL_TREND_SHIFT cap: min(0.0150 * 88, 1.35) = 1.32 even if trade_action is ENTER
    rev_cap = RiskManager.get_effective_max_risk_usd(88.0, trade_action="ENTER", strategy_mode="REVERSAL_TREND_SHIFT")
    assert round(rev_cap, 2) == 1.32

    # Large equity ceiling:
    enter_large = RiskManager.get_effective_max_risk_usd(500.0, trade_action="ENTER", strategy_mode="TREND_FOLLOWING")
    assert enter_large == 2.00
    rev_large = RiskManager.get_effective_max_risk_usd(500.0, trade_action="ENTER", strategy_mode="REVERSAL_TREND_SHIFT")
    assert rev_large == 1.35


@pytest.mark.asyncio
async def test_reversal_pullback_candidate_p_star_passes_risk_manager_approval():
    """
    Contract 2: A REVERSAL_TREND_SHIFT candidate registered in PullbackWatchlist
    solves P* using get_effective_max_risk_usd. When price reaches P*,
    RiskManager.analyze() approves the trade within the $1.30 risk cap on $105.50 notional.
    """
    equity = 86.67
    effective_max_risk = RiskManager.get_effective_max_risk_usd(
        equity=equity,
        trade_action="REDUCE_SIZE",
        strategy_mode="REVERSAL_TREND_SHIFT"
    )
    assert effective_max_risk <= 1.3001

    # Simulate ZEC-USD setup
    current_price = 902.45
    atr_14 = 7.50
    # True structural stop behind 15m CHoCH swing low (894.0 - 0.3*7.5 = 891.75)
    swing_low_15m = 894.0
    struct_sl = swing_low_15m - (0.30 * atr_14)  # 891.75
    target_tp = 930.0
    target_bump_notional = 105.50

    from agents.risk_manager import solve_optimal_pullback_entry
    p_star = solve_optimal_pullback_entry(
        direction="LONG",
        current_price=current_price,
        structural_sl=struct_sl,
        target_tp=target_tp,
        atr_15m=atr_14,
        max_sl_dist_pct=2.50,
        max_risk_usd=effective_max_risk,
        min_notional_usd=target_bump_notional,
        min_net_rr=1.40,
        taker_fee_pct=0.0005
    )

    assert p_star is not None
    # Verify that at P*, dollar risk on min notional is strictly <= effective_max_risk
    sl_dist = (p_star - struct_sl) / p_star
    dollar_risk = target_bump_notional * sl_dist + (target_bump_notional * 0.0010)
    assert dollar_risk <= effective_max_risk + 0.01

    # Now verify RiskManager.analyze() approves when triggered at p_star
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    ceo_decision = {
        "decision": "LONG",
        "trade_action": "REDUCE_SIZE",
        "conviction": 70,
        "symbol": "ZEC-USD",
        "structural_sl": struct_sl,
        "sl_price": struct_sl,
        "tp_price": target_tp,
        "is_pullback_retest": True
    }
    portfolio_data = {
        "total_balance": equity,
        "current_balance": equity,
        "available_margin": equity,
        "free_margin": equity,
        "active_positions": {}
    }
    market_data = {
        "symbol": "ZEC-USD",
        "price_data": {
            "current_price": p_star,
            "candles_20": [
                {"open": 892.0, "high": 895.0, "low": 891.75, "close": p_star}
            ]
        },
        "indicators": {
            "current_price": p_star,
            "atr_14": atr_14,
            "swing_low_15m": swing_low_15m
        },
        "derivatives_data": {
            "min_notional": 100.0,
            "funding_rate": 0.00001
        }
    }

    res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        macro_regime="TRANSITION",
        strategy_mode="REVERSAL_TREND_SHIFT"
    )

    assert res["approved"] is True
    assert res["notional_size_usd"] >= 105.0
    assert res["risk_usd"] <= 1.35


@pytest.mark.asyncio
async def test_dynamic_watchlist_revalidation_heals_legacy_risk_mismatches():
    """
    Contract 2, Requirement 3: Self-healing watchlist validation on check/load.
    Dynamically recomputes P* or invalidates legacy entries whose implied risk
    exceeds get_effective_max_risk_usd without requiring manual JSON edits.
    """
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.services = MagicMock()
    pipeline.services.logger = MagicMock()
    pipeline.services.trading_service.active_positions = {}
    pipeline.services.trading_service._last_equity = 86.67
    pipeline._save_pullback_watchlist = MagicMock()
    pipeline._cycle_running = False
    pipeline._pullback_context = {}

    # Pre-populate watchlist with legacy wide-stop entry (e.g. ZEC with 48h extreme 882.33)
    # Stop distance was 1.68% -> implied risk ~$1.88 > $1.30!
    pipeline._pullback_watchlist = {
        "ZEC-USD": {
            "symbol": "ZEC-USD",
            "direction": "LONG",
            "signal_price": 902.45,
            "target_pullback_price": 897.420774,
            "target_entry": 897.420774,
            "invalidation_price": 882.329298,
            "directional_conf": 73,
            "conviction": 58,
            "strategy_mode": "REVERSAL_TREND_SHIFT",
            "trade_action": "REDUCE_SIZE",
            "created_at": time.time(),
            "expires_at": time.time() + 2700,
            "ttl_seconds": 2700
        }
    }

    # Fetcher returns current price 900.0 with 15m candles showing local swing low at 894.0
    ohlcv_mock = {
        "current_price": 900.0,
        "atr_14": 7.50,
        "candles_20": [
            {"open": 895.0, "high": 898.0, "low": 894.0, "close": 896.0},
            {"open": 896.0, "high": 901.0, "low": 895.0, "close": 900.0}
        ]
    }
    pipeline.services.fetcher.fetch_ohlcv = AsyncMock(return_value=ohlcv_mock)

    with patch.object(
        DeterministicGuard,
        "evaluate_structural_reversal_state",
        return_value={"is_bullish_reversal": False, "is_bearish_reversal": False}
    ):
        triggered, sym, reas = await pipeline.check_pullback_watchlist()

        # Watchlist entry should have self-healed:
        # invalidation_price updated from 48h extreme (882.33) to 15m swing low - buffer (894.0 - 2.25 = 891.75)
        # target_pullback_price re-solved with max_risk <= $1.30!
        item = pipeline._pullback_watchlist.get("ZEC-USD")
        if item is not None:
            new_target = item["target_pullback_price"]
            new_inv = item["invalidation_price"]
            sl_dist = (new_target - new_inv) / new_target
            implied_risk = 105.50 * sl_dist + (105.50 * 0.0010)
            assert implied_risk <= 1.31  # Self-healed to conform to $1.30 cap!

