from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Optional

from ..factory.runner import WorkflowRunner
from ..factory.selector import select_winner
from ..factory.spec import load_workflow_spec
from ..factory.state import WorkflowState
from .generator import CampaignExpansion, CampaignGenerator
from .spec import CampaignSpec


def resolve_campaign_root(spec: CampaignSpec, output_root: Optional[Path]) -> Path:
    if output_root is None:
        return (Path("ml_pipeline_2") / "artifacts" / "campaign_runs" / spec.campaign_id).resolve()
    explicit = Path(output_root).resolve()
    if explicit.name == spec.campaign_id:
        return explicit
    return (explicit / spec.campaign_id).resolve()


class CampaignRunner:
    def __init__(self, spec: CampaignSpec, campaign_root: Path) -> None:
        self.spec = spec
        self.campaign_root = Path(campaign_root).resolve()

    def _load_expansion_metadata(self, expansion: CampaignExpansion) -> Dict[str, Dict[str, Any]]:
        return {
            lane.lane_id: {
                "template_id": lane.template_id,
                "selections": dict(lane.selections),
                "depends_on": list(lane.depends_on),
                "grid_manifest_path": str(lane.grid_manifest_path),
                "staged_manifest_path": str(lane.staged_manifest_path),
            }
            for lane in expansion.generated_lanes
        }

    def _blocking_reasons_by_family(self, expansion: CampaignExpansion, workflow_state: WorkflowState) -> Dict[str, Any]:
        family_rollups: Dict[str, Dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
        family_lanes: Dict[str, Dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for lane in expansion.generated_lanes:
            lane_state = workflow_state.get(lane.lane_id)
            reason = str(lane_state.last_error or "").strip()
            if not reason:
                continue
            for axis, value in lane.selections.items():
                family_rollups[axis][value][reason] += 1
                family_lanes[axis][value].add(lane.lane_id)
        payload: Dict[str, Any] = {}
        for axis, values in sorted(family_rollups.items()):
            rows = []
            for value, reasons in sorted(values.items()):
                rows.append(
                    {
                        "value": value,
                        "lanes": sorted(family_lanes[axis][value]),
                        "reasons": [
                            {"reason": reason, "count": count}
                            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
                        ],
                    }
                )
            payload[axis] = rows
        return payload

    def _best_nonpublishable_by_template(self, expansion: CampaignExpansion, workflow_state: WorkflowState) -> list[Dict[str, Any]]:
        rows_by_template: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
        for lane in expansion.generated_lanes:
            lane_state = workflow_state.get(lane.lane_id)
            if lane_state.status.value != "held" or not isinstance(lane_state.metrics, dict):
                continue
            rows_by_template[lane.template_id].append({"lane_id": lane.lane_id, **lane_state.metrics})
        results: list[Dict[str, Any]] = []
        for template_id, rows in sorted(rows_by_template.items()):
            winner = select_winner(rows, strategy=self.spec.execution_defaults.ranking_strategy)
            if winner is None:
                continue
            results.append({"template_id": template_id, **winner})
        return results

    def _write_campaign_result(
        self,
        *,
        expansion: CampaignExpansion,
        workflow_result: Optional[Dict[str, Any]],
        generated_only: bool,
        factory_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        expansion_metadata = self._load_expansion_metadata(expansion)
        payload: Dict[str, Any] = {
            "campaign_id": self.spec.campaign_id,
            "status": ("generated_only" if generated_only else str((workflow_result or {}).get("status") or "unknown")),
            "generated_lane_count": len(expansion.generated_lanes),
            "generated_workflow_path": str(expansion.generated_workflow_path),
            "campaign_expansion_path": str(expansion.campaign_expansion_path),
            "lanes": [
                {
                    "lane_id": lane_id,
                    **metadata,
                }
                for lane_id, metadata in sorted(expansion_metadata.items())
            ],
            "factory_result": workflow_result,
        }
        if generated_only:
            result_path = self.campaign_root / "campaign_result.json"
            result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return payload

        workflow_spec = load_workflow_spec(expansion.generated_workflow_path)
        workflow_state = WorkflowState.load(factory_root or self.campaign_root, workflow_spec)
        payload["blocking_reasons_by_family"] = self._blocking_reasons_by_family(expansion, workflow_state)
        payload["best_nonpublishable_by_template"] = self._best_nonpublishable_by_template(expansion, workflow_state)
        result_path = self.campaign_root / "campaign_result.json"
        result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _reset_campaign_root(self) -> None:
        if self.campaign_root.exists():
            shutil.rmtree(self.campaign_root)

    def generate(self) -> CampaignExpansion:
        return CampaignGenerator(self.spec, self.campaign_root).generate()

    def run(self, *, generate_only: bool = False, fresh: bool = False) -> Dict[str, Any]:
        if fresh:
            self._reset_campaign_root()
        expansion = self.generate()
        if generate_only:
            return self._write_campaign_result(expansion=expansion, workflow_result=None, generated_only=True)
        workflow_spec = load_workflow_spec(expansion.generated_workflow_path)
        factory_root = self.campaign_root
        workflow_result = WorkflowRunner(workflow_spec, factory_root).run()
        return self._write_campaign_result(expansion=expansion, workflow_result=workflow_result, generated_only=False, factory_root=factory_root)


__all__ = ["CampaignRunner", "resolve_campaign_root"]
