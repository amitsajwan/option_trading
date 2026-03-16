from typing import Tuple
import warnings

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


CALIBRATION_NONE = "none"
CALIBRATION_ISOTONIC = "isotonic"
CALIBRATION_PLATT = "platt"
MIN_CALIBRATION_SAMPLES_ISOTONIC = 500
MIN_CALIBRATION_SAMPLES_PLATT = 100
MIN_CALIBRATION_SAMPLES = 100


def calibrate_probs(
    *,
    method: str,
    valid_prob: np.ndarray,
    valid_label: np.ndarray,
    score_prob: np.ndarray | None = None,
    test_prob: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    mode = str(method or CALIBRATION_NONE).strip().lower()
    v_fit = np.asarray(valid_prob, dtype=float)
    y = np.asarray(valid_label, dtype=int)
    v_score = np.asarray(score_prob, dtype=float) if score_prob is not None else v_fit
    t = np.asarray(test_prob, dtype=float)
    if mode == CALIBRATION_NONE:
        return v_score, t
    if len(np.unique(y)) < 2:
        return v_score, t
    min_required = (
        MIN_CALIBRATION_SAMPLES_ISOTONIC
        if mode == CALIBRATION_ISOTONIC
        else MIN_CALIBRATION_SAMPLES_PLATT
    )
    if len(v_fit) < min_required:
        warnings.warn(
            f"calibration skipped ({mode}): only {len(v_fit)} calibration-fit samples, "
            f"need >={min_required}. Returning raw probabilities.",
            RuntimeWarning,
        )
        return v_score, t
    if mode == CALIBRATION_ISOTONIC:
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(v_fit, y)
        return iso.predict(v_score), iso.predict(t)
    if mode == CALIBRATION_PLATT:
        lr = LogisticRegression(max_iter=200, solver="lbfgs", random_state=42)
        lr.fit(v_fit.reshape(-1, 1), y)
        return lr.predict_proba(v_score.reshape(-1, 1))[:, 1], lr.predict_proba(t.reshape(-1, 1))[:, 1]
    raise ValueError(f"unsupported calibration method: {method}")
