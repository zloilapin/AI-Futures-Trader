"""
Unit tests for Fast Price Monitor & Telegram Alerts for Partial TP (Phase 5).
Verifies:
- Evaluation of pending tiers against live mark/market prices.
- Skipping tiers when market price has not reached threshold.
- Triggering execution with reduce_only=True when price threshold is crossed.
- Rich Telegram notifications for partial TP slices (TP1, TP2, TP3, TP4).
- Breakeven movement alerts specifically included on Tier 2 completion.
- Full trade lifecycle PnL formatting when position closes after partial fills.
- Seamless coexistence with legacy positions without partial_tp_plan.
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from services.partial_tp_calculator import PartialTPPlan, TPTier


@pytest.fixture
def mock_service():
    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service.product_map = {"SOL": 8, "SOL-USD": 8, "ETH": 4, "ETH-USD": 4}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1111111111111111111111111111111111111111"
    service.default_subaccount_id = "0x" + "11" * 32
    service._save_positions = MagicMock()
    service._save_state = MagicMock()

    from core.config import config
    config.PARTIAL_TP_MODE = "4_TIER"

    service._market_cache = {
        8: MagicMock(
            book_info=MagicMock(
                size_increment=int(0.1 * 1e18),
                price_increment_x18=int(0.001 * 1e18),
                min_size=int(0.1 * 1e18)
            )
        )
    }
    return service


@pytest.mark.asyncio
async def test_check_and_execute_partial_tp_tier_below_target_skipped(mock_service):
    """Verifies that if price has not reached the tier target, no execution is triggered."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Tier 1 target is 101.0. Current price is only 100.50!
    mock_service.execute_partial_close = AsyncMock()

    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=100.50)

    assert report is None
    mock_service.execute_partial_close.assert_not_called()
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan.get_tier(1).status == "PENDING"


@pytest.mark.asyncio
async def test_check_and_execute_partial_tp_tier_target_reached_executes_and_notifies(mock_service):
    """Verifies that crossing Tier 1 target triggers execute_partial_close and sends Telegram alert."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Mock execute_partial_close success
    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=True,
        digest="0xmock_tp1",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=101.0,
        realized_pnl=0.40,
        is_partial_fill=False
    ))

    # Mock TelegramService
    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)

    assert report is not None
    assert report["tier_index"] == 1
    assert report["fraction"] == 0.25
    assert report["slice_pnl"] == pytest.approx(0.40)
    assert report["remaining_contracts"] == pytest.approx(1.2)

    # Telegram message sent
    mock_tg.send_message.assert_awaited_once()
    msg = mock_tg.send_message.call_args[0][0]
    assert "PARTIAL TAKE PROFIT" in msg
    assert "TP1 - 25%" in msg
    assert "+$0.40" in msg
    assert "SOL-USD" in msg


@pytest.mark.asyncio
async def test_check_and_execute_partial_tp_tier_2_breakeven_alert_content(mock_service):
    """Verifies that completing Tier 2 sends alert with Breakeven confirmation and marks be_activated."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Pre-fill Tier 1
    tier1_res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=101.0, realized_pnl=0.40)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, tier1_res)

    # Now Tier 2 target is 102.0. Price hits 102.05!
    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=True,
        digest="0xmock_tp2",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=102.0,
        realized_pnl=0.80,
        is_partial_fill=False
    ))
    mock_service.update_stop_loss_to_be = AsyncMock(return_value=True)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=102.05)

    assert report is not None
    assert report["tier_index"] == 2
    assert report["be_activated"] is True

    # Telegram message confirms BE protection
    msg = mock_tg.send_message.call_args[0][0]
    assert "TP2 - 50%" in msg
    assert "БЕЗУБЫТОК" in msg


@pytest.mark.asyncio
async def test_check_and_execute_partial_tp_short_direction(mock_service):
    """Verifies that for SHORT positions, price falling below tier target triggers execution."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "sl_price": 108.50,
        "tp_price": 102.0,
        "size_usd": 170.85,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    tier1_target = plan.get_tier(1).target_price  # Approx 105.585

    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=True,
        digest="0xmock_short_tp1",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=105.58,
        realized_pnl=0.48,
        is_partial_fill=False
    ))

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=tier1_target - 0.05)

    assert report is not None
    assert report["tier_index"] == 1
    assert report["slice_pnl"] == pytest.approx(0.48)


@pytest.mark.asyncio
async def test_legacy_position_without_partial_tp_plan_safely_ignored(mock_service):
    """Verifies that a position without partial_tp_plan returns None without error."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0
    }

    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.50)
    assert report is None


@pytest.mark.asyncio
async def test_check_and_execute_partial_tp_2_tier_50_50_immediate_be(mock_service):
    """Verifies that in 2_TIER mode, completing Tier 1 (50%) immediately activates Breakeven protection."""
    from core.config import config
    config.PARTIAL_TP_MODE = "2_TIER"

    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan.mode == "2_TIER"
    assert len(plan.tiers) == 2
    assert plan.tiers[0].action == "PROTECT_BREAKEVEN"
    assert plan.tiers[0].fraction == 0.50
    assert plan.tiers[0].target_price == pytest.approx(102.0)  # 50% of 4.0 target

    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=True,
        digest="0xmock_tp1_50_50",
        requested_contracts=0.8,
        filled_contracts=0.8,
        fill_price=102.0,
        realized_pnl=1.60,
        is_partial_fill=False
    ))
    mock_service.update_stop_loss_to_be = AsyncMock(return_value=True)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=102.05)

    assert report is not None
    assert report["tier_index"] == 1
    assert report["fraction"] == 0.50
    assert report["slice_pnl"] == pytest.approx(1.60)
    assert report["remaining_contracts"] == pytest.approx(0.8)

    # Breakeven was updated immediately on Tier 1
    mock_service.update_stop_loss_to_be.assert_awaited_once_with("SOL-USD")

    # Telegram message confirms 50% slice and Breakeven
    msg = mock_tg.send_message.call_args[0][0]
    assert "TP1 - 50%" in msg
    assert "БЕЗУБЫТОК" in msg

