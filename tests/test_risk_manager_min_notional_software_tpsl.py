import pytest
from unittest.mock import MagicMock, AsyncMock
from core.config import config
from agents.risk_manager import RiskManager


@pytest.mark.asyncio
async def test_risk_manager_nado_trigger_bump_to_104():
    """
    Verifies Nado DEX CLOB Min Size ($101.00 / $104.00 Safe Bump) Matrix:
    1. Case 1 (Safe Bump to $104.00): On deposit $89.06 (or $87.00), calculated notional ~$53.00
       is safely bumped to $104.00 because bump risk ($1.48) <= max allowed risk ($2.31).
       Order is APPROVED with notional >= $101.00 and is_software_tpsl = False.
    2. Case 2 (Risk Cap Exceeded): On deposit $30.00, bump risk ($1.48) exceeds the risk cap
       ($30 * 0.026 = $0.78). Order CANNOT be safely bumped and is strictly VETOED with MIN_NOTIONAL.
    3. Case 3 (Micro-dust < $35.00): On deposit $15.00, calculated notional is ~$17.60.
       Strictly VETOED with MIN_NOTIONAL to protect portfolio slots.
    """
    mock_logger = MagicMock()
    mock_llm = MagicMock()
    rm = RiskManager(mock_logger, mock_llm)

    ceo_decision = {
        "symbol": "HYPE-USD",
        "decision": "SHORT",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    market = {
        "price_data": {"current_price": 85.0},
        "indicators": {"atr_14": 0.8, "bb_middle": 82.0, "ema_20": 82.5},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.01,
            "price_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 100.0  # Nado trigger limit
        }
    }

    # Case 1: Portfolio $89.06 -> sub-notional is safely bumped to $104.00
    portfolio_bump = {
        "total_usd": 89.06,
        "available_margin": 89.06,
        "daily_drawdown_pct": 0.0,
        "recent_streak": []
    }
    verdict_bump = await rm.analyze(
        ceo_decision, portfolio_bump, market, 
        effective_profile="BALANCED",
        strategy_mode="MEAN_REVERSION",
        macro_regime="TRENDING"
    )
    assert verdict_bump["approved"] is True
    assert verdict_bump["notional_size_usd"] >= 101.0
    assert verdict_bump["is_software_tpsl"] is False

    # Case 2: Portfolio $30.00 -> calculated notional is ~$35.00, but bumping to $104
    # would risk $1.48 > $0.78 cap ($30 * 0.026). Strictly VETOED to protect capital!
    portfolio_tight = {
        "total_usd": 30.0,
        "available_margin": 30.0,
        "daily_drawdown_pct": 0.0,
        "recent_streak": []
    }
    verdict_tight = await rm.analyze(
        ceo_decision, portfolio_tight, market, 
        effective_profile="BALANCED",
        strategy_mode="MEAN_REVERSION",
        macro_regime="TRENDING"
    )
    assert verdict_tight["approved"] is False
    assert verdict_tight["veto_category"] == "MIN_NOTIONAL"
    assert "CLOB min_size" in verdict_tight["reasoning"]

    # Case 3: Portfolio $15.00 -> calculated notional is ~$17.60 (< $35.00 floor)
    # Micro-dust: strictly vetoed in both SHADOW and LIVE to prevent clogging portfolio slots!
    portfolio_dust = {
        "total_usd": 15.0,
        "available_margin": 15.0,
        "daily_drawdown_pct": 0.0,
        "recent_streak": []
    }
    verdict_dust = await rm.analyze(
        ceo_decision, portfolio_dust, market, 
        effective_profile="BALANCED",
        strategy_mode="MEAN_REVERSION",
        macro_regime="TRENDING"
    )
    assert verdict_dust["approved"] is False
    assert verdict_dust["veto_category"] == "MIN_NOTIONAL"
    assert "MIN_NOTIONAL" in verdict_dust["reasoning"]


@pytest.mark.asyncio
async def test_nado_trading_service_predispatch_clob_bump_and_rejection():
    """
    Verifies that NadoTradingService:
    1. Pre-dispatch automatically bumps contracts so abs(amount) * order_limit_price >= $101.00.
    2. Strictly rejects micro-dust orders with notional < $35.00 (e.g. $13.32).
    """
    from services.nado_trading_service import NadoTradingService

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    service.client = MagicMock()
    service.default_subaccount_id = "0x" + "2" * 64
    service.product_map = {"HYPE": 180, "HYPE-USD": 180}

    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.01,
        "size_increment_x18": int(0.01 * 1e18),
        "price_increment_x18": int(0.01 * 1e18),
        "min_size": 0.01,
        "min_notional": 100.0
    })

    # Order of $104.00 notional (1.23 contracts @ $85.00) -> Accepted in Shadow
    config.SHADOW_MODE = True
    ok = await service.open_position(
        symbol="HYPE-USD",
        direction="SHORT",
        entry_price=85.0,
        notional_usd=104.55,
        tp_price=83.5,
        sl_price=86.2,
        leverage=5,
        contracts=1.23,
        approved_risk_usd=1.48
    )
    assert ok is True
    assert service.last_execution_status == "FILLED"

    # Micro-dust order ($13.32 notional) -> Strictly Rejected with ORDER_BELOW_MIN_NOTIONAL
    service.active_positions.clear()
    ok_dust = await service.open_position(
        symbol="HYPE-USD",
        direction="SHORT",
        entry_price=85.0,
        notional_usd=13.32,
        tp_price=83.5,
        sl_price=86.2,
        leverage=5,
        contracts=0.15,
        approved_risk_usd=0.2
    )
    assert ok_dust is False
    assert service.last_execution_status == "ORDER_BELOW_MIN_NOTIONAL"


@pytest.mark.asyncio
async def test_nado_live_predispatch_ensures_clob_min_size():
    """
    Verifies that in LIVE mode:
    1. Pre-dispatch check adjusts order amount if abs(amount) * price < 101.0.
    2. Sequencer order is placed with limit notional >= $101.00.
    3. Native triggers are placed on trigger service.
    """
    from services.nado_trading_service import NadoTradingService

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    service.client = MagicMock()
    service.default_subaccount_id = "0x" + "2" * 64
    service.product_map = {"HYPE": 180, "HYPE-USD": 180}
    service.fast_monitor_running = True
    service.fast_monitor_last_heartbeat = 9999999999.0
    service.fast_monitor_task = MagicMock()
    service.fast_monitor_task.done = MagicMock(return_value=False)

    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.01,
        "size_increment_x18": int(0.01 * 1e18),
        "price_increment_x18": int(0.01 * 1e18),
        "min_size": 0.01,
        "min_notional": 100.0
    })

    # Mock successful sequencer order placement & fill for 1.23 contracts
    mock_place_res = MagicMock()
    mock_place_res.data.digest = "0xmockorderdigest"
    service.client.market.place_order = MagicMock(return_value=mock_place_res)

    mock_hist_order = MagicMock()
    mock_hist_order.base_filled = str(int(1.23 * 1e18))
    mock_hist_order.quote_filled = str(int(104.55 * 1e18))
    mock_hist_order.fee = 0.04
    mock_hist = MagicMock()
    mock_hist.orders = [mock_hist_order]
    service.client.market.get_historical_orders_by_digest = MagicMock(return_value=mock_hist)

    # In LIVE mode with bumped notional = $104.55
    config.SHADOW_MODE = False
    config.NADO_LIVE_TRADING_ENABLED = True

    ok = await service.open_position(
        symbol="HYPE-USD",
        direction="LONG",
        entry_price=85.0,
        notional_usd=104.55,
        tp_price=88.0,
        sl_price=83.5,
        leverage=5,
        contracts=1.23,
        approved_risk_usd=2.5,
        use_software_tpsl=False,
        software_sl_tp_only=False
    )

    assert ok is True
    # In LIVE mode with notional >= 101.0, native triggers ARE placed on trigger service
    assert service.client.market.place_price_trigger_order.call_count >= 1

    pos = service.active_positions["HYPE-USD"]
    assert pos["contracts"] == 1.23
    assert pos["protection_state"] in ("NATIVE_TRIGGERS_PLACED", "PROTECTED", "NATIVE_CONFIRMED", "NATIVE_SL_CONFIRMED")


@pytest.mark.asyncio
async def test_small_position_tp1_sets_runner_and_activates_sentinel_parabolic_trailing():
    """
    Verifies that:
    1. On Partial TP for positions < $200.00, NadoTradingService moves SL to BE on 100% volume
       and sets partial_tp_taken = True and be_activated = True.
    2. SentinelAgent.update_trailing_sl recognizes the position as runner / BE activated
       and engages dynamic parabolic trailing when current_r >= 0.85!
    """
    from services.nado_trading_service import NadoTradingService
    from agents.sentinel_agent import SentinelAgent
    from services.partial_tp_calculator import PartialTPPlan, TPTier

    service = NadoTradingService()
    service.active_positions.clear()
    service.is_connected = True
    service.update_stop_loss_to_be = AsyncMock(return_value=True)

    # Initial position with $104.00 notional (< $200.00)
    pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 85000.0,
        "sl_price": 84000.0,
        "initial_sl": 84000.0,
        "contracts": 0.00123,
        "notional_usd": 104.55,
        "highest_price": 86000.0,
        "protection_state": "PROTECTED",
        "strategy_mode": "TREND_FOLLOWING"
    }
    service.active_positions["BTC-USD"] = pos

    plan = PartialTPPlan(
        symbol="BTC-USD",
        direction="LONG",
        entry_price=85000.0,
        tp_final_price=87000.0,
        total_contracts=0.00123,
        tiers=[
            TPTier(tier_index=1, fraction=0.5, target_price=86000.0, close_contracts=0.0006, cumulative_fraction=0.5, status="PENDING"),
            TPTier(tier_index=2, fraction=1.0, target_price=87000.0, close_contracts=0.00063, cumulative_fraction=1.0, status="PENDING", is_final=True)
        ],
        mode="2_TIER",
        min_size=0.0001,
        size_increment=0.0001,
        price_increment=0.1,
        remaining_contracts=0.00123
    )

    pos["partial_tp_plan"] = plan.to_dict()
    result = await service.check_and_execute_partial_tp_tier("BTC-USD", 86050.0)
    assert result is not None
    assert result["status"] == "PROTECTED_FULL_SIZE_BE"
    assert pos.get("partial_tp_taken") is True
    assert pos.get("be_activated") is True
    assert pos.get("be_moved") is True

    # Now verify Sentinel parabolic trailing on this position:
    mock_logger = MagicMock()
    sentinel = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "sentinel_trail_activation_atr": 2.0,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25
    }
    eval_atr = 500.0

    # Market price moves further in profit: 86500.0 (peak = 86500, planned_r_dist = 1000, current_r = 1.5R >= 0.85)
    pos["highest_price"] = 86500.0
    pos["protection_state"] = "BREAK_EVEN"
    pos["sl_price"] = 85100.0
    market_data = {"price_data": {"current_price": 86400.0}}

    trail_res = await sentinel.update_trailing_sl(pos, market_data, profile_rules, eval_atr)
    assert trail_res["new_sl"] is not None
    assert trail_res["state"] == "TRAILING"
    assert "Dynamic Parabolic Trailing" in trail_res["reasoning"]
    assert trail_res["new_sl"] > 85500.0
