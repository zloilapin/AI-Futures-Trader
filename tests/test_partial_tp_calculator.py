"""
Comprehensive Unit Tests for PartialTPCalculator (Phase 1).
Verifies:
- Accurate LONG and SHORT price calculations with tick rounding.
- Strict lot size flooring without risk inflation.
- Dynamic ladder adaptation (4-tier -> 2-tier -> single-tier) based on product min_size.
- Zero dust: exact closure of full position.
- Plan serialization, deserialization, and status tracking.
"""

import pytest
from services.partial_tp_calculator import PartialTPCalculator, PartialTPPlan, TPTier


def test_long_4_tier_pricing_and_exact_sum():
    """Verifies standard 4-tier calculation for a LONG position."""
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.01
    }
    plan = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=120.0,
        total_contracts=1.6,
        market_params=market_params
    )

    assert plan.mode == "4_TIER"
    assert len(plan.tiers) == 4

    # Prices must be strictly monotonic increasing
    assert plan.tiers[0].target_price == pytest.approx(105.00)
    assert plan.tiers[1].target_price == pytest.approx(110.00)
    assert plan.tiers[2].target_price == pytest.approx(115.00)
    assert plan.tiers[3].target_price == pytest.approx(120.00)
    assert plan.tiers[0].target_price < plan.tiers[1].target_price < plan.tiers[2].target_price < plan.tiers[3].target_price

    # Slices: 0.4 each
    for t in plan.tiers:
        assert t.close_contracts == pytest.approx(0.4)
        assert t.status == "PENDING"

    # Exact sum
    total_closed = sum(t.close_contracts for t in plan.tiers)
    assert total_closed == pytest.approx(1.6)

    # Actions
    assert plan.tiers[0].action is None
    assert plan.tiers[1].action == "PROTECT_BREAKEVEN"
    assert plan.tiers[2].action == "START_TRAILING"
    assert plan.tiers[3].action == "CLOSE_ALL"
    assert plan.tiers[3].is_final is True


def test_short_4_tier_pricing_and_exact_sum():
    """Verifies standard 4-tier calculation for a SHORT position with tick rounding."""
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.001
    }
    plan = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="SHORT",
        entry_price=106.7826,
        tp_final_price=103.6736,
        total_contracts=1.6,
        market_params=market_params
    )

    assert plan.mode == "4_TIER"
    assert len(plan.tiers) == 4

    # Prices must be strictly monotonic decreasing
    p1 = plan.tiers[0].target_price
    p2 = plan.tiers[1].target_price
    p3 = plan.tiers[2].target_price
    p4 = plan.tiers[3].target_price

    assert p1 > p2 > p3 > p4
    assert p1 < 106.7826
    assert p4 == pytest.approx(103.674, abs=0.001)

    # Sum of contracts must equal total_contracts exactly
    total_closed = sum(t.close_contracts for t in plan.tiers)
    assert total_closed == pytest.approx(1.6)


def test_adaptation_4_tier_to_2_tier_small_position():
    """
    Verifies adaptive ladder degradation when 0.25 * V0 is smaller than min_size,
    but 0.50 * V0 satisfies min_size (e.g. ETH with 0.035 contracts and min_size=0.01).
    """
    market_params = {
        "size_increment": 0.005,
        "min_size": 0.01,
        "price_increment": 0.01
    }
    plan = PartialTPCalculator.calculate_plan(
        symbol="ETH-USD",
        direction="SHORT",
        entry_price=2500.0,
        tp_final_price=2400.0,
        total_contracts=0.035,
        market_params=market_params
    )

    # 0.25 * 0.035 = 0.00875 -> floored to 0.005 < min_size (0.01)
    # 0.50 * 0.035 = 0.0175 -> floored to 0.015 >= min_size (0.01)
    # Remaining: 0.035 - 0.015 = 0.020 >= min_size (0.01)
    assert plan.mode == "2_TIER"
    assert len(plan.tiers) == 2

    t1, t2 = plan.tiers
    assert t1.close_contracts == pytest.approx(0.015)
    assert t2.close_contracts == pytest.approx(0.020)
    assert t1.close_contracts >= market_params["min_size"]
    assert t2.close_contracts >= market_params["min_size"]

    # Target prices
    assert t1.target_price == pytest.approx(2450.0)  # 50% distance
    assert t2.target_price == pytest.approx(2400.0)  # 100% distance

    # Actions
    assert t1.action == "PROTECT_BREAKEVEN"
    assert t2.action == "CLOSE_ALL"
    assert t2.is_final is True

    # Exact sum
    assert (t1.close_contracts + t2.close_contracts) == pytest.approx(0.035)


def test_adaptation_to_single_tier_micro_position():
    """
    Verifies that if even 0.50 * V0 cannot satisfy min_size,
    the calculator gracefully falls back to SINGLE_TIER (100% TP) without inflating risk.
    """
    market_params = {
        "size_increment": 0.005,
        "min_size": 0.01,
        "price_increment": 0.01
    }
    plan = PartialTPCalculator.calculate_plan(
        symbol="ETH-USD",
        direction="LONG",
        entry_price=2500.0,
        tp_final_price=2600.0,
        total_contracts=0.015,
        market_params=market_params
    )

    # 0.50 * 0.015 = 0.0075 -> floored to 0.005 < min_size (0.01)
    # Cannot do 2 tiers! Must fallback to single tier!
    assert plan.mode == "SINGLE_TIER"
    assert len(plan.tiers) == 1
    assert plan.tiers[0].close_contracts == pytest.approx(0.015)
    assert plan.tiers[0].target_price == pytest.approx(2600.0)
    assert plan.tiers[0].is_final is True
    assert plan.tiers[0].action == "CLOSE_ALL"


def test_non_clean_multiples_flooring_prevents_risk_inflation():
    """
    Verifies that non-clean contract multiples floor intermediate slices down,
    accumulating any fractional residue into the final tier (zero dust, no risk inflation).
    """
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.01
    }
    plan = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=110.0,
        total_contracts=1.1,
        market_params=market_params
    )

    assert plan.mode == "4_TIER"
    # 0.25 * 1.1 = 0.275 -> floored to 0.2
    # Tiers 1-3: 0.2 each (total 0.6)
    # Tier 4: 1.1 - 0.6 = 0.5
    assert plan.tiers[0].close_contracts == pytest.approx(0.2)
    assert plan.tiers[1].close_contracts == pytest.approx(0.2)
    assert plan.tiers[2].close_contracts == pytest.approx(0.2)
    assert plan.tiers[3].close_contracts == pytest.approx(0.5)

    # Exact sum
    assert sum(t.close_contracts for t in plan.tiers) == pytest.approx(1.1)


def test_plan_serialization_roundtrip_and_status_tracking():
    """Verifies that PartialTPPlan serializes to dict and restores cleanly with status transitions."""
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.01
    }
    plan = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=120.0,
        total_contracts=1.6,
        market_params=market_params,
        created_at=1791890000.0
    )

    # Test initial pending tier
    next_tier = plan.get_next_pending_tier()
    assert next_tier is not None
    assert next_tier.tier_index == 1

    # Simulate tier 1 transitioning to FILLED
    next_tier.status = "FILLED"
    next_tier.filled_contracts = 0.4
    next_tier.fill_price = 105.02
    next_tier.realized_pnl_usd = 2.01

    # Next pending tier should now be tier 2
    next_tier2 = plan.get_next_pending_tier()
    assert next_tier2 is not None
    assert next_tier2.tier_index == 2

    # Roundtrip serialization
    d = plan.to_dict()
    restored = PartialTPPlan.from_dict(d)

    assert restored.symbol == "SOL-USD"
    assert restored.direction == "LONG"
    assert restored.entry_price == 100.0
    assert restored.mode == "4_TIER"
    assert len(restored.tiers) == 4
    assert restored.tiers[0].status == "FILLED"
    assert restored.tiers[0].filled_contracts == 0.4
    assert restored.tiers[1].status == "PENDING"
    assert restored.get_next_pending_tier().tier_index == 2


def test_validation_rejects_invalid_inputs():
    """Verifies that invalid prices, directions, or inverted TPs raise descriptive ValueErrors."""
    valid_params = {"size_increment": 0.1, "min_size": 0.1, "price_increment": 0.01}

    # Invalid direction
    with pytest.raises(ValueError, match="Invalid direction"):
        PartialTPCalculator.calculate_plan("SOL-USD", "HOLD", 100.0, 110.0, 1.0, valid_params)

    # Zero entry
    with pytest.raises(ValueError, match="must all be > 0"):
        PartialTPCalculator.calculate_plan("SOL-USD", "LONG", 0.0, 110.0, 1.0, valid_params)

    # Inverted LONG (TP < Entry)
    with pytest.raises(ValueError, match="must be > entry_price"):
        PartialTPCalculator.calculate_plan("SOL-USD", "LONG", 100.0, 90.0, 1.0, valid_params)

    # Inverted SHORT (TP > Entry)
    with pytest.raises(ValueError, match="must be < entry_price"):
        PartialTPCalculator.calculate_plan("SOL-USD", "SHORT", 100.0, 110.0, 1.0, valid_params)
