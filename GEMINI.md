# QUANTIX AI — ARCHITECTURAL CONSTITUTION & SYSTEM RULES

## 1. THE PRIME DIRECTIVE: ZERO CURVE-FITTING (ANTI-HINDSIGHT LAW)
- **No Hindsight Parameter Tuning:** Never modify numerical thresholds (e.g., tweaking `ADX` from `20.0` to `18.5`, `RSI` from `50` to `53`, or conviction from `65%` to `62%`) merely to make a specific historical trade from recent logs pass or fail. Fitting parameters to past logs is buying a lottery ticket with yesterday's winning numbers (Taleb's Turkey Problem).
- **Scale & Asset Invariance Test:** Every mathematical formula and routing rule must remain valid across any asset (`BTC`, `ETH`, or illiquid alts) and any account equity (`$85` or `$85,000`) without hardcoding coin-specific hacks or account-specific percentage clamps.
- **No Manual State Patching:** Never manually edit specific coin entries in runtime JSON files (`pullback_watchlist.json`, `active_positions.json`, `trade_journal.json`) to fix a state issue. Instead, build self-healing, deterministic validation logic in the code that handles state hydration universally.

## 2. VIA NEGATIVA (SUBTRACTION OVER ADDITION)
- **Fix by Removing Contradictions:** When a pipeline bug or deadlock is identified, prioritize removing redundant legacy overrides, conflicting thresholds, and dead code rather than stacking new heuristic filters or magic numbers on top.
- **Zero New Magic Constants:** Do not introduce arbitrary penalty points (e.g., `-5 pts`), ad-hoc multipliers, or uncalibrated percentage corridors. Rely on existing Single Source of Truth (SSOT) constants or first-principles market relationships (ATR, structural swing extremes, fee/slippage math).
- **Early Noise Rejection:** Filter flat/chop noise (`RANGE_CHOPPY`, low `ADX`) deterministically at the earliest pre-debate gate (0 LLM token cost, 0 watchlist pollution) rather than letting noise enter the funnel and patching it downstream.

## 3. MICROSTRUCTURE OVER APOPHENIA (REALITY OVER GEOMETRY)
- **Stops Belong Behind Real Structure:** A stop-loss represents the price where the trade thesis is objectively falsified (behind the actual swing high/low plus a volatility noise buffer). Never artificially compress a structural stop into a narrow percentage range just to force a trade into a small account's dollar risk cap.
- **Inverse Entry Solving ($P^*$):** When a true structural stop is too wide for immediate market entry on minimum CLOB notional, use the inverse pullback solver (`solve_optimal_pullback_entry`) to compute the exact limit/retest price $P^*$ that satisfies the dollar risk cap, or discard the setup if $P^*$ is unreachable.
- **Exact Execution Physics:** Always account for real CLOB microstructure in closed-form math: minimum exchange notional, round-trip taker/maker fees, and execution slippage buffers.

## 4. CONVEXITY, ERGODICITY & RUIN AVOIDANCE (EXTREMISTAN SURVIVAL)
- **Strict Left-Tail Truncation (1R Homogeneity):** Survival precedes optimization. Every trade and watchlist calculation must enforce the authoritative dollar risk cap (`RiskManager.get_effective_max_risk_usd`) as a Single Source of Truth. No single loss may exceed its pre-defined `1R` dollar budget.
- **Right-Tail Preservation (Positive Gamma):** Do not choke favorable trend excursions with overly tight trailing stops inside normal `15m` volatility noise. Protect capital via staged risk reduction (`HALF_RISK_LOCK` -> `BREAKEVEN_LOCK` -> `STEPPED_ATR_TRAIL`) while leaving upside convexity open for fat-tail moves.
- **State-Machine & Timeframe Symmetry:** Account for indicator phase lag mathematically (e.g., `15m/1H` structural breaks `CHoCH` lead `4H` moving averages by hours). Ensure pre-debate gates, watchlist retest gates, and risk manager gates share symmetric validation contracts.

## 5. MANDATORY PRE-PROPOSAL SELF-AUDIT PROTOCOL (`[TALEB GATE]`)
Before proposing or implementing ANY change to trading, routing, scoring, or risk logic, you MUST explicitly output this 4-point verification block in your response:
1. **Root Cause Type:** Is this a true software/mathematical contract bug (state deadlock, SSOT mismatch, phase-lag conflict) or normal statistical market variance (a valid setup hitting a standard `-1R` stop or waiting in flat chop)? *(If it is normal market variance, DO NOT change the code!)*
2. **Hindsight Check:** Does this proposal introduce any new magic numbers or tune thresholds to fit a specific past cycle? *(Must be NO).*
3. **Via Negativa Check:** Can this be solved by unifying an existing contract or deleting a legacy override instead of adding new parameters?
4. **Convexity & Invariance Impact:** How does this preserve left-tail risk capping (`1R` homogeneity) and right-tail upside across any balance or volatility regime?

---

## 6. ESSENTIAL VERIFICATION & TEST COMMANDS

### Core Regression Test Suite (Must Pass 100% Before Any Push)
```bash
pytest tests/test_risk_homogeneity_and_quarantine.py tests/test_structural_reversal_and_uturn.py tests/test_systemic_upgrade_modules_1_to_4.py tests/test_risk_manager_min_notional_software_tpsl.py -v
```

### Run Autonomous Trading Loop
```bash
python python/main.py
```
