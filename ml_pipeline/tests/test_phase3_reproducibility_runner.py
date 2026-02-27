import json
import tempfile
import unittest
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from ml_pipeline.phase3_reproducibility_runner import run_phase3_reproducibility


def _labeled(rows: int = 120) -> pd.DataFrame:
    ts = pd.date_range("2023-01-02 09:15:00", periods=rows, freq="min")
    x1 = np.sin(np.linspace(0.0, 10.0, rows))
    x2 = np.cos(np.linspace(0.0, 8.0, rows))
    ce = (x1 + 0.2 * x2 > 0).astype(int)
    pe = (x2 - 0.2 * x1 > 0).astype(int)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "trade_date": [str(t.date()) for t in ts],
            "f1": x1,
            "f2": x2,
            "ce_label_valid": np.ones(rows),
            "pe_label_valid": np.ones(rows),
            "ce_label": ce,
            "pe_label": pe,
            "ce_forward_return": np.where(ce == 1, 0.01, -0.007),
            "pe_forward_return": np.where(pe == 1, 0.01, -0.007),
        }
    )


def _model_package() -> dict:
    pipe1 = Pipeline([("imputer", SimpleImputer(strategy="median")), ("model", LogisticRegression(max_iter=300, random_state=3))])
    pipe2 = Pipeline([("imputer", SimpleImputer(strategy="median")), ("model", LogisticRegression(max_iter=300, random_state=5))])
    return {"feature_columns": ["f1", "f2"], "models": {"ce": pipe1, "pe": pipe2}}


def _events() -> list:
    return [
        {
            "timestamp": "2024-09-03T09:39:00+05:30",
            "event_type": "ENTRY",
            "action": "BUY_CE",
            "position": {"side": "CE"},
            "position_runtime": {"qty": 10.0, "entry_price": 100.0, "option_symbol": "BANKNIFTY04SEP2451600CE"},
            "prices": {"opt_0_ce_close": 101.0},
        },
        {
            "timestamp": "2024-09-03T09:41:00+05:30",
            "event_type": "EXIT",
            "action": "HOLD",
            "position": {"side": "CE"},
            "position_runtime": {"qty": 10.0, "entry_price": 100.0, "option_symbol": "BANKNIFTY04SEP2451600CE"},
            "prices": {"opt_0_ce_close": 98.0},
        },
    ]


class Phase3ReproducibilityRunnerTests(unittest.TestCase):
    def test_reproducibility_passes_on_deterministic_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            labeled_path = base / "labeled.parquet"
            model_path = base / "model.joblib"
            threshold_path = base / "threshold.json"
            decisions_path = base / "events.jsonl"
            workdir = base / "work"

            _labeled().to_parquet(labeled_path, index=False)
            joblib.dump(_model_package(), model_path)
            threshold_path.write_text(json.dumps({"dual_mode_policy": {"ce_threshold": 0.5, "pe_threshold": 0.5}}), encoding="utf-8")
            decisions_path.write_text("\n".join(json.dumps(x) for x in _events()) + "\n", encoding="utf-8")

            report = run_phase3_reproducibility(
                labeled_data_path=labeled_path,
                model_package_path=model_path,
                threshold_report_path=threshold_path,
                decisions_jsonl_path=decisions_path,
                workdir=workdir,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["comparison"]["mismatch_count"], 0)
            self.assertTrue((workdir / "run1" / "artifacts" / "t32_diagnostics_stress_report.json").exists())
            self.assertTrue((workdir / "run2" / "artifacts" / "t33_order_runtime_report.json").exists())


if __name__ == "__main__":
    unittest.main()
