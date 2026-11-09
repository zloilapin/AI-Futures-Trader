import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from decimal import Decimal
from services.nado_trading_service import NadoTradingService

@pytest.fixture
def mock_nado_service():
    mock_client = MagicMock()
    mock_client.market.get_all_product_symbols.return_value = [
        MagicMock(product_id=2, symbol="BTC-PERP"),
        MagicMock(product_id=4, symbol="ETH-PERP")
    ]
    mock_client.market.get_all_engine_markets.return_value = MagicMock(
        perp_products=[MagicMock(product_id=2), MagicMock(product_id=4)]
    )
    mock_client.subaccount.get_engine_subaccount_summary.return_value = MagicMock(
        spot_balances=[MagicMock(product_id=0, balance=MagicMock(amount=int(1000 * 1e18)))],
        perp_balances=[]
    )
    service = NadoTradingService(nado_client=mock_client)
    service.client = mock_client
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa"
    service.wallet.get_private_key.return_value = "0x" + "1" * 64
    service.is_connected = True
    service.default_subaccount_id = "0x" + "2" * 64
    service.product_map = {"ETH": 4, "ETH-USD": 4, "BTC": 2, "BTC-USD": 2}
    service.active_positions = {}
    return service

@pytest.mark.asyncio
async def test_error_2008_post_only_crosses_book_auto_fallback_to_ioc(mock_nado_service):
    """
    Verifies that when a Maker Post-Only order fails with error 2008 (crosses book),
    NadoTradingService automatically catches it and retries as an IOC Taker order with slippage buffer.
    """
    service = mock_nado_service

    # Setup market parameters mock
    mock_market_info = MagicMock()
    mock_market_info.book_info.size_increment = str(int(0.01 * 1e18))
    mock_market_info.book_info.price_increment_x18 = str(int(0.1 * 1e18))
    mock_market_info.book_info.min_size = str(int(0.01 * 1e18))
    mock_market_info.state.cumulative_funding = MagicMock()
    mock_market_info.state.cumulative_funding.rate_x18 = "0"
    service._market_cache = {4: mock_market_info}

    # Simulate error 2008 on first attempt, success on second attempt
    error_2008 = Exception('{"error_code": 2008, "error": "The order cannot be placed as it is post-only and crosses the book."}')
    success_resp = MagicMock()
    success_resp.data.digest = "0xfallbacksuccessdigest12345"

    calls = []
    def mock_place_order(params):
        calls.append(params)
        if len(calls) == 1:
            raise error_2008
        return success_resp

    service.client.market.place_order.side_effect = mock_place_order

    # Historical orders mock to verify fill
    mock_order = MagicMock()
    mock_order.base_filled = str(int(0.1 * 1e18))
    mock_order.quote_filled = str(int(230.0 * 1e18))
    mock_order.fee = "0"
    service.client.market.get_historical_orders_by_digest.return_value = MagicMock(orders=[mock_order])

    # Place order trigger mocks
    service.client.market.place_trigger_order = MagicMock(return_value=MagicMock(data=MagicMock(digest="0xtrigger")))

    # Act: Open position in TREND_FOLLOWING (which defaults to use_maker_entry = True)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        success = await service.open_position(
            symbol="ETH-USD",
            direction="LONG",
            entry_price=2300.0,
            notional_usd=230.0,
            tp_price=2400.0,
            sl_price=2200.0,
            leverage=5,
            contracts=0.1,
            approved_risk_usd=10.0,
            strategy_mode="TREND_FOLLOWING"
        )

    # Assert: Auto-fallback occurred, 2 place_order calls were made
    assert len(calls) == 2, f"Expected 2 place_order calls (Post-Only + Fallback IOC), got {len(calls)}"
    assert success is True, "Expected open_position to succeed after fallback!"
    assert "ETH-USD" in service.active_positions
    assert service.active_positions["ETH-USD"]["open_digest"] == "0xfallbacksuccessdigest12345"
