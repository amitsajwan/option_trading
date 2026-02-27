import argparse
import json
import shutil
from pathlib import Path
from typing import Dict, Optional

from .schema_validator import discover_available_days, resolve_archive_base


# Resolve paths from repository layout, not current working directory.
# Expected module path: <repo>/ml_pipeline/src/ml_pipeline/pipeline_layout.py
_PKG_ROOT = Path(__file__).resolve().parents[2]  # <repo>/ml_pipeline
ARTIFACTS_ROOT = _PKG_ROOT / "artifacts"
DATA_ROOT = ARTIFACTS_ROOT / "data"
EDA_ROOT = DATA_ROOT / "eda"
EDA_RAW_ROOT = EDA_ROOT / "raw_data"
EDA_PROCESSED_ROOT = EDA_ROOT / "processed_data"
FEATURE_ENG_ROOT = DATA_ROOT / "feature_engineering"
FEATURE_PROCESS_ROOT = FEATURE_ENG_ROOT / "feature_process"
FEATURES_ROOT = FEATURE_ENG_ROOT / "features"
LABELED_ROOT = FEATURE_ENG_ROOT / "labeled"
INPUTS_ROOT = DATA_ROOT / "inputs"
MARKET_ARCHIVE_ROOT = INPUTS_ROOT / "market_archive"
VIX_ROOT = INPUTS_ROOT / "vix"


def _is_banknifty_archive_root(path: Path) -> bool:
    return (
        (path / "banknifty_fut").exists()
        and (path / "banknifty_options").exists()
        and (path / "banknifty_spot").exists()
    )


def _banknifty_archive_candidates(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.exists():
        return out
    # Preferred: explicit banknifty_data folder.
    preferred = root / "banknifty_data"
    if preferred.exists():
        out.append(preferred)
    # Also allow root itself or any direct child that contains banknifty triplet.
    out.append(root)
    for child in root.iterdir():
        if child.is_dir():
            out.append(child)
    # De-duplicate preserving order.
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def ensure_layout_dirs() -> Dict[str, str]:
    dirs = {
        "artifacts_root": ARTIFACTS_ROOT,
        "data_root": DATA_ROOT,
        "eda_root": EDA_ROOT,
        "eda_raw_root": EDA_RAW_ROOT,
        "eda_processed_root": EDA_PROCESSED_ROOT,
        "feature_engineering_root": FEATURE_ENG_ROOT,
        "feature_process_root": FEATURE_PROCESS_ROOT,
        "features_root": FEATURES_ROOT,
        "labeled_root": LABELED_ROOT,
        "inputs_root": INPUTS_ROOT,
        "market_archive_root": MARKET_ARCHIVE_ROOT,
        "vix_root": VIX_ROOT,
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return {k: str(v).replace("\\", "/") for k, v in dirs.items()}


def resolve_market_archive_base(explicit_base: Optional[str] = None) -> Optional[Path]:
    if explicit_base:
        explicit = Path(explicit_base)
        for candidate in _banknifty_archive_candidates(explicit):
            if _is_banknifty_archive_root(candidate):
                return candidate
        return resolve_archive_base(explicit_base=explicit_base)
    if MARKET_ARCHIVE_ROOT.exists():
        for candidate in _banknifty_archive_candidates(MARKET_ARCHIVE_ROOT):
            if not _is_banknifty_archive_root(candidate):
                continue
            resolved = resolve_archive_base(explicit_base=str(candidate))
            if resolved is None:
                continue
            try:
                days = discover_available_days(resolved)
            except Exception:
                days = []
            if days:
                return resolved
    return resolve_archive_base(explicit_base=None)


def resolve_vix_source(explicit_vix: Optional[str] = None) -> Optional[str]:
    if explicit_vix:
        path = Path(explicit_vix)
        if path.exists():
            return str(path)
        return None
    if not VIX_ROOT.exists():
        return None
    csv_files = sorted(VIX_ROOT.glob("*.csv"))
    parquet_files = sorted(VIX_ROOT.glob("*.parquet"))
    files = csv_files + parquet_files
    if not files:
        return None
    if len(files) == 1:
        return str(files[0])
    return str(VIX_ROOT)


def stage_vix_data(source: str, move: bool = False) -> Dict[str, object]:
    ensure_layout_dirs()
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"vix source not found: {source}")
    copied = []
    if src.is_file():
        dst = VIX_ROOT / src.name
        if move:
            shutil.move(str(src), str(dst))
        else:
            shutil.copy2(src, dst)
        copied.append(str(dst).replace("\\", "/"))
    else:
        for file in sorted(src.rglob("*")):
            if not file.is_file():
                continue
            if file.suffix.lower() not in (".csv", ".parquet"):
                continue
            dst = VIX_ROOT / file.name
            if move:
                shutil.move(str(file), str(dst))
            else:
                shutil.copy2(file, dst)
            copied.append(str(dst).replace("\\", "/"))
    return {"vix_root": str(VIX_ROOT).replace("\\", "/"), "files_staged": copied, "count": len(copied)}


def layout_status() -> Dict[str, object]:
    ensure_layout_dirs()
    archive = resolve_market_archive_base(explicit_base=str(MARKET_ARCHIVE_ROOT))
    vix_source = resolve_vix_source(explicit_vix=None)
    return {
        "layout": ensure_layout_dirs(),
        "market_archive_resolved": (str(archive).replace("\\", "/") if archive else None),
        "vix_source_resolved": (str(vix_source).replace("\\", "/") if vix_source else None),
    }


def training_workspace(*, run_id: str) -> Path:
    ensure_layout_dirs()
    return FEATURE_PROCESS_ROOT / "runs" / str(run_id)


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Standardize and inspect ML pipeline data input layout.")
    parser.add_argument("--init", action="store_true", help="Create standard input layout directories")
    parser.add_argument("--status", action="store_true", help="Print current layout + resolved sources")
    parser.add_argument("--stage-vix", default=None, help="Copy VIX file/dir into standard vix input folder")
    parser.add_argument("--move", action="store_true", help="Move instead of copy for --stage-vix")
    args = parser.parse_args()

    out: Dict[str, object] = {}
    if args.init:
        out["init"] = ensure_layout_dirs()
    if args.stage_vix:
        out["stage_vix"] = stage_vix_data(source=str(args.stage_vix), move=bool(args.move))
    if args.status or (not args.init and not args.stage_vix):
        out["status"] = layout_status()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
