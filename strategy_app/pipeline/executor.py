"""Pipeline executor — traverses the decision DAG per bar.

Usage::

    cfg = PipelineConfig.from_yaml("ops/config/pipeline_default.yaml")
    executor = PipelineExecutor(cfg)

    # per bar:
    decision = executor.run(snap)
    if decision.is_trade:
        print(decision.action)   # "iron_condor" | "buy_call" | "buy_put" | "straddle"
        print(decision.path)     # [("regime","RANGE"), ("seller_flow","high_confidence"), ("iron_condor","iron_condor")]

The executor is stateless per-call — all state (consecutive losses, daily P&L)
is the caller's responsibility. The risk config is available on PipelineDecision
so the caller can enforce it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..market.snapshot_accessor import SnapshotAccessor
from .config import PipelineConfig
from .nodes import PipelineNode, build_node

logger = logging.getLogger(__name__)

_MAX_DEPTH = 20   # guard against config cycles the validator missed


# ── result ────────────────────────────────────────────────────────────────────

@dataclass
class PipelineDecision:
    """The result of one pipeline traversal."""
    action: str                              # terminal action: strategy or "no_trade"/"skip"
    path: List[Tuple[str, str]]              # [(node_name, output_key), ...]
    terminal_node: str                       # name of the node that ended the traversal
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        """True when the action is a real trade structure (not skip/no_trade)."""
        return self.action not in ("no_trade", "skip", "")

    @property
    def strategy(self) -> Optional[str]:
        """The trade structure if is_trade, else None."""
        return self.action if self.is_trade else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "is_trade": self.is_trade,
            "path": [{"node": n, "output": o} for n, o in self.path],
            "terminal_node": self.terminal_node,
            **self.metadata,
        }


# ── executor ──────────────────────────────────────────────────────────────────

class PipelineExecutor:
    """Traverses the config DAG from start → terminal, evaluating nodes per bar."""

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._nodes: Dict[str, PipelineNode] = {}
        self._build()

    def _build(self) -> None:
        """Instantiate all nodes once at startup (model loading happens here)."""
        for name, node_cfg in self._config.nodes.items():
            try:
                self._nodes[name] = build_node(name, node_cfg)
                logger.info("pipeline: built node '%s' model=%s", name, node_cfg.model or "terminal")
            except Exception:
                logger.exception("pipeline: failed to build node '%s' — it will always return no_trade", name)

    def run(
        self,
        snap: SnapshotAccessor,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PipelineDecision:
        """Traverse the DAG for one bar. Never raises."""
        path: List[Tuple[str, str]] = []
        node_name = self._config.start
        meta = dict(metadata or {})

        for depth in range(_MAX_DEPTH):
            node_cfg = self._config.nodes.get(node_name)
            if node_cfg is None:
                logger.warning("pipeline: unknown node '%s' at depth %d — stopping", node_name, depth)
                return _no_trade(node_name, path, meta)

            node = self._nodes.get(node_name)
            if node is None:
                # Node failed to build at startup — log and abort
                return _no_trade(node_name, path, meta)

            try:
                output_key = node.evaluate(snap)
            except Exception:
                logger.exception("pipeline: node '%s' raised during evaluate — routing to no_trade", node_name)
                return _no_trade(node_name, path, meta)

            path.append((node_name, output_key))
            logger.debug("pipeline: %s → %s", node_name, output_key)

            # Terminal node: no outputs defined
            if node_cfg.is_terminal:
                action = node_cfg.strategy or node_cfg.action or output_key or "no_trade"
                return PipelineDecision(action=action, path=path, terminal_node=node_name, metadata=meta)

            # Route to next node
            next_node = node_cfg.outputs.get(output_key)
            if next_node is None:
                logger.debug(
                    "pipeline: node '%s' output '%s' has no route — stopping at no_trade",
                    node_name, output_key,
                )
                return _no_trade(node_name, path, meta)

            node_name = next_node

        logger.warning("pipeline: max depth %d reached — stopping", _MAX_DEPTH)
        return _no_trade(node_name, path, meta)


def _no_trade(node: str, path: list, meta: dict) -> PipelineDecision:
    return PipelineDecision(action="no_trade", path=path, terminal_node=node, metadata=meta)
