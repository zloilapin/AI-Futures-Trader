from abc import ABC, abstractmethod
from typing import Dict, Any, List

class BaseTradingService(ABC):
    """
    Abstract Base Class for all trading services (Paper, Kraken, Nado).
    Enforces a consistent interface across different execution environments.
    """
    
    @abstractmethod
    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns the current portfolio balance and margin."""
        pass
        
    @abstractmethod
    async def get_active_positions(self) -> List[Dict[str, Any]]:
        """Returns a list of currently active open positions."""
        pass
        
    @abstractmethod
    async def open_position(self, symbol: str, direction: str, entry_price: float, size_usd: float, tp_price: float, sl_price: float, leverage: int) -> bool:
        """Opens a new position on the exchange."""
        pass
        
    @abstractmethod
    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """Checks if a position hit TP/SL locally and triggers close if needed. Returns reports of closed trades."""
        pass
        
    @abstractmethod
    async def force_close_position(self, symbol: str) -> tuple:
        """Manually closes a position. Returns (success_bool, message_string)."""
        pass
        
    @abstractmethod
    async def sync_with_exchange(self) -> None:
        """Synchronizes local state with exchange state (e.g. dropped orders, manual closes)."""
        pass
