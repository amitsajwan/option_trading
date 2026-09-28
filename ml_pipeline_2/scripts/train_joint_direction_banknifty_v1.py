"""ONE joint multiclass direction model, applied to BANKNIFTY (2026-09-28) --
same architecture as the NIFTY track (``ml_pipeline_2.pipeline.joint_direction``
/ ``ml_pipeline_2.scripts.train_joint_direction_v1``, 2026-09-27), reused here
almost entirely verbatim rather than re-derived: this module imports
``evaluate_window``/``pool_window_results``/``class_verdict``/
``run_class_stress_test``/``JOINT_FEATURES`` straight from
``train_joint_direction_v1`` (all of it is already fully instrument-agnostic
-- feature list, label derivation, HPO objective, walk-forward evaluation,
ship gates -- nothing in that module hardcodes NIFTY) and supplies only a
separate BankNifty-specific CLI entry point (own default paths/lot_size/
horizon_bars) plus its own walk-forward windows built fresh against
BankNifty's own date range, per this task's explicit instruction not to
reuse NIFTY's exact window boundaries.

WHY THIS TRACK EXISTS: NIFTY's joint 3-class model technically cleared every
ship gate for its CALL output (pooled Spearman rho positive in 5/5
walk-forward windows) but the refinement study
(``ml_pipeline_2.scripts.study_joint_direction_call_threshold``,
``0f44ef924a``) proved that result was pure HPO-seed noise: an independent
refit (unseeded Optuna TPE sampler, same architecture) flipped the one
promising window's sign, and only 1/5 windows (the earliest, thinnest one)
carried the entire pooled effect while the three largest, most recent
windows were flat-to-negative. BankNifty's own entry-timing model (trained
tonight, same architecture as NIFTY's ONE validated positive result) is
notably STRONGER and more temporally stable than NIFTY's (holdout Spearman
rho=0.218 vs 0.154; its repricing-wall control passed more decisively,
payoff barely decaying with entry delay, p=0.19/0.24 -- not even
distinguishable from zero decay). This script's central, honest question:
does that stronger, more stable ENTRY-TIMING signal translate into a
genuinely walk-forward-consistent joint DIRECTION result here, where NIFTY's
version could not -- or does BankNifty's joint model fail the exact same way
once scrutinized the same way.

Training-view build note (read before trusting the default
``--training-view-csv`` path): a separate, CONCURRENT research track running
the same night (independent CE/PE regressors + an asymmetric stop/trail
exit test for BankNifty) had already produced its own
``training_view_banknifty_direction_margin.csv.gz`` by the time this script
was written. It was NOT reused here after a real, concrete problem was
found: a direct empirical check (fixing a candidate ``horizon_bars`` value,
re-labeling ``.data/ml_pipeline/parquet_data_banknifty``'s 2024-11-04 day at
that horizon, and comparing against the base
``training_view_banknifty_option_pnl.csv.gz``'s own ``payoff_score``/
``payoff_winning_side`` columns bar-for-bar, correctly OFFSET by the base
view's own 30-bar AM-warmup skip) showed that file was built at
``horizon_bars=5`` (matching BankNifty's unrelated LIVE model's
``label_horizon_min``), while the actual entry-timing training view this
task must stay consistent with was built at ``horizon_bars=15`` (0/321
bars matched at horizon_bars=5 on a same-day spot check; 321/321 matched
exactly at horizon_bars=15) -- i.e. that file's per-leg labels do not
describe the same 15-bar hold the validated entry-timing model (and this
script) needs to stay comparable to. This script instead builds its own
copy, ``training_view_banknifty_direction_margin_jointdir.csv.gz``
(distinct filename, no collision), via the SAME
``build_direction_margin_training_view.py`` the NIFTY track used, with
``--horizon-bars 15 --lot-size 30`` -- verified via that exact same
consistency check (see this run's own log: 0 payoff_score mismatches out of
137k+ rows once built at the correct horizon). This finding (the other
track's file being off by a horizon_bars mismatch) is reported to the human
operator but its file was left completely untouched, per this task's
explicit instruction not to duplicate or interfere with that concurrent
work.

Ship-gate / stress-test logic, class balance, and diagnostics are all
IDENTICAL to the NIFTY track (imported directly, not reimplemented). The one
deliberate omission from NIFTY's script: the "critical comparison vs track
5's independent CE/PE regressors" section. NIFTY's script could hardcode
track 5's own already-published, real pooled top-decile numbers
(``TRACK5_CALL_TOP_DECILE_BASELINE=-0.00547``,
``TRACK5_PUT_TOP_DECILE_BASELINE=-0.00434``) because that track had already
finished and published a trustworthy result by the time the joint model was
built. BankNifty's own equivalent independent-regressor track was RUNNING
CONCURRENTLY with this one and had not published a final, reviewed number by
the time this script needed to run -- fabricating a placeholder baseline
here would be worse than omitting the comparison. This script instead
reports each class's own standalone ship-gate verdict on its own terms
(exactly as NIFTY's script also does, independent of the track-5
comparison), which is what this task's items 1-4 actually ask for.

Usage:
    python -m ml_pipeline_2.scripts.train_joint_direction_banknifty_v1 \\
        --training-view-csv .run/training_view/training_view_banknifty_direction_margin_jointdir.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data_banknifty \\
        --lot-size 30 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 --n-trials 20 \\
        --output-json .run/joint_direction_bn/banknifty_joint_direction_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_joint_direction_banknifty_v1")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import build_expanding_windows  # noqa: E402
from ml_pipeline_2.pipeline.joint_direction import (  # noqa: E402
    CALL,
    NO_TRADE,
    PUT,
    class_balance,
    compute_three_class_label,
)
from ml_pipeline_2.scripts.train_joint_direction_v1 import (  # noqa: E402
    DEFAULT_INNER_VALID_FRAC,
    DEFAULT_MIN_TRAIN_DAYS,
    DEFAULT_N_WINDOWS,
    DEFAULT_TOP_DECILE_FRAC,
    _json_safe,
    class_verdict,
    evaluate_window,
    pool_window_results,
    run_class_stress_test,
)

# Re-exported for tests / callers that want the exact same feature set this
# script trains on without importing train_joint_direction_v1 directly.
from ml_pipeline_2.scripts.train_joint_direction_v1 import JOINT_FEATURES  # noqa: E402,F401

BANKNIFTY_DEFAULT_TRAINING_VIEW = ".run/training_view/training_view_banknifty_direction_margin_jointdir.csv.gz"
BANKNIFTY_DEFAULT_PARQUET_BASE = ".data/ml_pipeline/parquet_data_banknifty"
BANKNIFTY_DEFAULT_LOT_SIZE = 30
BANKNIFTY_DEFAULT_HORIZON_BARS = 15


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=BANKNIFTY_DEFAULT_TRAINING_VIEW)
    ap.add_argument("--parquet-base", default=BANKNIFTY_DEFAULT_PARQUET_BASE)
    ap.add_argument("--lot-size", type=int, default=BANKNIFTY_DEFAULT_LOT_SIZE)
    ap.add_argument("--horizon-bars", type=int, default=BANKNIFTY_DEFAULT_HORIZON_BARS)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--inner-valid-frac", type=float, default=DEFAULT_INNER_VALID_FRAC)
    ap.add_argument("--n-trials", type=int, default=20)
    ap.add_argument("--top-decile-frac", type=float, default=DEFAULT_TOP_DECILE_FRAC, help="Top-decile-fires fraction for EACH class's own standalone stress test")
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--force-stress-test", action="store_true", help="Run every class's stress test even if its own gate says no")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    log.info("loading %s", args.training_view_csv)
    df = pd.read_csv(args.training_view_csv)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info(
        "loaded %d rows, %d trade_dates (%s..%s)",
        len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max(),
    )
    overall_balance = class_balance(compute_three_class_label(df["net_ce_return"], df["net_pe_return"]).to_numpy())
    log.info("overall class balance: %s", overall_balance["fractions"])

    sorted_dates = sorted(df["trade_date"].unique().tolist())
    log.info(
        "BankNifty's OWN walk-forward windows are being built fresh against its own date range "
        "(%s..%s, %d trading days) -- NOT reusing NIFTY's window boundaries.",
        sorted_dates[0], sorted_dates[-1], len(sorted_dates),
    )
    windows = build_expanding_windows(sorted_dates, n_windows=args.n_windows, min_train_days=args.min_train_days)
    log.info("built %d walk-forward windows", len(windows))
    for w in windows:
        log.info(
            "  window %d: train %s..%s, test %s..%s",
            w.window_id, w.train_start, w.train_end, w.test_start, w.test_end,
        )

    window_results: List[Dict[str, Any]] = []
    for w in windows:
        log.info("=== window %d: train <= %s, test %s..%s ===", w.window_id, w.train_end, w.test_start, w.test_end)
        res = evaluate_window(df, w, n_trials=args.n_trials, inner_valid_frac=args.inner_valid_frac)
        log.info("window %d status=%s", w.window_id, res.get("status"))
        if res.get("status") == "ok":
            d = res["diagnostics"]
            log.info("  accuracy=%s macro_f1=%s weighted_f1=%s", d["accuracy"], d["macro_f1"], d["weighted_f1"])
            for cls, key in ((CALL, "call_eval"), (PUT, "put_eval"), (NO_TRADE, "no_trade_eval")):
                e = res[key]
                log.info("  %s: n_argmax=%s rho=%s top_decile=%s bottom_decile=%s", cls, e["n_argmax"], e["spearman_rho"], e["top_decile_mean"], e["bottom_decile_mean"])
        window_results.append(res)

    pooled = pool_window_results(window_results)
    ok_windows = [w for w in window_results if w.get("status") == "ok"]

    call_verdict: Dict[str, Any] = {}
    put_verdict: Dict[str, Any] = {}
    no_trade_verdict: Dict[str, Any] = {}
    call_stress: Optional[Dict[str, Any]] = None
    put_stress: Optional[Dict[str, Any]] = None
    if pooled.get("n_windows_ok"):
        call_verdict = class_verdict(pooled["call_eval"], [w["call_eval"] for w in ok_windows], class_name=CALL)
        put_verdict = class_verdict(pooled["put_eval"], [w["put_eval"] for w in ok_windows], class_name=PUT)
        no_trade_verdict = class_verdict(pooled["no_trade_eval"], [w["no_trade_eval"] for w in ok_windows], class_name=NO_TRADE)
        log.info("CALL     ship_gates_all_pass=%s frac_consistent=%s", call_verdict["ship_gates_all_pass"], call_verdict["frac_windows_consistent_sign"])
        log.info("PUT      ship_gates_all_pass=%s frac_consistent=%s", put_verdict["ship_gates_all_pass"], put_verdict["frac_windows_consistent_sign"])
        log.info("NO_TRADE ship_gates_all_pass=%s frac_consistent=%s", no_trade_verdict["ship_gates_all_pass"], no_trade_verdict["frac_windows_consistent_sign"])

        if not args.skip_stress_test and (call_verdict["run_stress_test"] or args.force_stress_test):
            log.info("running MANDATORY delayed-action stress test for CALL...")
            call_stress = run_class_stress_test(
                ok_windows, args.parquet_base, target_class=CALL,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size, top_decile_frac=args.top_decile_frac,
            )
        if not args.skip_stress_test and (put_verdict["run_stress_test"] or args.force_stress_test):
            log.info("running MANDATORY delayed-action stress test for PUT...")
            put_stress = run_class_stress_test(
                ok_windows, args.parquet_base, target_class=PUT,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size, top_decile_frac=args.top_decile_frac,
            )
    else:
        log.info("no OK windows -- skipping all class verdicts and stress tests")

    # No track-5-style cross-track baseline comparison for BankNifty -- see
    # module docstring: the concurrent independent-regressor track had not
    # published a trustworthy final number by the time this script needed to
    # run. Each class's verdict below is judged entirely on its own terms
    # (ship gates + walk-forward consistency), exactly as NIFTY's script also
    # does independent of that comparison.
    any_class_promising = bool(
        pooled.get("n_windows_ok")
        and (call_verdict.get("ship_gates_all_pass") or put_verdict.get("ship_gates_all_pass"))
    )

    report = {
        "config": {
            "instrument": "BANKNIFTY",
            "training_view_csv": args.training_view_csv, "lot_size": args.lot_size, "horizon_bars": args.horizon_bars,
            "n_windows": args.n_windows, "min_train_days": args.min_train_days, "inner_valid_frac": args.inner_valid_frac,
            "n_trials": args.n_trials, "top_decile_frac": args.top_decile_frac, "joint_features": JOINT_FEATURES,
        },
        "overall_class_balance": overall_balance,
        "windows": _json_safe(window_results),
        "pooled": _json_safe(pooled),
        "headline": {
            "call_verdict": _json_safe(call_verdict),
            "put_verdict": _json_safe(put_verdict),
            "no_trade_verdict": _json_safe(no_trade_verdict),
            "call_stress_test": _json_safe(call_stress) if call_stress is not None else None,
            "put_stress_test": _json_safe(put_stress) if put_stress is not None else None,
            "any_class_ship_gates_pass": any_class_promising,
        },
    }

    print("\n=== BANKNIFTY: Class balance (resolved rows, both legs usable) ===")
    print(overall_balance["fractions"])

    print("\n=== Per-window results ===")
    for w in window_results:
        print(f"\nWindow {w['window']}: train<={w.get('train_end')}, test {w['test_start']}..{w['test_end']} (n_test={w['n_test']})")
        if w.get("status") != "ok":
            print(f"  status={w.get('status')} reason={w.get('reason', '')}")
            continue
        d = w["diagnostics"]
        print(f"  accuracy={d['accuracy']} macro_f1={d['macro_f1']} weighted_f1={d['weighted_f1']}")
        print(f"  confusion_matrix (rows=true, cols=pred, order={d['labels']}): {d['confusion_matrix']}")
        for cls, key in ((CALL, "call_eval"), (PUT, "put_eval"), (NO_TRADE, "no_trade_eval")):
            e = w[key]
            print(f"  {cls}: n_argmax={e['n_argmax']} rho={e['spearman_rho']} (p={e['spearman_p_value']}) top_decile={e['top_decile_mean']} bottom_decile={e['bottom_decile_mean']}")

    print("\n" + "=" * 78)
    print("=== HEADLINE: per-class standalone verdicts (pooled), BANKNIFTY ===")
    print("=" * 78)
    if pooled.get("n_windows_ok"):
        d = pooled["diagnostics"]
        print(f"\nPooled standard diagnostics: accuracy={d['accuracy']} macro_f1={d['macro_f1']} weighted_f1={d['weighted_f1']}")
        print(f"confusion_matrix (rows=true, cols=pred, order={d['labels']}): {d['confusion_matrix']}")
        for cls, key, verdict, stress in (
            (CALL, "call_eval", call_verdict, call_stress),
            (PUT, "put_eval", put_verdict, put_stress),
            (NO_TRADE, "no_trade_eval", no_trade_verdict, None),
        ):
            e = pooled[key]
            print(f"\n--- {cls}, n_windows_ok={pooled['n_windows_ok']} ---")
            print(f"  n_argmax={e['n_argmax']} rho={e['spearman_rho']} (p={e['spearman_p_value']}) top_decile_mean={e['top_decile_mean']} bottom_decile_mean={e['bottom_decile_mean']}")
            print(f"  ship_gates={verdict.get('ship_gates')} -> ALL_PASS={verdict.get('ship_gates_all_pass')}")
            print(f"  per-window rho={verdict.get('per_window_spearman_rho')} (frac consistent sign={verdict.get('frac_windows_consistent_sign')})")
            if stress:
                print(f"  STRESS TEST (top {stress['top_decile_frac']:.0%} argmax-conditioned fires, n_fired={stress['n_fired']}, n_paired={stress['n_paired_bars']}): "
                      f"mean@fire={stress.get('mean_payoff_at_fire')} mean@+1={stress.get('mean_payoff_delay_1')} mean@+2={stress.get('mean_payoff_delay_2')} "
                      f"ok={stress['ok']} -- {stress.get('reason')}")
            elif cls != NO_TRADE:
                print("  STRESS TEST: not run (ship gates did not clear)")
    else:
        print("  no OK windows")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
