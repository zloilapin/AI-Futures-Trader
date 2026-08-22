import os
import logging
from typing import Optional

logger = logging.getLogger("System_Core")

class Web3Wallet:
    """
    Handles Web3 Private Key management and basic RPC connectivity for Nado DEX on Ink L2.
    """
    def __init__(self):
        self.private_key = os.getenv("INK_PRIVATE_KEY", "")
        self.wallet_address = os.getenv("INK_WALLET_ADDRESS", "")
        self.rpc_url = os.getenv("INK_RPC_URL", "https://rpc-gel.inkonchain.com")
        self.chain_id = int(os.getenv("INK_CHAIN_ID", "763373"))  # Ink network chain ID
        
        if not self.private_key:
            logger.warning("[Web3Wallet] ⚠️ INK_PRIVATE_KEY is not set in .env! Nado Trading Service will fail.")
        else:
            if not self.wallet_address:
                try:
                    from eth_account import Account
                    self.wallet_address = Account.from_key(self.private_key).address
                except Exception as e:
                    logger.error(f"[Web3Wallet] ❌ Failed to derive wallet address: {e}")
            logger.info(f"[Web3Wallet] ✅ Web3 Wallet initialized for Ink L2. Address: {self.wallet_address}")

    def get_private_key(self) -> str:
        """Returns the private key for signing transactions/messages."""
        return self.private_key

    def get_address(self) -> str:
        """Returns the public wallet address."""
        return self.wallet_address

    def is_configured(self) -> bool:
        """Checks if the wallet has been configured properly."""
        return bool(self.private_key)
