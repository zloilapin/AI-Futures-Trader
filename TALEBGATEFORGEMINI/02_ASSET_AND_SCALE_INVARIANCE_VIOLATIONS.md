# 02. НАРУШЕНИЯ ИНВАРИАНТНОСТИ АКТИВОВ И МАСШТАБА ДЕПОЗИТА (SCALE & ASSET INVARIANCE)

> **Нарушаемые законы Конституции:**
> **1. THE PRIME DIRECTIVE: SCALE & ASSET INVARIANCE TEST**
> *"Every mathematical formula and routing rule must remain valid across any asset (BTC, ETH, or illiquid alts) and any account equity ($85 or $85,000) without hardcoding coin-specific hacks or account-specific percentage clamps."*
> 
> **4. CONVEXITY, ERGODICITY & RUIN AVOIDANCE**
> *"Survival precedes optimization. Every trade and watchlist calculation must enforce the authoritative dollar risk cap as a Single Source of Truth. No single loss may exceed its pre-defined 1R dollar budget."*

---

## 1. КРИТИЧЕСКИЙ БАГ МАСШТАБА: Хардкод риска $1.35 и $2.00 на депозит ~$87

Это самая опасная архитектурная мина, заложенная в систему. Код был жестко откалиброван под конкретный баланс разработчика (~$87-$88 USDC).

### Файл 1: [python/agents/risk_manager.py#L240-L265](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L240-L265)
```python
@staticmethod
def get_effective_max_risk_usd(
    equity: float,
    trade_action: str = "ENTER",
    strategy_mode: str = "TREND_FOLLOWING",
    profile_rules: Optional[dict] = None
) -> float:
    """
    SSOT for Maximum Dollar Risk (1R Ceiling) across the entire trading system.
    Enforces dollar risk homogeneity:
      - REDUCE_SIZE / REVERSAL_TREND_SHIFT: min(0.0150 * equity, 1.35) (~$1.30 at $87 equity).
      - ENTER / Full Size: min(0.0215 * equity, 2.00) (~$1.85 at $87 equity).
    """
    eq = float(equity) if (equity is not None and equity > 0) else 88.0
    act = str(trade_action or "").upper().strip()
    mode = str(strategy_mode or "").upper().strip()

    if act == "REDUCE_SIZE" or mode == "REVERSAL_TREND_SHIFT":
        return round(min(0.0150 * eq, 1.35), 4)
    else:
        return round(min(0.0215 * eq, 2.00), 4)
```

### Файл 2: [python/services/nado_trading_service.py#L938-L946](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/services/nado_trading_service.py#L938-L946)
```python
max_allowed_risk = 1.35 if (str(locals().get("trade_action", "")).upper() == "REDUCE_SIZE") else float(approved_risk_usd) * 1.35
if (str(locals().get("trade_action", "")).upper() == "REDUCE_SIZE") and bumped_risk > 1.35:
    logger.error(f"[NadoTradingService] ❌ REDUCE_SIZE_RISK_CAP_VETO: Pre-dispatch bump risk (${bumped_risk:.2f}) exceeds $1.35 cap.")
    self.last_execution_status = "REDUCE_SIZE_RISK_CAP_VETO"
    return False
```

### Файл 3: [python/agents/risk_manager.py#L1954-L1958](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1954-L1958)
```python
max_risk_allowed = 1.35 if trade_action == "REDUCE_SIZE" else max_allowed_risk_budget
if bumped_risk > max_risk_allowed:
    veto_tag = "REDUCE_SIZE_RISK_CAP_VETO" if trade_action == "REDUCE_SIZE" else "MIN_NOTIONAL"
```

### Что происходит при изменении баланса (Катастрофический отказ масштабирования):
- **Сценарий А (Депозит $87):** $0.015 \times 87 = \$1.305 \le \$1.35$. Система худо-бедно работает.
- **Сценарий Б (Депозит $1,000):** Функция `get_effective_max_risk_usd` вычисляет $\min(0.015 \times 1000, 1.35) = \mathbf{\$1.35}$! При балансе в $1,000 риск на сделку остается равным **$1.35 (0.13% депозита)**!
- **Сценарий В (Депозит $10,000 или $50,000):** Риск на сделку ВСЕ РАВНО зажат на уровне **$2.00 (или $1.35)**!
- **Физический конфликт с биржей Nado CLOB:**
  - Минимальный размер ордера на Nado составляет $100.
  - Если максимальный допустимый риск жестко зажат константой `$1.35`, то максимальная дистанция стопа для минимального ордера ($100) составляет:
    $$\Delta_{\text{max}} = \frac{\$1.35}{\$100} = 1.35\%$$
  - Если реальный структурный стоп рынка на BTC, ETH или альткоине составляет $1.40\%$ или $2.0\%$, риск ордера составит $\$1.40$ или $\$2.00$, что превысит хардкод `$1.35`.
  - **Результат:** На любом депозите выше $100 система войдет в перманентный отказ: 100% сделок в режиме `REDUCE_SIZE` будут падать с ошибкой `REDUCE_SIZE_RISK_CAP_VETO`!

---

## 2. Искусственные процентные коридоры стоп-лосса по именам монет

Вместо расчета стопа от рыночной структуры $S$ (High/Low свинга) и волатильности $ATR$, в `RiskManager` прописаны жесткие рамки по именам монет:

### Файл: [python/agents/risk_manager.py#L1353-L1365](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1353-L1365)
```python
# Asset-Tier Dynamic Ceilings (Block 02 Post-Audit Patch):
# Tier 1 (BTC-USD, ETH-USD): base 1.65%, floor 1.20%, cap 1.75%
# Tier 2 (SOL-USD, BNB-USD): base 2.35%, floor 1.60%, cap 2.50%
# Tier 3 (ZEC-USD, HYPE-USD, LIT-USD, and other high-beta alts): base 3.10%, floor 1.95%, cap 3.25%
clean_sym = str(symbol).replace('/', '-').upper()
base_coin = clean_sym.split('-')[0].strip()
if base_coin in ("BTC", "ETH"):
    tier_base_pct, tier_min_pct, tier_max_pct = 1.65, 1.20, 1.75
elif base_coin in ("SOL", "BNB"):
    tier_base_pct, tier_min_pct, tier_max_pct = 2.35, 1.60, 2.50
else:
    tier_base_pct, tier_min_pct, tier_max_pct = 3.10, 1.95, 3.25
```

### Доказательство подгонки под прошлую сделку:
В тесте [tests/test_quantitative_patch_160_cycles.py#L105](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/tests/test_quantitative_patch_160_cycles.py#L105) автор прямо написал причину появления этих чисел:
> *«Tier 3 (ZEC, HYPE, LIT): base ceiling 3.10% (floor 1.95%, cap 3.25%). LIT-USD with 1.96% SL distance passes cleanly (previously vetoed by 1.95% flat limit).»*

- В цикле торговой сессии на 160 циклов у монеты `LIT-USD` дистанция до структурного уровня составила $1.96\%$.
- Предыдущий плоский лимит был $1.95\%$. Сделка была отклонена.
- Разработчик не стал делать универсальный расчет от ATR, а просто расширил потолок Tier 3 до $3.25\%$, чтобы сделка по LIT проходила.

---

## 3. Монетная дискриминация по минимальным дистанциям SL/TP

### Файл: [python/agents/risk_manager.py#L1298-L1310](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1298-L1310)
```python
elif strategy_mode == "VOLATILITY_MOMENTUM":
    min_sl_ratio = 0.006 if base_asset in ["BTC", "ETH"] else 0.010
    min_tp_ratio = 0.012 if base_asset in ["BTC", "ETH"] else 0.020
else:
    is_major = base_asset in ["BTC", "ETH"]
    min_sl_dist = current_price * (0.008 if is_major else 0.010)  # 0.8% floor for majors, 1.0% for alts
    max_sl_dist = current_price * (0.022 if is_major else 0.055)  # 2.2% ceiling for majors, 5.5% for alts
```

- Почему стоп на BTC не может быть 2.5%, если рынок сейчас находится в штормовой волатильности с ATR 3.0%? Он будет принудительно зажат в искусственные 2.2%.
- Почему стоп на альткоине не может быть 0.7%, если волатильность сжалась в пружину? Он будет искусственно раздут до 1.0%.
- Это нарушение принципа **«Microstructure over Apophenia»**: стоп должен стоять там, где объективно ломается тезис сделки, а не там, где его зажал процентный шаблон.

---

## 4. Разделение параметров ордеров и фильтров по тикерам

1. **Проскальзывание:** [risk_manager.py#L1256](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1256):
   `base_slip = 0.001 if base_asset in ["BTC", "ETH"] else 0.002`
2. **Безопасный бамп нотионала Nado CLOB:** [risk_manager.py#L1949, L2268](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L1949):
   `target_bump_notional = 108.50 if base_coin_b in ("BTC", "ETH") else 105.50`
3. **Порог ADX для ретеста:** [strategy_router.py#L512](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/strategy_router.py#L512):
   `req_retest_adx = 22.0 if base_coin in ["BTC", "ETH"] else 20.0`
4. **Ширина полос Боллинджера:** [strategy_router.py#L868](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/core/strategy_router.py#L868):
   `min_bb_width = 0.8 if base_sym in ["BTC", "ETH"] else 1.5`
5. **Хардкод точности контрактов:** [risk_manager.py#L2251](file:///c:/Users/Admin/Downloads/AI-Futures-Trader-main/python/agents/risk_manager.py#L2251):
   `NADO_SIZE_INCREMENTS = {"LIT-USD": 1.0, "SOL-USD": 0.1, "HYPE-USD": 0.1, "ZEC-USD": 0.01, ...}`
   *(Если биржа добавит новый актив или изменит спецификацию контракта, код упадет или отправит некорректный размер).*

