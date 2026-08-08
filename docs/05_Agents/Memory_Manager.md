# Memory Manager Agent

**File:** `python/agents/memory_manager.py`

## Overview
The `Memory_Manager` acts as the historian and long-term storage layer of the AI-Futures-Trader system. It provides the system with a form of persistent state, allowing the AI agents to "remember" past decisions, market conditions, and the outcomes of previous trades. This historical context is vital for adaptive learning and preventing the system from repeating the same mistakes in similar market regimes.

## Core Responsibilities
1. **Context Archiving:** Serializes and securely stores the final trading verdicts, market snapshots, and Risk Manager decisions after every complete cycle.
2. **Context Retrieval:** Queries the local storage to retrieve relevant historical data based on current market conditions, feeding this context to the `CEO_Agent` before a final decision is made.
3. **Performance Tracking:** Maintains a high-level ledger of recent successes, failures, and vetoed trades to help the system dynamically adjust its risk appetite.

## Input / Output Flow
* **Input:** Receives comprehensive JSON payloads containing the completed trade cycle data (CEO verdict, Analyst reports, Risk Manager approval/veto) for storage.
* **Output:** When queried by the CEO, it outputs a summarized historical context (JSON or formatted string) detailing how similar setups performed in the past.

## System Integration
The `Memory_Manager` is queried by the `CEO_Agent` during *Stage 3: Synthesis* to incorporate past lessons into current decisions. It is also invoked by the orchestrator (`main.py`) at the very end of the cycle (*Stage 5: Execution*) to securely write the latest cycle's data to the disk or database.

