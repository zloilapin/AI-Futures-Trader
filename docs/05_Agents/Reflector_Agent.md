# Agent Specification: Reflector Agent (`ReflectorAgent`)

## 📌 Role & Mission
The **Reflector Agent** is the self-reflection and post-trade autopsy engine of the AI-Futures-Trader system.
Whenever a position closes (especially if triggered by Stop Loss), `ReflectorAgent` performs a post-mortem analysis to identify the root cause of failure, extracts an actionable trading lesson, and saves negative pattern warnings to `data/memory/lessons.json`.

---

## 🛠 Key Responsibilities
1. **Post-Trade Autopsy:** Evaluates closed position data (Entry, Exit, PnL, Triggered By) against live market context at closure.
2. **Actionable Lesson Extraction:** Formulates strict negative pattern rules (e.g., `"WARNING: Do not open LONG when 4H trend is BEARISH and RSI > 60"`).
3. **Feedback Loop Integration:** Feeds recent learned rules directly into `CEOAgent` prompts to prevent repeating past mistakes.

---

## 📥 Input Data
- Closed trade record from `PaperTradingService` (`symbol`, `direction`, `entry_price`, `exit_price`, `pnl_usd`, `pnl_pct`, `triggered_by`)
- Market snapshot context at time of trade execution and closure

---

## 📤 Output Format (JSON)
```json
{
  "trade_outcome": "WIN" | "LOSS",
  "root_cause": "Entered LONG into 4H bear trend during low-volume RSI divergence",
  "actionable_rule": "WARNING: Avoid LONG when 4H trend is BEARISH and funding rate is negative",
  "reasoning": "Post-mortem analysis confirmed counter-trend price rejection at 4H EMA-20 resistance."
}
```
