"""DailyFeaturesProvider — reads the nightly regime feature snapshot.

The companion script ``ml_pipeline_2/scripts/feature_builder/
build_daily_regime_features.py`` produces a JSON file containing
rolling daily features for every date in the dataset.  This provider
reads that file and surfaces the entry for *trade_date*.

File format (JSON)
------------------
Either a dict keyed by ISO date::

    {
      "2024-05-15": {
        "regime_rv20": 0.0112,
        "regime_dist_sma20": 0.0043,
        "regime_sma20_slope": 0.0002,
        "regime_60d_return": 0.067
      },
      ...
    }

Or a list of records with a "date" field::

    [{"date": "2024-05-15", "regime_rv20": 0.0112, ...}, ...]

Configuration
-------------
BRAIN_DAILY_FEATURES_PATH (env str)
    Path to daily_regime_features.json.  Defaults to
    ``{STRATEGY_RUNTIME_ARTIFACT_DIR}/daily_regime_features.json``.
    If the file does not exist the provider returns {} silently.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ..plugin import ContextProvider

logger = logging.getLogger(__name__)

_DEFAULT_FILENAME = "daily_regime_features.json"
_CONTEXT_PREFIX = "daily."

# Thresholds used when converting raw features into a DayScore suggestion.
# The brain itself synthesises the final DayScore from these values.
_RV20_CALM_MAX = 0.010      # trailing 20-day realised vol: calm if < 1.0%
_RV20_VOLATILE_MIN = 0.018  # volatile if > 1.8%
_SMA_SLOPE_POSITIVE_MIN = 0.0  # positive slope = drift


def _feature_file_path() -> Path:
    explicit = os.getenv("BRAIN_DAILY_FEATURES_PATH", "").strip()
    if explicit:
        return Path(explicit)
    run_dir_env = (
        os.getenv("STRATEGY_RUNTIME_ARTIFACT_DIR")
        or os.getenv("STRATEGY_RUN_DIR")
        or ".run/strategy_app"
    )
    return Path(run_dir_env) / _DEFAULT_FILENAME


class DailyFeaturesProvider(ContextProvider):
    """Reads pre-built daily rolling regime features from a JSON file.

    If the file is absent or the date is missing the provider returns {}
    and logs a debug message — the brain continues with DayScore.UNKNOWN.
    """

    name = "daily_features"

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None
        self._cache: Optional[dict[str, Any]] = None

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _feature_file_path()

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        path = self._resolved_path()
        if not path.exists():
            logger.debug("daily_features file not found path=%s", path)
            self._cache = {}
            return self._cache
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("daily_features load failed path=%s error=%s", path, exc)
            self._cache = {}
            return self._cache

        # Normalise list-of-records to date-keyed dict
        if isinstance(raw, list):
            result: dict[str, Any] = {}
            for rec in raw:
                if isinstance(rec, dict) and "date" in rec:
                    result[str(rec["date"])] = {k: v for k, v in rec.items() if k != "date"}
            self._cache = result
        elif isinstance(raw, dict):
            self._cache = raw
        else:
            logger.warning("daily_features unexpected format path=%s", path)
            self._cache = {}
        return self._cache

    def provide(self, trade_date: date) -> dict[str, Any]:
        data = self._load()
        date_str = trade_date.isoformat()
        entry = data.get(date_str)
        if not isinstance(entry, dict):
            logger.debug(
                "daily_features no entry for date=%s path=%s",
                date_str,
                self._resolved_path(),
            )
            return {}

        result: dict[str, Any] = {}
        for key in (
            "regime_rv20",
            "regime_dist_sma20",
            "regime_sma20_slope",
            "regime_60d_return",
            "vix_level",
        ):
            if key in entry and entry[key] is not None:
                try:
                    result[f"{_CONTEXT_PREFIX}{key}"] = float(entry[key])
                except (TypeError, ValueError):
                    pass

        if result:
            day_score_hint = self._score_hint(result)
            if day_score_hint:
                result[f"{_CONTEXT_PREFIX}day_score_hint"] = day_score_hint

        return result

    @staticmethod
    def _score_hint(ctx: dict[str, Any]) -> Optional[str]:
        """Derive a preliminary DayScore hint from raw feature values.

        The TradingBrain uses this as an input to its own scoring logic;
        it is NOT the authoritative DayScore.
        """
        rv20 = ctx.get(f"{_CONTEXT_PREFIX}regime_rv20")
        slope = ctx.get(f"{_CONTEXT_PREFIX}regime_sma20_slope")
        if rv20 is None:
            return None
        if float(rv20) > _RV20_VOLATILE_MIN:
            return "VOLATILE"
        if float(rv20) < _RV20_CALM_MAX and slope is not None and float(slope) >= _SMA_SLOPE_POSITIVE_MIN:
            return "CALM"
        return "NEUTRAL"


__all__ = ["DailyFeaturesProvider"]
