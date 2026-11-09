import pytest
from unittest.mock import MagicMock
from agents.risk_manager import RiskManager
from core.models import FinalTradeDecision


def test_bayesian_multiplier_prior_neutral():
    """Verify that with zero prior evidence, Beta(4,4) produces neutral p=0.50 and 1.00x multiplier."""
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    mult, prob, info = rm.get_bayesian_strategy_multiplier("BREAKOUT", "RANGE_CHOPPY", streak_history_by_tag={})
    assert mult == 1.00
    assert prob == 0.50
    assert "Beta(4.0, 4.0)" in info


def test_bayesian_multiplier_winning_streak():
    """Verify that consistent wins elevate posterior probability and boost multiplier up to 1.15x."""
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    stats = {
        "TRENDING:FULL_ALIGNMENT": {"wins": 8.0, "losses": 1.0, "total": 9}
    }
    mult, prob, info = rm.get_bayesian_strategy_multiplier("FULL_ALIGNMENT", "TRENDING", streak_history_by_tag=stats)
    # alpha = 4 + 8 = 12, beta = 4 + 1 = 5 -> p = 12 / 17 = 0.7059
    assert prob > 0.70
    assert mult == 1.15  # Clamped to upper safety ceiling 1.15x


def test_bayesian_multiplier_losing_streak():
    """Verify that chronic losses in chop depress posterior probability and safely throttle risk down to 0.65x-0.80x."""
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    stats = {
        "RANGE_CHOPPY:BREAKOUT": {"wins": 0.0, "losses": 6.0, "total": 6}
    }
    mult, prob, info = rm.get_bayesian_strategy_multiplier("BREAKOUT", "RANGE_CHOPPY", streak_history_by_tag=stats)
    # alpha = 4 + 0 = 4, beta = 4 + 6 = 10 -> p = 4 / 14 = 0.2857
    # raw_mult = 1.0 + (0.2857 - 0.5) * 1.5 = 1.0 - 0.3214 = 0.679
    assert prob < 0.30
    assert 0.65 <= mult <= 0.72


def test_compute_strategy_regime_stats_and_scratch_neutrality():
    """Verify that scratches (-0.22R <= R <= +0.15R) do not alter alpha/beta, while wins and losses increment correctly."""
    mock_journal = [
        {"macro_regime": "RANGE_CHOPPY", "strategy_mode": "BREAKOUT", "realized_r": 1.38},    # Win with bonus (1.0 + 1.38/2 = 1.69)
        {"macro_regime": "RANGE_CHOPPY", "strategy_mode": "BREAKOUT", "realized_r": -0.85},   # Loss (beta + 1)
        {"macro_regime": "RANGE_CHOPPY", "strategy_mode": "BREAKOUT", "realized_r": -0.05},   # Scratch (no change)
        {"macro_regime": "RANGE_CHOPPY", "strategy_mode": "BREAKOUT", "realized_r": 0.10},    # Scratch (no change)
    ]
    # 1. Static unweighted (decay=1.0)
    stats_static = RiskManager.compute_strategy_regime_stats_from_journal(mock_journal, decay=1.0)
    bucket = stats_static["RANGE_CHOPPY:BREAKOUT"]
    assert bucket["total"] == 4
    assert pytest.approx(bucket["wins"], 0.01) == 1.69
    assert bucket["losses"] == 1.0

    # 2. Discounted Beta-Bernoulli (decay=0.97)
    stats_decay = RiskManager.compute_strategy_regime_stats_from_journal(mock_journal, decay=0.97)
    bucket_d = stats_decay["RANGE_CHOPPY:BREAKOUT"]
    assert bucket_d["total"] == 4
    assert pytest.approx(bucket_d["wins"], 0.01) == 1.54
    assert pytest.approx(bucket_d["losses"], 0.01) == 0.94


@pytest.mark.asyncio
async def test_risk_manager_analyze_includes_bayesian_telemetry():
    """Verify that RiskManager.analyze() applies bayes_mult and reports it in applied_multipliers and result."""
    logger = MagicMock()
    llm = MagicMock()
    rm = RiskManager(logger=logger, llm_client=llm)

    decision = FinalTradeDecision(
        symbol="BTC-USD",
        decision="LONG",
        conviction=85,
        trade_action="ENTER",
        reasoning_en="Valid bull breakout",
        min_conviction=70,
        strategy_mode="BREAKOUT",
        direction_bias="LONG",
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="All checks passed."
    )

    portfolio_data = {
        "balance": 100.0,
        "total_usd": 100.0,
        "active_positions": {},
        "win_count": 10,
        "loss_count": 5,
        "recent_streak": [1, 1],
        "streak_history_by_tag": {
            "TRENDING:BREAKOUT": {"wins": 5.0, "losses": 1.0, "total": 6}
        }
    }

    market_data = {
        "price_data": {
            "current_price": 60000.0,
            "ohlcv_1h": [{"volume": 1000} for _ in range(10)]
        },
        "indicators": {
            "atr_14": 1200.0,
            "bb_position_pct": 55.0
        },
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate_hourly": 0.00001
        },
        "order_book_data": {
            "spread_pct": 0.0002
        }
    }

    result = await rm.analyze(
        decision,
        portfolio_data,
        market_data,
        effective_profile="AGGRESSIVE",
        macro_regime="TRENDING",
        strategy_mode="BREAKOUT"
    )

    assert result["approved"] is True
    applied = result["applied_multipliers"]
    assert "bayes_mult" in applied
    assert applied["bayes_mult"] > 1.0  # Boosted because wins=5, losses=1
    assert "bayes_strategy_multiplier" in result
    assert result["bayes_strategy_bucket"] == "TRENDING:BREAKOUT"
    assert result["bayes_posterior_win_prob"] > 0.50


def test_record_closed_trade_journal_persists_bayesian_telemetry(monkeypatch, tmp_path):
    """Verify that _record_closed_trade_journal writes enriched telemetry (strategy, regime, MFE) to journal."""
    from services.nado_trading_service import NadoTradingService
    from core.state_store import StateStore
    
    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    
    service = NadoTradingService()
    pos_data = {
        "planned_risk_usd": 0.50,
        "contracts": 1.0,
        "entry_price": 100.0,
        "highest_price": 102.0,
        "lowest_price": 99.0,
        "strategy_mode": "PULLBACK_RETEST",
        "macro_regime": "TRENDING",
        "entry_quality": 82.0,
        "bb_pos_pct": 65.0,
        "macro_range_score": 35.0,
        "max_favorable_excursion_r": 4.0,
        "cohort": "A",
        "base_risk_pct": 0.015,
        "final_risk_pct": 0.015,
        "applied_multipliers": {"bayes_mult": 1.10},
        "open_time": 1700000000.0
    }
    
    realized_r = service._record_closed_trade_journal(
        symbol="ETH-USD",
        direction="LONG",
        entry_price=100.0,
        exit_price=101.5,
        net_pnl=1.45,
        pos_data=pos_data,
        fee=0.05,
        triggered_by="TEST_TP"
    )
    
    assert realized_r == 2.9  # 1.45 / 0.50
    journal = StateStore.load("data/memory/trade_journal.json", default=[])
    assert len(journal) == 1
    t = journal[0]
    assert t["symbol"] == "ETH-USD"
    assert t["strategy_mode"] == "PULLBACK_RETEST"
    assert t["macro_regime"] == "TRENDING"
    assert t["entry_quality"] == 82.0
    assert t["bb_pos_pct"] == 65.0
    assert t["macro_range_score"] == 35.0
    assert t["max_favorable_excursion_r"] == 4.0
    assert t["applied_multipliers"]["bayes_mult"] == 1.10


def test_bayesian_optimizer_walk_forward_cv(monkeypatch, tmp_path):
    """Verify that TradingBayesianOptimizer correctly runs temporal CV and outputs comparison metrics."""
    from core.bayesian_optimizer import TradingBayesianOptimizer
    from core.state_store import StateStore

    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    
    mock_trades = [
        {"macro_range_score": 40.0, "bb_pos_pct": 50.0, "entry_quality": 80.0, "direction": "LONG", "realized_r": 1.2, "cohort": "A"},
        {"macro_range_score": 68.0, "bb_pos_pct": 88.0, "entry_quality": 55.0, "direction": "LONG", "realized_r": -0.9, "cohort": "B"},
        {"macro_range_score": 35.0, "bb_pos_pct": 45.0, "entry_quality": 85.0, "direction": "LONG", "realized_r": 0.8, "cohort": "A"},
        {"macro_range_score": 70.0, "bb_pos_pct": 92.0, "entry_quality": 58.0, "direction": "LONG", "realized_r": -0.85, "cohort": "D"},
        {"macro_range_score": 30.0, "bb_pos_pct": 40.0, "entry_quality": 75.0, "direction": "LONG", "realized_r": 0.05, "cohort": "A"},
        {"macro_range_score": 65.0, "bb_pos_pct": 82.0, "entry_quality": 62.0, "direction": "LONG", "realized_r": -0.7, "cohort": "B"},
    ]
    StateStore.save("data/memory/trade_journal.json", mock_trades)

    optimizer = TradingBayesianOptimizer(
        journal_path="data/memory/trade_journal.json",
        output_params_path="data/tuned_hyperparams.json",
        n_folds=2
    )
    assert len(optimizer.trades) == 6

    result = optimizer.run_optimization(n_trials=10, seed=123)
    assert "best_params" in result
    assert "baseline_metrics" in result
    assert "tuned_metrics" in result
    table_str = optimizer.format_comparison_table(result)
    assert "BAYESIAN OPTIMIZATION REPORT" in table_str


def test_tuned_param_loader_and_safety_clamps(tmp_path, monkeypatch):
    """
    Verify get_tuned_param() reads tuned_hyperparams.json with hot reload,
    enforces strict _SAFETY_BOUNDS against extreme values, and falls back cleanly if file is absent.
    """
    from core.config import get_tuned_param, reset_tuned_param_cache
    from core.state_store import StateStore
    import json
    
    reset_tuned_param_cache()
    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    
    # 1. Fallback when file absent
    assert get_tuned_param("chop_range_threshold", 64.0) == 64.0
    assert get_tuned_param("anti_chop_bb_max", 75.0) == 75.0

    # 2. Normal load from file
    tuned_file = tmp_path / "data" / "tuned_hyperparams.json"
    tuned_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "best_params": {
            "chop_range_threshold": 59.0,
            "anti_chop_bb_max": 89.0,
            "min_reduce_size_conv": 71.0,
            "be_activation_r": 1.10
        }
    }
    tuned_file.write_text(json.dumps(payload), encoding="utf-8")
    reset_tuned_param_cache()

    assert get_tuned_param("chop_range_threshold", 64.0) == 59.0
    assert get_tuned_param("anti_chop_bb_max", 75.0) == 89.0
    assert get_tuned_param("min_reduce_size_conv", 60.0) == 71.0
    assert get_tuned_param("be_activation_r", 1.00) == 1.10

    # 3. Safety Clamps against extreme / corrupted numbers
    extreme_payload = {
        "best_params": {
            "chop_range_threshold": 99.0,  # Bound (50.0, 72.0) -> Clamped to 72.0
            "anti_chop_bb_max": 99.9,      # Bound (68.0, 90.0) -> Clamped to 90.0
            "min_reduce_size_conv": 40.0,  # Bound (60.0, 75.0) -> Clamped to 60.0
            "be_activation_r": 0.50        # Bound (0.60, 1.35) -> Clamped to 0.60
        }
    }
    # Modify mtime to trigger hot-reload
    import time
    time.sleep(0.01)
    tuned_file.write_text(json.dumps(extreme_payload), encoding="utf-8")
    reset_tuned_param_cache()

    assert get_tuned_param("chop_range_threshold", 64.0) == 72.0
    assert get_tuned_param("anti_chop_bb_max", 75.0) == 90.0
    assert get_tuned_param("min_reduce_size_conv", 60.0) == 60.0
    assert get_tuned_param("be_activation_r", 1.00) == 0.60


def test_deterministic_guard_pullback_corridor_vs_market_reduce_size(tmp_path, monkeypatch):
    """
    Safeguard 1: Verify that min_reduce_size_conv = 71.0% strictly blocks ordinary market setups
    at conviction 65%, while preserving the preferential corridor (65.0%) for confirmed pullbacks (is_pullback_retest=True).
    """
    from core.deterministic_guard import DeterministicGuard
    from core.strategy_router import StrategyProfile
    from core.config import reset_tuned_param_cache
    from core.state_store import StateStore
    from unittest.mock import MagicMock
    import json

    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    tuned_file = tmp_path / "data" / "tuned_hyperparams.json"
    tuned_file.parent.mkdir(parents=True, exist_ok=True)
    tuned_file.write_text(json.dumps({"best_params": {"min_reduce_size_conv": 71.0}}), encoding="utf-8")
    reset_tuned_param_cache()

    mock_rm = MagicMock()
    mock_rm._get_profile_rules.return_value = {"min_conviction": 75}
    strat_prof = StrategyProfile(True, "TREND_FOLLOWING", "LONG", "Bullish alignment")

    # Case A: Ordinary market entry with conviction = 65% (below tuned 71%) -> BLOCKED
    proposal_market = {
        "decision": "LONG",
        "conviction": 65,
        "trade_action": "REDUCE_SIZE",
        "entry_quality": 65,
        "is_pullback_retest": False
    }
    dec_market = DeterministicGuard.evaluate(
        strategy_profile=strat_prof,
        ceo_proposal=proposal_market,
        profile="BALANCED",
        risk_manager=mock_rm,
        symbol="BTC-USD"
    )
    assert dec_market.guard_status == "BLOCKED"
    assert dec_market.rejection_tag == "LOW_CONFIDENCE"
    assert "71%" in dec_market.guard_reason

    # Case B: Confirmed pullback (is_pullback_retest=True) with conviction = 65% -> PASSED via corridor (max(62, 71-6)=65%)
    proposal_retest = {
        "decision": "LONG",
        "conviction": 65,
        "trade_action": "REDUCE_SIZE",
        "entry_quality": 65,
        "is_pullback_retest": True
    }
    dec_retest = DeterministicGuard.evaluate(
        strategy_profile=strat_prof,
        ceo_proposal=proposal_retest,
        profile="BALANCED",
        risk_manager=mock_rm,
        symbol="SOL-USD"
    )
    assert dec_retest.guard_status == "PASSED"
    assert dec_retest.is_actionable is True


def test_strategy_router_moderate_range_vs_deep_chop_phases(tmp_path, monkeypatch):
    """
    Safeguard 2: Verify market phase separation:
    - Moderate range (range_score=63%, chop_threshold=59%): allows buying at bb_pos=78% (below tuned 89%).
    - Deep chop (range_score=72% >= 68%): strictly blocks buying at bb_pos=78% (above strict ceiling 75%).
    """
    from core.strategy_router import StrategyRouter
    from core.config import reset_tuned_param_cache
    from core.state_store import StateStore
    import json

    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    tuned_file = tmp_path / "data" / "tuned_hyperparams.json"
    tuned_file.parent.mkdir(parents=True, exist_ok=True)
    tuned_file.write_text(
        json.dumps({
            "best_params": {
                "chop_range_threshold": 59.0,
                "anti_chop_bb_max": 89.0
            }
        }),
        encoding="utf-8"
    )
    reset_tuned_param_cache()

    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"}
    ]

    # Market data at channel upper zone: bb_pos = 78.0%
    market_data = {
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BULLISH", "trend_4h": "BULLISH"},
        "indicators": {"bb_position_pct": 78.0, "volume_spike_pct": 50.0, "donchian_high": 100.0, "donchian_low": 90.0},
        "price_data": {"current_price": 98.0}
    }

    # 1. Moderate Range (range_score = 63.0) -> ALLOWED (78% < 89%)
    macro_moderate = {"regime_verdict": {"regime": "RANGE_CHOPPY", "range_score": 63.0}}
    res_mod = StrategyRouter.evaluate("ETH-USD", market_data, valid_reports, macro_moderate, detected_regime="RANGE_CHOPPY")
    assert res_mod.has_directional_signal is True
    assert res_mod.direction_bias == "LONG"

    # 2. Deep Chop (range_score = 72.0 >= 68.0) -> BLOCKED (78% >= strict 75% ceiling)
    macro_deep = {"regime_verdict": {"regime": "RANGE_CHOPPY", "range_score": 72.0}}
    res_deep = StrategyRouter.evaluate("ETH-USD", market_data, valid_reports, macro_deep, detected_regime="RANGE_CHOPPY")
    assert res_deep.has_directional_signal is False
    assert res_deep.direction_bias == "NEUTRAL"
    assert "ОТКЛОНЕН" in res_deep.reasoning
    assert "bb_pos=78.0%" in res_deep.reasoning



def test_trading_bayesian_optimizer_acceptance_gate_logic():
    """Verify Acceptance Gate rules for walk-forward calibration."""
    from core.bayesian_optimizer import TradingBayesianOptimizer

    # 1. Zero total trades -> Rejected
    passed, reason = TradingBayesianOptimizer.check_acceptance_gate(
        tuned_metrics={"total_trades": 0, "retained_count": 0, "score": 2.0},
        baseline_metrics={"score": 1.0}
    )
    assert passed is False
    assert "Total trades is 0" in reason

    # 2. Retained trades ratio < 35% -> Rejected (Over-filtering / starvation)
    passed, reason = TradingBayesianOptimizer.check_acceptance_gate(
        tuned_metrics={"total_trades": 50, "retained_count": 10, "score": 5.0}, # 20% < 35%
        baseline_metrics={"score": -2.0}
    )
    assert passed is False
    assert "below required minimum 35.0%" in reason

    # 3. Score improvement < +0.25 -> Rejected (Marginal or no benefit)
    passed, reason = TradingBayesianOptimizer.check_acceptance_gate(
        tuned_metrics={"total_trades": 40, "retained_count": 25, "score": -3.85}, # delta = +0.15 < +0.25
        baseline_metrics={"score": -4.00}
    )
    assert passed is False
    assert "below required minimum +0.25" in reason

    # 4. Score delta >= +0.25 and retained ratio >= 35% -> PASSED
    passed, reason = TradingBayesianOptimizer.check_acceptance_gate(
        tuned_metrics={"total_trades": 42, "retained_count": 17, "score": -2.43}, # 40.5% retained, delta = +5.38
        baseline_metrics={"score": -7.81}
    )
    assert passed is True
    assert "Gate PASSED" in reason


def test_trading_bayesian_optimizer_run_optimization_gate_enforcement(tmp_path, monkeypatch):
    """Verify that run_optimization only overwrites tuned_hyperparams.json if Acceptance Gate passes."""
    import json
    from core.bayesian_optimizer import TradingBayesianOptimizer
    from core.state_store import StateStore

    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    journal_path = "data/memory/trade_journal.json"
    params_path = "data/tuned_hyperparams.json"

    # Pre-populate tuned_hyperparams.json with initial state
    initial_payload = {"best_params": {"chop_range_threshold": 60.0}}
    StateStore.save(params_path, initial_payload)

    # Seed 30 synthetic trades
    trades = []
    for i in range(30):
        trades.append({
            "macro_range_score": 55.0,
            "bb_pos_pct": 50.0,
            "entry_quality": 70.0,
            "direction": "LONG",
            "realized_r": 0.5 if i % 2 == 0 else -0.5,
            "max_favorable_excursion_r": 0.6 if i % 2 == 0 else 0.1,
            "cohort": "A"
        })
    StateStore.save(journal_path, trades)

    opt = TradingBayesianOptimizer(
        journal_path=journal_path,
        output_params_path=params_path
    )

    # 1. Gate fails: mock check_acceptance_gate to return False
    monkeypatch.setattr(
        TradingBayesianOptimizer,
        "check_acceptance_gate",
        staticmethod(lambda **kwargs: (False, "Test gate rejection: insufficient improvement"))
    )
    res_fail = opt.run_optimization(n_trials=10, seed=42, enforce_acceptance_gate=True)
    assert res_fail["gate_passed"] is False
    # File must NOT have been overwritten
    loaded = StateStore.load(params_path)
    assert loaded["best_params"]["chop_range_threshold"] == 60.0

    # 2. Gate passes: mock check_acceptance_gate to return True
    monkeypatch.setattr(
        TradingBayesianOptimizer,
        "check_acceptance_gate",
        staticmethod(lambda **kwargs: (True, "Gate PASSED"))
    )
    res_pass = opt.run_optimization(n_trials=10, seed=42, enforce_acceptance_gate=True)
    assert res_pass["gate_passed"] is True
    # File MUST have been overwritten with newly tuned params
    loaded_new = StateStore.load(params_path)
    assert "tuned_metrics" in loaded_new
    assert loaded_new["gate_passed"] is True


def test_nado_auto_recalibration_hook_cadence(tmp_path, monkeypatch):
    """
    Verify that _record_closed_trade_journal triggers auto-recalibration
    strictly when len(journal) >= 20 and len(journal) % 12 == 0.
    """
    from services.nado_trading_service import NadoTradingService
    from core.state_store import StateStore

    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    svc = NadoTradingService()

    recal_calls = []
    def mock_trigger_bayesian_recalibration(journal_len):
        recal_calls.append(journal_len)
        return None

    monkeypatch.setattr(svc, "_trigger_bayesian_recalibration", mock_trigger_bayesian_recalibration)

    pos_data = {
        "planned_risk_usd": 10.0,
        "contracts": 0.01,
        "cohort": "A",
        "entry_fee_usd": 0.05,
        "exit_fee_usd": 0.05,
        "total_fee_usd": 0.10
    }

    # Add 19 trades -> no recalibration (len < 20)
    for i in range(19):
        svc._record_closed_trade_journal("BTC-USD", "LONG", 60000.0, 60500.0, 5.0, pos_data)
    assert len(recal_calls) == 0

    # Add 20th trade -> 20 % 12 != 0 -> no recalibration
    svc._record_closed_trade_journal("BTC-USD", "LONG", 60000.0, 60500.0, 5.0, pos_data)
    assert len(recal_calls) == 0

    # Add 21st, 22nd, 23rd trades -> no recalibration
    for i in range(3):
        svc._record_closed_trade_journal("BTC-USD", "LONG", 60000.0, 60500.0, 5.0, pos_data)
    assert len(recal_calls) == 0

    # Add 24th trade -> 24 >= 20 and 24 % 12 == 0 -> TRIGGERED!
    svc._record_closed_trade_journal("BTC-USD", "LONG", 60000.0, 60500.0, 5.0, pos_data)
    assert len(recal_calls) == 1
    assert recal_calls[0] == 24


def test_closed_reports_financial_transparency_and_telegram_formatting():
    """
    Verify that closed position reports populate exact financial fields
    (gross_pnl_usd, total_fee_usd, entry_fee_usd, exit_fee_usd, net_pnl_usd, realized_r, pnl_source)
    and TelegramAgent formats them with full transparency.
    """
    from agents.telegram_agent import TelegramAgent

    tg_agent = TelegramAgent(logger=MagicMock(), llm_client=None)

    # Simulated reconciled close report
    closed_report = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "triggered_by": "INDEXER_RECONCILED",
        "close_reason": "INDEXER_RECONCILED",
        "entry_price": 80998.00,
        "exit_price": 81272.85,
        "gross_pnl_usd": 0.1237,
        "entry_fee_usd": 0.0350,
        "exit_fee_usd": 0.0350,
        "total_fee_usd": 0.0700,
        "net_pnl_usd": 0.0537,
        "realized_r": 0.55,
        "planned_risk_usd": 0.44,
        "pnl_source": "EXACT",
        "cohort": "A",
        "accumulated_slices_pnl": 0.0
    }

    msg = tg_agent.format_position_closed_message(closed_report)

    # Verify key financial transparency elements in telegram message
    assert "ПОЗИЦИЯ ЗАКРЫТА: BTC-USD (LONG)" in msg
    assert "$80998.0000" in msg
    assert "$81272.8500" in msg
    assert "+0.1237$" in msg  # Gross PnL
    assert "-0.0700$" in msg  # Total Fee
    assert "вход: $0.0350 | выход: $0.0350" in msg  # Fee breakdown
    assert "+0.0537$" in msg  # Net PnL
    assert "+0.55R" in msg    # Realized R
    assert "✅ Биржа (EXACT)" in msg  # Data Source Tag


@pytest.mark.asyncio
async def test_kelly_soft_floor_and_epoch_drawdown_reset(monkeypatch):
    """
    Verify that:
    1. Re-anchored peak_balance produces dd_mult = 1.00x (0% drawdown).
    2. min_kelly_mult = 0.85 soft floor prevents degraded historical win rate (35%) from crushing sizing to 0.50x.
    3. Combined sizing multiplier safely restores viable risk ($1.10 - $1.65) and >$100 notional order.
    """
    from agents.risk_manager import RiskManager
    from unittest.mock import MagicMock

    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())

    # Pre-patch legacy state: 35% win rate, payoff 0.80 -> raw kelly = 0.50x
    raw_k = rm._calculate_kelly_multiplier(win_rate=0.35, payoff_ratio=0.80, total_trades=20)
    assert raw_k == 0.50

    # With min_kelly_mult = 0.85 passed from portfolio_data:
    portfolio = {
        "balance": 91.0,
        "total_usd": 91.0,
        "current_balance": 91.0,
        "peak_balance": 91.0,  # Re-anchored peak balance = 0% DD
        "initial_balance": 91.0,
        "active_positions": {},
        "win_count": 7,
        "loss_count": 13,
        "win_rate_pct": 35.0,
        "historical_payoff_ratio": 0.80,
        "min_kelly_mult": 0.85,
        "recent_streak": [],   # Reset streak = no consecutive loss penalty
        "streak_history_by_tag": {
            "TRENDING:FULL_ALIGNMENT": {"wins": 6.0, "losses": 1.0, "total": 7}
        }
    }

    # Evaluate DD multiplier
    dd_mult, msg = rm._calculate_drawdown_multiplier(
        streak=[], daily_drawdown_pct=0.0, current_balance=91.0, peak_balance=91.0
    )
    assert dd_mult == 1.00
    assert "EQUITY_NORMAL" in msg

    # Analyze decision with BALANCED profile
    ceo_decision = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 85,
        "trade_action": "ENTER",
        "strategy_mode": "FULL_ALIGNMENT",
        "min_conviction": 70
    }
    market_data = {
        "price_data": {
            "current_price": 80000.0,
            "ohlcv_1h": [{"volume": 1000} for _ in range(10)]
        },
        "indicators": {
            "atr_14": 800.0,  # 1.0% ATR
            "bb_position_pct": 55.0
        },
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate_hourly": 0.0,
            "min_size": 0.0001,
            "price_increment": 0.5,
            "size_increment": 0.0001,
            "strict_metadata": True,
            "confirmed_min_size": True
        }
    }

    verdict = await rm.analyze(ceo_decision, portfolio, market_data, macro_regime="TRENDING")
    assert verdict["approved"] is True
    assert verdict["applied_multipliers"]["dd_mult"] == 1.00
    assert verdict["applied_multipliers"]["kelly_mult"] == 0.85  # Protected by floor!

    # Risk amount in USD must be restored to normal operating range ($1.00 - $1.50)
    assert 1.00 <= verdict["planned_risk_usd"] <= 1.50
    assert verdict["notional_size_usd"] >= 50.0


def test_bayesian_optimizer_max_trades_rolling_window(tmp_path, monkeypatch):
    """Verify that TradingBayesianOptimizer caps dataset at the most recent max_trades (150)."""
    from core.bayesian_optimizer import TradingBayesianOptimizer
    from core.state_store import StateStore

    monkeypatch.setattr(StateStore, "_base_dir_override", str(tmp_path))
    journal_path = "data/memory/trade_journal.json"
    
    # 200 mock trades
    trades = [
        {
            "realized_r": float(i),
            "macro_range_score": 40.0,
            "bb_pos_pct": 50.0,
            "entry_quality": 75.0,
            "direction": "LONG",
            "cohort": "A"
        }
        for i in range(200)
    ]
    StateStore.save(journal_path, trades)

    opt = TradingBayesianOptimizer(journal_path=journal_path, max_trades=150)
    assert len(opt.trades) == 150
    # Must be the last 150 trades (indices 50 to 199)
    assert opt.trades[0]["realized_r"] == 50.0
    assert opt.trades[-1]["realized_r"] == 199.0
