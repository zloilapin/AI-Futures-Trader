import os
import time
import asyncio
import uuid
import ccxt.async_support as ccxt
from typing import Dict, Any, List

class KrakenTradingService:
    """
    Live Execution Service for Kraken Futures via CCXT.
    Handles authenticated API requests, fetching balances, and opening/closing positions.
    """
    def __init__(self):
        self.api_key = os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        self.exchange = ccxt.krakenfutures({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
        })
        
        if not self.api_key or not self.api_secret:
            print("⚠️ [KrakenTradingService] ВНИМАНИЕ: KRAKEN_API_KEY или KRAKEN_API_SECRET не найдены в .env!")
        else:
            print("✅ [KrakenTradingService] Учетные данные Kraken Futures инициализированы.")

        # Local tracker for active positions (Keeper Logic)
        self.active_positions = {}
        
    async def _close_exchange_async(self):
        """Clean up CCXT session"""
        if self.exchange:
            await self.exchange.close()

    def _format_symbol(self, symbol: str) -> str:
        """
        Converts generic symbol (e.g. BTC) to Kraken Futures Perpetual symbol format.
        Usually ccxt handles standard formats, e.g., 'BTC/USD:USD' or similar for linear perps.
        """
        base = symbol.upper()
        if base == "BTC":
            base = "BTC"
        return f"{base}/USD:USD"

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """
        Reads real USD margin balance from Kraken Futures.
        """
        total_balance = 0.0
        
        if self.api_key and self.api_secret:
            try:
                # Fetch balance
                balance = await self.exchange.fetch_balance()
                # Assuming USD is the main margin asset
                if 'USD' in balance:
                    total_balance = float(balance['USD'].get('total', 0.0))
                else:
                    # Fallback to sum of all or free
                    total_balance = float(balance.get('total', {}).get('USD', 0.0))
                
                print(f"💰 [KrakenTradingService] Баланс аккаунта Kraken Futures: ${total_balance:,.2f}")
            except Exception as e:
                print(f"⚠️ [KrakenTradingService] Ошибка запроса баланса: {e}")
                # Fallback so bot doesn't crash during debugging
                total_balance = 0.0
        else:
            print("⚠️ [KrakenTradingService] API ключи не настроены, возвращаем 0.0 баланс.")

        return {
            "total_usd": total_balance,
            "current_balance": total_balance,
            "initial_balance": total_balance,
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

    async def _execute_market_order(self, symbol: str, direction: str, size_base: float):
        """Helper to execute order via CCXT"""
        formatted_symbol = self._format_symbol(symbol)
        side = 'buy' if direction == 'LONG' else 'sell'
        
        try:
            print(f"🌐 [KrakenTradingService] Отправка MARKET {side.upper()} ордера {size_base} {formatted_symbol}...")
            order = await self.exchange.create_market_order(formatted_symbol, side, size_base)
            print(f"✅ [KrakenTradingService] ОРДЕР ИСПОЛНЕН! ID: {order.get('id')}")
            return order
        except Exception as e:
            print(f"❌ [KrakenTradingService] Ошибка сети при отправке ордера: {e}")
            return None

    async def open_position(self, symbol: str, direction: str, entry_price: float, size_usd: float, tp_price: float, sl_price: float, leverage: int = 1):
        """
        Calculates position size in base currency and opens a Market order via Kraken Futures API.
        Registers the position in local Keeper for SL/TP tracking.
        """
        if not self.api_key:
            print(f"❌ [KrakenTradingService] Нет API ключей. Сделка {direction} по {symbol} отменена.")
            return

        print(f"🚀 [KrakenTradingService] ПОДГОТОВКА БОЕВОЙ СДЕЛКИ: {direction} {symbol}")
        
        size_base = size_usd / entry_price
        
        # Execute trade
        order_result = await self._execute_market_order(symbol, direction, size_base)
        
        if order_result:
            pos_id = str(uuid.uuid4())[:8]
            self.active_positions[symbol] = {
                "id": pos_id,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "size_usd": size_usd,
                "size_base": size_base,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "breakeven_activated": False,
                "timestamp": time.time()
            }

    def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """
        Keeper logic: acts as a software stop-loss.
        If current price hits TP/SL locally, sends a market order to close.
        Since this method is called synchronously in main loop, we dispatch closing orders via asyncio.create_task.
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
            is_profitable = (direction == "LONG" and current_price > entry_price) or \
                            (direction == "SHORT" and current_price < entry_price)
            if is_profitable and not pos.get("breakeven_activated"):
                pos["breakeven_activated"] = True
                new_sl = entry_price * 1.001 if direction == "LONG" else entry_price * 0.999
                pos["sl_price"] = new_sl
                print(f"🛡️ [KrakenTradingService/Keeper] {symbol} 50% TP пройдено. SL перенесен в безубыток.")

        # TP / SL Execution trigger
        triggered_exit = None
        
        # TTL Check (8 hours)
        ttl_seconds = 8 * 3600
        time_alive = time.time() - pos.get("timestamp", time.time())
        if time_alive > ttl_seconds:
            triggered_exit = "TIME_STOP"
            print(f"⏱️ [KrakenTradingService/Keeper] Сделка по {symbol} открыта более 8 часов. Срабатывает Time-Based Stop.")
            
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
            print(f"⚡ [KrakenTradingService/Keeper] Сработал {triggered_exit} для {symbol}! Отправка ордера на закрытие...")
            
            # Close position asynchronously
            close_direction = "SHORT" if direction == "LONG" else "LONG"
            asyncio.create_task(self._execute_market_order(symbol, close_direction, pos["size_base"]))
            
            pnl = abs(current_price - entry_price) * (pos["size_usd"] / entry_price)
            if triggered_exit == "SL": pnl = -pnl
            
            report = {
                "symbol": symbol,
                "direction": direction,
                "triggered_by": triggered_exit,
                "entry_price": entry_price,
                "exit_price": current_price,
                "pnl_usd": pnl,
                "pnl_pct": (pnl / pos["size_usd"]) * 100 if pos["size_usd"] > 0 else 0,
                "new_balance": 0 # Local balance tracking omitted for simplicity in live bot, will fetch from exchange next cycle
            }
            closed_reports.append(report)
            del self.active_positions[symbol]
            
        return closed_reports
