from __future__ import annotations

from typing import Sequence

import pandas as pd

from .rule_schema import Condition

_OPERATORS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def evaluate_condition(df: pd.DataFrame, cond: Condition) -> pd.Series:
    lhs = pd.to_numeric(df[cond.column], errors="coerce")

    if isinstance(cond.value, str) and cond.value in df.columns:
        rhs = pd.to_numeric(df[cond.value], errors="coerce")
    else:
        rhs = float(cond.value)

    op = _OPERATORS.get(cond.operator)
    if op is None:
        raise ValueError(f"Unknown operator: {cond.operator}")

    result = op(lhs, rhs)
    return result.fillna(False)


def evaluate_all_and(df: pd.DataFrame, conditions: Sequence[Condition]) -> pd.Series:
    if not conditions:
        return pd.Series(True, index=df.index)
    mask = pd.Series(True, index=df.index)
    for cond in conditions:
        mask = mask & evaluate_condition(df, cond)
    return mask


def evaluate_any_or(df: pd.DataFrame, conditions: Sequence[Condition]) -> pd.Series:
    if not conditions:
        return pd.Series(False, index=df.index)
    mask = pd.Series(False, index=df.index)
    for cond in conditions:
        mask = mask | evaluate_condition(df, cond)
    return mask
