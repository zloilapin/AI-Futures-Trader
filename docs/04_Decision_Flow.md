# Decision Flow

This document details the step-by-step decision-making pipeline of the multi-agent system. Every trading cycle must pass through this strict validation sequence.

## Stage 1: Gatekeeping
1.  **Universe Agent:** Determines the list of available trading pairs on the DEX that currently possess sufficient liquidity for execution.
2.  **Scanner Agent:** Conducts a primary scan of the selected pairs. If the market exhibits extreme stagnation or anomalous volatility, the cycle is aborted. If conditions are favorable, the pair proceeds to deep analysis.

## Stage 2: Deep Analytics (Parallel Analysis)
A pool of highly specialized agents analyzes the pair's metrics in parallel. Each agent generates an independent signal, strictly ignoring centralized exchange (CEX) noise and focusing on on-chain realities:
*   `Candle_Agent` (Price Action)
*   `Indicator_Agent` (Mathematical momentum indicators)
*   `Order_Book_Agent` (DEX liquidity and order wall analysis)
*   `OI_Funding_Agent` (Derivatives metrics and long/short pressure)
*   `News_Agent` (Fundamental sentiment)

## Stage 3: Synthesis and Consensus (CEO Level)
All 5 analytical reports are submitted to the **CEO Agent**. The CEO may also query the **Memory Manager** for context regarding similar historical market conditions. The CEO weighs the arguments (e.g., dismissing a bullish indicator if the Order Book Agent reports severe resistance) and issues a final verdict: `LONG`, `SHORT`, or `HOLD`.

## Stage 4: Risk Management (Safety Check)
The CEO's verdict is forwarded to the **Risk Manager Agent**, which acts as the ultimate safety filter.
*   If volatility is excessive or the proposed leverage is too high, the Risk Manager blocks the trade (Veto).
*   If risk parameters are acceptable, it calculates the safe Position Size and optimal Stop-Loss levels.

## Stage 5: Execution and Notification
The approved signal is sent for execution. Concurrently, the **Telegram Agent** formats a readable alert for the user and dispatches it to the chat, while the **Memory Manager** logs the entire cycle into the local archive.
