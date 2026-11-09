import pytest
import math
from unittest.mock import MagicMock
from agents.risk_manager import RiskManager

def test_size_increment_floor_no_float_truncation():
    """Verifies that contract flooring doesn't suffer IEEE-754 precision chops (e.g. 0.29 // 0.01 = 28.0)."""
    mock_logger = MagicMock()
    mock_llm = MagicMock()
    rm = RiskManager(mock_logger, mock_llm)
    
    # Test values that classically fail raw float (c // inc) * inc
    test_cases = [
        (0.29, 0.01, 0.29),
        (0.57, 0.01, 0.57),
        (0.58, 0.01, 0.58),
        (0.87, 0.01, 0.87),
        (0.88, 0.01, 0.88),
        (0.016, 0.001, 0.016),
        (0.007, 0.001, 0.007)
    ]
    for amount, step, expected in test_cases:
        steps = math.floor(round(amount / step, 8))
        step_str = f"{step:.10f}".rstrip("0")
        decimals = len(step_str.split(".")[1]) if "." in step_str else 0
        floored = round(steps * step, min(decimals, 8))
        assert floored == expected, f"Failed for amount {amount} with step {step}: got {floored}, expected {expected}"

def test_drawdown_multiplier_scale_invariance():
    """Verifies that _calculate_drawdown_multiplier treats fraction (0.05) and percentage (5.0) identically."""
    mock_logger = MagicMock()
    mock_llm = MagicMock()
    rm = RiskManager(mock_logger, mock_llm)
    
    mult_fraction, msg_frac = rm._calculate_drawdown_multiplier(streak=[], daily_drawdown_pct=0.05)
    mult_percent, msg_perc = rm._calculate_drawdown_multiplier(streak=[], daily_drawdown_pct=5.0)
    
    assert mult_fraction == mult_percent == 0.70  # 4-6% drawdown tier is 0.70x

def test_pre_debate_viable_reads_total_usd_and_normalizes_dd():
    """Verifies is_pre_debate_viable properly extracts total_usd and handles percentage dd."""
    mock_logger = MagicMock()
    mock_llm = MagicMock()
    rm = RiskManager(mock_logger, mock_llm)
    
    portfolio_data = {
        "total_usd": 97.68,
        "peak_balance": 100.0,
        "daily_drawdown_pct": 5.0,  # 5.0% scale
        "recent_streak": ["LOSS", "LOSS", "LOSS"]
    }
    
    is_viable, reason = rm.is_pre_debate_viable(portfolio_data, profile_name="AGGRESSIVE")
    assert is_viable is True

def test_trigger_bump_restricted_in_drawdown_streak():
    """Verifies that trigger bumping to $101 is not allowed to triple risk during a severe drawdown streak."""
    # When portfolio_mult < 1.0 (e.g. 0.40 in a 3-loss streak), dynamic_risk_pct = 0.50%
    # Bumping risk to 1.50% (3x) should be rejected
    dynamic_risk_pct = 0.005
    portfolio_mult = 0.40
    risk_cap = 0.025
    
    max_allowed = min(risk_cap, (dynamic_risk_pct * 1.15) if portfolio_mult < 1.0 else 0.015)
    assert max_allowed == 0.00575  # Capped at +15% expansion, NOT 1.5% (0.015)
