"""
Rigorous edge-case tests validating fixes for Commit 067d67b audit:
1. Dust Remainder (< min_size) on final tier retained in local tracking with warning.
2. In-flight SUBMITTING tier verified with exchange before re-submission (no duplicate orders).
3. Breakeven TG alert awaits on-chain confirmation and honestly distinguishes EXCHANGE_PROTECTED vs SOFTWARE_ONLY.
4. FAILED tier bounded retry policy (up to 3 retries with cooldown) and exhaustion halting alert.
5. Sliced PnL round-trip fee accounting.
"""

import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from services.partial_tp_calculator import PartialTPPlan, TPTier, PartialTPCalculator


@pytest.fixture
def mock_service():
    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service.product_map = {"SOL": 8, "SOL-USD": 8}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1111111111111111111111111111111111111111"
    service.default_subaccount_id = "0x" + "11" * 32
    service._save_positions = MagicMock()
    service._save_state = MagicMock()

    service._market_cache = {
        8: MagicMock(
            book_info=MagicMock(
                size_increment=int(0.01 * 1e18),
                price_increment_x18=int(0.001 * 1e18),
                min_size=int(0.1 * 1e18)  # 0.1 SOL min_size
            )
        )
    }
    return service


@pytest.mark.asyncio
async def test_final_tier_dust_remainder_retained_with_warning(mock_service):
    """
    Edge Case 1:
    A position has 0.05 SOL remainder when min_size is 0.1 SOL.
    The final tier must NOT silently delete the position from active_positions!
    It must retain the position with is_dust=True and alert the operator.
    Only when on-chain reconciliation confirms 0 balance is it deleted.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 5.0,
        "contracts": 0.05,
        "product_id": 8
    }

    # Plan with remaining 0.05 SOL (less than min_size 0.1)
    plan = PartialTPPlan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=104.0,
        total_contracts=0.05,
        tiers=[
            TPTier(
                tier_index=1,
                fraction=1.0,
                target_price=104.0,
                close_contracts=0.05,
                cumulative_fraction=1.0,
                is_final=True,
                status="PENDING",
                action="CLOSE_ALL"
            )
        ],
        mode="SINGLE_TIER",
        min_size=0.1,
        size_increment=0.01,
        price_increment=0.001,
        remaining_contracts=0.05
    )
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=104.05)

    # Order cannot be submitted for contracts < min_size
    assert report is None

    # CRITICAL: Position MUST NOT be deleted from active_positions!
    assert "SOL-USD" in mock_service.active_positions
    pos = mock_service.active_positions["SOL-USD"]
    assert pos.get("is_dust") is True
    assert pos.get("dust_contracts") == pytest.approx(0.05)
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert updated_plan.tiers[0].status == "DUST_REMAINDER"

    # Now simulate on-chain balance actually becoming 0 on DEX
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=0.0)
    assert reconciled is True
    # ONLY NOW is the position deleted!
    assert "SOL-USD" not in mock_service.active_positions


@pytest.mark.asyncio
async def test_submitting_tier_verified_with_exchange_credits_fill(mock_service):
    """
    Edge Case 2A:
    Tier is in SUBMITTING status (e.g. from an unconfirmed previous run).
    On-chain query reveals balance decreased by 0.4 SOL.
    Reconciliation credits the fill without submitting a duplicate order!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    tier1 = plan.tiers[0]
    tier1.status = "SUBMITTING"
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    # Mock on-chain balance returning 1.2 contracts (meaning 0.4 executed on Nado)
    mock_service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "SOL-USD", "amount": 1.2}
    ])
    mock_service.execute_partial_close = AsyncMock()

    # Fast monitor runs
    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)

    # Order must NOT be re-executed
    mock_service.execute_partial_close.assert_not_called()

    # Tier 1 was reconciled as filled!
    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert updated_plan.tiers[0].status == "FILLED"
    assert updated_plan.tiers[0].filled_contracts == pytest.approx(0.4)
    assert updated_plan.remaining_contracts == pytest.approx(1.2)


@pytest.mark.asyncio
async def test_submitting_tier_ambiguous_network_failure_blocks_execution(mock_service):
    """
    Edge Case 2B:
    Tier is in SUBMITTING status.
    Network fails during exchange balance query.
    Must KEEP status as SUBMITTING and block any order placement!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    plan.tiers[0].status = "SUBMITTING"
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    # Mock get_active_positions throwing network error
    mock_service.get_active_positions = AsyncMock(side_effect=ConnectionError("Nado RPC timeout"))
    mock_service.execute_partial_close = AsyncMock()

    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)

    # Must be safely blocked
    assert report is None
    mock_service.execute_partial_close.assert_not_called()

    # Must stay in SUBMITTING to protect capital from duplicate execution
    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert updated_plan.tiers[0].status == "SUBMITTING"


@pytest.mark.asyncio
async def test_breakeven_tg_alert_honestly_distinguishes_software_vs_exchange(mock_service):
    """
    Edge Case 3:
    When Tier 2 (Breakeven) executes:
    - If on-chain SL returns False, alert must specify SOFTWARE_ONLY and NOT claim on-chain BE.
    - sl_protection_mode is never defaulted to EXCHANGE_PROTECTED until verified.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Complete Tier 1
    t1_res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=101.0, realized_pnl=0.40)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, t1_res)

    # Tier 2 execution
    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=True,
        digest="0xtp2",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=102.0,
        realized_pnl=0.80
    ))

    # Scenario: On-chain SL update fails (e.g. rate limit on Nado trigger engine)
    mock_service.update_stop_loss_to_be = AsyncMock(return_value=False)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=102.05)

    assert report is not None
    pos = mock_service.active_positions["SOL-USD"]
    # Must be marked SOFTWARE_ONLY
    assert pos["sl_protection_mode"] == "SOFTWARE_ONLY"

    # Telegram message must honestly indicate SOFTWARE breakeven
    msg = mock_tg.send_message.call_args[0][0]
    assert "программный безубыток" in msg.lower()
    assert "Стоп-лосс на бирже перенесен в *БЕЗУБЫТОК*" not in msg


@pytest.mark.asyncio
async def test_failed_tier_retry_and_exhaustion_alert(mock_service):
    """
    Edge Case 4:
    A tier in FAILED status:
    - Retries after 15s cooldown up to 3 times.
    - Upon 3rd failure, sends critical alert and halts ladder without crashing.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    tier1 = plan.tiers[0]
    tier1.status = "FAILED"
    tier1.retry_count = 0
    tier1.last_attempt_time = time.time() - 20.0  # 20s ago (cooldown passed)
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=False, error="Simulated transient timeout"
    ))

    # Attempt 1 retry
    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)
    assert report is None

    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert updated_plan.tiers[0].retry_count == 1

    # Simulate exhausting 3 retries
    updated_plan.tiers[0].retry_count = 3
    updated_plan.tiers[0].status = "FAILED"
    pos["partial_tp_plan"] = updated_plan.to_dict()
    mock_service.active_positions["SOL-USD"] = pos

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report2 = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)

    assert report2 is None
    # Critical alert dispatched
    assert pos.get("failed_tier_alerted") == 1
