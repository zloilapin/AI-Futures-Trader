import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from agents.risk_manager import RiskManager
from core.logger import TradeLogger
from core.models import FinalTradeDecision
from services.nado_trading_service import NadoTradingService

@pytest.fixture
def mock_logger():
    return MagicMock(spec=TradeLogger)

@pytest.fixture
def mock_llm():
    return MagicMock()

@pytest.fixture
def risk_mgr(mock_logger, mock_llm):
    return RiskManager(mock_logger, mock_llm)

@pytest.fixture
def base_market_data():
    return {
        "price_data": {
            "current_price": 100.0,
            "ohlcv_1h": [{"volume": 100}] * 10
        },
        "indicators": {
            "atr_14": 2.0,
            "ema_20": 100.0,
            "bb_middle": 100.0
        },
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.001,
            "min_notional": 10.0
        }
    }

@pytest.fixture
def base_portfolio_data():
    return {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {},
        "win_count": 0,
        "loss_count": 0,
        "recent_streak": []
    }

# ==============================================================================
# 1. DAILY DRAWDOWN VETO GATE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_vetos_excessive_daily_drawdown(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When daily drawdown reaches or exceeds max_daily_drawdown_pct (e.g. 6% in BALANCED),
    RiskManager must strictly veto new trade entries with MAX_DAILY_DRAWDOWN.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # Simulate 6.5% daily drawdown via daily_drawdown_pct
    portfolio = dict(base_portfolio_data, daily_drawdown_pct=0.065)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_DAILY_DRAWDOWN"
    assert "Daily drawdown limit" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_vetos_negative_daily_pnl_pct(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When daily drawdown is provided as a negative daily_pnl_pct (-7.0%),
    RiskManager detects the breach and vetoes with MAX_DAILY_DRAWDOWN.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # Simulate -7.0% daily loss via daily_pnl_pct
    portfolio = dict(base_portfolio_data, daily_pnl_pct=-7.0)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_DAILY_DRAWDOWN"

@pytest.mark.asyncio
async def test_risk_manager_allows_safe_daily_drawdown(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When daily drawdown is well within safety limits (e.g. 1.5% < 6.0%),
    RiskManager approves the trade.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    portfolio = dict(base_portfolio_data, daily_drawdown_pct=0.015)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is True
    assert verdict["veto_category"] is None

# ==============================================================================
# 2. MAX CONCURRENT POSITIONS VETO GATE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_vetos_when_max_concurrent_positions_reached(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When active_positions count reaches max_concurrent_positions limit (e.g. 3),
    RiskManager independently enforces the cap as defense-in-depth with MAX_CONCURRENT_POSITIONS veto.
    """
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # 3 existing open positions
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1},
        "AVAX-USD": {"entry_price": 30.0, "sl_price": 29.0, "amount": 10.0}
    })
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_CONCURRENT_POSITIONS"
    assert "Maximum concurrent positions reached" in verdict["reasoning"]

# ==============================================================================
# 3. DUPLICATE ASSET EXPOSURE VETO GATE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_vetos_duplicate_asset_position(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    If a position for the candidate asset is already open in active_positions,
    RiskManager strictly vetoes the trade with ALREADY_OPEN to prevent accidental double allocation.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # BTC position is already active
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01}
    })
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "ALREADY_OPEN"
    assert "already active" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_vetos_duplicate_base_asset_alias(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    Duplicate asset check normalizes symbol format (BTC vs BTC-USD) and prevents duplicate exposure.
    """
    ceo_decision = {
        "symbol": "BTC",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01}
    })
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "ALREADY_OPEN"

# ==============================================================================
# 4. KELLY SIZING MULTIPLIER TESTS
# ==============================================================================

def test_kelly_multiplier_scaling(risk_mgr):
    """
    Day 7 Audit:
    Validates Fractional (Half) Kelly sizing formula:
    - total_trades < 15: neutral 1.00x
    - Edge positive (55% WR, 1.80 RR): boost scaled to 1.15x
    - Edge negative or soft (35% WR, 1.20 RR): cut to 0.50x
    - Clamped within [0.50, 1.25]
    """
    # 1. Sample size too small (< 15) -> neutral 1.0x
    assert risk_mgr._calculate_kelly_multiplier(win_rate=0.70, realized_rr=2.0, total_trades=10) == 1.0

    # 2. Positive edge (55% WR, 1.8 RR) -> boost
    k_boost = risk_mgr._calculate_kelly_multiplier(win_rate=0.55, realized_rr=1.8, total_trades=20)
    assert 1.0 < k_boost <= 1.25

    # 3. Negative edge (35% WR, 1.2 RR) -> cut to 0.50x
    k_cut = risk_mgr._calculate_kelly_multiplier(win_rate=0.35, realized_rr=1.2, total_trades=20)
    assert k_cut == 0.50

    # 4. Extreme edge (90% WR, 3.0 RR) -> ceiling 1.25x
    k_ceiling = risk_mgr._calculate_kelly_multiplier(win_rate=0.90, realized_rr=3.0, total_trades=30)
    assert k_ceiling == 1.25

# ==============================================================================
# 5. DEFENSIVE GUARDS & NONE-TYPE IMMUNITY TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_guards_against_none_and_invalid_inputs(risk_mgr):
    """
    Day 7 Audit:
    Verifies that RiskManager gracefully handles None and corrupted inputs
    without raising unhandled exceptions.
    """
    # 1. Null decision -> veto NO_SIGNAL
    r1 = await risk_mgr.analyze(None, {}, {})
    assert r1["approved"] is False
    assert r1["veto_category"] == "NO_SIGNAL"

    # 2. Invalid price (0.0) -> veto INVALID_PRICE
    ceo = {"decision": "LONG", "conviction": 80, "symbol": "BTC-USD"}
    r2 = await risk_mgr.analyze(ceo, {"total_usd": 1000.0}, {"price_data": {"current_price": 0.0}})
    assert r2["approved"] is False
    assert r2["veto_category"] == "INVALID_PRICE"

    # 3. Invalid ATR (0.0) -> veto INVALID_ATR
    r3 = await risk_mgr.analyze(ceo, {"total_usd": 1000.0}, {"price_data": {"current_price": 100.0}, "indicators": {"atr_14": 0.0}})
    assert r3["approved"] is False
    assert r3["veto_category"] == "INVALID_ATR"

    # 4. Insufficient balance (0.0) -> veto INSUFFICIENT_BALANCE
    r4 = await risk_mgr.analyze(ceo, {"total_usd": 0.0}, {"price_data": {"current_price": 100.0}, "indicators": {"atr_14": 1.0}})
    assert r4["approved"] is False
    assert r4["veto_category"] == "INSUFFICIENT_BALANCE"

# ==============================================================================
# 6. NADO TRADING SERVICE DAILY DRAWDOWN TRACKING
# ==============================================================================

@pytest.mark.asyncio
async def test_nado_trading_service_tracks_daily_drawdown():
    """
    Day 7 Audit:
    Verifies that NadoTradingService computes daily_pnl_usd, daily_pnl_pct,
    and daily_drawdown_pct in get_portfolio_summary().
    """
    service = NadoTradingService()
    service.is_connected = True
    service.default_subaccount_id = "0x1234"
    
    # Mock client and get_active_positions
    service.client = MagicMock()
    mock_summary = MagicMock()
    mock_health = MagicMock()
    mock_health.liabilities = 0
    mock_health.health = int(500 * 1e18)
    mock_health.assets = int(1000 * 1e18)
    mock_summary.healths = [mock_health]
    mock_spot = MagicMock()
    mock_spot.product_id = 0
    mock_spot.balance.amount = int(1000 * 1e18)
    mock_summary.spot_balances = [mock_spot]
    
    service.client.subaccount.get_engine_subaccount_summary = MagicMock(return_value=mock_summary)
    service.get_active_positions = AsyncMock(return_value=[])
    
    # 1. Baseline day start at $1000
    summary1 = await service.get_portfolio_summary()
    assert summary1["current_balance"] == 1000.0
    assert summary1["daily_pnl_usd"] == 0.0
    assert summary1["daily_pnl_pct"] == 0.0
    assert summary1["daily_drawdown_pct"] == 0.0
    
    # 2. Simulate equity drop to $940 (6% daily drawdown)
    mock_spot.balance.amount = int(940 * 1e18)
    summary2 = await service.get_portfolio_summary()
    assert summary2["current_balance"] == 940.0
    assert summary2["daily_pnl_usd"] == -60.0
    assert summary2["daily_pnl_pct"] == -6.0
    assert summary2["daily_drawdown_pct"] == 0.06
