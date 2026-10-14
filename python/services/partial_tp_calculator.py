"""
Pure Deterministic Partial Take-Profit Calculator & Plan Manager.
Phase 1 implementation: Mathematical calculation, lot sizing, and tier adaptation.
Zero network dependencies, pure algorithmic logic.
"""

import math
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional


@dataclass
class TPTier:
    tier_index: int               # 1-indexed (1, 2, 3, 4)
    fraction: float               # Nominal fraction target (e.g. 0.25, 0.50, 0.75, 1.00)
    target_price: float           # Price rounded to market price_increment
    close_contracts: float        # Exact contracts to close at this tier (aligned to size_increment)
    cumulative_fraction: float    # Cumulative nominal fraction closed after this tier
    status: str = "PENDING"       # PENDING, SUBMITTING, FILLED, PARTIALLY_FILLED, FAILED, CANCELED
    is_final: bool = False        # True if this tier liquidates all remaining volume
    action: Optional[str] = None  # e.g. "PROTECT_BREAKEVEN", "START_TRAILING", "CLOSE_ALL"
    order_digest: Optional[str] = None
    filled_contracts: float = 0.0
    fill_price: Optional[float] = None
    realized_pnl_usd: float = 0.0
    executed_at: Optional[float] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    last_attempt_time: Optional[float] = None
    requires_pnl_reconciliation: bool = False
    pnl_reconciled: bool = True

    @property
    def remaining_tier_contracts(self) -> float:
        """Returns pending contracts remaining to be filled for this specific tier."""
        return max(0.0, round(self.close_contracts - self.filled_contracts, 8))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TPTier":
        valid_fields = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class PartialTPPlan:
    symbol: str
    direction: str                # "LONG" or "SHORT"
    entry_price: float
    tp_final_price: float
    total_contracts: float
    tiers: List[TPTier]
    mode: str                     # "4_TIER", "2_TIER", "SINGLE_TIER"
    min_size: float
    size_increment: float
    price_increment: float
    remaining_contracts: float
    be_activated: bool = False
    created_at: Optional[float] = None

    def get_active_or_next_tier(self) -> Optional[TPTier]:
        """
        Returns the currently active tier to execute or resolve.
        Strict sequential invariant:
        - If any earlier tier is SUBMITTING or PARTIALLY_FILLED (with remaining >= min_size),
          it MUST be completed/resolved before any subsequent tier can be processed.
        - Unclosable dust (< min_size) on intermediate tiers is absorbed into the final tier.
        - FAILED tiers are returned for bounded retry or halting.
        - Returns None if all tiers are FILLED or no actionable tier exists.
        """
        for tier in self.tiers:
            # 0. Halted tier stops the entire ladder
            if tier.status == "HALTED":
                return None

            # 1. In-flight or crash-recovery tier
            if tier.status == "SUBMITTING":
                return tier
            
            # 2. Incomplete partial fill that still has tradable volume
            if tier.status == "PARTIALLY_FILLED":
                rem = tier.remaining_tier_contracts
                if rem >= self.min_size and rem >= self.size_increment:
                    return tier
                else:
                    # Dust remainder (< min_size) cannot be closed as a standalone order on exchange.
                    # Mark this tier as FILLED; absorb dust remainder into the final tier.
                    tier.status = "FILLED"
                    final_tier = next((t for t in self.tiers if t.is_final), None)
                    if final_tier and final_tier != tier:
                        final_tier.close_contracts = round(final_tier.close_contracts + rem, 8)
                    continue
            
            # 3. Next pending tier (strictly sequential)
            if tier.status == "PENDING":
                return tier
                
            # 4. Failed tier (caller evaluates retry policy vs exhaustion)
            if tier.status == "FAILED":
                return tier

        return None

    def get_next_pending_tier(self) -> Optional[TPTier]:
        """Backward-compatible alias pointing to get_active_or_next_tier."""
        return self.get_active_or_next_tier()

    def get_tier(self, tier_index: int) -> Optional[TPTier]:
        """Returns tier by its 1-indexed number."""
        for tier in self.tiers:
            if tier.tier_index == tier_index:
                return tier
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "tp_final_price": self.tp_final_price,
            "total_contracts": self.total_contracts,
            "tiers": [t.to_dict() for t in self.tiers],
            "mode": self.mode,
            "min_size": self.min_size,
            "size_increment": self.size_increment,
            "price_increment": self.price_increment,
            "remaining_contracts": self.remaining_contracts,
            "be_activated": self.be_activated,
            "created_at": self.created_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PartialTPPlan":
        tiers = [TPTier.from_dict(t) for t in data.get("tiers", [])]
        return cls(
            symbol=data["symbol"],
            direction=data["direction"],
            entry_price=float(data["entry_price"]),
            tp_final_price=float(data["tp_final_price"]),
            total_contracts=float(data["total_contracts"]),
            tiers=tiers,
            mode=data.get("mode", "SINGLE_TIER"),
            min_size=float(data.get("min_size", 0.0)),
            size_increment=float(data.get("size_increment", 0.0)),
            price_increment=float(data.get("price_increment", 0.0)),
            remaining_contracts=float(data.get("remaining_contracts", data["total_contracts"])),
            be_activated=bool(data.get("be_activated", False)),
            created_at=data.get("created_at")
        )


class PartialTPCalculator:
    """
    Algorithmic calculator for multi-tier partial take-profit plans.
    Guarantees:
    - Accurate pricing strictly based on distance fractions.
    - Sizing strictly floored to size_increment (never inflates risk).
    - Dynamic adaptive ladder (4-tier -> 2-tier -> single-tier) based on product min_size.
    - Zero dust: the final tier always closes the exact remaining balance.
    """

    @staticmethod
    def round_to_tick(price: float, tick_size: float) -> float:
        """Rounds price to the nearest tick_size with clean floating precision."""
        if tick_size <= 0:
            return round(price, 6)
        
        ticks = round(price / tick_size)
        rounded = ticks * tick_size
        
        # Determine decimal places of tick_size for clean float representation
        tick_str = f"{tick_size:.10f}".rstrip("0")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, min(decimals, 8))

    @staticmethod
    def ceil_to_tick(price: float, tick_size: float) -> float:
        """Rounds price UP to next tick away from entry (for LONG TP)."""
        if tick_size <= 0:
            return round(price, 6)
        ticks = math.ceil(round(price / tick_size, 8))
        rounded = ticks * tick_size
        tick_str = f"{tick_size:.10f}".rstrip("0")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, min(decimals, 8))

    @staticmethod
    def floor_to_tick(price: float, tick_size: float) -> float:
        """Rounds price DOWN to next tick away from entry (for SHORT TP)."""
        if tick_size <= 0:
            return round(price, 6)
        ticks = math.floor(round(price / tick_size, 8))
        rounded = ticks * tick_size
        tick_str = f"{tick_size:.10f}".rstrip("0")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, min(decimals, 8))

    @staticmethod
    def floor_to_step(amount: float, step_size: float) -> float:
        """Floors amount down to the nearest step_size to never exceed allowed risk."""
        if step_size <= 0:
            return round(amount, 6)
        
        steps = math.floor(round(amount / step_size, 8))
        floored = steps * step_size
        
        step_str = f"{step_size:.10f}".rstrip("0")
        decimals = len(step_str.split(".")[1]) if "." in step_str else 0
        return round(floored, min(decimals, 8))

    @classmethod
    def calculate_plan(
        cls,
        symbol: str,
        direction: str,
        entry_price: float,
        tp_final_price: float,
        total_contracts: float,
        market_params: Dict[str, Any],
        created_at: Optional[float] = None
    ) -> PartialTPPlan:
        """
        Builds a comprehensive PartialTPPlan based on entry, final TP, and exchange product parameters.
        """
        dir_clean = str(direction).strip().upper()
        if dir_clean not in ("LONG", "SHORT"):
            raise ValueError(f"Invalid direction '{direction}'. Must be LONG or SHORT.")

        if entry_price <= 0 or tp_final_price <= 0 or total_contracts <= 0:
            raise ValueError("entry_price, tp_final_price, and total_contracts must all be > 0.")

        if dir_clean == "LONG" and tp_final_price <= entry_price:
            raise ValueError(f"For LONG, tp_final_price ({tp_final_price}) must be > entry_price ({entry_price}).")
        elif dir_clean == "SHORT" and tp_final_price >= entry_price:
            raise ValueError(f"For SHORT, tp_final_price ({tp_final_price}) must be < entry_price ({entry_price}).")

        # Extract market parameters
        size_inc = float(
            market_params.get("size_increment")
            or market_params.get("size_increment_base")
            or 0.0
        )
        if size_inc <= 0 and market_params.get("size_increment_x18"):
            try:
                size_inc = float(market_params["size_increment_x18"]) / 1e18
            except (ValueError, TypeError):
                size_inc = 0.001
        if size_inc <= 0:
            size_inc = 0.001

        price_inc = float(
            market_params.get("price_increment")
            or 0.0
        )
        if price_inc <= 0 and market_params.get("price_increment_x18"):
            try:
                price_inc = float(market_params["price_increment_x18"]) / 1e18
            except (ValueError, TypeError):
                price_inc = 0.01
        if price_inc <= 0:
            price_inc = 0.01

        min_size = float(market_params.get("min_size") or 0.0)
        if min_size <= 0:
            min_size = size_inc

        # Total distance
        delta_tp = abs(tp_final_price - entry_price)

        # Helper to compute price at fraction f (Directed Rounding ensures targets never move closer to entry)
        def get_price_at(fraction: float) -> float:
            if dir_clean == "LONG":
                raw_price = entry_price + (delta_tp * fraction)
                return cls.ceil_to_tick(raw_price, price_inc)
            else:
                raw_price = entry_price - (delta_tp * fraction)
                return cls.floor_to_tick(raw_price, price_inc)

        # Total contracts rounded down to step size
        total_contracts = cls.floor_to_step(total_contracts, size_inc)
        if total_contracts <= 0:
            raise ValueError(f"Total contracts ({total_contracts}) is zero after size_increment alignment.")

        # --- TIER SIZING & ADAPTATION LOGIC ---
        # 1. Test 4-tier candidate
        # Intermediate tiers close floor(0.25 * total_contracts)
        q4 = cls.floor_to_step(0.25 * total_contracts, size_inc)
        rem4 = cls.floor_to_step(total_contracts - (3 * q4), size_inc)

        can_do_4_tier = (
            q4 >= min_size and
            rem4 >= min_size and
            q4 > 0 and
            rem4 > 0
        )

        if can_do_4_tier:
            tiers = [
                TPTier(
                    tier_index=1,
                    fraction=0.25,
                    target_price=get_price_at(0.25),
                    close_contracts=q4,
                    cumulative_fraction=0.25,
                    is_final=False,
                    action=None
                ),
                TPTier(
                    tier_index=2,
                    fraction=0.50,
                    target_price=get_price_at(0.50),
                    close_contracts=q4,
                    cumulative_fraction=0.50,
                    is_final=False,
                    action="PROTECT_BREAKEVEN"
                ),
                TPTier(
                    tier_index=3,
                    fraction=0.75,
                    target_price=get_price_at(0.75),
                    close_contracts=q4,
                    cumulative_fraction=0.75,
                    is_final=False,
                    action="START_TRAILING"
                ),
                TPTier(
                    tier_index=4,
                    fraction=1.00,
                    target_price=cls.round_to_tick(tp_final_price, price_inc),
                    close_contracts=rem4,
                    cumulative_fraction=1.00,
                    is_final=True,
                    action="CLOSE_ALL"
                )
            ]
            mode = "4_TIER"

        else:
            # 2. Test 2-tier candidate
            q2 = cls.floor_to_step(0.50 * total_contracts, size_inc)
            rem2 = cls.floor_to_step(total_contracts - q2, size_inc)

            can_do_2_tier = (
                q2 >= min_size and
                rem2 >= min_size and
                q2 > 0 and
                rem2 > 0
            )

            if can_do_2_tier:
                tiers = [
                    TPTier(
                        tier_index=1,
                        fraction=0.50,
                        target_price=get_price_at(0.50),
                        close_contracts=q2,
                        cumulative_fraction=0.50,
                        is_final=False,
                        action="PROTECT_BREAKEVEN"
                    ),
                    TPTier(
                        tier_index=2,
                        fraction=1.00,
                        target_price=cls.round_to_tick(tp_final_price, price_inc),
                        close_contracts=rem2,
                        cumulative_fraction=1.00,
                        is_final=True,
                        action="CLOSE_ALL"
                    )
                ]
                mode = "2_TIER"

            else:
                # 3. Fallback: SINGLE_TIER (100% full TP)
                tiers = [
                    TPTier(
                        tier_index=1,
                        fraction=1.00,
                        target_price=cls.round_to_tick(tp_final_price, price_inc),
                        close_contracts=total_contracts,
                        cumulative_fraction=1.00,
                        is_final=True,
                        action="CLOSE_ALL"
                    )
                ]
                mode = "SINGLE_TIER"

        return PartialTPPlan(
            symbol=symbol,
            direction=dir_clean,
            entry_price=entry_price,
            tp_final_price=tp_final_price,
            total_contracts=total_contracts,
            tiers=tiers,
            mode=mode,
            min_size=min_size,
            size_increment=size_inc,
            price_increment=price_inc,
            remaining_contracts=total_contracts,
            created_at=created_at
        )
