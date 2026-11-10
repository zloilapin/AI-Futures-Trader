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
        
        # Determine strategy mode & predator / regular tag
        is_anti_bot = bool(
            final_trade_data.get("is_anti_bot")
            or str(final_trade_data.get("strategy_mode", "")).upper() == "MEAN_REVERSION"
            or str(ceo.get("strategy_mode", "")).upper() == "MEAN_REVERSION"
            or str(risk.get("strategy_mode", "")).upper() == "MEAN_REVERSION"
            or getattr(config, "ANTI_BOT_MODE", False)
        )
        raw_strat = str(
            final_trade_data.get("strategy_mode")
            or ceo.get("strategy_mode")
            or risk.get("strategy_mode")
            or ("MEAN_REVERSION" if is_anti_bot else "TREND_FOLLOWING")
        ).upper()

        if is_anti_bot or raw_strat == "MEAN_REVERSION":
            mode_badge = "🦈 *ОХОТНИК ЗА ЛИКВИДНОСТЬЮ (Anti-Bot v2.0)*"
            mode_header_tag = " [🦈 ХИЩНИК]"
        else:
            strat_label = raw_strat if raw_strat and raw_strat != "UNKNOWN" else "Trend/Breakout"
            mode_badge = f"🎯 *ОБЫЧНЫЙ РЕЖИМ ({strat_label})*"
            mode_header_tag = " [🎯 ОБЫЧНЫЙ]"

        if decision == "HOLD":
            return (
                f"⏸️ *MARKET UPDATE | NADO DEX{net_badge}*{mode_header_tag}\n\n"
                f"🪙 *Asset / Монета:* `{symbol}`\n"
                f"🕹 *Mode / Режим:* {mode_badge}\n"
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

        # Prioritize exact on-chain execution fill price if available
        exec_res = final_trade_data.get("execution_result", {})
        if isinstance(exec_res, dict) and exec_res.get("actual_fill_price"):
            entry_price = _safe_float(exec_res, "actual_fill_price")
        elif hasattr(exec_res, "actual_fill_price") and getattr(exec_res, "actual_fill_price", None):
            entry_price = float(exec_res.actual_fill_price)
        else:
            entry_price = _safe_float(risk, "entry_price")

        tp_price = _safe_float(risk, "take_profit_price")
        tp_pct = _safe_float(risk, "take_profit_pct")
        sl_price = _safe_float(risk, "stop_loss_price")
        sl_pct = _safe_float(risk, "stop_loss_pct")
        notional_usd = _safe_float(risk, "notional_size_usd")
        pos_pct = _safe_float(risk, "position_size_pct")
        rr_ratio = _safe_float(risk, "risk_reward_ratio")
        
        # Dynamically ensure TP/SL % and RR align precisely with the real entry price
        if entry_price > 0:
            if tp_price > 0:
                tp_pct = round(abs(tp_price - entry_price) / entry_price * 100, 2)
            if sl_price > 0:
                sl_pct = round(abs(sl_price - entry_price) / entry_price * 100, 2)
            if tp_price > 0 and sl_price > 0 and abs(sl_price - entry_price) > 0:
                rr_ratio = round(abs(tp_price - entry_price) / abs(sl_price - entry_price), 2)
        
        primary_conviction = ceo.get("primary_conviction", conviction)
        escalated = ceo.get("escalated", False)
        
        if escalated:
            conv_str = f"Primary {primary_conviction}% | Escalation {conviction}% (Consensus)"
        else:
            conv_str = f"Primary {primary_conviction}% (Direct)"

        small_size_warning = ""
        if 0 < notional_usd < 100:
            small_size_warning = "\n⚠️ *Защита:* Программный TP/SL (Fast Monitor 5с + Sentinel). На бирже Nado триггеры требуют объем ≥ $100.\n"

        is_shadow = bool(getattr(config, "SHADOW_MODE", False) or final_trade_data.get("is_shadow", False))
        header_title = f"👻 *SHADOW SIGNAL (СИМУЛЯЦИЯ) | NADO DEX{net_badge}*" if is_shadow else f"🚀 *TRADE SIGNAL | NADO DEX{net_badge}*"
        shadow_warning = "\n👻 *Внимание:* Сделка виртуальная (Shadow Mode активен в настройках, ончейн-ордер на биржу не отправлялся).\n" if is_shadow else ""

        message = (
            f"{header_title}{mode_header_tag}\n\n"
            f"🪙 *Asset / Монета:* `{symbol}`\n"
            f"🕹 *Mode / Режим:* {mode_badge}\n"
            f"📊 *Direction / Направление:* {dir_emoji}\n"
            f"🔥 *AI Conviction:* `{conv_str}`\n"
            f"💰 *Position / Сумма сделки:* `${notional_usd:,.2f}` ({pos_pct}%)\n"
            f"🎯 *Entry / Цена входа:* `${self._format_price(entry_price)}`\n\n"
            f"🟢 *Take Profit (TP):* `${self._format_price(tp_price)}` (+{tp_pct}%)\n"
            f"🔴 *Stop Loss (SL):* `${self._format_price(sl_price)}` (-{sl_pct}%)\n"
            f"⚖️ *Risk/Reward:* `{rr_ratio:.2f}`\n"
            f"{shadow_warning}"
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

    def format_position_closed_message(self, close_data: Dict[str, Any]) -> str:
        """
        Formats position closure notification with full financial transparency:
        Entry -> Exit prices, Gross PnL, Exchange Fees (Entry + Exit breakdown),
        Net PnL (in USD and R-multiples), Close Reason, and Data Source (EXACT / ESTIMATED).
        """
        if not isinstance(close_data, dict) or not close_data:
            return ""

        symbol = str(close_data.get("symbol", "UNKNOWN"))
        if symbol in ("UNKNOWN", "", "None"):
            return ""
        direction = str(close_data.get("direction", "UNKNOWN")).upper()
        
        entry_p = float(close_data.get("entry_price") or 0.0)
        exit_p = float(close_data.get("exit_price") or 0.0)
        gross_pnl = float(close_data.get("gross_pnl_usd", 0.0) or 0.0)
        total_fee = float(close_data.get("total_fee_usd", 0.0) or 0.0)
        
        entry_fee = float(close_data.get("entry_fee_usd") or (total_fee / 2.0 if total_fee > 0 else 0.0))
        exit_fee = float(close_data.get("exit_fee_usd") or (total_fee / 2.0 if total_fee > 0 else 0.0))
        
        net_pnl = float(close_data.get("net_pnl_usd", close_data.get("pnl_usd", close_data.get("pnl", 0.0))) or 0.0)
        r_mult = float(close_data.get("realized_r", 0.0) or 0.0)
        
        source_tag = "✅ Биржа (EXACT)" if close_data.get("pnl_source") == "EXACT" else "⏳ Оценка (ESTIMATED)"
        reason = str(close_data.get("close_reason") or close_data.get("triggered_by") or "CLOSED")

        slices_pnl = float(close_data.get("accumulated_slices_pnl", 0.0) or 0.0)
        slices_str = f"\n🧩 *Ступени TP:* `{slices_pnl:+.4f}$`" if slices_pnl != 0.0 else ""

        icon = "🟢" if net_pnl > 0.05 else ("⚪" if -0.05 <= net_pnl <= 0.05 else "🔴")

        is_anti_bot = bool(
            close_data.get("is_anti_bot")
            or str(close_data.get("strategy_mode", "")).upper() == "MEAN_REVERSION"
        )
        strat_mode = str(close_data.get("strategy_mode", "")).upper()
        if is_anti_bot or strat_mode == "MEAN_REVERSION":
            mode_str = "🦈 ОХОТНИК ЗА ЛИКВИДНОСТЬЮ (Anti-Bot v2.0)"
        else:
            strat_label = strat_mode if strat_mode and strat_mode != "UNKNOWN" else "Trend/Breakout"
            mode_str = f"🎯 ОБЫЧНЫЙ ({strat_label})"

        return (
            f"{icon} *ПОЗИЦИЯ ЗАКРЫТА: {symbol} ({direction})*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕹 *Режим:* `{mode_str}`\n"
            f"📍 *Вход → Выход:* `${entry_p:.4f}` → `${exit_p:.4f}`\n"
            f"📈 *PnL по цене (Gross):* `{gross_pnl:+.4f}$`\n"
            f"💸 *Комиссия биржи:* `-{total_fee:.4f}$` *(вход: ${entry_fee:.4f} | выход: ${exit_fee:.4f})*{slices_str}\n"
            f"💰 *Чистый PnL (Net):* *`{net_pnl:+.4f}$ ({r_mult:+.2f}R)`*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 *Причина:* `{reason}`\n"
            f"🔍 *Источник данных:* {source_tag}"
        )

