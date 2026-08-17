import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from dotenv import load_dotenv

# Fix for Windows console UnicodeEncodeError
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv(override=True)

# --- CORE IMPORTS ---
from core.logger import TradeLogger
from core.llm_client import LLMClient
from core.config import config

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
from core.diagnostics import tracker
from agents.telegram_agent import TelegramAgent
from agents.memory_manager import MemoryManager
from agents.reflector_agent import ReflectorAgent

# --- SERVICE IMPORTS ---
from services.market_data_service import MarketDataService
from services.telegram_service import TelegramService
from services.paper_trading_service import PaperTradingService
from services.kraken_trading_service import KrakenTradingService
from services.telegram_bot_listener import TelegramBotListener


def get_msk_status() -> tuple[bool, str]:
    """
    Checks if current time in MSK (UTC+3) is within quiet rest window (19:10 - 07:00 MSK).
    Returns (is_rest_period, current_msk_formatted_time).
    """
    offset = config.TIMEZONE_OFFSET
    msk_tz = timezone(timedelta(hours=offset))
    now_msk = datetime.now(msk_tz)
    
    start_parts = config.REST_START_TIME.split(":")
    end_parts = config.REST_END_TIME.split(":")
    
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
    profile = config.TRADING_PROFILE
    exchange_name = "Kraken Futures"
    
    print(f"\n" + "="*65)
    print(f"🚀 ТОРГОВЫЙ ЦИКЛ №{cycle_number} ({exchange_name}) [{time_str}]")
    print(f"⚙️ ПРОФИЛЬ РИСКА: {profile} | 📐 MULTI-TIMEFRAME (15m + 1H + 4H) АКТИВЕН")
    print("="*65)

    if is_rest and not force_scan:
        print(f"🌙 [Schedule] НОЧНОЙ РЕЖИМ СКАНИРОВАНИЯ. Сейчас {time_str} (с 19:00 до 07:00 МСК).")

    if force_scan and is_rest:
        print(f"⚡ [ForceScan] Ручной запуск /scan во время отдыха ({time_str}). Пропуск тихого режима!")

    # СТАДИЯ 1: UNIVERSE (Выбор активов)
    logger.info("[Stage 1] Universe Agent сканирует DEX на наличие ликвидных активов...")
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

    # Очищаем от дубликатов с сохранением приоритета от LLM
    selected_assets = list(dict.fromkeys(selected_assets))

    valid_symbols = {p["symbol"] for p in active_perps}
    selected_assets = [s for s in selected_assets if s in valid_symbols]

    logger.info(f"🎯 Отобраны уникальные валидные активы для сканирования: {', '.join(selected_assets)}")
    portfolio_data = await trading_service.get_portfolio_summary()

    recent_lessons = reflector_agent.get_lessons(limit=5)
    if recent_lessons:
        print(f"🧠 [Memory] Загружено {len(recent_lessons)} уроков из прошлых сделок.")

    any_signal_sent = False
    scan_summaries = []  # Сводка по каждому активу для отчёта ручного /scan

    # СТАДИЯ 1.5: ПРОВЕРКА УЖЕ ОТКРЫТЫХ ПОЗИЦИЙ (TP/SL)
    if hasattr(trading_service, "sync_with_exchange"):
        logger.info("[Stage 1.5] Синхронизация состояний позиций с биржей...")
        await trading_service.sync_with_exchange()

    active_symbols = list(trading_service.active_positions.keys())
    if active_symbols:
        logger.info("[Stage 1.5] Keeper проверяет TP/SL для открытых позиций...")
    for symbol in active_symbols:
        try:
            market_data = await fetcher.fetch_all_market_data(symbol)
            current_price = market_data.get("price_data", {}).get("current_price", 0)
            
            closed_reports = await trading_service.check_and_update_positions(symbol, current_price)
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
                await tg_sender.send_message(closed_msg)
                await tg_sender.broadcast_to_channel(closed_msg)
                asyncio.create_task(safe_reflect(closed, market_data))
        except Exception as e:
            print(f"❌ Ошибка проверки позиции {symbol}: {e}")

    # QW Quiet Rest: Выход из цикла, если сейчас тихий час и нет force_scan
    if is_rest and not force_scan:
        print(f"⏸️ [Schedule] Тихий час. Позиции проверены. Пропуск новых сделок.")
        return

    # Фильтруем активы: не сканируем то, что уже открыто
    selected_assets = [s for s in selected_assets if s not in trading_service.active_positions]

    # ИТЕРАЦИЯ ПО ВСЕМ АКТИВАМ
    for symbol in selected_assets:
        # QW #5: Check if we've reached the maximum number of concurrent positions
        if len(trading_service.active_positions) >= getattr(config, "MAX_CONCURRENT_POSITIONS", 2):
            msg = f"⏸️ Достигнут лимит одновременных позиций ({config.MAX_CONCURRENT_POSITIONS}). Пропуск новых активов."
            print(msg)
            logger.info(f"[System_Core] {msg}")
            break

        print(f"\n🔍 ПОЛНЫЙ АНАЛИЗ (15m, 1H, 4H) KRAKEN FUTURES: {symbol}")
        tracker.record_scan()
        
        # СТАДИЯ 2: СБОР ДАННЫХ И ПРОВЕРКА СТАТУСА
        logger.info(f"[Stage 2] Сбор мульти-таймфреймовых данных (15m, 1H, 4H) для {symbol}...")
        try:
            market_data = await fetcher.fetch_all_market_data(symbol)
        except Exception as e:
            print(f"❌ Ошибка загрузки данных для {symbol}: {e}. Пропуск актива.")
            scan_summaries.append({"symbol": symbol, "status": "⛔ ОШИБКА ДАННЫХ", "reason": str(e)})
            tracker.record_rejection("FETCH_ERROR")
            continue
            
        current_price = market_data.get("price_data", {}).get("current_price", 0)

        # 2.5 Scanner Agent (ATR Volatility & Spread Guard)
        logger.info(f"[Stage 2.5] Scanner Agent проверяет ATR волатильность и спред для {symbol}...")
        scan_result = await scanner_agent.analyze(market_data)

        scanner_blocked = not scan_result.get("proceed", True)
        scanner_reason = scan_result.get('reasoning', 'Неизвестная причина')
        if scanner_blocked:
            print(f"🛑 Scanner Agent пропустил {symbol}. Причина: {scanner_reason}")
            logger.info(f"[System_Core] Пропуск {symbol}. Причина: {scanner_reason}")
            scan_summaries.append({"symbol": symbol, "status": "⛔ СКАН ЗАБЛОКИРОВАН", "reason": scanner_reason})
            tracker.record_rejection(scan_result.get('status', 'SCANNER_BLOCKED'))
            continue

        # СТАДИЯ 3: СИНДИКАТ АНАЛИТИКОВ
        logger.info(f"[Stage 3] Запуск синдиката аналитиков для {symbol} (Concurrent)...")
        analyst_list = [candle_agent, ob_agent, oi_agent, news_agent, indicator_agent]
        
        # QW Concurrency: Запуск аналитиков параллельно
        reports = await asyncio.gather(*[agent.analyze(market_data) for agent in analyst_list], return_exceptions=True)
        
        # Tag each report with agent_name for the deterministic CEO voting engine
        valid_reports = []
        for agent, report in zip(analyst_list, reports):
            if isinstance(report, dict) and report.get("signal") != "ERROR":
                report["agent_name"] = agent.name
                valid_reports.append(report)

        # --- PRE-CEO FILTER ---
        # Экономим токены Llama 70B: если все базовые агенты нейтральны, пропускаем актив
        has_directional_signal = False
        for report in valid_reports:
            signal = str(report.get("signal", "NEUTRAL")).upper()
            if signal in ["BULLISH", "BEARISH", "LONG", "SHORT"]:
                has_directional_signal = True
                break
                
        if not has_directional_signal:
            msg = f"⏸️ Пропуск {symbol}. Причина: Нет базовых сигналов (Pre-CEO Filter)."
            print(msg)
            logger.info(f"[System_Core] {msg}")
            
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

        # СТАДИЯ 4: СИНТЕЗ CEO И МУЛЬТИ-ТАЙМФРЕЙМ ТРЕНД
        logger.info(f"[Stage 4] CEO Agent проверяет согласованность 1H/4H тренда и выносит решение по {symbol}...")
        historical_context = memory_manager.get_recent_context(limit=3)
        ceo_payload = {
            "symbol": symbol,
            "multi_timeframe": market_data.get("multi_timeframe", {}),
            "raw_market_data": {k: v for k, v in market_data.items() if k not in ["multi_timeframe", "price_data"]},
            "analyst_reports": valid_reports,
            "historical_context": historical_context,
            "past_lessons_learned": recent_lessons
        }
        ceo_verdict = await ceo_agent.analyze(ceo_payload)
        
        decision = str(ceo_verdict.get("decision", "HOLD")).upper()
        conviction = ceo_verdict.get("conviction", 0)
        
        print(f"⚖️ Решение CEO [{symbol}]: {decision} (Уверенность: {conviction}%)")

        min_conv = 70 if profile == "AGGRESSIVE" else (85 if profile == "CONSERVATIVE" else 80)
        
        if decision not in ["LONG", "SHORT"] or conviction < min_conv:
            print(f"⏸️ Пропуск {symbol}. Решение: {decision}, Уверенность: {conviction}% (Требуется LONG/SHORT и >= {min_conv}%).")
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

        # СТАДИЯ 5: РИСК-МЕНЕДЖМЕНТ
        logger.info(f"[Stage 5] Risk Manager ({profile}) проверяет параметры сделки для {symbol}...")
        risk_verdict = await risk_manager.analyze(ceo_verdict, portfolio_data, market_data)

        if risk_verdict.get("approved"):
            logger.info(f"✅ Status: APPROVED BY RISK MANAGER")
            print(f"💰 Position Amount: ${risk_verdict.get('notional_size_usd', 0):,.2f} ({risk_verdict.get('position_size_pct', 0)}% of portfolio)")
            print(f"🟢 Take Profit (TP): ${risk_verdict.get('take_profit_price', 0):,.2f} (+{risk_verdict.get('take_profit_pct', 0)}%)")
            # Автоматическая торговля 24/7 (полностью автономный режим)
            trade_success = await trading_service.open_position(
                symbol=symbol,
                direction=decision,
                entry_price=current_price,
                notional_usd=risk_verdict.get("notional_size_usd", 0),
                tp_price=risk_verdict.get("take_profit_price", 0),
                sl_price=risk_verdict.get("stop_loss_price", 0),
                leverage=config.LEVERAGE
            )
            
            if not trade_success:
                print(f"❌ Ошибка открытия позиции на бирже для {symbol}.")
                logger.error(f"❌ Status: REJECTED BY EXCHANGE ({symbol})")
                risk_verdict["approved"] = False
                risk_verdict["reasoning"] = "Биржа отклонила ордер (или ошибка сети/дубликат)."
                tracker.record_execution_failed()
            else:
                tracker.record_trade()
        else:
            logger.error(f"❌ Status: VETOED BY RISK MANAGER ({risk_verdict.get('reasoning')})")
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
            tg_response = await telegram_agent.analyze(final_trade_data)
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
                if hasattr(trading_service, "pending_trades") and pending_id in trading_service.pending_trades:
                    trading_service.pending_trades[pending_id]["tg_message"] = tg_message
            
            await tg_sender.send_message(tg_message, reply_markup=reply_markup)
            
            # Если сделка не требует ручного подтверждения (дневной режим), транслируем сразу
            if not pending_id:
                await tg_sender.broadcast_to_channel(tg_message)
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
        memory_manager.save_cycle(cycle_record)
        logger.info(f"[System_Core] Торговый цикл успешно завершен для {symbol}.")
        
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
        await tg_sender.send_message(scan_reply, parse_mode="")

    print(f"\n=== ТОРГОВЫЙ ЦИКЛ №{cycle_number} УСПЕШНО ЗАВЕРШЕН ===")


async def main():
    print("=== INITIALIZING NADO AI TRADING SYSTEM ===")
    
    logger = TradeLogger()
    
    # 4-Tier Architecture LLMs (ALL via OpenRouter)
    cheap_llm_client = LLMClient(provider="openrouter", model_name="meta-llama/llama-3.1-8b-instruct")
    primary_ceo_llm = LLMClient(provider="openrouter", model_name="meta-llama/llama-3.3-70b-instruct")
    escalation_ceo_llm = LLMClient(provider="openrouter", model_name="moonshotai/kimi-k3")
    fetcher = MarketDataService(exchange_name="Kraken Futures", logger=logger)
    tg_sender = TelegramService()
    print("Инициализация сервисов...")
    
    trading_engine = config.TRADING_ENGINE
    
    if trading_engine == "PAPER" and not config.LIVE_TRADING_ENABLED:
        from services.paper_trading_service import PaperTradingService
        trading_service = PaperTradingService(logger=logger)
        exchange_name = "Paper Trading"
    else:
        from services.kraken_trading_service import KrakenTradingService
        trading_service = KrakenTradingService()
        exchange_name = "Kraken Futures"
    
    # P0.3: Startup sync — rebuild state from Kraken BEFORE any trading logic
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
    scan_interval_min = config.SCAN_INTERVAL_MINUTES
    interval_seconds = scan_interval_min * 60

    cycle_number = 1

    try:
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
            profile = config.TRADING_PROFILE
            print(f"⏰ Режим автономного сканирования активирован! [Текущее время: {time_str}]")
            print(f"🔄 Интервал сканирования: Круглосуточно каждые {scan_interval_min} мин.")
            print(f"⚙️ Профиль риска: {profile} | 📐 Multi-Timeframe (15m + 1H + 4H) ON")
            print("💡 Для остановки нажмите Ctrl+C в любой момент.\n")
            
            asyncio.create_task(bot_listener.start_listening())

            try:
                while True:
                    try:
                        await run_single_cycle(
                            cycle_number, logger, fetcher, tg_sender, trading_service,
                            universe_agent, scanner_agent, candle_agent, ob_agent,
                            oi_agent, news_agent, indicator_agent, ceo_agent,
                            risk_manager, telegram_agent, reflector_agent, memory_manager
                        )
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
        if 'llm_client' in locals() and hasattr(llm_client, "close"):
            await llm_client.close()
            
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

if __name__ == "__main__":
    asyncio.run(main())
