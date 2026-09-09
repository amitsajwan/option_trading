"""Config-driven training/backtest pipeline for entry models.

Built 2026-09-09 after a real capital-weighted backtest was found to
overlap a model's own training window (in-sample contamination -- see
project_backtest_insample_contamination_finding_2026-09-09.md in the
operator's memory), and after several instrument-specific one-off scripts
this repo has accumulated (bn_labelgrid_bt.py, nifty_labelgrid_bt.py, ...)
turned out to duplicate the same logic with a different instrument's
constants copy-pasted in.

This package replaces those one-off scripts with one instrument-agnostic
pipeline: `instrument_config` supplies the per-instrument facts (lot size,
label horizon, live model/threshold, window policy), `validation` supplies
the hard gates that must pass before a result is trusted (window-overlap,
degeneracy), and `registry` captures every run's result in one place
instead of scattered log files and memory notes.

Deliberately NOT built on `ml_pipeline_2.staged` -- that module's feature
naming convention (`opt_flow_*`, `osc_*`, `fut_flow_*`) predates the
current Dhan-era snapshot schema (`vel_*`, `ctx_*`, `mtf_derived.*`) and
is not schema-compatible without real rework. `model_search.event_purge`
is reused where relevant since it's schema-agnostic (operates on generic
timestamp columns).
"""
