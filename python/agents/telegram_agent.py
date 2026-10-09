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
        if not isinstance(final_trade_data, dict):
            return "⚠️ Некорректные данные сигнала."

        symbol = str(final_trade_data.get("symbol", "UNKNOWN"))
        ceo = final_trade_data.get("ceo_verdict", {}) if isinstance(final_trade_data.get("ceo_verdict"), dict) else {}
        risk = final_trade_data.get("risk_verdict", {}) if isinstance(final_trade_data.get("risk_verdict"), dict) else {}

        decision = str(ceo.get("decision", "HOLD")).upper()
        try:
            conviction = int(ceo.get("conviction", 0) or 0)
        except (TypeError, ValueError):
            conviction = 0

        dir_emoji = "🟢 LONG" if decision == "LONG" else ("🔴 SHORT" if decision == "SHORT" else "⚪ HOLD")
        
        # Fall back from reasoning_en to general reasoning
        raw_reasoning = str(ceo.get("reasoning_en") or ceo.get("reasoning") or "").strip()
        if len(raw_reasoning) > 500:
            raw_reasoning = raw_reasoning[:497] + "..."
        reasoning = self._escape_md(raw_reasoning)

        from core.config import config
        net_badge = f" [{config.NADO_NETWORK}]" if getattr(config, "NADO_NETWORK", None) else ""
        
        if decision == "HOLD":
            return (
                f"⏸️ *MARKET UPDATE | NADO DEX{net_badge}*\n\n"
                f"🪙 *Asset / Монета:* `{symbol}`\n"
                f"📊 *Direction / Направление:* {dir_emoji}\n"
                f"🔥 *AI Conviction / Уверенность:* `{conviction}%`\n\n"
                f"📝 *Analysis / Аналитика:*\n{reasoning}"
            )

        def _safe_float(d: dict, k: str, default: float = 0.0) -> float:
            try:
                v = d.get(k, default)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        entry_price = _safe_float(risk, "entry_price")
        tp_price = _safe_float(risk, "take_profit_price")
        tp_pct = _safe_float(risk, "take_profit_pct")
        sl_price = _safe_float(risk, "stop_loss_price")
        sl_pct = _safe_float(risk, "stop_loss_pct")
        notional_usd = _safe_float(risk, "notional_size_usd")
        pos_pct = _safe_float(risk, "position_size_pct")
        rr_ratio = _safe_float(risk, "risk_reward_ratio")
        
        primary_conviction = ceo.get("primary_conviction", conviction)
        escalated = ceo.get("escalated", False)
        
        if escalated:
            conv_str = f"Primary {primary_conviction}% | Escalation {conviction}% (Consensus)"
        else:
            conv_str = f"Primary {primary_conviction}% (Direct)"

        small_size_warning = ""
        if 0 < notional_usd < 100:
            small_size_warning = "\n⚠️ *Защита:* Программный TP/SL (Fast Monitor 5с + Sentinel). На бирже Nado триггеры требуют объем ≥ $100.\n"

        message = (
            f"🚀 *TRADE SIGNAL | NADO DEX{net_badge}*\n\n"
            f"🪙 *Asset / Монета:* `{symbol}`\n"
            f"📊 *Direction / Направление:* {dir_emoji}\n"
            f"🔥 *AI Conviction:* `{conv_str}`\n"
            f"💰 *Position / Сумма сделки:* `${notional_usd:,.2f}` ({pos_pct}%)\n"
            f"🎯 *Entry / Цена входа:* `${self._format_price(entry_price)}`\n\n"
            f"🟢 *Take Profit (TP):* `${self._format_price(tp_price)}` (+{tp_pct}%)\n"
            f"🔴 *Stop Loss (SL):* `${self._format_price(sl_price)}` (-{sl_pct}%)\n"
            f"⚖️ *Risk/Reward:* `{rr_ratio:.2f}`\n"
            f"{small_size_warning}\n"
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
