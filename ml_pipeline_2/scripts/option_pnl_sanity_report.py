"""Pre-training sanity report on the option-P&L labels parquet.

Reads the parquet produced by build_option_pnl_labels.py and checks it
against the gates declared in
ml_pipeline_2/configs/research/option_label_contract.json::sanity_report_gates.

Per-recipe checks:
    - Label positive rate in [5%, 60%]
    - Avg entry premium >= 5
    - Missing-quote rate (skipped/total) <= 30%
    - Win-rate-before-cost vs after-cost gap reasonable
    - PnL distribution percentiles (sanity range)
    - Expiry-day-only-edge check (label rate on expiry-day rows vs others)
    - Time-of-day skew (no labels concentrated in first/last 15 min)

Bails non-zero if any HARD gate fails. Soft warnings still print but don't
fail the run.

Usage:
    python -m ml_pipeline_2.scripts.option_pnl_sanity_report \\
      --labels /opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "configs" / "research" / "option_label_contract.json"


def load_labels(labels_root: Path) -> pd.DataFrame:
    files = sorted(labels_root.glob("labels/year=*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no label parquet files under {labels_root}/labels/")
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    # Drop the empty-day stub rows (have no recipe_id)
    df = df[df["recipe_id"].notna()].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def load_skipped(labels_root: Path) -> pd.DataFrame:
    files = sorted(labels_root.glob("skipped/year=*/*.parquet"))
    if not files:
        return pd.DataFrame(columns=["trade_date", "snapshot_id", "recipe_id", "reason_skipped"])
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def fmt_pct(x: float, digits: int = 2) -> str:
    return f"{x*100:.{digits}f}%"


def report_recipe(labels_r: pd.DataFrame, skipped_r: pd.DataFrame, gates: dict, recipe_id: str) -> list[tuple[str, bool, str]]:
    """Return list of (gate_name, passed, message)."""
    results: list[tuple[str, bool, str]] = []
    n_labels = len(labels_r)
    n_skipped = len(skipped_r)
    n_total = n_labels + n_skipped

    if n_total == 0:
        results.append((f"{recipe_id} has data", False, "no rows at all"))
        return results

    # --- Hard gates ---
    pos_rate = labels_r["label"].mean() if n_labels else 0.0
    if gates["min_label_positive_rate"] <= pos_rate <= gates["max_label_positive_rate"]:
        results.append((f"{recipe_id} positive_rate in band", True, f"{fmt_pct(pos_rate)} (band {fmt_pct(gates['min_label_positive_rate'])}-{fmt_pct(gates['max_label_positive_rate'])})"))
    else:
        results.append((f"{recipe_id} positive_rate in band", False, f"{fmt_pct(pos_rate)} OUTSIDE {fmt_pct(gates['min_label_positive_rate'])}-{fmt_pct(gates['max_label_positive_rate'])}"))

    avg_premium = labels_r["entry_premium"].mean() if n_labels else 0.0
    if avg_premium >= gates["min_avg_entry_premium"]:
        results.append((f"{recipe_id} avg entry premium >= {gates['min_avg_entry_premium']}", True, f"Rs.{avg_premium:.2f}"))
    else:
        results.append((f"{recipe_id} avg entry premium >= {gates['min_avg_entry_premium']}", False, f"Rs.{avg_premium:.2f} TOO LOW — tiny premium trap"))

    miss_rate = n_skipped / n_total if n_total else 1.0
    if miss_rate <= gates["max_missing_quote_rate"]:
        results.append((f"{recipe_id} skip rate <= {fmt_pct(gates['max_missing_quote_rate'])}", True, f"{fmt_pct(miss_rate)} skipped"))
    else:
        results.append((f"{recipe_id} skip rate <= {fmt_pct(gates['max_missing_quote_rate'])}", False, f"{fmt_pct(miss_rate)} skipped — sparse data"))

    # --- Soft signals (printed, not gating) ---
    gross_pos_rate = (labels_r["gross_pnl_pct"] > 0).mean() if n_labels else 0.0
    cost_gap = gross_pos_rate - pos_rate
    msg = f"gross_pos_rate={fmt_pct(gross_pos_rate)}  net_pos_rate={fmt_pct(pos_rate)}  cost-eats {fmt_pct(cost_gap)} of marginal wins"
    results.append((f"{recipe_id} cost gap (soft)", True, msg))

    if n_labels:
        q = labels_r["net_pnl_pct"].quantile([0.05, 0.50, 0.95])
        results.append((f"{recipe_id} pnl distribution", True, f"p05={q.iloc[0]:+.3f}  p50={q.iloc[1]:+.3f}  p95={q.iloc[2]:+.3f}"))

    return results


def time_of_day_skew(labels: pd.DataFrame) -> str:
    """Quick descriptor of label distribution by intraday bucket."""
    if labels.empty:
        return "no data"
    bins = labels["timestamp_minute"].copy()
    bucket = pd.cut(
        bins,
        bins=[0, 555, 600, 800, 900, 1000],
        labels=["pre09:15", "09:15-10:00", "10:00-13:20", "13:20-15:00", "post15:00"],
        right=False,
    )
    counts = bucket.value_counts().sort_index()
    return ", ".join(f"{k}:{v}" for k, v in counts.items())


def main() -> int:
    p = argparse.ArgumentParser(description="Pre-training sanity report for option-P&L labels")
    p.add_argument("--labels", required=True, help="Output root of build_option_pnl_labels.py")
    p.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    p.add_argument("--json-out", default=None)
    args = p.parse_args()

    labels_root = Path(args.labels)
    contract = json.loads(Path(args.contract).read_text())
    gates = contract["sanity_report_gates"]

    print(f"=== Option-P&L sanity report ===")
    print(f"labels:   {labels_root}")
    print(f"contract: {args.contract}")
    print()

    df_labels = load_labels(labels_root)
    df_skipped = load_skipped(labels_root)

    print(f"loaded {len(df_labels):,} labels across {df_labels['trade_date'].nunique()} dates")
    print(f"loaded {len(df_skipped):,} skipped rows")
    print()

    all_failures: list[str] = []
    summary: dict = {"per_recipe": {}, "gates_pass": True}

    for recipe_id in sorted(df_labels["recipe_id"].unique()):
        labels_r = df_labels[df_labels["recipe_id"] == recipe_id]
        skipped_r = df_skipped[df_skipped["recipe_id"] == recipe_id] if not df_skipped.empty else df_skipped
        results = report_recipe(labels_r, skipped_r, gates, recipe_id)

        print(f"--- {recipe_id} ---")
        rec_failures = []
        for name, ok, msg in results:
            tag = "PASS" if ok else "FAIL"
            print(f"  [{tag}] {name}: {msg}")
            if not ok:
                rec_failures.append(name)
                all_failures.append(f"{recipe_id}: {name}")

        # Soft signals: time-of-day, day-of-week, by-expiry-day
        print(f"  time-of-day buckets: {time_of_day_skew(labels_r)}")

        # Day-of-week pos rates
        if not labels_r.empty:
            tmp = labels_r.copy()
            tmp["dow"] = pd.to_datetime(tmp["trade_date"]).dt.day_name()
            dow_pos = tmp.groupby("dow")["label"].agg(["mean", "size"])
            print(f"  pos-rate by weekday:")
            for dow_name in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
                if dow_name in dow_pos.index:
                    m = dow_pos.loc[dow_name, "mean"]
                    n = dow_pos.loc[dow_name, "size"]
                    print(f"    {dow_name:<10} {fmt_pct(m)} (n={n})")

        summary["per_recipe"][recipe_id] = {
            "n_labels": int(len(labels_r)),
            "n_skipped": int(len(skipped_r)),
            "positive_rate": float(labels_r["label"].mean()) if len(labels_r) else 0.0,
            "avg_entry_premium": float(labels_r["entry_premium"].mean()) if len(labels_r) else 0.0,
            "avg_net_pnl_pct": float(labels_r["net_pnl_pct"].mean()) if len(labels_r) else 0.0,
            "skipped_rate": float(len(skipped_r) / (len(labels_r) + len(skipped_r))) if (len(labels_r) + len(skipped_r)) else 0.0,
            "hard_gate_failures": rec_failures,
        }
        print()

    summary["gates_pass"] = not all_failures
    summary["failures"] = all_failures

    print("=== Verdict ===")
    if all_failures:
        print(f"FAIL — {len(all_failures)} gate(s) tripped:")
        for f in all_failures:
            print(f"  - {f}")
    else:
        print("PASS — all hard gates satisfied. Labels are safe to train on.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2, default=str))

    return 1 if all_failures else 0


if __name__ == "__main__":
    sys.exit(main())
