# Data Flow Architecture

This document outlines the data lifecycle within the AI-Futures-Trader system: from retrieving raw metrics from decentralized sources to generating a final trading signal.

## 1. Data Sources
The system is strictly focused on decentralized markets (DEX) and relies on the following sources:
*   **DEX API / RPC Nodes:** Retrieval of price quotes, trading volumes, liquidity pool states, and network fees.
*   **On-Chain Metrics:** Tracking Open Interest (OI) and Funding Rates exclusively on decentralized derivatives platforms.
*   **News Aggregators / Web3 Sources:** Collection of market sentiment and news specifically related to targeted tokens and protocols.

## 2. Ingestion Layer
The `market_data_service.py` (located in the `services/` directory) is responsible for data collection. It queries the sources, filters out market noise, and constructs a standardized `MarketSnapshot` object.

## 3. Distribution Layer
The raw market snapshot is passed to the orchestrator (`main.py`), which segments the data into specific payloads tailored for each analytical agent:
*   Candlestick patterns -> `Candle_Agent`
*   Liquidity depth -> `Order_Book_Agent`
*   RSI/MACD values -> `Indicator_Agent`
*   Funding rates -> `OI_Funding_Agent`
*   News text -> `News_Agent`

## 4. Aggregation Layer
The specialized analysts return strongly typed signals (BULLISH/BEARISH/NEUTRAL + Confidence Score 1-100). These responses are aggregated into a single JSON object and routed to the `CEO_Agent` for the final strategic decision.

## 5. Storage Layer
Upon completion of the trading cycle, the final verdict, market state, and Risk Manager's decision are serialized and saved locally via the `Memory_Manager`. This builds the historical context required for future trade evaluations.
## 6. Service Implementation: MarketDataService

# Market Data Service

**File:** `python/services/market_data_service.py` (formerly `data_fetcher.py`)

## Overview
The `MarketDataService` acts as the primary data ingestion layer for the AI-Futures-Trader system. It is strictly designed to interact with decentralized exchanges (DEX) and on-chain data sources, ensuring the analytical agents receive clean, normalized, and accurate market context.

## Core Responsibilities
1. **DEX API & RPC Integration:** Connects to decentralized exchange endpoints and blockchain RPC nodes to fetch real-time market data, avoiding reliance on centralized exchange (CEX) feeds.
2. **Data Aggregation:** Collects a comprehensive suite of metrics required by the specialized analytical agents, including:
   * OHLCV (Open, High, Low, Close, Volume) candlestick data for price action analysis.
   * Order book depth, liquidity pool states, and slippage metrics.
   * On-chain derivatives data, specifically Open Interest (OI) and Funding Rates.
3. **Data Normalization:** Cleans and formats raw API responses into a standardized, strictly typed dictionary (or `MarketSnapshot` payload). This prevents `KeyError` exceptions and data type mismatches further down the analytical pipeline.
4. **Resilience & Rate Limiting:** Manages API request limits, connection timeouts, and retry logic to maintain continuous system operation even during network congestion or RPC node degradation.

## Input / Output Flow
* **Input:** Requires target trading pair symbols (e.g., `ETH/USDC`), specified timeframes (e.g., `15m`, `1h`), and configuration parameters (RPC URLs, API keys) passed from the core config module.
* **Output:** Returns a comprehensive, unified JSON-like structure containing all requested market metrics, ready for agent consumption.

## System Integration
This service operates at the very beginning of the data pipeline. In the orchestrator (`main.py`), it is invoked immediately after the `UniverseAgent` selects a target asset. The standardized data payload returned by this service is then sliced and distributed to the parallel analytical team (`Candle_Agent`, `Order_Book_Agent`, `Indicator_Agent`, `OI_Funding_Agent`, etc.).
