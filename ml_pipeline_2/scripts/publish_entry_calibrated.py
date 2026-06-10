"""Publish ANY entry S1 research run as a calibrated, ship-gated entry_only_bundle.

Generalization of ``publish_entry_v2_calibrated.py`` for the full-feature retrain
(ENTRY_MODEL_FULLFEATURE_HANDOVER.md). It is feature-set agnostic and label
agnostic, and reads the model's *real* feature contract instead of assuming the
legacy ``feature_columns`` key:

    feature resolution order:
      1. package["feature_columns"]                       (legacy)
      2. package["_model_input_contract"]["required_features"]  (current pipeline)
      3. stages/stage1/feature_contract.json                (on-disk fallback)

    model resolution order:
      1. package["single_target"]["model_key"] in models
      2. models["move"] / models["entry"]
      3. the sole model if exactly one exists

What it adds on top of the raw research run (which omits calibration):
  * isotonic calibration fit on the held-out VALID window (separate from train
    and from the final OOS holdout) so predict_proba returns calibrated probs,
  * reliability table + ECE on the OOS holdout,
  * holdout separation table: precision(fired) - base(not-fired) across thresholds,
  * a data-driven operating threshold (selective, targets a fire-rate),
  * real feature_medians from a recent slice (NaN-safe morning-velocity fill),
  * the five handover ship-gates, with an explicit ALL_PASS verdict.

The label (--min-pct) MUST match the manifest's labels.stage1_entry_move.min_pct
so the calibration/holdout labels reproduce the training target exactly.

Usage:
    python -m ml_pipeline_2.scripts.publish_entry_calibrated \
        --run-dir ml_pipeline_2/artifacts/research/entry_s1_comprehensive_5m_020pct_<ts> \
        --min-pct 0.0020 --label-tag comprehensive_020pct \
        --feature-set-label fo_comprehensive \
        --output ml_pipeline_2/artifacts/entry_only/published_comprehensive/entry_only_model_020pct.joblib \
        [--target-fire 0.25] [--set-active]
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
THRESHOLD_GRID = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]


def _load_view(view: str, start: str, end: str, cols: List[str] | None = None) -> pd.DataFrame:
    files = sorted(glob.glob(str(_PARQUET / view / "**" / "*.parquet"), recursive=True))
    if not files:
        raise FileNotFoundError(f"no parquet files for view {view!r} under {_PARQUET}")
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


def _resolve_features(pkg: Dict[str, Any], run_dir: Path) -> List[str]:
    feats = [str(c) for c in list(pkg.get("feature_columns") or [])]
    if feats:
        return feats
    contract = dict(pkg.get("_model_input_contract") or {})
    feats = [str(c) for c in list(contract.get("required_features") or [])]
    if feats:
        return feats
    cpath = run_dir / "stages" / "stage1" / "feature_contract.json"
    if cpath.is_file():
        disk = json.loads(cpath.read_text(encoding="utf-8"))
        feats = [str(c) for c in list(disk.get("required_features") or [])]
    if not feats:
        raise ValueError("cannot resolve feature contract from package or feature_contract.json")
    return feats


def _resolve_base_model(pkg: Dict[str, Any]) -> Any:
    models = dict(pkg.get("models") or {})
    if not models:
        raise ValueError("package has no models")
    single = pkg.get("single_target") if isinstance(pkg.get("single_target"), dict) else {}
    key = str((single or {}).get("model_key") or "").strip()
    if key and key in models:
        return models[key]
    for k in ("move", "entry"):
        if k in models:
            return models[k]
    if len(models) == 1:
        return next(iter(models.values()))
    raise ValueError(f"cannot resolve base model from keys: {sorted(models.keys())}")


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


def _entries_per_day(p: np.ndarray, dates: np.ndarray, thr: float) -> dict:
    fired = p >= thr
    if fired.sum() == 0:
        return {"thr": thr, "mean": 0.0, "median": 0.0, "p10": 0.0, "p90": 0.0}
    per_day = pd.Series(fired.astype(int)).groupby(pd.Series(dates)).sum()
    return {
        "thr": round(float(thr), 3),
        "mean": round(float(per_day.mean()), 2),
        "median": round(float(per_day.median()), 2),
        "p10": round(float(per_day.quantile(0.10)), 2),
        "p90": round(float(per_day.quantile(0.90)), 2),
    }


def _pick_threshold(sep_rows: list, target_fire: float) -> float:
    cand = [r for r in sep_rows if not r.get("degenerate")]
    if not cand:
        return 0.60
    le = [r for r in cand if r["fire_rate"] <= target_fire]
    pick = le[0] if le else min(cand, key=lambda r: abs(r["fire_rate"] - target_fire))
    return float(pick["thr"])


def _drop_outlier_separation(y: np.ndarray, p: np.ndarray, thr: float) -> dict:
    """Robustness: precision among fired bars after removing the most-confident
    fired positives. A separation that craters under drop-top is noise."""
    fired = p >= thr
    if fired.sum() < 5:
        return {"fired": int(fired.sum()), "precision": None, "drop_top1": None, "drop_top3": None}
    idx = np.where(fired)[0]
    order = np.argsort(-p[idx])           # most confident first
    base = float(y[idx].mean())
    def _prec_excluding(k: int) -> float:
        keep = idx[order[k:]]
        return float(y[keep].mean()) if len(keep) else float("nan")
    return {
        "fired": int(fired.sum()),
        "precision": round(base, 4),
        "drop_top1": round(_prec_excluding(1), 4),
        "drop_top3": round(_prec_excluding(3), 4),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--min-pct", type=float, required=True, help="MUST match the manifest stage1_entry_move.min_pct")
    ap.add_argument("--label-tag", required=True)
    ap.add_argument("--feature-set-label", default="fo_comprehensive")
    ap.add_argument("--view", default="stage1_entry_view_v2")
    ap.add_argument("--output", required=True)
    ap.add_argument("--target-fire", type=float, default=0.25, help="desired holdout fire-rate for the operating threshold")
    ap.add_argument("--set-active", action="store_true", help="also install as the active deployed bundle")
    args = ap.parse_args(argv)

    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import roc_auc_score, brier_score_loss

    run_dir = Path(args.run_dir).resolve()
    pkg = joblib.load(run_dir / "stages" / "stage1" / "model.joblib")
    if not isinstance(pkg, dict):
        raise ValueError(f"expected dict package at {run_dir}/stages/stage1/model.joblib")
    if pkg.get("_bypass_stage1"):
        raise ValueError("stage1 was bypassed — nothing to publish")

    features = _resolve_features(pkg, run_dir)
    base = _resolve_base_model(pkg)
    selected = dict(pkg.get("selected_model") or {})
    feature_set = str(pkg.get("selected_feature_set") or args.feature_set_label)
    print(f"loaded base model: {len(features)} features, feature_set={feature_set}, selected={selected.get('name')}")

    # feature medians (real, from a recent broad slice)
    med_df = _load_view(args.view, MED_START, MED_END)
    raw_med = med_df.reindex(columns=features).median(numeric_only=True)
    medians: Dict[str, float] = {}
    for f in features:
        v = raw_med.get(f)
        medians[f] = 0.0 if (v is None or pd.isna(v)) else float(v)

    def frame(view_start, view_end):
        feat = _load_view(args.view, view_start, view_end)
        lab = _labels(args.min_pct, view_start, view_end)
        m = feat.merge(lab, on="snapshot_id", how="inner")
        X = m.reindex(columns=features)
        for f in features:
            X[f] = X[f].fillna(medians[f])
        y = m["entry_label"].to_numpy(float)
        dates = pd.to_datetime(m["trade_date"]).dt.date.to_numpy()
        ok = np.isfinite(y)
        return X.loc[ok, features], y[ok], dates[ok]

    Xv, yv, _ = frame(VALID_START, VALID_END)
    Xh, yh, dh = frame(HOLD_START, HOLD_END)
    print(f"calibration(valid) n={len(yv)} pos={yv.mean():.3f} | holdout n={len(yh)} pos={yh.mean():.3f}")

    raw_h = base.predict_proba(Xh)[:, 1]
    raw_ece, _ = _ece(yh, raw_h)

    try:
        cal = CalibratedClassifierCV(estimator=base, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=base, method="isotonic", cv="prefit")
    cal.fit(Xv, yv)

    cal_h = cal.predict_proba(Xh)[:, 1]
    cal_ece, reliability = _ece(yh, cal_h)
    sep_rows = _separation(yh, cal_h)
    op_thr = _pick_threshold(sep_rows, args.target_fire)
    op_row = next((r for r in sep_rows if r["thr"] == op_thr), {})
    cal_auc = float(roc_auc_score(yh, cal_h))
    cal_brier = float(brier_score_loss(yh, cal_h))
    drop_outlier = _drop_outlier_separation(yh, cal_h, op_thr)
    epd = [_entries_per_day(cal_h, dh, t) for t in (op_thr, 0.50, 0.60)]

    half = len(yh) // 2
    drift = abs(float(roc_auc_score(yh[:half], cal_h[:half])) - float(roc_auc_score(yh[half:], cal_h[half:])))

    print(f"\nholdout AUC={cal_auc:.4f} brier={cal_brier:.4f} ECE raw={raw_ece:.4f} -> cal={cal_ece:.4f} drift={drift:.4f}")
    print(f"operating thr={op_thr} -> {op_row}")
    print(f"drop-outlier @thr: {drop_outlier}")
    print(f"entries/day: {epd}")
    print("prob spread:", round(float(cal_h.min()), 3), round(float(np.median(cal_h)), 3), round(float(cal_h.max()), 3))

    gates = {
        "discrimination_auc>=0.62": cal_auc >= 0.62,
        "stability_drift<=0.08": drift <= 0.08,
        "calibration_ece<=0.05": cal_ece <= 0.05,
        "separation>=0.10": (op_row.get("separation", 0) or 0) >= 0.10,
        "prob_spread_not_collapsed": (float(cal_h.max()) - float(cal_h.min())) >= 0.30,
        "drop_top3_separation_holds": (
            drop_outlier.get("drop_top3") is not None
            and drop_outlier.get("precision") is not None
            and (drop_outlier["drop_top3"] - float(yh.mean())) >= 0.05
        ),
    }
    all_pass = all(gates.values())
    print("gates:", json.dumps(gates), "ALL_PASS" if all_pass else "FAIL")

    bundle: Dict[str, Any] = {
        "kind": "entry_only_bundle",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "entry_comprehensive_5m_calibrated",
        "source_run": run_dir.name,
        "source_description": (
            f"Full-feature entry model. 5-min EITHER-direction move, level-invariant "
            f"min_pct={args.min_pct} ({args.min_pct*100:.2f}%). {selected.get('name')} on {args.view} "
            f"({len(features)} feat, {feature_set}). Isotonic-calibrated on 2024-05..07; OOS 2024-08..10."
        ),
        "features": features,
        "feature_medians": medians,
        "model": cal,
        "label_tag": args.label_tag,
        "holdout_eval": {
            "rows": int(len(yh)), "roc_auc": round(cal_auc, 4), "brier": round(cal_brier, 4),
            "ece_raw": round(raw_ece, 4), "ece_calibrated": round(cal_ece, 4),
            "auc_half_split_drift": round(drift, 4), "base_rate": round(float(yh.mean()), 4),
        },
        "reliability_table": reliability,
        "separation_table": sep_rows,
        "drop_outlier_robustness": drop_outlier,
        "entries_per_day": epd,
        "recommended_min_prob": op_thr,
        "operating_point": op_row,
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "training_metadata": {
            "labeler": "entry_bn_5m_100pts_v1",
            "horizon_minutes": 5,
            "min_pct": args.min_pct,
            "view": args.view,
            "support_dataset": "snapshots_ml_flat_v2",
            "feature_set": feature_set,
            "model_name": selected.get("name"),
            "model_params": selected.get("params"),
            "calibration": "isotonic_prefit_on_valid_2024-05_07",
            "feature_median_slice": f"{MED_START}..{MED_END}",
            "target_fire": args.target_fire,
        },
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)
    out.with_name(out.stem + "_report.json").write_text(
        json.dumps({k: v for k, v in bundle.items() if k != "model"}, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nwrote: {out}")

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
