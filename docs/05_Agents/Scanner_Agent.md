# Scanner Agent Specification v1.0

## 1. Overview
* **Role:** Market Opportunity Scanner
* **Type:** Lightweight LLM Agent (e.g., Claude 3 Haiku / GPT-4o-mini)
* **Domain:** Decentralized Exchanges (DEX)

## 2. Responsibility
The Scanner Agent receives the mathematically filtered list of assets from the Universe Agent. Its responsibility is to analyze the broader market context (e.g., Bitcoin trend, sector narratives) and select the top 1-2 pairs that present the most immediate trading opportunities. It acts as a smart filter before spending compute resources on deep analysis.

## 3.1 Input Data (From Universe Agent)

    {
      "timestamp": 1774618050,
      "filtered_universe": [
        {"symbol": "ETH-USD", "24h_change_pct": 5.2, "volume_spike_multiplier": 2.1},
        {"symbol": "BTC-USD", "24h_change_pct": 1.1, "volume_spike_multiplier": 0.9}
      ],
      "macro_context": {
        "overall_trend": "Bullish",
        "recent_news_sentiment": "Positive"
      }
    }

## 3.2 Output Data (Selected Targets)
*Passed to the Pair Analyst Agents (Candle, Order Book, etc.).*

    {
      "target_pairs": ["ETH-USD"],
      "reasoning": "ETH-USD is showing a 2.1x volume spike aligning with the overall bullish macro context, whereas BTC volume is relatively flat. Prioritizing ETH for deep analysis.",
      "confidence_score": 85
    }

## 4. Execution Rules
* **Rule 1:** Must select a maximum of 2 pairs to prevent overloading the downstream Analyst Agents.
* **Rule 2:** Must explicitly state the reasoning for the selection.
* 
