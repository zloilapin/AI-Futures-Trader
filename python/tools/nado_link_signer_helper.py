"""
Nado DEX Non-Custodial LinkSigner Onboarding Helper
===================================================

This utility enables non-custodial, permission-isolated onboarding of AI-Futures-Trader
to a user's Nado DEX subaccount on Ink L2 (Chain ID 57073 / Testnet 763373).

Key Security Guarantees:
------------------------
1. ZERO-WITHDRAWAL PRIVILEGE:
   The generated session key (or any Linked Signer) has permissions ONLY to sign:
   - `execute_place_order` (Maker/Taker trades)
   - `execute_cancel_orders` (Order lifecycle & risk management)
   
   A Linked Signer CANNOT call:
   - `withdraw_collateral`
   - `withdraw_collateral_v2`
   - `transfer_quote`
   Withdrawal of collateral strictly requires an EIP-712 signature from the primary
   subaccount owner key (`owner == sender[0:20]`). User capital cannot be stolen even
   if the agent's server or session key is compromised.

2. EPHEMERAL SESSION DELEGATION:
   The agent operates using a disposable session key. The subaccount owner can revoke
   or overwrite the linked signer at any time directly through the Nado DEX UI or
   contracts without moving their capital.
"""

import sys
import json
import time
import argparse
from typing import Dict, Any, Tuple, Optional
from eth_account import Account
from eth_account.signers.local import LocalAccount

from nado_protocol.contracts.eip712.sign import build_eip712_typed_data, sign_eip712_typed_data
from nado_protocol.contracts.types import NadoExecuteType
from nado_protocol.utils.bytes32 import subaccount_to_hex, subaccount_to_bytes32

# Constants
MAINNET_CHAIN_ID = 57073  # Ink L2 Mainnet
TESTNET_CHAIN_ID = 763373  # Ink Sepolia Testnet

MAINNET_ENDPOINT = "0x05ec92D78ED421f3D3Ada77FFdE167106565974E"
TESTNET_ENDPOINT = "0x698D87105274292B5673367DEC81874Ce3633Ac2"


def generate_session_key() -> Tuple[str, str]:
    """
    Generate a new random ephemeral session key for the AI trading agent.
    Returns:
        (address, private_key_hex)
    """
    acc = Account.create()
    return acc.address, acc.key.hex()


def format_subaccount_bytes32(address: str, subaccount_name: str = "default", as_bytes: bool = False) -> Any:
    """Format an Ethereum address and subaccount name into 32-byte hex string or raw bytes."""
    if as_bytes:
        return subaccount_to_bytes32(address, subaccount_name)
    return subaccount_to_hex(address, subaccount_name)


def format_signer_bytes32(address: str, as_bytes: bool = False) -> Any:
    """Format a signer Ethereum address into 32-byte hex string or raw bytes."""
    if as_bytes:
        return subaccount_to_bytes32(address, "")
    return subaccount_to_hex(address, "")


def build_link_signer_eip712_payload(
    owner_address: str,
    signer_address: str,
    subaccount_name: str = "default",
    chain_id: int = MAINNET_CHAIN_ID,
    verifying_contract: Optional[str] = None,
    nonce: Optional[int] = None,
    for_python_signing: bool = False
) -> Dict[str, Any]:
    """
    Build EIP-712 structured data dict matching eth_signTypedData_v4 for Nado LinkSigner.
    If for_python_signing=True, message values for bytes32 fields are raw bytes.
    If for_python_signing=False (default for JSON export), values are 0x-prefixed hex strings.
    """
    endpoint = verifying_contract or (MAINNET_ENDPOINT if chain_id == MAINNET_CHAIN_ID else TESTNET_ENDPOINT)
    tx_nonce = nonce if nonce is not None else int(time.time() * 1000)

    sender_val = format_subaccount_bytes32(owner_address, subaccount_name, as_bytes=for_python_signing)
    signer_val = format_signer_bytes32(signer_address, as_bytes=for_python_signing)

    payload = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"}
            ],
            "LinkSigner": [
                {"name": "sender", "type": "bytes32"},
                {"name": "signer", "type": "bytes32"},
                {"name": "nonce", "type": "uint64"}
            ]
        },
        "primaryType": "LinkSigner",
        "domain": {
            "name": "Nado",
            "version": "0.0.1",
            "chainId": int(chain_id),
            "verifyingContract": endpoint
        },
        "message": {
            "sender": sender_val,
            "signer": signer_val,
            "nonce": int(tx_nonce)
        }
    }
    return payload


def sign_link_signer_payload(
    owner_account: LocalAccount,
    signer_address: str,
    subaccount_name: str = "default",
    chain_id: int = MAINNET_CHAIN_ID,
    verifying_contract: Optional[str] = None,
    nonce: Optional[int] = None
) -> str:
    """
    Signs LinkSigner EIP-712 message using the official Nado SDK pipeline.
    Returns 65-byte signature string (0x...).
    """
    endpoint = verifying_contract or (MAINNET_ENDPOINT if chain_id == MAINNET_CHAIN_ID else TESTNET_ENDPOINT)
    tx_nonce = nonce if nonce is not None else int(time.time() * 1000)

    msg = {
        "sender": subaccount_to_bytes32(owner_account.address, subaccount_name),
        "signer": subaccount_to_bytes32(signer_address, ""),
        "nonce": tx_nonce
    }
    typed_data = build_eip712_typed_data(
        NadoExecuteType.LINK_SIGNER,
        msg,
        endpoint,
        chain_id
    )
    return sign_eip712_typed_data(typed_data, owner_account)


def get_security_permission_matrix() -> str:
    """Returns a visual non-custodial permission matrix."""
    return """
========================================================================================
🔐 NADO DEX NON-CUSTODIAL PERMISSION MATRIX (Owner vs. Linked Signer)
========================================================================================
Action / RPC Type               Primary Owner Wallet        Agent Linked Signer Key
----------------------------------------------------------------------------------------
Deposit Collateral              ✅ PERMITTED                ❌ Not applicable
execute_place_order (Limit/IOC) ✅ PERMITTED                ✅ PERMITTED (Algorithmic)
execute_cancel_orders (Stops)   ✅ PERMITTED                ✅ PERMITTED (Sentinel Agent)
withdraw_collateral (USDC)      ✅ PERMITTED (Owner only)   ⛔ FORBIDDEN (Zero Privilege)
withdraw_collateral_v2 (USDC)   ✅ PERMITTED (Owner only)   ⛔ FORBIDDEN (Zero Privilege)
transfer_quote                  ✅ PERMITTED (Owner only)   ⛔ FORBIDDEN (Zero Privilege)
link_signer / unlink_signer     ✅ PERMITTED (Owner only)   ⛔ FORBIDDEN (Zero Privilege)
----------------------------------------------------------------------------------------
🛡️ CRYPTOGRAPHIC SAFETY GUARANTEE:
The Nado CLOB Sequencer and Clearinghouse contracts strictly verify that `withdraw_collateral`
signatures match the subaccount owner (`owner == sender[0:20]`). Even if the agent's server
is completely compromised, funds CANNOT be drained.
========================================================================================
"""


def main():
    parser = argparse.ArgumentParser(
        description="Nado DEX LinkSigner Non-Custodial Onboarding Tool"
    )
    parser.add_argument(
        "--owner",
        type=str,
        default=None,
        help="Primary wallet address of subaccount owner (e.g. 0xYourWallet)"
    )
    parser.add_argument(
        "--signer",
        type=str,
        default=None,
        help="Signer address for agent (if omitted, generates a new session key)"
    )
    parser.add_argument(
        "--network",
        choices=["MAINNET", "TESTNET"],
        default="MAINNET",
        help="Target network (default: MAINNET, Chain ID 57073)"
    )
    parser.add_argument(
        "--subaccount",
        type=str,
        default="default",
        help="Subaccount label (default: 'default')"
    )
    parser.add_argument(
        "--export-json",
        type=str,
        default=None,
        help="File path to save the EIP-712 payload for MetaMask/Rabby signing"
    )

    args = parser.parse_args()

    print(get_security_permission_matrix())

    chain_id = MAINNET_CHAIN_ID if args.network == "MAINNET" else TESTNET_CHAIN_ID
    endpoint = MAINNET_ENDPOINT if args.network == "MAINNET" else TESTNET_ENDPOINT

    signer_address = args.signer
    signer_privkey = None

    if not signer_address:
        signer_address, signer_privkey = generate_session_key()
        print(f"🔑 [1/3] Generated New Ephemeral Agent Session Key:")
        print(f"   • Signer Address:     {signer_address}")
        print(f"   • Agent Private Key:  {signer_privkey}")
        print(f"   ⚠️ ACTION: Put this private key into the agent's .env:")
        print(f"      INK_PRIVATE_KEY={signer_privkey}\n")
    else:
        print(f"🔑 [1/3] Using Specified Signer Address: {signer_address}\n")

    owner_addr = args.owner or "0x0000000000000000000000000000000000000000"
    if not args.owner:
        print("ℹ️ Note: No --owner was provided; using dummy address for demonstration.")
        print("   Run with: python python/tools/nado_link_signer_helper.py --owner 0xYourWalletAddress\n")

    payload = build_link_signer_eip712_payload(
        owner_address=owner_addr,
        signer_address=signer_address,
        subaccount_name=args.subaccount,
        chain_id=chain_id,
        verifying_contract=endpoint
    )

    print("📄 [2/3] EIP-712 LinkSigner Signing Payload:")
    print(json.dumps(payload, indent=2))
    print()

    if args.export_json:
        with open(args.export_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"💾 [3/3] Payload successfully exported to: {args.export_json}\n")
    else:
        print("💡 [3/3] How to execute:")
        print("   1. Open MetaMask / Rabby / Web3 Wallet connected to Ink L2.")
        print("   2. Sign this EIP-712 LinkSigner message using owner wallet.")
        print("   3. Submit the signature to Nado Gateway (`execute_link_signer`).")
        print("   4. The agent can now trade on your behalf with 0 withdrawal risk!\n")


if __name__ == "__main__":
    main()
