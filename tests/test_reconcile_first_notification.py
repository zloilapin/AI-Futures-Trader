import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch
from services.nado_trading_service import NadoTradingService
from agents.telegram_agent import TelegramAgent
from core.logger import TradeLogger

def get_test_service():
    service = NadoTradingService.__new__(NadoTradingService)
    service.logger = TradeLogger("TestNado")
    service.is_connected = True
    service.active_positions = {}
    service.pending_reconciliations = {}
    service.processed_fill_digests = set()
    service._position_locks = {}
    service.default_subaccount_id = "0x12d1293cbd98eea18a2e5906df5ed3bef07fb1aa64656661756c740000000000"
    service.product_map = {"BTC": 2, "BTC-USD": 2, "ETH": 4, "ETH-USD": 4, "ZEC": 18, "ZEC-USD": 18}
    service.client = MagicMock()
    service._mock_mode = True
    service._save_positions = MagicMock()
    service._save_pending_reconciliations = MagicMock()
    service._save_state = MagicMock()
    service.win_count = 0
    service.loss_count = 0
    service.recent_streak = []
    service.cooldown_until = 0
    return service

def test_telegram_agent_format_position_closed_message_exact():
    logger = TradeLogger("TestTG")
    agent = TelegramAgent(logger, None)

    close_data = {
        "symbol": "ZEC-USD",
        "direction": "LONG",
        "entry_price": 1228.7300,
        "exit_price": 1228.9100,
        "closed_qty": 0.02,
        "gross_pnl_usd": 0.0140,
        "entry_fee_usd": 0.0255,
        "exit_fee_usd": 0.0255,
        "total_fee_usd": 0.0510,
        "net_pnl_usd": -0.0370,
        "realized_r": -0.18,
        "pnl_source": "EXACT",
        "close_reason": "TIME_DECAY_EXIT",
    }

    msg = agent.format_position_closed_message(close_data)
    
    assert "ПОЗИЦИЯ ЗАКРЫТА: ZEC-USD (LONG)" in msg
    assert "Вход → Выход:* `$1228.7300` → `$1228.9100`" in msg
    assert "PnL по цене (Gross):* `+0.0140$`" in msg
    assert "Комиссия биржи:* `-0.0510$` *(вход: $0.0255 | выход: $0.0255)*" in msg
    assert "Чистый PnL (Net):* *`-0.0370$ (-0.18R)`*" in msg
    assert "Причина:* `TIME_DECAY_EXIT`" in msg
    assert "Источник данных:* ✅ Биржа (EXACT)" in msg
    assert "⚪" in msg  # Scratch/BE range: -0.05 to +0.05

def test_telegram_agent_format_position_closed_message_win():
    logger = TradeLogger("TestTG")
    agent = TelegramAgent(logger, None)

    close_data = {
        "symbol": "SOL-USD",
        "direction": "SHORT",
        "entry_price": 100.0,
        "exit_price": 95.0,
        "closed_qty": 1.0,
        "gross_pnl_usd": 5.0,
        "entry_fee_usd": 0.05,
        "exit_fee_usd": 0.05,
        "total_fee_usd": 0.10,
        "net_pnl_usd": 4.90,
        "realized_r": 2.45,
        "pnl_source": "EXACT",
        "close_reason": "TP_FINAL",
        "accumulated_slices_pnl": 2.10
    }

    msg = agent.format_position_closed_message(close_data)
    
    assert "🟢" in msg
    assert "SOL-USD (SHORT)" in msg
    assert "PnL по цене (Gross):* `+5.0000$`" in msg
    assert "Ступени TP:* `+2.1000$`" in msg
    assert "Чистый PnL (Net):* *`+4.9000$ (+2.45R)`*" in msg
    assert "✅ Биржа (EXACT)" in msg

def test_telegram_agent_format_position_closed_message_estimated():
    logger = TradeLogger("TestTG")
    agent = TelegramAgent(logger, None)

    close_data = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 60000.0,
        "exit_price": 58000.0,
        "closed_qty": 0.1,
        "gross_pnl_usd": -200.0,
        "entry_fee_usd": 3.0,
        "exit_fee_usd": 2.9,
        "total_fee_usd": 5.9,
        "net_pnl_usd": -205.9,
        "realized_r": -1.03,
        "pnl_source": "ESTIMATED",
        "close_reason": "FAST_MONITOR_STOP LOSS"
    }

    msg = agent.format_position_closed_message(close_data)
    
    assert "🔴" in msg
    assert "BTC-USD (LONG)" in msg
    assert "PnL по цене (Gross):* `-200.0000$`" in msg
    assert "Чистый PnL (Net):* *`-205.9000$ (-1.03R)`*" in msg
    assert "Источник данных:* ⏳ Оценка (ESTIMATED)" in msg

@pytest.mark.asyncio
async def test_fetch_verified_close_metrics_reconciles_fills():
    service = get_test_service()
    pos_data = {
        "symbol": "ZEC-USD",
        "direction": "LONG",
        "entry_price": 1200.0,
        "contracts": 0.02,
        "size_usd": 24.0,
        "planned_risk_usd": 0.30,
        "entry_fee_usd": 0.0120,
        "sender": service.default_subaccount_id,
        "product_id": 18
    }

    mock_recon = {
        "is_exact": True,
        "exit_price": 1250.0,
        "total_base": 0.02,
        "fee": 0.0125,
        "digests": ["0xdigest123"]
    }

    with patch.object(service, "_reconcile_closing_fills_from_history", new_callable=AsyncMock) as mock_recon_call, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_recon_call.return_value = mock_recon
        
        metrics = await service._fetch_verified_close_metrics("ZEC-USD", pos_data)

        assert metrics["pnl_source"] == "EXACT"
        assert metrics["is_exact"] is True
        assert metrics["exit_price"] == 1250.0
        assert metrics["closed_qty"] == 0.02
        # Gross: (1250 - 1200) * 0.02 = 1.00 USD
        assert metrics["gross_pnl_usd"] == pytest.approx(1.00, rel=1e-3)
        assert metrics["entry_fee_usd"] == pytest.approx(0.0120, rel=1e-3)
        assert metrics["exit_fee_usd"] == pytest.approx(0.0125, rel=1e-3)
        assert metrics["total_fee_usd"] == pytest.approx(0.0245, rel=1e-3)
        # Net: 1.00 - 0.0245 = 0.9755 USD
        assert metrics["net_pnl_usd"] == pytest.approx(0.9755, rel=1e-3)
        # R: 0.9755 / 0.30 = 3.25 R
        assert metrics["realized_r"] == pytest.approx(3.25, rel=1e-2)
        assert "0xdigest123" in service.processed_fill_digests

@pytest.mark.asyncio
async def test_fetch_verified_close_metrics_fallback_on_lag():
    service = get_test_service()
    pos_data = {
        "symbol": "ETH-USD",
        "direction": "SHORT",
        "entry_price": 2000.0,
        "assumed_exit_price": 1950.0,
        "contracts": 0.1,
        "size_usd": 200.0,
        "planned_risk_usd": 2.0,
        "entry_fee_usd": 0.10,
        "sender": service.default_subaccount_id,
        "product_id": 4,
        "close_reason": "STOP_LOSS"
    }

    # Indexer returns empty (indexer lag) across all 3 retries
    empty_recon = {"is_exact": False, "exit_price": None}

    with patch.object(service, "_reconcile_closing_fills_from_history", new_callable=AsyncMock) as mock_recon_call, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_recon_call.return_value = empty_recon
        
        metrics = await service._fetch_verified_close_metrics("ETH-USD", pos_data)

        # 3 retry attempts executed with progressive delays
        assert mock_recon_call.call_count == 3
        mock_sleep.assert_any_call(1.5)
        mock_sleep.assert_any_call(2.0)
        mock_sleep.assert_any_call(2.5)

        assert metrics["pnl_source"] == "ESTIMATED"
        assert metrics["is_exact"] is False
        assert metrics["exit_price"] == 1950.0
        # Gross: SHORT (2000 - 1950) * 0.1 = 5.0 USD
        assert metrics["gross_pnl_usd"] == pytest.approx(5.00, rel=1e-3)
        assert metrics["entry_fee_usd"] == pytest.approx(0.10, rel=1e-3)
        # Exit fee: 1950 * 0.1 * 0.0005 = 0.0975 USD
        assert metrics["exit_fee_usd"] == pytest.approx(0.0975, rel=1e-3)
        assert metrics["total_fee_usd"] == pytest.approx(0.1975, rel=1e-3)
        # Net: 5.0 - 0.1975 = 4.8025 USD
        assert metrics["net_pnl_usd"] == pytest.approx(4.8025, rel=1e-3)
        assert metrics["close_reason"] == "STOP_LOSS"


@pytest.mark.asyncio
async def test_live_nado_closing_fill_fee_reconciliation():
    """
    Verifies that on live Nado DEX (where quote_filled includes fee deduction/addition):
    - A BUY order closing a SHORT with quote_filled = -20.001412 and fee = 0.035012
      correctly reconstructs gross quote 19.9664 and exact execution price 99.8320 (not 100.0071).
    - Entry price 100.0790 -> Exit price 99.8320 yields positive gross PnL +$0.0494.
    - Double-fee deduction is completely eliminated.
    """
    service = get_test_service()
    service._mock_mode = False  # Simulate live exchange mode

    # Real order from Nado Indexer for SOL SHORT close
    closing_order = MagicMock()
    closing_order.base_filled = 0.2 * 1e18  # BUY 0.2 SOL
    closing_order.quote_filled = -20.00141225428900115 * 1e18  # Net quote spent
    closing_order.fee = 0.03501225428900115 * 1e18
    closing_order.fee_x18 = None
    closing_order.timestamp = 1793089749
    closing_order.digest = "0xe57e4ad25310def9edbead4e151d83de20a9969e87e61e15b6796d253908d742"
    closing_order.submission_idx = "95869136"

    mock_hist = MagicMock()
    mock_hist.orders = [closing_order]
    service.client.market.get_subaccount_historical_orders.return_value = mock_hist

    # Also mock get_matches returning the exact maker price
    mock_tx = MagicMock()
    mock_tx.submission_idx = "95869136"
    mock_tx.timestamp = 1793089749
    mock_tx.tx = {
        "match_orders": {
            "maker": {
                "order": {
                    "price_x18": str(int(99.8320 * 1e18))
                }
            }
        }
    }
    mock_matches_res = MagicMock()
    mock_matches_res.txs = [mock_tx]
    mock_matches_res.matches = []
    service.client.context = MagicMock()
    service.client.context.indexer_client.get_matches.return_value = mock_matches_res

    recon = await service._reconcile_closing_fills_from_history(
        subaccount=service.default_subaccount_id,
        product_id=8,
        direction="SHORT",
        entry_price=100.0790,
        expected_contracts=0.2,
        close_timestamp=1793089749
    )

    assert recon["is_exact"] is True
    assert recon["total_base"] == pytest.approx(0.2, rel=1e-4)
    # Execution price must be exactly 99.8320, NEVER the slippage/net price 100.0071
    assert recon["exit_price"] == pytest.approx(99.8320, rel=1e-4)
    assert recon["fee"] == pytest.approx(0.0350, rel=1e-3)
    # Gross PnL: 0.2 * (100.0790 - 99.8320) = +0.0494 USD
    gross_pnl = recon["total_base"] * (100.0790 - recon["exit_price"])
    assert gross_pnl == pytest.approx(0.0494, rel=1e-3)
    # Net PnL: 0.0494 - 0.0350 = +0.0144 USD (or net positive before entry fee)
    assert recon["net_pnl"] == pytest.approx(0.0144, rel=1e-3)


@pytest.mark.asyncio
async def test_reconcile_pending_trade_fills_prunes_zero_contracts_gracefully():
    """
    Verifies that unresolvable pending reconciliations with contracts=0.0 and entry_price=0.0
    are cleanly pruned without calling _reconcile_closing_fills_from_history or spamming warnings.
    """
    service = get_test_service()
    service.pending_reconciliations = {
        "GHOST-USD": {
            "symbol": "GHOST-USD",
            "base_symbol": "GHOST",
            "product_id": 99,
            "subaccount": service.default_subaccount_id,
            "direction": "LONG",
            "entry_price": 0.0,
            "contracts": 0.0,
            "size_usd": 0.0,
            "status": "PNL_RECONCILIATION_PENDING",
            "reason": "ERROR_2002_FILL_PENDING"
        }
    }

    with patch.object(service, "_reconcile_closing_fills_from_history", new_callable=AsyncMock) as mock_recon:
        await service.reconcile_pending_trade_fills()
        # Must NOT call history query for zero volume
        assert mock_recon.call_count == 0
        # Ghost record must be pruned
        assert "GHOST-USD" not in service.pending_reconciliations
        assert service._save_pending_reconciliations.called


@pytest.mark.asyncio
async def test_reconcile_pending_trade_fills_recovers_contracts_from_size_usd():
    """
    Verifies that if contracts is 0.0 but size_usd and entry_price are valid,
    expected_contracts is recovered as size_usd / entry_price and reconciliation proceeds.
    """
    service = get_test_service()
    service.pending_reconciliations = {
        "RECOVER-USD": {
            "symbol": "RECOVER-USD",
            "base_symbol": "RECOVER",
            "product_id": 99,
            "subaccount": service.default_subaccount_id,
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 0.0,
            "size_usd": 50.0,  # 0.5 contracts
            "accumulated_realized_pnl": 0.0,
            "status": "PNL_RECONCILIATION_PENDING"
        }
    }

    mock_recon_result = {
        "is_exact": True,
        "exit_price": 110.0,
        "net_pnl": 5.0,
        "fee": 0.05,
        "digests": ["0x123"]
    }

    with patch.object(service, "_reconcile_closing_fills_from_history", new_callable=AsyncMock) as mock_recon:
        mock_recon.return_value = mock_recon_result
        await service.reconcile_pending_trade_fills()
        assert mock_recon.call_count == 1
        assert mock_recon.call_args.kwargs["expected_contracts"] == pytest.approx(0.5, rel=1e-4)
        assert "RECOVER-USD" not in service.pending_reconciliations


