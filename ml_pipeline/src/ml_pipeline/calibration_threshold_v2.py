import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

from .config import DecisionConfig, TrainConfig
from .train_baseline import FEATURE_PROFILE_FUTURES_OPTIONS_ONLY, FEATURE_PROFILES, build_baseline_pipeline, select_feature_columns
from .walk_forward import build_day_folds


IST = timezone(timedelta(hours=5, minutes=30))
CAL_METHODS: Tuple[str, ...] = ("identity", "platt", "isotonic")
LABEL_TARGET_BASE = "base_label"
LABEL_TARGET_PATH_TP_SL = "path_tp_sl"
LABEL_TARGET_CHOICES: Tuple[str, ...] = (LABEL_TARGET_BASE, LABEL_TARGET_PATH_TP_SL)
SELECTION_MODE_THRESHOLD = "threshold"
SELECTION_MODE_TOPK = "topk"
SELECTION_MODE_CHOICES: Tuple[str, ...] = (SELECTION_MODE_THRESHOLD, SELECTION_MODE_TOPK)


def _rows_for_days(df: pd.DataFrame, day_list: Sequence[str]) -> pd.DataFrame:
    day_set = {str(x) for x in day_list}
    out = df[df["trade_date"].astype(str).isin(day_set)].copy()
    return out.sort_values("timestamp").reset_index(drop=True)


def _prepare_side(df: pd.DataFrame, side: str, label_target: str = LABEL_TARGET_BASE) -> pd.DataFrame:
    valid_col = f"{side}_label_valid"
    ret_col = f"{side}_forward_return"
    target_mode = str(label_target).lower()
    if target_mode == LABEL_TARGET_BASE:
        target_col = f"{side}_label"
        out = df[(df[valid_col] == 1.0) & df[target_col].notna() & df[ret_col].notna()].copy()
        out["target"] = out[target_col].astype(int)
    elif target_mode == LABEL_TARGET_PATH_TP_SL:
        exit_col = f"{side}_path_exit_reason"
        if exit_col not in df.columns:
            raise ValueError(f"missing required column for path_tp_sl target: {exit_col}")
        out = df[(df[valid_col] == 1.0) & df[ret_col].notna() & df[exit_col].notna()].copy()
        mapped = out[exit_col].astype(str).map({"tp": 1, "tp_sl_same_bar": 1, "sl": 0})
        out = out[mapped.notna()].copy()
        out["target"] = mapped.loc[out.index].astype(int)
    else:
        raise ValueError(f"unsupported label_target: {label_target}")
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def _brier(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    return float(brier_score_loss(y_true.astype(int), y_prob.astype(float)))


def _calibrate_probs(
    method: str,
    valid_prob_raw: np.ndarray,
    y_valid: np.ndarray,
    test_prob_raw: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    m = str(method).lower()
    if m == "identity":
        return valid_prob_raw.astype(float), test_prob_raw.astype(float)

    x_valid = np.asarray(valid_prob_raw, dtype=float).reshape(-1, 1)
    x_test = np.asarray(test_prob_raw, dtype=float).reshape(-1, 1)
    yv = np.asarray(y_valid, dtype=int)
    if len(np.unique(yv)) < 2:
        constant = float(np.mean(yv)) if len(yv) else 0.0
        return (
            np.full(len(x_valid), constant, dtype=float),
            np.full(len(x_test), constant, dtype=float),
        )

    if m == "platt":
        clf = LogisticRegression(max_iter=1000, random_state=42)
        clf.fit(x_valid, yv)
        return clf.predict_proba(x_valid)[:, 1], clf.predict_proba(x_test)[:, 1]

    if m == "isotonic":
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(np.asarray(valid_prob_raw, dtype=float), yv.astype(float))
        return iso.predict(np.asarray(valid_prob_raw, dtype=float)), iso.predict(np.asarray(test_prob_raw, dtype=float))

    raise ValueError(f"unsupported calibration method: {method}")


def _reliability_bins(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> List[Dict[str, object]]:
    y_true = np.asarray(y_true, dtype=int)
    y_prob = np.asarray(y_prob, dtype=float)
    if len(y_true) == 0:
        return []
    edges = np.linspace(0.0, 1.0, int(bins) + 1)
    out: List[Dict[str, object]] = []
    for i in range(len(edges) - 1):
        lo = float(edges[i])
        hi = float(edges[i + 1])
        if i == len(edges) - 2:
            mask = (y_prob >= lo) & (y_prob <= hi)
        else:
            mask = (y_prob >= lo) & (y_prob < hi)
        count = int(mask.sum())
        if count == 0:
            out.append(
                {
                    "bin": int(i),
                    "prob_min": lo,
                    "prob_max": hi,
                    "count": 0,
                    "avg_pred": None,
                    "event_rate": None,
                    "abs_gap": None,
                }
            )
            continue
        avg_pred = float(np.mean(y_prob[mask]))
        event_rate = float(np.mean(y_true[mask]))
        out.append(
            {
                "bin": int(i),
                "prob_min": lo,
                "prob_max": hi,
                "count": count,
                "avg_pred": avg_pred,
                "event_rate": event_rate,
                "abs_gap": float(abs(avg_pred - event_rate)),
            }
        )
    return out


def _threshold_grid(min_v: float, max_v: float, step: float) -> List[float]:
    vals = np.arange(float(min_v), float(max_v) + float(step) * 0.5, float(step))
    return [float(round(v, 10)) for v in vals]


def _eval_threshold(prob: np.ndarray, fwd_ret: np.ndarray, threshold: float, cost_per_trade: float) -> Dict[str, float]:
    p = np.asarray(prob, dtype=float)
    r = np.asarray(fwd_ret, dtype=float)
    mask = p >= float(threshold)
    n = len(p)
    trades = int(mask.sum())
    if trades == 0:
        return {
            "threshold": float(threshold),
            "rows": int(n),
            "trades": 0,
            "trade_rate": 0.0,
            "mean_net_per_trade": 0.0,
            "total_net_return": 0.0,
            "mean_net_per_row": 0.0,
        }
    net = r[mask] - float(cost_per_trade)
    total_net = float(np.sum(net))
    return {
        "threshold": float(threshold),
        "rows": int(n),
        "trades": int(trades),
        "trade_rate": float(trades / n) if n > 0 else 0.0,
        "mean_net_per_trade": float(np.mean(net)),
        "total_net_return": total_net,
        "mean_net_per_row": float(total_net / n) if n > 0 else 0.0,
    }


def _eval_topk_per_day(
    prob: np.ndarray,
    fwd_ret: np.ndarray,
    trade_date: np.ndarray,
    topk_per_day: int,
    cost_per_trade: float,
) -> Dict[str, float]:
    p = np.asarray(prob, dtype=float)
    r = np.asarray(fwd_ret, dtype=float)
    d = np.asarray(trade_date, dtype=object)
    n = len(p)
    if n == 0:
        return {
            "topk_per_day": int(topk_per_day),
            "rows": 0,
            "trades": 0,
            "trade_rate": 0.0,
            "mean_net_per_trade": 0.0,
            "total_net_return": 0.0,
            "mean_net_per_row": 0.0,
        }
    work = pd.DataFrame({"trade_date": d.astype(str), "score": p, "ret": r})
    selected = (
        work.sort_values(["trade_date", "score"], ascending=[True, False], kind="mergesort")
        .groupby("trade_date", sort=False)
        .head(max(1, int(topk_per_day)))
    )
    trades = int(len(selected))
    if trades == 0:
        return {
            "topk_per_day": int(topk_per_day),
            "rows": int(n),
            "trades": 0,
            "trade_rate": 0.0,
            "mean_net_per_trade": 0.0,
            "total_net_return": 0.0,
            "mean_net_per_row": 0.0,
        }
    net = selected["ret"].to_numpy(dtype=float) - float(cost_per_trade)
    total_net = float(np.sum(net))
    return {
        "topk_per_day": int(topk_per_day),
        "rows": int(n),
        "trades": int(trades),
        "trade_rate": float(trades / n) if n > 0 else 0.0,
        "mean_net_per_trade": float(np.mean(net)),
        "total_net_return": total_net,
        "mean_net_per_row": float(total_net / n) if n > 0 else 0.0,
    }


def _choose_threshold(prob_valid: np.ndarray, ret_valid: np.ndarray, decision_cfg: DecisionConfig) -> Dict[str, object]:
    grid = _threshold_grid(decision_cfg.threshold_min, decision_cfg.threshold_max, decision_cfg.threshold_step)
    rows = [_eval_threshold(prob_valid, ret_valid, t, decision_cfg.cost_per_trade) for t in grid]
    ordered = sorted(rows, key=lambda x: (x["mean_net_per_trade"], x["trades"], -x["threshold"]), reverse=True)
    best = ordered[0] if ordered else None
    return {
        "grid": rows,
        "selected_threshold": (float(best["threshold"]) if best is not None else None),
        "best_valid": best,
    }


def _aggregate(vals: Sequence[Optional[float]]) -> Dict[str, Optional[float]]:
    numeric = [float(v) for v in vals if v is not None and np.isfinite(v)]
    if not numeric:
        return {"mean": None, "std": None}
    return {"mean": float(np.mean(numeric)), "std": float(np.std(numeric))}


def _run_side(
    frame: pd.DataFrame,
    side: str,
    feature_columns: Sequence[str],
    train_cfg: TrainConfig,
    decision_cfg: DecisionConfig,
    label_target: str,
    selection_mode: str,
    topk_per_day: int,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int,
    embargo_days: int,
    reliability_bins: int,
) -> Dict[str, object]:
    work = _prepare_side(frame, side=side, label_target=label_target)
    days = sorted(work["trade_date"].astype(str).unique().tolist())
    folds = build_day_folds(
        days=days,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )
    target_col = "target"
    ret_col = f"{side}_forward_return"
    method_valid_brier: Dict[str, List[float]] = {m: [] for m in CAL_METHODS}
    method_test_brier: Dict[str, List[float]] = {m: [] for m in CAL_METHODS}
    fold_records: List[Dict[str, object]] = []
    for fold_idx, fold in enumerate(folds, start=1):
        train_df = _rows_for_days(work, fold["train_days"])
        valid_df = _rows_for_days(work, fold["valid_days"])
        test_df = _rows_for_days(work, fold["test_days"])
        if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
            fold_records.append({"fold_index": int(fold_idx), "fold_ok": False, "days": fold, "error": "empty split"})
            continue

        x_train = train_df.loc[:, list(feature_columns)]
        y_train = train_df[target_col].astype(int).to_numpy()
        x_valid = valid_df.loc[:, list(feature_columns)]
        y_valid = valid_df[target_col].astype(int).to_numpy()
        x_test = test_df.loc[:, list(feature_columns)]
        y_test = test_df[target_col].astype(int).to_numpy()

        if len(np.unique(y_train)) < 2:
            constant = float(np.mean(y_train)) if len(y_train) else 0.0
            valid_prob_raw = np.full(len(x_valid), constant, dtype=float)
            test_prob_raw = np.full(len(x_test), constant, dtype=float)
        else:
            model = build_baseline_pipeline(train_cfg)
            model.fit(x_train, y_train)
            valid_prob_raw = model.predict_proba(x_valid)[:, 1]
            test_prob_raw = model.predict_proba(x_test)[:, 1]

        fold_method_brier: Dict[str, Dict[str, float]] = {}
        fold_method_valid_prob: Dict[str, np.ndarray] = {}
        fold_method_test_prob: Dict[str, np.ndarray] = {}
        for method in CAL_METHODS:
            valid_prob, test_prob = _calibrate_probs(method, valid_prob_raw, y_valid, test_prob_raw)
            brier_valid = _brier(y_valid, valid_prob)
            brier_test = _brier(y_test, test_prob)
            method_valid_brier[method].append(brier_valid)
            method_test_brier[method].append(brier_test)
            fold_method_valid_prob[method] = valid_prob
            fold_method_test_prob[method] = test_prob
            fold_method_brier[method] = {"valid_brier": brier_valid, "test_brier": brier_test}

        fold_best_method = sorted(
            CAL_METHODS,
            key=lambda m: (fold_method_brier[m]["valid_brier"], m),
        )[0]

        fold_records.append(
            {
                "fold_index": int(fold_idx),
                "fold_ok": True,
                "days": fold,
                "rows": {"train": int(len(train_df)), "valid": int(len(valid_df)), "test": int(len(test_df))},
                "best_method_by_valid_brier": fold_best_method,
                "method_brier": fold_method_brier,
            }
        )

    method_summary = {
        m: {
            "valid_brier": _aggregate(method_valid_brier[m]),
            "test_brier": _aggregate(method_test_brier[m]),
        }
        for m in CAL_METHODS
    }
    selected_method = sorted(
        CAL_METHODS,
        key=lambda m: (
            float("inf") if method_summary[m]["valid_brier"]["mean"] is None else float(method_summary[m]["valid_brier"]["mean"]),
            m,
        ),
    )[0]

    # Rebuild valid/test arrays for globally selected method.
    valid_probs: List[np.ndarray] = []
    valid_rets: List[np.ndarray] = []
    test_probs: List[np.ndarray] = []
    test_rets: List[np.ndarray] = []
    valid_days_arr: List[np.ndarray] = []
    test_days_arr: List[np.ndarray] = []
    test_labels: List[np.ndarray] = []
    selected_frames_valid: List[pd.DataFrame] = []
    selected_frames_test: List[pd.DataFrame] = []
    for fold_idx, fold in enumerate(folds, start=1):
        train_df = _rows_for_days(work, fold["train_days"])
        valid_df = _rows_for_days(work, fold["valid_days"])
        test_df = _rows_for_days(work, fold["test_days"])
        if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
            continue
        x_train = train_df.loc[:, list(feature_columns)]
        y_train = train_df[target_col].astype(int).to_numpy()
        x_valid = valid_df.loc[:, list(feature_columns)]
        y_valid = valid_df[target_col].astype(int).to_numpy()
        x_test = test_df.loc[:, list(feature_columns)]
        y_test = test_df[target_col].astype(int).to_numpy()
        if len(np.unique(y_train)) < 2:
            constant = float(np.mean(y_train)) if len(y_train) else 0.0
            valid_prob_raw = np.full(len(x_valid), constant, dtype=float)
            test_prob_raw = np.full(len(x_test), constant, dtype=float)
        else:
            model = build_baseline_pipeline(train_cfg)
            model.fit(x_train, y_train)
            valid_prob_raw = model.predict_proba(x_valid)[:, 1]
            test_prob_raw = model.predict_proba(x_test)[:, 1]
        valid_prob, test_prob = _calibrate_probs(selected_method, valid_prob_raw, y_valid, test_prob_raw)
        valid_probs.append(valid_prob)
        valid_rets.append(valid_df[ret_col].to_numpy(dtype=float))
        test_probs.append(test_prob)
        test_rets.append(test_df[ret_col].to_numpy(dtype=float))
        valid_days_arr.append(valid_df["trade_date"].astype(str).to_numpy())
        test_days_arr.append(test_df["trade_date"].astype(str).to_numpy())
        test_labels.append(y_test.astype(int))

        v = valid_df.loc[:, ["timestamp", "trade_date", ret_col]].copy()
        t = test_df.loc[:, ["timestamp", "trade_date", ret_col]].copy()
        v[f"{side}_prob"] = valid_prob
        t[f"{side}_prob"] = test_prob
        v = v.rename(columns={ret_col: f"{side}_forward_return"})
        t = t.rename(columns={ret_col: f"{side}_forward_return"})
        selected_frames_valid.append(v)
        selected_frames_test.append(t)

    if valid_probs:
        v_prob = np.concatenate(valid_probs)
        v_ret = np.concatenate(valid_rets)
        v_day = np.concatenate(valid_days_arr)
        t_prob = np.concatenate(test_probs)
        t_ret = np.concatenate(test_rets)
        t_day = np.concatenate(test_days_arr)
        t_lbl = np.concatenate(test_labels)
    else:
        v_prob = np.array([], dtype=float)
        v_ret = np.array([], dtype=float)
        v_day = np.array([], dtype=object)
        t_prob = np.array([], dtype=float)
        t_ret = np.array([], dtype=float)
        t_day = np.array([], dtype=object)
        t_lbl = np.array([], dtype=int)
    mode = str(selection_mode).lower()
    if mode == SELECTION_MODE_THRESHOLD:
        thr = _choose_threshold(v_prob, v_ret, decision_cfg)
        selected_threshold = thr["selected_threshold"]
        test_eval = (
            _eval_threshold(t_prob, t_ret, selected_threshold, decision_cfg.cost_per_trade)
            if selected_threshold is not None
            else None
        )
    elif mode == SELECTION_MODE_TOPK:
        valid_eval = _eval_topk_per_day(
            prob=v_prob,
            fwd_ret=v_ret,
            trade_date=v_day,
            topk_per_day=topk_per_day,
            cost_per_trade=decision_cfg.cost_per_trade,
        )
        thr = {
            "grid": [],
            "selected_threshold": None,
            "best_valid": None,
            "selection_mode": SELECTION_MODE_TOPK,
            "topk_per_day": int(topk_per_day),
            "valid_eval": valid_eval,
        }
        test_eval = _eval_topk_per_day(
            prob=t_prob,
            fwd_ret=t_ret,
            trade_date=t_day,
            topk_per_day=topk_per_day,
            cost_per_trade=decision_cfg.cost_per_trade,
        )
    else:
        raise ValueError(f"unsupported selection_mode: {selection_mode}")
    reliability = _reliability_bins(t_lbl, t_prob, bins=reliability_bins)

    return {
        "rows_total": int(len(work)),
        "days_total": int(len(days)),
        "fold_count": int(len(folds)),
        "fold_ok_count": int(sum(1 for x in fold_records if x.get("fold_ok"))),
        "calibration_method_summary": method_summary,
        "selected_calibration_method": selected_method,
        "threshold_search": thr,
        "test_threshold_eval": test_eval,
        "reliability_bins_test": reliability,
        "folds": fold_records,
        "selected_mode_frames": {
            "valid": selected_frames_valid,
            "test": selected_frames_test,
        },
    }


def _dual_eval(
    ce_payload: Dict[str, object],
    pe_payload: Dict[str, object],
    ce_threshold: Optional[float],
    pe_threshold: Optional[float],
    cost_per_trade: float,
    selection_mode: str,
    topk_per_day: int,
) -> Dict[str, object]:
    ce_frames = ce_payload["selected_mode_frames"]["test"]
    pe_frames = pe_payload["selected_mode_frames"]["test"]
    if not ce_frames or not pe_frames:
        return {
            "rows_total": 0,
            "trades_total": 0,
            "trade_rate": 0.0,
            "ce_trades": 0,
            "pe_trades": 0,
            "net_return_sum": 0.0,
            "mean_net_return_per_trade": 0.0,
            "win_rate": 0.0,
        }

    mode = str(selection_mode).lower()
    merged_rows = 0
    ce_trades = 0
    pe_trades = 0
    net_returns: List[float] = []
    for ce_df, pe_df in zip(ce_frames, pe_frames):
        joined = ce_df.merge(pe_df, on=["timestamp", "trade_date"], how="inner")
        if len(joined) == 0:
            continue
        merged_rows += int(len(joined))
        if mode == SELECTION_MODE_THRESHOLD:
            ce_thr = float(ce_threshold if ce_threshold is not None else 1.1)
            pe_thr = float(pe_threshold if pe_threshold is not None else 1.1)
            for row in joined.itertuples(index=False):
                ce_prob = float(getattr(row, "ce_prob"))
                pe_prob = float(getattr(row, "pe_prob"))
                side = None
                if ce_prob >= ce_thr and pe_prob >= pe_thr:
                    side = "CE" if ce_prob >= pe_prob else "PE"
                elif ce_prob >= ce_thr:
                    side = "CE"
                elif pe_prob >= pe_thr:
                    side = "PE"
                if side is None:
                    continue
                if side == "CE":
                    ce_trades += 1
                    gross = float(getattr(row, "ce_forward_return"))
                else:
                    pe_trades += 1
                    gross = float(getattr(row, "pe_forward_return"))
                net_returns.append(gross - float(cost_per_trade))
        elif mode == SELECTION_MODE_TOPK:
            ranked = joined.copy()
            ranked["side"] = np.where(ranked["ce_prob"].to_numpy(dtype=float) >= ranked["pe_prob"].to_numpy(dtype=float), "CE", "PE")
            ranked["score"] = np.where(
                ranked["side"] == "CE",
                ranked["ce_prob"].to_numpy(dtype=float),
                ranked["pe_prob"].to_numpy(dtype=float),
            )
            ranked["gross"] = np.where(
                ranked["side"] == "CE",
                ranked["ce_forward_return"].to_numpy(dtype=float),
                ranked["pe_forward_return"].to_numpy(dtype=float),
            )
            chosen = (
                ranked.sort_values(["trade_date", "score"], ascending=[True, False], kind="mergesort")
                .groupby("trade_date", sort=False)
                .head(max(1, int(topk_per_day)))
            )
            if len(chosen) == 0:
                continue
            ce_trades += int((chosen["side"] == "CE").sum())
            pe_trades += int((chosen["side"] == "PE").sum())
            net_returns.extend((chosen["gross"].to_numpy(dtype=float) - float(cost_per_trade)).tolist())
        else:
            raise ValueError(f"unsupported selection_mode: {selection_mode}")

    trades_total = int(len(net_returns))
    net_sum = float(np.sum(net_returns)) if net_returns else 0.0
    return {
        "selection_mode": mode,
        "topk_per_day": (int(topk_per_day) if mode == SELECTION_MODE_TOPK else None),
        "rows_total": int(merged_rows),
        "trades_total": trades_total,
        "trade_rate": (float(trades_total / merged_rows) if merged_rows > 0 else 0.0),
        "ce_trades": int(ce_trades),
        "pe_trades": int(pe_trades),
        "net_return_sum": net_sum,
        "mean_net_return_per_trade": (float(np.mean(net_returns)) if net_returns else 0.0),
        "win_rate": (float(np.mean(np.asarray(net_returns) > 0.0)) if net_returns else 0.0),
    }


def run_calibration_threshold_v2(
    labeled_df: pd.DataFrame,
    train_cfg: TrainConfig,
    decision_cfg: DecisionConfig,
    feature_profile: str = FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    label_target: str = LABEL_TARGET_BASE,
    selection_mode: str = SELECTION_MODE_THRESHOLD,
    topk_per_day: int = 10,
    train_days: int = 180,
    valid_days: int = 30,
    test_days: int = 30,
    step_days: int = 30,
    purge_days: int = 1,
    embargo_days: int = 1,
    reliability_bins: int = 10,
) -> Dict[str, object]:
    target_mode = str(label_target).lower()
    if target_mode not in LABEL_TARGET_CHOICES:
        raise ValueError(f"unsupported label_target: {label_target}")
    policy_mode = str(selection_mode).lower()
    if policy_mode not in SELECTION_MODE_CHOICES:
        raise ValueError(f"unsupported selection_mode: {selection_mode}")
    if int(topk_per_day) <= 0:
        raise ValueError("topk_per_day must be >= 1")
    frame = labeled_df.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    feature_columns = select_feature_columns(frame, feature_profile=feature_profile)
    if not feature_columns:
        raise ValueError("no feature columns for calibration/thresholding")

    ce = _run_side(
        frame=frame,
        side="ce",
        feature_columns=feature_columns,
        train_cfg=train_cfg,
        decision_cfg=decision_cfg,
        label_target=target_mode,
        selection_mode=policy_mode,
        topk_per_day=int(topk_per_day),
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
        reliability_bins=reliability_bins,
    )
    pe = _run_side(
        frame=frame,
        side="pe",
        feature_columns=feature_columns,
        train_cfg=train_cfg,
        decision_cfg=decision_cfg,
        label_target=target_mode,
        selection_mode=policy_mode,
        topk_per_day=int(topk_per_day),
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
        reliability_bins=reliability_bins,
    )

    ce_thr = ce["threshold_search"]["selected_threshold"] if policy_mode == SELECTION_MODE_THRESHOLD else None
    pe_thr = pe["threshold_search"]["selected_threshold"] if policy_mode == SELECTION_MODE_THRESHOLD else None
    dual = _dual_eval(
        ce_payload=ce,
        pe_payload=pe,
        ce_threshold=(float(ce_thr) if ce_thr is not None else None),
        pe_threshold=(float(pe_thr) if pe_thr is not None else None),
        cost_per_trade=float(decision_cfg.cost_per_trade),
        selection_mode=policy_mode,
        topk_per_day=int(topk_per_day),
    )

    # strip non-serializable runtime frames from report payload
    ce_serial = dict(ce)
    pe_serial = dict(pe)
    ce_serial.pop("selected_mode_frames", None)
    pe_serial.pop("selected_mode_frames", None)

    return {
        "created_at_ist": datetime.now(IST).isoformat(),
        "task": "T31",
        "status": "completed",
        "rows_total": int(len(frame)),
        "days_total": int(frame["trade_date"].astype(str).nunique()) if "trade_date" in frame.columns else 0,
        "feature_profile": str(feature_profile),
        "label_target": target_mode,
        "feature_count": int(len(feature_columns)),
        "feature_columns": feature_columns,
        "train_config": asdict(train_cfg),
        "decision_config": asdict(decision_cfg),
        "walk_forward_config": {
            "train_days": int(train_days),
            "valid_days": int(valid_days),
            "test_days": int(test_days),
            "step_days": int(step_days),
            "purge_days": int(purge_days),
            "embargo_days": int(embargo_days),
        },
        "reliability_bins": int(reliability_bins),
        "ce": ce_serial,
        "pe": pe_serial,
        "dual_mode_policy": {
            "selection_mode": policy_mode,
            "topk_per_day": (int(topk_per_day) if policy_mode == SELECTION_MODE_TOPK else None),
            "ce_threshold": ce_thr,
            "pe_threshold": pe_thr,
            "test_eval": dual,
        },
    }


def _summary_md(report: Dict[str, object]) -> str:
    ce = report["ce"]
    pe = report["pe"]
    policy = report["dual_mode_policy"]
    dual = policy["test_eval"]
    selection_mode = str(policy.get("selection_mode", SELECTION_MODE_THRESHOLD))
    lines = [
        "# T31 Calibration + Thresholding V2 Summary",
        "",
        f"- Created (IST): `{report['created_at_ist']}`",
        f"- Rows: `{report['rows_total']}` over `{report['days_total']}` days",
        f"- Feature profile: `{report['feature_profile']}` (`{report['feature_count']}` features)",
        f"- Label target: `{report.get('label_target', LABEL_TARGET_BASE)}`",
        "",
        "## Calibration Selection",
        f"- CE selected method: `{ce['selected_calibration_method']}`",
        f"- PE selected method: `{pe['selected_calibration_method']}`",
        "",
        "## Execution Policy",
        f"- Selection mode: `{selection_mode}`",
    ]
    if selection_mode == SELECTION_MODE_TOPK:
        lines.append(f"- Top-k per day: `{policy.get('topk_per_day')}`")
    else:
        lines.append(f"- CE threshold: `{policy.get('ce_threshold')}`")
        lines.append(f"- PE threshold: `{policy.get('pe_threshold')}`")
    lines.extend(
        [
            "",
            "## Dual-Mode Test Evaluation",
            f"- Rows: `{dual['rows_total']}`",
            f"- Trades: `{dual['trades_total']}`",
            f"- Trade rate: `{dual['trade_rate']}`",
            f"- Net return sum: `{dual['net_return_sum']}`",
            f"- Mean net/trade: `{dual['mean_net_return_per_trade']}`",
            f"- Win rate: `{dual['win_rate']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T31 calibration + thresholding V2")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t29_2y_auto_t05_labeled_features.parquet")
    parser.add_argument("--feature-profile", default=FEATURE_PROFILE_FUTURES_OPTIONS_ONLY, choices=list(FEATURE_PROFILES))
    parser.add_argument("--label-target", default=LABEL_TARGET_BASE, choices=list(LABEL_TARGET_CHOICES))
    parser.add_argument("--selection-mode", default=SELECTION_MODE_THRESHOLD, choices=list(SELECTION_MODE_CHOICES))
    parser.add_argument("--topk-per-day", type=int, default=10)
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--valid-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument("--purge-days", type=int, default=1)
    parser.add_argument("--embargo-days", type=int, default=1)
    parser.add_argument("--threshold-min", type=float, default=None)
    parser.add_argument("--threshold-max", type=float, default=None)
    parser.add_argument("--threshold-step", type=float, default=None)
    parser.add_argument("--cost-per-trade", type=float, default=None)
    parser.add_argument("--random-state", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--n-estimators", type=int, default=None)
    parser.add_argument("--learning-rate", type=float, default=None)
    parser.add_argument("--reliability-bins", type=int, default=10)
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t31_calibration_threshold_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t31_calibration_threshold_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    data_path = Path(args.labeled_data)
    if not data_path.exists():
        print(f"ERROR: labeled dataset not found: {data_path}")
        return 2
    df = pd.read_parquet(data_path)

    default_train = TrainConfig()
    train_cfg = TrainConfig(
        train_ratio=default_train.train_ratio,
        valid_ratio=default_train.valid_ratio,
        random_state=int(args.random_state) if args.random_state is not None else default_train.random_state,
        max_depth=int(args.max_depth) if args.max_depth is not None else default_train.max_depth,
        n_estimators=int(args.n_estimators) if args.n_estimators is not None else default_train.n_estimators,
        learning_rate=float(args.learning_rate) if args.learning_rate is not None else default_train.learning_rate,
    )
    default_decision = DecisionConfig()
    decision_cfg = DecisionConfig(
        threshold_min=float(args.threshold_min) if args.threshold_min is not None else default_decision.threshold_min,
        threshold_max=float(args.threshold_max) if args.threshold_max is not None else default_decision.threshold_max,
        threshold_step=float(args.threshold_step) if args.threshold_step is not None else default_decision.threshold_step,
        cost_per_trade=float(args.cost_per_trade) if args.cost_per_trade is not None else default_decision.cost_per_trade,
    )

    report = run_calibration_threshold_v2(
        labeled_df=df,
        train_cfg=train_cfg,
        decision_cfg=decision_cfg,
        feature_profile=str(args.feature_profile),
        label_target=str(args.label_target),
        selection_mode=str(args.selection_mode),
        topk_per_day=int(args.topk_per_day),
        train_days=int(args.train_days),
        valid_days=int(args.valid_days),
        test_days=int(args.test_days),
        step_days=int(args.step_days),
        purge_days=int(args.purge_days),
        embargo_days=int(args.embargo_days),
        reliability_bins=int(args.reliability_bins),
    )
    report_path = Path(args.report_out)
    summary_path = Path(args.summary_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path.write_text(_summary_md(report), encoding="utf-8")

    print(f"Rows: {report['rows_total']}")
    print(f"CE method: {report['ce']['selected_calibration_method']}")
    print(f"PE method: {report['pe']['selected_calibration_method']}")
    print(f"Selection mode: {report['dual_mode_policy']['selection_mode']}")
    if report["dual_mode_policy"]["selection_mode"] == SELECTION_MODE_TOPK:
        print(f"Top-k/day: {report['dual_mode_policy']['topk_per_day']}")
    else:
        print(f"CE threshold: {report['dual_mode_policy']['ce_threshold']}")
        print(f"PE threshold: {report['dual_mode_policy']['pe_threshold']}")
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
