# Полный аудит открытия позиции по BNB-USD (Cycle #5)

> **Дата и время:** `2026-11-01 01:51:50 UTC` (локальное время бота)  
> **Инструмент:** `BNB-USD` (Perpetual Futures на Nado DEX / Ink L2)  
> **Направление:** `LONG`  
> **Статус исполнения:** `FILLED_AND_PROTECTED`  
> **Транзакция входа (Open Digest):** [`0xb80100808a7d3a1ba39d92a215265b6a2ea988e45cbf163495d31d611b703bc6`](https://explorer.inkonchain.com/tx/0xb80100808a7d3a1ba39d92a215265b6a2ea988e45cbf163495d31d611b703bc6)

---

## 1. Краткая сводка трейда (Executive Summary)

| Параметр | Значение | Пояснение |
| :--- | :--- | :--- |
| **Стратегия & Режим** | `BREAKOUT` в режиме `TRANSITION` | Пробой верхней границы Donchian High ($712.91) при сильном всплеске объема (+190%) |
| **Цена входа (`entry_price`)** | **$714.24** | Исполнение на пробое |
| **Стоп-лосс (`sl_price`)** | **$706.49** (-1.09%) | Защитный стоп, дистанция $7.75 (+ комиссии = $7.81 loss per contract) |
| **Тейк-профит (`tp_price`)** | **$735.42** (+2.97%) | Финальная цель, соотношение R:R = **2.73** (Net R:R = 2.0) |
| **Количество контрактов** | **0.085 BNB** | Рассчитано строго от допустимого риска $0.82 |
| **Номинальный объем (`notional_usd`)** | **$60.71 USD** | **64.0% от всего депозита** аккаунта |
| **Использованная маржа (`margin_used`)** | **$8.67 USD** | Эффективное кредитное плечо: **7.0x** |
| **Одобренный риск (`approved_risk_usd`)** | **$0.82 USD** | **0.89%** от депозита $94.85 при базовом риске 1.5% |
| **Когорта сделки** | `Cohort B` | Высокая уверенность (High Conviction) + `REDUCE_SIZE` |

---

## 2. Разбор вопроса пользователя: «Почему размер позиции снова маленький, хотя память удалена?»

В логах зафиксировано математически строгое доказательство:

1. **Память истории сделок (`trade_journal.json`) действительно чиста:**
   * `bayes_mult = 1.00x` (Байесовский шлюз полностью нейтрален, старые 29 убытков больше не влияют).
   * `kelly_mult = 1.00x` (Келли нейтрален).
   * `recent_streak = STREAK_NORMAL` (стрик поражений = 0).

2. **Что снизило одобренный риск с $1.42 (1.5%) до $0.82 (0.89%):**
   * **Фактор 1: Необнуленный пик баланса в `nado_state.json` (`dd_mult = 0.70x`):**
     * В файле оставалось значение `"peak_balance": 99.16 USD`.
     * Текущий реальный баланс на бирже: `$94.85 USD`.
     * Просадка от пика: `(99.16 - 94.85) / 99.16 = 4.34%`.
     * Правило риск-менеджера (`risk_manager.py:209-211`): просадка 4.0%..6.0% активирует защитный множитель `0.70x` (-30% от базового риска).
   * **Фактор 2: Модель CEO предписала `REDUCE_SIZE` (`action_mult = 0.85x`):**
     * Индикатор RSI был перекуплен (**78.3**), а стакан был нейтральным (дисбаланс +0.04).
     * CEO справедливо решил не заходить на 100% сайза на перегретом RSI: `action_mult = 0.85x` (-15%).
   * **Итоговый комбинированный множитель:**
     $$	ext{Final Risk} = 1.5\% 	imes 1.0 (	ext{Bayes}) 	imes 1.0 (	ext{Kelly}) 	imes 0.70 (	ext{DD}) 	imes 0.85 (	ext{Action}) = \mathbf{0.8925\% \ (\$0.82 \ USD)}$$

3. **Почему $60.71 — это на самом деле крупная позиция:**
   * При балансе кошелька **$94.85** позиция в **$60.71** занимает **64% всего капитала**.
   * Маржа равна $8.67 только благодаря 7x плечу. Если бы маржа была $40, позиция составляла бы $280 (300% от депозита), и при стопе 1.09% убыток составил бы $3.50+ (риск банкротства при череде из 5 стопов).

---

## 3. Рыночный контекст (Market Conditions & Guards)

* **Текущая цена:** `$714.07`
* **Donchian High (верхний канал):** `$712.91` (пробит вверх)
* **Всплеск объема (Volume Spike):** `+190.67%` (аномальный приток институционального объема)
* **Волатильность (ATR 14):** `0.1817%` ($1.298)
* **Спред в стакане:** `0.0014%` ($0.01) — идеальная ликвидность
* **Вердикт Strategy Router:** `READY -> BREAKOUT LONG`

---

## 4. Голоса синдиката аналитиков (5 агентов)

| Агент | Сигнал | Уверенность | Аргументация |
| :--- | :---: | :---: | :--- |
| **`Candle_Agent`** | 🟢 **BULLISH** | **70%** | Чистый бычий пробой (Bullish Breakout) канала Donchian на растущей закрытой свече. |
| **`OI_Funding_Agent`** | 🟢 **BULLISH** | **80%** | Приток институционального капитала в лонг (рост цены + рост Open Interest). Фандинг нейтрален (0.0000). Сильный открытый интерес. |
| **`News_Agent`** | 🟢 **BULLISH** | **75%** | Индекс Fear & Greed = 34 (Страх). Ситуация для контртрендового накопления крупными игроками перед выносом вверх. |
| **`Order_Book_Agent`** | ⚪ **NEUTRAL** | **50%** | Спред узкий ($0.01), дисбаланс книги заявок нейтральный (+0.0459). Нет сопротивления крупными асками. |
| **`Indicator_Agent`** | ⚪ **NEUTRAL** | **50%** | Цена выше EMA-20 и VWAP ($704.09), сильный тренд по ADX (29.1). Однако RSI перекуплен (78.3). |

---

## 5. Решение CEO Agent и дебаты

* **Directional Confidence (Уверенность в направлении):** `76%`
* **Entry Quality (Качество входа):** `66%`
* **Итоговая уверенность (`Conviction`):** `66%` (проходит порог `min_conviction = 65%`)
* **Штрафы за риски (`risk_penalties`):** `-10`
  * `price_extension`: `-10` (растяжение цены от VWAP)
  * `macro_sentiment`: `-5`
  * `market_structure`: `-5`
  * `liquidity_friction`: `-5`
* **Действие (`trade_action`):** **`REDUCE_SIZE`**
* **Тезис модели (CEO Reasoning):**
  > *"Bullish breakout confirmed by high volume and institutional accumulation. Neutral order book and overbought RSI reduce entry quality."*
* **Статус дебатов:** `bull_status: OK`, `bear_status: OK`, `debate_status: COMPLETE`.

---

## 6. Детальный аудит математики Risk Manager

```text
[RiskManager] Canonical Sizing Equation:
Total Balance:         $94.85 USD
Peak Balance:          $99.16 USD
Drawdown:              4.34% -> Drawdown Multiplier: 0.70x
Trade Action:          REDUCE_SIZE -> Action Multiplier: 0.85x
Bayesian Multiplier:   1.00x (Beta(4.0, 4.0), p=0.50, fresh journal)
Kelly Multiplier:      1.00x
Base Risk Pct:         1.50%
Dynamic Risk Pct:      1.50% * 0.70 * 0.85 = 0.8925%
Approved Risk USD:     $94.85 * 0.008925 = $0.82 USD

Entry Price:           $714.24
Stop Loss:             $706.49 (Dist: $7.75)
Taker Fee (2x 0.05%):  $0.06 per BNB
Effective Loss / BNB:  $7.75 + $0.06 = $7.81 USD
Calculated Contracts:  $0.82 / $7.81 = 0.085 BNB
Notional Size:         0.085 * $714.24 = $60.71 USD
Leverage:              7.0x
Margin Required:       $60.71 / 7 = $8.67 USD
Take Profit Target:    $735.42 (+2.97%)
Risk / Reward (Net):   2.00x (Gross: 2.73x)
```

---

## 7. On-Chain исполнение и 2-Tier Partial Take-Profit Plan

* **DEX:** Nado DEX (Ink L2)
* **Product ID:** `14` (BNB-PERP)
* **Хэш исполнения ордера:** `0xb80100808a7d3a1ba39d92a215265b6a2ea988e45cbf163495d31d611b703bc6`
* **План фиксации прибыли (2-Tier Partial TP Plan):**
  * **Уровень 1 (Tier 1):** Закрыть **0.04 BNB** (~47%) по цене **$719.28** (`+0.65R` / `+0.70%`). При исполнении автоматически перенести Stop-Loss в **Безубыток (Breakeven + fees)**.
  * **Уровень 2 (Tier 2 - Final):** Закрыть оставшиеся **0.045 BNB** по финальной цели **$735.42** (`+2.73R` / `+2.97%`).

---

## 8. Сырые JSON-логи для анализа в Gemini

### А. Снимок из `decision_snapshots.jsonl`
```json
{
  "timestamp": 1793523110.4382706,
  "iso_time": "2026-11-01T08:51:50.438270+00:00",
  "candidate_id": "BNB-USD:c5_1793523001228",
  "is_fallback_candidate_id": false,
  "cycle_number": 5,
  "symbol": "BNB-USD",
  "macro_regime": "TRANSITION",
  "trading_profile": "BALANCED",
  "stage": "EXECUTION",
  "final_action": "EXECUTED",
  "rejection_tag": null,
  "rejection_reason": null,
  "min_conviction": 65,
  "market_guard_inputs": {
    "current_price": 714.0,
    "donchian_high": 712.9100000000001,
    "donchian_low": 702.77,
    "volume_spike_pct": 190.67,
    "atr_pct": 0.18,
    "bb_position_pct": 162.63,
    "ema_20": 706.845631,
    "rsi_14": 78.34,
    "funding_rate": 0.0,
    "is_pullback_retest": false,
    "multi_timeframe": {
      "trend_15m": "BULLISH",
      "trend_1h": "BULLISH",
      "trend_4h": "BEARISH",
      "mtf_alignment": "TRANSITION",
      "tf_15m": {
        "interval_min": 15,
        "current_price": 714.07,
        "trend": "BULLISH",
        "atr": 1.290907,
        "change_pct": 1.05,
        "volume": 509103.35,
        "candles_20": [
          {
            "open": 706.6099999999999,
            "high": 706.66,
            "low": 706.6099999999999,
            "close": 706.66,
            "volume": 7.07
          },
          {
            "open": 705.86,
            "high": 706.2700000000001,
            "low": 705.86,
            "close": 706.2099999999999,
            "volume": 13.285
          },
          {
            "open": 706.5100000000001,
            "high": 706.5100000000001,
            "low": 706.5,
            "close": 706.5,
            "volume": 1.465
          },
          {
            "open": 706.07,
            "high": 706.07,
            "low": 705.23,
            "close": 705.23,
            "volume": 24.305
          },
          {
            "open": 705.16,
            "high": 705.16,
            "low": 704.74,
            "close": 704.88,
            "volume": 3.005
          },
          {
            "open": 704.4499999999999,
            "high": 704.4499999999999,
            "low": 704.02,
            "close": 704.02,
            "volume": 7.71
          },
          {
            "open": 703.48,
            "high": 703.5699999999999,
            "low": 702.1,
            "close": 702.2,
            "volume": 21.1
          },
          {
            "open": 702.34,
            "high": 702.34,
            "low": 702.0300000000001,
            "close": 702.0300000000001,
            "volume": 8.49
          },
          {
            "open": 702.1500000000001,
            "high": 702.1500000000001,
            "low": 701.8,
            "close": 701.8,
            "volume": 10.305
          },
          {
            "open": 702.26,
            "high": 702.7900000000001,
            "low": 702.16,
            "close": 702.78,
            "volume": 6.16
          },
          {
            "open": 702.98,
            "high": 703.6700000000001,
            "low": 702.98,
            "close": 703.1,
            "volume": 19.315
          },
          {
            "open": 703.14,
            "high": 703.14,
            "low": 703.14,
            "close": 703.14,
            "volume": 0.49
          },
          {
            "open": 703.4899999999999,
            "high": 703.4899999999999,
            "low": 703.28,
            "close": 703.28,
            "volume": 0.95
          },
          {
            "open": 703.35,
            "high": 703.36,
            "low": 702.77,
            "close": 702.77,
            "volume": 24.56
          },
          {
            "open": 702.81,
            "high": 702.83,
            "low": 702.35,
            "close": 702.35,
            "volume": 26.26
          },
          {
            "open": 703.46,
            "high": 703.68,
            "low": 703.3,
            "close": 703.68,
            "volume": 12.085
          },
          {
            "open": 702.98,
            "high": 703.94,
            "low": 702.98,
            "close": 703.94,
            "volume": 8.23
          },
          {
            "open": 704.0899999999999,
            "high": 704.8,
            "low": 704.0899999999999,
            "close": 704.8,
            "volume": 0.545
          },
          {
            "open": 704.4899999999999,
            "high": 704.9699999999999,
            "low": 704.4899999999999,
            "close": 704.95,
            "volume": 6.01
          },
          {
            "open": 704.9699999999999,
            "high": 705.2099999999999,
            "low": 704.9699999999999,
            "close": 705.2099999999999,
            "volume": 0.61
          },
          {
            "open": 704.4,
            "high": 704.4,
            "low": 703.42,
            "close": 703.42,
            "volume": 6.34
          },
          {
            "open": 703.7900000000001,
            "high": 704.56,
            "low": 703.36,
            "close": 703.4899999999999,
            "volume": 16.62
          },
          {
            "open": 703.47,
            "high": 703.47,
            "low": 702.84,
            "close": 703.02,
            "volume": 15.655
          },
          {
            "open": 703.26,
            "high": 704.22,
            "low": 702.93,
            "close": 702.93,
            "volume": 34.815
          },
          {
            "open": 702.93,
            "high": 703.2700000000001,
            "low": 702.17,
            "close": 702.2,
            "volume": 0.755
          },
          {
            "open": 702.2,
            "high": 702.28,
            "low": 701.9,
            "close": 702.0899999999999,
            "volume": 27.665
          },
          {
            "open": 702.5500000000001,
            "high": 702.96,
            "low": 702.42,
            "close": 702.42,
            "volume": 16.445
          },
          {
            "open": 702.1500000000001,
            "high": 703.3100000000001,
            "low": 702.1500000000001,
            "close": 703.16,
            "volume": 17.61
          },
          {
            "open": 703.08,
            "high": 703.26,
            "low": 703.08,
            "close": 703.26,
            "volume": 3.145
          },
          {
            "open": 703.78,
            "high": 704.01,
            "low": 703.78,
            "close": 704.01,
            "volume": 0.77
          },
          {
            "open": 703.6300000000001,
            "high": 703.6300000000001,
            "low": 703.35,
            "close": 703.41,
            "volume": 4.085
          },
          {
            "open": 703.5100000000001,
            "high": 704.52,
            "low": 702.77,
            "close": 704.52,
            "volume": 21.305
          },
          {
            "open": 704.71,
            "high": 705.64,
            "low": 703.56,
            "close": 705.64,
            "volume": 98.1
          },
          {
            "open": 704.99,
            "high": 705.71,
            "low": 704.99,
            "close": 705.3699999999999,
            "volume": 17.665
          },
          {
            "open": 704.98,
            "high": 705.19,
            "low": 704.98,
            "close": 705.16,
            "volume": 1.205
          },
          {
            "open": 704.74,
            "high": 704.74,
            "low": 704.74,
            "close": 704.74,
            "volume": 2.0
          },
          {
            "open": 705.6899999999999,
            "high": 705.71,
            "low": 705.0,
            "close": 705.0,
            "volume": 3.445
          },
          {
            "open": 705.62,
            "high": 705.62,
            "low": 705.62,
            "close": 705.62,
            "volume": 2.355
          },
          {
            "open": 705.3900000000001,
            "high": 705.81,
            "low": 705.3900000000001,
            "close": 705.81,
            "volume": 1.555
          },
          {
            "open": 706.1500000000001,
            "high": 706.1500000000001,
            "low": 705.14,
            "close": 705.3,
            "volume": 5.635
          },
          {
            "open": 705.3,
            "high": 705.3100000000001,
            "low": 705.3,
            "close": 705.3100000000001,
            "volume": 1.81
          },
          {
            "open": 705.18,
            "high": 706.29,
            "low": 705.18,
            "close": 706.29,
            "volume": 16.65
          },
          {
            "open": 706.22,
            "high": 706.22,
            "low": 705.5100000000001,
            "close": 705.5100000000001,
            "volume": 4.0
          },
          {
            "open": 704.84,
            "high": 704.84,
            "low": 703.83,
            "close": 704.26,
            "volume": 10.32
          },
          {
            "open": 704.89,
            "high": 705.42,
            "low": 704.89,
            "close": 705.1500000000001,
            "volume": 12.23
          },
          {
            "open": 705.44,
            "high": 707.08,
            "low": 705.44,
            "close": 707.08,
            "volume": 2.885
          },
          {
            "open": 706.34,
            "high": 707.29,
            "low": 706.34,
            "close": 706.53,
            "volume": 3.01
          },
          {
            "open": 706.62,
            "high": 707.42,
            "low": 706.62,
            "close": 707.3900000000001,
            "volume": 11.32
          },
          {
            "open": 707.6300000000001,
            "high": 712.9100000000001,
            "low": 707.3699999999999,
            "close": 712.6700000000001,
            "volume": 119.24
          },
          {
            "open": 712.6,
            "high": 714.07,
            "low": 712.6,
            "close": 714.07,
            "volume": 32.375
          }
        ],
        "timestamp": 1793523002.0358925
      },
      "tf_1h": {
        "interval_min": 60,
        "current_price": 714.07,
        "trend": "BULLISH",
        "atr": 2.822563,
        "change_pct": 0.95,
        "volume": 3770268.18,
        "candles_20": [
          {
            "open": 715.17,
            "high": 715.18,
            "low": 705.7,
            "close": 707.34,
            "volume": 227.2
          },
          {
            "open": 707.12,
            "high": 708.02,
            "low": 703.6300000000001,
            "close": 704.92,
            "volume": 205.205
          },
          {
            "open": 704.8,
            "high": 704.96,
            "low": 701.52,
            "close": 702.5500000000001,
            "volume": 225.815
          },
          {
            "open": 701.7299999999999,
            "high": 704.1500000000001,
            "low": 700.34,
            "close": 702.6300000000001,
            "volume": 114.555
          },
          {
            "open": 701.4899999999999,
            "high": 703.44,
            "low": 699.24,
            "close": 699.24,
            "volume": 213.915
          },
          {
            "open": 699.3900000000001,
            "high": 701.35,
            "low": 697.58,
            "close": 700.41,
            "volume": 162.56
          },
          {
            "open": 700.53,
            "high": 700.53,
            "low": 694.24,
            "close": 696.68,
            "volume": 278.975
          },
          {
            "open": 697.82,
            "high": 698.76,
            "low": 693.41,
            "close": 697.82,
            "volume": 185.0
          },
          {
            "open": 699.4300000000001,
            "high": 705.24,
            "low": 697.47,
            "close": 699.82,
            "volume": 312.07500000000005
          },
          {
            "open": 698.68,
            "high": 701.44,
            "low": 697.88,
            "close": 700.17,
            "volume": 111.42
          },
          {
            "open": 700.47,
            "high": 702.5100000000001,
            "low": 698.29,
            "close": 699.02,
            "volume": 183.845
          },
          {
            "open": 699.16,
            "high": 700.5100000000001,
            "low": 695.1500000000001,
            "close": 698.05,
            "volume": 115.59
          },
          {
            "open": 696.44,
            "high": 700.16,
            "low": 695.54,
            "close": 700.16,
            "volume": 78.285
          },
          {
            "open": 701.89,
            "high": 703.3,
            "low": 699.12,
            "close": 699.4,
            "volume": 25.225
          },
          {
            "open": 699.58,
            "high": 699.7500000000001,
            "low": 696.12,
            "close": 696.62,
            "volume": 34.095
          },
          {
            "open": 697.24,
            "high": 699.48,
            "low": 696.96,
            "close": 699.4300000000001,
            "volume": 70.555
          },
          {
            "open": 700.38,
            "high": 704.26,
            "low": 700.38,
            "close": 703.94,
            "volume": 104.6
          },
          {
            "open": 703.77,
            "high": 710.56,
            "low": 703.6099999999999,
            "close": 708.0,
            "volume": 116.235
          },
          {
            "open": 709.0,
            "high": 709.0,
            "low": 706.0899999999999,
            "close": 706.16,
            "volume": 52.675
          },
          {
            "open": 705.7900000000001,
            "high": 705.7900000000001,
            "low": 702.84,
            "close": 705.2499999999999,
            "volume": 131.695
          },
          {
            "open": 705.2099999999999,
            "high": 705.74,
            "low": 703.68,
            "close": 703.99,
            "volume": 154.65
          },
          {
            "open": 704.12,
            "high": 704.56,
            "low": 700.6300000000001,
            "close": 701.7,
            "volume": 116.125
          },
          {
            "open": 701.53,
            "high": 703.4899999999999,
            "low": 701.52,
            "close": 703.4899999999999,
            "volume": 105.585
          },
          {
            "open": 703.95,
            "high": 704.18,
            "low": 701.78,
            "close": 701.95,
            "volume": 42.76
          },
          {
            "open": 701.89,
            "high": 701.89,
            "low": 699.4,
            "close": 699.77,
            "volume": 345.66
          },
          {
            "open": 699.72,
            "high": 701.05,
            "low": 698.5500000000001,
            "close": 700.5500000000001,
            "volume": 280.71
          },
          {
            "open": 700.6,
            "high": 702.7,
            "low": 700.6,
            "close": 702.1500000000001,
            "volume": 39.815
          },
          {
            "open": 702.47,
            "high": 704.48,
            "low": 701.96,
            "close": 704.32,
            "volume": 67.235
          },
          {
            "open": 703.6700000000001,
            "high": 705.77,
            "low": 703.38,
            "close": 705.65,
            "volume": 67.02
          },
          {
            "open": 704.94,
            "high": 706.88,
            "low": 704.3299999999999,
            "close": 705.35,
            "volume": 90.77
          },
          {
            "open": 705.4300000000001,
            "high": 706.0,
            "low": 703.7500000000001,
            "close": 703.78,
            "volume": 52.68
          },
          {
            "open": 704.11,
            "high": 708.56,
            "low": 703.01,
            "close": 707.6899999999999,
            "volume": 121.295
          },
          {
            "open": 706.7299999999999,
            "high": 707.7900000000001,
            "low": 705.11,
            "close": 705.36,
            "volume": 34.495
          },
          {
            "open": 705.12,
            "high": 706.65,
            "low": 704.84,
            "close": 705.38,
            "volume": 31.11
          },
          {
            "open": 704.98,
            "high": 707.05,
            "low": 704.98,
            "close": 707.05,
            "volume": 37.17
          },
          {
            "open": 707.14,
            "high": 708.14,
            "low": 706.59,
            "close": 707.23,
            "volume": 20.04
          },
          {
            "open": 707.42,
            "high": 707.86,
            "low": 705.86,
            "close": 706.2099999999999,
            "volume": 30.725
          },
          {
            "open": 706.5100000000001,
            "high": 706.5100000000001,
            "low": 704.02,
            "close": 704.02,
            "volume": 36.485
          },
          {
            "open": 703.48,
            "high": 703.5699999999999,
            "low": 701.8,
            "close": 702.78,
            "volume": 46.055
          },
          {
            "open": 702.98,
            "high": 703.6700000000001,
            "low": 702.98,
            "close": 703.28,
            "volume": 20.755
          },
          {
            "open": 703.35,
            "high": 703.36,
            "low": 702.35,
            "close": 702.35,
            "volume": 50.82
          },
          {
            "open": 703.46,
            "high": 704.9699999999999,
            "low": 702.98,
            "close": 704.95,
            "volume": 26.87
          },
          {
            "open": 704.9699999999999,
            "high": 705.2099999999999,
            "low": 702.84,
            "close": 703.02,
            "volume": 39.225
          },
          {
            "open": 703.26,
            "high": 704.22,
            "low": 701.9,
            "close": 702.42,
            "volume": 79.68
          },
          {
            "open": 702.1500000000001,
            "high": 704.01,
            "low": 702.1500000000001,
            "close": 703.41,
            "volume": 25.61
          },
          {
            "open": 703.5100000000001,
            "high": 705.71,
            "low": 702.77,
            "close": 705.16,
            "volume": 138.275
          },
          {
            "open": 704.74,
            "high": 705.71,
            "low": 704.74,
            "close": 705.62,
            "volume": 7.8
          },
          {
            "open": 705.3900000000001,
            "high": 706.29,
            "low": 705.14,
            "close": 706.29,
            "volume": 25.65
          },
          {
            "open": 706.22,
            "high": 707.08,
            "low": 703.83,
            "close": 707.08,
            "volume": 29.435
          },
          {
            "open": 706.34,
            "high": 714.07,
            "low": 706.34,
            "close": 714.07,
            "volume": 165.945
          }
        ],
        "timestamp": 1793523002.0238957
      },
      "tf_4h": {
        "interval_min": 240,
        "current_price": 714.07,
        "trend": "BEARISH",
        "atr": 9.784847,
        "change_pct": -2.85,
        "volume": 22754533.2,
        "candles_20": [
          {
            "open": 740.34,
            "high": 740.98,
            "low": 735.0,
            "close": 735.0300000000001,
            "volume": 789.6099999999999
          },
          {
            "open": 735.22,
            "high": 735.3699999999999,
            "low": 730.6700000000001,
            "close": 731.0899999999999,
            "volume": 791.335
          },
          {
            "open": 730.96,
            "high": 732.14,
            "low": 717.38,
            "close": 720.74,
            "volume": 1114.98
          },
          {
            "open": 720.3299999999999,
            "high": 730.2099999999999,
            "low": 719.36,
            "close": 729.46,
            "volume": 315.63
          },
          {
            "open": 731.05,
            "high": 739.34,
            "low": 730.8700000000001,
            "close": 739.32,
            "volume": 432.65
          },
          {
            "open": 739.62,
            "high": 745.83,
            "low": 738.6300000000001,
            "close": 744.93,
            "volume": 971.5550000000001
          },
          {
            "open": 745.4499999999999,
            "high": 748.3900000000001,
            "low": 741.3699999999999,
            "close": 744.5,
            "volume": 420.12
          },
          {
            "open": 744.4499999999999,
            "high": 744.5100000000001,
            "low": 735.14,
            "close": 743.5100000000001,
            "volume": 333.83500000000004
          },
          {
            "open": 742.23,
            "high": 743.0300000000001,
            "low": 735.5699999999999,
            "close": 742.3100000000001,
            "volume": 678.88
          },
          {
            "open": 741.98,
            "high": 743.3100000000001,
            "low": 740.6700000000001,
            "close": 743.14,
            "volume": 168.025
          },
          {
            "open": 742.04,
            "high": 742.54,
            "low": 735.44,
            "close": 738.05,
            "volume": 506.83
          },
          {
            "open": 738.41,
            "high": 742.3699999999999,
            "low": 733.7,
            "close": 736.19,
            "volume": 400.01500000000004
          },
          {
            "open": 736.22,
            "high": 742.9100000000001,
            "low": 736.22,
            "close": 738.1,
            "volume": 681.5100000000001
          },
          {
            "open": 738.2499999999999,
            "high": 743.7,
            "low": 732.72,
            "close": 743.34,
            "volume": 799.245
          },
          {
            "open": 742.5100000000001,
            "high": 746.41,
            "low": 736.84,
            "close": 744.17,
            "volume": 742.9649999999999
          },
          {
            "open": 744.54,
            "high": 746.1500000000001,
            "low": 739.28,
            "close": 742.99,
            "volume": 367.825
          },
          {
            "open": 742.36,
            "high": 743.41,
            "low": 739.2499999999999,
            "close": 742.96,
            "volume": 501.23
          },
          {
            "open": 743.2499999999999,
            "high": 746.16,
            "low": 737.4,
            "close": 738.84,
            "volume": 529.3
          },
          {
            "open": 738.92,
            "high": 739.06,
            "low": 734.4,
            "close": 736.05,
            "volume": 268.775
          },
          {
            "open": 736.58,
            "high": 741.0300000000001,
            "low": 733.66,
            "close": 740.07,
            "volume": 292.53
          },
          {
            "open": 739.9,
            "high": 754.07,
            "low": 739.08,
            "close": 750.01,
            "volume": 901.16
          },
          {
            "open": 750.1500000000001,
            "high": 752.7500000000001,
            "low": 747.84,
            "close": 748.9100000000001,
            "volume": 588.855
          },
          {
            "open": 748.59,
            "high": 749.68,
            "low": 743.2499999999999,
            "close": 745.12,
            "volume": 432.11
          },
          {
            "open": 744.53,
            "high": 748.5100000000001,
            "low": 741.65,
            "close": 747.07,
            "volume": 255.38
          },
          {
            "open": 747.02,
            "high": 747.41,
            "low": 744.41,
            "close": 745.0899999999999,
            "volume": 295.39
          },
          {
            "open": 745.88,
            "high": 762.0899999999999,
            "low": 745.74,
            "close": 760.6300000000001,
            "volume": 649.2700000000001
          },
          {
            "open": 760.88,
            "high": 761.1299999999999,
            "low": 746.65,
            "close": 749.1299999999999,
            "volume": 589.99
          },
          {
            "open": 749.47,
            "high": 754.6899999999999,
            "low": 746.82,
            "close": 754.68,
            "volume": 837.885
          },
          {
            "open": 754.95,
            "high": 759.5100000000001,
            "low": 751.5500000000001,
            "close": 759.5100000000001,
            "volume": 494.91
          },
          {
            "open": 759.65,
            "high": 760.5699999999999,
            "low": 744.59,
            "close": 746.42,
            "volume": 762.645
          },
          {
            "open": 746.5100000000001,
            "high": 758.81,
            "low": 745.34,
            "close": 748.74,
            "volume": 594.485
          },
          {
            "open": 747.8700000000001,
            "high": 753.06,
            "low": 738.29,
            "close": 739.81,
            "volume": 1768.2250000000001
          },
          {
            "open": 740.12,
            "high": 752.93,
            "low": 739.07,
            "close": 749.83,
            "volume": 712.275
          },
          {
            "open": 750.2099999999999,
            "high": 754.14,
            "low": 742.6,
            "close": 744.86,
            "volume": 467.205
          },
          {
            "open": 744.7299999999999,
            "high": 744.7299999999999,
            "low": 679.53,
            "close": 697.53,
            "volume": 3132.88
          },
          {
            "open": 697.36,
            "high": 718.92,
            "low": 681.86,
            "close": 718.9,
            "volume": 2257.155
          },
          {
            "open": 718.64,
            "high": 720.65,
            "low": 705.7,
            "close": 707.34,
            "volume": 966.5350000000001
          },
          {
            "open": 707.12,
            "high": 708.02,
            "low": 699.24,
            "close": 699.24,
            "volume": 759.4899999999999
          },
          {
            "open": 699.3900000000001,
            "high": 705.24,
            "low": 693.41,
            "close": 699.82,
            "volume": 938.6099999999999
          },
          {
            "open": 698.68,
            "high": 702.5100000000001,
            "low": 695.1500000000001,
            "close": 700.16,
            "volume": 489.14
          },
          {
            "open": 701.89,
            "high": 704.26,
            "low": 696.12,
            "close": 703.94,
            "volume": 234.475
          },
          {
            "open": 703.77,
            "high": 710.56,
            "low": 702.84,
            "close": 703.99,
            "volume": 455.25500000000005
          },
          {
            "open": 704.12,
            "high": 704.56,
            "low": 699.4,
            "close": 699.77,
            "volume": 610.1299999999999
          },
          {
            "open": 699.72,
            "high": 705.77,
            "low": 698.5500000000001,
            "close": 705.65,
            "volume": 454.78
          },
          {
            "open": 704.94,
            "high": 708.56,
            "low": 703.01,
            "close": 705.36,
            "volume": 299.24
          },
          {
            "open": 705.12,
            "high": 708.14,
            "low": 704.84,
            "close": 706.2099999999999,
            "volume": 119.045
          },
          {
            "open": 706.5100000000001,
            "high": 706.5100000000001,
            "low": 701.8,
            "close": 702.35,
            "volume": 154.115
          },
          {
            "open": 703.46,
            "high": 705.2099999999999,
            "low": 701.9,
            "close": 703.41,
            "volume": 171.385
          },
          {
            "open": 703.5100000000001,
            "high": 707.08,
            "low": 702.77,
            "close": 707.08,
            "volume": 201.16
          },
          {
            "open": 706.34,
            "high": 714.07,
            "low": 706.34,
            "close": 714.07,
            "volume": 165.945
          }
        ],
        "timestamp": 1793523002.0238957
      }
    },
    "algo_signals": {
      "rsi_divergence": "none",
      "macd_crossover": "none",
      "liquidity_sweeps": [
        {
          "type": "upper_sweep",
          "sweep_type": "bearish_sweep",
          "direction": "bearish",
          "bar": -4,
          "rejection_from": 707.29
        }
      ],
      "candle_patterns": [
        {
          "bar": -3,
          "pattern": "strong_bullish"
        },
        {
          "bar": -2,
          "pattern": "strong_bullish"
        }
      ]
    },
    "asset_return_24h": 2.07,
    "rs_divergence": null
  },
  "router": {
    "strategy_mode": "BREAKOUT",
    "direction_bias": "LONG",
    "has_directional_signal": true,
    "reasoning": "BREAKOUT LONG: Пробой 712.9100000000001 (close=712.6700000000001, price=714.07) с объемом 190.67%, OB=NEUTRAL"
  },
  "analysts": {
    "Candle_Agent": {
      "signal": "BULLISH",
      "confidence": 70
    },
    "Order_Book_Agent": {
      "signal": "NEUTRAL",
      "confidence": 50
    },
    "OI_Funding_Agent": {
      "signal": "BULLISH",
      "confidence": 80
    },
    "News_Agent": {
      "signal": "BULLISH",
      "confidence": 75
    },
    "Indicator_Agent": {
      "signal": "NEUTRAL",
      "confidence": 50
    }
  },
  "debate": {
    "bull_status": "OK",
    "bear_status": "OK",
    "debate_status": "COMPLETE"
  },
  "ceo": {
    "decision": "LONG",
    "conviction": 66,
    "directional_confidence": 76,
    "entry_quality": 66,
    "raw_score": null,
    "deterministic_score": null,
    "risk_breakdown": {
      "price_extension": -10,
      "macro_sentiment": -5,
      "market_structure": -5,
      "liquidity_friction": -5,
      "liquidity": -5,
      "total": -10
    },
    "trade_action": "REDUCE_SIZE",
    "reasoning": "Bullish breakout confirmed by high volume and institutional accumulation. Neutral order book and overbought RSI reduce entry quality."
  },
  "guard": {
    "action": "REDUCE_SIZE",
    "decision": "LONG",
    "conviction": 66,
    "reason": "Passed deterministic guard.",
    "is_actionable": true,
    "guard_status": "PASSED",
    "rejection_tag": null
  },
  "risk": {
    "approved": true,
    "notional_size_usd": 60.71040000000001,
    "cohort": "B",
    "leverage": 7.0,
    "take_profit_price": 735.42,
    "stop_loss_price": 706.490234,
    "approved_risk_usd": 0.82,
    "reasoning": "Math calculated based on BALANCED profile. SL=706.490234, TP=735.42, RR=2.0"
  },
  "execution": {
    "status": "FILLED_AND_PROTECTED",
    "trade_success": true,
    "fill_price": 714.24
  }
}
```

### Б. Полный дамп цикла `cycle_20261101_015150_..._bnb.json`
```json
{
  "symbol": "BNB-USD",
  "market_conditions": {
    "reasoning": "Рынок ликвиден (спред 0.0014%) и волатилен (ATR 0.1817%).",
    "status": "READY",
    "proceed": true,
    "strategy_mode": "BREAKOUT",
    "direction_bias": "LONG",
    "router_reasoning": "BREAKOUT LONG: Пробой 712.9100000000001 (close=712.6700000000001, price=714.07) с объемом 190.67%, OB=NEUTRAL"
  },
  "analysts": [
    {
      "signal": "BULLISH",
      "confidence": 70,
      "reasoning": "Бычий пробой (продолжение тренда) на закрытой свече. Закрытие выше максимума предыдущей свечи.",
      "pattern_detected": "Bullish Breakout",
      "agent_name": "Candle_Agent"
    },
    {
      "signal": "NEUTRAL",
      "confidence": 50,
      "reasoning": "Спред 0.0100. Баланс покупателей и продавцов нейтрален (imbalance: 0.0459)",
      "agent_name": "Order_Book_Agent"
    },
    {
      "signal": "BULLISH",
      "confidence": 80,
      "reasoning": "Приток институционального капитала в лонги (цена растет + OI растет: аккумуляция). Фандинг сбалансирован (0.0000). Высокий открытый интерес",
      "agent_name": "OI_Funding_Agent"
    },
    {
      "signal": "BULLISH",
      "confidence": 75,
      "reasoning": "Fear & Greed Index at 34 indicates Fear, suggesting potential contrarian LONG opportunity as institutions may accumulate.",
      "agent_name": "News_Agent"
    },
    {
      "signal": "NEUTRAL",
      "confidence": 50,
      "reasoning": "RSI перекуплен (78.3). MACD выше сигнальной линии. Цена выше EMA-20. Обнаружен сбор ликвидности сверху (1x). Цена выше VWAP (704.09). Сильный бычий тренд по ADX (29.1). Противоречивые сигналы индикаторов (Быки: 4, Медведи: 3). Нейтралитет.",
      "agent_name": "Indicator_Agent"
    }
  ],
  "ceo_decision": {
    "decision": "LONG",
    "conviction": 66,
    "directional_confidence": 76,
    "dir_conf": 76,
    "entry_quality": 66,
    "risk_score": 25,
    "risk_penalties": -10,
    "risk_penalty": -10,
    "risk_breakdown": {
      "price_extension": -10,
      "macro_sentiment": -5,
      "market_structure": -5,
      "liquidity_friction": -5,
      "liquidity": -5,
      "total": -10
    },
    "trade_action": "REDUCE_SIZE",
    "action": "REDUCE_SIZE",
    "final_status": "REDUCE_SIZE",
    "conviction_for_risk": 76.0,
    "reasoning_en": "Bullish breakout confirmed by high volume and institutional accumulation. Neutral order book and overbought RSI reduce entry quality.",
    "reasoning_ru": "",
    "consensus_summary": "Bullish breakout, institutional accumulation, neutral order book.",
    "mtf_validation": "",
    "hold_category": null,
    "primary_conviction": 66,
    "escalated": false,
    "disputed_arbitration": false,
    "debate_status": "COMPLETE",
    "bull_status": "OK",
    "bear_status": "OK",
    "symbol": "BNB-USD",
    "is_actionable": true,
    "guard_status": "PASSED",
    "strategy_mode": "BREAKOUT",
    "direction_bias": "LONG",
    "min_conviction": 65,
    "guard_reason": "Passed deterministic guard.",
    "rejection_tag": null,
    "volume_explosion_override": false,
    "action_size_mult_override": null
  },
  "risk_assessment": {
    "approved": true,
    "veto_category": null,
    "trade_action": "REDUCE_SIZE",
    "reasoning": "Math calculated based on BALANCED profile. SL=706.490234, TP=735.42, RR=2.0",
    "risk_amount_usd": 0.82,
    "approved_risk_usd": 0.82,
    "planned_risk_usd": 0.82,
    "planned_price_risk_usd": 0.76,
    "base_risk_pct": 0.015,
    "final_risk_pct": 0.008924999999999999,
    "cohort": "B",
    "applied_multipliers": {
      "conf_mult": 1.0,
      "action_mult": 0.85,
      "signal_mult": 0.85,
      "quality_mult": 1.0,
      "dd_mult": 0.7,
      "kelly_mult": 1.0,
      "bayes_mult": 1.0,
      "combined_mult": 0.595
    },
    "expected_fee_usd": 0.06,
    "total_expected_loss_usd": 0.82,
    "total_stress_loss_usd": 0.84,
    "max_tolerated_risk_usd": 0.95,
    "max_allowed_stress_loss_usd": 1.22,
    "funding_rate_hourly": 0.0,
    "holding_hours": 4.0,
    "slippage_stress_pct": 0.002,
    "notional_size_usd": 60.71040000000001,
    "margin_usd": 8.69,
    "leverage": 7.0,
    "effective_leverage": 7.0,
    "contracts": 0.085,
    "margin_pct": 9.16,
    "entry_price": 714.24,
    "expected_slippage_pct": 0.002,
    "take_profit_price": 735.42,
    "take_profit_pct": 2.97,
    "stop_loss_price": 706.490234,
    "stop_loss_pct": 1.09,
    "risk_reward_ratio": 2.73,
    "candidate_net_rr": 2.0,
    "historical_payoff_ratio": 1.0,
    "liquidation_price": 634.68684,
    "estimated_liquidation_price": 634.68684,
    "is_estimated_liquidation": true,
    "limit_price": 715.428,
    "slip_buffer": 0.002,
    "maintenance_margin_pct": 0.03,
    "position_size_pct": 9.16,
    "bayes_strategy_multiplier": 1.0,
    "bayes_posterior_win_prob": 0.5,
    "bayes_strategy_bucket": "TRANSITION:BREAKOUT",
    "actual_risk_usd": 0.66,
    "total_actual_loss_usd": 0.72,
    "execution_status": "FILLED_AND_PROTECTED"
  },
  "execution_result": {
    "symbol": "BNB-USD",
    "status": "FILLED_AND_PROTECTED",
    "order_id": null,
    "executed_at": 1793523110.4301956,
    "error": null,
    "notional_usd": 60.71040000000001,
    "actual_fill_price": 714.24
  },
  "status": "APPROVED",
  "timestamp": "2026-11-01T01:51:50.641748"
}
```

### В. Запись из `active_positions.json`
```json
{
  "symbol": "BNB-USD",
  "direction": "LONG",
  "entry_price": 714.24,
  "size_usd": 60.71040000000001,
  "notional_usd": 60.71040000000001,
  "margin_used": 8.672914285714286,
  "tp_price": 735.42,
  "sl_price": 706.490234,
  "leverage": 7.0,
  "highest_price": 715.905,
  "lowest_price": 713.2549999999999,
  "protection_state": "SOFTWARE_SL_CONFIRMED",
  "atr_reference": 1.297664,
  "product_id": 14,
  "sender": "0x12d1293cbd98eea18a2e5906df5ed3bef07fb1aa64656661756c740000000000",
  "sl_digest": null,
  "tp_digest": null,
  "sl_type": "oracle_price_below",
  "tp_type": "last_price_above",
  "trigger_amount_x18": "-85000000000000000",
  "contracts": 0.085,
  "amount": 0.085,
  "funding_rate_hourly": 0.0,
  "holding_hours": 4.0,
  "slippage_stress_pct": 0.002,
  "accumulated_realized_pnl": 0.0,
  "original_thesis": "Bullish breakout confirmed by high volume and institutional accumulation. Neutral order book and overbought RSI reduce entry quality.",
  "native_triggers_placed": false,
  "open_time": 1793523108.6636465,
  "open_digest": "0xb80100808a7d3a1ba39d92a215265b6a2ea988e45cbf163495d31d611b703bc6",
  "planned_risk_usd": 0.82,
  "base_risk_pct": 0.015,
  "final_risk_pct": 0.008924999999999999,
  "cohort": "B",
  "entry_fee_usd": 0.035,
  "applied_multipliers": {
    "conf_mult": 1.0,
    "action_mult": 0.85,
    "signal_mult": 0.85,
    "quality_mult": 1.0,
    "dd_mult": 0.7,
    "kelly_mult": 1.0,
    "bayes_mult": 1.0,
    "combined_mult": 0.595
  },
  "strategy_mode": "BREAKOUT",
  "macro_regime": "TRANSITION",
  "entry_quality": 66.0,
  "bb_pos_pct": 162.63,
  "macro_range_score": 46.516666666666666,
  "max_favorable_excursion_r": 0.1726,
  "sl_protection_mode": "SOFTWARE_ONLY",
  "native_sl_placed": false,
  "native_tp_placed": false,
  "be_activation_r": 0.65,
  "tp1_r_mult": 0.65,
  "partial_tp_plan": {
    "symbol": "BNB-USD",
    "direction": "LONG",
    "entry_price": 714.24,
    "tp_final_price": 735.42,
    "total_contracts": 0.085,
    "tiers": [
      {
        "tier_index": 1,
        "fraction": 0.5,
        "target_price": 719.28,
        "close_contracts": 0.04,
        "cumulative_fraction": 0.5,
        "status": "PENDING",
        "is_final": false,
        "action": "PROTECT_BREAKEVEN",
        "order_digest": null,
        "filled_contracts": 0.0,
        "fill_price": null,
        "realized_pnl_usd": 0.0,
        "executed_at": null,
        "error_message": null,
        "retry_count": 0,
        "last_attempt_time": null,
        "requires_pnl_reconciliation": false,
        "pnl_reconciled": true
      },
      {
        "tier_index": 2,
        "fraction": 1.0,
        "target_price": 735.42,
        "close_contracts": 0.045,
        "cumulative_fraction": 1.0,
        "status": "PENDING",
        "is_final": true,
        "action": "CLOSE_ALL",
        "order_digest": null,
        "filled_contracts": 0.0,
        "fill_price": null,
        "realized_pnl_usd": 0.0,
        "executed_at": null,
        "error_message": null,
        "retry_count": 0,
        "last_attempt_time": null,
        "requires_pnl_reconciliation": false,
        "pnl_reconciled": true
      }
    ],
    "mode": "2_TIER",
    "min_size": 0.005,
    "size_increment": 0.005,
    "price_increment": 0.01,
    "remaining_contracts": 0.085,
    "be_activated": false,
    "created_at": 1793523110.4156175
  }
}
```
