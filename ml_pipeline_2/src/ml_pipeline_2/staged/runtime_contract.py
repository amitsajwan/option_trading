from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable


STAGED_RUNTIME_BUNDLE_KIND = "ml_pipeline_2_staged_runtime_bundle_v1"
STAGED_RUNTIME_POLICY_KIND = "ml_pipeline_2_staged_runtime_policy_v1"

_RECIPE_REQUIRED_FIELDS = (
    "recipe_id",
    "horizon_minutes",
    "take_profit_pct",
    "stop_loss_pct",
)

_STAGE_POLICY_REQUIRED_FIELDS = {
    "stage1": ("selected_threshold",),
    "stage2": ("selected_ce_threshold", "selected_pe_threshold", "selected_min_edge"),
    "stage3": ("selected_threshold", "selected_margin_min"),
}


def _require_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be boolean")


def _require_stage3_selection_mode(value: object) -> str:
    mode = str(value or "dynamic").strip().lower()
    if mode not in {"dynamic", "fixed_recipe"}:
        raise ValueError("staged runtime policy stage3.selection_mode must be 'dynamic' or 'fixed_recipe'")
    return mode


def validate_recipe_catalog_payload(recipes: Iterable[Dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(list(recipes or [])):
        if not isinstance(item, dict):
            raise ValueError(f"recipe catalog row[{idx}] must be an object")
        row = dict(item)
        for field in _RECIPE_REQUIRED_FIELDS:
            if field not in row:
                raise ValueError(f"recipe catalog row[{idx}] missing {field}")
        recipe_id = str(row.get("recipe_id") or "").strip()
        if not recipe_id:
            raise ValueError(f"recipe catalog row[{idx}] has empty recipe_id")
        if recipe_id in seen:
            raise ValueError(f"duplicate recipe_id in recipe catalog: {recipe_id}")
        seen.add(recipe_id)
        out.append(
            {
                "recipe_id": recipe_id,
                "horizon_minutes": int(row["horizon_minutes"]),
                "take_profit_pct": float(row["take_profit_pct"]),
                "stop_loss_pct": float(row["stop_loss_pct"]),
            }
        )
    if not out:
        raise ValueError("recipe catalog must not be empty")
    return out


def load_staged_runtime_policy(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("staged runtime policy must be a JSON object")
    kind = str(payload.get("kind") or payload.get("publish_kind") or "").strip()
    if kind != STAGED_RUNTIME_POLICY_KIND:
        raise ValueError(f"unsupported staged runtime policy kind: {kind}")
    for section in ("stage1", "stage2", "stage3", "runtime"):
        if not isinstance(payload.get(section), dict):
            raise ValueError(f"staged runtime policy missing section: {section}")
    for section, fields in _STAGE_POLICY_REQUIRED_FIELDS.items():
        block = dict(payload.get(section) or {})
        for field in fields:
            if field not in block:
                raise ValueError(f"staged runtime policy missing {section}.{field}")
            try:
                block[field] = float(block[field])
            except Exception as exc:
                raise ValueError(f"staged runtime policy {section}.{field} must be numeric") from exc
        payload[section] = block
    payload["recipe_catalog"] = validate_recipe_catalog_payload(payload.get("recipe_catalog") or [])
    recipe_ids = {str(item["recipe_id"]) for item in payload["recipe_catalog"]}
    stage3 = dict(payload["stage3"])
    stage3["selection_mode"] = _require_stage3_selection_mode(stage3.get("selection_mode", "dynamic"))
    if stage3["selection_mode"] == "fixed_recipe":
        selected_recipe_id = str(stage3.get("selected_recipe_id") or "").strip()
        if not selected_recipe_id:
            raise ValueError("staged runtime policy stage3.selected_recipe_id must be set for fixed_recipe mode")
        if selected_recipe_id not in recipe_ids:
            raise ValueError(
                f"staged runtime policy stage3.selected_recipe_id must exist in recipe_catalog: {selected_recipe_id}"
            )
        stage3["selected_recipe_id"] = selected_recipe_id
    payload["stage3"] = stage3
    runtime = dict(payload["runtime"])
    gate_ids = list(runtime.get("prefilter_gate_ids") or [])
    if not gate_ids:
        raise ValueError("staged runtime policy runtime.prefilter_gate_ids must not be empty")
    runtime["block_expiry"] = _require_bool(runtime.get("block_expiry", False), field_name="staged runtime policy runtime.block_expiry")
    payload["runtime"] = runtime
    return payload
