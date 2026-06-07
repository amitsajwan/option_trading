"""Contract tests for the senses package (board B-1.0).

Proves the SenseVerdict contract is usable (an example sense), enforces the
abstain rule and validation, and guards the "one job, no peeking" rule for
every sense module added later.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Mapping

import pytest

from strategy_app.senses import UNCLEAR, Sense, SenseVerdict

_SENSES_DIR = Path(__file__).resolve().parents[1] / "senses"


# ---- an example sense proves the contract is usable end-to-end ----

class _CompressionExample:
    """Toy sense: 'is the recent range tight vs baseline?' — abstains when unknown."""

    name = "compression_example"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        atr_build = context.get("atr_build")
        atr_base = context.get("atr_base")
        if not atr_build or not atr_base:
            return SenseVerdict.abstain(self.name, reason="missing atr inputs")
        ratio = atr_build / atr_base
        loaded = ratio < 0.70
        return SenseVerdict(
            sense=self.name,
            verdict="loaded" if loaded else "quiet",
            confidence=round(max(0.0, min(1.0, 1.0 - ratio)), 3),
            evidence={"ratio": ratio, "atr_build": atr_build, "atr_base": atr_base},
            value=ratio,
        )


def test_example_sense_satisfies_protocol():
    sense = _CompressionExample()
    assert isinstance(sense, Sense)


def test_example_sense_loaded_path():
    v = _CompressionExample().evaluate({"atr_build": 5.0, "atr_base": 10.0})
    assert v.sense == "compression_example"
    assert v.verdict == "loaded"
    assert 0.0 <= v.confidence <= 1.0
    assert v.evidence["ratio"] == 0.5
    assert not v.is_abstain
    assert set(v.to_trace()) == {"sense", "verdict", "confidence", "value", "evidence"}


def test_example_sense_abstains_on_missing_inputs():
    v = _CompressionExample().evaluate({})
    assert v.is_abstain
    assert v.verdict == UNCLEAR
    assert v.confidence == 0.0
    assert v.evidence["reason"] == "missing atr inputs"


# ---- abstain + validation rules ----

def test_abstain_is_zero_confidence_and_carries_evidence():
    v = SenseVerdict.abstain("move", reason="no window", bars_seen=3)
    assert v.is_abstain and v.confidence == 0.0
    assert v.evidence == {"reason": "no window", "bars_seen": 3}


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_confidence_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        SenseVerdict(sense="x", verdict="y", confidence=bad)


def test_empty_sense_or_verdict_rejected():
    with pytest.raises(ValueError):
        SenseVerdict(sense="", verdict="y", confidence=0.5)
    with pytest.raises(ValueError):
        SenseVerdict(sense="x", verdict="", confidence=0.5)


def test_verdict_is_immutable():
    v = SenseVerdict(sense="x", verdict="y", confidence=0.5)
    with pytest.raises(Exception):
        v.confidence = 0.9  # type: ignore[misc]


# ---- "one job, no peeking": no sense module imports a sibling sense ----

# Non-sense infra modules in senses/ that senses MAY import (shared feature-prep / adapters).
_INFRA_MODULES = {"context", "snapshot_adapter"}


def _sense_stems() -> set[str]:
    return {p.stem for p in _SENSES_DIR.glob("*.py")
            if p.name != "__init__.py" and p.stem not in _INFRA_MODULES}


def _imported_sibling(node, sense_stems: set[str]) -> str | None:
    """Return the sibling sense module a node imports, if any (relative or absolute)."""
    if isinstance(node, ast.ImportFrom) and node.module:
        if node.level >= 1:                                    # `from .move import X`
            stem = node.module.split(".")[0]
        elif node.module.startswith("strategy_app.senses."):   # `from strategy_app.senses.move import X`
            stem = node.module.split(".")[2]
        else:
            return None
        return stem if stem in sense_stems else None
    if isinstance(node, ast.Import):                           # `import strategy_app.senses.move`
        for alias in node.names:
            if alias.name.startswith("strategy_app.senses."):
                stem = alias.name.split(".")[2]
                if stem in sense_stems:
                    return stem
    return None


def test_no_sense_imports_another_sense():
    sense_stems = _sense_stems()
    offenders: list[str] = []
    for path in _SENSES_DIR.glob("*.py"):
        if path.name == "__init__.py" or path.stem in _INFRA_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            sibling = _imported_sibling(node, sense_stems)
            if sibling and sibling != path.stem:
                offenders.append(f"{path.name}: imports sibling sense '{sibling}'")
    assert not offenders, "senses must not import each other (no peeking): " + "; ".join(offenders)


def test_guard_would_catch_a_relative_sibling_import():
    # the guard must flag a relative sibling-sense import (not just absolute ones)
    sense_stems = {"move", "regime"}
    node = ast.parse("from .move import MoveSense").body[0]
    assert _imported_sibling(node, sense_stems) == "move"
    infra = ast.parse("from .context import compression_ratio").body[0]
    assert _imported_sibling(infra, sense_stems) is None        # context is infra, allowed
