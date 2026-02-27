import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

from .profiles import FEATURE_PROFILE_ALL, FEATURE_PROFILES, apply_feature_profile


IDENTITY_COLUMNS = (
    "timestamp",
    "trade_date",
    "fut_symbol",
    "expiry_code",
    "source_day",
    "ce_symbol",
    "pe_symbol",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_split(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"split file not found: {path}")
    frame = pd.read_parquet(path)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    return frame


def _prepare_xy(frame: pd.DataFrame, *, target_col: str, feature_profile: str) -> tuple[pd.DataFrame, pd.Series, List[str]]:
    if target_col not in frame.columns:
        raise ValueError(f"target column missing for OpenFE: {target_col}")
    y = pd.to_numeric(frame[target_col], errors="coerce")
    keep_mask = y.notna()
    work = frame.loc[keep_mask].copy()
    y = y.loc[keep_mask]
    # Exclude label/return columns (base AND aux-horizon variants like ce_label_h15m,
    # ce_forward_return_h15m) from the OpenFE base feature set. Without this guard,
    # these numeric columns would be passed to OpenFE as input features, and any
    # generated feature combining them with others would carry target leakage
    # through the entire generated feature set.
    from .profiles import _is_always_excluded  # noqa: PLC0415
    numeric_cols = [
        c for c in work.select_dtypes(include=["number"]).columns
        if c != target_col and not _is_always_excluded(c)
    ]
    selected = apply_feature_profile(numeric_cols, feature_profile=feature_profile)
    if not selected:
        raise ValueError(f"no numeric features selected for profile={feature_profile}")
    x = work.loc[:, selected].copy()
    # OpenFE expects no missing in feature names and consistent dtype handling
    x.columns = [str(c) for c in x.columns]
    return x, y.astype(int), selected


def run_openfe_pipeline(
    *,
    train_path: Path,
    valid_path: Path,
    eval_path: Path,
    target_col: str,
    out_root: Path,
    feature_profile: str = FEATURE_PROFILE_ALL,
    n_jobs: int = 4,
    top_k: int = 50,
) -> Dict[str, object]:
    # Required dependency by design (no fallback path).
    try:
        from openfe import OpenFE, transform
    except Exception as exc:
        raise RuntimeError(
            "OpenFE is required. Install it first: pip install openfe"
        ) from exc

    train_df = _load_split(train_path)
    valid_df = _load_split(valid_path)
    eval_df = _load_split(eval_path)

    x_train, y_train, selected_cols = _prepare_xy(train_df, target_col=target_col, feature_profile=feature_profile)
    missing_valid = [c for c in selected_cols if c not in valid_df.columns]
    missing_eval = [c for c in selected_cols if c not in eval_df.columns]
    if missing_valid:
        preview = ",".join(missing_valid[:10])
        raise ValueError(
            f"OpenFE valid split is missing {len(missing_valid)} selected base feature columns: {preview}"
        )
    if missing_eval:
        preview = ",".join(missing_eval[:10])
        raise ValueError(
            f"OpenFE eval split is missing {len(missing_eval)} selected base feature columns: {preview}"
        )
    x_valid = valid_df.loc[:, list(selected_cols)].copy()
    x_eval = eval_df.loc[:, list(selected_cols)].copy()

    engine = OpenFE()
    # OpenFE returns selected candidate feature objects.
    candidate_features = engine.fit(
        data=x_train,
        label=y_train,
        n_jobs=int(n_jobs),
        verbose=False,
    )
    if isinstance(top_k, int) and top_k > 0 and len(candidate_features) > top_k:
        candidate_features = candidate_features[:top_k]
    if len(candidate_features) == 0:
        train_aug = x_train.copy()
        valid_aug = x_valid.copy()
        eval_aug = x_eval.copy()
    else:
        train_aug, valid_aug = transform(x_train, x_valid, candidate_features, n_jobs=int(n_jobs))
        _, eval_aug = transform(x_train, x_eval, candidate_features, n_jobs=int(n_jobs))

    profile_root = out_root / "openfe"
    profile_root.mkdir(parents=True, exist_ok=True)

    identity_cols = [c for c in IDENTITY_COLUMNS if c in train_df.columns]
    train_out = pd.concat(
        [
            train_df.loc[x_train.index, identity_cols].reset_index(drop=True),
            train_aug.reset_index(drop=True),
            y_train.reset_index(drop=True).rename(target_col),
        ],
        axis=1,
    )
    valid_out = pd.concat(
        [
            valid_df.loc[:, [c for c in identity_cols if c in valid_df.columns]].reset_index(drop=True),
            valid_aug.reset_index(drop=True),
            valid_df[target_col].reset_index(drop=True) if target_col in valid_df.columns else pd.Series([pd.NA] * len(valid_aug), name=target_col),
        ],
        axis=1,
    )
    eval_out = pd.concat(
        [
            eval_df.loc[:, [c for c in identity_cols if c in eval_df.columns]].reset_index(drop=True),
            eval_aug.reset_index(drop=True),
            eval_df[target_col].reset_index(drop=True) if target_col in eval_df.columns else pd.Series([pd.NA] * len(eval_aug), name=target_col),
        ],
        axis=1,
    )

    outputs = {
        "train_parquet": str((profile_root / "train.parquet")).replace("\\", "/"),
        "valid_parquet": str((profile_root / "valid.parquet")).replace("\\", "/"),
        "eval_parquet": str((profile_root / "eval.parquet")).replace("\\", "/"),
    }
    train_out.to_parquet(Path(outputs["train_parquet"]), index=False)
    valid_out.to_parquet(Path(outputs["valid_parquet"]), index=False)
    eval_out.to_parquet(Path(outputs["eval_parquet"]), index=False)

    report = {
        "created_at_utc": _utc_now(),
        "engine": "openfe",
        "target_col": str(target_col),
        "feature_profile": str(feature_profile),
        "n_jobs": int(n_jobs),
        "top_k": int(top_k),
        "base_feature_count": int(len(selected_cols)),
        "candidate_feature_count": int(len(candidate_features)),
        "fallback_used_no_candidates": bool(len(candidate_features) == 0),
        "outputs": outputs,
        "rows": {
            "train": int(len(train_out)),
            "valid": int(len(valid_out)),
            "eval": int(len(eval_out)),
        },
    }
    report_path = profile_root / "openfe_report.json"
    _write_json(report_path, report)
    return report


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="OpenFE feature automation on labeled train/valid/eval splits.")
    parser.add_argument("--train", required=True, help="Train split parquet (labeled)")
    parser.add_argument("--valid", required=True, help="Validation split parquet (labeled)")
    parser.add_argument("--eval", required=True, help="Eval split parquet (labeled)")
    parser.add_argument("--target-col", required=True, help="Binary target column name")
    parser.add_argument(
        "--feature-profile",
        default=FEATURE_PROFILE_ALL,
        choices=list(FEATURE_PROFILES),
        help="Base feature profile before OpenFE generation",
    )
    parser.add_argument("--out-root", default="ml_pipeline/artifacts/data/feature_engineering/features", help="Output root")
    parser.add_argument("--n-jobs", type=int, default=4, help="OpenFE parallel workers")
    parser.add_argument("--top-k", type=int, default=50, help="Keep top K generated feature candidates")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run_openfe_pipeline(
        train_path=Path(args.train),
        valid_path=Path(args.valid),
        eval_path=Path(args.eval),
        target_col=str(args.target_col),
        out_root=Path(args.out_root),
        feature_profile=str(args.feature_profile),
        n_jobs=int(args.n_jobs),
        top_k=int(args.top_k),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
