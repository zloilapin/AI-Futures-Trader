from typing import List, Dict, Any, Optional, Literal, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
import copy
from pydantic import BaseModel, Field

def _make_deep_immutable(val: Any) -> Any:
    """Recursively converts mappings and collections into strictly immutable structures."""
    if isinstance(val, (dict, Mapping)):
        return MappingProxyType({k: _make_deep_immutable(v) for k, v in val.items()})
    elif isinstance(val, (list, tuple)):
        return tuple(_make_deep_immutable(v) for v in val)
    return val

def _make_mutable(val: Any) -> Any:
    """Recursively converts immutable structures back into standard python dicts and lists."""
    if isinstance(val, (dict, MappingProxyType, Mapping)):
        return {k: _make_mutable(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple)):
        return [_make_mutable(v) for v in val]
    return val

@dataclass(frozen=True)
class ExecutionResult:
    """
    Decoupled representation of order execution outcome on the exchange.
    Ensures that execution state is accurately preserved without mutating FinalRiskDecision.
    """
    symbol: str
    status: str                     # "SUCCESS" | "REJECTED_BY_EXCHANGE" | "SKIPPED"
    order_id: Optional[str] = None
    executed_at: float = 0.0
    error: Optional[str] = None
    notional_usd: float = 0.0
    actual_fill_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "order_id": self.order_id,
            "executed_at": self.executed_at,
            "error": self.error,
            "notional_usd": self.notional_usd,
            "actual_fill_price": self.actual_fill_price,
        }

@dataclass(frozen=True)
class FinalTradeDecision:
    """
    Immutable representation of the final trade decision produced by Deterministic Guard.
    Once evaluated, no component may mutate 'decision'.
    """
    symbol: str
    decision: str                   # "LONG" | "SHORT" | "HOLD"
    conviction: int                 # 0 to 100
    trade_action: str               # "ENTER" | "WAIT_FOR_PULLBACK" | "HOLD" | "REDUCE_SIZE"
    strategy_mode: str              # "TREND_FOLLOWING", "BREAKOUT", "MEAN_REVERSION", etc.
    direction_bias: str             # "LONG" | "SHORT" | "NEUTRAL"
    min_conviction: int             # Profile / mode threshold
    is_actionable: bool             # True only if candidate passed all deterministic guards
    guard_status: str               # "PASSED" | "BLOCKED" | "HOLD"
    guard_reason: str               # Human-readable rationale
    rejection_tag: Optional[str] = None # e.g. "STRATEGY_GUARD_VETO", "LOW_CONFIDENCE", "FUNDING_VETO", "WAIT_FOR_PULLBACK", "CEO_HOLD"
    directional_confidence: int = 0
    entry_quality: int = 0
    reasoning_en: str = ""
    reasoning_ru: str = ""
    raw_ceo_verdict: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Deep defensive conversion of raw_ceo_verdict to guarantee nested immutability
        if self.raw_ceo_verdict:
            object.__setattr__(self, "raw_ceo_verdict", _make_deep_immutable(self.raw_ceo_verdict))

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        if self.raw_ceo_verdict and key in self.raw_ceo_verdict:
            return self.raw_ceo_verdict[key]
        return default

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            if val is not None:
                return val
        if self.raw_ceo_verdict and key in self.raw_ceo_verdict:
            return self.raw_ceo_verdict[key]
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key) or (bool(self.raw_ceo_verdict) and key in self.raw_ceo_verdict)

    def to_dict(self) -> Dict[str, Any]:
        d = _make_mutable(self.raw_ceo_verdict) if self.raw_ceo_verdict else {}
        d.update({
            "symbol": self.symbol,
            "decision": self.decision,
            "conviction": self.conviction,
            "trade_action": self.trade_action,
            "strategy_mode": self.strategy_mode,
            "direction_bias": self.direction_bias,
            "min_conviction": self.min_conviction,
            "is_actionable": self.is_actionable,
            "guard_status": self.guard_status,
            "guard_reason": self.guard_reason,
            "rejection_tag": self.rejection_tag,
            "hold_category": self.rejection_tag if self.decision == "HOLD" else None,
            "directional_confidence": self.directional_confidence,
            "entry_quality": self.entry_quality,
            "reasoning_en": self.reasoning_en,
            "reasoning_ru": self.reasoning_ru,
        })
        return d

    def __iter__(self):
        """Allows unpacking as (decision, conviction, trade_action, min_conv) for backward compatibility."""
        return iter((self.decision, self.conviction, self.trade_action, self.min_conviction))


@dataclass(frozen=True)
class FinalRiskDecision:
    """
    Immutable representation of the final risk decision produced by RiskManager.
    RiskManager can ONLY APPROVE (approved=True) or VETO (approved=False).
    It never mutates 'decision'.
    """
    approved: bool
    veto_category: Optional[str] = None
    reasoning: str = ""
    trade_action: str = "ENTER"
    risk_amount_usd: float = 0.0
    notional_size_usd: float = 0.0
    margin_usd: float = 0.0
    leverage: float = 1.0
    effective_leverage: float = 1.0
    contracts: float = 0.0
    margin_pct: float = 0.0
    entry_price: float = 0.0
    take_profit_price: float = 0.0
    take_profit_pct: float = 0.0
    stop_loss_price: float = 0.0
    stop_loss_pct: float = 0.0
    risk_reward_ratio: float = 0.0
    liquidation_price: float = 0.0
    position_size_pct: float = 0.0
    execution_status: Optional[str] = None
    pending_trade_id: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        return default

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "veto_category": self.veto_category,
            "reasoning": self.reasoning,
            "trade_action": self.trade_action,
            "risk_amount_usd": self.risk_amount_usd,
            "notional_size_usd": self.notional_size_usd,
            "margin_usd": self.margin_usd,
            "leverage": self.leverage,
            "effective_leverage": self.effective_leverage,
            "contracts": self.contracts,
            "margin_pct": self.margin_pct,
            "entry_price": self.entry_price,
            "take_profit_price": self.take_profit_price,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_price": self.stop_loss_price,
            "stop_loss_pct": self.stop_loss_pct,
            "risk_reward_ratio": self.risk_reward_ratio,
            "liquidation_price": self.liquidation_price,
            "position_size_pct": self.position_size_pct,
            "execution_status": self.execution_status,
            "pending_trade_id": self.pending_trade_id,
        }

class ScoreBreakdown(BaseModel):
    scanner: Optional[float] = None
    candle: Optional[float] = None
    orderbook: Optional[float] = None
    oi_funding: Optional[float] = None
    news: Optional[float] = None
    indicator: Optional[float] = None

class CeoVerdict(BaseModel):
    decision: Literal["LONG", "SHORT", "HOLD"]
    conviction: int = Field(ge=0, le=100)
    reasoning_en: str
    score_breakdown: ScoreBreakdown

class RiskVerdict(BaseModel):
    approved: bool
    notional_size_usd: float = Field(ge=0)
    take_profit_price: float
    stop_loss_price: float
    risk_reward_ratio: float
    reason: str = ""

class Position(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_price: float
    size_usd: float
    tp_price: float
    sl_price: float
    leverage: int
    open_time: float
    
    # Optional fields for nado specific data
    order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    product_id: Optional[int] = None
    highest_pnl_pct: float = 0.0

class ClosedTrade(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_price: float
    close_price: float
    size_usd: float
    leverage: int
    pnl_usd: float
    pnl_pct: float
    open_time: float
    close_time: float
    duration_min: float
    reason: str
