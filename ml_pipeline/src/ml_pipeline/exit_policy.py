import argparse
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class ExitPolicyConfig:
    version: str = "v1"
    time_stop_minutes: int = 3
    stop_loss_pct: Optional[float] = 0.12
    take_profit_pct: Optional[float] = 0.24
    enable_trailing_stop: bool = False
    trailing_stop_pct: Optional[float] = None
    move_to_break_even_at_profit_pct: Optional[float] = None
    allow_hold_extension: bool = False
    max_hold_extension_minutes: int = 0
    extension_min_model_prob: Optional[float] = None
    forced_eod_exit_time: str = "15:24"


@dataclass(frozen=True)
class ExitPolicyValidationResult:
    ok: bool
    errors: List[str]
    config: Optional[ExitPolicyConfig]


def _to_time_hhmm(value: str) -> Optional[time]:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        return None


def _validate_numeric_range(
    key: str,
    value: Optional[float],
    minimum: float,
    maximum: float,
    allow_none: bool = True,
) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        return f"{key} is required"
    if value < minimum or value > maximum:
        return f"{key} must be in [{minimum}, {maximum}]"
    return None


def validate_exit_policy_dict(payload: Dict[str, object]) -> ExitPolicyValidationResult:
    if not isinstance(payload, dict):
        return ExitPolicyValidationResult(ok=False, errors=["payload must be an object"], config=None)

    allowed_fields = {f.name for f in fields(ExitPolicyConfig)}
    unknown = sorted(set(payload.keys()) - allowed_fields)
    errors: List[str] = []
    if unknown:
        errors.append(f"unknown fields: {', '.join(unknown)}")

    default = ExitPolicyConfig()
    merged: Dict[str, object] = asdict(default)
    merged.update(payload)

    try:
        version = str(merged["version"])
        time_stop_minutes = int(merged["time_stop_minutes"])
        stop_loss_pct = None if merged["stop_loss_pct"] is None else float(merged["stop_loss_pct"])
        take_profit_pct = None if merged["take_profit_pct"] is None else float(merged["take_profit_pct"])
        enable_trailing_stop = bool(merged["enable_trailing_stop"])
        trailing_stop_pct = None if merged["trailing_stop_pct"] is None else float(merged["trailing_stop_pct"])
        move_to_break_even = (
            None
            if merged["move_to_break_even_at_profit_pct"] is None
            else float(merged["move_to_break_even_at_profit_pct"])
        )
        allow_hold_extension = bool(merged["allow_hold_extension"])
        max_hold_extension_minutes = int(merged["max_hold_extension_minutes"])
        extension_min_model_prob = (
            None if merged["extension_min_model_prob"] is None else float(merged["extension_min_model_prob"])
        )
        forced_eod_exit_time = str(merged["forced_eod_exit_time"])
    except (TypeError, ValueError) as exc:
        return ExitPolicyValidationResult(ok=False, errors=[f"type conversion error: {exc}"], config=None)

    if version != "v1":
        errors.append("version must be 'v1'")
    if time_stop_minutes < 1 or time_stop_minutes > 60:
        errors.append("time_stop_minutes must be in [1, 60]")

    err = _validate_numeric_range("stop_loss_pct", stop_loss_pct, minimum=0.0, maximum=1.0, allow_none=False)
    if err:
        errors.append(err)
    err = _validate_numeric_range("take_profit_pct", take_profit_pct, minimum=0.0, maximum=2.0, allow_none=False)
    if err:
        errors.append(err)

    if enable_trailing_stop:
        err = _validate_numeric_range("trailing_stop_pct", trailing_stop_pct, minimum=0.0, maximum=1.0, allow_none=False)
        if err:
            errors.append(err)
    elif trailing_stop_pct is not None:
        errors.append("trailing_stop_pct must be null when enable_trailing_stop=false")

    if move_to_break_even is not None:
        err = _validate_numeric_range(
            "move_to_break_even_at_profit_pct",
            move_to_break_even,
            minimum=0.0,
            maximum=2.0,
            allow_none=False,
        )
        if err:
            errors.append(err)

    if allow_hold_extension:
        if max_hold_extension_minutes < 1 or max_hold_extension_minutes > 30:
            errors.append("max_hold_extension_minutes must be in [1, 30] when allow_hold_extension=true")
        err = _validate_numeric_range(
            "extension_min_model_prob",
            extension_min_model_prob,
            minimum=0.0,
            maximum=1.0,
            allow_none=False,
        )
        if err:
            errors.append(err)
    else:
        if max_hold_extension_minutes != 0:
            errors.append("max_hold_extension_minutes must be 0 when allow_hold_extension=false")
        if extension_min_model_prob is not None:
            errors.append("extension_min_model_prob must be null when allow_hold_extension=false")

    parsed_time = _to_time_hhmm(forced_eod_exit_time)
    if parsed_time is None:
        errors.append("forced_eod_exit_time must be HH:MM format")
    else:
        nse_open = time(hour=9, minute=15)
        nse_close = time(hour=15, minute=30)
        if parsed_time < nse_open or parsed_time > nse_close:
            errors.append("forced_eod_exit_time must be within NSE session [09:15, 15:30]")

    if stop_loss_pct is not None and take_profit_pct is not None and take_profit_pct <= stop_loss_pct:
        errors.append("take_profit_pct must be greater than stop_loss_pct")

    if errors:
        return ExitPolicyValidationResult(ok=False, errors=errors, config=None)

    cfg = ExitPolicyConfig(
        version=version,
        time_stop_minutes=time_stop_minutes,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        enable_trailing_stop=enable_trailing_stop,
        trailing_stop_pct=trailing_stop_pct,
        move_to_break_even_at_profit_pct=move_to_break_even,
        allow_hold_extension=allow_hold_extension,
        max_hold_extension_minutes=max_hold_extension_minutes,
        extension_min_model_prob=extension_min_model_prob,
        forced_eod_exit_time=forced_eod_exit_time,
    )
    return ExitPolicyValidationResult(ok=True, errors=[], config=cfg)


def parse_exit_policy(payload: Dict[str, object]) -> ExitPolicyConfig:
    result = validate_exit_policy_dict(payload)
    if not result.ok or result.config is None:
        raise ValueError("; ".join(result.errors))
    return result.config


def load_exit_policy(path: Path) -> ExitPolicyConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("policy file must contain JSON object")
    return parse_exit_policy(payload)


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and normalize exit policy config")
    parser.add_argument("--policy-json", default=None, help="Input exit policy JSON path. If omitted, default is used.")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t14_exit_policy_validation_report.json")
    parser.add_argument("--normalized-out", default="ml_pipeline/artifacts/t14_exit_policy_config.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.policy_json:
        policy_path = Path(args.policy_json)
        if not policy_path.exists():
            print(f"ERROR: policy file not found: {policy_path}")
            return 2
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    else:
        payload = asdict(ExitPolicyConfig())

    result = validate_exit_policy_dict(payload if isinstance(payload, dict) else {})
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "ok": result.ok,
        "errors": result.errors,
        "normalized_config": asdict(result.config) if result.config is not None else None,
    }

    report_out = Path(args.report_out)
    normalized_out = Path(args.normalized_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if result.config is not None:
        normalized_out.parent.mkdir(parents=True, exist_ok=True)
        normalized_out.write_text(json.dumps(asdict(result.config), indent=2), encoding="utf-8")

    print(f"OK: {result.ok}")
    print(f"Errors: {len(result.errors)}")
    print(f"Report: {report_out}")
    if result.config is not None:
        print(f"Normalized: {normalized_out}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
