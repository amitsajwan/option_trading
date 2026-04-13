from __future__ import annotations

import json

import pandas as pd

from snapshot_app.historical.morning_session import _enrich_with_iv


def test_enrich_with_iv_matches_timezone_aware_raw_rows_to_naive_ml_flat() -> None:
    ml_flat = pd.DataFrame(
        [
            {"timestamp": pd.Timestamp("2021-03-02 10:00:00"), "px_fut_close": 100.0},
            {"timestamp": pd.Timestamp("2021-03-02 10:01:00"), "px_fut_close": 101.0},
        ]
    )
    raw = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2021-03-02 10:00:00", tz="Asia/Kolkata"),
                "snapshot_raw_json": json.dumps(
                    {
                        "atm_options": {"atm_ce_iv": 0.31, "atm_pe_iv": 0.28},
                        "iv_derived": {"iv_skew": 0.03},
                    }
                ),
            }
        ]
    )

    enriched = _enrich_with_iv(ml_flat, raw)

    assert float(enriched.loc[0, "atm_ce_iv"]) == 0.31
    assert float(enriched.loc[0, "atm_pe_iv"]) == 0.28
    assert float(enriched.loc[0, "iv_skew"]) == 0.03
    assert pd.isna(enriched.loc[1, "atm_ce_iv"])
