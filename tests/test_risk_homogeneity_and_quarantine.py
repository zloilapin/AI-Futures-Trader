import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch
from core.directional_quarantine import DirectionalQuarantine
from agents.risk_manager import RiskManager, solve_optimal_pullback_entry

# ═════════════════════════════════════════════════════════════════════════════
# INVARIANT 1: Homogeneous 1R Dollar Cap & Mandatory P* Routing for Wide Stops
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_invariant1_wide_stop_market_entry_vetoed_and_routed_to_p_star():
    """
    On wide structural stops (e.g. 3.2% on high-beta alts), bumping notional to
    exchange floor ($105.50) creates dollar risk ~$3.48+, exceeding hard_max_risk_usd ($1.89 at $88 equity).
    RiskManager MUST reject market execution with MIN_NOTIONAL_RISK_CAP_VETO and route
    to solve_optimal_pullback_entry (P*) where risk <= hard_max_risk_usd.
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    total_balance = 88.0
    # At $88.0 equity, hard_max_risk_usd = min(0.0215 * 88.0, 2.00) = $1.892
    
    ceo_decision = {
        "action": "ENTER",
        "decision": "SHORT",
        "conviction": 75,
        "entry_price": 844.71,
        "sl_price": 871.74, # 3.2% wide stop
        "tp_price": 790.00,
        "strategy_mode": "TREND_FOLLOWING",
        "timeframe": "15m",
        "symbol": "ZEC-USD"
    }
    
    portfolio_data = {
        "total_balance": total_balance,
        "available_margin": 70.0,
        "active_positions": {},
        "recent_streak": ["WIN", "WIN", "WIN"],
        "daily_drawdown_pct": 0.0,
        "win_rate": 66.7,
        "payoff_ratio": 1.5,
        "total_trades": 10,
    }
    
    market_data = {
        "price_data": {
            "current_price": 844.71,
            "ohlcv_15m": [{"volume": 100} for _ in range(15)]
        },
        "indicators": {
            "atr_14": 15.0
        },
        "derivatives_data": {
            "min_notional": 100.0,
            "taker_fee_pct": 0.0005,
            "step_size": 0.001
        },
        "order_book_data": {
            "spread_pct": 0.0002
        }
    }
    
    result = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING"
    )
    
    # Must be vetoed with MIN_NOTIONAL_RISK_CAP_VETO (or routed to WAIT_FOR_PULLBACK)
    assert result["approved"] is False
    assert result["trade_action"] == "WAIT_FOR_PULLBACK"
    assert "MIN_NOTIONAL_RISK_CAP_VETO" in result["veto_category"] or "MIN_NOTIONAL" in result["veto_category"]
    
    # Target pullback price P* must be set and closer to structural SL
    p_star = result.get("target_pullback_price")
    assert p_star is not None
    assert p_star > 844.71  # For a SHORT, pullback price is higher (closer to SL)
    assert p_star < 871.74
    
    # Verify that at P*, the dollar risk on min notional ($105.50) is strictly <= hard_max_risk_usd ($1.892)
    sl_dist_at_p_star = (871.74 - p_star) / p_star
    dollar_risk_at_p_star = 105.50 * sl_dist_at_p_star
    assert dollar_risk_at_p_star <= 1.892 + 0.10  # within margin of taker fees


@pytest.mark.asyncio
async def test_invariant1_tight_stop_safely_bumps_within_hard_risk_cap():
    """
    On tight stops (e.g. 0.8% on BTC), bumping notional to exchange floor ($108.50)
    produces dollar risk ~$0.87, which is well below hard_max_risk_usd ($1.89).
    RiskManager safely bumps and approves trade.
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    total_balance = 88.0
    
    ceo_decision = {
        "action": "ENTER",
        "decision": "SHORT",
        "conviction": 80,
        "entry_price": 67226.0,
        "sl_price": 67763.0,  # 0.80% tight stop
        "tp_price": 65500.0,
        "strategy_mode": "TREND_FOLLOWING",
        "timeframe": "15m",
        "symbol": "BTC-USD"
    }
    
    portfolio_data = {
        "total_balance": total_balance,
        "available_margin": 70.0,
        "active_positions": {},
        "recent_streak": ["WIN", "WIN"],
        "daily_drawdown_pct": 0.0,
        "win_rate": 66.7,
        "payoff_ratio": 1.5,
        "total_trades": 10,
    }
    
    market_data = {
        "price_data": {
            "current_price": 67226.0,
            "ohlcv_15m": [{"volume": 100} for _ in range(15)]
        },
        "indicators": {
            "atr_14": 400.0
        },
        "derivatives_data": {
            "min_notional": 100.0,
            "taker_fee_pct": 0.0005,
            "step_size": 0.0001
        },
        "order_book_data": {
            "spread_pct": 0.0001
        }
    }
    
    result = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING"
    )
    
    assert result["approved"] is True
    # Risk amount must be bounded by hard_max_risk_usd (<= $2.00)
    assert result["risk_amount_usd"] <= 2.00
    assert result["notional_usd"] >= 105.0


@pytest.mark.asyncio
async def test_invariant1_reduce_size_hard_cap_enforced():
    """
    For REDUCE_SIZE actions, hard_max_risk_usd is capped at min(0.0150 * equity, 1.35).
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    total_balance = 88.0
    # hard_max_risk_usd = min(0.0150 * 88.0, 1.35) = 1.32
    
    ceo_decision = {
        "action": "REDUCE_SIZE",
        "decision": "SHORT",
        "conviction": 70,
        "entry_price": 3.0991,
        "sl_price": 3.1610,  # 2.0% stop -> $105.50 * 2% = $2.11 > $1.32
        "tp_price": 2.9500,
        "strategy_mode": "TREND_FOLLOWING",
        "timeframe": "15m",
        "symbol": "LIT-USD"
    }
    
    portfolio_data = {
        "total_balance": total_balance,
        "available_margin": 70.0,
        "active_positions": {},
        "recent_streak": [],
        "daily_drawdown_pct": 0.0,
        "win_rate": 50.0,
        "payoff_ratio": 1.5,
        "total_trades": 10,
    }
    
    market_data = {
        "price_data": {
            "current_price": 3.0991,
            "ohlcv_15m": [{"volume": 100} for _ in range(15)]
        },
        "indicators": {
            "atr_14": 0.05
        },
        "derivatives_data": {
            "min_notional": 100.0,
            "taker_fee_pct": 0.0005,
            "step_size": 0.1
        },
        "order_book_data": {
            "spread_pct": 0.0002
        }
    }
    
    result = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING"
    )
    
    assert result["approved"] is False
    assert result["trade_action"] == "WAIT_FOR_PULLBACK"
    assert "MIN_NOTIONAL_RISK_CAP_VETO" in result["veto_category"] or "REDUCE_SIZE_RISK_CAP_VETO" in result["veto_category"] or "MIN_NOTIONAL" in result["veto_category"]


# ═════════════════════════════════════════════════════════════════════════════
# INVARIANT 2: Stateful Directional Slot Quarantine on Stop-Out
# ═════════════════════════════════════════════════════════════════════════════

def test_invariant2_single_stopout_reduces_cap_for_90_minutes():
    """
    A single stop-out (<= -0.35R) in direction D must reduce effective_directional_cap
    from 2 to 1 for 90 minutes (5400s).
    """
    dq = DirectionalQuarantine()
    dq.reset_quarantine()
    
    now = 100000.0
    cap, _ = dq.get_effective_directional_cap("SHORT", base_cap=2, now_ts=now)
    assert cap == 2
    
    # Record stop-out of -0.90R in SHORT at t=100000
    dq.record_stopout("SHORT", timestamp=now, realized_r=-0.90)
    
    # At t + 45m (2700s): cap must be 1
    cap_45m, reason_45m = dq.get_effective_directional_cap("SHORT", base_cap=2, now_ts=now + 2700.0)
    assert cap_45m == 1
    assert "SINGLE_STOPOUT_QUARANTINE" in reason_45m
    
    # LONG direction remains unaffected (cap = 2)
    cap_long, _ = dq.get_effective_directional_cap("LONG", base_cap=2, now_ts=now + 2700.0)
    assert cap_long == 2
    
    # At t + 91m (5460s): quarantine expires, cap returns to 2
    cap_91m, _ = dq.get_effective_directional_cap("SHORT", base_cap=2, now_ts=now + 5460.0)
    assert cap_91m == 2


def test_invariant2_double_stopout_freezes_direction_for_120_minutes():
    """
    Two stopouts (<= -0.35R) in direction D within a rolling 180-minute window
    must freeze all new entries in direction D (effective_directional_cap = 0) for 120 minutes.
    """
    dq = DirectionalQuarantine()
    dq.reset_quarantine()
    
    now = 100000.0
    
    # 1st stopout at t=100000
    dq.record_stopout("SHORT", timestamp=now, realized_r=-1.00)
    
    # 2nd stopout at t=100000 + 3600s (60m later)
    dq.record_stopout("SHORT", timestamp=now + 3600.0, realized_r=-0.41)
    
    # At t + 4000s (elapsed from 2nd stop is 400s <= 7200s): cap must be 0 (FROZEN)
    cap_frozen, reason_frozen = dq.get_effective_directional_cap("SHORT", base_cap=2, now_ts=now + 4000.0)
    assert cap_frozen == 0
    assert "DOUBLE_STOPOUT_FREEZE" in reason_frozen
    
    # At t + 3600 + 7300s: freeze expires, recovers
    cap_recovered, _ = dq.get_effective_directional_cap("SHORT", base_cap=2, now_ts=now + 3600.0 + 7300.0)
    assert cap_recovered == 2


def test_invariant2_scratch_does_not_trigger_quarantine():
    """
    Trades closing with realized_r > -0.35R (e.g. scratch at -0.10R or small loss -0.25R)
    must NOT trigger directional slot quarantine.
    """
    dq = DirectionalQuarantine()
    dq.reset_quarantine()
    
    now = 100000.0
    # Scratch close at -0.15R
    dq.record_stopout("SHORT", timestamp=now, realized_r=-0.15)
    
    cap, _ = dq.get_effective_directional_cap("SHORT", base_cap=2, now_ts=now + 60.0)
    assert cap == 2


# ═════════════════════════════════════════════════════════════════════════════
# INVARIANT 3: On-Chain Ledger Purity
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_invariant3_on_chain_position_cannot_be_virtually_closed_without_order_success():
    """
    When a position is on-chain (has product_id/sender or is in live trading),
    a virtual close trigger must NEVER remove it from active_positions if on-chain close fails.
    """
    from services.nado_trading_service import NadoTradingService
    
    nts = NadoTradingService()
    nts.is_connected = True
    
    # Set up an on-chain position
    pos = {
        "symbol": "BTC-USD",
        "direction": "SHORT",
        "entry_price": 67226.0,
        "contracts": 0.0021,
        "product_id": 1,
        "sender": "0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa",
        "native_triggers_placed": True
    }
    nts.active_positions = {"BTC-USD": pos}
    
    # Mock client.market.close_position to simulate failure / network exception
    with patch.object(nts, "_save_positions") as mock_save, \
         patch.object(nts, "_record_closed_trade_journal") as mock_journal:
        
        # When client throws error
        nts.client = MagicMock()
        nts.client.subaccount.get_subaccounts.return_value = MagicMock(subaccounts=[MagicMock(subaccount="0x12d")])
        nts.client.market.close_position.side_effect = Exception("Network timeout / DEX RPC error")
        
        # Attempt force close
        success, pnl = await nts.force_close_position("BTC-USD", exit_reason="TIME_STOP_BE")
        
        # Must fail and NOT delete from active_positions or log to trade_journal
        assert success is False
        assert "BTC-USD" in nts.active_positions
        assert mock_journal.call_count == 0
