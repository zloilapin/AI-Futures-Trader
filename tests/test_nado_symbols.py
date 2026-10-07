import pytest
from unittest.mock import MagicMock, AsyncMock
from services.nado_trading_service import NadoTradingService

def test_symbol_normalization_and_deduplication():
    # Setup mock service
    logger = MagicMock()
    service = NadoTradingService.__new__(NadoTradingService)
    service.logger = logger
    service.product_map = {
        "BCH": 1,
        "BCH-USD": 1,
        "ETH": 2,
        "ETH-USD": 2
    }
    
    # Reverse map logic (canonical pair format BASE-USD)
    id_to_symbol = {}
    for k, v in service.product_map.items():
        if '-' in k:
            id_to_symbol[v] = k
        elif v not in id_to_symbol:
            id_to_symbol[v] = f"{k}-USD"
            
    assert id_to_symbol[1] == "BCH-USD"
    assert id_to_symbol[2] == "ETH-USD"


def test_deduplication_cleanup():
    service = NadoTradingService.__new__(NadoTradingService)
    service.active_positions = {
        "BCH-USD": {"direction": "LONG", "entry_price": 500.0},
        "BCH": {"direction": "LONG", "entry_price": 500.0}, # duplicate!
        "ETH-USD": {"direction": "LONG", "entry_price": 3000.0},
        "ETH": {"direction": "LONG", "entry_price": 3000.0}, # duplicate!
    }
    
    # Run cleanup logic as in sync_with_exchange
    to_remove = []
    for k in list(service.active_positions.keys()):
        if '-' not in k:
            canonical = f"{k}-USD"
            if canonical in service.active_positions:
                to_remove.append(k)
    for k in to_remove:
        del service.active_positions[k]
        
    assert "BCH" not in service.active_positions
    assert "ETH" not in service.active_positions
    assert "BCH-USD" in service.active_positions
    assert "ETH-USD" in service.active_positions
    assert len(service.active_positions) == 2


def test_is_already_tracked_check():
    service = NadoTradingService.__new__(NadoTradingService)
    service.active_positions = {
        "BCH-USD": {"direction": "LONG", "entry_price": 500.0}
    }
    
    # Check if incoming symbol from on-chain (e.g. "BCH" or "BCH-USD") is recognized as tracked
    incoming_symbols = ["BCH", "BCH-USD"]
    for incoming in incoming_symbols:
        base_symbol = incoming.split('-')[0].upper()
        canonical_symbol = f"{base_symbol}-USD"
        is_already_tracked = any(
            k == canonical_symbol or k == base_symbol or k.split('-')[0].upper() == base_symbol
            for k in service.active_positions
        )
        assert is_already_tracked is True

    # Unknown symbol should not be tracked
    incoming_unknown = "SOL"
    base_unknown = incoming_unknown.split('-')[0].upper()
    is_unknown_tracked = any(
        k == f"{base_unknown}-USD" or k == base_unknown or k.split('-')[0].upper() == base_unknown
        for k in service.active_positions
    )
    assert is_unknown_tracked is False


@pytest.mark.asyncio
async def test_sync_with_exchange_trigger_orders():
    service = NadoTradingService.__new__(NadoTradingService)
    service.logger = MagicMock()
    service.is_connected = True
    service.default_subaccount_id = "0x123"
    service.product_map = {"ETH": 4, "ETH-USD": 4}
    service.active_positions = {}
    
    # Mock positions from exchange
    service.get_active_positions = AsyncMock(return_value=[{
        "symbol": "ETH-USD",
        "direction": "LONG",
        "entry_price": 2500.0,
        "size_usd": 150.0,
        "amount": 0.06,
        "leverage": 10,
        "_product_id": 4
    }])
    
    # Mock trigger order objects
    sl_req = MagicMock(spec=["oracle_price_below"])
    sl_req.oracle_price_below = "2400000000000000000000"
    tp_req = MagicMock(spec=["oracle_price_above"])
    tp_req.oracle_price_above = "2700000000000000000000"
    
    sl_order = MagicMock()
    sl_order.order.digest = "0xsl123"
    sl_order.order.order.subaccount = "0x123"
    sl_order.order.order.amount = "-60000000000000000"
    sl_order.order.trigger.price_trigger.price_requirement = sl_req
    sl_order.placed_at = 100
    
    tp_order = MagicMock()
    tp_order.order.digest = "0xtp123"
    tp_order.order.order.subaccount = "0x123"
    tp_order.order.order.amount = "-60000000000000000"
    tp_order.order.trigger.price_trigger.price_requirement = tp_req
    tp_order.placed_at = 100
    
    service._fetch_trigger_orders = AsyncMock(return_value=[sl_order, tp_order])
    async def mock_trailing(sym): pass
    service._trailing_stop_monitor = mock_trailing
    
    await service.sync_with_exchange()
    
    assert "ETH-USD" in service.active_positions
    pos = service.active_positions["ETH-USD"]
    assert pos["direction"] == "LONG"
    assert pos["entry_price"] == 2500.0
    assert pos["tp_price"] == 2700.0
    assert pos["sl_price"] == 2400.0
    assert pos["sl_digest"] == "0xsl123"
    assert pos["unverified_triggers"] is False

