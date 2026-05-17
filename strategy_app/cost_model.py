"""Pure-math trading cost model — leaf module, zero non-stdlib imports.

Lives in its own file so non-runtime consumers (the offline ML labeler,
notebooks, ad-hoc analysis scripts) can import it WITHOUT pulling in
strategy_app's runtime dependencies (redis, fastapi, etc.).

If you change the cost defaults, both the runtime fill simulator and the
offline labeler pick the change up automatically. That equivalence is the
whole point — see ml_pipeline_2/docs/training/OPTION_LABEL_CONTRACT.md.
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_BROKERAGE_PER_ORDER = 20.0
DEFAULT_CHARGES_BPS_PER_SIDE = 2.5
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 7.5


@dataclass(frozen=True)
class TradingCostModel:
    brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER
    charges_bps_per_side: float = DEFAULT_CHARGES_BPS_PER_SIDE
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE

    def breakdown(self, *, entry_value: float, exit_value: float) -> dict[str, float]:
        safe_entry = max(0.0, float(entry_value))
        safe_exit = max(0.0, float(exit_value))
        brokerage = 2.0 * max(0.0, float(self.brokerage_per_order))
        charges_rate = max(0.0, float(self.charges_bps_per_side)) / 10000.0
        slippage_rate = max(0.0, float(self.slippage_bps_per_side)) / 10000.0
        charges = (safe_entry + safe_exit) * charges_rate
        slippage = (safe_entry + safe_exit) * slippage_rate
        total = brokerage + charges + slippage
        return {
            "brokerage_cost_amount": brokerage,
            "charges_cost_amount": charges,
            "slippage_cost_amount": slippage,
            "total_cost_amount": total,
        }

    def to_metadata(self) -> dict[str, float]:
        return {
            "brokerage_per_order": float(self.brokerage_per_order),
            "charges_bps_per_side": float(self.charges_bps_per_side),
            "slippage_bps_per_side": float(self.slippage_bps_per_side),
        }


__all__ = [
    "TradingCostModel",
    "DEFAULT_BROKERAGE_PER_ORDER",
    "DEFAULT_CHARGES_BPS_PER_SIDE",
    "DEFAULT_SLIPPAGE_BPS_PER_SIDE",
]
