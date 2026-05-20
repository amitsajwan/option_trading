"""Single rule × single window backtest CLI.

Loads a rule from JSON, loads merged flat+options data for the requested
date window, simulates single-position execution, writes trades.parquet,
and runs the canonical audit harness against the resulting trades.

Outputs in <output_dir>:
    rule.json         — copy of the input rule (provenance)
    trades.parquet    — one row per trade (cols: trade_date, entry_minute,
                        exit_minute, entry_premium, exit_premium,
                        net_pnl_pct (decimal), exit_reason, mfe_pct, mae_pct)
    audit.json        — audit_run.audit() output (passed/failed + gate breakdown)
    summary.txt       — one-line human-readable verdict

Usage:
    python -m ml_pipeline_2.scripts.rules_pipeline.run_backtest \\
        --rule path/to/rule.json \\
        --start 2024-08-01 --end 2024-10-31 \\
        --output-dir artifacts/rules_runs/r1_aug_oct \\
        --exit-mode mechanical

Designed to be both a standalone CLI and a callable function so the
orchestrator (pipeline.py) can drive it without subprocess overhead.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def run_backtest(
    rule_dict: Dict[str, Any],
    start_date: str,
    end_date: str,
    output_dir: Path,
    *,
    flat_root: Optional[Path] = None,
    options_root: Optional[Path] = None,
    exit_mode: str = "mechanical",
    cost_bps: float = 2.0,
    audit_thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one rule against one window. Returns the audit dict.

    Pure-function: writes artifacts under output_dir, returns the audit
    result so the caller can rank cells without re-reading disk.
    """
    # Local imports — keep module-import-time cheap so the CLI starts fast.
    import pandas as pd

    from .data_loader import DEFAULT_FLAT_ROOT, DEFAULT_OPTIONS_ROOT, load_merged_data_both
    from .execution_sim import simulate_trades
    from .rule_schema import Rule
    from .signal_generator import generate_signals
    from ..model_selection.audit_run import audit

    flat_root = flat_root or DEFAULT_FLAT_ROOT
    options_root = options_root or DEFAULT_OPTIONS_ROOT
    output_dir.mkdir(parents=True, exist_ok=True)

    # Provenance: save the rule we ran with.
    (output_dir / "rule.json").write_text(json.dumps(rule_dict, indent=2))

    rule = Rule.from_dict(rule_dict)

    logger.info("loading data: %s → %s", start_date, end_date)
    df = load_merged_data_both(flat_root, options_root, start_date, end_date)
    logger.info("loaded %d rows (%d distinct days)", len(df),
                df["trade_date"].nunique() if len(df) else 0)

    df["signal"] = generate_signals(df, rule)
    n_signals = int(df["signal"].sum())
    logger.info("rule fires on %d/%d rows (%.2f%%)", n_signals, len(df),
                100 * n_signals / max(1, len(df)))

    trades = simulate_trades(df, rule, exit_mode=exit_mode, cost_bps=cost_bps)
    trades_path = output_dir / "trades.parquet"
    if len(trades) == 0:
        # audit_run handles a zero-row file by erroring, so emit an empty
        # parquet with the right schema so downstream tooling doesn't crash.
        pd.DataFrame(columns=[
            "trade_date", "entry_minute", "exit_minute", "entry_premium",
            "exit_premium", "net_pnl_pct", "exit_reason", "mfe_pct", "mae_pct",
        ]).to_parquet(trades_path)
    else:
        trades.to_parquet(trades_path)
    logger.info("trades written: %d → %s", len(trades), trades_path)

    thresholds = {
        "min_trades": 30, "max_trades": 100_000,
        "min_win_rate": 0.40, "t_min": 2.0,
        "ci_must_exclude_zero": True,
        "outlier_survival_must_be_nonneg": True,
    }
    if audit_thresholds:
        thresholds.update(audit_thresholds)

    result = audit(
        trades_path=trades_path,
        return_col="net_pnl_pct",
        date_col="trade_date",
        min_trades=int(thresholds["min_trades"]),
        max_trades=int(thresholds["max_trades"]),
        min_win_rate=float(thresholds["min_win_rate"]),
        t_min=float(thresholds["t_min"]),
        ci_must_exclude_zero=bool(thresholds["ci_must_exclude_zero"]),
        outlier_survival_must_be_nonneg=bool(thresholds["outlier_survival_must_be_nonneg"]),
    )

    # Annotate with the cell metadata so the orchestrator can rank without
    # re-reading other state.
    result["rule_id"] = rule.rule_id
    result["direction"] = rule.direction
    result["exit_mode"] = exit_mode
    result["window"] = {"start": start_date, "end": end_date}
    result["n_signal_rows"] = n_signals
    result["n_trades_emitted"] = int(len(trades))

    (output_dir / "audit.json").write_text(json.dumps(result, indent=2, default=str))

    verdict = "PASS" if result.get("passed") else "FAIL"
    summary_line = (
        f"{rule.rule_id} {start_date}→{end_date} mode={exit_mode} "
        f"signals={n_signals} trades={len(trades)} -> {verdict}"
    )
    if result.get("stats"):
        st = result["stats"]
        ci = result.get("ci", {})
        summary_line += (
            f" | t={st.get('t', 0):+.2f}"
            f" ci=[{ci.get('ci_lo', 0)*100:+.2f}%,{ci.get('ci_hi', 0)*100:+.2f}%]"
            f" wr={result.get('win_rate', 0)*100:.1f}%"
        )
    (output_dir / "summary.txt").write_text(summary_line + "\n")
    logger.info(summary_line)
    return result


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--rule", required=True, help="Path to rule JSON")
    p.add_argument("--start", required=True, help="Window start YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Window end YYYY-MM-DD")
    p.add_argument("--output-dir", required=True, help="Directory for artifacts")
    p.add_argument("--exit-mode", default="mechanical", choices=["mechanical", "signal"])
    p.add_argument("--cost-bps", type=float, default=2.0)
    p.add_argument("--flat-root", default=None, help="Override flat parquet root")
    p.add_argument("--options-root", default=None, help="Override options parquet root")
    p.add_argument("--min-trades", type=int, default=30)
    p.add_argument("--t-min", type=float, default=2.0)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    rule_path = Path(args.rule)
    if not rule_path.exists():
        print(f"FATAL: rule file not found: {rule_path}", file=sys.stderr)
        return 1
    rule_dict = json.loads(rule_path.read_text())

    result = run_backtest(
        rule_dict=rule_dict,
        start_date=args.start,
        end_date=args.end,
        output_dir=Path(args.output_dir),
        flat_root=Path(args.flat_root) if args.flat_root else None,
        options_root=Path(args.options_root) if args.options_root else None,
        exit_mode=args.exit_mode,
        cost_bps=args.cost_bps,
        audit_thresholds={"min_trades": args.min_trades, "t_min": args.t_min},
    )

    return 0 if result.get("passed") else 0  # cell completion ≠ pass — orchestrator ranks


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
