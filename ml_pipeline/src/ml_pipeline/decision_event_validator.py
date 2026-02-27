import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SUPPORTED_MODES = {"dual", "ce_only", "pe_only"}
SUPPORTED_ACTIONS = {"BUY_CE", "BUY_PE", "HOLD"}
SUPPORTED_EVENT_TYPES = {"ENTRY", "MANAGE", "EXIT", "IDLE"}

COMMON_REQUIRED_FIELDS = (
    "generated_at",
    "timestamp",
    "mode",
    "ce_prob",
    "pe_prob",
    "ce_threshold",
    "pe_threshold",
    "action",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_number(value: object) -> bool:
    try:
        _ = float(value)
        return True
    except Exception:
        return False


def _is_non_negative_int(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float) and value.is_integer():
        return int(value) >= 0
    return False


def validate_event(event: Dict[str, object]) -> List[str]:
    errors: List[str] = []
    for key in COMMON_REQUIRED_FIELDS:
        if key not in event:
            errors.append(f"missing_{key}")

    mode = str(event.get("mode", ""))
    action = str(event.get("action", ""))
    if mode not in SUPPORTED_MODES:
        errors.append("invalid_mode")
    if action not in SUPPORTED_ACTIONS:
        errors.append("invalid_action")

    for numeric in ("ce_prob", "pe_prob", "ce_threshold", "pe_threshold"):
        if numeric in event and (not _is_number(event.get(numeric))):
            errors.append(f"invalid_{numeric}")

    event_type = event.get("event_type")
    if event_type is None:
        return errors

    et = str(event_type)
    if et not in SUPPORTED_EVENT_TYPES:
        errors.append("invalid_event_type")
        return errors

    reason = event.get("event_reason")
    if not isinstance(reason, str) or (not reason.strip()):
        errors.append("invalid_event_reason")

    if et in {"ENTRY", "MANAGE", "EXIT"}:
        position = event.get("position")
        if not isinstance(position, dict):
            errors.append("invalid_position")
        else:
            side = str(position.get("side", ""))
            if side not in {"CE", "PE"}:
                errors.append("invalid_position_side")
            entry_ts = position.get("entry_timestamp")
            if not isinstance(entry_ts, str) or (not entry_ts):
                errors.append("invalid_position_entry_timestamp")
            if not _is_number(position.get("entry_confidence")):
                errors.append("invalid_position_entry_confidence")

    if et in {"MANAGE", "EXIT"}:
        if not _is_non_negative_int(event.get("held_minutes")):
            errors.append("invalid_held_minutes")

    return errors


def validate_jsonl(
    path: Path,
    *,
    max_error_samples: int = 25,
) -> Dict[str, object]:
    rows_total = 0
    valid_rows = 0
    invalid_rows = 0
    error_counts: Counter[str] = Counter()
    error_samples: List[Dict[str, object]] = []

    for idx, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        rows_total += 1
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            invalid_rows += 1
            error_counts["invalid_json"] += 1
            if len(error_samples) < int(max_error_samples):
                error_samples.append({"line": int(idx), "errors": ["invalid_json"]})
            continue

        if not isinstance(payload, dict):
            invalid_rows += 1
            error_counts["invalid_record_type"] += 1
            if len(error_samples) < int(max_error_samples):
                error_samples.append({"line": int(idx), "errors": ["invalid_record_type"]})
            continue

        errs = validate_event(payload)
        if len(errs) == 0:
            valid_rows += 1
            continue

        invalid_rows += 1
        for err in errs:
            error_counts[err] += 1
        if len(error_samples) < int(max_error_samples):
            error_samples.append({"line": int(idx), "errors": errs})

    invalid_share = float(invalid_rows / rows_total) if rows_total > 0 else 0.0
    status = "pass" if invalid_rows == 0 else "fail"
    return {
        "created_at_utc": _utc_now(),
        "status": status,
        "input_path": str(path),
        "rows_total": int(rows_total),
        "valid_rows": int(valid_rows),
        "invalid_rows": int(invalid_rows),
        "invalid_share": float(invalid_share),
        "error_type_counts": dict(error_counts),
        "error_samples": error_samples,
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate decision/event JSONL contract")
    parser.add_argument("--input-jsonl", default="ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t25_decision_event_validation_report.json")
    parser.add_argument("--max-error-samples", type=int, default=25)
    args = parser.parse_args(list(argv) if argv is not None else None)

    input_path = Path(args.input_jsonl)
    if not input_path.exists():
        print(f"ERROR: input JSONL not found: {input_path}")
        return 2

    report = validate_jsonl(input_path, max_error_samples=max(1, int(args.max_error_samples)))
    out_path = Path(args.report_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
