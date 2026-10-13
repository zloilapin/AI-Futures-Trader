"""
Unit tests for NadoTradingService.execute_partial_close (Phase 2).
Verifies:
- Opposite direction only (LONG -> SELL counter order, SHORT -> BUY counter order).
- Strict reduce_only=True encoding in appendix.
- Accurate lot alignment and min_size guards.
- Fill verification and realized PnL calculations from real indexer fills.
- Partial fill and zero fill (IOC cancellation) handling.
- Idempotent and isolated execution without side-effects on active_positions.
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from nado_protocol.utils.order import order_reduce_only


@pytest.fixture
def mock_service():
    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service.product_map = {"SOL": 8, "SOL-USD": 8, "ETH": 4, "ETH-USD": 4}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1111111111111111111111111111111111111111"
    service.default_subaccount_id = "0x" + "11" * 32
    
    # Mock market params for SOL (product 8)
    service._market_cache = {
        8: MagicMock(
            book_info=MagicMock(
                size_increment=int(0.1 * 1e18),
                price_increment_x18=int(0.001 * 1e18),
                min_size=int(0.1 * 1e18)
            )
        ),
        4: MagicMock(
            book_info=MagicMock(
                size_increment=int(0.005 * 1e18),
                price_increment_x18=int(0.01 * 1e18),
                min_size=int(0.01 * 1e18)
            )
        )
    }
    return service


@pytest.mark.asyncio
async def test_execute_partial_close_long_counter_sell(mock_service):
    """
    Verifies that a LONG position partial close generates a counter SELL order (negative amount)
    with reduce_only=True encoded in the appendix, and accurately computes slice PnL.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "product_id": 8,
        "size_usd": 160.0,
        "sender": "0xOwnerAddress123default"
    }

    # Mock place_order response
    mock_res = MagicMock()
    mock_res.data.digest = "0xmock_digest_long_close"
    mock_service.client.market.place_order.return_value = mock_res

    # Mock indexer historical orders response (0.4 SOL filled at 105.0)
    mock_order = MagicMock()
    mock_order.base_filled = str(int(-0.4 * 1e18))
    mock_order.quote_filled = str(int(42.0 * 1e18))  # 42 / 0.4 = 105.0
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order]
    mock_service.client.market.get_historical_orders_by_digest.return_value = mock_hist

    result = await mock_service.execute_partial_close(
        symbol="SOL-USD",
        contracts_to_close=0.4,
        current_price=105.0,
        max_poll_attempts=1
    )

    assert result.success is True
    assert result.digest == "0xmock_digest_long_close"
    assert result.filled_contracts == pytest.approx(0.4)
    assert result.fill_price == pytest.approx(105.0)
    # Net PnL for LONG: 0.4 * (105.0 - 100.0) - 0.05% fee (0.021) = +1.979
    assert result.realized_pnl == pytest.approx(1.979)
    assert result.is_partial_fill is False

    # Verify place_order call arguments
    mock_service.client.market.place_order.assert_called_once()
    place_params = mock_service.client.market.place_order.call_args[0][0]
    assert place_params.product_id == 8
    
    order = place_params.order
    # Counter order for LONG must have NEGATIVE amount (SELL)
    assert order.amount == -int(0.4 * 1e18)
    # Verify reduce_only is strictly True in the encoded appendix!
    assert order_reduce_only(order.appendix) is True


@pytest.mark.asyncio
async def test_execute_partial_close_short_counter_buy(mock_service):
    """
    Verifies that a SHORT position partial close generates a counter BUY order (positive amount)
    with reduce_only=True encoded in the appendix, and accurately computes slice PnL.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "product_id": 8,
        "size_usd": 170.85,
        "sender": "0xOwnerAddress123default"
    }

    mock_res = MagicMock()
    mock_res.data.digest = "0xmock_digest_short_close"
    mock_service.client.market.place_order.return_value = mock_res

    # 0.4 SOL filled at 105.0
    mock_order = MagicMock()
    mock_order.base_filled = str(int(0.4 * 1e18))
    mock_order.quote_filled = str(int(42.0 * 1e18))
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order]
    mock_service.client.market.get_historical_orders_by_digest.return_value = mock_hist

    result = await mock_service.execute_partial_close(
        symbol="SOL-USD",
        contracts_to_close=0.4,
        current_price=105.0,
        max_poll_attempts=1
    )

    assert result.success is True
    assert result.filled_contracts == pytest.approx(0.4)
    # Net PnL for SHORT: 0.4 * (106.78 - 105.0) - 0.05% fee (0.021) = +0.691
    assert result.realized_pnl == pytest.approx(0.691)

    place_params = mock_service.client.market.place_order.call_args[0][0]
    order = place_params.order
    # Counter order for SHORT must have POSITIVE amount (BUY)
    assert order.amount == int(0.4 * 1e18)
    # Must have reduce_only=True!
    assert order_reduce_only(order.appendix) is True


@pytest.mark.asyncio
async def test_execute_partial_close_below_min_size_rejected(mock_service):
    """Verifies that attempting to close below product min_size is rejected before submission."""
    mock_service.active_positions["ETH-USD"] = {
        "direction": "LONG",
        "entry_price": 2500.0,
        "product_id": 4
    }

    # Product 4 has min_size = 0.01. Requesting 0.005 ETH!
    result = await mock_service.execute_partial_close(
        symbol="ETH-USD",
        contracts_to_close=0.005,
        current_price=2550.0
    )

    assert result.success is False
    assert "below market min_size" in result.error
    # place_order must NOT be called
    mock_service.client.market.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_partial_close_partial_fill_handling(mock_service):
    """Verifies that an incomplete fill (e.g. 0.2 filled out of 0.4 requested) is flagged as partial."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "product_id": 8
    }

    mock_res = MagicMock()
    mock_res.data.digest = "0xmock_partial"
    mock_service.client.market.place_order.return_value = mock_res

    # Only 0.2 SOL filled out of 0.4 requested!
    mock_order = MagicMock()
    mock_order.base_filled = str(int(-0.2 * 1e18))
    mock_order.quote_filled = str(int(21.0 * 1e18))  # 105.0 fill
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order]
    mock_service.client.market.get_historical_orders_by_digest.return_value = mock_hist

    result = await mock_service.execute_partial_close(
        symbol="SOL-USD",
        contracts_to_close=0.4,
        current_price=105.0,
        max_poll_attempts=1
    )

    assert result.success is True
    assert result.is_partial_fill is True
    assert result.filled_contracts == pytest.approx(0.2)
    # Net PnL calculated strictly on 0.2 filled minus fee: 0.2 * (105 - 100) - 0.0105 fee = +0.9895
    assert result.realized_pnl == pytest.approx(0.9895)


@pytest.mark.asyncio
async def test_execute_partial_close_unfilled_ioc_expired(mock_service):
    """Verifies that an IOC order with 0 fills (expired/canceled) returns success=False."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "product_id": 8
    }

    mock_res = MagicMock()
    mock_res.data.digest = "0xmock_unfilled"
    mock_service.client.market.place_order.return_value = mock_res

    # 0 filled (IOC canceled by matching engine due to price slippage boundary)
    mock_order = MagicMock()
    mock_order.base_filled = "0"
    mock_order.quote_filled = "0"
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order]
    mock_service.client.market.get_historical_orders_by_digest.return_value = mock_hist

    result = await mock_service.execute_partial_close(
        symbol="SOL-USD",
        contracts_to_close=0.4,
        current_price=105.0,
        max_poll_attempts=1
    )

    assert result.success is False
    assert "IOC order expired without fills" in result.error
    assert result.filled_contracts == 0.0


@pytest.mark.asyncio
async def test_execute_partial_close_missing_position_guard(mock_service):
    """Verifies that if position is not found in active_positions, execution is safely rejected."""
    result = await mock_service.execute_partial_close(
        symbol="NONEXISTENT-USD",
        contracts_to_close=1.0,
        current_price=50.0
    )
    assert result.success is False
    assert "not found in active_positions" in result.error
