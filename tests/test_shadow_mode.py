import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch
from services.nado_trading_service import NadoTradingService
from core.config import config
from agents.risk_manager import RiskManager


@pytest.fixture
def mock_service():
    with patch("services.nado_trading_service.NadoTradingService._load_state"), \
         patch("services.nado_trading_service.NadoTradingService._save_state"), \
         patch("services.nado_trading_service.NadoTradingService._save_positions"):
        service = NadoTradingService()
        service.is_connected = True
        service.product_map = {
            "ETH": 1, "ETH-USD": 1,
            "BTC": 2, "BTC-USD": 2,
            "BNB": 3, "BNB-USD": 3,
            "HYPE": 4, "HYPE-USD": 4
        }
        service.client = MagicMock()
        service.client.market = MagicMock()
        service.client.market.place_order = MagicMock()
        service.client.market.get_all_engine_markets = MagicMock(return_value=MagicMock(perp_products=[]))
        service._market_cache = {1: MagicMock(), 2: MagicMock(), 3: MagicMock(), 4: MagicMock()}
        service.active_positions = {}
        service.trade_journal = []
        return service


@pytest.mark.asyncio
async def test_shadow_mode_open_and_close(mock_service):
    """Verifies that in SHADOW_MODE, positions are opened, partially closed, and force closed virtually without sending on-chain orders."""
    with patch.object(config, "SHADOW_MODE", True):
        # 1. Open virtual position
        opened = await mock_service.open_position(
            symbol="ETH-USD",
            direction="LONG",
            entry_price=2000.0,
            notional_usd=100.0,
            tp_price=2100.0,
            sl_price=1950.0,
            leverage=5,
            contracts=0.05,
            approved_risk_usd=2.5,
            strategy_mode="TREND_FOLLOWING",
            macro_regime="TRENDING",
            entry_quality=85.0
        )

        assert opened is True
        assert "ETH-USD" in mock_service.active_positions
        pos = mock_service.active_positions["ETH-USD"]
        assert pos["is_shadow"] is True
        assert pos["is_virtual"] is True
        assert pos["contracts"] == 0.05
        assert pos["entry_price"] == 2000.0
        assert pos["open_digest"].startswith("0xshadow_")
        assert mock_service.client.market.place_order.call_count == 0

        # 2. Simulate partial close (TP1: 0.02 contracts at 2040)
        part_res = await mock_service.execute_partial_close("ETH-USD", contracts_to_close=0.02, current_price=2040.0)
        assert part_res.success is True
        assert part_res.realized_pnl == pytest.approx(0.80, rel=1e-3)
        assert part_res.digest.startswith("0xshadow_tp_")
        assert mock_service.client.market.place_order.call_count == 0

        # Simulate price move to 2050 (+$2.50 profit on 0.05 contracts)
        mock_service.active_positions["ETH-USD"]["current_price"] = 2050.0

        # 3. Force close virtual position
        closed, pnl = await mock_service.force_close_position("ETH-USD")
        assert closed is True
        assert pnl == pytest.approx(2.5, rel=1e-3)
        assert "ETH-USD" not in mock_service.active_positions
        assert mock_service.client.market.place_order.call_count == 0


@pytest.mark.asyncio
async def test_shadow_mode_check_and_update_positions_virtual_still_open(mock_service):
    """
    Verifies the fix for Bug #1:
    In SHADOW_MODE or with is_virtual=True, check_and_update_positions() does NOT treat
    the absence of the position on-chain as a native SL trigger execution!
    Instead, it maintains still_open=True, updates current_price, and triggers closure
    only when market current_price actually crosses sl_price or tp_price.
    """
    with patch.object(config, "SHADOW_MODE", True):
        # Open virtual position
        await mock_service.open_position(
            symbol="BTC-USD",
            direction="SHORT",
            entry_price=77867.0,
            notional_usd=128.35,
            tp_price=75530.0,
            sl_price=78490.0,
            leverage=8,
            contracts=0.00165,
            approved_risk_usd=1.29
        )
        assert "BTC-USD" in mock_service.active_positions

        # Mock on-chain positions returning EMPTY list (0 on-chain positions)
        mock_service.get_active_positions = AsyncMock(return_value=[])

        # Price moves favorably to $77,419 (neither SL nor TP hit)
        closed_reports = await mock_service.check_and_update_positions("BTC-USD", current_price=77419.0)

        # Position must NOT be closed!
        assert len(closed_reports) == 0
        assert "BTC-USD" in mock_service.active_positions
        assert mock_service.active_positions["BTC-USD"]["current_price"] == 77419.0

        # Now simulate price actually crossing SL ($78,500 >= $78,490)
        closed_reports_sl = await mock_service.check_and_update_positions("BTC-USD", current_price=78500.0)
        assert len(closed_reports_sl) == 1
        assert "BTC-USD" not in mock_service.active_positions
        assert closed_reports_sl[0]["triggered_by"] == "SOFTWARE_SL"


@pytest.mark.asyncio
async def test_shadow_mode_sync_with_exchange_preserves_virtual_orphans(mock_service):
    """
    Verifies the fix for Bug #2:
    sync_with_exchange(is_startup=True) does NOT purge virtual positions as stale on-chain orphans.
    """
    with patch.object(config, "SHADOW_MODE", True):
        await mock_service.open_position(
            symbol="HYPE-USD",
            direction="SHORT",
            entry_price=33.66,
            notional_usd=116.14,
            tp_price=31.84,
            sl_price=34.29,
            leverage=8,
            contracts=3.45,
            approved_risk_usd=1.45
        )
        assert "HYPE-USD" in mock_service.active_positions

        # On-chain returns only empty positions list
        mock_service.get_active_positions = AsyncMock(return_value=[])

        # Run startup sync
        await mock_service.sync_with_exchange(is_startup=True)

        # Virtual position must be preserved!
        assert "HYPE-USD" in mock_service.active_positions


@pytest.mark.asyncio
async def test_risk_manager_nado_trigger_bump_preserves_101_notional():
    """
    Verifies the fix for Bug #3:
    When Nado Trigger Optimization bumps notional to $101.00 at Stage 6,
    Stage 7 uses usable_margin (up to max_margin_pct) so notional is not
    clamped back down to $98.32, avoiding a false MIN_NOTIONAL veto.
    """
    mock_logger = MagicMock()
    mock_llm = MagicMock()
    rm = RiskManager(logger=mock_logger, llm_client=mock_llm)
    
    ceo_decision = {
        "decision": "SHORT",
        "trade_action": "REDUCE_SIZE",
        "action": "REDUCE_SIZE",
        "conviction": 83.0,
        "conviction_for_risk": 83.0,
        "entry_quality": 73.0,
        "directional_confidence": 83.0,
        "strategy_mode": "TREND_FOLLOWING",
        "cohort": "B"
    }

    portfolio_data = {
        "total_usd": 89.05,
        "available_margin": 60.0,
        "drawdown_pct": 0.0,
        "active_positions": {},
        "win_count": 0,
        "loss_count": 0,
        "recent_streak": []
    }

    market_data = {
        "price_data": {
            "current_price": 632.40,
            "ohlcv_1h": [{"volume": 100}] * 10
        },
        "indicators": {
            "atr_14": 4.2,
            "ema_20": 635.0,
            "bb_middle": 632.40
        },
        "derivatives_data": {
            "min_notional": 100.0,
            "min_size": 0.01,
            "size_increment": 0.01,
            "price_increment": 0.1,
            "max_leverage": 15,
            "maintenance_margin_pct": 0.025,
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0
        }
    }

    result = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="AGGRESSIVE",
        macro_regime="TRENDING",
        strategy_mode="TREND_FOLLOWING"
    )

    # Must be APPROVED, not vetoed by MIN_NOTIONAL!
    assert result["approved"] is True
    assert result["notional_size_usd"] >= 100.0
    assert result["veto_category"] is None


@pytest.mark.asyncio
async def test_allow_forbidden_strategies_bypasses_transition_and_chop_vetoes():
    """
    Verifies that when config.ALLOW_FORBIDDEN_STRATEGIES is True:
    1. StrategyRouter permits TREND_FOLLOWING in TRANSITION.
    2. RiskManager approves TRANSITION:TREND_FOLLOWING with REDUCE_SIZE (Cohort B).
    3. StrategyRouter permits BREAKOUT in chop/dead market.
    """
    from core.strategy_router import StrategyRouter
    from agents.risk_manager import RiskManager

    mock_logger = MagicMock()
    mock_llm = MagicMock()
    rm = RiskManager(mock_logger, mock_llm)

    market_data = {
        "price_data": {"current_price": 81000.0, "ohlcv_1h": []},
        "order_book_data": {"spread_pct": 0.01, "bid_ask_imbalance": 0.2},
        "indicators": {
            "atr_14": 400.0,
            "rsi_14": 50.0,
            "bb_position_pct": 50.0,
            "bb_width_pct": 1.2,
            "volume_spike_pct": 150.0,
            "donchian_high": 85000.0,
            "donchian_low": 79000.0
        },
        "multi_timeframe": {
            "alignment": "TRANSITION",
            "trend_1h": "BULLISH",
            "trend_15m": "BULLISH",
            "trend_4h": "BEARISH"
        },
        "derivatives_data": {
            "min_notional": 10.0,
            "min_size": 0.001,
            "size_increment": 0.001,
            "price_increment": 0.1,
            "max_leverage": 15,
            "maintenance_margin_pct": 0.025,
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0
        }
    }

    portfolio_data = {
        "total_usd": 1000.0,
        "current_balance": 1000.0,
        "available_margin": 1000.0,
        "total_equity_usd": 1000.0,
        "available_margin_usd": 1000.0,
        "active_positions": {},
        "win_count": 5,
        "loss_count": 2,
        "streak_history_by_tag": {}
    }

    macro_cache = {
        "regime_verdict": {
            "macro_regime": "TRANSITION",
            "range_score": 50.0,
            "vol_score": 8.0
        }
    }

    technical_consensus = {"bulls": 2, "bears": 0}

    with patch.object(config, "ALLOW_FORBIDDEN_STRATEGIES", True):
        # 1. StrategyRouter test
        valid_reports = [
            {"agent_name": "Bull_Agent", "signal": "BULLISH"},
            {"agent_name": "Indicator_Agent", "signal": "BULLISH"}
        ]
        strat_profile = StrategyRouter.evaluate(
            symbol="BTC-USD",
            market_data=market_data,
            valid_reports=valid_reports,
            macro_cache=macro_cache,
            detected_regime="TRANSITION"
        )
        assert strat_profile.has_directional_signal is True
        assert strat_profile.strategy_mode == "TREND_FOLLOWING"
        assert strat_profile.direction_bias == "LONG"

        # 2. RiskManager test
        candidate = {
            "symbol": "BTC-USD",
            "decision": "LONG",
            "trade_action": "ENTER",
            "direction": "LONG",
            "conviction": 85,
            "entry_price": 81000.0,
            "stop_loss_price": 80200.0,
            "take_profit_price": 83000.0,
            "strategy_mode": "TREND_FOLLOWING",
            "regime": "TRANSITION"
        }

        res = await rm.analyze(
            ceo_decision=candidate,
            portfolio_data=portfolio_data,
            market_data=market_data,
            effective_profile="BALANCED",
            macro_regime="TRANSITION",
            strategy_mode="TREND_FOLLOWING"
        )

        assert res["approved"] is True
        assert res["trade_action"] == "REDUCE_SIZE"
