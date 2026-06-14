"""Configurable decision-DAG pipeline.

Load a YAML config and run it per bar::

    from strategy_app.pipeline import PipelineConfig, PipelineExecutor

    cfg = PipelineConfig.from_yaml("ops/config/pipeline_default.yaml")
    executor = PipelineExecutor(cfg)
    decision = executor.run(snap)
    if decision.is_trade:
        print(decision.action)   # "iron_condor" | "buy_call" | "buy_put" | "straddle"
"""
from .config import ExecutionConfig, NodeConfig, PipelineConfig, RiskConfig
from .executor import PipelineDecision, PipelineExecutor
from .nodes import (
    BigMoveNode,
    DirectionNode,
    PipelineNode,
    RegimeNode,
    SellerNode,
    TerminalNode,
    build_node,
)

__all__ = [
    "PipelineConfig",
    "NodeConfig",
    "RiskConfig",
    "ExecutionConfig",
    "PipelineDecision",
    "PipelineExecutor",
    "PipelineNode",
    "RegimeNode",
    "BigMoveNode",
    "DirectionNode",
    "SellerNode",
    "TerminalNode",
    "build_node",
]
