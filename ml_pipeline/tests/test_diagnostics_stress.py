import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from ml_pipeline.diagnostics_stress import _load_thresholds, run_diagnostics_stress


def _make_labeled(rows: int = 160) -> pd.DataFrame:
    ts = pd.date_range("2023-06-01 09:15:00", periods=rows, freq="min")
    x1 = np.sin(np.linspace(0.0, 16.0, rows))
    x2 = np.cos(np.linspace(0.0, 11.0, rows))
    ce_label = (x1 + 0.25 * x2 > 0.0).astype(int)
    pe_label = (x2 - 0.25 * x1 > 0.0).astype(int)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "trade_date": [str(t.date()) for t in ts],
            "f1": x1,
            "f2": x2,
            "ce_label_valid": np.ones(rows),
            "pe_label_valid": np.ones(rows),
            "ce_label": ce_label,
            "pe_label": pe_label,
            "ce_forward_return": np.where(ce_label == 1, 0.012, -0.008),
            "pe_forward_return": np.where(pe_label == 1, 0.011, -0.008),
            "ce_path_exit_reason": np.where(ce_label == 1, "tp", "sl"),
            "pe_path_exit_reason": np.where(pe_label == 1, "tp", "sl"),
        }
    )


def _model_package() -> dict:
    def _pipe(seed: int) -> Pipeline:
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("model", LogisticRegression(max_iter=300, random_state=seed)),
            ]
        )

    return {
        "feature_columns": ["f1", "f2"],
        "models": {"ce": _pipe(7), "pe": _pipe(11)},
    }


class DiagnosticsStressTests(unittest.TestCase):
    def test_run_diagnostics_stress_structure(self) -> None:
        report = run_diagnostics_stress(
            labeled_df=_make_labeled(rows=140),
            model_package=_model_package(),
            ce_threshold=0.45,
            pe_threshold=0.45,
            train_ratio=0.6,
            valid_ratio=0.2,
            cost_grid=(0.0006, 0.0010),
            slippage_grid=(0.0, 0.0005),
        )
        self.assertEqual(report["task"], "T32")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["feature_count"], 2)
        self.assertEqual(len(report["cost_slippage_stress"]), 12)
        self.assertIn("ce", report["overfit_underfit"])
        self.assertIn("pe", report["overfit_underfit"])
        self.assertEqual(len(report["overfit_underfit"]["ce"]["learning_curve"]), 6)
        self.assertEqual(len(report["overfit_underfit"]["pe"]["learning_curve"]), 6)

    def test_cost_slippage_sensitivity_accounting(self) -> None:
        report = run_diagnostics_stress(
            labeled_df=_make_labeled(rows=160),
            model_package=_model_package(),
            ce_threshold=0.45,
            pe_threshold=0.45,
            train_ratio=0.6,
            valid_ratio=0.2,
            cost_grid=(0.0006, 0.0016),
            slippage_grid=(0.0,),
        )
        rows = [r for r in report["cost_slippage_stress"] if r["mode"] == "dual"]
        low = next(r for r in rows if abs(r["cost_per_trade"] - 0.0006) < 1e-12)
        high = next(r for r in rows if abs(r["cost_per_trade"] - 0.0016) < 1e-12)
        self.assertEqual(low["trades_total"], high["trades_total"])
        expected_delta = float(high["trades_total"]) * (0.0016 - 0.0006)
        observed_delta = float(low["net_return_sum"]) - float(high["net_return_sum"])
        self.assertAlmostEqual(observed_delta, expected_delta, places=10)

    def test_topk_path_tp_sl_mode(self) -> None:
        report = run_diagnostics_stress(
            labeled_df=_make_labeled(rows=160),
            model_package=_model_package(),
            ce_threshold=None,
            pe_threshold=None,
            selection_mode="topk",
            topk_per_day=10,
            label_target="path_tp_sl",
            train_ratio=0.6,
            valid_ratio=0.2,
            cost_grid=(0.0006,),
            slippage_grid=(0.0,),
        )
        self.assertEqual(report["label_target"], "path_tp_sl")
        self.assertEqual(report["selection_policy"]["selection_mode"], "topk")
        self.assertEqual(report["selection_policy"]["topk_per_day"], 10)
        self.assertEqual(len(report["cost_slippage_stress"]), 3)
        self.assertTrue(all(r["selection_mode"] == "topk" for r in report["cost_slippage_stress"]))

    def test_load_thresholds_t31_and_t08_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            t31 = base / "t31.json"
            t31.write_text(json.dumps({"dual_mode_policy": {"ce_threshold": 0.71, "pe_threshold": 0.63}}), encoding="utf-8")
            ce1, pe1 = _load_thresholds(t31)
            self.assertAlmostEqual(ce1, 0.71)
            self.assertAlmostEqual(pe1, 0.63)

            t08 = base / "t08.json"
            t08.write_text(json.dumps({"ce": {"selected_threshold": 0.68}, "pe": {"selected_threshold": 0.59}}), encoding="utf-8")
            ce2, pe2 = _load_thresholds(t08)
            self.assertAlmostEqual(ce2, 0.68)
            self.assertAlmostEqual(pe2, 0.59)


if __name__ == "__main__":
    unittest.main()
