#!/usr/bin/env python3
"""R1-S1: VIX field audit — verify snapshot.vix_current is populated in IS parquet quarters.

Usage (on VM):
    source /opt/option_trading/.venv/bin/activate
    python ops/gcp/audit_vix_field.py
    python ops/gcp/audit_vix_field.py --base /opt/option_trading/.data/ml_pipeline/parquet_data

Exit codes:
    0  — VIX data present and populated for all IS quarters → R1-S1 PASS, R1-S2 unblocked
    1  — VIX file missing or sparsely populated → R1-S1 FAIL, investigate before running R1-S2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    _REPO = Path(__file__).resolve().parents[2]
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE

IS_START = "2020-07-01"
IS_END = "2023-12-31"

IS_QUARTERS = [
    ("2020-Q3", "2020-07-01", "2020-09-30"),
    ("2020-Q4", "2020-10-01", "2020-12-31"),
    ("2021-Q1", "2021-01-01", "2021-03-31"),
    ("2021-Q2", "2021-04-01", "2021-06-30"),
    ("2021-Q3", "2021-07-01", "2021-09-30"),
    ("2021-Q4", "2021-10-01", "2021-12-31"),
    ("2022-Q1", "2022-01-01", "2022-03-31"),
    ("2022-Q2", "2022-04-01", "2022-06-30"),
    ("2022-Q3", "2022-07-01", "2022-09-30"),
    ("2022-Q4", "2022-10-01", "2022-12-31"),
    ("2023-Q1", "2023-01-01", "2023-03-31"),
    ("2023-Q2", "2023-04-01", "2023-06-30"),
    ("2023-Q3", "2023-07-01", "2023-09-30"),
    ("2023-Q4", "2023-10-01", "2023-12-31"),
]

VIX_THRESHOLD = 16.0
MIN_FILL_RATE = 0.90


def _pct(num: float, den: float) -> str:
    if den == 0:
        return "N/A"
    return f"{num / den * 100:.1f}%"


def _run_audit(base_path: Path) -> int:
    print(f"\n{'=' * 60}")
    print(f"R1-S1 VIX Field Audit")
    print(f"Parquet base: {base_path}")
    print(f"IS window   : {IS_START} → {IS_END}")
    print(f"{'=' * 60}\n")

    try:
        store = ParquetStore(base_path)
    except FileNotFoundError as exc:
        print(f"[FAIL] Cannot open parquet store: {exc}")
        return 1

    summary = store.summary()

    print("=== Dataset summary ===")
    for key, val in summary.items():
        print(f"  {key:30s}: {val}")
    print()

    vix_status = summary.get("vix", {})
    if isinstance(vix_status, dict) and vix_status.get("status") in {"missing", "error"}:
        print(f"[FAIL] VIX parquet file status: {vix_status}")
        print("       Expected at: <parquet_base>/vix/vix.parquet")
        print("       R1-S2 is BLOCKED — cannot run R1S replay without VIX data.")
        return 1

    vix_df = store.vix()
    if len(vix_df) == 0:
        print("[FAIL] vix.parquet exists but is EMPTY.")
        return 1

    vix_df["trade_date"] = pd.to_datetime(vix_df["trade_date"], errors="coerce")
    vix_df = vix_df.dropna(subset=["trade_date"]).sort_values("trade_date")

    is_vix = vix_df[
        (vix_df["trade_date"] >= IS_START) & (vix_df["trade_date"] <= IS_END)
    ]
    print(f"VIX rows in IS window : {len(is_vix)} days")
    if len(is_vix) == 0:
        print(f"[FAIL] VIX parquet covers {vix_df['trade_date'].min().date()} → {vix_df['trade_date'].max().date()}")
        print("       No rows overlap with IS window. Backfill needed.")
        return 1

    vix_null = is_vix["vix_close"].isna().sum()
    fill_rate = (len(is_vix) - vix_null) / len(is_vix) if len(is_vix) > 0 else 0.0
    print(f"VIX close fill rate   : {_pct(len(is_vix) - vix_null, len(is_vix))} ({vix_null} nulls)")

    print(f"\n{'=' * 60}")
    print("Per-quarter VIX coverage")
    print(f"{'Quarter':<10} {'Days':>5} {'VIX rows':>9} {'Fill%':>7} {'Avg VIX':>9} {'<16?':>6} {'Status'}")
    print(f"{'-' * 60}")

    any_quarter_fail = False
    for (label, q_start, q_end) in IS_QUARTERS:
        q = is_vix[
            (is_vix["trade_date"] >= q_start) & (is_vix["trade_date"] <= q_end)
        ]
        n = len(q)
        n_null = q["vix_close"].isna().sum() if n > 0 else 0
        n_filled = n - n_null
        fill = n_filled / n if n > 0 else 0.0
        avg_vix = q["vix_close"].mean() if n_filled > 0 else float("nan")
        below_16_pct = (q["vix_close"] < VIX_THRESHOLD).sum() / n if n > 0 else 0.0
        status = "OK" if (n > 0 and fill >= MIN_FILL_RATE) else ("MISSING" if n == 0 else "SPARSE")
        if status != "OK":
            any_quarter_fail = True
        avg_str = f"{avg_vix:.2f}" if avg_vix == avg_vix else "N/A"
        print(
            f"{label:<10} {n:>5} {n_filled:>9} {_pct(n_filled, n):>7} "
            f"{avg_str:>9} {_pct(int((q['vix_close'] < VIX_THRESHOLD).sum()), n):>6} {status}"
        )

    print(f"\n{'=' * 60}")
    print("VIX integration check (snapshot ml_flat columns)")
    snap_check = _check_snapshot_vix_columns(store)
    for line in snap_check["lines"]:
        print(f"  {line}")
    snap_ok = snap_check["ok"]
    print()

    vix_parquet_pass = (not any_quarter_fail) and (fill_rate >= MIN_FILL_RATE)
    if vix_parquet_pass:
        print("[PASS] R1-S1 VIX audit complete.")
        print("       vix.parquet populated across all IS quarters ≥ 90% fill rate.")
        if not snap_ok:
            print("       NOTE: snapshots_ml_flat vix columns have issues (see above).")
            print("             Re-run snapshot batch build to regenerate ml_flat before R1-S2.")
        else:
            print("       snapshots_ml_flat vix columns OK.")
        print("       → R1-S2 IS replay (Gate 1) is now UNBLOCKED.")
        return 0
    else:
        print("[FAIL] R1-S1 VIX audit found issues (see above).")
        print("       Resolve before running R1-S2.")
        return 1


def _check_snapshot_vix_columns(store: ParquetStore) -> dict[str, Any]:
    lines: list[str] = []
    ok = True
    try:
        import duckdb  # type: ignore[import-untyped]

        con = duckdb.connect(":memory:")
        ml_flat_glob = (store.base_path / "snapshots_ml_flat" / "**" / "data.parquet").as_posix()
        try:
            schema_df = con.execute(
                f"SELECT * FROM read_parquet('{ml_flat_glob}', union_by_name=true) LIMIT 0"
            ).df()
            cols = set(schema_df.columns)
            for field in ("vix_prev_close", "is_high_vix_day"):
                present = field in cols
                lines.append(f"{'OK' if present else 'MISSING':6s}  snapshots_ml_flat column: {field}")
                if not present:
                    ok = False

            if "vix_prev_close" in cols:
                sample = con.execute(
                    f"""
                    SELECT
                        COUNT(*) AS total_rows,
                        COUNT(vix_prev_close) AS vix_filled,
                        MIN(trade_date) AS first_date,
                        MAX(trade_date) AS last_date
                    FROM read_parquet('{ml_flat_glob}', union_by_name=true)
                    WHERE trade_date BETWEEN '{IS_START}' AND '{IS_END}'
                    """
                ).df()
                if len(sample):
                    row = sample.iloc[0]
                    total = int(row["total_rows"])
                    filled = int(row["vix_filled"])
                    fill_pct = filled / total * 100 if total > 0 else 0.0
                    lines.append(
                        f"ml_flat IS rows={total}, vix_prev_close filled={filled} "
                        f"({fill_pct:.1f}%) "
                        f"[{row['first_date']} → {row['last_date']}]"
                    )
                    if fill_pct < MIN_FILL_RATE * 100:
                        lines.append(
                            f"WARN: fill rate {fill_pct:.1f}% < {MIN_FILL_RATE * 100:.0f}% threshold"
                        )
                        ok = False
        except Exception as exc:
            lines.append(f"snapshots_ml_flat not found or unreadable: {exc}")
            lines.append("(snapshots_ml_flat may not exist yet — vix.parquet check above is sufficient for R1-S1)")
        finally:
            con.close()
    except ImportError:
        lines.append("duckdb not available; skipping ml_flat column check")

    return {"lines": lines, "ok": ok}


def main() -> None:
    parser = argparse.ArgumentParser(description="R1-S1 VIX field audit")
    parser.add_argument(
        "--base",
        default=str(DEFAULT_HISTORICAL_PARQUET_BASE),
        help="Parquet base directory (default: %(default)s)",
    )
    args = parser.parse_args()
    sys.exit(_run_audit(Path(args.base)))


if __name__ == "__main__":
    main()
