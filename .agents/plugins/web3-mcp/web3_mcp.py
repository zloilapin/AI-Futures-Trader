import sys
import json
from mcp.server.mcpserver import MCPServer
from web3 import Web3

# Initialize MCP server
mcp = MCPServer("Ink-Web3-Explorer")

RPC_URL = "https://rpc-gel-sepolia.inkonchain.com"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

@mcp.tool()
def get_transaction_receipt(tx_hash: str) -> str:
    """Fetch the transaction receipt for a given transaction hash on Ink Sepolia Testnet."""
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        
        # Convert web3 AttributeDict to standard dict and hex-encode bytes
        def default_serializer(obj):
            if isinstance(obj, bytes):
                return obj.hex()
            return str(obj)
            
        return json.dumps(dict(receipt), default=default_serializer, indent=2)
    except Exception as e:
        return f"Error: {e}"

@mcp.tool()
def get_eth_balance(address: str) -> str:
    """Fetch the ETH balance of a given address on Ink Sepolia Testnet."""
    try:
        balance_wei = w3.eth.get_balance(w3.to_checksum_address(address))
        balance_eth = w3.from_wei(balance_wei, 'ether')
        return f"Balance for {address}: {balance_eth} ETH"
    except Exception as e:
        return f"Error: {e}"

@mcp.tool()
def get_contract_code(address: str) -> str:
    """Check if an address has smart contract code deployed."""
    try:
        code = w3.eth.get_code(w3.to_checksum_address(address))
        if code and code != b'':
            return f"Address {address} is a smart contract. Bytecode size: {len(code)} bytes."
        return f"Address {address} is an EOA (Externally Owned Account), no contract code."
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    mcp.run()
