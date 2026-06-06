"""The senses package — Layer 1 of the Intelligent Brain (board B-1.0).

A *sense* is a pure function that answers ONE question about the current bar and
returns a structured :class:`SenseVerdict` — never a bare number, always with
evidence so the decision brain can reason and we can audit. Senses run in
parallel each bar (cheap, deterministic, <1s) and feed the Layer-2 decision brain.

Design rules (binding — enforced by tests in ``test_senses_contract.py``):

1. **One job, no peeking.** A sense answers a single question and MUST NOT import
   or call another sense. Independence is what makes agreement meaningful;
   *comparing* senses is a Layer-2 job (ConflictAnalysis), not a sense's job.
2. **Always return evidence.** ``evidence`` is a dict that explains the verdict in
   enough detail to write the "why" in one sentence. This is the dataset the
   (deferred) oversight layer will learn from.
3. **Always allow abstain.** A sense that isn't sure says so via
   :meth:`SenseVerdict.abstain` (verdict ``UNCLEAR``, confidence 0.0) — it never
   guesses. Abstaining is a first-class, expected outcome.
4. **Pure + leaf.** Senses take a read-only context mapping and return a verdict.
   No I/O, no global state, no runtime deps (redis/fastapi) in this contract
   module, so offline analysis and tests can import it freely (cf. ``cost_model``).

The verdict shape is the contract the B-2.1 decision logic reads
(see ``docs/INTELLIGENT_BRAIN_B2_1_DECISION_LOGIC_SPEC.md`` §1). Each sense
publishes a known ``verdict`` domain and a known set of ``evidence`` keys; those
field names are binding across the sense (CODEX/B-1.x) and the brain (CURSOR/B-2.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


#: Canonical verdict label every sense may return when it is not sure.
UNCLEAR = "unclear"


@dataclass(frozen=True)
class SenseVerdict:
    """One sense's structured answer for one bar.

    Attributes
    ----------
    sense:
        Identifier of the producing sense (e.g. ``"move"``, ``"regime"``). Used as
        the dict key in the brain's verdict map and in the reasoning trace.
    verdict:
        A label from the sense's own domain (e.g. ``"loaded"``, ``"alive"``,
        ``"CE"``), or :data:`UNCLEAR` when abstaining.
    confidence:
        0.0–1.0. Abstain ⇒ 0.0. A low-confidence directional read is the sense's
        job to downgrade (e.g. low-confidence ``CE`` should be returned as
        ``UNKNOWN`` by the direction sense), not the brain's.
    evidence:
        Free-form dict of the values behind the verdict (the "why"). Keys are part
        of the sense's published contract.
    value:
        Optional single headline scalar (e.g. a score or ratio), when one exists.
    """

    sense: str
    verdict: str
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    value: float | None = None

    def __post_init__(self) -> None:
        if not self.sense:
            raise ValueError("SenseVerdict.sense must be a non-empty identifier")
        if not isinstance(self.verdict, str) or not self.verdict:
            raise ValueError("SenseVerdict.verdict must be a non-empty string label")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"SenseVerdict.confidence must be in [0,1], got {self.confidence!r}")
        if not isinstance(self.evidence, Mapping):
            raise ValueError("SenseVerdict.evidence must be a mapping")

    @classmethod
    def abstain(cls, sense: str, reason: str = "", **evidence: Any) -> "SenseVerdict":
        """Build the standard abstain verdict (UNCLEAR, confidence 0.0)."""
        ev: dict[str, Any] = {"reason": reason} if reason else {}
        ev.update(evidence)
        return cls(sense=sense, verdict=UNCLEAR, confidence=0.0, evidence=ev, value=None)

    @property
    def is_abstain(self) -> bool:
        return self.verdict == UNCLEAR

    def to_trace(self) -> dict[str, Any]:
        """Serialise for the per-bar reasoning trace (B-2.4 / B-2.5)."""
        return {
            "sense": self.sense,
            "verdict": self.verdict,
            "confidence": round(float(self.confidence), 4),
            "value": self.value,
            "evidence": dict(self.evidence),
        }


@runtime_checkable
class Sense(Protocol):
    """Structural contract every sense satisfies.

    A sense is any object with a stable ``name`` and an ``evaluate`` that maps a
    read-only context to a :class:`SenseVerdict`. Implementations stay pure: they
    must not mutate the context, perform I/O, or reference another sense.
    """

    name: str

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        ...


__all__ = ["UNCLEAR", "SenseVerdict", "Sense"]
