"""
Trader Registry Service for Non-Custodial Multi-Account Trading
===============================================================
Manages storage, validation, and session lifecycle for connected Telegram traders.
All subscriber records are persisted in `data/memory/connected_traders.json`.

Security Guarantees:
--------------------
- EIP-712 cryptographic signature verification before accepting any session.
- Only delegated session keys are stored; user primary private keys are NEVER requested.
- Delegation has ZERO withdrawal authority on Nado DEX CLOB smart contracts.
- File is excluded from git via .gitignore.
"""

import os
import json
import time
import tempfile
from typing import Dict, Any, Optional, List, Tuple
from eth_account import Account
from eth_account.messages import encode_structured_data

from tools.nado_link_signer_helper import (
    generate_session_key,
    format_subaccount_bytes32,
    format_signer_bytes32,
    MAINNET_CHAIN_ID,
    MAINNET_ENDPOINT
)


class TraderRegistry:
    """Manages connected trader profiles, pending session keys, and persistence."""

    def __init__(self, storage_path: Optional[str] = None):
        if storage_path:
            self.storage_path = storage_path
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.storage_path = os.path.join(base_dir, "data", "memory", "connected_traders.json")

        self._pending_sessions: Dict[str, Dict[str, Any]] = {}
        self._ensure_storage_dir()

    def _ensure_storage_dir(self):
        dir_name = os.path.dirname(self.storage_path)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)

    def load_traders(self) -> Dict[str, Dict[str, Any]]:
        """Loads all connected traders from storage."""
        if not os.path.exists(self.storage_path) or os.path.getsize(self.storage_path) == 0:
            return {}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"⚠️ [TraderRegistry] Error loading traders from {self.storage_path}: {e}")
            return {}

    def save_traders(self, traders: Dict[str, Dict[str, Any]]) -> bool:
        """Atomically saves connected traders to storage."""
        self._ensure_storage_dir()
        dir_name = os.path.dirname(self.storage_path)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix="traders_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(traders, f, indent=2, ensure_ascii=False)
            if os.path.exists(self.storage_path):
                os.replace(temp_path, self.storage_path)
            else:
                os.rename(temp_path, self.storage_path)
            return True
        except Exception as e:
            print(f"❌ [TraderRegistry] Failed to save traders: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return False

    def prepare_pending_session(self, chat_id: str) -> Dict[str, Any]:
        """
        Generates a new disposable session key for this chat_id before user signs LinkSigner.
        Returns:
            {"signer_address": str, "signer_private_key": str, "created_at": float}
        """
        c_id = str(chat_id).strip()
        signer_addr, signer_key = generate_session_key()
        session = {
            "signer_address": signer_addr.lower(),
            "signer_private_key": signer_key,
            "created_at": time.time()
        }
        self._pending_sessions[c_id] = session
        return session

    def get_pending_session(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves pending session for chat_id."""
        return self._pending_sessions.get(str(chat_id).strip())

    @staticmethod
    def verify_link_signer_signature(
        owner_address: str,
        signer_address: str,
        subaccount_name: str = "default",
        nonce: int = 0,
        signature: str = "",
        chain_id: int = MAINNET_CHAIN_ID,
        verifying_contract: str = MAINNET_ENDPOINT
    ) -> bool:
        """
        Verifies the EIP-712 LinkSigner signature against the expected owner address.
        """
        try:
            clean_owner = owner_address.lower().strip()
            if not signature or not clean_owner.startswith("0x"):
                return False

            sender_hex = format_subaccount_bytes32(clean_owner, subaccount_name, as_bytes=False)
            signer_hex = format_signer_bytes32(signer_address, as_bytes=False)

            # Build EIP-712 structured data dict
            structured_data = {
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
                    "verifyingContract": verifying_contract
                },
                "message": {
                    "sender": bytes.fromhex(sender_hex.replace("0x", "")),
                    "signer": bytes.fromhex(signer_hex.replace("0x", "")),
                    "nonce": int(nonce)
                }
            }

            encoded = encode_structured_data(primitive=structured_data)
            recovered_addr = Account.recover_message(encoded, signature=signature)
            return recovered_addr.lower() == clean_owner
        except Exception as e:
            print(f"⚠️ [TraderRegistry] Signature verification error: {e}")
            return False

    def register_trader(
        self,
        chat_id: str,
        owner_address: str,
        subaccount_name: str = "default",
        signer_address: Optional[str] = None,
        signer_private_key: Optional[str] = None,
        signature: str = "",
        nonce: int = 0,
        max_risk_pct: float = 0.02
    ) -> Tuple[bool, str]:
        """
        Validates signature, registers subscriber profile, and persists to storage.
        """
        c_id = str(chat_id).strip()
        clean_owner = str(owner_address).strip().lower()
        sub_name = str(subaccount_name or "default").strip()

        # If signer_address/key not explicitly passed, check pending session
        s_addr = signer_address
        s_key = signer_private_key
        if not s_addr or not s_key:
            pending = self.get_pending_session(c_id)
            if pending:
                s_addr = pending.get("signer_address")
                s_key = pending.get("signer_private_key")

        if not s_addr or not s_key:
            return False, "❌ Сессионный ключ не найден. Запустите /connect заново."

        # Validate signature
        is_valid = self.verify_link_signer_signature(
            owner_address=clean_owner,
            signer_address=s_addr,
            subaccount_name=sub_name,
            nonce=nonce,
            signature=signature
        )

        if not is_valid:
            return False, "❌ Ошибка валидации: EIP-712 подпись LinkSigner не совпадает с адресом владельца."

        traders = self.load_traders()
        now_ts = time.time()
        trader_entry = {
            "chat_id": c_id,
            "owner_address": clean_owner,
            "subaccount": sub_name,
            "signer_address": s_addr.lower(),
            "signer_private_key": s_key,
            "signature": signature,
            "nonce": int(nonce),
            "copy_enabled": True,
            "max_risk_pct": float(max_risk_pct),
            "risk_profile": "BALANCED",
            "leverage": 6.0,
            "max_concurrent_positions": 2,
            "tp_mode": "2_TIER",
            "anti_bot_enabled": True,
            "target_notional_usd": 104.0,
            "created_at": traders.get(c_id, {}).get("created_at", now_ts),
            "updated_at": now_ts
        }

        traders[c_id] = trader_entry
        if self.save_traders(traders):
            # Clean up pending session
            self._pending_sessions.pop(c_id, None)
            return True, f"✅ Кошелек {clean_owner[:6]}...{clean_owner[-4:]} успешно подключен!"
        return False, "❌ Ошибка при сохранении профиля трейдера в хранилище."

    def register_watch_only(
        self,
        chat_id: str,
        owner_address: str,
        subaccount_name: str = "default"
    ) -> Tuple[bool, str]:
        """Registers a trader in Watch-Only mode without session keys."""
        c_id = str(chat_id).strip()
        clean_owner = str(owner_address).strip().lower()
        if not clean_owner.startswith("0x") or len(clean_owner) != 42:
            return False, "❌ Неверный формат адреса. Адрес кошелька должен начинаться с 0x и содержать 42 символа."

        traders = self.load_traders()
        now_ts = time.time()
        trader_entry = {
            "chat_id": c_id,
            "owner_address": clean_owner,
            "subaccount": subaccount_name or "default",
            "signer_address": None,
            "signer_private_key": None,
            "signature": None,
            "nonce": 0,
            "copy_enabled": False,
            "watch_only": True,
            "risk_profile": "WATCH_ONLY",
            "leverage": 1.0,
            "max_concurrent_positions": 0,
            "tp_mode": "MONITOR",
            "anti_bot_enabled": True,
            "target_notional_usd": 0.0,
            "created_at": traders.get(c_id, {}).get("created_at", now_ts),
            "updated_at": now_ts
        }
        traders[c_id] = trader_entry
        if self.save_traders(traders):
            self._pending_sessions.pop(c_id, None)
            return True, f"✅ Кошелек {clean_owner[:6]}...{clean_owner[-4:]} подключен в режиме Watch-Only!"
        return False, "❌ Ошибка при сохранении профиля Watch-Only в хранилище."

    def get_trader(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves registered trader by chat_id."""
        traders = self.load_traders()
        return traders.get(str(chat_id).strip())

    def update_trader_settings(self, chat_id: str, **kwargs) -> bool:
        """
        Updates personal trading constants (risk_profile, leverage, max_concurrent_positions,
        tp_mode, anti_bot_enabled, target_notional_usd, max_risk_pct) for a connected trader.
        """
        c_id = str(chat_id).strip()
        traders = self.load_traders()
        if c_id not in traders:
            return False

        trader = traders[c_id]
        allowed_keys = {
            "risk_profile", "leverage", "max_concurrent_positions",
            "tp_mode", "anti_bot_enabled", "target_notional_usd", "max_risk_pct"
        }
        for k, v in kwargs.items():
            if k in allowed_keys and v is not None:
                trader[k] = v
        trader["updated_at"] = time.time()
        return self.save_traders(traders)

    def get_trader_settings(self, chat_id: str) -> Dict[str, Any]:
        """Returns personal trading settings dictionary for trader, or defaults."""
        trader = self.get_trader(chat_id) or {}
        return {
            "risk_profile": trader.get("risk_profile", "BALANCED"),
            "leverage": float(trader.get("leverage", 6.0)),
            "max_concurrent_positions": int(trader.get("max_concurrent_positions", 2)),
            "tp_mode": trader.get("tp_mode", "2_TIER"),
            "anti_bot_enabled": bool(trader.get("anti_bot_enabled", True)),
            "target_notional_usd": float(trader.get("target_notional_usd", 104.0)),
            "max_risk_pct": float(trader.get("max_risk_pct", 0.02)),
            "copy_enabled": bool(trader.get("copy_enabled", True))
        }

    def toggle_copy(self, chat_id: str) -> Tuple[bool, Optional[bool]]:
        """
        Toggles copy_enabled flag for chat_id.
        Returns: (success: bool, new_state: Optional[bool])
        """
        c_id = str(chat_id).strip()
        traders = self.load_traders()
        if c_id not in traders:
            return False, None
        current_state = traders[c_id].get("copy_enabled", True)
        new_state = not current_state
        traders[c_id]["copy_enabled"] = new_state
        traders[c_id]["updated_at"] = time.time()
        success = self.save_traders(traders)
        return success, new_state

    def disconnect_trader(self, chat_id: str) -> bool:
        """Removes trader profile and session key."""
        c_id = str(chat_id).strip()
        traders = self.load_traders()
        if c_id in traders:
            del traders[c_id]
            self.save_traders(traders)
            self._pending_sessions.pop(c_id, None)
            return True
        return False

    def get_active_subscribers(self) -> List[Dict[str, Any]]:
        """Returns all registered traders who have copy_enabled == True and are NOT watch_only."""
        traders = self.load_traders()
        return [
            t for t in traders.values() 
            if t.get("copy_enabled", True) and not t.get("watch_only", False) and t.get("signer_private_key")
        ]

    def get_all_traders(self) -> List[Dict[str, Any]]:
        """Returns all registered traders."""
        return list(self.load_traders().values())


# Global singleton instance
trader_registry = TraderRegistry()
