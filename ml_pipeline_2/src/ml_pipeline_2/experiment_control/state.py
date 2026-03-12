from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunContext:
    output_root: Path
    resolved_config: Dict[str, Any]

    @property
    def state_path(self) -> Path:
        return self.output_root / "state.jsonl"

    def write_json(self, relative_path: str | Path, payload: Dict[str, Any]) -> Path:
        path = self.output_root / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def write_text(self, relative_path: str | Path, payload: str) -> Path:
        path = self.output_root / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(payload), encoding="utf-8")
        return path

    def append_state(self, event: str, **data: Any) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts_utc": utc_now(), "event": str(event)}
        payload.update(data)
        with self.state_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

