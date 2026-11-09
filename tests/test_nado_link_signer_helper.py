import pytest
from eth_account import Account
from tools.nado_link_signer_helper import (
    generate_session_key,
    build_link_signer_eip712_payload,
    sign_link_signer_payload,
    get_security_permission_matrix,
    MAINNET_CHAIN_ID,
    TESTNET_CHAIN_ID,
    MAINNET_ENDPOINT
)


def test_generate_session_key():
    addr, privkey = generate_session_key()
    assert addr.startswith("0x") and len(addr) == 42
    assert privkey.startswith("0x") and len(privkey) == 66
    derived_acc = Account.from_key(privkey)
    assert derived_acc.address == addr


def test_build_link_signer_eip712_payload():
    owner = "0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa"
    signer = "0x9876543210987654321098765432109876543210"

    payload = build_link_signer_eip712_payload(
        owner_address=owner,
        signer_address=signer,
        subaccount_name="default",
        chain_id=MAINNET_CHAIN_ID,
        nonce=123456789
    )

    assert payload["primaryType"] == "LinkSigner"
    assert payload["domain"]["chainId"] == 57073
    assert payload["domain"]["verifyingContract"] == MAINNET_ENDPOINT
    assert payload["domain"]["name"] == "Nado"
    assert payload["domain"]["version"] == "0.0.1"

    msg = payload["message"]
    assert msg["nonce"] == 123456789
    assert msg["sender"].startswith(owner.lower())
    assert msg["signer"].startswith(signer.lower())


def test_sign_link_signer_payload_official_sdk():
    owner_acc = Account.create()
    signer_acc = Account.create()

    sig = sign_link_signer_payload(
        owner_account=owner_acc,
        signer_address=signer_acc.address,
        subaccount_name="default",
        chain_id=MAINNET_CHAIN_ID,
        nonce=99999
    )

    assert isinstance(sig, str)
    assert sig.startswith("0x")
    assert len(sig) == 132  # 0x + 130 hex chars (65 bytes)


def test_permission_matrix_text():
    matrix = get_security_permission_matrix()
    assert "PERMISSION MATRIX" in matrix
    assert "withdraw_collateral" in matrix
    assert "FORBIDDEN (Zero Privilege)" in matrix
    assert "execute_place_order" in matrix
