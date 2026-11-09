import os
import sys
import aiohttp
import asyncio
import traceback
from typing import Callable, Awaitable, Dict, Any, Optional, Tuple

from services.trader_registry import trader_registry

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Start"}, {"text": "📊 Статус & PnL"}, {"text": "📂 Позиции"}],
        [{"text": "🚀 Сканировать рынок"}, {"text": "🎛 Пульт Режимов"}, {"text": "🦈 Анти-Бот"}],
        [{"text": "⚖️ Профиль Риска"}, {"text": "👻 Shadow Mode"}, {"text": "🚨 ЗАКРЫТЬ ВСЕ"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

SHOWCASE_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Start"}, {"text": "🔗 Подключить кошелек"}],
        [{"text": "📊 /status"}, {"text": "🌐 /regime"}, {"text": "🏆 /stats"}],
        [{"text": "⚙️ Настройки торговли"}, {"text": "🎁 Скидка 10% Nado"}],
        [{"text": "ℹ️ О проекте"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

TRADER_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Start"}, {"text": "💰 Мой баланс"}, {"text": "📂 Мои позиции"}],
        [{"text": "⚙️ Настройки торговли"}, {"text": "🎁 Скидка 10% Nado"}],
        [{"text": "⏯ Вкл/Пауза копирования"}, {"text": "🔌 Отключить кошелек"}],
        [{"text": "📊 /status"}, {"text": "🏆 /stats"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

BUTTON_COMMAND_MAP = {
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

    def _get_control_panel_payload(self) -> Tuple[str, dict]:
        from core.config import config
        predator_on = bool(getattr(config, "ANTI_BOT_MODE", False))
        auto_predator_on = bool(getattr(config, "AUTO_PREDATOR_SWITCH", True))
        shadow_on = bool(getattr(config, "SHADOW_MODE", False))
        profile = str(getattr(config, "TRADING_PROFILE", "BALANCED")).upper()

        predator_status = "🟢 ВКЛЮЧЕН" if predator_on else "⚪ ВЫКЛЮЧЕН"
        predator_btn_text = "🦈 Анти-Бот: Выкл" if predator_on else "🦈 Анти-Бот: Вкл"

        auto_status = "🤖 ВКЛ (по волатильности)" if auto_predator_on else "🔴 ВЫКЛ (Ручной)"
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
            "🎛 *ПУЛЬТ УПРАВЛЕНИЯ РЕЖИМАМИ ТОРГОВЛИ*\n\n"
            f"🦈 *Анти-Бот (Predator Mode):* {predator_status}\n"
            f"• *Автопилот (Auto-Predator):* {auto_status}\n"
            f"_(Охота на ликвидации и стопы ботов на краях Боллинджера/Donchian)_\n\n"
            f"📡 *Режим исполнения:* {shadow_status}\n\n"
            f"⚖️ *Профиль риска:* `{current_risk_str}`\n\n"
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

    async def _send_reply(self, text: str, reply_markup: dict = None, target_chat_id: str = None):
        dest_chat_id = target_chat_id or getattr(self, "_current_sender_chat_id", None) or self.chat_id
        if not self.send_url or not dest_chat_id:
            print(f"⚠️ [TelegramListener] _send_reply: send_url или chat_id не задан! send_url={bool(self.send_url)}, chat_id={bool(dest_chat_id)}")
            return
        safe_text = text[:3980] + "\n...[TRUNCATED]" if text and len(text) > 4000 else (text or "")
        payload = {
            "chat_id": dest_chat_id,
            "text": safe_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
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

    async def _edit_message_text(self, text: str, reply_markup: dict = None, message_id: int = None, chat_id: str = None):
        dest_chat_id = chat_id or getattr(self, "_current_sender_chat_id", None) or self.chat_id
        if not self.bot_token or not dest_chat_id or not message_id:
            return
        edit_url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        safe_text = text[:3980] + "\n...[TRUNCATED]" if text and len(text) > 4000 else (text or "")
        payload = {
            "chat_id": dest_chat_id,
            "message_id": message_id,
            "text": safe_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            from core.session import SessionManager
            session = await SessionManager.get()
            async with session.post(edit_url, json=payload) as resp:
                if resp.status != 200:
                    payload.pop("parse_mode", None)
                    async with session.post(edit_url, json=payload) as resp2:
                        pass
        except Exception as e:
            print(f"⚠️ [TelegramListener] Ошибка editMessageText: {e}")

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
            f"🎁 *Реферальный статус:* `Скидка 10% на комиссии Nado DEX` (Код: `{ref_code}`)\n\n"
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
                "/status", "/info", "/regime", "/stats", "/diagnostics",
                "/connect", "/link", "/onboarding", "/link_confirm",
                "/ref", "/referral",
                "/settings", "/setup", "настройки",
                "/my_balance", "/balance_sub", "/my_positions", "/my_pos", "/toggle", "/disconnect"
            ]
            if cmd not in allowed_showcase_cmds:
                print(f"🔒 [TelegramListener] Команда '{cmd}' заблокирована для не-админа chat_id={effective_chat_id}")
                await self._send_reply(
                    "🔒 Command restricted to sub-account owner. Use /status or /stats in Public Showcase Mode.",
                    reply_markup=SHOWCASE_KEYBOARD,
                    target_chat_id=effective_chat_id
                )
                return

        try:
            if cmd in ["/start", "/help", "start", "старт"]:
                from core.config import config
                ref_url = getattr(config, "NADO_REFERRAL_URL", "https://app.nado.xyz?join=ptn3l1u")
                ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")

                admin_extra = (
                    "\n\n🎛 *Панель администратора суб-аккаунта:*\n"
                    "• `/menu` — Интерактивный пульт переключения режимов (Predator / Shadow / Risk Profile)\n"
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
                    "🎁 *ПАРТНЕРСКАЯ ПРОГРАММА И БОНУСЫ:*\n"
                    f"• Получите **скидку 10% на все торговые комиссии** Nado DEX:\n"
                    f"👉 [Регистрация по партнерской ссылке]({ref_url}) (Код: `{ref_code}`)\n\n"
                    "🔗 *ОНЧЕЙН МОНИТОРИНГ:*\n"
                    "[View Sub-Account on Ink Explorer](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)\n\n"
                    "🧭 *Доступные команды Showcase Mode (0 LLM Tokens):*\n"
                    "• `/start` — Главное меню и презентация AI-терминала\n"
                    "• `/settings` — Индивидуальные настройки торговли (Q&A мастер)\n"
                    "• `/status` — Состояние бота, защита капитала, активные пары\n"
                    "• `/regime` — Текущий макро-режим рынка, тренд ADX и консенсус\n"
                    "• `/stats` — Публичная торговая статистика, винрейт и R-multiple\n"
                    "• `/connect` — Подключение вашего суб-аккаунта (LinkSigner)\n"
                    f"• `/ref` — Официальная партнерская ссылка со скидкой 10%{admin_extra}"
                )

                from services.trader_registry import trader_registry
                is_trader = bool(effective_chat_id and trader_registry.get_trader(effective_chat_id))
                active_kb = REPLY_KEYBOARD if is_admin else (TRADER_KEYBOARD if is_trader else SHOWCASE_KEYBOARD)

                await self._send_reply(presentation_card, reply_markup=active_kb, target_chat_id=effective_chat_id)
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
                    adx = last_snapshot.get("adx")
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
                    
            elif cmd == "/reset_ledger":
                if hasattr(self.trading_service, "reset_ledger"):
                    self.trading_service.reset_ledger()
                    await self._send_reply("🔄 Ledger сброшен! Текущий баланс стал новой стартовой точкой для подсчета ROI.")
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

            elif cmd in ["/ref", "/referral"]:
                from core.config import config
                ref_url = getattr(config, "NADO_REFERRAL_URL", "https://app.nado.xyz?join=ptn3l1u")
                ref_code = getattr(config, "NADO_REFERRAL_CODE", "ptn3l1u")
                ref_msg = (
                    "🎁 *ОФИЦИАЛЬНАЯ ПАРТНЕРСКАЯ ПРОГРАММА NADO DEX (INK L2)*\n\n"
                    "Все пользователи торгового терминала получают *скидку 10%* на торговые комиссии CLOB.\n\n"
                    "📌 *Как активировать скидку:*\n"
                    "1. Перейдите по официальной ссылке биржи:\n"
                    f"👉 [{ref_url}]({ref_url})\n\n"
                    f"2. Убедитесь, что реферальный код `{ref_code}` применен.\n"
                    "3. Подключите ваш EVM-кошелек на Nado DEX.\n\n"
                    "💡 _Скидка 10% на комиссии закрепится за вашим кошельком навсегда._"
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
                        f"• Скидка Nado 10%: `🟢 АКТИВНА` (Код: `{ref_code}`)\n\n"
                        "Используйте:\n"
                        "• `/my_balance` — проверить баланс\n"
                        "• `/my_positions` — посмотреть позиции\n"
                        "• `/ref` — партнерская ссылка со скидкой 10%\n"
                        "• `/toggle` — пауза копирования\n"
                        "• `/disconnect` — отключить кошелек"
                    )
                    await self._send_reply(reply, reply_markup=TRADER_KEYBOARD, target_chat_id=effective_chat_id)
                    return

                # Generate new ephemeral session key for this chat_id
                session = trader_registry.prepare_pending_session(effective_chat_id)
                signer_addr = session["signer_address"]
                miniapp_base = getattr(config, "TELEGRAM_MINIAPP_URL", "https://raw.githack.com/zloilapin/AI-Futures-Trader/main/web/tg_miniapp_link_signer.html")
                miniapp_url = f"{miniapp_base}?signer={signer_addr}&subaccount=default&join={ref_code}"

                connect_markup = {
                    "keyboard": [
                        [{"text": "🔗 Открыть Nado LinkSigner App", "web_app": {"url": miniapp_url}}],
                        [{"text": "🎁 Скидка 10% Nado"}, {"text": "📊 /status"}],
                        [{"text": "ℹ️ О проекте (/start)"}]
                    ],
                    "resize_keyboard": True
                }

                connect_text = (
                    "🔐 *Не-кастодиальное подключение к AI-Futures-Trader*\n"
                    "_Торговля на Nado DEX CLOB (Ink L2)_\n\n"
                    "🎁 *Шаг 1. Активация скидки 10% на комиссии (Nado Referral):*\n"
                    "Перед подключением ключа перейдите по официальной партнерской ссылке:\n"
                    f"👉 [{ref_url}]({ref_url}) (Код: `{ref_code}`)\n"
                    "_Это закрепит постоянную скидку 10% на торговые комиссии Nado DEX за вашим кошельком._\n\n"
                    "🛡️ *Шаг 2. Безопасное подключение (LinkSigner):*\n"
                    "1. Нажмите кнопку **`🔗 Открыть Nado LinkSigner App`** внизу.\n"
                    "2. Подключите ваш EVM-кошелек (MetaMask / Rabby / Trust) в сети **Ink L2 Mainnet (57073)**.\n"
                    "3. Подпишите безопасный EIP-712 запрос `LinkSigner`.\n\n"
                    "🔒 *100% Защита депозита:*\n"
                    "Сессионный ключ бота имеет права **только** на выставление и снятие ордеров. "
                    "Вывод средств (`withdraw_collateral`) жестко запрещен смарт-контрактом Nado DEX — ваши средства в безопасности.\n\n"
                    f"🌐 _Если кнопка не открывается, откройте ссылку в Web3-браузере:_\n"
                    f"`{miniapp_url}`\n\n"
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
                            [{"text": "🎁 Скидка 10% Nado DEX", "callback_data": "cmd_ref"}]
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

        is_wizard_callback = callback_data.startswith("wiz_")
        is_subscriber_callback = (
            callback_data.startswith("cmd_") or
            callback_data.startswith("show_") or
            callback_data in ["cmd_balance", "cmd_positions", "cmd_settings", "cmd_toggle", "cmd_disconnect", "cmd_ref", "cmd_status", "cmd_regime", "cmd_stats", "cmd_start", "cmd_connect"]
        )

        if not is_admin and not is_wizard_callback and not is_subscriber_callback:
            print(f"⛔ [TelegramListener] Неавторизованный callback от chat_id={chat_id}")
            await tg.answer_callback_query(
                callback_id,
                "🔒 Команда доступна только администратору. В Showcase Mode используйте /status, /stats или /connect.",
                show_alert=True
            )
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
            
            success, pnl = await self.trading_service.force_close_position(symbol)
            if success:
                close_info = getattr(self.trading_service, "_last_close_info", {}).get(symbol) or {}
                try:
                    from agents.telegram_agent import TelegramAgent
                    from core.logger import TradeLogger
                    tg_agent = TelegramAgent(TradeLogger("TelegramBot"), None)
                    msg = tg_agent.format_position_closed_message(close_info)
                except Exception:
                    mode = "⚡ [БОЕВАЯ]"
                    emoji = "🎉" if pnl >= 0 else "🔻"
                    msg = f"{emoji} {mode} Сделка по {symbol} ЗАКРЫТА ВРУЧНУЮ!\n💰 PnL: `${pnl:+.2f}`"
                await tg.send_message(msg)
                await tg.broadcast_to_channel(msg)
            else:
                await tg.send_message(f"❌ Ошибка закрытия {symbol}.")
                
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
                {"command": "start", "description": "▶️ Главное меню и модели AI синдиката"},
                {"command": "settings", "description": "⚙️ Персональные настройки торговли (Q&A)"},
                {"command": "status", "description": "📊 Состояние бота, защита капитала, PnL"},
                {"command": "regime", "description": "🌐 Текущий макро-режим и консенсус AI"},
                {"command": "stats", "description": "🏆 Статистика винрейта и R-multiple"},
                {"command": "connect", "description": "🔗 Подключить суб-аккаунт (LinkSigner)"},
                {"command": "ref", "description": "🎁 Скидка 10% на комиссии Nado DEX"},
                {"command": "my_balance", "description": "💰 Баланс и маржа на Nado DEX"},
                {"command": "my_positions", "description": "📂 Мои открытые позиции"},
                {"command": "toggle", "description": "⏯ Вкл/Пауза копи-трейдинга"},
                {"command": "disconnect", "description": "🔌 Отключить сессионный ключ"},
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
            "🎁 Скидка 10% на комиссии Nado DEX (Код: ptn3l1u).\n\n"
            "Нажмите START для запуска!"
        )
        try:
            async with session.post(f"{base_url}/setMyDescription", json={"description": description_text}) as resp:
                if resp.status == 200:
                    print("✅ [TelegramListener] Описание бота (Description) синхронизировано в Telegram API.")
        except Exception as e:
            print(f"⚠️ [TelegramListener] Ошибка синхронизации setMyDescription: {e}")

        short_desc = "AI-терминал Nado DEX (Ink L2). Gemini 2.5 Flash + DeepSeek V3 + Llama 3.3. Скидка 10% на комиссии (Код: ptn3l1u)."
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
