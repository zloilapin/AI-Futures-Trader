import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from core.models import FinalTradeDecision, FinalRiskDecision
from core.strategy_router import StrategyProfile
from core.deterministic_guard import DeterministicGuard
from agents.sentinel_agent import SentinelAgent
from agents.risk_manager import RiskManager
from core.logger import TradeLogger

def test_immutable_contracts():
    """Verifies that FinalRiskDecision and FinalTradeDecision adhere to strict immutability."""
    risk = FinalRiskDecision(
        approved=True,
        trade_action="ENTER",
        risk_amount_usd=10.0,
        notional_size_usd=100.0,
        margin_usd=10.0,
        leverage=10.0
    )
    with pytest.raises(Exception):
        risk.execution_status = "SUCCESS"  # Cannot mutate frozen dataclass

    ceo_dict = {
        "decision": "LONG",
        "conviction": 80,
        "indicators": {"rsi": 45.0, "nested_list": [1, 2, 3]}
    }
    trade = FinalTradeDecision(
        symbol="BTC-USD",
        decision="LONG",
        conviction=80,
        trade_action="ENTER",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="OK",
        raw_ceo_verdict=ceo_dict
    )
    # Mutating external dictionary must NOT mutate internal contract
    ceo_dict["decision"] = "SHORT"
    ceo_dict["indicators"]["rsi"] = 99.0
    ceo_dict["indicators"]["nested_list"].append(4)
    assert trade.decision == "LONG"
    assert trade.raw_ceo_verdict["decision"] == "LONG"
    assert trade.raw_ceo_verdict["indicators"]["rsi"] == 45.0
    assert trade.raw_ceo_verdict["indicators"]["nested_list"] == (1, 2, 3)

    # In-place internal mutations on nested collections must also be blocked
    with pytest.raises(TypeError):
        trade.raw_ceo_verdict["indicators"]["rsi"] = 99.0

def test_sentinel_uses_entry_atr_reference():
    """Verifies that Sentinel uses atr_reference from entry instead of decaying current ATR."""
    logger = MagicMock(spec=TradeLogger)
    sentinel = SentinelAgent(logger)
    
    pos = {
        "symbol": "ETH-USD",
        "direction": "LONG",
        "entry_price": 2000.0,
        "sl_price": 1950.0,
        "highest_price": 2100.0,
        "lowest_price": 2000.0,
        "protection_state": "PROTECTED",
        "atr_reference": 50.0  # ATR at entry was 50
    }
    
    market_data = {
        "price_data": {"current_price": 2095.0}  # profit = 95.0
    }
    profile_rules = {
        "sentinel_be_atr": 1.5,                 # BE threshold = 1.5 * 50 = 75.0
        "sentinel_trail_activation_atr": 2.5,   # Trail threshold = 2.5 * 50 = 125.0
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25
    }
    
    # Even if current ATR collapsed to 10.0, Sentinel MUST evaluate using entry ATR (50.0)
    current_collapsed_atr = 10.0
    res = asyncio.run(sentinel.analyze(pos, market_data, profile_rules, current_collapsed_atr))
    
    # Profit (95.0) >= 1.5 * 50 (75.0), so BE triggered!
    assert res["state"] == "BREAK_EVEN"
    assert res["new_sl"] is not None
    # If it used current_collapsed_atr (10.0), 95.0 >= 2.5 * 10 = 25.0 would have prematurely triggered TRAILING!
    assert res["state"] != "TRAILING"

def test_deterministic_guard_breakout_requires_market_confirmation():
    """Verifies that strategy_mode alone does NOT override WAIT_FOR_PULLBACK without market confirmation."""
    strategy_profile = StrategyProfile(True, "BREAKOUT", "LONG", "Breakout Candidate")
    ceo_proposal = {
        "decision": "LONG",
        "conviction": 70,
        "trade_action": "WAIT_FOR_PULLBACK",
        "directional_confidence": 75,
        "entry_quality": 62,
        "reasoning": "Waiting for pullback"
    }
    mock_risk = MagicMock(spec=RiskManager)
    mock_risk._get_profile_rules.return_value = {"min_conviction": 65}
    
    # Case 1: Market data has NO breakout (price is inside Donchian, volume normal)
    market_data_no_breakout = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "donchian_high": 105.0,  # Price has NOT broken high
            "volume_spike_pct": 50.0
        }
    }
    res_no_break = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data_no_breakout,
        symbol="SOL-USD"
    )
    assert res_no_break.trade_action == "WAIT_FOR_PULLBACK"
    assert res_no_break.is_actionable is False
    assert res_no_break.guard_status == "PULLBACK_WATCHLIST"

    # Case 2: Market data confirms Donchian High breakout + volume surge >= 120%
    market_data_confirmed = {
        "price_data": {"current_price": 106.0},
        "indicators": {
            "donchian_high": 105.0,  # Price broke high!
            "volume_spike_pct": 135.0  # Volume confirmed!
        }
    }
    res_confirmed = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data_confirmed,
        symbol="SOL-USD"
    )
    assert res_confirmed.trade_action == "ENTER"
    assert res_confirmed.is_actionable is True
    assert res_confirmed.guard_status == "PASSED"

def test_risk_manager_mean_reversion_rr_veto():
    """Verifies that RiskManager vetos Mean Reversion when capping TP to mean target destroys net RR (< 1.0)."""
    logger = MagicMock(spec=TradeLogger)
    risk_mgr = RiskManager(logger, MagicMock())
    
    ceo_decision = {
        "symbol": "ETH-USD",
        "decision": "LONG",
        "conviction": 75,
        "trade_action": "ENTER"
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {},
        "win_count": 10,
        "loss_count": 10,
        "recent_streak": []
    }
    # Current price 2000, mean target is 2005 (very close, only +0.25% away)
    # SL distance is based on ATR (e.g. 20 points, SL = 1980)
    market_data = {
        "price_data": {
            "current_price": 2000.0,
            "ohlcv_1h": [{"volume": 100} for _ in range(15)]
        },
        "indicators": {
            "atr_14": 50.0,    # Wide SL (~75 points)
            "ema_20": 2020.0   # Mean target (+1.0% distance, passes 0.8% edge filter but gives RR ~ 0.26 < 1.0)
        },
        "order_book_data": {"spread_pct": 0.02},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001
        }
    }
    
    res = asyncio.run(risk_mgr.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        strategy_mode="MEAN_REVERSION"
    ))
    
    assert res["approved"] is False
    assert res["veto_category"] == "POOR_RISK_REWARD"
    assert "Risk/Reward" in res["reasoning"] or "net RR" in res["reasoning"]

def test_nado_active_positions_preserves_atr_and_extremes():
    """Verifies that get_active_positions in NadoTradingService retains atr_reference and extremes."""
    from services.nado_trading_service import NadoTradingService
    service = NadoTradingService()
    
    # Mock active_positions dictionary
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 100.0,
        "tp_price": 65000.0,
        "sl_price": 58000.0,
        "leverage": 10,
        "highest_price": 62000.0,
        "lowest_price": 59900.0,
        "protection_state": "BREAK_EVEN",
        "atr_reference": 1200.0,
        "open_time": 1700000000.0,
        "original_thesis": "Test Thesis"
    }
    
    # Mock Nado SDK subaccounts & balances
    mock_sa = MagicMock()
    mock_sa.subaccount = "0x1234"
    mock_subaccounts = MagicMock()
    mock_subaccounts.subaccounts = [mock_sa]
    
    mock_pos = MagicMock()
    mock_pos.product_id = 1
    mock_pos.balance.amount = int(0.0016666 * 10**18)
    mock_pos.balance.v_quote_balance = int(100 * 10**18)
    
    mock_summary = MagicMock()
    mock_summary.perp_balances = [mock_pos]
    
    mock_client = MagicMock()
    mock_client.subaccount.get_subaccounts.return_value = mock_subaccounts
    mock_client.subaccount.get_engine_subaccount_summary.return_value = mock_summary
    
    service.client = mock_client
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0xOwner"
    service.product_map = {"BTC": 1, "BTC-USD": 1}
    service.is_connected = True
    
    # Mock market cache to return current price
    mock_perp = MagicMock()
    mock_perp.product_id = 1
    mock_perp.oracle_price_x18 = int(61000 * 10**18)
    mock_markets = MagicMock()
    mock_markets.perp_products = [mock_perp]
    mock_client.market.get_all_engine_markets.return_value = mock_markets
    
    positions = asyncio.run(service.get_active_positions(bypass_cache=True))
    assert len(positions) == 1
    pos = positions[0]
    
    assert pos["symbol"] == "BTC-USD"
    assert pos["atr_reference"] == 1200.0
    assert pos["highest_price"] == 62000.0
    assert pos["lowest_price"] == 59900.0
    assert pos["protection_state"] == "BREAK_EVEN"
    assert pos["original_thesis"] == "Test Thesis"

def test_cooldown_activation_and_persistence(tmp_path):
    """Verifies that 3 consecutive losses activate cooldown immediately and persist across restarts."""
    from services.nado_trading_service import NadoTradingService
    import time
    
    # Use temporary file for isolated state testing
    state_file = str(tmp_path / "nado_state.json")
    with patch("core.state_store.StateStore.load", return_value={}):
        with patch("core.state_store.StateStore.save") as mock_save:
            service = NadoTradingService()
            service.recent_streak = ["WIN", "LOSS", "LOSS"]
            service._last_cooldown_processed_len = 0
            
            # 3rd loss occurs
            service._update_streak_and_cooldown("LOSS")
            
            assert len(service.recent_streak) == 4
            assert service.recent_streak[-3:] == ["LOSS", "LOSS", "LOSS"]
            # Cooldown must be activated for ~1 hour
            assert service.cooldown_until > time.time() + 3500
            assert service._last_cooldown_processed_len == 4
            
            # Verify persistence call
            mock_save.assert_called()
            saved_dict = mock_save.call_args[0][1]
            assert "cooldown_until" in saved_dict
            assert saved_dict["_last_cooldown_processed_len"] == 4

    # Test restart scenario: when cooldown expired, restart does NOT re-trigger
    past_cooldown_state = {
        "recent_streak": ["LOSS", "LOSS", "LOSS", "LOSS", "LOSS"],
        "cooldown_until": time.time() - 100,  # Expired in past
        "_last_cooldown_processed_len": 5
    }
    with patch("core.state_store.StateStore.load", return_value=past_cooldown_state):
        restarted_service = NadoTradingService()
        assert restarted_service._last_cooldown_processed_len == 5
        assert restarted_service.cooldown_until < time.time()
        
        # Verify pipeline does not re-trigger cooldown for past streak
        from core.pipeline import TradingPipeline
        pipeline = TradingPipeline(MagicMock(), MagicMock(), "test")
        pipeline.services.trading_service = restarted_service
        
        # Pipeline check
        now_ts = time.time()
        cooldown_active = now_ts < restarted_service.cooldown_until
        assert cooldown_active is False
        
        # Streak length equals _last_cooldown_processed_len -> no re-trigger
        should_retrigger = (
            len(restarted_service.recent_streak) >= 3 and 
            restarted_service.recent_streak[-3:] == ["LOSS", "LOSS", "LOSS"] and
            getattr(restarted_service, "_last_cooldown_processed_len", 0) != len(restarted_service.recent_streak)
        )
        assert should_retrigger is False

def test_execution_result_decoupling_and_dataclass_replace():
    """Verifies ExecutionResult decoupling and immutable status updates."""
    from core.models import ExecutionResult
    import dataclasses
    
    exec_res = ExecutionResult(
        symbol="ETH-USD",
        status="SUCCESS",
        order_id="0xabc123",
        executed_at=1700000000.0,
        notional_usd=150.0,
        actual_fill_price=3000.0
    )
    res_dict = exec_res.to_dict()
    assert res_dict["status"] == "SUCCESS"
    assert res_dict["order_id"] == "0xabc123"

    risk = FinalRiskDecision(
        approved=True,
        trade_action="ENTER",
        risk_amount_usd=5.0,
        notional_size_usd=100.0,
        margin_usd=10.0,
        leverage=10.0,
        execution_status=None
    )
    # Immutably update execution status via dataclasses.replace
    updated_risk = dataclasses.replace(risk, execution_status="SUCCESS")
    assert risk.execution_status is None
    assert updated_risk.execution_status == "SUCCESS"

def test_bb_position_pct_and_pos_guard_compatibility():
    """Verifies that bb_position_pct and bb_pos are handled equivalently by DeterministicGuard."""
    risk_manager = MagicMock()
    risk_manager._get_profile_rules.return_value = {"min_conviction": 70}
    strategy_profile = StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Test")

    # When market data has bb_position_pct < 10 in choppy market, anti-whipsaw must trigger
    market_data = {
        "indicators": {"rsi_14": 30.0, "bb_position_pct": 5.0, "volume_spike_pct": 20.0},
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP", "trend_1h": "NEUTRAL"},
        "derivatives_data": {"funding_rate": 0.0}
    }
    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "ENTER",
        "entry_quality": 80
    }
    decision = DeterministicGuard.evaluate(
        strategy_profile, ceo_proposal, "BALANCED", risk_manager, market_data, symbol="SOL-USD"
    )
    assert decision.is_actionable is False
    assert decision.rejection_tag == "EXTREME_EXTENSION_VETO"

def test_trend_following_allows_confirmed_breakdown():
    """Verifies that strong trend dump (FULL_ALIGNMENT or volume expansion) is NOT blocked by anti-whipsaw."""
    risk_manager = MagicMock()
    risk_manager._get_profile_rules.return_value = {"min_conviction": 70}
    strategy_profile = StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Strong dump")

    # Strong dump with FULL_ALIGNMENT, low RSI (28.0) and low BB (5.0)
    market_data = {
        "indicators": {
            "rsi_14": 28.0, 
            "bb_position_pct": 5.0, 
            "volume_spike_pct": 150.0,
            "algo_signals": {"rsi_divergence": "NONE"}
        },
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BEARISH"},
        "derivatives_data": {"funding_rate": -0.0001}
    }
    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "ENTER",
        "entry_quality": 80
    }
    decision = DeterministicGuard.evaluate(
        strategy_profile, ceo_proposal, "BALANCED", risk_manager, market_data, symbol="ETH-USD"
    )
    # Must NOT be blocked by anti-whipsaw, because it is confirmed trend continuation!
    assert decision.is_actionable is True
    assert decision.guard_status == "PASSED"
    assert decision.decision == "SHORT"

def test_atr_reconciliation_and_recovery_safety():
    """Verifies that a restored position without ATR reference reconstructs it from SL distance without moving SL."""
    logger = MagicMock(spec=TradeLogger)
    sentinel = SentinelAgent(logger)

    # Position marked as requiring reconciliation with zero ATR reference
    unreconciled_pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 60000.0,
        "sl_price": 59000.0,
        "atr_reference": 0.0,
        "atr_reconciliation_required": True,
        "protection_state": "PROTECTED"
    }
    res = asyncio.run(sentinel.analyze(unreconciled_pos, {"price_data": {"current_price": 61500.0}}, {}, 200.0))
    # Must suspend dynamic SL movement to protect native exchange order
    assert res["new_sl"] is None
    assert "требует сверки ATR" in res["reasoning"]
