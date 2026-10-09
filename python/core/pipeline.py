import time
import asyncio
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

class TradingPipeline:
    def __init__(self, agents: AgentRegistry, services: ServiceRegistry, exchange_name: str):
        self.agents = agents
        self.services = services
        self.exchange_name = exchange_name
        self.data_guard = DataQualityGuard(self.services.logger)
        self._cycle_running = False
        self._fast_radar_cooldowns: Dict[str, float] = {}
        self._pullback_watchlist: Dict[str, Dict[str, Any]] = {}
        self._priority_symbol: Optional[str] = None
        self._effective_profile = getattr(config, "TRADING_PROFILE", "BALANCED")

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

    async def run_cycle(self, cycle_number: int, force_scan: bool = False, skip_new_trades: bool = False):
        # Fix #5: Prevent overlapping cycles
        if self._cycle_running and not force_scan:
            print("⏸️ [Pipeline] Предыдущий цикл ещё выполняется. Пропуск.")
            return
        self._cycle_running = True
        try:
            await self._run_cycle_inner(cycle_number, force_scan, skip_new_trades)
        finally:
            self._cycle_running = False

    async def _run_cycle_inner(self, cycle_number: int, force_scan: bool = False, skip_new_trades: bool = False):
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

        if hasattr(self.services.trading_service, "recent_streak") and len(self.services.trading_service.recent_streak) >= 3 and self.services.trading_service.recent_streak[-3:] == ["LOSS", "LOSS", "LOSS"]:
            # Only trigger cooldown once per 3-loss streak, preserving streak memory for RiskManager
            if getattr(self.services.trading_service, "_last_cooldown_processed_len", 0) != len(self.services.trading_service.recent_streak):
                self.services.trading_service.cooldown_until = time.time() + 3600
                self.services.trading_service._last_cooldown_processed_len = len(self.services.trading_service.recent_streak)
                if hasattr(self.services.trading_service, "_save_state"):
                    self.services.trading_service._save_state()
                print(f"🛑 [Cooldown Activated] Зафиксировано 3 убытка подряд! Торговля приостановлена на 1 час.")
                self.services.logger.info("[System_Core] 3 consecutive losses detected. 1 hour cooldown activated.")
                if not force_scan:
                    return

        max_positions = getattr(config, "MAX_CONCURRENT_POSITIONS", 2)
        active_pos_count = len(self.services.trading_service.active_positions)
        if active_pos_count >= max_positions and not force_scan:
            active_symbols = ", ".join(self.services.trading_service.active_positions.keys())
            msg = (
                f"⏸️ [Positions Cap] Достигнут лимит открытых позиций ({active_pos_count}/{max_positions}): [{active_symbols}].\n"
                f"Новые сделки заблокированы. Сканирование рынка пропущено (экономия токенов 100%)."
            )
            print(f"\n{msg}\n")
            self.services.logger.info(f"[System_Core] {msg}")
            return

        # СТАДИЯ 0: REGIME DETECTION (Адаптивный профиль риска)
        self.services.logger.info("[Stage 0] Regime Agent определяет фазу рынка (BTC/ETH)...")
        macro_cache = {}
        detected_regime = "RANGE_CHOPPY"
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
            self._effective_profile = profile
            
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

        # QW Quiet Rest disabled: Bot runs 24/7 (48 cycles per day at 30 min intervals)

        # Фильтруем активы: не сканируем то, что уже открыто
        selected_assets = [s for s in selected_assets if s not in self.services.trading_service.active_positions]

        # Приоритет для актива с подтвержденным откатом (PULLBACK_RETEST)
        if getattr(self, "_priority_symbol", None):
            pri_sym = self._priority_symbol
            self._priority_symbol = None
            if pri_sym in selected_assets:
                selected_assets = [pri_sym] + [s for s in selected_assets if s != pri_sym]
                print(f"🎯 [Pullback Priority] {pri_sym} перемещен в начало очереди анализа (PULLBACK_RETEST).")
            elif pri_sym not in self.services.trading_service.active_positions:
                selected_assets = [pri_sym] + selected_assets
                print(f"🎯 [Pullback Priority] {pri_sym} добавлен первым в очередь анализа (PULLBACK_RETEST).")

        # ИТЕРАЦИЯ ПО ВСЕМ АКТИВАМ
        for symbol in selected_assets:
            # QW #5: Check if we've reached the maximum number of concurrent positions
            max_cap = getattr(config, "MAX_CONCURRENT_POSITIONS", 2)
            if len(self.services.trading_service.active_positions) >= max_cap:
                msg = f"⏸️ Достигнут лимит одновременных позиций ({max_cap}). Пропуск новых активов."
                print(msg)
                self.services.logger.info(f"[System_Core] {msg}")
                break

            print(f"\n🔍 ПОЛНЫЙ АНАЛИЗ (15m, 1H, 4H) NADO DEX: {symbol}")
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
                if "min_notional" in limits and limits["min_notional"] > 0:
                    market_data["derivatives_data"]["min_notional"] = limits["min_notional"]
                if "min_size" in limits:
                    market_data["derivatives_data"]["min_size"] = limits["min_size"]

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

            # --- STRATEGY ROUTER ---
            strategy_profile = StrategyRouter.evaluate(
                symbol, market_data, valid_reports, macro_cache,
                detected_regime=detected_regime, profile=profile
            )
            has_directional_signal = strategy_profile.has_directional_signal
            strategy_mode = strategy_profile.strategy_mode
            
            if has_directional_signal:
                self.services.logger.info(f"[System_Core] Strategy Router: {symbol} допущен ({strategy_profile.reasoning})")

            if not has_directional_signal:
                msg = f"⏸️ Пропуск {symbol}. Причина: {strategy_profile.reasoning} Экономим токены."
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
                    "risk_reason": "Pre-CEO Filter: нет направленного консенсуса аналитиков (боковик)",
                    "ceo_reasoning": "Bypassed (Pre-CEO Filter)",
                    "status": "⏸️ НЕТ СИГНАЛА"
                })
                tracker.record_rejection("NO_SIGNAL")
                continue

            # СТАДИЯ 4: MULTI-AGENT DEBATE & CEO JUDGEMENT
            self.services.logger.info(f"[Stage 4] Bull and Bear agents debating on {symbol}...")
            
            clean_mtf = self._clean_mtf_for_llm(market_data.get("multi_timeframe", {}))
            debate_payload = {
                "symbol": symbol,
                "strategy_mode": strategy_mode,
                "multi_timeframe_context": clean_mtf,
                "analyst_reports": valid_reports
            }
            
            # Запускаем Быка и Медведя параллельно
            bull_verdict, bear_verdict = await asyncio.gather(
                self.agents.bull.analyze(debate_payload),
                self.agents.bear.analyze(debate_payload)
            )
            
            bull_summary = str(bull_verdict.get('summary', 'N/A')).strip()
            bear_summary = str(bear_verdict.get('summary', 'N/A')).strip()
            print(f"🐂 Bull Thesis: {bull_summary}")
            print(f"🐻 Bear Thesis: {bear_summary}")
            
            self.services.logger.info(f"[Stage 4] CEO Agent (Judge) evaluates the debate and MTF trend for {symbol}...")
            historical_context = self.agents.memory.get_recent_context(limit=3)
            
            ceo_payload = {
                "symbol": symbol,
                "strategy_mode": strategy_mode,
                "direction_bias": strategy_profile.direction_bias,
                "router_reasoning": strategy_profile.reasoning,
                "macro_regime": detected_regime,
                "macro_profile": profile,
                "multi_timeframe_context": clean_mtf,
                "bull_thesis": bull_verdict,
                "bear_thesis": bear_verdict,
                "subordinate_analyst_reports": valid_reports,
                "historical_context": historical_context,
                "past_lessons_learned": recent_lessons,
                "indicators": market_data.get("indicators", {}),
                "news_data": market_data.get("news_data", {})
            }
            ceo_verdict = await self.agents.ceo.analyze(ceo_payload)

            directional_conf = ceo_verdict.get("directional_confidence", ceo_verdict.get("conviction", 0))
            entry_qual = ceo_verdict.get("entry_quality", ceo_verdict.get("conviction", 0))

            # STAGE 4.5: DETERMINISTIC GUARD -> FINAL TRADE DECISION
            final_trade_decision = self.apply_pipeline_guards_and_sync(
                strategy_profile, ceo_verdict, profile, symbol=symbol, market_data=market_data
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

            # Enrich scan_result with routing info for cycle logs
            scan_result["strategy_mode"] = strategy_mode
            scan_result["direction_bias"] = strategy_profile.direction_bias
            scan_result["router_reasoning"] = strategy_profile.reasoning

            # Execution Gate: Checks if Deterministic Guard approved candidate as actionable
            if not final_trade_decision.is_actionable:
                # Регистрация в Pullback Watchlist для непрерывного фонового отслеживания отката (0 LLM токенов)
                if final_trade_decision.trade_action == "WAIT_FOR_PULLBACK" and final_trade_decision.decision in ["LONG", "SHORT"]:
                    self.register_pullback_candidate(
                        symbol=symbol,
                        decision=final_trade_decision.decision,
                        current_price=current_price,
                        directional_conf=directional_conf,
                        conviction=conviction,
                        strategy_mode=strategy_mode,
                        market_data=market_data,
                        ceo_verdict=ceo_verdict,
                        strategy_profile=strategy_profile
                    )

                print(f"⏸️ Пропуск {symbol}. {final_trade_decision.guard_reason}")
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
                
            self.services.logger.info(f"[Stage 5] Risk Manager ({profile}, Regime: {detected_regime}) проверяет параметры сделки для {symbol}...")
            portfolio_data["active_positions"] = self.services.trading_service.active_positions
            risk_verdict = await self.agents.risk.analyze(
                final_trade_decision, 
                portfolio_data, 
                market_data, 
                effective_profile=profile,
                macro_regime=detected_regime,
                strategy_mode=strategy_mode
            )

            if risk_verdict.get("approved"):
                self.services.logger.info(f"✅ Status: APPROVED BY RISK MANAGER")
                print(f"💰 Position Amount: ${risk_verdict.get('notional_size_usd', 0):,.2f} ({risk_verdict.get('position_size_pct', 0)}% of portfolio)")
                print(f"🟢 Take Profit (TP): ${risk_verdict.get('take_profit_price', 0):,.2f} (+{risk_verdict.get('take_profit_pct', 0)}%)")
                # Автоматическая торговля 24/7 (полностью автономный режим)
                atr_14_val = float(market_data.get("indicators", {}).get("atr_14", 0) or 0)
                trade_success = await self.services.trading_service.open_position(
                    symbol=symbol,
                    direction=final_trade_decision.decision,
                    entry_price=current_price,
                    notional_usd=risk_verdict.get("notional_size_usd", 0),
                    tp_price=risk_verdict.get("take_profit_price", 0),
                    sl_price=risk_verdict.get("stop_loss_price", 0),
                    leverage=risk_verdict.get("leverage", 10),
                    original_thesis=final_trade_decision.reasoning_en,
                    contracts=risk_verdict.get("contracts", 0.0),
                    atr_value=atr_14_val
                )

                execution_status = "SUCCESS" if trade_success else "REJECTED_BY_EXCHANGE"
                execution_result = ExecutionResult(
                    symbol=symbol,
                    status=execution_status,
                    executed_at=time.time(),
                    notional_usd=float(risk_verdict.get("notional_size_usd", 0) or 0),
                    actual_fill_price=float(risk_verdict.get("entry_price", current_price) or current_price)
                )
                if isinstance(risk_verdict, dict):
                    risk_verdict["execution_status"] = execution_status
                elif hasattr(risk_verdict, "execution_status"):
                    import dataclasses
                    risk_verdict = dataclasses.replace(risk_verdict, execution_status=execution_status)

                if not trade_success:
                    print(f"❌ Ошибка открытия позиции на бирже для {symbol}.")
                    self.services.logger.error(f"❌ Status: REJECTED BY EXCHANGE ({symbol})")
                    tracker.record_execution_failed()
                else:
                    tracker.record_trade()
            else:
                self.services.logger.error(f"❌ Status: VETOED BY RISK MANAGER ({risk_verdict.get('reasoning')})")
                veto_cat = risk_verdict.get("veto_category") or "RISK_VETO"
                tracker.record_rejection(veto_cat)
                execution_result = ExecutionResult(
                    symbol=symbol,
                    status="SKIPPED",
                    executed_at=time.time(),
                    error=f"Vetoed by Risk Manager: {veto_cat}"
                )

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
                    "ceo_verdict": final_trade_decision.to_dict(),
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
                "ceo_decision": final_trade_decision.to_dict(),
                "risk_assessment": risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else risk_verdict,
                "execution_result": execution_result.to_dict() if execution_result else None,
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

        tok_stats = LLMClient.get_token_stats()
        print(f"\n=== ТОРГОВЫЙ ЦИКЛ №{cycle_number} УСПЕШНО ЗАВЕРШЕН ===")
        print(f"📊 [РАСХОД ТОКЕНОВ] За цикл: {tok_stats['cycle_tokens']:,} токенов | Всего за сессию: {tok_stats['total_tokens']:,} (Prompt: {tok_stats['total_prompt_tokens']:,}, Output: {tok_stats['total_completion_tokens']:,})")
        self.services.logger.info(f"[Tokens] Cycle #{cycle_number}: {tok_stats['cycle_tokens']:,} | Total Session: {tok_stats['total_tokens']:,}")




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
                    
                    if new_sl:
                        success = await self.services.trading_service.update_stop_loss(symbol, new_sl)
                        if success:
                            pos["protection_state"] = new_state
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

        for symbol in core_symbols:
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

                # 2. Volume Ratio vs recent average
                cur_vol = float(last_c.get("volume", 0.0))
                hist_vols = [float(c.get("volume", 0.0)) for c in candles[-11:-1]]
                avg_vol = sum(hist_vols) / len(hist_vols) if hist_vols else 0.0
                vol_ratio = (cur_vol / avg_vol) if avg_vol > 0 else 1.0

                # 3. Donchian breakdown / breakout
                lows = [float(c.get("low", 0.0)) for c in candles[-21:-1]]
                highs = [float(c.get("high", 0.0)) for c in candles[-21:-1]]
                donchian_low = min(lows) if lows else 0.0
                donchian_high = max(highs) if highs else 0.0

                is_breakdown = (donchian_low > 0 and current_price < donchian_low and delta_pct <= -0.5) or (delta_pct <= -1.2) or (delta_pct <= -0.8 and vol_ratio >= 1.8)
                is_breakout = (donchian_high > 0 and current_price > donchian_high and delta_pct >= 0.5) or (delta_pct >= 1.2) or (delta_pct >= 0.8 and vol_ratio >= 1.8)

                if is_breakdown:
                    self._fast_radar_cooldowns[symbol] = now_ts + 600  # 10m cooldown
                    return True, symbol, f"MOMENTUM_BREAKDOWN (Δ={delta_pct:.2f}%, Vol={vol_ratio:.1f}x)"
                elif is_breakout:
                    self._fast_radar_cooldowns[symbol] = now_ts + 600  # 10m cooldown
                    return True, symbol, f"MOMENTUM_BREAKOUT (Δ={delta_pct:.2f}%, Vol={vol_ratio:.1f}x)"
            except Exception as e:
                self.services.logger.debug(f"[FastRadar] Error checking {symbol}: {e}")

        return False, "", ""

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
        ttl_seconds: int = 2700  # 45 minutes
    ):
        """
        Registers a high-conviction setup where price is temporarily extended from base (WAIT_FOR_PULLBACK).
        Monitors lightweight live price (0 LLM tokens) in background fast radar loop.
        """
        indicators = market_data.get("indicators", {}) if isinstance(market_data, dict) else {}
        ema_20 = float(indicators.get("ema_20") or 0.0)

        # Pullback target calculation
        if decision == "LONG":
            if ema_20 > 0 and ema_20 < current_price:
                target_pullback_price = ema_20
            else:
                target_pullback_price = round(current_price * 0.992, 4)  # ~0.8% pullback
            invalidation_price = round(target_pullback_price * 0.985, 4)  # 1.5% below target breaks setup
        else:  # SHORT
            if ema_20 > 0 and ema_20 > current_price:
                target_pullback_price = ema_20
            else:
                target_pullback_price = round(current_price * 1.008, 4)  # ~0.8% bounce/pullback
            invalidation_price = round(target_pullback_price * 1.015, 4)  # 1.5% above target breaks setup

        now_ts = time.time()
        self._pullback_watchlist[symbol] = {
            "symbol": symbol,
            "direction": decision,
            "signal_price": current_price,
            "target_pullback_price": target_pullback_price,
            "invalidation_price": invalidation_price,
            "directional_conf": directional_conf,
            "conviction": conviction,
            "strategy_mode": strategy_mode,
            "created_at": now_ts,
            "expires_at": now_ts + ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "status": "WATCHING"
        }

        print(f"👁️ [Pullback Watchlist] {symbol} добавлен в мониторинг отката ({decision}): "
              f"Сигнальная цена ${current_price:,.2f} -> Цель отката ~${target_pullback_price:,.2f} "
              f"(Инвалидация: ${invalidation_price:,.2f}, TTL: {int(ttl_seconds/60)} мин)")
        self.services.logger.info(
            f"[PullbackWatchlist] Added {symbol} ({decision}). Signal: {current_price}, "
            f"Target: {target_pullback_price}, Invalidation: {invalidation_price}, TTL: {ttl_seconds}s"
        )

    async def check_pullback_watchlist(self) -> Tuple[bool, str, str]:
        """
        Lightweight continuous pullback monitor (0 LLM tokens).
        Executed every 45-60s in background fast radar loop.
        
        Checks watchlist symbols:
        1. Cancels/expires stale signals (> 45 min).
        2. Cancels signals if price invalidates thesis (breaks invalidation level).
        3. If pullback occurred (price touched pullback target zone), triggers immediate re-test!
        
        Returns: (triggered: bool, symbol: str, reason: str)
        """
        if not self._pullback_watchlist:
            return False, "", ""

        if self._cycle_running:
            return False, "", "Cycle currently running"

        now_ts = time.time()
        symbols_to_delete = []

        for symbol, item in list(self._pullback_watchlist.items()):
            # 1. Stale signal check (TTL Expiration)
            if now_ts >= item["expires_at"]:
                elapsed_min = int((now_ts - item["created_at"]) / 60)
                print(f"🗑️ [Pullback Watchlist] Сигнал {symbol} ({item['direction']}) устарел ({elapsed_min} мин >= {int(item['ttl_seconds']/60)} мин). Удален.")
                self.services.logger.info(f"[PullbackWatchlist] Stale signal expired for {symbol}. Removed.")
                symbols_to_delete.append(symbol)
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

            direction = item["direction"]
            target_price = item["target_pullback_price"]
            inv_price = item["invalidation_price"]

            # 2. Invalidation check
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

            # 3. Pullback check
            # For LONG: price pulled back to target (price <= target * 1.002)
            # For SHORT: price bounced to target (price >= target * 0.998)
            pullback_hit = False
            if direction == "LONG" and current_price <= (target_price * 1.002):
                pullback_hit = True
            elif direction == "SHORT" and current_price >= (target_price * 0.998):
                pullback_hit = True

            if pullback_hit:
                print(f"\n🎯 [Pullback Watchlist] ОТКАТ ПРОИЗОШЕЛ! {symbol} ({direction}): "
                      f"Текущая цена ${current_price:,.2f} достигла зоны отката ~${target_price:,.2f}!")
                self.services.logger.info(f"[PullbackWatchlist] Pullback confirmed for {symbol} ({direction}) at {current_price}. Triggering entry retest.")
                symbols_to_delete.append(symbol)
                self._priority_symbol = symbol
                for s in symbols_to_delete:
                    self._pullback_watchlist.pop(s, None)
                return True, symbol, f"PULLBACK_RETEST ({direction} at ${current_price:,.2f}, target was ${target_price:,.2f})"

        for s in symbols_to_delete:
            self._pullback_watchlist.pop(s, None)

        return False, "", ""
