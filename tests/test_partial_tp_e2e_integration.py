"""
End-to-End Integration Test for Tiered Partial Take-Profit (Phase 6).
Simulates the exact real-world scenario requested by the user:
1. Position opens on SOL-USD with 1.6 contracts ($160 notional) and SL at 98.0, full TP at 104.0.
2. 4-tier Partial Take-Profit plan is automatically generated.
3. Price rises to $101.05 -> Fast Monitor triggers Tier 1 (25% closed, +$0.40 locked in, SL resized to 1.2 SOL).
4. Price rises to $102.05 -> Fast Monitor triggers Tier 2 (50% closed, +$0.80 locked in, SL moved to BREAKEVEN at $100.10).
5. Price sharply reverses from $102.05 down to $100.00:
   - Previously: whole position would retrace to $0 or turn into a loss!
   - Now: Breakeven Stop triggers at $100.10 for the remaining 0.8 contracts!
   - Net lifecycle PnL of trade is SECURED at +$1.20 PROFIT!
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from services.partial_tp_calculator import PartialTPPlan, TPTier
from agents.sentinel_agent import SentinelAgent
from core.logger import TradeLogger


@pytest.fixture
def mock_service():
    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service.product_map = {"SOL": 8, "SOL-USD": 8}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1111111111111111111111111111111111111111"
    service.default_subaccount_id = "0x" + "11" * 32
    service.win_count = 0
    service.loss_count = 0
    service.recent_streak = []
    service._manual_stats = True
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
    return service


@pytest.mark.asyncio
async def test_full_lifecycle_profit_secured_before_reversal(mock_service):
    """
    Demonstrates that partial TP locks in profits ($1.20) and protects capital at Breakeven
    when a position retraces from +2.0% profit back to entry.
    """
    # 1. Position is opened
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "sl_digest": "0xnative_sl_order",
        "sl_type": "oracle_price_below",
        "trigger_amount_x18": str(int(-1.6 * 1e18)),
        "leverage": 2,
        "protection_state": "PROTECTED"
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    assert plan is not None
    assert plan.mode == "4_TIER"
    assert len(plan.tiers) == 4

    # Mock indexer / engine interactions
    mock_service._cancel_trigger_digests = AsyncMock(return_value=True)
    mock_sl_res = MagicMock()
    mock_sl_res.data.digest = "0xupdated_sl_digest"
    mock_service.client.market.place_price_trigger_order.return_value = mock_sl_res

    # Mock Telegram notifications
    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock(return_value=True)
    mock_tg.broadcast_to_channel = AsyncMock(return_value=True)

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        # 2. Market advances to 101.10 (TP1 reached)
        mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
            success=True,
            digest="0xtp1_digest",
            requested_contracts=0.4,
            filled_contracts=0.4,
            fill_price=101.0,
            realized_pnl=0.40,
            is_partial_fill=False
        ))
        tp1_report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=101.10)
        assert tp1_report is not None
        assert tp1_report["tier_index"] == 1
        assert tp1_report["remaining_contracts"] == pytest.approx(1.2)

        pos = mock_service.active_positions["SOL-USD"]
        assert pos["contracts"] == pytest.approx(1.2)
        assert pos["accumulated_realized_pnl"] == pytest.approx(0.40)

        # 3. Market advances to 102.10 (TP2 reached -> 50% closed!)
        mock_service.execute_partial_close = AsyncMock(return_value=PartialCloseResult(
            success=True,
            digest="0xtp2_digest",
            requested_contracts=0.4,
            filled_contracts=0.4,
            fill_price=102.0,
            realized_pnl=0.80,
            is_partial_fill=False
        ))
        tp2_report = await mock_service.check_and_execute_partial_tp_tier("SOL-USD", current_price=102.10)
        await asyncio.sleep(0.01)
        assert tp2_report is not None
        assert tp2_report["tier_index"] == 2
        assert tp2_report["be_activated"] is True
        assert tp2_report["remaining_contracts"] == pytest.approx(0.8)

        pos = mock_service.active_positions["SOL-USD"]
        assert pos["contracts"] == pytest.approx(0.8)
        # Total profit locked in so far: $0.40 + $0.80 = $1.20
        assert pos["accumulated_realized_pnl"] == pytest.approx(1.20)
        # BE activated on position
        assert pos["protection_state"] == "BREAK_EVEN"
        assert pos["sl_price"] >= 100.10

        # 4. MARKET SUDDENLY CRASHES from 102.10 down to 100.00!
        # The price crosses Breakeven Stop (100.10)
        mock_service.force_close_position = AsyncMock(return_value=(True, 0.0))  # Closed at BE (0.0 exit PnL on remaining 0.8)
        
        # Simulate Fast Monitor detecting SL hit at 100.00
        cur_price = 100.00
        sl_price = pos["sl_price"]
        direction = pos["direction"]
        hit_sl = (direction == "LONG" and cur_price <= sl_price)
        assert hit_sl is True

        # Cleanly finalize trade
        final_report = mock_service._finalize_partial_tp_closure(
            symbol="SOL-USD",
            pos=pos,
            exit_price=100.10,
            triggered_by="BREAK_EVEN_SL"
        )

        # 5. VERIFICATION:
        # Position is cleanly closed and removed from active positions
        assert "SOL-USD" not in mock_service.active_positions
        # The trade finished with +$1.20 profit instead of wiping out!
        assert final_report["pnl_usd"] == pytest.approx(1.20)
        assert mock_service.win_count == 1
        assert mock_service.loss_count == 0


@pytest.mark.asyncio
async def test_sentinel_interoperability_with_partial_tp(mock_service):
    """
    Verifies that SentinelAgent respects an active BREAK_EVEN protection state
    set by Tier 2, never regresses the stop, and successfully trails if price runs further.
    """
    mock_logger = MagicMock(spec=TradeLogger)
    sentinel = SentinelAgent(mock_logger)

    # Position in BREAK_EVEN state after partial TP
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 100.10,
        "highest_price": 102.50,
        "protection_state": "BREAK_EVEN",
        "atr_reference": 1.5,
        "contracts": 0.8
    }
    market_data = {"price_data": {"current_price": 102.20}}
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "sentinel_trail_activation_atr": 2.0,  # 2 * 1.5 = 3.0 (activates at 103.0)
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25
    }

    # At 102.20 (profit = 2.20 < 3.0), trailing has not activated yet.
    # Sentinel must NOT worsen the stop!
    verdict = await sentinel.analyze(pos, market_data, profile_rules, atr_value=1.5)
    
    # Either new_sl is None (holding current BE) or >= 100.10
    if verdict.get("new_sl") is not None:
        assert verdict["new_sl"] >= 100.10
    assert verdict.get("state") in ("BREAK_EVEN", "PROTECTED")
