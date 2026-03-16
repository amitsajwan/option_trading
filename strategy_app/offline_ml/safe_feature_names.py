from __future__ import annotations

from typing import Any

import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin


class SafeFeatureNameTransformer(BaseEstimator, TransformerMixin):
    """Sanitize transformed column names for model compatibility."""

    def __init__(self) -> None:
        self._columns: list[str] | None = None

    @staticmethod
    def _sanitize(name: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(name))
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned.strip("_") or "feature"

    def fit(self, x: Any, y: Any = None) -> "SafeFeatureNameTransformer":
        if isinstance(x, pd.DataFrame):
            seen: dict[str, int] = {}
            columns: list[str] = []
            for raw in x.columns:
                base = self._sanitize(str(raw))
                index = seen.get(base, 0)
                seen[base] = index + 1
                columns.append(base if index == 0 else f"{base}_{index}")
            self._columns = columns
        else:
            self._columns = None
        return self

    def transform(self, x: Any) -> Any:
        if self._columns is None:
            return x
        if isinstance(x, pd.DataFrame):
            frame = x.copy()
        else:
            frame = pd.DataFrame(x)
        frame.columns = list(self._columns)
        return frame
