import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from services.nado_trading_service import NadoTradingService
from agents.telegram_agent import TelegramAgent

def get_mock_service():
    service = NadoTradingService.__new__(NadoTradingService)
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x" + "11" * 20
    service.logger = MagicMock()
    service.is_connected = True
    service.default_subaccount_id = "0x" + "12" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2, "ETH": 4, "ETH-USD": 4, "ZEC": 8, "ZEC-USD": 8}
    service.active_positions = {}
    service.pending_reconciliations = {}
    service.processed_fills_registry = {}
    service.processed_fill_digests = set()
    service._reconcile_lock = None
    service._position_locks = {}
    service._market_cache = {}
    service.win_count = 0
    service.loss_count = 0
    service.recent_streak = []
    service.cooldown_until = 0.0
    service._last_cooldown_processed_len = 0
    service._last_positions_fetch_ok = True
    service._save_positions = MagicMock()
    service._save_pending_reconciliations = MagicMock()
    service._save_processed_fills = MagicMock()
    service._save_state = MagicMock()
    service.client = MagicMock()
    return service

@pytest.mark.asyncio
async def test_duplicate_position_blocked_in_open_position():
    """Verifies that open_position strictly blocks duplicate positions for the same asset."""
    service = get_mock_service()
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 100.0,
        "product_id": 2
    }

    # Attempt to open duplicate under canonical symbol BTC-USD
    res1 = await service.open_position("BTC-USD", "LONG", 60100.0, 100.0, 65000.0, 58000.0, 10)
    assert res1 is False

    # Attempt to open duplicate under base symbol BTC
    res2 = await service.open_position("BTC", "LONG", 60100.0, 100.0, 65000.0, 58000.0, 10)
    assert res2 is False

    # Attempt to open duplicate under slash format BTC/USD
    res3 = await service.open_position("BTC/USD", "LONG", 60100.0, 100.0, 65000.0, 58000.0, 10)
    assert res3 is False

    # ETH-USD is not open, should proceed past duplicate check
    with patch.object(service, "_get_market_parameters", return_value={"size_increment_x18": 0}):
        res4 = await service.open_position("ETH-USD", "LONG", 3000.0, 100.0, 3200.0, 2900.0, 10)
        # Blocked downstream by missing market params, NOT by duplicate guard
        assert res4 is False
        service.logger.warning.assert_not_called()

@pytest.mark.asyncio
async def test_zombie_triggers_cancelled_on_force_close():
    """Verifies that force_close_position automatically cancels remaining SL/TP triggers on exchange."""
    service = get_mock_service()
    sl_digest = "0x" + "aa" * 32
    tp_digest = "0x" + "bb" * 32
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 200.0,
        "product_id": 2,
        "sl_digest": sl_digest,
        "tp_digest": tp_digest,
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs

    # Mock cancel_trigger_orders
    service.client.market.cancel_trigger_orders = MagicMock(return_value=MagicMock())
    service.client.market.close_position = MagicMock(return_value="0xtx123")
    service.get_active_positions = AsyncMock(return_value=[{
        "symbol": "BTC-USD",
        "pnl": 10.0
    }])

    success, pnl = await service.force_close_position("BTC-USD", bypass_check=True)
    assert success is True
    assert pnl == 10.0
    assert "BTC-USD" not in service.active_positions

    # Verify that cancel_trigger_orders was called with both digests
    service.client.market.cancel_trigger_orders.assert_called_once()
    called_params = service.client.market.cancel_trigger_orders.call_args[0][0]
    called_hex = ['0x' + d.hex() if isinstance(d, bytes) else d for d in called_params.digests]
    assert sl_digest in called_hex
    assert tp_digest in called_hex
    assert called_params.productIds == [2]

@pytest.mark.asyncio
async def test_zombie_triggers_cancelled_on_native_trigger_closure():
    """Verifies that check_and_update_positions cancels the opposite trigger when closed on-chain."""
    service = get_mock_service()
    sl_digest = "0x" + "cc" * 32
    tp_digest = "0x" + "dd" * 32
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 200.0,
        "tp_price": 65000.0,
        "sl_price": 58000.0,
        "product_id": 2,
        "sl_digest": sl_digest,
        "tp_digest": tp_digest,
        "sender": service.default_subaccount_id
    }

    # Position is no longer in get_active_positions (closed on-chain)
    service.get_active_positions = AsyncMock(return_value=[])
    service.client.market.cancel_trigger_orders = MagicMock(return_value=MagicMock())

    closed_reports = await service.check_and_update_positions("BTC-USD", 65100.0)
    assert len(closed_reports) == 1
    assert "BTC-USD" not in service.active_positions

    # Remaining opposite trigger order must be cancelled
    service.client.market.cancel_trigger_orders.assert_called_once()
    called_params = service.client.market.cancel_trigger_orders.call_args[0][0]
    called_hex = ['0x' + d.hex() if isinstance(d, bytes) else d for d in called_params.digests]
    assert sl_digest in called_hex
    assert tp_digest in called_hex

def test_telegram_signal_sub_100_notional_warning():
    """Verifies that TelegramAgent adds a software-only protection warning when notional < $100."""
    logger = MagicMock()
    llm = MagicMock()
    tg_agent = TelegramAgent(logger, llm)

    # Case 1: Small trade under $100 notional (e.g. $60 deposit * 1x)
    sub_100_data = {
        "symbol": "BTC-USD",
        "ceo_verdict": {
            "decision": "LONG",
            "conviction": 80,
            "reasoning_en": "Bullish momentum"
        },
        "risk_verdict": {
            "entry_price": 60000.0,
            "take_profit_price": 63000.0,
            "take_profit_pct": 5.0,
            "stop_loss_price": 58500.0,
            "stop_loss_pct": 2.5,
            "notional_size_usd": 60.0,
            "position_size_pct": 10.0,
            "risk_reward_ratio": 2.0
        }
    }
    msg_sub_100 = tg_agent.format_signal(sub_100_data)
    assert "Защита" in msg_sub_100
    assert "≥ $100" in msg_sub_100

    # Case 2: Trade >= $100 notional (eligible for native on-chain triggers)
    over_100_data = dict(sub_100_data)
    over_100_data["risk_verdict"] = dict(sub_100_data["risk_verdict"])
    over_100_data["risk_verdict"]["notional_size_usd"] = 150.0

    msg_over_100 = tg_agent.format_signal(over_100_data)
    assert "На бирже Nado триггеры требуют объем ≥ $100" not in msg_over_100

@pytest.mark.asyncio
async def test_manual_close_short_zero_tp_sl_never_yields_zero_exit_price():
    """Verifies that a manually closed SHORT without explicit TP/SL (e.g. SOL-USD) does not yield exit_price 0.0."""
    service = get_mock_service()
    service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 109.41,
        "size_usd": 32.80,
        "margin_used": 6.56,
        "tp_price": 0.0,
        "sl_price": 0.0,
        "product_id": 4,
        "sl_digest": None,
        "tp_digest": None,
        "sender": service.default_subaccount_id
    }
    # Position closed on exchange
    service.get_active_positions = AsyncMock(return_value=[])

    closed_reports = await service.check_and_update_positions("SOL-USD", 108.50)
    assert len(closed_reports) == 1
    report = closed_reports[0]
    assert report["exit_price"] == 108.50  # Must be real market exit price, NEVER 0.0
    # Expected gross PnL for short: (109.41 - 108.50) / 109.41 * 32.80 = 0.2727
    expected_gross = (109.41 - 108.50) / 109.41 * 32.80
    expected_fees = 32.80 * 0.001
    assert abs(report["pnl_usd"] - (expected_gross - expected_fees)) < 0.01
    assert "SOL-USD" not in service.active_positions

@pytest.mark.asyncio
async def test_manual_close_long_zero_tp_sl_never_yields_zero_exit_price():
    """Verifies that a manually closed LONG without explicit TP/SL does not yield exit_price 0.0."""
    service = get_mock_service()
    service.active_positions["ETH-USD"] = {
        "direction": "LONG",
        "entry_price": 2500.0,
        "size_usd": 50.0,
        "margin_used": 10.0,
        "tp_price": 0.0,
        "sl_price": 0.0,
        "product_id": 3,
        "sl_digest": None,
        "tp_digest": None,
        "sender": service.default_subaccount_id
    }
    service.get_active_positions = AsyncMock(return_value=[])

    closed_reports = await service.check_and_update_positions("ETH-USD", 2550.0)
    assert len(closed_reports) == 1
    report = closed_reports[0]
    assert report["exit_price"] == 2550.0  # Must be real market price, NEVER 0.0
    expected_gross = (2550.0 - 2500.0) / 2500.0 * 50.0
    expected_fees = 50.0 * 0.001
    assert abs(report["pnl_usd"] - (expected_gross - expected_fees)) < 0.01
    assert "ETH-USD" not in service.active_positions

@pytest.mark.asyncio
async def test_closure_fallback_when_current_price_is_zero():
    """Verifies that if current_price is 0 or unavailable, exit_price safely falls back to entry_price (breakeven)."""
    service = get_mock_service()
    service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 109.41,
        "size_usd": 32.80,
        "margin_used": 6.56,
        "tp_price": 0.0,
        "sl_price": 0.0,
        "product_id": 4,
        "sl_digest": None,
        "tp_digest": None,
        "sender": service.default_subaccount_id
    }
    service.get_active_positions = AsyncMock(return_value=[])

    closed_reports = await service.check_and_update_positions("SOL-USD", 0.0)
    assert len(closed_reports) == 1
    report = closed_reports[0]
    assert report["exit_price"] == 109.41  # Falls back to entry_price, NEVER 0.0
    # Gross PnL is 0, Net PnL is only fees (-0.03)
    assert report["pnl_usd"] < 0  # Only fees
    assert abs(report["pnl_usd"] - (-32.80 * 0.001)) < 0.01
@pytest.mark.asyncio
async def test_force_close_reconciles_exact_fill_from_indexer():
    """Verifies that force_close_position queries indexer for exact fill price and net PnL."""
    service = get_mock_service()
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 30000.0,
        "product_id": 2,
        "contracts": 0.5,
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs
    service.client.market.close_position = MagicMock(return_value="0xtx123")
    service.get_active_positions = AsyncMock(return_value=[{
        "symbol": "BTC-USD",
        "pnl": 10.0  # Pre-close estimate, should be overridden by exact fill
    }])

    # Mock historical orders returned from indexer (both base and quote in x18 notation)
    mock_order = MagicMock()
    mock_order.base_filled = "-500000000000000000"  # -0.5 base (closing LONG) in x18
    mock_order.quote_filled = "35000000000000000000000"  # $35,000 quote in x18 -> exit price 70,000.0
    mock_order.timestamp = str(int(time.time() * 1000))
    mock_order.fee = "200000000000000000"  # 0.20 USD fee in x18

    mock_hist = MagicMock()
    mock_hist.orders = [mock_order]
    service.client.market.get_subaccount_historical_orders = MagicMock(return_value=mock_hist)

    success, pnl = await service.force_close_position("BTC-USD", bypass_check=True)

    assert success is True
    # Gross PnL: 0.5 * (70000 - 60000) = 5000.0. Net: 5000.0 - 0.20 = 4999.80
    assert pnl == pytest.approx(4999.80, rel=1e-3)
    assert "BTC-USD" in service._last_close_info
    assert service._last_close_info["BTC-USD"]["exit_price"] == pytest.approx(70000.0)
    assert service._last_close_info["BTC-USD"]["is_exact"] is True


@pytest.mark.asyncio
async def test_force_close_reconciles_human_readable_quote_units():
    """Verifies that force_close_position handles quote_filled provided as raw human USD without scaling."""
    service = get_mock_service()
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 30000.0,
        "product_id": 2,
        "contracts": 0.5,
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs
    service.client.market.close_position = MagicMock(return_value="0xtx123")
    service.get_active_positions = AsyncMock(return_value=[{"symbol": "BTC-USD", "pnl": 0.0}])

    # Mock historical orders with quote_filled in human dollars
    mock_order = MagicMock()
    mock_order.base_filled = "-500000000000000000"  # -0.5 base (x18)
    mock_order.quote_filled = "35000"  # 35,000 human USD
    mock_order.timestamp = str(int(time.time() * 1000))
    mock_order.fee = "0.20"  # human fee

    mock_hist = MagicMock()
    mock_hist.orders = [mock_order]
    service.client.market.get_subaccount_historical_orders = MagicMock(return_value=mock_hist)

    success, pnl = await service.force_close_position("BTC-USD", bypass_check=True)

    assert success is True
    assert pnl == pytest.approx(4999.80, rel=1e-3)
    assert service._last_close_info["BTC-USD"]["exit_price"] == pytest.approx(70000.0)


@pytest.mark.asyncio
async def test_cancel_trigger_digests_matches_product_ids_length():
    """Verifies that _cancel_trigger_digests supplies equal lengths for productIds and digests."""
    service = get_mock_service()
    service.is_connected = True
    service.default_subaccount_id = "0xsubaccount"
    
    cancel_mock = MagicMock()
    service.client.market.cancel_trigger_orders = cancel_mock

    digests = ["0xdigest1", "0xdigest2"]
    res = await service._cancel_trigger_digests(product_id=5, digests=digests, sender="0xsubaccount")

    assert res is True
    cancel_mock.assert_called_once()
    call_args = cancel_mock.call_args[0][0]
    assert call_args.productIds == [5, 5]
    assert call_args.digests == ["0xdigest1", "0xdigest2"]
    assert len(call_args.productIds) == len(call_args.digests)


@pytest.mark.asyncio
async def test_force_close_handles_amount_zero_error_2002_gracefully():
    """Verifies that when close_position encounters error 2002 ('amount is zero'),
    it verifies zero balance on-chain, cleans up local state, and returns success."""
    service = get_mock_service()
    service.active_positions["ZEC-USD"] = {
        "direction": "LONG",
        "entry_price": 1235.0,
        "size_usd": 100.0,
        "product_id": 8,
        "contracts": 0.09,
        "sl_digest": "0xsl",
        "tp_digest": "0xtp",
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs
    
    # Simulate Nado API throwing Error 2002 because on-chain SL already closed the position
    service.client.market.close_position.side_effect = Exception(
        '{"status":"failure","error_code":2002,"error":"Invalid order amount: The provided amount is zero."}'
    )
    service.get_active_positions = AsyncMock(return_value=[])
    service._cancel_trigger_digests = AsyncMock(return_value=True)

    success, pnl = await service.force_close_position("ZEC-USD", bypass_check=True)

    assert success is True
    assert pnl == 0.0
    assert "ZEC-USD" not in service.active_positions
    service._cancel_trigger_digests.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_close_error_2002_retains_position_if_still_open_on_chain():
    """Critical safety test: if close_position raises error 2002 but on-chain query shows
    position is STILL OPEN, local tracking is RETAINED and not silently deleted!"""
    service = get_mock_service()
    service.active_positions["BTC-USD"] = {
        "direction": "SHORT",
        "entry_price": 80000.0,
        "size_usd": 40.0,
        "product_id": 2,
        "contracts": 0.0005,
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs

    # API returns 2002
    service.client.market.close_position.side_effect = Exception(
        '{"status":"failure","error_code":2002,"error":"Invalid order amount: The provided amount is zero."}'
    )
    # But get_active_positions confirms position is STILL OPEN on-chain
    service.get_active_positions = AsyncMock(return_value=[{
        "symbol": "BTC-USD",
        "amount": -0.0005,
        "_product_id": 2
    }])

    success, pnl = await service.force_close_position("BTC-USD", bypass_check=True)

    # Must reject false cleanup and keep position for next retry cycle
    assert success is False
    assert "BTC-USD" in service.active_positions


@pytest.mark.asyncio
async def test_reconcile_partial_tp_remainder_calculates_stop_loss_pnl():
    """Verifies that when on-chain contracts hit 0 after TP1, the remainder PnL is
    calculated using sl_price instead of being erased with entry_price."""
    service = get_mock_service()
    service.active_positions["ETH-USD"] = {
        "direction": "LONG",
        "entry_price": 2000.0,
        "sl_price": 1900.0,
        "tp_price": 2200.0,
        "size_usd": 2000.0,
        "contracts": 1.0,
        "product_id": 4,
        "accumulated_realized_pnl": 50.0  # +$50 from TP1
    }
    # Initialize plan: 2 tiers (0.5 contracts each)
    service.initialize_partial_tp_plan("ETH-USD")
    pos = service.active_positions["ETH-USD"]
    plan_dict = pos["partial_tp_plan"]
    # Simulate tier 1 filled, 0.5 contracts remaining
    plan_dict["tiers"][0]["status"] = "FILLED"
    plan_dict["remaining_contracts"] = 0.5
    pos["partial_tp_plan"] = plan_dict

    # Now on-chain balance is confirmed ZERO (simulated with is_verified_pnl=True)
    reconciled = service.reconcile_partial_tp_with_chain("ETH-USD", on_chain_contracts=0.0, is_verified_pnl=True)

    assert reconciled is True
    assert "ETH-USD" not in service.active_positions
    # Remainder loss: 0.5 * (1900 - 2000) = -$50.0. Remainder fee: 0.5 * 1900 * 0.0005 = $0.475.
    # Total PnL: +$50.0 (TP1) - $50.0 (remainder loss) - $0.475 (fee) = -$0.475
    # The trade must be recorded as a LOSS (net < 0), not as a fake +$50 WIN!
    assert service.loss_count == 1
    assert service.win_count == 0


@pytest.mark.asyncio
async def test_force_close_error_2002_retains_position_if_fetch_fails():
    """Safety test: if close_position raises 2002 but get_active_positions fails (API/indexer error),
    fail-closed guarantee RETAINS position in active_positions and sets requires_reconciliation=True."""
    service = get_mock_service()
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 65000.0,
        "size_usd": 130.0,
        "product_id": 2,
        "contracts": 0.002,
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs

    service.client.market.close_position.side_effect = Exception("2002: amount is zero")
    # Simulate API/network failure when checking fresh positions
    service.get_active_positions = AsyncMock(side_effect=Exception("Connection reset by peer"))

    success, pnl = await service.force_close_position("BTC-USD", bypass_check=True)

    assert success is False
    assert pnl == 0.0
    assert "BTC-USD" in service.active_positions
    assert service.active_positions["BTC-USD"].get("requires_reconciliation") is True


@pytest.mark.asyncio
async def test_reconcile_closing_fills_aggregates_multiple_fills():
    """Verifies that _reconcile_closing_fills_from_history aggregates multiple partial fills,
    computes VWAP exit price, sums taker fees, and determines exact net PnL."""
    service = get_mock_service()
    import time

    # Mock order objects
    fill1 = MagicMock()
    fill1.base_filled = -0.4 * 1e18  # Closing LONG (SELL)
    fill1.quote_filled = 40000.0 * 1e18  # $100,000/BTC
    fill1.fee_x18 = 20.0 * 1e18
    fill1.timestamp = int(time.time() * 1000)
    fill1.digest = "0xfill1"

    fill2 = MagicMock()
    fill2.base_filled = -0.6 * 1e18  # Closing LONG (SELL)
    fill2.quote_filled = 61200.0 * 1e18  # $102,000/BTC
    fill2.fee_x18 = 30.6 * 1e18
    fill2.timestamp = int(time.time() * 1000)
    fill2.digest = "0xfill2"

    mock_hist = MagicMock()
    mock_hist.orders = [fill1, fill2]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=100000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    assert recon["is_exact"] is True
    # Total base = 1.0, Total quote = 101,200 -> VWAP = 101,200.0
    assert pytest.approx(recon["exit_price"]) == 101200.0
    assert pytest.approx(recon["total_base"]) == 1.0
    assert pytest.approx(recon["fee"]) == 50.6
    # Gross: 1.0 * (101200 - 100000) = 1200.0. Net: 1200.0 - 50.6 = 1149.4
    assert pytest.approx(recon["net_pnl"]) == 1149.4
    assert recon["digests"] == ["0xfill1", "0xfill2"]


@pytest.mark.asyncio
async def test_force_close_preserves_accumulated_realized_pnl_from_pos_data():
    """Verifies that force_close_position correctly includes accumulated_realized_pnl from
    local authoritative pos_data even if exchange snapshot target_pos omits the field."""
    service = get_mock_service()
    service.active_positions["BTC-USD"] = {
        "direction": "LONG",
        "entry_price": 60000.0,
        "size_usd": 120.0,
        "product_id": 2,
        "contracts": 0.002,
        "accumulated_realized_pnl": 25.50,  # $25.50 banked from partial TP1
        "sender": service.default_subaccount_id
    }

    mock_sub = MagicMock()
    mock_sub.subaccount = service.default_subaccount_id
    mock_subs = MagicMock()
    mock_subs.subaccounts = [mock_sub]
    service.client.subaccount.get_subaccounts.return_value = mock_subs

    service.client.market.close_position.return_value = "0xtx_close"
    # Exchange query returns snapshot WITHOUT accumulated_realized_pnl
    service.get_active_positions = AsyncMock(return_value=[{
        "symbol": "BTC-USD",
        "pnl": -5.0  # Remainder loss
    }])

    with patch.object(service, "_reconcile_closing_fills_from_history", return_value={"is_exact": False}):
        success, final_pnl = await service.force_close_position("BTC-USD", bypass_check=True)

    assert success is True
    # Final PnL must be remainder (-$5.0) + accumulated (+$25.50) = +$20.50
    assert pytest.approx(final_pnl) == 20.50


@pytest.mark.asyncio
async def test_unverified_remainder_closure_marks_pending_without_streak_corruption():
    """Verifies that when on-chain contracts hit 0 without confirmed fills,
    the closure is preserved in pending_reconciliations with status PNL_RECONCILIATION_PENDING
    and does NOT update win_count or loss_count."""
    service = get_mock_service()
    service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 150.0,
        "sl_price": 140.0,
        "tp_price": 170.0,
        "size_usd": 300.0,
        "contracts": 2.0,
        "product_id": 8,
        "accumulated_realized_pnl": 10.0
    }
    service.product_map["SOL"] = 8
    service.product_map["SOL-USD"] = 8

    service.initialize_partial_tp_plan("SOL-USD")
    pos = service.active_positions["SOL-USD"]
    plan_dict = pos["partial_tp_plan"]
    plan_dict["remaining_contracts"] = 1.0
    pos["partial_tp_plan"] = plan_dict

    # On-chain returns 0 without verified indexer fills (is_verified_pnl=None -> defaults False for open remainder)
    reconciled = service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=0.0)

    assert reconciled is True
    assert "SOL-USD" not in service.active_positions
    # Crucial check: streak must NOT be modified yet!
    assert service.loss_count == 0
    assert service.win_count == 0
    # Must be tracked in pending_reconciliations
    assert "SOL-USD" in service.pending_reconciliations
    pending_entry = service.pending_reconciliations["SOL-USD"]
    assert pending_entry["status"] == "PNL_RECONCILIATION_PENDING"
    assert pending_entry["contracts"] == 1.0


@pytest.mark.asyncio
async def test_reconcile_pending_trade_fills_resolves_and_updates_streak():
    """Verifies that reconcile_pending_trade_fills polls history, matches the exact fill,
    updates the win/loss streak and cooldown, and removes the trade from pending_reconciliations."""
    service = get_mock_service()
    service.pending_reconciliations["SOL-USD"] = {
        "symbol": "SOL-USD",
        "base_symbol": "SOL",
        "product_id": 8,
        "subaccount": service.default_subaccount_id,
        "direction": "LONG",
        "entry_price": 150.0,
        "contracts": 1.0,
        "size_usd": 150.0,
        "accumulated_realized_pnl": 10.0,
        "closed_at": 1000.0,
        "status": "PNL_RECONCILIATION_PENDING",
        "reason": "ON_CHAIN_RECONCILED_SL"
    }

    # Mock indexer resolving the remainder fill at $135 (loss of $15 gross - $0.0675 fee = -$15.0675)
    # Total PnL: +$10.0 (TP1) - $15.0675 = -$5.0675 -> LOSS
    mock_recon_res = {
        "is_exact": True,
        "exit_price": 135.0,
        "total_base": 1.0,
        "total_quote": 135.0,
        "fee": 0.0675,
        "net_pnl": -15.0675,
        "digests": ["0xsol_sl_digest"]
    }

    with patch.object(service, "_reconcile_closing_fills_from_history", return_value=mock_recon_res):
        resolved = await service.reconcile_pending_trade_fills()

    assert len(resolved) == 1
    assert resolved[0]["symbol"] == "SOL-USD"
    assert pytest.approx(resolved[0]["pnl_usd"]) == -5.0675
    assert "SOL-USD" not in service.pending_reconciliations
    assert "0xsol_sl_digest" in service.processed_fill_digests
    # Streak now updated with verified loss!
    assert service.loss_count == 1
    assert service.win_count == 0


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_18_percent_volume_divergence():
    """Verifies that a volume divergence of 18% (e.g. expected 1.0, found 1.18)
    is strictly rejected (is_exact=False) under the <= 1% tolerance rule."""
    service = get_mock_service()
    import time

    fill = MagicMock()
    fill.base_filled = -1.18 * 1e18  # 1.18 contracts closed
    fill.quote_filled = 118000.0 * 1e18  # $100,000/BTC
    fill.fee_x18 = 59.0 * 1e18
    fill.timestamp = int(time.time() * 1000)
    fill.digest = "0xfill_divergent"

    mock_hist = MagicMock()
    mock_hist.orders = [fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=95000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    # 1.18 vs 1.0 is 18% divergence -> MUST NOT be exact
    assert recon["is_exact"] is False
    assert recon["is_volume_exact"] is False
    assert recon["net_pnl"] is None


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_missing_or_invalid_timestamp():
    """Verifies that orders with missing or unparseable timestamps are skipped
    and never accepted into an exact reconciliation."""
    service = get_mock_service()
    import time

    fill_no_ts = MagicMock()
    fill_no_ts.base_filled = -1.0 * 1e18
    fill_no_ts.quote_filled = 100000.0 * 1e18
    fill_no_ts.fee_x18 = 50.0 * 1e18
    fill_no_ts.timestamp = None
    fill_no_ts.digest = "0xfill_no_ts"

    fill_bad_ts = MagicMock()
    fill_bad_ts.base_filled = -1.0 * 1e18
    fill_bad_ts.quote_filled = 100000.0 * 1e18
    fill_bad_ts.fee_x18 = 50.0 * 1e18
    fill_bad_ts.timestamp = "invalid_timestamp_string"
    fill_bad_ts.digest = "0xfill_bad_ts"

    mock_hist = MagicMock()
    mock_hist.orders = [fill_no_ts, fill_bad_ts]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=95000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    assert recon["is_exact"] is False
    assert recon["total_base"] == 0.0


@pytest.mark.asyncio
async def test_reconcile_closing_fills_supports_missing_timestamp_when_allowed():
    """Verifies that real Nado Indexer fills with timestamp=None are successfully
    reconciled when allow_missing_timestamp=True or target_digest matches."""
    service = get_mock_service()
    import time

    now = time.time()
    fill_no_ts = MagicMock()
    fill_no_ts.base_filled = -1.0 * 1e18
    fill_no_ts.quote_filled = 100000.0 * 1e18
    fill_no_ts.fee_x18 = 50.0 * 1e18
    fill_no_ts.timestamp = None
    fill_no_ts.digest = "0xfill_real_nado"

    mock_hist = MagicMock()
    mock_hist.orders = [fill_no_ts]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=95000.0,
        expected_contracts=1.0,
        close_timestamp=now,
        allow_missing_timestamp=True
    )

    assert recon["is_exact"] is True
    assert recon["total_base"] == pytest.approx(1.0)
    assert recon["exit_price"] == pytest.approx(100000.0)
    assert recon["digests"] == ["0xfill_real_nado"]
    assert recon["fee"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_reconcile_closing_fills_excludes_tp_and_open_digests():
    """Verifies that TP tier fills and open order digests are excluded so they
    cannot be accidentally aggregated into the remainder closure."""
    service = get_mock_service()
    import time

    now_ms = int(time.time() * 1000)

    # TP1 fill (0.5 contracts @ $105,000)
    tp1_fill = MagicMock()
    tp1_fill.base_filled = -0.5 * 1e18
    tp1_fill.quote_filled = 52500.0 * 1e18
    tp1_fill.fee_x18 = 26.25 * 1e18
    tp1_fill.timestamp = now_ms - 20000
    tp1_fill.digest = "0xtp1_tier_digest"

    # Remainder market close fill (0.5 contracts @ $102,000)
    remainder_fill = MagicMock()
    remainder_fill.base_filled = -0.5 * 1e18
    remainder_fill.quote_filled = 51000.0 * 1e18
    remainder_fill.fee_x18 = 25.5 * 1e18
    remainder_fill.timestamp = now_ms - 2000
    remainder_fill.digest = "0xremainder_close_digest"

    mock_hist = MagicMock()
    mock_hist.orders = [tp1_fill, remainder_fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    # Exclude TP1 digest explicitly
    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=100000.0,
        expected_contracts=0.5,
        close_timestamp=time.time(),
        excluded_digests={"0xtp1_tier_digest"}
    )

    assert recon["is_exact"] is True
    assert recon["digests"] == ["0xremainder_close_digest"]
    assert pytest.approx(recon["total_base"]) == 0.5
    assert pytest.approx(recon["exit_price"]) == 102000.0


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_zero_expected_contracts():
    """Verifies that if expected_contracts is 0, reconciliation fail-closes
    with is_exact=False to avoid matching arbitrary orders."""
    service = get_mock_service()

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=100000.0,
        expected_contracts=0.0
    )

    assert recon["is_exact"] is False
    assert recon["total_base"] == 0.0


@pytest.mark.asyncio
async def test_reconcile_closing_fills_handles_none_fee_with_fee_x18_fallback():
    """Verifies that if order.fee is present but None, the fallback correctly
    reads order.fee_x18 instead of failing or defaulting to an estimate."""
    service = get_mock_service()
    import time

    fill = MagicMock()
    fill.base_filled = -1.0 * 1e18
    fill.quote_filled = 100000.0 * 1e18
    fill.fee = None  # Key bug scenario: attribute exists with value None!
    fill.fee_x18 = 50.0 * 1e18
    fill.timestamp = int(time.time() * 1000)
    fill.digest = "0xfill_fee_none"

    mock_hist = MagicMock()
    mock_hist.orders = [fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=90000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    assert recon["is_exact"] is True
    assert recon["is_fee_exact"] is True
    assert pytest.approx(recon["fee"]) == 50.0


@pytest.mark.asyncio
async def test_reconcile_closing_fills_estimated_fee_marks_unverified():
    """Verifies that if fee and fee_x18 are both missing or zero, the fallback
    estimator is used, marking is_fee_exact=False and is_exact=False."""
    service = get_mock_service()
    import time

    fill = MagicMock()
    fill.base_filled = -1.0 * 1e18
    fill.quote_filled = 100000.0 * 1e18
    fill.fee = 0
    fill.fee_x18 = 0
    fill.timestamp = int(time.time() * 1000)
    fill.digest = "0xfill_no_fee"

    mock_hist = MagicMock()
    mock_hist.orders = [fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=90000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    assert recon["is_exact"] is False
    assert recon["is_fee_exact"] is False
    # Estimated fee: 100,000 * 0.0005 = 50.0
    assert pytest.approx(recon["fee"]) == 50.0


@pytest.mark.asyncio
async def test_reconcile_pending_finds_fill_executed_before_detection():
    """Verifies that when a position closes on-chain before monitoring detects it
    (e.g. 5 minutes earlier), the open_time-based search window still finds the fill."""
    service = get_mock_service()
    now = 2000.0

    fill_sl = MagicMock()
    fill_sl.base_filled = 2.0 * 1e18  # Closing SHORT (BUY)
    fill_sl.quote_filled = 60000.0 * 1e18  # $30,000/ETH
    fill_sl.fee_x18 = 30.0 * 1e18
    fill_sl.timestamp = int((now - 300.0) * 1000)  # Executed 300s (5 min) prior!
    fill_sl.digest = "0xearly_sl_digest"

    mock_hist = MagicMock()
    mock_hist.orders = [fill_sl]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    service.pending_reconciliations["ETH-USD"] = {
        "symbol": "ETH-USD",
        "base_symbol": "ETH",
        "product_id": 4,
        "subaccount": service.default_subaccount_id,
        "direction": "SHORT",
        "entry_price": 32000.0,
        "contracts": 2.0,
        "size_usd": 64000.0,
        "accumulated_realized_pnl": 0.0,
        "open_time": now - 1000.0,  # Opened 1000s ago
        "detected_at": now,         # Detected just now
        "status": "PNL_RECONCILIATION_PENDING"
    }

    resolved = await service.reconcile_pending_trade_fills()

    assert len(resolved) == 1
    assert resolved[0]["symbol"] == "ETH-USD"
    # Gross: 2.0 * (32000 - 30000) = +4000. Net: 4000 - 30 = 3970
    assert pytest.approx(resolved[0]["pnl_usd"]) == 3970.0
    assert "0xearly_sl_digest" in service.processed_fill_digests
    assert service.win_count == 1


@pytest.mark.asyncio
async def test_processed_fill_digests_prevents_reusing_fills():
    """Verifies that an execution digest already in processed_fill_digests is skipped
    and never matched for a subsequent trade."""
    service = get_mock_service()
    import time

    used_fill = MagicMock()
    used_fill.base_filled = -1.0 * 1e18
    used_fill.quote_filled = 100000.0 * 1e18
    used_fill.fee_x18 = 50.0 * 1e18
    used_fill.timestamp = int(time.time() * 1000)
    used_fill.digest = "0xalready_processed_digest"

    service.processed_fill_digests.add("0xalready_processed_digest")

    mock_hist = MagicMock()
    mock_hist.orders = [used_fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=90000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    # Must be skipped because digest was already processed
    assert recon["is_exact"] is False
    assert recon["total_base"] == 0.0
    assert "0xalready_processed_digest" not in recon["digests"]


@pytest.mark.asyncio
async def test_reconcile_pending_increments_attempts_and_triggers_alert():
    """Verifies that unresolved pending reconciliations increment attempts on each cycle
    and log an error alert when reaching 20 attempts without dropping the record."""
    service = get_mock_service()

    service.pending_reconciliations["BTC-USD"] = {
        "symbol": "BTC-USD",
        "product_id": 2,
        "subaccount": service.default_subaccount_id,
        "direction": "LONG",
        "entry_price": 60000.0,
        "contracts": 1.0,
        "attempts": 19,
        "status": "PNL_RECONCILIATION_PENDING"
    }

    # No fills found
    mock_hist = MagicMock()
    mock_hist.orders = []
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    with patch("services.nado_trading_service.logger.error") as mock_err:
        resolved = await service.reconcile_pending_trade_fills()

    assert len(resolved) == 0
    assert "BTC-USD" in service.pending_reconciliations
    assert service.pending_reconciliations["BTC-USD"]["attempts"] == 20
    # Alert triggered at attempt 20
    mock_err.assert_called_once()
    assert "CRITICAL PENDING AUDIT" in mock_err.call_args[0][0]


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_stitching_across_disparate_times():
    """Verifies that fills occurring at disparate times (e.g. 45 min apart) are NOT
    stitched together into a single closing event, preventing fake VWAP/PnL calculation."""
    service = get_mock_service()

    t_now = 5000.0

    # Old fill: 0.2 BTC closed 45 minutes prior (t = 2300)
    fill_old = MagicMock()
    fill_old.base_filled = -0.2 * 1e18
    fill_old.quote_filled = 20000.0 * 1e18  # $100,000/BTC
    fill_old.fee_x18 = 10.0 * 1e18
    fill_old.timestamp = int(2300.0 * 1000)
    fill_old.digest = "0xfill_old"

    # New fill: 0.3 BTC closed just now (t = 5000)
    fill_new = MagicMock()
    fill_new.base_filled = -0.3 * 1e18
    fill_new.quote_filled = 31500.0 * 1e18  # $105,000/BTC
    fill_new.fee_x18 = 15.75 * 1e18
    fill_new.timestamp = int(t_now * 1000)
    fill_new.digest = "0xfill_new"

    mock_hist = MagicMock()
    mock_hist.orders = [fill_old, fill_new]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    # Expected contracts is 0.5 BTC (the sum of both!)
    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=95000.0,
        expected_contracts=0.5,
        min_timestamp=2000.0,
        max_timestamp=5060.0
    )

    # Must NOT stitch 0.2 and 0.3 together across 45 minutes!
    assert recon["is_exact"] is False
    assert recon["net_pnl"] is None


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_ambiguous_duplicate_clusters():
    """Verifies that if multiple distinct execution clusters in the window each match
    the expected volume, reconciliation refuses to guess and leaves is_exact=False."""
    service = get_mock_service()

    # Event 1 at t=1000: 0.5 BTC
    fill1 = MagicMock()
    fill1.base_filled = -0.5 * 1e18
    fill1.quote_filled = 50000.0 * 1e18
    fill1.fee_x18 = 25.0 * 1e18
    fill1.timestamp = int(1000.0 * 1000)
    fill1.digest = "0xevent1"

    # Event 2 at t=3000: 0.5 BTC
    fill2 = MagicMock()
    fill2.base_filled = -0.5 * 1e18
    fill2.quote_filled = 51000.0 * 1e18
    fill2.fee_x18 = 25.5 * 1e18
    fill2.timestamp = int(3000.0 * 1000)
    fill2.digest = "0xevent2"

    mock_hist = MagicMock()
    mock_hist.orders = [fill1, fill2]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=95000.0,
        expected_contracts=0.5,
        min_timestamp=500.0,
        max_timestamp=3500.0
    )

    # Ambiguity must be rejected fail-closed
    assert recon["is_exact"] is False
    assert recon["net_pnl"] is None


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_small_position_large_relative_error():
    """Verifies that small positions (e.g. 0.00005 BTC) strictly reject 100% relative errors
    (found 0.00010 BTC), eliminating the old loose absolute tolerance flaw."""
    service = get_mock_service()
    import time

    fill = MagicMock()
    fill.base_filled = -0.00010 * 1e18  # Found 0.00010 BTC
    fill.quote_filled = 10.0 * 1e18
    fill.fee_x18 = 0.005 * 1e18
    fill.timestamp = int(time.time() * 1000)
    fill.digest = "0xtiny_overshoot"

    mock_hist = MagicMock()
    mock_hist.orders = [fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=100000.0,
        expected_contracts=0.00005,  # Expected 0.00005 BTC
        close_timestamp=time.time()
    )

    # Diff is 0.00005 (which was <= 1e-4), but divergence is 100% -> MUST be rejected!
    assert recon["is_exact"] is False
    assert recon["is_volume_exact"] is False


@pytest.mark.asyncio
async def test_reconcile_closing_fills_rejects_fill_without_unique_digest():
    """Verifies that an execution without a unique non-empty digest cannot be verified as exact,
    preventing untracked fills and replay vulnerability."""
    service = get_mock_service()
    import time

    fill = MagicMock()
    fill.base_filled = -1.0 * 1e18
    fill.quote_filled = 100000.0 * 1e18
    fill.fee_x18 = 50.0 * 1e18
    fill.timestamp = int(time.time() * 1000)
    fill.digest = None  # Missing digest!

    mock_hist = MagicMock()
    mock_hist.orders = [fill]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=2,
        direction="LONG",
        entry_price=95000.0,
        expected_contracts=1.0,
        close_timestamp=time.time()
    )

    assert recon["is_exact"] is False
    assert recon["has_missing_digest"] is True


def test_processed_fills_registry_lru_ordered_retention():
    """Verifies that processed_fills_registry strictly maintains chronological FIFO/LRU order,
    pruning oldest records when exceeding 3000 items and guaranteeing newest records are preserved."""
    service = get_mock_service()

    # Insert 3100 records with sequential timestamps 1..3100
    for i in range(1, 3101):
        dig = f"0xdigest_{i:04d}"
        service.processed_fills_registry[dig] = {
            "digest": dig,
            "timestamp": float(i),
            "subaccount": "0x123",
            "product_id": 2
        }

    assert len(service.processed_fills_registry) == 3100

    # Save and prune
    with patch("core.state_store.StateStore.save") as mock_save:
        service._save_processed_fills()

    # Preserved exactly 3000
    assert len(service.processed_fills_registry) == 3000
    assert len(service.processed_fill_digests) == 3000

    # Oldest 100 (1..100) are pruned
    assert "0xdigest_0001" not in service.processed_fill_digests
    assert "0xdigest_0100" not in service.processed_fill_digests

    # Newest (101..3100) are strictly preserved
    assert "0xdigest_0101" in service.processed_fill_digests
    assert "0xdigest_3100" in service.processed_fill_digests


@pytest.mark.asyncio
async def test_reconcile_pending_concurrency_lock_serializes():
    """Verifies that concurrent invocations of reconcile_pending_trade_fills safely
    serialize through _reconcile_lock without race conditions."""
    service = get_mock_service()
    service._reconcile_lock = asyncio.Lock()

    service.pending_reconciliations["BTC-USD"] = {
        "symbol": "BTC-USD",
        "product_id": 2,
        "subaccount": service.default_subaccount_id,
        "direction": "LONG",
        "entry_price": 60000.0,
        "contracts": 1.0,
        "attempts": 0,
        "status": "PNL_RECONCILIATION_PENDING"
    }

    mock_recon_res = {
        "is_exact": True,
        "exit_price": 61000.0,
        "total_base": 1.0,
        "total_quote": 61000.0,
        "fee": 30.5,
        "net_pnl": 969.5,
        "digests": ["0xconcurrent_fill"]
    }

    call_count = 0
    async def mock_reconcile(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return mock_recon_res

    with patch.object(service, "_reconcile_closing_fills_from_history", side_effect=mock_reconcile):
        # Fire two concurrent tasks
        res1, res2 = await asyncio.gather(
            service.reconcile_pending_trade_fills(),
            service.reconcile_pending_trade_fills()
        )

    # First task resolves the trade, second task sees empty pending list
    total_resolved = len(res1) + len(res2)
    assert total_resolved == 1
    assert "BTC-USD" not in service.pending_reconciliations



