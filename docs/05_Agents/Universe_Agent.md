# Universe Agent Specification v1.0

## 1. Overview
* **Role:** Market Universe Filter
* **Type:** Python Engine (Deterministic)
* **Domain:** Decentralized Exchanges (DEX)

## 2. Responsibility
The Universe Agent is the first layer of the funnel. It does not use LLMs. Its sole responsibility is to filter thousands of available DEX trading pairs down to a manageable "universe" of high-potential assets based on strict mathematical rules (liquidity thresholds, on-chain volume, and volatility).

## 3. Data Interface

### 3.1 Input Data (Raw DEX Data)
```json
{
  "timestamp": 1774618000,
  "exchange": "Nado DEX",
  "raw_pairs": [
    {"symbol": "ETH-USD", "liquidity_usd": 50000000, "24h_volume": 15000000, "volatility_index": 0.8},
    {"symbol": "SHIB-USD", "liquidity_usd": 15000, "24h_volume": 5000, "volatility_index": 2.1},
    {"symbol": "BTC-USD", "liquidity_usd": 120000000, "24h_volume": 45000000, "volatility_index": 0.4}
  ]
}

#### 3.2 Output Data (Filtered Universe)
'''json
{
  "timestamp": 1774618000,
  "filtered_universe": [
    {
      "symbol": "ETH-USD",
      "reason": "Passed liquidity (>1M) and volume thresholds."
    },
    {
      "symbol": "BTC-USD",
      "reason": "Passed liquidity (>1M) and volume thresholds."
    }
  ],
  "rejected_count": 1
}
4. Execution Rules
Rule 1: Reject any pair with liquidity under the defined minimum threshold (e.g., $1,000,000).
Rule 2: LLM must NOT be used for this filtering process.
