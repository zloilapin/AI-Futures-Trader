# System Architecture Overview (Nado DEX v2.0)

## 🏗 High-Level Architecture

The **AI-Futures-Trader** is structured into 4 decoupled, collaborative layers:

```
+-----------------------------------------------------------------------+
|                         TELEGRAM INTERFACE                            |
|             Interactive Commands (/status, /pnl, /scan)               |
|            Crisp Signal Notifications & Breakeven Alerts              |
+-----------------------------------------------------------------------+
                                  ^
                                  |
+-----------------------------------------------------------------------+
|                        AUTONOMOUS DAEMON (main.py)                    |
|          5-Minute Cycle Loop | 19:10 - 07:00 MSK Quiet Rest Window        |
+-----------------------------------------------------------------------+
                                  |
       +--------------------------+--------------------------+
       |                                                     |
+--------------+                                    +-------------------+
| MARKET DATA  |                                    |  PAPER TRADING    |
| (Kraken REST)|                                    |  (portfolio.json) |
| - 15m/1H/4H  |                                    | - $10,000 Balance |
| - Indicators |                                    | - Breakeven Guard |
| - Orderbook  |                                    | - Trailing Stop   |
| - Sentiment  |                                    | - Trade History   |
+--------------+                                    +-------------------+
       |                                                     |
+-----------------------------------------------------------------------+
|                           AGENT SYNDICATE                             |
|  UniverseAgent -> ScannerAgent (ATR Guard) -> Analyst Syndicate       |
|  (Candle, Indicator, OrderBook, OI/Funding, News)                     |
|  -> CEOAgent (MTF Alignment + Consensus)                              |
|  -> RiskManager (Profiles: AGGRESSIVE, BALANCED, CONSERVATIVE)        |
|  -> ReflectorAgent (Post-Trade Autopsy Feedback Loop)                |
+-----------------------------------------------------------------------+
```

## 🔄 Execution Pipeline Sequence

1. **Schedule Check:** Evaluates current MSK time (UTC+3). If between `19:10` and `07:00` MSK, pauses trading and logging.
2. **Universe Selection:** `UniverseAgent` selects liquid perpetual tickers (`BTC`, `ETH`, `SOL`).
3. **Paper Trading Check:** `PaperTradingService` evaluates active positions against live prices. If 50% TP distance is reached, moves Stop Loss to Breakeven (+0.1%). If TP or SL is hit, closes trade and triggers `ReflectorAgent` post-mortem.
4. **Pre-Flight Scanner:** `ScannerAgent` checks ATR-14 volatility (>0.15%) and orderbook spread (<=0.1%).
5. **Analyst Syndicate:** 5 specialized agents evaluate technicals, candle patterns, market microstructure, funding rates, and sentiment.
6. **CEO Consensus & MTF Alignment:** `CEOAgent` enforces 1H/4H macro trend alignment and 4/5 agent agreement.
7. **Risk Manager:** Evaluates `TRADING_PROFILE`, calculates exact Entry, TP, SL, USD Position Size, and Risk/Reward Ratio.
8. **Telegram Alert:** Generates structured Telegram signal card for approved trades with conviction >= 80%.
