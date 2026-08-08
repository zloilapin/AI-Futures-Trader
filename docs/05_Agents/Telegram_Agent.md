# Telegram Agent

**File:** `python/agents/telegram_agent.py`

## Overview
The `Telegram_Agent` acts as the primary communication bridge between the AI-Futures-Trader system and the human operator. While the other agents process raw data and output strict JSON payloads, the Telegram Agent translates these complex algorithmic decisions into readable, well-formatted, and visually structured alerts.

## Core Responsibilities
1. **Message Formatting:** Transforms nested JSON outputs (such as the CEO's final verdict and the Risk Manager's position sizing) into clear, aesthetically pleasing Markdown/HTML messages using appropriate emojis and formatting.
2. **Alert Dispatching:** Sends immediate notifications for critical events, including approved trade signals, blocked trades (vetoed by Risk Manager), and critical system errors.
3. **Daily Summaries (Optional):** Can be configured to compile and send periodic performance heartbeats or daily summaries of system activity.

## Input / Output Flow
* **Input:** Raw JSON dictionaries containing trading signals, risk assessments, error logs, or general system status updates.
* **Output:** A formatted string that is then passed to the underlying `TelegramService` for actual transmission via the Telegram Bot API.

## System Integration
This agent is typically invoked at the very end of the decision pipeline in `main.py`. Once the `Risk_Manager` finalizes its verdict, the `Telegram_Agent` ensures the user is immediately notified of the outcome. It interacts closely with `python/services/telegram_service.py`, which handles the actual network requests to Telegram's servers.

