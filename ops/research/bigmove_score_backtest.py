"""BigMoveScore Phase-0 backtest for the Intelligent Brain gate.

Direction-agnostic detector: "is a large BankNifty futures move loading?"
No ML, no engine path, no costs. This proof measures opportunity only; the
cost-aware P&L gate is B-2.6.

Run inside a container with Mongo access:
    python ops/research/bigmove_score_backtest.py

Environment:
    MONGO_HOST=mongo
    MONGO_PORT=27017
    MONGO_DB=trading_ai
    BIGMOVE_SOURCE_COLL=phase1_market_snapshots
    BIGMOVE_DAYS=2026-05-26,2026-05-27   # optional; default: all dates in coll
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Iterable, Mapping


HORIZON = 10
COMPRESS_RATIO = 0.70
VOL_SPIKE = 1.8
VELOCITY_K = 1.5
OI_BUILD = 1.002
RELEASE_WINDOW = 3
TARGETS = (50.0, 100.0, 200.0)


@dataclass(frozen=True)
class Observation:
    day: str
    index: int
    move_pt: float
    score: int
    compression_tightness: float
    compression: bool
    oi_build: bool
    velocity: bool
    volume: bool
    loaded: bool
    released_strict: bool
    released_or: bool
    released_window: bool


def _tr(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr(highs: list[float], lows: list[float], closes: list[float]) -> float:
    return sum(_tr(highs[k], lows[k], closes[k - 1]) for k in range(1, len(highs))) / max(len(highs) - 1, 1)


def _none_in(values: Iterable[object]) -> bool:
    return any(value is None for value in values)


def _bar_from_doc(doc: Mapping[str, object]) -> dict[str, float | None]:
    payload = doc.get("payload") if isinstance(doc, Mapping) else None
    snapshot = (payload or {}).get("snapshot") if isinstance(payload, Mapping) else {}
    futures_bar = (snapshot or {}).get("futures_bar") if isinstance(snapshot, Mapping) else {}
    chain = (snapshot or {}).get("chain_aggregates") if isinstance(snapshot, Mapping) else {}
    if not isinstance(futures_bar, Mapping):
        futures_bar = {}
    if not isinstance(chain, Mapping):
        chain = {}
    return {
        "c": _as_float(futures_bar.get("fut_close")),
        "h": _as_float(futures_bar.get("fut_high")),
        "l": _as_float(futures_bar.get("fut_low")),
        "ovol": _as_float(chain.get("total_ce_volume"), 0.0) + _as_float(chain.get("total_pe_volume"), 0.0),
        "ooi": _as_float(chain.get("total_ce_oi"), 0.0) + _as_float(chain.get("total_pe_oi"), 0.0),
    }


def _as_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _component_flags(bars: list[dict[str, float | None]], index: int) -> dict[str, bool | float] | None:
    if index < 42 or index >= len(bars):
        return None
    bar = bars[index]
    prev = bars[index - 1]
    if bar["c"] is None or prev["c"] is None:
        return None

    highs = [x["h"] for x in bars[index - 15:index]]
    lows = [x["l"] for x in bars[index - 15:index]]
    closes = [x["c"] for x in bars[index - 16:index]]
    base_highs = [x["h"] for x in bars[index - 41:index - 15]]
    base_lows = [x["l"] for x in bars[index - 41:index - 15]]
    base_closes = [x["c"] for x in bars[index - 42:index - 15]]
    if _none_in(highs + lows + closes + base_highs + base_lows + base_closes):
        return None

    atr_build = _atr(highs, lows, closes)  # type: ignore[arg-type]
    atr_base = _atr(base_highs, base_lows, base_closes)  # type: ignore[arg-type]
    vol_build = sum(float(x["ovol"] or 0.0) for x in bars[index - 15:index]) / 15.0
    ret = abs(float(bar["c"]) - float(prev["c"]))
    compression = bool(atr_base and atr_build < COMPRESS_RATIO * atr_base)
    oi_build = bool(bar["ooi"] and bars[index - 15]["ooi"] and float(bar["ooi"]) > float(bars[index - 15]["ooi"]) * OI_BUILD)
    velocity = bool(atr_build and ret > VELOCITY_K * atr_build)
    volume = bool(vol_build and float(bar["ovol"] or 0.0) > VOL_SPIKE * vol_build)
    return {
        "compression": compression,
        "oi_build": oi_build,
        "velocity": velocity,
        "volume": volume,
        "compression_tightness": atr_build / atr_base if atr_base else 0.0,
    }


def collect_observations(
    days_bars: Mapping[str, list[dict[str, float | None]]],
    *,
    horizon: int = HORIZON,
    release_window: int = RELEASE_WINDOW,
) -> list[Observation]:
    observations: list[Observation] = []
    for day, bars in days_bars.items():
        flag_cache: dict[int, dict[str, bool | float] | None] = {}
        for index, bar in enumerate(bars):
            if index < 42 or bar.get("c") is None:
                continue
            future = [x for x in bars[index + 1:index + 1 + horizon] if x.get("h") is not None and x.get("l") is not None]
            if not future:
                continue
            flags = flag_cache.setdefault(index, _component_flags(bars, index))
            if flags is None:
                continue
            close = float(bar["c"])
            move_pt = max(max(float(x["h"]) for x in future) - close, close - min(float(x["l"]) for x in future))
            loaded = flags["compression"] and flags["oi_build"]
            released_or = flags["velocity"] or flags["volume"]
            window_start = max(42, index - max(1, release_window) + 1)
            released_window = False
            for window_index in range(window_start, index + 1):
                window_flags = flag_cache.setdefault(window_index, _component_flags(bars, window_index))
                released_window = released_window or bool(window_flags and (window_flags["velocity"] or window_flags["volume"]))
            observations.append(
                Observation(
                    day=day,
                    index=index,
                    move_pt=move_pt,
                    score=sum(1 for key in ("compression", "oi_build", "velocity", "volume") if flags[key]),
                    compression_tightness=float(flags["compression_tightness"]),
                    compression=flags["compression"],
                    oi_build=flags["oi_build"],
                    velocity=flags["velocity"],
                    volume=flags["volume"],
                    loaded=loaded,
                    released_strict=flags["velocity"] and flags["volume"],
                    released_or=released_or,
                    released_window=released_window,
                )
            )
    return observations


def stats(values: Iterable[float]) -> dict[str, float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"n": 0}
    return {
        "n": float(len(ordered)),
        "mean": sum(ordered) / len(ordered),
        "median": _quantile(ordered, 0.50),
        "p75": _quantile(ordered, 0.75),
        "p90": _quantile(ordered, 0.90),
        "hit_50": _hit_rate(ordered, 50.0),
        "hit_100": _hit_rate(ordered, 100.0),
        "hit_200": _hit_rate(ordered, 200.0),
    }


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _hit_rate(ordered: Iterable[float], target: float) -> float:
    values = list(ordered)
    if not values:
        return 0.0
    return sum(1 for value in values if value >= target) / len(values)


def score_bucket_rows(observations: Iterable[Observation]) -> list[dict[str, object]]:
    observations_list = list(observations)
    rows: list[dict[str, object]] = []
    for score in range(5):
        bucket = [obs for obs in observations_list if obs.score == score]
        row: dict[str, object] = {"bucket": str(score), **stats(obs.move_pt for obs in bucket)}
        for component in ("compression", "oi_build", "velocity", "volume", "loaded"):
            row[f"{component}_rate"] = _bool_rate(getattr(obs, component) for obs in bucket)
        rows.append(row)
    rows.append({"bucket": "loaded", **stats(obs.move_pt for obs in observations_list if obs.loaded)})
    return rows


def release_rows(observations: Iterable[Observation]) -> list[dict[str, object]]:
    observations_list = list(observations)
    variants = (
        ("strict_and", lambda obs: obs.released_strict),
        ("current_or", lambda obs: obs.released_or),
        (f"{RELEASE_WINDOW}bar_or", lambda obs: obs.released_window),
    )
    rows = []
    for name, predicate in variants:
        released = [obs for obs in observations_list if predicate(obs)]
        loaded_released = [obs for obs in observations_list if obs.loaded and predicate(obs)]
        rows.append({"bucket": name, **stats(obs.move_pt for obs in released)})
        rows.append({"bucket": f"loaded+{name}", **stats(obs.move_pt for obs in loaded_released)})
    return rows


def compression_tightness_rows(observations: Iterable[Observation]) -> list[dict[str, object]]:
    observations_list = list(observations)
    buckets: tuple[tuple[str, float, float], ...] = (
        ("tight_<=0.50", 0.0, 0.50),
        ("tight_0.50_0.60", 0.50, 0.60),
        ("tight_0.60_0.70", 0.60, COMPRESS_RATIO),
        ("not_compressed", COMPRESS_RATIO, float("inf")),
    )
    rows: list[dict[str, object]] = []
    for label, low, high in buckets:
        if math.isinf(high):
            bucket = [obs for obs in observations_list if obs.compression_tightness >= low]
        else:
            bucket = [obs for obs in observations_list if low <= obs.compression_tightness < high]
        rows.append({"bucket": label, **stats(obs.move_pt for obs in bucket)})
    return rows


def compression_tightness_notes(rows: list[dict[str, object]], *, min_n: int = 20) -> list[str]:
    eligible = [row for row in rows if str(row.get("bucket", "")).startswith("tight_") and int(float(row.get("n", 0.0))) >= min_n]
    if len(eligible) < 2:
        return [f"INCONCLUSIVE: fewer than two compressed tightness buckets have n>={min_n}."]
    notes: list[str] = []
    previous = eligible[0]
    for row in eligible[1:]:
        previous_hit = float(previous["hit_100"])
        current_hit = float(row["hit_100"])
        if current_hit > previous_hit:
            notes.append(
                "WARN: compression tightness inverted for >=100pt hit-rate; "
                f"{previous['bucket']}={previous_hit * 100:.1f}% vs {row['bucket']}={current_hit * 100:.1f}%. "
                "Tighter compression should not underperform a looser compressed bucket on a large sample."
            )
        previous = row
    if not notes:
        notes.append(f"PASS: looser compressed buckets do not beat tighter buckets by >=100pt hit-rate for n>={min_n}.")
    return notes


def day_coverage_rows(
    days: Iterable[str],
    days_bars: Mapping[str, list[dict[str, float | None]]],
    observations: Iterable[Observation],
) -> list[dict[str, object]]:
    observations_list = list(observations)
    rows: list[dict[str, object]] = []
    for day in days:
        day_observations = [obs for obs in observations_list if obs.day == day]
        rows.append(
            {
                "day": day,
                "bars": len(days_bars.get(day, [])),
                "eligible": len(day_observations),
                "loaded": sum(1 for obs in day_observations if obs.loaded),
                "hit_100": _hit_rate((obs.move_pt for obs in day_observations), 100.0),
            }
        )
    return rows


def monotonicity_notes(rows: list[dict[str, object]], metric: str = "median") -> list[str]:
    notes: list[str] = []
    previous: tuple[int, dict[str, object]] | None = None
    for row in rows:
        bucket = row.get("bucket")
        if not isinstance(bucket, str) or not bucket.isdigit() or int(float(row.get("n", 0.0))) == 0:
            continue
        score = int(bucket)
        if previous is not None and float(row[metric]) < float(previous[1][metric]):
            notes.append(
                f"score {previous[0]}->{score}: {metric} fell "
                f"{float(previous[1][metric]):.0f}->{float(row[metric]):.0f}; "
                f"component mix changed ({_component_mix(row)}), so lone signals remain noisy."
            )
        previous = (score, row)
    if not notes:
        notes.append(f"PASS: non-empty score buckets are monotonic by {metric}.")
    return notes


def loaded_gate(observations: Iterable[Observation], target: float = 100.0) -> dict[str, float]:
    observations_list = list(observations)
    base = _hit_rate((obs.move_pt for obs in observations_list), target)
    loaded = _hit_rate((obs.move_pt for obs in observations_list if obs.loaded), target)
    return {
        "target": target,
        "base_hit": base,
        "loaded_hit": loaded,
        "lift": loaded / base if base > 0.0 else 0.0,
        "base_n": float(len(observations_list)),
        "loaded_n": float(sum(1 for obs in observations_list if obs.loaded)),
    }


def _bool_rate(values: Iterable[bool]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(1 for value in items if value) / len(items)


def _component_mix(row: Mapping[str, object]) -> str:
    parts = []
    for key, label in (
        ("compression_rate", "compression"),
        ("oi_build_rate", "oi"),
        ("velocity_rate", "velocity"),
        ("volume_rate", "volume"),
    ):
        value = float(row.get(key, 0.0) or 0.0) * 100.0
        parts.append(f"{label}={value:.0f}%")
    return ", ".join(parts)


def _format_table(rows: list[dict[str, object]]) -> str:
    lines = [f"{'bucket':>18} {'n':>5} {'mean':>6} {'med':>6} {'p75':>6} {'p90':>6} {'>=50':>7} {'>=100':>7} {'>=200':>7}"]
    for row in rows:
        n = int(float(row.get("n", 0.0) or 0.0))
        if n == 0:
            lines.append(f"{str(row['bucket']):>18} {0:>5}")
            continue
        lines.append(
            f"{str(row['bucket']):>18} {n:>5} "
            f"{float(row['mean']):>6.0f} {float(row['median']):>6.0f} "
            f"{float(row['p75']):>6.0f} {float(row['p90']):>6.0f} "
            f"{float(row['hit_50']) * 100:>6.0f}% {float(row['hit_100']) * 100:>6.0f}% "
            f"{float(row['hit_200']) * 100:>6.0f}%"
        )
    return "\n".join(lines)


def _format_day_coverage(rows: list[dict[str, object]]) -> str:
    lines = [f"{'day':>12} {'bars':>6} {'eligible':>9} {'loaded':>7} {'>=100':>7}"]
    for row in rows:
        lines.append(
            f"{str(row['day']):>12} {int(row['bars']):>6} {int(row['eligible']):>9} "
            f"{int(row['loaded']):>7} {float(row['hit_100']) * 100:>6.1f}%"
        )
    return "\n".join(lines)


def _load_days_from_mongo() -> tuple[list[str], dict[str, list[dict[str, float | None]]]]:
    from pymongo import MongoClient

    host = os.getenv("MONGO_HOST", "mongo")
    port = int(os.getenv("MONGO_PORT", "27017"))
    db_name = os.getenv("MONGO_DB", "trading_ai")
    coll_name = os.getenv("BIGMOVE_SOURCE_COLL", "phase1_market_snapshots")
    timeout_ms = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000"))
    collection = MongoClient(f"mongodb://{host}:{port}", serverSelectionTimeoutMS=timeout_ms)[db_name][coll_name]
    explicit_days = [day.strip() for day in os.getenv("BIGMOVE_DAYS", "").split(",") if day.strip()]
    days = explicit_days or sorted(str(day) for day in collection.distinct("trade_date_ist") if day)
    days_bars: dict[str, list[dict[str, float | None]]] = {}
    for day in days:
        docs = collection.find({"trade_date_ist": day}).sort("timestamp", 1)
        days_bars[day] = [_bar_from_doc(doc) for doc in docs]
    return days, days_bars


def main() -> None:
    days, days_bars = _load_days_from_mongo()
    observations = collect_observations(days_bars)
    print(f"BigMoveScore | {HORIZON}-min horizon | days={len(days)} | observations={len(observations)}")
    print()
    print("Day coverage")
    print(_format_day_coverage(day_coverage_rows(days, days_bars, observations)))
    if not observations:
        print("No eligible observations. Check Mongo source, dates, and snapshot shape.")
        return

    gate = loaded_gate(observations, 100.0)
    print(
        "Gate target >=100pt: "
        f"base={gate['base_hit'] * 100:.1f}% (n={int(gate['base_n'])}) | "
        f"loaded={gate['loaded_hit'] * 100:.1f}% (n={int(gate['loaded_n'])}) | "
        f"lift={gate['lift']:.2f}x | required>=1.40x"
    )
    print()
    print("Dose-response by score bucket")
    score_rows = score_bucket_rows(observations)
    print(_format_table(score_rows))
    print()
    print("Monotonicity")
    for note in monotonicity_notes(score_rows, "median"):
        print(f"- {note}")
    print()
    print("Release trigger variants")
    print(_format_table(release_rows(observations)))
    print()
    print("Compression-tightness dose-response")
    tightness_rows = compression_tightness_rows(observations)
    print(_format_table(tightness_rows))
    print()
    print("Compression-tightness monotonicity")
    for note in compression_tightness_notes(tightness_rows):
        print(f"- {note}")


if __name__ == "__main__":
    main()
