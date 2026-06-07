from __future__ import annotations

from ops.research.bigmove_score_backtest import (
    Observation,
    compression_tightness_notes,
    compression_tightness_rows,
    collect_observations,
    day_coverage_rows,
    loaded_gate,
    monotonicity_notes,
    release_rows,
    score_bucket_rows,
)


def _obs(score: int, move: float, *, loaded: bool = False, released_window: bool = False) -> Observation:
    return Observation(
        day="2026-06-01",
        index=42 + score,
        move_pt=move,
        score=score,
        compression_tightness=0.55 if loaded else 0.80,
        compression=score >= 1,
        oi_build=loaded,
        velocity=score >= 3,
        volume=score >= 4,
        loaded=loaded,
        released_strict=score >= 4,
        released_or=score >= 3,
        released_window=released_window,
    )


def test_score_bucket_rows_emit_gate_metrics_and_non_monotonic_note() -> None:
    observations = [
        _obs(0, 90.0),
        _obs(0, 110.0),
        _obs(1, 80.0),
        _obs(1, 85.0),
        _obs(2, 130.0, loaded=True),
        _obs(2, 150.0, loaded=True),
        _obs(3, 180.0, loaded=True, released_window=True),
    ]

    gate = loaded_gate(observations)
    assert gate["base_hit"] == 4 / 7
    assert gate["loaded_hit"] == 1.0
    assert gate["lift"] > 1.4

    rows = score_bucket_rows(observations)
    score_one = next(row for row in rows if row["bucket"] == "1")
    assert score_one["median"] == 80.0
    assert score_one["hit_100"] == 0.0

    notes = monotonicity_notes(rows)
    assert any("score 0->1" in note for note in notes)
    assert any("lone signals remain noisy" in note for note in notes)


def test_release_rows_compare_strict_or_and_window_variants() -> None:
    observations = [
        _obs(2, 120.0, loaded=True, released_window=False),
        _obs(3, 160.0, loaded=True, released_window=True),
        _obs(4, 220.0, loaded=True, released_window=True),
    ]

    rows = {row["bucket"]: row for row in release_rows(observations)}
    assert rows["strict_and"]["n"] == 1.0
    assert rows["current_or"]["n"] == 2.0
    assert rows["3bar_or"]["n"] == 2.0
    assert rows["loaded+3bar_or"]["hit_200"] == 0.5


def test_compression_tightness_rows_detect_large_bucket_inversion() -> None:
    observations = [
        *[_obs(2, 120.0, loaded=True) for _ in range(20)],
        *[_obs(2, 80.0, loaded=True) for _ in range(5)],
        *[
            Observation(
                day="2026-06-01",
                index=100 + i,
                move_pt=80.0 if i < 2 else 140.0,
                score=2,
                compression_tightness=0.65,
                compression=True,
                oi_build=True,
                velocity=False,
                volume=False,
                loaded=True,
                released_strict=False,
                released_or=False,
                released_window=False,
            )
            for i in range(25)
        ],
    ]

    rows = compression_tightness_rows(observations)
    by_bucket = {row["bucket"]: row for row in rows}
    assert by_bucket["tight_0.50_0.60"]["n"] == 25.0
    assert by_bucket["tight_0.60_0.70"]["n"] == 25.0

    notes = compression_tightness_notes(rows, min_n=20)
    assert any("inverted" in note for note in notes)


def test_day_coverage_rows_show_bars_eligible_loaded_and_hits() -> None:
    observations = [
        _obs(2, 120.0, loaded=True),
        _obs(1, 80.0),
        Observation(
            day="2026-06-02",
            index=42,
            move_pt=140.0,
            score=2,
            compression_tightness=0.55,
            compression=True,
            oi_build=True,
            velocity=False,
            volume=False,
            loaded=True,
            released_strict=False,
            released_or=False,
            released_window=False,
        ),
    ]
    days_bars = {
        "2026-06-01": [{"c": 1.0, "h": 1.0, "l": 1.0, "ovol": 1.0, "ooi": 1.0}] * 50,
        "2026-06-02": [{"c": 1.0, "h": 1.0, "l": 1.0, "ovol": 1.0, "ooi": 1.0}] * 55,
    }

    rows = day_coverage_rows(["2026-06-01", "2026-06-02"], days_bars, observations)
    assert rows[0] == {"day": "2026-06-01", "bars": 50, "eligible": 2, "loaded": 1, "hit_100": 0.5}
    assert rows[1] == {"day": "2026-06-02", "bars": 55, "eligible": 1, "loaded": 1, "hit_100": 1.0}


def test_collect_observations_marks_loaded_and_window_release() -> None:
    bars = []
    close = 1000.0
    oi = 1000.0
    for index in range(60):
        oi += 1.0
        width = 20.0 if index < 16 else 3.0
        volume = 100.0
        if index == 42:
            close += 10.0
            volume = 400.0
        elif index == 43:
            close += 1.0
        elif index == 44:
            close += 120.0
        else:
            close += 0.1
        bars.append(
            {
                "c": close,
                "h": close + width,
                "l": close - width,
                "ovol": volume,
                "ooi": oi,
            }
        )

    observations = collect_observations({"2026-06-01": bars}, horizon=10, release_window=3)
    by_index = {obs.index: obs for obs in observations}
    assert by_index[42].loaded is True
    assert by_index[42].compression_tightness < 0.70
    assert by_index[42].released_or is True
    assert by_index[43].released_or is False
    assert by_index[43].released_window is True
    assert by_index[42].move_pt >= 100.0
