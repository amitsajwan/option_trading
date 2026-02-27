import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd

from ..config import LabelConfig
from ..exit_policy import ExitPolicyConfig, load_exit_policy
from ..label_engine import EffectiveLabelConfig, build_labeled_dataset
from ..pipeline_layout import LABELED_ROOT, resolve_market_archive_base

QUALITY_PRESET_BASELINE = "baseline"
QUALITY_PRESET_STRICT = "strict"
QUALITY_PRESETS = (QUALITY_PRESET_BASELINE, QUALITY_PRESET_STRICT)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_cfg(
    *,
    horizon_minutes: Optional[int],
    return_threshold: Optional[float],
    use_excursion_gate: bool,
    min_favorable_excursion: Optional[float],
    max_adverse_excursion: Optional[float],
    ce_return_threshold: Optional[float],
    pe_return_threshold: Optional[float],
    ce_use_excursion_gate: bool,
    pe_use_excursion_gate: bool,
    ce_min_favorable_excursion: Optional[float],
    pe_min_favorable_excursion: Optional[float],
    ce_max_adverse_excursion: Optional[float],
    pe_max_adverse_excursion: Optional[float],
    stop_loss_pct: Optional[float],
    take_profit_pct: Optional[float],
    allow_hold_extension: bool,
    extension_trigger_profit_pct: Optional[float],
    aux_horizons: Optional[str],
    exit_policy_json: Optional[str],
    quality_preset: str,
) -> EffectiveLabelConfig:
    preset = str(quality_preset or QUALITY_PRESET_BASELINE).strip().lower()
    if preset not in QUALITY_PRESETS:
        raise ValueError(f"unsupported quality preset: {quality_preset}")

    # Strict preset is tuned to reduce weak positives and encourage cleaner labels.
    if preset == QUALITY_PRESET_STRICT:
        label_default = LabelConfig(
            horizon_minutes=5,
            return_threshold=0.005,
            use_excursion_gate=True,
            min_favorable_excursion=0.006,
            max_adverse_excursion=0.002,
        )
    else:
        label_default = LabelConfig()

    exit_default = ExitPolicyConfig()
    exit_cfg = exit_default
    if exit_policy_json:
        exit_cfg = load_exit_policy(Path(exit_policy_json))
    resolved_horizon = int(horizon_minutes if horizon_minutes is not None else label_default.horizon_minutes)
    aux_list = [int(x.strip()) for x in str(aux_horizons or "").split(",") if x.strip()]
    return EffectiveLabelConfig(
        horizon_minutes=resolved_horizon,
        return_threshold=float(return_threshold if return_threshold is not None else label_default.return_threshold),
        use_excursion_gate=bool(use_excursion_gate if use_excursion_gate else label_default.use_excursion_gate),
        min_favorable_excursion=float(
            min_favorable_excursion
            if min_favorable_excursion is not None
            else label_default.min_favorable_excursion
        ),
        max_adverse_excursion=float(
            max_adverse_excursion if max_adverse_excursion is not None else label_default.max_adverse_excursion
        ),
        ce_return_threshold=(
            float(ce_return_threshold) if ce_return_threshold is not None else None
        ),
        pe_return_threshold=(
            float(pe_return_threshold) if pe_return_threshold is not None else None
        ),
        ce_use_excursion_gate=(True if ce_use_excursion_gate else None),
        pe_use_excursion_gate=(True if pe_use_excursion_gate else None),
        ce_min_favorable_excursion=(
            float(ce_min_favorable_excursion) if ce_min_favorable_excursion is not None else None
        ),
        pe_min_favorable_excursion=(
            float(pe_min_favorable_excursion) if pe_min_favorable_excursion is not None else None
        ),
        ce_max_adverse_excursion=(
            float(ce_max_adverse_excursion) if ce_max_adverse_excursion is not None else None
        ),
        pe_max_adverse_excursion=(
            float(pe_max_adverse_excursion) if pe_max_adverse_excursion is not None else None
        ),
        stop_loss_pct=float(stop_loss_pct if stop_loss_pct is not None else exit_cfg.stop_loss_pct),
        take_profit_pct=float(take_profit_pct if take_profit_pct is not None else exit_cfg.take_profit_pct),
        allow_hold_extension=bool(allow_hold_extension if allow_hold_extension else exit_cfg.allow_hold_extension),
        extension_trigger_profit_pct=float(
            extension_trigger_profit_pct
            if extension_trigger_profit_pct is not None
            else (exit_cfg.move_to_break_even_at_profit_pct or 0.0)
        ),
        aux_horizons=tuple(sorted({h for h in aux_list if h > 0 and h != resolved_horizon})),
    )


def _label_split(
    *,
    split_name: str,
    input_path: Path,
    output_path: Path,
    base_path: Path,
    cfg: EffectiveLabelConfig,
) -> Dict[str, object]:
    if not input_path.exists():
        raise FileNotFoundError(f"{split_name} split not found: {input_path}")
    frame = pd.read_parquet(input_path)
    labeled = build_labeled_dataset(features=frame, base_path=base_path, cfg=cfg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    labeled.to_parquet(output_path, index=False)

    # Report valid-row counts for base horizon and all aux horizons so
    # we can immediately see whether aux-horizon labels were generated correctly.
    aux_counts: Dict[str, int] = {}
    for h in (cfg.aux_horizons or []):
        for side in ("ce", "pe"):
            col = f"{side}_label_valid_h{int(h)}m"
            if col in labeled.columns:
                aux_counts[col] = int((labeled[col].fillna(0.0) == 1.0).sum())
            else:
                aux_counts[col] = -1  # -1 = column absent (label engine did not produce it)

    return {
        "input": str(input_path).replace("\\", "/"),
        "output": str(output_path).replace("\\", "/"),
        "rows": int(len(labeled)),
        "columns": int(len(labeled.columns)),
        "ce_valid_rows": int((labeled.get("ce_label_valid", 0.0).fillna(0.0) == 1.0).sum()) if len(labeled) else 0,
        "pe_valid_rows": int((labeled.get("pe_label_valid", 0.0).fillna(0.0) == 1.0).sum()) if len(labeled) else 0,
        "aux_horizon_valid_rows": aux_counts,
    }


def run_label_stage(
    *,
    train_path: Path,
    valid_path: Path,
    eval_path: Path,
    base_path: Path,
    out_root: Path,
    profile: str,
    cfg: EffectiveLabelConfig,
) -> Dict[str, object]:
    profile_root = out_root / str(profile)
    result = {
        "created_at_utc": _utc_now(),
        "profile": str(profile),
        "base_path": str(base_path).replace("\\", "/"),
        "config": {
            "horizon_minutes": int(cfg.horizon_minutes),
            "return_threshold": float(cfg.return_threshold),
            "use_excursion_gate": bool(cfg.use_excursion_gate),
            "min_favorable_excursion": float(cfg.min_favorable_excursion),
            "max_adverse_excursion": float(cfg.max_adverse_excursion),
            "ce_return_threshold": (
                float(cfg.ce_return_threshold) if cfg.ce_return_threshold is not None else None
            ),
            "pe_return_threshold": (
                float(cfg.pe_return_threshold) if cfg.pe_return_threshold is not None else None
            ),
            "ce_use_excursion_gate": (
                bool(cfg.ce_use_excursion_gate) if cfg.ce_use_excursion_gate is not None else None
            ),
            "pe_use_excursion_gate": (
                bool(cfg.pe_use_excursion_gate) if cfg.pe_use_excursion_gate is not None else None
            ),
            "ce_min_favorable_excursion": (
                float(cfg.ce_min_favorable_excursion) if cfg.ce_min_favorable_excursion is not None else None
            ),
            "pe_min_favorable_excursion": (
                float(cfg.pe_min_favorable_excursion) if cfg.pe_min_favorable_excursion is not None else None
            ),
            "ce_max_adverse_excursion": (
                float(cfg.ce_max_adverse_excursion) if cfg.ce_max_adverse_excursion is not None else None
            ),
            "pe_max_adverse_excursion": (
                float(cfg.pe_max_adverse_excursion) if cfg.pe_max_adverse_excursion is not None else None
            ),
            "stop_loss_pct": float(cfg.stop_loss_pct),
            "take_profit_pct": float(cfg.take_profit_pct),
            "allow_hold_extension": bool(cfg.allow_hold_extension),
            "extension_trigger_profit_pct": float(cfg.extension_trigger_profit_pct),
            "aux_horizons": [int(x) for x in cfg.aux_horizons],
        },
        "splits": {},
    }
    result["splits"]["train"] = _label_split(
        split_name="train",
        input_path=train_path,
        output_path=profile_root / "train.parquet",
        base_path=base_path,
        cfg=cfg,
    )
    result["splits"]["valid"] = _label_split(
        split_name="valid",
        input_path=valid_path,
        output_path=profile_root / "valid.parquet",
        base_path=base_path,
        cfg=cfg,
    )
    result["splits"]["eval"] = _label_split(
        split_name="eval",
        input_path=eval_path,
        output_path=profile_root / "eval.parquet",
        base_path=base_path,
        cfg=cfg,
    )
    lineage_path = profile_root / "lineage.json"
    lineage_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["lineage_json"] = str(lineage_path).replace("\\", "/")
    return result


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Label stage: build CE/PE supervised labels for train/valid/eval splits.")
    parser.add_argument(
        "--train",
        default="ml_pipeline/artifacts/data/eda/processed_data/datasets/train.parquet",
        help="EDA train split parquet",
    )
    parser.add_argument(
        "--valid",
        default="ml_pipeline/artifacts/data/eda/processed_data/datasets/valid.parquet",
        help="EDA valid split parquet",
    )
    parser.add_argument(
        "--eval",
        default="ml_pipeline/artifacts/data/eda/processed_data/datasets/eval.parquet",
        help="EDA eval split parquet",
    )
    parser.add_argument("--base-path", default=None, help="Market archive base path (optional, auto-resolve by layout)")
    parser.add_argument("--profile", default="core_v2", help="Profile folder for labeled outputs")
    parser.add_argument(
        "--out-root",
        default=str(LABELED_ROOT).replace("\\", "/"),
        help="Output root for labeled splits",
    )
    parser.add_argument("--quality-preset", default=QUALITY_PRESET_BASELINE, choices=list(QUALITY_PRESETS))
    parser.add_argument("--horizon-minutes", type=int, default=None)
    parser.add_argument("--return-threshold", type=float, default=None)
    parser.add_argument("--ce-return-threshold", type=float, default=None)
    parser.add_argument("--pe-return-threshold", type=float, default=None)
    parser.add_argument("--use-excursion-gate", action="store_true")
    parser.add_argument("--ce-use-excursion-gate", action="store_true")
    parser.add_argument("--pe-use-excursion-gate", action="store_true")
    parser.add_argument("--min-favorable-excursion", type=float, default=None)
    parser.add_argument("--max-adverse-excursion", type=float, default=None)
    parser.add_argument("--ce-min-favorable-excursion", type=float, default=None)
    parser.add_argument("--pe-min-favorable-excursion", type=float, default=None)
    parser.add_argument("--ce-max-adverse-excursion", type=float, default=None)
    parser.add_argument("--pe-max-adverse-excursion", type=float, default=None)
    parser.add_argument("--stop-loss-pct", type=float, default=None)
    parser.add_argument("--take-profit-pct", type=float, default=None)
    parser.add_argument("--allow-hold-extension", action="store_true")
    parser.add_argument("--extension-trigger-profit-pct", type=float, default=None)
    parser.add_argument(
        "--aux-horizons",
        default="",
        help="Comma-separated additional horizons in minutes (example: 5,15,30)",
    )
    parser.add_argument("--exit-policy-json", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    archive = resolve_market_archive_base(args.base_path)
    if archive is None:
        raise SystemExit("Could not resolve market archive base. Provide --base-path.")

    cfg = _resolve_cfg(
        horizon_minutes=args.horizon_minutes,
        return_threshold=args.return_threshold,
        use_excursion_gate=bool(args.use_excursion_gate),
        min_favorable_excursion=args.min_favorable_excursion,
        max_adverse_excursion=args.max_adverse_excursion,
        ce_return_threshold=args.ce_return_threshold,
        pe_return_threshold=args.pe_return_threshold,
        ce_use_excursion_gate=bool(args.ce_use_excursion_gate),
        pe_use_excursion_gate=bool(args.pe_use_excursion_gate),
        ce_min_favorable_excursion=args.ce_min_favorable_excursion,
        pe_min_favorable_excursion=args.pe_min_favorable_excursion,
        ce_max_adverse_excursion=args.ce_max_adverse_excursion,
        pe_max_adverse_excursion=args.pe_max_adverse_excursion,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        allow_hold_extension=bool(args.allow_hold_extension),
        extension_trigger_profit_pct=args.extension_trigger_profit_pct,
        aux_horizons=args.aux_horizons,
        exit_policy_json=args.exit_policy_json,
        quality_preset=str(args.quality_preset),
    )

    out = run_label_stage(
        train_path=Path(args.train),
        valid_path=Path(args.valid),
        eval_path=Path(args.eval),
        base_path=archive,
        out_root=Path(args.out_root),
        profile=str(args.profile),
        cfg=cfg,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
