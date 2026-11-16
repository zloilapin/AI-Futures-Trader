# 01. ПОДГОНКА ПОД ИСТОРИЮ И ИЛЛЮЗИЯ ВЫЖИВШЕГО (HINDSIGHT & CURVE-FITTING)

> **Нарушаемый закон Конституции:**
> **1. THE PRIME DIRECTIVE: ZERO CURVE-FITTING (ANTI-HINDSIGHT LAW)**
> *"Never modify numerical thresholds merely to make a specific historical trade from recent logs pass or fail. Fitting parameters to past logs is buying a lottery ticket with yesterday's winning numbers (Taleb's Turkey Problem)."*

---

## 1. Прямая подгонка пороговых констант под номера циклов

### Файл: [python/agents/ceo_agent.py](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/ceo_agent.py#L324-L345)
В коде боевого агента `CEO_Agent` прямо прописаны ссылки на исторические циклы:

```python
class CEOEscalationPatch:
    # Пороги прямого доверия Primary CEO (qwen-plus / qwen-2.5-72b) без вызова 8B-арбитра
    DIRECT_ENTER_MIN_CONF: float = 75.0
    DIRECT_ENTER_MIN_QUAL: float = 70.0   # Было 80.0 — из-за этого сливались сделки с Qual=75% (#35, #42, #45)
    DIRECT_REDUCE_MIN_CONF: float = 75.0
    DIRECT_REDUCE_MIN_QUAL: float = 62.0

    # 2. Чистый сигнал ENTER от старшей модели (Циклы #35, #42, #45 больше не блокируются)
    if (
        action == "ENTER"
        and dir_conf >= cls.DIRECT_ENTER_MIN_CONF
        and entry_qual >= cls.DIRECT_ENTER_MIN_QUAL
    ):
        ...
```

А также в строке 862 того же файла:
```python
# Step 4: Validate arbiter math (FIX FOR CYCLES #12, #43-#47)
if esc_score_res.get("math_conflict", False) or not CEOEscalationPatch.validate_arbiter_math(arb_eval_dict):
    self.logger.warning(...)
```

### Анатомия дефекта:
1. В циклах #35, #42, #45 бот пропустил несколько потенциально прибыльных импульсов, потому что их качество входа оценивалось в 75%, а порог стоял 80%.
2. Вместо того чтобы проанализировать, почему формула скоринга занижала оценку (например, из-за мультиколлинеарности индикаторов), разработчики просто сдвинули порог: `80.0 -> 70.0`, чтобы прошлые сделки прошли постфактум.
3. Это типичное решение в духе «подгонки под ответ»: завтра на рынке возникнет ситуация с качеством 68%, и порог придется опускать до 65%, затем до 60%, пока фильтр полностью не деградирует.

---

## 2. Адаптация кода боевого агента под искусственные юнит-тесты

### Файл: [python/agents/ceo_agent.py](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/ceo_agent.py#L1545-L1552)

В функции вычисления категоризованных штрафов риска боевой агент содержит специальную ветку, написанную исключительно ради прохождения синтетического теста:

```python
# Cap cumulative penalties:
# If pure unit test with capped LLM breakdown (-30 cap) and no market context:
max_allowed_penalty = -10 if is_pullback_retest else (-15 if is_reversal else (-30 if (not market_context and llm_penalty <= -30) else -40))
total_penalty = max(max_allowed_penalty, final_penalty)
```

### Анатомия дефекта:
- Продовый код содержит условие `not market_context and llm_penalty <= -30`, которое применяется только тогда, когда запускается мок-тест.
- Если боевой агент начинает вести себя по-разному в тесте и в реальной торговле — нарушается принцип верифицируемости (Invariance of Validation).

---

## 3. Байесовская оптимизация на выборке из 36 сделок (Статистическая иллюзия)

### Файлы:
- [python/data/tuned_hyperparams.json](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/data/tuned_hyperparams.json)
- [python/core/bayesian_optimizer.py](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/bayesian_optimizer.py)

В файле `tuned_hyperparams.json` зафиксированы результаты тюнинга:
```json
{
    "n_trials": 120,
    "baseline_params": {
        "chop_range_threshold": 64.0,
        "anti_chop_bb_max": 75.0,
        "min_reduce_size_conv": 60.0,
        "be_activation_r": 0.75
    },
    "best_params": {
        "chop_range_threshold": 54.0,
        "anti_chop_bb_max": 87.0,
        "min_reduce_size_conv": 75.0,
        "be_activation_r": 1.35,
        "trail_activation_r": 1.6
    },
    "baseline_metrics": {
        "total_trades": 36,
        "retained_count": 36,
        "filtered_count": 0,
        "wins_count": 16,
        "losses_count": 18,
        "scratches_count": 2,
        "net_r": -2.9266
    },
    "tuned_metrics": {
        "total_trades": 36,
        "retained_count": 22,
        "filtered_count": 14,
        "net_r": -1.6555
    }
}
```

### Анатомия дефекта:
1. **Экстремально малый размер выборки ($N=36$):** В количественных финансах выборка из 36 трейдов несет нулевую статистическую значимость (P-value > 0.4).
2. **Искусственный отбор (Data Snooping):** Алгоритм оптимизации просто подобрал значения `chop_range_threshold = 54.0` и `min_reduce_size_conv = 75.0`, при которых 14 конкретных убыточных сделок прошлого периода не вошли в выборку, снизив бумажный убыток с `-2.92R` до `-1.65R`.
3. **Хрупкость системы:** Эти 5 подобранных чисел подгружаются в `strategy_router.py` (строка 611) как «истина в последней инстанции». При малейшем изменении режима рынка эти параметры начинают резать прибыльные трейды и пропускать новые убытки.

---

## 4. Насильственное усечение штрафов LLM для импульсов

### Файл: [python/agents/ceo_agent.py](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/ceo_agent.py#L173-L215)

Функция `apply_breakout_penalty_override`:
```python
def apply_breakout_penalty_override(ceo_decision, router_metadata):
    """
    Корректирует ложные штрафы Mean-Reversion (RiskPenalty -20..-25) от LLM,
    если Strategy Router передал валидный импульсный сетап BREAKOUT.
    """
    if is_fresh_impulse and abs(raw_penalty) >= 15.0:
        if vol_pct >= 250.0 or is_trend_ob:
            capped_penalty = -5.0
        else:
            capped_penalty = -10.0

        new_entry_quality = max(0.0, dir_conf + capped_penalty)
        ceo_decision["risk_penalty"] = capped_penalty
        ceo_decision["entry_quality"] = new_entry_quality
```

### Анатомия дефекта:
- LLM оценивает импульсный пробой и, видя растянутость цены, обоснованно начисляет большой штраф за риск (`-20..-25`).
- Вместо того чтобы научить промпт разделять моментум и растянутость (ортогональный скоринг), разработчики добавили жесткий перехватчик, который принудительно заменяет штраф на `-5.0` или `-10.0`.
- Это искажает внутреннюю логику модели и порождает ложное чувство уверенности.

