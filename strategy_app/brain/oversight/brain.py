"""OVERSIGHT BRAIN — ties Sense + Memory + Calculator + Reasoner into one cycle.

Run pre-open and every ~30 min with the latest snapshot. Each ``cycle()``:
  1. SENSE     build :class:`MarketFacts` from the snapshot (+ optional FII/events)
  2. MEMORY    load the day's :class:`Scratchpad`
  3. REASON    LLM forms/updates posture + lean + risk flag (risk-reducing only)
  4. WRITE     emit the risk-reducing variables to a state file the engine reads
  5. LOG       append to the scratchpad + a per-cycle JSONL for later scoring

OFF by default (``BRAIN_OVERSIGHT_ENABLED=false``). Never raises. When disabled or
unkeyed, emits a neutral verdict (no veto, normal risk) — a strict no-op.

The engine consumes only ``oversight_state.json`` (the risk-reducing variables);
reading it per-bar is a cheap file/dict lookup, never an LLM call.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .facts import MarketFacts
from .reasoner import OversightVerdict, reason, rule_reason
from .scratchpad import Scratchpad
from .verify import verify_verdict

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _run_dir() -> Path:
    return Path(
        os.getenv("STRATEGY_RUNTIME_ARTIFACT_DIR")
        or os.getenv("STRATEGY_RUN_DIR")
        or ".run/strategy_app"
    )


class OversightBrain:
    """Periodic, risk-reducing oversight reasoning. Engine-decoupled via a state file."""

    def __init__(self, run_dir: Optional[Path] = None) -> None:
        self._enabled = _env_bool("BRAIN_OVERSIGHT_ENABLED", False)
        # reuse the LLM creds/config from the morning-posture provider
        self._api_key = os.getenv("BRAIN_LLM_API_KEY", "").strip()
        self._base_url = os.getenv("BRAIN_LLM_BASE_URL", "").strip() or _DEFAULT_BASE_URL
        self._model = os.getenv("BRAIN_LLM_MODEL", "").strip() or _DEFAULT_MODEL
        self._run_dir = run_dir or _run_dir()

    def _scratch_path(self, trade_date: str) -> Path:
        return self._run_dir / f"oversight_scratchpad_{trade_date}.json"

    def _state_path(self) -> Path:
        return self._run_dir / "oversight_state.json"

    def _log_path(self, trade_date: str) -> Path:
        return self._run_dir / f"oversight_cycles_{trade_date}.jsonl"

    def cycle(
        self,
        snapshot: Any,
        *,
        prior_fii_cr: Any = None,
        events: Optional[list] = None,
    ) -> OversightVerdict:
        """Run one oversight cycle. Returns the verdict; writes the risk state file."""
        try:
            facts = MarketFacts.from_snapshot(snapshot, prior_fii_cr=prior_fii_cr, events=events)
        except Exception as exc:  # never break the caller
            logger.warning("oversight: facts build failed: %s", exc)
            return OversightVerdict()

        trade_date = facts.trade_date or date.today().isoformat()
        scratch = Scratchpad.load_or_new(self._scratch_path(trade_date), trade_date)

        mode = os.getenv("BRAIN_OVERSIGHT_MODE", "llm").strip().lower()
        if self._enabled and mode == "rule":
            verdict = rule_reason(facts.to_prompt_dict())          # deterministic, no key
        elif self._enabled and self._api_key:
            verdict = reason(
                facts.to_prompt_dict(),
                scratch.to_prompt_context(),
                api_key=self._api_key,
                base_url=self._base_url,
                model=self._model,
            )
        else:
            verdict = OversightVerdict()  # disabled / unkeyed → strict no-op (neutral)

        # VERIFY — catch hallucinations: downgrade any verdict that contradicts the
        # ground-truth facts before it can influence anything.
        compact = facts.to_prompt_dict()
        verdict, _hallucination_flags = verify_verdict(verdict, compact)
        if _hallucination_flags:
            logger.warning("oversight hallucination flags=%s", _hallucination_flags)
        scratch.add(time=facts.timestamp[11:16] or "", verdict=verdict, facts=compact)
        scratch.persist(self._scratch_path(trade_date))
        self._write_state(verdict)
        self._log_cycle(trade_date, facts, verdict)
        logger.info(
            "oversight cycle date=%s zone=%s posture=%s lean=%s(%.2f) risk=%s",
            trade_date, facts.location_zone, verdict.posture,
            verdict.direction_lean, verdict.lean_confidence, verdict.risk_flag,
        )
        return verdict

    # ── outputs ──────────────────────────────────────────────────────────────
    def _write_state(self, verdict: OversightVerdict) -> None:
        """Write the risk-reducing variables the engine reads next ~30 min."""
        try:
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(verdict.to_risk_state()), encoding="utf-8")
        except Exception as exc:
            logger.warning("oversight: state write failed: %s", exc)

    def _log_cycle(self, trade_date: str, facts: MarketFacts, verdict: OversightVerdict) -> None:
        try:
            rec = {
                "trade_date": trade_date,
                "time": facts.timestamp,
                "facts": facts.to_prompt_dict(),
                "verdict": {**asdict(verdict_to_plain(verdict))},
            }
            p = self._log_path(trade_date)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except Exception as exc:
            logger.warning("oversight: cycle log failed: %s", exc)

    # ── engine-side reader (cheap, per-bar safe) ─────────────────────────────
    @staticmethod
    def read_risk_state(run_dir: Optional[Path] = None) -> dict[str, Any]:
        """Read the current risk-reducing variables (engine calls this; no LLM)."""
        p = (run_dir or _run_dir()) / "oversight_state.json"
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except Exception:
            pass
        return {"oversight_risk_flag": "normal", "oversight_veto_side": ""}


def verdict_to_plain(v: OversightVerdict) -> OversightVerdict:
    """key_levels tuple → list so asdict() is JSON-clean."""
    from dataclasses import replace
    return replace(v, key_levels=list(v.key_levels))


__all__ = ["OversightBrain"]
