import time
import pytest
import asyncio
from unittest.mock import MagicMock

from agents.sentinel_agent import SentinelAgent
from core.strategy_router import StrategyRouter, StrategyProfile
from core.state_store import StateStore
from core.logger import TradeLogger


@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


# ==============================================================================
# (a) SENTINEL AGENT: STRICT 0.75R / 0.85R THRESHOLD TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_sentinel_no_sl_shift_when_mfe_below_0_75r(mock_logger):
    """
    Verifies that when MFE < 0.75R (e.g. 0.19R or 0.65R),
    SentinelAgent strictly preserves initial SL and returns new_sl: None.
    Position is allowed full room to breathe within initial risk bounds.
    """
    agent = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "be_activation_r": 0.75,
        "sentinel_trail_activation_atr": 1.5,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25,
        "sentinel_fee_pct": 0.001,
        "sentinel_slippage_pct": 0.001
    }
    eval_atr = 1.0

    # 1. LONG: Entry 100.0, SL 98.0 (1R = 2.0). 0.75R = 1.50 (Price 101.50).
    pos_long = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "highest_price": 101.30,  # Peak MFE = 1.30 / 2.0 = 0.65R (< 0.75R)
        "protection_state": "SOFTWARE_PROTECTED",
        "open_time": time.time() - 600
    }
    market_long = {"price_data": {"current_price": 101.0}} # Cur profit = 0.50R

    res_long = await agent.analyze(pos_long, market_long, profile_rules, eval_atr)
    assert res_long["new_sl"] is None
    assert res_long["state"] == "SOFTWARE_PROTECTED"

    # 2. SHORT: Entry 100.0, SL 102.0 (1R = 2.0). 0.75R = 1.50 (Price 98.50).
    pos_short = {
        "symbol": "BTC-USD",
        "direction": "SHORT",
        "entry_price": 100.0,
        "sl_price": 102.0,
        "initial_sl": 102.0,
        "lowest_price": 99.62,  # Peak MFE = 0.38 / 2.0 = 0.19R (< 0.75R)
        "protection_state": "NATIVE_SL_CONFIRMED",
        "open_time": time.time() - 300
    }
    market_short = {"price_data": {"current_price": 99.70}} # Cur profit = 0.15R

    res_short = await agent.analyze(pos_short, market_short, profile_rules, eval_atr)
    assert res_short["new_sl"] is None
    assert res_short["state"] == "NATIVE_SL_CONFIRMED"


@pytest.mark.asyncio
async def test_sentinel_free_ride_corridor_between_0_75r_and_1_35r(mock_logger):
    """
    Verifies that on range 0.75R <= current_r < 1.35R (Free Ride corridor):
    1. SentinelAgent moves SL to Break-Even (entry +/- cost_buffer) once 0.75R is reached.
    2. Once in BREAK_EVEN, SL is NOT tightened behind price while current_r < 1.35R (e.g. at 1.20R),
       giving the position free room to breathe without being shaken out.
    """
    agent = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "be_activation_r": 0.75,
        "sentinel_trail_activation_atr": 1.5,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25,
        "sentinel_fee_pct": 0.001,
        "sentinel_slippage_pct": 0.001
    }
    eval_atr = 1.0

    # 1. LONG: Entry 100.0, SL 98.0 (1R = 2.0).
    # Price reaches 101.56 (profit = 1.56 = 0.78R, strictly between 0.75R and 1.35R).
    pos_long = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "highest_price": 101.56,
        "protection_state": "SOFTWARE_PROTECTED",
        "open_time": time.time() - 600
    }
    market_long = {"price_data": {"current_price": 101.56}}

    res = await agent.analyze(pos_long, market_long, profile_rules, eval_atr)
    assert res["new_sl"] is not None
    assert res["state"] == "BREAK_EVEN"
    # Cost buffer = 100.0 * 0.002 = 0.20 -> BE SL = 100.20
    assert res["new_sl"] == pytest.approx(100.20)
    assert "Price reached BE trigger" in res["reasoning"]

    # 2. Position is already in BREAK_EVEN at 100.20 and advances to 102.40 (+1.20R < 1.35R)
    # Trailing stop must NOT tighten behind price!
    pos_long_breathing = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 100.20,
        "initial_sl": 98.0,
        "highest_price": 102.40,  # Peak MFE = 1.20R (< 1.35R)
        "protection_state": "BREAK_EVEN",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": time.time() - 900
    }
    market_long_breathing = {"price_data": {"current_price": 102.20}}

    res_breathing = await agent.analyze(pos_long_breathing, market_long_breathing, profile_rules, eval_atr)
    # Must NOT move stop closer behind price during Free Ride phase!
    assert res_breathing["new_sl"] is None
    assert res_breathing["state"] == "BREAK_EVEN"


@pytest.mark.asyncio
async def test_sentinel_dynamic_trailing_activates_strictly_at_1_35r(mock_logger):
    """
    Verifies that dynamic parabolic trailing activates strictly at current_r >= 1.35R,
    and guarantees a minimum locked floor of at least +0.75R.
    """
    agent = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "be_activation_r": 0.75,
        "sentinel_trail_activation_atr": 1.5,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.05,
        "sentinel_fee_pct": 0.001,
        "sentinel_slippage_pct": 0.001
    }
    eval_atr = 0.50

    # 1. At 1.25R (< 1.35R): trailing must NOT activate
    pos_1_25r = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 100.20,
        "initial_sl": 98.0,
        "highest_price": 102.50, # 1.25R (1R = 2.0)
        "protection_state": "BREAK_EVEN",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": time.time() - 1200
    }
    res_1_25 = await agent.analyze(pos_1_25r, {"price_data": {"current_price": 102.40}}, profile_rules, eval_atr)
    assert res_1_25["new_sl"] is None
    assert res_1_25["state"] == "BREAK_EVEN"

    # 2. At 1.40R (>= 1.35R): dynamic parabolic trailing activates!
    pos_1_40r = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 100.20,
        "initial_sl": 98.0,
        "highest_price": 102.80, # 2.80 / 2.0 = 1.40R (>= 1.35R)
        "protection_state": "BREAK_EVEN",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": time.time() - 1500
    }
    market_long = {"price_data": {"current_price": 102.75}}

    res = await agent.analyze(pos_1_40r, market_long, profile_rules, eval_atr)
    assert res["new_sl"] is not None
    assert res["state"] == "TRAILING"
    # Floor must be at least +0.75R (100.0 + 0.75 * 2.0 = 101.50)
    assert res["new_sl"] >= 101.50


# ==============================================================================
# (b) ANTI-CHASE RE-ENTRY GUARD TESTS
# ==============================================================================

def test_anti_chase_blocks_same_direction_reentry_without_pullback_short(monkeypatch, tmp_path):
    """
    Verifies Anti-Chase Re-Entry Guard blocks SHORT when:
    - Last closed trade in journal was SHORT and profitable < 120m ago.
    - Current price did not bounce +0.5 ATR above last exit price OR bb_pos < 45.0%.
    """
    # Create fake journal in tmp_path
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    mock_journal = [
        {
            "symbol": "HYPE-USD",
            "direction": "SHORT",
            "entry_price": 77.5155,
            "exit_price": 77.055,
            "net_pnl_usd": 0.8017,
            "realized_r": 0.4198,
            "timestamp": now_ts - 1800.0  # 30 minutes ago (< 120 min)
        }
    ]
    StateStore.save(journal_file, mock_journal)

    # Attempting to re-enter SHORT at 77.08 (chasing the bottom!)
    # ATR is 0.80 -> 0.5 ATR bounce required = 77.055 + 0.40 = 77.455.
    # Current price 77.08 < 77.455 AND bb_pos 20.0% < 45.0%.
    can_reenter, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="HYPE-USD",
        target_direction="SHORT",
        current_price=77.08,
        eval_atr=0.80,
        bb_position_pct=20.0,
        max_age_seconds=7200.0,
        journal_path=journal_file
    )

    assert can_reenter is False
    assert "Anti-Chase Guard" in reason
    assert "WAIT_FOR_PULLBACK" in reason
    assert "не вернулась к средней" in reason


def test_anti_chase_allows_reentry_after_valid_pullback_short(tmp_path):
    """
    Verifies Anti-Chase Re-Entry Guard permits SHORT once price pulls back:
    - Bounces >= +0.5 ATR above last exit price AND bb_pos >= 45.0%.
    """
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    mock_journal = [
        {
            "symbol": "HYPE-USD",
            "direction": "SHORT",
            "entry_price": 77.5155,
            "exit_price": 77.055,
            "net_pnl_usd": 0.8017,
            "realized_r": 0.4198,
            "timestamp": now_ts - 1800.0
        }
    ]
    StateStore.save(journal_file, mock_journal)

    # Price bounced to 77.55 (above 77.455 threshold) AND BB pos returned to 48.0%
    can_reenter, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="HYPE-USD",
        target_direction="SHORT",
        current_price=77.55,
        eval_atr=0.80,
        bb_position_pct=48.0,
        max_age_seconds=7200.0,
        journal_path=journal_file
    )

    assert can_reenter is True
    assert "criteria satisfied" in reason


def test_anti_chase_blocks_same_direction_reentry_without_pullback_long(tmp_path):
    """
    Verifies Anti-Chase Re-Entry Guard blocks LONG when:
    - Last closed trade was LONG and profitable < 120m ago.
    - Current price did not pull down -0.5 ATR below last exit price OR bb_pos > 55.0%.
    """
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    mock_journal = [
        {
            "symbol": "SOL-USD",
            "direction": "LONG",
            "entry_price": 195.0,
            "exit_price": 200.0,
            "net_pnl_usd": 1.25,
            "realized_r": 0.80,
            "timestamp": now_ts - 2400.0  # 40 minutes ago
        }
    ]
    StateStore.save(journal_file, mock_journal)

    # Chasing the peak: price at 199.80 (ATR=2.0 -> required <= 199.0) and bb_pos 70% > 55%
    can_reenter, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="LONG",
        current_price=199.80,
        eval_atr=2.0,
        bb_position_pct=70.0,
        max_age_seconds=7200.0,
        journal_path=journal_file
    )

    assert can_reenter is False
    assert "Anti-Chase Guard" in reason
    assert "WAIT_FOR_PULLBACK" in reason


def test_anti_chase_allows_reentry_after_valid_pullback_long(tmp_path):
    """
    Verifies Anti-Chase permits LONG once price pulls down:
    - >= 0.5 ATR below last exit price AND bb_pos <= 55.0%.
    """
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    mock_journal = [
        {
            "symbol": "SOL-USD",
            "direction": "LONG",
            "entry_price": 195.0,
            "exit_price": 200.0,
            "net_pnl_usd": 1.25,
            "realized_r": 0.80,
            "timestamp": now_ts - 2400.0
        }
    ]
    StateStore.save(journal_file, mock_journal)

    # Valid pullback: price down to 198.80 (<= 199.0) and bb_pos = 50.0%
    can_reenter, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="LONG",
        current_price=198.80,
        eval_atr=2.0,
        bb_position_pct=50.0,
        max_age_seconds=7200.0,
        journal_path=journal_file
    )

    assert can_reenter is True


def test_anti_chase_allows_opposite_direction(tmp_path):
    """
    Verifies Anti-Chase does NOT block reversals (e.g. entering LONG after exiting SHORT).
    """
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    mock_journal = [
        {
            "symbol": "BTC-USD",
            "direction": "SHORT",
            "entry_price": 76000.0,
            "exit_price": 75000.0,
            "net_pnl_usd": 2.0,
            "realized_r": 1.0,
            "timestamp": now_ts - 600.0
        }
    ]
    StateStore.save(journal_file, mock_journal)

    can_reenter, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="BTC-USD",
        target_direction="LONG",
        current_price=75100.0,
        eval_atr=500.0,
        bb_position_pct=30.0,
        journal_path=journal_file
    )

    assert can_reenter is True
    assert "Direction changed" in reason


def test_anti_chase_allows_after_cooldown_expired(tmp_path):
    """
    Verifies that once 240 minutes (14400s / 4 hours) have passed, anti-chase cooldown expires.
    Also verifies that within 240 minutes (e.g. 180 min ago), re-entry is still blocked.
    """
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    mock_journal = [
        {
            "symbol": "BTC-USD",
            "direction": "SHORT",
            "entry_price": 76000.0,
            "exit_price": 75000.0,
            "net_pnl_usd": 2.0,
            "realized_r": 1.0,
            "timestamp": now_ts - 10800.0  # 180 min ago (< 240 min)
        }
    ]
    StateStore.save(journal_file, mock_journal)

    # 1. At 180 minutes: still blocked under 240 min rule
    can_reenter_180, reason_180 = StrategyRouter.check_anti_chase_reentry(
        symbol="BTC-USD",
        target_direction="SHORT",
        current_price=74900.0,
        eval_atr=500.0,
        bb_position_pct=20.0,
        journal_path=journal_file
    )
    assert can_reenter_180 is False
    assert "Anti-Chase Guard" in reason_180

    # 2. At 250 minutes (> 240 min): cooldown expires
    mock_journal[0]["timestamp"] = now_ts - 15000.0
    StateStore.save(journal_file, mock_journal)

    can_reenter_250, reason_250 = StrategyRouter.check_anti_chase_reentry(
        symbol="BTC-USD",
        target_direction="SHORT",
        current_price=74900.0,
        eval_atr=500.0,
        bb_position_pct=20.0,
        journal_path=journal_file
    )
    assert can_reenter_250 is True
    assert "Cooldown expired" in reason_250


# ==============================================================================
# (c) PRE-DEBATE GATE: ANALYST PARITY (2 vs 2) REJECTION TESTS
# ==============================================================================

def test_pre_debate_gate_rejects_analyst_parity_2_vs_2_short():
    """
    Verifies that when analysts are tied 2 vs 2 (e.g. 2 Bears vs 2 Bulls),
    Pre-Debate Gate strictly rejects candidate (no strict majority of bears).
    """
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"},
        {"agent_name": "OI_Funding_Agent", "signal": "BULLISH"}
    ]
    market_data = {
        "price_data": {"current_price": 25.0},
        "indicators": {
            "trend_1h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT",
            "atr_14": 0.5,
            "bb_position_pct": 50.0,
            "rsi_14": 45.0
        }
    }
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        symbol="ZEC-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime="TRENDING_BEAR"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "ANALYST_DISAGREEMENT"
    assert "Паритет аналитиков 2 vs 2" in profile.reasoning


def test_pre_debate_gate_rejects_analyst_parity_2_vs_2_long():
    """
    Verifies that when analysts are tied 2 vs 2 for a BULLISH trend,
    Pre-Debate Gate strictly rejects candidate (no strict majority of bulls).
    """
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH"}
    ]
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "trend_1h": "BULLISH",
            "mtf_alignment": "FULL_ALIGNMENT",
            "atr_14": 1.0,
            "bb_position_pct": 50.0,
            "rsi_14": 55.0
        }
    }
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime="TRENDING_BULL"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "ANALYST_DISAGREEMENT"
    assert "Паритет аналитиков 2 vs 2" in profile.reasoning


def test_pre_debate_gate_allows_strict_majority_bears():
    """
    Verifies that when bears have strict majority (e.g. 3 vs 1 or 2 vs 1),
    candidate is approved for SHORT.
    """
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"},
        {"agent_name": "OI_Funding_Agent", "signal": "BULLISH"}
    ]
    market_data = {
        "price_data": {"current_price": 25.0},
        "indicators": {
            "trend_1h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT",
            "atr_14": 0.5,
            "bb_position_pct": 50.0,
            "rsi_14": 45.0
        }
    }
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        symbol="ZEC-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime="TRENDING_BEAR"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "TREND_FOLLOWING"
    assert profile.direction_bias == "SHORT"
    assert "3 vs 1" in profile.reasoning


# ==============================================================================
# (d) ANTI-CHASE BB & RSI OVEREXTENSION TESTS (WAIT_FOR_PULLBACK to EMA-20)
# ==============================================================================

def test_anti_chase_overextension_short_veto():
    """
    Verifies that for TREND_FOLLOWING SHORT, if bb_pos < 30.0% and RSI < 40.0,
    StrategyRouter forbids market entry and redirects to WAIT_FOR_PULLBACK.
    """
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"}
    ]
    market_data = {
        "price_data": {"current_price": 20.0},
        "indicators": {
            "trend_1h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT",
            "atr_14": 0.5,
            "bb_position_pct": 22.0,  # < 30.0% (lower third)
            "rsi_14": 34.0,           # < 40.0
            "ema_20": 21.2
        }
    }
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        symbol="ZEC-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime="TRENDING_BEAR"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "ANTI_CHASE_REENTRY_VETO"
    assert "WAIT_FOR_PULLBACK к EMA-20" in profile.reasoning


def test_anti_chase_overextension_long_veto():
    """
    Verifies that for TREND_FOLLOWING LONG, if bb_pos > 70.0% and RSI > 60.0,
    StrategyRouter forbids market entry and redirects to WAIT_FOR_PULLBACK.
    """
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]
    market_data = {
        "price_data": {"current_price": 200.0},
        "indicators": {
            "trend_1h": "BULLISH",
            "mtf_alignment": "FULL_ALIGNMENT",
            "atr_14": 2.0,
            "bb_position_pct": 78.0,  # > 70.0% (upper third)
            "rsi_14": 66.0,           # > 60.0
            "ema_20": 194.0
        }
    }
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache=macro_cache,
        detected_regime="TRENDING_BULL"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "ANTI_CHASE_REENTRY_VETO"
    assert "WAIT_FOR_PULLBACK к EMA-20" in profile.reasoning
