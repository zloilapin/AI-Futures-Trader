import logging
from typing import Optional

logger = logging.getLogger("System_Core")

def get_nado_client_mode(network_name: str = "TESTNET"):
    """
    Dynamically discovers and returns the appropriate NadoClientMode enum.
    Supports TESTNET / SEPOLIA_TESTNET and MAINNET.
    """
    from nado_protocol.client import NadoClientMode
    net = (network_name or "TESTNET").upper()
    
    if net == "MAINNET":
        return getattr(NadoClientMode, "MAINNET", NadoClientMode.MAINNET)
    
    # Priority check for testnet modes in SDK
    for attr in ["TESTNET", "SEPOLIA_TESTNET", "INK_SEPOLIA_TESTNET", "DEVNET"]:
        if hasattr(NadoClientMode, attr):
            return getattr(NadoClientMode, attr)
            
    for mode in NadoClientMode:
        if "TEST" in mode.name.upper() or "SEPOLIA" in mode.name.upper() or "DEV" in mode.name.upper():
            return mode
            
    logger.warning("[NadoHelper] No explicit TESTNET mode found in NadoClientMode, falling back to default.")
    return NadoClientMode.MAINNET

def create_configured_nado_client(network_name: str = "TESTNET", signer: Optional[str] = None):
    """
    Creates and returns an initialized NadoClient configured for the specified network.
    """
    from nado_protocol.client import create_nado_client
    mode = get_nado_client_mode(network_name)
    logger.info(f"[NadoHelper] Creating Nado Client in mode: {mode} (Network: {network_name})")
    return create_nado_client(mode=mode, signer=signer)
