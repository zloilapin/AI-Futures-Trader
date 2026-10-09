from typing import Dict, Any, Optional
from core.models import FinalTradeDecision
from core.strategy_router import StrategyProfile

class DeterministicGuard:
    """
    Deterministic Guard:
    Centralized canonical guard enforcing the immutable trading contract:
    
    StrategyRouter (strategy_mode, direction_bias)
        ↓
    CEO Agent (raw proposal: decision, conviction, trade_action)
        ↓
    Deterministic Guard
        ↓
    FINAL TRADE DECISION (immutable, single source of truth)
        ↓
    RiskManager (evaluates capital & risk: APPROVE or VETO only, never mutates decision)
        ↓
    Execution Gate (executes strictly if approved)
    """

    @staticmethod
    def evaluate(
        strategy_profile: StrategyProfile,
        ceo_proposal: Dict[str, Any],
        profile: str,
        risk_manager: Any,
        market_data: Optional[Dict[str, Any]] = None,
        symbol: str = "",
        logger: Optional[Any] = None
    ) -> FinalTradeDecision:
        strategy_mode = strategy_profile.strategy_mode
        direction_bias = strategy_profile.direction_bias

        raw_decision = str(ceo_proposal.get("decision", "HOLD")).upper()
        raw_conviction = int(ceo_proposal.get("conviction", 0) or 0)
        raw_trade_action = str(ceo_proposal.get("trade_action", "ENTER" if raw_conviction >= 70 else "HOLD")).upper()
        dir_conf = int(ceo_proposal.get("directional_confidence", raw_conviction) or 0)
        entry_qual = int(ceo_proposal.get("entry_quality", raw_conviction) or 0)
        reasoning_en = ceo_proposal.get("reasoning_en", ceo_proposal.get("reasoning", ""))
        reasoning_ru = ceo_proposal.get("reasoning_ru", "")

        # Get deterministic min_conviction threshold for this profile & strategy mode
        profile_rules = risk_manager._get_profile_rules(profile, strategy_mode)
        min_conv = profile_rules["min_conviction"]

        decision = raw_decision
        conviction = raw_conviction
        trade_action = raw_trade_action
        guard_status = "PASSED"
        guard_reason = "Passed deterministic guard."
        rejection_tag = None

        # 1. Direction Guard: Never allow CEO to reverse specialized setup bias (Breakout / Mean Reversion)
        if strategy_mode in ["BREAKOUT", "MEAN_REVERSION"] and decision in ["LONG", "SHORT"]:
            if decision != direction_bias:
                guard_msg = f"🛡️ [Strategy Guard] Решение CEO {decision} противоречит {strategy_mode} bias ({direction_bias}). Сделка отменена в HOLD для защиты капитала."
                print(guard_msg)
                if logger:
                    logger.warning(f"[Pipeline] {guard_msg}")
                decision = "HOLD"
                conviction = 0
                trade_action = "HOLD"
                guard_status = "BLOCKED"
                rejection_tag = "STRATEGY_GUARD_VETO"
                guard_reason = guard_msg
                reasoning_en = f"{reasoning_en}\n\n{guard_msg}".strip()

        # 2. Volatility Momentum & Breakout: Confirmed impulse breakdown/breakout enters immediately
        # Only true if market indicators genuinely confirm a breakout with volume spike;
        # strategy name alone NEVER justifies overriding WAIT_FOR_PULLBACK.
        is_confirmed_breakout = False
        breakout_reason = ""
        if market_data and decision in ["LONG", "SHORT"]:
            indicators = market_data.get("indicators", {})
            donchian_high = float(indicators.get("donchian_high") or 0.0)
            donchian_low = float(indicators.get("donchian_low") or 0.0)
            vol_spike = float(indicators.get("volume_spike_pct") or 0.0)
            cur_price = float(market_data.get("price_data", {}).get("current_price") or 0.0)
            if decision == "LONG" and donchian_high > 0 and cur_price > donchian_high and vol_spike >= 120.0:
                is_confirmed_breakout = True
                breakout_reason = f"пробой Donchian High ({donchian_high}) с объемом {vol_spike:.1f}%"
            elif decision == "SHORT" and donchian_low > 0 and cur_price < donchian_low and vol_spike >= 100.0:
                is_confirmed_breakout = True
                breakout_reason = f"пробой Donchian Low ({donchian_low}) с объемом {vol_spike:.1f}%"

        if is_confirmed_breakout and trade_action == "WAIT_FOR_PULLBACK" and conviction >= min_conv and decision in ["LONG", "SHORT"]:
            trade_action = "ENTER"
            vm_msg = f"Подтвержденный импульс и пробой ({breakout_reason}). Вход по стратегии {strategy_mode} без ожидания отката."
            try:
                print(f"⚡ [Breakout Override] {symbol}: {vm_msg}")
            except (UnicodeEncodeError, Exception):
                try:
                    print(f"[Breakout Override] {symbol}: {vm_msg.encode('ascii', errors='replace').decode('ascii')}")
                except Exception:
                    pass
            if logger:
                logger.info(f"[System_Core] {vm_msg}")

        # 2.5. Disputed Arbitration Guard: Never allow full ENTER on disputed setups
        if ceo_proposal.get("disputed_arbitration") and trade_action == "ENTER":
            trade_action = "REDUCE_SIZE"
            da_msg = "⚖️ [Disputed Arbitration] Размер позиции ограничен REDUCE_SIZE из-за арбитражного спора."
            if logger:
                logger.info(f"[System_Core] {da_msg}")

        # 3. Actionability, Conviction & Pullback Checks
        is_actionable = True
        if decision not in ["LONG", "SHORT"]:
            is_actionable = False
            guard_status = "HOLD" if not rejection_tag else "BLOCKED"
            rejection_tag = rejection_tag or ceo_proposal.get("hold_category", "CEO_HOLD")
            guard_reason = f"Пропущен из-за решения CEO ({decision})" if not rejection_tag or rejection_tag == "CEO_HOLD" else guard_reason
        elif trade_action == "WAIT_FOR_PULLBACK":
            is_actionable = False
            guard_status = "PULLBACK_WATCHLIST"
            rejection_tag = "WAIT_FOR_PULLBACK"
            guard_reason = f"WAIT_FOR_PULLBACK: Цена оторвана от базы (Качество входа {entry_qual}% < 70% при уверенности тренда {dir_conf}%). Сетап зарегистрирован в Pullback Watchlist для непрерывного мониторинга отката."
        elif entry_qual < 60:
            # Rule: Не снижать порог с 60% просто ради увеличения числа сделок.
            # Если вход запоздал, правильное действие — дождаться нового сетапа или пропустить сделку, а не принудительно открыть позицию.
            is_actionable = False
            guard_status = "BLOCKED"
            rejection_tag = "POOR_ENTRY_QUALITY"
            guard_reason = f"Низкое качество входа: Entry Quality ({entry_qual}% < 60%). Вход запоздал или несет невыгодный Risk/Reward. Принудительное открытие запрещено."
        elif conviction < min_conv:
            is_actionable = False
            guard_status = "BLOCKED"
            rejection_tag = "LOW_CONFIDENCE"
            guard_reason = f"Пропущен из-за фильтра CEO (Уверенность {conviction}% < {min_conv}%)"

        # 4. Funding Rate Gate (Deterministic derivative protection)
        if is_actionable and market_data:
            funding_rate = float(market_data.get("derivatives_data", {}).get("funding_rate") or 0.0)
            if decision == "LONG" and funding_rate > 0.0005: # > 0.05%
                is_actionable = False
                guard_status = "BLOCKED"
                rejection_tag = "FUNDING_VETO"
                guard_reason = f"Фандинг гейт: Запрет LONG при экстремально положительном фандинге ({funding_rate*100:.3f}%)"
                print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                if logger:
                    logger.info(f"[System_Core] {guard_reason}")
            elif decision == "SHORT" and funding_rate < -0.0005: # < -0.05%
                is_actionable = False
                guard_status = "BLOCKED"
                rejection_tag = "FUNDING_VETO"
                guard_reason = f"Фандинг гейт: Запрет SHORT при экстремально отрицательном фандинге ({funding_rate*100:.3f}%)"
                print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                if logger:
                    logger.info(f"[System_Core] {guard_reason}")

        # 5. Anti-Whipsaw & Extreme Extension Guard (Prevent 'Shorting the Bottom' / 'Longing the Top')
        if is_actionable and market_data and strategy_mode == "TREND_FOLLOWING":
            indicators = market_data.get("indicators", {})
            rsi = float(indicators.get("rsi_14") or 50.0)
            bb_pos = float(indicators.get("bb_pos") or 50.0)
            
            if decision == "SHORT":
                if rsi < 35.0 or bb_pos < 10.0:
                    is_actionable = False
                    guard_status = "BLOCKED"
                    rejection_tag = "EXTREME_EXTENSION_VETO"
                    guard_reason = f"Анти-распил: Запрет SHORT в перепроданный рынок (RSI={rsi:.1f}, bb_pos={bb_pos:.1f}%). Риск отскока слишком высок."
                    try:
                        print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                    except UnicodeEncodeError:
                        pass
                    if logger:
                        logger.info(f"[System_Core] {guard_reason}")
                        
            elif decision == "LONG":
                if rsi > 65.0 or bb_pos > 90.0:
                    is_actionable = False
                    guard_status = "BLOCKED"
                    rejection_tag = "EXTREME_EXTENSION_VETO"
                    guard_reason = f"Анти-распил: Запрет LONG в перекупленный рынок (RSI={rsi:.1f}, bb_pos={bb_pos:.1f}%). Риск коррекции слишком высок."
                    try:
                        print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                    except UnicodeEncodeError:
                        pass
                    if logger:
                        logger.info(f"[System_Core] {guard_reason}")

        # Synchronize back to ceo_proposal dict for backward compatibility
        if symbol:
            ceo_proposal["symbol"] = symbol
        ceo_proposal["decision"] = decision
        ceo_proposal["conviction"] = conviction
        ceo_proposal["trade_action"] = trade_action
        ceo_proposal["is_actionable"] = is_actionable
        ceo_proposal["guard_status"] = guard_status
        ceo_proposal["reasoning_en"] = reasoning_en
        if rejection_tag:
            ceo_proposal["rejection_tag"] = rejection_tag
            if decision == "HOLD":
                ceo_proposal["hold_category"] = rejection_tag

        return FinalTradeDecision(
            symbol=symbol,
            decision=decision,
            conviction=conviction,
            trade_action=trade_action,
            strategy_mode=strategy_mode,
            direction_bias=direction_bias,
            min_conviction=min_conv,
            is_actionable=is_actionable,
            guard_status=guard_status,
            guard_reason=guard_reason,
            rejection_tag=rejection_tag,
            directional_confidence=dir_conf,
            entry_quality=entry_qual,
            reasoning_en=reasoning_en,
            reasoning_ru=reasoning_ru,
            raw_ceo_verdict=ceo_proposal
        )
