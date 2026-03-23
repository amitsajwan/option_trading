"""Aggregate open-search rebaseline cycle outputs into one comparable table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _deterministic_metrics(cycle_dir: Path) -> dict[str, Any]:
    champion_path = cycle_dir / "deterministic" / "champion.json"
    if not champion_path.exists():
        return {}
    payload = _load_json(champion_path)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "deterministic_champion_id": payload.get("candidate_id"),
        "deterministic_return_pct": _safe_float(summary.get("net_capital_return_pct")),
        "deterministic_max_drawdown_pct": _safe_float(summary.get("max_drawdown_pct")),
        "deterministic_trades": _safe_float(summary.get("trades")),
        "deterministic_profit_factor": _safe_float(summary.get("profit_factor")),
    }


def _ml_metrics(cycle_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ml_champion_count": 0,
        "ml_hard_gate_champion_id": None,
        "ml_hard_gate_return_pct": None,
        "ml_hard_gate_max_drawdown_pct": None,
        "ml_hard_gate_trades": None,
        "ml_best_holdout_experiment_id": None,
        "ml_best_holdout_return_pct": None,
        "ml_best_holdout_max_drawdown_pct": None,
        "ml_best_holdout_trades": None,
    }

    champions_path = cycle_dir / "ml" / "champions" / "champion_registry.json"
    if champions_path.exists():
        champions_payload = _load_json(champions_path)
        champions = champions_payload.get("champions") if isinstance(champions_payload.get("champions"), list) else []
        out["ml_champion_count"] = int(len(champions))
        if champions:
            first = champions[0] if isinstance(champions[0], dict) else {}
            out["ml_hard_gate_champion_id"] = first.get("experiment_id")
            out["ml_hard_gate_return_pct"] = _safe_float(first.get("ml_capital_return_pct"))
            out["ml_hard_gate_max_drawdown_pct"] = _safe_float(first.get("ml_max_drawdown_pct"))
            out["ml_hard_gate_trades"] = _safe_float(first.get("ml_trades"))

    holdout_registry = cycle_dir / "ml" / "replay_holdout" / "evaluation_registry.csv"
    if holdout_registry.exists():
        df = pd.read_csv(holdout_registry)
        if not df.empty:
            for col in ("ml_capital_return_pct", "ml_max_drawdown_pct", "ml_trades"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values(
                ["ml_capital_return_pct", "ml_max_drawdown_pct", "ml_trades"],
                ascending=[False, False, False],
                kind="stable",
            )
            top = df.iloc[0]
            out["ml_best_holdout_experiment_id"] = str(top.get("experiment_id") or "")
            out["ml_best_holdout_return_pct"] = _safe_float(top.get("ml_capital_return_pct"))
            out["ml_best_holdout_max_drawdown_pct"] = _safe_float(top.get("ml_max_drawdown_pct"))
            out["ml_best_holdout_trades"] = _safe_float(top.get("ml_trades"))

    return out


def build_tracker_table(*, runs_root: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for summary_path in runs_root.rglob("cycle_summary.json"):
        cycle_dir = summary_path.parent
        summary = _load_json(summary_path)
        row: dict[str, Any] = {
            "cycle_summary_json": str(summary_path).replace("\\", "/"),
            "cycle_dir": str(cycle_dir).replace("\\", "/"),
            "cycle_id": summary.get("cycle_id"),
            "created_at_utc": summary.get("created_at_utc"),
            "window_start": summary.get("window_start"),
            "window_end": summary.get("window_end"),
            "formal_run": bool(summary.get("formal_run")),
            "exploratory_only": bool(summary.get("exploratory_only")),
            "manifest_hash": summary.get("manifest_hash"),
        }
        row.update(_deterministic_metrics(cycle_dir))
        row.update(_ml_metrics(cycle_dir))
        rows.append(row)

    if not rows:
        return pd.DataFrame(
            columns=[
                "cycle_summary_json",
                "cycle_dir",
                "cycle_id",
                "created_at_utc",
                "window_start",
                "window_end",
                "formal_run",
                "exploratory_only",
                "manifest_hash",
                "deterministic_champion_id",
                "deterministic_return_pct",
                "deterministic_max_drawdown_pct",
                "deterministic_trades",
                "deterministic_profit_factor",
                "ml_champion_count",
                "ml_hard_gate_champion_id",
                "ml_hard_gate_return_pct",
                "ml_hard_gate_max_drawdown_pct",
                "ml_hard_gate_trades",
                "ml_best_holdout_experiment_id",
                "ml_best_holdout_return_pct",
                "ml_best_holdout_max_drawdown_pct",
                "ml_best_holdout_trades",
            ]
        )

    out = pd.DataFrame(rows)
    return out.sort_values(["created_at_utc", "cycle_id"], ascending=[False, True], kind="stable").reset_index(drop=True)


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate rebaseline cycle results into one table.")
    parser.add_argument("--runs-root", default=".run")
    parser.add_argument("--out-csv", default=".run/rebaseline_tracker/results.csv")
    parser.add_argument("--out-json", default=".run/rebaseline_tracker/results.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    table = build_tracker_table(runs_root=Path(args.runs_root))
    out_csv = Path(args.out_csv)
    out_json = Path(args.out_json)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out_csv, index=False)
    out_json.write_text(table.to_json(orient="records", indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "rows": int(len(table)),
                "out_csv": str(out_csv).replace("\\", "/"),
                "out_json": str(out_json).replace("\\", "/"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
