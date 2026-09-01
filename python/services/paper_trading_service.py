from core.interfaces import BaseTradingService
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

class PaperTradingService(BaseTradingService):
    """
    Paper Trading & PnL Tracking Service for Kraken Futures.
    Features:
    - Trailing Stop: activates at +1.5% profit, then trails by 1.5% (identical to KrakenTradingService).
    - Taker Fee: 0.05% deducted on both entry and exit (deducted from PnL at close time).
    - Time-Based Stop: positions older than 8 hours are auto-closed.
    - Persists balance, open positions, and trade history in data/memory/portfolio.json.
    """
    TAKER_FEE_PCT = 0.0005  # 0.05% per side

    def __init__(self, data_file: str = "data/memory/portfolio.json", logger=None):
        self.data_file = data_file
        self.logger = logger
        self.state = self._load_state()
        self.watcher_running = False
        self._watcher_task = None

    def _log(self, msg: str, level: str = "info"):
        if self.logger:
            if level == "error": self.logger.error(msg)
            elif level == "warning": self.logger.warning(msg)
            else: self.logger.info(msg)
        else:
            self._log(msg)

    def _load_state(self) -> Dict[str, Any]:
        from core.state_store import StateStore
        state = StateStore.load(self.data_file)
        if state:
            return state
        
        starting_balance = float(os.getenv("STARTING_BALANCE", "60.0"))
        default_state = {
            "initial_balance": starting_balance,
            "initial_deposit": starting_balance,
            "net_transfers": 0.0,
            "current_balance": starting_balance,
            "active_positions": [],
            "closed_trades": [],
            "total_pnl_usd": 0.0,
            "total_pnl_pct": 0.0,
            "win_count": 0,
            "loss_count": 0
        }
        self._save_state_dict(default_state)
        return default_state

    def _save_state(self):
        self._save_state_dict(self.state)

    def _save_state_dict(self, data: Dict[str, Any]):
        from core.state_store import StateStore
        StateStore.save(self.data_file, data)

    async def _fetch_current_price(self, symbol: str) -> float:
        prices = await self._fetch_prices_batch([symbol])
        return prices.get(symbol, 0.0)

    async def _fetch_prices_batch(self, symbols: List[str]) -> Dict[str, float]:
        import aiohttp
        results = {sym: 0.0 for sym in symbols}
        if not symbols:
            return results
            
        url = "https://futures.kraken.com/derivatives/api/v3/tickers"
        try:
                            suffix = f"_{base}USD".upper()
                            
                            for t in tickers:
                                s = t.get("symbol", "").upper()
                                if s.endswith(suffix):
                                    results[sym] = float(t.get("last", 0))
                                    break
        except Exception as e:
            self._log(f"⚠️ [PaperTrading] Ошибка загрузки цен (batch): {e}")
            
        return results

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns current balance, PnL, active positions count, win rate, and recent streak."""
        total_trades = self.state["win_count"] + self.state["loss_count"]
        win_rate = round((self.state["win_count"] / total_trades) * 100, 1) if total_trades > 0 else 0.0
        
        recent_streak = []
        for trade in self.state.get("closed_trades", [])[-5:]:
            pnl = trade.get("pnl_usd", 0)
            recent_streak.append("WIN" if pnl > 0 else "LOSS")
            
        base_capital = self.state.get("initial_deposit", self.state.get("initial_balance", 0.0)) + self.state.get("net_transfers", 0.0)
        account_roi_pct = ((self.state["current_balance"] - base_capital) / base_capital * 100) if base_capital > 0 else 0.0
        
        unrealized_pnl = 0.0
        import asyncio
        import asyncio
        if self.state["active_positions"]:
            symbols = list(set([pos["symbol"] for pos in self.state["active_positions"]]))
            prices_dict = await self._fetch_prices_batch(symbols)
            for pos in self.state["active_positions"]:
                price = prices_dict.get(pos["symbol"], 0.0)
                if price > 0:
                    entry = pos["entry_price"]
                    notional_usd = pos["notional_usd"]
                    if pos["direction"] == "LONG":
                        unrealized_pnl += (price - entry) / entry * notional_usd
                    else:
                        unrealized_pnl += (entry - price) / entry * notional_usd
                        
        used_margin = sum(pos.get("margin_usd", 0) for pos in self.state["active_positions"])
        available_margin = self.state["current_balance"] - used_margin
            
        return {
            "initial_balance": base_capital,
            "current_balance": round(self.state["current_balance"] + unrealized_pnl, 2), # Equity
            "total_pnl_usd": round(self.state["total_pnl_usd"], 2),
            "total_pnl_pct": round(account_roi_pct, 2),
            "unrealized_pnl_usd": round(unrealized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "available_margin": round(available_margin, 2),
            "used_margin": round(used_margin, 2),
            "active_positions_count": len(self.state["active_positions"]),
            "win_rate_pct": round(win_rate, 2),
            "win_count": self.state["win_count"],
            "loss_count": self.state["loss_count"],
            "recent_streak": recent_streak,
            "roi_pct": round(account_roi_pct, 2)
        }

    @property
    def active_positions(self) -> Dict[str, Any]:
        """Returns active positions as a dictionary mapping symbol to position data, for compatibility with KrakenTradingService."""
        return {pos["symbol"]: pos for pos in self.state["active_positions"]}
        
    async def get_active_positions(self) -> list:
        return self.state["active_positions"]
        
    async def sync_with_exchange(self) -> None:
        pass

    async def start_background_watcher(self, tg_sender=None):
        """Starts a background loop to check positions every 15 seconds."""
        if self.watcher_running:
            return
            
        self.watcher_running = True
        self._log("👀 [PaperTrading] Фоновый реал-тайм мониторинг позиций запущен (интервал: 15 сек).")
        
        import asyncio
        while self.watcher_running:
            try:
                if self.state.get("active_positions"):
                    symbols = list(set([pos["symbol"] for pos in self.state["active_positions"]]))
                    symbols = list(set([pos["symbol"] for pos in self.state["active_positions"]]))
                    
                    prices_dict = await self._fetch_prices_batch(symbols)
                    
                    for sym in symbols:
                        price = prices_dict.get(sym, 0.0)
                        if price > 0:
                            closed_reports = await self.check_and_update_positions(sym, price)
                            
                            if closed_reports and tg_sender:
                                for closed in closed_reports:
                                    pnl_emoji = "🎉" if closed["pnl_usd"] >= 0 else "🔻"
                                    closed_msg = (
                                        f"{pnl_emoji} *TRADE CLOSED / СДЕЛКА ЗАКРЫТА ({closed['triggered_by']})*\n\n"
                                        f"🪙 *Asset / Монета:* `{closed['symbol']}`\n"
                                        f"📊 *Direction / Направление:* `{closed['direction']}`\n"
                                        f"🎯 *Entry / Вход:* `${closed['entry_price']:,.2f}` ➔ *Exit / Выход:* `${closed['exit_price']:,.2f}`\n"
                                        f"💰 *PnL:* `${closed['pnl_usd']:,.2f}` (ROI: {closed.get('roi_pct', 0):+.2f}%)\n"
                                    )
                                    print(f"\n--- [ФОНОВЫЙ МОНИТОРИНГ] ЗАКРЫТИЕ ПОЗИЦИИ В TELEGRAM [{sym}] ---")
                                    print(closed_msg)
                                    print("--------------------------------------------")
                                    await tg_sender.send_message(closed_msg)
                                    await tg_sender.broadcast_to_channel(closed_msg)
                                    
                                    # We don't have reflector_agent here, but memory is saved locally anyway.
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"❌ [PaperTrading] Ошибка в фоновом мониторинге: {e}")
                await asyncio.sleep(15)

    async def _close_exchange_async(self):
        """Cleanup method called by main.py upon exit."""
        self.watcher_running = False

    async def open_position(self, symbol: str, direction: str, entry_price: float, notional_usd: float, tp_price: float, sl_price: float, leverage: int = 1) -> Optional[Dict[str, Any]]:
        """Opens a virtual position if balance is sufficient."""
        margin_usd = notional_usd / leverage if leverage > 0 else notional_usd
        # We only check if we have enough equity/free margin. We don't deduct it from current_balance (which tracks Wallet Balance/Equity)
        if margin_usd <= 0 or self.state["current_balance"] < margin_usd:
            return None
            
        size_base = notional_usd / entry_price if entry_price > 0 else 0.0

        position = {
            "id": f"{symbol}_{int(datetime.now().timestamp())}",
            "symbol": symbol,
            "direction": direction.upper(),
            "entry_price": entry_price,
            "notional_usd": notional_usd,        # NOTIONAL
            "size_base": size_base,      # BASE AMOUNT (for parity with KrakenTradingService)
            "leverage": leverage,        # LEVERAGE
            "margin_usd": margin_usd,    # MARGIN
            "tp_price": tp_price,
            "sl_price": sl_price,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Note: fees are NOT deducted from balance here.
        # Both entry and exit fees are deducted from PnL at close time,
        # so that total_pnl_usd accurately reflects net profit after all fees.

        self.state["active_positions"].append(position)
        self._save_state()
        entry_fee = notional_usd * self.TAKER_FEE_PCT
        self._log(f"💼 [PaperTrading] Открыта виртуальная позиция {direction} {symbol} на ${notional_usd:,.2f} (Маржа: ${margin_usd:,.2f}) по цене ${entry_price:,.2f}")
        self._log(f"💸 [PaperTrading] Комиссия за открытие (0.05%): ${entry_fee:.4f} — будет учтена при закрытии.")
        return position

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """
        Evaluates active positions against live current_price.
        Applies Trailing Stop logic identical to KrakenTradingService (1.5% activation, 1.5% trail).
        Returns a list of position closure reports if TP or SL is triggered.
        """
        closed_reports = []
        remaining_positions = []

        for pos in self.state["active_positions"]:
            if pos["symbol"] != symbol:
                remaining_positions.append(pos)
                continue

            direction = pos["direction"]
            entry = pos["entry_price"]
            tp = pos["tp_price"]
            sl = pos["sl_price"]
            notional_usd = pos["notional_usd"]
            leverage = pos.get("leverage", 1)

            # Update Extremes
            if current_price > pos.get("highest_price", entry):
                pos["highest_price"] = current_price
            if current_price < pos.get("lowest_price", entry):
                pos["lowest_price"] = current_price

            highest = pos["highest_price"]
            lowest = pos["lowest_price"]
            
            # Trailing Stop: activates at +1.5% profit, then trails by 1.5%
            trail_pct = 0.015
            activation_pct = 0.015

            # Breakeven Guard (SL -> Entry at 50% TP)
            if direction == "LONG":
                halfway_to_tp = entry + ((tp - entry) * 0.5)
                if current_price >= halfway_to_tp and pos["sl_price"] < entry:
                    pos["sl_price"] = entry
                    self._log(f"🛡️ [Breakeven Guard] {symbol} Цена прошла 50% до TP. SL переведен в безубыток: {entry:.4f}")
                
                # Check Trailing Stop
                if (highest - entry) / entry >= activation_pct:
                    new_sl = highest * (1 - trail_pct)
                    if new_sl > pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        self._log(f"📈 [Trailing Stop] {symbol} SL подтянут до: {new_sl:.4f}")
            else:
                halfway_to_tp = entry - ((entry - tp) * 0.5)
                if current_price <= halfway_to_tp and pos["sl_price"] > entry:
                    pos["sl_price"] = entry
                    self._log(f"🛡️ [Breakeven Guard] {symbol} Цена прошла 50% до TP. SL переведен в безубыток: {entry:.4f}")
                
                # Check Trailing Stop
                if (entry - lowest) / entry >= activation_pct:
                    new_sl = lowest * (1 + trail_pct)
                    if new_sl < pos["sl_price"] or pos["sl_price"] == 0:
                        pos["sl_price"] = new_sl
                        self._log(f"📉 [Trailing Stop] {symbol} SL подтянут до: {new_sl:.4f}")

            sl = pos["sl_price"]
            triggered_exit = None
            exit_price = current_price
            
            # TTL Check (8 hours)
            ttl_seconds = 8 * 3600
            opened_at_str = pos.get("opened_at")
            if opened_at_str:
                try:
                    opened_dt = datetime.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - opened_dt).total_seconds() > ttl_seconds:
                        triggered_exit = "TIME_STOP"
                        self._log(f"⏱️ [PaperTradingService/Keeper] Сделка по {symbol} открыта более 8 часов. Срабатывает Time-Based Stop.")
                except Exception:
                    pass

            if not triggered_exit:
                if direction == "LONG":
                    if current_price >= tp and tp > 0:
                        triggered_exit = "TP"
                        exit_price = tp
                    elif current_price <= sl and sl > 0:
                        triggered_exit = "SL"
                        exit_price = sl
                elif direction == "SHORT":
                    if current_price <= tp and tp > 0:
                        triggered_exit = "TP"
                        exit_price = tp
                    elif current_price >= sl and sl > 0:
                        triggered_exit = "SL"
                        exit_price = sl

            if triggered_exit:
                if direction == "LONG":
                    notional_pnl_pct = ((exit_price - entry) / entry) * 100
                else:
                    notional_pnl_pct = ((entry - exit_price) / entry) * 100

                margin_usd = pos.get("margin_usd", notional_usd / leverage if leverage > 0 else notional_usd)
                
                # Gross PnL (before fees)
                gross_pnl_usd = notional_usd * (notional_pnl_pct / 100)
                
                # Deduct BOTH entry and exit fees (Taker 0.05% each side)
                total_fees = notional_usd * self.TAKER_FEE_PCT * 2  # entry + exit
                pnl_usd = gross_pnl_usd - total_fees
                
                roi_pct = (pnl_usd / margin_usd) * 100 if margin_usd > 0 else 0

                # Realized PnL (net of all fees) is added to the wallet balance
                self.state["current_balance"] += pnl_usd
                self.state["total_pnl_usd"] += pnl_usd
                
                base_cap = self.state.get("initial_deposit", self.state.get("initial_balance", 0.0)) + self.state.get("net_transfers", 0.0)
                self.state["total_pnl_pct"] = ((self.state["current_balance"] - base_cap) / base_cap) * 100 if base_cap > 0 else 0.0

                if pnl_usd >= 0:
                    self.state["win_count"] += 1
                else:
                    self.state["loss_count"] += 1

                closed_record = {
                    "position_id": pos["id"],
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "triggered_by": triggered_exit,
                    "pnl_usd": round(pnl_usd, 2),
                    "pnl_pct": round(notional_pnl_pct, 2),
                    "roi_pct": round(roi_pct, 2),
                    "margin_usd": round(margin_usd, 2),
                    "leverage": leverage,
                    "notional_usd": notional_usd,
                    "fees_usd": round(total_fees, 4),
                    "new_balance": round(self.state["current_balance"], 2),
                    "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                self.state["closed_trades"].append(closed_record)
                closed_reports.append(closed_record)
                self._log(f"🎉 [PaperTrading] ЗАКРЫТА ПОЗИЦИЯ {symbol} ({triggered_exit})! PnL: ${pnl_usd:+.2f} (ROI: {roi_pct:+.2f}%, Fees: ${total_fees:.4f}). Новый баланс: ${self.state['current_balance']:,.2f}")
            else:
                remaining_positions.append(pos)

        self.state["active_positions"] = remaining_positions
        self._save_state()
        return closed_reports

    async def force_close_position(self, symbol: str) -> tuple:
        """Manually forces a close via Telegram button. Mirrors KrakenTradingService.force_close_position."""
        target_pos = None
        target_idx = -1
        for i, pos in enumerate(self.state["active_positions"]):
            if pos["symbol"] == symbol:
                target_pos = pos
                target_idx = i
                break

        if target_pos is None:
            return False, "Позиция не найдена"

        direction = target_pos["direction"]
        entry_price = target_pos["entry_price"]
        notional_usd = target_pos["notional_usd"]
        leverage = target_pos.get("leverage", 1)
        margin_usd = target_pos.get("margin_usd", notional_usd / leverage if leverage > 0 else notional_usd)

        # Запрашиваем реальную цену, чтобы не закрывать в ноль
        exit_price = await self._fetch_current_price(symbol)
        if exit_price <= 0:
            self._log(f"⚠️ [PaperTrading] Не удалось получить цену для закрытия {symbol}. Используем цену входа.")
            exit_price = entry_price

        # Calculate PnL
        if direction == "LONG":
            gross_pnl = (exit_price - entry_price) / entry_price * notional_usd
        else:
            gross_pnl = (entry_price - exit_price) / entry_price * notional_usd

        total_fees = notional_usd * self.TAKER_FEE_PCT * 2
        pnl = gross_pnl - total_fees

        if pnl > 0:
            self.state["win_count"] += 1
        else:
            self.state["loss_count"] += 1

        self.state["total_pnl_usd"] += pnl
        self.state["current_balance"] += pnl

        base_cap = self.state.get("initial_deposit", self.state.get("initial_balance", 0.0)) + self.state.get("net_transfers", 0.0)
        self.state["total_pnl_pct"] = ((self.state["current_balance"] - base_cap) / base_cap) * 100 if base_cap > 0 else 0.0

        closed_record = {
            "position_id": target_pos["id"],
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "triggered_by": "MANUAL_CLOSE",
            "pnl_usd": round(pnl, 2),
            "fees_usd": round(total_fees, 4),
            "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.state["closed_trades"].append(closed_record)

        # Remove from active positions
        self.state["active_positions"].pop(target_idx)
        self._save_state()

        self._log(f"🔴 [PaperTrading] Принудительное закрытие {symbol}. PnL: ${pnl:+.2f}")
        return True, {"pnl_usd": pnl, "exit_price": exit_price, "is_virtual": False}
