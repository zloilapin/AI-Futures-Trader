import os
import sys

_parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import json
import math
from typing import Dict, List, Any, Tuple
import optuna

# Silence Optuna logs
optuna.logging.set_verbosity(optuna.logging.WARNING)

BASELINE_PARAMS: Dict[str, float] = {
    "chop_range_threshold": 64.0,
    "anti_chop_bb_max": 75.0,
    "min_reduce_size_conv": 60.0,
    "be_activation_r": 0.75,
}


class TradingBayesianOptimizer:
    """
    Bayesian Optimizer (Optuna TPE - Tree-structured Parzen Estimator)
    for walk-forward calibration of deterministic thresholds across StrategyRouter,
    DeterministicGuard, and Sentinel using cross-validated Sortino Expectancy.
    """

    def __init__(
        self,
        journal_path: str = "data/memory/trade_journal.json",
        output_params_path: str = "data/tuned_hyperparams.json",
        n_folds: int = 3,
        max_trades: int = 150
    ):
        self.journal_path = journal_path
        self.output_params_path = output_params_path
        self.n_folds = n_folds
        self.max_trades = max_trades
        self.trades = self._load_trades()

    def _load_trades(self) -> List[Dict[str, Any]]:
        from core.state_store import StateStore
        trades = StateStore.load(self.journal_path, default=[])
        if not isinstance(trades, list):
            return []
        max_limit = getattr(self, "max_trades", 150)
        if max_limit > 0 and len(trades) > max_limit:
            return trades[-max_limit:]
        return trades

    def _split_into_folds(self, records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Splits trades chronologically into n_folds temporal blocks."""
        n = len(records)
        if n < self.n_folds:
            return [records]
        fold_size = n // self.n_folds
        folds = []
        for i in range(self.n_folds):
            start = i * fold_size
            end = (i + 1) * fold_size if i < self.n_folds - 1 else n
            folds.append(records[start:end])
        return folds

    def simulate_trades(
        self, trades: List[Dict[str, Any]], params: Dict[str, float]
    ) -> Dict[str, Any]:
        """Simulates filtering and Break-Even adjustments on a list of trades."""
        retained: List[float] = []
        filtered_count = 0
        cum_r = 0.0
        peak_r = 0.0
        max_dd_r = 0.0

        for trade in trades:
            range_score = float(trade.get("macro_range_score", 45.0) or 45.0)
            bb_pos = float(trade.get("bb_pos_pct", 50.0) or 50.0)
            entry_qual = float(trade.get("entry_quality", 70.0) or 70.0)
            direction = str(trade.get("direction", "LONG")).upper()
            raw_r = float(trade.get("realized_r", 0.0) or 0.0)
            mfe_r = float(trade.get("max_favorable_excursion_r", max(raw_r, 0.0)) or 0.0)
            cohort = str(trade.get("cohort", "A")).upper()

            # 1. Anti-Chop Gate
            if range_score >= params["chop_range_threshold"]:
                if direction == "LONG" and bb_pos >= params["anti_chop_bb_max"]:
                    filtered_count += 1
                    continue
                if direction == "SHORT" and bb_pos <= (100.0 - params["anti_chop_bb_max"]):
                    filtered_count += 1
                    continue

            # 2. Conviction Gate on reduced size / Cohorts B and D
            if cohort in ["B", "D"] and entry_qual < params["min_reduce_size_conv"]:
                filtered_count += 1
                continue

            # 3. Sentinel Break-Even & 2-Tier Partial TP Simulation
            adj_r = raw_r
            trade_notional = float(trade.get("notional_usd", trade.get("size_usd", 0.0)) or 0.0)
            is_small_pos = (trade_notional > 0.0 and trade_notional < 200.0) or bool(trade.get("small_pos_protect"))
            if mfe_r >= params["be_activation_r"]:
                if is_small_pos:
                    # Small positions (< $200) operate under small_pos_protect without 50% partial TP
                    if raw_r < 0.0:
                        adj_r = 0.05
                    else:
                        adj_r = raw_r
                else:
                    if raw_r < 0.0:
                        # Tier 1 locked in 50% at be_activation_r; remaining 50% exited at BE (+0.05R net fees)
                        adj_r = (0.50 * params["be_activation_r"]) + (0.50 * 0.05)
                    else:
                        # Tier 1 locked in 50% at be_activation_r; remaining 50% ran to raw_r
                        adj_r = (0.50 * params["be_activation_r"]) + (0.50 * raw_r)

            retained.append(adj_r)
            cum_r += adj_r
            peak_r = max(peak_r, cum_r)
            max_dd_r = max(max_dd_r, peak_r - cum_r)

        n = len(retained)
        wins = [r for r in retained if r > 0.15]
        losses = [r for r in retained if r < -0.22]
        scratches = [r for r in retained if -0.22 <= r <= 0.15]

        total_r = sum(retained)
        downside_sq = sum(r ** 2 for r in retained if r < 0.0)
        downside_dev = math.sqrt(downside_sq / n) if (downside_sq > 0 and n > 0) else 0.1

        activity_factor = min(1.0, n / max(1.0, len(trades) * 0.70)) if trades else 1.0
        sortino = (total_r / (downside_dev * math.sqrt(n))) if n > 0 else -10.0
        score = (sortino * activity_factor) - (0.35 * max_dd_r)

        return {
            "total_trades": len(trades),
            "retained_count": n,
            "filtered_count": filtered_count,
            "wins_count": len(wins),
            "losses_count": len(losses),
            "scratches_count": len(scratches),
            "win_rate_pct": round(len(wins) / (len(wins) + len(losses)) * 100, 1) if (len(wins) + len(losses)) > 0 else 0.0,
            "net_r": round(total_r, 4),
            "max_dd_r": round(max_dd_r, 4),
            "avg_r": round(total_r / n, 4) if n > 0 else 0.0,
            "score": round(score, 4)
        }

    def evaluate_cv(self, params: Dict[str, float]) -> Tuple[float, List[float], Dict[str, Any]]:
        """Evaluates parameters across temporal folds with cross-validation penalty."""
        if not self.trades:
            return 0.0, [], {}

        folds = self._split_into_folds(self.trades)
        fold_scores = []
        for f in folds:
            res = self.simulate_trades(f, params)
            fold_scores.append(res["score"])

        mean_score = sum(fold_scores) / len(fold_scores)
        variance = sum((s - mean_score) ** 2 for s in fold_scores) / len(fold_scores)
        std_score = math.sqrt(variance)

        # Objective = Mean - 0.5 * Std (penalizes volatility between temporal blocks)
        objective_value = mean_score - (0.5 * std_score)
        overall_stats = self.simulate_trades(self.trades, params)
        return objective_value, fold_scores, overall_stats

    def objective(self, trial: optuna.Trial) -> float:
        params = {
            "chop_range_threshold": trial.suggest_float("chop_range_threshold", 50.0, 75.0, step=1.0),
            "anti_chop_bb_max": trial.suggest_float("anti_chop_bb_max", 65.0, 90.0, step=1.0),
            "min_reduce_size_conv": trial.suggest_float("min_reduce_size_conv", 58.0, 75.0, step=1.0),
            "be_activation_r": trial.suggest_float("be_activation_r", 1.10, 1.40, step=0.05),
        }
        obj_val, _, _ = self.evaluate_cv(params)
        return obj_val

    @staticmethod
    def check_acceptance_gate(
        tuned_metrics: Dict[str, Any],
        baseline_metrics: Dict[str, Any],
        min_score_delta: float = 0.25,
        min_retained_ratio: float = 0.35,
    ) -> Tuple[bool, str]:
        """
        Acceptance Gate for Walk-Forward Bayesian Calibration.
        Validates whether calibrated hyperparameters provide meaningful, robust
        improvement without excessively starving trade volume.
        """
        total_trades = tuned_metrics.get("total_trades", 0)
        if total_trades <= 0:
            return False, "Total trades is 0; insufficient data for calibration gate."

        retained = tuned_metrics.get("retained_count", 0)
        retained_ratio = retained / total_trades
        if retained_ratio < min_retained_ratio:
            return False, (
                f"Retained trades ratio ({retained_ratio:.1%}) below required minimum {min_retained_ratio:.1%} "
                f"({retained}/{total_trades})."
            )

        tuned_score = tuned_metrics.get("score", -999.0)
        baseline_score = baseline_metrics.get("score", -999.0)
        score_delta = tuned_score - baseline_score
        if score_delta < min_score_delta:
            return False, (
                f"Tuned score improvement ({score_delta:+.4f}) below required minimum +{min_score_delta:.2f} "
                f"(tuned: {tuned_score:.4f}, baseline: {baseline_score:.4f})."
            )

        return True, (
            f"Gate PASSED: Score delta={score_delta:+.4f} >= +{min_score_delta:.2f}, "
            f"Retained={retained_ratio:.1%} >= {min_retained_ratio:.1%}."
        )

    def run_optimization(
        self,
        n_trials: int = 120,
        seed: int = 42,
        enforce_acceptance_gate: bool = True
    ) -> Dict[str, Any]:
        """
        Executes Optuna TPE search over trade journal history.
        Evaluates cross-validated objective and enforces the Acceptance Gate
        before updating active hyperparameter state.
        """
        # Reload latest trades from journal
        self.trades = self._load_trades()

        sampler = optuna.samplers.TPESampler(seed=seed, n_startup_trials=25, multivariate=True)
        study = optuna.create_study(direction="maximize", sampler=sampler)
        study.optimize(self.objective, n_trials=n_trials, show_progress_bar=False)

        best_params = study.best_params
        best_obj, best_folds, tuned_stats = self.evaluate_cv(best_params)
        baseline_obj, baseline_folds, baseline_stats = self.evaluate_cv(BASELINE_PARAMS)

        result_payload = {
            "n_trials": n_trials,
            "baseline_params": BASELINE_PARAMS,
            "best_params": best_params,
            "baseline_metrics": {
                "objective_score": round(baseline_obj, 4),
                "fold_scores": [round(s, 4) for s in baseline_folds],
                **baseline_stats
            },
            "tuned_metrics": {
                "objective_score": round(best_obj, 4),
                "fold_scores": [round(s, 4) for s in best_folds],
                **tuned_stats
            }
        }

        gate_passed = True
        gate_reason = "Acceptance gate bypassed"
        if enforce_acceptance_gate:
            gate_passed, gate_reason = self.check_acceptance_gate(
                tuned_metrics=result_payload["tuned_metrics"],
                baseline_metrics=result_payload["baseline_metrics"]
            )
        result_payload["gate_passed"] = gate_passed
        result_payload["gate_reason"] = gate_reason

        if gate_passed:
            # Save to file atomically and reset in-memory cache
            from core.state_store import StateStore
            StateStore.save(self.output_params_path, result_payload)
            from core.config import reset_tuned_param_cache
            reset_tuned_param_cache()
        else:
            import logging
            logging.getLogger("ai_trader").warning(
                f"[TradingBayesianOptimizer] ⚠️ Recalibration candidate rejected by Acceptance Gate: {gate_reason}. "
                f"Existing parameters retained."
            )

        return result_payload

    @staticmethod
    def format_comparison_table(result: Dict[str, Any]) -> str:
        b_p = result["baseline_params"]
        t_p = result["best_params"]
        b_m = result["baseline_metrics"]
        t_m = result["tuned_metrics"]
        gate_passed = result.get("gate_passed", True)
        gate_reason = result.get("gate_reason", "N/A")

        lines = []
        lines.append("\n" + "=" * 78)
        lines.append("   BAYESIAN OPTIMIZATION REPORT: BASELINE vs OPTUNA TUNED PARAMS")
        lines.append("=" * 78)
        lines.append(f"{'PARAMETER / METRIC':<32} | {'BASELINE':<18} | {'TUNED (OPTUNA)':<18}")
        lines.append("-" * 78)
        lines.append(f"{'chop_range_threshold':<32} | {b_p['chop_range_threshold']:<18.1f} | {t_p['chop_range_threshold']:<18.1f}")
        lines.append(f"{'anti_chop_bb_max':<32} | {b_p['anti_chop_bb_max']:<18.1f} | {t_p['anti_chop_bb_max']:<18.1f}")
        lines.append(f"{'min_reduce_size_conv':<32} | {b_p['min_reduce_size_conv']:<18.1f} | {t_p['min_reduce_size_conv']:<18.1f}")
        lines.append(f"{'be_activation_r':<32} | {b_p['be_activation_r']:<18.2f} | {t_p['be_activation_r']:<18.2f}")
        lines.append("-" * 78)
        lines.append(f"{'Total Trades Retained':<32} | {b_m['retained_count']}/{b_m['total_trades']:<15} | {t_m['retained_count']}/{t_m['total_trades']:<15}")
        lines.append(f"{'Trades Filtered (Skipped)':<32} | {b_m['filtered_count']:<18} | {t_m['filtered_count']:<18}")
        lines.append(f"{'Win / Loss / Scratch':<32} | {b_m['wins_count']}/{b_m['losses_count']}/{b_m['scratches_count']:<18} | {t_m['wins_count']}/{t_m['losses_count']}/{t_m['scratches_count']:<18}")
        lines.append(f"{'Win Rate % (excl. scratch)':<32} | {b_m['win_rate_pct']:<17.1f}% | {t_m['win_rate_pct']:<17.1f}%")
        lines.append(f"{'Net Realized R':<32} | {b_m['net_r']:<+17.2f}R | {t_m['net_r']:<+17.2f}R")
        lines.append(f"{'Max Drawdown (R)':<32} | {b_m['max_dd_r']:<17.2f}R | {t_m['max_dd_r']:<17.2f}R")
        lines.append(f"{'Average R per Trade':<32} | {b_m['avg_r']:<+17.3f}R | {t_m['avg_r']:<+17.3f}R")
        lines.append("-" * 78)
        lines.append(f"{'CV Objective Score':<32} | {b_m['objective_score']:<18.4f} | {t_m['objective_score']:<18.4f}")
        lines.append(f"{'Fold Scores (F1, F2, F3)':<32} | {str(b_m['fold_scores']):<18} | {str(t_m['fold_scores']):<18}")
        lines.append("-" * 78)
        status_str = "ACCEPTED (PASSED)" if gate_passed else "REJECTED (FAILED)"
        lines.append(f"{'Acceptance Gate Status':<32} | {status_str:<39}")
        lines.append(f"{'Gate Evaluation Detail':<32} | {gate_reason:<39}")
        lines.append("=" * 78)
        return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Trading Bayesian Optimizer (Contour 1)")
    parser.add_argument("--trials", type=int, default=120, help="Number of Optuna TPE trials (default: 120)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    args = parser.parse_args()

    optimizer = TradingBayesianOptimizer()
    print(f"[OPTIMIZER] Running Optuna TPE Bayesian Optimization ({args.trials} trials) over {len(optimizer.trades)} trades...")
    result = optimizer.run_optimization(n_trials=args.trials, seed=args.seed)
    print(optimizer.format_comparison_table(result))


if __name__ == "__main__":
    main()
