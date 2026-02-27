from typing import Tuple
import warnings

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


CALIBRATION_NONE = "none"
CALIBRATION_ISOTONIC = "isotonic"
CALIBRATION_PLATT = "platt"
MIN_CALIBRATION_SAMPLES = 50


def calibrate_probs(
    *,
    method: str,
    valid_prob: np.ndarray,
    valid_label: np.ndarray,
    test_prob: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    mode = str(method or CALIBRATION_NONE).strip().lower()
    v = np.asarray(valid_prob, dtype=float)
    y = np.asarray(valid_label, dtype=int)
    t = np.asarray(test_prob, dtype=float)
    if mode == CALIBRATION_NONE:
        return v, t
    if len(np.unique(y)) < 2:
        return v, t
    if len(v) < MIN_CALIBRATION_SAMPLES:
        warnings.warn(
            f"calibration skipped: only {len(v)} validation samples, "
            f"need >={MIN_CALIBRATION_SAMPLES} to avoid overfitting. Returning raw probabilities.",
            RuntimeWarning,
        )
        return v, t
    if mode == CALIBRATION_ISOTONIC:
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(v, y)
        return iso.predict(v), iso.predict(t)
    if mode == CALIBRATION_PLATT:
        lr = LogisticRegression(max_iter=200, solver="lbfgs")
        lr.fit(v.reshape(-1, 1), y)
        return lr.predict_proba(v.reshape(-1, 1))[:, 1], lr.predict_proba(t.reshape(-1, 1))[:, 1]
    raise ValueError(f"unsupported calibration method: {method}")
