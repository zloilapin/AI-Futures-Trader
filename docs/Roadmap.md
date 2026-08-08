# Project Roadmap: AI-Futures-Trader (Nado DEX v2.0)

## Phase 1: Documentation & Architecture Specifications
* [x] Project Charter & System Overview (`docs/01_Project_Charter.md`)
* [x] Core Agents Specifications (`docs/05_Agents/`)
* [x] Data Flow Documentation (`docs/03_Data_Flow.md`)
* [x] Decision Flow Documentation (`docs/04_Decision_Flow.md`)

## Phase 2: Live Market Data Engine & Multi-Timeframe Scanning
* [x] Setup root `python/` modular structure
* [x] Implement live market data fetchers from Kraken REST API (OHLCV, Order Book Depth, Spread)
* [x] Multi-Timeframe Alignment (15m + 1H + 4H Trend Alignment)
* [x] Technical Indicators (RSI-14, EMA-20, MACD, ATR-14 Volatility Guard)
* [x] Crypto Sentiment & Fear & Greed Index integration (Alternative.me API)

## Phase 3: Agent Syndicate & Institutional Decision Matrix
* [x] Universe Agent (Nado DEX single asset perp ticker selection: BTC, ETH, SOL)
* [x] Scanner Agent with ATR Volatility Squeeze Guard (<0.15% halt) & Spread Guard (0.1% limit)
* [x] Specialized Analyst Syndicate: Candle, Indicator, OrderBook, OI/Funding, News
* [x] CEO Agent with 4/5 Multi-Agent Consensus & MTF Trend Alignment Rules
* [x] Risk Manager with Dynamic Risk Profiles (`AGGRESSIVE`, `BALANCED`, `CONSERVATIVE`)
* [x] Groq LLM integration (`llama-3.3-70b-versatile`) with automatic 429 rate-limit backoff

## Phase 4: Paper Trading, Breakeven Guard & Self-Reflection
* [x] Paper Trading Engine (`services/paper_trading_service.py`) with $10,000 virtual balance
* [x] Breakeven Guard: automatically moves Stop Loss to Entry +0.1% at 50% TP distance
* [x] Reflector Agent (`agents/reflector_agent.py`): Post-trade autopsy & feedback loop (`data/memory/lessons.json`)
* [x] Crisp Telegram signal card generator (Symbol, Direction LONG/SHORT, Entry, TP, SL, Amount $)

## Phase 5: Autonomous 24/7 Operations & Interactive Telegram Commands
* [x] 5-minute continuous scanning daemon with graceful shutdown
* [x] Quiet Rest Window (19:10 - 07:00 MSK time-zone aware scheduling)
* [x] Interactive Telegram Commands (`/status`, `/pnl`, `/balance`, `/scan`, `/help`)
* [x] Full System Verification & Production Release (v2.0)
