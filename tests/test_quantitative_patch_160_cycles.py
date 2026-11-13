import pytest
import time
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from agents.sentinel_agent import SentinelAgent
from agents.risk_manager import RiskManager
from core.deterministic_guard import DeterministicGuard
from services.nado_trading_service import NadoTradingService

# ==============================================================================
# BLOCK 01: THREE-STAGE PROFIT PROTECTION (HALF_RISK_LOCK, BE_SCRATCH, STEPPED ATR)
# ==============================================================================

@pytest.mark.asyncio
async def test_block01_sentinel_stage1_half_risk_lock_long():
    """
    At +0.75R MFE (and < +1.15R), Sentinel moves SL from -1.00R to -0.40R
    (compressing initial risk by 60% while giving room for 15m retest).
    """
    agent = SentinelAgent(logger=MagicMock())
    # LONG entry at 10000.0, planned SL at 9600.0 (planned_r = 400.0)
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "sl_price": 9600.0,
        "highest_price": 10320.0, # +320 = +0.80R MFE (>= +0.75R, < +1.15R)
        "three_stage_trailing": True,
        "planned_r_dist": 400.0,
        "protection_state": "PROTECTED"
    }
    market_data = {"price_data": {"current_price": 10300.0}}
    profile_rules = {"be_activation_r": 1.15}
    # Expected new SL: entry - 0.40 * planned_r = 10000.0 - 160.0 = 9840.0
    res = await agent.analyze(pos, market_data, profile_rules, 100.0)
    assert res is not None
    assert res["state"] == "HALF_RISK_LOCK"
    assert abs(res["new_sl"] - 9840.0) < 1e-4

@pytest.mark.asyncio
async def test_block01_sentinel_stage1_half_risk_lock_short():
    """
    At +0.75R MFE for SHORT, Sentinel moves SL from -1.00R to -0.40R.
    """
    agent = SentinelAgent(logger=MagicMock())
    # SHORT entry at 10000.0, planned SL at 10400.0 (planned_r = 400.0)
    pos = {
        "symbol": "SOL-USD",
        "direction": "SHORT",
        "entry_price": 10000.0,
        "sl_price": 10400.0,
        "lowest_price": 9680.0, # -320 = +0.80R MFE (>= +0.75R, < +1.15R)
        "three_stage_trailing": True,
        "planned_r_dist": 400.0,
        "protection_state": "PROTECTED"
    }
    market_data = {"price_data": {"current_price": 9700.0}}
    profile_rules = {"be_activation_r": 1.15}
    # Expected new SL: entry + 0.40 * planned_r = 10000.0 + 160.0 = 10160.0
    res = await agent.analyze(pos, market_data, profile_rules, 100.0)
    assert res is not None
    assert res["state"] == "HALF_RISK_LOCK"
    assert abs(res["new_sl"] - 10160.0) < 1e-4

@pytest.mark.asyncio
async def test_block01_sentinel_stage2_be_scratch_lock():
    """
    At +1.15R MFE (previously +0.75R), Sentinel moves SL to breakeven + buffer (+0.20R).
    """
    agent = SentinelAgent(logger=MagicMock())
    # LONG entry at 10000.0, planned SL at 9600.0 (planned_r = 400.0)
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "initial_sl": 9600.0,
        "sl_price": 9840.0,
        "highest_price": 10480.0, # +480 = +1.20R MFE (>= +1.15R, < +1.50R)
        "three_stage_trailing": True,
        "planned_r_dist": 400.0,
        "protection_state": "HALF_RISK_LOCKED"
    }
    market_data = {"price_data": {"current_price": 10450.0}}
    profile_rules = {"be_activation_r": 1.15}
    # Expected new SL: entry + 0.20 * planned_r = 10000.0 + 80.0 = 10080.0
    res = await agent.analyze(pos, market_data, profile_rules, 100.0)
    assert res is not None
    assert res["state"] == "BREAK_EVEN"
    assert abs(res["new_sl"] - 10080.0) < 1e-4

@pytest.mark.asyncio
async def test_block01_sentinel_stage3_stepped_atr_trail():
    """
    At +1.50R MFE, locks at least +0.75R; at +2.00R MFE, locks at least +1.20R or 1.2*ATR trail.
    """
    agent = SentinelAgent(logger=MagicMock())
    # LONG entry at 10000.0, planned SL at 9600.0 (planned_r = 400.0)
    # +1.60R MFE (highest_price = 10640.0), atr_14 = 100.0
    # Floor: 10000.0 + 0.75*400.0 = 10300.0. ATR trail = 10640 - 1.2*100 = 10520. Max(10520, 10300) = 10520.0
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "sl_price": 10080.0,
        "highest_price": 10640.0,
        "three_stage_trailing": True,
        "planned_r_dist": 400.0,
        "protection_state": "BE_LOCKED"
    }
    market_data = {"price_data": {"current_price": 10600.0}}
    profile_rules = {"be_activation_r": 1.15}
    res = await agent.analyze(pos, market_data, profile_rules, 100.0)
    assert res is not None
    assert res["state"] == "TRAILING"
    assert abs(res["new_sl"] - 10520.0) < 1e-4

@pytest.mark.asyncio
async def test_block01_sentinel_hysteresis_suppresses_small_improvements():
    """
    On-chain SL orders require >= 0.15R improvement to suppress CLOB trigger spam.
    """
    agent = SentinelAgent(logger=MagicMock())
    # planned_r = 400.0 -> 0.15R = 60.0
    # Current SL is already 9840.0. Highest price is 10320 (+0.80R).
    # Target SL at Stage 1 is 9840.0 -> improvement is 0.0 < 60.0 -> returns None
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "initial_sl": 9600.0,
        "sl_price": 9840.0,
        "highest_price": 10320.0,
        "three_stage_trailing": True,
        "planned_r_dist": 400.0,
        "protection_state": "HALF_RISK_LOCKED"
    }
    market_data = {"price_data": {"current_price": 10300.0}}
    profile_rules = {"be_activation_r": 1.15}
    res = await agent.analyze(pos, market_data, profile_rules, 100.0)
    assert res["new_sl"] is None
    assert "No update required" in res["reasoning"]

# ==============================================================================
# BLOCK 02: VOLATILITY-NORMALIZED DISTANCE-TO-STRUCTURE GATE (ATR & ASSET TIERS)
# ==============================================================================

@pytest.mark.asyncio
async def test_block02_asset_tier_distance_gate_btc_eth():
    """
    Tier 1 (BTC-USD, ETH-USD): base 1.65%, floor 1.20%, cap 1.75%.
    With 1h ATR% = 0.92%, dynamic ceiling = clip(1.8 * 0.92%, 1.20%, 1.75%) = 1.656%.
    A distance of 1.55% passes; a distance of 1.80% triggers DISTANCE_TO_STRUCTURE_VETO.
    """
    risk_mgr = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    portfolio = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}

    # Case A: Pass (swing level at 101.425, distance = 1.425% + 0.125% = 1.55% < 1.656%)
    market_pass = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "atr_14": 0.50,
            "atr_1h_pct": 0.92,
            "swing_high_15m": 101.425,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.001, "min_notional": 0.0}
    }
    ceo_pass = {
        "symbol": "BTC-USD",
        "decision": "SHORT",
        "conviction": 80,
        "trade_action": "ENTER",
        "directional_confidence": 80.0,
        "entry_quality": 80.0
    }
    verdict_pass = await risk_mgr.analyze(
        ceo_pass, portfolio, market_pass,
        effective_profile="BALANCED",
        macro_regime="TRENDING_BEAR",
        strategy_mode="TREND_FOLLOWING"
    )
    assert verdict_pass.get("approved") is True

    # Case B: Veto (swing level at 101.80, distance = 1.80% + 0.125% = 1.925% > 1.75% cap)
    market_fail = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "atr_14": 0.50,
            "atr_1h_pct": 0.92,
            "swing_high_15m": 101.80,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.001, "min_notional": 0.0}
    }
    verdict_fail = await risk_mgr.analyze(
        ceo_pass, portfolio, market_fail,
        effective_profile="BALANCED",
        macro_regime="TRENDING_BEAR",
        strategy_mode="TREND_FOLLOWING"
    )
    assert verdict_fail.get("approved") is False
    assert verdict_fail.get("veto_category") == "DISTANCE_TO_STRUCTURE_VETO"

@pytest.mark.asyncio
async def test_block02_asset_tier_distance_gate_high_beta_alts():
    """
    Tier 3 (ZEC, HYPE, LIT): base ceiling 3.10% (floor 1.95%, cap 3.25%).
    LIT-USD with 1.96% SL distance passes cleanly (previously vetoed by 1.95% flat limit).
    """
    risk_mgr = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    portfolio = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    # LIT-USD: current price 100.0, swing_high at 101.835 -> distance = 1.835 + 0.125 = 1.96%
    # With atr_1h_pct = 1.40%, dynamic ceiling = min(max(1.8*1.4, 1.95), 3.25) = 2.52%
    market_lit = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "atr_14": 0.50,
            "atr_1h_pct": 1.40,
            "swing_high_15m": 101.835,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 1.0, "min_notional": 0.0}
    }
    ceo_lit = {
        "symbol": "LIT-USD",
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "ENTER",
        "directional_confidence": 85.0,
        "entry_quality": 80.0
    }
    verdict_lit = await risk_mgr.analyze(
        ceo_lit, portfolio, market_lit,
        effective_profile="BALANCED",
        macro_regime="TRENDING_BEAR",
        strategy_mode="TREND_FOLLOWING"
    )
    assert verdict_lit.get("approved") is True

# ==============================================================================
# BLOCK 03: DETERMINISTIC ANALYST_QUORUM_GATE
# ==============================================================================

def test_block03_analyst_quorum_gate_all_neutral_insufficient():
    """
    Cycle #19 scenario: all 4 technical sub-agents are NEUTRAL (50%).
    Must strictly veto with QUORUM_INSUFFICIENT_VETO.
    """
    reports = [
        {"agent_name": "Candle_Agent", "signal": "NEUTRAL", "confidence": 50},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL", "confidence": 50},
        {"agent_name": "OI_Funding_Agent", "signal": "NEUTRAL", "confidence": 50},
        {"agent_name": "News_Agent", "signal": "NEUTRAL", "confidence": 30},
        {"agent_name": "Indicator_Agent", "signal": "NEUTRAL", "confidence": 50}
    ]
    is_valid, veto_tag, reason = DeterministicGuard.evaluate_analyst_quorum("SHORT", reports)
    assert is_valid is False
    assert veto_tag == "QUORUM_INSUFFICIENT_VETO"
    assert "All 4 technical sub-agents are NEUTRAL" in reason

def test_block03_analyst_quorum_gate_counter_momentum_divergence():
    """
    Cycle #17 scenario: Both Candle_Agent (BULLISH 70%) and Indicator_Agent (BULLISH 51%)
    oppose CEO proposed SHORT. Must strictly veto with COUNTER_MOMENTUM_DIVERGENCE_VETO.
    """
    reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 70},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL", "confidence": 50},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH", "confidence": 65},
        {"agent_name": "News_Agent", "signal": "NEUTRAL", "confidence": 40},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 55}
    ]
    is_valid, veto_tag, reason = DeterministicGuard.evaluate_analyst_quorum("SHORT", reports)
    assert is_valid is False
    assert veto_tag == "COUNTER_MOMENTUM_DIVERGENCE_VETO"
    assert "Both Candle_Agent" in reason and "Indicator_Agent" in reason

def test_block03_analyst_quorum_gate_valid_quorum():
    """
    Valid quorum: 2 technical sub-agents match direction with confidence >= 60%.
    """
    reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH", "confidence": 65},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL", "confidence": 50},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH", "confidence": 70},
        {"agent_name": "News_Agent", "signal": "NEUTRAL", "confidence": 40},
        {"agent_name": "Indicator_Agent", "signal": "NEUTRAL", "confidence": 50}
    ]
    is_valid, veto_tag, reason = DeterministicGuard.evaluate_analyst_quorum("SHORT", reports)
    assert is_valid is True
    assert veto_tag is None

# ==============================================================================
# BLOCK 04: REDUCE_SIZE RISK-CAP ENFORCEMENT & $108.50 SAFE BUMP CALIBRATION
# ==============================================================================

@pytest.mark.asyncio
async def test_block04_reduce_size_risk_cap_veto():
    """
    When trade_action == 'REDUCE_SIZE', bumping to minimum notional must not cause
    risk > $1.35 USDC. If it exceeds $1.35, vetoes with REDUCE_SIZE_RISK_CAP_VETO.
    """
    risk_mgr = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    # SL distance = 2.625%. Tier 3 dynamic ceiling with atr_1h_pct=1.60% gives 2.88% (passes distance gate)
    # Balance $70.0 -> natural notional ~$40.0. Needs bump to $105.50 (min_notional: 100.0)
    # Bumped risk = 1.055 * 2.625 = $2.77 > $1.35 cap -> REDUCE_SIZE_RISK_CAP_VETO!
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "atr_14": 0.50,
            "atr_1h_pct": 1.60,
            "swing_high_15m": 102.50,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.001, "min_notional": 100.0}
    }
    portfolio = {"total_usd": 200.0, "available_margin": 200.0, "active_positions": {}}
    ceo_decision = {
        "symbol": "ZEC-USD",
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "REDUCE_SIZE",
        "directional_confidence": 85.0,
        "entry_quality": 80.0
    }
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING_BEAR",
        strategy_mode="TREND_FOLLOWING"
    )
    assert verdict.get("approved") is False
    assert verdict.get("veto_category") == "REDUCE_SIZE_RISK_CAP_VETO"
    assert "to exceed REDUCE_SIZE cap ($1.35 USDC)" in verdict.get("reasoning")

@pytest.mark.asyncio
async def test_block04_safe_bump_calibration_targets():
    """
    Safe bump targets are calibrated to $108.50 for BTC/ETH.
    """
    risk_mgr = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    portfolio = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_btc = {
        "price_data": {"current_price": 60000.0},
        "indicators": {
            "atr_14": 200.0,
            "atr_1h_pct": 0.90,
            "swing_high_15m": 60500.0,
            "swing_low_15m": 59700.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.001, "min_notional": 0.0}
    }
    ceo_btc = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 85,
        "trade_action": "ENTER",
        "directional_confidence": 85.0,
        "entry_quality": 80.0
    }
    verdict_btc = await risk_mgr.analyze(
        ceo_btc, portfolio, market_btc,
        effective_profile="BALANCED",
        macro_regime="TRENDING_BULL",
        strategy_mode="TREND_FOLLOWING"
    )
    assert verdict_btc.get("approved") is True
    assert verdict_btc.get("notional_size_usd") >= 108.50

# ==============================================================================
# BLOCK 05: SAME-SYMBOL DUPLICATE ENTRY GUARD & TRADE LOGGER INTEGRITY
# ==============================================================================

@pytest.mark.asyncio
async def test_block05_duplicate_position_prevention_in_service():
    """
    Verifies that NadoTradingService strictly prevents opening a duplicate position
    when the symbol or base ticker is already present in active_positions.
    """
    mock_client = MagicMock()
    service = NadoTradingService(nado_client=mock_client)
    service.is_connected = True
    service.active_positions["ETH-USD"] = {
        "symbol": "ETH-USD",
        "direction": "SHORT",
        "entry_price": 2056.19,
        "contracts": 0.05,
        "protection_state": "NATIVE_SL_CONFIRMED"
    }

    # Attempt to open another ETH position
    res = await service.open_position(
        symbol="ETH-USD",
        direction="SHORT",
        entry_price=2047.97,
        notional_usd=108.50,
        tp_price=2000.0,
        sl_price=2065.0,
        leverage=10,
        contracts=0.05,
        approved_risk_usd=1.05
    )
    assert res is False
    assert service.last_execution_status == "DUPLICATE_POSITION_BLOCKED"

def test_block05_trade_logger_records_unique_id_and_close_metadata():
    """
    Verifies that _record_closed_trade_journal records unique position_id, close_time,
    and exact realized R without mutating or losing prior entries.
    """
    mock_client = MagicMock()
    service = NadoTradingService(nado_client=mock_client)
    pos_data = {
        "position_id": "ETH-USD_1731512345000",
        "open_time": 1731512345.0,
        "planned_risk_usd": 1.05,
        "contracts": 0.05,
        "entry_price": 2056.19,
        "strategy_mode": "TREND_FOLLOWING",
        "macro_regime": "TRENDING"
    }
    with patch("core.state_store.StateStore.load", return_value=[{"symbol": "SOL-USD"}]), \
         patch("core.state_store.StateStore.save") as mock_save:
        realized_r = service._record_closed_trade_journal(
            symbol="ETH-USD",
            direction="SHORT",
            entry_price=2056.19,
            exit_price=2045.00,
            net_pnl=0.5595,
            pos_data=pos_data,
            triggered_by="BE_SCRATCH_LOCK"
        )
        assert realized_r == round(0.5595 / 1.05, 4)
        assert mock_save.called
        saved_journal = mock_save.call_args[0][1]
        assert len(saved_journal) == 2
        entry = saved_journal[-1]
        assert entry["position_id"] == "ETH-USD_1731512345000"
        assert entry["triggered_by"] == "BE_SCRATCH_LOCK"
        assert entry["close_reason"] == "BE_SCRATCH_LOCK"
        assert "close_time" in entry
        assert entry["net_pnl_usd"] == 0.5595
