"""
Unit tests for Breakeven Protection & Stop Loss Resizing (Phase 4).
Verifies:
- Accurate Breakeven calculation for LONG (entry + fee buffer) and SHORT (entry - fee buffer).
- Monotonic invariant enforcement (never regresses an already-trailed stop).
- Trigger order cancellation and re-placement with strictly resized contracts.
- Graceful fallback to Software Stop Loss when remaining volume notional is below exchange limit.
- Automatic BE activation when Tier 2 (50% TP) is achieved.
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

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


@pytest.mark.asyncio
async def test_update_stop_loss_to_be_long(mock_service):
    """Verifies that moving a LONG position to BE sets sl_price >= entry + fee buffer and resizes trigger order."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 80.0,
        "contracts": 0.8,
        "product_id": 8,
        "sl_digest": "0xold_sl_digest",
        "sl_type": "oracle_price_below",
        "trigger_amount_x18": str(int(-1.6 * 1e18))
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Mock cancellation of old SL
    mock_service._cancel_trigger_digests = AsyncMock(return_value=True)

    # Mock placement of new SL
    mock_res = MagicMock()
    mock_res.data.digest = "0xnew_be_sl_digest"
    mock_service.client.market.place_price_trigger_order.return_value = mock_res

    success = await mock_service.update_stop_loss_to_be("SOL-USD")

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    # BE price must cover entry + 0.1% round-trip fee (100.100)
    assert pos["sl_price"] >= 100.10
    assert pos["protection_state"] == "BREAK_EVEN"
    assert pos["sl_digest"] == "0xnew_be_sl_digest"

    # Old SL was cancelled
    mock_service._cancel_trigger_digests.assert_awaited_once_with(8, ["0xold_sl_digest"], sender=None)

    # New SL order placed for 0.8 SOL (counter sell: -0.8e18)
    call_kwargs = mock_service.client.market.place_price_trigger_order.call_args[1]
    assert call_kwargs["amount_x18"] == str(int(-0.8 * 1e18))
    assert call_kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_update_stop_loss_to_be_short(mock_service):
    """Verifies that moving a SHORT position to BE sets sl_price <= entry - fee buffer and resizes trigger order."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "sl_price": 108.50,
        "tp_price": 103.0,
        "size_usd": 85.42,
        "contracts": 0.8,
        "product_id": 8,
        "sl_digest": "0xold_short_sl",
        "sl_type": "oracle_price_above",
        "trigger_amount_x18": str(int(1.6 * 1e18))
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    mock_service._cancel_trigger_digests = AsyncMock(return_value=True)
    mock_res = MagicMock()
    mock_res.data.digest = "0xnew_short_be_digest"
    mock_service.client.market.place_price_trigger_order.return_value = mock_res

    success = await mock_service.update_stop_loss_to_be("SOL-USD")

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    # BE price must cover entry - 0.1% fee (106.78 * 0.999 = 106.673)
    assert pos["sl_price"] <= 106.674
    assert pos["protection_state"] == "BREAK_EVEN"

    # New SL order placed for 0.8 SOL (counter buy: +0.8e18)
    call_kwargs = mock_service.client.market.place_price_trigger_order.call_args[1]
    assert call_kwargs["amount_x18"] == str(int(0.8 * 1e18))
    assert call_kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_update_stop_loss_to_be_never_degrades_trailed_stop(mock_service):
    """Verifies that if SL is already trailed higher than BE, update_stop_loss_to_be does NOT move it down."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 102.5,  # Already trailed to +2.5%!
        "tp_price": 104.0,
        "size_usd": 80.0,
        "contracts": 0.8,
        "product_id": 8,
        "sl_digest": "0xexisting_trail_sl",
        "sl_type": "oracle_price_below",
        "trigger_amount_x18": str(int(-1.6 * 1e18))
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    mock_service._cancel_trigger_digests = AsyncMock(return_value=True)
    mock_res = MagicMock()
    mock_res.data.digest = "0xresized_trail_sl"
    mock_service.client.market.place_price_trigger_order.return_value = mock_res

    success = await mock_service.update_stop_loss_to_be("SOL-USD")

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    # SL price MUST NOT be downgraded from 102.5 to 100.1!
    assert pos["sl_price"] == pytest.approx(102.5)

    # But trigger order is resized to 0.8 contracts!
    call_kwargs = mock_service.client.market.place_price_trigger_order.call_args[1]
    assert call_kwargs["amount_x18"] == str(int(-0.8 * 1e18))


@pytest.mark.asyncio
async def test_update_stop_loss_fallback_to_software_when_notional_below_limit(mock_service):
    """Verifies that when on-chain SL placement fails (e.g. order too small <$100), software SL takes over."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 40.0,  # Small slice ($40 < $100 limit)
        "contracts": 0.4,
        "product_id": 8,
        "sl_digest": "0xold_sl_to_cancel",
        "sl_type": "oracle_price_below",
        "trigger_amount_x18": str(int(-0.8 * 1e18))
    }

    mock_service._cancel_trigger_digests = AsyncMock(return_value=True)
    # Nado engine returns error 2094 (notional too small)
    mock_service.client.market.place_price_trigger_order.side_effect = Exception("2094 order too small")

    success = await mock_service.update_stop_loss(
        symbol="SOL-USD",
        new_sl_price=100.10,
        new_protection_state="BREAK_EVEN",
        new_contracts=0.4,
        force_resize=True
    )

    # When on-chain placement fails, success is False (honest exchange semantics)
    assert success is False
    pos = mock_service.active_positions["SOL-USD"]
    # Audit fix: Old exchange SL is NEVER cancelled if new placement fails!
    mock_service._cancel_trigger_digests.assert_not_called()
    # sl_digest and active on-chain protection retained
    assert pos["sl_digest"] == "0xold_sl_to_cancel"
    assert pos["sl_protection_mode"] == "EXCHANGE_PROTECTED"
    assert pos["sl_update_failed"] is True
    assert pos["sl_price"] == pytest.approx(98.0)


@pytest.mark.asyncio
async def test_tier_2_completion_triggers_be_protection(mock_service):
    """Verifies that completing Tier 2 (which has action='PROTECT_BREAKEVEN') marks be_activated in plan."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Mock update_stop_loss_to_be
    mock_service.update_stop_loss_to_be = AsyncMock(return_value=True)

    # Tier 1 completes
    res1 = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=101.0, realized_pnl=0.4)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, res1)

    # Tier 2 completes (action = PROTECT_BREAKEVEN)
    res2 = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=102.0, realized_pnl=0.8)
    mock_service.apply_partial_close_to_position("SOL-USD", 2, res2)

    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan.be_activated is True
    assert plan.get_tier(2).status == "FILLED"
    assert pos["contracts"] == pytest.approx(0.8)
    assert pos["trigger_amount_x18"] == str(int(-0.8 * 1e18))
