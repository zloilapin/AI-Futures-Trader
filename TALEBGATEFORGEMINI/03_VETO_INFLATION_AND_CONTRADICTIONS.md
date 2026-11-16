# 03. ИНФЛЯЦИЯ VETO И ВНУТРЕННИЕ ПРОТИВОРЕЧИЯ (VIA NEGATIVA VIOLATIONS)

> **Нарушаемые законы Конституции:**
> **2. VIA NEGATIVA (SUBTRACTION OVER ADDITION)**
> *"Fix by Removing Contradictions: When a pipeline bug or deadlock is identified, prioritize removing redundant legacy overrides, conflicting thresholds, and dead code rather than stacking new heuristic filters or magic numbers on top. Zero New Magic Constants. Early Noise Rejection."*

---

## 1. Реестр 15 видов VETO в Strategy Router (50 точек вызова)

В файле [python/core/strategy_router.py](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/strategy_router.py) обнаружено **50 прямых возвратов VETO**, разделенных на **15 уникальных классов блокировок**:

| # | Имя VETO | Логика срабатывания | Последствие для пайплайна |
|---|---|---|---|
| 1 | `FALLING_KNIFE_VETO` | `retest_dir == "LONG" and (tech_bears >= 3 or tech_bulls < 1)` | Блокирует подбор отката на лонгах, если аналитики видят красную свечу |
| 2 | `ROCKET_RALLY_VETO` | `retest_dir == "SHORT" and (tech_bulls >= 3 or tech_bears < 1)` | Блокирует шорт отката, если свеча отскока зеленая |
| 3 | `TREND_EXHAUSTION_VETO` | `bb_position_pct > 80%` или близость к Donchian High/Low | Блокирует трендовые пробои и ретесты на сильном импульсе |
| 4 | `MACRO_LEADERS_VETO` | `BTC` или `ETH` имеют противоположный 1h тренд | Блокирует любые альткоины, даже если они показывают независимое ралли |
| 5 | `WASH_VOLUME_VETO` | Всплеск объема без изменения OI и с узким телом свечи | Блокирует волатильные пробои |
| 6 | `ANTI_CHASE_REENTRY_VETO` | Повторный вход в ту же сторону в течение короткого окна | Блокирует добор позиции по тренду |
| 7 | `MACRO_BOX_CEILING_VETO` | Цена в верхних 15% 48-часового диапазона | Запрещает покупки у верхней границы диапазона |
| 8 | `MACRO_BOX_FLOOR_VETO` | Цена в нижних 15% 48-часового диапазона | Запрещает продажи у нижней границы диапазона |
| 9 | `LOW_ADX_RETEST_VETO` | `ADX < 20.0` (или `< 22.0` для BTC/ETH) | Блокирует ретест уровней при затухании импульса |
| 10 | `LOW_ADX_REVERSAL_VETO` | `ADX < 20.0` при попытке разворота | Блокирует ранние стадии смены тренда |
| 11 | `PULLBACK_INVALIDATED_VETO` | Пробой структурного уровня инвалидации | Снимает сетап из вотчлиста |
| 12 | `PULLBACK_TREND_OPPOSED_VETO` | Смена тренда 1H против направления ретеста | Блокирует вход по лимитке |
| 13 | `RANGE_CHOPPY_VETO` | `range_score >= 55.0` и `detected_regime == "RANGE_CHOPPY"` | Блокирует трендовые стратегии в боковике |
| 14 | `UNKNOWN_REGIME_VETO` | Отсутствие данных по макро-режиму | Аварийный отказ при сбое телеметрии |
| 15 | `STRUCTURAL_REVERSAL_VETO` | Попытка входа по старому тренду против активного CHoCH | Предотвращает шорты на V-образных разворотах |

---

## 2. Анатомия «Частокола VETO» (Veto Stacking)

В классической количественной архитектуре фильтрация должна строиться по принципу **воронки с ранним отсевом шума (Early Noise Rejection)**:
```text
Рыночный шум / Боковик (0 токенов, 0 вотчлистов)
       ↓
Структурное направление (CHoCH / Кворум аналитиков)
       ↓
Физика риска и CLOB (1R Homogeneity, P*)
```

Вместо этого в системе был построен **частокол параллельных эвристик**:
- Каждый раз, когда в тестах или на реальном счете происходил убыточный трейд, в код добавлялось отдельное специализированное VETO.
- **Пример с `FALLING_KNIFE_VETO` и `ROCKET_RALLY_VETO`:**
  Когда цена откатывает к уровню поддержки для лонга, свечи на 15-минутном графике естественным образом **красные**, а индикаторы краткосрочно смотрят вниз. В этот момент аналитики (`Candle_Agent`, `Indicator_Agent`) фиксируют локальное снижение.
  Срабатывает `FALLING_KNIFE_VETO`: *«Bears=3, Bulls=0 — вход отменен»*.
  Затем свеча закрывается зеленой, откат завершается, цена улетает вверх без нас. На хаях следующей свечи система пытается догнать поезд, но там ее встречает `TREND_EXHAUSTION_VETO` или `ANTI_CHASE_REENTRY_VETO`!

---

## 3. Тотальное подавление альткоинов поводырями BTC/ETH

### Файл: [python/core/pipeline.py#L425-L450](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/pipeline.py#L425-L450)
```python
def _check_macro_leaders_uplift(candidate_direction, symbol, macro_leaders_state):
    base_sym = symbol.split("-")[0].upper()
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
```

### Почему это ломает прибыльность в реальном рынке:
- В сессиях 1 и 2 бот торговал монеты `ZEC`, `LIT`, `HYPE`, которые разворачивались в сильное бычье ралли на собственных катализаторах.
- В то же самое время `BTC` и `ETH` еще несколько часов оставались в вязком медвежьем сползании (запаздывание старших таймфреймов).
- Из-за фильтра `_check_macro_leaders_uplift` и `MACRO_LEADERS_VETO` бот заблокировал **более 30 идеальных лонгов по альтам**, потому что «поводырь ETH был медвежьим».
- Но как только на альте появлялась первая слабая красная свеча, бот тут же открывал шорт (ведь поводырь-то медвежий!) и немедленно получал стоп-аут об продолжающееся ралли альта.

---

## 4. Война модулей: `COHORT_D_VETO` vs накрутка Conviction в `CEO_Agent`

### Файл 1: [python/agents/risk_manager.py#L1830-L1850](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1830-L1850)
`RiskManager` вводит эвристику «Когорт»:
```python
if conviction < min_conviction + 10 and trade_action == "REDUCE_SIZE":
    cohort = "D"
if block_cohort_d and cohort == "D":
    return self._build_veto_result("COHORT_D_NEGATIVE_EXPECTANCY", msg, "HOLD")
```

### Файл 2: [python/agents/ceo_agent.py#L374, L451](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/ceo_agent.py#L374)
Видя, что `RiskManager` рубит сделки с тегом `COHORT_D_NEGATIVE_EXPECTANCY`, разработчики вносят патч в `ceo_agent.py`:
```python
# 3. Уверенный REDUCE_SIZE (защита от триггера COHORT_D_NEGATIVE_EXPECTANCY в Risk Manager)
# Сохраняем оригинальный conviction для Risk Manager (Cohort D check)
```
- Вместо того чтобы удалить избыточное вето когорт и полагаться на формулу математического ожидания, один агент начинает специально «подкручивать» свои выходные поля, чтобы обмануть проверку другого агента!

