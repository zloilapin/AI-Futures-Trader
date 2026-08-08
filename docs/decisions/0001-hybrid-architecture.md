# ADR 0001: Hybrid Asynchronous Architecture & Project Structure

## Status
Accepted

## Context
We are developing an automated AI-agent trading system for cryptocurrency futures on decentralized exchanges (DEX). We need to balance data collection speed, analytical quality, and absolute capital safety.

## Decision
1. **Hybrid Agent Pipeline:**
   * Analytical agents (Candles, Order Book, News, Indicators) run **concurrently** using `asyncio` to minimize latency.
   * The CEO Agent runs **sequentially after** the analysts, aggregating their reports into a unified trading verdict.
   * The Risk Manager acts as a **deterministic (non-LLM) module** after the CEO, holding absolute veto power based on strict mathematical limits.
2. **Documentation Structure:**
   * All project documentation is strictly categorized into directories (`docs/03_Agents/`, `docs/04_DataFlow/`, `docs/05_DecisionFlow/`, `docs/06_ADR/`).
3. **Logging:**
   * Logs and structured reports are saved hierarchically: the folder for a specific day is created strictly inside the `month-1` folder.

## Consequences
* **Pros:** High response speed to market changes, full transparency and traceability of every decision, elimination of human error and LLM hallucinations during risk calculation.
* **Cons:** Requires careful handling of asynchronous Python code.
* 
