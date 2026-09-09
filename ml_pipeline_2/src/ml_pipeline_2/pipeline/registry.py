"""Results registry -- one place every pipeline run's result is captured,
instead of scattered VM log files and ad-hoc memory notes.

Document construction (`build_run_document`) is pure and unit-testable
without a database. The thin Mongo read/write wrapper (`RunRegistry`) is
exercised by integration use on the VM, matching how the rest of this
repo's Mongo-touching code is tested (no mongomock dependency here).

Collection: `trading_ai.ml_pipeline_runs`. One document per node
invocation (train, backtest, or deploy_decision) -- NOT one per study, so
a study with 3 trained models + 5 backtest configs is 8 documents, all
sharing a `study_id` for grouping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

NodeKind = Literal["train", "backtest", "deploy_decision"]
Verdict = Literal["usable", "degenerate", "contaminated", "inconclusive", "deployed", "rejected", "pending"]

COLLECTION_NAME = "ml_pipeline_runs"


def build_run_document(
    *,
    instrument: str,
    study_id: str,
    node: NodeKind,
    config: dict[str, Any],
    result: dict[str, Any],
    verdict: Verdict = "pending",
    artifact_path: Optional[str] = None,
    notes: str = "",
) -> dict[str, Any]:
    """Build a registry document. Pure function -- no I/O -- so it's
    trivially unit-tested and the shape is guaranteed consistent across
    every call site (train script, backtest script, deploy script alike)."""
    return {
        "instrument": instrument.strip().upper(),
        "study_id": study_id,
        "node": node,
        "config": config,
        "result": result,
        "verdict": verdict,
        "artifact_path": artifact_path,
        "notes": notes,
        "created_at": datetime.now(timezone.utc),
    }


class RunRegistry:
    """Thin wrapper around the `ml_pipeline_runs` Mongo collection.

    Usage:
        registry = RunRegistry(mongo_client)
        registry.record(build_run_document(...))
        registry.history(instrument="NIFTY", study_id="label_target_2026-09-09")
    """

    def __init__(self, mongo_client: Any, db_name: str = "trading_ai") -> None:
        self._collection = mongo_client[db_name][COLLECTION_NAME]

    def record(self, document: dict[str, Any]) -> str:
        result = self._collection.insert_one(document)
        return str(result.inserted_id)

    def history(
        self,
        *,
        instrument: Optional[str] = None,
        study_id: Optional[str] = None,
        node: Optional[NodeKind] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if instrument:
            query["instrument"] = instrument.strip().upper()
        if study_id:
            query["study_id"] = study_id
        if node:
            query["node"] = node
        cursor = self._collection.find(query).sort("created_at", -1).limit(limit)
        return list(cursor)

    def latest_verdict(self, *, instrument: str, study_id: str) -> Optional[dict[str, Any]]:
        """The single most recent document for a study -- typically the
        deploy_decision node, if one was recorded."""
        docs = self.history(instrument=instrument, study_id=study_id, limit=1)
        return docs[0] if docs else None
