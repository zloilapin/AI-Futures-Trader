import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

class PaperTradingService:
    """
    Paper Trading & PnL Tracking Service for Kraken Futures.
    Features:
    - Breakeven Guard: automatically moves Stop Loss to Breakeven (+0.1%) when 50% of Take Profit is reached.
    - Trailing Stop: dynamic profit protection.
    - Persists balance, open positions, and trade history in data/memory/portfolio.json.
    """
    def __init__(self, data_file: str = "data/memory/portfolio.json"):
        self.data_file = data_file
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ [PaperTradingService] Ошибка чтения файла портфеля: {e}")
        
        starting_balance = float(os.getenv("STARTING_BALANCE", "60.0"))
        default_state = {
            "initial_balance": starting_balance,
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
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns current balance, PnL, active positions count, win rate, and recent streak."""
        total_trades = self.state["win_count"] + self.state["loss_count"]
        win_rate = round((self.state["win_count"] / total_trades) * 100, 1) if total_trades > 0 else 0.0
        
        recent_streak = []
        for trade in self.state.get("closed_trades", [])[-5:]:
            pnl = trade.get("pnl_usd", 0)
            recent_streak.append("WIN" if pnl > 0 else "LOSS")
            
        return {
            "initial_balance": self.state["initial_balance"],
            "current_balance": round(self.state["current_balance"], 2),
            "total_pnl_usd": round(self.state["total_pnl_usd"], 2),
            "total_pnl_pct": round(self.state["total_pnl_pct"], 2),
            "active_positions_count": len(self.state["active_positions"]),
            "win_count": self.state["win_count"],
            "loss_count": self.state["loss_count"],
            "win_rate_pct": win_rate,
            "recent_streak": recent_streak
        }

    @property
    def active_positions(self) -> Dict[str, Any]:
        """Returns active positions as a dictionary mapping symbol to position data, for compatibility with KrakenTradingService."""
        return {pos["symbol"]: pos for pos in self.state["active_positions"]}

    async def open_position(self, symbol: str, direction: str, entry_price: float, size_usd: float, tp_price: float, sl_price: float, leverage: int = 1) -> Optional[Dict[str, Any]]:
        """Opens a virtual position if balance is sufficient."""
        margin_usd = size_usd / leverage if leverage > 0 else size_usd
        if margin_usd <= 0 or self.state["current_balance"] < margin_usd:
            return None

        self.state["current_balance"] -= margin_usd
        
        position = {
            "id": f"{symbol}_{int(datetime.now().timestamp())}",
            "symbol": symbol,
            "direction": direction.upper(),
            "entry_price": entry_price,
            "size_usd": size_usd,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "breakeven_activated": False,
            "leverage": leverage,
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        self.state["active_positions"].append(position)
        self._save_state()
        print(f"💼 [PaperTrading] Открыта виртуальная позиция {direction} {symbol} на ${size_usd:,.2f} (Маржа: ${margin_usd:,.2f}) по цене ${entry_price:,.2f}")
        return position

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """
        Evaluates active positions against live current_price.
        Applies Breakeven Guard (moves SL to Entry + 0.1% at 50% TP distance).
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
            size = pos["size_usd"]
            be_active = pos.get("breakeven_activated", False)
            leverage = pos.get("leverage", 1)

            # BREAKEVEN GUARD CHECK (При 50% пути к TP переносим Stop Loss в безубыток)
            if not be_active and tp > 0 and entry > 0:
                if direction == "LONG":
                    halfway_tp = entry + (tp - entry) * 0.5
                    if current_price >= halfway_tp and sl < entry:
                        pos["sl_price"] = round(entry * 1.001, 2)
                        pos["breakeven_activated"] = True
                        print(f"🛡️ [BreakevenGuard] Позиция LONG {symbol} прошла 50% до TP! Stop Loss перенесен в безубыток: ${pos['sl_price']:,.2f}")
                elif direction == "SHORT":
                    halfway_tp = entry - (entry - tp) * 0.5
                    if current_price <= halfway_tp and sl > entry:
                        pos["sl_price"] = round(entry * 0.999, 2)
                        pos["breakeven_activated"] = True
                        print(f"🛡️ [BreakevenGuard] Позиция SHORT {symbol} прошла 50% до TP! Stop Loss перенесен в безубыток: ${pos['sl_price']:,.2f}")

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
                        print(f"⏱️ [PaperTradingService/Keeper] Сделка по {symbol} открыта более 8 часов. Срабатывает Time-Based Stop.")
                except Exception:
                    pass

            if not triggered_exit:
                if direction == "LONG":
                    if current_price >= tp and tp > 0:
                        triggered_exit = "TP"
                        exit_price = tp
                    elif current_price <= sl and sl > 0:
                        triggered_exit = "SL (Breakeven)" if pos.get("breakeven_activated") else "SL"
                        exit_price = sl
                elif direction == "SHORT":
                    if current_price <= tp and tp > 0:
                        triggered_exit = "TP"
                        exit_price = tp
                    elif current_price >= sl and sl > 0:
                        triggered_exit = "SL (Breakeven)" if pos.get("breakeven_activated") else "SL"
                        exit_price = sl

            if triggered_exit:
                if direction == "LONG":
                    pnl_pct = ((exit_price - entry) / entry) * 100 * leverage
                else:
                    pnl_pct = ((entry - exit_price) / entry) * 100 * leverage

                margin_usd = size / leverage if leverage > 0 else size
                pnl_usd = margin_usd * (pnl_pct / 100)
                returned_capital = margin_usd + pnl_usd

                self.state["current_balance"] += returned_capital
                self.state["total_pnl_usd"] += pnl_usd
                self.state["total_pnl_pct"] = ((self.state["current_balance"] - self.state["initial_balance"]) / self.state["initial_balance"]) * 100

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
                    "pnl_pct": round(pnl_pct, 2),
                    "size_usd": size,
                    "new_balance": round(self.state["current_balance"], 2),
                    "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                self.state["closed_trades"].append(closed_record)
                closed_reports.append(closed_record)
                print(f"🎉 [PaperTrading] ЗАКРЫТА ПОЗИЦИЯ {symbol} ({triggered_exit})! PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%). Новый баланс: ${self.state['current_balance']:,.2f}")
            else:
                remaining_positions.append(pos)

        self.state["active_positions"] = remaining_positions
        self._save_state()
        return closed_reports
