# CEO Agent Specification v1.0

## 1. Overview
* **Role:** Lead Decision Maker & Strategy Coordinator
* **Type:** High-Reasoning LLM Agent (e.g., GPT-4o / Claude 3.5 Sonnet)
* **Domain:** Consensus Evaluation, Trade Generation, and Memory Integration

## 2. Responsibility
The CEO Agent acts as the final decision-maker before a trade is sent to the Risk Management layer. It receives structured reports from all Pair Analyst Agents (Candle, Order Book, Indicator, News, OI/Funding). Its responsibility is to resolve conflicting signals, cross-reference the current setup with historical memory (past trade outcomes), and generate a definitive, highly structured Trade Request.

## 3.1 Input Data (From Analyst Agents & Memory Layer)

    {
      "symbol": "ETH-USD",
      "timestamp": 1774618400,
      "analyst_reports": {
        "CandleAgent": {"bias": "BULLISH", "confidence_score": 75},
        "OrderBookAgent": {"bias": "BULLISH", "confidence_score": 85},
        "IndicatorAgent": {"bias": "BULLISH", "confidence_score": 70},
        "OIFundingAgent": {"bias": "BEARISH", "confidence_score": 80},
        "NewsAgent": {"bias": "NEUTRAL", "confidence_score": 0}
      },
      "memory_context": "In the last 3 trades on ETH-USD with high bullish microstructure but bearish funding rates (high risk of long squeeze), the trades resulted in a loss. Proceed with extreme caution."
    }

## 3.2 Output Data (Trade Request to Risk Manager)

    {
      "request_id": "trade_req_1045",
      "action": "SKIP",
      "symbol": "ETH-USD",
      "overall_confidence": 20,
      "thesis": "Despite strong bullish confluence from Candle, Order Book, and Indicator agents, the OIFunding agent warns of an overheated market. Historical memory indicates a high failure rate for this specific contrarian setup. Opting to preserve capital."
    }

## 4. Execution Rules
* **Rule 1:** Must synthesize all inputs and explicitly weigh contradictions (e.g., Technicals vs. Derivatives Sentiment).
* **Rule 2:** Historical memory must be heavily weighted. Do not repeat documented mistakes.
* **Rule 3:** The output `action` must strictly be one of: `OPEN_LONG`, `OPEN_SHORT`, or `SKIP`.
* **Rule 4:** A trade request requires an overall confidence score above a predefined threshold (e.g., > 75) to be considered by the Risk Manager.
* 
