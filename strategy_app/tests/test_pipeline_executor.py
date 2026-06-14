"""Tests for strategy_app.pipeline — config loading, DAG validation, and execution.

All tests use stub nodes so no ML models or live data are required.
"""
import pytest
from unittest.mock import MagicMock, patch

from strategy_app.pipeline.config import NodeConfig, PipelineConfig, RiskConfig, ExecutionConfig
from strategy_app.pipeline.executor import PipelineDecision, PipelineExecutor


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_snap():
    return MagicMock()


def _simple_config(start, nodes_dict: dict) -> PipelineConfig:
    """Build a PipelineConfig from a plain dict without touching YAML."""
    return PipelineConfig.from_dict({"pipeline": {"start": start}, "nodes": nodes_dict})


# ── config loading ─────────────────────────────────────────────────────────────

class TestPipelineConfigLoad:

    def test_from_dict_minimal(self):
        cfg = _simple_config("a", {
            "a": {"model": "regime_classifier", "outputs": {"x": "b"}},
            "b": {"strategy": "iron_condor"},
        })
        assert cfg.start == "a"
        assert len(cfg.nodes) == 2
        assert cfg.nodes["b"].is_terminal

    def test_from_dict_pass_fail_shorthand(self):
        cfg = _simple_config("gate", {
            "gate": {"model": "big_move_model", "threshold": 0.85, "pass": "win", "fail": "lose"},
            "win":  {"strategy": "buy_call"},
            "lose": {"action": "skip"},
        })
        assert cfg.nodes["gate"].outputs == {"pass": "win", "fail": "lose"}
        assert cfg.nodes["gate"].threshold == pytest.approx(0.85)

    def test_from_dict_direction_shorthand(self):
        cfg = _simple_config("d", {
            "d": {
                "model": "direction_model",
                "bullish": "call_node",
                "bearish": "put_node",
                "uncertain": "straddle_node",
            },
            "call_node":    {"strategy": "directional_call"},
            "put_node":     {"strategy": "directional_put"},
            "straddle_node":{"strategy": "straddle"},
        })
        assert cfg.nodes["d"].outputs == {
            "bullish": "call_node",
            "bearish": "put_node",
            "uncertain": "straddle_node",
        }

    def test_risk_defaults(self):
        cfg = _simple_config("n", {"n": {"action": "skip"}})
        assert cfg.risk.daily_loss_pct == pytest.approx(0.02)
        assert cfg.risk.max_open_positions == 2

    def test_risk_override(self):
        cfg = PipelineConfig.from_dict({
            "pipeline": {"start": "n"},
            "nodes": {"n": {"action": "skip"}},
            "risk": {"daily_loss_pct": 0.01, "stop_trading_after_losses": 5},
        })
        assert cfg.risk.daily_loss_pct == pytest.approx(0.01)
        assert cfg.risk.stop_trading_after_losses == 5

    def test_execution_defaults(self):
        cfg = _simple_config("n", {"n": {"action": "skip"}})
        assert cfg.execution.order_type == "limit"
        assert cfg.execution.slippage_check is True

    def test_missing_start_raises(self):
        with pytest.raises(ValueError, match="start"):
            PipelineConfig.from_dict({"nodes": {"n": {"action": "skip"}}})

    def test_threshold_float_coercion(self):
        cfg = _simple_config("g", {
            "g": {"model": "big_move_model", "threshold": "0.9", "pass": "t", "fail": "t"},
            "t": {"action": "skip"},
        })
        assert cfg.nodes["g"].threshold == pytest.approx(0.9)

    def test_node_is_terminal_when_no_outputs(self):
        cfg = _simple_config("t", {"t": {"strategy": "straddle"}})
        assert cfg.nodes["t"].is_terminal
        assert cfg.nodes["t"].strategy == "straddle"


# ── config validation ──────────────────────────────────────────────────────────

class TestPipelineConfigValidation:

    def test_unknown_start_raises(self):
        with pytest.raises(ValueError, match="start node"):
            PipelineConfig(
                start="missing",
                nodes={"a": NodeConfig(name="a", action="skip")},
            ).validate()

    def test_unknown_output_target_raises(self):
        with pytest.raises(ValueError, match="'b' is not defined"):
            PipelineConfig(
                start="a",
                nodes={
                    "a": NodeConfig(name="a", model="regime_classifier", outputs={"x": "b"}),
                },
            ).validate()

    def test_cycle_detection(self):
        with pytest.raises(ValueError, match="cycle"):
            PipelineConfig(
                start="a",
                nodes={
                    "a": NodeConfig(name="a", model="regime_classifier", outputs={"x": "b"}),
                    "b": NodeConfig(name="b", model="big_move_model",    outputs={"pass": "a"}),
                },
            ).validate()

    def test_self_loop_detected(self):
        with pytest.raises(ValueError, match="cycle"):
            PipelineConfig(
                start="a",
                nodes={
                    "a": NodeConfig(name="a", model="regime_classifier", outputs={"x": "a"}),
                },
            ).validate()

    def test_valid_graph_does_not_raise(self):
        cfg = _simple_config("a", {
            "a": {"model": "regime_classifier", "outputs": {"x": "b", "y": "c"}},
            "b": {"strategy": "iron_condor"},
            "c": {"action": "skip"},
        })
        cfg.validate()   # no raise


# ── executor routing ───────────────────────────────────────────────────────────

class TestPipelineExecutor:
    """Use patch to replace build_node with a factory that returns stub nodes."""

    @staticmethod
    def _make_executor(config: PipelineConfig, output_map: dict) -> PipelineExecutor:
        """Build an executor where each node name maps to a fixed output string.

        Patches build_node at the executor module level (not nodes module) because
        executor does `from .nodes import build_node` — a local binding that is
        unaffected by patching the attribute on the nodes module itself.
        """
        from strategy_app.pipeline.nodes import TerminalNode

        def stub_build(name, cfg):
            if cfg.is_terminal:
                return TerminalNode(name, cfg)
            stub = MagicMock()
            stub.node_name = name
            stub.evaluate = MagicMock(return_value=output_map.get(name, "no_route"))
            return stub

        with patch("strategy_app.pipeline.executor.build_node", side_effect=stub_build):
            return PipelineExecutor(config)

    # Basic routing ──────────────────────────────────────────────────────────

    def test_trade_path(self):
        cfg = _simple_config("regime", {
            "regime":     {"model": "regime_classifier", "outputs": {"RANGE": "seller_flow"}},
            "seller_flow":{"model": "seller_model", "high_confidence": "iron_condor", "low_confidence": "no_trade"},
            "iron_condor":{"strategy": "iron_condor"},
            "no_trade":   {"action": "skip"},
        })
        ex = self._make_executor(cfg, {"regime": "RANGE", "seller_flow": "high_confidence"})
        d = ex.run(_make_snap())
        assert d.action == "iron_condor"
        assert d.is_trade is True
        assert d.strategy == "iron_condor"
        assert ("regime", "RANGE") in d.path
        assert ("seller_flow", "high_confidence") in d.path

    def test_no_trade_path(self):
        cfg = _simple_config("regime", {
            "regime":     {"model": "regime_classifier", "outputs": {"RANGE": "seller_flow"}},
            "seller_flow":{"model": "seller_model", "high_confidence": "iron_condor", "low_confidence": "no_trade"},
            "iron_condor":{"strategy": "iron_condor"},
            "no_trade":   {"action": "skip"},
        })
        ex = self._make_executor(cfg, {"regime": "RANGE", "seller_flow": "low_confidence"})
        d = ex.run(_make_snap())
        assert d.action == "skip"
        assert d.is_trade is False
        assert d.strategy is None

    def test_low_vol_short_circuit(self):
        cfg = _simple_config("regime", {
            "regime":   {"model": "regime_classifier", "outputs": {"LOW_VOL": "no_trade", "RANGE": "seller_flow"}},
            "seller_flow": {"model": "seller_model", "high_confidence": "ic", "low_confidence": "no_trade"},
            "ic":       {"strategy": "iron_condor"},
            "no_trade": {"action": "skip"},
        })
        ex = self._make_executor(cfg, {"regime": "LOW_VOL"})
        d = ex.run(_make_snap())
        assert d.action == "skip"
        # regime short-circuits to no_trade — seller_flow never visited
        visited_nodes = [n for n, _ in d.path]
        assert "seller_flow" not in visited_nodes

    def test_direction_routes_to_straddle_on_uncertain(self):
        cfg = _simple_config("vol", {
            "vol":      {"model": "big_move_model", "pass": "dir", "fail": "no_trade"},
            "dir":      {"model": "direction_model", "bullish": "call", "bearish": "put", "uncertain": "straddle"},
            "call":     {"strategy": "directional_call"},
            "put":      {"strategy": "directional_put"},
            "straddle": {"strategy": "straddle"},
            "no_trade": {"action": "skip"},
        })
        ex = self._make_executor(cfg, {"vol": "pass", "dir": "uncertain"})
        d = ex.run(_make_snap())
        assert d.action == "straddle"
        assert d.is_trade is True

    def test_full_pipeline_yaml_loads(self):
        """Smoke-test that the default YAML parses without error."""
        import os
        yaml_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "ops", "config", "pipeline_default.yaml"
        )
        yaml_path = os.path.normpath(yaml_path)
        if not os.path.exists(yaml_path):
            pytest.skip("pipeline_default.yaml not found")
        cfg = PipelineConfig.from_yaml(yaml_path)
        assert cfg.start == "regime"
        assert "volatility" in cfg.nodes
        assert "trade_selector" in cfg.nodes
        assert "iron_condor" in cfg.nodes
        assert cfg.nodes["straddle"].strategy == "straddle"

    # Guard rails ────────────────────────────────────────────────────────────

    def test_unknown_output_key_returns_no_trade(self):
        """If a node returns an output key not in its config, stop gracefully."""
        cfg = _simple_config("a", {
            "a": {"model": "regime_classifier", "outputs": {"known": "b"}},
            "b": {"strategy": "straddle"},
        })
        ex = self._make_executor(cfg, {"a": "unknown_key"})
        d = ex.run(_make_snap())
        assert d.is_trade is False
        assert d.terminal_node == "a"

    def test_node_exception_returns_no_trade(self):
        """If a node raises during evaluate(), the executor catches and returns no_trade."""
        cfg = _simple_config("a", {
            "a": {"model": "regime_classifier", "outputs": {"x": "b"}},
            "b": {"strategy": "straddle"},
        })
        from strategy_app.pipeline.nodes import TerminalNode

        def stub_build(name, node_cfg):
            if node_cfg.is_terminal:
                return TerminalNode(name, node_cfg)
            stub = MagicMock()
            stub.node_name = name
            stub.evaluate = MagicMock(side_effect=RuntimeError("model exploded"))
            return stub

        with patch("strategy_app.pipeline.executor.build_node", side_effect=stub_build):
            ex = PipelineExecutor(cfg)
        d = ex.run(_make_snap())
        assert d.is_trade is False

    def test_max_depth_guard(self):
        """A config that (somehow) loops at runtime is stopped at _MAX_DEPTH."""
        from strategy_app.pipeline.executor import _MAX_DEPTH

        # Build a long valid chain: n0 → n1 → n2 → ... → nN → terminal
        N = _MAX_DEPTH + 5
        nodes_dict = {}
        for i in range(N):
            nodes_dict[f"n{i}"] = {"model": "regime_classifier", "outputs": {"x": f"n{i+1}"}}
        nodes_dict[f"n{N}"] = {"strategy": "iron_condor"}

        cfg = PipelineConfig(
            start="n0",
            nodes={
                name: NodeConfig(
                    name=name,
                    model=nd.get("model"),
                    outputs=nd.get("outputs", {}),
                    strategy=nd.get("strategy"),
                )
                for name, nd in nodes_dict.items()
            }
        )
        # Skip cycle validation (intentionally long chain, no cycle)
        from strategy_app.pipeline.nodes import TerminalNode

        def stub_build(name, node_cfg):
            if node_cfg.is_terminal:
                return TerminalNode(name, node_cfg)
            stub = MagicMock()
            stub.node_name = name
            stub.evaluate = MagicMock(return_value="x")
            return stub

        with patch("strategy_app.pipeline.executor.build_node", side_effect=stub_build):
            ex = PipelineExecutor(cfg)
        d = ex.run(_make_snap())
        # Must not raise and must return no_trade
        assert d.is_trade is False
        assert len(d.path) == _MAX_DEPTH

    # PipelineDecision ────────────────────────────────────────────────────────

    def test_as_dict_shape(self):
        d = PipelineDecision(
            action="iron_condor",
            path=[("regime", "RANGE"), ("seller_flow", "high_confidence")],
            terminal_node="iron_condor",
            metadata={"iv_rank": 0.45},
        )
        out = d.as_dict()
        assert out["action"] == "iron_condor"
        assert out["is_trade"] is True
        assert out["iv_rank"] == pytest.approx(0.45)
        assert out["path"][0] == {"node": "regime", "output": "RANGE"}

    def test_is_trade_false_for_skip(self):
        d = PipelineDecision(action="skip", path=[], terminal_node="no_trade")
        assert d.is_trade is False
        assert d.strategy is None

    def test_is_trade_false_for_no_trade(self):
        d = PipelineDecision(action="no_trade", path=[], terminal_node="no_trade")
        assert d.is_trade is False
