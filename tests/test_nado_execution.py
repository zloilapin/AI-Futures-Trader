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
    service.product_map = {"BTC": 2, "BTC-USD": 2, "ETH": 4, "ETH-USD": 4}
    service.active_positions = {}
    service._position_locks = {}
    service._market_cache = {}
    service.win_count = 0
    service.loss_count = 0
    service.recent_streak = []
    service.cooldown_until = 0.0
    service._last_cooldown_processed_len = 0
    service._save_positions = MagicMock()
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

    # Mock historical orders returned from indexer
    mock_order = MagicMock()
    mock_order.base_filled = "-500000000000000000"  # -0.5 base (closing LONG)
    mock_order.quote_filled = "35000"  # $35,000 -> exit price 70,000.0
    mock_order.timestamp = str(int(time.time() * 1000))
    mock_order.fee = "200000000000000000"  # 0.20 USD fee

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
    it recognizes the position is already closed, cleans up local state, and returns success."""
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

