# AI Futures Trader (Nado DEX - Ink L2) 🚀

> Institutional-grade multi-agent AI trading system for DEX cryptocurrency perpetual futures, powered by Groq (`llama-3.3-70b-versatile`), Multi-Timeframe Trend Alignment, Breakeven Risk Protection, and Self-Reflection Feedback Loops.

---

## 📊 System Overview & Status

- **Status:** Production Ready (v2.0)
- **Primary Exchange:** Nado DEX (Ink L2 by Kraken)
- **LLM Engine:** Groq API (`llama-3.3-70b-versatile`)
- **Execution Mode:** Automated Paper Trading & Telegram Signal Alerts
- **Scan Frequency:** Every 5 minutes (Configurable)
- **Rest Schedule:** 19:10 - 07:00 MSK (UTC+3)

---

## 🎯 Key Features & Risk Guards

1. **📐 Multi-Timeframe Trend Filter (15m + 1H + 4H):** Prevents counter-trend trading traps. LONG trades are executed ONLY when 1H/4H macro trends are bullish or neutral.
2. **🛡️ Breakeven Guard:** Automatically moves Stop Loss to Entry (+0.1% profit) as soon as price achieves 50% of the distance to Take Profit.
3. **🧠 Self-Reflection Feedback Loop (`ReflectorAgent`):** Performs post-trade autopsies on closed trades, extracts actionable negative pattern lessons, and saves them to `data/memory/lessons.json` to prevent repeating past mistakes.
4. **📊 ATR-14 Volatility Squeeze Guard:** `ScannerAgent` halts market scanning if ATR < 0.15% (choppy range consolidation) or spread > 0.1%.
5. **⚙️ Configurable Risk Profiles (`TRADING_PROFILE`):**
   - `AGGRESSIVE`: Min CEO conviction 75%, min R/R 1.3, position size 5%-10%.
   - `BALANCED`: Min CEO conviction 80%, min R/R 1.5, position size 3%-7%.
   - `CONSERVATIVE`: Min CEO conviction 85%, min R/R 2.0, position size 2%-4%.
6. **📲 Interactive Telegram Bot Commands:**
   - `/status` — View system state, risk profile, MSK time, and rest schedule.
   - `/pnl` or `/balance` — View virtual balance, total PnL ($ and %), win rate, and open positions.
   - `/scan` — Trigger an immediate on-demand market scan for all DEX assets.
   - `/help` — Interactive command help menu.

---

## 🏗 System Architecture

```
AI-Futures-Trader/
├── .env                       # API keys, Risk Profile, Schedule settings
├── README.md                  # System overview & installation guide
├── docs/                      # Full architecture specifications & agent docs
│   ├── Roadmap.md             # Project roadmap (Phases 1 - 5)
│   ├── 01_Project_Charter.md
│   ├── 02_System_Architecture.md
│   ├── 03_Data_Flow.md
│   ├── 04_Decision_Flow.md
│   └── 05_Agents/
│         ├── CEO_Agent.md
│         ├── Candle_Agent.md
│         ├── Indicator_Agent.md
│         ├── Order_Book_Agent.md
│         ├── OI_Funding_Agent.md
│         ├── News_Agent.md
│         ├── Risk_Manager.md
│         ├── Reflector_Agent.md
│         ├── Scanner_Agent.md
│         └── Telegram_Agent.md
├── python/
│   ├── main.py                # 24/7 Autonomous Trading Loop & Schedule Manager
│   ├── agents/                # AI Agent Syndicate
│   │     ├── base_agent.py
│   │     ├── universe_agent.py
│   │     ├── scanner_agent.py
│   │     ├── candle_agent.py
│   │     ├── indicator_agent.py
│   │     ├── order_book_agent.py
│   │     ├── oi_funding_agent.py
│   │     ├── news_agent.py
│   │     ├── ceo_agent.py
│   │     ├── risk_manager.py
│   │     ├── telegram_agent.py
│   │     ├── memory_manager.py
│   │     └── reflector_agent.py
│   ├── core/                  # Engine & LLM Client
│   │     ├── logger.py
│   │     └── llm_client.py
│   └── services/              # Live Market & Paper Trading Services
│         ├── market_data_service.py
│         ├── paper_trading_service.py
│         ├── telegram_service.py
│         └── telegram_bot_listener.py
└── data/
    └── memory/                # Persistent memory JSONs (portfolio.json, lessons.json)
```

---

## 🚀 Quick Start & Installation

### 1. Requirements
- Python 3.10+
- Groq API Key (`GROQ_API_KEY`)
- Telegram Bot Token & Chat ID

### 2. Setup Environment Variables
Configure `.env`:
```env
GROQ_API_KEY=gsk_your_groq_api_key_here
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Nado DEX Settings
SCAN_INTERVAL_MINUTES=5
TRADING_PROFILE=BALANCED
REST_START_TIME=19:10
REST_END_TIME=07:00
TIMEZONE_OFFSET=3
```

### 3. Run Autonomous 24/7 Bot
```bash
py python/main.py
```

### 4. Run Single Test Cycle
```bash
py python/main.py --once
```

---

## 📄 License

MIT License. Free for research and quantitative DEX trading.
