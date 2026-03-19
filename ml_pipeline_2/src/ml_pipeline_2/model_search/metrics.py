from __future__ import annotations

from typing import Sequence

import numpy as np


def max_drawdown_pct(returns: Sequence[float]) -> float:
    """Return max drawdown from a geometrically compounded return path."""
    r = np.asarray(list(returns), dtype=float)
    if len(r) == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = (equity / np.where(peak == 0.0, 1.0, peak)) - 1.0
    return float(abs(np.nanmin(dd)))


def profit_factor(returns: Sequence[float]) -> float:
    """Return gross-profit / gross-loss using additive trade returns."""
    r = np.asarray(list(returns), dtype=float)
    if len(r) == 0:
        return 0.0
    gp = float(np.sum(r[r > 0.0]))
    gl = float(abs(np.sum(r[r < 0.0])))
    if gl == 0.0:
        return 999.0 if gp > 0 else 0.0
    return float(gp / gl)

