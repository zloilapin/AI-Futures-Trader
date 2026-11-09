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
from datetime import datetime, timezone
from eth_account import Account
from eth_account.messages import encode_structured_data

from tools.nado_link_signer_helper import (
    generate_session_key,
    format_subaccount_bytes32,
    format_signer_bytes32,
    MAINNET_CHAIN_ID,
    MAINNET_ENDPOINT
)

# Individual risk profile presets per production multi-user copy-trading requirements
RISK_PROFILES = {
    "conservative": {
        "risk_mode": "conservative",
        "risk_per_trade_pct": 0.9,
        "max_slots": 1,
        "max_leverage": 5.0,
        "tp_r_target": 1.8,
        "be_trigger_r": 0.60,
        "trail_activation_r": 1.00,
        "only_high_conviction": True,
    },
    "balanced": {
        "risk_mode": "balanced",
        "risk_per_trade_pct": 1.6,
        "max_slots": 2,
        "max_leverage": 8.0,
        "tp_r_target": 3.0,
        "be_trigger_r": 0.75,
        "trail_activation_r": 1.35,
        "only_high_conviction": False,
    },
    "aggressive": {
        "risk_mode": "aggressive",
        "risk_per_trade_pct": 2.6,
        "max_slots": 3,
        "max_leverage": 12.0,
        "tp_r_target": 3.8,
        "be_trigger_r": 0.90,
        "trail_activation_r": 1.60,
        "only_high_conviction": False,
    }
}


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

    def _normalize_trader_entry(self, trader: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures all risk profile and target fields exist with safe defaults."""
        if not isinstance(trader, dict):
            return trader
        mode = trader.get("risk_mode")
        if not mode:
            legacy_prof = str(trader.get("risk_profile", "BALANCED")).lower()
            mode = legacy_prof if legacy_prof in RISK_PROFILES else "balanced"
        preset = RISK_PROFILES.get(mode, RISK_PROFILES["balanced"])

        defaults = {
            "risk_mode": mode,
            "risk_per_trade_pct": preset["risk_per_trade_pct"],
            "max_slots": preset["max_slots"],
            "max_leverage": preset["max_leverage"],
            "tp_r_target": preset["tp_r_target"],
            "be_trigger_r": preset["be_trigger_r"],
            "trail_activation_r": preset["trail_activation_r"],
            "only_high_conviction": preset["only_high_conviction"],
            "max_daily_drawdown_pct": 4.0,
            "target_equity_usd": 0.0,
            "target_equity_reached": False,
            "daily_drawdown_paused": False,
            "daily_start_equity": 0.0,
            "daily_reset_date": "",
            "late_join_policy": "next_signal_only",
            "connected_at": trader.get("created_at", time.time())
        }
        for k, v in defaults.items():
            if k not in trader:
                trader[k] = v
        return trader

    def load_traders(self) -> Dict[str, Dict[str, Any]]:
        """Loads all connected traders from storage."""
        if not os.path.exists(self.storage_path) or os.path.getsize(self.storage_path) == 0:
            return {}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {k: self._normalize_trader_entry(v) for k, v in data.items()}
                return {}
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
        preset = RISK_PROFILES.get("balanced", {})
        trader_entry = {
            "chat_id": c_id,
            "owner_address": clean_owner,
            "subaccount": sub_name,
            "signer_address": s_addr.lower(),
            "signer_private_key": s_key,
            "signature": signature,
            "nonce": int(nonce),
            "copy_enabled": True,
            "risk_mode": "balanced",
            "risk_per_trade_pct": float(preset.get("risk_per_trade_pct", 1.6)),
            "max_slots": int(preset.get("max_slots", 2)),
            "max_leverage": float(preset.get("max_leverage", 8.0)),
            "tp_r_target": float(preset.get("tp_r_target", 3.0)),
            "be_trigger_r": float(preset.get("be_trigger_r", 0.75)),
            "trail_activation_r": float(preset.get("trail_activation_r", 1.35)),
            "only_high_conviction": bool(preset.get("only_high_conviction", False)),
            "max_daily_drawdown_pct": 4.0,
            "daily_start_equity": 0.0,
            "daily_drawdown_paused": False,
            "daily_reset_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "target_equity_usd": 0.0,
            "target_equity_reached": False,
            "late_join_policy": "next_signal_only",
            "connected_at": traders.get(c_id, {}).get("created_at", now_ts),
            # Legacy fields for backward compatibility
            "max_risk_pct": float(max_risk_pct) if max_risk_pct else 0.016,
            "risk_profile": "BALANCED",
            "leverage": float(preset.get("max_leverage", 8.0)),
            "max_concurrent_positions": int(preset.get("max_slots", 2)),
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
        Updates personal trading constants (risk_mode, risk_profile, leverage, max_concurrent_positions,
        tp_mode, anti_bot_enabled, target_notional_usd, max_risk_pct, etc.) for a connected trader.
        """
        c_id = str(chat_id).strip()
        traders = self.load_traders()
        if c_id not in traders:
            return False

        trader = traders[c_id]
        allowed_keys = {
            "risk_mode", "risk_per_trade_pct", "max_slots", "max_leverage",
            "tp_r_target", "be_trigger_r", "trail_activation_r", "only_high_conviction",
            "max_daily_drawdown_pct", "target_equity_usd", "target_equity_reached",
            "daily_drawdown_paused", "daily_start_equity", "daily_reset_date",
            "late_join_policy", "copy_enabled", "connected_at",
            "risk_profile", "leverage", "max_concurrent_positions",
            "tp_mode", "anti_bot_enabled", "target_notional_usd", "max_risk_pct"
        }
        for k, v in kwargs.items():
            if k in allowed_keys and v is not None:
                trader[k] = v
        trader["updated_at"] = time.time()
        return self.save_traders(traders)

    def set_trader_risk_profile(self, chat_id: str, mode: str) -> bool:
        """
        Switches a trader's risk profile to 'conservative', 'balanced', or 'aggressive'.
        Applies all preset rules (risk %, max slots, max leverage, tp R target, BE/trail thresholds).
        """
        mode_clean = str(mode).lower().strip()
        if mode_clean not in RISK_PROFILES:
            return False
        preset = RISK_PROFILES[mode_clean]
        c_id = str(chat_id).strip()
        traders = self.load_traders()
        if c_id not in traders:
            return False

        t = traders[c_id]
        t["risk_mode"] = mode_clean
        t["risk_per_trade_pct"] = float(preset["risk_per_trade_pct"])
        t["max_slots"] = int(preset["max_slots"])
        t["max_leverage"] = float(preset["max_leverage"])
        t["tp_r_target"] = float(preset["tp_r_target"])
        t["be_trigger_r"] = float(preset["be_trigger_r"])
        t["trail_activation_r"] = float(preset["trail_activation_r"])
        t["only_high_conviction"] = bool(preset["only_high_conviction"])

        # Sync legacy fields
        t["risk_profile"] = mode_clean.upper()
        t["leverage"] = float(preset["max_leverage"])
        t["max_concurrent_positions"] = int(preset["max_slots"])
        t["updated_at"] = time.time()
        return self.save_traders(traders)

    def update_trader_risk_settings(self, chat_id: str, **kwargs) -> bool:
        """Alias for update_trader_settings for risk and target settings."""
        return self.update_trader_settings(chat_id, **kwargs)

    def check_daily_drawdown_and_targets(self, chat_id: str, current_equity: float) -> Dict[str, Any]:
        """
        Evaluates daily drawdown limit and equity target for chat_id.
        Handles UTC day rollover: resets daily_start_equity and unpauses daily drawdown.
        Returns: {
            "daily_drawdown_paused": bool,
            "target_equity_reached": bool,
            "just_triggered_dd": bool,
            "just_reached_target": bool,
            "drawdown_pct": float
        }
        """
        c_id = str(chat_id).strip()
        traders = self.load_traders()
        if c_id not in traders:
            return {
                "daily_drawdown_paused": False,
                "target_equity_reached": False,
                "just_triggered_dd": False,
                "just_reached_target": False,
                "drawdown_pct": 0.0
            }

        t = traders[c_id]
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_reset_date = t.get("daily_reset_date", "")

        # Rollover to new UTC day if needed
        if daily_reset_date != today_utc:
            t["daily_reset_date"] = today_utc
            t["daily_start_equity"] = float(current_equity) if current_equity > 0 else 0.0
            t["daily_drawdown_paused"] = False

        daily_start = float(t.get("daily_start_equity", 0.0) or 0.0)
        if daily_start <= 0.0 and current_equity > 0.0:
            t["daily_start_equity"] = float(current_equity)
            daily_start = float(current_equity)

        max_dd_pct = float(t.get("max_daily_drawdown_pct", 4.0) or 4.0)
        curr_dd_pct = 0.0
        just_triggered_dd = False

        if daily_start > 0 and current_equity < daily_start:
            curr_dd_pct = round((daily_start - current_equity) / daily_start * 100.0, 2)
            if curr_dd_pct >= max_dd_pct:
                if not t.get("daily_drawdown_paused", False):
                    t["daily_drawdown_paused"] = True
                    just_triggered_dd = True

        target_eq = float(t.get("target_equity_usd", 0.0) or 0.0)
        just_reached_target = False
        if target_eq > 0.0 and current_equity >= target_eq:
            if not t.get("target_equity_reached", False):
                t["target_equity_reached"] = True
                just_reached_target = True

        t["updated_at"] = time.time()
        self.save_traders(traders)

        return {
            "daily_drawdown_paused": bool(t.get("daily_drawdown_paused", False)),
            "target_equity_reached": bool(t.get("target_equity_reached", False)),
            "just_triggered_dd": just_triggered_dd,
            "just_reached_target": just_reached_target,
            "drawdown_pct": curr_dd_pct
        }

    def get_trader_settings(self, chat_id: str) -> Dict[str, Any]:
        """Returns personal trading settings dictionary for trader, or defaults."""
        trader = self.get_trader(chat_id) or {}
        mode = trader.get("risk_mode", "balanced")
        preset = RISK_PROFILES.get(mode, RISK_PROFILES["balanced"])
        return {
            "risk_mode": mode,
            "risk_per_trade_pct": float(trader.get("risk_per_trade_pct", preset["risk_per_trade_pct"])),
            "max_slots": int(trader.get("max_slots", preset["max_slots"])),
            "max_leverage": float(trader.get("max_leverage", preset["max_leverage"])),
            "tp_r_target": float(trader.get("tp_r_target", preset["tp_r_target"])),
            "be_trigger_r": float(trader.get("be_trigger_r", preset["be_trigger_r"])),
            "trail_activation_r": float(trader.get("trail_activation_r", preset["trail_activation_r"])),
            "only_high_conviction": bool(trader.get("only_high_conviction", preset["only_high_conviction"])),
            "max_daily_drawdown_pct": float(trader.get("max_daily_drawdown_pct", 4.0)),
            "target_equity_usd": float(trader.get("target_equity_usd", 0.0)),
            "target_equity_reached": bool(trader.get("target_equity_reached", False)),
            "daily_drawdown_paused": bool(trader.get("daily_drawdown_paused", False)),
            "late_join_policy": trader.get("late_join_policy", "next_signal_only"),
            # Legacy fields
            "risk_profile": trader.get("risk_profile", mode.upper()),
            "leverage": float(trader.get("leverage", preset["max_leverage"])),
            "max_concurrent_positions": int(trader.get("max_concurrent_positions", preset["max_slots"])),
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
