from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional

import pandas as pd

from .schema_validator import build_file_path


OPTIONS_SYMBOL_RE = re.compile(r"^BANKNIFTY(?P<expiry>\d{2}[A-Z]{3}\d{2})(?P<strike>\d+)(?P<otype>CE|PE)$")


@dataclass(frozen=True)
class DayRawData:
    day: str
    fut: pd.DataFrame
    options: pd.DataFrame
    spot: pd.DataFrame


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


def _with_timestamp(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(
        out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip(),
        errors="coerce",
    )
    return out


def _to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _parse_option_symbols(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    parsed = out["symbol"].astype(str).str.upper().str.extract(OPTIONS_SYMBOL_RE)
    out["expiry_code"] = parsed["expiry"]
    out["strike"] = pd.to_numeric(parsed["strike"], errors="coerce")
    out["option_type"] = parsed["otype"]
    return out


def load_day_raw_data(base_path: Path, day: str) -> DayRawData:
    fut_path = build_file_path(base_path=base_path, dataset="fut", day=day)
    opt_path = build_file_path(base_path=base_path, dataset="options", day=day)
    spot_path = build_file_path(base_path=base_path, dataset="spot", day=day)

    fut = _with_timestamp(_read_csv(fut_path))
    fut = _to_numeric(fut, ["open", "high", "low", "close", "oi", "volume"])
    fut = fut.sort_values("timestamp").reset_index(drop=True)

    options = _with_timestamp(_read_csv(opt_path))
    options = _to_numeric(options, ["open", "high", "low", "close", "oi", "volume"])
    options = _parse_option_symbols(options)
    options = options.sort_values(["timestamp", "symbol"]).reset_index(drop=True)

    spot = _with_timestamp(_read_csv(spot_path))
    spot = _to_numeric(spot, ["open", "high", "low", "close"])
    spot = spot.sort_values("timestamp").reset_index(drop=True)

    return DayRawData(day=day, fut=fut, options=options, spot=spot)


def load_day_options(base_path: Path, day: str) -> pd.DataFrame:
    opt_path = build_file_path(base_path=base_path, dataset="options", day=day)
    options = _with_timestamp(_read_csv(opt_path))
    options = _to_numeric(options, ["open", "high", "low", "close", "oi", "volume"])
    options = _parse_option_symbols(options)
    options = options.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    return options


def filter_valid_options(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = out[out["timestamp"].notna()]
    out = out[out["strike"].notna()]
    out = out[out["option_type"].isin(["CE", "PE"])]
    return out.reset_index(drop=True)


def first_non_null(series: pd.Series) -> Optional[object]:
    if series.empty:
        return None
    valid = series.dropna()
    if valid.empty:
        return None
    return valid.iloc[0]
