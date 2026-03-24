from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from contracts_app import isoformat_ist
except Exception:  # pragma: no cover
    isoformat_ist = None  # type: ignore

from strategy_app.engines.runtime_artifacts import summarize_runtime_artifacts


REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_RUNTIME_DIR = REPO_ROOT / ".run" / "strategy_app"
STRATEGY_RUNTIME_CONFIG_PATH = STRATEGY_RUNTIME_DIR / "runtime_config.json"
STRATEGY_RUNTIME_STATE_PATH = STRATEGY_RUNTIME_DIR / "runtime_state.json"
STRATEGY_RUNTIME_METRICS_PATH = STRATEGY_RUNTIME_DIR / "metrics.jsonl"


def _now_iso() -> str:
    if isoformat_ist is not None:
        return isoformat_ist(datetime.now(tz=timezone.utc))
    return datetime.now(tz=timezone.utc).isoformat()


def _load_json_object(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "error",
            "path": str(path),
            "error": f"invalid_json: {exc}",
        }
    if isinstance(payload, dict):
        return payload
    return {
        "status": "error",
        "path": str(path),
        "error": "expected_json_object",
        "value": payload,
    }


def _tail_jsonl(path: Path, *, limit: int = 25) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "line_count": 0, "tail": []}

    tail = deque(maxlen=max(1, int(limit)))
    line_count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                line_count += 1
                try:
                    tail.append(json.loads(line))
                except Exception as exc:
                    tail.append(
                        {
                            "status": "error",
                            "error": f"invalid_json: {exc}",
                            "raw": line,
                        }
                    )
    except Exception as exc:
        return {
            "path": str(path),
            "line_count": line_count,
            "tail": list(tail),
            "status": "error",
            "error": str(exc),
        }

    return {
        "path": str(path),
        "line_count": line_count,
        "tail": list(tail),
    }


def load_strategy_runtime_observability(*, repo_root: Optional[Path] = None, metrics_tail_limit: int = 25) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    runtime_dir = root / ".run" / "strategy_app"
    config_path = runtime_dir / "runtime_config.json"
    state_path = runtime_dir / "runtime_state.json"
    metrics_path = runtime_dir / "metrics.jsonl"

    runtime_config = _load_json_object(config_path)
    runtime_state = _load_json_object(state_path)
    metrics = _tail_jsonl(metrics_path, limit=metrics_tail_limit)

    config_error = isinstance(runtime_config, dict) and str(runtime_config.get("status") or "").lower() == "error"
    state_error = isinstance(runtime_state, dict) and str(runtime_state.get("status") or "").lower() == "error"
    metrics_error = str(metrics.get("status") or "").lower() == "error"

    present_count = sum(
        1
        for item in (runtime_config, runtime_state)
        if item is not None
    )
    has_metrics = int(metrics.get("line_count") or 0) > 0
    if present_count == 0 and not has_metrics:
        status = "unavailable"
    elif config_error or state_error or metrics_error:
        status = "degraded"
    elif runtime_config is not None and runtime_state is not None:
        status = "healthy"
    else:
        status = "degraded"

    return {
        "status": status,
        "checked_at_ist": _now_iso(),
        "service": "market-data-dashboard",
        "artifact_root": str(runtime_dir),
        "paths": {
            "runtime_config": str(config_path),
            "runtime_state": str(state_path),
            "metrics": str(metrics_path),
        },
        "artifacts": {
            "runtime_config_present": runtime_config is not None,
            "runtime_state_present": runtime_state is not None,
            "metrics_present": has_metrics,
        },
        "summary": summarize_runtime_artifacts(
            runtime_config if isinstance(runtime_config, dict) else None,
            runtime_state if isinstance(runtime_state, dict) else None,
            metrics,
        ),
        "runtime_config": runtime_config,
        "runtime_state": runtime_state,
        "metrics": metrics,
    }
