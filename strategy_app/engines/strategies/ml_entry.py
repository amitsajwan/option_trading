"""ML-only entry strategy: emit ENTRY votes from the Stage-1 entry model.

The entry *trigger* is the ML probability gate; the *direction* is resolved by
the shared :mod:`entry_direction_policy` (so VOL_GATE_ENTRY can reuse identical
direction logic with a different trigger).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ...contracts import (
    BaseStrategy,
    Direction,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyVote,
)
from ...market.snapshot_accessor import SnapshotAccessor
from ...ml.bundle_inference import load_joblib_bundle, predict_positive_class_prob
from .entry_cost_gate import evaluate_cost_gate
from .entry_direction_policy import _env_float, resolve_direction_for_entry

logger = logging.getLogger(__name__)

STRATEGY_NAME = "ML_ENTRY"
_ENTRY_BUNDLE_KIND = "entry_only_bundle"


class _EntryModel:
    """One entry model: a joblib bundle plus its own probability threshold."""

    __slots__ = ("label", "path", "min_prob", "bundle")

    def __init__(self, label: str, path: str, min_prob: float) -> None:
        self.label = label
        self.path = path
        self.min_prob = min_prob
        self.bundle: Optional[dict[str, Any]] = None


class MlEntryStrategy(BaseStrategy):
    """Entry votes driven by one or more Stage-1 entry models combined with OR.

    Model 1 is ENTRY_ML_MODEL_PATH (threshold ENTRY_ML_MIN_PROB). Optional
    additional models (ENTRY_ML_MODEL_PATH_2 / ENTRY_ML_MIN_PROB_2, e.g. the
    10-minute-horizon compression model) are combined with **OR**: the entry
    trigger fires when *any* configured model's probability clears its own
    threshold. With only model 1 configured, behaviour is identical to before.
    """

    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._min_prob: float = _env_float("ENTRY_ML_MIN_PROB", 0.55)
        # Cache of loaded bundles keyed by path so per-model loads are reused
        # across bars (and across reconfiguration of which paths are active).
        self._bundle_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._loaded_signature: tuple = ()
        self._models: list[_EntryModel] = []
        # Data-readiness gate: abstain when too many features are NaN (feature warmup
        # at the open). The compression rolling features (adx_14, bb_width_20, range_30,
        # ...) are NaN until ~10:00-10:30 each day; median-filling them produces inflated
        # probs the threshold can't correct (the warmup-artifact trades). Calibrated:
        # warmed bars have 1-2 structural NaNs (vix_intraday_chg), warmup bars 5-10.
        # Unset → no gate (legacy). Live value: 3. Shared across models (same feature set).
        _mnf = os.getenv("ENTRY_ML_MAX_NAN_FEATURES", "").strip()
        self._max_nan_features: Optional[int] = int(_mnf) if _mnf.isdigit() else None
        self._no_bundle_warned: bool = False

    @staticmethod
    def _model_env_specs() -> list[tuple[str, str, float]]:
        """Read (label, path, min_prob) tuples for every configured entry model.

        Model 1 is ENTRY_ML_MODEL_PATH/ENTRY_ML_MIN_PROB. Additional models use
        the suffixed vars ENTRY_ML_MODEL_PATH_N/ENTRY_ML_MIN_PROB_N (N>=2); each
        falls back to the primary threshold when its own is unset.
        """
        primary_min = _env_float("ENTRY_ML_MIN_PROB", 0.55)
        specs: list[tuple[str, str, float]] = []
        path1 = os.getenv("ENTRY_ML_MODEL_PATH", "").strip()
        if path1:
            specs.append(("m1", path1, primary_min))
        idx = 2
        while True:
            path = os.getenv(f"ENTRY_ML_MODEL_PATH_{idx}", "").strip()
            if not path:
                break
            specs.append((f"m{idx}", path, _env_float(f"ENTRY_ML_MIN_PROB_{idx}", primary_min)))
            idx += 1
        return specs

    def _ensure_models(self) -> list[_EntryModel]:
        """(Re)load the configured entry models, caching bundles by path."""
        specs = self._model_env_specs()
        signature = tuple((label, path, round(min_prob, 6)) for label, path, min_prob in specs)
        if signature == self._loaded_signature and self._models:
            return self._models

        models: list[_EntryModel] = []
        for label, path, min_prob in specs:
            if path in self._bundle_cache:
                bundle = self._bundle_cache[path]
            else:
                bundle = load_joblib_bundle(path, expected_kind=_ENTRY_BUNDLE_KIND)
                self._bundle_cache[path] = bundle
            if bundle is None:
                continue
            model = _EntryModel(label=label, path=path, min_prob=min_prob)
            model.bundle = bundle
            holdout_auc = (bundle.get("holdout_eval") or {}).get("roc_auc")
            logger.info(
                "ml_entry: loaded entry model[%s] path=%s features=%d holdout_auc=%s min_prob=%.2f",
                label,
                path,
                len(bundle.get("features") or []),
                holdout_auc,
                min_prob,
            )
            models.append(model)

        self._models = models
        self._loaded_signature = signature
        if models:
            logger.info(
                "ml_entry: %d entry model(s) active, combine=OR (any model passes → fire)",
                len(models),
            )
        return models

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        if position is not None:
            return None
        snap = SnapshotAccessor(snapshot)
        snap_id = snap.snapshot_id or "unknown"
        models = self._ensure_models()
        if not models:
            if not self._no_bundle_warned:
                path = os.getenv("ENTRY_ML_MODEL_PATH", "").strip()
                logger.warning(
                    "ml_entry: no entry models loaded ENTRY_ML_MODEL_PATH=%r — ML_ENTRY will produce no votes (logged once)",
                    path or "<not set>",
                )
                self._no_bundle_warned = True
            return None
        self._no_bundle_warned = False  # reset if a bundle later becomes available

        # Score every model; a model "passes" when its prob clears its own threshold.
        # OR combination: the entry trigger fires when ANY model passes.
        per_model: list[dict[str, Any]] = []
        passing: list[tuple[_EntryModel, float]] = []
        for model in models:
            prob = predict_positive_class_prob(
                model.bundle, snap, max_nan_features=self._max_nan_features
            )
            entry = {
                "label": model.label,
                "prob": round(float(prob), 4) if prob is not None else None,
                "threshold": round(float(model.min_prob), 4),
                "passed": bool(prob is not None and prob >= model.min_prob),
            }
            per_model.append(entry)
            if entry["passed"]:
                passing.append((model, float(prob)))

        if all(m["prob"] is None for m in per_model):
            logger.warning(
                "ml_entry: all models returned None snap=%s — check bundle_inference logs above for NaN/error details",
                snap_id,
            )
            try:
                from ...runtime.eval_context import set_entry_diag
                set_entry_diag({
                    "error": "prediction_failed",
                    "snapshot_id": snap_id,
                    "threshold": round(float(self._min_prob), 4),
                    "fired": False,
                    "entry_models": per_model,
                })
            except Exception:
                pass
            return None

        # The deciding model is the passing one with the largest margin over its
        # own threshold; its prob/threshold drive confidence + the trace's primary
        # entry_prob. Falls back to the primary model's values for the per-bar diag.
        primary = per_model[0]
        primary_prob = primary["prob"] if primary["prob"] is not None else 0.0
        primary_thr = primary["threshold"]
        fired_any = bool(passing)

        # Record the per-bar distribution (incl. declines) before the threshold gate.
        try:
            from ...runtime.eval_context import set_entry_diag
            set_entry_diag({
                "entry_prob": round(float(primary_prob), 4),
                "threshold": round(float(primary_thr), 4),
                "fired": fired_any,
                "snapshot_id": snap_id,
                "entry_models": per_model,
            })
        except Exception:
            pass
        if not fired_any:
            logger.debug(
                "ml_entry: no model passed snap=%s models=%s — declined",
                snap_id, per_model,
            )
            return None

        deciding_model, deciding_prob = max(
            passing, key=lambda mp: mp[1] - mp[0].min_prob
        )
        deciding_thr = deciding_model.min_prob

        # Cost-ratio gate (arm B): ML floor selects the setup; this removes setups
        # whose expected move is too small to clear cost. Direction-agnostic, can only
        # remove. Fail-safe: missing inputs → pass. Recorded on entry diag either way.
        cost_gate = evaluate_cost_gate(snap)
        try:
            from ...runtime.eval_context import set_entry_diag
            set_entry_diag({
                "entry_prob": round(float(deciding_prob), 4),
                "threshold": round(float(deciding_thr), 4),
                "fired": True,
                "snapshot_id": snap_id,
                "entry_models": per_model,
                "deciding_model": deciding_model.label,
                "cost_gate": {"ok": cost_gate.ok, "reason": cost_gate.reason, **cost_gate.evidence},
            })
        except Exception:
            pass
        if not cost_gate.ok:
            logger.debug(
                "ml_entry: cost_gate blocked snap=%s prob=%.4f %s",
                snap_id, deciding_prob, cost_gate.reason,
            )
            return None

        direction, raw_signals = resolve_direction_for_entry(snap)
        if direction is None:
            logger.warning(
                "ml_entry: direction resolved to None snap=%s prob=%.4f — no vote (check direction policy)",
                snap_id, deciding_prob,
            )
            return None
        passed_labels = [m.label for m, _ in passing]
        raw_signals = {
            "entry_prob": round(deciding_prob, 4),
            "entry_threshold": deciding_thr,
            "entry_models": per_model,
            "entry_models_passed": passed_labels,
            "deciding_model": deciding_model.label,
            **raw_signals,
        }
        # Scale confidence to engine-passing range [0.65, 1.0] so min_confidence=0.65
        # gate does not block ML votes. Same formula pattern as VOL_GATE_ENTRY.
        conf = (
            min(1.0, 0.65 + 0.35 * max(0.0, (deciding_prob - deciding_thr) / deciding_thr))
            if deciding_thr > 0 else 0.65
        )
        premium = snap.atm_ce_close if direction == Direction.CE else snap.atm_pe_close
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=direction,
            confidence=round(conf, 3),
            reason=(
                f"ml_entry[{deciding_model.label}]: prob={deciding_prob:.3f}>={deciding_thr:.2f} "
                f"(passed={','.join(passed_labels)})"
            ),
            raw_signals=raw_signals,
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=premium,
        )
