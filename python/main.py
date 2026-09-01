import os
import sys
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from dotenv import load_dotenv

if sys.platform == "win32":
    pass # Removed sys.stdout.reconfigure(encoding='utf-8') to prevent mojibake

load_dotenv(override=True)

from core.logger import TradeLogger
from core.llm_client import LLMClient
from core.config import config
from core.utils import get_msk_status

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

from services.market_data_service import MarketDataService
from services.telegram_service import TelegramService
from services.telegram_bot_listener import TelegramBotListener

from core.pipeline import AgentRegistry, ServiceRegistry, TradingPipeline


async def main():
    print("=== INITIALIZING NADO AI TRADING SYSTEM ===")
    
    logger = TradeLogger()
    
    # 4-Tier Architecture LLMs (ALL via OpenRouter)
    cheap_llm_client = LLMClient(provider="openrouter", model_name="meta-llama/llama-3.1-8b-instruct")
    primary_ceo_llm = LLMClient(provider="openrouter", model_name="meta-llama/llama-3.3-70b-instruct")
    escalation_ceo_llm = LLMClient(provider="openrouter", model_name="google/gemini-3.7-flash")
    tg_sender = TelegramService()
    print("Инициализация сервисов...")
    
    trading_engine = config.TRADING_ENGINE
    
    # Global shared Nado Client if applicable
    global_nado_client = None
    
    if trading_engine == "PAPER" and not config.LIVE_TRADING_ENABLED:
        from services.paper_trading_service import PaperTradingService
        trading_service = PaperTradingService(logger=logger)
        exchange_name = f"Nado DEX (Paper - {config.NADO_NETWORK})"
        fetcher = MarketDataService(exchange_name=exchange_name, logger=logger)
    else:
        from services.nado_trading_service import NadoTradingService
        import os
        try:
            from core.nado_helper import create_configured_nado_client
            global_nado_client = create_configured_nado_client(
                network_name=config.NADO_NETWORK,
                signer=os.getenv("INK_PRIVATE_KEY")
            )
        except Exception as e:
            logger.error(f"[Init] Failed to create global Nado Client: {e}")
            sys.exit(1)
            
        trading_service = NadoTradingService()
        await trading_service.initialize(nado_client=global_nado_client)
        exchange_name = f"Nado DEX ({config.NADO_NETWORK})"
        fetcher = MarketDataService(exchange_name=exchange_name, logger=logger, nado_client=global_nado_client)
    
    # P0.3: Startup sync — rebuild state from Exchange BEFORE any trading logic
    if hasattr(trading_service, "sync_with_exchange"):
        print("🔄 [P0.3 Startup Sync] Синхронизация состояния с биржей при старте...")
        while True:
            try:
                await trading_service.sync_with_exchange()
                print(f"✅ [P0.3 Startup Sync] Состояние синхронизировано. Активных позиций: {len(trading_service.active_positions)}")
                break
            except Exception as e:
                print(f"🚨 [P0.3 Startup Sync] КРИТИЧЕСКАЯ ОШИБКА: {e}")
                print("🛑 СТАРТОВАЯ СИНХРОНИЗАЦИЯ ПРОВАЛЕНА. ТОРГОВЛЯ ЗАБЛОКИРОВАНА. Повторная попытка через 60 секунд...")
                await asyncio.sleep(60)
    
    universe_agent = UniverseAgent(logger, cheap_llm_client)
    scanner_agent = ScannerAgent(logger, None)
    candle_agent = CandleAgent(logger, None)
    ob_agent = OrderBookAgent(logger, None)
    oi_agent = OIFundingAgent(logger, None)
    news_agent = NewsAgent(logger, cheap_llm_client)
    indicator_agent = IndicatorAgent(logger, None)
    ceo_agent = CEOAgent(logger, primary_ceo_llm, escalation_ceo_llm)
    risk_manager = RiskManager(logger, primary_ceo_llm)
    telegram_agent = TelegramAgent(logger, cheap_llm_client)
    reflector_agent = ReflectorAgent(logger, cheap_llm_client)
    memory_manager = MemoryManager(logger)


    agents = AgentRegistry(
        universe=universe_agent,
        scanner=scanner_agent,
        candle=candle_agent,
        orderbook=ob_agent,
        oi_funding=oi_agent,
        news=news_agent,
        indicator=indicator_agent,
        ceo=ceo_agent,
        risk=risk_manager,
        telegram=telegram_agent,
        reflector=reflector_agent,
        memory=memory_manager
    )
    
    services = ServiceRegistry(
        logger=logger,
        fetcher=fetcher,
        tg_sender=tg_sender,
        trading_service=trading_service
    )
    
    pipeline = TradingPipeline(agents, services, exchange_name)

    async def trigger_scan():
        print("⚡ [TriggerScan] Вызвана ручная команда /scan вне очереди!")
        try:
            await pipeline.run_cycle(999, force_scan=True)
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
    scan_interval_min = config.SCAN_INTERVAL_MINUTES
    interval_seconds = scan_interval_min * 60

    cycle_number = 1

    try:
        if run_once:
            print("⚡ Одиночный запуск торгового цикла (--once).")
            await pipeline.run_cycle(cycle_number)
        else:
            is_rest, time_str = get_msk_status()
            profile = config.TRADING_PROFILE
            print(f"⏰ Режим автономного сканирования активирован! [Текущее время: {time_str}]")
            print(f"🔄 Интервал сканирования: Круглосуточно каждые {scan_interval_min} мин.")
            print(f"⚙️ Профиль риска: {profile} | 📐 Multi-Timeframe (15m + 1H + 4H) ON")
            print("💡 Для остановки нажмите Ctrl+C в любой момент.\n")
            if hasattr(trading_service, "start_background_watcher"):
                asyncio.create_task(trading_service.start_background_watcher(tg_sender))
            asyncio.create_task(bot_listener.start_listening())

            try:
                while True:
                    try:
                        await pipeline.run_cycle(cycle_number)
                    except Exception as e:
                        import traceback
                        error_msg = f"КРИТИЧЕСКАЯ ОШИБКА В ЦИКЛЕ №{cycle_number}: {e}"
                        print(f"\n❌ {error_msg}")
                        traceback.print_exc()
                        logger.error(error_msg)
                    
                    is_rest_now, _ = get_msk_status()
                    current_interval_min = config.SCAN_INTERVAL_MINUTES
                    interval_seconds = current_interval_min * 60
                    
                    cycle_number += 1
                    print(f"\n⏳ Ожидание {current_interval_min} мин. до следующего цикла №{cycle_number}...")
                    await asyncio.sleep(interval_seconds)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\n🛑 Автономный торговый бот остановлен.")
    finally:
        for client_name in ['cheap_llm_client', 'primary_ceo_llm', 'escalation_ceo_llm']:
            if client_name in locals() and hasattr(locals()[client_name], "close"):
                await locals()[client_name].close()
                
        if 'fetcher' in locals() and hasattr(fetcher, "close"):
            await fetcher.close()
            
        if hasattr(trading_service, "_close_exchange_async"):
            await trading_service._close_exchange_async()
            print("🧹 [System] Ресурсы соединения с биржей успешно очищены.")
            
        # Завершение всех зависших asyncio задач (включая bot_listener)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            print(f"🧹 [System] Отмена {len(pending)} фоновых задач (Telegram Polling и др.)...")
            for task in pending:
                task.cancel()
            import contextlib
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*pending, return_exceptions=True)
            print("🧹 [System] Все фоновые задачи успешно отменены.")
            
        from core.session import SessionManager
        await SessionManager.close_all()
        print("🧹 [System] Глобальные aiohttp сессии закрыты.")

if __name__ == "__main__":
    asyncio.run(main())
