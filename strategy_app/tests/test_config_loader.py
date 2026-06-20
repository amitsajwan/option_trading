"""Tests for the single-source config loader (Phase 1).

Proves: (a) resolve() covers every registry key, (b) env_wins is a no-op over
existing env, (c) yaml_wins overwrites, (d) the shipped YAML and the registry
are in sync.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from strategy_app.config import loader
from strategy_app.config.registry import BY_YAML, REGISTRY
from strategy_app.config.loader import DEFAULT_CONFIG_PATH, load_yaml, resolve


def test_resolve_covers_every_registry_key():
    cfg = resolve()
    for key in REGISTRY:
        assert key.env_var in cfg, f"{key.env_var} missing from resolved config"


def test_bool_and_csv_formatting():
    cfg = resolve()
    # bool -> "1"/"0" (compatible with both env_bool and == "1" checks)
    assert cfg["ENTRY_VOL_GATE_ENABLED"] in ("1", "0")
    assert cfg["EXIT_POLICY_STACK_ENABLED"] in ("1", "0")
    # csv -> comma-joined, no spaces
    assert cfg["ADAPTIVE_LOTTERY_REGIMES"] == "BREAKOUT,TRENDING"


def test_env_wins_is_a_noop_over_existing_env(monkeypatch):
    # Pretend live already set a value; env_wins must NOT clobber it.
    monkeypatch.setenv("LOTTERY_HARD_STOP_PCT", "0.99")
    with patch.dict("os.environ"):  # isolate direct os.environ writes in apply_to_environ
        loader.apply_to_environ(precedence="env_wins")
        assert os.environ["LOTTERY_HARD_STOP_PCT"] == "0.99"


def test_env_wins_fills_absent_keys(monkeypatch):
    monkeypatch.delenv("LOTTERY_TIMESTOP_BARS", raising=False)
    with patch.dict("os.environ"):  # isolate direct os.environ writes in apply_to_environ
        loader.apply_to_environ(precedence="env_wins")
        assert os.environ["LOTTERY_TIMESTOP_BARS"] == "90"


def test_yaml_wins_overwrites(monkeypatch):
    monkeypatch.setenv("LOTTERY_HARD_STOP_PCT", "0.99")
    with patch.dict("os.environ"):  # isolate direct os.environ writes in apply_to_environ
        loader.apply_to_environ(precedence="yaml_wins")
        # compare as float — YAML renders 0.20 as "0.2" (same value once parsed)
        assert float(os.environ["LOTTERY_HARD_STOP_PCT"]) == 0.20


def test_invalid_precedence_raises():
    with pytest.raises(ValueError):
        loader.apply_to_environ(config={}, precedence="bogus")


def test_builtin_parser_matches_pyyaml_on_shipped_file():
    """The no-pyyaml fallback parser must resolve identically to PyYAML."""
    yaml = pytest.importorskip("yaml")
    from strategy_app.config import loader
    from strategy_app.config.registry import REGISTRY
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    a = yaml.safe_load(text)
    b = loader._parse_simple_yaml(text)

    def res(tree):
        out = {}
        for k in REGISTRY:
            found, v = loader._walk(tree, k.yaml_path)
            out[k.env_var] = k.format(v if found else k.default)
        return out

    assert res(a) == res(b)


def test_typed_value_returns_correct_types(monkeypatch):
    from strategy_app.config import typed
    monkeypatch.setenv("LOTTERY_HARD_STOP_PCT", "0.20")
    monkeypatch.setenv("LOTTERY_TIMESTOP_BARS", "90")
    monkeypatch.setenv("ENTRY_VOL_GATE_ENABLED", "1")
    monkeypatch.setenv("ADAPTIVE_LOTTERY_REGIMES", "BREAKOUT,TRENDING")
    assert typed.value("LOTTERY_HARD_STOP_PCT") == 0.20          # float
    assert typed.value("exit.lottery.timestop_bars") == 90       # int, by yaml path
    assert typed.value("ENTRY_VOL_GATE_ENABLED") is True         # bool
    assert typed.value("exit.lottery.regimes") == ["BREAKOUT", "TRENDING"]  # csv


def test_typed_view_nested_access(monkeypatch):
    from strategy_app.config import typed
    monkeypatch.setenv("LOTTERY_BIG_TARGET_PCT", "0.50")
    v = typed.view()
    assert v.exit.lottery.big_target_pct == 0.50
    assert isinstance(v.exit.lottery.timestop_bars, int)


def test_typed_value_unknown_key_raises():
    from strategy_app.config import typed
    with pytest.raises(KeyError):
        typed.value("NOPE_NOT_A_KEY")


def test_shipped_yaml_in_sync_with_registry():
    """Every registry yaml_path must exist in the shipped YAML, and vice versa."""
    assert DEFAULT_CONFIG_PATH.exists(), f"missing {DEFAULT_CONFIG_PATH}"
    tree = load_yaml()

    def walk(t, prefix=""):
        out = set()
        for k, v in t.items():
            p = f"{prefix}.{k}" if prefix else k
            out |= walk(v, p) if isinstance(v, dict) else {p}
        return out

    yaml_paths = walk(tree)
    registry_paths = set(BY_YAML.keys())
    assert yaml_paths == registry_paths, (
        f"YAML/registry drift — only in YAML: {yaml_paths - registry_paths}; "
        f"only in registry: {registry_paths - yaml_paths}"
    )
