from __future__ import annotations

import pandas as pd

from .condition_evaluator import evaluate_all_and, evaluate_any_or
from .rule_schema import Rule


def generate_signals(df: pd.DataFrame, rule: Rule) -> pd.Series:
    disqualified = evaluate_any_or(df, rule.disqualifiers)
    for group in rule.disqualifier_all_of:
        disqualified = disqualified | evaluate_all_and(df, group)
    eligible = evaluate_all_and(df, rule.entry_conditions)
    return eligible & ~disqualified
