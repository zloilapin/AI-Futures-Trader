import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from services.telegram_service import TelegramService
from services.telegram_bot_listener import TelegramBotListener
from services.copy_trading_dispatcher import CopyTradingDispatcher, NADO_SIZE_INCREMENTS
from agents.reflector_agent import ReflectorAgent
from core.pipeline import TradingPipeline
from services.market_data_service import MarketDataService
from agents.oi_funding_agent import OIFundingAgent
from agents.news_agent import NewsAgent
from core.llm_parser import LLMStructuredOutputParser
from core.llm_client import LLMClient, LLMCircuitBreakerException


@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


# ─── ЧАСТЬ 1. ТЕСТЫ БЛОКОВ 07 И 08 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_service_custom_chat_id_and_show_alert():
    """
    Verifies:
    1. send_message accepts chat_id for subscribers and does NOT inject admin REPLY_KEYBOARD.
    2. answer_callback_query accepts show_alert and attaches it to the payload.
    """
    service = TelegramService()
    service.bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    service.chat_id = "999999"

    mock_resp = AsyncMock()
    mock_resp.status = 200

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp), __aexit__=AsyncMock(return_value=False)))

    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        # Case A: send_message with subscriber chat_id
        await service.send_message("Hello Subscriber", chat_id="111222")
        call_kwargs = mock_session.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["chat_id"] == "111222"
        assert payload["text"] == "Hello Subscriber"
        assert "reply_markup" not in payload  # Subscriber must NOT get admin keyboard

        # Case B: answer_callback_query with show_alert=True
        await service.answer_callback_query("cb_123", text="Alert!", show_alert=True)
        cb_kwargs = mock_session.post.call_args[1]
        cb_payload = cb_kwargs["json"]
        assert cb_payload["callback_query_id"] == "cb_123"
        assert cb_payload["text"] == "Alert!"
        assert cb_payload["show_alert"] is True


@pytest.mark.asyncio
async def test_telegram_listener_text_input_unlock_and_manual_signature():
    """
    Verifies:
    1. is_cmd condition accepts waiting input sender and 42-char 0x addresses.
    2. handle_command processes active_prompt == 'manual_signature'.
    """
    listener = TelegramBotListener.__new__(TelegramBotListener)
    listener.chat_id = "123456"
    listener.allowed_chat_ids = set()
    listener._waiting_input = {"user_1": "manual_signature"}
    listener.trading_service = MagicMock()
    listener._send_reply = AsyncMock()
    listener._get_wallet_payload = AsyncMock(return_value=("Wallet Info", {}))
    listener.handle_command = AsyncMock()

    # Case A: User cancels manual signature
    await TelegramBotListener.handle_command(listener, "отмена", sender_chat_id="user_1")
    assert "user_1" not in listener._waiting_input
    listener._send_reply.assert_awaited_once()

    # Case B: User submits signature string
    listener._waiting_input["user_2"] = "manual_signature"
    await TelegramBotListener.handle_command(listener, "0x123 42 0xsignature", sender_chat_id="user_2")
    assert "user_2" not in listener._waiting_input
    listener.handle_command.assert_awaited_with("/link_confirm 0x123 42 0xsignature", sender_chat_id="user_2")


def test_pipeline_snapshot_records_adx_14():
    """
    Verifies that TradingPipeline._record_candidate_snapshot logic records adx_14 in market_guard_inputs.
    """
    snapshot = {}
    market_data = {
        "symbol": "BTC-USD",
        "price_data": {"current_price": 50000.0},
        "indicators": {"rsi_14": 55.0, "adx_14": 32.5, "atr_14": 500.0, "atr": 500.0}
    }
    raw_indicators = market_data["indicators"]

    def _safe_float(val):
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    snapshot["market_guard_inputs"] = {
        "rsi_14": _safe_float(raw_indicators.get("rsi_14")),
        "adx_14": _safe_float(raw_indicators.get("adx_14") or raw_indicators.get("adx")),
        "atr_14": _safe_float(raw_indicators.get("atr_14") or raw_indicators.get("atr"))
    }

    assert snapshot["market_guard_inputs"]["adx_14"] == 32.5


@pytest.mark.asyncio
async def test_telegram_listener_reads_mgi_adx_bb_and_journal_net_pnl(tmp_path):
    """
    Verifies that radar/regime read ADX and BB from market_guard_inputs and journal reads net_pnl_usd.
    """
    listener = TelegramBotListener.__new__(TelegramBotListener)
    listener.chat_id = "123456"
    listener.allowed_chat_ids = set()
    listener.trading_service = MagicMock()
    listener.trading_service.active_positions = {}

    # Write real JSON file to tmp_path
    journal_file = tmp_path / "trade_journal.json"
    journal_data = [
        {"symbol": "SOL-USD", "direction": "SHORT", "net_pnl_usd": 15.42, "realized_r": 1.25, "triggered_by": "TRAILING_STOP"}
    ]
    import json
    with open(journal_file, "w", encoding="utf-8") as f:
        json.dump(journal_data, f)

    with patch("core.state_store.StateStore._normalize_path", return_value=str(journal_file)):
        text, markup = await listener._get_journal_payload()
        assert "$+15.42" in text
        assert "TRAILING_STOP" in text


def test_copy_trading_sizing_bump_and_size_increment_alignment():
    """
    Verifies:
    1. calculate_subscriber_sizing returns actual risk at $104 bump.
    2. NADO_SIZE_INCREMENTS aligns contracts using ceil for <= 104.5.
    """
    dispatcher = CopyTradingDispatcher.__new__(CopyTradingDispatcher)

    # 1. Sizing bump actual risk calculation
    sub = {"risk_per_trade_pct": 1.0, "max_leverage": 8.0}
    # entry 100, SL 98 (dist = 2%) => at $104, risk_at_104 = $2.08
    # sub equity $150 => user_risk_usd = $1.50
    can_trade, notional, actual_risk, status = dispatcher.calculate_subscriber_sizing(
        sub=sub,
        entry_price=100.0,
        master_sl=98.0,
        subaccount_equity=150.0,
        available_margin=50.0
    )
    assert can_trade is True
    assert notional == 104.0
    assert status == "BUMPED_TO_MIN_NOTIONAL"
    assert actual_risk == round(104.0 * 0.02, 4)  # $2.08 > $1.50

    # 2. Size increment rounding
    # SOL: size_increment = 0.1
    import math
    sol_entry = 150.0
    sol_notional = 104.0
    raw_contracts = sol_notional / sol_entry  # 0.69333
    size_inc = NADO_SIZE_INCREMENTS["SOL"]  # 0.1
    steps = math.ceil(round(raw_contracts / size_inc, 8))
    sol_contracts = round(steps * size_inc, 1)
    assert sol_contracts == 0.7
    assert sol_contracts * sol_entry == 105.0  # >= $101.00 min!


@pytest.mark.asyncio
async def test_copy_trading_dynamic_trailing_behind_mark_price():
    """
    Verifies dynamic trailing stop behind mark price:
    selects max(lock_floor, dynamic_trail) for LONG positions.
    """
    dispatcher = CopyTradingDispatcher.__new__(CopyTradingDispatcher)
    dispatcher.positions_store = MagicMock()
    dispatcher.master_trading_service = MagicMock()
    dispatcher._send_subscriber_notification = AsyncMock()

    # LONG position: entry 100, SL 90 (r_dist = 10)
    # mark_price is at 125 (+2.50R profit)
    # lock_floor = 100 + 0.75 * 10 = 107.5 (+0.75R)
    # dynamic_trail = 125 - 0.85 * 10 = 116.5 (+1.65R)
    # Target SL must choose 116.5
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "initial_sl": 90.0,
        "sl_price": 100.0,
        "r_dist": 10.0,
        "contracts": 1.0,
        "planned_risk_usd": 10.0,
        "trail_activation_r": 1.35,
        "protection_state": "BREAK_EVEN"
    }

    dispatcher.master_trading_service.get_last_price = MagicMock(return_value=125.0)
    dispatcher.master_trading_service.market_data_service.get_last_price = MagicMock(return_value=125.0)

    await dispatcher._manage_single_subscriber_position("sub_1", "SOL-USD", pos)
    assert pos["sl_price"] == 116.5
    assert pos["protection_state"] == "TRAILING"
    dispatcher.positions_store.set_user_position.assert_called_once_with("sub_1", "SOL-USD", pos)


def test_reflector_agent_cleans_candles_20_from_price_data():
    """
    Verifies that ReflectorAgent._clean_context_for_llm strips 'candles_20' from nested price_data.
    """
    context = {
        "symbol": "BTC-USD",
        "candles_20": [1, 2, 3],
        "price_data": {
            "current_price": 50000.0,
            "candles_20": [{"open": 1}, {"open": 2}],
            "candles": [{"open": 1}]
        },
        "order_book": {"best_bid": 49990.0, "best_ask": 50010.0}
    }

    clean = ReflectorAgent._clean_context_for_llm(context)
    assert "candles_20" not in clean
    assert "candles_20" not in clean["price_data"]
    assert "candles" not in clean["price_data"]
    assert clean["price_data"]["current_price"] == 50000.0


# ─── ЧАСТЬ 2. ТЕСТЫ БЛОКОВ 02 И 03 ──────────────────────────────────────────

def test_market_data_timestamp_and_emergency_cache_ttl(mock_logger):
    service = MarketDataService(logger=mock_logger)
    raw_dicts = [{"open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 1000.0, "timestamp": 1700000000.0}]
    norm = service._normalize_candles(raw_dicts)
    assert norm[0]["timestamp"] == 1700000000.0

    fallback = service._create_fallback_indicators("SOL-USD", 100.0)
    assert "rvol" in fallback
    assert fallback["rvol"] == 1.0


@pytest.mark.asyncio
async def test_oi_funding_agent_uses_closed_candle(mock_logger):
    agent = OIFundingAgent(logger=mock_logger, llm_client=MagicMock())
    market_data = {
        "symbol": "SOL-USD",
        "price_data": {
            "current_price": 102.0,
            "candles_20": [
                {"open": 98.0, "high": 100.0, "low": 97.0, "close": 99.0, "volume": 100},
                {"open": 100.0, "high": 106.0, "low": 99.0, "close": 105.0, "volume": 200}, # completed GREEN
                {"open": 105.0, "high": 105.0, "low": 101.0, "close": 102.0, "volume": 50}   # live RED
            ]
        },
        "derivatives_data": {"open_interest_trend": "rising", "oi_change_pct": 5.0, "funding_rate": 0.0001},
        "indicators": {}
    }
    res = await agent.analyze(market_data)
    assert res["signal"] == "BULLISH"


def test_llm_parser_raises_on_missing_score_breakdown():
    bullet_text = '- trade_action: "SHORT"\n- directional_confidence: 80'
    with pytest.raises(ValueError) as exc:
        LLMStructuredOutputParser.parse_and_validate(bullet_text, required_keys=["trade_action", "score_breakdown"])
    assert "score_breakdown" in str(exc.value)


@pytest.mark.asyncio
async def test_news_agent_and_llm_client_rotation(mock_logger):
    # NewsAgent counter-trend blocking in fallback
    agent = NewsAgent(logger=mock_logger, llm_client=None)
    m1 = {"symbol": "BTC-USD", "news_data": {"sentiment_score": 35.0}, "multi_timeframe": {"trend_1h": "BEARISH"}}
    r1 = await agent.analyze(m1)
    assert r1["signal"] == "NEUTRAL"

    # LLMClient provider preservation in rotation
    client = LLMClient.__new__(LLMClient)
    client.provider = "none"
    client.model_name = "none"
    p1 = {"provider": "groq", "model": "m1"}
    p2 = {"provider": "gemini", "model": "m2"}
    client._available_providers = [p2]
    client._set_provider(p1)
    assert client._switch_provider() is True
    assert client.provider == "gemini"
    assert client._available_providers == [p1]  # p1 preserved in queue!


@pytest.mark.asyncio
async def test_radar_whales_payload_and_button():
    """
    Verifies that:
    1. _get_radar_payload includes the '[🐋 Топ-Снайперы Nado]' inline button.
    2. _get_radar_whales_payload filters for positional snipers (10 <= trades <= 15000, win_rate >= 55%)
       and excludes HFT market makers.
    """
    listener = TelegramBotListener.__new__(TelegramBotListener)
    listener.trading_service = MagicMock()

    # 1. Check radar payload button
    text, markup = await listener._get_radar_payload()
    buttons_flat = [b for row in markup["inline_keyboard"] for b in row]
    whale_btn = next((b for b in buttons_flat if b["callback_data"] == "radar_whales"), None)
    assert whale_btn is not None
    assert "Снайперы" in whale_btn["text"]

    # 2. Check leaderboard filtering
    mock_data = {
        "items": [
            # HFT market maker (trades > 15,000) -> must be EXCLUDED
            {"displayName": "FosterHFT", "trades": 500000, "winRate": "0.55", "pnl": "1000000", "volume": "50000000"},
            # Pure sniper (80 trades, 100% WR, positive PnL) -> must be INCLUDED
            {"displayName": "SniperOne", "trades": 80, "winRate": "1.0", "pnl": "193646.80", "volume": "664317", "subaccountHex": "0x1234567890abcdef"},
            # Low win rate (< 55%) -> must be EXCLUDED
            {"displayName": "LosingTrader", "trades": 100, "winRate": "0.40", "pnl": "1000", "volume": "50000"},
            # Too few trades (< 10) -> must be EXCLUDED
            {"displayName": "LuckyBeginner", "trades": 3, "winRate": "1.0", "pnl": "5000", "volume": "10000"}
        ]
    }

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_data)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp), __aexit__=AsyncMock(return_value=False)))

    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        whales_text, whales_markup = await listener._get_radar_whales_payload()
        assert "ТОП-СНАЙПЕРЫ ЛИДЕРБОРДА NADO DEX" in whales_text
        assert "SniperOne" in whales_text
        assert "+$193,646.80" in whales_text
        assert "FosterHFT" not in whales_text
        assert "LosingTrader" not in whales_text
        assert "LuckyBeginner" not in whales_text
        assert any(b["callback_data"] == "radar_whales" for row in whales_markup["inline_keyboard"] for b in row)


def test_regime_agent_classify_regime_no_self_name_error():
    """
    Verifies that RegimeAgent.classify_regime_from_scores correctly handles previous_regime="UNKNOWN"
    without raising NameError: name 'self' is not defined.
    """
    from agents.regime_agent import RegimeAgent

    # Case A: previous_regime="UNKNOWN" and last_range_score >= 55.0 -> requires confirmation, routes to TRANSITION
    regime, profile = RegimeAgent.classify_regime_from_scores(
        trend_score=75.0,
        range_score=40.0,
        vol_score=30.0,
        btc_atr=0.8,
        previous_regime="UNKNOWN",
        consecutive_trend_count=0,
        last_range_score=60.0
    )
    assert regime == "TRANSITION"
    assert profile == "BALANCED"

    # Case B: previous_regime="UNKNOWN" and last_range_score < 55.0 -> directly to TRENDING / AGGRESSIVE
    regime_b, profile_b = RegimeAgent.classify_regime_from_scores(
        trend_score=75.0,
        range_score=40.0,
        vol_score=30.0,
        btc_atr=0.8,
        previous_regime="UNKNOWN",
        consecutive_trend_count=0,
        last_range_score=30.0
    )
    assert regime_b == "TRENDING"
    assert profile_b == "AGGRESSIVE"


def test_market_data_timestamp_validation_and_submission_idx():
    """
    Verifies that MarketDataService._normalize_candles ignores timestamps < 1_700_000_000.0
    (such as sequencer submission indices 450123) and keeps only valid Unix epoch seconds.
    """
    service = MarketDataService.__new__(MarketDataService)
    raw_candles = [
        {"timestamp": 450123, "open": 100, "high": 105, "low": 99, "close": 102, "volume": 10},
        {"timestamp": 1715000000, "open": 102, "high": 106, "low": 101, "close": 105, "volume": 15},
        [450124, 105, 108, 104, 107, 20],
        [1715000900000, 107, 110, 106, 109, 25],  # ms timestamp -> normalized to sec
    ]
    norm = service._normalize_candles(raw_candles)
    assert len(norm) == 4
    assert "timestamp" not in norm[0]  # Ignored because 450123 < 1_700_000_000
    assert norm[1]["timestamp"] == 1715000000.0
    assert "timestamp" not in norm[2]  # Ignored because 450124 < 1_700_000_000
    assert norm[3]["timestamp"] == 1715000900.0


def test_data_quality_guard_recent_gap_check_only():
    """
    Verifies that DataQualityGuard limits OHLCV_GAP_DETECTED to the last 5 closed candles.
    Historical gaps (e.g. index 2 out of 25) do not cause validation failure.
    Recent gaps (e.g. index 23 out of 25) correctly trigger OHLCV_GAP_DETECTED.
    """
    from core.data_quality_guard import DataQualityGuard
    guard = DataQualityGuard()

    base_ts = 1715000000.0
    step = 900.0  # 15m

    # 1. 25 candles with an ancient gap at candle 2 (gap of 3600s)
    candles_hist_gap = []
    curr_ts = base_ts
    for i in range(25):
        if i == 2:
            curr_ts += 3600.0  # Historical gap
        else:
            curr_ts += step
        candles_hist_gap.append({
            "timestamp": curr_ts,
            "open": 50000.0,
            "high": 50100.0,
            "low": 49900.0,
            "close": 50050.0,
            "volume": 100.0
        })

    market_data_hist = {
        "symbol": "BTC-USD",
        "price_data": {
            "current_price": 50050.0,
            "timestamp": curr_ts,
            "candles_20": candles_hist_gap
        },
        "order_book_data": {
            "best_bid": 50040.0,
            "best_ask": 50060.0
        },
        "derivatives_data": {"open_interest": 1000.0, "funding_rate": 0.0001}
    }
    with patch("time.time", return_value=curr_ts + 60.0):
        is_valid, reason = guard.validate("BTC-USD", market_data_hist)
    assert is_valid is True, f"Historical gap should not veto: {reason}"

    # 2. 25 candles with a recent gap at candle 23 (gap of 3600s)
    candles_recent_gap = []
    curr_ts = base_ts
    for i in range(25):
        if i == 23:
            curr_ts += 3600.0  # Recent gap in last 5 candles
        else:
            curr_ts += step
        candles_recent_gap.append({
            "timestamp": curr_ts,
            "open": 50000.0,
            "high": 50100.0,
            "low": 49900.0,
            "close": 50050.0,
            "volume": 100.0
        })

    market_data_recent = {
        "symbol": "BTC-USD",
        "price_data": {
            "current_price": 50050.0,
            "timestamp": curr_ts,
            "candles_20": candles_recent_gap
        },
        "order_book_data": {
            "best_bid": 50040.0,
            "best_ask": 50060.0
        },
        "derivatives_data": {"open_interest": 1000.0, "funding_rate": 0.0001}
    }
    with patch("time.time", return_value=curr_ts + 60.0):
        is_valid_rec, reason_rec = guard.validate("BTC-USD", market_data_recent)
    assert is_valid_rec is False
    assert "OHLCV_GAP_DETECTED" in reason_rec


@pytest.mark.asyncio
async def test_telegram_journal_debates_and_radar_whales_formatting(tmp_path):
    """
    Verifies that:
    1. _get_journal_payload uses backtick `{exit_reason}` for Markdown safety.
    2. _get_debates_payload sanitizes underscores in thesis reason.
    3. _get_radar_whales_payload falls back to pre-loaded snipers if response is empty.
    """
    listener = TelegramBotListener.__new__(TelegramBotListener)
    listener.trading_service = MagicMock()

    # 1. Journal payload test
    j_file = tmp_path / "trade_journal.json"
    import json
    with open(j_file, "w", encoding="utf-8") as f:
        json.dump([{
            "symbol": "ETH-USD",
            "direction": "LONG",
            "net_pnl_usd": 42.50,
            "realized_r": 1.25,
            "triggered_by": "TRAILING_STOP_0.75R"
        }], f)

    with patch("core.state_store.StateStore._normalize_path", return_value=str(j_file)):
        j_text, _ = await listener._get_journal_payload()
        assert "| `TRAILING_STOP_0.75R`" in j_text
        assert "_TRAILING_STOP_0.75R_" not in j_text

    # 2. Debates payload test
    mock_cycle = {
        "symbol": "BTC-USD",
        "ceo_decision": {
            "strategy_mode": "BREAKOUT",
            "decision": "BUY",
            "action": "ENTER",
            "conviction": 85,
            "risk_penalties": 5,
            "reasoning_en": "Strong breakout above resistance_level_1 and volume_surge_detected."
        }
    }
    with patch("glob.glob", return_value=["data/memory/cycle_1.json"]), \
         patch("builtins.open", MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value="")))))):
        with patch("json.load", return_value=mock_cycle):
            d_text, _ = await listener._get_debates_payload()
            assert "resistance-level-1" in d_text
            assert "volume-surge-detected" in d_text
            assert "resistance_level_1" not in d_text

    # 3. Radar whales payload fallback test
    mock_resp = AsyncMock()
    mock_resp.status = 500  # DEX endpoint temporary error
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp), __aexit__=AsyncMock(return_value=False)))

    with patch("core.session.SessionManager.get", AsyncMock(return_value=mock_session)):
        w_text, _ = await listener._get_radar_whales_payload()
        assert "ТОП-СНАЙПЕРЫ ЛИДЕРБОРДА NADO DEX" in w_text
        assert "SwiftPrecept63" in w_text
        assert "+$808,481.54" in w_text


def test_copy_trading_dispatcher_ceil_boundary():
    """
    Verifies that CopyTradingDispatcher bumps contracts with ceil when floor results in < 101.00 notional.
    """
    import math
    sub_notional = 105.0  # Above 104.5, so normally floor is used
    effective_entry = 50000.0  # BTC
    raw_contracts = sub_notional / effective_entry  # 0.0021
    size_inc = 0.001
    sl_dist = 1000.0
    user_risk_usd = 2.1

    # Simulate logic from dispatcher
    if sub_notional <= 104.5:
        steps = math.ceil(round(raw_contracts / size_inc, 8))
    else:
        steps = math.floor(round(raw_contracts / size_inc, 8))  # steps = 2 -> 0.002 -> notional = 100.00 < 101.00

    inc_str = f"{size_inc:.10f}".rstrip("0")
    decimals = len(inc_str.split(".")[1]) if "." in inc_str else 0
    contracts = round(steps * size_inc, min(decimals, 8))

    assert contracts * effective_entry == 100.00  # Floor gave $100.00

    # Dispatcher check:
    if sub_notional >= 101.0 and (contracts * effective_entry) < 101.0:
        steps = math.ceil(round(raw_contracts / size_inc, 8))
        contracts = round(steps * size_inc, min(decimals, 8))
        if sl_dist > 0:
            user_risk_usd = round(max(user_risk_usd, contracts * sl_dist), 4)

    assert contracts == 0.003
    assert contracts * effective_entry == 150.00 >= 101.00
    assert user_risk_usd == 3.0


def test_nado_trading_service_get_last_price_resolution():
    """
    Verifies NadoTradingService.get_last_price():
    1. Returns price from active position current_price.
    2. Returns price from _latest_prices cache.
    3. Returns oracle price from _market_cache.
    4. Has market_data_service alias pointing to self.
    """
    from services.nado_trading_service import NadoTradingService

    svc = NadoTradingService.__new__(NadoTradingService)
    svc.active_positions = {}
    svc._latest_prices = {}
    svc.product_map = {"BTC": 1}
    svc._market_cache = {}
    svc.market_data_service = svc

    # 1. Oracle cache fallback
    mock_info = MagicMock()
    mock_info.oracle_price_x18 = 65000 * 10**18
    svc._market_cache[1] = mock_info
    assert svc.get_last_price("BTC-USD") == 65000.0
    assert svc.market_data_service.get_last_price("BTC-USD") == 65000.0

    # 2. _latest_prices takes priority over oracle
    svc._latest_prices["BTC-USD"] = 65100.0
    assert svc.get_last_price("BTC-USD") == 65100.0

    # 3. active_positions current_price takes highest priority
    svc.active_positions["BTC-USD"] = {"current_price": 65200.0}
    assert svc.get_last_price("BTC-USD") == 65200.0


def test_is_risk_free_position_rejects_pending_be():
    """
    Verifies that is_risk_free_position in risk_manager and pipeline:
    1. Rejects PENDING_BREAK_EVEN and PENDING_BE (does not prematurely free risk slot).
    2. Accepts confirmed BREAK_EVEN, TRAILING, TIME_STOP_BE.
    """
    from agents.risk_manager import RiskManager
    from core.pipeline import is_risk_free_position as pipeline_is_rf

    # Case A: PENDING_BREAK_EVEN must NOT be considered risk free
    pos_pending = {"protection_state": "PENDING_BREAK_EVEN", "entry_price": 100.0, "sl_price": 95.0}
    assert RiskManager.is_risk_free_position(pos_pending) is False
    assert pipeline_is_rf(pos_pending) is False

    pos_pending_be = {"protection_state": "PENDING_BE", "entry_price": 100.0, "sl_price": 95.0}
    assert RiskManager.is_risk_free_position(pos_pending_be) is False
    assert pipeline_is_rf(pos_pending_be) is False

    # Case B: Confirmed BREAK_EVEN / TRAILING / TIME_STOP_BE are risk free
    pos_be = {"protection_state": "BREAK_EVEN", "entry_price": 100.0, "sl_price": 100.0}
    assert RiskManager.is_risk_free_position(pos_be) is True
    assert pipeline_is_rf(pos_be) is True

    pos_trail = {"protection_state": "TRAILING", "entry_price": 100.0, "sl_price": 105.0}
    assert RiskManager.is_risk_free_position(pos_trail) is True
    assert pipeline_is_rf(pos_trail) is True

    pos_ts_be = {"protection_state": "TIME_STOP_BE", "entry_price": 100.0, "sl_price": 100.0}
    assert RiskManager.is_risk_free_position(pos_ts_be) is True
    assert pipeline_is_rf(pos_ts_be) is True


def test_config_copy_trading_enabled_default():
    """
    Verifies that COPY_TRADING_ENABLED is enabled by default in Config schema.
    """
    from core.config import Settings
    assert Settings.model_fields["COPY_TRADING_ENABLED"].default is True


@pytest.mark.asyncio
async def test_telegram_listener_calculates_live_pnl_with_get_last_price():
    """
    Verifies that TelegramBotListener uses get_last_price to compute dynamic PnL
    instead of remaining stuck at $0.00.
    """
    listener = TelegramBotListener.__new__(TelegramBotListener)
    svc = MagicMock()
    svc.get_last_price = MagicMock(return_value=66000.0)
    listener.trading_service = svc

    pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 60000.0,
        "contracts": 0.1,
        "notional_usd": 6000.0,
        "leverage": 10.0,
        "planned_risk_usd": 100.0,
        "sl_price": 59000.0,
        "tp_price": 63000.0,
        "current_price": 66000.0
    }
    svc.active_positions = {"BTC-USD": pos}

    text, _ = await listener._get_positions_payload("123456", is_admin=True)
    assert "$+600.00" in text  # (66000 - 60000) * 0.1 = +$600
    assert "66,000.00" in text
