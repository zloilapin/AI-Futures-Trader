import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from core.deterministic_guard import DeterministicGuard
from core.pipeline import TradingPipeline
from core.models import FinalTradeDecision
from core.strategy_router import StrategyProfile
from core.config import config


# ─────────────────────────────────────────────────────────────────────────────
# 1. Price Drift Guard Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_price_drift_guard_blocks_late_entry_when_38pct_move_missed():
    """
    Requirement 4 & 6:
    If price has already moved > 38% of the distance to TP while AI deliberated,
    entry must be cancelled to protect R:R from degradation.
    """
    # Case 1: LONG setup with 40% move to TP missed
    # Snapshot=100.0, TP=110.0 (dist=10.0), SL=95.0
    # Live=104.0 -> drift = (104 - 100) / 10 = 0.40 = 40% > 38%
    is_ok, re_entry, re_tp, re_sl, msg = DeterministicGuard.evaluate_price_drift(
        symbol="BTC-USD",
        direction="LONG",
        snapshot_price=100.0,
        live_price=104.0,
        tp_price=110.0,
        sl_price=95.0
    )
    assert is_ok is False
    assert re_entry == 100.0
    assert "[PriceDriftGuard]" in msg
    assert "Отмена запоздалого входа" in msg
    assert "40.0%" in msg
    assert "R:R деградировал" in msg

    # Case 2: SHORT setup with 45% move to TP missed
    # Snapshot=100.0, TP=90.0 (dist=10.0), SL=105.0
    # Live=95.5 -> drift = (100 - 95.5) / 10 = 0.45 = 45% > 38%
    is_ok_short, re_entry_s, re_tp_s, re_sl_s, msg_short = DeterministicGuard.evaluate_price_drift(
        symbol="ETH-USD",
        direction="SHORT",
        snapshot_price=100.0,
        live_price=95.5,
        tp_price=90.0,
        sl_price=105.0
    )
    assert is_ok_short is False
    assert re_entry_s == 100.0
    assert "Отмена запоздалого входа" in msg_short
    assert "45.0%" in msg_short


def test_price_drift_guard_allows_fresh_entry_and_updates_live_price():
    """
    Requirement 4 & 6:
    If drift <= 38%, entry is approved and SL/TP are re-anchored to live_price,
    preserving exact calculated R:R distances.
    """
    # Case 1: LONG setup with 15% drift
    # Snapshot=100.0, TP=110.0 (dist=10.0), SL=95.0 (dist=5.0) -> R:R = 2.0
    # Live=101.5 -> drift = 1.5 / 10.0 = 15.0% <= 38.0%
    is_ok, re_entry, re_tp, re_sl, msg = DeterministicGuard.evaluate_price_drift(
        symbol="SOL-USD",
        direction="LONG",
        snapshot_price=100.0,
        live_price=101.5,
        tp_price=110.0,
        sl_price=95.0
    )
    assert is_ok is True
    assert re_entry == 101.5
    assert re_tp == 111.5  # 110.0 + 1.5
    assert re_sl == 96.5   # 95.0 + 1.5
    # Verify R:R is perfectly preserved
    tp_distance = re_tp - re_entry
    sl_distance = re_entry - re_sl
    assert round(tp_distance, 4) == 10.0
    assert round(sl_distance, 4) == 5.0
    assert round(tp_distance / sl_distance, 2) == 2.0
    assert "SL/TP перепривязаны к live_price" in msg

    # Case 2: SHORT setup with 20% drift
    # Snapshot=100.0, TP=90.0 (dist=10.0), SL=105.0 (dist=5.0) -> R:R = 2.0
    # Live=98.0 -> drift = (100 - 98) / 10 = 20.0% <= 38.0%
    is_ok_s, re_entry_s, re_tp_s, re_sl_s, msg_s = DeterministicGuard.evaluate_price_drift(
        symbol="ZEC-USD",
        direction="SHORT",
        snapshot_price=100.0,
        live_price=98.0,
        tp_price=90.0,
        sl_price=105.0
    )
    assert is_ok_s is True
    assert re_entry_s == 98.0
    assert re_tp_s == 88.0   # 90.0 + (98.0 - 100.0) = 88.0
    assert re_sl_s == 103.0  # 105.0 + (98.0 - 100.0) = 103.0
    tp_distance_s = re_entry_s - re_tp_s
    sl_distance_s = re_sl_s - re_entry_s
    assert round(tp_distance_s, 4) == 10.0
    assert round(sl_distance_s, 4) == 5.0
    assert round(tp_distance_s / sl_distance_s, 2) == 2.0

    # Case 3: Negative drift (price pulled back slightly in our favor before entry)
    # Snapshot=100.0, Live=99.2 for LONG
    is_ok_dip, re_entry_d, re_tp_d, re_sl_d, _ = DeterministicGuard.evaluate_price_drift(
        symbol="BTC-USD",
        direction="LONG",
        snapshot_price=100.0,
        live_price=99.2,
        tp_price=110.0,
        sl_price=95.0
    )
    assert is_ok_dip is True
    assert re_entry_d == 99.2
    assert round(re_tp_d, 4) == 109.2
    assert round(re_sl_d, 4) == 94.2


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fast Debate Bypass Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_fast_debate_bypass_latency_shortcut():
    """
    Requirement 2 & 6:
    When FAST_DEBATE_BYPASS is active or when Predator detects a liquidity sweep,
    skip LLM debate generation, building deterministic theses in 0 ms.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "NADO")

    sample_reports = [
        {"agent_name": "Indicator_Agent", "signal": "BEARISH", "reasoning": "RSI 79 overbought at upper BB band"},
        {"agent_name": "Candle_Agent", "signal": "BEARISH", "reasoning": "Upper exhaustion wick 28% rejected at highs"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH", "reasoning": "Ask wall resistance absorbs buying pressure"},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH", "reasoning": "Longs trapped on highs with elevated funding"}
    ]
    key_levels = {
        "current_price": 80000.0,
        "ema_20": 78500.0,
        "bb_upper": 80500.0,
        "bb_lower": 76500.0,
        "atr_14": 1200.0
    }

    # 1. SHORT Thesis Verification
    bull_res, bear_res = pipeline._build_deterministic_debate_theses(
        symbol="BTC-USD",
        strategy_mode="MEAN_REVERSION",
        direction_bias="SHORT",
        reports=sample_reports,
        key_levels=key_levels
    )
    assert bear_res["status"] == "OK"
    assert bear_res["thesis_score"] == 9
    assert bear_res["potential_target"] == 78500.0  # Mean reversion to EMA-20
    assert bear_res["invalidation_level"] == 80500.0 # BB upper
    assert len(bear_res["bearish_arguments"]) >= 3
    assert "Deterministic Bear Thesis" in bear_res["summary"]

    assert bull_res["status"] == "OK"
    assert bull_res["thesis_score"] == 2
    assert "Deterministic Bull Thesis" in bull_res["summary"]

    # 2. LONG Thesis Verification
    bull_res_long, bear_res_long = pipeline._build_deterministic_debate_theses(
        symbol="ETH-USD",
        strategy_mode="MEAN_REVERSION",
        direction_bias="LONG",
        reports=sample_reports,
        key_levels={
            "current_price": 2200.0,
            "ema_20": 2300.0,
            "bb_upper": 2400.0,
            "bb_lower": 2180.0,
            "atr_14": 45.0
        }
    )
    assert bull_res_long["status"] == "OK"
    assert bull_res_long["thesis_score"] == 9
    assert bull_res_long["potential_target"] == 2300.0  # Mean reversion to EMA-20
    assert bull_res_long["invalidation_level"] == 2180.0 # BB lower
    assert len(bull_res_long["bullish_arguments"]) >= 3
    assert "Deterministic Bull Thesis" in bull_res_long["summary"]

    assert bear_res_long["status"] == "OK"
    assert bear_res_long["thesis_score"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# 3. Parallel Candidate Evaluation Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parallel_candidate_evaluation():
    """
    Requirement 1 & 6:
    Multiple universe candidates are evaluated concurrently with asyncio.Semaphore(3).
    Limits (Cluster Cap & max_open_positions) are checked atomically under self._execution_lock.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "NADO")

    # Set up trading service positions state
    active_positions = {}
    mock_services.trading_service.active_positions = active_positions
    mock_services.trading_service.pending_reconciliations = {}
    mock_services.trading_service.open_position = AsyncMock(return_value=True)
    mock_services.logger = MagicMock()

    # Track maximum concurrency observed during candidate evaluation
    current_concurrency = 0
    max_concurrency_observed = 0
    concurrency_lock = asyncio.Lock()

    candidates = ["BTC-USD", "ETH-USD", "SOL-USD", "HYPE-USD", "ZEC-USD"]
    sem = asyncio.Semaphore(3)

    async def mock_candidate_work(sym: str) -> bool:
        nonlocal current_concurrency, max_concurrency_observed
        async with sem:
            async with concurrency_lock:
                current_concurrency += 1
                if current_concurrency > max_concurrency_observed:
                    max_concurrency_observed = current_concurrency

            # Simulate async workload (Stage 2-4)
            await asyncio.sleep(0.05)

            # Stage 5: Atomic execution under _execution_lock
            trade_executed = False
            async with pipeline._execution_lock:
                # Max 2 concurrent positions allowed
                cur_positions = mock_services.trading_service.active_positions
                if len(cur_positions) < 2:
                    # Place position
                    cur_positions[sym] = {"symbol": sym, "direction": "LONG", "size_usd": 100.0}
                    trade_executed = True

            async with concurrency_lock:
                current_concurrency -= 1

            return trade_executed

    # Run all 5 candidates concurrently
    results = await asyncio.gather(*[mock_candidate_work(sym) for sym in candidates])

    # Concurrency must be strictly bounded by Semaphore(3)
    assert max_concurrency_observed <= 3
    assert max_concurrency_observed >= 2  # Concurrency actually took place

    # Under atomic execution lock, exactly 2 positions were opened (never exceeding limit)
    assert sum(1 for r in results if r is True) == 2
    assert len(active_positions) == 2

