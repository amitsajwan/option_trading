import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

from .profiles import FEATURE_PROFILE_ALL, FEATURE_PROFILES, apply_feature_profile
from .regime import attach_regime_features
from ..pipeline_layout import FEATURES_ROOT, LABELED_ROOT
from ..train_baseline import IDENTITY_COLUMNS, LABEL_COLUMNS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _ensure_supervised_columns(df: pd.DataFrame) -> None:
    required = [
        "ce_label",
        "ce_label_valid",
        "pe_label",
        "pe_label_valid",
        "ce_forward_return",
        "pe_forward_return",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            "missing supervised columns in input split: "
            + ",".join(missing)
            + ". Run label stage first: python -m ml_pipeline.feature.label_stage"
        )


def _prepare_split(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    # Ensure expiry-time features exist for regime logic, regardless of source format.
    if "dte_days" not in out.columns and ("expiry_code" in out.columns and "trade_date" in out.columns):
        ec = out["expiry_code"].astype(str).str.upper().str.strip()
        exp = pd.to_datetime(ec, format="%Y%m%d", errors="coerce")
        miss = exp.isna()
        if miss.any():
            exp_alt = pd.to_datetime(ec.where(miss), format="%d%b%y", errors="coerce")
            exp = exp.where(~miss, exp_alt)
        td = pd.to_datetime(out["trade_date"], errors="coerce")
        dte = (exp.dt.normalize() - td.dt.normalize()).dt.days
        dte = pd.to_numeric(dte, errors="coerce").where(lambda s: s >= 0)
        out["dte_days"] = dte
        out["is_expiry_day"] = (out["dte_days"] == 0).astype(float)
        out["is_near_expiry"] = ((out["dte_days"] >= 0) & (out["dte_days"] <= 1)).astype(float)
    return attach_regime_features(out)


def run_feature_stage(
    *,
    train_path: Path,
    valid_path: Path,
    eval_path: Path,
    profile: str,
    out_root: Path,
) -> Dict[str, object]:
    for p, name in ((train_path, "train"), (valid_path, "valid"), (eval_path, "eval")):
        if not p.exists():
            raise FileNotFoundError(f"{name} split not found: {p}")

    train_raw = pd.read_parquet(train_path)
    valid_raw = pd.read_parquet(valid_path)
    eval_raw = pd.read_parquet(eval_path)

    _ensure_supervised_columns(train_raw)
    _ensure_supervised_columns(valid_raw)
    _ensure_supervised_columns(eval_raw)

    train_df = _prepare_split(train_raw)
    valid_df = _prepare_split(valid_raw)
    eval_df = _prepare_split(eval_raw)

    id_cols = [c for c in IDENTITY_COLUMNS if c in train_df.columns]
    label_cols = [c for c in LABEL_COLUMNS if c in train_df.columns]
    excluded = set(id_cols) | set(label_cols)

    # Guard against aux-horizon label/return columns leaking as features.
    # LABEL_COLUMNS only contains base names (ce_label, ce_forward_return, etc.).
    # When label_stage runs with --aux-horizons, it also writes ce_label_h15m,
    # ce_forward_return_h15m etc. — these are numeric and would pass the excluded
    # check, creating severe target leakage. Profiles._is_always_excluded covers this.
    from .profiles import _is_always_excluded  # noqa: PLC0415
    numeric_cols = [
        c for c in train_df.select_dtypes(include=[np.number]).columns
        if c not in excluded and not _is_always_excluded(c)
    ]
    selected_features = apply_feature_profile(numeric_cols, feature_profile=profile)
    keep_cols = list(dict.fromkeys(id_cols + label_cols + selected_features))

    def _project(df: pd.DataFrame, split_name: str = "") -> pd.DataFrame:
        missing = [c for c in keep_cols if c not in df.columns]
        if missing:
            import warnings
            warnings.warn(
                f"feature stage: {split_name} split is missing {len(missing)} columns "
                f"that exist in train (will be NaN-filled): {missing[:10]}",
                RuntimeWarning,
            )
        out = df.reindex(columns=keep_cols)
        return out

    train_out = _project(train_df, "train")
    valid_out = _project(valid_df, "valid")
    eval_out = _project(eval_df, "eval")

    profile_root = out_root / str(profile)
    profile_root.mkdir(parents=True, exist_ok=True)
    outputs: Dict[str, str] = {}
    rows: Dict[str, int] = {}
    for split_name, split_df in (("train", train_out), ("valid", valid_out), ("eval", eval_out)):
        out_path = profile_root / f"{split_name}.parquet"
        split_df.to_parquet(out_path, index=False)
        outputs[f"{split_name}_parquet"] = str(out_path).replace("\\", "/")
        rows[split_name] = int(len(split_df))

    lineage = {
        "created_at_utc": _utc_now(),
        "feature_profile": str(profile),
        "inputs": {
            "train_parquet": str(train_path).replace("\\", "/"),
            "valid_parquet": str(valid_path).replace("\\", "/"),
            "eval_parquet": str(eval_path).replace("\\", "/"),
        },
        "selected_feature_columns": [str(c) for c in selected_features],
        "identity_columns": id_cols,
        "label_columns": label_cols,
        "rows": rows,
        "outputs": outputs,
    }
    lineage_path = profile_root / "lineage.json"
    _write_json(lineage_path, lineage)
    return {
        "created_at_utc": _utc_now(),
        "profile": str(profile),
        "rows": rows,
        "outputs": outputs,
        "lineage_json": str(lineage_path).replace("\\", "/"),
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Feature stage: build model-ready train/valid/eval features from labeled splits."
    )
    parser.add_argument(
        "--train",
        default=str((LABELED_ROOT / "core_v2" / "train.parquet")).replace("\\", "/"),
        help="Labeled train split parquet",
    )
    parser.add_argument(
        "--valid",
        default=str((LABELED_ROOT / "core_v2" / "valid.parquet")).replace("\\", "/"),
        help="Labeled valid split parquet",
    )
    parser.add_argument(
        "--eval",
        default=str((LABELED_ROOT / "core_v2" / "eval.parquet")).replace("\\", "/"),
        help="Labeled eval split parquet",
    )
    parser.add_argument(
        "--profile",
        default=FEATURE_PROFILE_ALL,
        choices=list(FEATURE_PROFILES),
        help="Feature profile to select columns",
    )
    parser.add_argument(
        "--out-root",
        default=str(FEATURES_ROOT).replace("\\", "/"),
        help="Output root for profile datasets",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    summary = run_feature_stage(
        train_path=Path(args.train),
        valid_path=Path(args.valid),
        eval_path=Path(args.eval),
        profile=str(args.profile),
        out_root=Path(args.out_root),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
