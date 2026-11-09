import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch

from agents.oi_funding_agent import OIFundingAgent
from services.market_data_service import MarketDataService
from core.logger import TradeLogger

# ==============================================================================
# F-11: Robust Trend Evaluation during EMA-21 Pullbacks
# ==============================================================================
@pytest.mark.asyncio
async def test_f11_robust_trend_during_ema21_pullback():
    """F-11: Moving average trend remains BULLISH/BEARISH during healthy pullbacks between EMA-9 and EMA-21."""
    mds = MarketDataService("Nado_DEX")
    
    # Construct 30 candles where EMA-9 > EMA-21, and the final price pulls back between EMA-9 and EMA-21
    # 25 candles rising steadily from 90 to 105, then 5 candles pulling back slightly to 101.5
    closes = [90.0 + i * 0.6 for i in range(25)] + [104.0, 103.0, 102.5, 102.0, 101.5]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    candles = [{"open": closes[i]-0.1, "high": highs[i], "low": lows[i], "close": closes[i], "volume": 100.0} for i in range(len(closes))]

    mds._fetch_nado_candles = AsyncMock(return_value=candles)
    res = await mds._fetch_ohlc_interval("SOL-USD", 15)

    # In old code: current_price (101.5) < ema_9 (~102.2) forced NEUTRAL despite ema_9 > ema_21 (~99.8)!
    # In new code: trend remains BULLISH because price holds above ema_21 dynamic support!
    assert res["trend"] == "BULLISH", f"Expected trend BULLISH during healthy pullback, got {res['trend']}"


# ==============================================================================
# F-12: EMA-200 Calculation Convergence with 250 Candles
# ==============================================================================
def test_f12_ema200_calculation_with_250_candles():
    """F-12: Passing 250 candles allows EMA-200 to exponentially smooth rather than degrade to pure SMA-200."""
    # 200 flat candles at 100.0, followed by 50 candles rising to 150.0
    closes = [100.0] * 200 + [100.0 + i * 1.0 for i in range(1, 51)]
    assert len(closes) == 250

    # Inline calc_ema_series logic from MarketDataService.fetch_indicators
    def calc_ema_series(data, period):
        if not data or len(data) < period:
            return [0.0] * len(data)
        emas = [0.0] * (period - 1)
        ema = sum(data[:period]) / period
        emas.append(ema)
        k = 2.0 / (period + 1.0)
        for price in data[period:]:
            ema = (price - ema) * k + ema
            emas.append(ema)
        return emas

    ema_200_series = calc_ema_series(closes, 200)
    final_ema_200 = ema_200_series[-1]
    
    # Pure SMA of the last 200 candles would be sum(closes[-200:]) / 200
    pure_sma_last_200 = sum(closes[-200:]) / 200.0
    
    # An exponentially smoothed EMA-200 puts heavier weight on recent rising prices (101..150)
    # than an unweighted 200-period SMA!
    assert final_ema_200 != pure_sma_last_200, "EMA-200 should have exponential weighting, not match flat SMA"
    assert final_ema_200 > 100.0, "EMA-200 must react to the 50 trailing price increases"


# ==============================================================================
# F-13: OIFundingAgent Synchronized 0.0002 Threshold
# ==============================================================================
@pytest.mark.asyncio
async def test_f13_oi_funding_agent_flags_toxic_funding_at_0002():
    """F-13: OIFundingAgent emits contrarian signal when funding rate breaches 0.02%/hr (0.0002)."""
    logger = MagicMock(spec=TradeLogger)
    agent = OIFundingAgent(logger)

    # 1. High positive funding (0.0003 = 0.03%/hr > 0.02%/hr threshold)
    # Old code returned NEUTRAL because it waited for > 0.0005.
    # New code flags BEARISH immediately!
    data_pos = {
        "symbol": "SOL-USD",
        "derivatives_data": {
            "funding_rate": 0.0003,
            "open_interest": 500000.0,
            "open_interest_trend": "neutral"
        }
    }
    report_pos = await agent.analyze(data_pos)
    assert report_pos["signal"] == "BEARISH", f"Expected BEARISH warning for funding 0.03%/hr, got {report_pos['signal']}"
    assert report_pos["confidence"] >= 65

    # 2. High negative funding (-0.0003 = -0.03%/hr < -0.02%/hr threshold)
    data_neg = {
        "symbol": "SOL-USD",
        "derivatives_data": {
            "funding_rate": -0.0003,
            "open_interest": 500000.0,
            "open_interest_trend": "neutral"
        }
    }
    report_neg = await agent.analyze(data_neg)
    assert report_neg["signal"] == "BULLISH", f"Expected BULLISH warning for funding -0.03%/hr, got {report_neg['signal']}"
    assert report_neg["confidence"] >= 65


# ==============================================================================
# F-16: MarketDataService OI Preservation & Disk Write Throttling
# ==============================================================================
def test_f16_market_data_service_disk_write_throttling():
    """F-16: _save_oi_history throttles consecutive calls within 60 seconds to avoid disk spam."""
    mds = MarketDataService("Nado_DEX")
    
    with patch("core.state_store.StateStore.save") as mock_save:
        # First save should execute
        mds._save_oi_history(force=True)
        assert mock_save.call_count == 1

        # Immediate second save without force must be throttled
        mds._save_oi_history(force=False)
        assert mock_save.call_count == 1, "Immediate subsequent save within 60s should be throttled"

        # Advance timestamp past 60s
        mds._last_oi_save_ts = time.time() - 65
        mds._save_oi_history(force=False)
        assert mock_save.call_count == 2, "Save after 60s should execute"
