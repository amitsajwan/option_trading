from __future__ import annotations

from typing import Dict, List, Sequence


def build_day_folds(days: Sequence[str], train_days: int, valid_days: int, test_days: int, step_days: int, purge_days: int = 0, embargo_days: int = 0) -> List[Dict[str, List[str]]]:
    if train_days <= 0 or valid_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days, valid_days, test_days, step_days must all be > 0")
    if purge_days < 0 or embargo_days < 0:
        raise ValueError("purge_days and embargo_days must be >= 0")
    n = len(days)
    span = train_days + purge_days + valid_days + embargo_days + test_days
    folds: List[Dict[str, List[str]]] = []
    start = 0
    while start + span <= n:
        train_end = start + train_days
        valid_start = train_end + purge_days
        valid_end = valid_start + valid_days
        test_start = valid_end + embargo_days
        test_end = test_start + test_days
        folds.append(
            {
                "train_days": list(days[start:train_end]),
                "purge_days": list(days[train_end:valid_start]),
                "valid_days": list(days[valid_start:valid_end]),
                "embargo_days": list(days[valid_end:test_start]),
                "test_days": list(days[test_start:test_end]),
            }
        )
        start += step_days
    return folds

