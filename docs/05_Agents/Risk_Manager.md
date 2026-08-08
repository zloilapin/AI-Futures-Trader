# Risk Manager Agent

**File:** `python/agents/risk_manager.py`

## Overview
The `Risk_Manager` serves as the final, uncompromising gatekeeper of the AI-Futures-Trader system. While the `CEO_Agent` focuses on profitability and market direction, the Risk Manager is solely dedicated to capital preservation. It has absolute veto power over any trade proposed by the CEO and calculates exact position sizes to ensure mathematical safety within the volatile decentralized exchange (DEX) environment.

## Core Responsibilities
1. **Absolute Veto Power:** Evaluates the CEO's trade proposal against strict predefined risk parameters (e.g., maximum daily drawdown, correlation limits). It can reject a `LONG` or `SHORT` decision, effectively turning it into a `HOLD`.
2. **Dynamic Position Sizing:** Calculates the exact allocation size using fractional risk modeling, adjusted by the `conviction` score provided by the CEO Agent.
3. **Exposure Management:** Monitors current open positions across the DEX to ensure the system is not over-leveraged or over-exposed to a single asset class.

## Input / Output Flow
* **Input:** The final JSON decision payload from the `CEO_Agent`, coupled with the current portfolio balance and active exposure limits.
* **Output:** A definitive JSON verdict specifying whether the trade is `approved` (boolean), the `adjusted_position_size_pct`, and a `risk_reasoning` statement.

## System Integration
This agent is invoked immediately after the `CEO_Agent` successfully generates a trading signal. Only if the `Risk_Manager` returns `"approved": true` does the system proceed to send a Telegram alert and (eventually) route the order to the execution service. Its verdicts are also permanently logged by the `Memory_Manager` for future system audits.

