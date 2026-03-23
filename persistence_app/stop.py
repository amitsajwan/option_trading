from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from contracts_app import terminate_matching_processes

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stop persistence_app processes")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--no-force", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = terminate_matching_processes(
        tokens=["-m persistence_app.main_snapshot_consumer", "-m persistence_app.main_strategy_consumer"],
        timeout_seconds=max(0.5, float(args.timeout_seconds)),
        force_after_timeout=(not bool(args.no_force)),
    )
    payload = {
        "component": "persistence_app",
        "checked_at_ist": _ist_now_iso(),
        **result,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0 if payload["status"] in {"not_running", "stopped"} else 2


if __name__ == "__main__":
    raise SystemExit(run_cli())
