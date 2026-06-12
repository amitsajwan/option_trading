"""Premium-seller strategy (S3) — regime-switched, defined-risk credit spreads.

The honest edge (see docs/SELLER_SYSTEM_DESIGN_2026-06-12.md):
  Volatility-Risk-Premium (IV > realized) + directional timing, harvested with
  DEFINED RISK and discipline. Validated: positional (multi-day) holding ~2.8x the
  intraday P&L (T1 SIM, 209 days), edge grows more robust with hold (drop-top3 +).

Pipeline:  RegimeClassifier -> IVGate -> StructureSelector -> StrikeSelector
           -> SafeExecutor -> PositionManager -> RiskGates
This package builds the BRAIN (decide what spread to sell) first; executor + manager
are separate modules. Real money stays OFF until paper-proven over a full expiry cycle.
"""
from .brain import SellerBrain, SellerDecision, SpreadLeg
from .gateway import LegGateway, PaperLegGateway, DhanLegGateway, Fill
from .executor import SafeExecutor, OpenSpread, FilledLeg
from .manager import PositionManager, PositionStore, RiskGates

__all__ = [
    "SellerBrain", "SellerDecision", "SpreadLeg",
    "LegGateway", "PaperLegGateway", "DhanLegGateway", "Fill",
    "SafeExecutor", "OpenSpread", "FilledLeg",
    "PositionManager", "PositionStore", "RiskGates",
]
