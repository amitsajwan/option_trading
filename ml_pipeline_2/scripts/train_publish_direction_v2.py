"""Train + isotonic-calibrate + export a direction_only_bundle (CE vs PE).

Direction is a thin, near-random target (~0.50 base). The diagnostic
(docs/DIRECTION_MODEL_V2.md) found the levers that actually help: a SHORTER
horizon (3m > 5m > 10m), dropping oracle_rolling_* (no signal + live-inference
gap), and that the curated fo_direction_entry_context_v1 regex misses the
fut_return_* momentum columns that carry the most univariate signal. So we
select features empirically from the stage2 view rather than by the old regex.

Bundle is consumed by strategy_app.ml.direction_ml_policy as a SOFT overlay
(blend weight 0.40) or opt-in filter — never a hard gate. Positive class
(predict_proba[:,1]) = P(CE / up).

Usage:
    python -m ml_pipeline_2.scripts.train_publish_direction_v2 \
        --horizon 3 --output ml_pipeline_2/artifacts/direction_only/published_v2/direction_only_model_3m.joblib [--set-active]
"""
from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_PARQ = _REPO / ".data" / "ml_pipeline" / "parquet_data"
_ACTIVE = _REPO / "ml_pipeline_2" / "artifacts" / "direction_only" / "published" / "direction_only_model.joblib"

TR = ("2022-01-01", "2024-04-30")
VAL = ("2024-05-01", "2024-07-31")
HO = ("2024-08-01", "2024-10-31")
MED = ("2024-02-01", "2024-07-31")
LEAK = {
    "snapshot_id", "trade_date", "timestamp", "year", "direction_up", "direction_label",
    "entry_label", "entry_label_valid", "entry_up_move_pct", "entry_down_move_pct",
    "entry_threshold_pct", "recipe_label", "best_net_return_after_cost",
    "move_label", "move_label_valid", "move_first_hit_side",
}
REGIME_COLS = ["ctx_regime_trend_up", "ctx_regime_trend_down"]


def _load(view: str, s: str, e: str, cols: List[str] | None = None) -> pd.DataFrame:
    fr = [pd.read_parquet(f, columns=cols) if cols else pd.read_parquet(f)
          for f in sorted(glob.glob(str(_PARQ / view / "**" / "*.parquet"), recursive=True))]
    d = pd.concat(fr, ignore_index=True)
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    return d[(d["trade_date"] >= s) & (d["trade_date"] <= e)].copy()


def _label(s: str, e: str, horizon: int) -> pd.DataFrame:
    from ml_pipeline_2.staged.entry_move_oracle import build_entry_bn_move_oracle
    sup = _load("snapshots_ml_flat_v2", s, e,
                ["trade_date", "timestamp", "snapshot_id", "px_fut_close", "px_fut_high", "px_fut_low"])
    o = build_entry_bn_move_oracle(sup, horizon_minutes=horizon, min_pct=0.0010)
    o = o[o["entry_label_valid"] == 1]
    return o[["snapshot_id", "direction_up"]]


def _ece(y, p, bins=10):
    edges = np.linspace(0, 1, bins + 1); e = 0.0; n = len(y); tbl = []
    for i in range(bins):
        hi = edges[i + 1] if i < bins - 1 else 1.0001
        m = (p >= edges[i]) & (p < hi); c = int(m.sum())
        if c == 0:
            tbl.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": 0}); continue
        conf = float(p[m].mean()); acc = float(y[m].mean()); e += abs(acc - conf) * c / n
        tbl.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": c, "conf": round(conf, 4), "acc": round(acc, 4)})
    return e, tbl


def _confidence_table(y, p):
    """How usable as an overlay: at each confidence band, coverage + directional accuracy."""
    rows = []
    for t in [0.50, 0.55, 0.60, 0.65, 0.70]:
        # decisive = model says CE (p>=t) or PE (p<=1-t)
        ce = p >= t; pe = p <= (1 - t)
        dec = ce | pe
        if dec.sum() == 0:
            rows.append({"min_prob": t, "coverage": 0.0}); continue
        # predicted up where ce, down where pe; correct when matches y
        correct = np.sum((ce & (y == 1)) | (pe & (y == 0)))
        rows.append({
            "min_prob": t,
            "coverage": round(float(dec.mean()), 4),
            "decisive_accuracy": round(float(correct) / float(dec.sum()), 4),
            "ce_calls": int(ce.sum()), "pe_calls": int(pe.sum()),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--set-active", action="store_true")
    args = ap.parse_args(argv)

    import lightgbm as lgb
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import roc_auc_score, brier_score_loss

    # feature universe = stage2 view numeric cols minus leak/oracle_rolling/best_*
    v_tr = _load("stage2_direction_view_v2", *TR)
    feats = [c for c in v_tr.columns
             if c not in LEAK and not c.startswith("best_") and not c.startswith("oracle_rolling")
             and "win_rate" not in c and pd.api.types.is_numeric_dtype(v_tr[c])]
    print(f"horizon={args.horizon}m  features={len(feats)}")

    med_df = _load("stage2_direction_view_v2", *MED)
    medians = {f: (0.0 if pd.isna(med_df[f].median()) else float(med_df[f].median())) for f in feats if f in med_df}
    for f in feats:
        medians.setdefault(f, 0.0)

    def frame(view_s, view_e):
        fv = _load("stage2_direction_view_v2", view_s, view_e)
        lab = _label(view_s, view_e, args.horizon)
        m = fv.merge(lab, on="snapshot_id", how="inner")
        X = m.reindex(columns=feats).astype(float)
        for f in feats:
            X[f] = X[f].fillna(medians[f])
        return m, X, m["direction_up"].astype(int).to_numpy()

    m_tr, Xtr, ytr = frame(*TR)
    m_va, Xva, yva = frame(*VAL)
    m_ho, Xho, yho = frame(*HO)
    print(f"train n={len(ytr)} up={ytr.mean():.3f} | valid n={len(yva)} | holdout n={len(yho)} up={yho.mean():.3f}")

    base = lgb.LGBMClassifier(n_estimators=300, max_depth=4, learning_rate=0.03,
                              subsample=0.85, colsample_bytree=0.85, reg_lambda=2.0, n_jobs=8, verbose=-1)
    base.fit(Xtr, ytr)
    raw_h = base.predict_proba(Xho)[:, 1]
    raw_ece, _ = _ece(yho, raw_h)

    try:
        cal = CalibratedClassifierCV(estimator=base, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=base, method="isotonic", cv="prefit")
    cal.fit(Xva, yva)
    ph = cal.predict_proba(Xho)[:, 1]
    auc = float(roc_auc_score(yho, ph)); brier = float(brier_score_loss(yho, ph))
    cal_ece, reliability = _ece(yho, ph)
    half = len(yho) // 2
    drift = abs(roc_auc_score(yho[:half], ph[:half]) - roc_auc_score(yho[half:], ph[half:]))
    conf_tbl = _confidence_table(yho, ph)

    # regime breakdown on holdout
    regime = {}
    for col in REGIME_COLS:
        if col in m_ho.columns:
            msk = m_ho[col].fillna(0).to_numpy() > 0.5
            if msk.sum() > 300 and len(np.unique(yho[msk])) > 1:
                regime[col] = {"n": int(msk.sum()), "auc": round(float(roc_auc_score(yho[msk], ph[msk])), 4),
                               "up_rate": round(float(yho[msk].mean()), 4)}

    gates = {
        "auc>=0.55": auc >= 0.55,
        "calibration_ece<=0.05": cal_ece <= 0.05,
        "stability_drift<=0.08": drift <= 0.08,
    }
    all_pass = all(gates.values())
    print(f"holdout AUC={auc:.4f} brier={brier:.4f} ECE {raw_ece:.4f}->{cal_ece:.4f} drift={drift:.4f}")
    print("confidence table:", json.dumps(conf_tbl))
    print("regime:", json.dumps(regime))
    print("gates:", json.dumps(gates), "ALL_PASS" if all_pass else "FAIL")

    bundle: Dict[str, Any] = {
        "kind": "direction_only_bundle",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": f"direction_v2_{args.horizon}m_calibrated",
        "source_description": (
            f"Direction v2 (CE vs PE). Label = futures up-move wins over {args.horizon}min "
            f"(direction_up). LGBM on stage2_direction_view_v2 ({len(feats)} feat, empirically selected, "
            f"oracle_rolling dropped). Isotonic-calibrated on 2024-05..07; OOS 2024-08..10. "
            f"SOFT OVERLAY ONLY (direction_ml_policy weight~0.40 or filter), not a hard gate."
        ),
        "features": feats,
        "feature_medians": medians,
        "model": cal,
        "horizon_minutes": args.horizon,
        "holdout_eval": {"rows": int(len(yho)), "roc_auc": round(auc, 4), "brier": round(brier, 4),
                          "ece_raw": round(raw_ece, 4), "ece_calibrated": round(cal_ece, 4),
                          "drift": round(drift, 4), "up_rate": round(float(yho.mean()), 4)},
        "reliability_table": reliability,
        "confidence_table": conf_tbl,
        "regime_breakdown": regime,
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "recommended_usage": {"mode": "soft_overlay", "DIRECTION_ML_WEIGHT": 0.40,
                              "DIRECTION_ML_FILTER_MIN_PROB": "off until shadow-validated"},
        "training_metadata": {"labeler": "direction_up_move_oracle", "horizon_minutes": args.horizon,
                              "view": "stage2_direction_view_v2", "support_dataset": "snapshots_ml_flat_v2",
                              "model": "lgbm depth4 n300 lr0.03", "calibration": "isotonic_prefit_valid_2024-05_07"},
    }
    out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)
    out.with_name(out.stem + "_report.json").write_text(
        json.dumps({k: v for k, v in bundle.items() if k != "model"}, indent=2, default=str), encoding="utf-8")
    print(f"wrote: {out}")
    rb = joblib.load(out); print("reload check predict mean:", round(float(rb["model"].predict_proba(Xho.iloc[:100])[:, 1].mean()), 4))

    if args.set_active:
        import shutil
        _ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        if _ACTIVE.exists():
            shutil.copy2(_ACTIVE, _ACTIVE.with_name(_ACTIVE.name + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")))
        shutil.copy2(out, _ACTIVE)
        print(f"installed ACTIVE: {_ACTIVE}")
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
