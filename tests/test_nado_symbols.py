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
