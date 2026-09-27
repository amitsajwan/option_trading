"""Variable-hold, asymmetric exit simulator (NIFTY, 2026-09-27) -- tests
whether exit STRUCTURE alone (a tight capped loss + a trailing profit stop)
can create positive expectancy even under a coin-flip direction call,
independent of any direction-calling skill.

Extends (does not replace) ``option_payoff_labels.py``'s fixed-15-minute-
hold logic: that module answers "what would a FIXED hold have paid," this
one answers "what would a VARIABLE hold, exited by a stop/trailing rule,
have paid" -- same fixed-contract discipline (never re-based to a later
ATM strike, see ``compute_bar_payoff``'s docstring), same liquidity floors
(``MIN_ENTRY_OI``/``MIN_ENTRY_PREMIUM``, imported not re-derived), same
cost source (``strategy_app.senses.cost_ev.CostEvSense`` /
``strategy_app.cost_model.TradingCostModel``, imported not re-derived).

Motivating context (see the owning task / ``strategy_app/senses/cost_ev.py``
module docstring): this project's OWN empirically-anchored numbers describe
the CURRENT live exit logic as having the wrong-shaped asymmetry --
wrong-side holds lose MORE (-7 to -8%) than right-side holds gain (+3-5%),
the opposite of what a profitable asymmetric-exit strategy needs. This
module builds the instrument to test, empirically, whether a DIFFERENT
stop/trailing shape can flip that asymmetry the other way -- it does not
assume the answer either way.

Two performance-relevant design choices, both deliberate:

1. **Bar CLOSE only, never intraday high/low.** A real, already-documented
   prior finding in this project (``project_stop_check_intrabar_blindspot
   _2026-09-12.md``): checking intrabar LOWS for a stop-out sounds like the
   more "correct" fix for the bar-close blind spot, but tested against real
   trades it BACKFIRES -- it flips 13 real BankNifty winning trades into
   -3% losses, because positions can dip temporarily below a tight stop
   before recovering. This module deliberately checks only each bar's own
   close/LTP (``SnapshotAccessor.option_ltp``), exactly like
   ``compute_bar_payoff`` already does -- consistent with the rest of this
   pipeline, and not a re-introduction of that already-refuted "fix".

2. **CE and PE paths are computed once per entry bar, coin-flip selection
   applied after.** Testing several random-seed coin-flips (the owning
   task's mandatory seed-sensitivity check) would otherwise mean re-walking
   the same bar-by-bar premium path once per seed. Instead, the full
   forward premium path is built ONCE per (entry bar, side) for both CE and
   PE, the exit grid is evaluated once per side, and a given seed's
   CE/PE assignment is applied as a final, near-free ``np.where`` selection
   (``select_outcome_by_side``) -- any number of seeds can then be tried at
   negligible extra cost.

Grid evaluation is vectorized across (entries x grid combinations); only the
bar-by-bar walk itself is a genuine loop (bounded by ``max_bars``), which
keeps a 5-window x tens-of-thousands-of-bars x 60-combination real study
tractable. ``simulate_exit`` is a plain scalar reference implementation used
directly by unit tests and cross-checked against the vectorized
``simulate_exit_grid_batch`` (see ``tests/test_variable_hold_exit.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from strategy_app.cost_model import TradingCostModel
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.senses.cost_ev import CostEvSense

from .option_payoff_labels import MIN_ENTRY_OI, MIN_ENTRY_PREMIUM

EXIT_REASON_HARD_STOP = "hard_stop"
EXIT_REASON_TRAILING_STOP = "trailing_stop"
EXIT_REASON_TIME_CAP = "time_cap"

REASON_CODE: Dict[str, int] = {
    EXIT_REASON_HARD_STOP: 0,
    EXIT_REASON_TRAILING_STOP: 1,
    EXIT_REASON_TIME_CAP: 2,
}
CODE_TO_REASON: Dict[int, str] = {v: k for k, v in REASON_CODE.items()}

# Session-window convention matches option_payoff_labels.label_day's own
# default (09:45-15:05 IST) -- 15:05 is this project's established
# session-end convention for NIFTY.
DEFAULT_SESSION_END_MIN = 15 * 60 + 5

# 1-minute snapshot cadence (see option_payoff_labels.label_day's docstring).
# 90 bars = 1.5 hours -- generous relative to the existing FIXED-hold models
# in this project (5-15 min horizons), long enough to let a real trailing
# stop actually do its job (activation thresholds up to +30% need room to
# develop), but still bounded so a single entry can't dominate runtime or
# get held indefinitely on a day with a very late entry. The session-end
# cutoff (see DEFAULT_SESSION_END_MIN) binds first for any entry after
# ~13:35 IST regardless of this cap.
DEFAULT_MAX_BARS = 90

# Sentinel for "effectively never triggers" -- used to isolate ONE rule
# (e.g. hard-stop-only, no trailing) by disabling the other(s). No real
# option premium move within one session gets anywhere near +/-1000%.
NEVER_TRIGGER = 10.0

DEFAULT_STOP_GRID: Tuple[float, ...] = (0.05, 0.07, 0.10, 0.15, 0.20)
DEFAULT_TRAIL_ACTIVATE_GRID: Tuple[float, ...] = (0.10, 0.15, 0.20, 0.30)
DEFAULT_TRAIL_DISTANCE_GRID: Tuple[float, ...] = (0.20, 0.30, 0.40)


@dataclass(frozen=True)
class ExitGridParams:
    """One point in the stop/trailing grid.

    stop_loss_pct: exit if (premium - entry) / entry <= -stop_loss_pct.
    trail_activate_pct: once (premium - entry) / entry >= this, trailing
        becomes active (irreversible for the rest of the hold).
    trail_distance_pct: once active, exit if premium <= running peak
        premium * (1 - trail_distance_pct).
    """

    stop_loss_pct: float
    trail_activate_pct: float
    trail_distance_pct: float

    def __post_init__(self) -> None:
        if not (0.0 < self.stop_loss_pct <= 100.0):
            raise ValueError(f"stop_loss_pct must be in (0, 100], got {self.stop_loss_pct}")
        if not (0.0 < self.trail_activate_pct <= 100.0):
            raise ValueError(f"trail_activate_pct must be in (0, 100], got {self.trail_activate_pct}")
        if not (0.0 < self.trail_distance_pct < 1.0):
            raise ValueError(f"trail_distance_pct must be in (0, 1), got {self.trail_distance_pct}")

    @property
    def label(self) -> str:
        return f"stop{self.stop_loss_pct:.2f}_act{self.trail_activate_pct:.2f}_dist{self.trail_distance_pct:.2f}"


def build_exit_grid(
    stop_grid: Sequence[float] = DEFAULT_STOP_GRID,
    activate_grid: Sequence[float] = DEFAULT_TRAIL_ACTIVATE_GRID,
    distance_grid: Sequence[float] = DEFAULT_TRAIL_DISTANCE_GRID,
) -> List[ExitGridParams]:
    """Cartesian product of the three grids, e.g. the default
    5 x 4 x 3 = 60 combinations."""
    return [
        ExitGridParams(stop_loss_pct=s, trail_activate_pct=a, trail_distance_pct=d)
        for s, a, d in product(stop_grid, activate_grid, distance_grid)
    ]


@dataclass(frozen=True)
class ExitOutcome:
    exit_bar_offset: int  # 1-indexed bars held after entry
    exit_reason: str
    exit_premium: float
    peak_premium: float
    gross_return_pct: float
    net_return_pct: float  # gross - cost_pct


def simulate_exit(
    entry_premium: float,
    premium_path: Sequence[float],
    params: ExitGridParams,
    *,
    cost_pct: float,
) -> ExitOutcome:
    """Pure, scalar reference implementation for ONE (entry, grid combo)
    pair. `premium_path` is bars 1..len(path) AFTER entry, already
    forward-filled (no None/NaN) -- see `build_forward_premium_path`.

    Exit priority within a bar: the hard stop is checked BEFORE the
    trailing stop. These two conditions are evaluated against different
    reference points (entry price vs. running peak) so they essentially
    never compete on the same bar in real paths (trailing only ever
    activates after price has already risen above entry, so a later crash
    back through the hard-stop floor is exactly the case this ordering is
    for: an absolute floor should always win over a looser secondary rule).

    Raises ValueError if `premium_path` is empty -- callers must never call
    this for an entry bar with zero forward bars available (there is
    nothing to simulate).
    """
    path = np.asarray(premium_path, dtype=float)
    if path.size == 0:
        raise ValueError("simulate_exit requires at least one forward bar in premium_path")
    if entry_premium <= 0:
        raise ValueError(f"entry_premium must be positive, got {entry_premium}")

    peak = float(entry_premium)
    trailing_active = False
    for i, price in enumerate(path):
        price = float(price)
        offset = i + 1
        peak = max(peak, price)
        drawdown = (price - entry_premium) / entry_premium
        if drawdown <= -params.stop_loss_pct:
            return _make_outcome(offset, EXIT_REASON_HARD_STOP, price, entry_premium, peak, cost_pct)
        if not trailing_active and drawdown >= params.trail_activate_pct:
            trailing_active = True
        if trailing_active:
            trail_level = peak * (1.0 - params.trail_distance_pct)
            if price <= trail_level:
                return _make_outcome(offset, EXIT_REASON_TRAILING_STOP, price, entry_premium, peak, cost_pct)

    final_price = float(path[-1])
    return _make_outcome(len(path), EXIT_REASON_TIME_CAP, final_price, entry_premium, peak, cost_pct)


def _make_outcome(offset: int, reason: str, price: float, entry_premium: float, peak: float, cost_pct: float) -> ExitOutcome:
    gross = (price - entry_premium) / entry_premium
    net = gross - cost_pct
    return ExitOutcome(
        exit_bar_offset=offset, exit_reason=reason, exit_premium=price,
        peak_premium=peak, gross_return_pct=round(gross, 6), net_return_pct=round(net, 6),
    )


@dataclass(frozen=True)
class BatchExitResult:
    """Arrays of shape (n_entries, n_combos) -- one row per entry, one
    column per `ExitGridParams` in the grid passed to
    `simulate_exit_grid_batch`."""

    exit_bar_offset: np.ndarray
    exit_reason_code: np.ndarray
    exit_premium: np.ndarray
    gross_return_pct: np.ndarray
    net_return_pct: np.ndarray

    def reason_labels(self) -> np.ndarray:
        vec = np.vectorize(lambda c: CODE_TO_REASON.get(int(c), "unknown"))
        return vec(self.exit_reason_code)


def simulate_exit_grid_batch(
    entry_premiums: np.ndarray,
    premium_paths: np.ndarray,
    path_lens: np.ndarray,
    cost_pcts: np.ndarray,
    grid: Sequence[ExitGridParams],
) -> BatchExitResult:
    """Vectorized across (entries x grid combos); the only real loop is over
    bar step (bounded by `premium_paths.shape[1]`). See module docstring for
    why this shape of vectorization (not per-entry-per-combo) is what makes
    the real study tractable.

    `entry_premiums`: shape (n,). `premium_paths`: shape (n, max_bars),
    forward-filled (see `pad_premium_paths`) -- may contain NaN rows for
    entries with no valid leg (illiquid/missing quote), which propagate to
    NaN outputs for that entry (never a crash) and must be dropped by the
    caller before computing summary statistics. `path_lens`: shape (n,),
    the number of REAL (pre-padding) bars available for that entry -- used
    so a `time_cap` exit is reported at the entry's own real hold length,
    not `max_bars` (which may include forward-fill padding past session
    end or a shorter available day). `cost_pcts`: shape (n,).
    """
    entry_premiums = np.asarray(entry_premiums, dtype=float)
    premium_paths = np.asarray(premium_paths, dtype=float)
    path_lens = np.asarray(path_lens, dtype=int)
    cost_pcts = np.asarray(cost_pcts, dtype=float)
    n = entry_premiums.shape[0]
    if premium_paths.shape[0] != n or path_lens.shape[0] != n or cost_pcts.shape[0] != n:
        raise ValueError("simulate_exit_grid_batch: all inputs must share the same first dimension (n entries)")
    max_bars = premium_paths.shape[1]
    if max_bars == 0:
        raise ValueError("simulate_exit_grid_batch: premium_paths has zero columns -- nothing to simulate")
    m = len(grid)
    if m == 0:
        raise ValueError("simulate_exit_grid_batch: grid must be non-empty")

    stop = np.array([g.stop_loss_pct for g in grid], dtype=float)[None, :]
    activate = np.array([g.trail_activate_pct for g in grid], dtype=float)[None, :]
    distance = np.array([g.trail_distance_pct for g in grid], dtype=float)[None, :]

    entry = entry_premiums[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        peak = np.broadcast_to(entry, (n, m)).copy()
        trailing_active = np.zeros((n, m), dtype=bool)
        exited = np.zeros((n, m), dtype=bool)
        exit_bar = np.zeros((n, m), dtype=int)
        exit_reason = np.full((n, m), REASON_CODE[EXIT_REASON_TIME_CAP], dtype=int)
        exit_premium = np.full((n, m), np.nan, dtype=float)

        for b in range(max_bars):
            price_b = premium_paths[:, b][:, None]
            peak = np.maximum(peak, price_b)
            drawdown = (price_b - entry) / entry

            hard_hit = (drawdown <= -stop) & (~exited)
            exit_reason = np.where(hard_hit, REASON_CODE[EXIT_REASON_HARD_STOP], exit_reason)
            exit_premium = np.where(hard_hit, price_b, exit_premium)
            exit_bar = np.where(hard_hit, b + 1, exit_bar)
            exited = exited | hard_hit

            newly_active = (~trailing_active) & (drawdown >= activate)
            trailing_active = trailing_active | newly_active
            trail_level = peak * (1.0 - distance)
            trail_hit = trailing_active & (price_b <= trail_level) & (~exited)
            exit_reason = np.where(trail_hit, REASON_CODE[EXIT_REASON_TRAILING_STOP], exit_reason)
            exit_premium = np.where(trail_hit, price_b, exit_premium)
            exit_bar = np.where(trail_hit, b + 1, exit_bar)
            exited = exited | trail_hit

        never_exited = ~exited
        final_idx = np.clip(path_lens - 1, 0, max(max_bars - 1, 0))
        final_premium = premium_paths[np.arange(n), final_idx]
        exit_bar = np.where(never_exited, np.broadcast_to(path_lens[:, None], (n, m)), exit_bar)
        exit_premium = np.where(never_exited, np.broadcast_to(final_premium[:, None], (n, m)), exit_premium)

        gross = (exit_premium - entry) / entry
        net = gross - cost_pcts[:, None]

    return BatchExitResult(
        exit_bar_offset=exit_bar, exit_reason_code=exit_reason, exit_premium=exit_premium,
        gross_return_pct=gross, net_return_pct=net,
    )


@dataclass(frozen=True)
class ForwardPath:
    premiums: np.ndarray  # bars 1..n_real after entry, forward-filled
    n_real: int


def build_forward_premium_path(
    snaps: Sequence[SnapshotAccessor],
    entry_idx: int,
    side: str,
    strike: int,
    entry_premium: float,
    *,
    max_bars: int = DEFAULT_MAX_BARS,
    session_end_min: int = DEFAULT_SESSION_END_MIN,
) -> ForwardPath:
    """Forward-filled premium path for `side` at the FIXED `strike` (never
    re-based -- same fixed-contract discipline as `compute_bar_payoff`),
    starting at the bar after `entry_idx`, through
    min(entry_idx + max_bars, last bar within `session_end_min`, end of
    `snaps`). A missing quote (`option_ltp` returns None) is forward-filled
    from the last known price (or `entry_premium` if no price has been seen
    yet) -- a deliberate data-quality convention distinct from
    `option_payoff_labels`' "missing -> skip the label" rule for the ENTRY
    leg itself (still enforced upstream via `entry_leg_info`'s
    `MIN_ENTRY_OI`/`MIN_ENTRY_PREMIUM` gate before this is ever called).

    Returns a `ForwardPath` with `n_real=0` (empty `premiums`) if there is
    no forward bar at all (e.g. entry is the last bar of the session) --
    callers must exclude such entries, there is nothing to simulate.
    """
    n = len(snaps)
    prev = float(entry_premium)
    out: List[float] = []
    upper = min(n, entry_idx + max_bars + 1)
    for j in range(entry_idx + 1, upper):
        ts = snaps[j].timestamp
        if ts is not None:
            minute_of_day = ts.hour * 60 + ts.minute
            if minute_of_day > session_end_min:
                break
        raw = snaps[j].option_ltp(side, strike)
        if raw is None:
            out.append(prev)
        else:
            prev = float(raw)
            out.append(prev)
    return ForwardPath(premiums=np.asarray(out, dtype=float), n_real=len(out))


def pad_premium_paths(paths: Sequence[ForwardPath], max_bars: int) -> Tuple[np.ndarray, np.ndarray]:
    """Stack variable-length `ForwardPath.premiums` into a fixed-width
    (n, max_bars) array, forward-filling the padding region with each
    path's own last real value (a no-op continuation for
    `simulate_exit_grid_batch` -- see that function's docstring). Rows with
    `n_real == 0` are left as all-NaN (propagates to NaN outcomes, never a
    crash -- callers must exclude these from summary statistics)."""
    n = len(paths)
    out = np.full((n, max(max_bars, 1)), np.nan, dtype=float)
    lens = np.zeros(n, dtype=int)
    for i, p in enumerate(paths):
        L = min(p.n_real, max_bars)
        lens[i] = L
        if L == 0:
            continue
        out[i, :L] = p.premiums[:L]
        if L < max_bars:
            out[i, L:] = p.premiums[L - 1]
    return out, lens


@dataclass(frozen=True)
class EntryLegInfo:
    strike: int
    entry_premium: float
    cost_pct: float


def entry_leg_info(
    entry: SnapshotAccessor,
    side: str,
    *,
    lot_size: int,
    cost_model: Optional[TradingCostModel] = None,
) -> Optional[EntryLegInfo]:
    """Entry premium + round-trip cost_pct for `side` at the entry bar's own
    ATM strike, gated by the SAME liquidity floors `option_payoff_labels
    ._leg_payoff` uses (imported, not re-derived). Returns None if the
    entry has no resolvable ATM strike, or the leg is too thin/cheap to
    have been fillable -- mirrors that module's "skip, don't impute"
    contract for the entry leg."""
    strike = entry.atm_strike
    if strike is None:
        return None
    side_u = str(side).strip().upper()
    if side_u == "CE":
        premium = entry.atm_ce_close
        oi = entry.atm_ce_oi
    elif side_u == "PE":
        premium = entry.atm_pe_close
        oi = entry.atm_pe_oi
    else:
        raise ValueError(f"side must be 'CE' or 'PE', got {side!r}")
    if premium is None or premium < MIN_ENTRY_PREMIUM or premium <= 0:
        return None
    if oi is not None and oi < MIN_ENTRY_OI:
        return None
    cm = cost_model or TradingCostModel()
    cost_pct = CostEvSense(premium_pts=float(premium), lot_qty=lot_size, cost_model=cm).cost_pct()
    return EntryLegInfo(strike=int(strike), entry_premium=float(premium), cost_pct=float(cost_pct))


def assign_random_sides(n: int, seed: int) -> np.ndarray:
    """Fair (50/50) coin-flip CE/PE assignment, `True` = CE. A fixed `seed`
    makes this fully reproducible -- callers use a DIFFERENT seed per
    walk-forward window (documented at the call site) plus, for the
    mandatory seed-sensitivity check, 2-3 different seeds within at least
    one window."""
    rng = np.random.default_rng(seed)
    return rng.random(n) < 0.5


def select_outcome_by_side(is_ce: np.ndarray, outcome_ce: BatchExitResult, outcome_pe: BatchExitResult) -> BatchExitResult:
    """Apply a per-entry CE/PE assignment (e.g. from `assign_random_sides` or
    a real direction signal) to pick between two already-computed grid
    results -- the near-free step that lets many seeds/signals reuse the
    same expensive bar-by-bar walk (see module docstring, design choice 2)."""
    is_ce2d = np.asarray(is_ce, dtype=bool)[:, None]
    return BatchExitResult(
        exit_bar_offset=np.where(is_ce2d, outcome_ce.exit_bar_offset, outcome_pe.exit_bar_offset),
        exit_reason_code=np.where(is_ce2d, outcome_ce.exit_reason_code, outcome_pe.exit_reason_code),
        exit_premium=np.where(is_ce2d, outcome_ce.exit_premium, outcome_pe.exit_premium),
        gross_return_pct=np.where(is_ce2d, outcome_ce.gross_return_pct, outcome_pe.gross_return_pct),
        net_return_pct=np.where(is_ce2d, outcome_ce.net_return_pct, outcome_pe.net_return_pct),
    )


def select_inputs_by_side(
    is_ce: np.ndarray,
    entry_premium_ce: np.ndarray, premium_paths_ce: np.ndarray, path_lens_ce: np.ndarray, cost_pct_ce: np.ndarray,
    entry_premium_pe: np.ndarray, premium_paths_pe: np.ndarray, path_lens_pe: np.ndarray, cost_pct_pe: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Same per-entry CE/PE selection as `select_outcome_by_side`, but over
    the RAW simulation inputs (entry premium / padded path / real length /
    cost) rather than an already-simulated `BatchExitResult` -- needed for
    `hard_stop_whipsaw_report`, which re-simulates a pure hard-stop-only
    policy on whichever side was actually "traded" under a given coin-flip
    or direction-signal assignment."""
    is_ce1d = np.asarray(is_ce, dtype=bool)
    entry = np.where(is_ce1d, entry_premium_ce, entry_premium_pe)
    lens = np.where(is_ce1d, path_lens_ce, path_lens_pe).astype(int)
    cost = np.where(is_ce1d, cost_pct_ce, cost_pct_pe)
    path = np.where(is_ce1d[:, None], premium_paths_ce, premium_paths_pe)
    return entry, path, lens, cost


def summarize_returns(net_returns: np.ndarray) -> Dict[str, Any]:
    """Win rate / mean win / mean loss / expectancy for one grid combo's
    net-return column. NaN entries (invalid/illiquid legs -- see
    `pad_premium_paths`) are dropped, never imputed. `mean_loss` includes
    exactly-zero outcomes (rare, but a net_return of precisely 0.0 is not a
    "win" under a strict >0 definition)."""
    arr = np.asarray(net_returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    n = int(arr.size)
    if n == 0:
        return {"n": 0, "win_rate": None, "mean_win": None, "mean_loss": None, "expectancy": None, "median": None, "std": None}
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    return {
        "n": n,
        "win_rate": round(float(wins.size / n), 4),
        "mean_win": round(float(wins.mean()), 5) if wins.size else None,
        "mean_loss": round(float(losses.mean()), 5) if losses.size else None,
        "expectancy": round(float(arr.mean()), 5),
        "median": round(float(np.median(arr)), 5),
        "std": round(float(arr.std()), 5),
    }


def hard_stop_whipsaw_report(
    entry_premiums: np.ndarray,
    premium_paths: np.ndarray,
    path_lens: np.ndarray,
    cost_pcts: np.ndarray,
    stop_grid: Sequence[float],
    *,
    never_trigger: float = NEVER_TRIGGER,
) -> List[Dict[str, Any]]:
    """For each `stop_loss_pct` in `stop_grid`, in isolation (trailing
    disabled via `never_trigger`, i.e. a PURE hard-stop-only policy that
    otherwise holds to the time cap): what fraction of the entries that got
    stopped out would have ultimately been net-profitable by the time cap
    (end of session / max_bars) if the stop had NOT fired -- the whipsaw
    diagnostic this project's own prior finding
    (`project_stop_check_intrabar_blindspot_2026-09-12.md`) makes
    mandatory before trusting any stop level. A high whipsaw_rate means
    that stop level is shaking out trades that would have worked, exactly
    the failure mode already documented for this project's intrabar-low
    check (a different mechanism, same symptom).

    Isolating the hard stop from trailing (rather than reading whipsaw off
    the full grid, where a trailing stop might exit first) keeps this a
    clean read of the hard-stop LEVEL alone -- the trailing stop's own
    give-back tradeoff is a separate, already-visible dimension of the main
    grid results (a tighter trail_distance trades less upside for an
    earlier lock-in, not a "shaken out while still a loser" whipsaw)."""
    baseline_grid = [ExitGridParams(stop_loss_pct=never_trigger, trail_activate_pct=never_trigger, trail_distance_pct=0.99)]
    baseline = simulate_exit_grid_batch(entry_premiums, premium_paths, path_lens, cost_pcts, baseline_grid)
    baseline_net = baseline.net_return_pct[:, 0]

    rows: List[Dict[str, Any]] = []
    for x in stop_grid:
        grid = [ExitGridParams(stop_loss_pct=x, trail_activate_pct=never_trigger, trail_distance_pct=0.99)]
        res = simulate_exit_grid_batch(entry_premiums, premium_paths, path_lens, cost_pcts, grid)
        stopped = res.exit_reason_code[:, 0] == REASON_CODE[EXIT_REASON_HARD_STOP]
        n_stopped = int(np.nansum(stopped))
        if n_stopped == 0:
            rows.append({"stop_loss_pct": x, "n_stopped": 0, "whipsaw_rate": None, "mean_recovery_net_return": None})
            continue
        stopped_baseline = baseline_net[stopped]
        valid = ~np.isnan(stopped_baseline)
        if not valid.any():
            rows.append({"stop_loss_pct": x, "n_stopped": n_stopped, "whipsaw_rate": None, "mean_recovery_net_return": None})
            continue
        recovered = stopped_baseline[valid] > 0
        rows.append({
            "stop_loss_pct": x,
            "n_stopped": n_stopped,
            "whipsaw_rate": round(float(recovered.mean()), 4),
            "mean_recovery_net_return": round(float(stopped_baseline[valid].mean()), 5),
        })
    return rows


__all__ = [
    "EXIT_REASON_HARD_STOP", "EXIT_REASON_TRAILING_STOP", "EXIT_REASON_TIME_CAP",
    "REASON_CODE", "CODE_TO_REASON",
    "DEFAULT_SESSION_END_MIN", "DEFAULT_MAX_BARS", "NEVER_TRIGGER",
    "DEFAULT_STOP_GRID", "DEFAULT_TRAIL_ACTIVATE_GRID", "DEFAULT_TRAIL_DISTANCE_GRID",
    "ExitGridParams", "build_exit_grid", "ExitOutcome", "simulate_exit",
    "BatchExitResult", "simulate_exit_grid_batch",
    "ForwardPath", "build_forward_premium_path", "pad_premium_paths",
    "EntryLegInfo", "entry_leg_info",
    "assign_random_sides", "select_outcome_by_side", "select_inputs_by_side", "summarize_returns",
    "hard_stop_whipsaw_report",
]
