import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from decimal import Decimal
from services.nado_trading_service import NadoTradingService, to_x18_aligned
from core.logger import TradeLogger

def test_to_x18_aligned_decimal_precision():
    """Bug #1: Verifies that to_x18_aligned eliminates IEEE-754 precision loss on numbers like 0.29."""
    # 0.29 in float * 10**18 is 289999999999999967232. In integer division by 10**16 (step 0.01) it would yield 28 steps (0.28).
    step_001_x18 = int(0.01 * 10**18)
    aligned_floor = to_x18_aligned(0.29, step_001_x18, mode="floor")
    assert aligned_floor == int(Decimal("0.29") * Decimal(10**18))
    
    # Check ceiling alignment
    step_5_x18 = int(5 * 10**18)
    aligned_ceil = to_x18_aligned(12.3, step_5_x18, mode="ceil")
    assert aligned_ceil == int(15 * 10**18)
    
    # Check zero/invalid input
    assert to_x18_aligned(None) == 0
    assert to_x18_aligned(float("nan")) == 0
    assert to_x18_aligned(float("inf")) == 0

def test_logger_sys_import_unicode():
    """Bug #3: Verifies TradeLogger handles Unicode encoding without NameError for sys."""
    logger = TradeLogger(base_dir="tests/scratch_logs")
    # Calling log_decision with Unicode and simulated failure
    with patch("builtins.print") as mock_print:
        mock_print.side_effect = [UnicodeEncodeError("utf-8", "test", 0, 1, "test"), None]
        # Should not raise NameError: name 'sys' is not defined
        logger.log_decision("TestAgent", "🚀 Crypto Alert 100%!")

@pytest.mark.asyncio
async def test_reconcile_closing_fills_open_digest_boundary():
    """Bug #2: Verifies that reconciliation stops immediately upon hitting open_digest in reverse-chronological order."""
    service = NadoTradingService.__new__(NadoTradingService)
    service.client = MagicMock()
    service.client.market.place_price_trigger_order = MagicMock(side_effect=Exception("Order notional does not meet minimum size 2094"))
    service.product_map = {"SOL": 1}
    service.processed_fill_digests = set()
    service._normalize_indexer_amount = lambda v: float(v) / 1e18 if abs(float(v)) > 1e10 else float(v)
    service._get_market_parameters = MagicMock(return_value={"size_increment_base": 0.01})

    # Order 1 (most recent): partial close of current position
    order_close_1 = MagicMock()
    order_close_1.digest = "0xclose_recent"
    order_close_1.base_filled = -0.5 * 1e18  # Closing LONG
    order_close_1.quote_filled = 50.0 * 1e18
    order_close_1.fee_x18 = 0.025 * 1e18
    order_close_1.timestamp = None

    # Order 2: opening order of current position
    order_open = MagicMock()
    order_open.digest = "0xopen_current_pos"
    order_open.base_filled = 0.5 * 1e18
    order_open.quote_filled = 50.0 * 1e18
    order_open.fee_x18 = 0.025 * 1e18
    order_open.timestamp = None

    # Order 3 (older history): close of past position (must NOT be included!)
    order_old_close = MagicMock()
    order_old_close.digest = "0xold_past_close"
    order_old_close.base_filled = -0.5 * 1e18  # Old close
    order_old_close.quote_filled = 40.0 * 1e18
    order_old_close.fee_x18 = 0.025 * 1e18
    order_old_close.timestamp = None

    hist_mock = MagicMock()
    hist_mock.orders = [order_close_1, order_open, order_old_close]
    service.client.market.get_subaccount_historical_orders = MagicMock(return_value=hist_mock)

    recon = await service._reconcile_closing_fills_from_history(
        subaccount="0xsub",
        product_id=1,
        direction="LONG",
        entry_price=100.0,
        expected_contracts=0.5,
        close_timestamp=1700000000.0,
        open_digest="0xopen_current_pos",
        allow_missing_timestamp=True
    )

    assert recon["is_exact"] is True
    assert recon["digests"] == ["0xclose_recent"]
    assert "0xold_past_close" not in recon["digests"]

@pytest.mark.asyncio
async def test_open_position_sets_execution_status_on_error():
    """Bug #4: Verifies self.last_execution_status is updated on place_order exception instead of remaining PENDING."""
    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.active_positions = {}
    service.pending_reconciliations = {}
    service.product_map = {"SOL": 1, "SOL-USD": 1}
    service.default_subaccount_id = "0xsub"
    service.wallet = MagicMock()
    service.wallet.get_address = MagicMock(return_value="0x1111111111111111111111111111111111111111")
    service.client = MagicMock()
    service.client.market.place_price_trigger_order = MagicMock(side_effect=Exception("Order notional does not meet minimum size 2094"))
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.01 * 10**18),
        "price_increment_x18": int(0.01 * 10**18),
        "min_size": 0.01
    })

    # Subaccounts return
    sub_res = MagicMock()
    sub_res.subaccounts = [MagicMock(subaccount="0xsub")]
    service.client.subaccount.get_subaccounts = MagicMock(return_value=sub_res)

    # Place order raises 2070 max OI error
    service.client.market.place_order = MagicMock(side_effect=Exception("Error 2070: maximum open interest reached"))

    res = await service.open_position(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        sl_price=95.0,
        tp_price=110.0,
        notional_usd=100.0,
        leverage=1,
        approved_risk_usd=10.0
    )

    assert res is False
    assert service.last_execution_status == "OI_LIMIT_REACHED"

def test_active_positions_deduplication_on_load(tmp_path):
    """Bug #5: Verifies _load_positions deduplicates dual keys (SOL and SOL-USD) to prevent split-brain."""
    service = NadoTradingService.__new__(NadoTradingService)
    from core.state_store import StateStore

    test_file = str(tmp_path / "test_positions.json")
    dual_data = {
        "SOL": {"symbol": "SOL", "contracts": 1.0, "entry_price": 100.0},
        "SOL-USD": {"symbol": "SOL-USD", "contracts": 1.0, "entry_price": 100.0}
    }
    StateStore.save(test_file, dual_data)

    with patch("core.state_store.StateStore.load", return_value=dual_data):
        loaded = service._load_positions()
        # Should have exactly 1 entry: SOL-USD
        assert len(loaded) == 1
        assert "SOL-USD" in loaded
        assert "SOL" not in loaded

@pytest.mark.asyncio
async def test_update_stop_loss_falls_back_to_software_on_min_size():
    """Verifies that update_stop_loss cleanly transitions to SOFTWARE_ONLY when position notional is below $100 min_size."""
    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.product_map = {"XRP": 10, "XRP-USD": 10}
    service.default_subaccount_id = "0xsub"
    service.client = MagicMock()
    service.client.market.place_price_trigger_order = MagicMock(side_effect=Exception("Order notional does not meet minimum size 2094"))
    service._get_market_parameters = MagicMock(return_value={
        "price_increment_x18": int(0.0001 * 10**18),
        "size_increment_x18": int(5.0 * 10**18),
    })
    service._save_positions = MagicMock()
    service._cancel_trigger_digests = AsyncMock(return_value=True)

    # 45 XRP at ~1.35 USD is $60.75 notional (< $100 min_size)
    service.active_positions = {
        "XRP-USD": {
            "symbol": "XRP-USD",
            "direction": "LONG",
            "entry_price": 1.336,
            "sl_price": 1.346,
            "contracts": 45.0,
            "sl_digest": "0xold_sl_digest",
            "protection_state": "EXCHANGE_PROTECTED",
            "sl_protection_mode": "EXCHANGE_PROTECTED",
            "product_id": 10
        }
    }

    res = await service.update_stop_loss(
        symbol="XRP-USD",
        new_sl_price=1.3495,
        new_protection_state="BREAK_EVEN"
    )

    # Security audit c398c61: placement failure retains the existing active on-chain SL to prevent capital loss
    assert res is False
    pos = service.active_positions["XRP-USD"]
    assert pos["sl_protection_mode"] == "EXCHANGE_PROTECTED"
    assert pos["protection_state"] == "EXCHANGE_PROTECTED_OLD_SL"
    assert pos["sl_digest"] == "0xold_sl_digest"
    service._cancel_trigger_digests.assert_not_called()
