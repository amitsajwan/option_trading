import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .stage import run_eda_stage
from ..pipeline_layout import resolve_market_archive_base, resolve_vix_source
from ..schema_validator import discover_available_days


def _select_days(
    *,
    all_days: Sequence[str],
    holdout_days: int,
    min_day: Optional[str],
    max_day: Optional[str],
) -> List[str]:
    filtered = [str(d) for d in all_days]
    if min_day:
        filtered = [d for d in filtered if d >= str(min_day)]
    if max_day:
        filtered = [d for d in filtered if d <= str(max_day)]

    if holdout_days > 0:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=int(holdout_days))).isoformat()
        filtered = [d for d in filtered if d <= cutoff]
    return filtered


def _apply_lookback_window(days: Sequence[str], lookback_years: int) -> List[str]:
    if lookback_years <= 0 or not days:
        return [str(d) for d in days]
    last = datetime.strptime(str(days[-1]), "%Y-%m-%d").date()
    start = last - timedelta(days=int(lookback_years) * 365)
    return [str(d) for d in days if datetime.strptime(str(d), "%Y-%m-%d").date() >= start]


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run EDA on all available days, optionally excluding the most recent N calendar days."
    )
    parser.add_argument("--base-path", default=None, help="Optional explicit market archive root")
    parser.add_argument("--vix-path", default=None, help="Optional explicit VIX file/dir")
    parser.add_argument("--out-dir", default=None, help="Optional processed output directory")
    parser.add_argument("--holdout-days", type=int, default=30, help="Exclude most recent N calendar days")
    parser.add_argument("--train-ratio", type=float, default=0.70, help="Day-level train ratio")
    parser.add_argument("--valid-ratio", type=float, default=0.15, help="Day-level valid ratio")
    parser.add_argument(
        "--quality-max-days",
        type=int,
        default=90,
        help="Profile quality on latest N selected days (speed guard).",
    )
    parser.add_argument(
        "--max-otm-steps",
        type=int,
        default=0,
        help="Option-chain strike window around ATM in strike-steps (0 disables filtering).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel day workers for canonical event build.",
    )
    parser.add_argument("--min-day", default=None, help="Optional lower bound day (YYYY-MM-DD)")
    parser.add_argument("--max-day", default=None, help="Optional upper bound day (YYYY-MM-DD)")
    parser.add_argument("--lookback-years", type=int, default=0, help="Use only latest N years from selected end date")
    parser.add_argument("--dry-run", action="store_true", help="Print selected days and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    archive = resolve_market_archive_base(args.base_path)
    if archive is None:
        raise SystemExit("Could not resolve market archive base. Provide --base-path.")
    vix = resolve_vix_source(args.vix_path)

    all_days = discover_available_days(archive)
    selected_days = _select_days(
        all_days=all_days,
        holdout_days=int(args.holdout_days),
        min_day=args.min_day,
        max_day=args.max_day,
    )
    selected_days = _apply_lookback_window(selected_days, int(args.lookback_years))
    if not selected_days:
        raise SystemExit("No days selected after filters. Adjust holdout/date bounds.")

    preview = {
        "archive": str(archive).replace("\\", "/"),
        "vix": str(vix).replace("\\", "/") if vix else None,
        "all_days_total": int(len(all_days)),
        "selected_days_total": int(len(selected_days)),
        "selected_first_day": selected_days[0],
        "selected_last_day": selected_days[-1],
        "holdout_days": int(args.holdout_days),
        "train_ratio": float(args.train_ratio),
        "valid_ratio": float(args.valid_ratio),
    }
    print(json.dumps(preview, indent=2))
    if args.dry_run:
        return 0

    out = run_eda_stage(
        base_path=str(archive),
        days=",".join(selected_days),
        max_days=0,
        vix_path=str(vix) if vix else None,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        train_ratio=float(args.train_ratio),
        valid_ratio=float(args.valid_ratio),
        quality_max_days=int(args.quality_max_days),
        max_otm_steps=int(args.max_otm_steps),
        workers=int(args.workers),
    )
    print(json.dumps({"result": out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
