from __future__ import annotations

import numpy as np
import pandas as pd

from ml_pipeline_2.labeling import prepare_snapshot_labeled_frame


def test_prepare_snapshot_labeled_frame_derives_dealer_proxy_columns() -> None:
    timestamps = pd.date_range("2024-01-01 09:15:00", periods=6, freq="min")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "trade_date": ["2024-01-01"] * len(timestamps),
            "opt_flow_ce_oi_total": [1000.0, 1010.0, 1020.0, 1035.0, 1040.0, 1055.0],
            "opt_flow_pe_oi_total": [1100.0, 1090.0, 1085.0, 1075.0, 1060.0, 1050.0],
            "opt_flow_pcr_oi": [1.10, 1.08, 1.06, 1.03, 1.01, 1.00],
            "opt_flow_atm_oi_change_1m": [20.0, 25.0, 15.0, 30.0, 28.0, 35.0],
            "opt_flow_ce_pe_volume_diff": [-50.0, -40.0, -20.0, 10.0, 25.0, 30.0],
            "opt_flow_ce_volume_total": [500.0, 520.0, 540.0, 560.0, 590.0, 610.0],
            "opt_flow_pe_volume_total": [600.0, 590.0, 575.0, 560.0, 540.0, 530.0],
            "ctx_dte_days": [1.0] * len(timestamps),
            "vix_prev_close": [22.0] * len(timestamps),
        }
    )

    out = prepare_snapshot_labeled_frame(frame, context="dealer-proxy-unit")

    for column in (
        "dealer_proxy_oi_imbalance",
        "dealer_proxy_oi_imbalance_change_5m",
        "dealer_proxy_pcr_change_5m",
        "dealer_proxy_atm_oi_velocity_5m",
        "dealer_proxy_volume_imbalance",
    ):
        assert column in out.columns
    assert np.isfinite(float(out["dealer_proxy_oi_imbalance"].iloc[-1]))
    assert np.isfinite(float(out["dealer_proxy_pcr_change_5m"].iloc[-1]))
    assert np.isfinite(float(out["dealer_proxy_atm_oi_velocity_5m"].iloc[-1]))
    assert np.isfinite(float(out["dealer_proxy_volume_imbalance"].iloc[-1]))

