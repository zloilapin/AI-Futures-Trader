import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class TelegramAgent(BaseAgent):
    """
    Communication agent responsible for generating crisp, highly structured trade signals 
    containing exact Symbol, Direction (LONG/SHORT), Entry Price, Take Profit (TP), Stop Loss (SL), 
    and Position Amount.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Telegram_Agent", logger, llm_client)

    def _escape_md(self, text: str) -> str:
        """Экранирует спецсимволы Markdown для Telegram."""
        escape_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in escape_chars:
            text = text.replace(char, f"\\{char}")
        return text

    def _format_price(self, price: float) -> str:
        if price < 0.1:
            return f"{price:.6f}".rstrip('0').rstrip('.')
        elif price < 10:
            return f"{price:.4f}".rstrip('0').rstrip('.')
        else:
            return f"{price:,.2f}"

    def format_signal(self, final_trade_data: Dict[str, Any]) -> str:
        symbol = final_trade_data.get("symbol", "UNKNOWN")
        ceo = final_trade_data.get("ceo_verdict", {})
        risk = final_trade_data.get("risk_verdict", {})

        decision = str(ceo.get("decision", "HOLD")).upper()
        conviction = ceo.get("conviction", 0)
        dir_emoji = "🟢 LONG" if decision == "LONG" else ("🔴 SHORT" if decision == "SHORT" else "⚪ HOLD")
        reasoning_en = ceo.get("reasoning_en", "")
        reasoning = self._escape_md(f"{reasoning_en}".strip())

        from core.config import config
        net_badge = f" [{config.NADO_NETWORK}]" if config.NADO_NETWORK else ""
        
        if decision == "HOLD":
            return (
                f"⏸️ *MARKET UPDATE | NADO DEX{net_badge}*\n\n"
                f"🪙 *Asset / Монета:* `{symbol}`\n"
                f"📊 *Direction / Направление:* {dir_emoji}\n"
                f"🔥 *AI Conviction / Уверенность:* `{conviction}%`\n\n"
                f"📝 *Analysis / Аналитика:*\n{reasoning}"
            )

        entry_price = risk.get("entry_price", 0)
        tp_price = risk.get("take_profit_price", 0)
        tp_pct = risk.get("take_profit_pct", 0)
        sl_price = risk.get("stop_loss_price", 0)
        sl_pct = risk.get("stop_loss_pct", 0)
        notional_usd = risk.get("notional_size_usd", 0)
        pos_pct = risk.get("position_size_pct", 0)
        rr_ratio = risk.get("risk_reward_ratio", 0)
        
        primary_conviction = ceo.get("primary_conviction", conviction)
        escalated = ceo.get("escalated", False)
        
        if escalated:
            conv_str = f"Llama {primary_conviction}% | Gemini {conviction}% (Escalated)"
        else:
            conv_str = f"Llama {primary_conviction}% (Direct)"

        message = (
            f"🚀 *TRADE SIGNAL | NADO DEX{net_badge}*\n\n"
            f"🪙 *Asset / Монета:* `{symbol}`\n"
            f"📊 *Direction / Направление:* {dir_emoji}\n"
            f"🔥 *AI Conviction:* `{conv_str}`\n"
            f"💰 *Position / Сумма сделки:* `${notional_usd:,.2f}` ({pos_pct}%)\n"
            f"🎯 *Entry / Цена входа:* `${self._format_price(entry_price)}`\n\n"
            f"🟢 *Take Profit (TP):* `${self._format_price(tp_price)}` (+{tp_pct}%)\n"
            f"🔴 *Stop Loss (SL):* `${self._format_price(sl_price)}` (-{sl_pct}%)\n"
            f"⚖️ *Risk/Reward:* `{rr_ratio}`\n\n"
            f"📝 *Analysis / Аналитика:*\n{reasoning}"
        )
        return message

    async def analyze(self, final_trade_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Formats trade execution alert using strict trading signal layout.
        """
        self.logger.info(f"[{self.name}] Формирование четкого сигнала по шаблону (Монета, LONG/SHORT, Entry, TP/SL, Сумма)...")
        msg = self.format_signal(final_trade_data)
        return {"message": msg}
