import os
import sys
import time
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../python')))

from core.pipeline import TradingPipeline
from core.strategy_router import StrategyProfile
from core.models import FinalTradeDecision
from core.deterministic_guard import DeterministicGuard
from core.state_store import StateStore
from agents.risk_manager import RiskManager


@pytest.fixture
def clean_watchlist_file(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_register_and_persist_pullback_candidate(clean_watchlist_file):
    """
    Verifies that registering a candidate in _pullback_watchlist calculates target at 15m EMA-20
    and atomically persists it to pullback_watchlist.json.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    mock_services.logger = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")

    market_data = {
        "indicators": {
            "ema_20": 86.50,
            "atr_14": 1.20
        }
    }

    # For SHORT with current_price=84.80 (< ema_20=86.50), target MUST be 15m EMA-20 (86.50)
    pipeline.register_pullback_candidate(
        symbol="SOL-USD",
        decision="SHORT",
        current_price=84.80,
        directional_conf=80,
        conviction=75,
        strategy_mode="TREND_FOLLOWING",
        market_data=market_data,
        ceo_verdict={"reasoning": "Pullback required"},
        strategy_profile=None,
        reason="Wave Exhaustion Guard"
    )

    assert "SOL-USD" in pipeline._pullback_watchlist
    entry = pipeline._pullback_watchlist["SOL-USD"]
    assert entry["target_pullback_price"] == 86.50
    assert entry["target_entry"] == 86.50
    assert entry["invalidation_price"] > 86.50
    assert entry["direction"] == "SHORT"

    # Verify disk persistence via StateStore
    pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
    assert os.path.exists(pw_path)
    with open(pw_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert "SOL-USD" in saved_data
    assert saved_data["SOL-USD"]["target_pullback_price"] == 86.50


@pytest.mark.asyncio
async def test_check_pullback_watchlist_triggers_and_stores_context(clean_watchlist_file):
    """
    Verifies that when live price pulls back to the 15m EMA-20 zone, check_pullback_watchlist:
    1. Detects the pullback hit (covering >= 80% of distance to target).
    2. Stores context in _pullback_context.
    3. Cleans candidate from watchlist and disk.
    4. Sets _priority_symbol and _priority_direction.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    mock_services.logger = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")
    pipeline._cycle_running = False
    pipeline.services.trading_service.active_positions = {}

    pipeline.register_pullback_candidate(
        symbol="BNB-USD",
        decision="SHORT",
        current_price=680.0,
        directional_conf=85,
        conviction=78,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 685.0, "atr_14": 4.0}},
        ceo_verdict={"reasoning": "Waiting for bounce to EMA-20"},
        strategy_profile=None
    )

    # Price moves from 680 up to 684.50 (covers > 80% of 5.0 distance to 685.0)
    pipeline.services.fetcher.fetch_ohlcv = AsyncMock(return_value={"current_price": 684.50})

    triggered, sym, reason = await pipeline.check_pullback_watchlist()
    assert triggered is True
    assert sym == "BNB-USD"
    assert "retest" in reason.lower()
    assert pipeline._priority_symbol == "BNB-USD"
    assert pipeline._priority_direction == "SHORT"
    assert "BNB-USD" in pipeline._pullback_context
    assert pipeline._pullback_context["BNB-USD"]["target_pullback_price"] == 685.0

    # Watchlist on disk must no longer contain BNB-USD
    pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
    with open(pw_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert "BNB-USD" not in saved_data


@pytest.mark.asyncio
async def test_deterministic_guard_allows_pullback_retest_without_infinite_loop():
    """
    Verifies that DeterministicGuard does NOT block an incoming PULLBACK_RETEST back into
    WAIT_FOR_PULLBACK when trade_action is REDUCE_SIZE or conviction/quality is moderate.
    """
    strat_profile = StrategyProfile(
        True, "TREND_FOLLOWING", "SHORT", "PULLBACK_RETEST SHORT: Цена достигла зоны отката"
    )
    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 70,
        "directional_confidence": 80,
        "entry_quality": 68.0,
        "trade_action": "REDUCE_SIZE",
        "is_pullback_retest": True
    }
    market_data = {
        "is_pullback_retest": True,
        "indicators": {
            "rsi_14": 45.0,
            "bb_position_pct": 52.0,
            "ema_20": 86.50
        }
    }

    mock_rm = MagicMock()
    mock_rm._get_profile_rules.return_value = {"min_conviction": 65}

    decision = DeterministicGuard.evaluate(
        strategy_profile=strat_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_rm,
        market_data=market_data,
        symbol="SOL-USD"
    )

    # Must NOT be blocked or set to WAIT_FOR_PULLBACK!
    assert decision.is_actionable is True
    assert decision.trade_action in ["ENTER", "REDUCE_SIZE"]
    assert decision.guard_status != "PULLBACK_WATCHLIST"


@pytest.mark.asyncio
async def test_risk_manager_retest_structural_sl_and_distance_gate():
    """
    Verifies that RiskManager:
    1. Evaluates structural SL from fresh entry price at pullback.
    2. Allows confirmed retest with structural SL dist up to 2.50% without vetoing.
    3. Successfully approves the trade for execution.
    """
    risk_mgr = RiskManager(logger=MagicMock(), llm_client=MagicMock())

    current_price = 86.20
    swing_high_15m = 87.80  # Dist = (87.80 + buffer - 86.20) / 86.20 ≈ 2.15% (> 1.95%, but <= 2.50%)

    ceo_decision = {
        "decision": "SHORT",
        "conviction": 76,
        "directional_confidence": 82,
        "entry_quality": 70.0,
        "trade_action": "REDUCE_SIZE",
        "is_pullback_retest": True
    }
    portfolio_data = {
        "total_balance": 250.0,
        "total_usd": 250.0,
        "available_margin": 200.0,
        "active_positions": {},
        "is_pullback_retest": True
    }
    market_data = {
        "is_pullback_retest": True,
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": 1.0,
            "swing_high_15m": swing_high_15m,
            "swing_low_15m": 84.0,
            "rsi_14": 46.0,
            "bb_position_pct": 52.0,
            "ema_20": 86.20
        },
        "derivatives_data": {
            "funding_rate_hourly": 0.0,
            "taker_fee_pct": 0.0005
        }
    }

    final_decision = FinalTradeDecision(
        symbol="SOL-USD",
        decision="SHORT",
        conviction=76,
        trade_action="REDUCE_SIZE",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        min_conviction=65,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="Retest approved"
    )

    verdict = await risk_mgr.analyze(
        final_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING"
    )

    assert verdict.get("approved") is True, f"Expected approval on pullback retest, got veto: {verdict.get('reasoning')}"
    assert verdict.get("stop_loss_price") >= swing_high_15m
    assert verdict.get("notional_size_usd") > 0


@pytest.mark.asyncio
async def test_risk_manager_distance_veto_registers_into_pullback_watchlist(clean_watchlist_file):
    """
    Verifies that when RiskManager rejects a first-wave entry with DISTANCE_TO_STRUCTURE_VETO,
    the pipeline registers the setup into _pullback_watchlist and updates disk store.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    mock_services.logger = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")

    # Simulate RiskManager veto result with trade_action="WAIT_FOR_PULLBACK"
    risk_verdict = {
        "approved": False,
        "veto_category": "DISTANCE_TO_STRUCTURE_VETO",
        "trade_action": "WAIT_FOR_PULLBACK",
        "reasoning": "Distance-to-Structure Gate: Расстояние до структурного стопа (2.80% > 1.95%)"
    }

    final_trade_decision = FinalTradeDecision(
        symbol="SOL-USD",
        decision="SHORT",
        conviction=78,
        trade_action="ENTER",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        min_conviction=65,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="Signal valid"
    )

    market_data = {
        "indicators": {
            "ema_20": 87.0,
            "atr_14": 1.2
        }
    }

    # Simulate what pipeline does in Stage 5 else branch:
    if (
        risk_verdict.get("trade_action") == "WAIT_FOR_PULLBACK"
        and final_trade_decision.decision in ["LONG", "SHORT"]
    ):
        pipeline.register_pullback_candidate(
            symbol="SOL-USD",
            decision=final_trade_decision.decision,
            current_price=84.50,
            directional_conf=80,
            conviction=78,
            strategy_mode="TREND_FOLLOWING",
            market_data=market_data,
            ceo_verdict={"reasoning": "Late entry near bottom of wave"},
            strategy_profile=None,
            reason=risk_verdict.get("reasoning", "")
        )

    assert "SOL-USD" in pipeline._pullback_watchlist
    assert pipeline._pullback_watchlist["SOL-USD"]["target_pullback_price"] == 87.0

    # Check disk file
    pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
    with open(pw_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "SOL-USD" in data
    assert data["SOL-USD"]["target_entry"] == 87.0
