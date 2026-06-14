"""DIRECTION SHADOW — log what the LLM *would* have picked, change nothing.

Wired at the live entry-direction decision (ml/entry_direction_resolver.resolve_entry_direction).
When DIRECTION_SHADOW_ENABLED=1 it, for each entry:
  1. snapshots the cheap context synchronously (facts + taken side + fut price + ts),
  2. fires the LLM direction call on a DAEMON THREAD (off the trade hot path — the order
     is already decided and unaffected),
  3. appends one JSONL line: taken side, LLM pick, confidence, reason, grounded flag,
     composite scores, entry fut price (for later truth reconciliation).

Discipline:
- SHADOW ONLY: never influences the returned direction or the order. Pure measurement.
- Never raises: every failure is swallowed; trading is never impacted.
- Provider = Groq by default (fast, ~1 call/trade); grounding = Gemini web cache (optional).

Reconcile truth later with tools/reconcile_direction_shadow.py (compares the LLM/taken
side to the realised 15-min futures move from snapshots) — offline, no live coupling.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .direction_advisor import ask_direction, build_facts_from_accessor, resolve_provider
from .gemini_grounding import GeminiGrounding

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _run_dir() -> Path:
    return Path(os.getenv("STRATEGY_RUN_DIR", ".run/strategy_app"))


class DirectionShadow:
    """Best-effort shadow logger for the LLM direction advisor."""

    def __init__(self, *, provider: str = "", model: str = "", api_key: str = "",
                 grounding: Optional[GeminiGrounding] = None,
                 out_path: Optional[Path] = None, enabled: Optional[bool] = None) -> None:
        self._enabled = _env_bool("DIRECTION_SHADOW_ENABLED", False) if enabled is None else enabled
        prov = provider or os.getenv("DIRECTION_LLM_PROVIDER", "groq").strip() or "groq"
        try:
            base_url, default_model, key_envs = resolve_provider(prov)
        except ValueError:
            self._enabled = False
            base_url, default_model, key_envs = "", "", ()
        self._provider = prov
        self._base_url = base_url
        self._model = model or os.getenv("DIRECTION_LLM_MODEL", "").strip() or default_model
        self._api_key = (api_key
                         or next((os.getenv(k, "").strip() for k in key_envs if os.getenv(k)), "")).strip()
        self._timeout_s = float(os.getenv("DIRECTION_LLM_TIMEOUT_S", "12") or "12")
        self._grounding = grounding if grounding is not None else GeminiGrounding()
        self._out_path = out_path or (_run_dir() / "direction_shadow.jsonl")
        self._lock = threading.Lock()
        if self.enabled:
            logger.info("direction shadow ON provider=%s model=%s grounding=%s -> %s",
                        self._provider, self._model, self._grounding.enabled, self._out_path)

    @property
    def enabled(self) -> bool:
        return bool(self._enabled and self._api_key and self._base_url)

    def record(self, snap: Any, taken: Any) -> None:
        """Capture cheap context now; do the LLM call + write on a daemon thread."""
        if not self.enabled:
            return
        try:
            facts = build_facts_from_accessor(snap)
            ctx = {
                "ts": str(getattr(snap, "timestamp", "") or ""),
                "trade_date": str(getattr(snap, "trade_date", "") or getattr(snap, "date", "") or ""),
                "fut_price": getattr(snap, "fut_close", None),
                "taken_direction": getattr(getattr(taken, "direction", None), "value", None)
                                   or str(getattr(taken, "direction", "") or ""),
                "taken_source": str(getattr(taken, "source", "") or ""),
                "ce_score": round(float(getattr(taken, "ce_score", 0.0) or 0.0), 4),
                "pe_score": round(float(getattr(taken, "pe_score", 0.0) or 0.0), 4),
            }
        except Exception:  # never touch the trade path
            logger.debug("direction shadow: context capture failed", exc_info=True)
            return
        t = threading.Thread(target=self._call_and_write, args=(facts, ctx), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    def _call_and_write(self, facts: dict[str, Any], ctx: dict[str, Any]) -> None:
        try:
            web_context = ""
            try:
                web_context = self._grounding.get()
            except Exception:
                web_context = ""
            verdict = ask_direction(
                facts, base_url=self._base_url, api_key=self._api_key, model=self._model,
                web_context=web_context, timeout_s=self._timeout_s,
            )
            line = {
                **ctx,
                "facts": facts,
                "llm_direction": verdict.direction,
                "llm_confidence": verdict.confidence,
                "llm_reason": verdict.reason,
                "llm_grounded": verdict.grounded,
                "llm_model": verdict.model,
                "llm_error": verdict.error,
                "agrees_taken": (verdict.direction == ctx.get("taken_direction")
                                 if verdict.committed else None),
                "logged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._write(line)
        except Exception:
            logger.debug("direction shadow: call/write failed", exc_info=True)

    def _write(self, line: dict[str, Any]) -> None:
        try:
            with self._lock:
                self._out_path.parent.mkdir(parents=True, exist_ok=True)
                with self._out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line, default=str) + "\n")
        except Exception:
            logger.debug("direction shadow: write failed", exc_info=True)


def summarize_shadow_log(path: Optional[Path] = None, *, recent: int = 20) -> dict[str, Any]:
    """Read the shadow JSONL and summarise LLM behaviour vs the taken side.

    Truth-free: reports how often the LLM committed, agreed with our composite, its
    confidence/grounding — enough to WATCH it live on paper. Direction *accuracy* needs
    realised outcomes (tools/reconcile_direction_shadow.py), reported here only if a
    `truth` field has been backfilled into the log.
    """
    p = path or (_run_dir() / "direction_shadow.jsonl")
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        return {"exists": False, "path": str(p), "n": 0}

    n = len(rows)
    committed = [r for r in rows if r.get("llm_direction") in ("CE", "PE")]
    errs = [r for r in rows if r.get("llm_error")]
    agreed = [r for r in committed if r.get("agrees_taken") is True]
    grounded = [r for r in rows if r.get("llm_grounded")]
    confs = [float(r["llm_confidence"]) for r in committed
             if isinstance(r.get("llm_confidence"), (int, float))]
    truthed = [r for r in committed if r.get("truth") in ("CE", "PE")]
    correct = [r for r in truthed if r.get("llm_direction") == r.get("truth")]

    def rate(a: int, b: int) -> Optional[float]:
        return round(a / b, 4) if b else None

    return {
        "exists": True,
        "path": str(p),
        "n": n,
        "n_committed": len(committed),
        "n_abstain": n - len(committed) - len(errs),
        "n_errors": len(errs),
        "commit_rate": rate(len(committed), n),
        "agree_with_taken_rate": rate(len(agreed), len(committed)),
        "grounded_rate": rate(len(grounded), n),
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "llm_accuracy_if_truthed": rate(len(correct), len(truthed)),
        "n_truthed": len(truthed),
        "recent": rows[-recent:],
    }


# Process-local singleton so the resolver hook stays a one-liner and we don't rebuild
# the grounding cache / re-read env on every entry.
_SINGLETON: Optional[DirectionShadow] = None
_SINGLETON_LOCK = threading.Lock()


def get_direction_shadow() -> DirectionShadow:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = DirectionShadow()
    return _SINGLETON


__all__ = ["DirectionShadow", "get_direction_shadow"]
