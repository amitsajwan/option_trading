from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from ml_pipeline_2.scripts.option_pnl_smoke import pick_expiry_for_date

DEFAULT_FLAT_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v3")
DEFAULT_OPTIONS_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/options")


def _assert_unique_keys(df: pd.DataFrame, label: str) -> None:
    """Fail loudly if (trade_date, minute, strike) has duplicates — would
    inflate the merge with the flat features."""
    if df.empty:
        return
    dup = df.duplicated(subset=["trade_date", "minute", "strike"])
    if dup.any():
        n_dup = int(dup.sum())
        raise ValueError(
            f"{label} options have {n_dup} duplicate (trade_date, minute, strike) "
            "rows after expiry filter — multiple expiries leaked through"
        )


def _filter_to_chosen_expiry(options: pd.DataFrame) -> pd.DataFrame:
    """For each trade_date, keep only rows on the chosen expiry (nearest forward).

    Without this filter, merges on (trade_date, minute, strike) silently
    inflate rows when the parquet contains multiple expiries per day.
    """
    if "expiry_str" not in options.columns:
        raise ValueError(
            "options frame missing 'expiry_str' column — cannot disambiguate expiries"
        )
    chosen_rows = []
    for td, group in options.groupby("trade_date"):
        expiry = pick_expiry_for_date(group, pd.Timestamp(td))
        if expiry is None:
            continue
        chosen_rows.append(group[group["expiry_str"] == expiry])
    if not chosen_rows:
        return options.iloc[0:0]
    return pd.concat(chosen_rows, ignore_index=True)


def _load_flat_date_range(flat_root: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    years = set(range(start.year, end.year + 1))
    frames: list[pd.DataFrame] = []
    for y in sorted(years):
        pattern = flat_root / f"year={y}" / "*.parquet"
        files = sorted(Path(p) for p in flat_root.glob(f"year={y}/*.parquet"))
        for f in files:
            df = pd.read_parquet(f)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            mask = (df["trade_date"] >= start) & (df["trade_date"] <= end)
            frames.append(df[mask])
    if not frames:
        raise FileNotFoundError(f"no flat data found in {flat_root} for {start.date()} → {end.date()}")
    merged = pd.concat(frames, ignore_index=True)
    merged["trade_date"] = pd.to_datetime(merged["trade_date"])
    return merged


def _load_options_for_months(
    options_root: Path, start: pd.Timestamp, end: pd.Timestamp,
) -> pd.DataFrame:
    months = pd.date_range(start=start.replace(day=1), end=end, freq="MS")
    frames: list[pd.DataFrame] = []
    for m in months:
        path = options_root / f"year={m.year}" / f"month={m.month:02d}" / "data.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(
            path,
            columns=["timestamp", "trade_date", "strike", "option_type", "close", "expiry_str"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["minute"] = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
        mask = (df["trade_date"] >= start) & (df["trade_date"] <= end)
        frames.append(df[mask])
    if not frames:
        raise FileNotFoundError(f"no options data found in {options_root} for {start.date()} → {end.date()}")
    combined = pd.concat(frames, ignore_index=True)
    return _filter_to_chosen_expiry(combined)


def load_merged_data(
    flat_root: Path,
    options_root: Path,
    start_date: str,
    end_date: str,
    *,
    option_type: str = "CE",
) -> pd.DataFrame:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)

    flat = _load_flat_date_range(flat_root, start, end)
    options = _load_options_for_months(options_root, start, end)

    if "opt_flow_atm_strike" not in flat.columns:
        raise ValueError("flat data missing 'opt_flow_atm_strike' column")
    if "time_minute_of_day" not in flat.columns:
        raise ValueError("flat data missing 'time_minute_of_day' column")

    flat["atm_strike"] = pd.to_numeric(flat["opt_flow_atm_strike"], errors="coerce")
    flat["minute"] = pd.to_numeric(flat["time_minute_of_day"], errors="coerce")

    opt_filtered = options[options["option_type"] == option_type].copy()
    opt_filtered = opt_filtered.rename(columns={"close": f"{option_type.lower()}_close"})
    _assert_unique_keys(opt_filtered, option_type)

    merged = flat.merge(
        opt_filtered[["trade_date", "minute", "strike", f"{option_type.lower()}_close"]],
        left_on=["trade_date", "minute", "atm_strike"],
        right_on=["trade_date", "minute", "strike"],
        how="left",
    )

    merged.drop(columns=["strike"], inplace=True, errors="ignore")
    return merged


def load_merged_data_both(
    flat_root: Path,
    options_root: Path,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)

    flat = _load_flat_date_range(flat_root, start, end)
    options = _load_options_for_months(options_root, start, end)

    flat["atm_strike"] = pd.to_numeric(flat["opt_flow_atm_strike"], errors="coerce")
    flat["minute"] = pd.to_numeric(flat["time_minute_of_day"], errors="coerce")

    ce = options[options["option_type"] == "CE"][["trade_date", "minute", "strike", "close"]].rename(
        columns={"close": "ce_close"}
    )
    pe = options[options["option_type"] == "PE"][["trade_date", "minute", "strike", "close"]].rename(
        columns={"close": "pe_close"}
    )
    _assert_unique_keys(ce, "CE")
    _assert_unique_keys(pe, "PE")

    merged = flat.merge(
        ce, left_on=["trade_date", "minute", "atm_strike"], right_on=["trade_date", "minute", "strike"], how="left",
    )
    merged.drop(columns=["strike"], inplace=True, errors="ignore")
    merged = merged.merge(
        pe, left_on=["trade_date", "minute", "atm_strike"], right_on=["trade_date", "minute", "strike"], how="left",
    )
    merged.drop(columns=["strike"], inplace=True, errors="ignore")
    return merged
