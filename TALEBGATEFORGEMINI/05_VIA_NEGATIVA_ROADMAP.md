# 05. ДОРОЖНАЯ КАРТА ОЧИЩЕНИЯ СИСТЕМЫ (VIA NEGATIVA ROADMAP)

> **Принцип Quantix AI:**
> *"The art of quantitative architecture is subtraction, not addition. Removing a contradictory filter increases Sharpe ratio faster than adding three new indicators."*

---

## ФАЗА 1: ЛИКВИДАЦИЯ КРИТИЧЕСКИХ ДЕФЕКТОВ МАСШТАБА (P0)

### 1. Универсальный расчет 1R риска без долларовых замков
- **Где:** [python/agents/risk_manager.py#L240-L265](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L240-L265), [python/services/nado_trading_service.py#L938](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/services/nado_trading_service.py#L938).
- **Проблема:** Хардкод `min(0.015 * eq, 1.35)` и `min(0.0215 * eq, 2.00)` превращает бота в игрушку только для счетов ~$87. Любой баланс выше $100 блокируется или теряет эффект сложного процента.
- **Решение:** 
  $$\text{Target Risk USD} = \text{Equity} \times \text{RiskPct}$$
  где `RiskPct` определяется профилем (например, $1.5\%$ или $2.0\%$).
  Убрать жесткие константы `$1.35` и `$2.00`. Ограничение снизу диктуется исключительно минимальным ордером биржи ($N_{\min} \times \text{SL\_distance}$).

---

## ФАЗА 2: УСТРАНЕНИЕ ДИСКРИМИНАЦИИ МОНЕТ (ASSET INVARIANCE) (P1)

### 2. Замена Tier 1 / 2 / 3 коридоров на относительный ATR
- **Где:** [python/agents/risk_manager.py#L1353-L1365](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1353-L1365).
- **Проблема:** Ручные процентные рамки для `BTC/ETH`, `SOL/BNB`, `ZEC/HYPE/LIT`.
- **Решение:** Дистанция стопа определяется рыночной структурой ($S$), а динамический потолок шума калибруется через нормализованный $ATR_{1h\%}$:
  $$\text{Max SL Distance} = k_{\text{vol}} \times ATR_{1h\%}$$
  без оглядки на то, как называется тикер.

### 3. Динамическое чтение спецификаций контрактов Nado CLOB
- **Где:** [python/agents/risk_manager.py#L2251](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L2251).
- **Проблема:** Хардкод словаря `NADO_SIZE_INCREMENTS = {"LIT-USD": 1.0, ...}`.
- **Решение:** Получать `size_increment`, `price_increment` и `min_notional` исключительно из кэша метаданных биржи `market_data["derivatives_data"]`.

---

## ФАЗА 3: СВЕРТЫВАНИЕ ЧАСТОКОЛА VETO (SUBTRACTION) (P2)

### 4. Сокращение 15 типов VETO до 3 физических инвариантов
Вместо 15 разрозненных эвристик в `strategy_router.py` архитектура должна свестись к трем строгим физическим барьерам:

1. **Pre-Debate Gate (Фильтр плоского шума):**
   - Если рынок в глухом боковике (`ADX < 18` или `RangeScore > 65` на старшем таймфрейме) — 0 токенов, 0 вотчлистов, мгновенный возврат `HOLD`.
2. **Structural Shift Gate (Направление структуры):**
   - Направление сделки определяется подтвержденным сломом структуры (`CHoCH` / кворум технических аналитиков).
   - Устранить `FALLING_KNIFE_VETO` и `ROCKET_RALLY_VETO`: цвет единичной минутной свечи при ретесте уровня не должен отменять часовой сетап.
3. **Execution Physics Gate ($P^*$ Solver):**
   - Не сжимать стоп руками. Если текущая цена не дает войти в рамках 1R — решать уравнение $P^*$ и выставлять лимитку в зону отката, либо отбрасывать сетап.

### 5. Отвязка альткоинов от слепого вето поводырей
- **Где:** [python/core/pipeline.py#L425](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/pipeline.py#L425).
- **Проблема:** Медвежий BTC блокирует любые бычьи развороты на альтах.
- **Решение:** Использовать состояние BTC/ETH как модификатор сайзинга (коэффициент осторожности $0.8x$), но не как безусловное бинарное вето для монет с подтвержденным собственным импульсом и объемом.

---

## ФАЗА 4: ОЧИСТКА ОТ СИНТЕТИЧЕСКИХ ОВЕРРАЙДОВ (P3)

### 6. Удаление зависимости от `tuned_hyperparams.json`
- Зафиксировать фундаментальные микроструктурные константы в коде конфигурации.
- Запретить автоматический перезапуск байесовского тюнинга на выборках менее 500 подтвержденных трейдов.

### 7. Ликвидация «Anti-Bot Predator Mode» и обходов дебатов
- Убрать фиктивные обходы проверок в `deterministic_guard.py` и удвоенные тайм-стопы в `sentinel_agent.py`.
- Любая стратегия возврата к средней (`MEAN_REVERSION`) должна подчиняться тем же правилам риск-контроля и защиты от выноса, что и трендовые стратегии.

### 8. Удаление ручных оверрайдов скоринга в `CEO_Agent`
- Удалить `apply_breakout_penalty_override` (ручной срез штрафов до -5/-10).
- Удалить ветку `if pure unit test with capped LLM breakdown` в `_calculate_categorized_risk_penalties`.

