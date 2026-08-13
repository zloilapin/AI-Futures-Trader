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

    def format_signal(self, final_trade_data: Dict[str, Any]) -> str:
        symbol = final_trade_data.get("symbol", "UNKNOWN")
        ceo = final_trade_data.get("ceo_verdict", {})
        risk = final_trade_data.get("risk_verdict", {})

        decision = str(ceo.get("decision", "HOLD")).upper()
        conviction = ceo.get("conviction", 0)
        dir_emoji = "🟢 LONG" if decision == "LONG" else ("🔴 SHORT" if decision == "SHORT" else "⚪ HOLD")
        reasoning_en = ceo.get("reasoning_en", "")
        reasoning = f"{reasoning_en}".strip()

        if decision == "HOLD":
            return (
                f"⏸️ *MARKET UPDATE | NADO DEX*\n\n"
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
        pos_usd = risk.get("position_size_usd", 0)
        pos_pct = risk.get("position_size_pct", 0)
        rr_ratio = risk.get("risk_reward_ratio", 0)

        message = (
            f"🚀 *TRADE SIGNAL | NADO DEX*\n\n"
            f"🪙 *Asset / Монета:* `{symbol}`\n"
            f"📊 *Direction / Направление:* {dir_emoji}\n"
            f"🔥 *AI Conviction / Уверенность:* `{conviction}%` \n"
            f"💰 *Position / Сумма сделки:* `${pos_usd:,.2f}` ({pos_pct}%)\n"
            f"🎯 *Entry / Цена входа:* `${entry_price:,.2f}`\n\n"
            f"🟢 *Take Profit (TP):* `${tp_price:,.2f}` (+{tp_pct}%)\n"
            f"🔴 *Stop Loss (SL):* `${sl_price:,.2f}` (-{sl_pct}%)\n"
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
