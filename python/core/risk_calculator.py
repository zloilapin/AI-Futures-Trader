"""
Unified Risk & Loss Calculator for AI Futures Trader.
Centralizes calculation of price risk, round-trip taker trading fees,
slippage stress estimates, and funding cost estimates across RiskManager,
Pipeline, and NadoTradingService.
"""

from dataclasses import dataclass
from typing import Optional

@dataclass
class LossEstimationResult:
    contracts: float
    fill_price: float
    sl_price: float
    price_loss_usd: float
    taker_fee_pct: float
    fee_loss_usd: float
    total_expected_loss_usd: float
    slippage_stress_pct: float
    slippage_stress_usd: float
    funding_loss_usd: float
    total_stress_loss_usd: float
    max_tolerated_risk_usd: float

def estimate_total_loss(
    contracts: float,
    fill_price: float,
    sl_price: float,
    taker_fee_pct: float = 0.0005,
    slippage_stress_pct: float = 0.001,
    funding_rate_hourly: float = 0.0,
    holding_hours: float = 4.0,
    approved_risk: Optional[float] = None
) -> LossEstimationResult:
    """
    Computes a comprehensive loss estimation for an open position hitting its stop-loss.
    
    1. Price Loss: contracts * |fill_price - sl_price|
    2. Fee Loss: contracts * (fill_price + sl_price) * taker_fee_pct (round-trip taker fees)
    3. Slippage Stress: contracts * sl_price * slippage_stress_pct (adverse SL trigger fill)
    4. Funding Cost: contracts * fill_price * |funding_rate_hourly| * holding_hours
    5. Total Expected Loss: Price Loss + Fee Loss
    6. Total Stress Loss: Total Expected Loss + Slippage Stress + Funding Cost
    7. Max Tolerated Risk: (approved_risk or Total Expected Loss) * 1.10 + 0.05
    """
    c = abs(float(contracts or 0.0))
    entry = abs(float(fill_price or 0.0))
    sl = abs(float(sl_price or 0.0))
    fee_rate = float(taker_fee_pct if taker_fee_pct is not None else 0.0005)
    slip_rate = float(slippage_stress_pct if slippage_stress_pct is not None else 0.001)
    funding_rate = abs(float(funding_rate_hourly if funding_rate_hourly is not None else 0.0))
    hours = max(0.0, float(holding_hours if holding_hours is not None else 4.0))

    # Base price loss
    price_loss = round(c * abs(entry - sl), 4)

    # Round-trip taker fee (entry taker + exit taker)
    fee_loss = round(c * (entry + sl) * fee_rate, 4)

    # Slippage stress on stop-loss market execution
    slippage_stress = round(c * sl * slip_rate, 4)

    # Holding funding cost
    funding_loss = round(c * entry * funding_rate * hours, 4)

    # Aggregate figures
    total_expected = round(price_loss + fee_loss, 2)
    total_stress = round(price_loss + fee_loss + slippage_stress + funding_loss, 2)

    base_budget = float(approved_risk) if approved_risk is not None and approved_risk > 0 else total_expected
    max_tolerated = round((base_budget * 1.10) + 0.05, 2)

    return LossEstimationResult(
        contracts=c,
        fill_price=entry,
        sl_price=sl,
        price_loss_usd=round(price_loss, 2),
        taker_fee_pct=fee_rate,
        fee_loss_usd=round(fee_loss, 2),
        total_expected_loss_usd=total_expected,
        slippage_stress_pct=slip_rate,
        slippage_stress_usd=round(slippage_stress, 2),
        funding_loss_usd=round(funding_loss, 2),
        total_stress_loss_usd=total_stress,
        max_tolerated_risk_usd=max_tolerated
    )

def calculate_max_tolerated_risk(approved_risk: float) -> float:
    """
    Uniform threshold formula: 10% relative tolerance + $0.05 dust floor.
    """
    risk = max(0.0, float(approved_risk or 0.0))
    return round((risk * 1.10) + 0.05, 2)
