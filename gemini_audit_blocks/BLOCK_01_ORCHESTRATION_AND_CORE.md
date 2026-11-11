# BLOCK 01: ORCHESTRATION & RUNTIME CORE
> **Назначение:** Досье для стороннего глубокого аудита кодовой базы моделью Gemini Argon / сторонним ИИ.
> **Дата генерации:** 2026-11-10 (Актуальное боевое состояние репозитория)

## Описание блока
Главная точка входа системы, выравнивание цикла по 15m свечам, SingleInstanceGuard замок процесса, оркестрация TradingPipeline, базовая конфигурация и модели данных.

## Оглавление файлов блока
1. [`python/main.py`](#file-python-main-py)
2. [`python/core/pipeline.py`](#file-python-core-pipeline-py)
3. [`python/core/config.py`](#file-python-core-config-py)
4. [`python/core/models.py`](#file-python-core-models-py)
5. [`python/core/state_store.py`](#file-python-core-state_store-py)
6. [`python/core/session.py`](#file-python-core-session-py)
7. [`python/core/logger.py`](#file-python-core-logger-py)
8. [`python/core/utils.py`](#file-python-core-utils-py)

---

<a id="file-python-main-py"></a>
## File: `python/main.py` (409 строк, 21.5 KB)

```python
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

```

---

<a id="file-python-core-pipeline-py"></a>
## File: `python/core/pipeline.py` (2682 строк, 159.1 KB)

```python
import os
import time
import datetime
import asyncio
import math
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional

from core.utils import get_msk_status, _escape_md
from core.config import config
from core.diagnostics import tracker
from core.strategy_router import StrategyRouter, StrategyProfile
from core.models import FinalTradeDecision, FinalRiskDecision, ExecutionResult
from core.deterministic_guard import DeterministicGuard

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
from agents.reflector_agent import ReflectorAgent
from agents.memory_manager import MemoryManager
from services.market_data_service import MarketDataService
from services.telegram_service import TelegramService
from core.interfaces import BaseTradingService
from core.logger import TradeLogger
from core.data_quality_guard import DataQualityGuard
from core.llm_client import LLMClient

@dataclass
class AgentRegistry:
    universe: UniverseAgent
    scanner: ScannerAgent
    candle: CandleAgent
    orderbook: OrderBookAgent
    oi_funding: OIFundingAgent
    news: NewsAgent
    indicator: IndicatorAgent
    bull: BullAgent
    bear: BearAgent
    ceo: CEOAgent
    sentinel: SentinelAgent
    regime: RegimeAgent
    risk: RiskManager
    telegram: TelegramAgent
    reflector: ReflectorAgent
    memory: MemoryManager

@dataclass
class ServiceRegistry:
    logger: TradeLogger
    fetcher: MarketDataService
    tg_sender: TelegramService
    trading_service: BaseTradingService

async def safe_reflect(reflector_agent, closed_trade, market_data):
    """Fire-and-forget wrapper for ReflectorAgent.reflect() with error isolation."""
    try:
        await reflector_agent.reflect(closed_trade, market_data)
    except Exception as e:
        print(f"⚠️ [Reflector] Ошибка при пост-мортем анализе: {type(e).__name__}: {e}")

def is_risk_free_position(pos: dict) -> bool:
    """Returns True if position has zero open price risk (BE or trailing stop beyond entry)."""
    if not isinstance(pos, dict):
        return False
    state = str(pos.get("protection_state") or pos.get("state") or "").upper()
    if "PENDING" not in state and any(k in state for k in ("BREAK_EVEN", "TRAILING", "TIME_STOP_BE")):
        return True
    plan = pos.get("partial_tp_plan")
    if isinstance(plan, dict) and plan.get("be_activated"):
        return True
    direction = str(pos.get("direction", pos.get("side", ""))).upper()
    try:
        entry = float(pos.get("entry_price") or 0.0)
        sl = float(pos.get("sl_price") or 0.0)
        if entry > 0 and sl > 0:
            if ("LONG" in direction or "BUY" in direction) and sl >= entry:
                return True
            if ("SHORT" in direction or "SELL" in direction) and sl <= entry:
                return True
    except (ValueError, TypeError):
        pass
    return False


class TradingPipeline:
    def __init__(self, agents: AgentRegistry, services: ServiceRegistry, exchange_name: str):
        self.agents = agents
        self.services = services
        self.exchange_name = exchange_name
        self.data_guard = DataQualityGuard(self.services.logger)
        self._cycle_running = False
        self._fast_radar_cooldowns: Dict[str, float] = {}
        self._pullback_watchlist: Dict[str, Dict[str, Any]] = {}
        self._load_pullback_watchlist()
        self._priority_symbol: Optional[str] = None
        self._priority_direction: Optional[str] = None
        self._effective_profile = getattr(config, "TRADING_PROFILE", "BALANCED")
        self._sentinel_lock = asyncio.Lock()
        self._last_sentinel_sync_ts: float = 0.0
        self._macro_leaders_alignment: Dict[str, str] = {}
        self._last_full_scan_ts: float = 0.0
        self._pullback_context: Dict[str, Dict[str, Any]] = {}
        self._execution_lock = asyncio.Lock()
        self._candidate_semaphore = asyncio.Semaphore(3)

    def _load_pullback_watchlist(self):
        """Loads active unexpired pullback candidates from persistent disk store."""
        try:
            from core.state_store import StateStore
            data = StateStore.load("data/memory/pullback_watchlist.json", default={})
            if isinstance(data, dict):
                now = time.time()
                valid = {
                    k: v for k, v in data.items()
                    if isinstance(v, dict) and float(v.get("expires_at", 0.0) or 0.0) > now
                }
                self._pullback_watchlist = valid
                if len(valid) != len(data):
                    StateStore.save("data/memory/pullback_watchlist.json", self._pullback_watchlist)
        except Exception as e:
            if hasattr(self, "services") and hasattr(self.services, "logger") and self.services.logger:
                self.services.logger.warning(f"[Pipeline] Could not load pullback_watchlist.json: {e}")

    def _save_pullback_watchlist(self):
        """Persists pullback watchlist atomically to disk for UI and restart resilience."""
        try:
            from core.state_store import StateStore
            StateStore.save("data/memory/pullback_watchlist.json", self._pullback_watchlist)
        except Exception as e:
            if hasattr(self, "services") and hasattr(self.services, "logger") and self.services.logger:
                self.services.logger.warning(f"[Pipeline] Could not save pullback_watchlist.json: {e}")

    async def fetch_live_price(self, symbol: str, fallback_price: float = 0.0) -> float:
        """
        Queries the freshest live price from the orderbook mid-price or ticker.
        """
        try:
            if hasattr(self.services, "fetcher") and hasattr(self.services.fetcher, "fetch_order_book"):
                ob = await self.services.fetcher.fetch_order_book(symbol)
                if isinstance(ob, dict):
                    best_bid = float(ob.get("best_bid", 0.0) or 0.0)
                    best_ask = float(ob.get("best_ask", 0.0) or 0.0)
                    if best_bid > 0 and best_ask > 0:
                        return round((best_bid + best_ask) / 2.0, 6)
        except Exception:
            pass

        try:
            if hasattr(self.services, "fetcher") and hasattr(self.services.fetcher, "fetch_all_market_data"):
                fresh_md = await self.services.fetcher.fetch_all_market_data(symbol)
                if isinstance(fresh_md, dict):
                    fresh_px = float(fresh_md.get("price_data", {}).get("current_price", 0.0) or 0.0)
                    if fresh_px > 0:
                        return fresh_px
        except Exception:
            pass

        return float(fallback_price)

    @staticmethod
    def evaluate_price_drift(
        symbol: str,
        direction: str,
        snapshot_price: float,
        live_price: float,
        tp_price: float,
        sl_price: float
    ) -> Tuple[bool, float, float, float, str]:
        """
        Execution Price Drift Guard & SL/TP Re-anchoring (delegates to DeterministicGuard).
        """
        return DeterministicGuard.evaluate_price_drift(
            symbol=symbol,
            direction=direction,
            snapshot_price=snapshot_price,
            live_price=live_price,
            tp_price=tp_price,
            sl_price=sl_price
        )

    def _build_deterministic_debate_theses(
        self,
        symbol: str,
        strategy_mode: str,
        direction_bias: str,
        reports: List[Dict[str, Any]],
        key_levels: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        FAST_DEBATE_BYPASS / Predator shortcut:
        Bypasses LLM text essays for Bull/Bear, building compact deterministic theses
        from subordinate analyst reports in 0ms.
        """
        target_dir = str(direction_bias).upper()
        rep_map = {r.get("agent_name", ""): r for r in reports if isinstance(r, dict)}
        ind_r = rep_map.get("Indicator_Agent", {})
        cnd_r = rep_map.get("Candle_Agent", {})
        ob_r = rep_map.get("Order_Book_Agent", {})
        oi_r = rep_map.get("OI_Funding_Agent", {})

        curr_px = float(key_levels.get("current_price", 0.0) or 0.0)
        ema_20 = float(key_levels.get("ema_20", curr_px) or curr_px)
        bb_upper = float(key_levels.get("bb_upper", curr_px * 1.02) or (curr_px * 1.02))
        bb_lower = float(key_levels.get("bb_lower", curr_px * 0.98) or (curr_px * 0.98))
        atr_14 = float(key_levels.get("atr_14", curr_px * 0.015) or (curr_px * 0.015))

        if target_dir == "SHORT":
            bear_args = [
                f"Overbought extreme / upper band rejection: {ind_r.get('reasoning', 'BB upper band retested')}",
                f"Candle pattern exhaustion: {cnd_r.get('reasoning', 'Rejection wick on highs')}",
                f"Order book wall resistance: {ob_r.get('reasoning', 'Ask pressure absorbs bids')}",
                f"Derivatives trap: {oi_r.get('reasoning', 'Longs overcrowded at resistance')}"
            ]
            bull_args = [
                "Momentum reaching exhaustion at resistance band.",
                "Lack of aggressive buy volume to sustain breakout.",
                "High probability of mean reversion toward 20 EMA."
            ]
            bear_verdict = {
                "status": "OK",
                "is_fallback": False,
                "thesis_score": 9,
                "bearish_arguments": bear_args,
                "potential_target": ema_20 if ema_20 > 0 else (curr_px - 2 * atr_14),
                "invalidation_level": bb_upper if bb_upper > curr_px else (curr_px + 1.5 * atr_14),
                "summary": f"Deterministic Bear Thesis: High-probability {strategy_mode} SHORT. Exhaustion at upper band, mean reversion target {ema_20:.2f}."
            }
            bull_verdict = {
                "status": "OK",
                "is_fallback": False,
                "thesis_score": 2,
                "bullish_arguments": bull_args,
                "potential_target": bb_upper,
                "invalidation_level": ema_20,
                "summary": "Deterministic Bull Thesis: Momentum exhausted into resistance. Counter-trend risks elevated."
            }
        else:
            bull_args = [
                f"Oversold extreme / lower band bounce: {ind_r.get('reasoning', 'BB lower band panic exhaustion')}",
                f"Candle pattern absorption: {cnd_r.get('reasoning', 'Lower rejection wick / seller exhaustion')}",
                f"Order book bid absorption: {ob_r.get('reasoning', 'Bid wall supporting price')}",
                f"Derivatives trap: {oi_r.get('reasoning', 'Shorts trapped on panic lows')}"
            ]
            bear_args = [
                "Downtrend momentum exhausted at lower Bollinger boundary.",
                "Panic sellers absorbed by institutional bid liquidity.",
                "High probability of mean reversion relief bounce toward 20 EMA."
            ]
            bull_verdict = {
                "status": "OK",
                "is_fallback": False,
                "thesis_score": 9,
                "bullish_arguments": bull_args,
                "potential_target": ema_20 if ema_20 > 0 else (curr_px + 2 * atr_14),
                "invalidation_level": bb_lower if bb_lower < curr_px else (curr_px - 1.5 * atr_14),
                "summary": f"Deterministic Bull Thesis: High-probability {strategy_mode} LONG. Seller exhaustion at lower band, mean reversion target {ema_20:.2f}."
            }
            bear_verdict = {
                "status": "OK",
                "is_fallback": False,
                "thesis_score": 2,
                "bearish_arguments": bear_args,
                "potential_target": bb_lower,
                "invalidation_level": ema_20,
                "summary": "Deterministic Bear Thesis: Heavy oversold conditions and liquidity sweep limit downside."
            }
        return bull_verdict, bear_verdict

    def apply_pipeline_guards_and_sync(
        self,
        strategy_profile: StrategyProfile,
        ceo_verdict: Dict[str, Any],
        profile: str,
        symbol: str = "",
        market_data: Optional[Dict[str, Any]] = None
    ) -> FinalTradeDecision:
        """
        Deterministic Guard:
        Enforces Strategy Direction Guards, Volatility Momentum pullback override,
        funding rate protection, and conviction threshold validation.
        Produces an immutable FinalTradeDecision (which also unpacks as a 4-tuple
        (decision, conviction, trade_action, min_conv) for backward compatibility).
        """
        return DeterministicGuard.evaluate(
            strategy_profile=strategy_profile,
            ceo_proposal=ceo_verdict,
            profile=profile,
            risk_manager=self.agents.risk,
            market_data=market_data,
            symbol=symbol,
            logger=self.services.logger
        )

    def can_apply_profile_uplift(
        self,
        symbol: str,
        candidate_direction: str,
        macro_leaders_state: Dict[str, str],
        macro_regime: str = "",
    ) -> Tuple[bool, str]:
        """
        Проверяет, не идет ли Asset Profile Uplift альткоина вразрез с трендом BTC/ETH,
        и блокирует Uplift до AGGRESSIVE в режиме боковика (RANGE_CHOPPY).
        macro_leaders_state: {"BTC": "BEARISH" | "BULLISH" | "MIXED", "ETH": ...}
        """
        from core.config import config
        if getattr(config, "ANTI_BOT_MODE", False) is True:
            return True, "Anti-Bot Predator Mode: bypass macro leader opposition"

        if macro_regime == "RANGE_CHOPPY":
            return False, "Макро-режим RANGE_CHOPPY (флэт): Uplift до AGGRESSIVE заблокирован"

        base_sym = symbol.split("-")[0].upper()
        # Исключение для самих лидеров и некоррелированного золота
        if base_sym in ("BTC", "ETH", "XAUT", "PAXG"):
            return True, "Leader or non-correlated asset"

        direction = candidate_direction.upper()
        btc_bias = macro_leaders_state.get("BTC", "MIXED").upper()
        eth_bias = macro_leaders_state.get("ETH", "MIXED").upper()

        if direction == "LONG" and ("BEARISH" in (btc_bias, eth_bias)):
            opposing = [k for k, v in (("BTC", btc_bias), ("ETH", eth_bias)) if v == "BEARISH"]
            return False, f"Поводыри ({', '.join(opposing)}) в медвежьем тренде"

        if direction == "SHORT" and ("BULLISH" in (btc_bias, eth_bias)):
            opposing = [k for k, v in (("BTC", btc_bias), ("ETH", eth_bias)) if v == "BULLISH"]
            return False, f"Поводыри ({', '.join(opposing)}) в бычьем тренде"

        return True, "Macro leaders aligned or neutral"

    @staticmethod
    def _clean_mtf_for_llm(mtf: Dict[str, Any]) -> Dict[str, Any]:
        """Strips heavy candle arrays (candles_20) from MTF context to save ~1,500 prompt tokens per LLM call."""
        if not isinstance(mtf, dict):
            return {}
        clean = {
            "trend_15m": mtf.get("trend_15m", "NEUTRAL"),
            "trend_1h": mtf.get("trend_1h", "NEUTRAL"),
            "trend_4h": mtf.get("trend_4h", "NEUTRAL"),
            "mtf_alignment": mtf.get("mtf_alignment", "MIXED_CHOP")
        }
        for tf_key in ["tf_15m", "tf_1h", "tf_4h"]:
            tf = mtf.get(tf_key)
            if isinstance(tf, dict):
                clean[tf_key] = {k: v for k, v in tf.items() if k != "candles_20"}
        return clean

    async def evaluate_candidate(
        self,
        symbol: str,
        cycle_number: int = 1,
        detected_regime: str = "RANGE_CHOPPY",
        profile: str = "BALANCED",
        macro_cache: Optional[Dict[str, Any]] = None,
        portfolio_data: Optional[Dict[str, Any]] = None,
        tracker: Optional[Any] = None,
        scan_summaries: Optional[List[Dict[str, Any]]] = None,
        retest_symbol: Optional[str] = None,
        retest_direction: Optional[str] = None,
        targeted_symbol: Optional[str] = None,
        candidate_sem: Optional[asyncio.Semaphore] = None
    ) -> bool:
        """
        Evaluates a single candidate asset through Stages 2-7.
        Executes concurrently under candidate_sem with atomic execution lock in Stage 5.
        """
        if macro_cache is None:
            macro_cache = {}
        if portfolio_data is None:
            portfolio_data = await self.services.trading_service.get_portfolio_summary()
        if scan_summaries is None:
            scan_summaries = []
        if tracker is None:
            class _DummyTracker:
                def record_scan(self): pass
                def record_rejection(self, *a, **k): pass
                def record_trade(self): pass
                def record_execution_failed(self): pass
            tracker = _DummyTracker()
        if candidate_sem is None:
            candidate_sem = getattr(self, "_candidate_semaphore", None) or asyncio.Semaphore(3)

        async with candidate_sem:
            # Risk-Free Slot Check: allow up to 4 total if 1+ in BE, but never more than 3 with active risk
            cur_pos_dict = self.services.trading_service.active_positions
            cur_pos_list = list(cur_pos_dict.values()) if isinstance(cur_pos_dict, dict) else (cur_pos_dict or [])
            cur_risk_pos = [p for p in cur_pos_list if not is_risk_free_position(p)]
            cur_be_pos = [p for p in cur_pos_list if is_risk_free_position(p)]
            cur_max_risk = getattr(config, "MAX_CONCURRENT_POSITIONS", 3)
            cur_max_total = getattr(config, "MAX_ACCOUNT_POSITIONS", 4) if len(cur_be_pos) > 0 else cur_max_risk

            if len(cur_pos_list) >= cur_max_total or len(cur_risk_pos) >= cur_max_risk:
                msg = f"⏸️ Достигнут лимит позиций ({len(cur_pos_list)}/{cur_max_total}, с риском: {len(cur_risk_pos)}/{cur_max_risk}). Пропуск новых активов."
                print(msg)
                self.services.logger.info(f"[System_Core] {msg}")
                return False

            try:
                candidate_id = f"{symbol}:c{cycle_number}_{int(time.time() * 1000)}"
                is_retest = (symbol == retest_symbol)
                print(f"\n🔍 ПОЛНЫЙ АНАЛИЗ (15m, 1H, 4H) NADO DEX: {symbol}" + (" [PULLBACK_RETEST]" if is_retest else ""))
                tracker.record_scan()

                # СТАДИЯ 2: СБОР ДАННЫХ И ПРОВЕРКА СТАТУСА
                self.services.logger.info(f"[Stage 2] Сбор мульти-таймфреймовых данных (15m, 1H, 4H) для {symbol}...")
                try:
                    if symbol in macro_cache and (time.time() - float(macro_cache[symbol].get("price_data", {}).get("timestamp", 0) or 0) < 45):
                        market_data = macro_cache[symbol]
                    else:
                        market_data = await self.services.fetcher.fetch_all_market_data(symbol)

                    # Inject Nado native market limits
                    limits = await self.services.trading_service.get_market_limits(symbol)
                    if "derivatives_data" not in market_data:
                        market_data["derivatives_data"] = {}
                    market_data["derivatives_data"]["strict_metadata"] = True
                    if "size_increment" in limits:
                        market_data["derivatives_data"]["size_increment"] = limits["size_increment"]
                    if "min_notional" in limits and limits["min_notional"] > 0:
                        market_data["derivatives_data"]["min_notional"] = limits["min_notional"]
                    if "min_size" in limits:
                        market_data["derivatives_data"]["min_size"] = limits["min_size"]
                    if "price_increment" in limits and limits["price_increment"] > 0:
                        market_data["derivatives_data"]["price_increment"] = limits["price_increment"]

                    if is_retest:
                        market_data["is_pullback_retest"] = True
                        pb_ctx = getattr(self, "_pullback_context", {}).get(symbol, {})
                        if pb_ctx:
                            market_data["invalidation_price"] = pb_ctx.get("invalidation_price", 0.0)
                            market_data["target_pullback_price"] = pb_ctx.get("target_pullback_price", 0.0)
                            market_data["signal_price"] = pb_ctx.get("signal_price", 0.0)
                        if retest_direction:
                            market_data["pullback_retest_direction"] = retest_direction
                            market_data["retest_direction"] = retest_direction

                except Exception as e:
                    print(f"❌ Ошибка загрузки данных для {symbol}: {e}. Пропускаем.")
                    scan_summaries.append({"symbol": symbol, "status": "⏭ Пропуск", "reason": str(e)})
                    tracker.record_rejection("FETCH_ERROR")
                    return False

                # 2.1 DATA QUALITY GUARD (Strict Deterministic VETO)
                try:
                    is_valid, dq_reason = self.data_guard.validate(symbol, market_data)
                except Exception as e:
                    is_valid, dq_reason = False, f"DATA_GUARD_EXCEPTION: {e}"

                if not is_valid:
                    print(f"🛑 Data Quality Guard забраковал данные {symbol}. Причина: {dq_reason}")
                    self.services.logger.warning(f"[System_Core] DATA QUALITY VETO | {symbol} | {dq_reason}")
                    scan_summaries.append({"symbol": symbol, "status": "⛔ DATA INVALID", "reason": dq_reason})
                    tracker.record_rejection("DATA_INVALID")
                    self._record_candidate_snapshot(
                        candidate_id=candidate_id,
                        symbol=symbol,
                        cycle_number=cycle_number,
                        macro_regime=detected_regime,
                        profile=profile,
                        stage="DATA_GUARD",
                        final_action="REJECTED",
                        rejection_tag="DATA_INVALID",
                        rejection_reason=dq_reason,
                        market_data=market_data
                    )
                    return False

                current_price = market_data.get("price_data", {}).get("current_price", 0)

                # 2.5 Scanner Agent (ATR Volatility & Spread Guard)
                self.services.logger.info(f"[Stage 2.5] Scanner Agent проверяет ATR волатильность и спред для {symbol}...")
                scan_result = await self.agents.scanner.analyze(market_data)

                scanner_blocked = not scan_result.get("proceed", True)
                scanner_reason = scan_result.get('reasoning', 'Неизвестная причина')
                if scanner_blocked:
                    print(f"🛑 Scanner Agent пропустил {symbol}. Причина: {scanner_reason}")
                    self.services.logger.info(f"[System_Core] Пропуск {symbol}. Причина: {scanner_reason}")
                    scan_summaries.append({"symbol": symbol, "status": "⛔ СКАН ЗАБЛОКИРОВАН", "reason": scanner_reason})
                    tracker.record_rejection(scan_result.get('status', 'SCANNER_BLOCKED'))
                    self._record_candidate_snapshot(
                        candidate_id=candidate_id,
                        symbol=symbol,
                        cycle_number=cycle_number,
                        macro_regime=detected_regime,
                        profile=profile,
                        stage="SCANNER",
                        final_action="REJECTED",
                        rejection_tag=scan_result.get('status', 'SCANNER_BLOCKED'),
                        rejection_reason=scanner_reason,
                        market_data=market_data
                    )
                    return False

                # СТАДИЯ 3: СИНДИКАТ АНАЛИТИКОВ
                self.services.logger.info(f"[Stage 3] Запуск синдиката аналитиков для {symbol} (Concurrent)...")
                analyst_list = [self.agents.candle, self.agents.orderbook, self.agents.oi_funding, self.agents.news, self.agents.indicator]

                # QW Concurrency: Запуск аналитиков параллельно
                reports = await asyncio.gather(*[agent.analyze(market_data) for agent in analyst_list], return_exceptions=True)

                # Tag each report with agent_name for the deterministic CEO voting engine
                valid_reports = []
                has_critical_error = False

                for agent, report in zip(analyst_list, reports):
                    if isinstance(report, Exception):
                        self.services.logger.error(f"[Stage 3] Ошибка агента {agent.name}: {report}")
                        continue

                    if isinstance(report, dict):
                        if report.get("signal") == "ERROR":
                            self.services.logger.error(f"[Stage 3] Агент {agent.name} вернул статус ERROR. Данные недоступны.")
                            return False
                        report["agent_name"] = agent.name
                        valid_reports.append(report)
                    else:
                        self.services.logger.error(f"[Stage 3] Агент {agent.name} вернул некорректный ответ.")
                        return False

                if len(valid_reports) < 3:
                    has_critical_error = True

                if has_critical_error:
                    msg = f"🛑 Пропуск {symbol}. Причина: Слишком много ошибок агентов (успешно только {len(valid_reports)}/5)."
                    print(msg)
                    self.services.logger.info(f"[System_Core] {msg}")
                    scan_summaries.append({
                        "symbol": symbol,
                        "decision": "HOLD",
                        "conviction": 0,
                        "scanner_status": "✅ OK" if not scanner_blocked else "⚠️ ЗАБЛОКИРОВАН СКАНЕРОМ",
                        "scanner_reason": None if not scanner_blocked else scanner_reason,
                        "risk_approved": False,
                        "risk_reason": f"Fallback: недостаточно успешных аналитиков ({len(valid_reports)}/5)",
                        "ceo_reasoning": "Bypassed (Too many agent errors)",
                        "status": "🛑 ОШИБКА АГЕНТОВ"
                    })
                    tracker.record_rejection("AGENT_ERROR")
                    self._record_candidate_snapshot(
                        candidate_id=candidate_id,
                        symbol=symbol,
                        cycle_number=cycle_number,
                        macro_regime=detected_regime,
                        profile=profile,
                        stage="ANALYST_QUORUM",
                        final_action="REJECTED",
                        rejection_tag="AGENT_ERROR",
                        rejection_reason=f"Insufficient valid analysts ({len(valid_reports)}/5)",
                        analyst_reports=valid_reports,
                        market_data=market_data
                    )
                    return False

                # --- STRATEGY ROUTER ---
                strategy_profile = StrategyRouter.evaluate(
                    symbol, market_data, valid_reports, macro_cache,
                    detected_regime=detected_regime, profile=profile
                )
                has_directional_signal = strategy_profile.has_directional_signal
                strategy_mode = strategy_profile.strategy_mode
                
                if has_directional_signal:
                    self.services.logger.info(f"[System_Core] Strategy Router: {symbol} допущен ({strategy_profile.reasoning})")

                    # Pre-Debate Directional Cluster Cap Gate (Save 100% LLM tokens when direction slot is full)
                    active_positions = getattr(self.services.trading_service, "active_positions", {})
                    target_dir = str(strategy_profile.direction_bias).upper()
                    if target_dir in ["LONG", "SHORT"] and isinstance(active_positions, dict):
                        seen_bases = set()
                        same_dir_open = 0
                        same_dir_risk = 0
                        for pos_k, p in active_positions.items():
                            if isinstance(p, dict):
                                sym_raw = str(p.get("symbol") or pos_k or "").upper().replace('/', '-')
                                base_ticker = sym_raw.split('-')[0].strip() if sym_raw else ""
                                p_id = p.get("product_id")
                                dedup_key = f"{base_ticker}:{p_id}" if (base_ticker and p_id is not None) else (base_ticker or str(pos_k))

                                if dedup_key in seen_bases:
                                    continue
                                seen_bases.add(dedup_key)

                                p_dir = str(p.get("direction", p.get("side", ""))).upper()
                                if (target_dir == "LONG" and ("LONG" in p_dir or "BUY" in p_dir)) or \
                                   (target_dir == "SHORT" and ("SHORT" in p_dir or "SELL" in p_dir)):
                                    same_dir_open += 1
                                    if not is_risk_free_position(p):
                                        same_dir_risk += 1

                        # Formula: max 2 with active risk; allow 3rd total only if at least 1 is in Break-Even
                        max_dir_risk = 2
                        max_dir_total = 3 if (same_dir_open > same_dir_risk) else 2

                        if same_dir_open >= max_dir_total or same_dir_risk >= max_dir_risk:
                            msg = (
                                f"⏸️ Пропуск {symbol}. Лимит однонаправленных позиций ({same_dir_open}/{max_dir_total} {target_dir}, "
                                f"с риском: {same_dir_risk}/{max_dir_risk}) заполнен. Дебаты LLM пропущены (экономия токенов 100%)."
                            )
                            print(msg)
                            self.services.logger.info(f"[System_Core] {msg}")

                            scanner_status = "⚠️ ЗАБЛОКИРОВАН СКАНЕРОМ" if scanner_blocked else "✅ OK"
                            scan_summaries.append({
                                "symbol": symbol,
                                "decision": "HOLD",
                                "conviction": 0,
                                "scanner_status": scanner_status,
                                "scanner_reason": scanner_reason if scanner_blocked else None,
                                "risk_approved": False,
                                "risk_reason": f"Pre-Debate Gate: лимит однонаправленных позиций ({same_dir_open}/2 {target_dir}) исчерпан",
                                "ceo_reasoning": "Bypassed (Pre-Debate Cluster Cap)",
                                "status": "⏸️ ЛИМИТ ПОЗИЦИЙ"
                            })
                            tracker.record_rejection("DIRECTIONAL_CLUSTER_CAP")
                            self._record_candidate_snapshot(
                                candidate_id=candidate_id,
                                symbol=symbol,
                                cycle_number=cycle_number,
                                macro_regime=detected_regime,
                                profile=profile,
                                stage="DIRECTIONAL_CLUSTER_GATE",
                                final_action="REJECTED",
                                rejection_tag="DIRECTIONAL_CLUSTER_CAP",
                                rejection_reason=f"Pre-Debate Gate: {same_dir_open}/2 active {target_dir} positions already open",
                                strategy_profile=strategy_profile,
                                analyst_reports=valid_reports,
                                market_data=market_data
                            )
                            return False

                if not has_directional_signal:
                    msg = f"⏸️ Пропуск {symbol}. Причина: {strategy_profile.reasoning} Экономим токены."
                    print(msg)
                    self.services.logger.info(f"[System_Core] {msg}")

                    scanner_status = "⚠️ ЗАБЛОКИРОВАН СКАНЕРОМ" if scanner_blocked else "✅ OK"
                    is_anti_chase = (strategy_profile.strategy_mode == "ANTI_CHASE_REENTRY_VETO")
                    is_macro_box = strategy_profile.strategy_mode in ["MACRO_BOX_FLOOR_VETO", "MACRO_BOX_CEILING_VETO"]
                    is_pullback_route = is_anti_chase or is_macro_box or ("WAIT_FOR_PULLBACK" in str(strategy_profile.reasoning))
                    target_pb_dir = str(strategy_profile.direction_bias or "").upper()

                    if (
                        is_pullback_route
                        and target_pb_dir in ["LONG", "SHORT"]
                        and not getattr(config, "ANTI_BOT_MODE", False)
                        and not is_retest
                    ):
                        self.register_pullback_candidate(
                            symbol=symbol,
                            decision=target_pb_dir,
                            current_price=current_price,
                            directional_conf=75,
                            conviction=75,
                            strategy_mode="TREND_FOLLOWING",
                            market_data=market_data,
                            ceo_verdict={"reasoning": strategy_profile.reasoning},
                            strategy_profile=strategy_profile,
                            reason=strategy_profile.reasoning
                        )

                    status_text = "⏳ ОЖИДАНИЕ ОТКАТА" if is_pullback_route else "⏸️ НЕТ СИГНАЛА"
                    rejection_tag = strategy_profile.strategy_mode if is_pullback_route else "NO_SIGNAL"
                    risk_reason = f"Pre-CEO Filter: {strategy_profile.reasoning}" if is_pullback_route else "Pre-CEO Filter: нет направленного консенсуса аналитиков (боковик)"

                    scan_summaries.append({
                        "symbol": symbol,
                        "decision": "HOLD",
                        "conviction": 0,
                        "scanner_status": scanner_status,
                        "scanner_reason": scanner_reason if scanner_blocked else None,
                        "risk_approved": False,
                        "risk_reason": risk_reason,
                        "ceo_reasoning": strategy_profile.reasoning,
                        "status": status_text
                    })
                    tracker.record_rejection(rejection_tag)
                    self._record_candidate_snapshot(
                        candidate_id=candidate_id,
                        symbol=symbol,
                        cycle_number=cycle_number,
                        macro_regime=detected_regime,
                        profile=profile,
                        stage="STRATEGY_ROUTER",
                        final_action="REJECTED",
                        rejection_tag=rejection_tag,
                        rejection_reason=strategy_profile.reasoning,
                        strategy_profile=strategy_profile,
                        analyst_reports=valid_reports,
                        market_data=market_data
                    )
                    return False

                # PRE-DEBATE RISK VIABILITY CHECK (Saves ~40k tokens/cycle if trade mathematically cannot pass Risk Manager)
                is_viable = True
                viability_reason = "OK"
                try:
                    if hasattr(self.agents.risk, "is_pre_debate_viable"):
                        pre_check = self.agents.risk.is_pre_debate_viable(portfolio_data, profile_name=profile)
                        if isinstance(pre_check, tuple) and len(pre_check) == 2:
                            is_viable, viability_reason = pre_check
                except Exception as ve:
                    self.services.logger.warning(f"[Pre-Debate Gate] Safe fallback on viability check: {ve}")
                    is_viable, viability_reason = True, "OK"

                if not is_viable:
                    self.services.logger.warning(
                        f"[Pre-Debate Gate] 🛑 Bypassing Stage 4 debates on {symbol}: {viability_reason} Preserving LLM tokens."
                    )
                    self._record_candidate_snapshot(
                        candidate_id=candidate_id,
                        symbol=symbol,
                        cycle_number=cycle_number,
                        macro_regime=detected_regime,
                        profile=profile,
                        stage="PRE_DEBATE_RISK_GATE",
                        final_action="REJECTED",
                        rejection_tag="PRE_RISK_SUPPRESSION",
                        rejection_reason=viability_reason,
                        strategy_profile=strategy_profile,
                        analyst_reports=valid_reports,
                        market_data=market_data
                    )
                    return False

                # Candle-Debate Deduplication Gate
                # If this symbol was already debated and resulted in HOLD during the current 15m candle bucket,
                # do NOT re-run Bull/Bear/CEO debate until the candle closes!
                current_candle_bucket = int(time.time() // 900)
                if not hasattr(self, "_candle_debate_history") or self._candle_debate_history is None:
                    self._candle_debate_history = {}
                last_debate = self._candle_debate_history.get(symbol)
                is_targeted = (targeted_symbol is not None and symbol == targeted_symbol)
                if last_debate and last_debate[0] == current_candle_bucket and last_debate[1] == "HOLD" and not is_retest and not is_targeted:
                    msg = f"⏸️ [Candle Debate Lock] {symbol} уже получил вердикт HOLD в текущей 15m свече (bucket {current_candle_bucket}). Дебаты пропущены (экономия токенов 100%)."
                    print(msg)
                    self.services.logger.info(f"[Stage 4] {msg}")
                    return False

                # СТАДИЯ 4: MULTI-AGENT DEBATE & CEO JUDGEMENT
                self.services.logger.info(f"[Stage 4] Bull and Bear agents debating on {symbol}...")
                
                clean_mtf = self._clean_mtf_for_llm(market_data.get("multi_timeframe", {}))
                indicators_dict = market_data.get("indicators", {}) or {}
                price_data_dict = market_data.get("price_data", {}) or {}
                curr_px = float(price_data_dict.get("current_price") or indicators_dict.get("current_price") or 0.0)

                key_levels = {
                    "current_price": curr_px,
                    "ema_20": float(indicators_dict.get("ema_20") or 0.0),
                    "donchian_high": float(indicators_dict.get("donchian_high") or 0.0),
                    "donchian_low": float(indicators_dict.get("donchian_low") or 0.0),
                    "bb_upper": float(indicators_dict.get("bb_upper") or 0.0),
                    "bb_lower": float(indicators_dict.get("bb_lower") or 0.0),
                    "atr_14": float(indicators_dict.get("atr_14") or 0.0),
                    "swing_high_15m": float(indicators_dict.get("swing_high_15m") or 0.0),
                    "swing_low_15m": float(indicators_dict.get("swing_low_15m") or 0.0),
                    "high_48h": float(indicators_dict.get("high_48h") or 0.0),
                    "low_48h": float(indicators_dict.get("low_48h") or 0.0),
                    "macro_box_pos_pct": float(indicators_dict.get("macro_box_pos_pct") or 50.0),
                    "atr_target_long": round(curr_px + 2.0 * float(indicators_dict.get("atr_14") or 0.0), 4) if curr_px > 0 else 0.0,
                    "atr_target_short": round(curr_px - 2.0 * float(indicators_dict.get("atr_14") or 0.0), 4) if curr_px > 0 else 0.0,
                    "structural_sl_long": round(float(indicators_dict.get("swing_low_15m") or 0.0) - 0.25 * float(indicators_dict.get("atr_14") or 0.0), 4) if float(indicators_dict.get("swing_low_15m") or 0.0) > 0 else round(curr_px - 1.5 * float(indicators_dict.get("atr_14") or 0.0), 4),
                    "structural_sl_short": round(float(indicators_dict.get("swing_high_15m") or 0.0) + 0.25 * float(indicators_dict.get("atr_14") or 0.0), 4) if float(indicators_dict.get("swing_high_15m") or 0.0) > 0 else round(curr_px + 1.5 * float(indicators_dict.get("atr_14") or 0.0), 4),
                }

                debate_payload = {
                    "symbol": symbol,
                    "strategy_mode": strategy_mode,
                    "multi_timeframe_context": clean_mtf,
                    "analyst_reports": valid_reports,
                    "key_levels": key_levels
                }
                
                # Profit Booster #2: Fast deterministic debate bypass for high-conviction Pullback Retests
                pb_ctx_peek = getattr(self, "_pullback_context", {}).get(symbol) if is_retest else None
                saved_conf = float(pb_ctx_peek.get("directional_confidence", 0) or 0) if isinstance(pb_ctx_peek, dict) else 0.0
                is_retest_fast_bypass = (is_retest and saved_conf >= 75.0)

                is_fast_bypass = getattr(config, "FAST_DEBATE_BYPASS", False) is True
                has_sweep = getattr(strategy_profile, "liquidity_sweep", False)
                if not has_sweep:
                    sweeps = market_data.get("indicators", {}).get("liquidity_sweeps", []) or market_data.get("liquidity_sweeps", [])
                    if sweeps:
                        has_sweep = True
                is_predator_sweep = (strategy_mode == "MEAN_REVERSION" and has_sweep)

                if is_fast_bypass or is_predator_sweep or is_retest_fast_bypass:
                    if is_retest_fast_bypass:
                        bypass_tag = f"RETEST_BOOSTER_FAST_SYNTHESIS (Conf: {saved_conf:.0f}%)"
                    elif is_fast_bypass:
                        bypass_tag = "FAST_DEBATE_BYPASS"
                    else:
                        bypass_tag = "PREDATOR_SWEEP_SHORTCUT"
                    self.services.logger.info(
                        f"[Stage 4] ⚡ [{bypass_tag}] Bypassing LLM debate on {symbol} ({strategy_mode} {strategy_profile.direction_bias}). "
                        f"Building deterministic theses in 0ms."
                    )
                    bull_verdict, bear_verdict = self._build_deterministic_debate_theses(
                        symbol=symbol,
                        strategy_mode=strategy_mode,
                        direction_bias=strategy_profile.direction_bias,
                        reports=valid_reports,
                        key_levels=key_levels
                    )
                    bull_status = "OK"
                    bear_status = "OK"
                    debate_status = "COMPLETE"
                    bull_res = bull_verdict
                    bear_res = bear_verdict
                else:
                    # Запускаем Быка и Медведя строго параллельно с изоляцией исключений
                    bull_res, bear_res = await asyncio.gather(
                        self.agents.bull.analyze(debate_payload),
                        self.agents.bear.analyze(debate_payload),
                        return_exceptions=True
                    )
                
                bull_status = "OK"
                if isinstance(bull_res, Exception) or not isinstance(bull_res, dict) or bull_res.get("signal") == "ERROR":
                    self.services.logger.error(f"[Stage 4] Ошибка Bull Agent: {bull_res}")
                    bull_status = "ERROR"
                    bull_verdict = {
                        "status": "ERROR",
                        "is_fallback": True,
                        "error_details": str(bull_res),
                        "thesis_score": 0,
                        "bullish_arguments": ["Бычий тезис недоступен (fallback из-за ошибки LLM)."],
                        "potential_target": 0.0,
                        "invalidation_level": 0.0,
                        "summary": "Fallback bull thesis (error)"
                    }
                else:
                    bull_verdict = bull_res
                    bull_verdict["status"] = "OK"
                    bull_verdict["is_fallback"] = False

                bear_status = "OK"
                if isinstance(bear_res, Exception) or not isinstance(bear_res, dict) or bear_res.get("signal") == "ERROR":
                    self.services.logger.error(f"[Stage 4] Ошибка Bear Agent: {bear_res}")
                    bear_status = "ERROR"
                    bear_verdict = {
                        "status": "ERROR",
                        "is_fallback": True,
                        "error_details": str(bear_res),
                        "thesis_score": 0,
                        "bearish_arguments": ["Медвежий тезис недоступен (fallback из-за ошибки LLM)."],
                        "potential_target": 0.0,
                        "invalidation_level": 0.0,
                        "summary": "Fallback bear thesis (error)"
                    }
                else:
                    bear_verdict = bear_res
                    bear_verdict["status"] = "OK"
                    bear_verdict["is_fallback"] = False

                if bull_status == "OK" and bear_status == "OK":
                    debate_status = "COMPLETE"
                elif bull_status == "ERROR" and bear_status == "ERROR":
                    debate_status = "FAILED"
                elif bull_status == "ERROR":
                    debate_status = "DEGRADED_BULL_FAILED"
                else:
                    debate_status = "DEGRADED_BEAR_FAILED"

                if debate_status != "COMPLETE":
                    self.services.logger.warning(
                        f"[Stage 4] Debate completeness degraded for {symbol}: {debate_status} "
                        f"(Bull: {bull_status}, Bear: {bear_status})"
                    )
                
                bull_summary = str(bull_verdict.get('summary', 'N/A')).strip()
                bear_summary = str(bear_verdict.get('summary', 'N/A')).strip()
                print(f"🐂 Bull Thesis ({bull_status}): {bull_summary}")
                print(f"🐻 Bear Thesis ({bear_status}): {bear_summary}")
                
                self.services.logger.info(f"[Stage 4] CEO Agent (Judge) evaluates the debate and MTF trend for {symbol} (Debate: {debate_status})...")
                historical_context = self.agents.memory.get_recent_context(limit=3, symbol=symbol)
                symbol_lessons = self.agents.reflector.get_lessons(limit=5, symbol=symbol)
                
                # Per-Asset Profile Resolution:
                # If user configured AGGRESSIVE, but macro regime was downgraded to BALANCED due to BTC chop,
                # do NOT penalize an asset that exhibits FULL_ALIGNMENT,
                # UNLESS the candidate direction conflicts with macro market leaders (BTC/ETH) or macro is RANGE_CHOPPY.
                effective_asset_profile = profile
                if getattr(config, "TRADING_PROFILE", "BALANCED") == "AGGRESSIVE" and profile == "BALANCED":
                    asset_mtf_align = market_data.get("multi_timeframe", {}).get("mtf_alignment", "")
                    # Strictly require FULL_ALIGNMENT and reject if macro regime is deep chop (RANGE_CHOPPY)
                    is_anti_bot_asset = bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION")
                    if (asset_mtf_align == "FULL_ALIGNMENT" and detected_regime != "RANGE_CHOPPY") or is_anti_bot_asset:
                        cand_dir = getattr(strategy_profile, "direction_bias", "NEUTRAL") or "NEUTRAL"
                        can_uplift, uplift_reason = self.can_apply_profile_uplift(
                            symbol=symbol,
                            candidate_direction=cand_dir,
                            macro_leaders_state=getattr(self, "_macro_leaders_alignment", {}),
                            macro_regime=detected_regime
                        )
                        if can_uplift:
                            effective_asset_profile = "AGGRESSIVE"
                            self.services.logger.info(
                                f"⚡ [Asset Profile Uplift] {symbol} имеет {asset_mtf_align}. "
                                f"Применен профиль AGGRESSIVE вместо макро BALANCED ({uplift_reason})."
                            )
                        else:
                            print(f"🛡️ [Asset Profile Uplift Blocked] {symbol} ({cand_dir}) оставлен на профиле BALANCED: {uplift_reason}.")
                            self.services.logger.info(
                                f"🛡️ [Asset Profile Uplift Blocked] {symbol} ({cand_dir}) оставлен на профиле BALANCED: {uplift_reason}."
                            )

                pb_context = getattr(self, "_pullback_context", {}).pop(symbol, None) if is_retest else None

                ceo_payload = {
                    "symbol": symbol,
                    "strategy_mode": strategy_mode,
                    "direction_bias": strategy_profile.direction_bias,
                    "router_reasoning": strategy_profile.reasoning,
                    "macro_regime": detected_regime,
                    "macro_profile": effective_asset_profile,
                    "multi_timeframe_context": clean_mtf,
                    "bull_thesis": bull_verdict,
                    "bear_thesis": bear_verdict,
                    "bull_status": bull_status,
                    "bear_status": bear_status,
                    "debate_status": debate_status,
                    "subordinate_analyst_reports": valid_reports,
                    "historical_context": historical_context,
                    "past_lessons_learned": symbol_lessons,
                    "indicators": market_data.get("indicators", {}),
                    "news_data": market_data.get("news_data", {}),
                    "price_data": market_data.get("price_data", {}),
                    "derivatives_data": market_data.get("derivatives_data", {}),
                    "market_data": market_data,
                    "current_price": current_price,
                    "is_pullback_retest": is_retest,
                    "pullback_retest_direction": retest_direction if is_retest else None,
                    "pullback_context": pb_context
                }
                ceo_verdict = await self.agents.ceo.analyze(ceo_payload)

                directional_conf = ceo_verdict.get("directional_confidence", ceo_verdict.get("conviction", 0))
                entry_qual = ceo_verdict.get("entry_quality", ceo_verdict.get("conviction", 0))

                # STAGE 4.5: DETERMINISTIC GUARD -> FINAL TRADE DECISION
                final_trade_decision = self.apply_pipeline_guards_and_sync(
                    strategy_profile, ceo_verdict, effective_asset_profile, symbol=symbol, market_data=market_data
                )
                decision = final_trade_decision.decision
                conviction = final_trade_decision.conviction
                trade_action = final_trade_decision.trade_action
                min_conv = final_trade_decision.min_conviction

                if decision == "HOLD":
                    conv_str = "N/A"
                else:
                    conv_str = f"{conviction}% (DirConf: {directional_conf}%, EntryQuality: {entry_qual}%, Action: {trade_action})"

                print(f"⚖️ Решение CEO [{symbol}]: {decision} (Уверенность: {conv_str})")
                if not hasattr(self, "_candle_debate_history") or self._candle_debate_history is None:
                    self._candle_debate_history = {}
                self._candle_debate_history[symbol] = (int(time.time() // 900), decision)

                # Enrich scan_result with routing info for cycle logs
                scan_result["strategy_mode"] = strategy_mode
                scan_result["direction_bias"] = strategy_profile.direction_bias
                scan_result["router_reasoning"] = strategy_profile.reasoning

                # 1-Per-Candle Breakout Cluster Cap Gate:
                # Disallow opening multiple market breakout positions in the same direction on the same 15m candle.
                # If a BREAKOUT in this direction was already opened in this candle bucket, downgrade second candidate
                # to WAIT_FOR_PULLBACK and route to PullbackWatchlist (0 LLM tokens, protects against whipsaw double loss).
                current_candle_bucket = int(time.time() // 900)
                if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and final_trade_decision.is_actionable and final_trade_decision.trade_action in ["ENTER", "REDUCE_SIZE"]:
                    target_dir = str(final_trade_decision.decision).upper()
                    has_breakout_this_candle = False

                    if hasattr(self, "_breakout_candle_entries") and isinstance(self._breakout_candle_entries, dict):
                        if target_dir in self._breakout_candle_entries.get(current_candle_bucket, set()):
                            has_breakout_this_candle = True

                    active_positions = getattr(self.services.trading_service, "active_positions", {})
                    if not has_breakout_this_candle and isinstance(active_positions, dict):
                        for pos_k, p in active_positions.items():
                            if isinstance(p, dict):
                                p_strat = str(p.get("strategy_mode", "")).upper()
                                p_dir = str(p.get("direction", p.get("side", ""))).upper()
                                p_open = float(p.get("open_time", 0.0) or p.get("timestamp", 0.0) or 0.0)
                                if p_strat in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and int(p_open // 900) == current_candle_bucket:
                                    if (target_dir == "LONG" and ("LONG" in p_dir or "BUY" in p_dir)) or \
                                       (target_dir == "SHORT" and ("SHORT" in p_dir or "SELL" in p_dir)):
                                        has_breakout_this_candle = True
                                        break

                    if has_breakout_this_candle:
                        cap_msg = (
                            f"1-Per-Candle Cluster Cap: в текущей 15m свече (bucket {current_candle_bucket}) уже открыт {target_dir} BREAKOUT. "
                            f"{symbol} перенаправлен в PullbackWatchlist без входа по рынку (защита от дублирования риска)."
                        )
                        print(f"⏸️ [{symbol}] {cap_msg}")
                        if self.services.logger:
                            self.services.logger.info(f"[System_Core] {cap_msg}")
                        import dataclasses
                        final_trade_decision = dataclasses.replace(
                            final_trade_decision,
                            trade_action="WAIT_FOR_PULLBACK",
                            is_actionable=False,
                            guard_status="BLOCKED",
                            guard_reason=cap_msg,
                            rejection_tag="BREAKOUT_CANDLE_CLUSTER_CAP"
                        )

                # Execution Gate: Checks if Deterministic Guard approved candidate as actionable
                if not final_trade_decision.is_actionable:
                    # Регистрация в Pullback Watchlist для непрерывного фонового отслеживания отката (0 LLM токенов)
                    if (
                        final_trade_decision.trade_action == "WAIT_FOR_PULLBACK"
                        and final_trade_decision.decision in ["LONG", "SHORT"]
                        and strategy_mode != "MEAN_REVERSION"
                        and not getattr(config, "ANTI_BOT_MODE", False)
                        and not is_retest
                    ):
                        self.register_pullback_candidate(
                            symbol=symbol,
                            decision=final_trade_decision.decision,
                            current_price=current_price,
                            directional_conf=directional_conf,
                            conviction=conviction,
                            strategy_mode=strategy_mode,
                            market_data=market_data,
                            ceo_verdict=ceo_verdict,
                            strategy_profile=strategy_profile,
                            reason=final_trade_decision.guard_reason
                        )

                    print(f"⏸️ Пропуск {symbol}. {final_trade_decision.guard_reason}")
                    if self.services.logger:
                        self.services.logger.info(
                            f"[DeterministicGuard] ⏸️ {symbol}: {final_trade_decision.guard_reason} "
                            f"({final_trade_decision.rejection_tag or 'GUARD_BLOCKED'})"
                        )
                    scanner_status = "⚠️ ЗАБЛОКИРОВАН СКАНЕРОМ" if scanner_blocked else "✅ OK"
                    status_text = "⏳ ОЖИДАНИЕ ОТКАТА" if final_trade_decision.trade_action == "WAIT_FOR_PULLBACK" else "⏸️ НЕТ СИГНАЛА"
                    asset_summary = {
                        "symbol": symbol,
                        "decision": decision,
                        "conviction": conviction,
                        "scanner_status": scanner_status,
                        "scanner_reason": scanner_reason if scanner_blocked else None,
                        "risk_approved": False,
                        "risk_reason": final_trade_decision.guard_reason,
                        "ceo_reasoning": ceo_verdict.get("reasoning_en", ceo_verdict.get("reasoning", "")),
                        "status": status_text
                    }
                    scan_summaries.append(asset_summary)
                    tracker.record_rejection(final_trade_decision.rejection_tag or "NO_SIGNAL")
                    self._record_candidate_snapshot(
                        candidate_id=candidate_id,
                        symbol=symbol,
                        cycle_number=cycle_number,
                        macro_regime=detected_regime,
                        profile=effective_asset_profile,
                        stage="DETERMINISTIC_GUARD",
                        final_action=final_trade_decision.trade_action,
                        rejection_tag=final_trade_decision.rejection_tag or "GUARD_BLOCKED",
                        rejection_reason=final_trade_decision.guard_reason,
                        strategy_profile=strategy_profile,
                        analyst_reports=valid_reports,
                        debate_info={
                            "bull_status": bull_status,
                            "bear_status": bear_status,
                            "debate_status": debate_status
                        },
                        ceo_verdict=ceo_verdict,
                        final_trade_decision=final_trade_decision,
                        market_data=market_data,
                        min_conviction=min_conv
                    )
                    return False

                # СТАДИЯ 5: РИСК-МЕНЕДЖМЕНТ И АТОМАРНОЕ ИСПОЛНЕНИЕ
                async with self._execution_lock:
                    # 1. Атомарная проверка лимитов позиций
                    cur_pos_dict = self.services.trading_service.active_positions
                    cur_pos_list = list(cur_pos_dict.values()) if isinstance(cur_pos_dict, dict) else (cur_pos_dict or [])
                    cur_risk_pos = [p for p in cur_pos_list if not is_risk_free_position(p)]
                    cur_be_pos = [p for p in cur_pos_list if is_risk_free_position(p)]
                    cur_max_risk = getattr(config, "MAX_CONCURRENT_POSITIONS", 3)
                    cur_max_total = getattr(config, "MAX_ACCOUNT_POSITIONS", 4) if len(cur_be_pos) > 0 else cur_max_risk

                    if len(cur_pos_list) >= cur_max_total or len(cur_risk_pos) >= cur_max_risk:
                        msg = f"⏸️ Достигнут лимит позиций ({len(cur_pos_list)}/{cur_max_total}, с риском: {len(cur_risk_pos)}/{cur_max_risk}). Вход {symbol} отменен."
                        print(msg)
                        self.services.logger.info(f"[System_Core] {msg}")
                        return False

                    # 2. Атомарная проверка направленного Cluster Cap
                    target_dir = str(final_trade_decision.decision).upper()
                    if target_dir in ["LONG", "SHORT"] and isinstance(self.services.trading_service.active_positions, dict):
                        same_dir_open = 0
                        same_dir_risk = 0
                        seen_bases = set()
                        for pos_k, p in self.services.trading_service.active_positions.items():
                            if isinstance(p, dict):
                                sym_raw = str(p.get("symbol") or pos_k or "").upper().replace('/', '-')
                                base_ticker = sym_raw.split('-')[0].strip() if sym_raw else ""
                                p_id = p.get("product_id")
                                dedup_key = f"{base_ticker}:{p_id}" if (base_ticker and p_id is not None) else (base_ticker or str(pos_k))
                                if dedup_key in seen_bases:
                                    continue
                                seen_bases.add(dedup_key)
                                p_dir = str(p.get("direction", p.get("side", ""))).upper()
                                if (target_dir == "LONG" and ("LONG" in p_dir or "BUY" in p_dir)) or                                    (target_dir == "SHORT" and ("SHORT" in p_dir or "SELL" in p_dir)):
                                    same_dir_open += 1
                                    if not is_risk_free_position(p):
                                        same_dir_risk += 1
                        max_dir_risk = 2
                        max_dir_total = 3 if (same_dir_open > same_dir_risk) else 2
                        if same_dir_open >= max_dir_total or same_dir_risk >= max_dir_risk:
                            msg = f"⏸️ Отмена входа {symbol}: лимит позиций {target_dir} ({same_dir_open}/{max_dir_total}) заполнен."
                            print(msg)
                            self.services.logger.info(f"[System_Core] {msg}")
                            return False

                    self.services.logger.info(f"[Stage 5] Risk Manager (Macro Profile: {profile}, Candidate Profile: {effective_asset_profile}, Regime: {detected_regime}) проверяет параметры сделки для {symbol}...")
                    portfolio_data["active_positions"] = self.services.trading_service.active_positions
                    portfolio_data["pending_reconciliations"] = getattr(self.services.trading_service, "pending_reconciliations", {})
                    portfolio_data["strict_portfolio_health"] = True
                    risk_verdict = await self.agents.risk.analyze(
                        final_trade_decision, 
                        portfolio_data, 
                        market_data, 
                        effective_profile=effective_asset_profile,
                        macro_regime=detected_regime,
                        strategy_mode=strategy_mode
                    )

                    trade_success = False
                    execution_result = None
                    execution_status = None
                    if risk_verdict.get("approved"):
                        # Execution Price Drift Guard & SL/TP Re-anchoring
                        live_price = await self.fetch_live_price(symbol, current_price)
                        is_drift_ok, re_entry, re_tp, re_sl, drift_msg = self.evaluate_price_drift(
                            symbol=symbol,
                            direction=final_trade_decision.decision,
                            snapshot_price=current_price,
                            live_price=live_price,
                            tp_price=risk_verdict.get("take_profit_price", 0),
                            sl_price=risk_verdict.get("stop_loss_price", 0)
                        )
                        print(drift_msg)
                        self.services.logger.info(f"[PriceDriftGuard] {drift_msg}")

                        if not is_drift_ok:
                            scan_summaries.append({
                                "symbol": symbol,
                                "decision": decision,
                                "conviction": conviction,
                                "scanner_status": "✅ OK",
                                "scanner_reason": None,
                                "risk_approved": False,
                                "risk_reason": drift_msg,
                                "ceo_reasoning": "Vetoed by Price Drift Guard",
                                "status": "🚫 LATE ENTRY CANCELLED"
                            })
                            tracker.record_rejection("PRICE_DRIFT_VETO")
                            self._record_candidate_snapshot(
                                candidate_id=candidate_id,
                                symbol=symbol,
                                cycle_number=cycle_number,
                                macro_regime=detected_regime,
                                profile=effective_asset_profile,
                                stage="PRICE_DRIFT_GATE",
                                final_action="REJECTED",
                                rejection_tag="PRICE_DRIFT_VETO",
                                rejection_reason=drift_msg,
                                strategy_profile=strategy_profile,
                                analyst_reports=valid_reports,
                                debate_info={
                                    "bull_status": bull_status,
                                    "bear_status": bear_status,
                                    "debate_status": debate_status
                                },
                                ceo_verdict=ceo_verdict,
                                final_trade_decision=final_trade_decision,
                                market_data=market_data,
                                min_conviction=min_conv
                            )
                            return False

                        # Apply re-anchored entry, TP, and SL
                        current_price = re_entry
                        risk_verdict["entry_price"] = re_entry
                        risk_verdict["take_profit_price"] = re_tp
                        risk_verdict["stop_loss_price"] = re_sl
                        self.services.logger.info(f"✅ Status: APPROVED BY RISK MANAGER")
                        print(f"💰 Position Amount: ${risk_verdict.get('notional_size_usd', 0):,.2f} ({risk_verdict.get('position_size_pct', 0)}% of portfolio)")
                        print(f"🟢 Take Profit (TP): ${risk_verdict.get('take_profit_price', 0):,.2f} (+{risk_verdict.get('take_profit_pct', 0)}%)")
                        # Автоматическая торговля 24/7 (полностью автономный режим)
                        atr_14_val = float(market_data.get("indicators", {}).get("atr_14", 0) or 0)
                        approved_risk_val = float(risk_verdict.get("approved_risk_usd") or risk_verdict.get("risk_amount_usd") or 0.0)
                        taker_fee_val = float(market_data.get("derivatives_data", {}).get("taker_fee_pct") or 0.0005)
                        try:
                            trade_success = await self.services.trading_service.open_position(
                                symbol=symbol,
                                direction=final_trade_decision.decision,
                                entry_price=current_price,
                                limit_price=risk_verdict.get("limit_price"),
                                notional_usd=risk_verdict.get("notional_size_usd", 0),
                                tp_price=risk_verdict.get("take_profit_price", 0),
                                sl_price=risk_verdict.get("stop_loss_price", 0),
                                leverage=risk_verdict.get("leverage", 10),
                                original_thesis=final_trade_decision.reasoning_en,
                                contracts=risk_verdict.get("contracts", 0.0),
                                atr_value=atr_14_val,
                                max_slippage_pct=risk_verdict.get("expected_slippage_pct"),
                                approved_risk_usd=approved_risk_val,
                                taker_fee_pct=taker_fee_val,
                                planned_risk_usd=float(risk_verdict.get("planned_risk_usd") or approved_risk_val),
                                base_risk_pct=float(risk_verdict.get("base_risk_pct", 0.0)),
                                final_risk_pct=float(risk_verdict.get("final_risk_pct", 0.0)),
                                cohort=risk_verdict.get("cohort", "UNKNOWN"),
                                applied_multipliers=risk_verdict.get("applied_multipliers", {}),
                                funding_rate_hourly=float(risk_verdict.get("funding_rate_hourly", 0.0)),
                                holding_hours=float(risk_verdict.get("holding_hours", 4.0)),
                                slippage_stress_pct=float(risk_verdict.get("slippage_stress_pct", 0.001)),
                                strategy_mode=strategy_mode,
                                macro_regime=detected_regime,
                                entry_quality=float(entry_qual or 0.0),
                                bb_pos_pct=float(market_data.get("indicators", {}).get("bb_position_pct", market_data.get("indicators", {}).get("bb_position", 50.0)) or 50.0),
                                macro_range_score=float(macro_cache.get("regime_verdict", {}).get("range_score", 0.0) or 0.0) if 'macro_cache' in locals() and isinstance(macro_cache, dict) else 0.0,
                                use_software_tpsl=bool(risk_verdict.get("use_software_tpsl") or risk_verdict.get("is_software_tpsl", False)),
                                software_sl_tp_only=bool(risk_verdict.get("software_sl_tp_only") or risk_verdict.get("is_software_tpsl", False))
                            )
                        except Exception as e:
                            self.services.logger.error(f"[Stage 5] Unexpected error placing order on exchange for {symbol}: {e}", exc_info=True)
                            print(f"❌ Критическая ошибка при отправке ордера {symbol}: {e}")
                            trade_success = False

                        if trade_success and strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]:
                            if not hasattr(self, "_breakout_candle_entries") or self._breakout_candle_entries is None:
                                self._breakout_candle_entries = {}
                            if current_candle_bucket not in self._breakout_candle_entries:
                                self._breakout_candle_entries[current_candle_bucket] = set()
                            self._breakout_candle_entries[current_candle_bucket].add(final_trade_decision.decision)

                        # Propagate rich execution status from trading service if available
                        service_exec_status = getattr(self.services.trading_service, "last_execution_status", None)
                        if isinstance(service_exec_status, str) and service_exec_status:
                            execution_status = service_exec_status

                        actual_fill_price = float(risk_verdict.get("entry_price", current_price) or current_price)
                        if trade_success:
                            self.remove_pullback_candidate(symbol, reason="позиция открыта в пайплайне")
                        if trade_success and hasattr(self.services.trading_service, "active_positions"):
                            active_pos = self.services.trading_service.active_positions.get(symbol)
                            if not active_pos:
                                base_s = symbol.split("-")[0].upper()
                                active_pos = self.services.trading_service.active_positions.get(base_s)
                            if active_pos:
                                active_pos["planned_risk_usd"] = float(risk_verdict.get("planned_risk_usd") or approved_risk_val)
                                active_pos["base_risk_pct"] = float(risk_verdict.get("base_risk_pct", 0.0))
                                active_pos["final_risk_pct"] = float(risk_verdict.get("final_risk_pct", 0.0))
                                active_pos["cohort"] = risk_verdict.get("cohort", "UNKNOWN")
                                active_pos["applied_multipliers"] = risk_verdict.get("applied_multipliers", {})
                                active_pos["strategy_mode"] = str(strategy_mode or "UNKNOWN").upper()
                                active_pos["is_anti_bot"] = bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION")
                                active_pos["macro_regime"] = str(detected_regime or "UNKNOWN").upper()
                                active_pos["entry_quality"] = float(entry_qual or 0.0)
                                active_pos["bb_pos_pct"] = float(market_data.get("indicators", {}).get("bb_position_pct", market_data.get("indicators", {}).get("bb_position", 50.0)) or 50.0)
                                active_pos["macro_range_score"] = float(macro_cache.get("regime_verdict", {}).get("range_score", 0.0) or 0.0) if 'macro_cache' in locals() and isinstance(macro_cache, dict) else 0.0
                                if active_pos.get("entry_price"):
                                    actual_fill_price = float(active_pos["entry_price"])
                                    real_notional = float(active_pos.get("notional_usd") or active_pos.get("size_usd") or 0.0)
                                    real_contracts = float(active_pos.get("contracts") or 0.0)
                                    if isinstance(risk_verdict, dict):
                                        risk_verdict["entry_price"] = actual_fill_price
                                        if real_notional > 0:
                                            risk_verdict["notional_size_usd"] = real_notional
                                        if real_contracts > 0:
                                            risk_verdict["contracts"] = real_contracts
                                    tp_p = float(risk_verdict.get("take_profit_price") or 0.0)
                                    sl_p = float(risk_verdict.get("stop_loss_price") or 0.0)
                                    approved_risk = float(risk_verdict.get("approved_risk_usd") or risk_verdict.get("risk_amount_usd") or 0.0)
                                    if actual_fill_price > 0:
                                        if tp_p > 0:
                                            risk_verdict["take_profit_pct"] = round(abs(tp_p - actual_fill_price) / actual_fill_price * 100, 2)
                                        if sl_p > 0:
                                            risk_verdict["stop_loss_pct"] = round(abs(sl_p - actual_fill_price) / actual_fill_price * 100, 2)
                                        if tp_p > 0 and sl_p > 0 and abs(sl_p - actual_fill_price) > 0:
                                            risk_verdict["risk_reward_ratio"] = round(abs(tp_p - actual_fill_price) / abs(sl_p - actual_fill_price), 2)
                                    
                                        # Post-Execution Actual Risk Verification via Unified Loss Calculator
                                        if sl_p > 0 and real_contracts > 0 and actual_fill_price > 0:
                                            from core.risk_calculator import estimate_total_loss, calculate_max_allowed_stress_loss
                                            loss_calc = None
                                            try:
                                                loss_calc = estimate_total_loss(
                                                    contracts=real_contracts,
                                                    fill_price=actual_fill_price,
                                                    sl_price=sl_p,
                                                    taker_fee_pct=taker_fee_val,
                                                    slippage_stress_pct=float(risk_verdict.get("slippage_stress_pct", 0.001)),
                                                    funding_rate_hourly=float(risk_verdict.get("funding_rate_hourly", 0.0)),
                                                    holding_hours=float(risk_verdict.get("holding_hours", 4.0)),
                                                    approved_risk=approved_risk
                                                )
                                                total_actual_loss_usd = loss_calc.total_expected_loss_usd
                                                total_actual_stress_usd = loss_calc.total_stress_loss_usd
                                                max_tolerated_risk = loss_calc.max_tolerated_risk_usd
                                                max_allowed_stress = calculate_max_allowed_stress_loss(approved_risk)
                                            
                                                risk_verdict["approved_risk_usd"] = approved_risk
                                                risk_verdict["actual_risk_usd"] = loss_calc.price_loss_usd
                                                risk_verdict["total_actual_loss_usd"] = total_actual_loss_usd
                                                risk_verdict["total_stress_loss_usd"] = total_actual_stress_usd
                                            except Exception as val_err:
                                                self.services.logger.error(
                                                    f"[Stage 5] 🚨 Fatal risk calculator error during post-fill check: {val_err}. "
                                                    f"Risk budget cannot be established for open position {symbol}. Emergency protecting position."
                                                )
                                                close_success, pnl = await self.services.trading_service.emergency_protect_risk_breach(
                                                    symbol=symbol,
                                                    approved_risk=approved_risk,
                                                    actual_risk=approved_risk
                                                )
                                                trade_success = False
                                                execution_status = "ABORTED_CALC_ERROR" if close_success else "RISK_BREACH_UNRESOLVED"
                                                loss_calc = None
                                        
                                            if loss_calc is not None:
                                                expected_breached = (approved_risk > 0 and total_actual_loss_usd > max_tolerated_risk)
                                                stress_breached = (approved_risk > 0 and total_actual_stress_usd > max_allowed_stress)

                                                if expected_breached or stress_breached:
                                                    breach_type = "EXPECTED_LOSS" if expected_breached else "STRESS_LOSS"
                                                    risk_diff_pct = ((total_actual_loss_usd / approved_risk) - 1.0) * 100 if approved_risk > 0 else 0.0
                                                    fee_disp = getattr(loss_calc, 'fee_loss_usd', 0.0)
                                                    self.services.logger.error(
                                                        f"[Stage 5] 🚨 CRITICAL EXECUTION RISK BREACH ({breach_type}): "
                                                        f"Loss (expected=${total_actual_loss_usd:.2f}, stress=${total_actual_stress_usd:.2f}) "
                                                        f"exceeds approved risk limits (max_exp=${max_tolerated_risk:.2f}, max_stress=${max_allowed_stress:.2f}, "
                                                        f"approved=${approved_risk:.2f}, fee=${fee_disp:.2f}). "
                                                        f"Triggering emergency position protection."
                                                    )
                                                    # Invoke emergency protection
                                                    close_success, pnl = await self.services.trading_service.emergency_protect_risk_breach(
                                                        symbol=symbol,
                                                        approved_risk=approved_risk,
                                                        actual_risk=total_actual_loss_usd
                                                    )
                                                    if close_success:
                                                        self.services.logger.warning(
                                                            f"[Stage 5] 🛡️ Emergency close confirmed for {symbol}. PnL: ${pnl:.2f}. Trade aborted."
                                                        )
                                                        trade_success = False
                                                        execution_status = "ABORTED_RISK_BREACH"
                                                    else:
                                                        self.services.logger.critical(
                                                            f"[Stage 5] ❌ EMERGENCY CLOSE UNCONFIRMED for {symbol}! "
                                                            f"Position marked as RISK_BREACH_UNRESOLVED in state store."
                                                        )
                                                        trade_success = False
                                                        execution_status = "RISK_BREACH_UNRESOLVED"
                                                elif approved_risk > 0 and total_actual_loss_usd > (approved_risk * 1.05):
                                                    risk_diff_pct = ((total_actual_loss_usd / approved_risk) - 1.0) * 100
                                                    self.services.logger.warning(
                                                        f"[Stage 5] ⚠️ EXECUTION RISK WARNING: Total potential loss (${total_actual_loss_usd:.2f}) "
                                                        f"is slightly elevated (+{risk_diff_pct:.1f}%) within acceptable tolerance."
                                                    )
                                                else:
                                                    self.services.logger.info(
                                                        f"[Stage 5] 🛡️ Post-Execution Risk Verified: Total loss ${total_actual_loss_usd:.2f} "
                                                        f"within approved budget ${approved_risk:.2f}."
                                                    )

                        if not execution_status:
                            execution_status = "SUCCESS" if trade_success else "REJECTED_BY_EXCHANGE"
                        elif execution_status == "SUCCESS" and not trade_success:
                            execution_status = "REJECTED_BY_EXCHANGE"

                        execution_result = ExecutionResult(
                            symbol=symbol,
                            status=execution_status,
                            executed_at=time.time(),
                            notional_usd=float(risk_verdict.get("notional_size_usd", 0) or 0),
                            actual_fill_price=actual_fill_price
                        )

                        if isinstance(risk_verdict, dict):
                            risk_verdict["execution_status"] = execution_status
                        elif hasattr(risk_verdict, "execution_status"):
                            import dataclasses
                            risk_verdict = dataclasses.replace(risk_verdict, execution_status=execution_status)

                        if not trade_success:
                            print(f"❌ Ошибка открытия/защиты позиции на бирже для {symbol} (Статус: {execution_status}).")
                            self.services.logger.error(f"❌ Status: {execution_status} ({symbol})")
                            if execution_status == "ABORTED_RISK_BREACH":
                                tracker.record_rejection("RISK_BREACH_ABORT")
                            else:
                                tracker.record_execution_failed()
                        else:
                            tracker.record_trade()

                        self._record_candidate_snapshot(
                            candidate_id=candidate_id,
                            symbol=symbol,
                            cycle_number=cycle_number,
                            macro_regime=detected_regime,
                            profile=effective_asset_profile,
                            stage="EXECUTION",
                            final_action="EXECUTED" if trade_success else "EXECUTION_FAILED",
                            rejection_tag=None if trade_success else (execution_status or "EXECUTION_FAILED"),
                            rejection_reason=None if trade_success else f"Order execution failed: {execution_status}",
                            strategy_profile=strategy_profile,
                            analyst_reports=valid_reports,
                            debate_info={
                                "bull_status": bull_status,
                                "bear_status": bear_status,
                                "debate_status": debate_status
                            },
                            ceo_verdict=ceo_verdict,
                            final_trade_decision=final_trade_decision,
                            risk_verdict=risk_verdict,
                            execution_status=execution_status,
                            trade_success=trade_success,
                            actual_fill_price=actual_fill_price,
                            market_data=market_data,
                            min_conviction=min_conv
                        )
                    else:
                        self.services.logger.error(f"❌ Status: VETOED BY RISK MANAGER ({risk_verdict.get('reasoning')})")
                        veto_cat = risk_verdict.get("veto_category") or "RISK_VETO"
                        tracker.record_rejection(veto_cat)

                        # Register in Pullback Watchlist if Risk Manager routed to WAIT_FOR_PULLBACK
                        # (e.g. DISTANCE_TO_STRUCTURE_VETO or POOR_RISK_REWARD)
                        if (
                            risk_verdict.get("trade_action") == "WAIT_FOR_PULLBACK"
                            and final_trade_decision.decision in ["LONG", "SHORT"]
                            and strategy_mode != "MEAN_REVERSION"
                            and not getattr(config, "ANTI_BOT_MODE", False)
                            and not is_retest
                        ):
                            self.register_pullback_candidate(
                                symbol=symbol,
                                decision=final_trade_decision.decision,
                                current_price=current_price,
                                directional_conf=directional_conf,
                                conviction=conviction,
                                strategy_mode=strategy_mode,
                                market_data=market_data,
                                ceo_verdict=ceo_verdict,
                                strategy_profile=strategy_profile,
                                reason=risk_verdict.get("reasoning", "")
                            )

                        execution_result = ExecutionResult(
                            symbol=symbol,
                            status="SKIPPED",
                            executed_at=time.time(),
                            error=f"Vetoed by Risk Manager: {veto_cat}"
                        )
                        self._record_candidate_snapshot(
                            candidate_id=candidate_id,
                            symbol=symbol,
                            cycle_number=cycle_number,
                            macro_regime=detected_regime,
                            profile=effective_asset_profile,
                            stage="RISK_MANAGER",
                            final_action="REJECTED",
                            rejection_tag=veto_cat,
                            rejection_reason=risk_verdict.get("reasoning", ""),
                            strategy_profile=strategy_profile,
                            analyst_reports=valid_reports,
                            debate_info={
                                "bull_status": bull_status,
                                "bear_status": bear_status,
                                "debate_status": debate_status
                            },
                            ceo_verdict=ceo_verdict,
                            final_trade_decision=final_trade_decision,
                            risk_verdict=risk_verdict,
                            market_data=market_data,
                            min_conviction=min_conv
                        )

                # СТАДИЯ 6: ТЕЛЕГРАМ
                # Собираем сводку по активу для отчёта ручного /scan
                scanner_status = "✅ OK"
                if risk_verdict.get("approved"):
                    if trade_success:
                        summary_status = "🚀 СИГНАЛ"
                        summary_risk_reason = risk_verdict.get("reasoning", "")
                    else:
                        summary_status = "❌ ОШИБКА БИРЖИ"
                        summary_risk_reason = f"Ордер отклонён биржей ({execution_status})"
                else:
                    summary_status = "⏳ ОЖИДАНИЕ ОТКАТА" if risk_verdict.get("trade_action") == "WAIT_FOR_PULLBACK" else "⏸️ VETO"
                    summary_risk_reason = risk_verdict.get("reasoning", "")

                asset_summary = {
                    "symbol": symbol,
                    "decision": decision,
                    "conviction": conviction,
                    "scanner_status": scanner_status,
                    "scanner_reason": None,
                    "risk_approved": risk_verdict.get("approved", False),
                    "risk_reason": summary_risk_reason,
                    "ceo_reasoning": f"{ceo_verdict.get('reasoning_en', '')}\n\n{ceo_verdict.get('reasoning_ru', '')}".strip(),
                    "status": summary_status
                }
                scan_summaries.append(asset_summary)

                if risk_verdict.get("approved"):
                    if trade_success:
                        print(f"🚀 СДЕЛКА ИСПОЛНЕНА! Генерация Telegram-уведомления [{symbol}] ({decision}, Уверенность {conviction}%)...")

                        is_ab = bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION")
                        final_trade_data = {
                            "symbol": symbol,
                            "strategy_mode": str(strategy_mode or "UNKNOWN").upper(),
                            "is_anti_bot": is_ab,
                            "ceo_verdict": final_trade_decision.to_dict(),
                            "risk_verdict": risk_verdict,
                            "execution_result": execution_result.to_dict() if hasattr(execution_result, "to_dict") else {
                                "status": execution_result.status,
                                "actual_fill_price": execution_result.actual_fill_price,
                                "notional_usd": execution_result.notional_usd,
                                "executed_at": execution_result.executed_at
                            }
                        }
                        tg_response = await self.agents.telegram.analyze(final_trade_data)
                        tg_message = tg_response.get("message", f"Отчет по {symbol}")

                        print(f"\n--- ОТПРАВЛЕНО В TELEGRAM [{symbol}] ---")
                        print(tg_message)
                        print("-----------------------------")

                        pending_id = risk_verdict.get("pending_trade_id")
                        reply_markup = None
                        if pending_id:
                            reply_markup = {
                                "inline_keyboard": [[
                                    {"text": "✅ Одобрить", "callback_data": f"approve_{pending_id}"},
                                    {"text": "❌ Отклонить", "callback_data": f"reject_{pending_id}"}
                                ]]
                            }
                            if hasattr(self.services.trading_service, "pending_trades") and pending_id in self.services.trading_service.pending_trades:
                                self.services.trading_service.pending_trades[pending_id]["tg_message"] = tg_message

                        await self.services.tg_sender.send_message(tg_message, reply_markup=reply_markup)

                        # Транслируем в канал только РЕАЛЬНО исполненные сделки
                        if not pending_id:
                            await self.services.tg_sender.broadcast_to_channel(tg_message)
                    else:
                        # Предупреждение оператору об ошибке исполнения на бирже (НЕ транслируем фантом в канал)
                        if execution_status == "RISK_BREACH_UNRESOLVED":
                            fail_msg = (
                                f"🚨 *КРИТИЧЕСКИЙ СБОЙ / RISK BREACH UNRESOLVED*\n\n"
                                f"🪙 *Asset:* `{symbol}`\n"
                                f"📊 *Decision:* `{decision}` (Conviction: {conviction}%)\n"
                                f"❌ *Status:* Позиция была исполнена с ПРЕВЫШЕНИЕМ РИСКА, но закрытие НЕ подтверждено!\n"
                                f"⚠️ *ВНИМАНИЕ:* Позиция удерживается в active_positions для экстренного вмешательства Sentinel / оператора. Все новые сделки заблокированы!"
                            )
                        elif execution_status == "ABORTED_RISK_BREACH":
                            fail_msg = (
                                f"🛡️ *ПРЕВЫШЕНИЕ РИСКА / ABORTED RISK BREACH*\n\n"
                                f"🪙 *Asset:* `{symbol}`\n"
                                f"📊 *Decision:* `{decision}` (Conviction: {conviction}%)\n"
                                f"⚠️ *Status:* Фактический риск превысил лимит. Позиция успешно АВАРИЙНО ЗАКРЫТА на бирже.\n"
                                f"🛡 *Capital Protected:* Средства защищены от неконтролируемого убытка."
                            )
                        elif execution_status == "ABORTED_PROTECTION_FAILURE":
                            fail_msg = (
                                f"🛡️ *ОТКАЗ ЗАЩИТЫ / ABORTED PROTECTION FAILURE*\n\n"
                                f"🪙 *Asset:* `{symbol}`\n"
                                f"📊 *Decision:* `{decision}` (Conviction: {conviction}%)\n"
                                f"⚠️ *Status:* Ордер исполнился, но стоп-лосс НЕ подтверждён (нативный отклонён биржей, программный мониторинг не подтверждён)!\n"
                                f"🛡 *Capital Protected:* Позиция АВАРИЙНО ЗАКРЫТА на бирже. Торговля без подтверждённого стоп-лосса запрещена!"
                            )
                        elif execution_status == "PENDING_ORDER_RECONCILIATION":
                            fail_msg = (
                                f"⏳ *НЕОПРЕДЕЛЕННОСТЬ ОРДЕРА / ORDER PENDING*\n\n"
                                f"🪙 *Asset:* `{symbol}`\n"
                                f"⚠️ *Status:* Ордер не исполнился вовремя, отмена не подтверждена. Помещен в очередь сверки."
                            )
                        else:
                            fail_msg = (
                                f"⚠️ *ОШИБКА ИСПОЛНЕНИЯ / EXCHANGE ERROR*\n\n"
                                f"🪙 *Asset:* `{symbol}`\n"
                                f"📊 *Decision:* `{decision}` (Conviction: {conviction}%)\n"
                                f"❌ *Status:* Ордер отклонён биржей Nado (`{execution_status}`). Позиция НЕ открыта!\n"
                                f"🛡 *Risk Check:* Одобрен риск-менеджером, но транзакция на бирже отклонена."
                            )
                        print(f"⚠️ [Telegram Alert] Отправка уведомления для {symbol} ({execution_status})...")
                        await self.services.tg_sender.send_message(fail_msg)
                elif risk_verdict.get("pending_trade_id"):
                    pass
                else:
                    print(f"ℹ️ [Telegram] Пропуск отправки для {symbol}. Решение '{decision}' с уверенностью {conviction}% (Фильтр: LONG/SHORT и Уверенность ≥ {min_conv}%).")

                # СТАДИЯ 7: СОХРАНЕНИЕ В ПАМЯТЬ
                cycle_record = {
                    "symbol": symbol,
                    "market_conditions": scan_result,
                    "analysts": valid_reports,
                    "ceo_decision": final_trade_decision.to_dict(),
                    "risk_assessment": risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else risk_verdict,
                    "execution_result": execution_result.to_dict() if execution_result else None,
                    "status": "APPROVED" if (risk_verdict.get("approved") and trade_success) else "VETOED"
                }
                self.agents.memory.save_cycle(cycle_record)
                self.services.logger.info(f"[System_Core] Торговый цикл успешно завершен для {symbol}.")

                await asyncio.sleep(1.5)
                return trade_success

            except Exception as e:
                self.services.logger.error(f"[Pipeline] Ошибка обработки символа {symbol}: {type(e).__name__}: {e}", exc_info=True)
                print(f"❌ [Pipeline] Критическая ошибка при обработке {symbol}: {e}. Изолируем и продолжаем...")
                scan_summaries.append({
                    "symbol": symbol,
                    "status": "❌ ОШИБКА ЦИКЛА",
                    "reason": f"{type(e).__name__}: {str(e)[:150]}"
                })
                tracker.record_rejection("SYMBOL_PROCESSING_ERROR")
                return False

        # Если сканирование было запрошено вручную через /scan — всегда присылаем подробный отчёт (сводку)

    _evaluate_candidate = evaluate_candidate

    async def run_cycle(self, cycle_number: int, force_scan: bool = False, skip_new_trades: bool = False, targeted_symbol: Optional[str] = None):
        # Fix #5: Prevent overlapping cycles
        if self._cycle_running and not force_scan:
            print("⏸️ [Pipeline] Предыдущий цикл ещё выполняется. Пропуск.")
            return
        self._cycle_running = True
        try:
            await self._run_cycle_inner(cycle_number, force_scan, skip_new_trades, targeted_symbol=targeted_symbol)
        finally:
            self._cycle_running = False

    async def _run_cycle_inner(self, cycle_number: int, force_scan: bool = False, skip_new_trades: bool = False, targeted_symbol: Optional[str] = None):
        LLMClient.reset_cycle_tokens()
        is_rest, time_str = get_msk_status()
        profile = config.TRADING_PROFILE

        print(f"\n" + "="*65)
        print(f"🚀 ТОРГОВЫЙ ЦИКЛ №{cycle_number} ({self.exchange_name}) [{time_str}]")
        print(f"⚙️ ПРОФИЛЬ РИСКА: {profile} | 📐 MULTI-TIMEFRAME (15m + 1H + 4H) АКТИВЕН")
        print("="*65)

        if force_scan:
            print(f"⚡ [ForceScan] Ручной запуск /scan ({time_str}).")

        # 1. Синхронизация и проверка TP/SL открытых позиций ПЕРЕД проверкой кулдауна
        await self.run_sentinel_checks()

        # 2. Проверка Cooldown после серии убытков
        cooldown_until = getattr(self.services.trading_service, "cooldown_until", 0.0)
        now_ts = time.time()
        if now_ts < cooldown_until:
            remain_min = max(1, int((cooldown_until - now_ts) / 60))
            print(f"🛑 [Cooldown] Бот на паузе после 3 убытков подряд. Осталось {remain_min} мин.")
            self.services.logger.info(f"[System_Core] Cooldown active. {remain_min} min remaining.")
            if not force_scan:
                return

        active_pos_dict = self.services.trading_service.active_positions
        active_pos_list = list(active_pos_dict.values()) if isinstance(active_pos_dict, dict) else (active_pos_dict or [])
        risk_positions = [p for p in active_pos_list if not is_risk_free_position(p)]
        be_positions = [p for p in active_pos_list if is_risk_free_position(p)]
        max_risk_cap = getattr(config, "MAX_CONCURRENT_POSITIONS", 3)
        max_total_cap = getattr(config, "MAX_ACCOUNT_POSITIONS", 4) if len(be_positions) > 0 else max_risk_cap

        if (len(active_pos_list) >= max_total_cap or len(risk_positions) >= max_risk_cap) and not force_scan:
            active_symbols = ", ".join(active_pos_dict.keys())
            be_note = f" (в т.ч. в БУ: {len(be_positions)})" if be_positions else ""
            msg = (
                f"⏸️ [Positions Cap] Достигнут лимит позиций ({len(active_pos_list)}/{max_total_cap}{be_note}, активный риск: {len(risk_positions)}/{max_risk_cap}): [{active_symbols}].\n"
                f"Новые сделки заблокированы. Сканирование рынка пропущено (экономия токенов 100%)."
            )
            print(f"\n{msg}\n")
            self.services.logger.info(f"[System_Core] {msg}")
            return

        # СТАДИЯ 0: REGIME DETECTION (Адаптивный профиль риска)
        self.services.logger.info("[Stage 0] Regime Agent определяет фазу рынка (BTC/ETH)...")
        macro_cache = {}
        self._macro_leaders_alignment = {}
        detected_regime = "UNKNOWN"
        detected_profile = "CONSERVATIVE"
        try:
            btc_data = await self.services.fetcher.fetch_all_market_data("BTC-USD")
            eth_data = await self.services.fetcher.fetch_all_market_data("ETH-USD")
            macro_cache["BTC-USD"] = btc_data
            macro_cache["ETH-USD"] = eth_data

            def _extract_leader_bias(data: Dict[str, Any]) -> str:
                if not isinstance(data, dict):
                    return "MIXED"
                mtf = data.get("multi_timeframe", {})
                align = str(mtf.get("mtf_alignment", "")).upper()
                t1h = str(mtf.get("trend_1h", "")).upper()
                t4h = str(mtf.get("trend_4h", "")).upper()
                if "FULL_ALIGNMENT" in align:
                    if t1h == "BEARISH" or t4h == "BEARISH":
                        return "BEARISH"
                    elif t1h == "BULLISH" or t4h == "BULLISH":
                        return "BULLISH"
                if t1h == "BEARISH" and t4h == "BEARISH":
                    return "BEARISH"
                elif t1h == "BULLISH" and t4h == "BULLISH":
                    return "BULLISH"
                return "MIXED"

            self._macro_leaders_alignment["BTC"] = _extract_leader_bias(btc_data)
            self._macro_leaders_alignment["ETH"] = _extract_leader_bias(eth_data)
            
            regime_payload = {
                "btc_data": btc_data,
                "eth_data": eth_data
            }
            regime_verdict = await self.agents.regime.analyze(regime_payload)
            macro_cache["regime_verdict"] = regime_verdict
            
            detected_regime = regime_verdict.get("regime", "UNKNOWN")
            detected_profile = regime_verdict.get("recommended_profile", "CONSERVATIVE")
            regime_reasoning = regime_verdict.get("reasoning_en", "")
            
            # Динамически меняем профиль на этот цикл (но с ограничением сверху)
            original_profile = config.TRADING_PROFILE
            profile_ranks = {"CONSERVATIVE": 1, "BALANCED": 2, "AGGRESSIVE": 3}
            orig_rank = profile_ranks.get(original_profile, 2)
            detected_rank = profile_ranks.get(detected_profile, 1 if detected_regime == "UNKNOWN" else 2)
            
            # Защита: RegimeAgent может только ПОНИЖАТЬ риск во время шторма или при неизвестном режиме
            if detected_regime == "UNKNOWN":
                effective_profile = "CONSERVATIVE"
            elif detected_rank > orig_rank:
                effective_profile = original_profile
                self.services.logger.info(f"[Macro Regime] LLM proposed {detected_profile}, but bounded to {original_profile}.")
            else:
                effective_profile = detected_profile
                
            profile = effective_profile
            self._effective_profile = profile
            
            print(f"🌍 [Macro Regime] Рынок находится в фазе: {detected_regime}")
            print(f"🛡️ [Risk Profile] Профиль риска на этот цикл: {profile}")
            self.services.logger.info(f"[Macro Regime] {detected_regime} -> {profile}. Reason: {regime_reasoning}")

            # --- AUTO_PREDATOR_SWITCH AUTOPILOT (v2.1 Patch: Priority to TREND_FOLLOWING) ---
            if getattr(config, "AUTO_PREDATOR_SWITCH", False) is True:
                trend_score = float(regime_verdict.get("trend_score", 0.0) or 0.0)
                range_score = float(regime_verdict.get("range_score", 0.0) or 0.0)
                vol_score = float(regime_verdict.get("vol_score", 0.0) or 0.0)
                prev_mode = bool(getattr(config, "ANTI_BOT_MODE", False))

                # Включать Хищника ТОЛЬКО в настоящем боковике:
                # 1) Чистый RANGE_CHOPPY, ИЛИ
                # 2) TRANSITION с преобладанием боковика (range_score >= 52.0 и trend_score < 48.0)
                if detected_regime == "RANGE_CHOPPY" or (detected_regime == "TRANSITION" and range_score >= 52.0 and trend_score < 48.0):
                    new_mode = True
                elif detected_regime == "TRENDING" or trend_score >= 50.0:
                    new_mode = False
                else:
                    new_mode = False

                if new_mode != prev_mode:
                    config.ANTI_BOT_MODE = new_mode
                    os.environ["ANTI_BOT_MODE"] = str(new_mode).lower()
                    log_msg = (
                        f"[AutoPredator] 🔄 Режим автоматически переключен -> "
                        f"ANTI_BOT_MODE={new_mode} (Regime={detected_regime}, Trend={trend_score:.1f}, Range={range_score:.1f}, Vol={vol_score:.1f})"
                    )
                    self.services.logger.info(log_msg)
                    print(f"🦈 {log_msg}")
        except Exception as e:
            detected_regime = "UNKNOWN"
            profile = "CONSERVATIVE"
            self._effective_profile = "CONSERVATIVE"
            print(f"⚠️ [Macro Regime] Ошибка при определении режима: {e}. Применён защитный профиль CONSERVATIVE (режим UNKNOWN).")
            self.services.logger.error(f"[Macro Regime] Failed to detect regime: {e}. Failsafe CONSERVATIVE engaged.")

        # СТАДИЯ 1: UNIVERSE (Выбор активов)
        target_list = []
        if targeted_symbol:
            if isinstance(targeted_symbol, (list, tuple, set)):
                target_list = [s for s in targeted_symbol if s]
            elif isinstance(targeted_symbol, str):
                target_list = [s.strip() for s in targeted_symbol.split(",") if s.strip()]

        radar_targets = getattr(self, "_radar_target_symbols", None)
        if radar_targets:
            for rt in radar_targets:
                if rt and rt not in target_list:
                    target_list.append(rt)
            self._radar_target_symbols = set()

        if target_list:
            target_str = ", ".join(target_list)
            print(f"🎯 [Targeted Scan] Внеочередной таргетный анализ по сигналу радара: {target_str}")
            self.services.logger.info(f"[Stage 1] Targeted scan strictly on {target_str}")
            selected_assets = list(dict.fromkeys(target_list))
            active_perps = getattr(self, "_universe_active_perps", []) or []
        else:
            now_ts = time.time()
            if not getattr(self, "_universe_cache", None) or (now_ts - getattr(self, "_universe_cache_time", 0) > 3600):
                self.services.logger.info("[Stage 1] Universe Agent сканирует DEX на наличие ликвидных активов...")
                active_perps = []
                try:
                    active_perps = await self.services.fetcher.fetch_active_perps(limit=15)
                    broad_market_data = {
                        "trending_perps": active_perps, 
                        "volume_24h": "high"
                    } 
                    universe_report = await self.agents.universe.analyze(broad_market_data)
                    selected_assets = universe_report.get("selected_pairs", [])
                    self._universe_cache = selected_assets
                    self._universe_cache_time = now_ts
                    self._universe_active_perps = active_perps
                except Exception as e:
                    self.services.logger.error(f"[Stage 1] Ошибка выбора Universe: {e}. Используем резервный список.")
                    selected_assets = getattr(self, "_universe_cache", []) or []
                    active_perps = getattr(self, "_universe_active_perps", []) or []
            else:
                selected_assets = list(self._universe_cache)
                active_perps = getattr(self, "_universe_active_perps", []) or []
                self.services.logger.info(f"[Stage 1] Universe cached (age {(now_ts - self._universe_cache_time)/60:.1f}m/60m): {', '.join(selected_assets)}")

        # Canonical normalization & mapping against active perps
        valid_symbols_map = {}
        if active_perps:
            for p in active_perps:
                sym = p.get("symbol", "")
                if sym:
                    valid_symbols_map[sym.upper()] = sym
                    base = sym.replace('/', '-').split('-')[0].upper()
                    valid_symbols_map[base] = sym
                    valid_symbols_map[f"{base}-USD"] = sym

        normalized_selected = []
        for s in selected_assets:
            if not isinstance(s, str):
                continue
            clean = s.strip().upper()
            base = clean.replace('/', '-').split('-')[0]
            if clean in valid_symbols_map:
                normalized_selected.append(valid_symbols_map[clean])
            elif f"{base}-USD" in valid_symbols_map:
                normalized_selected.append(valid_symbols_map[f"{base}-USD"])
            elif base in valid_symbols_map:
                normalized_selected.append(valid_symbols_map[base])
            elif not active_perps:
                # If active_perps unavailable, ensure standard BASE-USD
                normalized_selected.append(f"{base}-USD")

        selected_assets = list(dict.fromkeys(normalized_selected))

        # Fallback if LLM failed, returned empty, or all selected symbols were invalid
        if not selected_assets:
            if active_perps:
                selected_assets = [p["symbol"] for p in active_perps[:6]]
            else:
                selected_assets = ["BTC-USD", "ETH-USD", "SOL-USD"]
            selected_assets = list(dict.fromkeys(selected_assets))

        self.services.logger.info(f"🎯 Отобраны уникальные валидные активы для сканирования: {', '.join(selected_assets)}")
        portfolio_data = await self.services.trading_service.get_portfolio_summary()

        recent_lessons = self.agents.reflector.get_lessons(limit=5)
        if recent_lessons:
            print(f"🧠 [Memory] Загружено {len(recent_lessons)} уроков из прошлых сделок.")

        any_signal_sent = False
        scan_summaries = []  # Сводка по каждому активу для отчёта ручного /scan

        # QW Quiet Rest disabled: Bot runs 24/7 (48 cycles per day at 30 min intervals)

        # Фильтруем активы: не сканируем то, что уже открыто
        active_bases = {k.replace('/', '-').split('-')[0].upper() for k in self.services.trading_service.active_positions}
        selected_assets = [
            s for s in selected_assets 
            if s not in self.services.trading_service.active_positions 
            and s.replace('/', '-').split('-')[0].upper() not in active_bases
        ]

        # Приоритет для актива с подтвержденным откатом (PULLBACK_RETEST)
        retest_symbol = None
        retest_direction = None
        if getattr(self, "_priority_symbol", None):
            pri_sym = self._priority_symbol
            retest_symbol = pri_sym
            retest_direction = getattr(self, "_priority_direction", None)
            self._priority_symbol = None
            self._priority_direction = None
            pri_base = pri_sym.replace('/', '-').split('-')[0].upper()
            if pri_sym in selected_assets:
                selected_assets = [pri_sym] + [s for s in selected_assets if s != pri_sym]
                print(f"🎯 [Pullback Priority] {pri_sym} ({retest_direction or 'RETEST'}) перемещен в начало очереди анализа (PULLBACK_RETEST).")
            elif pri_sym not in self.services.trading_service.active_positions and pri_base not in active_bases:
                selected_assets = [pri_sym] + selected_assets
                print(f"🎯 [Pullback Priority] {pri_sym} ({retest_direction or 'RETEST'}) добавлен первым в очередь анализа (PULLBACK_RETEST).")

        # 🎯 Targeted Pullback Scan:
        # Если пробуждение произошло ради конкретного отката (retest_symbol), и с последнего
        # планового полного обхода вселенной прошло менее 25 минут, изолируем сканирование
        # исключительно этим тикером, экономя 35 000 - 50 000 токенов на остальных монетах.
        now_ts = time.time()
        if retest_symbol and (now_ts - getattr(self, "_last_full_scan_ts", 0.0) < 1500):
            # Point 4 Fix: Retain any other radar-triggered movers (e.g. fast momentum breakout) alongside retest_symbol
            retest_targets = [retest_symbol]
            extra_targets = [s for s in target_list if s != retest_symbol]
            retest_targets.extend(extra_targets)
            selected_assets = list(dict.fromkeys(retest_targets))
            self.services.logger.info(
                f"[System_Core] 🎯 Targeted Scan Mode: проверка {', '.join(selected_assets)} "
                f"(прошло менее 25 мин с полного сканирования, экономия токенов на остальных активах)."
            )
        else:
            self._last_full_scan_ts = now_ts

        self._last_universe_symbols = list(selected_assets)

        # ИТЕРАЦИЯ ПО ВСЕМ АКТИВАМ
        # ИТЕРАЦИЯ ПО ВСЕМ АКТИВАМ (Параллельный анализ кандидатов с Semaphore(3))
        candidate_sem = asyncio.Semaphore(3)
        candidate_tasks = [
            self.evaluate_candidate(
                symbol=sym,
                cycle_number=cycle_number,
                detected_regime=detected_regime,
                profile=profile,
                macro_cache=macro_cache,
                portfolio_data=portfolio_data,
                tracker=tracker,
                scan_summaries=scan_summaries,
                retest_symbol=retest_symbol,
                retest_direction=retest_direction,
                targeted_symbol=targeted_symbol,
                candidate_sem=candidate_sem
            )
            for sym in selected_assets
        ]
        results = await asyncio.gather(*candidate_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, bool) and r:
                any_signal_sent = True
            elif isinstance(r, Exception):
                self.services.logger.error(f"[Pipeline] Ошибка при параллельном анализе кандидата: {r}", exc_info=True)

        if force_scan:
            assets_list = ", ".join(selected_assets)
            parts = [f"📋 SCAN RESULTS / РЕЗУЛЬТАТЫ СКАНИРОВАНИЯ\n\n🔍 Analyzed / Проанализированы: {assets_list}\n"]

            for item in scan_summaries:
                sym = item.get("symbol", "?")
                status = item.get("status", "?")

                # Если скан заблокирован на самом первом этапе (ScannerAgent)
                if status == "⛔ СКАН ЗАБЛОКИРОВАН":
                    reason = item.get("reason", "")
                    safe_reason = _escape_md(str(reason)[:200])
                    block = f"{'='*28}\n🪙 {sym} — {status}\n"
                    block += f"🔎 Scanner/Сканер: ОТКЛОНЁН\n   > {safe_reason}\n"
                    parts.append(block)
                    continue

                dec = item.get("decision", "N/A")
                conv = item.get("conviction", 0)
                scanner_st = item.get("scanner_status", "")
                scanner_rsn = item.get("scanner_reason")
                risk_ok = item.get("risk_approved", False)
                risk_rsn = item.get("risk_reason", "")
                ceo_rsn = item.get("ceo_reasoning", "")

                block = f"{'='*28}\n🪙 {sym} — {status}\n"
                block += f"📊 CEO: {dec} | Conviction/Уверенность: {conv}%\n"
                block += f"🔎 Scanner/Сканер: {scanner_st}\n"
                if scanner_rsn:
                    safe_rsn = _escape_md(str(scanner_rsn)[:200])
                    block += f"   > {safe_rsn}\n"
                risk_label = "✅ APPROVED / ОДОБРЕН" if risk_ok else "❌ REJECTED / ОТКЛОНЁН"
                block += f"🛡 Risk Manager / Риск-менеджер: {risk_label}\n"
                if not risk_ok and risk_rsn:
                    safe_risk = _escape_md(str(risk_rsn)[:200])
                    block += f"   > {safe_risk}\n"
                if ceo_rsn:
                    safe_ceo = _escape_md(str(ceo_rsn)[:200])
                    block += f"📝 {safe_ceo}\n"
                parts.append(block)

            # Проверяем, была ли одобрена хоть одна монета
            any_approved = any(item.get("risk_approved", False) for item in scan_summaries)
            if not any_approved:
                parts.append("\n💡 Сильных сигналов для входа не обнаружено. Бот продолжает мониторинг.")
            else:
                parts.append("\n🚀 Найден отличный сетап! Ордера отправлены на биржу.")

            scan_reply = "\n".join(parts)
            # Отправляем без Markdown чтобы избежать ошибок парсинга спецсимволов
            await self.services.tg_sender.send_message(scan_reply, parse_mode="")

        tok_stats = LLMClient.get_token_stats()
        print(f"\n=== ТОРГОВЫЙ ЦИКЛ №{cycle_number} УСПЕШНО ЗАВЕРШЕН ===")
        print(f"📊 [РАСХОД ТОКЕНОВ] За цикл: {tok_stats['cycle_tokens']:,} токенов | Всего за сессию: {tok_stats['total_tokens']:,} (Prompt: {tok_stats['total_prompt_tokens']:,}, Output: {tok_stats['total_completion_tokens']:,})")
        self.services.logger.info(f"[Tokens] Cycle #{cycle_number}: {tok_stats['cycle_tokens']:,} | Total Session: {tok_stats['total_tokens']:,}")




    async def run_sentinel_checks(self, is_background: bool = False):
        """
        Independent Stage 1.5 (TP/SL) and 1.6 (Sentinel) execution.
        Protected by singleflight mutex and 5-second debounce.
        """
        # 1. Если это фоновый тик и синхронизация уже идет — сразу пропускаем
        if is_background and self._sentinel_lock.locked():
            if hasattr(self.services.logger, "debug"):
                self.services.logger.debug("[Stage 1.5] Синхронизация уже выполняется основным циклом. Пропуск фонового тика.")
            return

        async with self._sentinel_lock:
            # 2. Защита от повторного запроса (Debounce): если другой поток только что
            # закончил синхронизацию (< 5 сек назад), используем его свежий стейт
            now_mono = time.monotonic()
            if (now_mono - self._last_sentinel_sync_ts) < 5.0:
                if hasattr(self.services.logger, "debug"):
                    self.services.logger.debug("[Stage 1.5] Стейт позиций синхронизирован < 5с назад. Повторный вызов пропущен.")
                return

            try:
                async def safe_reflect(reflector, closed_trd, context):
                    try:
                        await reflector.reflect(closed_trd, context)
                    except Exception as e:
                        self.services.logger.error(f"[ReflectorAgent] Error during background reflection: {e}")

                if hasattr(self.services.trading_service, "sync_with_exchange"):
                    if hasattr(self.services.logger, "debug"):
                        self.services.logger.debug("[Stage 1.5] Синхронизация состояний позиций с биржей...")
                    await self.services.trading_service.sync_with_exchange()

                active_symbols = list(self.services.trading_service.active_positions.keys())
                if active_symbols and hasattr(self.services.logger, "debug"):
                    self.services.logger.debug("[Stage 1.5] Keeper проверяет TP/SL для открытых позиций...")
                for symbol in active_symbols:
                    try:
                        market_data = await self.services.fetcher.fetch_all_market_data(symbol)
                        current_price = market_data.get("price_data", {}).get("current_price", 0)

                        closed_reports = await self.services.trading_service.check_and_update_positions(symbol, current_price)
                        for closed in closed_reports:
                            if hasattr(self.agents, "telegram") and hasattr(self.agents.telegram, "format_position_closed_message"):
                                closed_msg = self.agents.telegram.format_position_closed_message(closed)
                            else:
                                pnl_emoji = "🎉" if closed.get("pnl_usd", 0) >= 0 else "🔻"
                                closed_msg = (
                                    f"{pnl_emoji} *TRADE CLOSED / СДЕЛКА ЗАКРЫТА ({closed['triggered_by']})*\n\n"
                                    f"🪙 *Asset / Монета:* `{closed['symbol']}`\n"
                                    f"📊 *Direction / Направление:* `{closed['direction']}`\n"
                                    f"🎯 *Entry / Вход:* `${closed['entry_price']:,.2f}` ➔ *Exit / Выход:* `${closed['exit_price']:,.2f}`\n"
                                    f"💰 *PnL:* `${closed['pnl_usd']:,.2f}` (ROI: {closed.get('roi_pct', 0):+.2f}%)\n"
                                )
                            print(f"\n--- ЗАКРЫТИЕ ПОЗИЦИИ В TELEGRAM [{symbol}] ---")
                            print(closed_msg)
                            print("--------------------------------------------")
                            await self.services.tg_sender.send_message(closed_msg)
                            await self.services.tg_sender.broadcast_to_channel(closed_msg)
                            pnl_val = float(closed.get("net_pnl_usd", closed.get("pnl_usd", 0.0)) or 0.0)
                            out_tag = "WIN" if pnl_val > 0.001 else ("LOSS" if pnl_val < -0.001 else "BREAK_EVEN")
                            tracker.record_closed_trade(out_tag, pnl_val)
                            if getattr(self.agents.reflector, "enabled", False):
                                asyncio.create_task(safe_reflect(self.agents.reflector, closed, market_data))
                        
                        # СТАДИЯ 1.6: SENTINEL AGENT (PHASE 2) - DETERMINISTIC RISK CONTROL
                        if symbol in self.services.trading_service.active_positions:
                            pos = self.services.trading_service.active_positions[symbol]
                            if hasattr(self.services.logger, "debug"):
                                self.services.logger.debug(f"[Stage 1.6] Sentinel Agent оценивает актуальность SL для {symbol}...")
                            
                            # Fetch ATR (14-period ATR from indicators, fallback to 1% of price)
                            atr_value = market_data.get("indicators", {}).get("atr_14", 0)
                            if atr_value <= 0:
                                atr_value = current_price * 0.01
                                
                            profile_rules = self.agents.risk._get_profile_rules(getattr(self, '_effective_profile', getattr(config, "TRADING_PROFILE", "BALANCED")))
                            
                            sentinel_verdict = await self.agents.sentinel.analyze(pos, market_data, profile_rules, atr_value)
                            
                            new_sl = sentinel_verdict.get("new_sl")
                            new_state = sentinel_verdict.get("state")
                            reasoning = sentinel_verdict.get("reasoning")
                            
                            is_anti_bot_pos = bool(pos.get("strategy_mode") == "MEAN_REVERSION" or pos.get("is_anti_bot"))
                            if is_anti_bot_pos and new_state == "EXTREME_BLOWOUT_DEFENSE":
                                new_sl = None

                            if new_sl:
                                success = await self.services.trading_service.update_stop_loss(symbol, new_sl, new_protection_state=new_state)
                                if success:
                                    msg = (
                                        f"🛡 *SENTINEL UPDATE / ЗАЩИТА ПРИБЫЛИ*\n\n"
                                        f"🪙 *Asset / Монета:* `{symbol}`\n"
                                        f"🔄 *State / Статус:* `{new_state}`\n"
                                        f"🎯 *New SL / Новый стоп:* `${new_sl:.4f}`\n"
                                        f"📝 *Reason:* `{reasoning}`"
                                    )
                                    print(f"🛡 [Sentinel] {symbol} SL updated to {new_sl:.4f} ({new_state})")
                                    await self.services.tg_sender.send_message(msg)
                                
                    except Exception as e:
                        print(f"❌ Ошибка проверки позиции {symbol}: {e}")

                # Manage active copy-trading subscriber positions in background
                try:
                    if hasattr(self.services.trading_service, "dispatch_copy_trade_manage"):
                        await self.services.trading_service.dispatch_copy_trade_manage()
                except Exception as copy_manage_err:
                    if hasattr(self.services.logger, "debug"):
                        self.services.logger.debug(f"[CopyTrading] Sentinel manage check error: {copy_manage_err}")
            finally:
                self._last_sentinel_sync_ts = time.monotonic()

    async def check_fast_market_momentum(self) -> Tuple[bool, str, str]:
        """
        Lightweight deterministic radar (0 LLM tokens).
        Checks top assets for sharp breakdowns, price dumps, or momentum breakouts.
        Returns: (spike_detected, symbol, reason)
        """
        if self._cycle_running:
            return False, "", "Cycle currently running"

        now_ts = time.time()
        core_symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
        # Expand FastRadar to top selected universe assets (0 LLM token cost)
        universe_symbols = [s for s in getattr(self, "_last_universe_symbols", []) if isinstance(s, str)]
        radar_symbols = list(dict.fromkeys(core_symbols + universe_symbols[:5]))

        # Pre-check active positions and directional cluster counts
        active_positions = getattr(self.services.trading_service, "active_positions", {})
        active_bases = set()
        directional_counts = {"LONG": 0, "SHORT": 0}
        directional_risk_counts = {"LONG": 0, "SHORT": 0}
        if isinstance(active_positions, dict):
            for pos_k, p in active_positions.items():
                if isinstance(p, dict):
                    sym_raw = str(p.get("symbol") or pos_k or "").upper().replace('/', '-')
                    base_t = sym_raw.split('-')[0].strip() if sym_raw else ""
                    if base_t:
                        active_bases.add(base_t)
                    p_dir = str(p.get("direction", p.get("side", ""))).upper()
                    is_rf = is_risk_free_position(p)
                    if "LONG" in p_dir or "BUY" in p_dir:
                        directional_counts["LONG"] += 1
                        if not is_rf:
                            directional_risk_counts["LONG"] += 1
                    elif "SHORT" in p_dir or "SELL" in p_dir:
                        directional_counts["SHORT"] += 1
                        if not is_rf:
                            directional_risk_counts["SHORT"] += 1

        max_directional_risk = 2

        for symbol in radar_symbols:
            base_sym = symbol.split("-")[0].upper()
            # 1. Отсекаем уже открытые активы
            if symbol in active_positions or base_sym in active_bases:
                continue

            if now_ts < self._fast_radar_cooldowns.get(symbol, 0):
                continue

            try:
                ohlcv = await self.services.fetcher.fetch_ohlcv(symbol)
                candles = ohlcv.get("candles_20", []) if isinstance(ohlcv, dict) else []
                if len(candles) < 5:
                    continue

                current_price = float(ohlcv.get("current_price", 0.0) or candles[-1].get("close", 0.0))
                if current_price <= 0:
                    continue

                last_c = candles[-1]
                o1 = float(last_c.get("open", current_price))
                if o1 <= 0:
                    continue

                # 1. Price change in current candle (%)
                delta_pct = ((current_price - o1) / o1) * 100.0

                # 2. Volume Ratio vs recent average (с защитой от эффекта «молодой свечи»)
                hist_vols = [float(c.get("volume", 0.0)) for c in candles[-11:-1]]
                avg_vol = sum(hist_vols) / len(hist_vols) if hist_vols else 0.0

                cur_vol = float(last_c.get("volume", 0.0))
                curr_vol_ratio = (cur_vol / avg_vol) if avg_vol > 0 else 1.0

                prev_c = candles[-2] if len(candles) >= 2 else {}
                prev_vol = float(prev_c.get("volume", 0.0))
                prev_closed_vol_ratio = (prev_vol / avg_vol) if avg_vol > 0 else 1.0

                effective_vol_ratio = max(curr_vol_ratio, prev_closed_vol_ratio)

                # 3. Donchian breakdown / breakout
                lows = [float(c.get("low", 0.0)) for c in candles[-21:-1]]
                highs = [float(c.get("high", 0.0)) for c in candles[-21:-1]]
                donchian_low = min(lows) if lows else 0.0
                donchian_high = max(highs) if highs else 0.0

                from core.config import config
                is_anti_bot = bool(getattr(config, "ANTI_BOT_MODE", False))

                is_breakdown = (donchian_low > 0 and current_price < donchian_low and delta_pct <= -0.5) or (delta_pct <= -1.2) or (delta_pct <= -0.8 and effective_vol_ratio >= 1.8) or (is_anti_bot and delta_pct <= -0.65)
                is_breakout = (donchian_high > 0 and current_price > donchian_high and delta_pct >= 0.5) or (delta_pct >= 1.2) or (delta_pct >= 0.8 and effective_vol_ratio >= 1.8) or (is_anti_bot and delta_pct >= 0.65)

                if not (is_breakdown or is_breakout):
                    continue

                # 4. Определяем направление импульса
                impulse_dir = "SHORT" if is_breakdown else "LONG"

                # 5. Не будим пайплайн, если слоты этого направления заняты (макс 2 с риском, до 3 если 1+ в БУ)
                max_dir_total = 3 if directional_counts.get(impulse_dir, 0) > directional_risk_counts.get(impulse_dir, 0) else 2
                if directional_counts.get(impulse_dir, 0) >= max_dir_total or directional_risk_counts.get(impulse_dir, 0) >= max_directional_risk:
                    continue

                # 5.5. Wash-Volume Rejection (Защита от поинт-фармеров Nado DEX):
                # Если volume_ratio > 2.5, но изменение OI < 0.15% и тело свечи < 0.25 * ATR — фейковый всплеск
                oi_change = float(ohlcv.get("oi_change_pct", 0.0) or (ohlcv.get("derivatives_data") or {}).get("oi_change_pct", 0.0) or 0.0)
                candle_body = abs(float(last_c.get("close", current_price)) - float(last_c.get("open", current_price)))
                radar_atr = float(ohlcv.get("atr_14", 0.0) or (ohlcv.get("indicators") or {}).get("atr_14", 0.0) or 0.0)
                if radar_atr <= 0 and len(candles) >= 14:
                    radar_atr = sum(float(c.get("high", 0.0)) - float(c.get("low", 0.0)) for c in candles[-14:]) / 14.0
                is_wash_volume = (effective_vol_ratio > 2.5) and (abs(oi_change) < 0.15) and (radar_atr > 0 and candle_body < 0.25 * radar_atr)
                if is_wash_volume:
                    if hasattr(self.services.logger, "info"):
                        self.services.logger.info(
                            f"[FastRadar] 🛑 Wash-Volume Rejection for {symbol}: Vol={effective_vol_ratio:.2f}x > 2.5, "
                            f"flat OI ({oi_change:+.2f}%), candle body {candle_body:.4f} < 0.25*ATR. Skipping fakeout."
                        )
                    continue

                # 6. Проверка объема: требуем vol_ratio >= 1.25x (или экстремальный ценовой импульс >= 1.2%)
                is_volume_confirmed = (effective_vol_ratio >= 1.25) or (abs(delta_pct) >= 1.20)
                is_predator_fakeout = is_anti_bot and (abs(delta_pct) >= 0.65)

                if not is_volume_confirmed and not is_predator_fakeout:
                    if hasattr(self.services.logger, "debug"):
                        self.services.logger.debug(
                            f"[FastRadar] Пропуск импульса {symbol} ({delta_pct:+.2f}%): "
                            f"недостаточный объем (Vol={effective_vol_ratio:.2f}x < 1.25x)."
                        )
                    continue

                self._fast_radar_cooldowns[symbol] = now_ts + 600  # 10m cooldown
                if is_predator_fakeout and not is_volume_confirmed:
                    return True, symbol, f"PREDATOR_FAKEOUT_SPIKE (Δ={delta_pct:.2f}%, Vol={effective_vol_ratio:.1f}x)"
                if is_breakdown:
                    return True, symbol, f"MOMENTUM_BREAKDOWN (Δ={delta_pct:.2f}%, Vol={effective_vol_ratio:.1f}x)"
                elif is_breakout:
                    return True, symbol, f"MOMENTUM_BREAKOUT (Δ={delta_pct:.2f}%, Vol={effective_vol_ratio:.1f}x)"
            except Exception as e:
                self.services.logger.debug(f"[FastRadar] Error checking {symbol}: {e}")

        return False, "", ""

    def _record_candidate_snapshot(
        self,
        symbol: str,
        cycle_number: int,
        macro_regime: str,
        profile: str,
        stage: str,
        final_action: str,
        rejection_tag: Optional[str] = None,
        rejection_reason: Optional[str] = None,
        strategy_profile: Optional[Any] = None,
        analyst_reports: Optional[List[Dict[str, Any]]] = None,
        debate_info: Optional[Dict[str, Any]] = None,
        ceo_verdict: Optional[Dict[str, Any]] = None,
        final_trade_decision: Optional[Any] = None,
        risk_verdict: Optional[Dict[str, Any]] = None,
        execution_status: Optional[str] = None,
        trade_success: Optional[bool] = None,
        actual_fill_price: Optional[float] = None,
        market_data: Optional[Dict[str, Any]] = None,
        min_conviction: Optional[int] = None,
        candidate_id: Optional[str] = None
    ):
        """
        Builds and logs a structured Decision Pipeline Snapshot to diagnostics tracker.
        Enables offline replay tests and historical funnel verification (Audit Point 8).
        """
        now_ts = time.time()
        iso_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        resolved_candidate_id = candidate_id or f"{symbol}:{cycle_number}"
        resolved_min_conv = min_conviction or (getattr(final_trade_decision, "min_conviction", None) if final_trade_decision else None)

        snapshot = {
            "timestamp": now_ts,
            "iso_time": iso_str,
            "candidate_id": resolved_candidate_id,
            "is_fallback_candidate_id": (candidate_id is None),
            "cycle_number": cycle_number,
            "symbol": symbol,
            "macro_regime": macro_regime,
            "trading_profile": profile,
            "stage": stage,
            "final_action": final_action,
            "rejection_tag": rejection_tag,
            "rejection_reason": rejection_reason,
            "min_conviction": resolved_min_conv
        }

        if market_data and isinstance(market_data, dict):
            indicators = market_data.get("indicators") or {}
            price_data = market_data.get("price_data") or {}
            derivatives = market_data.get("derivatives_data") or {}
            def _safe_float(val: Any) -> Optional[float]:
                if val is None:
                    return None
                try:
                    f = float(val)
                    return f if math.isfinite(f) else None
                except (ValueError, TypeError):
                    return None

            # Explicit presence check for current price rather than truthiness (Audit Point 6)
            raw_cur_price = price_data.get("current_price")
            if raw_cur_price is None:
                raw_cur_price = market_data.get("current_price")
            cur_price_val = _safe_float(raw_cur_price)
            if cur_price_val is not None and cur_price_val <= 0.0:
                cur_price_val = None

            raw_dh = indicators.get("donchian_high")
            raw_dl = indicators.get("donchian_low")
            raw_vol = indicators.get("volume_spike_pct")
            raw_atr = indicators.get("atr_pct")
            raw_bb = indicators.get("bb_position_pct") if "bb_position_pct" in indicators else indicators.get("bb_pos")
            raw_ema = indicators.get("ema_20")
            raw_rsi = indicators.get("rsi_14") if indicators.get("rsi_14") is not None else indicators.get("rsi")
            # Preserve genuine 0.0 vs None for missing/unreported funding rate (Audit Point 6)
            raw_funding = derivatives.get("funding_rate_hourly", derivatives.get("funding_rate"))
            funding_val = _safe_float(raw_funding)

            snapshot["market_guard_inputs"] = {
                "current_price": cur_price_val,
                "donchian_high": _safe_float(raw_dh),
                "donchian_low": _safe_float(raw_dl),
                "volume_spike_pct": _safe_float(raw_vol),
                "atr_pct": _safe_float(raw_atr),
                "bb_position_pct": _safe_float(raw_bb),
                "ema_20": _safe_float(raw_ema),
                "rsi_14": _safe_float(raw_rsi),
                "adx_14": _safe_float(indicators.get("adx_14") or indicators.get("adx")),
                "atr_14": _safe_float(indicators.get("atr_14") or indicators.get("atr")),
                "rvol": _safe_float(indicators.get("rvol")),
                "macro_box_pos_pct": _safe_float(indicators.get("macro_box_pos_pct")),
                "low_48h": _safe_float(indicators.get("low_48h")),
                "high_48h": _safe_float(indicators.get("high_48h")),
                "candles_20": (market_data.get("price_data") or {}).get("candles_20") or [],
                "funding_rate": funding_val,
                "is_pullback_retest": bool(market_data.get("is_pullback_retest", False)),
                "multi_timeframe": market_data.get("multi_timeframe") or {},
                "algo_signals": indicators.get("algo_signals") or {},
                "asset_return_24h": _safe_float(indicators.get("asset_return_24h")),
                "rs_divergence": _safe_float(indicators.get("rs_divergence"))
            }
        if strategy_profile:
            snapshot["router"] = {
                "strategy_mode": getattr(strategy_profile, "strategy_mode", None),
                "direction_bias": getattr(strategy_profile, "direction_bias", None),
                "has_directional_signal": getattr(strategy_profile, "has_directional_signal", None),
                "reasoning": getattr(strategy_profile, "reasoning", None)
            }
        if analyst_reports:
            snapshot["analysts"] = {
                r.get("agent_name", f"agent_{i}"): {
                    "signal": r.get("signal"),
                    "confidence": r.get("confidence")
                }
                for i, r in enumerate(analyst_reports) if isinstance(r, dict)
            }
        if debate_info:
            snapshot["debate"] = debate_info
        if ceo_verdict:
            snapshot["ceo"] = {
                "decision": ceo_verdict.get("decision"),
                "conviction": ceo_verdict.get("conviction"),
                "directional_confidence": ceo_verdict.get("directional_confidence"),
                "entry_quality": ceo_verdict.get("entry_quality"),
                "raw_score": ceo_verdict.get("raw_score"),
                "deterministic_score": ceo_verdict.get("deterministic_score"),
                "risk_breakdown": ceo_verdict.get("risk_breakdown", {}),
                "trade_action": ceo_verdict.get("trade_action"),
                "reasoning": ceo_verdict.get("reasoning_en", "")
            }
        if final_trade_decision:
            snapshot["guard"] = {
                "action": getattr(final_trade_decision, "trade_action", None),
                "decision": getattr(final_trade_decision, "decision", None),
                "conviction": getattr(final_trade_decision, "conviction", None),
                "reason": getattr(final_trade_decision, "guard_reason", None),
                "is_actionable": getattr(final_trade_decision, "is_actionable", None),
                "guard_status": getattr(final_trade_decision, "guard_status", None),
                "rejection_tag": getattr(final_trade_decision, "rejection_tag", None)
            }
        if risk_verdict:
            approved_risk_val = float(risk_verdict.get("approved_risk_usd") or risk_verdict.get("risk_amount_usd") or 0.0)
            snapshot["risk"] = {
                "approved": risk_verdict.get("approved"),
                "notional_size_usd": risk_verdict.get("notional_size_usd", 0),
                "cohort": risk_verdict.get("cohort", "UNKNOWN"),
                "leverage": risk_verdict.get("leverage", 10),
                "take_profit_price": risk_verdict.get("take_profit_price", 0),
                "stop_loss_price": risk_verdict.get("stop_loss_price", 0),
                "approved_risk_usd": approved_risk_val,
                "reasoning": risk_verdict.get("reasoning", "")
            }
        if execution_status is not None:
            snapshot["execution"] = {
                "status": execution_status,
                "trade_success": bool(trade_success),
                "fill_price": actual_fill_price
            }
        tracker.record_decision_snapshot(snapshot)

    def register_pullback_candidate(
        self,
        symbol: str,
        decision: str,
        current_price: float,
        directional_conf: int,
        conviction: int,
        strategy_mode: str,
        market_data: Dict[str, Any],
        ceo_verdict: Dict[str, Any],
        strategy_profile: Any,
        reason: str = "",
        ttl_seconds: int = 2700  # 45 minutes
    ):
        """
        Registers a high-conviction setup where price is temporarily extended from base (WAIT_FOR_PULLBACK).
        Monitors lightweight live price (0 LLM tokens) in background fast radar loop.
        """
        from core.config import config
        if strategy_mode == "MEAN_REVERSION" or getattr(config, "ANTI_BOT_MODE", False) is True:
            return

        indicators = market_data.get("indicators", {}) if isinstance(market_data, dict) else {}
        ema_20 = float(indicators.get("ema_20") or 0.0)
        atr_14 = float(indicators.get("atr_14") or 0.0)
        swing_high_15m = float(indicators.get("swing_high_15m") or indicators.get("high_48h") or 0.0)
        swing_low_15m = float(indicators.get("swing_low_15m") or indicators.get("low_48h") or 0.0)
        min_pullback_dist = max(0.5 * atr_14, current_price * 0.004) if atr_14 > 0 else (current_price * 0.008)
        inval_dist = 1.2 * atr_14 if atr_14 > 0 else (current_price * 0.015)

        # Dynamic pullback target calculation tied to 15m EMA-20
        if decision == "LONG":
            target_pullback_price = round(
                min(ema_20, current_price - min_pullback_dist) if (ema_20 > 0 and ema_20 < current_price) else (current_price - min_pullback_dist),
                6
            )
            raw_inval = target_pullback_price - inval_dist
            if swing_low_15m > 0:
                invalidation_price = round(min(raw_inval, swing_low_15m - 0.30 * atr_14), 6)
            else:
                invalidation_price = round(raw_inval, 6)
        else:  # SHORT
            target_pullback_price = round(
                max(ema_20, current_price + min_pullback_dist) if (ema_20 > current_price) else (current_price + min_pullback_dist),
                6
            )
            raw_inval = target_pullback_price + inval_dist
            if swing_high_15m > 0:
                invalidation_price = round(max(raw_inval, swing_high_15m + 0.30 * atr_14), 6)
            else:
                invalidation_price = round(raw_inval, 6)

        now_ts = time.time()
        import datetime
        added_str = datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")
        cand_reason = reason or (ceo_verdict.get("reasoning", "") if isinstance(ceo_verdict, dict) else "")
        self._pullback_watchlist[symbol] = {
            "symbol": symbol,
            "direction": decision,
            "signal_price": current_price,
            "target_pullback_price": target_pullback_price,
            "target_entry": target_pullback_price,
            "invalidation_price": invalidation_price,
            "directional_conf": directional_conf,
            "conviction": conviction,
            "strategy_mode": strategy_mode,
            "reason": cand_reason,
            "created_at": now_ts,
            "added_at": added_str,
            "expires_at": now_ts + ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "status": "WATCHING"
        }
        self._save_pullback_watchlist()

        print(f"👁️ [Pullback Watchlist] {symbol} добавлен в мониторинг отката ({decision}): "
              f"Сигнальная цена ${current_price:,.2f} -> Цель отката ~${target_pullback_price:,.2f} "
              f"(Инвалидация: ${invalidation_price:,.2f}, TTL: {int(ttl_seconds/60)} мин)")
        self.services.logger.info(
            f"[PullbackWatchlist] Added {symbol} ({decision}). Signal: {current_price}, "
            f"Target: {target_pullback_price}, Invalidation: {invalidation_price}, TTL: {ttl_seconds}s"
        )

    def remove_pullback_candidate(self, symbol: str, reason: str = "позиция открыта в пайплайне") -> bool:
        """Удаляет актив (по полному и базовому тикеру) из вотчлиста отката."""
        if not symbol or not self._pullback_watchlist:
            return False

        base_target = symbol.split("-")[0].upper()
        keys_to_remove = [
            k for k in self._pullback_watchlist.keys()
            if k.upper() == symbol.upper() or k.split("-")[0].upper() == base_target
        ]

        for k in keys_to_remove:
            self._pullback_watchlist.pop(k, None)
            self.services.logger.info(f"[PullbackWatchlist] 🧹 {k} удален из мониторинга отката ({reason}).")

        if keys_to_remove:
            self._save_pullback_watchlist()
        return bool(keys_to_remove)

    async def check_pullback_watchlist(self) -> Tuple[bool, str, str]:
        """
        Lightweight continuous pullback monitor (0 LLM tokens).
        Executed every 45-60s in background fast radar loop.
        
        Checks watchlist symbols:
        1. Cleans up candidates that are already open in active_positions.
        2. Skips wake-up if directional cluster cap (2/2) for the candidate's direction is full.
        3. Cancels/expires stale signals (> 45 min).
        4. Cancels signals if price invalidates thesis (breaks invalidation level).
        5. If pullback occurred (price touched pullback target zone), triggers immediate re-test!
        
        Returns: (triggered: bool, symbol: str, reason: str)
        """
        if not self._pullback_watchlist:
            return False, "", ""

        if self._cycle_running:
            return False, "", "Cycle currently running"

        # Pre-check active positions and directional counts (F-07 Fix: Recognize Risk-Free BE Slots)
        active_positions = getattr(self.services.trading_service, "active_positions", {})
        active_bases = set()
        active_counts = {"LONG": 0, "SHORT": 0}
        from agents.risk_manager import RiskManager

        if isinstance(active_positions, dict):
            for pos_k, p in active_positions.items():
                if isinstance(p, dict):
                    sym_raw = str(p.get("symbol") or pos_k or "").upper().replace('/', '-')
                    base_t = sym_raw.split('-')[0].strip() if sym_raw else ""
                    if base_t:
                        active_bases.add(base_t)
                    # Risk-free position (BE or trailing in profit) does NOT consume capital risk slots
                    if RiskManager.is_risk_free_position(p):
                        continue
                    p_dir = str(p.get("direction", p.get("side", ""))).upper()
                    if "LONG" in p_dir or "BUY" in p_dir:
                        active_counts["LONG"] += 1
                    elif "SHORT" in p_dir or "SELL" in p_dir:
                        active_counts["SHORT"] += 1

        now_ts = time.time()
        symbols_to_delete = []

        for symbol, item in list(self._pullback_watchlist.items()):
            base_sym = symbol.split("-")[0].upper()
            # 0. If position is already open for this asset, remove and skip
            if symbol in active_positions or base_sym in active_bases:
                self.services.logger.info(
                    f"[PullbackWatchlist] 🧹 {symbol} уже находится в активных позициях. Удален из мониторинга."
                )
                symbols_to_delete.append(symbol)
                continue

            # 1. Stale signal check (TTL Expiration)
            if now_ts >= item["expires_at"]:
                elapsed_min = int((now_ts - item["created_at"]) / 60)
                print(f"🗑️ [Pullback Watchlist] Сигнал {symbol} ({item['direction']}) устарел ({elapsed_min} мин >= {int(item['ttl_seconds']/60)} мин). Удален.")
                self.services.logger.info(f"[PullbackWatchlist] Stale signal expired for {symbol}. Removed.")
                symbols_to_delete.append(symbol)
                continue

            direction = item["direction"]

            # 2. Directional Cluster Cap Check: if 2/2 slots for direction already full, do not wake pipeline
            target_dir = str(direction).upper()
            if active_counts.get(target_dir, 0) >= 2:
                # Do not delete yet (thesis might still be active), but do not poll price or wake pipeline
                continue

            # Fetch fresh current price (0 LLM tokens)
            try:
                ohlcv = await self.services.fetcher.fetch_ohlcv(symbol)
                current_price = float(ohlcv.get("current_price", 0.0) or (ohlcv.get("candles_20", [{}])[-1].get("close", 0.0)))
                if current_price <= 0:
                    continue
            except Exception as e:
                self.services.logger.debug(f"[PullbackWatchlist] Error fetching price for {symbol}: {e}")
                continue

            target_price = item["target_pullback_price"]
            inv_price = item["invalidation_price"]

            # 3. Invalidation check
            if direction == "LONG" and current_price < inv_price:
                print(f"🛑 [Pullback Watchlist] Сетап {symbol} (LONG) инвалидирован: цена (${current_price:,.2f}) пробила уровень инвалидации (${inv_price:,.2f}). Удален.")
                self.services.logger.info(f"[PullbackWatchlist] Setup invalidated for {symbol} LONG. Removed.")
                symbols_to_delete.append(symbol)
                continue
            elif direction == "SHORT" and current_price > inv_price:
                print(f"🛑 [Pullback Watchlist] Сетап {symbol} (SHORT) инвалидирован: цена (${current_price:,.2f}) пробила уровень инвалидации (${inv_price:,.2f}). Удален.")
                self.services.logger.info(f"[PullbackWatchlist] Setup invalidated for {symbol} SHORT. Removed.")
                symbols_to_delete.append(symbol)
                continue

            # 4. Pullback check: trigger only when price covers at least 80% of distance from signal_price to target_price
            signal_price = float(item.get("signal_price", 0.0) or item.get("initial_price", current_price))
            pullback_hit = False
            if direction == "LONG":
                dist = max(0.0, signal_price - target_price)
                trigger_lvl = target_price + (0.20 * dist) if dist > 0 else target_price
                pullback_hit = (current_price <= trigger_lvl)
            elif direction == "SHORT":
                dist = max(0.0, target_price - signal_price)
                trigger_lvl = target_price - (0.20 * dist) if dist > 0 else target_price
                pullback_hit = (current_price >= trigger_lvl)

            if pullback_hit:
                print(f"\n🎯 [Pullback Watchlist] ОТКАТ ПРОИЗОШЕЛ! {symbol} ({direction}): "
                      f"Текущая цена ${current_price:,.2f} достигла зоны отката ~${target_price:,.2f}!")
                self.services.logger.info(f"[PullbackWatchlist] Pullback confirmed for {symbol} ({direction}) at {current_price}. Triggering entry retest.")
                symbols_to_delete.append(symbol)
                self._priority_symbol = symbol
                self._priority_direction = direction
                # Store full context for CEO Agent retest memory:
                self._pullback_context[symbol] = {
                    "signal_price": signal_price,
                    "target_pullback_price": target_price,
                    "invalidation_price": inv_price,
                    "direction": direction,
                    "directional_confidence": item.get("directional_confidence") or item.get("conviction") or item.get("original_dir_conf", 80),
                    "created_at": item.get("created_at", now_ts),
                }
                for s in symbols_to_delete:
                    self._pullback_watchlist.pop(s, None)
                self._save_pullback_watchlist()
                return True, symbol, f"PULLBACK_RETEST ({direction} at ${current_price:,.2f}, target was ${target_price:,.2f})"

        if symbols_to_delete:
            for s in symbols_to_delete:
                self._pullback_watchlist.pop(s, None)
            self._save_pullback_watchlist()

        return False, "", ""
```

---

<a id="file-python-core-config-py"></a>
## File: `python/core/config.py` (226 строк, 9.8 KB)

```python
import os
import json
from pathlib import Path
from typing import Literal, Dict, Any, Optional, Tuple
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Глобальные настройки проекта и API ключи, с валидацией типов."""
    
    # API Ключи
    GEMINI_API_KEY: str = Field(default="", validation_alias="GEMINI_API_KEY")
    GOOGLE_API_KEY: str = Field(default="", validation_alias="GOOGLE_API_KEY")
    GROQ_API_KEY: str = Field(default="", validation_alias="GROQ_API_KEY")
    OPENROUTER_API_KEY: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    OPENROUTER_MODEL: str = Field(default="deepseek/deepseek-chat")
    OPENROUTER_MODEL_CHEAP: str = Field(default="google/gemini-2.5-flash-lite")
    OPENROUTER_MODEL_CEO: str = Field(default="deepseek/deepseek-chat")
    OPENROUTER_MODEL_ESCALATION: str = Field(default="meta-llama/llama-3.3-70b-instruct")
    ESCALATE_WAIT_FOR_PULLBACK: bool = Field(default=False)
    USE_LLM_UNIVERSE: bool = Field(default=False)
    ENABLE_MAKER_LIMIT_ENTRY: bool = Field(default=True)
    ENABLE_DYNAMIC_TP2_RUNNER: bool = Field(default=True)
    KIE_API_KEY: str = Field(default="", validation_alias="KIE_API_KEY")
    KIE_MODEL: str = Field(default="DeepSeek-V3")
    
    # Web3 / Nado
    INK_PRIVATE_KEY: SecretStr = Field(default=SecretStr(""))
    NADO_NETWORK: Literal["TESTNET", "MAINNET"] = Field(default="TESTNET")
    SHADOW_MODE: bool = Field(default=False, description="Simulate execution without sending on-chain transactions")
    ANTI_BOT_MODE: bool = Field(
        default=False,
        description="Predator / Anti-Bot Mode targeting bots at Bollinger/Donchian extremes"
    )
    AUTO_PREDATOR_SWITCH: bool = Field(
        default=True,
        description="Auto-switch ANTI_BOT_MODE based on volatility and regime"
    )
    ALLOW_FORBIDDEN_STRATEGIES: bool = Field(
        default=False,
        description="Allow forbidden regime/strategy combinations (e.g. Trend-Following in Transition, Breakouts in Chop) for research/shadow exploration"
    )
    FAST_DEBATE_BYPASS: bool = Field(
        default=False,
        description="Bypass LLM debate and use deterministic analyst thesis for ultra-fast execution"
    )
    MIN_NOTIONAL_FLOOR_USD: float = Field(
        default=35.0,
        description="Minimum order notional in USD before auto-bumping to exchange CLOB min_size ($104.00). Orders below this are vetoed as micro-dust."
    )
    
    LIVE_TRADING_ENABLED: bool = Field(default=False)
    NADO_LIVE_TRADING_ENABLED: bool = Field(default=False)
    TRADING_ENGINE: str = Field(default="NADO")
    NADO_BUILDER_ID: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="Nado Builder Program ID (0 if not registered/unassigned)"
    )
    NADO_BUILDER_FEE_RATE: int = Field(
        default=0,
        ge=0,
        le=1023,
        description="Nado Builder Fee Rate in tenths of bps (0-1023)"
    )
    
    # Реферальная программа Nado DEX
    NADO_REFERRAL_CODE: str = Field(
        default="ptn3l1u",
        description="Nado DEX Referral Code for 10% fee rebate and affiliate rewards"
    )
    NADO_REFERRAL_URL: str = Field(
        default="https://app.nado.xyz?join=ptn3l1u",
        description="Nado DEX Referral Link with 10% trading fee discount"
    )
    
    # Мульти-аккаунты и Telegram Mini App (Copy-Trading)
    COPY_TRADING_ENABLED: bool = Field(
        default=True,
        description="Enable multi-trader copy trading for connected subscribers (default: True)"
    )
    TELEGRAM_MINIAPP_URL: str = Field(
        default="https://zloilapin.github.io/AI-Futures-Trader/",
        description="Public HTTPS URL for the Telegram Mini App (TMA) LinkSigner frontend"
    )
    
    # Телеграм
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_CHAT_ID: str = Field(default="")
    PUBLIC_CHANNEL_ID: str = Field(default="")
    
    # Торговые настройки
    TRADING_PROFILE: Literal["AGGRESSIVE", "BALANCED", "CONSERVATIVE"] = Field(default="BALANCED")
    STARTING_BALANCE: float = Field(default=60.0)
    SCAN_INTERVAL_MINUTES: int = Field(default=15, ge=1)
    MAX_CYCLES: int = Field(default=0, ge=0)
    SENTINEL_INTERVAL_SECONDS: int = Field(default=30, ge=5)
    SENTINEL_COOLDOWN_SECONDS: int = Field(default=600, ge=10)
    FAST_RADAR_INTERVAL_SECONDS: int = Field(default=45, ge=10)
    
    # Настройки Sentinel (Risk Control)
    SENTINEL_BE_ATR: float = Field(default=1.5)
    SENTINEL_TRAIL_ACTIVATION_ATR: float = Field(default=2.5)
    SENTINEL_TRAIL_DISTANCE_ATR: float = Field(default=2.0)
    SENTINEL_MIN_IMPROVE_ATR: float = Field(default=0.25)
    SENTINEL_FAST_POLL_SEC: int = Field(default=5, ge=1)
    
    # Настройки времени сна
    TIMEZONE_OFFSET: int = Field(default=3)
    REST_START_TIME: str = Field(default="19:00")
    REST_END_TIME: str = Field(default="07:00")
    
    # Настройки логирования
    LOG_LEVEL: str = Field(default="INFO")
    
    # Лимиты риск-менеджера
    MAX_CONCURRENT_POSITIONS: int = Field(default=3, ge=1)
    MAX_ACCOUNT_POSITIONS: int = Field(default=4, ge=1)
    
    # Флаг активности Reflector Agent (пост-мортем анализ и инъекция прошлых правил)
    ENABLE_REFLECTOR_AGENT: bool = Field(default=False)
    
    # Режим сетки частичного Take-Profit ("2_TIER" = 50/50 с Б/У на TP1, "4_TIER" = 25/25/25/25, "SINGLE_TIER" = 100%)
    PARTIAL_TP_MODE: Literal["2_TIER", "4_TIER", "SINGLE_TIER"] = Field(default="2_TIER")
    
    # Расширенные лимиты Risk Manager
    MIN_SL_PCT: float = Field(default=0.01)    # 1.0% floor (was 2.5% — dominated ATR for stable coins)
    MIN_TP_PCT: float = Field(default=0.03)    # 3.0% floor (was 7.5%)
    SPREAD_PENALTY_THRESHOLD: float = Field(default=0.4)
    SPREAD_VETO_THRESHOLD: float = Field(default=1.0)
    
    # Блокировка сделок Когорты D (низкая уверенность + REDUCE_SIZE) из-за отрицательного матожидания E(R) < 0
    BLOCK_COHORT_D_TRADES: bool = Field(default=True)

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

    @property
    def NADO_DEX_URL(self) -> str:
        return "https://testnet.app.nado.xyz/perpetuals" if self.NADO_NETWORK == "TESTNET" else "https://app.nado.xyz/perpetuals"

    @property
    def GEMINI_KEY(self) -> str:
        return self.GEMINI_API_KEY or self.GOOGLE_API_KEY

# Глобальный экземпляр конфига для импорта в другие модули
try:
    config = Settings()
except Exception as e:
    print(f"❌ [Config] Ошибка конфигурации среды: {e}")
    # Fallback for when we're just parsing AST or initializing docs without .env
    config = Settings(_env_file=None)


# ═════════════════════════════════════════════════════════════════════════════
# ⚡ Bayesian Tuned Hyperparameters Loader with Hot-Reload and Safety Clamps
# ═════════════════════════════════════════════════════════════════════════════

_TUNED_CACHE: Dict[str, Any] = {"mtime": 0.0, "params": {}}

# Hard safety bounds (Safety Clamps) that Optuna / tuned values can never exceed
_SAFETY_BOUNDS: Dict[str, Tuple[float, float]] = {
    "chop_range_threshold": (50.0, 72.0),
    "anti_chop_bb_max": (68.0, 90.0),
    "min_reduce_size_conv": (60.0, 75.0),
    "be_activation_r": (0.70, 0.90),
    "trailing_atr_mult": (1.50, 3.00),
    "min_kelly_mult": (0.50, 1.00),
}


def _resolve_tuned_path() -> Optional[Path]:
    """Resolves the active path to tuned_hyperparams.json across test overrides and roots."""
    try:
        from core.state_store import StateStore
        if StateStore._base_dir_override:
            normalized = StateStore._normalize_path("data/tuned_hyperparams.json")
            p = Path(normalized)
            return p if p.exists() else None
        normalized = StateStore._normalize_path("data/tuned_hyperparams.json")
        p = Path(normalized)
        if p.exists():
            return p
    except Exception:
        pass

    for candidate in [Path("python/data/tuned_hyperparams.json"), Path("data/tuned_hyperparams.json")]:
        if candidate.exists():
            return candidate
    return None


def reset_tuned_param_cache() -> None:
    """Resets in-memory cached hyperparameters (for test isolation and forced reload)."""
    global _TUNED_CACHE
    _TUNED_CACHE = {"mtime": 0.0, "params": {}}


def get_tuned_param(key: str, default: float) -> float:
    """
    Reads calibrated hyperparameter from tuned_hyperparams.json with hot reloading
    via mtime and strict bounding within _SAFETY_BOUNDS.
    """
    resolved_path = _resolve_tuned_path()
    if resolved_path is not None and resolved_path.exists():
        try:
            mtime = resolved_path.stat().st_mtime
            if mtime > _TUNED_CACHE.get("mtime", 0.0):
                raw = json.loads(resolved_path.read_text(encoding="utf-8"))
                params = raw.get("best_params") or raw.get("params") or {}
                if isinstance(params, dict):
                    _TUNED_CACHE["params"] = params
                    _TUNED_CACHE["mtime"] = mtime
        except Exception:
            pass

    val = float(_TUNED_CACHE.get("params", {}).get(key, default))
    if key in _SAFETY_BOUNDS:
        lo, hi = _SAFETY_BOUNDS[key]
        val = max(lo, min(hi, val))
    return val

```

---

<a id="file-python-core-models-py"></a>
## File: `python/core/models.py` (248 строк, 9.2 KB)

```python
from typing import List, Dict, Any, Optional, Literal, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
import copy
from pydantic import BaseModel, Field

def _make_deep_immutable(val: Any) -> Any:
    """Recursively converts mappings and collections into strictly immutable structures."""
    if isinstance(val, (dict, Mapping)):
        return MappingProxyType({k: _make_deep_immutable(v) for k, v in val.items()})
    elif isinstance(val, (list, tuple)):
        return tuple(_make_deep_immutable(v) for v in val)
    return val

def _make_mutable(val: Any) -> Any:
    """Recursively converts immutable structures back into standard python dicts and lists."""
    if isinstance(val, (dict, MappingProxyType, Mapping)):
        return {k: _make_mutable(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple)):
        return [_make_mutable(v) for v in val]
    return val

@dataclass(frozen=True)
class ExecutionResult:
    """
    Decoupled representation of order execution outcome on the exchange.
    Ensures that execution state is accurately preserved without mutating FinalRiskDecision.
    """
    symbol: str
    status: str                     # "SUCCESS" | "REJECTED_BY_EXCHANGE" | "SKIPPED"
    order_id: Optional[str] = None
    executed_at: float = 0.0
    error: Optional[str] = None
    notional_usd: float = 0.0
    actual_fill_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "order_id": self.order_id,
            "executed_at": self.executed_at,
            "error": self.error,
            "notional_usd": self.notional_usd,
            "actual_fill_price": self.actual_fill_price,
        }

@dataclass(frozen=True)
class FinalTradeDecision:
    """
    Immutable representation of the final trade decision produced by Deterministic Guard.
    Once evaluated, no component may mutate 'decision'.
    """
    symbol: str
    decision: str                   # "LONG" | "SHORT" | "HOLD"
    conviction: int                 # 0 to 100
    trade_action: str               # "ENTER" | "WAIT_FOR_PULLBACK" | "HOLD" | "REDUCE_SIZE"
    strategy_mode: str              # "TREND_FOLLOWING", "BREAKOUT", "MEAN_REVERSION", etc.
    direction_bias: str             # "LONG" | "SHORT" | "NEUTRAL"
    min_conviction: int             # Profile / mode threshold
    is_actionable: bool             # True only if candidate passed all deterministic guards
    guard_status: str               # "PASSED" | "BLOCKED" | "HOLD"
    guard_reason: str               # Human-readable rationale
    rejection_tag: Optional[str] = None # e.g. "STRATEGY_GUARD_VETO", "LOW_CONFIDENCE", "FUNDING_VETO", "WAIT_FOR_PULLBACK", "CEO_HOLD"
    directional_confidence: int = 0
    entry_quality: int = 0
    reasoning_en: str = ""
    reasoning_ru: str = ""
    raw_ceo_verdict: Mapping[str, Any] = field(default_factory=dict)
    conviction_for_risk: Optional[float] = None
    volume_explosion_override: bool = False
    action_size_mult_override: Optional[float] = None

    def __post_init__(self):
        # Deep defensive conversion of raw_ceo_verdict to guarantee nested immutability
        if self.raw_ceo_verdict:
            object.__setattr__(self, "raw_ceo_verdict", _make_deep_immutable(self.raw_ceo_verdict))

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        if self.raw_ceo_verdict and key in self.raw_ceo_verdict:
            return self.raw_ceo_verdict[key]
        return default

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            if val is not None:
                return val
        if self.raw_ceo_verdict and key in self.raw_ceo_verdict:
            return self.raw_ceo_verdict[key]
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key) or (bool(self.raw_ceo_verdict) and key in self.raw_ceo_verdict)

    def to_dict(self) -> Dict[str, Any]:
        d = _make_mutable(self.raw_ceo_verdict) if self.raw_ceo_verdict else {}
        d.update({
            "symbol": self.symbol,
            "decision": self.decision,
            "conviction": self.conviction,
            "trade_action": self.trade_action,
            "strategy_mode": self.strategy_mode,
            "direction_bias": self.direction_bias,
            "min_conviction": self.min_conviction,
            "is_actionable": self.is_actionable,
            "guard_status": self.guard_status,
            "guard_reason": self.guard_reason,
            "rejection_tag": self.rejection_tag,
            "hold_category": self.rejection_tag if self.decision == "HOLD" else None,
            "directional_confidence": self.directional_confidence,
            "entry_quality": self.entry_quality,
            "conviction_for_risk": self.conviction_for_risk,
            "volume_explosion_override": self.volume_explosion_override,
            "action_size_mult_override": self.action_size_mult_override,
            "reasoning_en": self.reasoning_en,
            "reasoning_ru": self.reasoning_ru,
        })
        return d

    def __iter__(self):
        """Allows unpacking as (decision, conviction, trade_action, min_conv) for backward compatibility."""
        return iter((self.decision, self.conviction, self.trade_action, self.min_conviction))


@dataclass(frozen=True)
class FinalRiskDecision:
    """
    Immutable representation of the final risk decision produced by RiskManager.
    RiskManager can ONLY APPROVE (approved=True) or VETO (approved=False).
    It never mutates 'decision'.
    """
    approved: bool
    veto_category: Optional[str] = None
    reasoning: str = ""
    trade_action: str = "ENTER"
    risk_amount_usd: float = 0.0
    notional_size_usd: float = 0.0
    margin_usd: float = 0.0
    leverage: float = 1.0
    effective_leverage: float = 1.0
    contracts: float = 0.0
    margin_pct: float = 0.0
    entry_price: float = 0.0
    take_profit_price: float = 0.0
    take_profit_pct: float = 0.0
    stop_loss_price: float = 0.0
    stop_loss_pct: float = 0.0
    risk_reward_ratio: float = 0.0
    liquidation_price: float = 0.0
    position_size_pct: float = 0.0
    execution_status: Optional[str] = None
    pending_trade_id: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        return default

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "veto_category": self.veto_category,
            "reasoning": self.reasoning,
            "trade_action": self.trade_action,
            "risk_amount_usd": self.risk_amount_usd,
            "notional_size_usd": self.notional_size_usd,
            "margin_usd": self.margin_usd,
            "leverage": self.leverage,
            "effective_leverage": self.effective_leverage,
            "contracts": self.contracts,
            "margin_pct": self.margin_pct,
            "entry_price": self.entry_price,
            "take_profit_price": self.take_profit_price,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_price": self.stop_loss_price,
            "stop_loss_pct": self.stop_loss_pct,
            "risk_reward_ratio": self.risk_reward_ratio,
            "liquidation_price": self.liquidation_price,
            "position_size_pct": self.position_size_pct,
            "execution_status": self.execution_status,
            "pending_trade_id": self.pending_trade_id,
        }

class ScoreBreakdown(BaseModel):
    scanner: Optional[float] = None
    candle: Optional[float] = None
    orderbook: Optional[float] = None
    oi_funding: Optional[float] = None
    news: Optional[float] = None
    indicator: Optional[float] = None

class CeoVerdict(BaseModel):
    decision: Literal["LONG", "SHORT", "HOLD"]
    conviction: int = Field(ge=0, le=100)
    reasoning_en: str
    score_breakdown: ScoreBreakdown

class RiskVerdict(BaseModel):
    approved: bool
    notional_size_usd: float = Field(ge=0)
    take_profit_price: float
    stop_loss_price: float
    risk_reward_ratio: float
    reason: str = ""

class Position(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_price: float
    size_usd: float
    tp_price: float
    sl_price: float
    leverage: int
    open_time: float
    
    # Optional fields for nado specific data
    order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    product_id: Optional[int] = None
    highest_pnl_pct: float = 0.0

class ClosedTrade(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_price: float
    close_price: float
    size_usd: float
    leverage: int
    pnl_usd: float
    pnl_pct: float
    open_time: float
    close_time: float
    duration_min: float
    reason: str
```

---

<a id="file-python-core-state_store-py"></a>
## File: `python/core/state_store.py` (134 строк, 5.9 KB)

```python
import os
import json
import threading
from typing import Dict, Any

class StateStore:
    """
    Centralized JSON State Store with Atomic Writes and Shadow Backup Recovery.
    Prevents file corruption on unexpected crashes by writing to a temporary file
    and replacing the original file atomically.
    Uses re-entrant thread locks (RLock) to prevent race conditions during reads/writes.
    Maintains .bak shadow backups to guarantee disaster recovery if primary file is corrupted.
    """
    _locks: Dict[str, threading.RLock] = {}
    _locks_mutex: threading.RLock = threading.RLock()
    _base_dir_override: Any = None

    @classmethod
    def _normalize_path(cls, filepath: str) -> str:
        if cls._base_dir_override:
            base_dir = cls._base_dir_override
            norm_f = os.path.normpath(filepath)
            if os.path.isabs(filepath):
                if "data" in norm_f:
                    parts = norm_f.split(os.sep + "data" + os.sep)
                    if len(parts) > 1:
                        target = os.path.join(base_dir, "data", parts[-1])
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        return os.path.normcase(os.path.abspath(target))
                return os.path.normcase(os.path.abspath(filepath))
            if filepath.startswith("python/") or filepath.startswith("python\\"):
                filepath = filepath[7:]
            target = os.path.join(base_dir, filepath)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            return os.path.normcase(os.path.abspath(target))

        if os.path.isabs(filepath):
            return os.path.normcase(os.path.abspath(filepath))
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if filepath.startswith("python/") or filepath.startswith("python\\"):
            filepath = filepath[7:]
        return os.path.normcase(os.path.abspath(os.path.join(base_dir, filepath)))

    @classmethod
    def _get_lock(cls, filepath: str) -> threading.RLock:
        norm_key = cls._normalize_path(filepath)
        with cls._locks_mutex:
            if norm_key not in cls._locks:
                cls._locks[norm_key] = threading.RLock()
            return cls._locks[norm_key]

    @classmethod
    def load(cls, filepath: str, default: Any = None) -> Any:
        if default is None:
            default = {}
        norm_path = cls._normalize_path(filepath)
        if not os.path.exists(norm_path) and not os.path.exists(f"{norm_path}.bak"):
            return default
            
        with cls._get_lock(norm_path):
            # Attempt 1: Load primary file if present and non-empty
            if os.path.exists(norm_path):
                try:
                    with open(norm_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if content.strip():
                            return json.loads(content)
                        else:
                            print(f"⚠️ [StateStore] Primary file is empty (0 bytes): {norm_path}")
                except Exception as e:
                    print(f"⚠️ [StateStore] Failed to load primary {norm_path}: {e}")

            # Attempt 2: Recover from .bak shadow backup
            bak_path = f"{norm_path}.bak"
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "r", encoding="utf-8") as f:
                        bak_content = f.read()
                        if bak_content.strip():
                            recovered = json.loads(bak_content)
                            print(f"🛡️ [StateStore] Successfully recovered state from backup: {bak_path}")
                            return recovered
                except Exception as bak_e:
                    print(f"❌ [StateStore] Backup file also corrupt {bak_path}: {bak_e}")

            return default

    @classmethod
    def save(cls, filepath: str, data: Any):
        norm_path = cls._normalize_path(filepath)
        os.makedirs(os.path.dirname(norm_path), exist_ok=True)
        import uuid
        import time
        import shutil
        temp_filepath = f"{norm_path}.{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:6]}.tmp"
        bak_filepath = f"{norm_path}.bak"
        
        with cls._get_lock(norm_path):
            try:
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False, default=str)
                
                # Update shadow backup before replacing if existing file is non-empty
                if os.path.exists(norm_path):
                    try:
                        if os.path.getsize(norm_path) > 0:
                            shutil.copy2(norm_path, bak_filepath)
                    except Exception:
                        pass

                # Atomic replace with retry loop for Windows file lock contention
                for attempt in range(5):
                    try:
                        os.replace(temp_filepath, norm_path)
                        break
                    except (PermissionError, OSError) as pe:
                        if attempt < 4:
                            time.sleep(0.05 * (attempt + 1))
                        else:
                            # Direct write fallback if os.replace is locked on Windows
                            try:
                                with open(norm_path, "w", encoding="utf-8") as f:
                                    json.dump(data, f, indent=4, ensure_ascii=False, default=str)
                                break
                            except Exception:
                                raise pe
            except Exception as e:
                print(f"❌ [StateStore] Failed to save {norm_path}: {e}")
            finally:
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except Exception:
                        pass
```

---

<a id="file-python-core-session-py"></a>
## File: `python/core/session.py` (21 строк, 0.6 KB)

```python
import aiohttp
from typing import Optional

class SessionManager:
    """
    Global aiohttp ClientSession manager.
    Prevents socket exhaustion and TIME_WAIT leaks by sharing a single session.
    """
    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    async def get(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession()
        return cls._session
        
    @classmethod
    async def close(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None
```

---

<a id="file-python-core-logger-py"></a>
## File: `python/core/logger.py` (128 строк, 4.7 KB)

```python
import os
import sys
import json
import threading
import structlog
from datetime import datetime, timezone

class TradeLogger:
    """
    Handles hierarchical logging of agent reasoning and trade decisions.
    Uses structlog for structured JSON lines output.
    """
    def __init__(self, base_dir="logs"):
        self._lock = threading.Lock()
        is_test_env = ("pytest" in sys.modules) or bool(os.environ.get("PYTEST_CURRENT_TEST"))
        if is_test_env and base_dir == "logs":
            self.base_dir = os.path.join("logs", "pytest_sandbox")
        else:
            self.base_dir = base_dir
        
        # Configure structlog for JSON output to files
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(ensure_ascii=False)
            ],
            wrapper_class=structlog.make_filtering_bound_logger(20), # INFO
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False
        )

    def _get_hierarchical_path(self) -> str:
        """
        Ensures the day folder is created strictly inside the month-1 folder.
        If under test environment, writes inside logs/pytest_sandbox directly.
        """
        is_test_env = ("pytest" in sys.modules) or bool(os.environ.get("PYTEST_CURRENT_TEST"))
        if is_test_env:
            path = self.base_dir if "pytest_sandbox" in self.base_dir else os.path.join(self.base_dir, "pytest_sandbox")
            os.makedirs(path, exist_ok=True)
            return path

        now = datetime.now()
        month_folder = f"{now.strftime('%m')}-1"    
        day_folder = now.strftime("%d_%m_%Y")       
        
        path = os.path.join(self.base_dir, month_folder, day_folder)
        os.makedirs(path, exist_ok=True)
        return path

    def log_decision(self, agent_name: str, message: str, **kwargs):
        """
        Appends agent reasoning and decisions to a daily JSONL file.
        """
        log_dir = self._get_hierarchical_path()
        file_path = os.path.join(log_dir, "decisions.jsonl")
        
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "agent": agent_name,
            "message": message,
            **kwargs
        }
        
        with self._lock:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_data, ensure_ascii=False) + "\n")
            
        try:
            print(f"[{agent_name}] {message}")
        except UnicodeEncodeError:
            try:
                print(f"[{agent_name}] {message}".encode("utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
            except Exception:
                pass

    def info(self, message: str, **kwargs):
        """
        Standard info logging method used across all agents and services.
        Maps to log_decision under the hood with 'System' as the actor.
        """
        self.log_decision("System_Core", message, level="info", **kwargs)

    def error(self, message: str, **kwargs):
        """
        Standard error logging method.
        """
        self.log_decision("System_Error", message, level="error", **kwargs)

    def critical(self, message: str, **kwargs):
        """
        Critical error logging method for urgent safety and risk incidents.
        """
        self.log_decision("System_Critical", message, level="critical", **kwargs)

    def warning(self, message: str, **kwargs):
        """
        Standard warning logging method.
        """
        self.log_decision("System_Warning", message, level="warning", **kwargs)

    def debug(self, message: str, **kwargs):
        """
        Silent debug logging method for routine heartbeats.
        Records to JSONL without console output.
        """
        try:
            log_dir = self._get_hierarchical_path()
            file_path = os.path.join(log_dir, "decisions.jsonl")
            log_data = {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "agent": "System_Core",
                "message": message,
                "level": "debug",
                **kwargs
            }
            with self._lock:
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_data, ensure_ascii=False) + "\n")
        except Exception:
            pass

if __name__ == "__main__":
    logger = TradeLogger()
    logger.info("System initialized for Nado DEX monitoring.")
```

---

<a id="file-python-core-utils-py"></a>
## File: `python/core/utils.py` (41 строк, 1.4 KB)

```python
import os
from datetime import datetime, timezone, timedelta
from core.config import config

def get_msk_status() -> tuple[bool, str]:
    """
    Checks if current time in MSK (UTC+3) is within quiet rest window.
    Returns (is_rest_period, current_msk_formatted_time).
    """
    offset = config.TIMEZONE_OFFSET
    msk_tz = timezone(timedelta(hours=offset))
    now_msk = datetime.now(msk_tz)
    
    def parse_time(time_str: str, default_h: int, default_m: int):
        try:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            if h >= 24: h, m = 23, 59
            return h, m
        except:
            return default_h, default_m

    start_h, start_m = parse_time(config.REST_START_TIME, 19, 0)
    end_h, end_m = parse_time(config.REST_END_TIME, 7, 0)
    
    current_mins = now_msk.hour * 60 + now_msk.minute
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    
    if start_mins > end_mins:
        is_rest = current_mins >= start_mins or current_mins < end_mins
    else:
        is_rest = start_mins <= current_mins < end_mins
        
    return is_rest, now_msk.strftime("%H:%M:%S") + " MSK"

def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in ['_', '*', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, f'\\{ch}')
    return text
```

---

