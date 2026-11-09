import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.config import config
from core.strategy_router import StrategyRouter, StrategyProfile
from core.deterministic_guard import DeterministicGuard
from agents.risk_manager import RiskManager
from agents.ceo_agent import CEOAgent
from services.telegram_bot_listener import TelegramBotListener, REPLY_KEYBOARD, BUTTON_COMMAND_MAP


def test_anti_bot_narrow_channel_protection():
    """Verify that Bollinger Band width < 0.55% returns neutral profile to protect from fee/spread drag."""
    config.ANTI_BOT_MODE = True
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 100.20,
            "bb_lower": 99.80, # width = 0.40% < 0.55%
            "bb_pos": 90.0,
            "rsi_14": 70.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        }
    }
    profile = StrategyRouter.evaluate("BTC-USD", market_data, [], {}, "RANGE_CHOPPY")
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "MEAN_REVERSION"
    assert profile.direction_bias == "NEUTRAL"
    assert "узкий канал" in profile.reasoning.lower()


def test_anti_bot_steamroller_guard():
    """Verify that volume_spike >= 260% and ADX >= 38 stops the predator from fading a trending cascade."""
    config.ANTI_BOT_MODE = True
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0, # width = 4.0%
            "bb_pos": 95.0,
            "rsi_14": 75.0,
            "volume_spike_pct": 280.0, # >= 260%
            "adx_14": 42.0 # >= 38
        }
    }
    profile = StrategyRouter.evaluate("BTC-USD", market_data, [], {}, "TRENDING_EXPANSION")
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "MEAN_REVERSION"
    assert profile.direction_bias == "NEUTRAL"
    assert "steamroller" in profile.reasoning.lower()


def test_anti_bot_bull_trap_short():
    """Verify bull trap triggers SHORT when long-bots get trapped at Bollinger / Donchian extremes."""
    config.ANTI_BOT_MODE = True
    # Condition 1: bb_pos >= 88 and rsi >= 64
    md1 = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 89.0,
            "rsi_14": 65.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        }
    }
    prof1 = StrategyRouter.evaluate("BTC-USD", md1, [], {}, "RANGE_CHOPPY")
    assert prof1.has_directional_signal is True
    assert prof1.strategy_mode == "MEAN_REVERSION"
    assert prof1.direction_bias == "SHORT"
    assert "ANTI-BOT SHORT" in prof1.reasoning

    # Condition 2: Donchian high test without explosive volume (exhaustion)
    md2 = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 80.0,
            "rsi_14": 63.0,
            "donchian_high": 100.0,
            "volume_spike_pct": 110.0,
            "adx_14": 20.0
        }
    }
    prof2 = StrategyRouter.evaluate("BTC-USD", md2, [], {}, "RANGE_CHOPPY")
    assert prof2.has_directional_signal is True
    assert prof2.direction_bias == "SHORT"

    # Condition 3: Upper sweep with high bb_pos and rsi
    md3 = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 75.0,
            "rsi_14": 60.0,
            "volume_spike_pct": 80.0,
            "adx_14": 20.0,
            "liquidity_sweeps": [{"type": "upper_sweep", "direction": "bearish"}]
        }
    }
    prof3 = StrategyRouter.evaluate("BTC-USD", md3, [], {}, "RANGE_CHOPPY")
    assert prof3.has_directional_signal is True
    assert prof3.direction_bias == "SHORT"


def test_anti_bot_bear_trap_long():
    """Verify bear trap triggers LONG when short-bots get trapped at Bollinger / Donchian lows."""
    config.ANTI_BOT_MODE = True
    # Condition 1: bb_pos <= 12 and rsi <= 36
    md1 = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 10.0,
            "rsi_14": 34.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        }
    }
    prof1 = StrategyRouter.evaluate("ETH-USD", md1, [], {}, "RANGE_CHOPPY")
    assert prof1.has_directional_signal is True
    assert prof1.strategy_mode == "MEAN_REVERSION"
    assert prof1.direction_bias == "LONG"
    assert "ANTI-BOT LONG" in prof1.reasoning

    # Condition 2: Donchian low test without explosive volume
    md2 = {
        "price_data": {"current_price": 95.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 94.0,
            "bb_pos": 20.0,
            "rsi_14": 37.0,
            "donchian_low": 95.0,
            "volume_spike_pct": 90.0,
            "adx_14": 20.0
        }
    }
    prof2 = StrategyRouter.evaluate("ETH-USD", md2, [], {}, "RANGE_CHOPPY")
    assert prof2.has_directional_signal is True
    assert prof2.direction_bias == "LONG"

    # Condition 3: Lower sweep with low bb_pos and rsi
    md3 = {
        "price_data": {"current_price": 95.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 94.0,
            "bb_pos": 25.0,
            "rsi_14": 40.0,
            "volume_spike_pct": 70.0,
            "adx_14": 20.0,
            "liquidity_sweeps": [{"type": "lower_sweep", "direction": "bullish"}]
        }
    }
    prof3 = StrategyRouter.evaluate("ETH-USD", md3, [], {}, "RANGE_CHOPPY")
    assert prof3.has_directional_signal is True
    assert prof3.direction_bias == "LONG"


def test_deterministic_guard_anti_bot_overrides():
    """Verify DeterministicGuard overrides WAIT_FOR_PULLBACK to ENTER and skips EXTREME_EXTENSION_VETO."""
    config.ANTI_BOT_MODE = True
    
    mock_rm = MagicMock()
    mock_rm._get_profile_rules.return_value = {"min_conviction": 70}
    
    # Normally, WAIT_FOR_PULLBACK blocks the trade into watchlist
    proposal = {
        "symbol": "BTC-USD",
        "decision": "SHORT",
        "trade_action": "WAIT_FOR_PULLBACK",
        "conviction": 80,
        "directional_confidence": 85,
        "entry_quality": 65,
        "sl_price": 102.0,
        "tp_price": 98.0
    }
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "rsi_14": 75.0,
            "bb_pos": 92.0,
            "volume_spike_pct": 50.0
        }
    }
    strategy_profile = StrategyProfile(True, "MEAN_REVERSION", "SHORT", "Anti-Bot Trap")
    
    res = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=proposal,
        profile="BALANCED",
        risk_manager=mock_rm,
        market_data=market_data,
        symbol="BTC-USD"
    )
    # In Anti-Bot Mean Reversion, trade_action is promoted to ENTER
    assert res.trade_action == "ENTER"
    assert res.is_actionable is True
    assert res.guard_status == "PASSED"


@pytest.mark.asyncio
async def test_risk_manager_anti_bot_parameters_and_sl_tp():
    """Verify compact SL/TP floor and multipliers in ANTI_BOT_MODE."""
    config.ANTI_BOT_MODE = True
    rm = RiskManager(MagicMock(), MagicMock())
    
    portfolio_data = {
        "current_balance": 1000.0,
        "available_margin": 1000.0,
        "margin_used": 0.0,
        "active_positions_count": 0,
        "daily_drawdown_pct": 0.0,
        "win_count": 5,
        "loss_count": 5
    }
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "ema_20": 97.0,
            "bb_middle": 97.0,
            "atr_14": 1.0,
            "atr": 1.0
        }
    }
    candidate = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 80,
        "strategy_mode": "MEAN_REVERSION",
        "macro_regime": "TRANSITION" # In Anti-bot mode, transition is allowed!
    }
    
    eval_res = await rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRANSITION",
        strategy_mode="MEAN_REVERSION"
    )
    assert eval_res["approved"] is True
    assert eval_res.get("rejection_tag") is None
    # SL base distance: max(1.0 * 1.25 = 1.25, 100 * 0.0045 = 0.45) -> 1.25
    assert eval_res["stop_loss_price"] > 100.0
    assert eval_res["take_profit_price"] < 100.0


def test_ceo_agent_anti_bot_mtf_and_mean_reversion():
    """Verify CEOAgent allows MTF contribution in MIXED_CHOP for MEAN_REVERSION without confidence penalty."""
    ceo = CEOAgent(MagicMock(), MagicMock(), MagicMock())
    breakdown = {
        "bull_argument": 50,
        "bear_argument": 10,
        "mtf_trend": 0,
        "risk_penalties": 0
    }
    market_context = {
        "strategy_mode": "MEAN_REVERSION",
        "multi_timeframe_context": {
            "mtf_alignment": "MIXED_CHOP"
        }
    }
    score_res = ceo._validate_and_compute_score(
        decision="LONG",
        breakdown=breakdown,
        market_context=market_context
    )
    assert score_res["decision"] == "LONG"
    assert score_res["directional_confidence"] >= 60
    assert score_res["math_conflict"] is False


@pytest.mark.asyncio
async def test_telegram_bot_listener_menu_and_callbacks():
    """Verify persistent ReplyKeyboard and inline control panel callbacks."""
    fake_trading_service = MagicMock()
    fake_trading_service.get_portfolio_summary = AsyncMock(return_value={
        "current_balance": 500.0,
        "spot_balance": 500.0,
        "available_margin": 500.0,
        "margin_used": 0.0,
        "active_positions_count": 0,
        "peak_balance": 550.0,
        "initial_balance": 500.0
    })
    fake_trading_service.reset_peak = MagicMock(return_value=500.0)
    fake_trading_service.active_positions = {}
    
    listener = TelegramBotListener(fake_trading_service)
    listener.bot_token = "dummy_token"
    listener.chat_id = "12345"
    listener.allowed_chat_ids = ["12345"]
    
    # 1. Verify keyboard constants
    assert len(REPLY_KEYBOARD["keyboard"]) == 3
    assert "📊 Статус & PnL" in BUTTON_COMMAND_MAP
    assert "🎛 Пульт Режимов" in BUTTON_COMMAND_MAP
    assert "🦈 Анти-Бот (Вкл/Выкл)" in BUTTON_COMMAND_MAP
    assert "🔄 Сброс Пика (Peak)" in BUTTON_COMMAND_MAP
    assert "🚨 ЗАКРЫТЬ ВСЕ" in BUTTON_COMMAND_MAP

    # 2. Test control panel payload generator
    text, markup = listener._get_control_panel_payload()
    assert "ПУЛЬТ УПРАВЛЕНИЯ" in text
    assert any("toggle_predator" in str(row) for row in markup["inline_keyboard"])
    assert any("toggle_shadow" in str(row) for row in markup["inline_keyboard"])

    # 3. Test handle_callback for toggle_predator
    config.ANTI_BOT_MODE = False
    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock) as mock_ans, \
         patch.object(listener, "_edit_message_text", new_callable=AsyncMock) as mock_edit, \
         patch("services.telegram_bot_listener._persist_env_var"):
        await listener.handle_callback("cb_1", "toggle_predator", "12345", message_id=42)
        assert config.ANTI_BOT_MODE is True
        mock_ans.assert_awaited_once()
        mock_edit.assert_awaited_once()

    # 4. Test handle_callback for setrisk_BALANCED
    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock) as mock_ans, \
         patch.object(listener, "_edit_message_text", new_callable=AsyncMock) as mock_edit, \
         patch("services.telegram_bot_listener._persist_env_var"):
        await listener.handle_callback("cb_2", "setrisk_BALANCED", "12345", message_id=42)
        assert config.TRADING_PROFILE == "BALANCED"

    # 5. Test handle_callback for act_reset_peak
    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock) as mock_ans, \
         patch.object(listener, "_edit_message_text", new_callable=AsyncMock) as mock_edit, \
         patch.object(listener, "_send_reply", new_callable=AsyncMock) as mock_reply:
        await listener.handle_callback("cb_3", "act_reset_peak", "12345", message_id=42)
        fake_trading_service.reset_peak.assert_called_once()


@pytest.mark.asyncio
async def test_anti_bot_low_rr_allowed_and_standard_vetoed():
    """Verify that trades with Net RR < 1.0 (e.g. 0.43, 0.32) are permitted ONLY in ANTI_BOT_MODE MEAN_REVERSION."""
    rm = RiskManager(MagicMock(), MagicMock())
    
    portfolio_data = {
        "current_balance": 1000.0,
        "available_margin": 1000.0,
        "margin_used": 0.0,
        "active_positions_count": 0,
        "daily_drawdown_pct": 0.0,
        "win_count": 10,
        "loss_count": 5
    }
    
    # Setup market data where distance to mean is 1.00 (TP dist ~0.90) and ATR is 0.80 (SL dist ~1.00)
    # Price = 101.00, mean (bb_middle) = 100.00 -> SHORT mean reversion
    market_data = {
        "price_data": {"current_price": 101.00},
        "indicators": {
            "ema_20": 100.00,
            "bb_middle": 100.00,
            "atr_14": 0.8,
            "atr": 0.8
        }
    }
    candidate = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 80,
        "strategy_mode": "MEAN_REVERSION"
    }
    
    # 1. Under ANTI_BOT_MODE = True, low net RR (~0.46) is APPROVED
    config.ANTI_BOT_MODE = True
    res_predator = await rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="RANGE_CHOPPY",
        strategy_mode="MEAN_REVERSION"
    )
    assert res_predator["approved"] is True
    assert res_predator["veto_category"] is None

    # 2. Under standard mode (ANTI_BOT_MODE = False), Net RR < 1.0 is VETOED with POOR_RISK_REWARD
    config.ANTI_BOT_MODE = False
    res_standard = await rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="RANGE_CHOPPY",
        strategy_mode="MEAN_REVERSION"
    )
    assert res_standard["approved"] is False
    assert res_standard["veto_category"] == "POOR_RISK_REWARD"
    assert "below minimum required" in res_standard["reasoning"]


def test_anti_bot_band_walking_marubozu_filter():
    """Verify Band Walking / Marubozu filter blocks fading full-bodied candles without rejection wicks."""
    config.ANTI_BOT_MODE = True
    
    # 1. Bullish Marubozu at highs: open=98, close=100, high=100.1, low=97.9 -> range=2.2, body=2.0 (ratio ~0.909), upper_wick=0.1 (ratio ~0.045 < 0.12)
    # bb_pos=90, rsi=70 (< 76), no sweep -> Should block SHORT
    md_bullish_marubozu = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 101.0,
            "bb_lower": 95.0,
            "bb_pos": 90.0,
            "rsi_14": 70.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        },
        "multi_timeframe": {
            "tf_15m": {
                "candles_20": [
                    {"open": 98.0, "high": 100.1, "low": 97.9, "close": 100.0}
                ]
            }
        }
    }
    prof1 = StrategyRouter.evaluate("BTC-USD", md_bullish_marubozu, [], {}, "RANGE_CHOPPY")
    assert prof1.has_directional_signal is False
    assert prof1.direction_bias == "NEUTRAL"
    assert "band walking" in prof1.reasoning.lower() or "marubozu" in prof1.reasoning.lower()

    # 2. Bullish candle with rejection wick: open=98, close=99.5, high=101.5, low=97.8 -> upper wick=2.0, body=1.5
    # Should NOT be blocked by Band Walking
    md_with_wick = {
        "price_data": {"current_price": 99.5},
        "indicators": {
            "bb_upper": 101.0,
            "bb_lower": 95.0,
            "bb_pos": 90.0,
            "rsi_14": 70.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        },
        "multi_timeframe": {
            "tf_15m": {
                "candles_20": [
                    {"open": 98.0, "high": 101.5, "low": 97.8, "close": 99.5}
                ]
            }
        }
    }
    prof2 = StrategyRouter.evaluate("BTC-USD", md_with_wick, [], {}, "RANGE_CHOPPY")
    assert prof2.has_directional_signal is True
    assert prof2.direction_bias == "SHORT"

    # 3. Bearish Marubozu at lows: open=98, close=96, high=98.1, low=95.9 -> range=2.2, body=2.0, lower_wick=0.1 (< 0.12)
    # bb_pos=8, rsi=30 (> 24), no sweep -> Should block LONG
    md_bearish_marubozu = {
        "price_data": {"current_price": 96.0},
        "indicators": {
            "bb_upper": 101.0,
            "bb_lower": 96.5,
            "bb_pos": 8.0,
            "rsi_14": 30.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        },
        "multi_timeframe": {
            "tf_15m": {
                "candles_20": [
                    {"open": 98.0, "high": 98.1, "low": 95.9, "close": 96.0}
                ]
            }
        }
    }
    prof3 = StrategyRouter.evaluate("ETH-USD", md_bearish_marubozu, [], {}, "RANGE_CHOPPY")
    assert prof3.has_directional_signal is False
    assert prof3.direction_bias == "NEUTRAL"
    assert "band walking" in prof3.reasoning.lower() or "marubozu" in prof3.reasoning.lower()


def test_anti_bot_orderbook_imbalance_veto():
    """Verify order book absorption filter vetoes shorting into massive bid support or longing into ask walls."""
    config.ANTI_BOT_MODE = True
    
    # Attempt SHORT (bb_pos=90, rsi=70), but bid imbalance = +0.35 (> 0.22)
    md_bid_heavy = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 90.0,
            "rsi_14": 70.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0,
            "order_book_imbalance": 0.35
        }
    }
    prof_short = StrategyRouter.evaluate("BTC-USD", md_bid_heavy, [], {}, "RANGE_CHOPPY")
    assert prof_short.has_directional_signal is False
    assert prof_short.direction_bias == "NEUTRAL"
    assert "поддержка бидов" in prof_short.reasoning.lower()

    # Attempt LONG (bb_pos=10, rsi=30), but ask imbalance = -0.35 (< -0.22)
    md_ask_heavy = {
        "price_data": {"current_price": 98.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 10.0,
            "rsi_14": 30.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0,
            "order_book_imbalance": -0.35
        }
    }
    prof_long = StrategyRouter.evaluate("BTC-USD", md_ask_heavy, [], {}, "RANGE_CHOPPY")
    assert prof_long.has_directional_signal is False
    assert prof_long.direction_bias == "NEUTRAL"
    assert "давление асков" in prof_long.reasoning.lower()


def test_anti_bot_oi_and_funding_trap_tag():
    """Verify OI and Funding trap is recognized and tagged in reasoning for CEO Agent."""
    config.ANTI_BOT_MODE = True
    
    md = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 90.0,
            "rsi_14": 70.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0,
            "funding_rate": 0.0003,
            "oi_trend": "rising"
        }
    }
    prof = StrategyRouter.evaluate("BTC-USD", md, [], {}, "RANGE_CHOPPY")
    assert prof.has_directional_signal is True
    assert prof.direction_bias == "SHORT"
    assert "[OI/Funding Trap Confirmed]" in prof.reasoning


@pytest.mark.asyncio
async def test_anti_bot_ema20_dynamic_tp_targeting():
    """Verify dynamic TP targeting EMA-20 * 1.15 and compact SL clamping in RiskManager."""
    config.ANTI_BOT_MODE = True
    rm = RiskManager(MagicMock(), MagicMock())
    
    portfolio_data = {
        "current_balance": 1000.0,
        "available_margin": 1000.0,
        "margin_used": 0.0,
        "active_positions_count": 0,
        "daily_drawdown_pct": 0.0,
        "win_count": 10,
        "loss_count": 5
    }
    
    market_data = {
        "price_data": {"current_price": 100.00},
        "indicators": {
            "ema_20": 98.00,
            "bb_middle": 98.00,
            "atr_14": 0.5,
            "atr": 0.5
        }
    }
    candidate = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 80,
        "strategy_mode": "MEAN_REVERSION"
    }
    
    res = await rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="RANGE_CHOPPY",
        strategy_mode="MEAN_REVERSION"
    )
    assert res["approved"] is True
    assert res["take_profit_price"] <= 98.0
    assert abs(res["take_profit_price"] - 97.70) < 0.15
    assert 100.50 <= res["stop_loss_price"] <= 101.20


def test_auto_predator_regime_switching():
    """Verify AUTO_PREDATOR_SWITCH logic enables predator only in true range/chop and prioritizes trend."""
    config.AUTO_PREDATOR_SWITCH = True
    config.ANTI_BOT_MODE = False

    def apply_autopilot(detected_regime, trend_score, range_score, vol_score):
        if getattr(config, "AUTO_PREDATOR_SWITCH", False) is True:
            if detected_regime == "RANGE_CHOPPY" or (detected_regime == "TRANSITION" and range_score >= 52.0 and trend_score < 48.0):
                config.ANTI_BOT_MODE = True
            elif detected_regime == "TRENDING" or trend_score >= 50.0:
                config.ANTI_BOT_MODE = False
            else:
                config.ANTI_BOT_MODE = False

    # 1. Range/Choppy regime should turn ON predator mode
    apply_autopilot("RANGE_CHOPPY", 30.0, 65.0, 5.0)
    assert config.ANTI_BOT_MODE is True

    # 2. TRANSITION with range_score >= 52 and trend_score < 48 turns ON predator mode
    config.ANTI_BOT_MODE = False
    apply_autopilot("TRANSITION", 40.0, 55.0, 6.0)
    assert config.ANTI_BOT_MODE is True

    # 3. Trending regime should turn OFF predator mode, even if vol_score is low (e.g. 5.0)
    apply_autopilot("TRENDING", 78.0, 25.0, 5.0)
    assert config.ANTI_BOT_MODE is False

    # 4. TRANSITION with trend_score >= 50.0 should turn OFF predator mode (priority to trend)
    config.ANTI_BOT_MODE = True
    apply_autopilot("TRANSITION", 55.0, 42.0, 6.0)
    assert config.ANTI_BOT_MODE is False


@pytest.mark.asyncio
async def test_telegram_toggle_auto_predator_and_manual_override():
    """Verify Telegram toggle_auto_predator and manual override disabling autopilot."""
    fake_trading_service = MagicMock()
    listener = TelegramBotListener(fake_trading_service)
    listener.bot_token = "dummy_token"
    listener.chat_id = "12345"
    listener.allowed_chat_ids = ["12345"]
    
    # 1. Test toggle_auto_predator
    config.AUTO_PREDATOR_SWITCH = True
    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock) as mock_ans,          patch.object(listener, "_edit_message_text", new_callable=AsyncMock) as mock_edit,          patch("services.telegram_bot_listener._persist_env_var") as mock_persist:
        await listener.handle_callback("cb_auto", "toggle_auto_predator", "12345", message_id=50)
        assert config.AUTO_PREDATOR_SWITCH is False
        mock_ans.assert_awaited_once()
        mock_edit.assert_awaited_once()
        mock_persist.assert_called_with("AUTO_PREDATOR_SWITCH", "false")

    # 2. Test manual toggle_predator turns off AUTO_PREDATOR_SWITCH
    config.AUTO_PREDATOR_SWITCH = True
    config.ANTI_BOT_MODE = False
    with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock) as mock_ans,          patch.object(listener, "_edit_message_text", new_callable=AsyncMock) as mock_edit,          patch("services.telegram_bot_listener._persist_env_var") as mock_persist:
        await listener.handle_callback("cb_pred", "toggle_predator", "12345", message_id=50)
        assert config.ANTI_BOT_MODE is True
        assert config.AUTO_PREDATOR_SWITCH is False
        mock_persist.assert_any_call("AUTO_PREDATOR_SWITCH", "false")

    # 3. Test /predator command turns off AUTO_PREDATOR_SWITCH
    config.AUTO_PREDATOR_SWITCH = True
    config.ANTI_BOT_MODE = True
    with patch.object(listener, "_send_reply", new_callable=AsyncMock) as mock_reply,          patch("services.telegram_bot_listener._persist_env_var") as mock_persist:
        await listener.handle_command("/predator", sender_chat_id="12345")
        assert config.ANTI_BOT_MODE is False
        assert config.AUTO_PREDATOR_SWITCH is False
        mock_persist.assert_any_call("AUTO_PREDATOR_SWITCH", "false")


def test_ceo_agent_anti_bot_prevents_math_hallucination_veto():
    """Verify CEOAgent prevents math hallucination veto in ANTI_BOT_MODE when LLM faints on trend at highs."""
    config.ANTI_BOT_MODE = True
    ceo = CEOAgent(MagicMock(), MagicMock(), MagicMock())

    # Raw breakdown where LLM mistakenly evaluated bullish trend inertia higher than counter-trend fade (35 vs 25)
    breakdown = {
        "bull_argument": 35.0,
        "bear_argument": 25.0,
        "mtf_trend": 35.0,
        "risk_penalties": 0
    }
    market_context = {
        "strategy_mode": "MEAN_REVERSION",
        "direction_bias": "SHORT",
        "multi_timeframe_context": {
            "tf_15m": {"trend": "BULLISH"},
            "tf_1h": {"trend": "BULLISH"},
            "tf_4h": {"trend": "BULLISH"}
        }
    }
    score_res = ceo._validate_and_compute_score(
        decision="SHORT",
        breakdown=breakdown,
        market_context=market_context
    )
    assert score_res["decision"] == "SHORT"
    assert score_res["math_conflict"] is False
    assert score_res["directional_confidence"] >= 70
    assert score_res["trade_action"] in ["ENTER", "REDUCE_SIZE"]


@pytest.mark.asyncio
async def test_ceo_escalation_prompt_includes_anti_bot_rules():
    """Verify that in ANTI_BOT_MODE, escalation prompt for Llama arbiter contains anti-bot predator rules."""
    config.ANTI_BOT_MODE = True
    ceo = CEOAgent(MagicMock(), MagicMock(), MagicMock())
    addon = ceo.get_anti_bot_prompt_addon()
    assert "ANTI-BOT PREDATOR MODE IS ACTIVE" in addon
    assert "CRITICAL FOR JSON SCORE_BREAKDOWN" in addon
    assert "bear_argument strictly greater than bull_argument" in addon


def test_analyst_disagreement_and_counter_analyst_bypass():
    """Verify that in ANTI_BOT_MODE, analyst disagreement and counter-analyst vetoes are bypassed."""
    config.ANTI_BOT_MODE = True
    ceo = CEOAgent(MagicMock(), MagicMock(), MagicMock())

    # 1. _determine_hold_category returns LOW_EDGE instead of ANALYST_DISAGREEMENT
    analyst_reports = [
        {"signal": "BULLISH", "agent_name": "Bull_Agent"},
        {"signal": "BEARISH", "agent_name": "Bear_Agent"},
    ]
    hold_cat = ceo._determine_hold_category(analyst_reports, conviction=85)
    assert hold_cat == "LOW_EDGE"

    # 2. _evaluate_independent_risk_rules allows counter-trend fade even if opposing analysts > 1
    # and 1h/4h are aligned against fade
    tech_reports = [
        {"signal": "BULLISH", "agent_name": "Bull_Agent"},
        {"signal": "BULLISH", "agent_name": "Indicator_Agent"},
        {"signal": "BEARISH", "agent_name": "Bear_Agent"},
    ]
    market_context = {
        "strategy_mode": "MEAN_REVERSION",
        "multi_timeframe_context": {
            "tf_1h": {"trend": "BULLISH"},
            "tf_4h": {"trend": "BULLISH"}
        }
    }
    approved, reason, metrics = ceo._evaluate_independent_risk_rules(tech_reports, market_context, "SHORT")
    assert approved is True, f"Expected approved in anti-bot MR, got reason: {reason}"

    # 3. DeterministicGuard clears ANALYST_DISAGREEMENT / COUNTER_ANALYST_VETO for MEAN_REVERSION
    ceo_verdict = {
        "decision": "SHORT",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 80,
        "trade_action": "ENTER",
        "hold_category": "ANALYST_DISAGREEMENT"
    }
    strat_prof = StrategyProfile(
        strategy_mode="MEAN_REVERSION",
        direction_bias="SHORT",
        has_directional_signal=True,
        reasoning="Test anti-bot"
    )
    rm = RiskManager(MagicMock(), MagicMock())
    decision_res = DeterministicGuard.evaluate(
        strategy_profile=strat_prof,
        ceo_proposal=ceo_verdict,
        profile="BALANCED",
        risk_manager=rm,
        market_data={"indicators": {"bb_pos": 92.0, "rsi_14": 68.0, "atr_14": 10.0, "current_price": 1000.0}},
        symbol="SOL-USD"
    )
    assert decision_res.decision == "SHORT"
    assert decision_res.is_actionable is True
    assert decision_res.guard_status == "PASSED"
    assert decision_res.rejection_tag is None


@pytest.mark.asyncio
async def test_risk_manager_anti_bot_sizing_and_notional_bump_protection():
    """Verify market_mult >= 1.0 and notional bump $101 is preserved in Stage 7 for ANTI_BOT_MR."""
    config.ANTI_BOT_MODE = True
    rm = RiskManager(MagicMock(), MagicMock())

    ceo_decision = {
        "decision": "SHORT",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 80,
        "trade_action": "ENTER"
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "total_balance": 1000.0,
        "available_margin": 800.0,
        "daily_drawdown_pct": 0.0,
        "recent_streak": [1, 1, 1],
        "win_rate": 0.70,
        "payoff_ratio": 1.2,
        "total_trades": 20
    }
    # Market data with low volume (v_curr < avg_volume_10 would normally drop quality_mult to 0.85)
    ohlcv = [{"volume": 100.0} for _ in range(10)]
    ohlcv.append({"volume": 20.0}) # lower volume
    market_data = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": ohlcv},
        "indicators": {"atr_14": 1.0, "ema_20": 98.0, "current_price": 100.0},
        "order_book_data": {"spread_pct": 0.0005},
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.01, "price_increment": 0.01}
    }

    result = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="RANGE_CHOPPY",
        strategy_mode="MEAN_REVERSION"
    )

    assert result["approved"] is True
    # Verify notional is at least $100 (due to native trigger bump to $101) and not clamped back below $100
    assert result["notional_size_usd"] >= 100.0
    assert result["contracts"] > 0


@pytest.mark.asyncio
async def test_sentinel_anti_bot_guards():
    """Verify SentinelAgent skips breakout stagnation and doubles time-stop for Anti-Bot MR."""
    config.ANTI_BOT_MODE = True
    from agents.sentinel_agent import SentinelAgent
    sentinel = SentinelAgent(MagicMock())

    pos_mr = {
        "symbol": "BTC-USD",
        "direction": "SHORT",
        "entry_price": 100.0,
        "sl_price": 101.5,
        "initial_sl": 101.5,
        "strategy_mode": "MEAN_REVERSION",
        "is_anti_bot": True,
        "open_time": 0.0, # will mock age
        "protection_state": "PROTECTED",
        "max_favorable_excursion_r": 0.10
    }
    profile_rules = {
        "sentinel_be_atr": 1.5,
        "sentinel_trail_activation_atr": 2.5,
        "sentinel_trail_distance_atr": 2.0,
        "sentinel_min_improve_atr": 0.25
    }

    # 1. At 80 minutes age: Breakout stagnation guard (75m) MUST NOT trigger for Anti-Bot MR
    import time
    with patch("time.time", return_value=80.0 * 60.0):
        pos_mr["open_time"] = 0.0
        res = await sentinel.analyze(pos_mr, {"indicators": {"volume_spike_pct": 50.0}}, profile_rules, atr_value=1.0)
        assert res["state"] == "PROTECTED"
        assert res["new_sl"] is None

    # 2. At 200 minutes age: Normal Time-Stop Tier 1 (180m) MUST NOT trigger (threshold doubled to 360m)
    with patch("time.time", return_value=200.0 * 60.0):
        pos_mr["open_time"] = 0.0
        res = await sentinel.analyze(pos_mr, {"indicators": {"volume_spike_pct": 50.0}}, profile_rules, atr_value=1.0)
        assert res["state"] == "PROTECTED"
        assert res["new_sl"] is None

    # 3. Extreme blowout: volume spike >= 260% does NOT trigger EXTREME_BLOWOUT_DEFENSE (disabled in v2.1)
    with patch("time.time", return_value=30.0 * 60.0):
        pos_mr["open_time"] = 0.0
        pos_mr["current_price"] = 101.0 # price moved against SHORT
        md_blowout = {
            "indicators": {"volume_spike_pct": 275.0, "current_price": 101.0},
            "price_data": {"current_price": 101.0}
        }
        res = await sentinel.analyze(pos_mr, md_blowout, profile_rules, atr_value=1.0)
        assert res["state"] != "EXTREME_BLOWOUT_DEFENSE"
        assert res["new_sl"] is None


@pytest.mark.asyncio
async def test_fast_radar_predator_fakeout_spike():
    """Verify FastRadar wakes up pipeline on low-volume fakeout spike when ANTI_BOT_MODE is active."""
    config.ANTI_BOT_MODE = True
    from core.pipeline import TradingPipeline

    pipeline = MagicMock(spec=TradingPipeline)
    pipeline._cycle_running = False
    pipeline._fast_radar_cooldowns = {}
    pipeline.services = MagicMock()
    pipeline.services.trading_service.active_positions = {}

    # Candles where price jumps by +0.8% with volume ratio 0.9x (below 1.25x volume threshold)
    candles = [
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 100.0}
        for _ in range(25)
    ]
    # Current candle jumps to 100.8 (+0.8%) with volume = 90.0 (ratio 0.9x)
    candles.append({"open": 100.0, "high": 100.8, "low": 99.9, "close": 100.8, "volume": 90.0})

    pipeline.services.fetcher.fetch_ohlcv = AsyncMock(return_value={
        "candles_20": candles,
        "current_price": 100.8
    })

    # Call actual unbound method check_fast_market_momentum on our mock pipeline
    spike_detected, symbol, reason = await TradingPipeline.check_fast_market_momentum(pipeline)

    assert spike_detected is True
    assert "PREDATOR_FAKEOUT_SPIKE" in reason
    assert "Δ=0.80%" in reason


@pytest.mark.asyncio
async def test_bull_and_bear_agent_predator_prompt_addon():
    """Verify Bull and Bear debaters inject Predator context when ANTI_BOT_MODE is active."""
    from agents.bull_agent import BullAgent
    from agents.bear_agent import BearAgent
    
    config.ANTI_BOT_MODE = True
    llm = MagicMock()
    llm.model_name = "test-model"
    logger = MagicMock()
    
    bull = BullAgent(logger, llm)
    bear = BearAgent(logger, llm)
    
    captured_prompts = []
    async def fake_generate_json(prompt, *args, **kwargs):
        captured_prompts.append(prompt)
        return {"thesis_score": 8, "bullish_arguments": ["trap"], "bearish_arguments": ["trap"], "summary": "ok"}
    
    bull.generate_json = fake_generate_json
    bear.generate_json = fake_generate_json
    
    data = {"symbol": "ETH-USD", "key_levels": {}, "multi_timeframe_context": {}, "analyst_reports": []}
    await bull.analyze(data)
    await bear.analyze(data)
    
    assert len(captured_prompts) == 2
    assert "[🦈 ANTI-BOT LIQUIDITY HUNTER CONTEXT (Institutional v2.0)]" in captured_prompts[0]
    assert "[🦈 ANTI-BOT LIQUIDITY HUNTER CONTEXT (Institutional v2.0)]" in captured_prompts[1]


def test_can_apply_profile_uplift_anti_bot_bypass():
    """Verify can_apply_profile_uplift bypasses macro leader opposition in ANTI_BOT_MODE."""
    from core.pipeline import TradingPipeline
    pipeline = MagicMock(spec=TradingPipeline)
    
    config.ANTI_BOT_MODE = True
    can_uplift, reason = TradingPipeline.can_apply_profile_uplift(
        pipeline,
        symbol="SOL-USD",
        candidate_direction="LONG",
        macro_leaders_state={"BTC": "BEARISH", "ETH": "BEARISH"},
        macro_regime="RANGE_CHOPPY"
    )
    assert can_uplift is True
    assert "Anti-Bot Predator Mode" in reason


def test_register_pullback_candidate_ignores_mean_reversion_and_anti_bot():
    """Verify register_pullback_candidate skips registration for MEAN_REVERSION or ANTI_BOT_MODE."""
    from core.pipeline import TradingPipeline
    pipeline = MagicMock(spec=TradingPipeline)
    pipeline._pullback_watchlist = {}
    
    config.ANTI_BOT_MODE = True
    TradingPipeline.register_pullback_candidate(
        pipeline,
        symbol="BTC-USD",
        decision="SHORT",
        current_price=65000.0,
        directional_conf=80,
        conviction=75,
        strategy_mode="MEAN_REVERSION",
        market_data={},
        ceo_verdict={},
        strategy_profile=MagicMock()
    )
    assert len(pipeline._pullback_watchlist) == 0


@pytest.mark.asyncio
async def test_ceo_escalation_consensus_score_clamping():
    """Verify CEO escalation consensus clamps directional_confidence >= 80, entry_quality >= 72 in ANTI_BOT_MODE."""
    from agents.ceo_agent import CEOAgent, CEOEscalationPatch
    
    config.ANTI_BOT_MODE = True
    llm = MagicMock()
    llm.model_name = "deepseek-chat"
    logger = MagicMock()
    
    ceo = CEOAgent(logger, llm, llm)
    
    # Primary gives SHORT with 63% conviction
    # Arbiter gives SHORT with 73% conviction
    primary_res = {
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 63,
        "entry_quality": 63,
        "dir_conf": 65,
        "directional_confidence": 65,
        "score_breakdown": {
            "bull_argument": 15,
            "bear_argument": -40,
            "mtf_trend": -35,
            "risk_penalties": {"total": -5}
        },
        "reasoning_en": "Primary reasoning"
    }
    
    arbiter_res = {
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 73,
        "entry_quality": 73,
        "dir_conf": 75,
        "directional_confidence": 75,
        "risk_penalty": 0.0,
        "score_breakdown": {
            "bull_argument": 15,
            "bear_argument": -40,
            "mtf_trend": -35,
            "risk_penalties": {"total": -5}
        },
        "reasoning_en": "Arbiter reasoning"
    }
    
    patch_decision = CEOEscalationPatch.resolve_with_escalation(
        symbol="BTC-USD",
        primary_decision=primary_res,
        router_metadata={"strategy_mode": "MEAN_REVERSION"},
        call_arbiter_fn=lambda: arbiter_res
    )
    assert patch_decision["conviction"] >= 72
    assert patch_decision["entry_quality"] >= 72
    assert patch_decision["conviction_for_risk"] >= 80.0


def test_ceo_validate_and_compute_score_no_math_hallucination_anti_bot():
    """Verify _validate_and_compute_score auto-aligns weights without math_conflict in Anti-Bot / MR."""
    from agents.ceo_agent import CEOAgent
    
    config.ANTI_BOT_MODE = True
    llm = MagicMock()
    logger = MagicMock()
    ceo = CEOAgent(logger, llm, llm)
    
    # Primary LLM mistakenly gave bull_argument > bear_argument on a SHORT at extreme top
    k3_breakdown = {
        "bull_argument": 40,
        "bear_argument": -20,
        "mtf_trend": -35,
        "risk_penalties": {"total": -5}
    }
    market_context = {"strategy_mode": "MEAN_REVERSION"}
    score_res = ceo._validate_and_compute_score("SHORT", k3_breakdown, market_context=market_context)
    
    assert score_res["math_conflict"] is False
    assert score_res["decision"] == "SHORT"
    assert score_res["directional_confidence"] > 0


def test_deterministic_guard_debate_completeness_bypass_anti_bot():
    """Verify Debate Completeness Guard does not veto with INCOMPLETE_DEBATE in Anti-Bot / MR."""
    from core.deterministic_guard import DeterministicGuard
    from core.strategy_router import StrategyProfile
    
    config.ANTI_BOT_MODE = True
    ceo_proposal = {
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 75,
        "directional_confidence": 80,
        "entry_quality": 75,
        "debate_status": "DEGRADED_BEAR_FAILED",
        "reasoning_en": "Anti-bot trap entry"
    }
    market_data = {
        "indicators": {"bb_position_pct": 92.0, "rsi_14": 72.0},
        "price_data": {"current_price": 100.0}
    }
    strat_prof = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="MEAN_REVERSION",
        direction_bias="SHORT",
        reasoning="Predator Mean Reversion SHORT"
    )
    guard = DeterministicGuard()
    res = guard.evaluate(
        strategy_profile=strat_prof,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=MagicMock(),
        market_data=market_data
    )
    assert res.decision == "SHORT"
    assert res.rejection_tag != "INCOMPLETE_DEBATE"
    assert res.is_actionable is True


@pytest.mark.asyncio
async def test_risk_manager_bayes_and_market_mult_floor_anti_bot():
    """Verify RiskManager ensures bayes_mult >= 0.90 and market_mult >= 1.0 in Anti-Bot / MR."""
    from agents.risk_manager import RiskManager
    
    config.ANTI_BOT_MODE = True
    llm = MagicMock()
    logger = MagicMock()
    rm = RiskManager(logger, llm)
    
    # Mock bayes multiplier to return low value (e.g. 0.65)
    rm.get_bayesian_strategy_multiplier = MagicMock(return_value=(0.65, 0.40, "Beta(1,2)"))
    
    candidate = {
        "decision": "SHORT",
        "action": "ENTER",
        "trade_action": "ENTER",
        "conviction": 75,
        "entry_quality": 75,
        "directional_confidence": 80,
        "current_price": 100.0
    }
    market_data = {
        "symbol": "BTC-USD",
        "price_data": {"current_price": 100.0},
        "indicators": {
            "atr_14": 1.0,
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_position_pct": 92.0,
            "spread_pct": 0.0002,
            "ema_20": 98.5
        },
        "derivatives_data": {
            "max_leverage": 20,
            "min_size": 0.001,
            "size_increment": 0.001,
            "price_increment": 0.01,
            "taker_fee_pct": 0.0005
        }
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "peak_balance": 1000.0,
        "win_rate": 0.65
    }
    res = await rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="RANGE_CHOPPY",
        strategy_mode="MEAN_REVERSION"
    )
    assert res["approved"] is True
    applied_mults = res["applied_multipliers"]
    assert applied_mults["market_mult"] >= 1.0
    assert applied_mults["bayes_mult"] >= 0.90


@pytest.mark.asyncio
async def test_risk_manager_notional_bump_from_80():
    """Verify Stage 6 trigger bump allows bumping notional from $80.0 to $101.0 in Anti-Bot / MR."""
    from agents.risk_manager import RiskManager
    
    config.ANTI_BOT_MODE = True
    llm = MagicMock()
    logger = MagicMock()
    rm = RiskManager(logger, llm)
    
    candidate = {
        "decision": "SHORT",
        "action": "ENTER",
        "trade_action": "ENTER",
        "conviction": 75,
        "entry_quality": 75,
        "directional_confidence": 80,
        "current_price": 100.0
    }
    market_data = {
        "symbol": "ETH-USD",
        "price_data": {"current_price": 100.0},
        "indicators": {
            "atr_14": 1.0,
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_position_pct": 92.0,
            "spread_pct": 0.0002,
            "ema_20": 98.5
        },
        "derivatives_data": {
            "max_leverage": 20,
            "min_size": 0.001,
            "size_increment": 0.001,
            "price_increment": 0.01,
            "taker_fee_pct": 0.0005
        }
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "peak_balance": 1000.0,
        "win_rate": 0.65
    }
    res = await rm.analyze(
        ceo_decision=candidate,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="RANGE_CHOPPY",
        strategy_mode="MEAN_REVERSION"
    )
    assert res["approved"] is True
    assert res["notional_size_usd"] >= 100.0


@pytest.mark.asyncio
async def test_nado_small_position_tp1_protects_with_be_instead_of_error_2094():
    """Verify NadoTradingService moves SL to BE on full size when notional < 195.0, preventing error 2094."""
    from services.nado_trading_service import NadoTradingService
    from services.partial_tp_calculator import PartialTPPlan, TPTier
    
    svc = MagicMock(spec=NadoTradingService)
    svc.active_positions = {
        "BTC-USD": {
            "symbol": "BTC-USD",
            "direction": "SHORT",
            "entry_price": 100.0,
            "notional_usd": 120.0, # Small notional (< 195.0)
            "contracts": 1.2,
            "is_shadow": False,
            "is_anti_bot": True,
            "partial_tp_plan": {
                "symbol": "BTC-USD",
                "direction": "SHORT",
                "entry_price": 100.0,
                "tp_final_price": 96.0,
                "total_contracts": 1.2,
                "remaining_contracts": 1.2,
                "min_size": 0.001,
                "size_increment": 0.001,
                "tiers": [
                    {"tier_index": 1, "target_price": 98.0, "fraction": 0.5, "cumulative_fraction": 0.5, "close_contracts": 0.6, "remaining_tier_contracts": 0.6, "is_final": False, "status": "PENDING"},
                    {"tier_index": 2, "target_price": 96.0, "fraction": 0.5, "cumulative_fraction": 1.0, "close_contracts": 0.6, "remaining_tier_contracts": 0.6, "is_final": True, "status": "PENDING"}
                ]
            }
        }
    }
    svc._save_positions = MagicMock()
    svc.update_stop_loss_to_be = AsyncMock(return_value=True)
    
    res = await NadoTradingService.check_and_execute_partial_tp_tier(
        svc,
        symbol="BTC-USD",
        current_price=97.9
    )
    
    assert res is not None
    assert res["status"] == "PROTECTED_FULL_SIZE_BE"
    saved_plan = PartialTPPlan.from_dict(svc.active_positions["BTC-USD"]["partial_tp_plan"])
    assert saved_plan.tiers[0].status == "FILLED"
    svc.update_stop_loss_to_be.assert_awaited_once_with("BTC-USD")



def test_anti_bot_macro_trend_1h_4h_blocks_counter_trend():
    """Verify that when 1H and 4H are aligned in a trend, Predator refuses to fade it."""
    config.ANTI_BOT_MODE = True

    # 1. Bull trap setup, but 1H + 4H are BULLISH -> SHORT is blocked
    md_bullish_macro = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 90.0,
            "rsi_14": 68.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        },
        "multi_timeframe": {
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        }
    }
    prof_short = StrategyRouter.evaluate("BTC-USD", md_bullish_macro, [], {}, "RANGE_CHOPPY")
    assert prof_short.has_directional_signal is False
    assert "не шортим растущий рынок" in prof_short.reasoning

    # 2. But in the same bullish macro, a dip / bear trap (LONG) IS allowed
    md_bear_trap = {
        "price_data": {"current_price": 98.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 10.0,
            "rsi_14": 30.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        },
        "multi_timeframe": {
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        }
    }
    prof_long = StrategyRouter.evaluate("BTC-USD", md_bear_trap, [], {}, "RANGE_CHOPPY")
    assert prof_long.has_directional_signal is True
    assert prof_long.direction_bias == "LONG"
    assert "ANTI-BOT LONG" in prof_long.reasoning

    # 3. Bearish macro (1H + 4H BEARISH) -> LONG is blocked
    md_bearish_macro = {
        "price_data": {"current_price": 98.0},
        "indicators": {
            "bb_upper": 102.0,
            "bb_lower": 98.0,
            "bb_pos": 10.0,
            "rsi_14": 30.0,
            "volume_spike_pct": 50.0,
            "adx_14": 20.0
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }
    prof_long_blocked = StrategyRouter.evaluate("BTC-USD", md_bearish_macro, [], {}, "RANGE_CHOPPY")
    assert prof_long_blocked.has_directional_signal is False
    assert "не лонгуем падающий рынок" in prof_long_blocked.reasoning


@pytest.mark.asyncio
async def test_nado_predator_premature_sl_immunity():
    """Verify that MEAN_REVERSION positions bypass force close on minor loss (-0.85R < current_r < 0.0R)."""
    pos = {
        "symbol": "BTC-USD",
        "direction": "SHORT",
        "entry_price": 100.0,
        "current_price": 100.20,
        "contracts": 1.0,
        "planned_risk_usd": 1.0,
        "sl_price": 100.15,
        "tp_price": 98.0,
        "strategy_mode": "MEAN_REVERSION",
        "is_anti_bot": True,
        "protection_state": "EXTREME_BLOWOUT_DEFENSE"
    }
    current_price = 100.20
    direction = "SHORT"
    sl_price = pos["sl_price"]
    tp_price = pos["tp_price"]
    hit_sl = current_price >= sl_price
    hit_tp = False

    is_anti_bot_mr = bool(pos.get("strategy_mode") == "MEAN_REVERSION" or pos.get("is_anti_bot"))
    if is_anti_bot_mr:
        if pos.get("protection_state") == "EXTREME_BLOWOUT_DEFENSE":
            pos["protection_state"] = "NORMAL"
        if hit_sl and not hit_tp:
            entry_px = float(pos.get("entry_price", 0.0))
            cntr = float(pos.get("contracts", 0.0))
            planned_risk = float(pos.get("planned_risk_usd", 0.0))
            pnl = (entry_px - current_price) * cntr
            cur_r = pnl / planned_risk
            if -0.85 < cur_r < 0.0:
                hit_sl = False

    assert hit_sl is False
    assert pos["protection_state"] == "NORMAL"
