# tests/test_audit_alpha_levers_packet7.py
import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService
from agents.sentinel_agent import SentinelAgent
from agents.risk_manager import RiskManager
from services.partial_tp_calculator import PartialTPPlan, TPTier
from nado_protocol.utils.expiration import OrderType


@pytest.mark.asyncio
async def test_lever3_maker_limit_vs_taker_ioc_selection():
    """
    Lever 3: Verify that on quiet trends (TREND_FOLLOWING), NadoTradingService uses
    OrderType.POST_ONLY resting at entry_price, whereas BREAKOUT uses OrderType.IOC with slippage buffer.
    """
    service = NadoTradingService()
    service.is_connected = True
    service.active_positions.clear()
    service.wallet = MagicMock()
    service.wallet.get_address = MagicMock(return_value="0x1234567890123456789012345678901234567890")
    service.product_map = {"BTC": 1}

    mock_book_info = MagicMock()
    mock_book_info.size_increment = str(int(1e15)) # 0.001
    mock_book_info.price_increment_x18 = str(int(1e17)) # 0.1
    mock_book_info.min_size = str(int(1e15))
    mock_perp = MagicMock(product_id=1, book_info=mock_book_info)
    service._market_cache = {1: mock_perp}

    mock_client = MagicMock()
    mock_res = MagicMock()
    mock_res.data.digest = "0xabc123"
    mock_client.market.place_order = MagicMock(return_value=mock_res)

    mock_order_info = MagicMock()
    mock_order_info.base_filled = str(int(0.1 * 1e18)) # 0.1 BTC
    mock_order_info.quote_filled = str(int(7000 * 1e18))
    mock_order_info.fee = "0"
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order_info]
    mock_client.market.get_historical_orders_by_digest = MagicMock(return_value=mock_hist)
    mock_client.market.get_product = MagicMock(return_value=MagicMock(oracle_price=str(int(70000 * 1e18))))

    service.client = mock_client
    service._mock_mode = True

    # Case A: TREND_FOLLOWING (Maker / Post-Only)
    await service.open_position(
        symbol="BTC-USD",
        direction="LONG",
        entry_price=70000.0,
        notional_usd=7000.0,
        tp_price=73000.0,
        sl_price=69000.0,
        leverage=5,
        strategy_mode="TREND_FOLLOWING",
        approved_risk_usd=500.0
    )
    placed_params_tf = mock_client.market.place_order.call_args[0][0]
    tf_price = float(placed_params_tf.order.priceX18) / 1e18
    assert tf_price == 70000.0, f"Expected 70000.0 for maker limit, got {tf_price}"

    # Case B: BREAKOUT (Taker IOC with slippage buffer)
    service.active_positions.clear()
    mock_client.market.place_order.reset_mock()
    await service.open_position(
        symbol="BTC-USD",
        direction="LONG",
        entry_price=70000.0,
        notional_usd=7000.0,
        tp_price=73000.0,
        sl_price=69000.0,
        leverage=5,
        strategy_mode="BREAKOUT",
        max_slippage_pct=0.002,
        approved_risk_usd=500.0
    )
    placed_params_bo = mock_client.market.place_order.call_args[0][0]
    bo_price = float(placed_params_bo.order.priceX18) / 1e18
    assert bo_price >= 70140.0, f"Expected taker IOC price >= 70140.0, got {bo_price}"


@pytest.mark.asyncio
async def test_lever4_dynamic_runner_activation_and_trailing():
    """
    Lever 4: Verify that when Tier 1 (+0.75R) is FILLED and position is risk-free in a strong trend,
    Tier 2 transitions into RUNNER_ACTIVE instead of closing, and Sentinel trails with 1.5x ATR.
    """
    service = NadoTradingService()
    service.is_connected = True
    service.active_positions.clear()
    service.wallet = MagicMock()
    service.wallet.get_address = MagicMock(return_value="0x1234")
    service.product_map = {"BTC": 1}

    tier1 = TPTier(
        tier_index=1,
        target_price=71500.0,
        fraction=0.5,
        close_contracts=0.5,
        cumulative_fraction=0.5,
        action="PROTECT_BREAKEVEN",
        is_final=False,
        status="FILLED"
    )
    tier2 = TPTier(
        tier_index=2,
        target_price=73500.0,
        fraction=0.5,
        close_contracts=0.5,
        cumulative_fraction=1.0,
        action="CLOSE_ALL",
        is_final=True,
        status="PENDING"
    )
    plan = PartialTPPlan(
        symbol="BTC-USD",
        direction="LONG",
        entry_price=70000.0,
        tp_final_price=73500.0,
        total_contracts=1.0,
        remaining_contracts=0.5,
        min_size=0.001,
        size_increment=0.001,
        price_increment=0.1,
        tiers=[tier1, tier2],
        mode="2_TIER"
    )

    pos_data = {
        'symbol': "BTC-USD",
        'direction': "LONG",
        'entry_price': 70000.0,
        'contracts': 0.5,
        'protection_state': "BREAK_EVEN",
        'trend_score': 85.0,
        'strategy_mode': "TREND_FOLLOWING",
        'partial_tp_plan': plan.to_dict(),
        'highest_price': 73600.0,
        'lowest_price': 70000.0,
        'is_closing': False
    }
    service.active_positions["BTC-USD"] = pos_data

    # Price reaches Tier 2 target (73500.0)
    await service.check_and_execute_partial_tp_tier("BTC-USD", current_price=73600.0)

    # Must activate Dynamic Runner!
    updated_pos = service.active_positions["BTC-USD"]
    assert updated_pos.get("is_runner") is True, "Dynamic runner was not activated on Tier 2!"
    updated_plan = PartialTPPlan.from_dict(updated_pos["partial_tp_plan"])
    assert updated_plan.tiers[1].status == "RUNNER_ACTIVE"

    # Now verify SentinelAgent trails the active runner with 1.5x ATR from peak
    sentinel = SentinelAgent(MagicMock())
    market_data = {
        "price_data": {"current_price": 73600.0}
    }
    runner_pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 70000.0,
        "highest_price": 73600.0,
        "sl_price": 70000.0,
        "protection_state": "BREAK_EVEN",
        "is_runner": True,
        "planned_risk_usd": 500.0,
        "contracts": 0.5
    }
    res = await sentinel.analyze(runner_pos, market_data=market_data, profile_rules={}, atr_value=1000.0)
    
    assert res["state"] == "TRAILING"
    # Peak is 73600, 1.5x ATR trail = 73600 - 1500 = 72100.0
    assert res["new_sl"] == 72100.0, f"Expected runner SL trailed to 72100.0, got {res['new_sl']}"
    assert "Dynamic Runner Trailing" in res["reasoning"]


@pytest.mark.asyncio
async def test_lever5_adaptive_sl_clamp_and_structural_anchor():
    """
    Lever 5: Verify RiskManager clamps SL within [0.8%, 2.2%] of current price
    and anchors behind 3-candle structural swing extremes when available.
    """
    logger = MagicMock()
    rm = RiskManager(logger, MagicMock(), default_macro_regime="BULL_TREND")

    current_price = 100000.0
    ceo_long = {
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER",
        "symbol": "BTC-USD",
        "is_actionable": True
    }
    portfolio = {
        "total_usd": 10000.0,
        "available_margin": 10000.0
    }

    # Case A: Low ATR noise floor clamp (ATR = $200 = 0.2% -> clamped to 0.8% floor = $800)
    market_data_low_atr = {
        "price_data": {"current_price": current_price, "candles": []},
        "indicators": {"atr_14": 200.0},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "maintenance_margin_pct": 0.025,
            "size_increment": 0.001,
            "price_increment": 0.1,
            "min_size": 0.001
        }
    }
    res_low = await rm.analyze(
        ceo_decision=ceo_long,
        portfolio_data=portfolio,
        market_data=market_data_low_atr,
        macro_regime="BULL_TREND"
    )
    sl_dist_low = current_price - res_low["stop_loss_price"]
    assert sl_dist_low == 800.0, f"Expected 0.8% floor ($800), got {sl_dist_low}"

    # Case B: High ATR spike ceiling clamp (ATR = $3000 = 3.0% -> clamped to 2.2% ceiling = $2200)
    market_data_high_atr = {
        "price_data": {"current_price": current_price, "candles": []},
        "indicators": {"atr_14": 3000.0},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "maintenance_margin_pct": 0.025,
            "size_increment": 0.001,
            "price_increment": 0.1,
            "min_size": 0.001
        }
    }
    res_high = await rm.analyze(
        ceo_decision=ceo_long,
        portfolio_data=portfolio,
        market_data=market_data_high_atr,
        macro_regime="BULL_TREND"
    )
    sl_dist_high = current_price - res_high["stop_loss_price"]
    assert sl_dist_high == 2200.0, f"Expected 2.2% ceiling ($2200), got {sl_dist_high}"

    # Case C: Structural Swing Anchor (recent 3-candle low at $98,800, 1.2% away -> anchor at $98,700)
    market_data_swing = {
        "price_data": {
            "current_price": current_price,
            "candles": [
                {"open": 99500, "high": 100200, "low": 99200, "close": 99800},
                {"open": 99800, "high": 100100, "low": 98800, "close": 99900}, # swing low = 98800
                {"open": 99900, "high": 100500, "low": 99500, "close": 100000}
            ]
        },
        "indicators": {"atr_14": 1000.0},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "maintenance_margin_pct": 0.025,
            "size_increment": 0.001,
            "price_increment": 0.1,
            "min_size": 0.001
        }
    }
    res_swing = await rm.analyze(
        ceo_decision=ceo_long,
        portfolio_data=portfolio,
        market_data=market_data_swing,
        macro_regime="BULL_TREND"
    )
    # Swing low = 98800 - 0.10 * 1000 = 98700 (1.3% dist, within [0.8%, 2.2%])
    assert res_swing["stop_loss_price"] == 98700.0, f"Expected structural SL at 98700.0, got {res_swing['stop_loss_price']}"
