import os
import sys
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

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
from agents.bull_agent import BullAgent
from agents.bear_agent import BearAgent
from agents.ceo_agent import CEOAgent
from agents.sentinel_agent import SentinelAgent
from agents.regime_agent import RegimeAgent
from agents.risk_manager import RiskManager
from agents.telegram_agent import TelegramAgent
from agents.memory_manager import MemoryManager
from agents.reflector_agent import ReflectorAgent

def get_seconds_to_next_candle(interval_minutes: int = 15, offset_seconds: int = 5) -> float:
    """
    Computes exact sleep seconds to wake up precisely offset_seconds after the next candle close.
    For interval_minutes=15, aligns with :00:05, :15:05, :30:05, :45:05 UTC/Unix wall-clock.
    """
    now = time.time()
    interval_sec = interval_minutes * 60
    current_bucket = int(now // interval_sec)
    target_time = ((current_bucket + 1) * interval_sec) + offset_seconds
    remaining = target_time - now
    if remaining <= 2.0:
        target_time += interval_sec
        remaining = target_time - now
    return max(1.0, remaining)



from services.market_data_service import MarketDataService
from services.telegram_service import TelegramService
from services.telegram_bot_listener import TelegramBotListener

from core.pipeline import AgentRegistry, ServiceRegistry, TradingPipeline


async def main():
    print("=== INITIALIZING NADO AI TRADING SYSTEM ===")
    
    logger = TradeLogger()
    
    # 4-Tier Architecture LLMs (ALL via OpenRouter)
    model_cheap = config.OPENROUTER_MODEL_CHEAP
    model_ceo = config.OPENROUTER_MODEL_CEO
    model_escalation = config.OPENROUTER_MODEL_ESCALATION
    
    cheap_llm_client = LLMClient(provider="openrouter", model_name=model_cheap)
    primary_ceo_llm = LLMClient(provider="openrouter", model_name=model_ceo)
    escalation_ceo_llm = LLMClient(provider="openrouter", model_name=model_escalation)
    tg_sender = TelegramService()
    print("Инициализация сервисов...")
    
    # Global shared Nado Client if applicable
    global_nado_client = None
    
    from services.nado_trading_service import NadoTradingService
    try:
        from core.nado_helper import create_configured_nado_client
        signer_val = config.INK_PRIVATE_KEY.get_secret_value()
        global_nado_client = create_configured_nado_client(
            signer=signer_val if signer_val else None,
            network_name=config.NADO_NETWORK
        )
        trading_service = NadoTradingService(nado_client=global_nado_client)
        exchange_name = f"Nado DEX ({config.NADO_NETWORK})"
        fetcher = MarketDataService(exchange_name=exchange_name, logger=logger, nado_client=global_nado_client)
        await trading_service.initialize(nado_client=global_nado_client)
    except Exception as e:
        logger.error(f"[System] Failed to initialize Nado Trading Engine: {e}")
        print(f"❌ Критическая ошибка: Не удалось запустить Nado Trading Engine. {e}")
        sys.exit(1)
    
    # P0.3: Startup sync — rebuild state from Exchange BEFORE any trading logic
    if hasattr(trading_service, "sync_with_exchange"):
        print("🔄 [P0.3 Startup Sync] Синхронизация состояния с биржей при старте...")
        while True:
            try:
                await trading_service.sync_with_exchange(is_startup=True)
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
    bull_agent = BullAgent(logger, primary_ceo_llm)
    bear_agent = BearAgent(logger, primary_ceo_llm)
    ceo_agent = CEOAgent(logger, primary_ceo_llm, escalation_ceo_llm)
    sentinel_agent = SentinelAgent(logger, cheap_llm_client)
    regime_agent = RegimeAgent(logger, cheap_llm_client)
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
        bull=bull_agent,
        bear=bear_agent,
        ceo=ceo_agent,
        sentinel=sentinel_agent,
        regime=regime_agent,
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
            if getattr(pipeline, "_cycle_running", False):
                await tg_sender.send_message("⏳ В данный момент уже выполняется цикл сканирования рынка. Пожалуйста, дождитесь окончания текущего цикла.")
                return
            await pipeline.run_cycle(999, force_scan=True)
            print("✅ [TriggerScan] Ручное сканирование завершено успешно.")
            await tg_sender.send_message("✅ *Ручное сканирование рынка завершено!*")
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
            async def sentinel_loop():
                while True:
                    try:
                        sentinel_interval = getattr(config, "SENTINEL_INTERVAL_SECONDS", 30)
                        await pipeline.run_sentinel_checks(is_background=True)
                    except Exception as e:
                        logger.error(f"[SentinelLoop] Ошибка: {e}")
                    await asyncio.sleep(sentinel_interval)

            wake_up_event = asyncio.Event()

            async def fast_radar_loop():
                """
                Continuous Fast Market Radar (0 LLM tokens).
                Polls top assets and watchlist every 45-60s.
                If a registered pullback reaches entry zone, or a major breakout occurs,
                it wakes up the pipeline immediately instead of waiting for the 30m sleep.
                """
                radar_interval = getattr(config, "FAST_RADAR_INTERVAL_SECONDS", 45)
                while True:
                    try:
                        await asyncio.sleep(radar_interval)
                        # Do not wake pipeline if trading cooldown is active
                        if time.time() < getattr(trading_service, "cooldown_until", 0.0):
                            continue
                        if not getattr(pipeline, "_cycle_running", False):
                            async def _check_radar_signals():
                                pb_trig, pb_s, pb_reas = await pipeline.check_pullback_watchlist()
                                if pb_trig:
                                    return ("pullback", pb_s, pb_reas)
                                trig, s, reas = await pipeline.check_fast_market_momentum()
                                if trig:
                                    return ("momentum", s, reas)
                                return (None, None, None)

                            try:
                                # Safe async timeout guard: prevent hanging RPC / network calls from freezing the radar loop
                                r_type, r_sym, r_reason = await asyncio.wait_for(_check_radar_signals(), timeout=15.0)
                                if r_type == "pullback":
                                    print(f"\n⚡ [Pullback Radar] {r_sym}: {r_reason}! Прерываем сон для входа на откате...")
                                    logger.info(f"[PullbackRadar] Triggered on {r_sym}: {r_reason}. Waking pipeline.")
                                    if not hasattr(pipeline, "_radar_target_symbols") or pipeline._radar_target_symbols is None:
                                        pipeline._radar_target_symbols = set()
                                    pipeline._radar_target_symbols.add(r_sym)
                                    pipeline._radar_target_symbol = r_sym
                                    wake_up_event.set()
                                elif r_type == "momentum":
                                    if "PREDATOR" in str(r_reason):
                                        print(f"\n🦈 [Predator Radar] Обнаружен спайк ликвидности на {r_sym} ({r_reason})! Прерываем сон для перехвата...")
                                        logger.info(f"[FastRadar] Predator spike triggered on {r_sym}: {r_reason}. Waking pipeline.")
                                    else:
                                        print(f"\n🚨 [Fast Radar] Обнаружен сильный импульс на {r_sym} ({r_reason})! Прерываем сон...")
                                        logger.info(f"[FastRadar] Momentum triggered on {r_sym}: {r_reason}. Waking pipeline.")
                                    if not hasattr(pipeline, "_radar_target_symbols") or pipeline._radar_target_symbols is None:
                                        pipeline._radar_target_symbols = set()
                                    pipeline._radar_target_symbols.add(r_sym)
                                    pipeline._radar_target_symbol = r_sym
                                    wake_up_event.set()
                            except asyncio.TimeoutError:
                                logger.warning("[FastRadarLoop] ⚠️ Radar check timed out after 15.0s (RPC/DEX network latency). Resuming next tick.")
                    except Exception as e:
                        logger.debug(f"[FastRadarLoop] Ошибка: {e}")
            
            asyncio.create_task(sentinel_loop())
            asyncio.create_task(fast_radar_loop())
            asyncio.create_task(bot_listener.start_listening())

            # Send greeting and activate keyboard in Telegram on startup
            try:
                ctrl_text, ctrl_markup = bot_listener._get_control_panel_payload()
                asyncio.create_task(bot_listener._send_reply(ctrl_text, reply_markup=ctrl_markup))
            except Exception as e:
                print(f"⚠️ Не удалось отправить приветственное меню: {e}")

            if "--drain-and-stop" in sys.argv or "--close-only" in sys.argv:
                print("\n🛡️ [DRAIN & STOP MODE] Режим только сопровождения активен.")
                print("🛑 Открытие новых позиций ПОЛНОСТЬЮ ЗАБЛОКИРОВАНО.")
                print("👀 Бот сопровождает существующие позиции (Fast Monitor 5s + TP/SL) и завершит работу сразу после их закрытия.\n")
                
                try:
                    await asyncio.sleep(5)  # Grace period for initial sync
                    while True:
                        await asyncio.sleep(3)
                        active_count = len(getattr(trading_service, "active_positions", {}))
                        if active_count == 0:
                            print("\n🏁 [DRAIN & STOP] Все открытые позиции закрыты! Автоматическая остановка фонового процесса.")
                            logger.info("[DrainAndStop] All positions closed. Exiting process.")
                            break
                except (KeyboardInterrupt, asyncio.CancelledError):
                    print("\n🛑 Остановлено пользователем.")
                return

            try:
                while True:
                    targeted_symbol = getattr(pipeline, "_radar_target_symbol", None)
                    pipeline._radar_target_symbol = None
                    try:
                        await pipeline.run_cycle(cycle_number, targeted_symbol=targeted_symbol)
                    except Exception as e:
                        import traceback
                        error_msg = f"КРИТИЧЕСКАЯ ОШИБКА В ЦИКЛЕ №{cycle_number}: {e}"
                        print(f"\n❌ {error_msg}")
                        traceback.print_exc()
                        logger.error(error_msg)
                    
                    if "--once" in sys.argv:
                        print(f"\n✅ [Run Once] Цикл №{cycle_number} успешно завершен. Выход.")
                        break
                    
                    max_cycles = getattr(config, "MAX_CYCLES", 0)
                    if max_cycles > 0 and cycle_number >= max_cycles:
                        print(f"\n🏁 Достигнут лимит {max_cycles} циклов. Автоматическое завершение работы.")
                        break
                    
                    is_rest_now, _ = get_msk_status()
                    scan_interval_min = getattr(config, "SCAN_INTERVAL_MINUTES", 15)
                    wait_sec = get_seconds_to_next_candle(interval_minutes=scan_interval_min, offset_seconds=5)
                    next_dt = datetime.fromtimestamp(time.time() + wait_sec)
                    next_str = next_dt.strftime("%H:%M:%S")
                    
                    cycle_number += 1
                    print(f"\n⏳ Ожидание {wait_sec:.0f}с (до {next_str}) до закрытия {scan_interval_min}m свечи...")
                    try:
                        await asyncio.wait_for(wake_up_event.wait(), timeout=wait_sec)
                        wake_up_event.clear()
                        wake_syms = list(getattr(pipeline, "_radar_target_symbols", set()))
                        if not wake_syms and getattr(pipeline, "_radar_target_symbol", None):
                            wake_syms = [pipeline._radar_target_symbol]
                        target_info = f" по активам: {', '.join(wake_syms)}" if wake_syms else ""
                        print(f"\n⚡ [Fast Radar Trigger] Внеочередной запуск сканирования рынка{target_info} по сигналу радара...")
                    except asyncio.TimeoutError:
                        pipeline._radar_target_symbol = None
                        if hasattr(pipeline, "_radar_target_symbols"):
                            pipeline._radar_target_symbols.clear()
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
        await SessionManager.close()
        print("🧹 [System] Глобальные aiohttp сессии закрыты.")

def acquire_single_instance_lock():
    """
    Acquires an exclusive OS-level file lock on data/memory/bot.lock.
    Prevents duplicate instances of python/main.py from running simultaneously,
    which causes Telegram HTTP 409 Conflict (getUpdates collision) and duplicate trade executions.
    """
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if "--force-ignore-lock" in sys.argv:
        return None
    lock_path = os.path.join("data", "memory", "bot.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock_file = open(lock_path, "a+")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"PID={os.getpid()}\nSTART={datetime.now().isoformat()}\n")
        lock_file.flush()
        return lock_file
    except (IOError, OSError):
        print("\n" + "=" * 70)
        print("🛑 [SingleInstanceGuard] ОБНАРУЖЕН ДРУГОЙ РАБОТАЮЩИЙ ЭКЗЕМПЛЯР БОТА!")
        print("⚠️ Запуск второго процесса заблокирован для предотвращения:")
        print("   1) Конфликта Telegram Polling (HTTP 409 getUpdates conflict)")
        print("   2) Дублирования ордеров и рассинхронизации состояния счетов.")
        print("💡 Если вы хотите перезапустить бота, сначала остановите предыдущий процесс.")
        print("=" * 70 + "\n")
        sys.exit(0)

if __name__ == "__main__":
    _instance_lock = acquire_single_instance_lock()
    try:
        asyncio.run(main())
    finally:
        if _instance_lock:
            try:
                _instance_lock.close()
            except Exception:
                pass

