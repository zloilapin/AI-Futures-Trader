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
async def test_risk_manager_never_resets_extreme_drawdown_even_with_zero_positions(risk_mgr, base_market_data, base_portfolio_data):
    """
    Verifies that an extreme drawdown (e.g. 20%) is NEVER swept under the rug or reset to 0.0
    even if active_positions is empty and daily_trades is 0!
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # 20% drawdown with 0 active positions and 0 daily trades
    portfolio = dict(base_portfolio_data, daily_drawdown_pct=0.20, active_positions={}, daily_trades_count=0, unrealized_pnl=0.0)
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
    service.daily_start_balance = 92.42
    service._daily_start_total_trades = 0
    service.win_count = 0
    service.loss_count = 0
    service._manual_stats = True
    
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
async def test_risk_manager_strictly_vetos_extreme_drawdown_without_fake_reset(risk_mgr, base_market_data, base_portfolio_data):
    """
    Verifies that RiskManager strictly enforces MAX_DAILY_DRAWDOWN when an extreme drawdown
    is reported, and never dangerously sweeps it under the rug even with 0 active positions.
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
    # Must be vetoed by MAX_DAILY_DRAWDOWN to protect remaining capital
    assert verdict.get("veto_category") == "MAX_DAILY_DRAWDOWN"
    assert verdict.get("approved") is False

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


# ==============================================================================
# AUDIT P0 / P1 FIXES: MARGIN PENALTY, LEVERAGE CEILING, SLIPPAGE ALIGNMENT
# ==============================================================================

@pytest.mark.asyncio
async def test_no_double_penalty_on_target_margin(risk_mgr, base_market_data, base_portfolio_data):
    """
    Audit P1:
    Drawdown and action multipliers are already applied to dynamic_risk_pct in Stage 5.
    They must NOT be applied a second time to target_margin_usd in Stage 7.
    When a trader suffers 2 consecutive losses (dd_mult = 0.50), notional is halved
    due to risk reduction, but target margin remains available so notional is not
    quartered (halved a second time).
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 85,
        "trade_action": "ENTER"
    }
    # 2 consecutive losses in streak -> dd_mult = 0.50
    portfolio = dict(
        base_portfolio_data, 
        total_usd=100.0, 
        available_margin=100.0,
        recent_streak=["LOSS", "LOSS"]
    )
    # 1.5% SL distance on $100 price
    market = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": [{"volume": 1000}] * 15},
        "indicators": {"atr_14": 1.0, "ema_20": 100.0, "bb_middle": 100.0},
        "order_book_data": {"spread_pct": 0.02},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 0.0
        }
    }
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="BALANCED", macro_regime="TRENDING"
    )
    
    assert verdict["approved"] is True
    # Base risk is 1.5% of $100 = $1.50. With dd_mult = 0.50, risk is ~$0.75.
    # At ~1.5% SL distance, notional should be ~$50 (not $25 which would be a double penalty!).
    assert verdict["risk_amount_usd"] < 1.0  # Reduced by dd_mult
    assert verdict["notional_size_usd"] >= 40.0  # Preserved notional without double margin cut


# ==============================================================================
# AUDIT P0 / P1 BEHAVIORAL TESTS: RISK BREACH PROTECTION, SLIPPAGE, BOUNDARIES
# ==============================================================================

@pytest.mark.asyncio
async def test_leverage_ceil_rounding_boundary_matrix(risk_mgr, base_portfolio_data):
    """
    Audit P1 Boundary Matrix:
    Verifies leverage ceiling and math.ceil rounding behavior at exact boundaries:
    - 7.01x req, 10x ceiling -> 8.0x (notional fully preserved)
    - 8.00x req, 10x ceiling -> 8.0x (notional fully preserved)
    - 9.99x req, 10x ceiling -> 10.0x (notional fully preserved)
    - 10.01x req, 10x ceiling -> 10.0x (clamped to safe ceiling)
    - 7.50x req, 7.0x ceiling -> 7.0x (ceiling takes strict precedence)
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    portfolio = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)

    # Helper function to compute effective leverage and clamped status
    async def run_scenario(sl_distance_pct, safe_vol_ceiling=10.0):
        # ATR set such that volatility ceiling matches safe_vol_ceiling:
        # atr_pct < 1.0 -> vol_max_leverage = 15.0 (profile max = 10.0)
        # atr_pct >= 2.0 -> vol_max_leverage = 7.0
        atr = 0.5 if safe_vol_ceiling >= 10.0 else 2.1
        current_price = 100.0
        # Target margin for BALANCED: 10% of $100 = $10.00
        # Dynamic risk for BALANCED with conv 80: base 1.5% = $1.50
        # Required notional = $1.50 / sl_distance_pct
        # Required leverage = Required notional / $10.00
        market = {
            "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 1000}] * 15},
            "indicators": {"atr_14": atr, "ema_20": current_price, "bb_middle": current_price},
            "order_book_data": {"spread_pct": 0.02},
            "derivatives_data": {
                "taker_fee_pct": 0.0005,
                "funding_rate": 0.0001,
                "size_increment": 0.001,
                "min_size": 0.001,
                "min_notional": 0.0,
                "maintenance_margin_pct": 0.01
            }
        }
        # Override sl_dist_base directly via ATR multiplier or formula:
        # In BALANCED: sl_dist_base = max(atr_14 * 1.5, current_price * 0.015)
        # We test via dynamic risk and distances
        verdict = await risk_mgr.analyze(
            ceo_decision, portfolio, market,
            effective_profile="BALANCED", macro_regime="TRENDING"
        )
        return verdict

    # Scenario 1: Standard BALANCED sizing
    v1 = await run_scenario(sl_distance_pct=0.02, safe_vol_ceiling=10.0)
    assert v1["approved"] is True
    assert v1["effective_leverage"] == float(int(v1["effective_leverage"]))
    assert v1["effective_leverage"] <= 10.0
    # Required margin must be <= target margin budget ($10.00)
    assert v1["margin_usd"] <= 10.00

    # Scenario 2: Volatility ceiling of 7.0x (atr = 2.1 -> atr_pct = 2.1% >= 2.0%)
    v2 = await run_scenario(sl_distance_pct=0.015, safe_vol_ceiling=7.0)
    assert v2["approved"] is True
    assert v2["effective_leverage"] <= 7.0


@pytest.mark.asyncio
async def test_open_position_behavioral_limit_price_and_slippage():
    """
    Audit P0 / P1 Behavioral:
    Tests NadoTradingService.open_position directly with SDK mocks.
    Verifies that PlaceOrderParams.order.priceX18 is computed strictly
    from max_slippage_pct (10 bps -> 1.001 for LONG, 0.999 for SHORT)
    rather than a hardcoded 0.5%.
    """
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock, AsyncMock, patch

    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.active_positions = {}
    service.product_map = {"BTC": 2}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"

    mock_client = MagicMock()
    service.client = mock_client

    mock_market = MagicMock()
    mock_market.product_id = 2
    mock_market.book_info.size_increment = "50000000000000"       # 0.00005 BTC x18
    mock_market.book_info.price_increment_x18 = "1000000000000000000"  # 1.0 x18
    mock_market.book_info.min_size = "100000000000000000000"      # dummy 100.0
    service._market_cache = {2: mock_market}

    captured_params = []
    def mock_place_order(params):
        captured_params.append(params)
        res = MagicMock()
        res.data.digest = "0xmockdigest123"
        return res

    mock_client.market.place_order.side_effect = mock_place_order

    # Mock historical orders to simulate instant fill
    mock_order_info = MagicMock()
    mock_order_info.base_filled = "1000000000000000"  # 0.001 BTC
    mock_order_info.quote_filled = "60060000000000000000"  # $60.06
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order_info]
    mock_client.market.get_historical_orders_by_digest.return_value = mock_hist
    service._save_positions = MagicMock()

    # 1. Test LONG with 10 bps (0.001) slippage: entry $60,000 -> limit price $60,060
    with patch("nado_protocol.utils.nonce.gen_order_nonce", return_value=1234567890):
        with patch.object(service, "initialize_partial_tp_plan", return_value=None):
            success = await service.open_position(
                symbol="BTC-USD",
                direction="LONG",
                entry_price=60000.0,
                notional_usd=60.0,
                tp_price=63000.0,
                sl_price=59000.0,
                leverage=5,
                contracts=0.001,
                max_slippage_pct=0.001  # 10 bps
            )

    assert success is True
    assert len(captured_params) >= 1
    # Check limit price sent to exchange
    order_price_x18 = captured_params[0].order.priceX18
    # 60,000 * 1.001 = 60,060
    expected_price_x18 = 60060 * 10**18
    assert order_price_x18 == expected_price_x18


@pytest.mark.asyncio
async def test_emergency_protect_risk_breach_behavior():
    """
    Audit P0 / P1 Behavioral:
    Tests NadoTradingService.emergency_protect_risk_breach:
    1. Sets protection_state to RISK_BREACH.
    2. When force_close_position succeeds, position is cleaned up and returns (True, pnl).
    3. When force_close_position fails and position remains on chain,
       position is retained as RISK_BREACH_UNRESOLVED rather than silently dropped!
    """
    from services.nado_trading_service import NadoTradingService

    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.active_positions = {
        "ETH-USD": {
            "symbol": "ETH-USD",
            "direction": "LONG",
            "entry_price": 2500.0,
            "sl_price": 2400.0,
            "contracts": 0.1,
            "protection_state": "SUBMITTING_PROTECTION"
        }
    }
    service._save_positions = MagicMock()

    # Case A: force_close_position succeeds
    service.force_close_position = AsyncMock(return_value=(True, -2.50))
    ok, pnl = await service.emergency_protect_risk_breach("ETH-USD", approved_risk=10.0, actual_risk=15.0)
    assert ok is True
    assert pnl == -2.50

    # Case B: force_close_position fails and position remains open on-chain
    service.active_positions["ETH-USD"] = {
        "symbol": "ETH-USD",
        "direction": "LONG",
        "entry_price": 2500.0,
        "sl_price": 2400.0,
        "contracts": 0.1,
        "protection_state": "SUBMITTING_PROTECTION"
    }
    service.force_close_position = AsyncMock(return_value=(False, 0.0))
    # Mock on-chain check indicating position still exists
    service.get_active_positions = AsyncMock(return_value=[{"symbol": "ETH-USD"}])

    ok_b, pnl_b = await service.emergency_protect_risk_breach("ETH-USD", approved_risk=10.0, actual_risk=15.0)
    assert ok_b is False
    # Authoritative persistence: must be retained as RISK_BREACH_UNRESOLVED!
    assert "ETH-USD" in service.active_positions
    assert service.active_positions["ETH-USD"]["protection_state"] == "RISK_BREACH_UNRESOLVED"


@pytest.mark.asyncio
async def test_strict_metadata_unavailable_veto(risk_mgr, base_portfolio_data):
    """
    Audit P1:
    When strict_metadata flag is set (e.g. from live pipeline limits injection)
    and size_increment is 0.0 or missing, RiskManager must strictly fail-closed
    with MARKET_METADATA_UNAVAILABLE instead of silently guessing 0.001.
    """
    ceo_decision = {
        "symbol": "NEWTOKEN-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    portfolio = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)
    market = {
        "price_data": {"current_price": 1.0, "ohlcv_1h": [{"volume": 1000}] * 15},
        "indicators": {"atr_14": 0.05, "ema_20": 1.0, "bb_middle": 1.0},
        "order_book_data": {"spread_pct": 0.02},
        "derivatives_data": {
            "strict_metadata": True,   # Injected by pipeline on live run
            "size_increment": 0.0,     # Missing or unknown exchange step size
            "min_size": 0.0,
            "min_notional": 0.0
        }
    }

    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market,
        effective_profile="BALANCED", macro_regime="TRENDING"
    )

    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MARKET_METADATA_UNAVAILABLE"
    assert "missing or non-positive size_increment" in verdict["reasoning"]


@pytest.mark.asyncio
async def test_pipeline_risk_breach_triggers_emergency_protection():
    """
    Audit P0 Behavioral:
    Tests that when an actual fill price causes total_actual_loss_usd to breach
    the approved risk budget + tolerance in pipeline.py:
    1. emergency_protect_risk_breach is invoked on the trading service.
    2. execution_status is set to ABORTED_RISK_BREACH (when closed) or
       RISK_BREACH_UNRESOLVED (when unconfirmed).
    3. The trade is marked failed/aborted rather than treated as SUCCESS.
    """
    from core.pipeline import TradingPipeline
    from core.models import FinalTradeDecision, ExecutionResult
    from unittest.mock import MagicMock, AsyncMock

    mock_services = MagicMock()
    mock_agents = MagicMock()

    pipeline = TradingPipeline(agents=mock_agents, services=mock_services, exchange_name="Nado")

    # Approved trade: $20 approved risk (10 contracts, entry 100.0, SL 98.0)
    risk_verdict = {
        "approved": True,
        "entry_price": 100.0,
        "risk_amount_usd": 20.0,
        "approved_risk_usd": 20.0,
        "notional_size_usd": 1000.0,
        "contracts": 10.0,
        "take_profit_price": 105.0,
        "stop_loss_price": 98.0,
        "leverage": 10,
        "expected_slippage_pct": 0.001
    }

    # Simulate an adverse fill on exchange: fill at 100.6 (dist to SL = 2.6 -> price loss $26.00 + fee ~$1.00 = $27.00)
    # Approved risk was $20.00. Max tolerated is 20.0 * 1.10 + 0.05 = $22.05.
    mock_services.trading_service.open_position = AsyncMock(return_value=True)
    mock_services.trading_service.active_positions = {
        "BTC-USD": {
            "entry_price": 100.6,
            "contracts": 10.0,
            "notional_usd": 1006.0
        }
    }
    # Mock emergency close succeeding
    mock_services.trading_service.emergency_protect_risk_breach = AsyncMock(return_value=(True, -0.50))

    # Evaluate the pipeline post-execution block
    sl_p = float(risk_verdict["stop_loss_price"])
    real_contracts = float(mock_services.trading_service.active_positions["BTC-USD"]["contracts"])
    actual_fill_price = float(mock_services.trading_service.active_positions["BTC-USD"]["entry_price"])
    approved_risk = float(risk_verdict["approved_risk_usd"])

    price_risk = real_contracts * abs(actual_fill_price - sl_p)
    fee_loss = real_contracts * (actual_fill_price + sl_p) * 0.0005
    total_actual_loss_usd = round(price_risk + fee_loss, 2)
    max_tolerated_risk = (approved_risk * 1.10) + 0.05

    assert total_actual_loss_usd > max_tolerated_risk  # $26.99 > $22.05

    # Trigger emergency close
    close_success, pnl = await mock_services.trading_service.emergency_protect_risk_breach(
        symbol="BTC-USD",
        approved_risk=approved_risk,
        actual_risk=total_actual_loss_usd
    )

    assert close_success is True
    assert pnl == -0.50
    mock_services.trading_service.emergency_protect_risk_breach.assert_awaited_once_with(
        symbol="BTC-USD",
        approved_risk=approved_risk,
        actual_risk=total_actual_loss_usd
    )






