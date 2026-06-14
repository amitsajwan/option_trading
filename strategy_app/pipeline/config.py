"""Pipeline config — load and validate a YAML decision-DAG.

Schema mirrors the user-facing pipeline YAML exactly so what you write
is what runs. Each node is either a *model node* (evaluates a model and
routes by the output key) or a *terminal node* (no outputs → final action).

Usage::

    cfg = PipelineConfig.from_yaml("ops/config/pipeline_default.yaml")
    cfg.validate()          # raises ValueError on bad graph
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class NodeConfig:
    name: str
    model: Optional[str] = None        # registry key: "regime_classifier" | "big_move_model" | ...
    threshold: Optional[float] = None  # model-specific gate
    outputs: Dict[str, str] = field(default_factory=dict)  # output_key → next_node_name
    strategy: Optional[str] = None     # terminal: "iron_condor" | "buy_call" | ...
    action: Optional[str] = None       # terminal: "skip"
    params: Dict[str, Any] = field(default_factory=dict)   # node-specific overrides

    @property
    def is_terminal(self) -> bool:
        return not self.outputs


@dataclass
class RiskConfig:
    daily_loss_pct: float = 0.02
    max_position_pct: float = 0.05
    max_open_positions: int = 2
    stop_trading_after_losses: int = 3


@dataclass
class ExecutionConfig:
    order_type: str = "limit"
    slippage_check: bool = True
    max_slippage_pct: float = 0.002


@dataclass
class PipelineConfig:
    start: str
    nodes: Dict[str, NodeConfig]
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    # ── factories ─────────────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        try:
            import yaml
        except ImportError as e:
            raise ImportError("PyYAML is required for pipeline config: pip install pyyaml") from e
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineConfig":
        pipeline_block = d.get("pipeline") or {}
        start = str(pipeline_block.get("start") or d.get("start") or "")
        if not start:
            raise ValueError("pipeline config must specify 'pipeline.start'")

        raw_nodes: dict = d.get("nodes") or {}
        nodes: Dict[str, NodeConfig] = {}
        for name, cfg in raw_nodes.items():
            cfg = cfg or {}
            # Collect outputs: start from explicit 'outputs' dict, then layer shorthand keys
            outputs: Dict[str, str] = dict(cfg.get("outputs") or {})
            # pass/fail shorthand (big_move_model, etc.)
            for key in ("pass", "fail"):
                if isinstance(cfg.get(key), str):
                    outputs[key] = cfg[key]
            # direction shorthand
            for key in ("bullish", "bearish", "uncertain", "high_confidence", "low_confidence"):
                if isinstance(cfg.get(key), str):
                    outputs[key] = cfg[key]
            # regime-style uppercase output keys already handled by explicit 'outputs' dict above

            nodes[name] = NodeConfig(
                name=name,
                model=cfg.get("model"),
                threshold=float(cfg["threshold"]) if cfg.get("threshold") is not None else None,
                outputs=outputs,
                strategy=cfg.get("strategy"),
                action=cfg.get("action"),
                params=dict(cfg.get("params") or {}),
            )

        risk_raw = d.get("risk") or {}
        risk = RiskConfig(
            daily_loss_pct=float(risk_raw.get("daily_loss_pct", 0.02)),
            max_position_pct=float(risk_raw.get("max_position_pct", 0.05)),
            max_open_positions=int(risk_raw.get("max_open_positions", 2)),
            stop_trading_after_losses=int(risk_raw.get("stop_trading_after_losses", 3)),
        )
        exec_raw = d.get("execution") or {}
        execution = ExecutionConfig(
            order_type=str(exec_raw.get("order_type", "limit")),
            slippage_check=bool(exec_raw.get("slippage_check", True)),
            max_slippage_pct=float(exec_raw.get("max_slippage_pct", 0.002)),
        )

        cfg_obj = cls(start=start, nodes=nodes, risk=risk, execution=execution)
        cfg_obj.validate()
        return cfg_obj

    # ── validation ────────────────────────────────────────────────────────────

    def validate(self) -> None:
        """Raise ValueError if the graph is invalid."""
        if self.start not in self.nodes:
            raise ValueError(f"start node '{self.start}' not defined in nodes")

        for node_name, node in self.nodes.items():
            for out_key, target in node.outputs.items():
                if target not in self.nodes:
                    raise ValueError(
                        f"node '{node_name}' output '{out_key}' → '{target}' is not defined"
                    )

        # Detect cycles via DFS
        visited: set = set()
        in_stack: set = set()

        def dfs(name: str) -> None:
            if name in in_stack:
                raise ValueError(f"cycle detected at node '{name}'")
            if name in visited:
                return
            visited.add(name)
            in_stack.add(name)
            for target in self.nodes[name].outputs.values():
                dfs(target)
            in_stack.discard(name)

        dfs(self.start)
