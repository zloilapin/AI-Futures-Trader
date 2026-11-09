import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from services.nado_trading_service import NadoTradingService, PartialCloseResult
from agents.sentinel_agent import SentinelAgent

@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger

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
                size_increment=int(0.01 * 1e18),
                price_increment_x18=int(0.001 * 1e18),
                min_size=int(0.1 * 1e18)
            )
        )
    }

    async def mock_fetch_triggers(product_id: int):
        place_mock = service.client.market.place_price_trigger_order
        ret = getattr(place_mock, "return_value", None)
        dig = getattr(getattr(ret, "data", None), "digest", None)
        if dig:
            pos = service.active_positions.get("SOL-USD", {})
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
async def test_update_stop_loss_no_premature_cancellation(mock_service):
    """
    Audit Fix: Positions with partial slices (e.g. $80 notional) must NOT have
    their old SL cancelled preemptively before placement, and must confirm on-chain.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 104.0,
        "size_usd": 80.0,
        "contracts": 0.8,
        "product_id": 8,
        "sl_digest": "0xold_sl_digest",
        "sl_protection_mode": "EXCHANGE_PROTECTED",
        "trigger_amount_x18": str(int(-0.8 * 1e18))
    }

    mock_cancel = AsyncMock(return_value=True)
    mock_service._cancel_trigger_digests = mock_cancel

    mock_res = MagicMock()
    mock_res.data.digest = "0xnew_confirmed_sl"
    mock_service.client.market.place_price_trigger_order.return_value = mock_res

    ok = await mock_service.update_stop_loss("SOL-USD", 100.10, new_protection_state="BREAK_EVEN", new_contracts=0.8)
    assert ok is True

    pos = mock_service.active_positions["SOL-USD"]
    assert pos["sl_digest"] == "0xnew_confirmed_sl"
    assert pos["sl_protection_mode"] == "EXCHANGE_PROTECTED"
    assert pos["protection_state"] == "BREAK_EVEN"
    assert pos["sl_price"] == pytest.approx(100.10)
    mock_cancel.assert_awaited_once_with(8, ["0xold_sl_digest"], sender=None)

@pytest.mark.asyncio
async def test_sentinel_monotonic_sl_prevents_regression(mock_logger):
    """
    Audit Fix: Sentinel must enforce monotonic SL invariant (never allow worse SL).
    """
    sentinel = SentinelAgent(mock_logger)
    pos_long = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 102.0,  # Already trailed in profit
        "highest_price": 105.0,
        "protection_state": "TRAILING"
    }
    # Market drops back to 101.0
    market_data = {"price_data": {"current_price": 101.0}}
    res = await sentinel.analyze(pos_long, market_data, profile_rules={}, atr_value=1.5)
    # Must NOT regress the stop loss below 102.0
    assert res["new_sl"] is None or res["new_sl"] >= 102.0

@pytest.mark.asyncio
async def test_sentinel_timestop_decay_ladder(mock_logger):
    """
    Audit Fix: Verify Sentinel time-stop decay tiers:
    180 min -> BE on modest profit
    240 min -> tighten to -0.50R
    360 min -> tighten to -0.25R
    """
    import time
    now = time.time()
    sentinel = SentinelAgent(mock_logger)

    # Case 1: 190 min (>= 180 min) with modest profit (+0.20R) -> TIME_STOP_BE
    pos_180 = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,  # 1R = 2.0
        "initial_sl": 98.0,
        "open_time": now - (190 * 60),
        "protection_state": "PROTECTED"
    }
    mdata_profit = {"price_data": {"current_price": 100.40}}  # +0.40 = +0.20R
    res_180 = await sentinel.analyze(pos_180, mdata_profit, profile_rules={}, atr_value=2.0)
    assert res_180["state"] == "TIME_STOP_BE"
    assert res_180["new_sl"] >= 100.0

    # Case 2: 250 min (>= 240 min) with no impulse -> tighten SL to -0.50R (99.0)
    pos_240 = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "open_time": now - (250 * 60),
        "protection_state": "PROTECTED"
    }
    mdata_flat = {"price_data": {"current_price": 100.05}}
    res_240 = await sentinel.analyze(pos_240, mdata_flat, profile_rules={}, atr_value=2.0)
    assert res_240["state"] == "TIME_STOP_TIGHTEN"
    assert res_240["new_sl"] == pytest.approx(99.0, abs=0.05)

    # Case 3: 370 min (>= 360 min) -> tighten SL to -0.25R (99.50)
    pos_360 = {
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "open_time": now - (370 * 60),
        "protection_state": "PROTECTED"
    }
    res_360 = await sentinel.analyze(pos_360, mdata_flat, profile_rules={}, atr_value=2.0)
    assert res_360["state"] == "TIME_STOP_MAX_HORIZON"
    assert res_360["new_sl"] == pytest.approx(99.50, abs=0.05)
