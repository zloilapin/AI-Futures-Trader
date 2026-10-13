"""
Unit tests for Partial TP State Machine, Persistence, Idempotency, and Reconciliation (Phase 3).
Verifies:
- Plan initialization and attachment to position state.
- Atomic updates of contracts, size_usd, margin, and accumulated_realized_pnl upon tier execution.
- Strict idempotency (duplicate tier execution calls ignored).
- Failed executions safely reverted to PENDING with contracts intact.
- Partial fills accurately tracked and deducted.
- Final tier completion and position lifecycle finalization.
- On-chain reconciliation: zero balance cleanup and volume divergence alignment.
- Complete backward compatibility for legacy positions without partial_tp_plan.
"""

import pytest
import asyncio
from unittest.mock import MagicMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from services.partial_tp_calculator import PartialTPPlan, TPTier


@pytest.fixture
def mock_service():
    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service.product_map = {"SOL": 8, "SOL-USD": 8, "ETH": 4, "ETH-USD": 4}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1111111111111111111111111111111111111111"
    service.default_subaccount_id = "0x" + "11" * 32
    service._save_positions = MagicMock()
    service._save_state = MagicMock()

    service._market_cache = {
        8: MagicMock(
            book_info=MagicMock(
                size_increment=int(0.1 * 1e18),
                price_increment_x18=int(0.001 * 1e18),
                min_size=int(0.1 * 1e18)
            )
        )
    }
    return service


def test_initialize_partial_tp_plan_success(mock_service):
    """Verifies that initialize_partial_tp_plan creates and attaches a 4-tier plan to an active position."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }

    plan = mock_service.initialize_partial_tp_plan("SOL-USD")

    assert plan is not None
    assert plan.symbol == "SOL-USD"
    assert plan.mode == "4_TIER"
    assert len(plan.tiers) == 4
    assert plan.remaining_contracts == pytest.approx(1.6)

    # Position updated
    pos = mock_service.active_positions["SOL-USD"]
    assert "partial_tp_plan" in pos
    assert pos["accumulated_realized_pnl"] == 0.0
    mock_service._save_positions.assert_called()


def test_apply_partial_close_tier_1_success(mock_service):
    """Verifies atomic state update after Tier 1 partial close succeeds."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Tier 1 execution result: 0.4 SOL filled at 101.0
    result = PartialCloseResult(
        success=True,
        digest="0xmock_tier1",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=101.0,
        realized_pnl=0.40,  # 0.4 * (101 - 100)
        is_partial_fill=False
    )

    success = mock_service.apply_partial_close_to_position("SOL-USD", tier_index=1, result=result)

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])

    tier1 = plan.get_tier(1)
    assert tier1.status == "FILLED"
    assert tier1.filled_contracts == pytest.approx(0.4)
    assert tier1.realized_pnl_usd == pytest.approx(0.40)
    assert tier1.order_digest == "0xmock_tier1"

    # Position fields updated
    assert plan.remaining_contracts == pytest.approx(1.2)
    assert pos["contracts"] == pytest.approx(1.2)
    assert pos["size_usd"] == pytest.approx(120.0)
    assert pos["margin_used"] == pytest.approx(60.0)  # 120 / 2 leverage
    assert pos["accumulated_realized_pnl"] == pytest.approx(0.40)
    assert "SOL-USD" in mock_service.active_positions


def test_apply_partial_close_idempotency_duplicate_call(mock_service):
    """Verifies that a repeated call for an already FILLED tier is ignored idempotently."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    result = PartialCloseResult(
        success=True,
        digest="0xmock_tier1",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=101.0,
        realized_pnl=0.40,
        is_partial_fill=False
    )
    mock_service.apply_partial_close_to_position("SOL-USD", 1, result)

    # Duplicate call with identical tier
    dup_success = mock_service.apply_partial_close_to_position("SOL-USD", 1, result)

    assert dup_success is True
    pos = mock_service.active_positions["SOL-USD"]
    # Contracts must NOT be deducted twice!
    assert pos["contracts"] == pytest.approx(1.2)
    assert pos["accumulated_realized_pnl"] == pytest.approx(0.40)


def test_apply_partial_close_failed_result_reverts_to_pending(mock_service):
    """Verifies that an execution failure resets the tier to PENDING and preserves contracts."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Mark tier 1 as SUBMITTING
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    plan.get_tier(1).status = "SUBMITTING"
    pos["partial_tp_plan"] = plan.to_dict()

    fail_result = PartialCloseResult(
        success=False,
        error="IOC order expired without fills"
    )

    success = mock_service.apply_partial_close_to_position("SOL-USD", 1, fail_result)

    assert success is False
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    tier1 = updated_plan.get_tier(1)
    assert tier1.status == "PENDING"
    assert tier1.error_message == "IOC order expired without fills"
    assert updated_plan.remaining_contracts == pytest.approx(1.6)
    assert pos["contracts"] == pytest.approx(1.6)


def test_apply_partial_close_partial_fill_handling(mock_service):
    """Verifies that an incomplete fill is marked PARTIALLY_FILLED and only filled contracts are deducted."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    partial_result = PartialCloseResult(
        success=True,
        digest="0xmock_part",
        requested_contracts=0.4,
        filled_contracts=0.2,
        fill_price=101.0,
        realized_pnl=0.20,
        is_partial_fill=True
    )

    success = mock_service.apply_partial_close_to_position("SOL-USD", 1, partial_result)

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    tier1 = plan.get_tier(1)
    assert tier1.status == "PARTIALLY_FILLED"
    assert tier1.filled_contracts == pytest.approx(0.2)
    assert plan.remaining_contracts == pytest.approx(1.4)  # 1.6 - 0.2
    assert pos["contracts"] == pytest.approx(1.4)
    assert pos["size_usd"] == pytest.approx(140.0)
    assert pos["accumulated_realized_pnl"] == pytest.approx(0.20)


def test_apply_partial_close_final_tier_completes_and_finalizes_position(mock_service):
    """Verifies that completing all tiers finalizes the position and removes it from active_positions."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "sl_digest": "0xold_sl"
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Execute tiers 1, 2, 3
    for i in range(1, 4):
        res = PartialCloseResult(
            success=True,
            digest=f"0xmock_tier_{i}",
            requested_contracts=0.4,
            filled_contracts=0.4,
            fill_price=100.0 + i,
            realized_pnl=0.4 * i,
            is_partial_fill=False
        )
        mock_service.apply_partial_close_to_position("SOL-USD", i, res)

    # Position still open with 0.4 contracts
    assert "SOL-USD" in mock_service.active_positions
    assert mock_service.active_positions["SOL-USD"]["contracts"] == pytest.approx(0.4)

    # Execute final tier 4 (0.4 SOL @ 104.0)
    final_res = PartialCloseResult(
        success=True,
        digest="0xmock_tier_4",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=104.0,
        realized_pnl=1.60,
        is_partial_fill=False
    )
    final_success = mock_service.apply_partial_close_to_position("SOL-USD", 4, final_res)

    assert final_success is True
    # Position must now be removed from active_positions!
    assert "SOL-USD" not in mock_service.active_positions
    assert mock_service.win_count == 1


def test_reconciliation_zero_on_chain_balance_cleans_up(mock_service):
    """Verifies that an on-chain zero balance triggers full position closure finalization."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "tp_price": 103.0,
        "size_usd": 170.85,
        "contracts": 1.6,
        "product_id": 8,
        "accumulated_realized_pnl": 1.25
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # On-chain returns 0 contracts (closed on chain)
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=0.0)

    assert reconciled is True
    assert "SOL-USD" not in mock_service.active_positions
    assert mock_service.win_count == 1  # 1.25 > 0.001 recorded as WIN


def test_reconciliation_divergence_adjusts_remaining_contracts(mock_service):
    """Verifies that volume divergence between local plan and on-chain balance is adjusted safely."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # On-chain balance is only 1.2 SOL (0.4 was somehow executed externally)
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=1.2)

    assert reconciled is True
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan.remaining_contracts == pytest.approx(1.2)
    assert pos["contracts"] == pytest.approx(1.2)
    assert pos["size_usd"] == pytest.approx(120.0)


def test_legacy_position_backward_compatibility(mock_service):
    """Verifies that positions without partial_tp_plan continue to work safely without errors."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "size_usd": 170.85,
        "product_id": 8
    }

    # Calling apply on position without plan returns False cleanly
    res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=105.0)
    applied = mock_service.apply_partial_close_to_position("SOL-USD", 1, res)
    assert applied is False

    # Calling reconcile on position without plan returns False cleanly
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", 1.6)
    assert reconciled is False

    # Position remains completely untouched
    assert mock_service.active_positions["SOL-USD"]["size_usd"] == 170.85
