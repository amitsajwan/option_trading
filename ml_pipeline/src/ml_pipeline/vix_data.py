from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

import pandas as pd


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_").replace(".", "") for c in out.columns]
    return out


def _coerce_vix_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_columns(df)
    date_col = "date" if "date" in out.columns else None
    if date_col is None:
        raise ValueError("vix file missing date column")

    mapping = {
        "open": "vix_open",
        "high": "vix_high",
        "low": "vix_low",
        "close": "vix_close",
    }
    keep = {"date"}
    for raw in mapping.keys():
        if raw in out.columns:
            keep.add(raw)
    out = out.loc[:, [c for c in out.columns if c in keep]].copy()
    out["trade_date"] = pd.to_datetime(out["date"].astype(str).str.strip(), format="%d-%b-%Y", errors="coerce")
    if out["trade_date"].isna().all():
        out["trade_date"] = pd.to_datetime(out["date"].astype(str).str.strip(), errors="coerce")
    out = out.dropna(subset=["trade_date"]).copy()
    out["trade_date"] = out["trade_date"].dt.date.astype(str)
    for raw, clean in mapping.items():
        if raw in out.columns:
            out[clean] = pd.to_numeric(out[raw], errors="coerce")
        else:
            out[clean] = pd.NA
    out = out.loc[:, ["trade_date", "vix_open", "vix_high", "vix_low", "vix_close"]]
    out = out.dropna(subset=["vix_close"]).sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    return out.reset_index(drop=True)


def _iter_csv_files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        return []
    return sorted([p for p in path.rglob("*.csv") if p.is_file()])


def load_vix_daily(path_or_dir: Optional[Union[str, Path]]) -> pd.DataFrame:
    if path_or_dir is None:
        return pd.DataFrame()
    path = Path(path_or_dir)
    files = _iter_csv_files(path)
    if not files:
        return pd.DataFrame()
    frames: List[pd.DataFrame] = []
    for file in files:
        frame = pd.read_csv(file)
        frames.append(_coerce_vix_frame(frame))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last").reset_index(drop=True)
    return out


def align_vix_to_trade_dates(
    trade_dates: Sequence[str],
    vix_daily: pd.DataFrame,
) -> pd.DataFrame:
    if not trade_dates:
        return pd.DataFrame(columns=["trade_date", "vix_close_raw", "vix_close_aligned", "vix_prev_close_aligned"])
    base = pd.DataFrame({"trade_date": sorted({str(x) for x in trade_dates})})
    if vix_daily is None or len(vix_daily) == 0:
        base["vix_close_raw"] = pd.NA
        base["vix_close_aligned"] = pd.NA
        base["vix_prev_close_aligned"] = pd.NA
        return base

    vd = vix_daily.copy()
    vd["trade_date"] = vd["trade_date"].astype(str)
    vd = vd.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    merged = base.merge(vd[["trade_date", "vix_close"]], on="trade_date", how="left")
    merged = merged.rename(columns={"vix_close": "vix_close_raw"})
    merged["vix_close_raw"] = pd.to_numeric(merged["vix_close_raw"], errors="coerce")
    merged["vix_close_aligned"] = merged["vix_close_raw"].ffill()
    merged["vix_prev_close_aligned"] = merged["vix_close_aligned"].shift(1)
    return merged
