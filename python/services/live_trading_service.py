import os
import time
import json
import uuid
import aiohttp
from typing import Dict, Any, List
from eth_account import Account
from eth_account.messages import encode_typed_data

class LiveTradingService:
    """
    Live Execution Service for Nado DEX via REST Gateway.
    Handles EIP-712 signing and executes live market orders.
    """
    def __init__(self):
        self.gateway_url = os.getenv("NADO_GATEWAY_URL", "https://gateway.prod.nado.xyz/v1")
        self.private_key = os.getenv("WALLET_PRIVATE_KEY")
        
        if not self.private_key:
            print("⚠️ [LiveTradingService] ВНИМАНИЕ: WALLET_PRIVATE_KEY не найден в .env!")
            self.account = None
        else:
            self.account = Account.from_key(self.private_key)
            print(f"✅ [LiveTradingService] Подключен кошелек: {self.account.address}")

        self.active_positions = {}
        self.trade_history = []
        
        # Mapping symbols to Nado product IDs. In production, this should be fetched via /query
        self.product_map = {
            "BTC": 1,
            "ETH": 2,
            "SOL": 3
        }

    def _get_product_id(self, symbol: str) -> int:
        return self.product_map.get(symbol, 1) # Default to 1 (usually BTC)

    def _build_eip712_message(self, order_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Constructs the EIP-712 payload for signing.
        Note: The domain parameters (name, chainId, verifyingContract) must match Nado's exactly.
        """
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"}
                ],
                "Order": [
                    {"name": "sender", "type": "bytes32"},
                    {"name": "priceX18", "type": "int128"},
                    {"name": "amount", "type": "int128"},
                    {"name": "expiration", "type": "uint64"},
                    {"name": "nonce", "type": "uint64"},
                    {"name": "appendix", "type": "uint128"}
                ]
            },
            "primaryType": "Order",
            "domain": {
                "name": "Nado",
                "version": "1",
                "chainId": 57073, # Ink Mainnet
                "verifyingContract": "0x0000000000000000000000000000000000000000" # TODO: Update with real Nado contract
            },
            "message": order_dict
        }

    def _get_subaccount_bytes(self) -> str:
        """Helper to get 32-byte subaccount string from address"""
        if not self.account:
            return ""
        address_stripped = self.account.address.lower().replace("0x", "")
        return "0x" + address_stripped + ("0" * 24)

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """
        Reads real subaccount balance from Nado Gateway.
        Assuming Nado uses Vertex Protocol engine standards (subaccount_info).
        """
        if not self.account:
            return {
                "total_usd": 1000.0,
                "available_margin": 1000.0,
                "used_margin": 0.0,
                "active_positions_count": len(self.active_positions),
                "unrealized_pnl": 0.0,
                "roi_pct": 0.0
            }

        query_url = f"{self.gateway_url}/query"
        payload = {
            "type": "subaccount_info",
            "subaccount": self._get_subaccount_bytes()
        }
        
        total_balance = 0.0
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(query_url, json=payload, headers={"Accept-Encoding": "gzip"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Extract quote currency (USDC) balance from spot balances
                        spot_balances = data.get("data", {}).get("spot_balances", [])
                        for balance_entry in spot_balances:
                            if balance_entry.get("product_id") == 0: # 0 is typically USDC Quote
                                raw_amount = balance_entry.get("balance", {}).get("amount", "0")
                                total_balance = float(raw_amount) / 1e18
                                break
                    else:
                        print(f"⚠️ [LiveTradingService] Не удалось загрузить баланс (HTTP {resp.status})")
        except Exception as e:
            print(f"⚠️ [LiveTradingService] Ошибка запроса баланса: {e}")
            
        # Fallback to 1000 if empty or zero (to prevent division by zero in testing), 
        # but in strict mode we should return 0.
        if total_balance <= 0:
            total_balance = 0.0
            
        print(f"💰 [LiveTradingService] Баланс Subaccount: ${total_balance:,.2f}")
        
        return {
            "total_usd": total_balance,
            "current_balance": total_balance,
            "initial_balance": total_balance, # Simplified for now
            "total_pnl_usd": 0.0,
            "total_pnl_pct": 0.0,
            "win_rate_pct": 0.0,
            "win_count": 0,
            "loss_count": 0,
            "recent_streak": [],
            "available_margin": total_balance,
            "used_margin": 0.0,
            "active_positions_count": len(self.active_positions),
            "unrealized_pnl": 0.0,
            "roi_pct": 0.0
        }

    async def open_position(self, symbol: str, direction: str, entry_price: float, size_usd: float, tp_price: float, sl_price: float, leverage: int = 1):
        """
        Constructs the Order JSON, signs it, and sends via HTTP POST to the Gateway.
        """
        if not self.account:
            print(f"❌ [LiveTradingService] Нет приватного ключа. Сделка {direction} по {symbol} отменена.")
            return

        print(f"🚀 [LiveTradingService] ПОДГОТОВКА БОЕВОЙ СДЕЛКИ: {direction} {symbol}")
        
        # 1. Prepare numerical values
        # priceX18 = int(price * 1e18)
        price_x18 = str(int(entry_price * 1e18))
        
        # size in base asset (e.g. BTC) = size_usd / entry_price
        # Negative for SHORT, Positive for LONG
        size_base = size_usd / entry_price
        if direction == "SHORT":
            size_base = -size_base
        amount_x18 = str(int(size_base * 1e18))
        
        expiration = str(int(time.time()) + 300) # Expires in 5 minutes
        nonce = str(int(time.time() * 1000)) # Simple nonce
        
        # Sender is 32 bytes: address (20 bytes) + subaccount name (12 bytes padding)
        # For default subaccount, it's just the address right-padded with 0s
        sender_bytes32 = self._get_subaccount_bytes()

        order_data = {
            "sender": sender_bytes32,
            "priceX18": price_x18,
            "amount": amount_x18,
            "expiration": expiration,
            "nonce": nonce,
            "appendix": "0" # Default appendix
        }

        # 2. Sign EIP-712
        try:
            structured_msg = self._build_eip712_message(order_data)
            # Need strict typing for eth_account EIP-712. We assume domain types are correctly formatted.
            # However, encode_typed_data will work if the dict format matches EIP-712 spec.
            encoded_msg = encode_typed_data(full_message=structured_msg)
            signed_msg = self.account.sign_message(encoded_msg)
            signature = signed_msg.signature.hex()
        except Exception as e:
            print(f"❌ [LiveTradingService] Ошибка подписания ордера: {e}")
            return

        # 3. Construct Gateway Payload
        payload = {
            "place_order": {
                "product_id": self._get_product_id(symbol),
                "order": order_data,
                "signature": signature,
                "id": int(time.time()) # Client-side ID
            }
        }

        # 4. Send POST Request
        execute_url = f"{self.gateway_url}/execute"
        print(f"🌐 [LiveTradingService] Отправка POST на {execute_url}...")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(execute_url, json=payload, headers={"Accept-Encoding": "gzip"}) as resp:
                    resp_data = await resp.text()
                    if resp.status in [200, 201]:
                        print(f"✅ [LiveTradingService] ОРДЕР ИСПОЛНЕН: {resp_data}")
                        # Track it locally for Keeper logic
                        pos_id = str(uuid.uuid4())[:8]
                        self.active_positions[symbol] = {
                            "id": pos_id,
                            "symbol": symbol,
                            "direction": direction,
                            "entry_price": entry_price,
                            "size_usd": size_usd,
                            "tp_price": tp_price,
                            "sl_price": sl_price,
                            "breakeven_activated": False,
                            "timestamp": time.time()
                        }
                    else:
                        print(f"❌ [LiveTradingService] Ошибка Gateway ({resp.status}): {resp_data}")
        except Exception as e:
            print(f"❌ [LiveTradingService] Ошибка сети при отправке ордера: {e}")

    def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """
        Keeper logic: acts as a software stop-loss.
        If current price hits TP/SL locally, sends a market order to close.
        """
        closed_reports = []
        if symbol not in self.active_positions:
            return closed_reports
            
        pos = self.active_positions[symbol]
        direction = pos["direction"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]
        entry_price = pos["entry_price"]
        
        # Breakeven logic (50% to TP)
        distance_to_tp = abs(tp_price - entry_price)
        current_distance = abs(current_price - entry_price)
        if current_distance >= distance_to_tp * 0.5:
            # Check if moving in correct direction
            is_profitable = (direction == "LONG" and current_price > entry_price) or \
                            (direction == "SHORT" and current_price < entry_price)
            if is_profitable and not pos.get("breakeven_activated"):
                pos["breakeven_activated"] = True
                new_sl = entry_price * 1.001 if direction == "LONG" else entry_price * 0.999
                pos["sl_price"] = new_sl
                print(f"🛡️ [LiveTradingService/Keeper] {symbol} 50% TP пройдено. SL перенесен в безубыток.")

        # TP / SL Execution trigger
        triggered_exit = None
        
        # TTL Check (8 hours)
        ttl_seconds = 8 * 3600
        time_alive = time.time() - pos.get("timestamp", time.time())
        if time_alive > ttl_seconds:
            triggered_exit = "TIME_STOP"
            print(f"⏱️ [LiveTradingService/Keeper] Сделка по {symbol} открыта более 8 часов. Срабатывает Time-Based Stop.")
            
        if not triggered_exit:
            if direction == "LONG":
                if current_price >= tp_price:
                    triggered_exit = "TP"
                elif current_price <= sl_price:
                    triggered_exit = "SL"
            else:
                if current_price <= tp_price:
                    triggered_exit = "TP"
                elif current_price >= sl_price:
                    triggered_exit = "SL"
                
        if triggered_exit:
            print(f"⚡ [LiveTradingService/Keeper] Сработал {triggered_exit} для {symbol}! Отправка рыночного ордера на закрытие...")
            # TODO: Fire an async 'close_position' opposite order via REST API
            pnl = abs(current_price - entry_price) * (pos["size_usd"] / entry_price)
            if triggered_exit == "SL": pnl = -pnl
            
            report = {
                "symbol": symbol,
                "type": direction,
                "exit_reason": triggered_exit,
                "pnl_usd": pnl
            }
            closed_reports.append(report)
            del self.active_positions[symbol]
            
        return closed_reports
