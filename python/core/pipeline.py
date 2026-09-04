import time
import asyncio
from dataclasses import dataclass
from typing import Dict, Any, List

from core.utils import get_msk_status, _escape_md
from core.config import config
from core.diagnostics import tracker

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

class TradingPipeline:
    def __init__(self, agents: AgentRegistry, services: ServiceRegistry, exchange_name: str):
        self.agents = agents
        self.services = services
        self.exchange_name = exchange_name
        self.data_guard = DataQualityGuard(self.services.logger)

    async def run_cycle(self, cycle_number: int, force_scan: bool = False, skip_new_trades: bool = False):
        is_rest, time_str = get_msk_status()
        profile = config.TRADING_PROFILE

        print(f"\n" + "="*65)
        print(f"🚀 ТОРГОВЫЙ ЦИКЛ №{cycle_number} ({self.exchange_name}) [{time_str}]")
        print(f"⚙️ ПРОФИЛЬ РИСКА: {profile} | 📐 MULTI-TIMEFRAME (15m + 1H + 4H) АКТИВЕН")
        print("="*65)

        if is_rest and not force_scan:
            print(f"🌙 [Schedule] НОЧНОЙ РЕЖИМ СКАНИРОВАНИЯ. Сейчас {time_str} (с 19:00 до 07:00 МСК).")

        if force_scan and is_rest:
            print(f"⚡ [ForceScan] Ручной запуск /scan во время отдыха ({time_str}). Пропуск тихого режима!")

        # Проверка Cooldown после серии убытков
        cooldown_until = getattr(self.services.trading_service, "cooldown_until", 0)
        now_ts = time.time()
        if now_ts < cooldown_until:
            remain_min = int((cooldown_until - now_ts) / 60)
            print(f"🛑 [Cooldown] Бот на паузе после 3 убытков подряд. Осталось {remain_min} мин.")
            self.services.logger.info(f"[System_Core] Cooldown active. {remain_min} min remaining.")
            if not force_scan:
                return

        if hasattr(self.services.trading_service, "recent_streak") and len(self.services.trading_service.recent_streak) >= 3 and self.services.trading_service.recent_streak[-3:] == ["LOSS", "LOSS", "LOSS"]:
            # Only trigger cooldown once per 3-loss streak, preserving streak memory for RiskManager
            if getattr(self.services.trading_service, "_last_cooldown_processed_len", 0) != len(self.services.trading_service.recent_streak):
                self.services.trading_service.cooldown_until = time.time() + 3600
                self.services.trading_service._last_cooldown_processed_len = len(self.services.trading_service.recent_streak)
                print(f"🛑 [Cooldown Activated] Зафиксировано 3 убытка подряд! Торговля приостановлена на 1 час.")
                self.services.logger.info("[System_Core] 3 consecutive losses detected. 1 hour cooldown activated.")
                if not force_scan:
                    return

        # СТАДИЯ 0: REGIME DETECTION (Адаптивный профиль риска)
        self.services.logger.info("[Stage 0] Regime Agent определяет фазу рынка (BTC/ETH)...")
        macro_cache = {}
        try:
            btc_data = await self.services.fetcher.fetch_all_market_data("BTC-USD")
            eth_data = await self.services.fetcher.fetch_all_market_data("ETH-USD")
            macro_cache["BTC-USD"] = btc_data
            macro_cache["ETH-USD"] = eth_data
            
            regime_payload = {
                "btc_data": btc_data,
                "eth_data": eth_data
            }
            regime_verdict = await self.agents.regime.analyze(regime_payload)
            
            detected_regime = regime_verdict.get("regime", "RANGE_CHOPPY")
            detected_profile = regime_verdict.get("recommended_profile", "BALANCED")
            regime_reasoning = regime_verdict.get("reasoning_en", "")
            
            # Динамически меняем профиль на этот цикл (но с ограничением сверху)
            original_profile = config.TRADING_PROFILE
            profile_ranks = {"CONSERVATIVE": 1, "BALANCED": 2, "AGGRESSIVE": 3}
            orig_rank = profile_ranks.get(original_profile, 2)
            detected_rank = profile_ranks.get(detected_profile, 2)
            
            # Защита: RegimeAgent может только ПОНИЖАТЬ риск во время шторма
            if detected_rank > orig_rank:
                effective_profile = original_profile
                self.services.logger.info(f"[Macro Regime] LLM proposed {detected_profile}, but bounded to {original_profile}.")
            else:
                effective_profile = detected_profile
                
            profile = effective_profile
            
            print(f"🌍 [Macro Regime] Рынок находится в фазе: {detected_regime}")
            print(f"🛡️ [Risk Profile] Профиль риска на этот цикл: {profile}")
            self.services.logger.info(f"[Macro Regime] {detected_regime} -> {profile}. Reason: {regime_reasoning}")
        except Exception as e:
            print(f"⚠️ [Macro Regime] Ошибка при определении режима: {e}. Используем базовый профиль {profile}.")
            self.services.logger.error(f"[Macro Regime] Failed to detect regime: {e}")

        # СТАДИЯ 1: UNIVERSE (Выбор активов)
        self.services.logger.info("[Stage 1] Universe Agent сканирует DEX на наличие ликвидных активов...")
        active_perps = await self.services.fetcher.fetch_active_perps(limit=15)
        broad_market_data = {
            "trending_perps": active_perps, 
            "volume_24h": "high"
        } 
        universe_report = await self.agents.universe.analyze(broad_market_data)

        selected_assets = universe_report.get("selected_pairs", [])
        if not selected_assets:
            # Fallback to top 6 by 24h volume directly from the exchange if LLM fails
            selected_assets = [p["symbol"] for p in active_perps[:6]]

        # Очищаем от дубликатов с сохранением приоритета от LLM
        selected_assets = list(dict.fromkeys(selected_assets))

        valid_symbols = {p["symbol"] for p in active_perps}
        selected_assets = [s for s in selected_assets if s in valid_symbols]

        self.services.logger.info(f"🎯 Отобраны уникальные валидные активы для сканирования: {', '.join(selected_assets)}")
        portfolio_data = await self.services.trading_service.get_portfolio_summary()

        recent_lessons = self.agents.reflector.get_lessons(limit=5)
        if recent_lessons:
            print(f"🧠 [Memory] Загружено {len(recent_lessons)} уроков из прошлых сделок.")

        any_signal_sent = False
        scan_summaries = []  # Сводка по каждому активу для отчёта ручного /scan

        await self.run_sentinel_checks()

        # QW Quiet Rest: Выход из цикла, если сейчас тихий час и нет force_scan
        if is_rest and not force_scan:
            print(f"⏸️ [Schedule] Тихий час. Позиции проверены. Пропуск новых сделок.")
            return

        # Фильтруем активы: не сканируем то, что уже открыто
        selected_assets = [s for s in selected_assets if s not in self.services.trading_service.active_positions]

        # ИТЕРАЦИЯ ПО ВСЕМ АКТИВАМ
        for symbol in selected_assets:
            # QW #5: Check if we've reached the maximum number of concurrent positions
            if len(self.services.trading_service.active_positions) >= getattr(config, "MAX_CONCURRENT_POSITIONS", 2):
                msg = f"⏸️ Достигнут лимит одновременных позиций ({config.MAX_CONCURRENT_POSITIONS}). Пропуск новых активов."
                print(msg)
                self.services.logger.info(f"[System_Core] {msg}")
                break

            print(f"\n🔍 ПОЛНЫЙ АНАЛИЗ (15m, 1H, 4H) KRAKEN FUTURES: {symbol}")
            tracker.record_scan()

            # СТАДИЯ 2: СБОР ДАННЫХ И ПРОВЕРКА СТАТУСА
            self.services.logger.info(f"[Stage 2] Сбор мульти-таймфреймовых данных (15m, 1H, 4H) для {symbol}...")
            try:
                if symbol in macro_cache:
                    market_data = macro_cache[symbol]
                else:
                    market_data = await self.services.fetcher.fetch_all_market_data(symbol)

                # Inject Nado native market limits
                limits = await self.services.trading_service.get_market_limits(symbol)
                if "derivatives_data" not in market_data:
                    market_data["derivatives_data"] = {}
                if "size_increment" in limits:
                    market_data["derivatives_data"]["size_increment"] = limits["size_increment"]

            except Exception as e:
                print(f"❌ Ошибка загрузки данных для {symbol}: {e}. Пропускаем.")
                scan_summaries.append({"symbol": symbol, "status": "⏭ Пропуск", "reason": str(e)})
                tracker.record_rejection("FETCH_ERROR")
                continue

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
                continue

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
                continue

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
                        continue
                    report["agent_name"] = agent.name
                    valid_reports.append(report)
                else:
                    self.services.logger.error(f"[Stage 3] Агент {agent.name} вернул некорректный ответ.")
                    continue

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
                continue

            # --- PRE-CEO FILTER ---
            # Экономим токены Llama 70B: если все базовые агенты нейтральны, пропускаем актив
            has_directional_signal = False
            mtf_alignment = market_data.get("multi_timeframe", {}).get("mtf_alignment")
            
            if mtf_alignment == "FULL_ALIGNMENT":
                has_directional_signal = True
                self.services.logger.info(f"[System_Core] Pre-CEO Filter bypassed for {symbol} due to FULL_ALIGNMENT MTF trend.")
            else:
                for report in valid_reports:
                    signal = str(report.get("signal", "NEUTRAL")).upper()
                    if signal in ["BULLISH", "BEARISH", "LONG", "SHORT"]:
                        has_directional_signal = True
                        break

            if not has_directional_signal:
                msg = f"⏸️ Пропуск {symbol}. Причина: Нет базовых сигналов (Pre-CEO Filter)."
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
                    "risk_reason": "Pre-CEO Filter: все базовые аналитики нейтральны",
                    "ceo_reasoning": "Bypassed",
                    "status": "⏸️ НЕТ СИГНАЛА"
                })
                tracker.record_rejection("NO_SIGNAL")
                continue

            # СТАДИЯ 4: MULTI-AGENT DEBATE & CEO JUDGEMENT
            self.services.logger.info(f"[Stage 4] Bull and Bear agents debating on {symbol}...")
            
            debate_payload = {
                "symbol": symbol,
                "multi_timeframe_context": market_data.get("multi_timeframe", {}),
                "analyst_reports": valid_reports
            }
            
            # Запускаем Быка и Медведя параллельно
            bull_verdict, bear_verdict = await asyncio.gather(
                self.agents.bull.analyze(debate_payload),
                self.agents.bear.analyze(debate_payload)
            )
            
            print(f"🐂 Bull Thesis: {bull_verdict.get('summary', 'N/A')[:100]}...")
            print(f"🐻 Bear Thesis: {bear_verdict.get('summary', 'N/A')[:100]}...")
            
            self.services.logger.info(f"[Stage 4] CEO Agent (Judge) evaluates the debate and MTF trend for {symbol}...")
            historical_context = self.agents.memory.get_recent_context(limit=3)
            
            ceo_payload = {
                "symbol": symbol,
                "multi_timeframe_context": market_data.get("multi_timeframe", {}),
                "bull_thesis": bull_verdict,
                "bear_thesis": bear_verdict,
                "subordinate_analyst_reports": valid_reports,
                "historical_context": historical_context,
                "past_lessons_learned": recent_lessons
            }
            ceo_verdict = await self.agents.ceo.analyze(ceo_payload)

            decision = str(ceo_verdict.get("decision", "HOLD")).upper()
            conviction = ceo_verdict.get("conviction", 0)
            
            conv_str = "N/A" if decision == "HOLD" else f"{conviction}%"

            print(f"⚖️ Решение CEO [{symbol}]: {decision} (Уверенность: {conv_str})")

            min_conv = 65 if profile == "AGGRESSIVE" else (80 if profile == "CONSERVATIVE" else 70)

            if decision not in ["LONG", "SHORT"] or conviction < min_conv:
                print(f"⏸️ Пропуск {symbol}. Решение: {decision}, Уверенность: {conv_str} (Требуется LONG/SHORT и >= {min_conv}%).")
                scanner_status = "⚠️ ЗАБЛОКИРОВАН СКАНЕРОМ" if scanner_blocked else "✅ OK"
                asset_summary = {
                    "symbol": symbol,
                    "decision": decision,
                    "conviction": conviction,
                    "scanner_status": scanner_status,
                    "scanner_reason": scanner_reason if scanner_blocked else None,
                    "risk_approved": False,
                    "risk_reason": f"Пропущен из-за фильтра CEO (HOLD или Уверенность < {min_conv})",
                    "ceo_reasoning": ceo_verdict.get("reasoning", ""),
                    "status": "⏸️ НЕТ СИГНАЛА"
                }
                scan_summaries.append(asset_summary)

                if decision == "HOLD":
                    hold_cat = ceo_verdict.get("hold_category", "CEO_HOLD")
                    tracker.record_rejection(hold_cat)
                elif conviction < min_conv:
                    tracker.record_rejection("LOW_CONFIDENCE")
                else:
                    # Если решение не LONG/SHORT и не HOLD (например, LLM выдал "NEUTRAL" или "NO_SIGNAL")
                    tracker.record_rejection("NO_SIGNAL")

                continue

            # (Stage 4.5 Correlation Filter moved to RiskManager)
            # СТАДИЯ 4.6: ФАНДИНГ ГЕЙТ (Funding Rate Gate)
            funding_rate = market_data.get("derivatives_data", {}).get("funding_rate", 0.0)
            if decision == "LONG" and funding_rate > 0.0005: # 0.05%
                print(f"⏸️ Пропуск {symbol}. Фандинг гейт: Запрет LONG при экстремально положительном фандинге ({funding_rate*100:.3f}%).")
                self.services.logger.info(f"[System_Core] Funding Gate: LONG denied for {symbol}. Funding = {funding_rate}")
                scan_summaries.append({
                    "symbol": symbol,
                    "decision": decision,
                    "conviction": conviction,
                    "scanner_status": "✅ OK",
                    "scanner_reason": None,
                    "risk_approved": False,
                    "risk_reason": f"Фандинг гейт (Funding = {funding_rate*100:.3f}%)",
                    "ceo_reasoning": "Bypassed by Funding Gate",
                    "status": "⏸️ НЕТ СИГНАЛА"
                })
                tracker.record_rejection("FUNDING_VETO")
                continue

            if decision == "SHORT" and funding_rate < -0.0005:
                print(f"⏸️ Пропуск {symbol}. Фандинг гейт: Запрет SHORT при экстремально отрицательном фандинге ({funding_rate*100:.3f}%).")
                self.services.logger.info(f"[System_Core] Funding Gate: SHORT denied for {symbol}. Funding = {funding_rate}")
                scan_summaries.append({
                    "symbol": symbol,
                    "decision": decision,
                    "conviction": conviction,
                    "scanner_status": "✅ OK",
                    "scanner_reason": None,
                    "risk_approved": False,
                    "risk_reason": f"Фандинг гейт (Funding = {funding_rate*100:.3f}%)",
                    "ceo_reasoning": "Bypassed by Funding Gate",
                    "status": "⏸️ НЕТ СИГНАЛА"
                })
                tracker.record_rejection("FUNDING_VETO")
                continue

            # СТАДИЯ 5: РИСК-МЕНЕДЖМЕНТ
            self.services.logger.info(f"[Stage 5] Fetching fresh price to prevent slippage on {symbol}...")
            try:
                fresh_market_data = await self.services.fetcher.fetch_all_market_data(symbol)
                fresh_price = fresh_market_data.get("price_data", {}).get("current_price", current_price)
                if fresh_price > 0 and current_price > 0:
                    deviation = abs(fresh_price - current_price) / current_price
                    if deviation > 0.003: # 0.3%
                        msg = f"⏸️ Пропуск {symbol}. Сильное проскальзывание цены во время анализа: {deviation*100:.2f}% (Signal: {current_price}, Fresh: {fresh_price})."
                        print(msg)
                        self.services.logger.info(f"[System_Core] Slippage Gate: {msg}")
                        scan_summaries.append({
                            "symbol": symbol,
                            "decision": decision,
                            "conviction": conviction,
                            "scanner_status": "✅ OK",
                            "scanner_reason": None,
                            "risk_approved": False,
                            "risk_reason": f"Slippage Gate: отклонение {deviation*100:.2f}%",
                            "ceo_reasoning": "Bypassed by Slippage Gate",
                            "status": "⏸️ НЕТ СИГНАЛА"
                        })
                        tracker.record_rejection("SLIPPAGE_VETO")
                        continue
                
                # Update market data and price for RiskManager
                market_data["price_data"]["current_price"] = fresh_price
                current_price = fresh_price
            except Exception as e:
                self.services.logger.error(f"[Stage 5] Failed to fetch fresh price for {symbol}: {e}. Proceeding with signal price.")
                
            self.services.logger.info(f"[Stage 5] Risk Manager ({profile}) проверяет параметры сделки для {symbol}...")
            portfolio_data["active_positions"] = self.services.trading_service.active_positions
            risk_verdict = await self.agents.risk.analyze(ceo_verdict, portfolio_data, market_data, effective_profile=profile)

            if risk_verdict.get("approved"):
                self.services.logger.info(f"✅ Status: APPROVED BY RISK MANAGER")
                print(f"💰 Position Amount: ${risk_verdict.get('notional_size_usd', 0):,.2f} ({risk_verdict.get('position_size_pct', 0)}% of portfolio)")
                print(f"🟢 Take Profit (TP): ${risk_verdict.get('take_profit_price', 0):,.2f} (+{risk_verdict.get('take_profit_pct', 0)}%)")
                # Автоматическая торговля 24/7 (полностью автономный режим)
                trade_success = await self.services.trading_service.open_position(
                    symbol=symbol,
                    direction=decision,
                    entry_price=current_price,
                    notional_usd=risk_verdict.get("notional_size_usd", 0),
                    tp_price=risk_verdict.get("take_profit_price", 0),
                    sl_price=risk_verdict.get("stop_loss_price", 0),
                    leverage=risk_verdict.get("leverage", 10),
                    original_thesis=ceo_verdict.get("reasoning_en", ""),
                    contracts=risk_verdict.get("contracts", 0.0)
                )

                if not trade_success:
                    print(f"❌ Ошибка открытия позиции на бирже для {symbol}.")
                    self.services.logger.error(f"❌ Status: REJECTED BY EXCHANGE ({symbol})")
                    risk_verdict["approved"] = False
                    risk_verdict["reasoning"] = "Биржа отклонила ордер (или ошибка сети/дубликат)."
                    tracker.record_execution_failed()
                else:
                    tracker.record_trade()
            else:
                self.services.logger.error(f"❌ Status: VETOED BY RISK MANAGER ({risk_verdict.get('reasoning')})")
                veto_cat = risk_verdict.get("veto_category") or "RISK_VETO"
                tracker.record_rejection(veto_cat)

            # СТАДИЯ 6: ТЕЛЕГРАМ
            # Собираем сводку по активу для отчёта ручного /scan
            scanner_status = "✅ OK"
            asset_summary = {
                "symbol": symbol,
                "decision": decision,
                "conviction": conviction,
                "scanner_status": scanner_status,
                "scanner_reason": None,
                "risk_approved": risk_verdict.get("approved", False),
                "risk_reason": risk_verdict.get("reasoning", ""),
                "ceo_reasoning": f"{ceo_verdict.get('reasoning_en', '')}\n\n{ceo_verdict.get('reasoning_ru', '')}".strip(),
                "status": "🚀 СИГНАЛ" if risk_verdict.get("approved") else "⏸️ VETO"
            }
            scan_summaries.append(asset_summary)

            if risk_verdict.get("approved") or risk_verdict.get("pending_trade_id"):
                print(f"🚀 НАЙДЕН СИГНАЛ! Генерация Telegram-уведомления [{symbol}] ({decision}, Уверенность {conviction}%)...")

                final_trade_data = {
                    "symbol": symbol,
                    "ceo_verdict": ceo_verdict,
                    "risk_verdict": risk_verdict
                }
                tg_response = await self.agents.telegram.analyze(final_trade_data)
                tg_message = tg_response.get("message", f"Отчет по {symbol}")

                print(f"\n--- ОТПРАВЛЕНО В TELEGRAM [{symbol}] ---")
                print(tg_message)
                print("-----------------------------")

                reply_markup = None
                pending_id = risk_verdict.get("pending_trade_id")
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

                # Если сделка не требует ручного подтверждения (дневной режим), транслируем сразу
                if not pending_id:
                    await self.services.tg_sender.broadcast_to_channel(tg_message)
            else:
                print(f"ℹ️ [Telegram] Пропуск отправки для {symbol}. Решение '{decision}' с уверенностью {conviction}% (Фильтр: LONG/SHORT и Уверенность ≥ {min_conv}%).")

            # СТАДИЯ 7: СОХРАНЕНИЕ В ПАМЯТЬ
            cycle_record = {
                "symbol": symbol,
                "market_conditions": scan_result,
                "analysts": valid_reports,
                "ceo_decision": ceo_verdict,
                "risk_assessment": risk_verdict,
                "status": "APPROVED" if risk_verdict.get("approved") else "VETOED"
            }
            self.agents.memory.save_cycle(cycle_record)
            self.services.logger.info(f"[System_Core] Торговый цикл успешно завершен для {symbol}.")

            await asyncio.sleep(1.5)

        # Если сканирование было запрошено вручную через /scan — всегда присылаем подробный отчёт (сводку)
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

        print(f"\n=== ТОРГОВЫЙ ЦИКЛ №{cycle_number} УСПЕШНО ЗАВЕРШЕН ===")




    async def run_sentinel_checks(self):
        """
        Independent Stage 1.5 (TP/SL) and 1.6 (Sentinel) execution.
        """
        async def safe_reflect(reflector, closed_trd, context):
            try:
                await reflector.reflect(closed_trd, context)
            except Exception as e:
                self.services.logger.error(f"[ReflectorAgent] Error during background reflection: {e}")

        if hasattr(self.services.trading_service, "sync_with_exchange"):
            self.services.logger.info("[Stage 1.5] Синхронизация состояний позиций с биржей...")
            await self.services.trading_service.sync_with_exchange()

        active_symbols = list(self.services.trading_service.active_positions.keys())
        if active_symbols:
            self.services.logger.info("[Stage 1.5] Keeper проверяет TP/SL для открытых позиций...")
        for symbol in active_symbols:
            try:
                market_data = await self.services.fetcher.fetch_all_market_data(symbol)
                current_price = market_data.get("price_data", {}).get("current_price", 0)

                closed_reports = await self.services.trading_service.check_and_update_positions(symbol, current_price)
                for closed in closed_reports:
                    pnl_emoji = "🎉" if closed["pnl_usd"] >= 0 else "🔻"
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
                    asyncio.create_task(safe_reflect(self.agents.reflector, closed, market_data))
                
                # СТАДИЯ 1.6: SENTINEL AGENT (PHASE 2) - SIGNAL EVOLUTION TRACKING
                if symbol in self.services.trading_service.active_positions:
                    import time
                    now = time.time()
                    if not hasattr(self, "_sentinel_last_run"):
                        self._sentinel_last_run = {}
                    cooldown = getattr(config, "SENTINEL_COOLDOWN_SECONDS", 180)
                    if now - self._sentinel_last_run.get(symbol, 0) < cooldown:
                        continue
                    self._sentinel_last_run[symbol] = now

                    pos = self.services.trading_service.active_positions[symbol]
                    self.services.logger.info(f"[Stage 1.6] Sentinel Agent оценивает актуальность тезиса для {symbol}...")
                    
                    safe_pos = {k: str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v for k, v in pos.items()}
                    
                    sentinel_payload = {
                        "symbol": symbol,
                        "position_details": safe_pos,
                        "market_data": market_data,
                        "original_thesis": pos.get("original_thesis", "No thesis recorded.")
                    }
                    sentinel_verdict = await self.agents.sentinel.analyze(sentinel_payload)
                    
                    if not hasattr(self, "_pending_sentinel_closes"):
                        self._pending_sentinel_closes = {}
                        
                    if sentinel_verdict.get("decision") == "CLOSE_POSITION":
                        count = self._pending_sentinel_closes.get(symbol, 0) + 1
                        self._pending_sentinel_closes[symbol] = count
                        
                        if count >= 2:
                            self.services.logger.warning(f"🚨 [Sentinel] Thesis invalidated for {symbol}! Triggering early exit (Confirmed).")
                            print(f"🚨 [Sentinel] EARLY EXIT TRIGGERED for {symbol}. Reason: {sentinel_verdict.get('reasoning_en', '')}")
                            await self.services.trading_service.force_close_position(symbol, bypass_check=False)
                            
                            early_exit_msg = (
                                f"🚨 *SENTINEL EARLY EXIT / РАННИЙ ВЫХОД*\n\n"
                                f"🪙 *Asset / Монета:* `{symbol}`\n"
                                f"📝 *Reason / Причина:* `{sentinel_verdict.get('reasoning_en', '')}`\n"
                                f"🛡 *Action:* The original thesis was invalidated. Position closed to prevent further losses."
                            )
                            await self.services.tg_sender.send_message(early_exit_msg)
                            self._pending_sentinel_closes[symbol] = 0
                        else:
                            self.services.logger.info(f"⚠️ [Sentinel] First CLOSE vote for {symbol}. Waiting for confirmation on next tick.")
                            print(f"⚠️ [Sentinel] {symbol} thesis questioned. Waiting for 1 more confirmation.")
                    elif sentinel_verdict.get("decision") == "ERROR":
                        self._pending_sentinel_closes[symbol] = 0
                        self.services.logger.error(f"❌ [Sentinel] SENTINEL_UNAVAILABLE: {sentinel_verdict.get('reasoning_en')}")
                    else:
                        self._pending_sentinel_closes[symbol] = 0
                        self.services.logger.info(f"[Sentinel] Thesis intact for {symbol}.")
                        
                        
            except Exception as e:
                print(f"❌ Ошибка проверки позиции {symbol}: {e}")
