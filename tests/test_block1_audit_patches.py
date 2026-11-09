import pytest
from unittest.mock import MagicMock, patch
from services.nado_trading_service import NadoTradingService

def test_position_lock_canonical_deduplication():
    with patch("services.nado_trading_service.Web3Wallet"):
        service = NadoTradingService()
        
        lock1 = service._get_position_lock("SOL")
        lock2 = service._get_position_lock("SOL-USD")
        lock3 = service._get_position_lock("sol")
        lock4 = service._get_position_lock("SOL/USD")
        
        assert lock1 is lock2
        assert lock2 is lock3
        assert lock3 is lock4
        
        # Different asset gets distinct lock
        eth_lock = service._get_position_lock("ETH-USD")
        assert eth_lock is not lock1

def test_load_state_force_flag_reloads_cooldown():
    with patch("services.nado_trading_service.Web3Wallet"):
        service = NadoTradingService()
        service.cooldown_until = 999999.0
        service._last_cooldown_processed_len = 5
        
        # Simulated state where operator reset cooldown to 0.0
        mock_state = {
            "initial_balance": 100.0,
            "win_count": 10,
            "loss_count": 5,
            "cooldown_until": 0.0,
            "_last_cooldown_processed_len": 0
        }
        
        with patch("core.state_store.StateStore.load", return_value=mock_state):
            # Without force=True, non-zero memory values were previously stuck
            service._load_state(force=False)
            assert service.cooldown_until == 999999.0
            
            # With force=True, cooldown and length MUST reload from disk state
            service._load_state(force=True)
            assert service.cooldown_until == 0.0
            assert service._last_cooldown_processed_len == 0
