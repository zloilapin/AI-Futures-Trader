# 04. НАРРАТИВНЫЕ «ОБХОДЫ» И АПОФЕНИЯ (NARRATIVE BYPASSES & APOPHENIA)

> **Нарушаемые законы Конституции:**
> **3. MICROSTRUCTURE OVER APOPHENIA (REALITY OVER GEOMETRY)**
> *"Stops Belong Behind Real Structure. A stop-loss represents the price where the trade thesis is objectively falsified. Never artificially compress a structural stop into a narrow percentage range just to force a trade into a small account's dollar risk cap."*
> 
> **2. VIA NEGATIVA (SUBTRACTION OVER ADDITION)**
> *"Zero New Magic Constants. Do not introduce arbitrary penalty points, ad-hoc multipliers, or uncalibrated percentage corridors."*

---

## 1. «Anti-Bot Predator Mode»: Маркетинговая легенда вместо риск-контроля

Один из самых ярких примеров нарративной надстройки в проекте — так называемый `Anti-Bot Predator Mode` (`ANTI_BOT_MODE`), позиционируемый как *«институциональный перехват стопов маркет-мейкеров»*.

### Анатомия в кодовой базе:
При включении этого флага (или выборе режима `MEAN_REVERSION`) система последовательно **отключает ключевые защитные барьеры**:

#### А. Обход дебатов аналитиков в [python/core/deterministic_guard.py#L765-L840](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/deterministic_guard.py#L765-L840):
```python
is_anti_bot_mr = bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION")
if is_anti_bot_mr:
    bypass_msg = f"🦈 [Debate Guard Bypass] Неполные дебаты ({debate_status}), но активен Anti-Bot Predator Mode ({strategy_mode}). Вход разрешён по правилу институционального перехвата ликвидности."
    if rejection_tag in ["ANALYST_DISAGREEMENT", "COUNTER_ANALYST_VETO"]:
        guard_status = "PASSED"
        guard_reason = "Passed deterministic guard (Anti-Bot Predator bypass of analyst disagreement)."
```

#### Б. Отключение защиты от выноса экстремумов в [python/agents/sentinel_agent.py#L300, L467](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/sentinel_agent.py#L300):
```python
# 1.6. Extreme Blowout Defense: Disabled for Anti-Bot MR (v2.1 Patch: band extremes are normal entry zones for MEAN_REVERSION)
```

#### В. Удвоение тайм-стопов в [python/agents/sentinel_agent.py#L170-L185](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/sentinel_agent.py#L170-L185):
```python
# 2. Time-Stop / Stagnation Guard (Time-Based Risk Decay - Doubled for Anti-Bot MR: 360m / 480m / 720m)
t1_age = 360.0 if is_anti_bot_mr else 180.0
t2_age = 480.0 if is_anti_bot_mr else 240.0
t3_age = 720.0 if is_anti_bot_mr else 360.0
```

#### Г. Полный пропуск затухания импульса в [python/agents/sentinel_agent.py#L160](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/sentinel_agent.py#L160):
```python
# 1.5. Breakout Stagnation Guard: strictly skipped for Anti-Bot / MR
```

### Количественный вердикт:
- Понятие «Anti-Bot Predator» не имеет строгого математического определения в коде. Это обычный возврат к средней (`MEAN_REVERSION`).
- Вместо того чтобы формализовать распределение вероятностей возврата к средней с учетом хвостового риска, разработчики просто выдали этой стратегии «дипломатический иммунитет»: разрешили входить при несогласии аналитиков, терпеть просадку в два раза дольше по времени и игнорировать выносы за границы Боллинджера.
- В условиях сильного тренда стратегия с отключенной защитой от выноса гарантированно ловит каскадный стоп-аут против тренда.

---

## 2. «Volume Explosion Guard»: Магический оверрайд отката

### Файл: [python/core/deterministic_guard.py#L740-L755](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/deterministic_guard.py#L740-L755)

```python
if is_confirmed_breakout and trade_action == "WAIT_FOR_PULLBACK" and conviction >= min_conv and decision in ["LONG", "SHORT"]:
    # Only override WAIT_FOR_PULLBACK if entry quality is solid (>= 72%) to avoid buying the top of an overextended move
    if entry_qual >= 72:
        volume_explosion_triggered = True
        ceo_proposal["volume_explosion_override"] = True
        ceo_proposal["action_size_mult_override"] = 0.50
        trade_action = "ENTER"
```

### В чем опасность:
1. **Магическое число `72%`:** Почему именно `72%`? Не `70%`, не `75%`? Это число подогнано под конкретный исторический лог.
2. **Логическое противоречие:** Если сетап классифицирован как `WAIT_FOR_PULLBACK`, это означает, что цена оторвана от базы и вход по рынку несет математически невыгодное соотношение R:R. Превращение `WAIT_FOR_PULLBACK` в маркет-вход `ENTER` просто потому, что объем вырос на 250%, означает покупку на самом пике импульса ровно перед началом естественной коррекции.
3. Половинный размер позиции (`action_size_mult_override = 0.50`) не спасает от отрицательного матожидания, а лишь вдвое уменьшает скорость накопления убытка.

---

## 3. Апофения: Фиксация на полосах Боллинджера и 48-часовых «коробках»

В агентах `Indicator_Agent` и `Regime_Agent` присутствует чрезмерная опора на геометрические индикаторы положения цены внутри диапазона:
- `macro_box_pos_pct < 32.0` или `> 68.0` в [indicator_agent.py#L65](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/indicator_agent.py#L65).
- `bb_position_pct >= 89.0%` (из `tuned_hyperparams.json`) как жесткий потолок покупок.

Рынок криптовалют находится в режиме **Extremistan** (толстые хвосты Мандельброта/Талеба). В фазе сильного трендового импульса цена может часами и днями скользить по верхней полосе Боллинджера (`bb_position_pct > 95%`), обновляя максимумы. Запрет на вход в тренд только потому, что цена «касается верхней линии полосы Боллинджера», отсекает самые прибыльные трендовые хвосты распределения (Positive Gamma).

