from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from contracts_app import terminate_matching_processes

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


def _stop_component(name: str, tokens: list[str], timeout_seconds: float, force_after_timeout: bool) -> dict[str, Any]:
    res = terminate_matching_processes(
        tokens=tokens,
        timeout_seconds=timeout_seconds,
        force_after_timeout=force_after_timeout,
    )
    return {"component": name, **res}


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Stop process-app components")
    parser.add_argument("--include-dashboard", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--no-force", action="store_true")
    args = parser.parse_args()

    timeout_seconds = max(0.5, float(args.timeout_seconds))
    force_after_timeout = not bool(args.no_force)
    components = [
        _stop_component("snapshot_app", ["-m snapshot_app.main_live"], timeout_seconds, force_after_timeout),
        _stop_component("persistence_app", ["-m persistence_app.main_snapshot_consumer"], timeout_seconds, force_after_timeout),
        _stop_component("ingestion_app", ["-m ingestion_app.main_live", "-m ingestion_app.runner"], timeout_seconds, force_after_timeout),
    ]
    if bool(args.include_dashboard):
        components.append(
            _stop_component("dashboard", ["market_data_dashboard/start_dashboard.py", "start_dashboard.py"], timeout_seconds, force_after_timeout)
        )

    status = "stopped"
    exit_code = 0
    if any(c.get("status") == "partial" for c in components):
        status = "partial"
        exit_code = 2
    elif all(c.get("status") == "not_running" for c in components):
        status = "not_running"

    payload = {
        "checked_at_ist": _ist_now_iso(),
        "status": status,
        "components": components,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_cli())
