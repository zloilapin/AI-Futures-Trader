import pytest
import time
from unittest.mock import MagicMock
from core.strategy_router import StrategyProfile
from core.deterministic_guard import DeterministicGuard
from agents.sentinel_agent import SentinelAgent
from agents.risk_manager import RiskManager

class TestBreakoutProtections:
    
    def test_breakout_override_requires_high_entry_qual_or_volume_explosion(self):
        """
        Verify that modest volume breakouts (165%) with low entry quality (66%)
        do NOT override WAIT_FOR_PULLBACK to ENTER, protecting from bull traps.
        """
        strat = StrategyProfile(
            has_directional_signal=True,
            strategy_mode="BREAKOUT",
            direction_bias="LONG",
            reasoning="Test breakout"
        )
        
        # 1. Modest breakout with EntryQuality = 66% -> must stay WAIT_FOR_PULLBACK
        ceo_proposal = {
            "decision": "LONG",
            "conviction": 66,
            "trade_action": "WAIT_FOR_PULLBACK",
            "directional_confidence": 81,
            "entry_quality": 66,
            "sl_price": 99.0
        }
        market_data = {
            "price_data": {"current_price": 102.5, "candles_20": [{"high": 102.6, "low": 101.5, "close": 102.4}]},
            "indicators": {
                "current_price": 102.5,
                "donchian_high": 102.0,
                "volume_spike_pct": 165.7,
                "rsi_14": 65.0
            }
        }
        rm = MagicMock()
        rm._get_profile_rules.return_value = {"min_conviction": 65}
        
        decision = DeterministicGuard.evaluate(strat, ceo_proposal, "AGGRESSIVE", rm, market_data, symbol="SOL-USD")
        assert decision.trade_action == "WAIT_FOR_PULLBACK"
        assert decision.is_actionable is False
        
        # 2. Solid EntryQuality = 75% -> allowed to ENTER
        ceo_proposal_high_eq = {
            "decision": "LONG",
            "conviction": 75,
            "trade_action": "WAIT_FOR_PULLBACK",
            "directional_confidence": 81,
            "entry_quality": 75,
            "sl_price": 99.0
        }
        decision_high_eq = DeterministicGuard.evaluate(strat, ceo_proposal_high_eq, "AGGRESSIVE", rm, market_data, symbol="SOL-USD")
        assert decision_high_eq.trade_action == "ENTER"
        assert decision_high_eq.is_actionable is True

        # 3. Low EntryQuality = 60%, but true VOLUME_EXPLOSION (260% spike, safe RSI=65) -> REDUCE_SIZE
        market_data_vol_exp = {
            "price_data": {"current_price": 102.5, "candles_20": [{"high": 102.6, "low": 101.5, "close": 102.4}]},
            "indicators": {
                "current_price": 102.5,
                "donchian_high": 102.0,
                "volume_spike_pct": 260.0,
                "rsi_14": 65.0
            }
        }
        ceo_proposal_low_eq = {
            "decision": "LONG",
            "conviction": 60,
            "trade_action": "WAIT_FOR_PULLBACK",
            "directional_confidence": 80,
            "entry_quality": 60,
            "sl_price": 100.0 # tight SL
        }
        decision_vol_exp = DeterministicGuard.evaluate(strat, ceo_proposal_low_eq, "AGGRESSIVE", rm, market_data_vol_exp, symbol="SOL-USD")
        assert decision_vol_exp.trade_action == "REDUCE_SIZE"
        assert decision_vol_exp.is_actionable is True
        assert decision_vol_exp.volume_explosion_override is True

    @pytest.mark.asyncio
    async def test_sentinel_breakout_stagnation_guard(self):
        """
        Verify that Sentinel moves SL to Break-Even at 75 min if initial impulse reached >=0.45R
        but faded, and tightens SL to -0.50R if thesis never showed any impulse.
        """
        sentinel = SentinelAgent(logger=MagicMock())
        now_ts = time.time()
        
        # Scenario A: LONG BREAKOUT, 80 min old, MFE was +0.50R, currently at +0.05R (decayed)
        pos_decayed = {
            "symbol": "SOL-USD",
            "direction": "LONG",
            "entry_price": 100.0,
            "sl_price": 98.0,
            "initial_sl": 98.0,
            "strategy_mode": "BREAKOUT",
            "protection_state": "PROTECTED",
            "open_time": now_ts - (80 * 60), # 80 minutes ago
            "max_favorable_excursion_r": 0.50
        }
        market_data_a = {"price_data": {"current_price": 100.10}}
        profile_rules = {"sentinel_fee_pct": 0.0010, "sentinel_slippage_pct": 0.0010} # cost buffer = 0.20
        verdict_a = await sentinel.analyze(pos_decayed, market_data_a, profile_rules=profile_rules, atr_value=2.0)
        assert verdict_a["new_sl"] is not None
        assert verdict_a["state"] == "BREAKOUT_STAGNATION_BE"
        assert verdict_a["new_sl"] == pytest.approx(100.20, abs=0.01) # entry + cost_buffer

        # Scenario B: LONG BREAKOUT, 80 min old, never had impulse (MFE = 0.15R), currently at 1975.0
        pos_no_impulse = {
            "symbol": "ETH-USD",
            "direction": "LONG",
            "entry_price": 2000.0,
            "sl_price": 1960.0, # initial SL was -1.0R (dist = 40.0)
            "initial_sl": 1960.0,
            "strategy_mode": "BREAKOUT",
            "protection_state": "PROTECTED",
            "open_time": now_ts - (80 * 60),
            "max_favorable_excursion_r": 0.15
        }
        market_data_b = {"price_data": {"current_price": 1975.0}}
        verdict_b = await sentinel.analyze(pos_no_impulse, market_data_b, profile_rules=profile_rules, atr_value=40.0)
        assert verdict_b["new_sl"] is not None
        assert verdict_b["state"] == "BREAKOUT_STAGNATION_TIGHTEN"
        assert verdict_b["new_sl"] == pytest.approx(1980.0, abs=0.5) # entry - 0.50 * 40.0 = 1980.0

        # Scenario C: TREND_FOLLOWING does NOT trigger breakout stagnation at 80 min (uses 180 min)
        pos_trend = dict(pos_decayed)
        pos_trend["strategy_mode"] = "TREND_FOLLOWING"
        verdict_c = await sentinel.analyze(pos_trend, market_data_a, profile_rules=profile_rules, atr_value=2.0)
        assert verdict_c["new_sl"] is None # No update at 80 min for trend following

    @pytest.mark.asyncio
    async def test_1_per_candle_breakout_cluster_cap_in_risk_manager(self):
        """
        Verify that RiskManager vetos second BREAKOUT order in the same direction
        within the same 15m candle bucket.
        """
        rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
        now_ts = time.time()
        
        # Existing active position opened 30 seconds ago (same 15m candle)
        active_positions = {
            "SOL-USD": {
                "symbol": "SOL-USD",
                "direction": "LONG",
                "entry_price": 100.0,
                "sl_price": 98.0,
                "strategy_mode": "BREAKOUT",
                "open_time": now_ts - 30, # 30s ago in this same candle bucket
                "contracts": 1.0,
                "planned_risk_usd": 2.0
            }
        }
        portfolio_data = {
            "balance": 100.0,
            "equity": 100.0,
            "total_usd": 100.0,
            "current_balance": 100.0,
            "available_margin": 100.0,
            "peak_balance": 100.0,
            "daily_drawdown_pct": 0.0,
            "recent_streak": ["WIN"],
            "active_positions": active_positions
        }
        market_data = {
            "price_data": {"current_price": 2000.0},
            "indicators": {"atr_14": 20.0, "current_price": 2000.0}
        }
        
        # 1. Second LONG BREAKOUT in same candle -> MUST be vetoed
        decision_long = {
            "symbol": "ETH-USD",
            "decision": "LONG",
            "trade_action": "ENTER",
            "conviction": 80,
            "directional_confidence": 80,
            "entry_quality": 80,
            "sl_price": 1960.0
        }
        res_same = await rm.analyze(
            decision_long, portfolio_data, market_data,
            effective_profile="AGGRESSIVE", macro_regime="TRENDING", strategy_mode="BREAKOUT"
        )
        assert res_same["approved"] is False
        assert res_same.get("veto_category") == "BREAKOUT_CANDLE_CLUSTER_CAP"

        # 2. SHORT BREAKOUT in same candle -> NOT vetoed by candle cluster cap
        decision_short = {
            "symbol": "ETH-USD",
            "decision": "SHORT",
            "trade_action": "ENTER",
            "conviction": 80,
            "directional_confidence": 80,
            "entry_quality": 80,
            "sl_price": 2040.0
        }
        res_diff_dir = await rm.analyze(
            decision_short, portfolio_data, market_data,
            effective_profile="AGGRESSIVE", macro_regime="TRENDING", strategy_mode="BREAKOUT"
        )
        assert res_diff_dir.get("veto_category") != "BREAKOUT_CANDLE_CLUSTER_CAP"

        # 3. LONG BREAKOUT from a PREVIOUS candle bucket (e.g. 20 min ago) -> NOT vetoed by candle cap
        active_positions_old = {
            "SOL-USD": {
                "symbol": "SOL-USD",
                "direction": "LONG",
                "entry_price": 100.0,
                "sl_price": 98.0,
                "strategy_mode": "BREAKOUT",
                "open_time": now_ts - (20 * 60), # 20m ago (different 15m candle)
                "contracts": 1.0,
                "planned_risk_usd": 2.0
            }
        }
        portfolio_data_old = dict(portfolio_data)
        portfolio_data_old["active_positions"] = active_positions_old
        res_old_bucket = await rm.analyze(
            decision_long, portfolio_data_old, market_data,
            effective_profile="AGGRESSIVE", macro_regime="TRENDING", strategy_mode="BREAKOUT"
        )
        assert res_old_bucket.get("veto_category") != "BREAKOUT_CANDLE_CLUSTER_CAP"

    def test_breakout_overextension_atr_stretch_forces_wait_for_pullback(self):
        """
        Verify that when price has moved > 3.5x ATR away from EMA-20 or bb_pos > 140% with RSI > 75,
        DeterministicGuard strictly blocks immediate market entry and enforces WAIT_FOR_PULLBACK.
        """
        strat = StrategyProfile(
            has_directional_signal=True,
            strategy_mode="BREAKOUT",
            direction_bias="LONG",
            reasoning="Test breakout"
        )
        # Even with high conviction (75%) and ENTER action:
        ceo_proposal = {
            "decision": "LONG",
            "conviction": 75,
            "trade_action": "ENTER",
            "directional_confidence": 85,
            "entry_quality": 80,
            "sl_price": 700.0
        }
        # Price 714.24, EMA-20 706.84, ATR 1.29 -> stretch = (714.24 - 706.84) / 1.29 = 5.73 ATR (> 3.5x)
        market_data_stretched = {
            "price_data": {"current_price": 714.24},
            "indicators": {
                "current_price": 714.24,
                "ema_20": 706.84,
                "atr_14": 1.29,
                "donchian_high": 712.91,
                "volume_spike_pct": 190.0,
                "rsi_14": 78.3,
                "bb_pos": 162.6
            }
        }
        rm = MagicMock()
        rm._get_profile_rules.return_value = {"min_conviction": 65}
        
        decision = DeterministicGuard.evaluate(strat, ceo_proposal, "AGGRESSIVE", rm, market_data_stretched, symbol="BNB-USD")
        assert decision.trade_action == "WAIT_FOR_PULLBACK"
        assert decision.is_actionable is False
        assert decision.rejection_tag == "WAIT_FOR_PULLBACK"
        assert "оторвалась" in decision.guard_reason or "Overextended Breakout" in decision.guard_reason or "Климатический пробой" in decision.guard_reason

