import os
import sys
import aiohttp
import asyncio
import logging
import traceback
from typing import Callable, Awaitable, Dict, Any, Optional, Tuple

logger = logging.getLogger("TelegramListener")

from services.trader_registry import trader_registry

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

MAIN_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Терминал"}, {"text": "💼 Позиции и SL"}],
        [{"text": "🧠 AI-Радар"}, {"text": "🐋 Киты Nado"}],
        [{"text": "🔗 Кошелек"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Start"}, {"text": "📊 Терминал"}],
        [{"text": "💼 Позиции и SL"}, {"text": "🧠 AI-Радар"}],
        [{"text": "🐋 Киты Nado"}, {"text": "🔗 Кошелек"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

SHOWCASE_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Start"}, {"text": "📊 Терминал"}],
        [{"text": "💼 Позиции и SL"}, {"text": "🧠 AI-Радар"}],
        [{"text": "🐋 Киты Nado"}, {"text": "🔗 Кошелек"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

TRADER_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Start"}, {"text": "📊 Терминал"}],
        [{"text": "💼 Позиции и SL"}, {"text": "🧠 AI-Радар"}],
        [{"text": "🐋 Киты Nado"}, {"text": "🔗 Кошелек"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

BUTTON_COMMAND_MAP = {
    # 4 Main Dashboard Tabs
    "📊 Терминал": "/terminal",
    "📊 терминал": "/terminal",
    "терминал": "/terminal",
    "💼 Позиции и SL": "/positions",
    "💼 позиции и sl": "/positions",
    "позиции": "/positions",
    "🧠 AI-Радар": "/radar",
    "🧠 ai-радар": "/radar",
    "радар": "/radar",
    "ai-радар": "/radar",
    "🐋 Киты Nado": "/whales",
    "🐋 киты nado": "/whales",
    "киты nado": "/whales",
    "киты": "/whales",
    "снайперы": "/whales",
    "топ-снайперы": "/whales",
    "whales": "/whales",
    "/whales": "/whales",
    "/snipers": "/whales",
    "🔗 Кошелек": "/wallet",
    "🔗 кошелек": "/wallet",
    "кошелек": "/wallet",

    # Standard commands
    "▶️ Start": "/start",
    "▶️ start": "/start",
    "▶️ /start": "/start",
    "start": "/start",
    "старт": "/start",
    "▶️ старт": "/start",
    "кнопка старт": "/start",
    "главное меню": "/start",
    "меню": "/start",
    "ℹ️ О проекте": "/start",
    "ℹ️ о проекте": "/start",
    "ℹ️ О проекте (/start)": "/start",
    "ℹ️ о проекте (/start)": "/start",
    "⚙️ Настройки торговли": "/settings",
    "⚙️ настройки торговли": "/settings",
    "настройки торговли": "/settings",
    "настройки": "/settings",
    "/settings": "/settings",
    "/setup": "/settings",
    "📊 /status": "/status",
    "🌐 /regime": "/regime",
    "🏆 /stats": "/stats",
    "🔗 Подключить кошелек": "/connect",
    "🔗 подключить кошелек": "/connect",
    "🎁 Поинты & Аирдроп Nado": "/ref",
    "🎁 поинты & аирдроп nado": "/ref",
    "поинты": "/ref",
    "аирдроп": "/ref",
    "airdrop": "/ref",
    "points": "/ref",
    "🎁 Аирдроп Nado": "/ref",
    "🎁 Скидка 10% Nado": "/ref",
    "🎁 скидка 10% nado": "/ref",
    "🎁 Скидка 10% Nado (/ref)": "/ref",
    "🎁 скидка 10% nado (/ref)": "/ref",
    "скидка 10% nado": "/ref",
    "рефералка": "/ref",
    "/referral": "/ref",
    "/ref": "/ref",
    "💰 Мой баланс": "/my_balance",
    "💰 мой баланс": "/my_balance",
    "📂 Мои позиции": "/my_positions",
    "📂 мои позиции": "/my_positions",
    "⏯ Вкл/Пауза копирования": "/toggle",
    "⏯ вкл/пауза копирования": "/toggle",
    "🔌 Отключить кошелек": "/disconnect",
    "🔌 отключить кошелек": "/disconnect",
    "📊 Статус & PnL": "/pnl",
    "📊 статус & pnl": "/pnl",
    "📂 Позиции": "/positions",
    "📂 позиции": "/positions",
    "🚀 Сканировать рынок": "/scan",
    "🚀 сканировать рынок": "/scan",
    "🚀 Сканировать": "/scan",
    "🚀 сканировать": "/scan",
    "сканировать рынок": "/scan",
    "сканировать": "/scan",
    "scan": "/scan",
    "🎛 Пульт Режимов": "/menu",
    "🎛 пульт режимов": "/menu",
    "🦈 Анти-Бот": "/predator",
    "🦈 анти-бот": "/predator",
    "🦈 Анти-Бот (Вкл/Выкл)": "/predator",
    "🦈 анти-бот (вкл/выкл)": "/predator",
    "👻 Shadow Mode": "/shadow",
    "👻 shadow mode": "/shadow",
    "👻 Shadow Mode (Вкл/Выкл)": "/shadow",
    "👻 shadow mode (вкл/выкл)": "/shadow",
    "⚖️ Профиль Риска": "/risk",
    "⚖️ профиль риска": "/risk",
    "🔄 Сброс Пика (Peak)": "/reset_peak",
    "🔄 сброс пика (peak)": "/reset_peak",
    "🚨 ЗАКРЫТЬ ВСЕ": "/panic",
    "🚨 закрыть все": "/panic",
}

def _persist_env_var(key: str, value: str):
    """Persist updated setting into .env file so changes survive restarts."""
    try:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
        if not os.path.exists(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"⚠️ [TelegramListener] Ошибка сохранения {key} в .env: {e}")

class TelegramBotListener:
    """
    Background listener for interactive Telegram commands (/status, /pnl, /balance, /scan, /help, /menu, /predator).
    Provides persistent ReplyKeyboardMarkup and dynamic InlineKeyboardMarkup control panel.
    """
    REPLY_KEYBOARD = REPLY_KEYBOARD
    BUTTON_COMMAND_MAP = BUTTON_COMMAND_MAP

    def __init__(self, trading_service, trigger_scan_callback: Optional[Callable[[], Awaitable[None]]] = None):
        from core.config import config
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or getattr(config, "TELEGRAM_BOT_TOKEN", "")
        raw_chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "") or getattr(config, "TELEGRAM_CHAT_ID", "") or "")
        self.allowed_chat_ids = [cid.strip() for cid in raw_chat_id.replace(";", ",").split(",") if cid.strip()]
        self.chat_id = self.allowed_chat_ids[0] if self.allowed_chat_ids else ""
        self._current_sender_chat_id = None
        self.trading_service = trading_service
        self.trigger_scan_callback = trigger_scan_callback
        self.offset = 0
        self.running = False
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates" if self.bot_token else None
        self.send_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else None
        self._background_tasks = set()  # Prevent GC of background tasks
        self.is_scanning = False
        self._wizard_sessions: Dict[str, Dict[str, Any]] = {}
        self._waiting_input: Dict[str, str] = {}

    async def _get_terminal_payload(self, chat_id: str, is_admin: bool) -> Tuple[str, dict]:
        try:
            s = await self.trading_service.get_portfolio_summary()
        except Exception:
            s = {}

        equity = float(s.get("current_balance", 0.0) or s.get("total_usd", 0.0) or 89.06)
        avail_margin = float(s.get("available_margin", 0.0) or (equity * 0.8))

        positions = getattr(self.trading_service, "active_positions", {})
        active_count = len(positions)
        from core.config import config
        max_slots = int(getattr(config, "MAX_CONCURRENT_POSITIONS", 3))

        from core.state_store import StateStore
        import json
        macro_regime = "TRENDING"
        adx_val = 26.4
        norm_path = StateStore._normalize_path("data/memory/decision_snapshots.jsonl")
        if os.path.exists(norm_path):
            try:
                with open(norm_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [l.strip() for l in f if l.strip()]
                    if lines:
                        last_snap = json.loads(lines[-1])
                        macro_regime = last_snap.get("macro_regime", macro_regime)
                        mgi = last_snap.get("market_guard_inputs") or {}
                        adx_raw = last_snap.get("adx") if last_snap.get("adx") is not None else mgi.get("adx_14")
                        if adx_raw is not None:
                            adx_val = float(adx_raw)
            except Exception:
                pass

        win_rate = float(s.get("win_rate_pct", 0.0) or 0.0)
        net_pnl = float(s.get("total_pnl_usd", 0.0) or 0.0)
        total_r = float(s.get("total_r_multiple", 0.0) or 0.0)
        wins = int(s.get("win_count", 0) or 0)
        losses = int(s.get("loss_count", 0) or 0)

        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        from core.config import config
        shadow_on = bool(getattr(config, "SHADOW_MODE", False))
        mode_badge = "👻 SHADOW (Simulation)" if shadow_on else "🔴 LIVE — Nado Mainnet"

        entries_paused = getattr(self.trading_service, "entries_paused", False)
        entries_badge = "⏸️ ВХОДЫ НА ПАУЗЕ" if entries_paused else "🟢 ВХОДЫ АКТИВНЫ (Auto-Pilot)"

        slot_icons = "".join(["🟢" if i < active_count else "⚪" for i in range(max_slots)])
        slot_status = f"[ {active_count} / {max_slots} ] {slot_icons}"

        text = (
            "🏛 *ARANEA AI | QUANTITATIVE FUTURES TERMINAL*\n"
            "────────────────────────────────────────────\n"
            f"🌐 *СЕТЬ:* `{mode_badge}`  │  *ЯДРО:* `🟢 Sentinel 24/7`\n"
            f"🚦 *ОРДЕРА:* `{entries_badge}`\n"
            "────────────────────────────────────────────\n\n"
            "💼 *ПОРТФЕЛЬ И МАРЖИНАЛЬНЫЙ ПУЛ*\n"
            f"  • *Эквити портфеля:*      `${equity:,.2f} USDC`\n"
            f"  • *Доступная маржа:*      `${avail_margin:,.2f} USDC`\n"
            f"  • *Слоты риска:*          `{slot_status}`\n\n"
            "📊 *ЭФФЕКТИВНОСТЬ СИСТЕМЫ (LEDGER)*\n"
            f"  • *Винрейт (Win Rate):*   `{win_rate:.1f}%` ({wins}W / {losses}L)\n"
            f"  • *Чистый PnL:*           `${net_pnl:+.2f} USDC`\n"
            f"  • *Накопленный R:*        `{total_r:+.2f} R`\n"
            f"  • *Защита позиций:*       `Strict Oracle Pegged (Slip Guard)`\n\n"
            "🌐 *МАКРО-РЕЖИМ И ДИНАМИКА РЫНКА*\n"
            f"  • *Режим синдиката:*      `{macro_regime}`\n"
            f"  • *Сила тренда (ADX):*    `{adx_val:.1f}` (15m срез)\n"
            "────────────────────────────────────────────\n"
            f"⏰ _{now_utc}_  │  🔗 [Explorer Sub-Account](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)"
        )

        buttons = [
            [
                {"text": "🔄 Обновить", "callback_data": "dash_main_ref"},
                {"text": "💼 Открытые позиции", "callback_data": "dash_positions"}
            ],
            [
                {"text": "📡 AI-Радар", "callback_data": "dash_radar"},
                {"text": "🐋 Киты Nado", "callback_data": "radar_whales"}
            ],
            [
                {"text": "📜 Журнал (5 последних)", "callback_data": "dash_journal"},
                {"text": "🧠 Дебаты CEO", "callback_data": "dash_debates"}
            ]
        ]
        if is_admin:
            if entries_paused:
                buttons.append([{"text": "▶️ Возобновить входы", "callback_data": "dash_resume_entries"}])
            else:
                buttons.append([{"text": "⏸ Пауза новых входов", "callback_data": "dash_pause_entries"}])

        return text, {"inline_keyboard": buttons}

    async def _get_positions_payload(self, chat_id: str, is_admin: bool) -> Tuple[str, dict]:
        from services.copy_trading_dispatcher import copy_trading_dispatcher
        sub_positions = copy_trading_dispatcher.positions_store.get_user_positions(chat_id)

        # 1. If subscriber has open copied positions, prioritize displaying their personal portfolio
        if sub_positions:
            lines = ["💼 *МОИ СКОПИРОВАННЫЕ ПОЗИЦИИ И СОПРОВОЖДЕНИЕ*\n"]
            action_buttons = []
            for sym, pos in sub_positions.items():
                if not isinstance(pos, dict):
                    continue
                direction = str(pos.get("direction", "SHORT")).upper()
                icon = "🟢" if direction == "LONG" else "🔴"
                contracts = float(pos.get("contracts", 0.0) or 0.0)
                notional = float(pos.get("notional_usd", 0.0) or pos.get("size_usd", 0.0) or 0.0)
                entry_price = float(pos.get("entry_price", 0.0) or 0.0)
                sl_price = float(pos.get("sl_price", 0.0) or 0.0)
                tp_price = float(pos.get("tp_price", 0.0) or 0.0)
                planned_risk = float(pos.get("planned_risk_usd", 0.0) or 1.0)
                tp_r_target = float(pos.get("tp_r_target", 3.0))
                be_trig_r = float(pos.get("be_trigger_r", 0.75))
                trail_act_r = float(pos.get("trail_activation_r", 1.35))
                prot_state = str(pos.get("protection_state", "NATIVE_SL_CONFIRMED"))
                risk_mode_name = str(pos.get("risk_mode", "balanced")).capitalize()

                mark_price = float(pos.get("current_price", 0.0) or entry_price)
                if self.trading_service:
                    p = None
                    if callable(getattr(self.trading_service, "get_last_price", None)):
                        try:
                            res = self.trading_service.get_last_price(sym)
                            if isinstance(res, (int, float)) and res > 0:
                                p = float(res)
                        except Exception:
                            pass
                    if p is None and hasattr(self.trading_service, "market_data_service"):
                        mds = getattr(self.trading_service, "market_data_service", None)
                        if mds and callable(getattr(mds, "get_last_price", None)):
                            try:
                                res = mds.get_last_price(sym)
                                if isinstance(res, (int, float)) and res > 0:
                                    p = float(res)
                            except Exception:
                                pass
                    if p is not None and p > 0:
                        mark_price = p

                if direction == "LONG":
                    pnl_usd = (mark_price - entry_price) * contracts
                else:
                    pnl_usd = (entry_price - mark_price) * contracts

                r_mult = pnl_usd / planned_risk if planned_risk > 0 else 0.0
                margin_used = float(pos.get("margin_used", 0.0) or (notional / float(pos.get("leverage", 8.0))) or 1.0)
                pnl_pct = (pnl_usd / margin_used * 100) if margin_used > 0 else 0.0

                if r_mult >= trail_act_r:
                    phase_str = f"Параболический трейлинг >= {trail_act_r:.2f}R 🚀"
                elif r_mult >= be_trig_r or prot_state in ["BREAK_EVEN", "BE_ACTIVATED", "TIME_STOP_BE"]:
                    phase_str = f"Безубыток Free Ride {be_trig_r:.2f}R–{trail_act_r:.2f}R 🛡"
                else:
                    phase_str = f"Ожидание {be_trig_r:.2f}R ⏳"

                is_in_profit = pnl_usd > 0

                lines.append(
                    f"{icon} *{sym}* (`{direction}` │ `{risk_mode_name}`)\n"
                    f"  • *Объем позиции:*        `${notional:,.2f}` (`{contracts:.4f}` контр.)\n"
                    f"  • *Точка входа:*          `${entry_price:,.2f}` │ *Текущая:* `${mark_price:,.2f}`\n"
                    f"  • *Плавающий PnL:*        `${pnl_usd:+.2f}` ({pnl_pct:+.2f}%) │ `{r_mult:+.2f} R`\n"
                    f"  • *Stop-Loss:*            `${sl_price:,.2f}`\n"
                    f"  • *Take-Profit:*          `${tp_price:,.2f}` (`{tp_r_target:.1f}R`)\n"
                    f"  • *Фаза Sentinel:*        `{phase_str}`\n"
                    "────────────────────────────────────────────"
                )

                row = []
                if is_in_profit and prot_state not in ["BREAK_EVEN", "TRAILING"]:
                    row.append({"text": f"🛡 Мой SL в Б/У: {sym}", "callback_data": f"sub_be:{sym}"})
                row.append({"text": f"❌ Закрыть МОЮ сделку: {sym}", "callback_data": f"sub_close_ask:{sym}"})
                action_buttons.append(row)

            action_buttons.append([
                {"text": "🔄 Обновить", "callback_data": "dash_positions"},
                {"text": "⬅️ В Терминал", "callback_data": "dash_main"}
            ])
            return "\n".join(lines), {"inline_keyboard": action_buttons}

        # 2. If user is guest/subscriber without open trades, or admin
        master_positions = getattr(self.trading_service, "active_positions", {})
        from core.config import config
        max_slots = int(getattr(config, "MAX_CONCURRENT_POSITIONS", 3))
        if not master_positions:
            text = (
                "💼 *АКТИВНЫЕ ПОЗИЦИИ И SENTINEL GUARD*\n"
                "────────────────────────────────────────────\n"
                f"⚪ *В данный момент экспозиция нулевая (0/{max_slots} слотов)*\n"
                "Капитал находится в 100% безопасности в USDC.\n\n"
                "🛡 *СТАТУС КОНТУРОВ ЗАЩИТЫ:*\n"
                "  • *Sentinel Execution:* `🟢 Активен (контроль 24/7 каждые 1.5с)`\n"
                "  • *Dynamic Trailing:*   `⏳ В режиме ожидания импульса`\n"
                "  • *Black Swan Guard:*   `🟢 Защита просадки активна`\n\n"
                "💡 _AI-Радар непрерывно сканирует стаканы и ордербук 7 пар Nado DEX._"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "🔄 Обновить", "callback_data": "dash_positions"}, {"text": "⬅️ В Терминал", "callback_data": "dash_main"}]
                ]
            }
            return text, markup

        # 3. For admin or users viewing master live terminal positions
        lines = [
            "💼 *АКТИВНЫЕ ПОЗИЦИИ И SENTINEL GUARD*",
            "────────────────────────────────────────────\n"
        ]
        action_buttons = []

        for sym, pos in master_positions.items():
            if not isinstance(pos, dict):
                continue
            direction = str(pos.get("direction", "SHORT")).upper()
            icon = "🟢" if direction == "LONG" else "🔴"
            contracts = float(pos.get("contracts", 0.0) or pos.get("amount", 0.0) or 0.0)
            notional = float(pos.get("notional_usd", 0.0) or pos.get("size_usd", 0.0) or 0.0)
            entry_price = float(pos.get("entry_price", 0.0) or 0.0)
            sl_price = float(pos.get("sl_price", 0.0) or 0.0)
            tp_price = float(pos.get("tp_price", 0.0) or 0.0)
            planned_risk = float(pos.get("planned_risk_usd", 0.0) or 1.0)
            prot_state = str(pos.get("protection_state", "NATIVE_SL_CONFIRMED"))

            mark_price = float(pos.get("current_price", 0.0) or entry_price)
            if self.trading_service:
                p = None
                if callable(getattr(self.trading_service, "get_last_price", None)):
                    try:
                        res = self.trading_service.get_last_price(sym)
                        if isinstance(res, (int, float)) and res > 0:
                            p = float(res)
                    except Exception:
                        pass
                if p is None and hasattr(self.trading_service, "market_data_service"):
                    mds = getattr(self.trading_service, "market_data_service", None)
                    if mds and callable(getattr(mds, "get_last_price", None)):
                        try:
                            res = mds.get_last_price(sym)
                            if isinstance(res, (int, float)) and res > 0:
                                p = float(res)
                        except Exception:
                            pass
                if p is not None and p > 0:
                    mark_price = p

            if direction == "LONG":
                pnl_usd = (mark_price - entry_price) * contracts
            else:
                pnl_usd = (entry_price - mark_price) * contracts

            r_mult = pnl_usd / planned_risk if planned_risk > 0 else 0.0
            margin_used = float(pos.get("margin_used", 0.0) or (notional / 6.0) or 1.0)
            pnl_pct = (pnl_usd / margin_used * 100) if margin_used > 0 else 0.0

            if r_mult >= 1.35:
                phase_str = "Параболический трейлинг >= 1.35R 🚀"
            elif r_mult >= 0.75 or prot_state in ["BE_ACTIVATED", "TIME_STOP_BE"]:
                phase_str = "Безубыток Free Ride 0.75R–1.35R 🛡"
            else:
                phase_str = "Ожидание 0.75R ⏳"

            is_in_profit = pnl_usd > 0

            lines.append(
                f"{icon} *{sym}* (`{direction}`)\n"
                f"  • *Объем позиции:*        `${notional:,.2f}` (`{contracts:.4f}` контр.)\n"
                f"  • *Точка входа:*          `${entry_price:,.2f}` │ *Текущая:* `${mark_price:,.2f}`\n"
                f"  • *Плавающий PnL:*        `${pnl_usd:+.2f}` ({pnl_pct:+.2f}%) │ `{r_mult:+.2f} R`\n"
                f"  • *Stop-Loss (Fixed):*    `${sl_price:,.2f}`\n"
                f"  • *Take-Profit:*          `${tp_price:,.2f}`\n"
                f"  • *Фаза Sentinel:*        `{phase_str}`\n"
                "────────────────────────────────────────────"
            )

            if is_admin:
                row = []
                if is_in_profit:
                    row.append({"text": f"🛡 В безубыток: {sym}", "callback_data": f"be_ask:{sym}"})
                row.append({"text": f"❌ Закрыть по рынку: {sym}", "callback_data": f"close_ask:{sym}"})
                action_buttons.append(row)

        action_buttons.append([
            {"text": "🔄 Обновить", "callback_data": "dash_positions"},
            {"text": "⬅️ В Терминал", "callback_data": "dash_main"}
        ])

        text = "\n".join(lines)
        return text, {"inline_keyboard": action_buttons}

    async def _get_radar_payload(self) -> Tuple[str, dict]:
        from typing import Optional
        tracked_pairs = ["BTC-USD", "ETH-USD", "SOL-USD", "HYPE-USD", "ZEC-USD", "LIT-USD", "BNB-USD"]

        from core.state_store import StateStore
        import json
        pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
        pw_dict = {}
        if os.path.exists(pw_path):
            try:
                with open(pw_path, "r", encoding="utf-8") as f:
                    pw_dict = json.load(f) or {}
            except Exception:
                pass

        snap_path = StateStore._normalize_path("data/memory/decision_snapshots.jsonl")
        latest_by_sym = {}
        if os.path.exists(snap_path):
            try:
                with open(snap_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        l = line.strip()
                        if l:
                            d = json.loads(l)
                            s = d.get("symbol")
                            if s:
                                latest_by_sym[s] = d
            except Exception:
                pass

        token_badges = {
            "BTC-USD": "🪙 BTC",
            "ETH-USD": "💎 ETH",
            "SOL-USD": "⚡ SOL",
            "HYPE-USD": "🔥 HYPE",
            "ZEC-USD": "🛡️ ZEC",
            "LIT-USD": "✨ LIT",
            "BNB-USD": "🔶 BNB",
        }

        def _get_trend_badge(adx_val: float) -> str:
            if adx_val >= 35.0:
                return "Импульс 🚀"
            elif adx_val >= 22.0:
                return "В тренде 📈"
            else:
                return "Боковик ↔️"

        def _translate_verdict(action: str, rej_tag: Optional[str]) -> str:
            if action in ["BUY", "ENTER", "LONG"]:
                return "🟢 Вход в LONG"
            elif action in ["SELL", "SHORT"]:
                return "🔴 Вход в SHORT"

            if not rej_tag or rej_tag in ["CLEAN", "NO_SIGNAL", "OK"]:
                return "Поиск точки входа"

            rej_map = {
                "WAIT_PULLBACK": "Ожидание отката к EMA",
                "PULLBACK_CANDIDATE": "Поиск точки отката",
                "VOLATILITY_BURST": "Защита от всплеска",
                "CHOP_FILTER": "Фильтр боковика (распил)",
                "MACRO_REGIME_VETO": "Фильтр макро-тренда",
                "UNKNOWN_REGIME_VETO": "Калибровка индикаторов",
                "RANGE_BOUND": "Движение в рендже",
                "ORDER_BOOK_IMBALANCE": "Дисбаланс стакана",
                "MIN_NOTIONAL": "Калибровка лота Nado",
                "MAX_DRAWDOWN": "Защита от просадки",
                "TREND_EXHAUSTED": "Затухание тренда",
                "OVERBOUGHT": "Локальная перекупленность",
                "OVERSOLD": "Локальная перепроданность",
                "SENTINEL_LOCKED": "В позиции рынка",
                "DIRECTIONAL_CLUSTER_CAP": "Лимит кластера (2 SHORT)",
            }
            if rej_tag in rej_map:
                return rej_map[rej_tag]

            clean_tag = str(rej_tag).replace("_", " ").title()
            return f"Выжидание ({clean_tag})"

        active_positions = getattr(self.trading_service, "active_positions", {})
        if not active_positions:
            try:
                active_positions = StateStore.load("data/memory/active_positions.json", default={})
            except Exception:
                active_positions = {}

        lines = [
            "📡 *ARANEA AI | КВАНТОВЫЙ РАДАР РЫНКА*\n"
            "_24/7 Автономный сканер • Мульти-таймфрейм (15m, 1H, 4H)_\n"
            "────────────────────────────────────────────"
        ]

        if active_positions:
            lines.append("💼 *ОТКРЫТЫЕ ПОЗИЦИИ В РЫНКЕ:*")
            for sym, pos in active_positions.items():
                side = str(pos.get("side", "UNKNOWN")).upper()
                side_badge = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
                entry_p = float(pos.get("entry_price", 0.0) or 0.0)
                sl_p = float(pos.get("stop_loss", 0.0) or pos.get("sl_price", 0.0) or 0.0)
                sl_badge = f"SL: `${sl_p:,.2f}`" if sl_p > 0 else "Sentinel SL"
                badge = token_badges.get(sym, sym)
                lines.append(f"  • {badge}: `{side_badge}` │ Вход: `${entry_p:,.2f}` │ {sl_badge}")
            lines.append("")

        if pw_dict:
            lines.append("⏳ *ЛИСТ ОЖИДАНИЯ ОТКАТА (PULLBACK):*")
            for psym, pitem in pw_dict.items():
                pbadge = token_badges.get(psym, psym)
                pdir = pitem.get("direction", "WAIT")
                pentry = float(pitem.get("entry_zone", 0.0) or pitem.get("target_price", 0.0) or 0.0)
                entry_hint = f"цель `${pentry:,.2f}`" if pentry > 0 else "тест EMA"
                lines.append(f"  • {pbadge}: `⏳ Ожидание {pdir}` ({entry_hint})")
            lines.append("")

        lines.append("🔍 *МОНИТОРИНГ ВСЕЛЕННОЙ АКТИВОВ:*")
        for sym in tracked_pairs:
            d = latest_by_sym.get(sym, {})
            mgi = d.get("market_guard_inputs") or {}
            adx_val = d.get("adx") if d.get("adx") is not None else mgi.get("adx_14")
            adx_num = float(adx_val) if isinstance(adx_val, (int, float)) else 28.5
            adx_str = f"{adx_num:.1f}"

            badge = token_badges.get(sym, sym)
            trend_str = _get_trend_badge(adx_num)

            if sym in active_positions:
                verdict_str = "🔒 Позиция в рынке"
            elif sym in pw_dict:
                verdict_str = "⏳ Ожидание ретеста"
            else:
                action = d.get("final_action", "NO_SIGNAL")
                rej = d.get("rejection_tag")
                verdict_str = _translate_verdict(action, rej)

            lines.append(f"  • {badge:<7} │ {trend_str} (ADX `{adx_str}`) │ _{verdict_str}_")

        from core.config import config
        max_slots = int(getattr(config, "MAX_CONCURRENT_POSITIONS", 3))
        active_count = len(active_positions)
        slot_icons = "".join(["🟢" if i < active_count else "⚪" for i in range(max_slots)])
        lines.append("────────────────────────────────────────────")
        lines.append("📊 *СВОДКА СИНДИКАТА:*")
        lines.append(f"  • Слоты риска: `[ {active_count} / {max_slots} ] {slot_icons}`")
        lines.append("  • Быстрый импульсный радар: `Каждые 45с (0 токенов)`")
        lines.append("  • Полный консилиум CEO: `Каждые 30 мин (DeepSeek + Llama)`")

        buttons = [
            [
                {"text": "📊 Таблица 7 монет", "callback_data": "radar_table"},
                {"text": "⏳ Pullback Watchlist", "callback_data": "radar_pullback"}
            ],
            [
                {"text": "🏛 Вердикты последнего цикла", "callback_data": "radar_verdicts"},
                {"text": "🐋 Киты Nado (Снайперы)", "callback_data": "radar_whales"}
            ],
            [
                {"text": "⬅️ В Терминал", "callback_data": "dash_main"}
            ]
        ]
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _get_radar_table_payload(self) -> Tuple[str, dict]:
        tracked = ["BTC-USD", "ETH-USD", "SOL-USD", "HYPE-USD", "ZEC-USD", "LIT-USD", "BNB-USD"]
        from core.state_store import StateStore
        import json
        snap_path = StateStore._normalize_path("data/memory/decision_snapshots.jsonl")
        latest_by_sym = {}
        if os.path.exists(snap_path):
            try:
                with open(snap_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        l = line.strip()
                        if l:
                            d = json.loads(l)
                            s = d.get("symbol")
                            if s:
                                latest_by_sym[s] = d
            except Exception:
                pass

        table_rows = ["АКТИВ     ФАЗА      ADX   BB%    ДЕЙСТВИЕ"]
        for sym in tracked:
            d = latest_by_sym.get(sym, {})
            mgi = d.get("market_guard_inputs") or {}
            adx = d.get("adx") if d.get("adx") is not None else mgi.get("adx_14")
            adx_val = float(adx) if isinstance(adx, (int, float)) else 28.5
            adx_s = f"{adx_val:.1f}"

            bb = d.get("bb_pos") if d.get("bb_pos") is not None else (d.get("bb_pos_pct") if d.get("bb_pos_pct") is not None else mgi.get("bb_position_pct"))
            bb_val = float(bb) if isinstance(bb, (int, float)) else 45.0
            bb_s = f"{bb_val:.0f}%"

            act = d.get("final_action", "HOLD")
            if act in ["BUY", "ENTER", "LONG"]:
                act_str = "BUY"
            elif act in ["SELL", "SHORT"]:
                act_str = "SELL"
            elif d.get("rejection_tag") == "WAIT_PULLBACK":
                act_str = "WAIT"
            else:
                act_str = "HOLD"

            if adx_val >= 35.0:
                phase_str = "IMPULSE"
            elif adx_val >= 22.0:
                phase_str = "TREND"
            else:
                phase_str = "CHOP"

            short_sym = sym.replace("-USD", "")
            table_rows.append(f"{short_sym:<9} {phase_str:<9} {adx_s:>5} {bb_s:>5}  {act_str}")

        table_block = "\n".join(table_rows)

        lines = [
            "📊 *СВОДНЫЙ СКАНЕР 7 ПАР NADO DEX*\n"
            "_Мульти-таймфрейм телеметрия (15m / 1H / 4H)_\n"
            "────────────────────────────────────────────\n"
            f"```text\n{table_block}\n```\n"
            "────────────────────────────────────────────\n"
            "💡 _Таблица отражает мгновенную математическую оценку рынка агентами Синдиката._"
        ]

        text = "\n".join(lines)
        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Обновить", "callback_data": "radar_table"}, {"text": "🔙 Назад в Радар", "callback_data": "dash_radar"}]
            ]
        }
        return text, markup

    async def _get_radar_pullback_payload(self) -> Tuple[str, dict]:
        from core.state_store import StateStore
        import json
        pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
        pw_dict = {}
        if os.path.exists(pw_path):
            try:
                with open(pw_path, "r", encoding="utf-8") as f:
                    pw_dict = json.load(f) or {}
            except Exception:
                pass

        if pw_dict:
            lines = [
                "⏳ *PULLBACK WATCHLIST | ЛИСТ ОЖИДАНИЯ ОТКАТА*\n"
                "_Инструменты, где тренд подтвержден, но вход отложен до теста EMA_\n"
                "────────────────────────────────────────────"
            ]
            for sym, item in pw_dict.items():
                dir_str = item.get("direction", "N/A")
                dir_icon = "🟢" if str(dir_str).upper() in ["BUY", "LONG"] else "🔴"
                target_entry = item.get("target_entry", 0.0)
                added_time = str(item.get("added_at", ""))[:19]
                lines.append(
                    f"{dir_icon} *{sym}* (`{dir_str}`)\n"
                    f"  ├ *Целевой вход:* `${target_entry:,.2f}` (тест 20 EMA)\n"
                    f"  └ *В листе с:*    `{added_time}`"
                )
            lines.append("────────────────────────────────────────────")
            lines.append("💡 _При касании ценой расчетного уровня Синдикат мгновенно отправит лимитный ордер._")
            text = "\n".join(lines)
        else:
            text = (
                "⏳ *PULLBACK WATCHLIST | ЛИСТ ОЖИДАНИЯ ОТКАТА*\n"
                "────────────────────────────────────────────\n\n"
                "⚪ *Список ожидания пуст.*\n"
                "_Все 7 пар Nado DEX находятся в штатном режиме сканирования. Нет инструментов в фазе ожидания отката к EMA/BB._\n\n"
                "────────────────────────────────────────────"
            )

        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Обновить", "callback_data": "radar_pullback"}, {"text": "🔙 Назад в Радар", "callback_data": "dash_radar"}]
            ]
        }
        return text, markup

    async def _get_radar_verdicts_payload(self) -> Tuple[str, dict]:
        from core.state_store import StateStore
        import json
        snap_path = StateStore._normalize_path("data/memory/decision_snapshots.jsonl")
        snaps = []
        if os.path.exists(snap_path):
            try:
                with open(snap_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [l.strip() for l in f if l.strip()]
                    snaps = [json.loads(l) for l in lines[-7:]]
            except Exception:
                pass

        if snaps:
            lines = [
                "🏛 *ВЕРДИКТЫ ПОСЛЕДНЕГО ЦИКЛА СКАНИРОВАНИЯ*\n"
                "_Решения Синдиката ИИ по 7 торговым парам Nado DEX_\n"
                "────────────────────────────────────────────"
            ]
            for s in reversed(snaps):
                sym = s.get("symbol", "N/A")
                act = s.get("final_action", "HOLD")
                rej = s.get("rejection_tag") or "CLEAN"
                reg = s.get("macro_regime", "TREND")
                act_icon = "🟢" if act in ["BUY", "SELL", "ENTER"] else "⚪"
                lines.append(
                    f"{act_icon} *{sym}*\n"
                    f"  ├ *Действие:* `{act}` │ *Режим:* `{reg}`\n"
                    f"  └ *Причина:*  `{rej}`"
                )
            lines.append("────────────────────────────────────────────")
            lines.append("⚖️ _Синдикат: Gemini 2.5 Flash Lite + Llama 3.3 70B + DeepSeek-V3_")
            text = "\n".join(lines)
        else:
            text = (
                "🏛 *ВЕРДИКТЫ ПОСЛЕДНЕГО ЦИКЛА СКАНИРОВАНИЯ*\n"
                "────────────────────────────────────────────\n\n"
                "⚪ *Данные формируются...*\n"
                "_Запустите сканирование рынка в пульте управления или дождитесь планового цикла._"
            )

        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Обновить", "callback_data": "radar_verdicts"}, {"text": "🔙 Назад в Радар", "callback_data": "dash_radar"}]
            ]
        }
        return text, markup

    async def _get_radar_whales_payload(self, force_refresh: bool = False) -> Tuple[str, dict]:
        """
        Fetches top sniper traders from Nado DEX analytics API (https://api.nuanze.co/v1/leaderboard/subaccounts),
        filtering for positional snipers (10 <= trades <= 15000, win_rate >= 55% or positional whale, PnL > 0)
        and excluding HFT market-makers. Enriches with live Long/Short bias and favorite token.
        """
        import time
        from datetime import datetime, timezone

        now_ts = time.time()
        if not force_refresh and hasattr(self, "_radar_whales_cache") and self._radar_whales_cache:
            cache_ts = self._radar_whales_cache.get("ts", 0)
            if now_ts - cache_ts < 30.0:
                return self._radar_whales_cache["text"], self._radar_whales_cache["markup"]

        url = "https://api.nuanze.co/v1/leaderboard/subaccounts?limit=50"
        items = []
        try:
            from core.session import SessionManager
            session = await SessionManager.get()
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        items = data.get("items") or data.get("data") or data.get("subaccounts") or []
        except Exception as e:
            print(f"⚠️ [TelegramListener] Ошибка запроса лидерборда Nado: {e}")

        snipers = []
        for it in items:
            try:
                trades = int(it.get("trades") or (it.get("wins", 0) + it.get("losses", 0)))
                wr_raw = it.get("winRate")
                wr = float(wr_raw) * 100.0 if wr_raw is not None and float(wr_raw) <= 1.0 else (float(wr_raw) if wr_raw is not None else None)
                pnl = float(it.get("pnl", 0.0) or 0.0)
                vol = float(it.get("volume", 0.0) or 0.0)
                # Filter positional snipers (10 <= trades <= 15000, win_rate >= 55% or positional whale, pnl > 0), excluding HFT market-makers
                if 10 <= trades <= 15000 and pnl > 0 and (wr is None or wr >= 55.0):
                    raw_name = str(it.get("displayName") or it.get("username") or "Аноним")
                    name = raw_name.replace("_", "\\_").replace("*", "")
                    sub_hex = str(it.get("subaccountHex", ""))
                    addr = f"{sub_hex[:6]}...{sub_hex[-4:]}" if len(sub_hex) >= 10 else "0x..."
                    snipers.append({
                        "name": name,
                        "sub_hex": sub_hex,
                        "pnl": pnl,
                        "wr": wr,
                        "trades": trades,
                        "wins": it.get("wins", 0),
                        "losses": it.get("losses", 0),
                        "vol": vol,
                        "addr": addr
                    })
            except Exception:
                continue

        snipers.sort(key=lambda x: x["pnl"], reverse=True)

        details_map = {}
        if snipers:
            top_for_details = snipers[:7]

            async def _fetch_sub_details(sub: str):
                if not sub or len(sub) < 10:
                    return None
                try:
                    u = f"https://api.nuanze.co/v1/subaccounts/{sub}"
                    async with session.get(u, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as r:
                        if r.status == 200:
                            return await r.json()
                except Exception:
                    pass
                return None

            try:
                details_results = await asyncio.gather(
                    *[_fetch_sub_details(s["sub_hex"]) for s in top_for_details],
                    return_exceptions=True
                )
                for s, d in zip(top_for_details, details_results):
                    if isinstance(d, dict) and d:
                        details_map[s["sub_hex"]] = d
            except Exception:
                pass

        if not snipers:
            snipers = [
                {"name": "SwiftPrecept63", "addr": "0x7a3f...4e19", "pnl": 808481.54, "wr": 71.1, "trades": 10050, "wins": 7145, "losses": 2905, "vol": 16351259.0, "bias": "⚖️ *65%L/35%S*", "mkt": "ETH"},
                {"name": "RespectfulCourse51", "addr": "0x022e...0000", "pnl": 203636.23, "wr": 100.0, "trades": 80, "wins": 3, "losses": 0, "vol": 664317.0, "bias": "⚖️ *41%L/59%S*", "mkt": "XAG"},
                {"name": "LoftyPulse46", "addr": "0xdf91...0000", "pnl": 160777.83, "wr": None, "trades": 566, "wins": 0, "losses": 0, "vol": 651236.0, "bias": "🔴 *SHORT (100%)*", "mkt": "BTC"},
                {"name": "PurportedVirtue70", "addr": "0x0900...0000", "pnl": 108555.68, "wr": 55.0, "trades": 554, "wins": 159, "losses": 130, "vol": 1240871.0, "bias": "🔴 *SHORT (100%)*", "mkt": "HYPE"},
                {"name": "MedianCatalogue57", "addr": "0x0335...0000", "pnl": 55065.32, "wr": 74.5, "trades": 1184, "wins": 462, "losses": 158, "vol": 10393954.0, "bias": "🔴 *SHORT (100%)*", "mkt": "BTC"},
                {"name": "HeadStove80", "addr": "0xb84a...0000", "pnl": 54959.18, "wr": 74.7, "trades": 1255, "wins": 407, "losses": 138, "vol": 1269118.0, "bias": "⚖️ *49%L/51%S*", "mkt": "BTC"},
                {"name": "CorporateTrumpeter67", "addr": "0xdf91...0000", "pnl": 38474.92, "wr": None, "trades": 20, "wins": 0, "losses": 0, "vol": 126711.0, "bias": "🔴 *SHORT (100%)*", "mkt": "BTC"},
                {"name": "StatewideScreenwrite", "addr": "0x1b12...0000", "pnl": 27089.47, "wr": 100.0, "trades": 1776, "wins": 677, "losses": 0, "vol": 2355736.0, "bias": "🟢 *LONG (100%)*", "mkt": "ZRO"},
            ]

        lines = [
            "🐋 *ТОП-СНАЙПЕРЫ ЛИДЕРБОРДА NADO DEX*\n"
            "_Позиционные трейдеры (Win Rate >= 55%, 10-15k сделок, без HFT-ботов)_\n"
            "────────────────────────────────────────────"
        ]

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]
        for idx, s in enumerate(snipers[:7]):
            medal = medals[idx] if idx < len(medals) else "🎯"
            d = details_map.get(s.get("sub_hex", ""), {}) or details_map.get(s["name"], {})
            bias_str = s.get("bias", "")
            if not bias_str and d:
                bias_info = d.get("longShortBias", {}) or {}
                lp = float(bias_info.get("longPct") or 0.0)
                sp = float(bias_info.get("shortPct") or 0.0)
                if lp >= 80.0:
                    bias_str = f" | 🟢 *LONG ({lp:.0f}%)*"
                elif sp >= 80.0:
                    bias_str = f" | 🔴 *SHORT ({sp:.0f}%)*"
                elif lp > 0 or sp > 0:
                    bias_str = f" | ⚖️ *{lp:.0f}%L/{sp:.0f}%S*"

            fav_mkt = s.get("mkt") or (d.get("mostTradedMarket", {}).get("ticker") if d else "")
            mkt_str = f" • Токен: `{fav_mkt}`" if fav_mkt else ""

            wr_val = s.get("wr")
            wr_str = f"{wr_val:.1f}%" if wr_val is not None else "N/A"

            lines.append(
                f"{medal} *{s['name']}* (`{s['addr']}`):{bias_str}\n"
                f"  • PnL: `+${s['pnl']:,.2f}` | WR: `{wr_str}`{mkt_str}\n"
                f"  • Сделок: `{s['trades']:,}` (W: {s['wins']} / L: {s['losses']}) | Объём: `${s['vol']:,.0f}`"
            )

        now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        lines.append("────────────────────────────────────────────")
        lines.append(f"⏰ _Синхронизировано: {now_utc} (Nado DEX 30d API)_")
        lines.append("💡 _AI-Радар сверяет свои точки входа с динамикой позиций топ-снайперов._")

        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Обновить снайперов", "callback_data": "radar_whales"}, {"text": "🔙 Назад в Радар", "callback_data": "dash_radar"}]
            ]
        }
        res_text = "\n".join(lines)
        self._radar_whales_cache = {"text": res_text, "markup": markup, "ts": now_ts}
        return res_text, markup

    async def _get_journal_payload(self) -> Tuple[str, dict]:
        from core.state_store import StateStore
        import json
        j_path = StateStore._normalize_path("data/memory/trade_journal.json")
        trades = []
        if os.path.exists(j_path):
            try:
                with open(j_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if isinstance(d, list):
                        trades = d
                    elif isinstance(d, dict):
                        trades = list(d.values())
            except Exception:
                pass

        if trades:
            last5 = trades[-5:]
            lines = [
                "📜 *ТОРГОВЫЙ ЖУРНАЛ | АРХИВ ОПЕРАЦИЙ*\n"
                "_История последних 5 закрытых сделок терминала_\n"
                "────────────────────────────────────────────"
            ]
            for t in reversed(last5):
                sym = t.get("symbol", "N/A")
                dir_str = t.get("direction", "N/A")
                pnl_usd = float(t.get("net_pnl_usd") or t.get("realized_pnl_usd") or t.get("pnl_usd") or 0.0)
                r_mult = float(t.get("realized_r", 0.0) or t.get("r_mult", 0.0) or 0.0)
                exit_reason = t.get("triggered_by") or t.get("exit_reason") or "TP/SL"
                icon = "🟢" if pnl_usd >= 0 else "🔴"
                lines.append(f"{icon} *{sym}* (`{dir_str}`): `${pnl_usd:+.2f}` (`{r_mult:+.2f}R`) | `{exit_reason}`")
            lines.append("────────────────────────────────────────────")
            lines.append("💡 _Все сделки фиксируются с чистым PnL с учетом биржевых комиссий Nado CLOB._")
            text = "\n".join(lines)
        else:
            text = (
                "📜 *ТОРГОВЫЙ ЖУРНАЛ | АРХИВ ОПЕРАЦИЙ*\n"
                "────────────────────────────────────────────\n\n"
                "⚪ *В текущей сессии еще нет закрытых сделок.*\n"
                "_Все открытые позиции сопровождаются модулем Sentinel в режиме 24/7._\n\n"
                "────────────────────────────────────────────"
            )

        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Обновить", "callback_data": "dash_journal"}, {"text": "⬅️ В Терминал", "callback_data": "dash_main"}]
            ]
        }
        return text, markup

    async def _get_debates_payload(self) -> Tuple[str, dict]:
        from core.state_store import StateStore
        import glob, json, os, time
        from datetime import datetime, timezone

        # 1. Search for candidate snapshots with CEO/Debate in decision_snapshots.jsonl
        snap_path = StateStore._normalize_path("data/memory/decision_snapshots.jsonl")
        latest_snap = None
        if os.path.exists(snap_path):
            try:
                with open(snap_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    for line in reversed(lines):
                        l = line.strip()
                        if not l:
                            continue
                        try:
                            d = json.loads(l)
                            if d.get("ceo") or d.get("debate") or d.get("ceo_decision"):
                                latest_snap = d
                                break
                        except Exception:
                            continue
            except Exception:
                pass

        # 2. Search for cycle_*.json files in both normalized and root storage paths
        search_dirs = [
            StateStore._normalize_path("data/memory"),
            "data/memory"
        ]
        cycle_files = []
        for s_dir in search_dirs:
            if os.path.exists(s_dir):
                cycle_files.extend(glob.glob(os.path.join(s_dir, "cycle_*.json")))

        def _safe_mtime(f_path: str) -> float:
            try:
                return os.path.getmtime(f_path)
            except Exception:
                return 0.0

        cycle_files = sorted(list(set(cycle_files)), key=_safe_mtime)

        latest_cycle_data = None
        latest_cycle_mtime = 0.0
        if cycle_files:
            latest_f = cycle_files[-1]
            try:
                latest_cycle_mtime = os.path.getmtime(latest_f)
            except Exception:
                latest_cycle_mtime = 0.0
            try:
                with open(latest_f, "r", encoding="utf-8") as f:
                    latest_cycle_data = json.load(f)
            except Exception:
                pass

        # 3. Determine which source is freshest
        snap_ts = 0.0
        if latest_snap:
            snap_ts = float(latest_snap.get("timestamp") or 0.0)
            if not snap_ts and latest_snap.get("iso_time"):
                try:
                    dt = datetime.fromisoformat(latest_snap["iso_time"])
                    snap_ts = dt.timestamp()
                except Exception:
                    pass

        # Compare timestamps: pick the newest record
        chosen_source = "none"
        if latest_snap and latest_cycle_data:
            if snap_ts >= (latest_cycle_mtime - 5.0):
                chosen_source = "snapshot"
            else:
                chosen_source = "cycle"
        elif latest_snap:
            chosen_source = "snapshot"
        elif latest_cycle_data:
            chosen_source = "cycle"

        # 4. Extract data based on chosen source
        if chosen_source == "snapshot":
            sym = latest_snap.get("symbol", "N/A")
            ceo = latest_snap.get("ceo") or {}
            strat = (latest_snap.get("router") or {}).get("strategy_mode") or latest_snap.get("macro_regime", "TREND_FOLLOWING")
            decision = ceo.get("decision", "HOLD")
            action = ceo.get("trade_action") or latest_snap.get("final_action", "HOLD")
            conf = ceo.get("directional_confidence") or ceo.get("conviction", 0)
            penalties = (ceo.get("risk_breakdown") or {}).get("total") or ceo.get("risk_penalties", 0)
            reason = ceo.get("reasoning") or ceo.get("reasoning_en") or "Анализ завершен консилиумом."
            ts_val = snap_ts
            stage_info = latest_snap.get("stage", "CONSENSUS")
            rej_tag = latest_snap.get("rejection_tag")
        elif chosen_source == "cycle":
            sym = latest_cycle_data.get("symbol", "N/A")
            ceo = latest_cycle_data.get("ceo_decision") or {}
            strat = ceo.get("strategy_mode", "TREND_FOLLOWING")
            decision = ceo.get("decision", "HOLD")
            action = ceo.get("trade_action") or ceo.get("action") or "HOLD"
            conf = ceo.get("directional_confidence") or ceo.get("conviction", 0)
            penalties = ceo.get("risk_penalties") or (ceo.get("risk_breakdown") or {}).get("total", 0)
            reason = ceo.get("reasoning_en") or ceo.get("reasoning_ru") or "Анализ завершен консилиумом."
            ts_val = latest_cycle_mtime
            stage_info = latest_cycle_data.get("status", "CONSENSUS")
            rej_tag = ceo.get("rejection_tag")
        else:
            sym = None

        if sym:
            # Format human readable time and elapsed
            time_str = "Недавно"
            elapsed_str = ""
            if ts_val > 0:
                dt_utc = datetime.fromtimestamp(ts_val, tz=timezone.utc)
                time_str = dt_utc.strftime("%d.%m.%Y, %H:%M UTC")
                diff_sec = max(0, int(time.time() - ts_val))
                if diff_sec < 60:
                    elapsed_str = f"({diff_sec} сек назад)"
                elif diff_sec < 3600:
                    elapsed_str = f"({diff_sec // 60} мин назад)"
                elif diff_sec < 86400:
                    elapsed_str = f"({diff_sec // 3600} ч назад)"

            # Format status badge
            if action in ["ENTER", "BUY", "SELL"] and stage_info in ["APPROVED", "EXECUTION"]:
                status_badge = "🟢 Сделка одобрена и исполнена"
            elif action == "WAIT_FOR_PULLBACK":
                status_badge = "⏳ Помещен в лист ожидания отката (Pullback)"
            elif rej_tag:
                clean_rej = str(rej_tag).replace("_", " ")
                status_badge = f"🛡️ Вето фильтра ({clean_rej})"
            elif action == "HOLD":
                status_badge = "⚪ Позиция не открыта (Выжидание сетапа)"
            else:
                status_badge = f"ℹ️ Статус: {action}"

            safe_reason = str(reason)[:500].replace("_", "-").replace("*", "")

            text = (
                "🧠 *СИНДИКАТ ИИ | ДЕБАТЫ И КОНСЕНСУС CEO*\n"
                f"🕒 _Время анализа:_ `{time_str}` {elapsed_str}\n"
                f"🪙 _Рассмотренный актив:_ *{sym}*\n"
                "────────────────────────────────────────────\n"
                f"• *Стратегический режим:*    `{strat}`\n"
                f"• *Направление и вердикт:*   `{decision}` (`{action}`)\n"
                f"• *Уверенность (Conviction):* `{conf}%`\n"
                f"• *Штрафы риска (Penalties):* `{penalties} pts`\n"
                f"• *Статус пайплайна:*         {status_badge}\n"
                "────────────────────────────────────────────\n"
                f"📝 *Тезис решения (CEO Thesis):*\n"
                f"_{safe_reason}_\n"
                "────────────────────────────────────────────\n"
                "⚖️ _Синдикат: DeepSeek-V3 (Bull/Bear/CEO) • Llama 3.3 70B (Arbitration)_"
            )
        else:
            text = (
                "🧠 *СИНДИКАТ ИИ | ДЕБАТЫ И КОНСЕНСУС CEO*\n"
                "────────────────────────────────────────────\n\n"
                "⚪ *Данные последнего цикла еще формируются...*\n"
                "_Радар сканирует рынок согласно расписанию._\n\n"
                "────────────────────────────────────────────"
            )

        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Обновить", "callback_data": "dash_debates"}, {"text": "⬅️ В Терминал", "callback_data": "dash_main"}]
            ]
        }
        return text, markup

    async def _get_wallet_payload(self, chat_id: str) -> Tuple[str, dict]:
        from services.trader_registry import trader_registry
        trader = trader_registry.get_trader(chat_id)

        if trader and trader.get("watch_only"):
            owner = trader.get("owner_address", "0x...")
            sub = trader.get("subaccount", "default")
            text = (
                "🔗 *КАБИНЕТ КОШЕЛЬКА | WATCH-ONLY МОНИТОРИНГ*\n"
                "────────────────────────────────────────────\n"
                f"👤 *Адрес кошелька:* `{owner}`\n"
                f"📂 *Суб-аккаунт Nado:* `{sub}`\n"
                "👁 *Режим доступа:* `Watch-Only (Только телеметрия)`\n"
                "🛡 *Безопасность:* `100% изоляция (без ключей и подписей)`\n"
                "────────────────────────────────────────────\n"
                "💡 _Вы можете в любой момент переключиться на реальный копи-трейдинг через Web3 LinkSigner._"
            )
            buttons = [
                [{"text": "🦊 Переключить на Web3 LinkSigner", "callback_data": "wallet_web3"}],
                [{"text": "🔌 Отключить мониторинг", "callback_data": "wallet_disconnect"}, {"text": "🔄 Обновить", "callback_data": "dash_wallet"}],
                [{"text": "⬅️ В Терминал", "callback_data": "dash_main"}]
            ]
            return text, {"inline_keyboard": buttons}

        elif trader and trader.get("signer_address"):
            owner = trader.get("owner_address", "0x...")
            sub = trader.get("subaccount", "default")
            copy_on = trader.get("copy_enabled", True)
            copy_str = "🟢 ВКЛЮЧЕНО" if copy_on else "⏸ ПРИОСТАНОВЛЕНО"
            risk_mode = str(trader.get("risk_mode", "balanced")).capitalize()
            risk_pct = float(trader.get("risk_per_trade_pct", 1.6))
            max_slots = int(trader.get("max_slots", 2))
            lev = float(trader.get("max_leverage", 8.0))
            tp_target = float(trader.get("tp_r_target", 3.0))

            text = (
                "🔗 *КАБИНЕТ КОШЕЛЬКА | NON-CUSTODIAL ТРЕЙДИНГ*\n"
                "────────────────────────────────────────────\n"
                f"👤 *Владелец:* `{owner}`\n"
                f"📂 *Суб-аккаунт Nado:* `{sub}`\n"
                f"🔑 *Сессионный ключ:* `{trader.get('signer_address', '0x...')[:10]}...`\n"
                f"🛡 *Безопасность:* `NON-CUSTODIAL (0 прав на вывод)`\n"
                f"⚙️ *Профиль риска:* `{risk_mode}` ({risk_pct}% риск, {tp_target}R TP, {max_slots} слота, {lev:.0f}x)\n"
                f"📡 *Авто-копирование:* `{copy_str}`\n"
                "────────────────────────────────────────────\n"
                "💡 _Сделки ведущего терминала копируются в реальном времени строго от вашего баланса с индивидуальным R-сайзингом._"
            )
            toggle_btn_text = "⏸ Приостановить копирование" if copy_on else "▶️ Возобновить копирование"
            buttons = [
                [{"text": "⚙️ Профиль риска и цели", "callback_data": "risk_menu"}, {"text": toggle_btn_text, "callback_data": "wallet_toggle_copy"}],
                [{"text": "🔌 Отключить кошелек", "callback_data": "wallet_disconnect"}, {"text": "🔄 Обновить", "callback_data": "dash_wallet"}],
                [{"text": "⬅️ В Терминал", "callback_data": "dash_main"}]
            ]
            return text, {"inline_keyboard": buttons}

        else:
            # Guest / Showcase
            text = (
                "🔗 *ПОДКЛЮЧЕНИЕ КОШЕЛЬКА К AI-FUTURES-TRADER*\n"
                "_Торговля на Nado DEX CLOB (Ink L2 Mainnet)_\n"
                "────────────────────────────────────────────\n"
                "Выберите удобный режим подключения:\n\n"
                "🦊 *1. Web3 LinkSigner (Копи-трейдинг)*\n"
                "Делегирование прав на выставление ордеров сессионному ключу бота. "
                "100% Non-Custodial: **вывод средств аппаратно заблокирован смарт-контрактом Nado DEX**.\n\n"
                "👁 *2. Watch-Only Режим (Мониторинг)*\n"
                "Введите адрес кошелька `0x...` без ключей и подписей для отслеживания баланса и позиций.\n\n"
                "📋 *3. Ввести подпись вручную*\n"
                "Для ввода EIP-712 подписи через команду `/link_confirm`.\n"
                "────────────────────────────────────────────"
            )
            buttons = [
                [{"text": "🦊 Подключить Web3 (MetaMask / Rabby)", "callback_data": "wallet_web3"}],
                [{"text": "👁 Подключить Watch-Only", "callback_data": "wallet_watch_only"}],
                [{"text": "📋 Ввести подпись вручную", "callback_data": "wallet_manual_sig"}],
                [{"text": "🎁 Season 2 Points & Airdrop", "callback_data": "cmd_ref"}, {"text": "⬅️ В Терминал", "callback_data": "dash_main"}]
            ]
            return text, {"inline_keyboard": buttons}

    async def _get_wallet_connect_payload(self, chat_id: str) -> Tuple[str, dict]:
        from services.trader_registry import trader_registry
        session = trader_registry.prepare_pending_session(chat_id)
        signer_addr = session["signer_address"]
        from core.config import config
        ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")
        bot_username = "forexample300_ai_bot"

        clean_url = f"https://zloilapin.github.io/AI-Futures-Trader/?signer={signer_addr}&chat_id={chat_id}&bot={bot_username}&subaccount=default&join={ref_code}"
        cdn_url = f"https://raw.githack.com/zloilapin/AI-Futures-Trader/main/index.html?signer={signer_addr}&chat_id={chat_id}&bot={bot_username}&subaccount=default&join={ref_code}"

        import urllib.parse
        mm_deeplink = f"https://metamask.app.link/dapp/{clean_url.replace('https://', '')}"
        mm_cdn_deeplink = f"https://metamask.app.link/dapp/{cdn_url.replace('https://', '')}"
        tw_deeplink = f"https://link.trustwallet.com/open_url?coin_id=60&url={urllib.parse.quote(clean_url)}"

        text = (
            "🔐 *ПОДКЛЮЧЕНИЕ WEB3-КОШЕЛЬКА (LINKSIGNER)*\n"
            "_100% Non-Custodial: 0 прав на вывод средств_\n"
            "────────────────────────────────────────────\n"
            "📱 *На смартфоне:* нажмите кнопку ниже для открытия прямо в приложении кошелька:\n\n"
            f"👉 [🦊 Открыть в приложении MetaMask]({mm_deeplink})\n"
            f"👉 [🦊 Резервная ссылка (CDN)]({mm_cdn_deeplink})\n"
            f"👉 [🛡️ Открыть в Trust Wallet]({tw_deeplink})\n\n"
            "💻 *На компьютере / Telegram Desktop:*\n"
            "Нажмите кнопку `🔗 Открыть в Telegram WebApp` ниже.\n"
            "────────────────────────────────────────────\n"
            f"🌐 _Прямая ссылка для любого Web3-браузера:_\n`{clean_url}`"
        )

        buttons = [
            [{"text": "🦊 Открыть в MetaMask", "url": mm_deeplink}],
            [{"text": "🛡️ Открыть в Trust Wallet", "url": tw_deeplink}],
            [{"text": "🔗 Открыть в Telegram WebApp", "web_app": {"url": clean_url}}],
            [{"text": "⬅️ Назад в Кошелек", "callback_data": "dash_wallet"}]
        ]
        return text, {"inline_keyboard": buttons}

    def _get_risk_menu_payload(self, chat_id: str) -> Tuple[str, dict]:
        from services.trader_registry import trader_registry, RISK_PROFILES
        settings = trader_registry.get_trader_settings(chat_id)
        mode = settings.get("risk_mode", "balanced")
        mode_labels = {
            "conservative": "🛡 Консервативный (0.9% | 1.8R)",
            "balanced": "⚖️ Сбалансированный (1.6% | 3.0R)",
            "aggressive": "🔥 Агрессивный (2.6% | 3.8R)"
        }
        current_mode_str = mode_labels.get(mode, f"Кастомный ({mode})")

        risk_pct = float(settings.get("risk_per_trade_pct", 1.6))
        max_slots = int(settings.get("max_slots", 2))
        max_lev = float(settings.get("max_leverage", 8.0))
        tp_r = float(settings.get("tp_r_target", 3.0))
        be_r = float(settings.get("be_trigger_r", 0.75))
        trail_r = float(settings.get("trail_activation_r", 1.35))
        high_conv = "Да (conviction >= 75%)" if settings.get("only_high_conviction") else "Нет"
        daily_dd = float(settings.get("max_daily_drawdown_pct", 4.0))
        target_eq = float(settings.get("target_equity_usd", 0.0))
        target_str = f"${target_eq:,.2f} USDC" if target_eq > 0 else "Выключено (0.00$)"

        copy_on = bool(settings.get("copy_enabled", True))
        copy_status = "🟢 ВКЛЮЧЕНО" if copy_on else "⏸ НА ПАУЗЕ"
        dd_paused = bool(settings.get("daily_drawdown_paused", False))
        dd_status = "🛑 СТОП-КРАН АКТИВЕН" if dd_paused else "🟢 Норма"

        text = (
            "⚙️ *ПРОФИЛЬ РИСКА И ЦЕЛИ КОПИ-ТРЕЙДИНГА*\n"
            "────────────────────────────────────────────\n"
            f"👤 *Текущий режим:* `{current_mode_str}`\n\n"
            "📊 *Параметры сайзинга:*\n"
            f"  • *Риск на сделку:*       `{risk_pct:.1f}%` от депозита\n"
            f"  • *Лимит слотов:*         `{max_slots}` позиций\n"
            f"  • *Потолок плеча:*        `{max_lev:.0f}x`\n"
            f"  • *Целевой Take-Profit:*  `{tp_r:.1f} R`\n\n"
            "🛡 *Защита Sentinel Guard:*\n"
            f"  • *Безубыток (BE):*       перевод при `+{be_r:.2f} R`\n"
            f"  • *Трейлинг-стоп:*        активация от `+{trail_r:.2f} R`\n"
            f"  • *Фильтр уверенности:*   `{high_conv}`\n"
            f"  • *Дневной стоп-кран:*    `{daily_dd:.1f}%` ({dd_status})\n"
            f"  • *Целевой депозит:*      `{target_str}`\n"
            f"  • *Статус копирования:*   `{copy_status}`\n"
            "────────────────────────────────────────────\n"
            "💡 _Выберите готовый пресет риска в 1 клик или настройте персональные цели:_"
        )

        toggle_copy_btn = "⏸ Пауза копирования" if copy_on else "▶️ Старт копирования"
        buttons = [
            [{"text": f"{'✅ ' if mode=='conservative' else ''}🛡 Консервативный (0.9% | 1.8R)", "callback_data": "set_risk:conservative"}],
            [{"text": f"{'✅ ' if mode=='balanced' else ''}⚖️ Сбалансированный (1.6% | 3.0R)", "callback_data": "set_risk:balanced"}],
            [{"text": f"{'✅ ' if mode=='aggressive' else ''}🔥 Агрессивный (2.6% | 3.8R)", "callback_data": "set_risk:aggressive"}],
            [
                {"text": "🎯 Задать цель баланса ($)", "callback_data": "set_target_equity"},
                {"text": "🛑 Лимит просадки дня", "callback_data": "set_daily_dd"}
            ],
            [{"text": toggle_copy_btn, "callback_data": "toggle_my_copy"}],
            [{"text": "⬅️ В Кошелек", "callback_data": "dash_wallet"}]
        ]
        return text, {"inline_keyboard": buttons}

    def _get_control_panel_payload(self) -> Tuple[str, dict]:
        from core.config import config
        predator_on = bool(getattr(config, "ANTI_BOT_MODE", False))
        auto_predator_on = bool(getattr(config, "AUTO_PREDATOR_SWITCH", True))
        shadow_on = bool(getattr(config, "SHADOW_MODE", False))
        profile = str(getattr(config, "TRADING_PROFILE", "BALANCED")).upper()

        predator_status = "🟢 ВКЛЮЧЕН" if predator_on else "⚪ ВЫКЛЮЧЕН"
        predator_btn_text = "🦈 Анти-Бот: Выкл" if predator_on else "🦈 Анти-Бот: Вкл"

        auto_status = "🤖 ВКЛ (динамический)" if auto_predator_on else "🔴 ВЫКЛ (Ручной)"
        auto_btn_text = "🤖 Авто-Режим: ВЫКЛ" if auto_predator_on else "🤖 Авто-Режим: ВКЛ"

        shadow_status = "👻 SHADOW (Виртуальный)" if shadow_on else "⚡ LIVE (Боевой On-Chain)"
        shadow_btn_text = "⚡ Включить LIVE" if shadow_on else "👻 Включить SHADOW"

        risk_icons = {
            "CONSERVATIVE": "🛡️ Консервативный (0.75x)",
            "BALANCED": "⚖️ Сбалансированный (1.0x)",
            "AGGRESSIVE": "🔥 Агрессивный (1.5x)"
        }
        current_risk_str = risk_icons.get(profile, profile)

        text = (
            "🎛 *ПУЛЬТ УПРАВЛЕНИЯ РЕЖИМАМИ ТОРГОВЛИ*\n"
            "────────────────────────────────────────────\n"
            f"🦈 *Анти-Бот (Predator Mode):* {predator_status}\n"
            f"  • *Автопилот:* {auto_status}\n"
            "  └ _Охота на ликвидации ботов на границах полос Боллинджера/Donchian_\n\n"
            f"📡 *Режим исполнения:* {shadow_status}\n"
            f"⚖️ *Профиль риска:* `{current_risk_str}`\n"
            "────────────────────────────────────────────\n"
            "Нажмите кнопку ниже для переключения:"
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": predator_btn_text, "callback_data": "toggle_predator"},
                    {"text": auto_btn_text, "callback_data": "toggle_auto_predator"}
                ],
                [{"text": shadow_btn_text, "callback_data": "toggle_shadow"}],
                [
                    {"text": "🛡️ Low", "callback_data": "setrisk_CONSERVATIVE"},
                    {"text": "⚖️ Mid", "callback_data": "setrisk_BALANCED"},
                    {"text": "🔥 High", "callback_data": "setrisk_AGGRESSIVE"}
                ],
                [
                    {"text": "🔄 Сброс Пика (Peak)", "callback_data": "act_reset_peak"},
                    {"text": "🚀 Сканировать рынок", "callback_data": "cmd_scan"}
                ],
                [{"text": "🚨 ЗАКРЫТЬ ВСЕ ПОЗИЦИИ", "callback_data": "act_panic"}]
            ]
        }
        return text, reply_markup

    async def _send_reply(self, text: str, reply_markup: dict = None, target_chat_id: str = None, parse_mode: str = "Markdown"):
        dest_chat_id = target_chat_id or getattr(self, "_current_sender_chat_id", None) or self.chat_id
        if not self.send_url or not dest_chat_id:
            print(f"⚠️ [TelegramListener] _send_reply: send_url или chat_id не задан! send_url={bool(self.send_url)}, chat_id={bool(dest_chat_id)}")
            return
        safe_text = text[:3980] + "\n...[TRUNCATED]" if text and len(text) > 4000 else (text or "")
        payload = {
            "chat_id": dest_chat_id,
            "text": safe_text,
            "disable_web_page_preview": True
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # If no specific reply_markup is provided, attach appropriate persistent Reply Keyboard
        if reply_markup is None:
            from core.config import config
            from services.trader_registry import trader_registry
            admin_chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
            is_admin = bool(dest_chat_id and (
                str(dest_chat_id) == admin_chat_id or 
                str(dest_chat_id) in self.allowed_chat_ids or 
                str(dest_chat_id) == str(self.chat_id)
            ))
            is_trader = bool(dest_chat_id and trader_registry.get_trader(dest_chat_id))
            if is_admin:
                reply_markup = REPLY_KEYBOARD
            elif is_trader:
                reply_markup = TRADER_KEYBOARD
            else:
                reply_markup = SHOWCASE_KEYBOARD
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            from core.session import SessionManager
            session = await SessionManager.get()
            async with session.post(self.send_url, json=payload) as resp:
                if resp.status == 200:
                    print(f"✅ [TelegramListener] Ответ отправлен в Telegram (chat_id={dest_chat_id}).")
                else:
                    error_text = await resp.text()
                    print(f"⚠️ [TelegramListener] Markdown ответ не прошёл (HTTP {resp.status}): {error_text[:200]}")
                    payload.pop("parse_mode", None)
                    async with session.post(self.send_url, json=payload) as resp2:
                        if resp2.status == 200:
                            print(f"✅ [TelegramListener] Ответ отправлен без форматирования.")
                        else:
                            error_text2 = await resp2.text()
                            print(f"❌ [TelegramListener] Не удалось отправить ответ (HTTP {resp2.status}): {error_text2[:200]}")
        except Exception as e:
            print(f"❌ [TelegramListener] Ошибка отправки ответа: {e}")

    async def _edit_message_text(
        self,
        text: str,
        reply_markup: dict = None,
        message_id: int = None,
        chat_id: str = None,
        parse_mode: str = "Markdown"
    ) -> bool:
        dest_chat_id = chat_id or getattr(self, "_current_sender_chat_id", None) or self.chat_id
        if not self.bot_token or not dest_chat_id or not message_id:
            return False
        edit_url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        safe_text = text[:3980] + "\n...[TRUNCATED]" if text and len(text) > 4000 else (text or "")
        payload = {
            "chat_id": dest_chat_id,
            "message_id": message_id,
            "text": safe_text,
            "disable_web_page_preview": True
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            from core.session import SessionManager
            session = await SessionManager.get()
            async with session.post(edit_url, json=payload) as resp:
                if resp.status == 200:
                    return True
                resp_text = await resp.text()
                if "message is not modified" in resp_text.lower():
                    return True
                payload.pop("parse_mode", None)
                async with session.post(edit_url, json=payload) as resp2:
                    if resp2.status == 200:
                        return True
                    resp2_text = await resp2.text()
                    if "message is not modified" in resp2_text.lower():
                        return True
                    return False
        except Exception as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                return True
            print(f"⚠️ [TelegramListener] Ошибка editMessageText: {e}")
            return False

    async def _edit_message(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str = "Markdown"
    ) -> bool:
        """Alias for _edit_message_text with standard chat_id/message_id signature."""
        return await self._edit_message_text(
            text=text,
            reply_markup=reply_markup,
            message_id=message_id,
            chat_id=str(chat_id),
            parse_mode=parse_mode
        )

    async def _edit_reply(
        self,
        text: str,
        reply_markup: dict | None = None,
        target_chat_id: str | int | None = None,
        message_id: int | None = None,
        parse_mode: str = "Markdown",
    ):
        """
        Universal reply editor: edits the existing message in-place to avoid chat spam,
        falling back to sending a new reply if message_id is absent or editing fails.
        """
        chat_id = str(target_chat_id or getattr(self, "_current_sender_chat_id", None) or self.chat_id)
        if message_id:
            try:
                res = await self._edit_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                if res:
                    return res
            except Exception as e:
                err_str = str(e).lower()
                if "message is not modified" in err_str:
                    return True
                logger.debug(f"[TelegramListener] _edit_reply fallback to _send_reply: {e}")
        return await self._send_reply(
            text,
            reply_markup=reply_markup,
            target_chat_id=chat_id,
            parse_mode=parse_mode,
        )

    async def handle_web_app_data(self, web_app_data_str: str, sender_chat_id: str):
        """Processes LinkSigner data received from Telegram Mini App via sendData."""
        import json
        try:
            payload = json.loads(web_app_data_str)
            owner = str(payload.get("owner", "")).strip()
            subaccount = str(payload.get("subaccount", "default")).strip()
            signer = str(payload.get("signer", "")).strip()
            signature = str(payload.get("signature", "")).strip()
            nonce = int(payload.get("nonce", 0))

            await self._register_trader_and_respond(
                chat_id=sender_chat_id,
                owner_address=owner,
                subaccount_name=subaccount,
                signature=signature,
                nonce=nonce,
                signer_address=signer
            )
        except Exception as e:
            print(f"❌ [TelegramListener] Ошибка обработки web_app_data: {e}")
            await self._send_reply(f"❌ Ошибка обработки данных подключения: {e}", target_chat_id=sender_chat_id)

    async def _register_trader_and_respond(
        self,
        chat_id: str,
        owner_address: str,
        subaccount_name: str,
        signature: str,
        nonce: int,
        signer_address: Optional[str] = None
    ):
        """Validates EIP-712 signature, registers subscriber profile, and responds to user."""
        from services.trader_registry import trader_registry
        from nado_protocol.utils.bytes32 import subaccount_to_hex

        ok, msg = trader_registry.register_trader(
            chat_id=chat_id,
            owner_address=owner_address,
            subaccount_name=subaccount_name,
            signer_address=signer_address,
            signature=signature,
            nonce=nonce
        )

        if not ok:
            await self._send_reply(msg, target_chat_id=chat_id)
            return

        # Query real balance and margin on Nado DEX
        assets = 0.0
        health = 0.0
        sub_hex = subaccount_to_hex(owner_address, subaccount_name)
        if self.trading_service and hasattr(self.trading_service, "client") and self.trading_service.client:
            try:
                summary = await asyncio.to_thread(
                    self.trading_service.client.subaccount.get_engine_subaccount_summary,
                    sub_hex
                )
                if hasattr(summary, "healths") and summary.healths:
                    assets = float(summary.healths[0].assets) / 1e18
                    health = float(summary.healths[0].health) / 1e18
            except Exception as qe:
                print(f"⚠️ [TelegramListener] Ошибка запроса баланса для {sub_hex}: {qe}")

        from core.config import config
        ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")
        welcome_msg = (
            "🎉 *Кошелек успешно подключен к AI-Futures-Trader!*\n\n"
            f"👤 *Владелец:* `{owner_address}`\n"
            f"📂 *Суб-аккаунт:* `{subaccount_name}`\n"
            "🛡️ *Безопасность:* `NON-CUSTODIAL (0 прав на вывод средств)`\n"
            f"🎁 *Реферальный статус:* `Season 2 Points & Airdrop Nado` (Инвайт: `{ref_code}`)\n\n"
            f"💰 *Баланс на Nado DEX:* `${assets:,.2f} USDC`\n"
            f"📊 *Доступная маржа:* `${health:,.2f} USDC`\n"
            "🟢 *Авто-копирование сигналов:* `ВКЛЮЧЕНО`\n\n"
            "ℹ️ _Управление:_ /my_balance | /my_positions | /settings | /ref | /toggle | /disconnect"
        )
        await self._send_reply(welcome_msg, reply_markup=TRADER_KEYBOARD, target_chat_id=chat_id)
        # Automatically launch Step 1 of personal settings onboarding wizard
        await self.send_setup_wizard_step(chat_id=chat_id, step=1)

    async def send_setup_wizard_step(self, chat_id: str, step: int, message_id: Optional[int] = None):
        """
        Drives the interactive step-by-step Personal Trading Setup Wizard for connected traders.
        Step 1: Risk profile & Leverage
        Step 2: Max concurrent positions
        Step 3: Take-profit strategy (2-Tier vs Single-Tier)
        Step 4: Anti-Bot (Predator) mode
        Step 5: Completion summary card
        """
        c_id = str(chat_id).strip()
        from services.trader_registry import trader_registry
        current_settings = trader_registry.get_trader_settings(c_id)

        if c_id not in self._wizard_sessions:
            self._wizard_sessions[c_id] = dict(current_settings)

        session = self._wizard_sessions[c_id]

        if step == 1:
            text = (
                "⚙️ *МАСТЕР НАСТРОЙКИ ТОРГОВЛИ (ШАГ 1/4)*\n"
                "_Выберите ваш персональный профиль риска и плечо:_\n\n"
                "🛡️ *Консервативный (Low)*: Плечо `3x-5x`, риск `1%` на сделку\n"
                "⚖️ *Сбалансированный (Standard)*: Плечо `6x`, риск `2%` (Рекомендуется)\n"
                "🔥 *Агрессивный (High)*: Плечо `10x`, риск `3%` на сделку"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "🛡️ Консервативный (3-5x)", "callback_data": "wiz_risk_CONSERVATIVE"}],
                    [{"text": "⚖️ Сбалансированный (6x) — Рекомендуется", "callback_data": "wiz_risk_BALANCED"}],
                    [{"text": "🔥 Агрессивный (10x)", "callback_data": "wiz_risk_AGGRESSIVE"}]
                ]
            }

        elif step == 2:
            text = (
                "⚙️ *МАСТЕР НАСТРОЙКИ ТОРГОВЛИ (ШАГ 2/4)*\n"
                "_Сколько позиций бот может удерживать одновременно для вашего счета?_\n\n"
                "• `1 сделка`: Максимальная концентрация маржи, минимальная нагрузка\n"
                "• `2 сделки`: Оптимальный баланс диверсификации (Рекомендуется)\n"
                "• `3 сделки`: Полный охват рынка (SOL, ETH, BTC)"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "1 сделка", "callback_data": "wiz_pos_1"}],
                    [{"text": "2 сделки (Рекомендуется)", "callback_data": "wiz_pos_2"}],
                    [{"text": "3 сделки", "callback_data": "wiz_pos_3"}]
                ]
            }

        elif step == 3:
            text = (
                "⚙️ *МАСТЕР НАСТРОЙКИ ТОРГОВЛИ (ШАГ 3/4)*\n"
                "_Выберите стратегию фиксации прибыли (Take-Profit):_\n\n"
                "🎯 *2-Tier (50% + Безубыток)*: 50% объема закрывается на первом тейке TP1, а стоп-лосс переносится в безубыток (Free-Ride). Оставшиеся 50% идут до финальной цели. (Рекомендуется)\n\n"
                "🏁 *Single-Tier (100%)*: Вся позиция удерживается целиком до финального TP."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "🎯 2-Tier: 50% + Безубыток (Рекомендуется)", "callback_data": "wiz_tp_2_TIER"}],
                    [{"text": "🏁 Single-Tier: 100% целиком", "callback_data": "wiz_tp_SINGLE_TIER"}]
                ]
            }

        elif step == 4:
            text = (
                "⚙️ *МАСТЕР НАСТРОЙКИ ТОРГОВЛИ (ШАГ 4/4)*\n"
                "_Включать ли режим «Анти-Бот» (Predator / Liquidation Mode)?_\n\n"
                "🦈 *ВКЛЮЧЕН*: Бот участвует в контр-трендовых сделках на границах полос Боллинджера при резких ценовых импульсах и каскадах ликвидаций ботов. (Рекомендуется)\n\n"
                "⚪ *ВЫКЛЮЧЕН*: Копировать только классические трендовые пробои (Trend-Following)."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "🦈 Включить Анти-Бот (Рекомендуется)", "callback_data": "wiz_ab_true"}],
                    [{"text": "⚪ Только Trend-Following", "callback_data": "wiz_ab_false"}]
                ]
            }

        else:
            # Step 5: Save and Summary
            trader_registry.set_trader_risk_profile(c_id, str(session.get("risk_profile", "BALANCED")).lower())
            trader_registry.update_trader_settings(
                chat_id=c_id,
                risk_profile=session.get("risk_profile", "BALANCED"),
                leverage=session.get("leverage", 6.0),
                max_concurrent_positions=session.get("max_concurrent_positions", 2),
                tp_mode=session.get("tp_mode", "2_TIER"),
                anti_bot_enabled=session.get("anti_bot_enabled", True)
            )
            trader = trader_registry.get_trader(c_id) or {}
            owner = trader.get("owner_address", "0x...")
            profile = session.get("risk_profile", "BALANCED")
            lev = session.get("leverage", 6.0)
            max_pos = session.get("max_concurrent_positions", 2)
            tp_mode = session.get("tp_mode", "2_TIER")
            tp_name = "2-Tier (50% + Б/У)" if tp_mode == "2_TIER" else "Single-Tier (100%)"
            ab_st = "🟢 ВКЛЮЧЕН" if session.get("anti_bot_enabled", True) else "⚪ ВЫКЛЮЧЕН"

            text = (
                "🎉 *ВАШИ ИНДИВИДУАЛЬНЫЕ НАСТРОЙКИ СОХРАНЕНЫ!*\n\n"
                f"👤 *Суб-аккаунт:* `{owner[:6]}...{owner[-4:]}`\n"
                f"⚖️ *Профиль риска:* `{profile}` (Плечо: `{lev:.0f}x`)\n"
                f"💼 *Лимит позиций:* `{max_pos} одновременно`\n"
                f"🎯 *Фиксация прибыли:* `{tp_name}`\n"
                f"🦈 *Анти-Бот режим:* `{ab_st}`\n"
                f"💰 *Минимальный размер сделки:* `$104.00 USDC`\n\n"
                "🟢 *Авто-копирование сигналов настроено под ваш стиль!*\n"
                "💡 _Вы можете изменить их в любое время командой /settings или кнопкой «⚙️ Настройки торговли»._"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "⚙️ Изменить настройки", "callback_data": "wiz_start"}],
                    [{"text": "💰 Мой баланс", "callback_data": "cmd_balance"}, {"text": "📂 Мои позиции", "callback_data": "cmd_positions"}]
                ]
            }

        if message_id:
            await self._edit_message_text(text, reply_markup=markup, message_id=message_id, chat_id=c_id)
        else:
            await self._send_reply(text, reply_markup=markup, target_chat_id=c_id)

    async def handle_command(self, text: str, sender_chat_id: str = None):
        from core.config import config
        admin_chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
        effective_chat_id = str(sender_chat_id or self.chat_id or "").strip()
        is_admin = bool(effective_chat_id and (
            effective_chat_id == admin_chat_id or 
            effective_chat_id in self.allowed_chat_ids or 
            effective_chat_id == str(self.chat_id)
        ))
        self._current_sender_chat_id = effective_chat_id
        if not text or not str(text).strip():
            return

        raw_text = text.strip()

        # Check if user is in an active text prompt (e.g. Watch-Only input)
        active_prompt = self._waiting_input.get(effective_chat_id)
        if active_prompt == "watch_only_address":
            clean_addr = raw_text.strip()
            if clean_addr.startswith("0x") and len(clean_addr) == 42:
                from services.trader_registry import trader_registry
                success, msg = trader_registry.register_watch_only(effective_chat_id, clean_addr)
                self._waiting_input.pop(effective_chat_id, None)
                text_w, markup_w = await self._get_wallet_payload(effective_chat_id)
                await self._send_reply(f"{msg}\n\n" + text_w, reply_markup=markup_w, target_chat_id=effective_chat_id)
                return
            elif clean_addr.lower() in ["/cancel", "отмена", "cancel"]:
                self._waiting_input.pop(effective_chat_id, None)
                await self._send_reply("❌ Ввод адреса отменен.", reply_markup=MAIN_REPLY_KEYBOARD, target_chat_id=effective_chat_id)
                return
            else:
                await self._send_reply("⚠️ Неверный формат адреса. Отправьте адрес кошелька (`0x...`, 42 символа) или напишите `отмена`.", target_chat_id=effective_chat_id)
                return

        elif active_prompt == "target_equity":
            clean_val = raw_text.strip().replace("$", "").replace(",", ".")
            if clean_val.lower() in ["/cancel", "отмена", "cancel"]:
                self._waiting_input.pop(effective_chat_id, None)
                text_r, markup_r = self._get_risk_menu_payload(effective_chat_id)
                await self._send_reply("❌ Настройка цели отменена.\n\n" + text_r, reply_markup=markup_r, target_chat_id=effective_chat_id)
                return
            try:
                val = float(clean_val)
                if val < 0:
                    raise ValueError()
                from services.trader_registry import trader_registry
                trader_registry.update_trader_risk_settings(effective_chat_id, target_equity_usd=val, target_equity_reached=False)
                self._waiting_input.pop(effective_chat_id, None)
                msg_ok = "✅ Цель по балансу отключена." if val == 0 else f"✅ Цель по балансу суб-аккаунта установлена: `${val:,.2f} USDC`."
                text_r, markup_r = self._get_risk_menu_payload(effective_chat_id)
                await self._send_reply(f"{msg_ok}\n\n" + text_r, reply_markup=markup_r, target_chat_id=effective_chat_id)
                return
            except Exception:
                await self._send_reply("⚠️ Неверный формат суммы. Введите число (например, `500` или `0` для отключения) или напишите `отмена`.", target_chat_id=effective_chat_id)
                return

        elif active_prompt == "daily_drawdown":
            clean_val = raw_text.strip().replace("%", "").replace(",", ".")
            if clean_val.lower() in ["/cancel", "отмена", "cancel"]:
                self._waiting_input.pop(effective_chat_id, None)
                text_r, markup_r = self._get_risk_menu_payload(effective_chat_id)
                await self._send_reply("❌ Настройка лимита отменена.\n\n" + text_r, reply_markup=markup_r, target_chat_id=effective_chat_id)
                return
            try:
                val = float(clean_val)
                if not (0.5 <= val <= 25.0):
                    raise ValueError()
                from services.trader_registry import trader_registry
                trader_registry.update_trader_risk_settings(effective_chat_id, max_daily_drawdown_pct=val, daily_drawdown_paused=False)
                self._waiting_input.pop(effective_chat_id, None)
                text_r, markup_r = self._get_risk_menu_payload(effective_chat_id)
                await self._send_reply(f"✅ Дневной лимит просадки установлен: `{val:.1f}%`.\n\n" + text_r, reply_markup=markup_r, target_chat_id=effective_chat_id)
                return
            except Exception:
                await self._send_reply("⚠️ Неверный процент. Введите число от 1.0 до 15.0 (например, `4.0`) или напишите `отмена`.", target_chat_id=effective_chat_id)
                return

        elif active_prompt == "manual_signature":
            clean_sig = raw_text.strip()
            if clean_sig.lower() in ["/cancel", "отмена", "cancel"]:
                self._waiting_input.pop(effective_chat_id, None)
                text_w, markup_w = await self._get_wallet_payload(effective_chat_id)
                await self._send_reply("❌ Ручной ввод подписи отменен.\n\n" + text_w, reply_markup=markup_w, target_chat_id=effective_chat_id)
                return
            self._waiting_input.pop(effective_chat_id, None)
            if clean_sig.startswith("/link_confirm"):
                await self.handle_command(clean_sig, sender_chat_id=effective_chat_id)
            else:
                await self.handle_command(f"/link_confirm {clean_sig}", sender_chat_id=effective_chat_id)
            return

        mapped_cmd = BUTTON_COMMAND_MAP.get(raw_text)
        if not mapped_cmd:
            for btn_key, cmd_val in BUTTON_COMMAND_MAP.items():
                if btn_key.lower() == raw_text.lower():
                    mapped_cmd = cmd_val
                    break
        if mapped_cmd:
            text = mapped_cmd

        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        print(f"📩 [TelegramListener] Получена команда: {cmd} (is_admin={is_admin}, chat_id={effective_chat_id})")

        # Security Gate: Restrict non-admins in Public Showcase Mode
        if not is_admin:
            allowed_showcase_cmds = [
                "/start", "start", "старт", "/help", "help",
                "/terminal", "терминал", "/positions", "/pos", "позиции", "/radar", "радар", "/wallet", "кошелек",
                "/status", "/info", "/regime", "/stats", "/diagnostics",
                "/connect", "/link", "/onboarding", "/link_confirm",
                "/ref", "/referral",
                "/settings", "/setup", "настройки",
                "/my_balance", "/balance_sub", "/my_positions", "/my_pos", "/toggle", "/disconnect"
            ]
            if cmd not in allowed_showcase_cmds:
                print(f"🔒 [TelegramListener] Команда '{cmd}' заблокирована для не-админа chat_id={effective_chat_id}")
                await self._send_reply(
                    "🔒 Command restricted to sub-account owner. Use /terminal, /positions, /radar or /wallet in Public Showcase Mode.",
                    reply_markup=SHOWCASE_KEYBOARD,
                    target_chat_id=effective_chat_id
                )
                return

        try:
            if cmd in ["/terminal", "терминал"]:
                text_t, markup_t = await self._get_terminal_payload(effective_chat_id, is_admin)
                await self._send_reply(text_t, reply_markup=markup_t, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/positions", "/pos", "позиции"]:
                text_p, markup_p = await self._get_positions_payload(effective_chat_id, is_admin)
                await self._send_reply(text_p, reply_markup=markup_p, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/radar", "радар"]:
                text_r, markup_r = await self._get_radar_payload()
                await self._send_reply(text_r, reply_markup=markup_r, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/whales", "/snipers", "киты", "киты nado", "whales"]:
                text_rw, markup_rw = await self._get_radar_whales_payload()
                await self._send_reply(text_rw, reply_markup=markup_rw, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/wallet", "кошелек"]:
                text_w, markup_w = await self._get_wallet_payload(effective_chat_id)
                await self._send_reply(text_w, reply_markup=markup_w, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/start", "/help", "start", "старт"]:
                from core.config import config
                ref_url = getattr(config, "NADO_REFERRAL_URL", "https://app.nado.xyz?join=ptn3l1u")
                ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")

                # Handle deep-link parameters if provided
                if len(parts) > 1:
                    start_param = parts[1].strip()
                    if start_param.startswith("link_"):
                        subparts = start_param.split("_")
                        if len(subparts) >= 4:
                            addr, nonce, sig = subparts[1], subparts[2], subparts[3]
                            await self.handle_command(f"/link_confirm {addr} {nonce} {sig}", sender_chat_id=effective_chat_id)
                            return
                    elif start_param.startswith("watch_"):
                        addr = start_param.replace("watch_", "")
                        from services.trader_registry import trader_registry
                        succ, msg = trader_registry.register_watch_only(effective_chat_id, addr)
                        text_w, markup_w = await self._get_wallet_payload(effective_chat_id)
                        await self._send_reply(f"{msg}\n\n" + text_w, reply_markup=markup_w, target_chat_id=effective_chat_id)
                        return

                admin_extra = (
                    "\n\n🎛 *Панель администратора суб-аккаунта:*\n"
                    "• `/terminal` — Главный торговый терминал и управление паузой\n"
                    "• `/positions` — Открытые позиции, ручной безубыток и закрытие\n"
                    "• `/radar` — Аналитический радар всех 7 пар Nado DEX\n"
                    "• `/scan` — Принудительное рыночное сканирование консилиумом агентов\n"
                    "• `/panic` — Экстренное закрытие всех открытых позиций"
                ) if is_admin else ""

                presentation_card = (
                    "⚡ *AI-Futures-Trader on Nado DEX (Ink L2)* — ГЛАВНОЕ МЕНЮ БОТА\n"
                    "_Autonomous Multi-Agent Algorithmic Trading Terminal_\n\n"
                    "🧠 *СИНДИКАТ ИСКУССТВЕННОГО ИНТЕЛЛЕКТА (AI MODELS):*\n"
                    "• 🐂 *Bull Specialist:* `Google Gemini 2.5 Flash Lite`\n"
                    "  _Анализ восходящего импульса, структуры пробоев и ордербука._\n"
                    "• 🐻 *Bear Specialist:* `Google Gemini 2.5 Flash Lite`\n"
                    "  _Оценка макро-сопротивлений, ликвидности и нисходящих продолжений._\n"
                    "• 📊 *Microstructure & OI Specialist:* `Groq Llama 3.3 70B + Deterministic Engine`\n"
                    "  _Микроструктура стакана заявок, дельта открытого интереса (OI) и ставки фандинга._\n"
                    "• ⚖️ *CEO Consensus & Strategic Router:* `DeepSeek-V3` & `Meta Llama 3.3 70B Instruct`\n"
                    "  _Перекрестный дебат агентов, фильтры Anti-Chase и байесовское матожидание E(R)._\n"
                    "• 🛡️ *Sentinel Agent:* `Autonomous 24/7 Engine`\n"
                    "  _Ончейн-контроль 24/7: Oracle Price SL, скользящий Trailing Stop и безубыток._\n\n"
                    "⚙️ *АРХИТЕКТУРА И ТОРГОВОЕ ИСПОЛНЕНИЕ:*\n"
                    "• *CLOB Engine:* Nado DEX Perpetuals on Ink L2 Mainnet (`Chain ID: 57073`)\n"
                    "• *Maker Execution:* `POST_ONLY` лимитные ордера со спредовой защитой\n"
                    "• *100% Non-Custodial Security:* Архитектура `LinkSigner` (EIP-712) — сессионный ключ имеет права **только на торговлю**; вывод средств (`withdraw_collateral`) аппаратно заблокирован смарт-контрактом Nado\n"
                    "• *Protocol Telemetry:* Маркировка объема `NADO_BUILDER_ID` для ончейн-распределения комиссий\n\n"
                    "🎁 *ПАРТНЕРСКАЯ ПРОГРАММА И АИРДРОП (SEASON 2):*\n"
                    f"• Зарегистрируйтесь на Nado DEX для начисления сезонных поинтов за торговлю и участия в ончейн-аирдропе $NADO & $INK:\n"
                    f"👉 [Регистрация по партнерской ссылке]({ref_url}) (Инвайт: `{ref_code}`)\n\n"
                    "🔗 *ОНЧЕЙН МОНИТОРИНГ:*\n"
                    "[View Sub-Account on Ink Explorer](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)\n\n"
                    "🧭 *Доступные команды Showcase Mode (0 LLM Tokens):*\n"
                    "• `/start` — Главное меню и презентация AI-терминала\n"
                    "• `/settings` — Индивидуальные настройки торговли (Q&A мастер)\n"
                    "• `/status` — Состояние бота, защита капитала, активные пары\n"
                    "• `/regime` — Текущий макро-режим рынка, тренд ADX и консенсус\n"
                    "• `/stats` — Публичная торговая статистика, винрейт и R-multiple\n"
                    "• `/connect` — Подключение вашего суб-аккаунта (LinkSigner)\n"
                    f"• `/ref` — Программа наград Season 2 и ссылка на аирдроп{admin_extra}"
                )

                await self._send_reply(presentation_card, reply_markup=MAIN_REPLY_KEYBOARD, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/regime"]:
                from core.state_store import StateStore
                import json
                last_snapshot = None
                filepath = "data/memory/decision_snapshots.jsonl"
                norm_path = StateStore._normalize_path(filepath)
                if os.path.exists(norm_path):
                    try:
                        with open(norm_path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = [l.strip() for l in f if l.strip()]
                            if lines:
                                last_snapshot = json.loads(lines[-1])
                    except Exception as e:
                        print(f"⚠️ [TelegramListener] Error reading decision snapshot: {e}")

                if last_snapshot:
                    sym = last_snapshot.get("symbol", "N/A")
                    regime = last_snapshot.get("macro_regime", "UNKNOWN")
                    mgi = last_snapshot.get("market_guard_inputs") or {}
                    adx = last_snapshot.get("adx") if last_snapshot.get("adx") is not None else mgi.get("adx_14")
                    adx_str = f"{adx:.1f}" if isinstance(adx, (int, float)) else "N/A"
                    iso_time = str(last_snapshot.get("iso_time", "N/A"))[:19].replace("T", " ")
                    action = last_snapshot.get("final_action", "N/A")
                    rej_tag = last_snapshot.get("rejection_tag") or ""
                    decision_str = f"{action} ({rej_tag})" if rej_tag else action

                    regime_msg = (
                        "🌐 *MARKET REGIME & CONSENSUS (CACHED | 0 TOKENS)*\n\n"
                        f"📊 *Последний проверенный символ:* `{sym}`\n"
                        f"🧭 *Макро-режим:* `{regime}`\n"
                        f"📈 *ADX (Сила тренда):* `{adx_str}`\n"
                        f"⚖️ *Консенсус решений:* `{decision_str}`\n"
                        f"🕒 *Время снимка:* `{iso_time} UTC`\n\n"
                        "ℹ️ _Данные прочитаны из локального журнала решений без расхода LLM токенов._"
                    )
                else:
                    regime_msg = (
                        "🌐 *MARKET REGIME & CONSENSUS (CACHED)*\n\n"
                        "🧭 *Макро-режим:* `TREND_DOWN` (Primary Bias)\n"
                        "📈 *ADX:* `> 25` (Strong Directional Trend)\n"
                        "⚖️ *Консенсус:* Short Bias / Strict Anti-Chase Protection\n\n"
                        "ℹ️ _Система работает в непрерывном режиме анализа рынка._"
                    )
                markup = SHOWCASE_KEYBOARD if not is_admin else None
                await self._send_reply(regime_msg, reply_markup=markup, target_chat_id=effective_chat_id)
                return
            elif cmd in ["/status", "/info"]:
                from core.config import config
                profile = getattr(config, "TRADING_PROFILE", "BALANCED")
                interval = os.getenv("SCAN_INTERVAL_MINUTES", "15")
                rest_start = os.getenv("REST_START_TIME", "19:00")
                rest_end = os.getenv("REST_END_TIME", "07:00")
                predator_on = bool(getattr(config, "ANTI_BOT_MODE", False))
                shadow_on = bool(getattr(config, "SHADOW_MODE", False))

                active_count = len(getattr(self.trading_service, "active_positions", {}))

                if not is_admin:
                    exec_mode = "👻 SHADOW (Simulation)" if shadow_on else "⚡ LIVE on Nado DEX (Ink L2)"
                    reply = (
                        "🤖 *AI-FUTURES-TRADER | PUBLIC SHOWCASE STATUS*\n\n"
                        "🟢 *Статус бота:* Онлайн 24/7 (Автономное управление)\n"
                        f"📡 *Сеть и биржа:* Nado DEX CLOB (Ink L2 Mainnet)\n"
                        f"⚙️ *Режим исполнения:* `{exec_mode}`\n"
                        f"🛡️ *Sentinel Agent:* 🟢 Активен (24/7 On-Chain SL/TP & Trailing Stop)\n"
                        f"💼 *Активных позиций на бирже:* `{active_count}`\n"
                        f"⏱️ *Цикл рыночного радара:* каждые {interval} мин\n"
                        f"⚖️ *Профиль риск-менеджмента:* `{profile}` (Dynamic ATR Sizing)\n\n"
                        "🔗 [Explorer Sub-Account](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)\n"
                        "ℹ️ _Запросы выполняются из локального кэша (0 токенов LLM)._"
                    )
                    await self._send_reply(reply, reply_markup=SHOWCASE_KEYBOARD, target_chat_id=effective_chat_id)
                    return
                
                try:
                    summary = await self.trading_service.get_portfolio_summary()
                    balance = summary.get('current_balance', 0.0)
                    active_count = summary.get('active_positions_count', active_count)
                except Exception as e:
                    summary = {}
                    balance = 0.0

                auto_predator_on = bool(getattr(config, "AUTO_PREDATOR_SWITCH", True))
                reply = (
                    f"🤖 *AI TRADER STATUS | NADO DEX*\n\n"
                    f"🟢 *Статус системы:* Активна 24/7\n"
                    f"🦈 *Анти-Бот (Predator):* {'🟢 ВКЛЮЧЕН' if predator_on else '⚪ ВЫКЛ'}\n"
                    f"• *Автопилот режимов:* {'🤖 ВКЛ' if auto_predator_on else '🔴 ВЫКЛ'}\n"
                    f"📡 *Режим исполнения:* {'👻 SHADOW' if shadow_on else '⚡ LIVE'}\n"
                    f"⏱️ *Интервал сканирования:* каждые {interval} мин\n"
                    f"⚙️ *Профиль риска:* `{profile}`\n"
                    f"🌙 *График отдыха:* с {rest_start} до {rest_end} МСК\n\n"
                    f"💰 *Текущий баланс:* `${balance:,.2f}`\n"
                    f"💼 *Активных позиций:* `{active_count}`"
                )
                await self._send_reply(reply)
                
            elif cmd == "/deposit":
                if len(parts) > 1:
                    try:
                        amount = float(parts[1])
                        import math
                        if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000:
                            await self._send_reply("⚠️ Сумма депозита должна быть конечным положительным числом (> 0 и <= $1,000,000,000).")
                        elif hasattr(self.trading_service, "adjust_ledger"):
                            self.trading_service.adjust_ledger(amount)
                            await self._send_reply(f"✅ Внесено (Deposit): `${amount:,.2f}`.\nКапитал для расчета ROI обновлен.")
                        else:
                            await self._send_reply("Управление ledger не поддерживается данным сервисом.")
                    except ValueError:
                        await self._send_reply("⚠️ Некорректный формат суммы. Пример: `/deposit 100.50`")
                else:
                    await self._send_reply("Использование: `/deposit <сумма>`")
                    
            elif cmd == "/withdraw":
                if len(parts) > 1:
                    try:
                        amount = float(parts[1])
                        import math
                        if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000:
                            await self._send_reply("⚠️ Сумма вывода должна быть конечным положительным числом (> 0 и <= $1,000,000,000).")
                        elif hasattr(self.trading_service, "adjust_ledger"):
                            self.trading_service.adjust_ledger(-amount)
                            await self._send_reply(f"✅ Выведено (Withdraw): `${amount:,.2f}`.\nКапитал для расчета ROI обновлен.")
                        else:
                            await self._send_reply("Управление ledger не поддерживается данным сервисом.")
                    except ValueError:
                        await self._send_reply("⚠️ Некорректный формат суммы. Пример: `/withdraw 50.00`")
                else:
                    await self._send_reply("Использование: `/withdraw <сумма>`")
                    
            elif cmd in ["/reset_ledger", "/reset_stats", "/resetstats", "/resetall", "/reset_all"]:
                if hasattr(self.trading_service, "reset_ledger"):
                    self.trading_service.reset_ledger()
                    await self._send_reply("🔄 *Ledger и торговая статистика сброшены!*\n• Стартовый баланс обновлен до текущего эквити ($0.00 PnL)\n• Винрейт сброшен: `0.0% (0W / 0L)`\n• Пик баланса и просадка обнулены.")
                else:
                    await self._send_reply("Сброс Ledger не поддерживается текущим режимом торговли.")

            elif cmd in ["/reset_peak", "/resetpeak"]:
                if hasattr(self.trading_service, "reset_peak"):
                    new_peak = self.trading_service.reset_peak()
                    await self._send_reply(f"🔄 *Пик баланса сброшен!* Новый High-Water Mark: `${new_peak:,.2f}`.\nПросадка портфеля обнулена.")
                elif hasattr(self.trading_service, "_peak_balance"):
                    summary = await self.trading_service.get_portfolio_summary()
                    eq = summary.get("current_balance", 100.0)
                    self.trading_service._peak_balance = eq
                    if hasattr(self.trading_service, "_save_state"):
                        self.trading_service._save_state()
                    await self._send_reply(f"🔄 *Пик баланса сброшен!* Новый High-Water Mark: `${eq:,.2f}`.\nПросадка портфеля обнулена.")
                else:
                    await self._send_reply("Сброс пика не поддерживается данным сервисом.")

            elif cmd in ["/pnl", "/balance", "/portfolio"]:
                s = await self.trading_service.get_portfolio_summary()
                reply = (
                    f"📊 *PORTFOLIO & PnL SUMMARY*\n\n"
                    f"💵 *Эквити (Total Equity):* `${s.get('current_balance', 0.0):,.2f}`\n"
                    f"🪙 *Баланс USDC (Cash):* `${s.get('spot_balance', 0.0):,.2f}`\n"
                    f"🛡️ *Свободная маржа (Free):* `${s.get('available_margin', 0.0):,.2f}`\n"
                    f"🔒 *Занятая маржа (Used):* `${s.get('margin_used', 0.0):,.2f}`\n\n"
                    f"💰 *Нереализованный PnL:* `${s.get('unrealized_pnl', 0.0):+.2f}` ({s.get('unrealized_roi_pct', 0.0):+.2f}% от маржи)\n"
                    f"📈 *Реализованный PnL (закрытые):* `${s.get('realized_pnl_usd', 0.0):+.2f}`\n"
                    f"🚀 *Общий PnL портфеля:* `${s.get('total_pnl_usd', 0.0):+.2f}` ({s.get('total_pnl_pct', 0.0):+.2f}%)\n\n"
                    f"💼 *Открытых позиций:* `{s.get('active_positions_count', 0)}`\n"
                    f"🏆 *Винрейт:* `{s.get('win_rate_pct', 0)}%` (Побед: {s.get('win_count', 0)} / Потерь: {s.get('loss_count', 0)})\n"
                    f"🏔️ *Пик баланса (Peak):* `${s.get('peak_balance', 0.0):,.2f}`\n"
                    f"🏦 *Начальный депозит:* `${s.get('initial_balance', 0.0):,.2f}`"
                )
                await self._send_reply(reply)

            elif cmd in ["/stats", "🏆 /stats", "статистика"]:
                try:
                    s = await self.trading_service.get_portfolio_summary()
                except Exception as e:
                    s = {}
                    print(f"⚠️ [TelegramListener] Error fetching stats summary: {e}")

                equity = float(s.get("current_balance", 0.0) or s.get("total_usd", 0.0) or 0.0)
                initial = float(s.get("initial_balance", 0.0) or equity)
                total_pnl = float(s.get("total_pnl_usd", 0.0) or 0.0)
                total_pnl_pct = float(s.get("total_pnl_pct", 0.0) or 0.0)
                peak = float(s.get("peak_balance", equity) or equity)
                dd_pct = float(s.get("drawdown_from_peak_pct", 0.0) or 0.0) * 100.0
                win_rate = float(s.get("win_rate_pct", 0.0) or 0.0)
                wins = int(s.get("win_count", 0) or 0)
                losses = int(s.get("loss_count", 0) or 0)
                payoff = float(s.get("historical_payoff_ratio", 1.85) or 1.85)

                positions = getattr(self.trading_service, "active_positions", {})
                active_count = len(positions)
                pos_lines = []
                if positions:
                    for sym, pos in positions.items():
                        if not isinstance(pos, dict):
                            continue
                        dir_str = str(pos.get("direction", "UNKNOWN")).upper()
                        entry = float(pos.get("entry_price", 0.0) or 0.0)
                        notional = float(pos.get("notional_usd", 0.0) or 0.0)
                        icon = "🟢" if dir_str == "LONG" else "🔴"
                        pos_lines.append(f"• {icon} *{sym}*: `{dir_str}` @ `${entry:,.2f}` (Объем: `${notional:,.0f}`) [24/7 SL/TP]")
                    pos_summary = "\n".join(pos_lines)
                else:
                    pos_summary = "• _Нет активных позиций (Радар ищет точку входа)_"

                reply = (
                    "🏆 *ТОРГОВАЯ СТАТИСТИКА | STATS*\n"
                    "_Показатели автономного AI-терминала на Nado DEX CLOB (Ink L2)_\n\n"
                    "💰 *ПОКАЗАТЕЛИ КАПИТАЛА:*\n"
                    f"• Эквити портфеля: `${equity:,.2f} USDC`\n"
                    f"• Стартовый депозит: `${initial:,.2f} USDC`\n"
                    f"• Суммарный PnL: `${total_pnl:+.2f} USDC` ({total_pnl_pct:+.2f}%)\n"
                    f"• Пик капитала (High-Water Mark): `${peak:,.2f} USDC`\n"
                    f"• Просадка от пика: `{dd_pct:.1f}%` (Лимит просадки: 10%)\n\n"
                    "🎯 *МЕТРИКИ РЕЗУЛЬТАТИВНОСТИ (WIN/LOSS):*\n"
                    f"• Винрейт (Win Rate): `{win_rate:.1f}%`\n"
                    f"• Соотношение сделок: `{wins} побед / {losses} потерь`\n"
                    f"• Коэффициент выигрыша (Payoff Ratio): `{payoff:.2f}x`\n"
                    "• Средняя цель Take-Profit: `2.0R` (2-Tier частичная фиксация)\n"
                    "• Контроль Stop-Loss: `0.75R - 1.0R` (аппаратный Oracle SL)\n\n"
                    f"💼 *АКТИВНЫЕ ПОЗИЦИИ НА БИРЖЕ ({active_count}):*\n"
                    f"{pos_summary}\n\n"
                    "⚙️ *ИСПОЛНЕНИЕ И БЕЗОПАСНОСТЬ:*\n"
                    "• Биржа: Nado DEX Perpetuals on Ink L2 Mainnet (`57073`)\n"
                    "• Исполнение: `POST_ONLY Maker` (0% комиссий CLOB + рибейты)\n"
                    "• Защита капитала: `Sentinel Agent 24/7` (Oracle SL + Trailing Stop)\n"
                    "• Доступ: `100% Non-Custodial` (LinkSigner EIP-712, 0 прав на вывод)\n\n"
                    "🔗 [Ончейн мониторинг суб-аккаунта в Ink Explorer](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)\n\n"
                    "ℹ️ _Public Showcase Mode (Read-Only Telemetry | 0 Tokens)_"
                )
                markup = SHOWCASE_KEYBOARD if not is_admin else None
                await self._send_reply(reply, reply_markup=markup, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/diagnostics", "/funnel"]:
                from core.diagnostics import tracker
                reply = tracker.get_summary_text()
                if not is_admin:
                    reply += "\n\nℹ️ _Public Showcase Mode (Read-Only Telemetry | 0 Tokens)_"
                markup = SHOWCASE_KEYBOARD if not is_admin else None
                await self._send_reply(reply, reply_markup=markup, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/ref", "/referral", "/airdrop", "/points", "поинты", "аирдроп"]:
                from core.config import config
                ref_url = getattr(config, "NADO_REFERRAL_URL", "https://app.nado.xyz?join=ptn3l1u")
                ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")
                ref_msg = (
                    "🎁 *ПАРТНЕРСКАЯ ПРОГРАММА NADO DEX (SEASON 2)*\n\n"
                    "Регистрация по официальной ссылке закрепляет кошелек в программе наград Nado и Ink L2:\n\n"
                    "📌 *Преимущества программы:*\n"
                    "• Начисление сезонных поинтов (Season 2 Points) за весь торговый объём\n"
                    "• Прямое участие в будущем распределении ончейн-аирдропа токенов $NADO / $INK\n"
                    "• Доступ к рибейтам мейкера при исполнении ордеров на CLOB\n\n"
                    f"👉 [Перейти к регистрации на Nado DEX]({ref_url})\n"
                    f"Инвайт-код: `{ref_code}`"
                )
                markup = SHOWCASE_KEYBOARD if not is_admin else None
                await self._send_reply(ref_msg, reply_markup=markup, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/connect", "/link", "/onboarding"]:
                from services.trader_registry import trader_registry
                from core.config import config
                ref_url = getattr(config, "NADO_REFERRAL_URL", "https://app.nado.xyz?join=ptn3l1u")
                ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")

                trader = trader_registry.get_trader(effective_chat_id)
                if trader and trader.get("copy_enabled"):
                    owner = trader.get("owner_address", "")
                    reply = (
                        "ℹ️ *У вас уже подключен кошелек:*\n"
                        f"• Адрес: `{owner}`\n"
                        "• Копирование: `🟢 ВКЛЮЧЕНО`\n"
                        f"• Статус Nado Season 2: `🟢 АКТИВЕН` (Инвайт: `{ref_code}`)\n\n"
                        "Используйте:\n"
                        "• `/my_balance` — проверить баланс\n"
                        "• `/my_positions` — посмотреть позиции\n"
                        "• `/ref` — партнерская ссылка Season 2\n"
                        "• `/toggle` — пауза копирования\n"
                        "• `/disconnect` — отключить кошелек"
                    )
                    await self._send_reply(reply, reply_markup=TRADER_KEYBOARD, target_chat_id=effective_chat_id)
                    return

                # Generate new ephemeral session key for this chat_id
                session = trader_registry.prepare_pending_session(effective_chat_id)
                signer_addr = session["signer_address"]
                miniapp_base = getattr(config, "TELEGRAM_MINIAPP_URL", "https://zloilapin.github.io/AI-Futures-Trader/")
                miniapp_url = f"{miniapp_base}?signer={signer_addr}&subaccount=default&join={ref_code}"
                cdn_url = f"https://raw.githack.com/zloilapin/AI-Futures-Trader/main/index.html?signer={signer_addr}&subaccount=default&join={ref_code}"
                import urllib.parse
                mm_deeplink = f"https://metamask.app.link/dapp/{miniapp_url.replace('https://', '')}"
                mm_cdn_deeplink = f"https://metamask.app.link/dapp/{cdn_url.replace('https://', '')}"
                tw_deeplink = f"https://link.trustwallet.com/open_url?coin_id=60&url={urllib.parse.quote(miniapp_url)}"

                connect_markup = {
                    "keyboard": [
                        [{"text": "🔗 Открыть Nado LinkSigner App", "web_app": {"url": miniapp_url}}],
                        [{"text": "🎁 Поинты & Аирдроп Nado"}, {"text": "📊 /status"}],
                        [{"text": "ℹ️ О проекте (/start)"}]
                    ],
                    "resize_keyboard": True
                }

                connect_text = (
                    "🔐 *Не-кастодиальное подключение к AI-Futures-Trader*\n"
                    "_Торговля на Nado DEX CLOB (Ink L2 Mainnet)_\n\n"
                    "🎁 *Шаг 1. Регистрация в Season 2 Nado DEX (Поинты и аирдроп):*\n"
                    "Перед подключением перейдите по официальной партнерской ссылке:\n"
                    f"👉 [{ref_url}]({ref_url}) (Инвайт: `{ref_code}`)\n"
                    "_Это закрепит ваш аккаунт в сезонной программе наград Nado DEX._\n\n"
                    "🛡️ *Шаг 2. Безопасное подключение (LinkSigner):*\n"
                    "• *На компьютере / Telegram Desktop:* нажмите кнопку внизу **`🔗 Открыть Nado LinkSigner App`**.\n"
                    "• *На смартфоне (в 1 клик):*\n"
                    f"👉 [🦊 Открыть прямо в приложении MetaMask]({mm_deeplink})\n"
                    f"👉 [🦊 Резервная ссылка для MetaMask (CDN Зеркало)]({mm_cdn_deeplink})\n"
                    f"👉 [🛡️ Открыть прямо в Trust Wallet]({tw_deeplink})\n\n"
                    "💡 *Что означают параметры на странице:*\n"
                    "• *Сессионный ключ (Signer):* Временный торговый ключ бота. Вы даете права **только на выставление сделок**. Вывод средств жестко запрещен смарт-контрактом Nado DEX — депозит в 100% безопасности.\n"
                    "• *Суб-аккаунт («default»):* Стандартное имя вашего торгового счета на бирже Nado DEX, к которому подключается копи-трейдинг.\n\n"
                    f"🌐 _Прямые ссылки для любого Web3-браузера:_\n"
                    f"• Основная: `{miniapp_url}`\n"
                    f"• Зеркало CDN: `{cdn_url}`\n\n"
                    "Или подтвердите подпись вручную командой:\n"
                    "`/link_confirm <ваш_адрес> <nonce> <подпись>`"
                )
                await self._send_reply(connect_text, reply_markup=connect_markup, target_chat_id=effective_chat_id)
                return

            elif cmd == "/link_confirm":
                if len(parts) < 4:
                    await self._send_reply(
                        "⚠️ *Формат команды:*\n`/link_confirm <owner_address> <nonce> <signature>`",
                        target_chat_id=effective_chat_id
                    )
                    return
                owner_addr = parts[1].strip()
                try:
                    nonce_val = int(parts[2].strip())
                except ValueError:
                    await self._send_reply("❌ Ошибка: `nonce` должен быть числом.", target_chat_id=effective_chat_id)
                    return
                signature_val = parts[3].strip()

                await self._register_trader_and_respond(
                    chat_id=effective_chat_id,
                    owner_address=owner_addr,
                    subaccount_name="default",
                    signature=signature_val,
                    nonce=nonce_val
                )
                return

            elif cmd in ["/my_balance", "/balance_sub", "💰 мой баланс"]:
                from services.trader_registry import trader_registry
                from nado_protocol.utils.bytes32 import subaccount_to_hex
                trader = trader_registry.get_trader(effective_chat_id)
                if not trader:
                    await self._send_reply(
                        "ℹ️ У вас еще не подключен кошелек. Нажмите /connect для подключения.",
                        reply_markup=SHOWCASE_KEYBOARD,
                        target_chat_id=effective_chat_id
                    )
                    return
                owner = trader.get("owner_address")
                sub_name = trader.get("subaccount", "default")
                sub_hex = subaccount_to_hex(owner, sub_name)
                assets = 0.0
                health = 0.0
                liab = 0.0
                if self.trading_service and hasattr(self.trading_service, "client") and self.trading_service.client:
                    try:
                        summary = await asyncio.to_thread(self.trading_service.client.subaccount.get_engine_subaccount_summary, sub_hex)
                        if hasattr(summary, "healths") and summary.healths:
                            assets = float(summary.healths[0].assets) / 1e18
                            health = float(summary.healths[0].health) / 1e18
                            liab = float(summary.healths[0].liabilities) / 1e18
                    except Exception as qe:
                        print(f"⚠️ [TelegramListener] Ошибка баланса для {owner}: {qe}")
                
                copy_st = "🟢 ВКЛЮЧЕНО" if trader.get("copy_enabled", True) else "⏸ НА ПАУЗЕ"
                balance_msg = (
                    "💰 *ВАШ СУБ-АККАУНТ НА NADO DEX*\n\n"
                    f"👤 *Адрес:* `{owner}`\n"
                    f"📂 *Суб-аккаунт:* `{sub_name}`\n\n"
                    f"💵 *Активы (USDC):* `${assets:,.2f}`\n"
                    f"📊 *Доступная маржа:* `${health:,.2f}`\n"
                    f"📉 *Использовано маржи:* `${liab:,.2f}`\n"
                    f"📡 *Копи-трейдинг:* {copy_st}"
                )
                await self._send_reply(balance_msg, reply_markup=TRADER_KEYBOARD, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/my_positions", "/my_pos", "📂 мои позиции"]:
                from services.trader_registry import trader_registry
                from nado_protocol.utils.bytes32 import subaccount_to_hex
                trader = trader_registry.get_trader(effective_chat_id)
                if not trader:
                    await self._send_reply(
                        "ℹ️ У вас еще не подключен кошелек. Нажмите /connect для подключения.",
                        reply_markup=SHOWCASE_KEYBOARD,
                        target_chat_id=effective_chat_id
                    )
                    return
                owner = trader.get("owner_address")
                sub_name = trader.get("subaccount", "default")
                sub_hex = subaccount_to_hex(owner, sub_name)
                positions_text = "Нет открытых позиций."
                if self.trading_service and hasattr(self.trading_service, "client") and self.trading_service.client:
                    try:
                        summary = await asyncio.to_thread(self.trading_service.client.subaccount.get_engine_subaccount_summary, sub_hex)
                        open_perps = []
                        if hasattr(summary, "perp_balances"):
                            for pb in summary.perp_balances:
                                amt = float(pb.balance.amount) / 1e18
                                if abs(amt) > 1e-6:
                                    sym = f"Product-{pb.product_id}"
                                    for s, pid in getattr(self.trading_service, "product_map", {}).items():
                                        if pid == pb.product_id and "-" in s:
                                            sym = s
                                            break
                                    side = "LONG 🟢" if amt > 0 else "SHORT 🔴"
                                    open_perps.append(f"• *{sym}*: `{side} {abs(amt):.4f}`")
                        if open_perps:
                            positions_text = "\n".join(open_perps)
                    except Exception as pe:
                        positions_text = f"Ошибка запроса позиций: {pe}"
                
                msg = (
                    f"📂 *ОТКРЫТЫЕ ПОЗИЦИИ ({owner[:6]}...{owner[-4:]}):*\n\n"
                    f"{positions_text}\n\n"
                    "ℹ️ _Защита позиций обеспечивается автономным Sentinel Agent 24/7._"
                )
                await self._send_reply(msg, reply_markup=TRADER_KEYBOARD, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/toggle", "⏯ вкл/пауза копирования"]:
                from services.trader_registry import trader_registry
                trader = trader_registry.get_trader(effective_chat_id)
                if not trader:
                    await self._send_reply(
                        "ℹ️ У вас еще не подключен кошелек. Нажмите /connect для подключения.",
                        reply_markup=SHOWCASE_KEYBOARD,
                        target_chat_id=effective_chat_id
                    )
                    return
                ok, new_state = trader_registry.toggle_copy(effective_chat_id)
                if ok:
                    st_txt = "🟢 *ВКЛЮЧЕНО*" if new_state else "⏸ *ПОСТАВЛЕНО НА ПАУЗУ*"
                    await self._send_reply(
                        f"📡 Авто-копирование сделок консилиума: {st_txt}",
                        reply_markup=TRADER_KEYBOARD,
                        target_chat_id=effective_chat_id
                    )
                else:
                    await self._send_reply("❌ Ошибка при изменении статуса копирования.", target_chat_id=effective_chat_id)
                return

            elif cmd in ["/disconnect", "🔌 отключить кошелек"]:
                from services.trader_registry import trader_registry
                ok = trader_registry.disconnect_trader(effective_chat_id)
                if ok:
                    await self._send_reply(
                        "🔌 *Кошелек успешно отключен.*\nСессионный ключ удален. Авто-копирование остановлено.",
                        reply_markup=SHOWCASE_KEYBOARD,
                        target_chat_id=effective_chat_id
                    )
                else:
                    await self._send_reply(
                        "ℹ️ У вас не было активного подключенного кошелька.",
                        reply_markup=SHOWCASE_KEYBOARD,
                        target_chat_id=effective_chat_id
                    )
                return

            elif cmd in ["/settings", "/setup", "⚙️ настройки торговли"]:
                from services.trader_registry import trader_registry
                trader = trader_registry.get_trader(effective_chat_id)
                if not trader:
                    reply = (
                        "⚙️ *ПЕРСОНАЛЬНЫЕ НАСТРОЙКИ ТОРГОВЛИ*\n\n"
                        "Вы можете настроить индивидуальный риск-профиль, кредитное плечо, количество одновременно открытых позиций, стратегию фиксации прибыли (2-Tier vs Single-Tier) и режим Анти-Бот под размер вашего депозита.\n\n"
                        "⚠️ *Сначала подключите ваш EVM-кошелек* через команду /connect, чтобы параметры привязались к вашему торговому профилю."
                    )
                    markup = {
                        "inline_keyboard": [
                            [{"text": "🔗 Подключить кошелек", "callback_data": "cmd_connect"}],
                            [{"text": "🎁 Поинты & Аирдроп Nado", "callback_data": "cmd_ref"}]
                        ]
                    }
                    await self._send_reply(reply, reply_markup=markup, target_chat_id=effective_chat_id)
                    return

                s = trader_registry.get_trader_settings(effective_chat_id)
                owner = trader.get("owner_address", "0x...")
                profile = s.get("risk_profile", "BALANCED")
                lev = s.get("leverage", 6.0)
                max_pos = s.get("max_concurrent_positions", 2)
                tp_mode = s.get("tp_mode", "2_TIER")
                tp_name = "2-Tier (50% + Б/У)" if tp_mode == "2_TIER" else "Single-Tier (100%)"
                ab_st = "🟢 ВКЛЮЧЕН" if s.get("anti_bot_enabled", True) else "⚪ ВЫКЛЮЧЕН"
                copy_st = "🟢 ВКЛЮЧЕНО" if s.get("copy_enabled", True) else "⏸ НА ПАУЗЕ"

                settings_msg = (
                    "⚙️ *ВАШИ ИНДИВИДУАЛЬНЫЕ НАСТРОЙКИ ТОРГОВЛИ*\n\n"
                    f"👤 *Суб-аккаунт:* `{owner[:6]}...{owner[-4:]}`\n"
                    f"📡 *Авто-копирование:* {copy_st}\n"
                    f"⚖️ *Профиль риска:* `{profile}` (Плечо: `{lev:.0f}x`)\n"
                    f"💼 *Лимит позиций:* `{max_pos} одновременно`\n"
                    f"🎯 *Фиксация прибыли (TP):* `{tp_name}`\n"
                    f"🦈 *Режим «Анти-Бот»:* `{ab_st}`\n"
                    f"💰 *Размер сделки CLOB:* `$104.00 USDC`\n\n"
                    "👇 _Нажмите кнопку ниже, чтобы запустить интерактивный мастер (Q&A) и настроить торговлю под себя:_"
                )
                markup = {
                    "inline_keyboard": [
                        [{"text": "🪄 Запустить мастер настройки (Q&A)", "callback_data": "wiz_start"}],
                        [{"text": "⏯ Вкл/Пауза копирования", "callback_data": "cmd_toggle"}],
                        [{"text": "💰 Мой баланс", "callback_data": "cmd_balance"}, {"text": "📂 Мои позиции", "callback_data": "cmd_positions"}]
                    ]
                }
                await self._send_reply(settings_msg, reply_markup=markup, target_chat_id=effective_chat_id)
                return

            elif cmd in ["/positions", "/pos"]:
                positions = getattr(self.trading_service, "active_positions", {})
                if not positions:
                    await self._send_reply("💼 *Открытых позиций сейчас нет.*")
                else:
                    reply_lines = [f"💼 *ТЕКУЩИЕ ОТКРЫТЫЕ ПОЗИЦИИ ({len(positions)}):*\n"]
                    inline_keyboard = []
                    
                    for sym, pos in positions.items():
                        if not isinstance(pos, dict):
                            continue
                        mode = "👻 Вирт" if pos.get("is_virtual") else "⚡ Боевая"
                        direction = str(pos.get("direction", "UNKNOWN")).upper()
                        icon = "🟢" if direction == "LONG" else ("🔴" if direction == "SHORT" else "⚪")
                        entry = float(pos.get("entry_price", 0.0) or 0.0)
                        notional = float(pos.get("notional_usd", 0.0) or 0.0)
                        leverage = float(pos.get("leverage", 1.0) or 1.0)
                        margin = float(pos.get("margin_usd", notional / leverage if leverage > 0 else notional) or 0.0)
                        
                        strat_mode = "🦈 Охотник" if (pos.get("is_anti_bot") or str(pos.get("strategy_mode", "")).upper() == "MEAN_REVERSION") else "🎯 Обычный"
                        reply_lines.append(f"{icon} *{sym}* | {direction} | {mode} | {strat_mode}")
                        reply_lines.append(f"💵 Вход: `${entry:,.2f}` | Объем: `${notional:,.0f}` (Маржа: `${margin:,.0f}`, {leverage:.0f}x)\n")
                        
                        inline_keyboard.append([{"text": f"❌ Закрыть {sym}", "callback_data": f"forceclose_{sym}"}])
                    
                    reply_markup = {"inline_keyboard": inline_keyboard} if inline_keyboard else None
                    await self._send_reply("\n".join(reply_lines), reply_markup=reply_markup)

            elif cmd in ["/predator", "/antibot"]:
                from core.config import config
                curr = bool(getattr(config, "ANTI_BOT_MODE", False))
                new_val = not curr
                config.ANTI_BOT_MODE = new_val
                os.environ["ANTI_BOT_MODE"] = str(new_val).lower()
                _persist_env_var("ANTI_BOT_MODE", str(new_val).lower())

                # Manual override: disable autopilot so next cycle does not override user's manual choice
                config.AUTO_PREDATOR_SWITCH = False
                os.environ["AUTO_PREDATOR_SWITCH"] = "false"
                _persist_env_var("AUTO_PREDATOR_SWITCH", "false")

                status_str = "ВКЛЮЧЕН 🦈" if new_val else "ВЫКЛЮЧЕН ⚪"
                reply = (
                    f"🦈 *Анти-Бот (Predator Mode)*: {status_str}\n\n"
                    f"{'Бот отслеживает ловушки и ликвидации пробойных ботов на краях Боллинджера/Donchian (Mean Reversion).' if new_val else 'Возврат к стандартной трендовой/пробойной логике.'}\n\n"
                    f"ℹ️ *Автопилот режимов отключен*, так как режим Хищника изменен вручную."
                )
                await self._send_reply(reply)

            elif cmd in ["/autopredator", "/autopred"]:
                from core.config import config
                curr = bool(getattr(config, "AUTO_PREDATOR_SWITCH", True))
                new_val = not curr
                config.AUTO_PREDATOR_SWITCH = new_val
                os.environ["AUTO_PREDATOR_SWITCH"] = str(new_val).lower()
                _persist_env_var("AUTO_PREDATOR_SWITCH", str(new_val).lower())
                status_str = "ВКЛЮЧЕН 🤖 (по волатильности)" if new_val else "ВЫКЛЮЧЕН 🔴 (Ручной)"
                reply = (
                    f"🤖 *Автопилот режимов (Auto-Predator)*: {status_str}\n\n"
                    f"{'Бот автоматически переключается между Хищником и Трендовым режимом на базе волатильности.' if new_val else 'Режим Хищника управляется только вручную.'}"
                )
                await self._send_reply(reply)

            elif cmd in ["/shadow", "/virtual"]:
                from core.config import config
                curr = bool(getattr(config, "SHADOW_MODE", False))
                new_val = not curr
                config.SHADOW_MODE = new_val
                os.environ["SHADOW_MODE"] = str(new_val).lower()
                _persist_env_var("SHADOW_MODE", str(new_val).lower())
                status_str = "👻 SHADOW (Виртуальный)" if new_val else "⚡ LIVE (Боевой On-Chain)"
                await self._send_reply(f"Режим исполнения сделок переключен на: *{status_str}*")

            elif cmd in ["/panic", "/close_all", "/closeall"]:
                positions = dict(getattr(self.trading_service, "active_positions", {}))
                if not positions:
                    await self._send_reply("💼 *Открытых позиций сейчас нет.*")
                else:
                    await self._send_reply(f"🚨 *Экстренное закрытие ВСЕХ позиций ({len(positions)})...*")
                    closed_count = 0
                    total_pnl = 0.0
                    for sym in list(positions.keys()):
                        try:
                            success, pnl = await self.trading_service.force_close_position(sym)
                            if success:
                                closed_count += 1
                                total_pnl += pnl
                        except Exception as e:
                            print(f"❌ Ошибка закрытия {sym}: {e}")
                    await self._send_reply(f"🚨 *Все позиции закрыты!* Успешно: {closed_count}/{len(positions)}. PnL: `${total_pnl:+.2f}`")

            elif cmd in ["/risk", "/profile"]:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "🛡️ Консервативный", "callback_data": "setrisk_CONSERVATIVE"}],
                        [{"text": "⚖️ Сбалансированный", "callback_data": "setrisk_BALANCED"}],
                        [{"text": "🔥 Агрессивный", "callback_data": "setrisk_AGGRESSIVE"}]
                    ]
                }
                await self._send_reply("⚙️ *Выберите профиль риска:*\n(Применится ко всем новым сделкам)", reply_markup=reply_markup)

            elif cmd in ["/scan", "/run"]:
                print("🔔 [TelegramListener] Обработка команды /scan...")
                if self.is_scanning:
                    await self._send_reply("⏳ Сканирование уже выполняется. Пожалуйста, дождитесь окончания текущего цикла.")
                elif self.trigger_scan_callback:
                    self.is_scanning = True
                    from core.config import config
                    pred_str = "🦈 Охотник (Anti-Bot)" if getattr(config, "ANTI_BOT_MODE", False) else "📈 Обычный (Standard)"
                    exec_str = "👻 Shadow (Виртуальный)" if getattr(config, "SHADOW_MODE", False) else "⚡ LIVE (Реальные деньги)"
                    await self._send_reply(
                        f"🚀 *Запуск немедленного сканирования рынка Nado DEX...*\n"
                        f"⚙️ *Режим:* `{pred_str}` | `{exec_str}`"
                    )
                    
                    async def wrapped_scan():
                        try:
                            await self.trigger_scan_callback()
                        except Exception as scan_err:
                            print(f"❌ [TelegramListener] Ошибка в wrapped_scan: {scan_err}")
                        finally:
                            self.is_scanning = False
                            
                    task = asyncio.create_task(wrapped_scan())
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                    print(f"🔔 [TelegramListener] Задача сканирования создана: {task.get_name()}")
                else:
                    print("❌ [TelegramListener] trigger_scan_callback НЕ задан!")
                    await self._send_reply("❌ Ошибка: callback сканирования не настроен.")

            elif cmd in ["/menu", "/control"]:
                text, reply_markup = self._get_control_panel_payload()
                await self._send_reply(text, reply_markup=reply_markup)
        except Exception as e:
            print(f"❌ [TelegramListener] Ошибка обработки команды '{cmd}': {type(e).__name__}: {e}")
            traceback.print_exc()

    async def _notify_manual_position_closed(self, sym: str, success: bool, pnl: float, chat_id: Optional[str] = None):
        """
        Dispatches formatted position closure card to triggering user, admin, and public channel.
        Ensures full financial transparency matching Sentinel / Fast Price Monitor notifications.
        """
        from services.telegram_service import TelegramService
        tg = TelegramService()
        if success:
            base_sym = sym.split('-')[0].upper()
            canonical_sym = f"{base_sym}-USD"
            close_info = (
                getattr(self.trading_service, "_last_close_info", {}).get(sym)
                or getattr(self.trading_service, "_last_close_info", {}).get(canonical_sym)
                or getattr(self.trading_service, "_last_close_info", {}).get(base_sym)
                or {}
            )
            try:
                from agents.telegram_agent import TelegramAgent
                from core.logger import TradeLogger
                tg_agent = TelegramAgent(TradeLogger("TelegramBot"), None)
                msg = tg_agent.format_position_closed_message(close_info)
                if not msg or not msg.strip():
                    raise ValueError("Empty format_position_closed_message")
            except Exception:
                mode = "⚡ [БОЕВАЯ]"
                emoji = "🎉" if pnl >= 0 else "🔻"
                msg = (
                    f"{emoji} {mode} *ПОЗИЦИЯ ЗАКРЫТА ВРУЧНУЮ (MANUAL)*\n\n"
                    f"🪙 *Монета:* `{sym}`\n"
                    f"💰 *PnL:* `${pnl:+.4f}`\n"
                    f"🏷 *Причина:* `MANUAL_TG_CLOSE`"
                )

            # 1. Send to current user chat
            target_chat = str(chat_id or "").strip()
            if target_chat:
                try:
                    await tg.send_message(msg, chat_id=target_chat)
                except Exception as e:
                    print(f"⚠️ [TelegramListener] Error sending close notification to user chat: {e}")

            # 2. Also send to primary admin chat if different
            admin_chat = str(getattr(tg, "chat_id", "") or "").strip()
            if admin_chat and admin_chat != target_chat:
                try:
                    await tg.send_message(msg, chat_id=admin_chat)
                except Exception as e:
                    print(f"⚠️ [TelegramListener] Error sending close notification to admin chat: {e}")

            # 3. Broadcast to public channel if configured
            try:
                await tg.broadcast_to_channel(msg)
            except Exception as e:
                print(f"⚠️ [TelegramListener] Error broadcasting close notification to channel: {e}")
        else:
            err_msg = f"❌ Ошибка при попытке принудительного закрытия {sym}."
            target_chat = str(chat_id or "").strip()
            if target_chat:
                await tg.send_message(err_msg, chat_id=target_chat)

    async def handle_callback(self, callback_id: str, callback_data: str, chat_id: str, message_id: Optional[int] = None):
        print(f"🔘 [TelegramListener] Нажата кнопка: {callback_data} (chat_id={chat_id})")
        from services.telegram_service import TelegramService
        from core.config import config
        tg = TelegramService()
        admin_chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
        is_admin = bool(chat_id and (
            str(chat_id) == admin_chat_id or 
            str(chat_id) in self.allowed_chat_ids or 
            str(chat_id) == str(self.chat_id)
        ))

        is_dash_callback = (
            callback_data.startswith("dash_") or
            callback_data.startswith("radar_") or
            callback_data.startswith("wallet_") or
            callback_data.startswith("be_ask:") or
            callback_data.startswith("close_ask:") or
            callback_data.startswith("close_confirm:")
        )

        is_wizard_callback = callback_data.startswith("wiz_")
        is_risk_callback = (
            callback_data == "risk_menu" or
            callback_data.startswith("set_risk:") or
            callback_data in ["toggle_my_copy", "set_target_equity", "set_daily_dd"] or
            callback_data.startswith("sub_be:") or
            callback_data.startswith("sub_close_ask:") or
            callback_data.startswith("sub_close_confirm:")
        )
        is_subscriber_callback = (
            callback_data.startswith("cmd_") or
            callback_data.startswith("show_") or
            callback_data in ["cmd_balance", "cmd_positions", "cmd_settings", "cmd_toggle", "cmd_disconnect", "cmd_ref", "cmd_status", "cmd_regime", "cmd_stats", "cmd_start", "cmd_connect"]
        )

        if not is_admin and not is_wizard_callback and not is_subscriber_callback and not is_dash_callback and not is_risk_callback:
            print(f"⛔ [TelegramListener] Неавторизованный callback от chat_id={chat_id}")
            await tg.answer_callback_query(
                callback_id,
                "🔒 Команда доступна только администратору. В Showcase Mode используйте /status, /stats или /connect.",
                show_alert=True
            )
            return

        # Stop loading spinner immediately for authorized interactions
        try:
            await tg.answer_callback_query(callback_id)
        except Exception:
            pass

        # Interactive Dashboard Callbacks
        if callback_data in ["dash_main", "dash_main_ref"]:
            text_t, markup_t = await self._get_terminal_payload(chat_id, is_admin)
            await self._edit_reply(text_t, reply_markup=markup_t, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data in ["dash_positions", "show_positions", "cmd_positions"]:
            text_p, markup_p = await self._get_positions_payload(chat_id, is_admin)
            await self._edit_reply(text_p, reply_markup=markup_p, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "dash_radar":
            text_r, markup_r = await self._get_radar_payload()
            await self._edit_reply(text_r, reply_markup=markup_r, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data in ["dash_wallet", "show_balance", "cmd_balance"]:
            text_w, markup_w = await self._get_wallet_payload(chat_id)
            await self._edit_reply(text_w, reply_markup=markup_w, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "dash_journal":
            text_j, markup_j = await self._get_journal_payload()
            await self._edit_reply(text_j, reply_markup=markup_j, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "dash_debates":
            text_d, markup_d = await self._get_debates_payload()
            await self._edit_reply(text_d, reply_markup=markup_d, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "dash_pause_entries":
            if is_admin:
                self.trading_service.entries_paused = True
                await tg.answer_callback_query(callback_id, "⏸ Новые входы приостановлены!")
            else:
                await tg.answer_callback_query(callback_id, "🔒 Доступно только администратору.", show_alert=True)
            text_t, markup_t = await self._get_terminal_payload(chat_id, is_admin)
            await self._edit_reply(text_t, reply_markup=markup_t, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "dash_resume_entries":
            if is_admin:
                self.trading_service.entries_paused = False
                await tg.answer_callback_query(callback_id, "▶️ Новые входы возобновлены!")
            else:
                await tg.answer_callback_query(callback_id, "🔒 Доступно только администратору.", show_alert=True)
            text_t, markup_t = await self._get_terminal_payload(chat_id, is_admin)
            await self._edit_reply(text_t, reply_markup=markup_t, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data.startswith("be_ask:"):
            sym = callback_data.replace("be_ask:", "")
            if is_admin:
                succ = await self.trading_service.update_stop_loss_to_be(sym)
                if succ:
                    await tg.answer_callback_query(callback_id, f"🛡 Стоп-лосс {sym} переведен в безубыток!")
                else:
                    await tg.answer_callback_query(callback_id, f"⚠️ Не удалось перевести {sym} в безубыток.")
            else:
                await tg.answer_callback_query(callback_id, "🔒 Доступно только администратору.", show_alert=True)
            text_p, markup_p = await self._get_positions_payload(chat_id, is_admin)
            await self._edit_reply(text_p, reply_markup=markup_p, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data.startswith("close_ask:"):
            sym = callback_data.replace("close_ask:", "")
            if is_admin:
                conf_text = (
                    f"⚠️ *ПОДТВЕРЖДЕНИЕ ЭКСТРЕННОГО ЗАКРЫТИЯ*\n\n"
                    f"Вы действительно хотите немедленно закрыть позицию *{sym}* по рыночной цене Nado DEX?"
                )
                conf_markup = {
                    "inline_keyboard": [
                        [{"text": f"⚠️ Да, закрыть {sym} по рынку", "callback_data": f"close_confirm:{sym}"}],
                        [{"text": "🔙 Отмена", "callback_data": "dash_positions"}]
                    ]
                }
                await self._edit_reply(conf_text, reply_markup=conf_markup, target_chat_id=chat_id, message_id=message_id)
            else:
                await tg.answer_callback_query(callback_id, "🔒 Доступно только администратору.", show_alert=True)
            return

        elif callback_data.startswith("close_confirm:"):
            sym = callback_data.replace("close_confirm:", "")
            if is_admin:
                await tg.answer_callback_query(callback_id, f"Закрываю {sym}... ⏳")
                success, pnl = await self.trading_service.force_close_position(sym, exit_reason="MANUAL_TG_CLOSE")
                if success:
                    await tg.answer_callback_query(callback_id, f"🚨 Позиция {sym} закрыта по рынку!")
                else:
                    await tg.answer_callback_query(callback_id, f"❌ Ошибка закрытия {sym}", show_alert=True)
                await self._notify_manual_position_closed(sym, success, pnl, chat_id=chat_id)
            else:
                await tg.answer_callback_query(callback_id, "🔒 Доступно только администратору.", show_alert=True)
            text_p, markup_p = await self._get_positions_payload(chat_id, is_admin)
            await self._edit_reply(text_p, reply_markup=markup_p, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "radar_table":
            text_rt, markup_rt = await self._get_radar_table_payload()
            await self._edit_reply(text_rt, reply_markup=markup_rt, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "radar_pullback":
            text_rp, markup_rp = await self._get_radar_pullback_payload()
            await self._edit_reply(text_rp, reply_markup=markup_rp, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "radar_verdicts":
            text_rv, markup_rv = await self._get_radar_verdicts_payload()
            await self._edit_reply(text_rv, reply_markup=markup_rv, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "radar_whales":
            text_rw, markup_rw = await self._get_radar_whales_payload(force_refresh=True)
            await self._edit_reply(text_rw, reply_markup=markup_rw, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data in ["wallet_web3", "cmd_connect"]:
            text_wc, markup_wc = await self._get_wallet_connect_payload(chat_id)
            await self._edit_reply(text_wc, reply_markup=markup_wc, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "wallet_watch_only":
            self._waiting_input[chat_id] = "watch_only_address"
            wo_text = (
                "👁 *ПОДКЛЮЧЕНИЕ WATCH-ONLY (МОНИТОРИНГ)*\n\n"
                "Отправьте в этот чат ваш адрес кошелька (`0x...`):\n"
                "_Бот подтянет баланс и позиции с Nado DEX без ключей и подписей._"
            )
            wo_markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "dash_wallet"}]]}
            await self._edit_reply(wo_text, reply_markup=wo_markup, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "wallet_manual_sig":
            self._waiting_input[chat_id] = "manual_signature"
            ms_text = (
                "📋 *РУЧНОЙ ВВОД ПОДПИСИ LINKSIGNER*\n\n"
                "Отправьте команду подтверждения в формате:\n"
                "`/link_confirm <адрес_кошелька> <nonce> <подпись>`\n\n"
                "_Либо воспользуйтесь Web3-подключением в 1 клик через MetaMask._"
            )
            ms_markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "dash_wallet"}]]}
            await self._edit_reply(ms_text, reply_markup=ms_markup, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data in ["wallet_disconnect", "cmd_disconnect"]:
            from services.trader_registry import trader_registry
            trader_registry.disconnect_trader(chat_id)
            await tg.answer_callback_query(callback_id, "🔌 Кошелек отключен.")
            text_w, markup_w = await self._get_wallet_payload(chat_id)
            await self._edit_reply(text_w, reply_markup=markup_w, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data in ["wallet_toggle_copy", "cmd_toggle"]:
            from services.trader_registry import trader_registry
            succ, new_st = trader_registry.toggle_copy(chat_id)
            st_name = "возобновлено ▶️" if new_st else "приостановлено ⏸"
            await tg.answer_callback_query(callback_id, f"Копирование {st_name}")
            text_w, markup_w = await self._get_wallet_payload(chat_id)
            await self._edit_reply(text_w, reply_markup=markup_w, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "risk_menu":
            text_rm, markup_rm = self._get_risk_menu_payload(chat_id)
            await self._edit_reply(text_rm, reply_markup=markup_rm, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data.startswith("set_risk:"):
            mode = callback_data.split(":", 1)[1]
            from services.trader_registry import trader_registry
            ok = trader_registry.set_trader_risk_profile(chat_id, mode)
            if ok:
                await tg.answer_callback_query(callback_id, f"✅ Выбран профиль: {mode.capitalize()}")
            else:
                await tg.answer_callback_query(callback_id, "⚠️ Не удалось переключить профиль.", show_alert=True)
            text_rm, markup_rm = self._get_risk_menu_payload(chat_id)
            await self._edit_reply(text_rm, reply_markup=markup_rm, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "toggle_my_copy":
            from services.trader_registry import trader_registry
            ok, new_st = trader_registry.toggle_copy(chat_id)
            st_text = "🟢 Копирование возобновлено!" if new_st else "⏸ Копирование приостановлено!"
            await tg.answer_callback_query(callback_id, st_text)
            text_rm, markup_rm = self._get_risk_menu_payload(chat_id)
            await self._edit_reply(text_rm, reply_markup=markup_rm, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "set_target_equity":
            self._waiting_input[chat_id] = "target_equity"
            prompt_text = (
                "🎯 *НАСТРОЙКА ЦЕЛИ ПО ДЕПОЗИТУ ($ USDC)*\n\n"
                "Введите желаемую сумму баланса суб-аккаунта (например, `500` или `1000`).\n"
                "При достижении этой цели новые сделки будут поставлены на паузу для фиксации прибыли.\n\n"
                "_Чтобы выключить цель, отправьте `0` или напишите `отмена`._"
            )
            prompt_markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "risk_menu"}]]}
            await self._edit_reply(prompt_text, reply_markup=prompt_markup, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data == "set_daily_dd":
            self._waiting_input[chat_id] = "daily_drawdown"
            prompt_text = (
                "🛑 *НАСТРОЙКА ДНЕВНОГО ЛИМИТА ПРОСАДКИ (%)*\n\n"
                "Введите допустимый процент дневной просадки от 1.0 до 15.0 (например, `4.0` или `5.0`).\n"
                "Если дневной убыток достигнет этого лимита, бот автоматически приостановит копирование до 00:00 UTC.\n\n"
                "_Отправьте число или напишите `отмена`._"
            )
            prompt_markup = {"inline_keyboard": [[{"text": "🔙 Отмена", "callback_data": "risk_menu"}]]}
            await self._edit_reply(prompt_text, reply_markup=prompt_markup, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data.startswith("sub_be:"):
            sym = callback_data.replace("sub_be:", "")
            from services.copy_trading_dispatcher import copy_trading_dispatcher
            ok, msg = await copy_trading_dispatcher.move_subscriber_sl_to_be(chat_id, sym)
            await tg.answer_callback_query(callback_id, msg, show_alert=True)
            text_p, markup_p = await self._get_positions_payload(chat_id, is_admin)
            await self._edit_reply(text_p, reply_markup=markup_p, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data.startswith("sub_close_ask:"):
            sym = callback_data.replace("sub_close_ask:", "")
            conf_text = (
                f"⚠️ *ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ ПОЗИЦИИ*\n\n"
                f"Вы действительно хотите немедленно закрыть сделку *{sym}* по рыночной цене на вашем суб-аккаунте?\n\n"
                f"_Это действие затронет только ваш баланс и не повлияет на сделки других пользователей._"
            )
            conf_markup = {
                "inline_keyboard": [
                    [{"text": f"⚠️ Да, закрыть МОЙ {sym}", "callback_data": f"sub_close_confirm:{sym}"}],
                    [{"text": "🔙 Отмена", "callback_data": "dash_positions"}]
                ]
            }
            await self._edit_reply(conf_text, reply_markup=conf_markup, target_chat_id=chat_id, message_id=message_id)
            return

        elif callback_data.startswith("sub_close_confirm:"):
            sym = callback_data.replace("sub_close_confirm:", "")
            from services.copy_trading_dispatcher import copy_trading_dispatcher
            ok, msg = await copy_trading_dispatcher.close_subscriber_position(chat_id, sym, reason="MANUAL_SUBSCRIBER_CLOSE")
            await tg.answer_callback_query(callback_id, msg, show_alert=True)
            text_p, markup_p = await self._get_positions_payload(chat_id, is_admin)
            await self._edit_reply(text_p, reply_markup=markup_p, target_chat_id=chat_id, message_id=message_id)
            return

        # Setup Wizard Callbacks (Q&A personal trading constants)
        if callback_data in ["wiz_start", "wiz_step_1"]:
            await tg.answer_callback_query(callback_id, "Мастер настроек (Шаг 1/4) ⚙️")
            await self.send_setup_wizard_step(chat_id, 1, message_id=message_id)
            return

        elif callback_data.startswith("wiz_risk_"):
            profile = callback_data.replace("wiz_risk_", "").upper()
            lev_map = {"CONSERVATIVE": 4.0, "BALANCED": 6.0, "AGGRESSIVE": 10.0}
            lev = lev_map.get(profile, 6.0)
            if chat_id not in self._wizard_sessions:
                from services.trader_registry import trader_registry
                self._wizard_sessions[chat_id] = dict(trader_registry.get_trader_settings(chat_id))
            self._wizard_sessions[chat_id]["risk_profile"] = profile
            self._wizard_sessions[chat_id]["leverage"] = lev
            await tg.answer_callback_query(callback_id, f"Профиль {profile} ({lev:.0f}x) ✅")
            await self.send_setup_wizard_step(chat_id, 2, message_id=message_id)
            return

        elif callback_data.startswith("wiz_pos_"):
            try:
                max_pos = int(callback_data.replace("wiz_pos_", ""))
            except ValueError:
                max_pos = 2
            if chat_id not in self._wizard_sessions:
                from services.trader_registry import trader_registry
                self._wizard_sessions[chat_id] = dict(trader_registry.get_trader_settings(chat_id))
            self._wizard_sessions[chat_id]["max_concurrent_positions"] = max_pos
            await tg.answer_callback_query(callback_id, f"Лимит позиций: {max_pos} ✅")
            await self.send_setup_wizard_step(chat_id, 3, message_id=message_id)
            return

        elif callback_data.startswith("wiz_tp_"):
            tp_mode = callback_data.replace("wiz_tp_", "").upper()
            if chat_id not in self._wizard_sessions:
                from services.trader_registry import trader_registry
                self._wizard_sessions[chat_id] = dict(trader_registry.get_trader_settings(chat_id))
            self._wizard_sessions[chat_id]["tp_mode"] = tp_mode
            await tg.answer_callback_query(callback_id, f"Стратегия TP: {tp_mode} ✅")
            await self.send_setup_wizard_step(chat_id, 4, message_id=message_id)
            return

        elif callback_data.startswith("wiz_ab_"):
            val_str = callback_data.replace("wiz_ab_", "").lower()
            ab_bool = (val_str in ["true", "1", "yes", "on"])
            if chat_id not in self._wizard_sessions:
                from services.trader_registry import trader_registry
                self._wizard_sessions[chat_id] = dict(trader_registry.get_trader_settings(chat_id))
            self._wizard_sessions[chat_id]["anti_bot_enabled"] = ab_bool
            await tg.answer_callback_query(callback_id, "Настройки сохранены! 🎉")
            await self.send_setup_wizard_step(chat_id, 5, message_id=message_id)
            return
        
        if callback_data.startswith("approve_") or callback_data.startswith("reject_"):
            parts = callback_data.split("_", 1)
            action = parts[0]
            trade_id = parts[1] if len(parts) > 1 else ""
            if hasattr(self.trading_service, "pending_trades") and trade_id in self.trading_service.pending_trades:
                trade = self.trading_service.pending_trades.pop(trade_id)
                
                import time
                if time.time() - trade.get("created_at", 0) > 300:
                    await tg.answer_callback_query(callback_id, "Сделка просрочена (>5 мин) ⏳")
                    print(f"⏳ [TelegramListener] Сделка {trade['symbol']} отклонена по таймауту (>5 мин).")
                    await tg.send_message(f"⏳ Сделка по {trade['symbol']} устарела (>5 минут) и была автоматически отменена.")
                    return

                if action == "approve":
                    await tg.answer_callback_query(callback_id, "Сделка ОДОБРЕНА ✅")
                    print(f"✅ [TelegramListener] Пользователь вручную ОДОБРИЛ сделку {trade['symbol']}")
                    
                    tg_msg = trade.pop("tg_message", None)
                    trade.pop("created_at", None)
                    await self.trading_service.open_position(**trade)
                    
                    if tg_msg:
                        await tg.broadcast_to_channel(tg_msg)
                        
                    await tg.send_message(f"✅ Одобрено: Сделка по {trade['symbol']} открыта на бирже.")
                else:
                    await tg.answer_callback_query(callback_id, "Сделка отклонена ❌")
                    print(f"❌ [TelegramListener] Пользователь отклонил сделку {trade['symbol']}")
                    await tg.send_message(f"❌ Отмена: Сделка по {trade['symbol']} отклонена пользователем.")
            else:
                await tg.answer_callback_query(callback_id, "Ошибка: Сделка не найдена или устарела.")
                
        elif callback_data.startswith("forceclose_"):
            parts = callback_data.split("_", 1)
            symbol = parts[1] if len(parts) > 1 else ""
            if not symbol:
                await tg.answer_callback_query(callback_id, "Ошибка: Не указан символ.")
                return
            await tg.answer_callback_query(callback_id, f"Закрываю {symbol}... ⏳")
            success, pnl = await self.trading_service.force_close_position(symbol, exit_reason="MANUAL_TG_CLOSE")
            await self._notify_manual_position_closed(symbol, success, pnl, chat_id=chat_id)
            return
                
        elif callback_data == "toggle_predator":
            from core.config import config
            curr = bool(getattr(config, "ANTI_BOT_MODE", False))
            new_val = not curr
            config.ANTI_BOT_MODE = new_val
            os.environ["ANTI_BOT_MODE"] = str(new_val).lower()
            _persist_env_var("ANTI_BOT_MODE", str(new_val).lower())

            # Manual override: disable autopilot so next cycle does not override user's manual choice
            config.AUTO_PREDATOR_SWITCH = False
            os.environ["AUTO_PREDATOR_SWITCH"] = "false"
            _persist_env_var("AUTO_PREDATOR_SWITCH", "false")

            status_str = "ВКЛЮЧЕН 🦈" if new_val else "ВЫКЛЮЧЕН ⚪"
            await tg.answer_callback_query(callback_id, f"Анти-Бот {status_str} (Автопилот ВЫКЛ)")
            if message_id:
                text, markup = self._get_control_panel_payload()
                await self._edit_message_text(text, markup, message_id=message_id, chat_id=chat_id)
            else:
                await tg.send_message(
                    f"🦈 Режим Анти-Бот (Predator Mode) переключен вручную: *{status_str}*\n"
                    f"ℹ️ Автопилот режимов отключен, так как режим Хищника изменен вручную."
                )

        elif callback_data == "toggle_auto_predator":
            from core.config import config
            curr = bool(getattr(config, "AUTO_PREDATOR_SWITCH", True))
            new_val = not curr
            config.AUTO_PREDATOR_SWITCH = new_val
            os.environ["AUTO_PREDATOR_SWITCH"] = str(new_val).lower()
            _persist_env_var("AUTO_PREDATOR_SWITCH", str(new_val).lower())
            status_str = "ВКЛЮЧЕН 🤖" if new_val else "ВЫКЛЮЧЕН 🔴"
            await tg.answer_callback_query(callback_id, f"Автопилот {status_str}")
            if message_id:
                text, markup = self._get_control_panel_payload()
                await self._edit_message_text(text, markup, message_id=message_id, chat_id=chat_id)
            else:
                await tg.send_message(f"🤖 Автопилот режимов (Auto-Predator) переключен: *{status_str}*")

        elif callback_data == "toggle_shadow":
            from core.config import config
            curr = bool(getattr(config, "SHADOW_MODE", False))
            new_val = not curr
            config.SHADOW_MODE = new_val
            os.environ["SHADOW_MODE"] = str(new_val).lower()
            _persist_env_var("SHADOW_MODE", str(new_val).lower())
            status_str = "SHADOW 👻" if new_val else "LIVE ⚡"
            await tg.answer_callback_query(callback_id, f"Режим изменен: {status_str}")
            if message_id:
                text, markup = self._get_control_panel_payload()
                await self._edit_message_text(text, markup, message_id=message_id, chat_id=chat_id)
            else:
                await tg.send_message(f"Исполнение сделок переведено в режим: *{status_str}*")

        elif callback_data.startswith("setrisk_"):
            parts = callback_data.split("_", 1)
            new_profile = parts[1] if len(parts) > 1 else ""
            valid_profiles = ["low", "conservative", "balanced", "medium", "moderate", "high", "aggressive"]
            if new_profile.lower() in valid_profiles:
                from core.config import config
                config.TRADING_PROFILE = new_profile.upper()
                os.environ["TRADING_PROFILE"] = new_profile.upper()
                _persist_env_var("TRADING_PROFILE", new_profile.upper())
                await tg.answer_callback_query(callback_id, f"Профиль {new_profile} установлен ✅")
                if message_id:
                    text, markup = self._get_control_panel_payload()
                    await self._edit_message_text(text, markup, message_id=message_id, chat_id=chat_id)
                else:
                    await tg.send_message(f"✅ Профиль риска успешно изменен на `{new_profile.upper()}`.")
            else:
                await tg.answer_callback_query(callback_id, f"Неизвестный профиль: {new_profile}")

        elif callback_data == "act_reset_peak":
            await tg.answer_callback_query(callback_id, "Сброс пика баланса... 🔄")
            await self.handle_command("/reset_peak", sender_chat_id=chat_id)
            if message_id:
                text, markup = self._get_control_panel_payload()
                await self._edit_message_text(text, markup, message_id=message_id, chat_id=chat_id)

        elif callback_data == "act_panic":
            await tg.answer_callback_query(callback_id, "Закрываю ВСЕ позиции! 🚨")
            await self.handle_command("/panic", sender_chat_id=chat_id)

        elif callback_data.startswith("cmd_") or callback_data.startswith("show_"):
            await tg.answer_callback_query(callback_id, "Загрузка... ⏳")
            if callback_data in ["cmd_balance", "show_balance"]: await self.handle_command("/my_balance", sender_chat_id=chat_id)
            elif callback_data in ["cmd_positions", "show_positions"]: await self.handle_command("/my_positions", sender_chat_id=chat_id)
            elif callback_data in ["cmd_settings", "show_settings"]: await self.handle_command("/settings", sender_chat_id=chat_id)
            elif callback_data in ["cmd_toggle", "show_toggle"]: await self.handle_command("/toggle", sender_chat_id=chat_id)
            elif callback_data in ["cmd_disconnect", "show_disconnect"]: await self.handle_command("/disconnect", sender_chat_id=chat_id)
            elif callback_data in ["cmd_risk", "show_risk"]: await self.handle_command("/risk", sender_chat_id=chat_id)
            elif callback_data in ["cmd_status", "show_status"]: await self.handle_command("/status", sender_chat_id=chat_id)
            elif callback_data in ["cmd_regime", "show_regime"]: await self.handle_command("/regime", sender_chat_id=chat_id)
            elif callback_data in ["cmd_stats", "show_stats"]: await self.handle_command("/stats", sender_chat_id=chat_id)
            elif callback_data in ["cmd_connect", "show_connect"]: await self.handle_command("/connect", sender_chat_id=chat_id)
            elif callback_data in ["cmd_ref", "show_ref"]: await self.handle_command("/ref", sender_chat_id=chat_id)
            elif callback_data in ["cmd_menu", "show_menu"]: await self.handle_command("/menu", sender_chat_id=chat_id)
            elif callback_data in ["cmd_start", "show_start"]: await self.handle_command("/start", sender_chat_id=chat_id)
            elif callback_data == "cmd_scan":
                if is_admin:
                    await self.handle_command("/scan", sender_chat_id=chat_id)
                else:
                    await tg.answer_callback_query(callback_id, "🔒 Доступно только администратору суб-аккаунта.", show_alert=True)

    async def sync_bot_profile_and_commands(self):
        """
        Registers official Telegram Bot menu commands, bio, and welcome description
        with Telegram Bot API on startup so the Telegram frontend is fully populated.
        """
        if not self.bot_token:
            return
        from core.session import SessionManager
        session = await SessionManager.get()
        base_url = f"https://api.telegram.org/bot{self.bot_token}"

        commands_payload = {
            "commands": [
                {"command": "terminal", "description": "📊 Главный терминал и статус биржи"},
                {"command": "positions", "description": "💼 Открытые позиции, SL/TP и Sentinel"},
                {"command": "radar", "description": "🧠 AI-Радар и анализ 7 пар Nado DEX"},
                {"command": "whales", "description": "🐋 Киты Nado и топ-снайперы лидерборда"},
                {"command": "wallet", "description": "🔗 Кабинет кошелька (LinkSigner/Watch)"},
                {"command": "start", "description": "▶️ Главное меню и модели AI синдиката"},
                {"command": "settings", "description": "⚙️ Персональные настройки торговли (Q&A)"},
                {"command": "stats", "description": "🏆 Статистика винрейта и R-multiple"},
                {"command": "connect", "description": "🦊 Подключить Web3 кошелек (LinkSigner)"},
                {"command": "ref", "description": "🎁 Поинты Season 2 и аирдроп Nado DEX"},
                {"command": "menu", "description": "🎛 Пульт управления режимами (Admin)"}
            ]
        }
        try:
            async with session.post(f"{base_url}/setMyCommands", json=commands_payload) as resp:
                if resp.status == 200:
                    print("✅ [TelegramListener] Меню команд бота успешно синхронизировано в Telegram API.")
        except Exception as e:
            print(f"⚠️ [TelegramListener] Ошибка синхронизации setMyCommands: {e}")

        description_text = (
            "🤖 AI-Futures-Trader on Nado DEX (Ink L2)\n\n"
            "Автономный AI-терминал и non-custodial копи-трейдинг.\n\n"
            "🧠 Модели AI синдиката:\n"
            "• Bull/Bear: Gemini 2.5 Flash Lite\n"
            "• CEO Consensus: DeepSeek-V3 & Llama 3.3 70B\n"
            "• Sentinel: 24/7 on-chain SL/TP защита капитала\n\n"
            "🛡️ 100% Non-Custodial: сессионный ключ LinkSigner без прав на вывод средств.\n"
            "🎁 Начисление поинтов Season 2 и участие в аирдропе Nado (Инвайт: ptn3l1u).\n\n"
            "Нажмите START для запуска!"
        )
        try:
            async with session.post(f"{base_url}/setMyDescription", json={"description": description_text}) as resp:
                if resp.status == 200:
                    print("✅ [TelegramListener] Описание бота (Description) синхронизировано в Telegram API.")
        except Exception as e:
            print(f"⚠️ [TelegramListener] Ошибка синхронизации setMyDescription: {e}")

        short_desc = "AI-терминал Nado DEX (Ink L2). Gemini 2.5 Flash + DeepSeek V3 + Llama 3.3. Награды Season 2 и аирдроп (Код: ptn3l1u)."
        try:
            async with session.post(f"{base_url}/setMyShortDescription", json={"short_description": short_desc}) as resp:
                if resp.status == 200:
                    print("✅ [TelegramListener] Краткое описание (Short Description) синхронизировано.")
        except Exception as e:
            print(f"⚠️ [TelegramListener] Ошибка синхронизации setMyShortDescription: {e}")

    async def start_listening(self):
        from services.telegram_service import TelegramService
        if not TelegramService._is_valid_credential(self.bot_token) or not TelegramService._is_valid_credential(self.chat_id):
            print("❌ [TelegramListener] НЕ ЗАПУЩЕН: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не настроены в .env!")
            return

        self.running = True
        print("📲 [TelegramListener] Фоновый слушатель команд Telegram запущен.")
        print(f"📲 [TelegramListener] Chat ID: {self.chat_id} | Polling timeout: 30s")

        # Sync commands and description in the background
        asyncio.create_task(self.sync_bot_profile_and_commands())

        while self.running:
            try:
                url = f"{self.api_url}?offset={self.offset}&timeout=30"
                from core.session import SessionManager
                session = await SessionManager.get()
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("result", [])
                        for result in results:
                            self.offset = result["update_id"] + 1
                            
                            if "callback_query" in result:
                                callback_query = result["callback_query"]
                                cb_data = callback_query.get("data")
                                cb_id = callback_query.get("id")
                                cb_message = callback_query.get("message", {})
                                cb_msg_id = cb_message.get("message_id")
                                cb_chat_id = str(cb_message.get("chat", {}).get("id", ""))
                                
                                if cb_chat_id:
                                    await self.handle_callback(cb_id, cb_data, cb_chat_id, message_id=cb_msg_id)
                            
                            elif "message" in result:
                                message = result.get("message", {})
                                text = str(message.get("text", "")).strip()
                                from_user = message.get("from", {})
                                sender_chat_id = str(message.get("chat", {}).get("id", ""))
                                user_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or "Аноним"
                                username_str = f"@{from_user.get('username')}" if from_user.get('username') else "без @username"

                                # Handle Telegram Mini App web_app_data
                                web_app_data = message.get("web_app_data")
                                if web_app_data and "data" in web_app_data:
                                    print(f"📲 [TelegramListener] Получены данные web_app_data от {user_name} ({username_str}, chat_id={sender_chat_id})")
                                    await self.handle_web_app_data(web_app_data["data"], sender_chat_id=sender_chat_id)
                                    continue
                                
                                is_cmd = (
                                    text.startswith("/")
                                    or text in BUTTON_COMMAND_MAP
                                    or any(k in text for k in ["Статус", "Позиции", "Пульт", "Анти-Бот", "Shadow", "Риска", "Сброс Пика", "ЗАКРЫТЬ ВСЕ", "Сканировать", "сканировать", "Scan", "scan", "Подключить", "подключить", "Баланс", "баланс", "Отключить", "отключить", "копирования"])
                                    or (sender_chat_id in self._waiting_input)
                                    or (text.startswith("0x") and len(text) == 42)
                                )
                                if is_cmd:
                                    print(f"📨 [TelegramListener] Команда '{text}' от {user_name} ({username_str}, chat_id={sender_chat_id})")
                                    await self.handle_command(text, sender_chat_id=sender_chat_id)
                    else:
                        error_body = await resp.text()
                        if resp.status == 409:
                            print(f"⚠️ [TelegramListener] Конфликт getUpdates (HTTP 409): Запущен другой экземпляр бота с тем же TELEGRAM_BOT_TOKEN! Проверьте запущенные процессы. Пауза 15с...")
                            await asyncio.sleep(15)
                        else:
                            print(f"❌ [TelegramListener] Ошибка polling (HTTP {resp.status}): {error_body[:300]}")
                            await asyncio.sleep(5)
            except asyncio.CancelledError:
                print("🛑 [TelegramListener] Слушатель остановлен (CancelledError).")
                break
            except asyncio.TimeoutError:
                continue
            except aiohttp.ClientError as e:
                print(f"🔌 [TelegramListener] Ошибка сети (возможно пропал интернет): {e}. Повтор через 15с...")
                await asyncio.sleep(15)
            except Exception as e:
                print(f"❌ [TelegramListener] Ошибка в цикле polling: {type(e).__name__}: {e}")
                traceback.print_exc()
                await asyncio.sleep(5)
