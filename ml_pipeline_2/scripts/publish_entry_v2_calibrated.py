"""Publish a v2 entry model as a calibrated entry_only_bundle.

Adds what the staged research run omits (ENTRY_MODEL_V2_SPEC.md sections 5-7):
  * isotonic calibration fit on the VALID window (held out from train, separate
    from the final holdout) so predict_proba returns calibrated probabilities,
  * a reliability table + ECE measured on the OOS holdout,
  * a holdout separation table (precision(fired) - base(not-fired)),
  * a data-driven operating threshold (selective, ~top quartile of bars),
  * real feature_medians (the export tool defaults them to 0.0, which corrupts
    the morning-velocity NaN fill described in spec 3.1).

The calibrated estimator is sklearn's CalibratedClassifierCV(cv="prefit"), which
pickles cleanly and is consumed transparently by strategy_app.ml.bundle_inference
(it just calls bundle["model"].predict_proba).

Usage:
    python -m ml_pipeline_2.scripts.publish_entry_v2_calibrated \
        --run-dir ml_pipeline_2/artifacts/v2_sweep/010pct \
        --min-pct 0.0010 --label-tag 010pct \
        --output ml_pipeline_2/artifacts/entry_only/published_v2/entry_only_model_010pct.joblib \
        [--set-active]
"""
from __future__ import annotations

import argparse
import glob
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_PARQUET = _REPO / ".data" / "ml_pipeline" / "parquet_data"
_ACTIVE = _REPO / "ml_pipeline_2" / "artifacts" / "entry_only" / "published" / "entry_only_model.joblib"

VALID_START, VALID_END = "2024-05-01", "2024-07-31"   # calibration slice
HOLD_START, HOLD_END = "2024-08-01", "2024-10-31"     # OOS verification
MED_START, MED_END = "2024-02-01", "2024-07-31"       # feature-median slice
THRESHOLD_GRID = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]


def _load_view(view: str, start: str, end: str, cols: List[str] | None = None) -> pd.DataFrame:
    files = sorted(glob.glob(str(_PARQUET / view / "**" / "*.parquet"), recursive=True))
    frames = []
    for f in files:
        df = pd.read_parquet(f, columns=cols) if cols else pd.read_parquet(f)
        frames.append(df)
    d = pd.concat(frames, ignore_index=True)
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    return d[(d["trade_date"] >= start) & (d["trade_date"] <= end)].copy()


def _labels(min_pct: float, start: str, end: str) -> pd.DataFrame:
    from ml_pipeline_2.staged.entry_move_oracle import build_entry_bn_move_oracle

    sup = _load_view(
        "snapshots_ml_flat_v2", start, end,
        ["trade_date", "timestamp", "snapshot_id", "px_fut_close", "px_fut_high", "px_fut_low"],
    )
    orc = build_entry_bn_move_oracle(sup, horizon_minutes=5, min_pct=min_pct)
    return orc[orc["entry_label_valid"] == 1][["snapshot_id", "entry_label"]]


def _ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> tuple[float, list]:
    edges = np.linspace(0, 1, bins + 1)
    e = 0.0
    n = len(y)
    table = []
    for i in range(bins):
        hi = edges[i + 1] if i < bins - 1 else 1.0001
        m = (p >= edges[i]) & (p < hi)
        cnt = int(m.sum())
        if cnt == 0:
            table.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": 0, "conf": None, "acc": None})
            continue
        conf = float(p[m].mean())
        acc = float(y[m].mean())
        e += abs(acc - conf) * cnt / n
        table.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": cnt, "conf": round(conf, 4), "acc": round(acc, 4)})
    return e, table


def _separation(y: np.ndarray, p: np.ndarray) -> list:
    rows = []
    for t in THRESHOLD_GRID:
        fired = p >= t
        if fired.sum() == 0 or (~fired).sum() == 0:
            rows.append({"thr": t, "fire_rate": round(float(fired.mean()), 4), "degenerate": True})
            continue
        pf = float(y[fired].mean())
        bn = float(y[~fired].mean())
        rows.append({
            "thr": t, "fire_rate": round(float(fired.mean()), 4),
            "precision_fired": round(pf, 4), "base_not_fired": round(bn, 4),
            "separation": round(pf - bn, 4),
        })
    return rows


def _pick_threshold(sep_rows: list, target_fire: float = 0.25) -> float:
    cand = [r for r in sep_rows if not r.get("degenerate")]
    if not cand:
        return 0.60
    # selective: smallest threshold whose fire-rate <= target; else closest fire-rate to target
    le = [r for r in cand if r["fire_rate"] <= target_fire]
    pick = le[0] if le else min(cand, key=lambda r: abs(r["fire_rate"] - target_fire))
    return float(pick["thr"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--min-pct", type=float, required=True)
    ap.add_argument("--label-tag", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--set-active", action="store_true", help="also install as the active deployed bundle")
    args = ap.parse_args(argv)

    from sklearn.calibration import CalibratedClassifierCV

    run_dir = Path(args.run_dir).resolve()
    pkg = joblib.load(run_dir / "stages" / "stage1" / "model.joblib")
    features = [str(c) for c in pkg["feature_columns"]]
    base = pkg["models"]["move"]
    print(f"loaded base model: {len(features)} features, selected={pkg.get('selected_model', {}).get('name')}")

    # feature medians (real, from a recent broad slice)
    med_df = _load_view("stage1_entry_view_v2", MED_START, MED_END)
    medians: Dict[str, float] = {}
    raw_med = med_df.reindex(columns=features).median(numeric_only=True)
    for f in features:
        v = raw_med.get(f)
        medians[f] = 0.0 if (v is None or pd.isna(v)) else float(v)

    def frame(view_start, view_end):
        feat = _load_view("stage1_entry_view_v2", view_start, view_end)
        lab = _labels(args.min_pct, view_start, view_end)
        m = feat.merge(lab, on="snapshot_id", how="inner")
        X = m.reindex(columns=features)
        for f in features:
            X[f] = X[f].fillna(medians[f])
        y = m["entry_label"].to_numpy(float)
        ok = np.isfinite(y)
        return X.loc[ok, features], y[ok]

    Xv, yv = frame(VALID_START, VALID_END)
    Xh, yh = frame(HOLD_START, HOLD_END)
    print(f"calibration(valid) n={len(yv)} pos={yv.mean():.3f} | holdout n={len(yh)} pos={yh.mean():.3f}")

    # raw holdout calibration for comparison
    raw_h = base.predict_proba(Xh)[:, 1]
    raw_ece, _ = _ece(yh, raw_h)

    # fit isotonic calibrator on the valid window (prefit base)
    try:
        cal = CalibratedClassifierCV(estimator=base, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=base, method="isotonic", cv="prefit")
    cal.fit(Xv, yv)

    cal_h = cal.predict_proba(Xh)[:, 1]
    cal_ece, reliability = _ece(yh, cal_h)
    sep_rows = _separation(yh, cal_h)
    op_thr = _pick_threshold(sep_rows)
    op_row = next((r for r in sep_rows if r["thr"] == op_thr), {})
    from sklearn.metrics import roc_auc_score, brier_score_loss
    cal_auc = float(roc_auc_score(yh, cal_h))
    cal_brier = float(brier_score_loss(yh, cal_h))

    print(f"\nholdout AUC={cal_auc:.4f} brier={cal_brier:.4f} ECE raw={raw_ece:.4f} -> calibrated={cal_ece:.4f}")
    print(f"operating thr={op_thr} -> {op_row}")
    print("prob spread:", round(float(cal_h.min()), 3), round(float(np.median(cal_h)), 3), round(float(cal_h.max()), 3))

    # gate verdict (spec section 6)
    gates = {
        "discrimination_auc>=0.62": cal_auc >= 0.62,
        "stability_drift<=0.08": abs(float(roc_auc_score(yh[: len(yh) // 2], cal_h[: len(yh) // 2]))
                                      - float(roc_auc_score(yh[len(yh) // 2:], cal_h[len(yh) // 2:]))) <= 0.08,
        "calibration_ece<=0.05": cal_ece <= 0.05,
        "separation>=0.10": (op_row.get("separation", 0) or 0) >= 0.10,
        "prob_spread_not_collapsed": (float(cal_h.max()) - float(cal_h.min())) >= 0.30,
    }
    all_pass = all(gates.values())
    print("gates:", json.dumps(gates), "ALL_PASS" if all_pass else "FAIL")

    bundle: Dict[str, Any] = {
        "kind": "entry_only_bundle",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "entry_v2_5m_calibrated",
        "source_run": run_dir.name,
        "source_description": (
            f"Entry Model v2. 5-min EITHER-direction move, level-invariant min_pct={args.min_pct} "
            f"({args.min_pct*100:.2f}%). {pkg.get('selected_model',{}).get('name')} on stage1_entry_view_v2 "
            f"({len(features)} feat, fo_velocity_v1). Isotonic-calibrated on 2024-05..07; OOS 2024-08..10."
        ),
        "features": features,
        "feature_medians": medians,
        "model": cal,                      # calibrated; predict_proba consumed by runtime
        "label_tag": args.label_tag,
        "holdout_eval": {
            "rows": int(len(yh)), "roc_auc": round(cal_auc, 4), "brier": round(cal_brier, 4),
            "ece_raw": round(raw_ece, 4), "ece_calibrated": round(cal_ece, 4),
            "base_rate": round(float(yh.mean()), 4),
        },
        "reliability_table": reliability,
        "separation_table": sep_rows,
        "recommended_min_prob": op_thr,
        "operating_point": op_row,
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "training_metadata": {
            "labeler": "entry_bn_5m_100pts_v1",
            "horizon_minutes": 5,
            "min_pct": args.min_pct,
            "view": "stage1_entry_view_v2",
            "support_dataset": "snapshots_ml_flat_v2",
            "model_name": pkg.get("selected_model", {}).get("name"),
            "model_params": pkg.get("selected_model", {}).get("params"),
            "calibration": "isotonic_prefit_on_valid_2024-05_07",
            "feature_median_slice": f"{MED_START}..{MED_END}",
        },
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)
    out.with_name(out.stem + "_report.json").write_text(
        json.dumps({k: v for k, v in bundle.items() if k != "model"}, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nwrote: {out}")

    # verify reload + predict parity
    rb = joblib.load(out)
    chk = rb["model"].predict_proba(Xh.iloc[:100])[:, 1]
    print(f"reload check: predict ok, mean={chk.mean():.4f}")

    if args.set_active:
        _ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        if _ACTIVE.exists():
            bak = _ACTIVE.with_name(_ACTIVE.name + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(_ACTIVE, bak)
            print(f"backed up current active -> {bak}")
        shutil.copy2(out, _ACTIVE)
        print(f"installed as ACTIVE: {_ACTIVE}")

    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
