import pytest
import time
from agents.indicator_agent import IndicatorAgent
from core.strategy_router import StrategyRouter, StrategyProfile
from core.deterministic_guard import DeterministicGuard
from agents.risk_manager import RiskManager
from core.logger import TradeLogger

@pytest.fixture
def logger():
    return TradeLogger()

@pytest.fixture
def risk_manager(logger):
    return RiskManager(logger, None)

# ═══════════════════════════════════════════════════════════════════
# 1. Indicator Agent 48h Box and 15m Structural Extremes
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_indicator_agent_computes_48h_box_and_swing_extremes(logger):
    agent = IndicatorAgent(logger)
    
    # 48 1h candles ranging from 80.0 to 100.0
    candles_1h = []
    for i in range(48):
        candles_1h.append({
            "open": 90.0,
            "high": 95.0 if i < 40 else 100.0, # max high 100.0
            "low": 85.0 if i < 40 else 80.0,   # min low 80.0
            "close": 90.0
        })
        
    # 16 15m candles ranging from 84.0 to 88.0
    candles_15m = []
    for i in range(16):
        candles_15m.append({
            "open": 86.0,
            "high": 87.0 if i < 15 else 88.5, # max high 88.5
            "low": 85.0 if i < 15 else 84.2,  # min low 84.2
            "close": 86.0
        })

    market_data = {
        "price_data": {
            "current_price": 85.0,
            "candles_20": candles_15m
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "tf_1h": {"candles_20": candles_1h}
        },
        "indicators": {
            "rsi_14": 42.0,
            "ema_20": 87.0
        }
    }
    
    res = await agent.analyze(market_data)
    
    # high_48h = 100.0, low_48h = 80.0
    # current_price = 85.0 -> box_pos = (85 - 80) / (100 - 80) * 100 = 25.0%
    assert res["high_48h"] == 100.0
    assert res["low_48h"] == 80.0
    assert res["macro_box_pos_pct"] == 25.0
    assert res["swing_high_15m"] == 88.5
    assert res["swing_low_15m"] == 84.2
    
    # Verify injected into indicators dict
    assert market_data["indicators"]["macro_box_pos_pct"] == 25.0
    assert market_data["indicators"]["swing_high_15m"] == 88.5

# ═══════════════════════════════════════════════════════════════════
# 2. Wave Exhaustion Guard (check_anti_chase_reentry)
# ═══════════════════════════════════════════════════════════════════

def test_wave_exhaustion_guard_blocks_lower_short(tmp_path):
    # Simulate trade_journal with previous SHORT entry at 87.39 closed recently
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    
    from core.state_store import StateStore
    StateStore.save(journal_file, [
        {
            "timestamp": now_ts - 3600, # 1 hour ago
            "symbol": "SOL-USD",
            "direction": "SHORT",
            "entry_price": 87.39,
            "exit_price": 85.65,
            "net_pnl_usd": 2.01,
            "realized_r": 1.99
        }
    ])
    
    # 1. Price is lower than previous entry (86.40 < 87.39) and bb_pos is 35% (< 55%) -> VETO
    allowed, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="SHORT",
        current_price=86.40,
        eval_atr=0.65,
        bb_position_pct=35.0,
        low_48h=85.0,
        rvol=1.0,
        journal_path=journal_file
    )
    assert allowed is False
    assert "Wave Exhaustion Guard" in reason
    assert "ниже предыдущего входа" in reason

    # 2. Exception 1: Price pulled back to upper half (bb_pos >= 55%) -> ALLOWED (if exit distance satisfied)
    allowed_pb, _ = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="SHORT",
        current_price=87.00,
        eval_atr=0.65,
        bb_position_pct=58.0,
        low_48h=85.0,
        rvol=1.0,
        journal_path=journal_file
    )
    assert allowed_pb is True

    # 3. Exception 2: True breakout of 48h low with rvol >= 1.5 -> ALLOWED
    allowed_bo, _ = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="SHORT",
        current_price=85.00,
        eval_atr=0.65,
        bb_position_pct=25.0,
        low_48h=85.0,
        rvol=1.8,
        journal_path=journal_file
    )
    assert allowed_bo is True

# ═══════════════════════════════════════════════════════════════════
# 3. Multi-Day Box Floor/Ceiling Guard in StrategyRouter.evaluate
# ═══════════════════════════════════════════════════════════════════

def test_macro_box_floor_guard_vetoes_short_at_box_floor():
    market_data = {
        "price_data": {"current_price": 85.50},
        "indicators": {
            "macro_box_pos_pct": 25.0, # In lower third (< 32%)
            "bb_position_pct": 30.0,   # In lower channel (< 38%)
            "rvol": 1.1,               # Normal volume (< 1.6)
            "atr_14": 0.65,
            "rsi_14": 42.0
        },
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }
    
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH"}
    ]
    
    res = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache={},
        detected_regime="TRENDING"
    )
    
    assert res.has_directional_signal is False
    assert res.strategy_mode == "MACRO_BOX_FLOOR_VETO"
    assert "Macro Floor Guard" in res.reasoning

# ═══════════════════════════════════════════════════════════════════
# 4. Retest Priority on Pullback Rejection
# ═══════════════════════════════════════════════════════════════════

def test_retest_priority_approves_pullback_rejection_short():
    market_data = {
        "price_data": {
            "current_price": 88.0,
            "candles_20": [{
                "open": 87.5,
                "high": 88.8,
                "low": 87.2,
                "close": 87.4 # Bearish close with upper shadow
            }]
        },
        "indicators": {
            "macro_box_pos_pct": 55.0, # Mid-range
            "bb_position_pct": 65.0,   # Pulled back into retest zone 50-80%
            "atr_14": 0.65,
            "rsi_14": 52.0
        },
        "multi_timeframe": {
            "mtf_alignment": "TRANSITION", # 15m might be choppy
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }
    
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"}
    ]
    
    res = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache={},
        detected_regime="TRENDING"
    )
    
    assert res.has_directional_signal is True
    assert res.direction_bias == "SHORT"
    assert "RETEST_PRIORITY SHORT" in res.reasoning

# ═══════════════════════════════════════════════════════════════════
# 5. Ban on Market Execution for REDUCE_SIZE / Low Conviction
# ═══════════════════════════════════════════════════════════════════

def test_deterministic_guard_bans_market_execution_for_reduce_size(logger, risk_manager):
    ceo_proposal = {
        "decision": "SHORT",
        "trade_action": "REDUCE_SIZE", # Weak setup
        "conviction": 81,
        "entry_quality": 66,           # < 73%
        "directional_confidence": 81,
        "thesis": "Weak follow-through"
    }
    
    res = DeterministicGuard.evaluate(
        strategy_profile=StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Test"),
        ceo_proposal=ceo_proposal,
        profile="balanced",
        risk_manager=risk_manager,
        market_data={"indicators": {"bb_pos": 50.0, "rsi_14": 50.0}},
        symbol="SOL-USD",
        logger=logger
    )
    
    # Must NOT be actionable for immediate market entry!
    assert res.is_actionable is False
    assert res.trade_action == "WAIT_FOR_PULLBACK"
    assert res.rejection_tag == "WAIT_FOR_PULLBACK"
    assert "WAIT_FOR_PULLBACK" in res.guard_reason

# ═══════════════════════════════════════════════════════════════════
# 6. Structural Stop-Loss Geometry & Distance-to-Structure Gate
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_risk_manager_structural_sl_and_distance_gate(risk_manager):
    # Setup market data with 15m structural swing high at 88.50
    # Current entry is 86.40 (entering too late, distance is 88.50 - 86.40 = 2.10 -> 2.43% > 1.95%)
    market_data = {
        "price_data": {"current_price": 86.40},
        "indicators": {
            "atr_14": 0.65,
            "swing_high_15m": 88.50,
            "swing_low_15m": 85.00
        },
        "derivatives_data": {"taker_fee_pct": 0.00035}
    }
    
    proposal = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_quality": 80,
        "strategy_mode": "TREND_FOLLOWING",
        "approved_risk_usd": 1.5
    }
    
    portfolio_data = {
        "equity": 86.85,
        "available_margin": 86.85,
        "active_positions_count": 0,
        "streak_history_by_tag": {}
    }
    
    res = await risk_manager.analyze(proposal, portfolio_data, market_data, macro_regime="BEAR_TREND")
    
    # Distance to structural stop is (88.50 + 0.25*0.65 - 86.40) / 86.40 = 2.62% > 1.95%
    # Must veto with DISTANCE_TO_STRUCTURE_VETO and recommend WAIT_FOR_PULLBACK!
    assert res["approved"] is False
    assert res["veto_category"] == "DISTANCE_TO_STRUCTURE_VETO"
    assert res["trade_action"] == "WAIT_FOR_PULLBACK"
    assert "Distance-to-Structure Gate" in res["reasoning"]
