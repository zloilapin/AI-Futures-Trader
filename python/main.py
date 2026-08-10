import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from dotenv import load_dotenv
load_dotenv()

# --- CORE IMPORTS ---
from core.logger import TradeLogger
from core.llm_client import LLMClient

# --- AGENT IMPORTS ---
from agents.universe_agent import UniverseAgent
from agents.scanner_agent import ScannerAgent
from agents.candle_agent import CandleAgent
from agents.order_book_agent import OrderBookAgent
from agents.oi_funding_agent import OIFundingAgent
from agents.news_agent import NewsAgent
from agents.indicator_agent import IndicatorAgent
from agents.ceo_agent import CEOAgent
from agents.risk_manager import RiskManager
from agents.telegram_agent import TelegramAgent
from agents.memory_manager import MemoryManager
from agents.reflector_agent import ReflectorAgent

# --- SERVICE IMPORTS ---
from services.market_data_service import MarketDataService
from services.telegram_service import TelegramService
from services.paper_trading_service import PaperTradingService
from services.live_trading_service import LiveTradingService
from services.kraken_trading_service import KrakenTradingService
from services.telegram_bot_listener import TelegramBotListener


def get_msk_status() -> tuple[bool, str]:
    """
    Checks if current time in MSK (UTC+3) is within quiet rest window (19:10 - 07:00 MSK).
    Returns (is_rest_period, current_msk_formatted_time).
    """
    offset = int(os.getenv("TIMEZONE_OFFSET", "3"))
    msk_tz = timezone(timedelta(hours=offset))
    now_msk = datetime.now(msk_tz)
    
    start_parts = os.getenv("REST_START_TIME", "19:00").split(":")
    end_parts = os.getenv("REST_END_TIME", "07:00").split(":")
    
    start_mins = int(start_parts[0]) * 60 + int(start_parts[1])
    end_mins = int(end_parts[0]) * 60 + int(end_parts[1])
    cur_mins = now_msk.hour * 60 + now_msk.minute
    
    is_rest = cur_mins >= start_mins or cur_mins < end_mins
    return is_rest, now_msk.strftime("%H:%M:%S МСК")


def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in ['_', '*', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, f'\\{ch}')
    return text


async def run_single_cycle(
    cycle_number: int,
    logger: TradeLogger,
    fetcher: MarketDataService,
    tg_sender: TelegramService,
    trading_service,
    universe_agent: UniverseAgent,
    scanner_agent: ScannerAgent,
    candle_agent: CandleAgent,
    ob_agent: OrderBookAgent,
    oi_agent: OIFundingAgent,
    news_agent: NewsAgent,
    indicator_agent: IndicatorAgent,
    ceo_agent: CEOAgent,
    risk_manager: RiskManager,
    telegram_agent: TelegramAgent,
    reflector_agent: ReflectorAgent,
    memory_manager: MemoryManager,
    force_scan: bool = False
):
    is_rest, time_str = get_msk_status()
    profile = os.getenv("TRADING_PROFILE", "BALANCED")
    print(f"\n" + "="*65)
    print(f"🚀 ТОРГОВЫЙ ЦИКЛ №{cycle_number} (NADO DEX INK L2) [{time_str}]")
    print(f"⚙️ ПРОФИЛЬ РИСКА: {profile} | 📐 MULTI-TIMEFRAME (15m + 1H + 4H) АКТИВЕН")
    print("="*65)

    if is_rest and not force_scan:
        print(f"🌙 [Schedule] ВРЕМЯ ОТДЫХА БОТА! Сейчас {time_str} (Окно отдыха с 19:00 до 07:00 МСК).")
        print("💤 Анализ рынка и работа агентов приостановлены до 07:00 МСК.")
        return

    if force_scan and is_rest:
        print(f"⚡ [ForceScan] Ручной запуск /scan во время отдыха ({time_str}). Пропуск тихого режима!")

    # СТАДИЯ 1: UNIVERSE (Выбор активов)
    print("\n[Stage 1] Universe Agent сканирует DEX на наличие ликвидных активов...")
    active_perps = await fetcher.fetch_active_perps(limit=15)
    broad_market_data = {
        "trending_perps": active_perps, 
        "volume_24h": "high"
    } 
    universe_report = await universe_agent.analyze(broad_market_data)

    selected_assets = universe_report.get("selected_pairs", [])
    if not selected_assets:
        # Fallback to top 6 by 24h volume directly from the exchange if LLM fails
        selected_assets = [p["symbol"] for p in active_perps[:6]]

    print(f"🎯 Отобраны активы для сканирования: {', '.join(selected_assets)}")
    portfolio_data = await trading_service.get_portfolio_summary()

    recent_lessons = reflector_agent.get_lessons(limit=5)
    if recent_lessons:
        print(f"🧠 [Memory] Загружено {len(recent_lessons)} уроков из прошлых сделок.")

    any_signal_sent = False
    scan_summaries = []  # Сводка по каждому активу для отчёта ручного /scan

    # ИТЕРАЦИЯ ПО ВСЕМ АКТИВАМ
    for symbol in selected_assets:
        print(f"\n🔍 ПОЛНЫЙ АНАЛИЗ (15m, 1H, 4H) NADO DEX: {symbol}")
        
        # СТАДИЯ 2: СБОР ДАННЫХ И ПРОВЕРКА ОТКРЫТЫХ ПОЗИЦИЙ
        print(f"[Stage 2] Сбор мульти-таймфреймовых данных (15m, 1H, 4H) для {symbol}...")
        market_data = await fetcher.fetch_all_market_data(symbol)
        current_price = market_data.get("price_data", {}).get("current_price", 0)

        # Проверка исполнения TP/SL и активации Breakeven Guard
        closed_reports = trading_service.check_and_update_positions(symbol, current_price)
        for closed in closed_reports:
            pnl_emoji = "🎉" if closed["pnl_usd"] >= 0 else "🔻"
            closed_msg = (
                f"{pnl_emoji} *СДЕЛКА ЗАКРЫТА НА NADO DEX ({closed['triggered_by']})*\n\n"
                f"🪙 *Монета:* `{closed['symbol']}` | *Направление:* `{closed['direction']}`\n"
                f"🎯 *Цена входа:* `${closed['entry_price']:,.2f}` ➔ *Выход:* `${closed['exit_price']:,.2f}`\n"
                f"📈 *PnL:* `${closed['pnl_usd']:+.2f}` ({closed['pnl_pct']:+.2f}%)\n"
                f"💰 *Новый баланс:* `${closed['new_balance']:,.2f}`"
            )
            print(f"\n--- ЗАКРЫТИЕ ПОЗИЦИИ В TELEGRAM [{symbol}] ---")
            print(closed_msg)
            print("--------------------------------------------")
            await tg_sender.send_message(closed_msg)
            asyncio.create_task(reflector_agent.reflect(closed, market_data))

        # 2.5 Scanner Agent (ATR Volatility & Spread Guard)
        print(f"[Stage 2.5] Scanner Agent проверяет ATR волатильность и спред для {symbol}...")
        scan_result = await scanner_agent.analyze(market_data)

        scanner_blocked = not scan_result.get("proceed", True)
        scanner_reason = scan_result.get('reasoning', 'Неизвестная причина')
        if scanner_blocked:
            print(f"🛑 Scanner Agent пропустил {symbol}. Причина: {scanner_reason}")
            logger.info(f"[System_Core] Пропуск {symbol}. Причина: {scanner_reason}")
            if not force_scan:
                # В автоматическом режиме — пропускаем актив
                scan_summaries.append({"symbol": symbol, "status": "⛔ СКАН ЗАБЛОКИРОВАН", "reason": scanner_reason})
                continue
            else:
                # В ручном /scan — продолжаем анализ, несмотря на блокировку сканера
                print(f"⚡ [ForceScan] Продолжаем полный анализ {symbol} несмотря на блокировку сканера.")

        # СТАДИЯ 3: СИНДИКАТ АНАЛИТИКОВ
        print(f"[Stage 3] Запуск синдиката аналитиков для {symbol}...")
        analyst_list = [candle_agent, ob_agent, oi_agent, news_agent, indicator_agent]
        reports = []
        for agent in analyst_list:
            rep = await agent.analyze(market_data)
            reports.append(rep)
            await asyncio.sleep(0.6)
            
        valid_reports = [r for r in reports if r and r.get("signal") != "ERROR"]

        # СТАДИЯ 4: СИНТЕЗ CEO И МУЛЬТИ-ТАЙМФРЕЙМ ТРЕНД
        print(f"[Stage 4] CEO Agent проверяет согласованность 1H/4H тренда и выносит решение по {symbol}...")
        historical_context = memory_manager.get_recent_context(limit=3)
        ceo_payload = {
            "symbol": symbol,
            "multi_timeframe": market_data.get("multi_timeframe", {}),
            "analyst_reports": valid_reports,
            "historical_context": historical_context,
            "past_lessons_learned": recent_lessons
        }
        ceo_verdict = await ceo_agent.analyze(ceo_payload)
        print(f"⚖️ Решение CEO [{symbol}]: {ceo_verdict.get('decision')} (Уверенность: {ceo_verdict.get('conviction')}%)")

        # СТАДИЯ 5: РИСК-МЕНЕДЖМЕНТ
        print(f"[Stage 5] Risk Manager ({profile}) проверяет параметры сделки для {symbol}...")
        risk_verdict = await risk_manager.analyze(ceo_verdict, portfolio_data, market_data)

        if risk_verdict.get("approved"):
            print(f"✅ Status: APPROVED BY RISK MANAGER")
            print(f"💰 Position Amount: ${risk_verdict.get('position_size_usd', 0):,.2f} ({risk_verdict.get('position_size_pct', 0)}% of portfolio)")
            print(f"🎯 Entry Price: ${risk_verdict.get('entry_price', 0):,.2f}")
            print(f"🟢 Take Profit (TP): ${risk_verdict.get('take_profit_price', 0):,.2f} (+{risk_verdict.get('take_profit_pct', 0)}%)")
            print(f"🔴 Stop Loss (SL): ${risk_verdict.get('stop_loss_price', 0):,.2f} (-{risk_verdict.get('stop_loss_pct', 0)}%)")
            print(f"⚖️ Risk / Reward Ratio: {risk_verdict.get('risk_reward_ratio', 0)}")

            # Открываем виртуальную позицию
            await trading_service.open_position(
                symbol=symbol,
                direction=str(ceo_verdict.get("decision")).upper(),
                entry_price=risk_verdict.get("entry_price", current_price),
                size_usd=risk_verdict.get("position_size_usd", 0),
                tp_price=risk_verdict.get("take_profit_price", 0),
                sl_price=risk_verdict.get("stop_loss_price", 0)
            )
        else:
            print(f"❌ Status: VETOED BY RISK MANAGER ({risk_verdict.get('reasoning')})")

        # СТАДИЯ 6: ТЕЛЕГРАМ (ФИЛЬТР: LONG/SHORT И УВЕРЕННОСТЬ >= 80%)
        decision = str(ceo_verdict.get("decision", "HOLD")).upper()
        conviction = ceo_verdict.get("conviction", 0)

        # Собираем сводку по активу для отчёта ручного /scan
        scanner_status = "⚠️ ЗАБЛОКИРОВАН СКАНЕРОМ" if scanner_blocked else "✅ OK"
        asset_summary = {
            "symbol": symbol,
            "decision": decision,
            "conviction": conviction,
            "scanner_status": scanner_status,
            "scanner_reason": scanner_reason if scanner_blocked else None,
            "risk_approved": risk_verdict.get("approved", False),
            "risk_reason": risk_verdict.get("reasoning", ""),
            "ceo_reasoning": ceo_verdict.get("reasoning", ""),
            "status": "🚀 СИГНАЛ" if (decision in ["LONG", "SHORT"] and conviction >= 80) else "⏸️ НЕТ СИГНАЛА"
        }
        scan_summaries.append(asset_summary)

        if (decision in ["LONG", "SHORT"] and conviction >= 80) or force_scan:
            if decision in ["LONG", "SHORT"] and conviction >= 80:
                print(f"🚀 НАЙДЕН СИГНАЛ! Генерация Telegram-уведомления [{symbol}] ({decision}, Уверенность {conviction}%)...")
            else:
                print(f"ℹ️ [ForceScan] Ручной режим: отправка Telegram-уведомления [{symbol}] ({decision})...")
            
            final_trade_data = {
                "symbol": symbol,
                "ceo_verdict": ceo_verdict,
                "risk_verdict": risk_verdict
            }
            tg_response = await telegram_agent.analyze(final_trade_data)
            tg_message = tg_response.get("message", f"Отчет по {symbol}")
            
            print(f"\n--- ОТПРАВЛЕНО В TELEGRAM [{symbol}] ---")
            print(tg_message)
            print("-----------------------------")
            await tg_sender.send_message(tg_message)
            any_signal_sent = True
        else:
            print(f"ℹ️ [Telegram] Пропуск отправки для {symbol}. Решение '{decision}' с уверенностью {conviction}% (Фильтр: LONG/SHORT и Уверенность ≥ 80%).")

        # СТАДИЯ 7: СОХРАНЕНИЕ В ПАМЯТЬ
        cycle_record = {
            "symbol": symbol,
            "market_conditions": scan_result,
            "analysts": valid_reports,
            "ceo_decision": ceo_verdict,
            "risk_assessment": risk_verdict,
            "status": "APPROVED" if risk_verdict.get("approved") else "VETOED"
        }
        memory_manager.save_cycle(cycle_record)
        logger.info(f"[System_Core] Торговый цикл успешно завершен для {symbol}.")
        
        await asyncio.sleep(1.5)

    # Если сканирование было запрошено вручную через /scan — всегда присылаем подробный отчёт (сводку)
    if force_scan:
        assets_list = ", ".join(selected_assets)
        parts = [f"📋 РЕЗУЛЬТАТЫ РУЧНОГО СКАНИРОВАНИЯ\n\n🔍 Проанализированы: {assets_list}\n"]

        for item in scan_summaries:
            sym = item.get("symbol", "?")
            status = item.get("status", "?")
            dec = item.get("decision", "N/A")
            conv = item.get("conviction", 0)
            scanner_st = item.get("scanner_status", "")
            scanner_rsn = item.get("scanner_reason")
            risk_ok = item.get("risk_approved", False)
            risk_rsn = item.get("risk_reason", "")
            ceo_rsn = item.get("ceo_reasoning", "")

            block = f"{'='*28}\n🪙 {sym} — {status}\n"
            block += f"📊 Решение CEO: {dec} | Уверенность: {conv}%\n"
            block += f"🔎 Сканер: {scanner_st}\n"
            if scanner_rsn:
                safe_rsn = _escape_md(str(scanner_rsn)[:200])
                block += f"   > {safe_rsn}\n"
            risk_label = "✅ ОДОБРЕН" if risk_ok else "❌ ОТКЛОНЁН"
            block += f"🛡 Риск-менеджер: {risk_label}\n"
            if not risk_ok and risk_rsn:
                safe_risk = _escape_md(str(risk_rsn)[:200])
                block += f"   > {safe_risk}\n"
            if ceo_rsn:
                safe_ceo = _escape_md(str(ceo_rsn)[:200])
                block += f"📝 {safe_ceo}\n"
            parts.append(block)

        parts.append("\n💡 Сигналы >=80% не обнаружены. Бот продолжает мониторинг.")
        scan_reply = "\n".join(parts)
        # Отправляем без Markdown чтобы избежать ошибок парсинга спецсимволов
        await tg_sender.send_message(scan_reply, parse_mode="")

    print(f"\n=== ТОРГОВЫЙ ЦИКЛ №{cycle_number} УСПЕШНО ЗАВЕРШЕН ===")


async def main():
    print("=== INITIALIZING NADO AI TRADING SYSTEM ===")
    
    logger = TradeLogger()
    llm_client = LLMClient()
    fetcher = MarketDataService()
    tg_sender = TelegramService()
    trading_engine = os.getenv("TRADING_ENGINE", "PAPER").upper()
    if trading_engine == "KRAKEN":
        trading_service = KrakenTradingService()
        print("🔴 ВНИМАНИЕ: АКТИВИРОВАН БОЕВОЙ РЕЖИМ (KRAKEN FUTURES)!")
    elif trading_engine == "NADO" or os.getenv("LIVE_TRADING_ENABLED", "False").lower() == "true":
        trading_service = LiveTradingService()
        print("🔴 ВНИМАНИЕ: АКТИВИРОВАН БОЕВОЙ РЕЖИМ (NADO DEX)!")
    else:
        trading_service = PaperTradingService()
        print("🟢 Режим симуляции (Paper Trading) активен.")
    
    universe_agent = UniverseAgent(logger, llm_client)
    scanner_agent = ScannerAgent(logger, llm_client)
    candle_agent = CandleAgent(logger, llm_client)
    ob_agent = OrderBookAgent(logger, llm_client)
    oi_agent = OIFundingAgent(logger, llm_client)
    news_agent = NewsAgent(logger, llm_client)
    indicator_agent = IndicatorAgent(logger, llm_client)
    ceo_agent = CEOAgent(logger, llm_client)
    risk_manager = RiskManager(logger, llm_client)
    telegram_agent = TelegramAgent(logger, llm_client)
    reflector_agent = ReflectorAgent(logger, llm_client)
    memory_manager = MemoryManager(logger)

    async def trigger_scan():
        print("⚡ [TriggerScan] Вызвана ручная команда /scan вне очереди!")
        try:
            await run_single_cycle(
                999, logger, fetcher, tg_sender, trading_service,
                universe_agent, scanner_agent, candle_agent, ob_agent,
                oi_agent, news_agent, indicator_agent, ceo_agent,
                risk_manager, telegram_agent, reflector_agent, memory_manager,
                force_scan=True
            )
            print("✅ [TriggerScan] Ручное сканирование завершено успешно.")
        except Exception as e:
            import traceback
            print(f"❌ [TriggerScan] КРИТИЧЕСКАЯ ОШИБКА: {type(e).__name__}: {e}")
            traceback.print_exc()
            # Отправляем ошибку в Telegram (без спецсимволов)
            safe_error = str(e).replace('_', ' ').replace('*', '').replace('`', '')[:500]
            error_msg = f"Ошибка при выполнении /scan:\n{type(e).__name__}: {safe_error}"
            try:
                await tg_sender.send_message(error_msg, parse_mode="")
            except Exception as e2:
                print(f"❌ [TriggerScan] Не удалось отправить ошибку в Telegram: {e2}")

    bot_listener = TelegramBotListener(trading_service, trigger_scan_callback=trigger_scan)

    run_once = "--once" in sys.argv
    scan_interval_min = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
    interval_seconds = scan_interval_min * 60

    cycle_number = 1

    if run_once:
        print("⚡ Одиночный запуск торгового цикла (--once).")
        await run_single_cycle(
            cycle_number, logger, fetcher, tg_sender, trading_service,
            universe_agent, scanner_agent, candle_agent, ob_agent,
            oi_agent, news_agent, indicator_agent, ceo_agent,
            risk_manager, telegram_agent, reflector_agent, memory_manager
        )
    else:
        is_rest, time_str = get_msk_status()
        profile = os.getenv("TRADING_PROFILE", "BALANCED")
        print(f"⏰ Режим автономного сканирования активирован! [Текущее время: {time_str}]")
        print(f"🔄 Интервал сканирования: каждые {scan_interval_min} минут ({interval_seconds} сек).")
        print(f"⚙️ Профиль риска: {profile} | 📐 Multi-Timeframe (15m + 1H + 4H) ON")
        print("🌙 График отдыха: с 19:00 до 07:00 МСК (в этот период бот спит).")
        print("💡 Для остановки нажмите Ctrl+C в любой момент.\n")
        
        asyncio.create_task(bot_listener.start_listening())

        try:
            while True:
                await run_single_cycle(
                    cycle_number, logger, fetcher, tg_sender, trading_service,
                    universe_agent, scanner_agent, candle_agent, ob_agent,
                    oi_agent, news_agent, indicator_agent, ceo_agent,
                    risk_manager, telegram_agent, reflector_agent, memory_manager
                )
                cycle_number += 1
                print(f"\n⏳ Ожидание {scan_interval_min} мин. до следующего цикла №{cycle_number}...")
                await asyncio.sleep(interval_seconds)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n🛑 Автономный торговый бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
