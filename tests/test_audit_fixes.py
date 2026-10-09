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

    ceo_dict = {"decision": "LONG", "conviction": 80}
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
    assert trade.decision == "LONG"
    assert trade.raw_ceo_verdict["decision"] == "LONG"

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
