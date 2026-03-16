from __future__ import annotations

from typing import Optional

import pandas as pd

REQUIRED_SNAPSHOT_SCHEMA_VERSION = "2.0"


def enforce_snapshot_schema_version(
    frame: pd.DataFrame,
    *,
    required_version: str = REQUIRED_SNAPSHOT_SCHEMA_VERSION,
    context: str = "snapshot load",
) -> pd.DataFrame:
    """Fail fast when snapshot rows are not on the required schema version."""
    if frame is None or frame.empty:
        return frame
    if "schema_version" not in frame.columns:
        raise ValueError(
            f"{context}: missing required column `schema_version`; "
            f"cannot verify snapshot schema_version={required_version}"
        )

    versions = frame["schema_version"].astype(str).str.strip()
    bad_mask = versions != str(required_version)
    if not bool(bad_mask.any()):
        return frame

    bad_count = int(bad_mask.sum())
    total = int(len(frame))
    bad_days_msg = ""
    if "trade_date" in frame.columns:
        bad_days = sorted({str(x) for x in frame.loc[bad_mask, "trade_date"].dropna().tolist()})
        if bad_days:
            preview = ", ".join(bad_days[:10])
            if len(bad_days) > 10:
                preview = f"{preview}, ... (+{len(bad_days) - 10} more)"
            bad_days_msg = f" bad_days=[{preview}]"
    raise ValueError(
        f"{context}: found {bad_count}/{total} rows not on schema_version={required_version}."
        f"{bad_days_msg}"
    )

