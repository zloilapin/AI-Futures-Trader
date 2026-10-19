"""
Rigorous edge-case tests validating fixes for Commit 3bc6076 audit:
1. Dust Remainder (< min_size) on final tier retained in local tracking with warning.
2. In-flight SUBMITTING tier verified with exchange before re-submission (no duplicate orders).
3. Breakeven TG alert awaits on-chain confirmation and honestly distinguishes EXCHANGE_PROTECTED vs SOFTWARE_ONLY.
4. FAILED tier bounded retry policy (up to 3 retries with cooldown) and exhaustion halting alert.
5. Sliced PnL round-trip fee accounting (actual fee extraction and estimated fallback).
6. Ambiguous IOC response retains SUBMITTING status until on-chain reconciliation.
7. External balance reduction (SL/manual) adjusts position but is NOT credited as TP profit.
8. Reconciliation enforces exact product_id matching across identical base coin symbols.
9. Three consecutive real rejections definitively halt the ladder and alert the operator.
10. Reconciled fill restores exact fill price, digest, PnL, and fees.
11. RiskManager preserves legitimate exchange min_size >= 100 contracts.
"""

import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from services.partial_tp_calculator import PartialTPPlan, TPTier, PartialTPCalculator
from agents.risk_manager import RiskManager


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

    async def mock_fetch_triggers(product_id: int):
        if hasattr(service, "_custom_triggers"):
            return service._custom_triggers
        place_mock = service.client.market.place_price_trigger_order
        ret = getattr(place_mock, "return_value", None)
        dig = getattr(getattr(ret, "data", None), "digest", None)
        if not dig and hasattr(place_mock, "side_effect") and callable(place_mock.side_effect):
            return []
        if dig:
            pos = service.active_positions.get("SOL-USD") or (next(iter(service.active_positions.values())) if service.active_positions else {})
            amt_str = pos.get("trigger_amount_x18", "0")
            trg = MagicMock()
            trg.digest = dig
            trg.order.digest = dig
            trg.order.order.subaccount = service.default_subaccount_id
            trg.order.order.amount = amt_str
            return [trg]
        return []

    service._fetch_trigger_orders = mock_fetch_triggers
    service.partial_tp_mode = "4_TIER"
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

    assert report is None
    assert "SOL-USD" in mock_service.active_positions
    pos = mock_service.active_positions["SOL-USD"]
    assert pos.get("is_dust") is True
    assert pos.get("dust_contracts") == pytest.approx(0.05)
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert updated_plan.tiers[0].status == "DUST_REMAINDER"

    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=0.0)
    assert reconciled is True
    assert "SOL-USD" not in mock_service.active_positions


@pytest.mark.asyncio
async def test_submitting_tier_verified_with_exchange_credits_fill(mock_service):
    """
    Edge Case 2A:
    Tier is in SUBMITTING status (e.g. from an unconfirmed previous run).
    On-chain query reveals balance decreased by 0.4 SOL and historical orders confirm fill.
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
    tier1.order_digest = "0xrec123"
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    mock_service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "SOL-USD", "amount": 1.2, "_product_id": 8}
    ])
    mock_order = MagicMock()
    mock_order.base_filled = -0.4
    mock_order.quote_filled = 40.8
    mock_order.digest = "0xrec123"
    mock_order.timestamp = time.time()
    mock_order.fee = 0.04
    mock_service.client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[mock_order])
    )
    mock_service.execute_partial_close = AsyncMock()

    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)

    mock_service.execute_partial_close.assert_not_called()
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

    mock_service.get_active_positions = AsyncMock(side_effect=ConnectionError("Nado RPC timeout"))
    mock_service.execute_partial_close = AsyncMock()

    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.05)

    assert report is None
    mock_service.execute_partial_close.assert_not_called()
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

    t1_res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=101.0, realized_pnl=0.40)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, t1_res)

    mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
        success=True,
        digest="0xtp2",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=102.0,
        realized_pnl=0.80
    ))
    mock_service.update_stop_loss_to_be = AsyncMock(return_value=False)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=102.05)

    assert report is not None
    pos = mock_service.active_positions["SOL-USD"]
    assert pos["sl_protection_mode"] == "SOFTWARE_ONLY"
    msg = mock_tg.send_message.call_args[0][0]
    assert "программный безубыток" in msg.lower()
    assert "Стоп-лосс на бирже перенесен в *БЕЗУБЫТОК*" not in msg


# ============================================================================
# NEW AUDIT TEST CASES (Addresses all reviewer requirements from feedback)
# ============================================================================

@pytest.mark.asyncio
async def test_ambiguous_ioc_result_retains_submitting_until_reconciliation(mock_service):
    """
    Reviewer Requirement 1:
    Nado accepted IOC, but network response was dropped / timed out.
    The system must retain SUBMITTING status and NOT send a duplicate order
    on subsequent ticks until the state is reconciled.
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

    # Order submission resulted in an ambiguous timeout
    ambig_res = PartialCloseResult(
        success=False,
        digest="0xpending_ioc",
        requested_contracts=0.4,
        error="Network timeout awaiting sequencer reply",
        is_ambiguous=True
    )

    applied = mock_service.apply_partial_close_to_position("SOL-USD", 1, ambig_res)
    assert applied is False

    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    tier1 = plan.tiers[0]

    # Critical invariant: must remain in SUBMITTING to block duplicate order submissions
    assert tier1.status == "SUBMITTING"
    assert tier1.order_digest == "0xpending_ioc"

    # Next tick: execute_partial_close must NOT be called while SUBMITTING is unresolved
    mock_service.execute_partial_close = AsyncMock()
    mock_service.get_active_positions = AsyncMock(side_effect=ConnectionError("Exchange still unreachable"))

    report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.50)
    assert report is None
    mock_service.execute_partial_close.assert_not_called()


@pytest.mark.asyncio
async def test_position_reduced_by_sl_or_external_not_credited_as_tp(mock_service):
    """
    Reviewer Requirement 2:
    On-chain position decreased (e.g. Stop-Loss or manual close by user),
    NOT by our partial TP order.
    The system must detect the absence of a matching TP fill in history,
    adjust remaining contracts, but NOT record any TP profit or mark tier as FILLED by TP!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "accumulated_realized_pnl": 0.0,
        "product_id": 8
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    tier1 = plan.tiers[0]
    tier1.status = "SUBMITTING"
    tier1.last_attempt_time = time.time()
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    # On-chain balance reduced to 1.2 contracts (0.4 contracts disappeared)
    mock_service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "SOL-USD", "amount": 1.2, "_product_id": 8}
    ])

    # But history has NO matching TP fill for this order attempt
    mock_service.client.market.get_subaccount_historical_orders = MagicMock(
        return_value=MagicMock(orders=[])
    )
    mock_service.client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[])
    )

    reconciled = await mock_service.reconcile_submitting_tier("SOL-USD", mock_service.active_positions["SOL-USD"], plan, tier1)
    assert reconciled is True

    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    t1 = updated_plan.tiers[0]

    # Remaining contracts adjusted to match on-chain reality
    assert updated_plan.remaining_contracts == pytest.approx(1.2)
    assert pos["contracts"] == pytest.approx(1.2)

    # CRITICAL: Must NOT be credited as TP profit!
    assert t1.status == "PENDING"
    assert t1.filled_contracts == 0.0
    assert t1.realized_pnl_usd == 0.0
    assert pos["accumulated_realized_pnl"] == 0.0


@pytest.mark.asyncio
async def test_reconciliation_exact_product_id_matching(mock_service):
    """
    Reviewer Requirement 3:
    Exchange returns multiple positions sharing the same base coin prefix (e.g. SOL-PERP vs SOL-USD).
    Reconciler must match by exact product_id, not just split('-')[0]!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8  # Product 8 is SOL-USD
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    tier1 = plan.tiers[0]
    tier1.status = "SUBMITTING"
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    # Active on-chain has two products starting with SOL: product 99 (0.5 contracts) and product 8 (1.6 contracts)
    mock_service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "SOL-OTHER", "amount": 0.5, "_product_id": 99},
        {"symbol": "SOL-USD", "amount": 1.6, "_product_id": 8}
    ])

    reconciled = await mock_service.reconcile_submitting_tier("SOL-USD", mock_service.active_positions["SOL-USD"], plan, tier1)
    assert reconciled is True

    # If it wrongly matched product 99 (0.5 contracts), diff would be 1.6 - 0.5 = 1.1!
    # Because it correctly matched product 8 (1.6 contracts), diff is 0, so status resets to PENDING
    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert updated_plan.remaining_contracts == pytest.approx(1.6)
    assert updated_plan.tiers[0].status == "PENDING"


@pytest.mark.asyncio
async def test_three_consecutive_rejections_halt_ladder_and_alert(mock_service):
    """
    Reviewer Requirement 4:
    A real sequence of 3 consecutive execution rejections increments retry_count 1 -> 2 -> 3,
    transitions tier to HALTED, dispatches critical alert, and completely halts ladder!
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

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        # Failure 1
        res1 = PartialCloseResult(success=False, error="Post-only cross rejected", is_ambiguous=False)
        mock_service.apply_partial_close_to_position("SOL-USD", 1, res1)
        pos = mock_service.active_positions["SOL-USD"]
        plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
        assert plan.tiers[0].status == "FAILED"
        assert plan.tiers[0].retry_count == 1

        # Failure 2
        res2 = PartialCloseResult(success=False, error="Insufficient liquidity", is_ambiguous=False)
        mock_service.apply_partial_close_to_position("SOL-USD", 1, res2)
        pos = mock_service.active_positions["SOL-USD"]
        plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
        assert plan.tiers[0].status == "FAILED"
        assert plan.tiers[0].retry_count == 2

        # Failure 3 (Exhaustion)
        res3 = PartialCloseResult(success=False, error="Engine maintenance", is_ambiguous=False)
        mock_service.apply_partial_close_to_position("SOL-USD", 1, res3)
        pos = mock_service.active_positions["SOL-USD"]
        plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
        assert plan.tiers[0].status == "HALTED"
        assert plan.tiers[0].retry_count == 3

        # Critical alert sent
        assert mock_tg.send_message.called
        assert "PARTIAL TP FAILED EXHAUSTED" in mock_tg.send_message.call_args[0][0]

        # On subsequent price tick: get_active_or_next_tier returns None, order is NOT attempted
        mock_service.execute_partial_close = AsyncMock()
        report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.50)
        assert report is None
        mock_service.execute_partial_close.assert_not_called()


@pytest.mark.asyncio
async def test_reconciled_partial_fill_restores_pnl_digest_and_fees(mock_service):
    """
    Reviewer Requirement 5:
    When an in-flight SUBMITTING tier is reconciled with exchange fill history:
    Exact fill_price, fee, realized_pnl, and order_digest are fully restored!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "accumulated_realized_pnl": 0.0,
        "product_id": 8
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    tier1 = plan.tiers[0]
    tier1.status = "SUBMITTING"
    tier1.order_digest = "0xdigest_verified"
    tier1.last_attempt_time = time.time()
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    mock_service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "SOL-USD", "amount": 1.2, "_product_id": 8}
    ])

    mock_fill_order = MagicMock()
    mock_fill_order.base_filled = -0.4
    mock_fill_order.quote_filled = 40.8  # Price = 40.8 / 0.4 = 102.0
    mock_fill_order.digest = "0xdigest_verified"
    mock_fill_order.timestamp = time.time()
    mock_fill_order.fee = 0.0408  # Actual fee
    mock_service.client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[mock_fill_order])
    )

    reconciled = await mock_service.reconcile_submitting_tier("SOL-USD", mock_service.active_positions["SOL-USD"], plan, tier1)
    assert reconciled is True

    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    t1 = updated_plan.tiers[0]

    assert t1.status == "FILLED"
    assert t1.filled_contracts == pytest.approx(0.4)
    assert t1.fill_price == pytest.approx(102.0)
    assert t1.order_digest == "0xdigest_verified"
    expected_pnl = round(0.4 * (102.0 - 100.0) - 0.0408, 4)
    assert t1.realized_pnl_usd == pytest.approx(expected_pnl, abs=0.01)
    assert pos["accumulated_realized_pnl"] == pytest.approx(expected_pnl, abs=0.01)


@pytest.mark.asyncio
async def test_risk_manager_preserves_confirmed_min_size_100():
    """
    Reviewer Requirement 6:
    When a product has min_size == 100 contracts and size_increment == 1.0,
    RiskManager must NOT override min_size with size_increment.
    """
    logger = MagicMock()
    rm = RiskManager(logger=logger, llm_client=None)

    derivatives_data = {
        "min_size": 100.0,
        "size_increment": 1.0,
        "min_notional": 0.0,
        "confirmed_min_size": True
    }

    # Verify that confirmed min_size == 100.0 is preserved and NOT clamped to size_increment (1.0)
    is_confirmed_min = bool(derivatives_data.get("confirmed_min_size"))
    is_dummy_default = (derivatives_data["min_size"] == 100.0 and not is_confirmed_min)
    min_size = derivatives_data["min_size"]
    if is_dummy_default:
        min_size = derivatives_data["size_increment"]

    assert min_size == 100.0

    # Test end-to-end with analyze
    result = await rm.analyze(
        ceo_decision={"decision": "LONG", "conviction": 85, "trade_action": "ENTER", "symbol": "XYZ-USD"},
        portfolio_data={"total_usd": 100.0, "available_margin": 100.0, "active_positions": {}},
        market_data={
            "price_data": {"current_price": 10.0},
            "derivatives_data": derivatives_data,
            "indicators": {"atr_14": 0.5}
        },
        effective_profile="BALANCED",
        macro_regime="TRENDING",
        strategy_mode="TREND_FOLLOWING"
    )
    # With balance $100 and price $10, calculated contracts (1.0) < min_size (100.0), which triggers MIN_SIZE veto
    assert result.get("approved") is False
    assert result.get("veto_category") == "MIN_SIZE"

    # Also test an altcoin with legitimate min_size = 500.0 (which the old >= 100.0 blanket override broke)
    result_500 = await rm.analyze(
        ceo_decision={"decision": "LONG", "conviction": 85, "trade_action": "ENTER", "symbol": "MEME-USD"},
        portfolio_data={"total_usd": 100.0, "available_margin": 100.0, "active_positions": {}},
        market_data={
            "price_data": {"current_price": 0.10},
            "derivatives_data": {"min_size": 500.0, "size_increment": 10.0, "min_notional": 0.0},
            "indicators": {"atr_14": 0.01}
        },
        effective_profile="BALANCED",
        macro_regime="TRENDING",
        strategy_mode="TREND_FOLLOWING"
    )
    # Calculated contracts ~50 < min_size 500.0, triggers MIN_SIZE veto instead of overriding 500.0 to 10.0
    assert result_500.get("approved") is False
    assert result_500.get("veto_category") == "MIN_SIZE"


@pytest.mark.asyncio
async def test_be_protection_state_not_prematurely_confirmed_before_update_stop_loss(mock_service):
    """
    Reviewer Requirement P1:
    In apply_partial_close_to_position(), protection_state must NOT be marked
    as confirmed 'BREAK_EVEN' before update_stop_loss_to_be actually confirms on-chain.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "protection_state": "PROTECTED",
        "sl_digest": "0xold_sl"
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Tier 1 executes
    t1_res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=101.0, realized_pnl=0.40)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, t1_res)

    # Prevent automatic background task from running immediately
    mock_service._in_fast_monitor_tp = True

    # Tier 2 completes (action = PROTECT_BREAKEVEN)
    t2_res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=102.0, realized_pnl=0.80)
    mock_service.apply_partial_close_to_position("SOL-USD", 2, t2_res)

    pos = mock_service.active_positions["SOL-USD"]
    # protection_state must NOT falsely claim confirmed BREAK_EVEN!
    assert pos["protection_state"] == "PENDING_BREAK_EVEN"
    # sl_price must NOT be mutated until update_stop_loss confirms it
    assert pos["sl_price"] == 98.0

    # Now simulate update_stop_loss_to_be confirming on-chain
    mock_res = MagicMock()
    mock_res.data.digest = "0xnew_be_sl"
    mock_service.client.market.place_price_trigger_order.return_value = mock_res
    mock_service._cancel_trigger_digests = AsyncMock(return_value=True)

    ok = await mock_service.update_stop_loss_to_be("SOL-USD")
    assert ok is True
    # Now it is confirmed!
    assert pos["protection_state"] == "BREAK_EVEN"
    assert pos["sl_price"] >= 100.10
    assert pos["sl_protection_mode"] == "EXCHANGE_PROTECTED"


@pytest.mark.asyncio
async def test_stop_loss_replacement_places_new_order_before_cancelling_old(mock_service):
    """
    Reviewer Requirement:
    During SL replacement, the new order MUST be placed first.
    If the new order fails (e.g. network error), the old order MUST NOT be cancelled,
    preventing any window of vulnerability.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.2,
        "product_id": 8,
        "sl_digest": "0xold_active_sl",
        "sl_type": "oracle_price_below",
        "protection_state": "PROTECTED"
    }

    call_order = []

    def mock_place(*args, **kwargs):
        call_order.append("place_new")
        res = MagicMock()
        res.data.digest = "0xnew_confirmed_sl"
        trg = MagicMock()
        trg.digest = "0xnew_confirmed_sl"
        trg.order.digest = "0xnew_confirmed_sl"
        trg.order.order.subaccount = mock_service.default_subaccount_id
        trg.order.order.amount = kwargs.get("amount_x18", str(int(-0.8 * 1e18)))
        mock_service._custom_triggers = [trg]
        return res

    async def mock_cancel(*args, **kwargs):
        call_order.append("cancel_old")
        return True

    mock_service.client.market.place_price_trigger_order = mock_place
    mock_service._cancel_trigger_digests = mock_cancel

    success = await mock_service.update_stop_loss(
        symbol="SOL-USD",
        new_sl_price=100.10,
        new_protection_state="BREAK_EVEN"
    )

    assert success is True
    # Verify order of operations: place_new MUST come BEFORE cancel_old!
    assert call_order == ["place_new", "cancel_old"]
    pos = mock_service.active_positions["SOL-USD"]
    assert pos["sl_digest"] == "0xnew_confirmed_sl"

    # Now verify failure case: if placement fails with network error, old trigger is retained!
    call_order.clear()
    mock_service.active_positions["SOL-USD"]["sl_digest"] = "0xretained_old_sl"

    def mock_place_fail(*args, **kwargs):
        call_order.append("place_new_fail")
        raise ConnectionError("Sequencer network timeout")

    mock_service.client.market.place_price_trigger_order = mock_place_fail

    fail_success = await mock_service.update_stop_loss(
        symbol="SOL-USD",
        new_sl_price=101.00
    )


    assert fail_success is False
    # cancel_old must NEVER be called when placement of new SL fails!
    assert call_order == ["place_new_fail", "place_new_fail", "place_new_fail"]
    # Pos retains old on-chain SL and EXCHANGE_PROTECTED!
    assert mock_service.active_positions["SOL-USD"]["sl_digest"] == "0xretained_old_sl"
    assert mock_service.active_positions["SOL-USD"]["sl_protection_mode"] == "EXCHANGE_PROTECTED"
    assert mock_service.active_positions["SOL-USD"]["sl_update_failed"] is True


@pytest.mark.asyncio
async def test_reconcile_submitting_tier_unconfirmed_fetch_preserves_position(mock_service):
    """
    Reviewer Requirement:
    Absence of position in an unconfirmed / failed fetch must NOT be treated as zero balance!
    Position must be preserved in active_positions and tier retained in SUBMITTING.
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

    # Query returned empty because network errored out
    mock_service.get_active_positions = AsyncMock(return_value=[])
    mock_service._last_positions_fetch_ok = False

    reconciled = await mock_service.reconcile_submitting_tier("SOL-USD", mock_service.active_positions["SOL-USD"], plan, tier1)
    
    # Must return False (unresolved) and NOT wipe position!
    assert reconciled is False
    assert "SOL-USD" in mock_service.active_positions
    pos = mock_service.active_positions["SOL-USD"]
    assert pos["contracts"] == 1.6
    # Tier stays SUBMITTING
    assert tier1.status == "SUBMITTING"


@pytest.mark.asyncio
async def test_sync_position_contracts_updates_all_dependent_fields(mock_service):
    """
    Reviewer Requirement:
    When on-chain volume expands (on-chain > local remaining), all dependent fields:
    pos["contracts"], size_usd, notional_usd, margin_used, trigger_amount_x18, and plan.remaining_contracts
    must be updated in lockstep.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 100.0,
        "notional_usd": 100.0,
        "margin_used": 10.0,
        "leverage": 10.0,
        "contracts": 1.0,
        "product_id": 8,
        "trigger_amount_x18": "-1000000000000000000"
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    tier1 = plan.tiers[0]
    tier1.status = "SUBMITTING"
    plan.remaining_contracts = 1.0
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    # On-chain volume increased to 2.5 contracts (external add or correction)
    mock_service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "SOL-USD", "amount": 2.5, "product_id": 8}
    ])
    mock_service._last_positions_fetch_ok = True

    reconciled = await mock_service.reconcile_submitting_tier("SOL-USD", mock_service.active_positions["SOL-USD"], plan, tier1)
    assert reconciled is True

    pos = mock_service.active_positions["SOL-USD"]
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])

    assert updated_plan.remaining_contracts == pytest.approx(2.5)
    assert pos["contracts"] == pytest.approx(2.5)
    assert pos["size_usd"] == pytest.approx(250.0)
    assert pos["notional_usd"] == pytest.approx(250.0)
    assert pos["margin_used"] == pytest.approx(25.0)
    assert pos["trigger_amount_x18"] == "-2500000000000000000"
    assert updated_plan.tiers[0].status == "PENDING"


@pytest.mark.asyncio
async def test_check_and_update_positions_finalizes_dust_remainder(mock_service):
    """
    Reviewer Requirement:
    Positions marked as DUST_REMAINDER must be cleanly finalized and removed from active_positions
    once confirmed zero on-chain.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 0.05,
        "contracts": 0.0005,
        "is_dust": True,
        "dust_contracts": 0.0005,
        "product_id": 8
    }
    mock_service.get_active_positions = AsyncMock(return_value=[])
    mock_service._last_positions_fetch_ok = True

    reports = await mock_service.check_and_update_positions("SOL-USD", current_price=103.0)
    assert len(reports) == 1
    assert reports[0]["status"] == "CLOSED"
    assert reports[0]["reason"] == "DUST_ZERO_CONFIRMED"
    assert "SOL-USD" not in mock_service.active_positions


@pytest.mark.asyncio
async def test_disconnected_get_active_positions_sets_fetch_ok_false(mock_service):
    """
    Audit Fix P2:
    When service is disconnected, get_active_positions must set _last_positions_fetch_ok = False.
    A disconnected empty list must NEVER be interpreted as confirmed zero positions!
    """
    mock_service.is_connected = False
    mock_service._last_positions_fetch_ok = True

    positions = await mock_service.get_active_positions()
    assert positions == []
    assert mock_service._last_positions_fetch_ok is False


@pytest.mark.asyncio
async def test_update_stop_loss_unverified_new_digest_retains_old_trigger(mock_service):
    """
    Audit Fix P1:
    If place_price_trigger_order returns a new digest, but that digest is NOT verified
    among active trigger orders on exchange, the old protective SL must NEVER be cancelled!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "product_id": 8,
        "sl_digest": "0xretained_old_sl",
        "sl_protection_mode": "EXCHANGE_PROTECTED",
        "sender": mock_service.default_subaccount_id
    }

    # Simulate place returns 0xghost_digest
    res = MagicMock()
    res.data.digest = "0xghost_digest"
    mock_service.client.market.place_price_trigger_order = MagicMock(return_value=res)

    # Active triggers query returns an active list that does NOT contain 0xghost_digest
    active_other_trigger = MagicMock()
    active_other_trigger.order.digest = "0xsome_other_order"
    mock_service._fetch_trigger_orders = AsyncMock(return_value=[active_other_trigger])

    # Cancel must NEVER be called
    mock_cancel = AsyncMock(return_value=True)
    mock_service._cancel_trigger_digests = mock_cancel

    ok = await mock_service.update_stop_loss("SOL-USD", 99.50)
    assert ok is False
    mock_cancel.assert_not_called()

    pos = mock_service.active_positions["SOL-USD"]
    assert pos["sl_digest"] == "0xretained_old_sl"
    assert pos["sl_protection_mode"] == "EXCHANGE_PROTECTED"
    assert pos["sl_update_failed"] is True


@pytest.mark.asyncio
async def test_reconcile_submitting_tier_rejects_loose_volume_and_out_of_window(mock_service):
    """
    Audit Fix P1:
    Reconciliation must NOT match orders with ±15% volume mismatch or outside strict time window.
    """
    pos = {
        "direction": "LONG",
        "entry_price": 100.0,
        "contracts": 1.0,
        "product_id": 8,
        "accumulated_realized_pnl": 0.0
    }
    tier = TPTier(
        tier_index=1,
        fraction=0.5,
        target_price=104.0,
        close_contracts=0.5,
        cumulative_fraction=0.5,
        status="SUBMITTING",
        last_attempt_time=1000.0,
        order_digest="0xunknown_digest"
    )
    plan = MagicMock()
    plan.size_increment = 0.01
    plan.min_size = 0.01
    plan.remaining_contracts = 1.0

    # Mock get_active_positions to return remaining 0.5 contracts on-chain (diff = 0.5)
    mock_service.get_active_positions = AsyncMock(
        return_value=[{"symbol": "SOL-USD", "product_id": 8, "amount": 0.5}]
    )

    # Order 1: volume is -0.58 (16% difference from diff=0.5) -> must be rejected
    order_loose = MagicMock(
        base_filled="-0.58",
        quote_filled="60.32",
        timestamp=1005000,  # 1005s, within window
        digest="0xorder_loose"
    )
    # Order 2: timestamp is 800s (200s before attempt) -> must be rejected
    order_stale = MagicMock(
        base_filled="-0.50",
        quote_filled="52.0",
        timestamp=800000,
        digest="0xorder_stale"
    )
    mock_history = MagicMock(orders=[order_loose, order_stale])
    mock_service.client.market.get_subaccount_historical_orders = MagicMock(return_value=mock_history)

    await mock_service.reconcile_submitting_tier(
        symbol="SOL-USD",
        pos=pos,
        plan=plan,
        tier=tier
    )

    # Neither matched -> not attributed to TP! Tier reset to PENDING, 0 filled, no fake fill recorded
    assert tier.status == "PENDING"
    assert tier.filled_contracts == 0.0
    assert tier.fill_price is None
    assert tier.realized_pnl_usd == 0.0
    assert pos["accumulated_realized_pnl"] == 0.0


@pytest.mark.asyncio
async def test_reconcile_submitting_tier_no_fake_target_price_pnl(mock_service):
    """
    Audit Fix P1/P2:
    If historical order has missing or zero quote_filled, reconciliation must NOT
    substitute target_price or calculate fake PnL. It must mark requires_pnl_reconciliation = True.
    """
    pos = {
        "direction": "LONG",
        "entry_price": 100.0,
        "contracts": 1.0,
        "product_id": 8,
        "accumulated_realized_pnl": 0.0
    }
    tier = TPTier(
        tier_index=1,
        fraction=0.5,
        target_price=105.0,
        close_contracts=0.5,
        cumulative_fraction=0.5,
        status="SUBMITTING",
        last_attempt_time=1000.0,
        order_digest="0xexact_digest"
    )
    plan = MagicMock()
    plan.size_increment = 0.01
    plan.min_size = 0.01
    plan.remaining_contracts = 1.0

    # Mock get_active_positions to return remaining 0.5 contracts on-chain (diff = 0.5)
    mock_service.get_active_positions = AsyncMock(
        return_value=[{"symbol": "SOL-USD", "product_id": 8, "amount": 0.5}]
    )

    # Exact digest match, but quote_filled is 0 (indexer latency / synthetic)
    order_zero_quote = MagicMock(
        base_filled="-0.50",
        quote_filled="0.0",
        timestamp=1005000,
        digest="0xexact_digest",
        fee=0
    )
    mock_history = MagicMock(orders=[order_zero_quote])
    mock_service.client.market.get_historical_orders_by_digest = MagicMock(return_value=mock_history)
    mock_service.client.market.get_subaccount_historical_orders = MagicMock(return_value=mock_history)
    mock_service._sync_position_contracts = MagicMock()

    await mock_service.reconcile_submitting_tier(
        symbol="SOL-USD",
        pos=pos,
        plan=plan,
        tier=tier
    )

    assert tier.status == "FILLED"
    assert tier.fill_price is None
    assert tier.realized_pnl_usd == 0.0
    assert getattr(tier, "requires_pnl_reconciliation", False) is True
    # Accumulated realized PnL in pos must NOT include fake target_price profit!
    assert pos["accumulated_realized_pnl"] == 0.0


@pytest.mark.asyncio
async def test_update_stop_loss_fail_closed_on_none_and_empty_triggers(mock_service):
    """
    Audit Requirement P1:
    Strict fail-closed: If _fetch_trigger_orders returns None, [], or raises an exception,
    the SL update must be rejected, and the old protective SL must NEVER be cancelled.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "product_id": 8,
        "sl_digest": "0xold_safe_sl",
        "sl_protection_mode": "EXCHANGE_PROTECTED",
        "contracts": 1.0,
        "trigger_amount_x18": str(int(-1.0 * 1e18))
    }
    mock_res = MagicMock()
    mock_res.data.digest = "0xnew_unverified_digest"
    mock_service.client.market.place_price_trigger_order = MagicMock(return_value=mock_res)
    mock_cancel = AsyncMock(return_value=True)
    mock_service._cancel_trigger_digests = mock_cancel

    # Case 1: _fetch_trigger_orders returns None
    mock_service._fetch_trigger_orders = AsyncMock(return_value=None)
    ok_none = await mock_service.update_stop_loss("SOL-USD", 99.0)
    assert ok_none is False
    mock_cancel.assert_not_called()
    assert mock_service.active_positions["SOL-USD"]["sl_digest"] == "0xold_safe_sl"
    assert mock_service.active_positions["SOL-USD"]["sl_protection_mode"] == "EXCHANGE_PROTECTED"

    # Case 2: _fetch_trigger_orders returns empty list []
    mock_service._fetch_trigger_orders = AsyncMock(return_value=[])
    ok_empty = await mock_service.update_stop_loss("SOL-USD", 99.0)
    assert ok_empty is False
    mock_cancel.assert_not_called()
    assert mock_service.active_positions["SOL-USD"]["sl_digest"] == "0xold_safe_sl"

    # Case 3: _fetch_trigger_orders raises an exception
    mock_service._fetch_trigger_orders = AsyncMock(side_effect=ConnectionError("Indexer timeout"))
    ok_err = await mock_service.update_stop_loss("SOL-USD", 99.0)
    assert ok_err is False
    mock_cancel.assert_not_called()
    assert mock_service.active_positions["SOL-USD"]["sl_digest"] == "0xold_safe_sl"


@pytest.mark.asyncio
async def test_update_stop_loss_rejects_volume_or_subaccount_mismatch(mock_service):
    """
    Audit Requirement P1:
    Even if the trigger order digest matches, if its volume or subaccount does not match,
    the update must be rejected and old SL retained.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "product_id": 8,
        "sl_digest": "0xold_safe_sl",
        "sl_protection_mode": "EXCHANGE_PROTECTED",
        "contracts": 1.0,
        "trigger_amount_x18": str(int(-1.0 * 1e18))
    }
    mock_res = MagicMock()
    mock_res.data.digest = "0xnew_digest"
    mock_service.client.market.place_price_trigger_order = MagicMock(return_value=mock_res)
    mock_cancel = AsyncMock(return_value=True)
    mock_service._cancel_trigger_digests = mock_cancel

    # Wrong volume: order has amount for 0.4 contracts instead of 1.0
    bad_vol_order = MagicMock()
    bad_vol_order.digest = "0xnew_digest"
    bad_vol_order.order.digest = "0xnew_digest"
    bad_vol_order.order.order.subaccount = mock_service.default_subaccount_id
    bad_vol_order.order.order.amount = str(int(-0.4 * 1e18))  # only 0.4 contracts!
    mock_service._fetch_trigger_orders = AsyncMock(return_value=[bad_vol_order])

    ok_bad_vol = await mock_service.update_stop_loss("SOL-USD", 99.0)
    assert ok_bad_vol is False
    mock_cancel.assert_not_called()
    assert mock_service.active_positions["SOL-USD"]["sl_digest"] == "0xold_safe_sl"

    # Wrong subaccount:
    bad_sub_order = MagicMock()
    bad_sub_order.digest = "0xnew_digest"
    bad_sub_order.order.digest = "0xnew_digest"
    bad_sub_order.order.order.subaccount = "0xwrong_subaccount_id"
    bad_sub_order.order.order.amount = str(int(-1.0 * 1e18))
    mock_service._fetch_trigger_orders = AsyncMock(return_value=[bad_sub_order])

    ok_bad_sub = await mock_service.update_stop_loss("SOL-USD", 99.0)
    assert ok_bad_sub is False
    mock_cancel.assert_not_called()
    assert mock_service.active_positions["SOL-USD"]["sl_digest"] == "0xold_safe_sl"


@pytest.mark.asyncio
async def test_reconcile_submitting_tier_rejects_ambiguous_multiple_matches(mock_service):
    """
    Audit Requirement P1:
    If history has multiple matching orders with identical volume in the time window,
    the code must NOT arbitrarily pick the first one. It must treat the reduction as external
    and refuse to attribute TP profit to the tier.
    """
    pos = {
        "direction": "LONG",
        "entry_price": 100.0,
        "contracts": 1.0,
        "product_id": 8,
        "accumulated_realized_pnl": 0.0
    }
    tier = TPTier(
        tier_index=1,
        fraction=0.5,
        target_price=104.0,
        close_contracts=0.5,
        cumulative_fraction=0.5,
        status="SUBMITTING",
        last_attempt_time=1000.0,
        order_digest="0xunknown_digest"
    )
    plan = MagicMock()
    plan.size_increment = 0.01
    plan.min_size = 0.01
    plan.remaining_contracts = 1.0

    mock_service.get_active_positions = AsyncMock(
        return_value=[{"symbol": "SOL-USD", "product_id": 8, "amount": 0.5}]
    )

    # Two distinct orders matching the exact same diff (-0.5) in the time window!
    order_a = MagicMock(
        base_filled="-0.50",
        quote_filled="52.0",
        timestamp=1005000,
        digest="0xorder_a"
    )
    order_b = MagicMock(
        base_filled="-0.50",
        quote_filled="52.1",
        timestamp=1006000,
        digest="0xorder_b"
    )
    mock_history = MagicMock(orders=[order_a, order_b])
    mock_service.client.market.get_subaccount_historical_orders = MagicMock(return_value=mock_history)

    await mock_service.reconcile_submitting_tier(
        symbol="SOL-USD",
        pos=pos,
        plan=plan,
        tier=tier
    )

    # Ambiguous -> NOT credited to TP! Tier reset to PENDING, 0 filled, no fake profit
    assert tier.status == "PENDING"
    assert tier.filled_contracts == 0.0
    assert tier.fill_price is None
    assert tier.realized_pnl_usd == 0.0
    assert pos["accumulated_realized_pnl"] == 0.0


def test_tptier_pnl_reconciliation_fields_persistence():
    """
    Audit Requirement P1/P2:
    TPTier must declare requires_pnl_reconciliation, pnl_reconciled, and fill_price
    so they are preserved across serialization to_dict() and deserialization from_dict().
    """
    tier = TPTier(
        tier_index=2,
        fraction=0.5,
        target_price=105.0,
        close_contracts=0.5,
        cumulative_fraction=0.5,
        status="FILLED",
        fill_price=None,
        realized_pnl_usd=0.0,
        requires_pnl_reconciliation=True,
        pnl_reconciled=False
    )

    data = tier.to_dict()
    assert "requires_pnl_reconciliation" in data
    assert data["requires_pnl_reconciliation"] is True
    assert "pnl_reconciled" in data
    assert data["pnl_reconciled"] is False
    assert data["fill_price"] is None

    restored = TPTier.from_dict(data)
    assert restored.requires_pnl_reconciliation is True
    assert restored.pnl_reconciled is False
    assert restored.fill_price is None


@pytest.mark.asyncio
async def test_dust_zero_confirmed_does_not_fabricate_exit_price(mock_service):
    """
    Audit Requirement P2:
    Confirming zero dust balance must finalize the position with exit_price=None,
    rather than passing off entry_price or current_price as real execution price.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 0.05,
        "contracts": 0.0005,
        "is_dust": True,
        "dust_contracts": 0.0005,
        "product_id": 8,
        "accumulated_realized_pnl": 1.25
    }
    mock_service.get_active_positions = AsyncMock(return_value=[])
    mock_service._last_positions_fetch_ok = True

    reports = await mock_service.check_and_update_positions("SOL-USD", current_price=103.50)
    assert len(reports) == 1
    rep = reports[0]
    assert rep["status"] == "CLOSED"
    assert rep["reason"] == "DUST_ZERO_CONFIRMED"
    # exit_price must NOT be 100.0 (entry) or 103.50 (current)!
    assert rep["exit_price"] is None
    # Realized PnL is retained honestly from partial slices
    assert rep["pnl_usd"] == 1.25
    assert "SOL-USD" not in mock_service.active_positions






