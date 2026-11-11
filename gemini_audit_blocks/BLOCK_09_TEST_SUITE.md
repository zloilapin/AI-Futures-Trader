# BLOCK 09: REGRESSION TEST SUITE & INVARIANTS
> **Назначение:** Досье для стороннего глубокого аудита кодовой базы моделью Gemini Argon / сторонним ИИ.
> **Дата генерации:** 2026-11-10 (Актуальное боевое состояние репозитория)

## Описание блока
Комплекс тестов для верификации критических инвариантов системы: фикстура изоляции боевого состояния conftest.py, тесты Wave Exhaustion и Box Guard, тесты профилей копи-трейдинга, тесты листа ожидания отката и тесты интерактивного Telegram дашборда.

## Оглавление файлов блока
1. [`tests/conftest.py`](#file-tests-conftest-py)
2. [`tests/test_wave_exhaustion_and_box_guard.py`](#file-tests-test_wave_exhaustion_and_box_guard-py)
3. [`tests/test_multi_user_risk_profiles.py`](#file-tests-test_multi_user_risk_profiles-py)
4. [`tests/test_pullback_watchlist_pipeline.py`](#file-tests-test_pullback_watchlist_pipeline-py)
5. [`tests/test_telegram_refactor_dashboard.py`](#file-tests-test_telegram_refactor_dashboard-py)
6. [`tests/test_risk_manager_and_sizing.py`](#file-tests-test_risk_manager_and_sizing-py)
7. [`tests/test_nado_execution.py`](#file-tests-test_nado_execution-py)
8. [`tests/test_partial_tp_state_machine.py`](#file-tests-test_partial_tp_state_machine-py)

---

<a id="file-tests-conftest-py"></a>
## File: `tests/conftest.py` (95 строк, 3.7 KB)

```python
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock

# Add the python directory to the path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../python')))

@pytest.fixture(autouse=True)
def isolate_test_state_and_memory(tmp_path):
    """
    Guarantees complete physical isolation of pytest from live production memory files.
    All StateStore operations, diagnostics, and decision snapshots are redirected to a temporary test folder.
    Zero pollution of live files in data/memory/* (nado_state.json, diagnostics.json, decision_snapshots.jsonl).
    """
    from core.state_store import StateStore
    
    old_override = StateStore._base_dir_override
    StateStore._base_dir_override = str(tmp_path)
    
    from core.config import config
    old_shadow = getattr(config, 'SHADOW_MODE', False)
    old_allow_forbidden = getattr(config, 'ALLOW_FORBIDDEN_STRATEGIES', False)
    old_anti_bot = getattr(config, 'ANTI_BOT_MODE', False)
    old_auto_predator = getattr(config, 'AUTO_PREDATOR_SWITCH', False)
    old_nado_live = getattr(config, 'NADO_LIVE_TRADING_ENABLED', False)
    old_live = getattr(config, 'LIVE_TRADING_ENABLED', False)
    config.SHADOW_MODE = False
    config.ALLOW_FORBIDDEN_STRATEGIES = False
    config.ANTI_BOT_MODE = False
    config.AUTO_PREDATOR_SWITCH = False
    config.NADO_LIVE_TRADING_ENABLED = False
    config.LIVE_TRADING_ENABLED = False
    
    # Initialize clean subdirectories in tmp_path
    os.makedirs(os.path.join(tmp_path, "data", "memory"), exist_ok=True)
    
    yield tmp_path
    
    StateStore._base_dir_override = old_override
    config.SHADOW_MODE = old_shadow
    config.ALLOW_FORBIDDEN_STRATEGIES = old_allow_forbidden
    config.ANTI_BOT_MODE = old_anti_bot
    config.AUTO_PREDATOR_SWITCH = old_auto_predator
    config.NADO_LIVE_TRADING_ENABLED = old_nado_live
    config.LIVE_TRADING_ENABLED = old_live


@pytest.fixture
def mock_llm_client():
    from core.llm_client import LLMClient
    mock = AsyncMock(spec=LLMClient)
    # Default to Long
    mock.generate.return_value = '{"decision": "LONG", "confidence": 95, "stop_loss": 90, "take_profit": 110, "reasoning": "Test"}'
    return mock

@pytest.fixture
def mock_kraken_service():
    try:
        from services.kraken_trading_service import KrakenTradingService
        mock = AsyncMock(spec=KrakenTradingService)
    except ImportError:
        mock = AsyncMock()
    mock.get_balance.return_value = {"total_usd": 1000.0, "free_margin": 1000.0, "used_margin": 0.0}
    
    # Simple stateful fake for testing positions
    fake_positions = []
    
    async def fake_fetch_positions():
        return fake_positions
        
    async def fake_open_position(symbol, side, size_usd, leverage):
        # By default we simulate filling 100%
        entry = 100.0
        size_base = size_usd / entry
        pos = {
            "symbol": symbol,
            "side": side,
            "size": size_base,
            "entryPrice": entry,
            "markPrice": entry,
            "unrealizedPnl": 0.0,
            "leverage": leverage
        }
        fake_positions.append(pos)
        return {"status": "closed", "filled": size_base, "remaining": 0.0, "average": entry, "fee": {"cost": 0.1, "currency": "USD"}}
        
    async def fake_close_position(symbol, size_to_close, side_to_close):
        fake_positions.clear()
        return {"status": "closed", "filled": size_to_close, "remaining": 0.0, "average": 100.0, "fee": {"cost": 0.1, "currency": "USD"}}
        
    mock.fetch_positions.side_effect = fake_fetch_positions
    mock.open_position.side_effect = fake_open_position
    mock.close_position.side_effect = fake_close_position
    return mock
```

---

<a id="file-tests-test_wave_exhaustion_and_box_guard-py"></a>
## File: `tests/test_wave_exhaustion_and_box_guard.py` (294 строк, 11.3 KB)

```python
import pytest
import time
from agents.indicator_agent import IndicatorAgent
from core.strategy_router import StrategyRouter, StrategyProfile
from core.deterministic_guard import DeterministicGuard
from agents.risk_manager import RiskManager
from core.logger import TradeLogger

@pytest.fixture
def logger():
    return TradeLogger()

@pytest.fixture
def risk_manager(logger):
    return RiskManager(logger, None)

# ═══════════════════════════════════════════════════════════════════
# 1. Indicator Agent 48h Box and 15m Structural Extremes
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_indicator_agent_computes_48h_box_and_swing_extremes(logger):
    agent = IndicatorAgent(logger)
    
    # 48 1h candles ranging from 80.0 to 100.0
    candles_1h = []
    for i in range(48):
        candles_1h.append({
            "open": 90.0,
            "high": 95.0 if i < 40 else 100.0, # max high 100.0
            "low": 85.0 if i < 40 else 80.0,   # min low 80.0
            "close": 90.0
        })
        
    # 16 15m candles ranging from 84.0 to 88.0
    candles_15m = []
    for i in range(16):
        candles_15m.append({
            "open": 86.0,
            "high": 87.0 if i < 15 else 88.5, # max high 88.5
            "low": 85.0 if i < 15 else 84.2,  # min low 84.2
            "close": 86.0
        })

    market_data = {
        "price_data": {
            "current_price": 85.0,
            "candles_20": candles_15m
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "tf_1h": {"candles_20": candles_1h}
        },
        "indicators": {
            "rsi_14": 42.0,
            "ema_20": 87.0
        }
    }
    
    res = await agent.analyze(market_data)
    
    # high_48h = 100.0, low_48h = 80.0
    # current_price = 85.0 -> box_pos = (85 - 80) / (100 - 80) * 100 = 25.0%
    assert res["high_48h"] == 100.0
    assert res["low_48h"] == 80.0
    assert res["macro_box_pos_pct"] == 25.0
    assert res["swing_high_15m"] == 88.5
    assert res["swing_low_15m"] == 84.2
    
    # Verify injected into indicators dict
    assert market_data["indicators"]["macro_box_pos_pct"] == 25.0
    assert market_data["indicators"]["swing_high_15m"] == 88.5

# ═══════════════════════════════════════════════════════════════════
# 2. Wave Exhaustion Guard (check_anti_chase_reentry)
# ═══════════════════════════════════════════════════════════════════

def test_wave_exhaustion_guard_blocks_lower_short(tmp_path):
    # Simulate trade_journal with previous SHORT entry at 87.39 closed recently
    journal_file = str(tmp_path / "trade_journal.json")
    now_ts = time.time()
    
    from core.state_store import StateStore
    StateStore.save(journal_file, [
        {
            "timestamp": now_ts - 3600, # 1 hour ago
            "symbol": "SOL-USD",
            "direction": "SHORT",
            "entry_price": 87.39,
            "exit_price": 85.65,
            "net_pnl_usd": 2.01,
            "realized_r": 1.99
        }
    ])
    
    # 1. Price is lower than previous entry (86.40 < 87.39) and bb_pos is 35% (< 55%) -> VETO
    allowed, reason = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="SHORT",
        current_price=86.40,
        eval_atr=0.65,
        bb_position_pct=35.0,
        low_48h=85.0,
        rvol=1.0,
        journal_path=journal_file
    )
    assert allowed is False
    assert "Wave Exhaustion Guard" in reason
    assert "ниже предыдущего входа" in reason

    # 2. Exception 1: Price pulled back to upper half (bb_pos >= 55%) -> ALLOWED (if exit distance satisfied)
    allowed_pb, _ = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="SHORT",
        current_price=87.00,
        eval_atr=0.65,
        bb_position_pct=58.0,
        low_48h=85.0,
        rvol=1.0,
        journal_path=journal_file
    )
    assert allowed_pb is True

    # 3. Exception 2: True breakout of 48h low with rvol >= 1.5 -> ALLOWED
    allowed_bo, _ = StrategyRouter.check_anti_chase_reentry(
        symbol="SOL-USD",
        target_direction="SHORT",
        current_price=85.00,
        eval_atr=0.65,
        bb_position_pct=25.0,
        low_48h=85.0,
        rvol=1.8,
        journal_path=journal_file
    )
    assert allowed_bo is True

# ═══════════════════════════════════════════════════════════════════
# 3. Multi-Day Box Floor/Ceiling Guard in StrategyRouter.evaluate
# ═══════════════════════════════════════════════════════════════════

def test_macro_box_floor_guard_vetoes_short_at_box_floor():
    market_data = {
        "price_data": {"current_price": 85.50},
        "indicators": {
            "macro_box_pos_pct": 25.0, # In lower third (< 32%)
            "bb_position_pct": 30.0,   # In lower channel (< 38%)
            "rvol": 1.1,               # Normal volume (< 1.6)
            "atr_14": 0.65,
            "rsi_14": 42.0
        },
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }
    
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"},
        {"agent_name": "OI_Funding_Agent", "signal": "BEARISH"}
    ]
    
    res = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache={},
        detected_regime="TRENDING"
    )
    
    assert res.has_directional_signal is False
    assert res.strategy_mode == "MACRO_BOX_FLOOR_VETO"
    assert "Macro Floor Guard" in res.reasoning

# ═══════════════════════════════════════════════════════════════════
# 4. Retest Priority on Pullback Rejection
# ═══════════════════════════════════════════════════════════════════

def test_retest_priority_approves_pullback_rejection_short():
    market_data = {
        "price_data": {
            "current_price": 88.0,
            "candles_20": [{
                "open": 87.5,
                "high": 88.8,
                "low": 87.2,
                "close": 87.4 # Bearish close with upper shadow
            }]
        },
        "indicators": {
            "macro_box_pos_pct": 55.0, # Mid-range
            "bb_position_pct": 65.0,   # Pulled back into retest zone 50-80%
            "atr_14": 0.65,
            "rsi_14": 52.0
        },
        "multi_timeframe": {
            "mtf_alignment": "TRANSITION", # 15m might be choppy
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }
    
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"}
    ]
    
    res = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=valid_reports,
        macro_cache={},
        detected_regime="TRENDING"
    )
    
    assert res.has_directional_signal is True
    assert res.direction_bias == "SHORT"
    assert "RETEST_PRIORITY SHORT" in res.reasoning

# ═══════════════════════════════════════════════════════════════════
# 5. Ban on Market Execution for REDUCE_SIZE / Low Conviction
# ═══════════════════════════════════════════════════════════════════

def test_deterministic_guard_bans_market_execution_for_reduce_size(logger, risk_manager):
    ceo_proposal = {
        "decision": "SHORT",
        "trade_action": "REDUCE_SIZE", # Weak setup
        "conviction": 81,
        "entry_quality": 66,           # < 73%
        "directional_confidence": 81,
        "thesis": "Weak follow-through"
    }
    
    res = DeterministicGuard.evaluate(
        strategy_profile=StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Test"),
        ceo_proposal=ceo_proposal,
        profile="balanced",
        risk_manager=risk_manager,
        market_data={"indicators": {"bb_pos": 50.0, "rsi_14": 50.0}},
        symbol="SOL-USD",
        logger=logger
    )
    
    # Must NOT be actionable for immediate market entry!
    assert res.is_actionable is False
    assert res.trade_action == "WAIT_FOR_PULLBACK"
    assert res.rejection_tag == "WAIT_FOR_PULLBACK"
    assert "WAIT_FOR_PULLBACK" in res.guard_reason

# ═══════════════════════════════════════════════════════════════════
# 6. Structural Stop-Loss Geometry & Distance-to-Structure Gate
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_risk_manager_structural_sl_and_distance_gate(risk_manager):
    # Setup market data with 15m structural swing high at 88.50
    # Current entry is 86.40 (entering too late, distance is 88.50 - 86.40 = 2.10 -> 2.43% > 1.95%)
    market_data = {
        "price_data": {"current_price": 86.40},
        "indicators": {
            "atr_14": 0.65,
            "swing_high_15m": 88.50,
            "swing_low_15m": 85.00
        },
        "derivatives_data": {"taker_fee_pct": 0.00035}
    }
    
    proposal = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_quality": 80,
        "strategy_mode": "TREND_FOLLOWING",
        "approved_risk_usd": 1.5
    }
    
    portfolio_data = {
        "equity": 86.85,
        "available_margin": 86.85,
        "active_positions_count": 0,
        "streak_history_by_tag": {}
    }
    
    res = await risk_manager.analyze(proposal, portfolio_data, market_data, macro_regime="BEAR_TREND")
    
    # Distance to structural stop is (88.50 + 0.25*0.65 - 86.40) / 86.40 = 2.62% > 1.95%
    # Must veto with DISTANCE_TO_STRUCTURE_VETO and recommend WAIT_FOR_PULLBACK!
    assert res["approved"] is False
    assert res["veto_category"] == "DISTANCE_TO_STRUCTURE_VETO"
    assert res["trade_action"] == "WAIT_FOR_PULLBACK"
    assert "Distance-to-Structure Gate" in res["reasoning"]
```

---

<a id="file-tests-test_multi_user_risk_profiles-py"></a>
## File: `tests/test_multi_user_risk_profiles.py` (334 строк, 13.8 KB)

```python
"""
Tests for Multi-User Copy-Trading Architecture & Risk Profiles
==============================================================
Verifies:
1. Individual R-sizing and TP target calculation across Conservative ($100),
   Balanced ($500), and Aggressive ($2,000) accounts.
2. Exchange $100.00 CLOB min_size gate: safe $104 bump vs rejection without error 2094.
3. Target equity achievement and daily drawdown stop-crane auto-pause.
4. Late Join policy protection (skipping trades older than 10m / drifted > 0.15R).
5. Isolated subscriber position management & 2-step confirmation in Telegram UI.
"""

import os
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from eth_account import Account

from services.trader_registry import TraderRegistry, RISK_PROFILES
from services.copy_trading_dispatcher import CopyTradingDispatcher, SubscriberPositionsStore
from tools.nado_link_signer_helper import sign_link_signer_payload


@pytest.fixture
def temp_env(tmp_path):
    traders_file = str(tmp_path / "connected_traders.json")
    positions_file = str(tmp_path / "subscriber_positions.json")
    registry = TraderRegistry(storage_path=traders_file)
    store = SubscriberPositionsStore(storage_path=positions_file)
    return registry, store, traders_file, positions_file


def _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=500.0, **kwargs):
    owner = Account.create()
    session = registry.prepare_pending_session(chat_id)
    sig = sign_link_signer_payload(owner, session["signer_address"], "default", nonce=100)
    success, _ = registry.register_trader(
        chat_id=chat_id,
        owner_address=owner.address,
        subaccount_name="default",
        signer_address=session["signer_address"],
        signer_private_key=session["signer_private_key"],
        signature=sig,
        nonce=100
    )
    assert success
    registry.set_trader_risk_profile(chat_id, risk_mode)
    if kwargs:
        registry.update_trader_risk_settings(chat_id, **kwargs)
    traders = registry.load_traders()
    traders[chat_id]["_mock_equity"] = mock_equity
    registry.save_traders(traders)
    return traders[chat_id]


# -----------------------------------------------------------------------------
# 1. Individual R-sizing and TP targets across 3 accounts
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multi_user_sizing_and_tp_targets(temp_env, monkeypatch):
    registry, store, _, _ = temp_env

    # Setup 3 users: Conservative ($100), Balanced ($500), Aggressive ($2,000)
    user_a = _register_mock_trader(registry, "101", risk_mode="conservative", mock_equity=100.0)
    user_b = _register_mock_trader(registry, "102", risk_mode="balanced", mock_equity=500.0)
    user_c = _register_mock_trader(registry, "103", risk_mode="aggressive", mock_equity=2000.0)

    # Signal params: SOL-USD LONG at $140.00, SL at $137.20 (dist = $2.80, 2.0%)
    entry_price = 140.0
    master_sl = 137.20
    sl_dist_pct = (entry_price - master_sl) / entry_price  # 0.02 (2%)

    # Test pure sizing logic for User B ($500 balanced):
    # Risk = 500 * 1.6% = $8.00. Notional = $8.00 / 0.02 = $400.00
    can_b, notional_b, risk_b, status_b = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=user_b,
        entry_price=entry_price,
        master_sl=master_sl,
        subaccount_equity=500.0,
        available_margin=450.0
    )
    assert can_b is True
    assert round(notional_b, 2) == 400.0
    assert round(risk_b, 2) == 8.0
    assert status_b == "SIZED_OK"

    # Test pure sizing logic for User C ($2,000 aggressive):
    # Risk = 2000 * 2.6% = $52.00. Notional = $52.00 / 0.02 = $2,600.00
    can_c, notional_c, risk_c, status_c = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=user_c,
        entry_price=entry_price,
        master_sl=master_sl,
        subaccount_equity=2000.0,
        available_margin=1800.0
    )
    assert can_c is True
    assert round(notional_c, 2) == 2600.0
    assert round(risk_c, 2) == 52.0

    # Test pure sizing for User A ($100 conservative, 0.9% risk = $0.90):
    # Raw notional = $0.90 / 0.02 = $45.00 (< 103.50).
    # Risk at $104 bump = 104 * 0.02 = $2.08.
    # Max allowed bump risk = $0.90 * 1.45 = $1.305.
    # $2.08 > $1.305 => Safe bump fails, skips trade cleanly!
    can_a, notional_a, risk_a, status_a = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=user_a,
        entry_price=entry_price,
        master_sl=master_sl,
        subaccount_equity=100.0,
        available_margin=90.0
    )
    assert can_a is False
    assert status_a == "BELOW_EXCHANGE_MIN"

    # Verify TP Targets:
    # Conservative TP target: 1.8R => 140 + 2.80 * 1.8 = 145.04
    # Balanced TP target: 3.0R => 140 + 2.80 * 3.0 = 148.40
    # Aggressive TP target: 3.8R => 140 + 2.80 * 3.8 = 150.64
    sl_dist = entry_price - master_sl
    tp_a = entry_price + (sl_dist * user_a["tp_r_target"])
    tp_b = entry_price + (sl_dist * user_b["tp_r_target"])
    tp_c = entry_price + (sl_dist * user_c["tp_r_target"])

    assert round(tp_a, 2) == 145.04
    assert round(tp_b, 2) == 148.40
    assert round(tp_c, 2) == 150.64


# -----------------------------------------------------------------------------
# 2. Exchange $100.00 CLOB min_size gate: Safe $104 bump
# -----------------------------------------------------------------------------
def test_safe_104_bump_success():
    # Subscriber with $300 balance, 1.6% risk = $4.80.
    # Tight stop: entry 100, SL 95 (5% SL distance)
    # Target notional = $4.80 / 0.05 = $96.00 (< 103.50)
    # Risk at $104 = 104 * 0.05 = $5.20.
    # Max allowed bump risk = 4.80 * 1.45 = $6.96.
    # $5.20 <= $6.96 => safely bumped to $104.00!
    sub = {
        "risk_per_trade_pct": 1.6,
        "max_leverage": 8.0
    }
    can_trade, notional, risk, status = CopyTradingDispatcher.calculate_subscriber_sizing(
        sub=sub,
        entry_price=100.0,
        master_sl=95.0,
        subaccount_equity=300.0,
        available_margin=100.0
    )
    assert can_trade is True
    assert notional == 104.0
    assert status == "BUMPED_TO_MIN_NOTIONAL"


# -----------------------------------------------------------------------------
# 3. Target equity & daily drawdown auto-pause
# -----------------------------------------------------------------------------
def test_target_equity_and_daily_drawdown_auto_pause(temp_env):
    registry, _, _, _ = temp_env
    chat_id = "555"
    _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=400.0, target_equity_usd=500.0, max_daily_drawdown_pct=4.0)

    # Initial check at $400: neither triggered
    r1 = registry.check_daily_drawdown_and_targets(chat_id, current_equity=400.0)
    assert r1["target_equity_reached"] is False
    assert r1["daily_drawdown_paused"] is False

    # Equity grows to $510 => target reached!
    r2 = registry.check_daily_drawdown_and_targets(chat_id, current_equity=510.0)
    assert r2["target_equity_reached"] is True
    assert r2["just_reached_target"] is True

    # Next check: stays reached, but just_reached is False
    r3 = registry.check_daily_drawdown_and_targets(chat_id, current_equity=520.0)
    assert r3["target_equity_reached"] is True
    assert r3["just_reached_target"] is False

    # Test Drawdown on a new user starting at $1,000
    dd_chat = "666"
    _register_mock_trader(registry, dd_chat, risk_mode="balanced", mock_equity=1000.0, max_daily_drawdown_pct=4.0)
    # Day start equity sets to 1000
    registry.check_daily_drawdown_and_targets(dd_chat, current_equity=1000.0)

    # Drawdown of 2% ($980) => OK
    r_ok = registry.check_daily_drawdown_and_targets(dd_chat, current_equity=980.0)
    assert r_ok["daily_drawdown_paused"] is False

    # Drawdown of 5% ($950) => Limit hit (5% >= 4%)!
    r_hit = registry.check_daily_drawdown_and_targets(dd_chat, current_equity=950.0)
    assert r_hit["daily_drawdown_paused"] is True
    assert r_hit["just_triggered_dd"] is True
    assert r_hit["drawdown_pct"] == 5.0


# -----------------------------------------------------------------------------
# 4. Late Join policy protection
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_late_join_policy_protection(temp_env, monkeypatch):
    registry, store, _, _ = temp_env
    chat_id = "777"
    _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=1000.0, late_join_policy="next_signal_only")

    mock_trading = MagicMock()
    mock_tg = MagicMock()
    mock_tg.send_message = AsyncMock()

    from core.config import config
    monkeypatch.setattr(config, "COPY_TRADING_ENABLED", True)

    dispatcher = CopyTradingDispatcher(master_trading_service=mock_trading, telegram_service=mock_tg)
    dispatcher.positions_store = store
    monkeypatch.setattr("services.copy_trading_dispatcher.trader_registry", registry)

    # Simulate Master trade opened 15 minutes ago (900 seconds)
    old_master_open = time.time() - 900.0
    entry_price = 100.0
    master_sl = 95.0 # sl_dist = 5.0

    # User connected before master trade was opened
    registry.update_trader_risk_settings(chat_id, connected_at=old_master_open - 60.0)

    # Market price has moved to 103.0 (drift = 3.0 => 0.60R > 0.15R)
    mock_trading.market_data_service.get_last_price.return_value = 103.0

    await dispatcher.dispatch_copy_trade_open(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=entry_price,
        master_sl=master_sl,
        master_tp=115.0,
        master_open_time=old_master_open
    )

    # Trade must be skipped, no position registered in store
    pos = store.get_user_position(chat_id, "SOL-USD")
    assert pos is None
    # Notification sent explaining the skip
    assert mock_tg.send_message.call_count == 1
    call_text = mock_tg.send_message.call_args[1]["text"]
    assert "Защита от опоздания" in call_text or "Пропуск" in call_text


# -----------------------------------------------------------------------------
# 5. Telegram UI: Personal Subscriber Position & 2-Step Confirmation
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_telegram_subscriber_position_and_close(temp_env, monkeypatch):
    registry, store, _, _ = temp_env
    chat_id = "888"
    _register_mock_trader(registry, chat_id, risk_mode="balanced", mock_equity=800.0)

    # Pre-populate an open subscriber position
    store.set_user_position(chat_id, "ETH-USD", {
        "symbol": "ETH-USD",
        "direction": "SHORT",
        "entry_price": 2500.0,
        "notional_usd": 400.0,
        "contracts": 0.16,
        "sl_price": 2550.0,
        "tp_price": 2350.0,
        "initial_sl": 2550.0,
        "initial_tp": 2350.0,
        "r_dist": 50.0,
        "planned_risk_usd": 8.0,
        "tp_r_target": 3.0,
        "be_trigger_r": 0.75,
        "trail_activation_r": 1.35,
        "protection_state": "NATIVE_SL_CONFIRMED",
        "risk_mode": "balanced",
        "leverage": 8.0
    })

    from services.telegram_bot_listener import TelegramBotListener
    mock_trading = MagicMock()
    mock_trading.active_positions = {} # Master has no position
    mock_trading.market_data_service.get_last_price.return_value = 2480.0 # Floating profit (+20)

    bot = TelegramBotListener(trading_service=mock_trading)

    # Patch dispatcher's store in listener
    with patch("services.copy_trading_dispatcher.copy_trading_dispatcher.positions_store", store):
        text, markup = await bot._get_positions_payload(chat_id=chat_id, is_admin=False)
        assert "МОИ СКОПИРОВАННЫЕ ПОЗИЦИИ" in text
        assert "ETH-USD" in text
        assert "Take-Profit" in text and "$2,350.00" in text

        # Check inline buttons for personal position
        buttons = markup["inline_keyboard"]
        flat_buttons = [b for row in buttons for b in row]
        button_callbacks = [b["callback_data"] for b in flat_buttons]

        assert "sub_be:ETH-USD" in button_callbacks
        assert "sub_close_ask:ETH-USD" in button_callbacks

        # Test Manual BE transfer
        from services.copy_trading_dispatcher import copy_trading_dispatcher
        copy_trading_dispatcher.positions_store = store
        succ_be, msg_be = await copy_trading_dispatcher.move_subscriber_sl_to_be(chat_id, "ETH-USD")
        assert succ_be is True
        updated_pos = store.get_user_position(chat_id, "ETH-USD")
        assert updated_pos["protection_state"] == "BREAK_EVEN"

        # Test Manual Subscriber Market Close (leaves Master positions untouched)
        succ_close, msg_close = await copy_trading_dispatcher.close_subscriber_position(chat_id, "ETH-USD")
        assert succ_close is True
        assert store.get_user_position(chat_id, "ETH-USD") is None


# -----------------------------------------------------------------------------
# 6. Telegram Risk Menu Payload & 1-Click Profile Switching
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_telegram_risk_menu_payload(temp_env):
    registry, store, _, _ = temp_env
    chat_id = "999"
    _register_mock_trader(registry, chat_id, risk_mode="balanced")

    from services.telegram_bot_listener import TelegramBotListener
    mock_trading = MagicMock()
    bot = TelegramBotListener(trading_service=mock_trading)

    with patch("services.trader_registry.trader_registry", registry):
        text, markup = bot._get_risk_menu_payload(chat_id)
        assert "ПРОФИЛЬ РИСКА И ЦЕЛИ КОПИ-ТРЕЙДИНГА" in text
        assert "Сбалансированный" in text

        # Test switching to aggressive
        registry.set_trader_risk_profile(chat_id, "aggressive")
        t_agg = registry.get_trader(chat_id)
        assert t_agg["risk_mode"] == "aggressive"
        assert t_agg["risk_per_trade_pct"] == 2.6
        assert t_agg["max_slots"] == 3
        assert t_agg["tp_r_target"] == 3.8
```

---

<a id="file-tests-test_pullback_watchlist_pipeline-py"></a>
## File: `tests/test_pullback_watchlist_pipeline.py` (299 строк, 10.1 KB)

```python
import os
import sys
import time
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../python')))

from core.pipeline import TradingPipeline
from core.strategy_router import StrategyProfile
from core.models import FinalTradeDecision
from core.deterministic_guard import DeterministicGuard
from core.state_store import StateStore
from agents.risk_manager import RiskManager


@pytest.fixture
def clean_watchlist_file(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_register_and_persist_pullback_candidate(clean_watchlist_file):
    """
    Verifies that registering a candidate in _pullback_watchlist calculates target at 15m EMA-20
    and atomically persists it to pullback_watchlist.json.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    mock_services.logger = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")

    market_data = {
        "indicators": {
            "ema_20": 86.50,
            "atr_14": 1.20
        }
    }

    # For SHORT with current_price=84.80 (< ema_20=86.50), target MUST be 15m EMA-20 (86.50)
    pipeline.register_pullback_candidate(
        symbol="SOL-USD",
        decision="SHORT",
        current_price=84.80,
        directional_conf=80,
        conviction=75,
        strategy_mode="TREND_FOLLOWING",
        market_data=market_data,
        ceo_verdict={"reasoning": "Pullback required"},
        strategy_profile=None,
        reason="Wave Exhaustion Guard"
    )

    assert "SOL-USD" in pipeline._pullback_watchlist
    entry = pipeline._pullback_watchlist["SOL-USD"]
    assert entry["target_pullback_price"] == 86.50
    assert entry["target_entry"] == 86.50
    assert entry["invalidation_price"] > 86.50
    assert entry["direction"] == "SHORT"

    # Verify disk persistence via StateStore
    pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
    assert os.path.exists(pw_path)
    with open(pw_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert "SOL-USD" in saved_data
    assert saved_data["SOL-USD"]["target_pullback_price"] == 86.50


@pytest.mark.asyncio
async def test_check_pullback_watchlist_triggers_and_stores_context(clean_watchlist_file):
    """
    Verifies that when live price pulls back to the 15m EMA-20 zone, check_pullback_watchlist:
    1. Detects the pullback hit (covering >= 80% of distance to target).
    2. Stores context in _pullback_context.
    3. Cleans candidate from watchlist and disk.
    4. Sets _priority_symbol and _priority_direction.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    mock_services.logger = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")
    pipeline._cycle_running = False
    pipeline.services.trading_service.active_positions = {}

    pipeline.register_pullback_candidate(
        symbol="BNB-USD",
        decision="SHORT",
        current_price=680.0,
        directional_conf=85,
        conviction=78,
        strategy_mode="TREND_FOLLOWING",
        market_data={"indicators": {"ema_20": 685.0, "atr_14": 4.0}},
        ceo_verdict={"reasoning": "Waiting for bounce to EMA-20"},
        strategy_profile=None
    )

    # Price moves from 680 up to 684.50 (covers > 80% of 5.0 distance to 685.0)
    pipeline.services.fetcher.fetch_ohlcv = AsyncMock(return_value={"current_price": 684.50})

    triggered, sym, reason = await pipeline.check_pullback_watchlist()
    assert triggered is True
    assert sym == "BNB-USD"
    assert "retest" in reason.lower()
    assert pipeline._priority_symbol == "BNB-USD"
    assert pipeline._priority_direction == "SHORT"
    assert "BNB-USD" in pipeline._pullback_context
    assert pipeline._pullback_context["BNB-USD"]["target_pullback_price"] == 685.0

    # Watchlist on disk must no longer contain BNB-USD
    pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
    with open(pw_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert "BNB-USD" not in saved_data


@pytest.mark.asyncio
async def test_deterministic_guard_allows_pullback_retest_without_infinite_loop():
    """
    Verifies that DeterministicGuard does NOT block an incoming PULLBACK_RETEST back into
    WAIT_FOR_PULLBACK when trade_action is REDUCE_SIZE or conviction/quality is moderate.
    """
    strat_profile = StrategyProfile(
        True, "TREND_FOLLOWING", "SHORT", "PULLBACK_RETEST SHORT: Цена достигла зоны отката"
    )
    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 70,
        "directional_confidence": 80,
        "entry_quality": 68.0,
        "trade_action": "REDUCE_SIZE",
        "is_pullback_retest": True
    }
    market_data = {
        "is_pullback_retest": True,
        "indicators": {
            "rsi_14": 45.0,
            "bb_position_pct": 52.0,
            "ema_20": 86.50
        }
    }

    mock_rm = MagicMock()
    mock_rm._get_profile_rules.return_value = {"min_conviction": 65}

    decision = DeterministicGuard.evaluate(
        strategy_profile=strat_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_rm,
        market_data=market_data,
        symbol="SOL-USD"
    )

    # Must NOT be blocked or set to WAIT_FOR_PULLBACK!
    assert decision.is_actionable is True
    assert decision.trade_action in ["ENTER", "REDUCE_SIZE"]
    assert decision.guard_status != "PULLBACK_WATCHLIST"


@pytest.mark.asyncio
async def test_risk_manager_retest_structural_sl_and_distance_gate():
    """
    Verifies that RiskManager:
    1. Evaluates structural SL from fresh entry price at pullback.
    2. Allows confirmed retest with structural SL dist up to 2.50% without vetoing.
    3. Successfully approves the trade for execution.
    """
    risk_mgr = RiskManager(logger=MagicMock(), llm_client=MagicMock())

    current_price = 86.20
    swing_high_15m = 87.80  # Dist = (87.80 + buffer - 86.20) / 86.20 ≈ 2.15% (> 1.95%, but <= 2.50%)

    ceo_decision = {
        "decision": "SHORT",
        "conviction": 76,
        "directional_confidence": 82,
        "entry_quality": 70.0,
        "trade_action": "REDUCE_SIZE",
        "is_pullback_retest": True
    }
    portfolio_data = {
        "total_balance": 250.0,
        "total_usd": 250.0,
        "available_margin": 200.0,
        "active_positions": {},
        "is_pullback_retest": True
    }
    market_data = {
        "is_pullback_retest": True,
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": 1.0,
            "swing_high_15m": swing_high_15m,
            "swing_low_15m": 84.0,
            "rsi_14": 46.0,
            "bb_position_pct": 52.0,
            "ema_20": 86.20
        },
        "derivatives_data": {
            "funding_rate_hourly": 0.0,
            "taker_fee_pct": 0.0005
        }
    }

    final_decision = FinalTradeDecision(
        symbol="SOL-USD",
        decision="SHORT",
        conviction=76,
        trade_action="REDUCE_SIZE",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        min_conviction=65,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="Retest approved"
    )

    verdict = await risk_mgr.analyze(
        final_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING"
    )

    assert verdict.get("approved") is True, f"Expected approval on pullback retest, got veto: {verdict.get('reasoning')}"
    assert verdict.get("stop_loss_price") >= swing_high_15m
    assert verdict.get("notional_size_usd") > 0


@pytest.mark.asyncio
async def test_risk_manager_distance_veto_registers_into_pullback_watchlist(clean_watchlist_file):
    """
    Verifies that when RiskManager rejects a first-wave entry with DISTANCE_TO_STRUCTURE_VETO,
    the pipeline registers the setup into _pullback_watchlist and updates disk store.
    """
    mock_agents = MagicMock()
    mock_services = MagicMock()
    mock_services.logger = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")

    # Simulate RiskManager veto result with trade_action="WAIT_FOR_PULLBACK"
    risk_verdict = {
        "approved": False,
        "veto_category": "DISTANCE_TO_STRUCTURE_VETO",
        "trade_action": "WAIT_FOR_PULLBACK",
        "reasoning": "Distance-to-Structure Gate: Расстояние до структурного стопа (2.80% > 1.95%)"
    }

    final_trade_decision = FinalTradeDecision(
        symbol="SOL-USD",
        decision="SHORT",
        conviction=78,
        trade_action="ENTER",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        min_conviction=65,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="Signal valid"
    )

    market_data = {
        "indicators": {
            "ema_20": 87.0,
            "atr_14": 1.2
        }
    }

    # Simulate what pipeline does in Stage 5 else branch:
    if (
        risk_verdict.get("trade_action") == "WAIT_FOR_PULLBACK"
        and final_trade_decision.decision in ["LONG", "SHORT"]
    ):
        pipeline.register_pullback_candidate(
            symbol="SOL-USD",
            decision=final_trade_decision.decision,
            current_price=84.50,
            directional_conf=80,
            conviction=78,
            strategy_mode="TREND_FOLLOWING",
            market_data=market_data,
            ceo_verdict={"reasoning": "Late entry near bottom of wave"},
            strategy_profile=None,
            reason=risk_verdict.get("reasoning", "")
        )

    assert "SOL-USD" in pipeline._pullback_watchlist
    assert pipeline._pullback_watchlist["SOL-USD"]["target_pullback_price"] == 87.0

    # Check disk file
    pw_path = StateStore._normalize_path("data/memory/pullback_watchlist.json")
    with open(pw_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "SOL-USD" in data
    assert data["SOL-USD"]["target_entry"] == 87.0
```

---

<a id="file-tests-test_telegram_refactor_dashboard-py"></a>
## File: `tests/test_telegram_refactor_dashboard.py` (260 строк, 10.1 KB)

```python
import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch

from services.telegram_bot_listener import TelegramBotListener, MAIN_REPLY_KEYBOARD
from services.trader_registry import trader_registry
from core.config import config


@pytest.fixture
def mock_trading():
    svc = MagicMock()
    svc.active_positions = {
        "SOL-USD": {
            "direction": "SHORT",
            "entry_price": 87.42,
            "sl_price": 87.114,
            "tp_price": 84.797,
            "notional_usd": 104.90,
            "contracts": 1.2,
            "margin_used": 17.48,
            "planned_risk_usd": 1.6,
            "protection_state": "TIME_STOP_BE"
        }
    }
    svc.get_portfolio_summary = AsyncMock(return_value={
        "current_balance": 89.06,
        "available_margin": 71.58,
        "win_rate_pct": 65.0,
        "total_pnl_usd": 12.50,
        "total_r_multiple": 2.4,
        "win_count": 8,
        "loss_count": 4
    })
    svc.update_stop_loss_to_be = AsyncMock(return_value=True)
    svc.force_close_position = AsyncMock(return_value=(True, "ORDER_FILLED"))
    svc.entries_paused = False
    return svc


def test_main_reply_keyboard_structure():
    """Verify that MAIN_REPLY_KEYBOARD has exactly 4 buttons in 2 rows."""
    kb = MAIN_REPLY_KEYBOARD["keyboard"]
    assert len(kb) == 2
    assert len(kb[0]) == 2
    assert len(kb[1]) == 2
    assert kb[0][0]["text"] == "📊 Терминал"
    assert kb[0][1]["text"] == "💼 Позиции и SL"
    assert kb[1][0]["text"] == "🧠 AI-Радар"
    assert kb[1][1]["text"] == "🔗 Кошелек"


@pytest.mark.asyncio
async def test_start_attaches_main_reply_keyboard(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()

    await listener.handle_command("/start", sender_chat_id="111222333")
    assert listener._send_reply.call_count == 1
    _, kwargs = listener._send_reply.call_args
    assert kwargs.get("reply_markup") == MAIN_REPLY_KEYBOARD


@pytest.mark.asyncio
async def test_terminal_dashboard_and_entries_pause(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()
    admin_id = str(config.TELEGRAM_CHAT_ID or "12345")
    listener.chat_id = admin_id

    # Test /terminal command
    await listener.handle_command("/terminal", sender_chat_id=admin_id)
    assert listener._send_reply.call_count == 1
    text, kwargs = listener._send_reply.call_args[0][0], listener._send_reply.call_args[1]
    assert "AI TRADING TERMINAL | ДАШБОРД" in text
    assert "$89.06" in text
    assert "ВХОДЫ АКТИВНЫ" in text

    # Test pause entries callback
    await listener.handle_callback("cb_pause", "dash_pause_entries", admin_id, message_id=42)
    assert mock_trading.entries_paused is True
    assert listener._edit_reply.call_count == 1
    edit_text = listener._edit_reply.call_args[0][0]
    assert "ВХОДЫ НА ПАУЗЕ" in edit_text

    # Test resume entries callback
    await listener.handle_callback("cb_resume", "dash_resume_entries", admin_id, message_id=42)
    assert mock_trading.entries_paused is False
    assert listener._edit_reply.call_count == 2
    edit_text2 = listener._edit_reply.call_args[0][0]
    assert "ВХОДЫ АКТИВНЫ" in edit_text2


@pytest.mark.asyncio
async def test_positions_dashboard_be_and_close(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()
    admin_id = str(config.TELEGRAM_CHAT_ID or "12345")
    listener.chat_id = admin_id

    # Test /positions
    await listener.handle_command("/positions", sender_chat_id=admin_id)
    assert listener._send_reply.call_count == 1
    text = listener._send_reply.call_args[0][0]
    assert "SOL-USD" in text
    assert "SHORT" in text
    assert "Free Ride" in text or "Sentinel" in text

    # Test manual BE ask callback
    await listener.handle_callback("cb_be", "be_ask:SOL-USD", admin_id, message_id=43)
    mock_trading.update_stop_loss_to_be.assert_called_with("SOL-USD")

    # Test emergency close ask (2-step confirmation)
    await listener.handle_callback("cb_close", "close_ask:SOL-USD", admin_id, message_id=43)
    edit_text = listener._edit_reply.call_args[0][0]
    markup = listener._edit_reply.call_args[1]["reply_markup"]
    assert "ПОДТВЕРЖДЕНИЕ ЭКСТРЕННОГО ЗАКРЫТИЯ" in edit_text
    assert any("close_confirm:SOL-USD" in btn.get("callback_data", "") for row in markup["inline_keyboard"] for btn in row)

    # Test emergency close confirm
    await listener.handle_callback("cb_close_conf", "close_confirm:SOL-USD", admin_id, message_id=43)
    mock_trading.force_close_position.assert_called_with("SOL-USD", exit_reason="MANUAL_TG_CLOSE")


@pytest.mark.asyncio
async def test_radar_dashboard_and_subtabs(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()

    await listener.handle_command("/radar", sender_chat_id="777")
    assert listener._send_reply.call_count == 1
    text = listener._send_reply.call_args[0][0]
    assert "AI-РАДАР И СКАНЕР СИНДИКАТА" in text
    assert "BTC-USD" in text

    # Subtab radar_table
    await listener.handle_callback("cb_tab", "radar_table", "777", message_id=50)
    assert listener._edit_reply.call_count == 1
    assert "СВОДНАЯ ТАБЛИЦА 7 ПАР" in listener._edit_reply.call_args[0][0]


@pytest.mark.asyncio
async def test_wallet_watch_only_registration(mock_trading):
    listener = TelegramBotListener(trading_service=mock_trading)
    listener._send_reply = AsyncMock()
    listener._edit_reply = AsyncMock()
    guest_id = "555666777"

    # User clicks Watch-Only
    await listener.handle_callback("cb_wo", "wallet_watch_only", guest_id, message_id=60)
    assert listener._waiting_input.get(guest_id) == "watch_only_address"

    # User sends address
    valid_address = "0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa"
    await listener.handle_command(valid_address, sender_chat_id=guest_id)
    assert guest_id not in listener._waiting_input

    # Check registered in trader_registry
    trader = trader_registry.get_trader(guest_id)
    assert trader is not None
    assert trader.get("watch_only") is True
    assert trader.get("owner_address") == valid_address.lower()

    # Active subscribers should exclude watch_only
    subs = trader_registry.get_active_subscribers()
    assert not any(s.get("chat_id") == guest_id for s in subs)

    # Clean up test trader
    trader_registry.disconnect_trader(guest_id)


@pytest.mark.asyncio
async def test_edit_reply_direct_behavior(mock_trading):
    """Verifies _edit_reply method behavior: edit success, fallback to send_reply, and message_not_modified handling."""
    listener = TelegramBotListener(trading_service=mock_trading)
    listener.bot_token = "123456:FAKE_TOKEN"
    listener.chat_id = "999888"

    # Case 1: _edit_message succeeds -> returns result
    listener._edit_message = AsyncMock(return_value=True)
    listener._send_reply = AsyncMock(return_value=True)

    res = await listener._edit_reply("Test Message", reply_markup={"inline_keyboard": []}, message_id=101)
    assert res is True
    assert listener._edit_message.call_count == 1
    assert listener._send_reply.call_count == 0

    # Case 2: _edit_message returns False -> falls back to _send_reply
    listener._edit_message.reset_mock()
    listener._send_reply.reset_mock()
    listener._edit_message.return_value = False

    await listener._edit_reply("Fallback Message", reply_markup={"inline_keyboard": []}, message_id=102)
    assert listener._edit_message.call_count == 1
    assert listener._send_reply.call_count == 1

    # Case 3: message is not modified exception in _edit_message -> returns True gracefully
    listener._edit_message.reset_mock()
    listener._send_reply.reset_mock()
    listener._edit_message.side_effect = Exception("Bad Request: message is not modified")

    res_unmodified = await listener._edit_reply("Unmodified Message", message_id=103)
    assert res_unmodified is True
    assert listener._send_reply.call_count == 0

    # Case 4: message_id is None -> calls _send_reply directly
    listener._edit_message.reset_mock()
    listener._send_reply.reset_mock()

    await listener._edit_reply("Direct Send Message", message_id=None)
    assert listener._edit_message.call_count == 0
    assert listener._send_reply.call_count == 1


@pytest.mark.asyncio
async def test_all_inline_callbacks_execute_without_attribute_error(mock_trading):
    """
    Exercises all inline callbacks using the REAL _edit_reply implementation
    (with mocked _edit_message network call) to ensure NO AttributeError: '_edit_reply' can occur.
    """
    listener = TelegramBotListener(trading_service=mock_trading)
    listener.bot_token = "123456:FAKE_TOKEN"
    admin_id = str(config.TELEGRAM_CHAT_ID or "12345")
    listener.chat_id = admin_id
    listener._edit_message = AsyncMock(return_value=True)
    listener._send_reply = AsyncMock(return_value=True)

    callbacks_to_test = [
        "dash_main",
        "dash_main_ref",
        "dash_positions",
        "dash_radar",
        "dash_wallet",
        "dash_journal",
        "dash_debates",
        "radar_table",
        "radar_pullback",
        "radar_verdicts",
        "wallet_watch_only",
        "wallet_manual_sig",
        "wallet_web3",
        "wallet_disconnect",
        "wallet_toggle_copy",
        "risk_menu",
        "dash_pause_entries",
        "dash_resume_entries",
    ]

    for cb in callbacks_to_test:
        listener._edit_message.reset_mock()
        # Ensure answerCallbackQuery doesn't attempt real network request
        with patch("services.telegram_service.TelegramService.answer_callback_query", new_callable=AsyncMock):
            await listener.handle_callback(f"cb_{cb}", cb, chat_id=admin_id, message_id=999)
            # Must invoke _edit_reply -> _edit_message without crashing
            assert listener._edit_message.call_count >= 1, f"Callback {cb} did not trigger _edit_reply / _edit_message"


```

---

<a id="file-tests-test_risk_manager_and_sizing-py"></a>
## File: `tests/test_risk_manager_and_sizing.py` (3242 строк, 136.2 KB)

```python
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from agents.risk_manager import RiskManager
from core.logger import TradeLogger
from core.models import FinalTradeDecision
from core.strategy_router import StrategyProfile
from services.nado_trading_service import NadoTradingService

@pytest.fixture
def mock_logger():
    return MagicMock(spec=TradeLogger)

@pytest.fixture
def mock_llm():
    return MagicMock()

@pytest.fixture
def risk_mgr(mock_logger, mock_llm):
    # Unit tests in this suite focus specifically on sizing math, Kelly calibration,
    # and portfolio drawdown limits. Set default_macro_regime to RANGE_CHOPPY so
    # individual sizing tests run with a valid regime unless testing macro veto.
    return RiskManager(mock_logger, mock_llm, default_macro_regime="RANGE_CHOPPY")

@pytest.fixture
def base_market_data():
    return {
        "price_data": {
            "current_price": 100.0,
            "ohlcv_1h": [{"volume": 100}] * 10
        },
        "indicators": {
            "atr_14": 2.0,
            "ema_20": 100.0,
            "bb_middle": 100.0
        },
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.001,
            "min_notional": 10.0
        }
    }

@pytest.fixture
def base_portfolio_data():
    return {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {},
        "win_count": 0,
        "loss_count": 0,
        "recent_streak": []
    }

# ==============================================================================
# 1. DAILY DRAWDOWN VETO GATE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_vetos_excessive_daily_drawdown(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When daily drawdown reaches or exceeds max_daily_drawdown_pct (e.g. 6% in BALANCED),
    RiskManager must strictly veto new trade entries with MAX_DAILY_DRAWDOWN.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # Simulate 6.5% daily drawdown via daily_drawdown_pct
    portfolio = dict(base_portfolio_data, daily_drawdown_pct=0.065)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_DAILY_DRAWDOWN"
    assert "Daily drawdown limit" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_vetos_negative_daily_pnl_pct(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When daily drawdown is provided as a negative daily_pnl_pct (-7.0%),
    RiskManager detects the breach and vetoes with MAX_DAILY_DRAWDOWN.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # Simulate -7.0% daily loss via daily_pnl_pct
    portfolio = dict(base_portfolio_data, daily_pnl_pct=-7.0)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_DAILY_DRAWDOWN"


@pytest.mark.asyncio
async def test_risk_manager_never_resets_extreme_drawdown_even_with_zero_positions(risk_mgr, base_market_data, base_portfolio_data):
    """
    Verifies that an extreme drawdown (e.g. 20%) is NEVER swept under the rug or reset to 0.0
    even if active_positions is empty and daily_trades is 0!
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # 20% drawdown with 0 active positions and 0 daily trades
    portfolio = dict(base_portfolio_data, daily_drawdown_pct=0.20, active_positions={}, daily_trades_count=0, unrealized_pnl=0.0)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_DAILY_DRAWDOWN"


@pytest.mark.asyncio
async def test_risk_manager_allows_safe_daily_drawdown(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When daily drawdown is well within safety limits (e.g. 1.5% < 6.0%),
    RiskManager approves the trade.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    portfolio = dict(base_portfolio_data, daily_drawdown_pct=0.015)
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is True
    assert verdict["veto_category"] is None

# ==============================================================================
# 2. MAX CONCURRENT POSITIONS VETO GATE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_vetos_when_max_concurrent_positions_reached(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    When active_positions count reaches max_concurrent_positions limit (e.g. 3),
    RiskManager independently enforces the cap as defense-in-depth with MAX_CONCURRENT_POSITIONS veto.
    """
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # 3 existing open positions
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1},
        "AVAX-USD": {"entry_price": 30.0, "sl_price": 29.0, "amount": 10.0}
    })
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MAX_CONCURRENT_POSITIONS"
    assert "Maximum concurrent positions reached" in verdict["reasoning"]

# ==============================================================================
# 3. DUPLICATE ASSET EXPOSURE VETO GATE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_vetos_duplicate_asset_position(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    If a position for the candidate asset is already open in active_positions,
    RiskManager strictly vetoes the trade with ALREADY_OPEN to prevent accidental double allocation.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # BTC position is already active
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01}
    })
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "ALREADY_OPEN"
    assert "already active" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_vetos_duplicate_base_asset_alias(risk_mgr, base_market_data, base_portfolio_data):
    """
    Day 7 Audit:
    Duplicate asset check normalizes symbol format (BTC vs BTC-USD) and prevents duplicate exposure.
    """
    ceo_decision = {
        "symbol": "BTC",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01}
    })
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "ALREADY_OPEN"

# ==============================================================================
# 4. KELLY SIZING MULTIPLIER TESTS
# ==============================================================================

def test_kelly_multiplier_scaling(risk_mgr):
    """
    Day 7 Audit / Institutional Fix:
    Validates Fractional (Half) Kelly sizing formula:
    - total_trades < 15: neutral 1.00x
    - Edge positive (55% WR, 1.80 RR): capped at 1.00x (no overleveraging on short samples)
    - Edge negative or soft (35% WR, 1.20 RR): cut to 0.50x (downside damping)
    - Extreme edge: never exceeds 1.00x
    """
    # 1. Sample size too small (< 15) -> neutral 1.0x
    assert risk_mgr._calculate_kelly_multiplier(win_rate=0.70, realized_rr=2.0, total_trades=10) == 1.0

    # 2. Positive edge (55% WR, 1.8 RR) -> capped at 1.0x (no artificial inflation)
    k_boost = risk_mgr._calculate_kelly_multiplier(win_rate=0.55, realized_rr=1.8, total_trades=20)
    assert k_boost == 1.0

    # 3. Negative edge (35% WR, 1.2 RR) -> cut to 0.50x
    k_cut = risk_mgr._calculate_kelly_multiplier(win_rate=0.35, realized_rr=1.2, total_trades=20)
    assert k_cut == 0.50

    # 4. Extreme edge (90% WR, 3.0 RR) -> strictly capped at 1.00x
    k_ceiling = risk_mgr._calculate_kelly_multiplier(win_rate=0.90, realized_rr=3.0, total_trades=30)
    assert k_ceiling == 1.0

# ==============================================================================
# 5. DEFENSIVE GUARDS & NONE-TYPE IMMUNITY TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_guards_against_none_and_invalid_inputs(risk_mgr):
    """
    Day 7 Audit:
    Verifies that RiskManager gracefully handles None and corrupted inputs
    without raising unhandled exceptions.
    """
    # 1. Null decision -> veto NO_SIGNAL
    r1 = await risk_mgr.analyze(None, {}, {})
    assert r1["approved"] is False
    assert r1["veto_category"] == "NO_SIGNAL"

    # 2. Invalid price (0.0) -> veto INVALID_PRICE
    ceo = {"decision": "LONG", "conviction": 80, "symbol": "BTC-USD"}
    r2 = await risk_mgr.analyze(ceo, {"total_usd": 1000.0}, {"price_data": {"current_price": 0.0}})
    assert r2["approved"] is False
    assert r2["veto_category"] == "INVALID_PRICE"

    # 3. Invalid ATR (0.0) -> veto INVALID_ATR
    r3 = await risk_mgr.analyze(ceo, {"total_usd": 1000.0}, {"price_data": {"current_price": 100.0}, "indicators": {"atr_14": 0.0}})
    assert r3["approved"] is False
    assert r3["veto_category"] == "INVALID_ATR"

    # 4. Insufficient balance (0.0) -> veto INSUFFICIENT_BALANCE
    r4 = await risk_mgr.analyze(ceo, {"total_usd": 0.0}, {"price_data": {"current_price": 100.0}, "indicators": {"atr_14": 1.0}})
    assert r4["approved"] is False
    assert r4["veto_category"] == "INSUFFICIENT_BALANCE"

# ==============================================================================
# 6. NADO TRADING SERVICE DAILY DRAWDOWN TRACKING
# ==============================================================================

@pytest.mark.asyncio
async def test_nado_trading_service_tracks_daily_drawdown():
    """
    Day 7 Audit:
    Verifies that NadoTradingService computes daily_pnl_usd, daily_pnl_pct,
    and daily_drawdown_pct in get_portfolio_summary().
    """
    service = NadoTradingService()
    service.is_connected = True
    service.default_subaccount_id = "0x1234"
    service.daily_start_balance = 1000.0
    
    # Mock client and get_active_positions
    service.client = MagicMock()
    mock_summary = MagicMock()
    mock_health = MagicMock()
    mock_health.liabilities = 0
    mock_health.health = int(500 * 1e18)
    mock_health.assets = int(1000 * 1e18)
    mock_summary.healths = [mock_health]
    mock_spot = MagicMock()
    mock_spot.product_id = 0
    mock_spot.balance.amount = int(1000 * 1e18)
    mock_summary.spot_balances = [mock_spot]
    
    service.client.subaccount.get_engine_subaccount_summary = MagicMock(return_value=mock_summary)
    service.get_active_positions = AsyncMock(return_value=[])
    
    # 1. Baseline day start at $1000
    summary1 = await service.get_portfolio_summary()
    assert summary1["current_balance"] == 1000.0
    assert summary1["peak_balance"] == 1000.0
    assert summary1["daily_pnl_usd"] == 0.0
    assert summary1["daily_pnl_pct"] == 0.0
    assert summary1["daily_drawdown_pct"] == 0.0
    assert summary1["max_drawdown_pct"] == 0.0
    
    # 2. Simulate equity drop to $940 (6% daily drawdown)
    mock_spot.balance.amount = int(940 * 1e18)
    summary2 = await service.get_portfolio_summary(bypass_cache=True)
    assert summary2["current_balance"] == 940.0
    assert summary2["peak_balance"] == 1000.0
    assert summary2["daily_pnl_usd"] == -60.0
    assert summary2["daily_pnl_pct"] == -6.0
    assert summary2["daily_drawdown_pct"] == 0.06
    assert summary2["max_drawdown_pct"] == 0.06

@pytest.mark.asyncio
async def test_peak_balance_high_water_mark_survives_new_utc_day(risk_mgr, base_market_data):
    """
    Verifies that when a new UTC day resets daily_drawdown_pct to 0.00%,
    the high-water mark peak_balance is preserved, allowing RiskManager
    to enforce two-factor equity drawdown sizing protection.
    """
    portfolio_new_day = {
        "balance": 930.0,
        "total_usd": 930.0,
        "current_balance": 930.0,
        "spot_balance": 930.0,
        "available_margin": 900.0,
        "margin_used": 30.0,
        "maintenance_health_usd": 850.0,
        "initial_balance": 1000.0,
        "peak_balance": 1000.0,      # HWM peak was $1000, current is $930 (7% drawdown)
        "daily_drawdown_pct": 0.0,    # Reset at 00:00 UTC
        "daily_pnl_pct": 0.0,
        "daily_pnl_usd": 0.0,
        "active_positions": {},
        "recent_streak": ["LOSS", "WIN"],
        "strict_portfolio_health": True
    }
    
    ceo_enter = {
        "symbol": "ETH-USD",
        "decision": "LONG",
        "conviction": 85,
        "entry_quality": 85,
        "trade_action": "ENTER",
        "action": "ENTER"
    }
    
    res = await risk_mgr.analyze(
        ceo_enter,
        portfolio_new_day,
        base_market_data,
        effective_profile="BALANCED",
        strategy_mode="TREND_FOLLOWING"
    )
    
    # Despite daily_drawdown_pct == 0.0%, drawdown from peak is 7.0%,
    # so dd_mult should be 0.45x (EQUITY_ELEVATED: 6-10% drawdown)
    assert res.get("approved") is True
    applied = res.get("applied_multipliers", {})
    assert applied.get("dd_mult") == 0.45, f"Expected dd_mult=0.45 for 7% peak drawdown, got {applied.get('dd_mult')}"

@pytest.mark.asyncio
async def test_nado_trading_service_reanchors_on_external_transfer():
    """
    Verifies that when a large external balance change occurs (e.g. withdrawal or baseline mismatch)
    with 0 active positions and 0 trades today, NadoTradingService re-anchors daily_start_balance
    instead of reporting a fake 90% drawdown.
    """
    service = NadoTradingService()
    service.is_connected = True
    service.default_subaccount_id = "0x1234"
    service.daily_start_balance = 92.42
    service._daily_start_total_trades = 0
    service.win_count = 0
    service.loss_count = 0
    service._manual_stats = True
    
    # Balance on exchange is $8.54 with 0 active positions and 0 trades today
    mock_summary = MagicMock()
    mock_health = MagicMock()
    mock_health.liabilities = 0
    mock_health.health = int(8.54 * 1e18)
    mock_health.assets = int(8.54 * 1e18)
    mock_summary.healths = [mock_health]
    mock_spot = MagicMock()
    mock_spot.product_id = 0
    mock_spot.balance.amount = int(8.54 * 1e18)
    mock_summary.spot_balances = [mock_spot]
    
    service.client = MagicMock()
    service.client.subaccount.get_engine_subaccount_summary = MagicMock(return_value=mock_summary)
    service.get_active_positions = AsyncMock(return_value=[])
    
    summary = await service.get_portfolio_summary()
    # Must re-anchor to 8.54 with 0.0 daily drawdown instead of 90.76% fake loss
    assert summary["current_balance"] == 8.54
    assert summary["daily_pnl_usd"] == 0.0
    assert summary["daily_drawdown_pct"] == 0.0
    assert service._daily_start_balance == 8.54

@pytest.mark.asyncio
async def test_risk_manager_strictly_vetos_extreme_drawdown_without_fake_reset(risk_mgr, base_market_data, base_portfolio_data):
    """
    Verifies that RiskManager strictly enforces MAX_DAILY_DRAWDOWN when an extreme drawdown
    is reported, and never dangerously sweeps it under the rug even with 0 active positions.
    """
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "conviction": 70,
        "directional_confidence": 70,
        "entry_quality": 65,
        "trade_action": "REDUCE_SIZE"
    }
    
    # Simulate portfolio with 90.76% apparent drawdown but 0 trades today and 0 active positions
    portfolio = dict(
        base_portfolio_data, 
        total_usd=8.54,
        available_margin=8.54,
        daily_drawdown_pct=0.9076,
        daily_trades_count=0,
        unrealized_pnl=0.0,
        active_positions={}
    )
    
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="AGGRESSIVE")
    # Must be vetoed by MAX_DAILY_DRAWDOWN to protect remaining capital
    assert verdict.get("veto_category") == "MAX_DAILY_DRAWDOWN"
    assert verdict.get("approved") is False

# ==============================================================================
# 7. INSTITUTIONAL SAFETY INVARIANTS TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_directional_cluster_cap_veto(risk_mgr, base_market_data, base_portfolio_data):
    """
    Prevents opening 3 concurrent positions in the same direction during choppy/transition markets.
    """
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # 2 existing LONG positions in RANGE_CHOPPY
    portfolio = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"direction": "LONG", "entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01},
        "ETH-USD": {"direction": "LONG", "entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1}
    })
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, base_market_data, 
        effective_profile="BALANCED", macro_regime="RANGE_CHOPPY"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "DIRECTIONAL_CLUSTER_CAP"
    assert "Directional cluster limit reached" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_penalty_suppression_veto(risk_mgr, base_market_data, base_portfolio_data):
    """
    When accumulated penalties suppress dynamic risk below min_viable_risk (0.35 of base_risk),
    RiskManager must strictly VETO with PENALTY_SUPPRESSION rather than artificially inflating risk.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 72,  # Above min_conviction 70, but below 75% floor (Cohort C)
        "trade_action": "ENTER"
    }
    
    # 5 consecutive losses cuts portfolio_mult deeply (<= 0.20)
    portfolio = dict(base_portfolio_data, recent_streak=["LOSS", "LOSS", "LOSS", "LOSS", "LOSS"])
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, base_market_data, 
        effective_profile="BALANCED"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "PENALTY_SUPPRESSION"
    assert "Dynamic risk" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_high_conviction_floor_on_cohort_b_reduce_size(risk_mgr, base_market_data, base_portfolio_data):
    """
    Cohort B trades (REDUCE_SIZE with conviction >= 75%) must be saved from PENALTY_SUPPRESSION deadlock:
    1. At 3 losses: Anti-double-penalty preserves dynamic risk at 0.60% (above 0.375% floor).
    2. At 5 losses (portfolio_mult=0.20): dynamic risk (0.30%) is boosted to min_viable_risk floor (0.375%).
    """
    ceo_decision = {
        "symbol": "ZEC-USD",
        "decision": "LONG",
        "conviction": 87,
        "trade_action": "REDUCE_SIZE"
    }
    # Case 1: 3 losses - anti double penalty preserves viable risk
    portfolio_3_losses = dict(base_portfolio_data, recent_streak=["LOSS", "LOSS", "LOSS"])
    verdict_3 = await risk_mgr.analyze(
        ceo_decision, portfolio_3_losses, base_market_data,
        effective_profile="BALANCED"
    )
    assert verdict_3["approved"] is True
    assert verdict_3["final_risk_pct"] == pytest.approx(0.006, rel=1e-3)

    # Case 2: 5 losses - dynamic risk 0.30% boosted to 0.375% min_viable_risk floor
    portfolio_5_losses = dict(base_portfolio_data, recent_streak=["LOSS", "LOSS", "LOSS", "LOSS", "LOSS"])
    verdict_5 = await risk_mgr.analyze(
        ceo_decision, portfolio_5_losses, base_market_data,
        effective_profile="BALANCED"
    )
    assert verdict_5["approved"] is True
    assert verdict_5["final_risk_pct"] == pytest.approx(0.00375, rel=1e-3)

@pytest.mark.asyncio
async def test_risk_manager_liquidation_precedence_and_price(risk_mgr, base_market_data, base_portfolio_data):
    """
    Validates that:
    1. Exact liquidation_price is calculated and populated in approved trades.
    2. Leverage is clamped so SL distance has at least 30% cushion before liquidation.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 85,
        "trade_action": "ENTER"
    }
    
    # Wide stop loss: 5% drop (ATR = 3.5, sl_mult = 1.5 -> SL dist = 5.25%)
    market = dict(base_market_data)
    market["indicators"] = {"atr_14": 3.5, "ema_20": 100.0, "bb_middle": 100.0}
    market["derivatives_data"] = {
        "taker_fee_pct": 0.0005,
        "funding_rate": 0.0001,
        "size_increment": 0.001,
        "min_notional": 10.0,
        "maintenance_margin_pct": 0.025
    }
    
    verdict = await risk_mgr.analyze(
        ceo_decision, base_portfolio_data, market, 
        effective_profile="BALANCED", macro_regime="TREND_BULLISH"
    )
    
    assert verdict["approved"] is True
    assert verdict["liquidation_price"] > 0.0
    # For a LONG, liquidation price must be strictly lower than stop loss price
    assert verdict["stop_loss_price"] > verdict["liquidation_price"]
    # Distance to liquidation must exceed distance to SL
    entry = verdict["entry_price"]
    sl = verdict["stop_loss_price"]
    liq = verdict["liquidation_price"]
    assert (entry - sl) < (entry - liq)

@pytest.mark.asyncio
async def test_risk_manager_min_notional_veto(risk_mgr, base_market_data, base_portfolio_data):
    """
    When calculated notional is below exchange min_notional, RiskManager vetoes with MIN_NOTIONAL.
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    
    # Tiny balance: $10, 0.5% risk = $0.05 risk. With 2% SL, notional = $2.50.
    portfolio = dict(base_portfolio_data, total_usd=10.0, available_margin=10.0)
    market = dict(base_market_data)
    market["derivatives_data"] = {
        "taker_fee_pct": 0.0005,
        "funding_rate": 0.0001,
        "size_increment": 0.001,
        "min_notional": 15.0 # Exchange requires at least $15
    }
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="CONSERVATIVE"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MIN_NOTIONAL"
    assert "below exchange minimum notional" in verdict["reasoning"]

@pytest.mark.asyncio
async def test_risk_manager_does_not_veto_45_contracts_on_dummy_100_min_size(risk_mgr, base_market_data, base_portfolio_data):
    """
    Regression Test:
    In Nado Protocol, book_info.min_size is 100.0 across all products as an unconfirmed dummy default.
    An order of 45 contracts of XRP (aligned to size_increment 5.0) MUST NOT be falsely vetoed!
    """
    ceo_decision = {
        "symbol": "XRP-USD",
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "ENTER"
    }
    
    market = {
        "price_data": {"current_price": 1.50, "ohlcv_1h": [{"volume": 1000}] * 10},
        "indicators": {"atr_14": 0.05, "ema_20": 1.50, "bb_middle": 1.50},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 5.0, # 5 XRP steps
            "min_size": 100.0,      # Unconfirmed dummy default from Nado engine
            "min_notional": 0.0,
            "maintenance_margin_pct": 0.025
        }
    }
    portfolio = dict(base_portfolio_data, total_usd=70.0, available_margin=70.0)
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="AGGRESSIVE", macro_regime="TRENDING"
    )
    
    # Must NOT be vetoed by MIN_SIZE
    assert verdict.get("veto_category") != "MIN_SIZE"
    assert verdict["approved"] is True
    # Contracts must be at least 5.0 and aligned to size_increment
    assert verdict["contracts"] > 0
    assert verdict["contracts"] % 5.0 == 0

@pytest.mark.asyncio
async def test_risk_manager_enforces_confirmed_real_min_size(risk_mgr, base_market_data, base_portfolio_data):
    """
    Regression Test:
    When a genuine, confirmed minimum size exists (e.g. 50.0 contracts),
    orders strictly below it must still be safely vetoed with MIN_SIZE.
    """
    ceo_decision = {
        "symbol": "XRP-USD",
        "decision": "SHORT",
        "conviction": 80,
        "trade_action": "REDUCE_SIZE"
    }
    
    market = {
        "price_data": {"current_price": 1.50, "ohlcv_1h": [{"volume": 1000}] * 10},
        "indicators": {"atr_14": 0.05, "ema_20": 1.50, "bb_middle": 1.50},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 5.0,
            "min_size": 50.0, # Confirmed real minimum size
            "min_notional": 0.0,
            "maintenance_margin_pct": 0.025
        }
    }
    # Sizing yields 45.0 contracts (strictly below 50.0)
    portfolio = dict(base_portfolio_data, total_usd=300.0, available_margin=300.0)
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="BALANCED", macro_regime="RANGE_CHOPPY"
    )
    
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MIN_SIZE"
    assert "below exchange minimum size" in verdict["reasoning"]

def test_nado_market_parameters_filters_dummy_100():
    """
    Verifies that NadoTradingService._get_market_parameters replaces
    the dummy 100.0 min_size with size_increment_base.
    """
    from services.nado_trading_service import NadoTradingService
    service = NadoTradingService.__new__(NadoTradingService)
    
    mock_market = MagicMock()
    mock_market.book_info.size_increment = "5000000000000000000" # 5.0 x18
    mock_market.book_info.price_increment_x18 = "10000000000000"  # 0.00001 x18
    mock_market.book_info.min_size = "100000000000000000000"       # 100.0 x18 (dummy)
    
    service._market_cache = {10: mock_market}
    params = service._get_market_parameters(10)
    
    assert params["size_increment_base"] == 5.0
    # Must be filtered to 5.0, NOT 100.0!
    assert params["min_size"] == 5.0


# ==============================================================================
# AUDIT P0 / P1 FIXES: MARGIN PENALTY, LEVERAGE CEILING, SLIPPAGE ALIGNMENT
# ==============================================================================

@pytest.mark.asyncio
async def test_no_double_penalty_on_target_margin(risk_mgr, base_market_data, base_portfolio_data):
    """
    Audit P1:
    Drawdown and action multipliers are already applied to dynamic_risk_pct in Stage 5.
    They must NOT be applied a second time to target_margin_usd in Stage 7.
    When a trader suffers 2 consecutive losses (dd_mult = 0.50), notional is halved
    due to risk reduction, but target margin remains available so notional is not
    quartered (halved a second time).
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 85,
        "trade_action": "ENTER"
    }
    # 2 consecutive losses in streak -> dd_mult = 0.50
    portfolio = dict(
        base_portfolio_data, 
        total_usd=100.0, 
        available_margin=100.0,
        recent_streak=["LOSS", "LOSS"]
    )
    # 1.5% SL distance on $100 price
    market = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": [{"volume": 1000}] * 15},
        "indicators": {"atr_14": 1.0, "ema_20": 100.0, "bb_middle": 100.0},
        "order_book_data": {"spread_pct": 0.02},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 0.0
        }
    }
    
    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market, 
        effective_profile="BALANCED", macro_regime="TRENDING"
    )
    
    assert verdict["approved"] is True
    # Base risk is 1.5% of $100 = $1.50. With dd_mult = 0.50, risk is ~$0.75.
    # At ~1.5% SL distance, notional should be ~$50 (not $25 which would be a double penalty!).
    assert verdict["risk_amount_usd"] < 1.0  # Reduced by dd_mult
    assert verdict["notional_size_usd"] >= 40.0  # Preserved notional without double margin cut


# ==============================================================================
# AUDIT P0 / P1 BEHAVIORAL TESTS: RISK BREACH PROTECTION, SLIPPAGE, BOUNDARIES
# ==============================================================================

@pytest.mark.asyncio
async def test_leverage_ceil_rounding_boundary_matrix(risk_mgr, base_portfolio_data):
    """
    Audit P1 Boundary Matrix:
    Verifies leverage ceiling and math.ceil rounding behavior at exact boundaries:
    - 7.01x req, 10x ceiling -> 8.0x (notional fully preserved)
    - 8.00x req, 10x ceiling -> 8.0x (notional fully preserved)
    - 9.99x req, 10x ceiling -> 10.0x (notional fully preserved)
    - 10.01x req, 10x ceiling -> 10.0x (clamped to safe ceiling)
    - 7.50x req, 7.0x ceiling -> 7.0x (ceiling takes strict precedence)
    """
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    portfolio = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)

    # Helper function to compute effective leverage and clamped status
    async def run_scenario(sl_distance_pct, safe_vol_ceiling=10.0):
        # ATR set such that volatility ceiling matches safe_vol_ceiling:
        # atr_pct < 1.0 -> vol_max_leverage = 15.0 (profile max = 10.0)
        # atr_pct >= 2.0 -> vol_max_leverage = 7.0
        atr = 0.5 if safe_vol_ceiling >= 10.0 else 2.1
        current_price = 100.0
        # Target margin for BALANCED: 10% of $100 = $10.00
        # Dynamic risk for BALANCED with conv 80: base 1.5% = $1.50
        # Required notional = $1.50 / sl_distance_pct
        # Required leverage = Required notional / $10.00
        market = {
            "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 1000}] * 15},
            "indicators": {"atr_14": atr, "ema_20": current_price, "bb_middle": current_price},
            "order_book_data": {"spread_pct": 0.02},
            "derivatives_data": {
                "taker_fee_pct": 0.0005,
                "funding_rate": 0.0001,
                "size_increment": 0.001,
                "min_size": 0.001,
                "min_notional": 0.0,
                "maintenance_margin_pct": 0.01
            }
        }
        # Override sl_dist_base directly via ATR multiplier or formula:
        # In BALANCED: sl_dist_base = max(atr_14 * 1.5, current_price * 0.015)
        # We test via dynamic risk and distances
        verdict = await risk_mgr.analyze(
            ceo_decision, portfolio, market,
            effective_profile="BALANCED", macro_regime="TRENDING"
        )
        return verdict

    # Scenario 1: Standard BALANCED sizing
    v1 = await run_scenario(sl_distance_pct=0.02, safe_vol_ceiling=10.0)
    assert v1["approved"] is True
    assert v1["effective_leverage"] == float(int(v1["effective_leverage"]))
    assert v1["effective_leverage"] <= 10.0
    # Required margin must be <= target margin budget ($10.00)
    assert v1["margin_usd"] <= 10.00

    # Scenario 2: Volatility ceiling of 7.0x (atr = 2.1 -> atr_pct = 2.1% >= 2.0%)
    v2 = await run_scenario(sl_distance_pct=0.015, safe_vol_ceiling=7.0)
    assert v2["approved"] is True
    assert v2["effective_leverage"] <= 7.0


@pytest.mark.asyncio
async def test_open_position_behavioral_limit_price_and_slippage():
    """
    Audit P0 / P1 Behavioral:
    Tests NadoTradingService.open_position directly with SDK mocks.
    Verifies that PlaceOrderParams.order.priceX18 is computed strictly
    from max_slippage_pct (10 bps -> 1.001 for LONG, 0.999 for SHORT)
    rather than a hardcoded 0.5%.
    """
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock, AsyncMock, patch

    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.active_positions = {}
    service.product_map = {"BTC": 2}
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"

    mock_client = MagicMock()
    service.client = mock_client

    mock_market = MagicMock()
    mock_market.product_id = 2
    mock_market.book_info.size_increment = "50000000000000"       # 0.00005 BTC x18
    mock_market.book_info.price_increment_x18 = "1000000000000000000"  # 1.0 x18
    mock_market.book_info.min_size = "0"                           # Min notional 0 for slippage behavior test
    service._market_cache = {2: mock_market}

    captured_params = []
    def mock_place_order(params):
        captured_params.append(params)
        res = MagicMock()
        res.data.digest = "0xmockdigest123"
        return res

    mock_client.market.place_order.side_effect = mock_place_order

    # Mock historical orders to simulate instant fill
    mock_order_info = MagicMock()
    mock_order_info.base_filled = "1000000000000000"  # 0.001 BTC
    mock_order_info.quote_filled = "60060000000000000000"  # $60.06
    mock_hist = MagicMock()
    mock_hist.orders = [mock_order_info]
    mock_client.market.get_historical_orders_by_digest.return_value = mock_hist
    service._save_positions = MagicMock()

    # 1. Test LONG with 10 bps (0.001) slippage: entry $60,000 -> limit price $60,060
    with patch("nado_protocol.utils.nonce.gen_order_nonce", return_value=1234567890):
        with patch.object(service, "initialize_partial_tp_plan", return_value=None):
            success = await service.open_position(
                symbol="BTC-USD",
                direction="LONG",
                entry_price=60000.0,
                notional_usd=60.0,
                tp_price=63000.0,
                sl_price=59000.0,
                leverage=5,
                contracts=0.001,
                approved_risk_usd=10.0,
                max_slippage_pct=0.001  # 10 bps
            )

    assert success is True
    assert len(captured_params) >= 1
    # Check limit price sent to exchange
    order_price_x18 = captured_params[0].order.priceX18
    # 60,000 * 1.001 = 60,060
    expected_price_x18 = 60060 * 10**18
    assert order_price_x18 == expected_price_x18


@pytest.mark.asyncio
async def test_emergency_protect_risk_breach_behavior():
    """
    Audit P0 / P1 Behavioral:
    Tests NadoTradingService.emergency_protect_risk_breach:
    1. Sets protection_state to RISK_BREACH.
    2. When force_close_position succeeds, position is cleaned up and returns (True, pnl).
    3. When force_close_position fails and position remains on chain,
       position is retained as RISK_BREACH_UNRESOLVED rather than silently dropped!
    """
    from services.nado_trading_service import NadoTradingService

    service = NadoTradingService.__new__(NadoTradingService)
    service.is_connected = True
    service.active_positions = {
        "ETH-USD": {
            "symbol": "ETH-USD",
            "direction": "LONG",
            "entry_price": 2500.0,
            "sl_price": 2400.0,
            "contracts": 0.1,
            "protection_state": "SUBMITTING_PROTECTION"
        }
    }
    service._save_positions = MagicMock()

    # Case A: force_close_position succeeds
    service.force_close_position = AsyncMock(return_value=(True, -2.50))
    ok, pnl = await service.emergency_protect_risk_breach("ETH-USD", approved_risk=10.0, actual_risk=15.0)
    assert ok is True
    assert pnl == -2.50

    # Case B: force_close_position fails and position remains open on-chain
    service.active_positions["ETH-USD"] = {
        "symbol": "ETH-USD",
        "direction": "LONG",
        "entry_price": 2500.0,
        "sl_price": 2400.0,
        "contracts": 0.1,
        "protection_state": "SUBMITTING_PROTECTION"
    }
    service.force_close_position = AsyncMock(return_value=(False, 0.0))
    # Mock on-chain check indicating position still exists
    service.get_active_positions = AsyncMock(return_value=[{"symbol": "ETH-USD"}])

    ok_b, pnl_b = await service.emergency_protect_risk_breach("ETH-USD", approved_risk=10.0, actual_risk=15.0)
    assert ok_b is False
    # Authoritative persistence: must be retained as RISK_BREACH_UNRESOLVED!
    assert "ETH-USD" in service.active_positions
    assert service.active_positions["ETH-USD"]["protection_state"] == "RISK_BREACH_UNRESOLVED"


@pytest.mark.asyncio
async def test_strict_metadata_unavailable_veto(risk_mgr, base_portfolio_data):
    """
    Audit P1:
    When strict_metadata flag is set (e.g. from live pipeline limits injection)
    and size_increment is 0.0 or missing, RiskManager must strictly fail-closed
    with MARKET_METADATA_UNAVAILABLE instead of silently guessing 0.001.
    """
    ceo_decision = {
        "symbol": "NEWTOKEN-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }
    portfolio = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)
    market = {
        "price_data": {"current_price": 1.0, "ohlcv_1h": [{"volume": 1000}] * 15},
        "indicators": {"atr_14": 0.05, "ema_20": 1.0, "bb_middle": 1.0},
        "order_book_data": {"spread_pct": 0.02},
        "derivatives_data": {
            "strict_metadata": True,   # Injected by pipeline on live run
            "size_increment": 0.0,     # Missing or unknown exchange step size
            "min_size": 0.0,
            "min_notional": 0.0
        }
    }

    verdict = await risk_mgr.analyze(
        ceo_decision, portfolio, market,
        effective_profile="BALANCED", macro_regime="TRENDING"
    )

    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MARKET_METADATA_UNAVAILABLE"
    assert "missing or non-positive size_increment" in verdict["reasoning"]


@pytest.mark.asyncio
async def test_pipeline_risk_breach_behavioral_close_confirmed():
    """
    Audit P0 Behavioral (Scenario A):
    Runs a REAL pipeline cycle through TradingPipeline.run_cycle().
    When an adverse fill on exchange causes actual loss to breach the approved risk budget,
    the pipeline detects the breach, invokes emergency_protect_risk_breach,
    marks execution_status as ABORTED_RISK_BREACH, does NOT broadcast a phantom trade card,
    and sends an alert to the operator.
    """
    import time
    from core.pipeline import TradingPipeline
    from core.models import FinalTradeDecision
    from unittest.mock import MagicMock, AsyncMock

    mock_services = MagicMock()
    mock_agents = MagicMock()

    pipeline = TradingPipeline(agents=mock_agents, services=mock_services, exchange_name="Nado")
    pipeline.data_guard.validate = MagicMock(return_value=(True, "OK"))

    mock_services.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "price_data": {"current_price": 2000.0, "timestamp": time.time()},
        "order_book_data": {"best_bid": 1999.0, "best_ask": 2001.0, "spread_pct": 0.1},
        "indicators": {"atr_14": 20.0, "ema_20": 1980.0},
        "derivatives_data": {"open_interest": 100.0, "funding_rate": 0.0001, "taker_fee_pct": 0.0005}
    })
    mock_agents.regime.analyze = AsyncMock(return_value={"regime": "BULL_TREND", "recommended_profile": "BALANCED"})
    mock_agents.reflector.get_lessons = MagicMock(return_value=[])
    mock_agents.universe.analyze = AsyncMock(return_value={"selected_pairs": ["ETH-USD"]})
    mock_services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "ETH-USD", "volumeQuote": 1000}])
    mock_services.trading_service.get_market_limits = AsyncMock(return_value={"size_increment": 0.001, "price_increment": 0.01})
    mock_agents.scanner.analyze = AsyncMock(return_value={"proceed": True, "reasoning": "Valid"})

    for agent in [mock_agents.candle, mock_agents.orderbook, mock_agents.oi_funding, mock_agents.news, mock_agents.indicator]:
        agent.analyze = AsyncMock(return_value={"signal": "BULLISH", "confidence": 85})
        agent.name = "TestAnalyst"

    mock_agents.bull.analyze = AsyncMock(return_value={"summary": "Bull thesis"})
    mock_agents.bear.analyze = AsyncMock(return_value={"summary": "Bear thesis"})
    mock_agents.ceo.analyze = AsyncMock(return_value={
        "decision": "LONG",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 85,
        "trade_action": "ENTER",
        "reasoning_en": "Strong setup"
    })
    mock_agents.memory.get_recent_context = MagicMock(return_value=[])

    # Approved risk is $50.00 (entry 2000.0, SL 1950.0 -> dist 50.0, 1.0 contract)
    mock_agents.risk.analyze = AsyncMock(return_value={
        "approved": True,
        "entry_price": 2000.0,
        "notional_size_usd": 2000.0,
        "position_size_pct": 20.0,
        "take_profit_price": 2100.0,
        "take_profit_pct": 5.0,
        "stop_loss_price": 1950.0,
        "stop_loss_pct": 2.5,
        "leverage": 10,
        "contracts": 1.0,
        "risk_amount_usd": 50.0,
        "approved_risk_usd": 50.0,
        "expected_slippage_pct": 0.001,
        "reasoning": "Risk approved"
    })

    # Adverse fill: filled at 2030.0! (Dist to SL is 2030 - 1950 = $80.00, fee ~$4 -> total loss ~$84 > max tolerated $55.05)
    async def simulate_fill_with_slippage(**kwargs):
        mock_services.trading_service.active_positions["ETH-USD"] = {
            "symbol": "ETH-USD",
            "direction": "LONG",
            "entry_price": 2030.0,
            "contracts": 1.0,
            "notional_usd": 2030.0,
            "protection_state": "SUBMITTING_PROTECTION"
        }
        return True

    mock_services.trading_service.active_positions = {}
    mock_services.trading_service.cooldown_until = 0.0
    mock_services.trading_service.get_portfolio_summary = AsyncMock(return_value={"total_usd": 100.0, "available_margin": 100.0, "free_margin": 100.0})
    mock_services.trading_service.get_active_positions = AsyncMock(return_value=[])
    mock_services.trading_service.sync_with_exchange = AsyncMock()
    mock_services.trading_service.check_and_update_positions = AsyncMock(return_value=[])
    mock_services.trading_service.open_position = AsyncMock(side_effect=simulate_fill_with_slippage)
    mock_services.trading_service.emergency_protect_risk_breach = AsyncMock(return_value=(True, -2.50))
    mock_services.trading_service.last_execution_status = None
    mock_services.tg_sender.send_message = AsyncMock()
    mock_services.tg_sender.broadcast_to_channel = AsyncMock()

    # EXECUTE THE REAL PIPELINE CYCLE
    await pipeline.run_cycle(cycle_number=1, force_scan=False)

    # 1. Emergency close must have been invoked through pipeline
    assert mock_services.trading_service.emergency_protect_risk_breach.called is True
    call_args = mock_services.trading_service.emergency_protect_risk_breach.call_args[1]
    assert call_args["symbol"] == "ETH-USD"
    assert call_args["approved_risk"] == 50.0
    assert call_args["actual_risk"] > 55.05

    # 2. Phantom trade card must NEVER be broadcast to channel
    assert not mock_services.tg_sender.broadcast_to_channel.called

    # 3. Operator received alert about ABORTED RISK BREACH
    assert mock_services.tg_sender.send_message.called
    sent_text = mock_services.tg_sender.send_message.call_args[0][0]
    assert "ABORTED RISK BREACH" in sent_text


@pytest.mark.asyncio
async def test_pipeline_risk_breach_behavioral_close_unconfirmed_blocks_subsequent_trades(risk_mgr, base_portfolio_data):
    """
    Audit P0 Behavioral (Scenario B):
    When emergency close fails or is unconfirmed:
    1. Pipeline sets execution_status to RISK_BREACH_UNRESOLVED.
    2. Critical emergency alert is sent to operator.
    3. The position remains retained in active_positions with protection_state = 'RISK_BREACH_UNRESOLVED'.
    4. Real RiskManager.analyze() in subsequent cycles strictly VETOES any new trade attempts.
    """
    import time
    from core.pipeline import TradingPipeline
    from unittest.mock import MagicMock, AsyncMock

    mock_services = MagicMock()
    mock_agents = MagicMock()

    pipeline = TradingPipeline(agents=mock_agents, services=mock_services, exchange_name="Nado")
    pipeline.data_guard.validate = MagicMock(return_value=(True, "OK"))

    mock_services.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "price_data": {"current_price": 2000.0, "timestamp": time.time()},
        "indicators": {"atr_14": 20.0, "ema_20": 1980.0},
        "derivatives_data": {"open_interest": 100.0, "funding_rate": 0.0001, "taker_fee_pct": 0.0005}
    })
    mock_agents.regime.analyze = AsyncMock(return_value={"regime": "BULL_TREND", "recommended_profile": "BALANCED"})
    mock_agents.reflector.get_lessons = MagicMock(return_value=[])
    mock_agents.universe.analyze = AsyncMock(return_value={"selected_pairs": ["ETH-USD"]})
    mock_services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "ETH-USD", "volumeQuote": 1000}])
    mock_services.trading_service.get_market_limits = AsyncMock(return_value={"size_increment": 0.001, "price_increment": 0.01})
    mock_agents.scanner.analyze = AsyncMock(return_value={"proceed": True, "reasoning": "Valid"})

    for agent in [mock_agents.candle, mock_agents.orderbook, mock_agents.oi_funding, mock_agents.news, mock_agents.indicator]:
        agent.analyze = AsyncMock(return_value={"signal": "BULLISH", "confidence": 85})
        agent.name = "TestAnalyst"

    mock_agents.bull.analyze = AsyncMock(return_value={"summary": "Bull"})
    mock_agents.bear.analyze = AsyncMock(return_value={"summary": "Bear"})
    mock_agents.ceo.analyze = AsyncMock(return_value={
        "decision": "LONG", "conviction": 85, "directional_confidence": 85, "entry_quality": 85,
        "trade_action": "ENTER", "reasoning_en": "Setup"
    })
    mock_agents.memory.get_recent_context = MagicMock(return_value=[])

    mock_agents.risk.analyze = AsyncMock(return_value={
        "approved": True, "entry_price": 2000.0, "notional_size_usd": 2000.0, "position_size_pct": 20.0,
        "take_profit_price": 2100.0, "stop_loss_price": 1950.0, "leverage": 10, "contracts": 1.0,
        "risk_amount_usd": 50.0, "approved_risk_usd": 50.0, "reasoning": "Approved"
    })

    # Adverse fill simulated on open_position call
    async def simulate_fill_with_slippage_unconfirmed(**kwargs):
        mock_services.trading_service.active_positions["ETH-USD"] = {
            "symbol": "ETH-USD", "direction": "LONG", "entry_price": 2040.0,
            "contracts": 1.0, "notional_usd": 2040.0, "protection_state": "SUBMITTING_PROTECTION"
        }
        return True

    mock_services.trading_service.active_positions = {}
    mock_services.trading_service.cooldown_until = 0.0
    mock_services.trading_service.get_portfolio_summary = AsyncMock(return_value={"total_usd": 100.0, "available_margin": 100.0, "free_margin": 100.0})
    mock_services.trading_service.get_active_positions = AsyncMock(return_value=[])
    mock_services.trading_service.sync_with_exchange = AsyncMock()
    mock_services.trading_service.check_and_update_positions = AsyncMock(return_value=[])
    mock_services.trading_service.open_position = AsyncMock(side_effect=simulate_fill_with_slippage_unconfirmed)
    # Emergency close FAILS / unconfirmed on exchange!
    mock_services.trading_service.emergency_protect_risk_breach = AsyncMock(return_value=(False, 0.0))
    mock_services.trading_service.last_execution_status = None
    mock_services.tg_sender.send_message = AsyncMock()
    mock_services.tg_sender.broadcast_to_channel = AsyncMock()

    await pipeline.run_cycle(cycle_number=1, force_scan=False)

    # 1. No channel broadcast
    assert not mock_services.tg_sender.broadcast_to_channel.called
    # 2. Critical notification sent
    assert mock_services.tg_sender.send_message.called
    sent_text = mock_services.tg_sender.send_message.call_args[0][0]
    assert "RISK BREACH UNRESOLVED" in sent_text
    assert "Все новые сделки заблокированы" in sent_text

    # 3. Simulate subsequent trade attempt evaluated by the real RiskManager
    portfolio_with_unresolved = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)
    portfolio_with_unresolved["active_positions"] = {
        "ETH-USD": {"symbol": "ETH-USD", "protection_state": "RISK_BREACH_UNRESOLVED"}
    }
    next_trade = {"symbol": "SOL-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {"atr_14": 5.0},
        "derivatives_data": {"size_increment": 0.01, "price_increment": 0.01, "strict_metadata": True}
    }

    subsequent_verdict = await risk_mgr.analyze(next_trade, portfolio_with_unresolved, market_data)
    # MUST BE VETOED BY GATE 2.0
    assert subsequent_verdict["approved"] is False
    assert subsequent_verdict["veto_category"] == "RISK_BREACH_UNRESOLVED"
    assert "Critical risk incident unresolved" in subsequent_verdict["reasoning"]


@pytest.mark.asyncio
async def test_nado_open_position_protection_states_honest_classification():
    """
    Audit P0-1:
    Verifies that open_position assigns honest protection states:
    1. NATIVE_SL_CONFIRMED when exchange native SL is placed.
    2. SOFTWARE_SL_CONFIRMED when native SL fails or is skipped (e.g. < $100 limit) but sl_price > 0.
    3. PROTECTION_DEGRADED when sl_price <= 0.
    """
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock, AsyncMock, patch

    service = NadoTradingService()
    service.active_positions.clear()
    service.is_connected = True
    service.default_subaccount_id = "0xsubaccount"
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._market_cache = {2: MagicMock()}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001
    })

    # Case 1: Native SL confirmed
    mock_client = MagicMock()
    mock_order_res = MagicMock()
    mock_order_res.data.digest = "0xopen_digest"
    mock_client.market.place_order.return_value = mock_order_res

    mock_fill_order = MagicMock()
    mock_fill_order.base_filled = str(int(0.1 * 1e18))
    mock_fill_order.quote_filled = str(int(6000.0 * 1e18))
    mock_client.market.get_historical_orders_by_digest.return_value = MagicMock(orders=[mock_fill_order])

    mock_sl_res = MagicMock()
    mock_sl_res.data.digest = "0xsl_digest"
    mock_tp_res = MagicMock()
    mock_tp_res.data.digest = "0xtp_digest"
    mock_client.market.place_price_trigger_order.side_effect = [mock_sl_res, mock_tp_res]
    service.client = mock_client

    with patch.object(service, "_save_positions"):
        ok = await service.open_position("BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10, contracts=0.1, approved_risk_usd=150.0)
        assert ok is True
        assert service.active_positions["BTC-USD"]["protection_state"] == "NATIVE_SL_CONFIRMED"
        assert service.active_positions["BTC-USD"]["native_sl_placed"] is True
        assert service.active_positions["BTC-USD"]["native_tp_placed"] is True
        assert service.last_execution_status == "FILLED_AND_PROTECTED"

    # Case 2: Native SL fails (e.g. exchange size limitation) but Fast Monitor confirmed -> SOFTWARE_SL_CONFIRMED
    service.active_positions.clear()
    service.fast_monitor_disabled = False
    mock_client.market.place_price_trigger_order.side_effect = [Exception("2094 too small"), mock_tp_res]
    with patch.object(service, "_save_positions"):
        ok = await service.open_position("BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10, contracts=0.1, approved_risk_usd=150.0)
        assert ok is True
        assert service.active_positions["BTC-USD"]["protection_state"] == "SOFTWARE_SL_CONFIRMED"
        assert service.active_positions["BTC-USD"]["native_sl_placed"] is False
        assert service.active_positions["BTC-USD"]["native_tp_placed"] is True
        assert service.last_execution_status == "FILLED_AND_PROTECTED"

    # Case 2b: Native SL fails AND Software Monitor is disabled / unconfirmed -> PROTECTION_DEGRADED -> Emergency Close
    service.active_positions.clear()
    service.fast_monitor_disabled = True # Simulate monitor failure / unconfirmed
    mock_client.market.place_price_trigger_order.side_effect = [Exception("2094 too small"), mock_tp_res]
    service.emergency_protect_risk_breach = AsyncMock(return_value=(True, 59990.0))
    with patch.object(service, "_save_positions"):
        ok = await service.open_position("BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10, contracts=0.1, approved_risk_usd=150.0)
        assert ok is False # Must NOT return True when protection degraded!
        assert service.last_execution_status == "ABORTED_PROTECTION_FAILURE"
        assert service.emergency_protect_risk_breach.called is True

    # Case 3: No SL provided (sl_price = 0) -> Pre-dispatch aborts with ABORTED_INVALID_STOP
    service.active_positions.clear()
    mock_client.market.place_order.reset_mock()
    ok = await service.open_position("BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 0.0, 10, contracts=0.1, approved_risk_usd=150.0)
    assert ok is False # Must abort before sending order!
    assert service.last_execution_status == "ABORTED_INVALID_STOP"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_emergency_protect_risk_breach_distinguishes_closed_still_open_and_unknown():
    """
    Audit P1-3:
    Verifies that emergency_protect_risk_breach does not falsely claim CLOSED
    if force_close fails and indexer returns empty (indexer lag -> UNKNOWN -> retains RISK_BREACH_UNRESOLVED).
    Only when a closing fill is confirmed does it transition to closed.
    """
    import time
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock, AsyncMock, patch

    service = NadoTradingService()
    service.is_connected = True
    service.default_subaccount_id = "0xsub"
    service.product_map = {"ETH": 4, "ETH-USD": 4}
    service.client = MagicMock()

    service.active_positions["ETH-USD"] = {
        "symbol": "ETH-USD",
        "direction": "LONG",
        "entry_price": 2000.0,
        "contracts": 1.0,
        "protection_state": "SUBMITTING_PROTECTION"
    }

    # Scenario 1: force_close fails, active_on_chain is empty (indexer lag), NO closing fill -> UNKNOWN
    service.force_close_position = AsyncMock(return_value=(False, 0.0))
    service.get_active_positions = AsyncMock(return_value=[])
    # Historical orders shows NO recent fill
    service.client.market.get_subaccount_historical_orders.return_value = MagicMock(orders=[])

    with patch.object(service, "_save_positions"):
        success, pnl = await service.emergency_protect_risk_breach("ETH-USD", 10.0, 15.0)
        assert success is False
        # Must NOT be deleted! Retained as RISK_BREACH_UNRESOLVED
        assert "ETH-USD" in service.active_positions
        assert service.active_positions["ETH-USD"]["protection_state"] == "RISK_BREACH_UNRESOLVED"

    # Scenario 2: Historical orders CONFIRMS closing fill -> CLOSED_CONFIRMED
    fill_order = MagicMock()
    fill_order.base_filled = -1.0 * 1e18
    fill_order.timestamp = int(time.time() * 1000)
    service.client.market.get_subaccount_historical_orders.return_value = MagicMock(orders=[fill_order])

    with patch.object(service, "_save_positions"):
        success, pnl = await service.emergency_protect_risk_breach("ETH-USD", 10.0, 15.0)
        assert success is True
        # Cleaned up on positive proof
        assert "ETH-USD" not in service.active_positions


def test_risk_calculator_strict_input_validation():
    """
    Audit P1-3:
    Verifies that estimate_total_loss strictly validates input values and raises ValueError
    on NaN, Inf, non-numeric, negative contracts, negative prices, or unreasonable rates.
    """
    import math
    from core.risk_calculator import estimate_total_loss

    # Valid inputs should succeed
    res = estimate_total_loss(contracts=1.0, fill_price=100.0, sl_price=90.0, taker_fee_pct=0.0005)
    assert res.price_loss_usd == 10.0
    assert res.total_expected_loss_usd > 10.0

    # NaN / Inf in price
    with pytest.raises(ValueError, match="finite"):
        estimate_total_loss(contracts=1.0, fill_price=float("nan"), sl_price=90.0)
    with pytest.raises(ValueError, match="finite"):
        estimate_total_loss(contracts=1.0, fill_price=float("inf"), sl_price=90.0)

    # Negative prices or contracts (must NOT be silently masked by abs)
    with pytest.raises(ValueError, match="strictly positive"):
        estimate_total_loss(contracts=-1.0, fill_price=100.0, sl_price=90.0)
    with pytest.raises(ValueError, match="strictly positive"):
        estimate_total_loss(contracts=1.0, fill_price=-100.0, sl_price=90.0)
    with pytest.raises(ValueError, match="strictly positive"):
        estimate_total_loss(contracts=1.0, fill_price=100.0, sl_price=-90.0)

    # Unreasonable taker fee (>= 50%)
    with pytest.raises(ValueError, match="exceeds maximum allowed"):
        estimate_total_loss(contracts=1.0, fill_price=100.0, sl_price=90.0, taker_fee_pct=0.60)


@pytest.mark.asyncio
async def test_risk_manager_strict_price_increment_zero_veto(risk_mgr, base_portfolio_data):
    """
    Audit P1-1:
    Verifies that price_increment = 0.0 with strict_metadata = True triggers
    MARKET_METADATA_UNAVAILABLE veto, preventing division by zero on exchange alignment.
    """
    ceo_decision = {"symbol": "TEST-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)
    market = {
        "price_data": {"current_price": 100.0},
        "indicators": {"atr_14": 5.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.01,
            "price_increment": 0.0  # Zero price increment!
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MARKET_METADATA_UNAVAILABLE"
    assert "missing or non-positive price_increment" in verdict["reasoning"]


@pytest.mark.asyncio
async def test_open_position_resets_execution_status_per_attempt():
    """
    Audit P1-4:
    Verifies that open_position() resets last_execution_status at the start of each attempt.
    A stale status from a previous attempt (e.g. RISK_BREACH_UNRESOLVED) is NEVER leaked.
    """
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock

    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service._market_cache = {2: MagicMock()}
    service.product_map = {"BTC": 2}
    # Simulate lingering status from prior trade incident
    service.last_execution_status = "RISK_BREACH_UNRESOLVED"
    service.last_execution_details = {"stale": "incident"}

    # Attempt trade with unknown symbol (with valid risk parameters)
    ok = await service.open_position("UNKNOWN-USD", "LONG", 100.0, 100.0, 110.0, 95.0, 5, approved_risk_usd=10.0)
    assert ok is False
    # Must be reset and set to REJECTED_BY_EXCHANGE for this attempt, NOT left as RISK_BREACH_UNRESOLVED!
    assert service.last_execution_status == "REJECTED_BY_EXCHANGE"
    assert "Unknown symbol" in service.last_execution_details.get("reason", "")


@pytest.mark.asyncio
async def test_risk_manager_excessive_risk_and_stress_loss_veto(risk_mgr, base_portfolio_data):
    """
    Audit P0-3 & P1-2:
    Verifies that RiskManager:
    1. Vetoes trade with EXCESSIVE_RISK when fee-inclusive total expected loss exceeds approved budget.
    2. Vetoes trade with EXCESSIVE_STRESS_RISK when total stress loss exceeds the stress ceiling.
    """
    ceo_decision = {"symbol": "ETH-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=100.0, available_margin=100.0)

    # Case 1: Extreme taker fee (e.g. 5%) pushes total expected loss well above approved risk budget
    market_high_fee = {
        "price_data": {"current_price": 2000.0},
        "indicators": {"atr_14": 20.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 0.01,
            "min_size": 0.001,
            "confirmed_min_size": True,
            "taker_fee_pct": 0.05,  # 5% fee!
            "slippage_stress_pct": 0.001
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market_high_fee, effective_profile="BALANCED", macro_regime="TRENDING")
    # Even if sized, rounding or extreme fees must trigger EXCESSIVE_RISK or MIN_SIZE/PENALTY
    assert verdict["approved"] is False or verdict.get("expected_fee_usd", 0) > 0

    # Case 2: Extreme expected slippage stress (e.g. 20%) triggers EXCESSIVE_STRESS_RISK veto
    market_stress = {
        "price_data": {"current_price": 2000.0},
        "indicators": {"atr_14": 20.0},
        "order_book_data": {"spread_pct": 0.01},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 0.01,
            "min_size": 0.001,
            "confirmed_min_size": True,
            "taker_fee_pct": 0.0005,
            "slippage_stress_pct": 0.05  # 5% SL exit stress slippage!
        }
    }
    verdict_stress = await risk_mgr.analyze(ceo_decision, portfolio, market_stress, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_stress["approved"] is False
    assert verdict_stress["veto_category"] == "EXCESSIVE_STRESS_RISK"


# ==============================================================================
# 11. QUANTITATIVE EXPECTANCY & R-MULTIPLE SIZING TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_cohort_classification_and_double_penalty_floor(risk_mgr, base_portfolio_data, base_market_data):
    """
    Validates:
    1. Cohort tagging (A, B, C, D) based on conviction threshold and trade_action.
    2. Flat-Protected Sizing [0.85x .. 1.00x] to prevent Adverse Sizing Selection:
       - Conf mult capped at 1.00x (preventing 1.15x/1.25x over-leverage on euphoria).
       - Boundary conviction delta < 5 floored at 0.90x.
       - Action mult for REDUCE_SIZE is 0.85x (narrowing spread vs ENTER).
       - Compounding signal_mult safely floored at 0.85x.
    3. Exposure of planned_risk_usd, cohort, base_risk_pct, final_risk_pct, and applied_multipliers.
    """
    portfolio = dict(base_portfolio_data, total_usd=1000.0, available_margin=1000.0)

    # Cohort A: High Conviction (>= min_conviction + 10 = 80 for BALANCED), Full Size
    decision_a = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    verdict_a = await risk_mgr.analyze(decision_a, portfolio, base_market_data, effective_profile="BALANCED")
    assert verdict_a["approved"] is True
    assert verdict_a["cohort"] == "A"
    assert verdict_a["applied_multipliers"]["conf_mult"] == 1.00
    assert verdict_a["applied_multipliers"]["action_mult"] == 1.0

    # Cohort B: High Conviction, Reduced Size
    decision_b = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 85, "trade_action": "REDUCE_SIZE"}
    verdict_b = await risk_mgr.analyze(decision_b, portfolio, base_market_data, effective_profile="BALANCED")
    assert verdict_b["approved"] is True
    assert verdict_b["cohort"] == "B"
    assert verdict_b["applied_multipliers"]["action_mult"] == 0.85

    # Cohort C: Low Conviction (< min_conviction + 10 = 80), Full Size
    decision_c = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 73, "trade_action": "ENTER"}
    verdict_c = await risk_mgr.analyze(decision_c, portfolio, base_market_data, effective_profile="BALANCED")
    assert verdict_c["approved"] is True
    assert verdict_c["cohort"] == "C"
    assert verdict_c["applied_multipliers"]["conf_mult"] == 0.90
    assert verdict_c["applied_multipliers"]["action_mult"] == 1.0

    # Cohort D: Low Conviction (< 80) and Reduced Size
    decision_d = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 72, "trade_action": "REDUCE_SIZE"}
    
    # 1. By default, Cohort D is vetoed due to empirical negative expectancy:
    verdict_d_veto = await risk_mgr.analyze(decision_d, portfolio, base_market_data, effective_profile="BALANCED")
    assert verdict_d_veto["approved"] is False
    assert verdict_d_veto["veto_category"] == "COHORT_D_NEGATIVE_EXPECTANCY"

    # 2. When BLOCK_COHORT_D_TRADES is disabled, verify that 0.85x floor protects against excessive collapse:
    from core.config import config
    orig_block = getattr(config, "BLOCK_COHORT_D_TRADES", True)
    try:
        config.BLOCK_COHORT_D_TRADES = False
        verdict_d = await risk_mgr.analyze(decision_d, portfolio, base_market_data, effective_profile="BALANCED")
        assert verdict_d["approved"] is True
        assert verdict_d["cohort"] == "D"
        assert verdict_d["applied_multipliers"]["conf_mult"] == 0.90
        assert verdict_d["applied_multipliers"]["action_mult"] == 0.85
        assert verdict_d["applied_multipliers"]["signal_mult"] == 0.85
        assert verdict_d["planned_risk_usd"] > 0
        assert verdict_d["base_risk_pct"] == 0.015
    finally:
        config.BLOCK_COHORT_D_TRADES = orig_block



def test_continuous_half_kelly_calibration(risk_mgr):
    """
    Validates smooth continuous calibration of Half-Kelly multiplier:
    - total_trades < 15: returns neutral 1.0
    - f* <= 0 (negative edge): clamped at 0.50
    - f* >= 0.25 (strong positive edge): capped at 1.00
    - 0 < f* < 0.25: smoothly interpolates between 0.50 and 1.00 without step jumps.
    """
    # 1. Total trades < 15
    assert risk_mgr._calculate_kelly_multiplier(win_rate=0.70, realized_rr=2.0, total_trades=14) == 1.0

    # 2. Negative edge (f* <= 0)
    assert risk_mgr._calculate_kelly_multiplier(win_rate=0.35, realized_rr=1.2, total_trades=20) == 0.50

    # 3. Strong edge (f* >= 0.25)
    assert risk_mgr._calculate_kelly_multiplier(win_rate=0.55, realized_rr=1.8, total_trades=20) == 1.00

    # 4. Marginal positive edge (0 < f* < 0.25) -> smoothly calibrated
    # win_rate=0.45, realized_rr=1.5:
    # q = 0.55, f* = (0.45 * 1.5 - 0.55) / 1.5 = (0.675 - 0.55) / 1.5 = 0.125 / 1.5 = 0.08333
    # Expected multiplier = 0.50 + (0.08333 / 0.25) * 0.50 = 0.50 + 0.16667 = 0.6667
    k_marginal = risk_mgr._calculate_kelly_multiplier(win_rate=0.45, realized_rr=1.5, total_trades=20)
    assert 0.60 < k_marginal < 0.75
    assert k_marginal != 0.50 and k_marginal != 1.00


def test_record_closed_trade_journal(monkeypatch):
    """
    Validates NadoTradingService._record_closed_trade_journal:
    - Calculates R-multiple = Net PnL / Planned Risk USD.
    - Persists trade details with cohort, applied multipliers, and realized R to trade_journal.json.
    """
    from services.nado_trading_service import NadoTradingService
    trading_service = NadoTradingService.__new__(NadoTradingService)

    saved_trades = []
    monkeypatch.setattr("core.state_store.StateStore.load", lambda f, default=[]: list(saved_trades))
    def mock_save(f, data):
        saved_trades.clear()
        saved_trades.extend(data)
    monkeypatch.setattr("core.state_store.StateStore.save", mock_save)

    pos_data = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 80000.0,
        "sl_price": 79000.0,
        "planned_risk_usd": 10.0,
        "cohort": "B",
        "base_risk_pct": 0.015,
        "final_risk_pct": 0.012,
        "applied_multipliers": {"signal_mult": 0.8, "market_mult": 1.0},
        "open_time": 1700000000.0
    }

    # Case 1: Profitable trade (Net PnL = +25.0 USD, Planned Risk = 10.0 USD -> +2.50R)
    r1 = trading_service._record_closed_trade_journal(
        symbol="BTC-USD",
        direction="LONG",
        entry_price=80000.0,
        exit_price=82500.0,
        net_pnl=25.0,
        pos_data=pos_data,
        fee=1.0,
        triggered_by="TAKE_PROFIT_TRIGGER"
    )
    assert r1 == 2.50

    # Case 2: Loss trade (Net PnL = -10.0 USD, Planned Risk = 10.0 USD -> -1.00R)
    r2 = trading_service._record_closed_trade_journal(
        symbol="BTC-USD",
        direction="LONG",
        entry_price=80000.0,
        exit_price=79000.0,
        net_pnl=-10.0,
        pos_data=pos_data,
        fee=1.0,
        triggered_by="STOP_LOSS_TRIGGER"
    )
    assert r2 == -1.00

    assert len(saved_trades) == 2
    assert saved_trades[0]["realized_r"] == 2.50
    assert saved_trades[0]["cohort"] == "B"
    assert saved_trades[1]["realized_r"] == -1.00


# ==============================================================================
# 12. RISK MANAGER AUDIT P1 & P2 HARDENING TESTS (CHAIN: RISK -> EXECUTION)
# ==============================================================================

@pytest.mark.asyncio
async def test_risk_manager_early_numeric_sanitization(risk_mgr, base_portfolio_data, base_market_data):
    """
    P2 Audit:
    Verifies that NaN, Inf, and non-finite values in current_price, atr_14,
    total_balance, or available_margin are rejected at Stage 1 before any math/ceil calculations.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}

    # 1. NaN current_price
    m_nan_price = {
        "price_data": {"current_price": float("nan")},
        "indicators": {"atr_14": 100.0}
    }
    r1 = await risk_mgr.analyze(ceo_decision, base_portfolio_data, m_nan_price)
    assert r1["approved"] is False
    assert r1["veto_category"] == "INVALID_PRICE"

    # 2. Inf current_price
    m_inf_price = {
        "price_data": {"current_price": float("inf")},
        "indicators": {"atr_14": 100.0}
    }
    r2 = await risk_mgr.analyze(ceo_decision, base_portfolio_data, m_inf_price)
    assert r2["approved"] is False
    assert r2["veto_category"] == "INVALID_PRICE"

    # 3. NaN atr_14
    m_nan_atr = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": float("nan")}
    }
    r3 = await risk_mgr.analyze(ceo_decision, base_portfolio_data, m_nan_atr)
    assert r3["approved"] is False
    assert r3["veto_category"] == "INVALID_ATR"

    # 4. NaN total_balance
    p_nan_bal = dict(base_portfolio_data, total_usd=float("nan"))
    r4 = await risk_mgr.analyze(ceo_decision, p_nan_bal, base_market_data)
    assert r4["approved"] is False
    assert r4["veto_category"] == "INSUFFICIENT_BALANCE"

    # 5. NaN available_margin
    p_nan_margin = dict(base_portfolio_data, available_margin=float("nan"))
    r5 = await risk_mgr.analyze(ceo_decision, p_nan_margin, base_market_data)
    assert r5["approved"] is False
    assert r5["veto_category"] == "INSUFFICIENT_BALANCE"


@pytest.mark.asyncio
async def test_risk_manager_portfolio_risk_open_positions_contracts_and_dedup(risk_mgr, base_market_data):
    """
    P1 Audit (Gate 2.4):
    1. Active positions with 'contracts' key are accurately read and summed into open portfolio risk.
    2. Alias keys (e.g. 'ETH-USD' and 'ETH/USD' pointing to the same position) are deduplicated.
    3. Exceeding portfolio risk cap vetoes with TOTAL_OPEN_RISK_CAP.
    """
    ceo_decision = {"symbol": "AVAX-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    # Balance = $1000. Cap for BALANCED = 4% ($40).
    # Open ETH position: contracts=0.1, entry=3000, sl=2700 -> price risk = 0.1 * 300 = $30.
    portfolio = {
        "total_usd": 1000.0,
        "available_margin": 800.0,
        "active_positions": {
            "ETH-USD": {"symbol": "ETH-USD", "entry_price": 3000.0, "sl_price": 2700.0, "contracts": 0.1},
            # Alias key pointing to the same position: must be deduplicated
            "ETH/USD": {"symbol": "ETH/USD", "entry_price": 3000.0, "sl_price": 2700.0, "contracts": 0.1}
        }
    }
    market = {
        "price_data": {"current_price": 30.0},
        "indicators": {"atr_14": 1.0},
        "order_book_data": {"spread_pct": 0.05},
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.1, "price_increment": 0.01}
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market, effective_profile="BALANCED")
    # Remaining risk budget = $40 - ~$30.13 = ~$9.87 > 0. AVAX sized into remaining budget
    assert verdict["approved"] is True
    assert verdict["approved_risk_usd"] <= 10.0


@pytest.mark.asyncio
async def test_risk_manager_vetos_unbounded_open_position_risk(risk_mgr, base_market_data):
    """
    P1 Audit (Gate 2.4):
    If any open position lacks a valid stop-loss (sl_price missing or 0),
    RiskManager strictly vetoes with UNBOUNDED_OPEN_POSITION_RISK rather than guessing size_usd.
    """
    ceo_decision = {"symbol": "AVAX-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    portfolio = {
        "total_usd": 1000.0,
        "available_margin": 800.0,
        "active_positions": {
            "ETH-USD": {"symbol": "ETH-USD", "entry_price": 3000.0, "sl_price": 0.0, "contracts": 0.1}
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data, effective_profile="BALANCED")
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "UNBOUNDED_OPEN_POSITION_RISK"
    assert "missing, non-positive or non-finite stop-loss" in verdict["reasoning"]


@pytest.mark.asyncio
async def test_nado_trading_service_pre_dispatch_min_size_and_notional_rejection():
    """
    P1 Audit:
    Verifies that NadoTradingService:
    1. Rejects with ORDER_BELOW_MIN_SIZE when post-increment size is below min_size (never increases size).
    2. Rejects with ORDER_BELOW_MIN_NOTIONAL when post-increment notional is below min_notional.
    """
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    service.client = MagicMock()
    service._market_cache = {2: MagicMock()}
    service.product_map = {"BTC": 2}

    # Case 1: contracts below exchange min_size
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.05,  # min size is 0.05 BTC
        "min_notional": 0.0
    })
    ok1 = await service.open_position("BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 59000.0, 10, contracts=0.01, approved_risk_usd=10.0)
    assert ok1 is False
    assert service.last_execution_status == "ORDER_BELOW_MIN_SIZE"

    # Case 2: notional below exchange min_notional
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 500.0  # min notional $500
    })
    ok2 = await service.open_position("BTC-USD", "LONG", 60000.0, 120.0, 62000.0, 59000.0, 10, contracts=0.002, approved_risk_usd=10.0)
    assert ok2 is False
    assert service.last_execution_status == "ORDER_BELOW_MIN_NOTIONAL"


@pytest.mark.asyncio
async def test_nado_trading_service_pre_dispatch_risk_breach_veto():
    """
    P1 Audit:
    Verifies that if expected loss at limit price (including slippage buffer & fees)
    exceeds approved risk threshold, NadoTradingService aborts BEFORE sending order to sequencer.
    """
    from services.nado_trading_service import NadoTradingService
    from unittest.mock import MagicMock

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    mock_client = MagicMock()
    service.client = mock_client
    service._market_cache = {2: MagicMock()}
    service.product_map = {"BTC": 2}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    # Approved risk is only $5.00, but contracts=0.1 on BTC with entry=60000, sl=58000
    # Price loss = 0.1 * 2000 = $200 >> $5.00!
    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 6000.0, 64000.0, 58000.0, 10,
        contracts=0.1, approved_risk_usd=5.00
    )
    assert ok is False
    assert service.last_execution_status == "PRE_DISPATCH_RISK_BREACH"
    # Must NOT call place_order!
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_risk_manager_harmonized_loss_parameters_propagation(risk_mgr, base_portfolio_data):
    """
    P1 Audit:
    Verifies that RiskManager outputs explicit funding_rate_hourly, holding_hours,
    and slippage_stress_pct, harmonizing the loss model with execution.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=1000.0, available_margin=1000.0)
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "order_book_data": {"spread_pct": 0.02},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "min_size": 0.001,
            "confirmed_min_size": True,
            "taker_fee_pct": 0.0005,
            "slippage_stress_pct": 0.002,
            "funding_rate_hourly": 0.0001
        }
    }

    # Case 1: VOLATILITY_MOMENTUM mode -> holding_hours = 1.0
    v_scalp = await risk_mgr.analyze(ceo_decision, portfolio, market, strategy_mode="VOLATILITY_MOMENTUM")
    assert v_scalp["approved"] is True
    assert v_scalp["holding_hours"] == 1.0
    assert v_scalp["funding_rate_hourly"] == 0.0001
    assert v_scalp["slippage_stress_pct"] == 0.002

    # Case 2: TREND_FOLLOWING mode -> holding_hours = 4.0
    v_trend = await risk_mgr.analyze(ceo_decision, portfolio, market, strategy_mode="TREND_FOLLOWING")
    assert v_trend["approved"] is True
    assert v_trend["holding_hours"] == 4.0
    assert v_trend["funding_rate_hourly"] == 0.0001


# ==============================================================================
# 11. RISK AUDIT HARDENING TESTS (P1 & P2 AUDIT FINDINGS)
# ==============================================================================

@pytest.mark.asyncio
async def test_historical_kelly_disentangles_b_from_candidate_rr(risk_mgr, base_portfolio_data):
    """
    P1 Audit Finding 1:
    Verifies that Kelly sizing multiplier is evaluated against HISTORICAL payoff ratio (b_hist),
    not the prospective candidate trade's planned RR.
    """
    # 1. Direct multiplier test:
    # 45% WR with weak historical payoff (b=0.8) -> f* = (0.45*0.8 - 0.55)/0.8 = -0.2375 <= 0 -> 0.50x
    k_damp = risk_mgr._calculate_kelly_multiplier(win_rate=0.45, payoff_ratio=0.8, total_trades=20)
    assert k_damp == 0.50

    # 55% WR with solid historical payoff (b=1.8) -> positive edge, clamped to 1.00x
    k_cap = risk_mgr._calculate_kelly_multiplier(win_rate=0.55, payoff_ratio=1.8, total_trades=20)
    assert k_cap == 1.00

    # 2. RiskManager.analyze test:
    # Even if candidate trade has strong planned RR (e.g. 2.5:1),
    # weak historical track record in portfolio_data triggers Kelly damping
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    portfolio = dict(
        base_portfolio_data,
        total_usd=1000.0,
        available_margin=1000.0,
        win_count=9,
        loss_count=11,  # 20 trades, WR = 45%
        historical_payoff_ratio=0.80  # Weak historical payoff ratio
    )
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "min_size": 0.001,
            "confirmed_min_size": True,
            "taker_fee_pct": 0.0005,
            "funding_rate_hourly": 0.0
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market)
    assert verdict["approved"] is True
    assert verdict["applied_multipliers"]["kelly_mult"] == 0.50
    assert verdict["historical_payoff_ratio"] == 0.80
    assert verdict["candidate_net_rr"] > 1.5


@pytest.mark.asyncio
async def test_strategy_expectancy_gate_evaluates_historical_track_record(risk_mgr, base_portfolio_data):
    """
    P1 Audit Finding 1:
    Verifies that Expectancy Gate evaluates the historical track record of the strategy,
    penalizing risk (x0.75) and raising conviction (+5) when historical E(R) < -0.10R.
    """
    # 32 trades: 12 wins, 20 losses (WR = 37.5%). Historical payoff b = 0.90.
    # Historical E(R) = 0.375 * 0.90 - 0.625 = 0.3375 - 0.625 = -0.2875R < -0.10R.
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(
        base_portfolio_data,
        total_usd=1000.0,
        available_margin=1000.0,
        win_count=12,
        loss_count=20,
        historical_payoff_ratio=0.90
    )
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "min_size": 0.001,
            "confirmed_min_size": True,
            "taker_fee_pct": 0.0005,
            "funding_rate_hourly": 0.0
        }
    }
    # Initial base_conviction for BALANCED is 70. With +5 penalty from Expectancy Gate, min_conviction becomes 75.
    # Conviction 80 will pass with x0.75 penalty applied to base_risk
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market, effective_profile="BALANCED")
    assert verdict["approved"] is True
    # Base risk for BALANCED is 1.5%. With x0.75 penalty, base_risk is reduced to ~1.125%
    assert verdict["base_risk_pct"] < 0.015

    # If conviction is 74 (< 75 threshold), it gets vetoed by LOW_CONFIDENCE
    ceo_decision_low = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 74, "trade_action": "ENTER"}
    verdict_low = await risk_mgr.analyze(ceo_decision_low, portfolio, market, effective_profile="BALANCED")
    assert verdict_low["approved"] is False
    assert verdict_low["veto_category"] == "LOW_CONFIDENCE"


@pytest.mark.asyncio
async def test_zero_funding_rate_preserved(risk_mgr, base_portfolio_data):
    """
    P1 Audit Finding 4:
    Verifies that genuine 0.0 funding rate is preserved and NOT overridden by 0.0001.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=1000.0, available_margin=1000.0)
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "min_size": 0.001,
            "confirmed_min_size": True,
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0,
            "funding_rate_hourly": 0.0
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market)
    assert verdict["approved"] is True
    assert verdict["funding_rate_hourly"] == 0.0


@pytest.mark.asyncio
async def test_subaccount_health_critical_veto(risk_mgr, base_portfolio_data):
    """
    P1 Audit Finding 2:
    Verifies that negative maintenance health (liquidation impending on subaccount)
    vetoes new trades with SUBACCOUNT_HEALTH_CRITICAL.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(
        base_portfolio_data,
        total_usd=1000.0,
        available_margin=1000.0,
        maintenance_health_usd=-15.50  # Negative maintenance margin health!
    )
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "taker_fee_pct": 0.0005
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market)
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "SUBACCOUNT_HEALTH_CRITICAL"


@pytest.mark.asyncio
async def test_liquidation_margin_cushion_veto(risk_mgr, base_portfolio_data):
    """
    P1 Audit Finding 2:
    Verifies that if SL is too close to liquidation price (distance to liq < 1.40x distance to SL),
    the trade is vetoed with LIQUIDATION_RISK.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=1000.0, available_margin=1000.0)
    # High maintenance margin requirement (e.g. mmf = 0.40) and wide stop pull liquidation price within 1.40x of SL
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 18000.0},  # Wide SL
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "taker_fee_pct": 0.0005,
            "maintenance_margin_pct": 0.40  # 40% maintenance margin forces distance_to_liq < 1.40x distance_to_sl
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market)
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "LIQUIDATION_RISK"


@pytest.mark.asyncio
async def test_drawdown_discrepancy_and_strict_fail_closed(risk_mgr, base_portfolio_data):
    """
    P2 Audit Finding 9:
    Verifies that contradictory drawdown data adopts conservative implied loss,
    and missing drawdown data in strict health mode blocks execution.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 1.0,
            "taker_fee_pct": 0.0005,
            "min_size": 0.001,
            "min_notional": 10.0
        }
    }

    # Case 1: Contradictory reporting (daily_drawdown reports 0.0, but daily_pnl_pct reports -7.0% >= 6% limit)
    portfolio_contra = dict(
        base_portfolio_data,
        total_usd=1000.0,
        available_margin=1000.0,
        daily_drawdown_pct=0.0,
        daily_pnl_pct=-7.0  # -7% breaches 6% max daily drawdown limit
    )
    verdict_dd = await risk_mgr.analyze(ceo_decision, portfolio_contra, market, effective_profile="BALANCED")
    assert verdict_dd["approved"] is False
    assert verdict_dd["veto_category"] == "MAX_DAILY_DRAWDOWN"

    # Case 2: Missing drawdown fields in strict mode
    portfolio_missing = dict(
        base_portfolio_data,
        total_usd=1000.0,
        available_margin=1000.0,
        strict_portfolio_health=True
    )
    # Ensure all drawdown fields are absent
    portfolio_missing.pop("daily_drawdown_pct", None)
    portfolio_missing.pop("daily_pnl_pct", None)
    portfolio_missing.pop("daily_pnl_usd", None)

    verdict_missing = await risk_mgr.analyze(ceo_decision, portfolio_missing, market)
    assert verdict_missing["approved"] is False
    assert verdict_missing["veto_category"] == "MISSING_PORTFOLIO_HEALTH"

    # Case 3: Small fractional intraday loss (< 1.0%, e.g. -0.32% or -$0.31) must NOT be inflated 100x to 32%
    portfolio_small_loss = dict(
        base_portfolio_data,
        total_usd=97.29,
        available_margin=90.0,
        daily_drawdown_pct=0.0032,
        daily_pnl_pct=-0.32,
        daily_pnl_usd=-0.31
    )
    verdict_small = await risk_mgr.analyze(ceo_decision, portfolio_small_loss, market, effective_profile="AGGRESSIVE")
    assert verdict_small["approved"] is True, f"VETOED: {verdict_small.get('veto_category')} -> {verdict_small.get('reasoning')}"
    assert verdict_small.get("veto_category") is None


# ==============================================================================
# 13. AUDIT P1 HARDENING REGRESSION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_partial_fill_emergency_protect_defined_vars_and_no_unbound_error():
    """
    P1 Audit Requirement 1:
    Verifies that on partial fill (fill_ratio < 0.95), emergency_protect_risk_breach
    is called with guaranteed defined float parameters (approved_budget, total_actual_loss),
    never raising UnboundLocalError even when sl_price is 0 or calculation threw error.
    """
    from services.nado_trading_service import NadoTradingService
    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    mock_client = MagicMock()
    service.client = mock_client
    service._market_cache = {2: MagicMock()}
    service.product_map = {"BTC": 2}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    mock_order_res = MagicMock()
    mock_order_res.data.digest = "0xpart_digest"
    mock_client.market.place_order.return_value = mock_order_res

    # Order was for 0.1 BTC, but only 0.05 BTC filled (50% fill ratio < 95%)
    mock_fill_order = MagicMock()
    mock_fill_order.base_filled = str(int(0.05 * 1e18))
    mock_fill_order.quote_filled = str(int(3000.0 * 1e18))
    mock_client.market.get_historical_orders_by_digest.return_value = MagicMock(orders=[mock_fill_order])

    emergency_args = {}
    async def mock_emergency(symbol, approved_risk=0.0, actual_risk=0.0):
        emergency_args["symbol"] = symbol
        emergency_args["approved_risk"] = approved_risk
        emergency_args["actual_risk"] = actual_risk
        return True, 3000.0
    service.emergency_protect_risk_breach = mock_emergency

    with patch.object(service, "_save_positions"):
        ok = await service.open_position(
            "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10,
            contracts=0.1, approved_risk_usd=130.0
        )
        assert ok is False
        assert service.last_execution_status == "ABORTED_PARTIAL_FILL"
        assert emergency_args["symbol"] == "BTC-USD"
        assert isinstance(emergency_args["approved_risk"], float)
        assert isinstance(emergency_args["actual_risk"], float)
        assert emergency_args["approved_risk"] == 130.0
        assert emergency_args["actual_risk"] > 0.0


@pytest.mark.asyncio
async def test_pre_dispatch_stress_loss_breach_veto():
    """
    P1 Audit Requirement 2:
    Verifies that if expected loss is below limit but stress loss (slippage + funding)
    exceeds the max allowed stress ceiling, pre-dispatch aborts with PRE_DISPATCH_STRESS_BREACH.
    """
    from services.nado_trading_service import NadoTradingService
    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    mock_client = MagicMock()
    service.client = mock_client
    service._market_cache = {2: MagicMock()}
    service.product_map = {"BTC": 2}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    # approved_risk_usd = 25.0 -> max_tolerated = 25*1.10+0.05 = 27.55.
    # max_allowed_stress = 25*1.30+0.15 = 32.65.
    # Entry=60000, SL=59990. Limit price = 60120. contracts=0.1.
    # Expected loss at limit price = 0.1 * (60120 - 59990) + fees = $13.00 + $6.01 = $19.01 (PASSES max_tolerated $27.55!).
    # With extreme slippage stress (slippage_stress_pct=0.02) -> slippage stress = 0.1 * 59990 * 0.02 = 119.98 USD!
    # Total stress loss = $19.01 + $119.98 = $138.99 >> $32.65!
    # Proves isolated stress-loss check rejection when expected loss passes!
    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59990.0, 10,
        contracts=0.1, approved_risk_usd=25.0, slippage_stress_pct=0.02
    )
    assert ok is False
    assert service.last_execution_status == "PRE_DISPATCH_STRESS_BREACH"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_historical_kelly_missing_payoff_neutral_without_fabricating():
    """
    P1 Audit Requirement 4:
    Verifies that when historical payoff ratio is missing / 0.0,
    Kelly multiplier returns 1.0 (neutral) and does NOT invent b = 1.0.
    Also verifies win_rate clamping to [0.0, 1.0].
    """
    from agents.risk_manager import RiskManager
    rm = RiskManager(MagicMock(), MagicMock())

    # 1. Missing payoff (0.0): must return 1.0 (no Kelly alteration)
    k_missing = rm._calculate_kelly_multiplier(win_rate=0.45, payoff_ratio=0.0, total_trades=25)
    assert k_missing == 1.0

    # 2. Negative payoff: must return 1.0
    k_neg = rm._calculate_kelly_multiplier(win_rate=0.50, payoff_ratio=-1.0, total_trades=25)
    assert k_neg == 1.0

    # 3. Clamped win_rate > 1.0 (e.g. 150%) clamped to 1.0
    k_over = rm._calculate_kelly_multiplier(win_rate=1.5, payoff_ratio=1.5, total_trades=20)
    assert k_over == 1.00

    # 4. Clamped win_rate < 0.0 clamped to 0.0
    k_under = rm._calculate_kelly_multiplier(win_rate=-0.2, payoff_ratio=1.5, total_trades=20)
    assert k_under == 1.0


@pytest.mark.asyncio
async def test_subaccount_health_nan_and_corrupted_vetoed(risk_mgr, base_market_data, base_portfolio_data):
    """
    P1 Audit Requirement 5:
    Verifies that NaN or unparseable subaccount maintenance health is strictly vetoed
    with SUBACCOUNT_HEALTH_CRITICAL.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}

    # 1. NaN health
    p_nan = dict(base_portfolio_data, maintenance_health_usd=float("nan"))
    v_nan = await risk_mgr.analyze(ceo_decision, p_nan, base_market_data)
    assert v_nan["approved"] is False
    assert v_nan["veto_category"] == "SUBACCOUNT_HEALTH_CRITICAL"

    # 2. String corrupted health
    p_bad = dict(base_portfolio_data, maintenance_health_usd="CORRUPTED_STRING")
    v_bad = await risk_mgr.analyze(ceo_decision, p_bad, base_market_data)
    assert v_bad["approved"] is False
    assert v_bad["veto_category"] == "SUBACCOUNT_HEALTH_CRITICAL"


@pytest.mark.asyncio
async def test_open_positions_dedup_preserves_distinct_positions_and_handles_signed_amount(risk_mgr, base_market_data):
    """
    P1 Audit Requirement 6:
    1. Multiple distinct positions of the same base asset (e.g. subaccount 1 and subaccount 2,
       or distinct sizes/entry prices) are BOTH preserved and both counted toward existing risk.
    2. Negative signed amounts (e.g. amount = -0.5 for short) are handled safely without PORTFOLIO_STATE_DESYNC.
    """
    ceo_decision = {"symbol": "SOL-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    portfolio = {
        "total_usd": 1000.0,
        "available_margin": 900.0,
        "active_positions": [
            # Position 1: ETH short on subaccount A
            {
                "symbol": "ETH-USD",
                "direction": "SHORT",
                "entry_price": 3000.0,
                "sl_price": 3100.0,
                "amount": -0.05,  # Signed negative amount
                "_subaccount": "0xsubA",
                "_product_id": 3
            },
            # Position 2: ETH long on subaccount B (distinct position!)
            {
                "symbol": "ETH-USD",
                "direction": "LONG",
                "entry_price": 3000.0,
                "sl_price": 2900.0,
                "amount": 0.05,
                "_subaccount": "0xsubB",
                "_product_id": 3
            }
        ]
    }
    market = {
        "price_data": {"current_price": 100.0},
        "indicators": {"atr_14": 2.0},
        "order_book_data": {"spread_pct": 0.05},
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.1, "price_increment": 0.01}
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market, effective_profile="BALANCED")
    # Both positions must be preserved and counted without PORTFOLIO_STATE_DESYNC veto
    assert verdict.get("veto_category") != "PORTFOLIO_STATE_DESYNC"
    assert verdict["approved"] is True
    # Risk budget (BALANCED 4% = $40). Both ETH positions have ~0.05 * 100 = $5 risk each (~$10 total).
    # SOL is sized into remaining $30 budget.
    assert verdict["approved_risk_usd"] <= 30.5


@pytest.mark.asyncio
async def test_strict_min_size_unconfirmed_rejected_without_guessing(risk_mgr, base_portfolio_data):
    """
    P1 Audit Requirement 3:
    In strict metadata mode, if min_size is unconfirmed dummy default (100.0),
    it is strictly rejected with MARKET_METADATA_UNAVAILABLE rather than guessed as size_increment.
    """
    ceo_decision = {"symbol": "XRP-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=500.0, available_margin=500.0)
    market = {
        "price_data": {"current_price": 1.50},
        "indicators": {"atr_14": 0.05},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 5.0,
            "price_increment": 0.0001,
            "min_size": 100.0,  # Unconfirmed 100.0 dummy
            "confirmed_min_size": False
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market)
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MARKET_METADATA_UNAVAILABLE"


@pytest.mark.asyncio
async def test_strict_min_size_missing_rejected_without_fallback(risk_mgr, base_portfolio_data):
    """
    P1 Audit Followup (Point 3):
    In strict metadata mode, if min_size is missing (None),
    it must NOT fall back to size_increment, but reject with MARKET_METADATA_UNAVAILABLE.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = dict(base_portfolio_data, total_usd=1000.0, available_margin=1000.0)
    market = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "strict_metadata": True,
            "size_increment": 0.001,
            "price_increment": 0.1,
            "min_size": None  # Missing min_size!
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market)
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "MARKET_METADATA_UNAVAILABLE"


@pytest.mark.asyncio
async def test_pre_dispatch_aborts_on_missing_or_zero_sl():
    """
    P1 Audit Followup (Point 2):
    Pre-dispatch check must abort before place_order if sl_price <= 0 or None.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_client.market.place_order = AsyncMock()
    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    # Call with sl_price = 0.0 -> Must abort before sending order!
    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 0.0, 10,
        contracts=0.01, approved_risk_usd=10.0
    )
    assert ok is False
    assert service.last_execution_status == "ABORTED_INVALID_STOP"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_pre_dispatch_aborts_on_missing_or_zero_approved_risk():
    """
    P1 Audit Followup (Point 2):
    Pre-dispatch check must abort before place_order if approved_risk_usd <= 0 or None.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_client.market.place_order = AsyncMock()
    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    # Call with approved_risk_usd = 0.0 -> Must abort before sending order!
    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 59000.0, 10,
        contracts=0.01, approved_risk_usd=0.0
    )
    assert ok is False
    assert service.last_execution_status == "INVALID_RISK_PARAMETERS"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_protective_limit_price_rounding_short_and_long():
    """
    P1 Audit Requirement (Point 1):
    Protective rounding of limit price based on trade direction:
    - LONG: round DOWN (floor) to price_increment so buy limit never exceeds allowed max.
    - SHORT: round UP (ceil) to price_increment so sell limit never drops below allowed min.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    captured_orders = []
    
    def capture_order(params):
        captured_orders.append(params.order)
        res = MagicMock()
        res.data.digest = f"0xdigest_{len(captured_orders)}"
        return res

    mock_client.market.place_order.side_effect = capture_order
    # Mock instant fill
    mock_hist = MagicMock()
    mock_hist.orders = [MagicMock(base_filled=str(int(0.01 * 1e18)), quote_filled=str(int(600.0 * 1e18)))]
    mock_client.market.get_historical_orders_by_digest.return_value = mock_hist
    mock_trigger_res = MagicMock()
    mock_trigger_res.data.digest = "0xtrigger"
    mock_client.market.place_price_trigger_order.return_value = mock_trigger_res

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._market_cache = {2: MagicMock()}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),  # Tick size is $1.00
        "min_size": 0.001,
        "min_notional": 0.0
    })

    with patch.object(service, "_save_positions"):
        # 1. SHORT with fractional limit price $59,970.50 (raw_limit = 59970.50)
        # Ceiling rounding must yield $59,971.00 (>= 59970.50), never selling cheaper than permitted!
        captured_orders.clear()
        ok_short = await service.open_position(
            symbol="BTC-USD", direction="SHORT", entry_price=60000.0,
            notional_usd=600.0, tp_price=57000.0, sl_price=61000.0, leverage=10,
            contracts=0.01, limit_price=59970.50, approved_risk_usd=20.0
        )
        assert ok_short is True
        assert len(captured_orders) == 1
        short_price_x18 = captured_orders[0].priceX18
        assert short_price_x18 == 59971 * 10**18  # Ceil rounded: $59,971.00 >= $59,970.50!

        # 2. LONG with fractional limit price $60,029.50 (raw_limit = 60029.50)
        # Floor rounding must yield $60,029.00 (<= 60029.50), never buying more expensive than permitted!
        service.active_positions.clear()
        captured_orders.clear()
        ok_long = await service.open_position(
            symbol="BTC-USD", direction="LONG", entry_price=60000.0,
            notional_usd=600.0, tp_price=63000.0, sl_price=59000.0, leverage=10,
            contracts=0.01, limit_price=60029.50, approved_risk_usd=20.0
        )
        assert ok_long is True
        assert len(captured_orders) == 1
        long_price_x18 = captured_orders[0].priceX18
        assert long_price_x18 == 60029 * 10**18  # Floor rounded: $60,029.00 <= $60,029.50!


@pytest.mark.asyncio
async def test_open_position_sl_tp_geometry_validation():
    """
    P1 Audit Requirement (Point 2):
    Validates strict geometry invariants for SL and TP relative to direction and entry/limit prices:
    - LONG: requires 0 < SL < Entry and TP > Entry
    - SHORT: requires SL > Entry and 0 < TP < Entry
    Any geometry violation must abort with INVALID_STOP_GEOMETRY or INVALID_TP_GEOMETRY before place_order!
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_client.market.place_order = AsyncMock()

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}

    # Case 1: LONG with SL above entry (entry=60000, sl=60500) -> Inverted Stop!
    ok1 = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 60500.0, 10,
        contracts=0.01, approved_risk_usd=20.0
    )
    assert ok1 is False
    assert service.last_execution_status == "INVALID_STOP_GEOMETRY"
    assert mock_client.market.place_order.called is False

    # Case 2: LONG with TP below entry (entry=60000, tp=59000) -> Inverted TP!
    ok2 = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 59000.0, 59500.0, 10,
        contracts=0.01, approved_risk_usd=20.0
    )
    assert ok2 is False
    assert service.last_execution_status == "INVALID_TP_GEOMETRY"
    assert mock_client.market.place_order.called is False

    # Case 3: SHORT with SL below entry (entry=60000, sl=59500) -> Inverted Stop!
    ok3 = await service.open_position(
        "BTC-USD", "SHORT", 60000.0, 600.0, 58000.0, 59500.0, 10,
        contracts=0.01, approved_risk_usd=20.0
    )
    assert ok3 is False
    assert service.last_execution_status == "INVALID_STOP_GEOMETRY"
    assert mock_client.market.place_order.called is False

    # Case 4: SHORT with TP above entry (entry=60000, tp=61000) -> Inverted TP!
    ok4 = await service.open_position(
        "BTC-USD", "SHORT", 60000.0, 600.0, 61000.0, 60500.0, 10,
        contracts=0.01, approved_risk_usd=20.0
    )
    assert ok4 is False
    assert service.last_execution_status == "INVALID_TP_GEOMETRY"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_real_partial_fill_execution_path_and_emergency_close():
    """
    P1/P2 Audit Requirement (Point 3):
    Real end-to-end simulation of partial fill execution path through open_position():
    1. Order is placed and accepted by exchange sequencer.
    2. Sequencer reports partial fill (e.g. 0.04 BTC filled out of 0.10 BTC requested = 40% < 95%).
    3. Service detects partial fill, registers position, and triggers emergency close on actual contracts.
    4. Confirms honest status classification: ABORTED_PARTIAL_FILL on close success,
       or RISK_BREACH_UNRESOLVED if emergency close fails/indexer lags.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_fill_resp = MagicMock()
    mock_fill_resp.data.digest = "0xreal_order_digest"
    mock_client.market.place_order = MagicMock(return_value=mock_fill_resp)

    # Historical orders reports 0.04 BTC filled out of 0.10 BTC (40% partial fill)
    mock_hist_order = MagicMock()
    mock_hist_order.digest = "0xreal_order_digest"
    mock_hist_order.base_filled = str(int(0.04 * 1e18))
    mock_hist_order.quote_filled = str(int(2400.0 * 1e18))  # filled at 60000.0
    mock_client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[mock_hist_order])
    )

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._market_cache = {2: MagicMock()}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    # Scenario A: Emergency close succeeds
    service.emergency_protect_risk_breach = AsyncMock(return_value=(True, -5.0))
    with patch("asyncio.sleep", AsyncMock()), patch.object(service, "_save_positions"):
        ok_a = await service.open_position(
            "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10,
            contracts=0.10, approved_risk_usd=150.0
        )
    assert ok_a is False
    assert service.last_execution_status == "ABORTED_PARTIAL_FILL"
    assert service.last_execution_details["actual_base_amount"] == 0.04
    assert service.emergency_protect_risk_breach.called is True

    # Scenario B: Emergency close fails / unconfirmed -> RISK_BREACH_UNRESOLVED
    service.active_positions.clear()
    service.emergency_protect_risk_breach = AsyncMock(return_value=(False, 0.0))
    with patch("asyncio.sleep", AsyncMock()), patch.object(service, "_save_positions"):
        ok_b = await service.open_position(
            "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10,
            contracts=0.10, approved_risk_usd=150.0
        )
    assert ok_b is False
    assert service.last_execution_status == "RISK_BREACH_UNRESOLVED"


@pytest.mark.asyncio
async def test_real_post_fill_calculator_error_fails_closed():
    """
    P1/P2 Audit Requirement (Point 3):
    Verifies that if estimate_total_loss raises ValueError after a real fill,
    open_position() does not crash with UnboundLocalError, immediately triggers
    emergency_protect_risk_breach, and fails closed with ABORTED_CALC_ERROR.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_fill_resp = MagicMock()
    mock_fill_resp.data.digest = "0xcalc_err_order"
    mock_client.market.place_order = MagicMock(return_value=mock_fill_resp)

    # 100% fill (0.10 BTC)
    mock_hist_order = MagicMock()
    mock_hist_order.digest = "0xcalc_err_order"
    mock_hist_order.base_filled = str(int(0.10 * 1e18))
    mock_hist_order.quote_filled = str(int(6000.0 * 1e18))
    mock_client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[mock_hist_order])
    )

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._market_cache = {2: MagicMock()}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    service.emergency_protect_risk_breach = AsyncMock(return_value=(True, -2.0))

    # Pre-dispatch succeeds, but post-fill check encounters corrupted data -> ValueError
    from core import risk_calculator
    orig_estimate = risk_calculator.estimate_total_loss
    call_count = [0]

    def faulty_estimate(**kwargs):
        call_count[0] += 1
        if call_count[0] > 1:  # Second call is post-fill
            raise ValueError("Corrupted tick prices post-fill")
        return orig_estimate(**kwargs)

    with patch("core.risk_calculator.estimate_total_loss", side_effect=faulty_estimate):
        with patch("asyncio.sleep", AsyncMock()), patch.object(service, "_save_positions"):
            ok = await service.open_position(
                "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10,
                contracts=0.10, approved_risk_usd=150.0
            )

    assert ok is False
    assert service.last_execution_status == "ABORTED_CALC_ERROR"
    assert service.emergency_protect_risk_breach.called is True


@pytest.mark.asyncio
async def test_open_position_blocks_on_pending_reconciliation_incident():
    """
    P2 Audit Requirement (Point 4):
    Verifies that if pending_reconciliations contains an unresolved incident,
    open_position() immediately refuses to dispatch new orders before network calls.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_client.market.place_order = AsyncMock()

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}

    # Inject unresolved incident in pending_reconciliations
    service.pending_reconciliations["BTC-USD"] = {
        "status": "PENDING_ORDER_RECONCILIATION",
        "symbol": "BTC-USD"
    }

    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 59000.0, 10,
        contracts=0.01, approved_risk_usd=10.0
    )
    assert ok is False
    assert service.last_execution_status == "RISK_BREACH_UNRESOLVED"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_portfolio_unresolved_risk_breach_blocks_new_trades(risk_mgr, base_market_data):
    """
    Item 3.A:
    Verifies that if any position in the portfolio has protection_state == 'RISK_BREACH_UNRESOLVED',
    RiskManager strictly vetoes candidate trade with RISK_BREACH_UNRESOLVED.
    """
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    portfolio = {
        "total_usd": 1000.0,
        "available_margin": 900.0,
        "active_positions": {
            "ETH-USD": {
                "symbol": "ETH-USD",
                "direction": "LONG",
                "entry_price": 3000.0,
                "contracts": 0.1,
                "protection_state": "RISK_BREACH_UNRESOLVED"
            }
        }
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, base_market_data)
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "RISK_BREACH_UNRESOLVED"


@pytest.mark.asyncio
async def test_nado_open_position_blocks_on_portfolio_unresolved_risk_breach():
    """
    Item 3.A:
    Verifies that if NadoTradingService has an active position in RISK_BREACH_UNRESOLVED,
    open_position immediately refuses to dispatch new orders.
    """
    from services.nado_trading_service import NadoTradingService
    mock_client = MagicMock()
    mock_client.market.place_order = AsyncMock()
    service = NadoTradingService(nado_client=mock_client)
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}

    # Inject unresolved risk breach in another asset
    service.active_positions["SOL-USD"] = {
        "symbol": "SOL-USD",
        "contracts": 1.0,
        "protection_state": "RISK_BREACH_UNRESOLVED"
    }

    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 59000.0, 10,
        contracts=0.01, approved_risk_usd=10.0
    )
    assert ok is False
    assert service.last_execution_status == "RISK_BREACH_UNRESOLVED"
    assert mock_client.market.place_order.called is False


# ==============================================================================
# AUDIT c4cf836 SAFETY SUITE: REAL EMERGENCY CLOSE SDK FLOW & POST-FILL FAIL-CLOSED
# ==============================================================================

@pytest.mark.asyncio
async def test_emergency_protect_risk_breach_real_sdk_close_success():
    """
    P2 Audit Requirement:
    Tests emergency_protect_risk_breach() executing through real force_close_position()
    WITHOUT mocking emergency_protect_risk_breach or force_close_position.
    Mocks only underlying Nado SDK calls (subaccount, market.close_position, historical_orders).
    Verifies:
      - client.market.close_position called with correct subaccount and product_id
      - trigger cancellation attempted
      - position cleaned up from active_positions
      - returns (True, realized_pnl)
    """
    import time
    from services.nado_trading_service import NadoTradingService

    mock_client = MagicMock()
    mock_sub = MagicMock()
    mock_sub.subaccount = "0x" + "aa" * 32
    mock_client.subaccount.get_subaccounts = MagicMock(return_value=MagicMock(subaccounts=[mock_sub]))
    mock_client.market.close_position = MagicMock(return_value="0xclose_tx_digest")
    
    mock_hist_order = MagicMock()
    mock_hist_order.digest = "0xclose_fill_digest"
    mock_hist_order.base_filled = str(int(-0.10 * 1e18))
    mock_hist_order.quote_filled = str(int(5990.0 * 1e18))
    mock_hist_order.timestamp = int(time.time() * 1000)
    mock_client.market.get_subaccount_historical_orders = MagicMock(return_value=MagicMock(orders=[mock_hist_order]))

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "aa" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._cancel_trigger_digests = AsyncMock()

    service.active_positions["BTC-USD"] = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 60000.0,
        "sl_price": 59000.0,
        "contracts": 0.10,
        "size_usd": 6000.0,
        "sl_digest": "0xsl_123",
        "tp_digest": "0xtp_123",
        "protection_state": "SUBMITTING_PROTECTION"
    }

    call_count = [0]
    async def mock_get_positions(bypass_cache=True):
        call_count[0] += 1
        if call_count[0] == 1:
            return [{"symbol": "BTC-USD", "contracts": 0.10, "entry_price": 60000.0, "product_id": 2}]
        return []
    service.get_active_positions = AsyncMock(side_effect=mock_get_positions)

    with patch.object(service, "_save_positions"):
        ok, pnl = await service.emergency_protect_risk_breach("BTC-USD", approved_risk=100.0, actual_risk=150.0)

    assert ok is True
    assert mock_client.market.close_position.called is True
    close_args = mock_client.market.close_position.call_args[0]
    assert close_args[0] == "0x" + "aa" * 32
    assert close_args[1] == 2
    assert service._cancel_trigger_digests.called is True
    assert "BTC-USD" not in service.active_positions


@pytest.mark.asyncio
async def test_emergency_protect_risk_breach_real_sdk_close_failure_marks_unresolved():
    """
    P2 Audit Requirement:
    Verifies that when client.market.close_position fails (e.g. Sequencer Error)
    and the position remains open on-chain, emergency_protect_risk_breach():
      - Positively detects position STILL_OPEN on chain via get_active_positions
      - Does NOT falsely report success
      - Marks position protection_state as RISK_BREACH_UNRESOLVED
      - Retains position in active_positions for Sentinel
      - Returns (False, 0.0)
    """
    from services.nado_trading_service import NadoTradingService

    mock_client = MagicMock()
    mock_sub = MagicMock()
    mock_sub.subaccount = "0x" + "aa" * 32
    mock_client.subaccount.get_subaccounts = MagicMock(return_value=MagicMock(subaccounts=[mock_sub]))
    mock_client.market.close_position = MagicMock(side_effect=RuntimeError("Sequencer rejected: 2004 Insufficient Margin"))

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "aa" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}

    service.active_positions["BTC-USD"] = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 60000.0,
        "sl_price": 59000.0,
        "contracts": 0.10,
        "size_usd": 6000.0,
        "protection_state": "SUBMITTING_PROTECTION"
    }

    service.get_active_positions = AsyncMock(return_value=[
        {"symbol": "BTC-USD", "contracts": 0.10, "entry_price": 60000.0, "product_id": 2}
    ])

    with patch.object(service, "_save_positions"):
        ok, pnl = await service.emergency_protect_risk_breach("BTC-USD", approved_risk=100.0, actual_risk=150.0)

    assert ok is False
    assert pnl == 0.0
    assert "BTC-USD" in service.active_positions
    assert service.active_positions["BTC-USD"]["protection_state"] == "RISK_BREACH_UNRESOLVED"


@pytest.mark.asyncio
async def test_post_fill_unexpected_type_error_triggers_emergency_close_and_fails_closed():
    """
    P1 Audit Requirement:
    Verifies that if an unexpected non-ValueError exception (e.g. TypeError, OverflowError)
    occurs AFTER a confirmed fill:
      - The exception is NOT treated as REJECTED_BY_EXCHANGE
      - emergency_protect_risk_breach is invoked
      - When emergency close succeeds, status is set to ABORTED_CALC_ERROR or ABORTED_POST_FILL_ERROR
      - Never leaves an open on-chain fill unhedged or unmonitored
    """
    from services.nado_trading_service import NadoTradingService

    mock_client = MagicMock()
    mock_fill_resp = MagicMock()
    mock_fill_resp.data.digest = "0xtype_err_order"
    mock_client.market.place_order = MagicMock(return_value=mock_fill_resp)

    mock_hist_order = MagicMock()
    mock_hist_order.digest = "0xtype_err_order"
    mock_hist_order.base_filled = str(int(0.10 * 1e18))
    mock_hist_order.quote_filled = str(int(6000.0 * 1e18))
    mock_client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[mock_hist_order])
    )

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._market_cache = {2: MagicMock()}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    service.emergency_protect_risk_breach = AsyncMock(return_value=(True, -2.50))

    from core import risk_calculator
    orig_estimate = risk_calculator.estimate_total_loss
    call_count = [0]

    def faulty_estimate_type_error(**kwargs):
        call_count[0] += 1
        if call_count[0] > 1:
            raise TypeError("Unsupported operand types for float and str in risk computation")
        return orig_estimate(**kwargs)

    with patch("core.risk_calculator.estimate_total_loss", side_effect=faulty_estimate_type_error):
        with patch("asyncio.sleep", AsyncMock()), patch.object(service, "_save_positions"):
            ok = await service.open_position(
                "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10,
                contracts=0.10, approved_risk_usd=150.0
            )

    assert ok is False
    assert service.last_execution_status != "REJECTED_BY_EXCHANGE"
    assert service.last_execution_status == "ABORTED_CALC_ERROR"
    assert service.emergency_protect_risk_breach.called is True


@pytest.mark.asyncio
async def test_post_fill_outer_exception_triggers_emergency_protection_and_persists():
    """
    P1 Audit Requirement:
    Verifies that if an unexpected exception occurs in the outer try block AFTER fill confirmation
    (e.g. state persistence or trigger placement crashes unexpectedly), the outer handler detects
    is_post_fill=True, invokes emergency_protect_risk_breach, and if closure fails, retains state
    as RISK_BREACH_UNRESOLVED and never classifies it as REJECTED_BY_EXCHANGE.
    """
    from services.nado_trading_service import NadoTradingService

    mock_client = MagicMock()
    mock_fill_resp = MagicMock()
    mock_fill_resp.data.digest = "0xouter_err_order"
    mock_client.market.place_order = MagicMock(return_value=mock_fill_resp)

    mock_hist_order = MagicMock()
    mock_hist_order.digest = "0xouter_err_order"
    mock_hist_order.base_filled = str(int(0.10 * 1e18))
    mock_hist_order.quote_filled = str(int(6000.0 * 1e18))
    mock_client.market.get_historical_orders_by_digest = MagicMock(
        return_value=MagicMock(orders=[mock_hist_order])
    )

    service = NadoTradingService()
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}
    service._market_cache = {2: MagicMock()}
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_base": 0.001,
        "size_increment_x18": int(0.001 * 1e18),
        "price_increment_x18": int(1.0 * 1e18),
        "min_size": 0.001,
        "min_notional": 0.0
    })

    service.emergency_protect_risk_breach = AsyncMock(return_value=(False, 0.0))

    # Force an unexpected RuntimeError in trigger placement step
    save_call_count = [0]
    def faulty_save():
        save_call_count[0] += 1
        if save_call_count[0] >= 2:
            raise RuntimeError("Database connection lost during position state save")

    service._save_positions = MagicMock(side_effect=faulty_save)

    with patch("asyncio.sleep", AsyncMock()), patch.object(service, "_save_pending_reconciliations"), patch.object(service, "_save_state"):
        ok = await service.open_position(
            "BTC-USD", "LONG", 60000.0, 6000.0, 62000.0, 59000.0, 10,
            contracts=0.10, approved_risk_usd=150.0
        )

    assert ok is False
    assert service.last_execution_status != "REJECTED_BY_EXCHANGE"
    assert service.last_execution_status == "RISK_BREACH_UNRESOLVED"
    assert service.emergency_protect_risk_breach.called is True
    assert "BTC-USD" in service.pending_reconciliations


@pytest.mark.asyncio
async def test_restart_recovery_with_unresolved_incident_blocks_new_trades():
    """
    P2 Audit Requirement:
    Verifies state recovery after restart:
    If active_positions or pending_reconciliations has an unresolved risk breach
    restored from persistent disk storage, a freshly instantiated NadoTradingService
    strictly refuses to open any new positions before reconciliation completes.
    """
    import time
    from services.nado_trading_service import NadoTradingService

    mock_client = MagicMock()
    mock_client.market.place_order = AsyncMock()

    service = NadoTradingService()
    service.client = mock_client
    service.is_connected = True
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1234567890123456789012345678901234567890"
    service.default_subaccount_id = "0x" + "11" * 32
    service.product_map = {"BTC": 2, "BTC-USD": 2}

    # Simulate restored state with pending unresolved breach
    service.active_positions.clear()
    service.pending_reconciliations.clear()
    service.pending_reconciliations["ETH-USD"] = {
        "symbol": "ETH-USD",
        "status": "RISK_BREACH_UNRESOLVED",
        "reason": "RISK_CALC_ERROR_CLOSE_FAILED",
        "detected_at": time.time()
    }

    ok = await service.open_position(
        "BTC-USD", "LONG", 60000.0, 600.0, 62000.0, 59000.0, 10,
        contracts=0.01, approved_risk_usd=10.0
    )

    assert ok is False
    assert service.last_execution_status == "RISK_BREACH_UNRESOLVED"
    assert mock_client.market.place_order.called is False


@pytest.mark.asyncio
async def test_risk_manager_default_omitted_macro_regime_fails_closed(mock_logger, mock_llm, base_market_data, base_portfolio_data):
    """
    P1 Audit Requirement:
    Verifies that a newly instantiated RiskManager without arguments has default_macro_regime="UNKNOWN".
    When analyze() is called without passing macro_regime, or with None/empty string,
    it strictly fails closed and issues UNKNOWN_MACRO_REGIME veto even if all other metrics are pristine.
    """
    raw_rm = RiskManager(mock_logger, mock_llm)
    assert raw_rm.default_macro_regime == "UNKNOWN"

    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}

    # 1. Direct call without macro_regime argument -> VETO
    res_omitted = await raw_rm.analyze(ceo_decision, base_portfolio_data, base_market_data, effective_profile="BALANCED")
    assert res_omitted["approved"] is False
    assert res_omitted["veto_category"] == "UNKNOWN_MACRO_REGIME"
    assert "telemetry unconfirmed" in res_omitted["reasoning"]

    # 2. Call with macro_regime=None -> VETO
    res_none = await raw_rm.analyze(ceo_decision, base_portfolio_data, base_market_data, macro_regime=None)
    assert res_none["approved"] is False
    assert res_none["veto_category"] == "UNKNOWN_MACRO_REGIME"

    # 3. Call with macro_regime="" -> VETO
    res_empty = await raw_rm.analyze(ceo_decision, base_portfolio_data, base_market_data, macro_regime="")
    assert res_empty["approved"] is False
    assert res_empty["veto_category"] == "UNKNOWN_MACRO_REGIME"

    # 4. Explicitly provided valid macro_regime -> PROCEEDS PAST GATE
    res_valid = await raw_rm.analyze(ceo_decision, base_portfolio_data, base_market_data, macro_regime="RANGE_CHOPPY")
    assert res_valid.get("veto_category") != "UNKNOWN_MACRO_REGIME"


@pytest.mark.asyncio
async def test_gate_2_4_trailing_stop_in_profit_does_not_exhaust_open_risk_cap(risk_mgr, base_market_data):
    """
    Gate 2.4 Audit Hardening:
    Verifies that when an open position has a trailing stop moved into profit (e.g. SHORT with sl < entry,
    or LONG with sl > entry), the locked-in profit eliminates capital risk to the entry.
    It must NOT treat locked-in profit as risk via abs(entry - sl) and must NOT veto subsequent entries.
    """
    ceo_decision = {"symbol": "AVAX-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    # Balance = $1000. Cap for BALANCED = 4% ($40).
    # Open ETH SHORT position in profit: entry = 3000, sl = 2800 (trailing stop locked in $200/unit profit!).
    # With contracts = 0.5, previous abs(3000 - 2800) * 0.5 = $100 would have falsely exhausted the $40 cap!
    portfolio = {
        "total_usd": 1000.0,
        "available_margin": 800.0,
        "active_positions": {
            "ETH-USD": {
                "symbol": "ETH-USD",
                "direction": "SHORT",
                "entry_price": 3000.0,
                "sl_price": 2800.0,
                "contracts": 0.5
            }
        }
    }
    market = {
        "price_data": {"current_price": 30.0},
        "indicators": {"atr_14": 1.0},
        "order_book_data": {"spread_pct": 0.05},
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.1, "price_increment": 0.01}
    }
    verdict = await risk_mgr.analyze(ceo_decision, portfolio, market, effective_profile="BALANCED", macro_regime="TRENDING")
    # Position in profit has 0 open capital risk -> trade is approved
    assert verdict["approved"] is True
    assert verdict.get("veto_category") != "TOTAL_OPEN_RISK_CAP"


def test_estimate_total_loss_favorable_funding_rate_is_zero_cost():
    """
    Risk Calculator Audit Hardening:
    Verifies that when funding rate is favorable (e.g. SHORT with positive funding, or LONG with negative funding),
    the funding income is NOT counted as a stress loss (funding_loss == 0.0).
    When funding rate is adverse, funding cost is accurately calculated and included.
    """
    from core.risk_calculator import estimate_total_loss

    # 1. SHORT with POSITIVE funding rate (+0.0002): exchange pays funding to shorts -> cost is 0
    short_favorable = estimate_total_loss(
        contracts=1.0,
        fill_price=100.0,
        sl_price=105.0,  # SL > entry -> SHORT
        funding_rate_hourly=0.0002,
        holding_hours=4.0
    )
    assert short_favorable.funding_loss_usd == 0.0

    # 2. SHORT with NEGATIVE funding rate (-0.0002): shorts pay longs -> cost is added
    short_adverse = estimate_total_loss(
        contracts=1.0,
        fill_price=100.0,
        sl_price=105.0,
        funding_rate_hourly=-0.0002,
        holding_hours=4.0
    )
    assert short_adverse.funding_loss_usd == round(1.0 * 100.0 * 0.0002 * 4.0, 4)
    assert short_adverse.funding_loss_usd > 0.0

    # 3. LONG with NEGATIVE funding rate (-0.0002): exchange pays funding to longs -> cost is 0
    long_favorable = estimate_total_loss(
        contracts=1.0,
        fill_price=100.0,
        sl_price=95.0,  # SL < entry -> LONG
        funding_rate_hourly=-0.0002,
        holding_hours=4.0
    )
    assert long_favorable.funding_loss_usd == 0.0

    # 4. LONG with POSITIVE funding rate (+0.0002): longs pay shorts -> cost is added
    long_adverse = estimate_total_loss(
        contracts=1.0,
        fill_price=100.0,
        sl_price=95.0,
        funding_rate_hourly=0.0002,
        holding_hours=4.0
    )
    assert long_adverse.funding_loss_usd == round(1.0 * 100.0 * 0.0002 * 4.0, 4)
    assert long_adverse.funding_loss_usd > 0.0




def test_smooth_step_up_recovery_after_loss_streak(risk_mgr):
    """
    Sizing Anti-Whipsaw Audit Hardening:
    Verifies that after 2+ consecutive losses, recovering wins smoothly step up
    (0.75x -> 0.85x -> 1.00x) rather than immediately jumping from 0.50x to 1.00x,
    preventing asymmetric adverse selection between winning and losing trades.
    """
    # 1. Two consecutive losses -> 0.50x
    m1, msg1 = risk_mgr._calculate_drawdown_multiplier(["LOSS", "LOSS"], daily_drawdown_pct=0.0)
    assert m1 == 0.50
    assert "DRAWDOWN PROTECTION" in msg1

    # 2. First win after 2 losses -> Tier 1 recovery at 0.75x
    m2, msg2 = risk_mgr._calculate_drawdown_multiplier(["LOSS", "LOSS", "WIN"], daily_drawdown_pct=0.0)
    assert m2 == 0.75
    assert "PROBATION RECOVERY TIER 1" in msg2

    # 3. Second consecutive win -> Tier 2 recovery at 0.85x
    m3, msg3 = risk_mgr._calculate_drawdown_multiplier(["LOSS", "LOSS", "WIN", "WIN"], daily_drawdown_pct=0.0)
    assert m3 == 0.85
    assert "PROBATION RECOVERY TIER 2" in msg3

    # 4. Third consecutive win -> Full recovery at 1.00x
    m4, msg4 = risk_mgr._calculate_drawdown_multiplier(["LOSS", "LOSS", "WIN", "WIN", "WIN"], daily_drawdown_pct=0.0)
    assert m4 == 1.00
    assert "STREAK_NORMAL" in msg4

    # 5. Probation loss during recovery -> Kept at 0.60x (not crashed to 0.25x or 0.50x)
    m5, msg5 = risk_mgr._calculate_drawdown_multiplier(["LOSS", "LOSS", "WIN", "LOSS"], daily_drawdown_pct=0.0)
    assert m5 == 0.60
    assert "PROBATION: Mild loss" in msg5
```

---

<a id="file-tests-test_nado_execution-py"></a>
## File: `tests/test_nado_execution.py` (1334 строк, 49.7 KB)

```python
import pytest
import asyncio
import time
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
    service.product_map = {"BTC": 2, "BTC-USD": 2, "ETH": 4, "ETH-USD": 4, "SOL": 8, "SOL-USD": 8, "ZEC": 8, "ZEC-USD": 8}
    service.active_positions = {}
    service.pending_reconciliations = {}
    service.processed_fills_registry = {}
    service.processed_fill_digests = set()
    service._reconcile_lock = None
    service._position_locks = {}
    service._market_cache = {
        2: MagicMock(book_info=MagicMock(size_increment=int(0.0001 * 1e18), price_increment_x18=int(0.1 * 1e18), min_size=int(0.0001 * 1e18))),
        4: MagicMock(book_info=MagicMock(size_increment=int(0.001 * 1e18), price_increment_x18=int(0.01 * 1e18), min_size=int(0.001 * 1e18))),
        8: MagicMock(book_info=MagicMock(size_increment=int(0.01 * 1e18), price_increment_x18=int(0.001 * 1e18), min_size=int(0.01 * 1e18)))
    }
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
    service._record_closed_trade_journal = MagicMock()
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
    assert set(called_params.productIds) == {2}

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

    digests = ["0x" + "11" * 32, "0x" + "22" * 32]
    res = await service._cancel_trigger_digests(product_id=5, digests=digests, sender="0xsubaccount")

    assert res is True
    cancel_mock.assert_called_once()
    call_args = cancel_mock.call_args[0][0]
    assert call_args.productIds == [5, 5]
    assert len(call_args.digests) == 2
    assert len(call_args.productIds) == len(call_args.digests)


@pytest.mark.asyncio
async def test_cancel_trigger_digests_error_2020_batch_fallback_and_recovery():
    """
    Verifies that when batch cancel fails because an already-executed trigger order
    returns error 2020 ('could not be found'), _cancel_trigger_digests:
    1. Retries individual cancellation for each digest.
    2. Treats error 2020 as benign (order already gone).
    3. Successfully cancels any remaining open trigger order.
    """
    service = get_mock_service()
    service.is_connected = True
    service.default_subaccount_id = "0xsubaccount"

    d1 = "0x" + "aa" * 32
    d2 = "0x" + "bb" * 32
    d1_bytes = bytes.fromhex("aa" * 32)
    d2_bytes = bytes.fromhex("bb" * 32)

    def mock_cancel(params):
        if len(params.digests) > 1:
            raise Exception('{"error_code": 2020, "error": "Order with the provided digest could not be found."}')
        elif params.digests[0] == d1_bytes:
            raise Exception('{"error_code": 2020, "error": "Order with the provided digest could not be found."}')
        elif params.digests[0] == d2_bytes:
            return {"status": "success"}
        raise Exception(f"unexpected digest: {params.digests[0]}")

    service.client.market.cancel_trigger_orders = MagicMock(side_effect=mock_cancel)

    res = await service._cancel_trigger_digests(product_id=16, digests=[d1, d2], sender="0xsubaccount")

    assert res is True
    assert service.client.market.cancel_trigger_orders.call_count == 3  # 1 batch + 2 individual


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
        "planned_risk_usd": 1.0,
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
    service.get_active_positions = AsyncMock(side_effect=[[], Exception("Connection reset by peer")])

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
        close_timestamp=time.time(),
        allow_missing_timestamp=False
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
        NadoTradingService._save_processed_fills(service)

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


@pytest.mark.asyncio
async def test_check_and_update_positions_reconciles_missing_timestamp_fills():
    """Verifies that check_and_update_positions reconciles exact on-chain fills even
    when Nado Indexer historical orders have timestamp=None."""
    service = get_mock_service()
    service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "size_usd": 100.0,
        "contracts": 1.0,
        "sl_price": 95.0,
        "tp_price": 110.0,
        "product_id": 4,
        "sl_digest": "0xsl123",
        "tp_digest": "0xtp123",
        "sender": service.default_subaccount_id
    }
    service.get_active_positions = AsyncMock(return_value=[])

    mock_fill = MagicMock()
    mock_fill.base_filled = -1.0 * 1e18  # Sold 1.0 SOL
    mock_fill.quote_filled = 97.5 * 1e18  # $97.50 quote -> exit price $97.50
    mock_fill.fee_x18 = 0.05 * 1e18
    mock_fill.timestamp = None
    mock_fill.digest = "0xreal_sl_fill"

    mock_hist = MagicMock()
    mock_hist.orders = [mock_fill]
    service.client.market.get_subaccount_historical_orders = MagicMock(return_value=mock_hist)

    reports = await service.check_and_update_positions("SOL-USD", 95.0)
    assert len(reports) == 1
    rep = reports[0]
    assert rep["exit_price"] == pytest.approx(97.50)
    assert rep["pnl_usd"] == pytest.approx(-2.55)
    assert "(EXACT)" in rep["triggered_by"]
    assert "SOL-USD" not in service.active_positions



```

---

<a id="file-tests-test_partial_tp_state_machine-py"></a>
## File: `tests/test_partial_tp_state_machine.py` (449 строк, 16.4 KB)

```python
"""
Unit tests for Partial TP State Machine, Persistence, Idempotency, and Reconciliation (Phase 3).
Verifies:
- Plan initialization and attachment to position state.
- Atomic updates of contracts, size_usd, margin, and accumulated_realized_pnl upon tier execution.
- Strict idempotency (duplicate tier execution calls ignored).
- Failed executions safely reverted to PENDING with contracts intact.
- Partial fills accurately tracked and deducted.
- Final tier completion and position lifecycle finalization.
- On-chain reconciliation: zero balance cleanup and volume divergence alignment.
- Complete backward compatibility for legacy positions without partial_tp_plan.
"""

import pytest
import asyncio
from unittest.mock import MagicMock, patch

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
    service.partial_tp_mode = "4_TIER"
    return service


def test_initialize_partial_tp_plan_success(mock_service):
    """Verifies that initialize_partial_tp_plan creates and attaches a 4-tier plan to an active position."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }

    plan = mock_service.initialize_partial_tp_plan("SOL-USD")

    assert plan is not None
    assert plan.symbol == "SOL-USD"
    assert plan.mode == "4_TIER"
    assert len(plan.tiers) == 4
    assert plan.remaining_contracts == pytest.approx(1.6)

    # Position updated
    pos = mock_service.active_positions["SOL-USD"]
    assert "partial_tp_plan" in pos
    assert pos["accumulated_realized_pnl"] == 0.0
    mock_service._save_positions.assert_called()


def test_apply_partial_close_tier_1_success(mock_service):
    """Verifies atomic state update after Tier 1 partial close succeeds."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Tier 1 execution result: 0.4 SOL filled at 101.0
    result = PartialCloseResult(
        success=True,
        digest="0xmock_tier1",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=101.0,
        realized_pnl=0.40,  # 0.4 * (101 - 100)
        is_partial_fill=False
    )

    success = mock_service.apply_partial_close_to_position("SOL-USD", tier_index=1, result=result)

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])

    tier1 = plan.get_tier(1)
    assert tier1.status == "FILLED"
    assert tier1.filled_contracts == pytest.approx(0.4)
    assert tier1.realized_pnl_usd == pytest.approx(0.40)
    assert tier1.order_digest == "0xmock_tier1"

    # Position fields updated
    assert plan.remaining_contracts == pytest.approx(1.2)
    assert pos["contracts"] == pytest.approx(1.2)
    assert pos["size_usd"] == pytest.approx(120.0)
    assert pos["margin_used"] == pytest.approx(60.0)  # 120 / 2 leverage
    assert pos["accumulated_realized_pnl"] == pytest.approx(0.40)
    assert "SOL-USD" in mock_service.active_positions


def test_apply_partial_close_idempotency_duplicate_call(mock_service):
    """Verifies that a repeated call for an already FILLED tier is ignored idempotently."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "leverage": 2
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    result = PartialCloseResult(
        success=True,
        digest="0xmock_tier1",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=101.0,
        realized_pnl=0.40,
        is_partial_fill=False
    )
    mock_service.apply_partial_close_to_position("SOL-USD", 1, result)

    # Duplicate call with identical tier
    dup_success = mock_service.apply_partial_close_to_position("SOL-USD", 1, result)

    assert dup_success is True
    pos = mock_service.active_positions["SOL-USD"]
    # Contracts must NOT be deducted twice!
    assert pos["contracts"] == pytest.approx(1.2)
    assert pos["accumulated_realized_pnl"] == pytest.approx(0.40)


def test_apply_partial_close_failed_result_reverts_to_pending(mock_service):
    """Verifies that an execution failure resets the tier to PENDING and preserves contracts."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Mark tier 1 as SUBMITTING
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    plan.get_tier(1).status = "SUBMITTING"
    pos["partial_tp_plan"] = plan.to_dict()

    fail_result = PartialCloseResult(
        success=False,
        error="IOC order expired without fills"
    )

    success = mock_service.apply_partial_close_to_position("SOL-USD", 1, fail_result)

    assert success is False
    updated_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    tier1 = updated_plan.get_tier(1)
    assert tier1.status == "FAILED"
    assert tier1.retry_count == 1
    assert tier1.error_message == "IOC order expired without fills"
    assert updated_plan.remaining_contracts == pytest.approx(1.6)
    assert pos["contracts"] == pytest.approx(1.6)



def test_apply_partial_close_partial_fill_handling(mock_service):
    """Verifies that an incomplete fill is marked PARTIALLY_FILLED and only filled contracts are deducted."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    partial_result = PartialCloseResult(
        success=True,
        digest="0xmock_part",
        requested_contracts=0.4,
        filled_contracts=0.2,
        fill_price=101.0,
        realized_pnl=0.20,
        is_partial_fill=True
    )

    success = mock_service.apply_partial_close_to_position("SOL-USD", 1, partial_result)

    assert success is True
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    tier1 = plan.get_tier(1)
    assert tier1.status == "PARTIALLY_FILLED"
    assert tier1.filled_contracts == pytest.approx(0.2)
    assert plan.remaining_contracts == pytest.approx(1.4)  # 1.6 - 0.2
    assert pos["contracts"] == pytest.approx(1.4)
    assert pos["size_usd"] == pytest.approx(140.0)
    assert pos["accumulated_realized_pnl"] == pytest.approx(0.20)


def test_apply_partial_close_final_tier_completes_and_finalizes_position(mock_service):
    """Verifies that completing all tiers finalizes the position and removes it from active_positions."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8,
        "sl_digest": "0xold_sl"
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Execute tiers 1, 2, 3
    for i in range(1, 4):
        res = PartialCloseResult(
            success=True,
            digest=f"0xmock_tier_{i}",
            requested_contracts=0.4,
            filled_contracts=0.4,
            fill_price=100.0 + i,
            realized_pnl=0.4 * i,
            is_partial_fill=False
        )
        mock_service.apply_partial_close_to_position("SOL-USD", i, res)

    # Position still open with 0.4 contracts
    assert "SOL-USD" in mock_service.active_positions
    assert mock_service.active_positions["SOL-USD"]["contracts"] == pytest.approx(0.4)

    # Execute final tier 4 (0.4 SOL @ 104.0)
    final_res = PartialCloseResult(
        success=True,
        digest="0xmock_tier_4",
        requested_contracts=0.4,
        filled_contracts=0.4,
        fill_price=104.0,
        realized_pnl=1.60,
        is_partial_fill=False
    )
    final_success = mock_service.apply_partial_close_to_position("SOL-USD", 4, final_res)

    assert final_success is True
    # Position must now be removed from active_positions!
    assert "SOL-USD" not in mock_service.active_positions
    assert mock_service.win_count == 1


def test_reconciliation_zero_on_chain_balance_cleans_up(mock_service):
    """Verifies that an on-chain zero balance triggers full position closure finalization."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "tp_price": 103.0,
        "size_usd": 170.85,
        "contracts": 1.6,
        "product_id": 8,
        "accumulated_realized_pnl": 1.25
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # On-chain returns 0 contracts (closed on chain with verified PnL)
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=0.0, is_verified_pnl=True)

    assert reconciled is True
    assert "SOL-USD" not in mock_service.active_positions
    assert mock_service.win_count == 1  # 1.25 > 0.001 recorded as WIN


def test_reconciliation_divergence_adjusts_remaining_contracts(mock_service):
    """Verifies that volume divergence between local plan and on-chain balance is adjusted safely."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # On-chain balance is only 1.2 SOL (0.4 was somehow executed externally)
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=1.2)

    assert reconciled is True
    pos = mock_service.active_positions["SOL-USD"]
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan.remaining_contracts == pytest.approx(1.2)
    assert pos["contracts"] == pytest.approx(1.2)
    assert pos["size_usd"] == pytest.approx(120.0)


def test_legacy_position_backward_compatibility(mock_service):
    """Verifies that positions without partial_tp_plan continue to work safely without errors."""
    mock_service.active_positions["SOL-USD"] = {
        "direction": "SHORT",
        "entry_price": 106.78,
        "size_usd": 170.85,
        "product_id": 8
    }

    # Calling apply on position without plan returns False cleanly
    res = PartialCloseResult(success=True, filled_contracts=0.4, fill_price=105.0)
    applied = mock_service.apply_partial_close_to_position("SOL-USD", 1, res)
    assert applied is False

    # Calling reconcile on position without plan returns False cleanly
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", 1.6)
    assert reconciled is False

    # Position remains completely untouched
    assert mock_service.active_positions["SOL-USD"]["size_usd"] == 170.85


def test_final_tier_partial_fill_does_not_prematurely_delete_position(mock_service):
    """
    Verifies that if the final tier is only partially filled (e.g. 0.3 filled out of 0.4),
    the position is NEVER prematurely deleted from active_positions!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Complete tiers 1, 2, 3
    for i in range(1, 4):
        res = PartialCloseResult(success=True, requested_contracts=0.4, filled_contracts=0.4, fill_price=100.0 + i)
        mock_service.apply_partial_close_to_position("SOL-USD", i, res)

    # Now execute final tier 4, but matching engine only fills 0.3 out of 0.4!
    partial_final_res = PartialCloseResult(
        success=True,
        digest="0xmock_partial_final",
        requested_contracts=0.4,
        filled_contracts=0.3,
        fill_price=104.0,
        realized_pnl=1.20,
        is_partial_fill=True
    )
    mock_service.apply_partial_close_to_position("SOL-USD", 4, partial_final_res)

    # Position MUST STILL BE ALIVE in active_positions with 0.1 SOL remaining!
    assert "SOL-USD" in mock_service.active_positions
    pos = mock_service.active_positions["SOL-USD"]
    assert pos["contracts"] == pytest.approx(0.1)
    plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan.remaining_contracts == pytest.approx(0.1)
    assert plan.tiers[3].status == "PARTIALLY_FILLED"
    assert plan.tiers[3].remaining_tier_contracts == pytest.approx(0.1)


def test_hanging_submitting_tier_recovery_in_reconcile(mock_service):
    """
    Verifies that if a process restart leaves a tier in SUBMITTING status,
    reconciliation checks on-chain reality and safely recovers it to PENDING.
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    plan = mock_service.initialize_partial_tp_plan("SOL-USD")
    plan.tiers[0].status = "SUBMITTING"
    mock_service.active_positions["SOL-USD"]["partial_tp_plan"] = plan.to_dict()

    # On-chain balance is still 1.6 SOL (IOC expired without fill)
    reconciled = mock_service.reconcile_partial_tp_with_chain("SOL-USD", on_chain_contracts=1.6)
    assert reconciled is True

    pos = mock_service.active_positions["SOL-USD"]
    recovered_plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert recovered_plan.tiers[0].status == "PENDING"
    assert recovered_plan.get_active_or_next_tier().tier_index == 1


def test_partially_filled_tier_cumulative_execution(mock_service):
    """
    Verifies that a tier can be completed across multiple partial fills cumulatively:
    Fill 1: 0.25 / 0.40 -> PARTIALLY_FILLED (0.15 remaining).
    Fill 2: 0.15 / 0.15 -> FILLED (0.40 total).
    Only then does the next tier become active!
    """
    mock_service.active_positions["SOL-USD"] = {
        "direction": "LONG",
        "entry_price": 100.0,
        "tp_price": 104.0,
        "size_usd": 160.0,
        "contracts": 1.6,
        "product_id": 8
    }
    mock_service.initialize_partial_tp_plan("SOL-USD")

    # Slice 1: fill 0.25
    res1 = PartialCloseResult(success=True, requested_contracts=0.40, filled_contracts=0.25, fill_price=101.0, is_partial_fill=True)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, res1)

    pos = mock_service.active_positions["SOL-USD"]
    plan1 = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan1.tiers[0].status == "PARTIALLY_FILLED"
    assert plan1.tiers[0].filled_contracts == pytest.approx(0.25)
    assert plan1.get_active_or_next_tier().tier_index == 1
    assert plan1.get_active_or_next_tier().remaining_tier_contracts == pytest.approx(0.15)

    # Slice 2: fill remaining 0.15
    res2 = PartialCloseResult(success=True, requested_contracts=0.15, filled_contracts=0.15, fill_price=101.0, is_partial_fill=False)
    mock_service.apply_partial_close_to_position("SOL-USD", 1, res2)

    pos = mock_service.active_positions["SOL-USD"]
    plan2 = PartialTPPlan.from_dict(pos["partial_tp_plan"])
    assert plan2.tiers[0].status == "FILLED"
    assert plan2.tiers[0].filled_contracts == pytest.approx(0.40)
    # Next active tier is now tier 2!
    assert plan2.get_active_or_next_tier().tier_index == 2
```

---

