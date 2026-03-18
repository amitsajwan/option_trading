from __future__ import annotations

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore


def test_normalize_option_fields_prefers_four_digit_year_symbol_parse() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "BANKNIFTY02APR202545000CE",
                "strike": None,
                "option_type": None,
                "expiry_str": None,
            }
        ]
    )

    out = ParquetStore._normalize_option_fields(frame)

    assert float(out.loc[0, "strike"]) == 45000.0
    assert out.loc[0, "option_type"] == "CE"
    assert out.loc[0, "expiry_str"] == "02APR2025"
