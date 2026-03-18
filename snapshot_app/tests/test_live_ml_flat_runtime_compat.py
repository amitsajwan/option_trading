from __future__ import annotations

import pandas as pd

from snapshot_app.core.live_ml_flat import _build_runtime_compat_payload


def test_runtime_compat_payload_uses_elevated_vix_regime_label() -> None:
    panel = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-03-17T09:30:00")],
            "trade_date": ["2026-03-17"],
            "fut_close": [50000.0],
            "fut_oi": [1000.0],
        }
    )
    payload = _build_runtime_compat_payload(
        feature_row={
            "snapshot_id": "20260317_0930",
            "ctx_dte_days": 2,
            "ctx_is_expiry_day": 0,
            "vix_prev_close": 18.0,
        },
        panel=panel,
        chain={"strikes": []},
        vix_live_current=22.0,
        ts=pd.Timestamp("2026-03-17T09:30:00"),
    )

    assert payload["vix_context"]["vix_regime"] == "ELEVATED"
