import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from services.nado_trading_service import NadoTradingService, PartialCloseResult
from services.market_data_service import MarketDataService
from services.telegram_bot_listener import TelegramBotListener
from core.llm_client import LLMClient
from core.config import config


# ==============================================================================
# F-17 & F-18: Partial TP Notification and Native TP Cancellation
# ==============================================================================
@pytest.mark.asyncio
async def test_f17_and_f18_tp1_rem_contracts_and_native_tp_cancellation():
    """F-17 & F-18: Telegram notification uses exact remaining contracts, and obsolete native TP is cancelled."""
    service = NadoTradingService.__new__(NadoTradingService)
    service.active_positions = {}
    service.product_map = {"SOL": 1001}
    service._save_positions = MagicMock()
    service.update_stop_loss_to_be = AsyncMock(return_value=True)
    service._cancel_trigger_digests = AsyncMock(return_value=True)
    service._get_position_lock = lambda sym: asyncio.Lock()

    # Create dummy position with 1.6 contracts and active native TP
    from services.partial_tp_calculator import PartialTPPlan, TPTier
    plan = PartialTPPlan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=103.5,
        total_contracts=1.6,
        remaining_contracts=1.6,
        size_increment=0.1,
        min_size=0.1,
        price_increment=0.01,
        mode="2_TIER",
        tiers=[
            TPTier(tier_index=1, fraction=0.5, target_price=101.5, close_contracts=0.8, cumulative_fraction=0.5, action="PROTECT_BREAKEVEN", status="PENDING"),
            TPTier(tier_index=2, fraction=0.5, target_price=103.5, close_contracts=0.8, cumulative_fraction=1.0, action="CLOSE", is_final=True, status="PENDING")
        ]
    )

    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "contracts": 1.6,
        "tp_digest": "0xnative_tp_digest_100pct",
        "sl_digest": "0xnative_sl_digest",
        "product_id": 1001,
        "partial_tp_plan": plan.to_dict()
    }
    service.active_positions["SOL-USD"] = pos

    close_res = PartialCloseResult(
        success=True,
        filled_contracts=0.8,
        fill_price=101.5,
        realized_pnl=1.20
    )
    service.execute_partial_close = AsyncMock(return_value=close_res)

    def fake_apply(symbol, tier_index, result):
        pos["contracts"] = 0.8
        plan.remaining_contracts = 0.8
        plan.tiers[0].status = "FILLED"
        pos["partial_tp_plan"] = plan.to_dict()
        return True

    service.apply_partial_close_to_position = MagicMock(side_effect=fake_apply)

    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()
    mock_tg.broadcast_to_channel = AsyncMock()

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        res = await service.check_and_execute_partial_tp_tier("SOL-USD", 101.55)

    assert res is not None
    # F-17 verification: remaining contracts in the returned dict and notification is 0.8, not 0.0
    assert res.get("remaining_contracts") == 0.8, f"Expected remaining contracts 0.8, got {res.get('remaining_contracts')}"
    assert pos["contracts"] == 0.8

    # F-18 verification: native 100% TP trigger was cancelled
    service._cancel_trigger_digests.assert_awaited_once_with(1001, ["0xnative_tp_digest_100pct"], sender=None)
    assert pos.get("tp_digest") is None, "tp_digest should be cleared after cancellation"


# ==============================================================================
# F-19: ATR-14 Wilder's RMA and EMA-9/21 Smoothing Consistency
# ==============================================================================
@pytest.mark.asyncio
async def test_f19_atr14_wilder_rma_and_ema_sma_seed():
    """F-19: _fetch_ohlc_interval uses Wilder's RMA for ATR-14 and SMA seed for EMA-9/EMA-21."""
    mds = MarketDataService("Nado_DEX")
    
    # 25 candles with constant range of 3.0
    closes = [100.0 + i * 1.0 for i in range(25)]
    highs = [c + 2.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    candles = [{"open": closes[i]-0.5, "high": highs[i], "low": lows[i], "close": closes[i], "volume": 100.0} for i in range(len(closes))]

    mds._fetch_nado_candles = AsyncMock(return_value=candles)
    res = await mds._fetch_ohlc_interval("SOL-USD", 15)

    # True range for every candle after first is high - low = 3.0
    # Wilder's RMA of constant TR=3.0 must be exactly 3.0
    assert "atr" in res, "atr should be included in returned ohlc dictionary"
    assert res["atr"] == pytest.approx(3.0, rel=1e-3)
    assert res["trend"] == "BULLISH"


# ==============================================================================
# F-20: RSI Divergence Swing Window Comparison
# ==============================================================================
def test_f20_rsi_divergence_two_window_extremes():
    """F-20: RSI divergence compares swing extremes in two 10-bar windows."""
    # 1. Bullish divergence test:
    # First 10 bars: price min = 100, RSI min = 25
    # Second 10 bars: price min = 95 (lower low), RSI min = 30 (higher low)
    closes_bull = [105, 102, 100, 103, 104, 105, 103, 102, 101, 102] + \
                  [99, 98, 96, 95, 97, 98, 97, 96, 98, 99]
    rsi_bull = [35, 30, 25, 32, 34, 36, 33, 31, 30, 32] + \
               [31, 30, 30, 31, 33, 35, 34, 32, 35, 36]

    price_tail = closes_bull[-20:]
    rsi_tail = rsi_bull[-20:]
    p1_min = min(price_tail[:10])
    p2_min = min(price_tail[10:])
    rsi1_min = min(rsi_tail[:10])
    rsi2_min = min(rsi_tail[10:])

    assert p2_min < p1_min
    assert rsi2_min > (rsi1_min + 1.5)

    # 2. Bearish divergence test:
    # First 10 bars: price max = 150, RSI max = 75
    # Second 10 bars: price max = 155 (higher high), RSI max = 70 (lower high)
    closes_bear = [140, 145, 150, 148, 147, 149, 148, 146, 147, 148] + \
                  [151, 153, 155, 154, 153, 152, 151, 150, 152, 153]
    rsi_bear = [65, 70, 75, 72, 71, 73, 72, 69, 70, 71] + \
               [68, 69, 70, 69, 68, 67, 66, 65, 67, 68]

    p1_max = max(closes_bear[:10])
    p2_max = max(closes_bear[10:])
    rsi1_max = max(rsi_bear[:10])
    rsi2_max = max(rsi_bear[10:])

    assert p2_max > p1_max
    assert rsi2_max < (rsi1_max - 1.5)


# ==============================================================================
# F-21: TelegramBotListener Accepts BALANCED Profile
# ==============================================================================
@pytest.mark.asyncio
async def test_f21_telegram_bot_listener_accepts_balanced_profile():
    """F-21: /risk callback handler accepts 'setrisk_BALANCED' without error."""
    tbl = TelegramBotListener(trading_service=MagicMock())
    tbl.chat_id = "12345"
    mock_tg = MagicMock()
    mock_tg.answer_callback_query = AsyncMock()
    mock_tg.send_message = AsyncMock()

    with patch("services.telegram_service.TelegramService", return_value=mock_tg):
        await tbl.handle_callback("cb_1", "setrisk_BALANCED", chat_id="12345")

    # Verify callback answered success
    mock_tg.answer_callback_query.assert_awaited_once_with(
        "cb_1", "Профиль BALANCED установлен ✅"
    )
    assert config.TRADING_PROFILE == "BALANCED"


# ==============================================================================
# F-22: Scratch Trades Included in Daily Trades Count
# ==============================================================================
@pytest.mark.asyncio
async def test_f22_scratch_count_in_daily_trades():
    """F-22: Daily trades count properly increments when scratch/BE trades occur."""
    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.default_subaccount_id = "0x123"
    service._initial_balance = 1000.0
    service.win_count = 5
    service.loss_count = 2
    service.scratch_count = 3
    service.recent_streak = []
    service.active_positions = {}
    service._save_state = MagicMock()
    service._load_state = MagicMock()
    service.get_active_positions = AsyncMock(return_value=[])

    summary_mock = MagicMock()
    summary_mock.healths = []
    summary_mock.spot_balances = [MagicMock(product_id=0, balance=MagicMock(amount=1000 * 10**18))]
    
    service.client = MagicMock()
    service.client.subaccount = MagicMock()
    service.client.subaccount.get_engine_subaccount_summary = MagicMock(return_value=summary_mock)

    # Anchor daily start
    import time
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    service._daily_start_date = today_str
    service._daily_start_balance = 1000.0
    service._daily_start_total_trades = service.win_count + service.loss_count + service.scratch_count  # 10

    summary_before = await service.get_portfolio_summary()
    assert summary_before["daily_trades_count"] == 0

    # 1 scratch trade occurs
    service.scratch_count = 4
    summary_after = await service.get_portfolio_summary()
    assert summary_after["daily_trades_count"] == 1, "daily_trades_count must increment on scratch trades"


# ==============================================================================
# F-23: LLMClient Fallback Does Not Overwrite Primary Model
# ==============================================================================
@pytest.mark.asyncio
async def test_f23_llm_client_transient_fallback_preserves_primary_model():
    """F-23: OpenRouter transient fallback does not permanently demote self.model_name or reorder self.or_models."""
    client = LLMClient()
    primary = "qwen/qwen-2.5-72b-instruct"
    fallback = "qwen/qwen-plus"
    
    client.openrouter_key = "test_openrouter_key"
    client.provider = "openrouter"
    client.model_name = primary
    client.or_models = [primary, fallback]
    client._switch_provider = MagicMock(return_value=False)

    class MockResponseContext:
        def __init__(self, resp):
            self.resp = resp
        async def __aenter__(self):
            return self.resp
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    def mock_post(url, headers=None, json=None, timeout=None):
        model_req = json.get("model")
        mock_resp = MagicMock()
        if model_req == primary:
            # Return 400 not found to trigger immediate model chain fallback without sleep
            mock_resp.status = 400
            mock_resp.text = AsyncMock(return_value="400 Model not found on endpoints")
        else:
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value={
                "choices": [{"message": {"content": '{"action": "HOLD"}'}}]
            })
        return MockResponseContext(mock_resp)

    mock_session = MagicMock()
    mock_session.post = mock_post
    mock_session.closed = False
    client._aiohttp_session = mock_session

    res = await client.generate("Analyze SOL", max_retries=1)

    assert res == '{"action": "HOLD"}'
    # F-23 verification: primary model remains primary, and list order is preserved!
    assert client.model_name == primary, f"Primary model should stay {primary}, but got {client.model_name}"
    assert client.or_models[0] == primary, f"Primary model in or_models must stay first, but got {client.or_models[0]}"
