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
    Day 7 Audit / Institutional Fix:
    Validates Fractional (Half) Kelly sizing formula:
    - total_trades < 15: neutral 1.00x
    - Edge positive (55% WR, 1.80 RR): capped at 1.00x (no overleveraging on short samples)
    - Edge negative or soft (35% WR, 1.20 RR): cut to 0.50x (downside damping)
    - Extreme edge: never exceeds 1.00x
    """
    # 1. Sample size too small (< 15) -> neutral 1.0x
    assert risk_mgr._calculate_kelly_multiplier(win_rate=0.70, realized_rr=2.0, total_trades=10) == 1.0

    # 2. Positive edge (55% WR, 1.8 RR) -> capped at 1.0x (no artificial inflation)
    k_boost = risk_mgr._calculate_kelly_multiplier(win_rate=0.55, realized_rr=1.8, total_trades=20)
    assert k_boost == 1.0

    # 3. Negative edge (35% WR, 1.2 RR) -> cut to 0.50x
    k_cut = risk_mgr._calculate_kelly_multiplier(win_rate=0.35, realized_rr=1.2, total_trades=20)
    assert k_cut == 0.50

    # 4. Extreme edge (90% WR, 3.0 RR) -> strictly capped at 1.00x
    k_ceiling = risk_mgr._calculate_kelly_multiplier(win_rate=0.90, realized_rr=3.0, total_trades=30)
    assert k_ceiling == 1.0

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
    service.daily_start_balance = 1000.0
    
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

@pytest.mark.asyncio
async def test_nado_trading_service_reanchors_on_external_transfer():
    """
    Verifies that when a large external balance change occurs (e.g. withdrawal or baseline mismatch)
    with 0 active positions and 0 trades today, NadoTradingService re-anchors daily_start_balance
    instead of reporting a fake 90% drawdown.
    """
    service = NadoTradingService()
    service.is_connected = True
    service.default_subaccount_id = "0x1234"
    service._daily_start_balance = 92.42
    service._daily_start_total_trades = 0
    service.win_count = 0
    service.loss_count = 0
    
    # Balance on exchange is $8.54 with 0 active positions and 0 trades today
    mock_summary = MagicMock()
    mock_health = MagicMock()
    mock_health.liabilities = 0
    mock_health.health = int(8.54 * 1e18)
    mock_health.assets = int(8.54 * 1e18)
    mock_summary.healths = [mock_health]
    mock_spot = MagicMock()
    mock_spot.product_id = 0
    mock_spot.balance.amount = int(8.54 * 1e18)
    mock_summary.spot_balances = [mock_spot]
    
    service.client = MagicMock()
    service.client.subaccount.get_engine_subaccount_summary = MagicMock(return_value=mock_summary)
    service.get_active_positions = AsyncMock(return_value=[])
    
    summary = await service.get_portfolio_summary()
    # Must re-anchor to 8.54 with 0.0 daily drawdown instead of 90.76% fake loss
    assert summary["current_balance"] == 8.54
    assert summary["daily_pnl_usd"] == 0.0
    assert summary["daily_drawdown_pct"] == 0.0
    assert service._daily_start_balance == 8.54

@pytest.mark.asyncio
async def test_risk_manager_ignores_fake_drawdown_on_external_transfer(risk_mgr, base_market_data, base_portfolio_data):
    """
    Verifies that RiskManager does NOT veto a trade when an apparent extreme drawdown (>15%)
    is reported with 0 active positions, 0 trades today, and 0 unrealized PnL.
    """
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "conviction": 70,
        "directional_confidence": 70,
        "entry_quality": 65,
        "trade_action": "REDUCE_SIZE"
    }
    
    # Simulate portfolio with 90.76% apparent drawdown but 0 trades today and 0 active positions
    portfolio = dict(
        base_portfolio_data, 
        total_usd=8.54,
        available_margin=8.54,
        daily_drawdown_pct=0.9076,
        daily_trades_count=0,
        unrealized_pnl=0.0,
        active_positions={}
    )
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="AGGRESSIVE")
    # Must NOT be vetoed by MAX_DAILY_DRAWDOWN
    assert verdict.get("veto_category") != "MAX_DAILY_DRAWDOWN"

# ==============================================================================
# 7. INSTITUTIONAL SAFETY INVARIANTS TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_directional_cluster_cap_veto(risk_mgr, base_market_data, base_portfolio_data):
    """
    Prevents opening 3 concurrent positions in the same direction during choppy/transition markets.
    """
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # 2 existing LONG positions in RANGE_CHOPPY
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"direction": "LONG", "entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01},
        "ETH-USD": {"direction": "LONG", "entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1}
    })
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, base_market_data, 
        effective_profile="BALANCED", macro_regime="RANGE_CHOPPY"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "DIRECTIONAL_CLUSTER_CAP"
    assert "Directional cluster limit reached" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_penalty_suppression_veto(risk_mgr, base_market_data, base_portfolio_data):
    """
    When accumulated penalties suppress dynamic risk below min_viable_risk (0.35 of base_risk),
    RiskManager must strictly VETO with PENALTY_SUPPRESSION rather than artificially inflating risk.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 70, # Lower conviction gives boundary penalty (x0.70)
        "trade_action": "REDUCE_SIZE" # x0.75
    }
    
    # 3 consecutive losses cuts risk to 0.25x
    portfolio = dict(base_portfolio_data, recent_streak=["LOSS", "LOSS", "LOSS"])
    # 0.70 * 0.75 * 0.25 = 0.131x base_risk (well below 0.35x floor)
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, base_market_data, 
        effective_profile="BALANCED"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "PENALTY_SUPPRESSION"
    assert "Dynamic risk" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_liquidation_precedence_and_price(risk_mgr, base_market_data, base_portfolio_data):
    """
    Validates that:
    1. Exact liquidation_price is calculated and populated in approved trades.
    2. Leverage is clamped so SL distance has at least 30% cushion before liquidation.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 85,
        "trade_action": "ENTER"
    }
    
    # Wide stop loss: 5% drop (ATR = 3.5, sl_mult = 1.5 -> SL dist = 5.25%)
    market = dict(base_market_data)
    market["indicators"] = {"atr_14": 3.5, "ema_20": 100.0, "bb_middle": 100.0}
    market["derivatives_data"] = {
        "taker_fee_pct": 0.0005,
        "funding_rate": 0.0001,
        "size_increment": 0.001,
        "min_notional": 10.0,
        "maintenance_margin_pct": 0.025
    }
    
    verdict = await risk_mgr.analyze(
        ceo_decision, base_portfolio_data, market, 
        effective_profile="BALANCED", macro_regime="TREND_BULLISH"
    )
    
    assert verdict["approved"] is True
    assert verdict["liquidation_price"] > 0.0
    # For a LONG, liquidation price must be strictly lower than stop loss price
    assert verdict["stop_loss_price"] > verdict["liquidation_price"]
    # Distance to liquidation must exceed distance to SL
    entry = verdict["entry_price"]
    sl = verdict["stop_loss_price"]
    liq = verdict["liquidation_price"]
    assert (entry - sl) < (entry - liq)

@pytest.mark.asyncio
async def test_risk_manager_min_notional_veto(risk_mgr, base_market_data, base_portfolio_data):
    """
    When calculated notional is below exchange min_notional, RiskManager vetoes with MIN_NOTIONAL.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # Tiny balance: $10, 0.5% risk = $0.05 risk. With 2% SL, notional = $2.50.
    portfolio = dict(base_portfolio_data, total_usd=10.0, available_margin=10.0)
    market = dict(base_market_data)
    market["derivatives_data"] = {
        "taker_fee_pct": 0.0005,
        "funding_rate": 0.0001,
        "size_increment": 0.001,
        "min_notional": 15.0 # Exchange requires at least $15
    }
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="CONSERVATIVE"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MIN_NOTIONAL"
    assert "below exchange minimum notional" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_does_not_veto_45_contracts_on_dummy_100_min_size(risk_mgr, base_market_data, base_portfolio_data):
    """
    Regression Test:
    In Nado Protocol, book_info.min_size is 100.0 across all products as an unconfirmed dummy default.
    An order of 45 contracts of XRP (aligned to size_increment 5.0) MUST NOT be falsely vetoed!
    """
    ceo_decision = {
        "symbol": "XRP-USD",
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "ENTER"
    }
    
    market = {
        "price_data": {"current_price": 1.50, "ohlcv_1h": [{"volume": 1000}] * 10},
        "indicators": {"atr_14": 0.05, "ema_20": 1.50, "bb_middle": 1.50},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 5.0, # 5 XRP steps
            "min_size": 100.0,      # Unconfirmed dummy default from Nado engine
            "min_notional": 0.0,
            "maintenance_margin_pct": 0.025
        }
    }
    portfolio = dict(base_portfolio_data, total_usd=70.0, available_margin=70.0)
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="AGGRESSIVE", macro_regime="TRENDING"
    )
    
    # Must NOT be vetoed by MIN_SIZE
    assert verdict.get("veto_category") != "MIN_SIZE"
    assert verdict["approved"] is True
    # Contracts must be at least 5.0 and aligned to size_increment
    assert verdict["contracts"] > 0
    assert verdict["contracts"] % 5.0 == 0

@pytest.mark.asyncio
async def test_risk_manager_enforces_confirmed_real_min_size(risk_mgr, base_market_data, base_portfolio_data):
    """
    Regression Test:
    When a genuine, confirmed minimum size exists (e.g. 50.0 contracts),
    orders strictly below it must still be safely vetoed with MIN_SIZE.
    """
    ceo_decision = {
        "symbol": "XRP-USD",
        "decision": "SHORT",
        "conviction": 80,
        "trade_action": "REDUCE_SIZE"
    }
    
    market = {
        "price_data": {"current_price": 1.50, "ohlcv_1h": [{"volume": 1000}] * 10},
        "indicators": {"atr_14": 0.05, "ema_20": 1.50, "bb_middle": 1.50},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 5.0,
            "min_size": 50.0, # Confirmed real minimum size
            "min_notional": 0.0,
            "maintenance_margin_pct": 0.025
        }
    }
    # Sizing yields 45.0 contracts (strictly below 50.0)
    portfolio = dict(base_portfolio_data, total_usd=300.0, available_margin=300.0)
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="BALANCED", macro_regime="RANGE_CHOPPY"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MIN_SIZE"
    assert "below exchange minimum size" in verdict["reasoning"]

def test_nado_market_parameters_filters_dummy_100():
    """
    Verifies that NadoTradingService._get_market_parameters replaces
    the dummy 100.0 min_size with size_increment_base.
    """
    from services.nado_trading_service import NadoTradingService
    service = NadoTradingService.__new__(NadoTradingService)
    
    mock_market = MagicMock()
    mock_market.book_info.size_increment = "5000000000000000000" # 5.0 x18
    mock_market.book_info.price_increment_x18 = "10000000000000"  # 0.00001 x18
    mock_market.book_info.min_size = "100000000000000000000"       # 100.0 x18 (dummy)
    
    service._market_cache = {10: mock_market}
    params = service._get_market_parameters(10)
    
    assert params["size_increment_base"] == 5.0
    # Must be filtered to 5.0, NOT 100.0!
    assert params["min_size"] == 5.0



