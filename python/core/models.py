from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field

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
    
    # Optional fields for kraken/nado specific data
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
