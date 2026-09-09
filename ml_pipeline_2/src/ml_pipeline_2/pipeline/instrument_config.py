"""Per-instrument ML pipeline config -- the single place a training or
backtest run looks up lot size, label horizon, and the currently-live
model/threshold, instead of each script re-deriving (or mis-deriving) them.

Deliberately separate from `contracts_app.instruments.InstrumentSpec`
(exchange/contract facts used everywhere from ingestion to execution) --
this carries ML-pipeline-specific facts only, so changing a label recipe
here can never accidentally touch order placement or ingestion.

Every value below was verified against the live system on 2026-09-09,
not assumed -- source noted per field. Re-verify before trusting a value
that's more than a few weeks old; `.env.compose` on the VM is the actual
source of truth for anything with an env override, this file is a cache
of it for pipeline code to import, not a replacement for checking.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class InstrumentMLConfig:
    name: str
    lot_size: int  # verified from live .env.compose (or compose-file default if unset there)
    label_horizon_min: int  # 5 or 15 -- which train_entry_*.py script this instrument's live model uses
    live_model_path: Optional[str]  # /app/models/... -- None if no live model yet (e.g. cold-start)
    live_entry_min_prob: Optional[float]  # currently-deployed threshold, None if not live
    default_label_pct: float = 0.0015  # the "0.15%" convention every instrument's live model uses today
    mongo_hist_collection: Optional[str] = None  # phase1_market_snapshots_hist{,_<name>} -- None = derive from name
    training_view_path: Optional[str] = None  # cached training_view_<name>.csv.gz on the VM, if one exists
    parquet_base: Optional[str] = None  # SIM-harness snapshot parquet root for backtesting -- see parquet_base_notes
    parquet_base_notes: str = ""  # freshness/naming caveats for parquet_base -- read before trusting a backtest
    notes: str = ""


# Keyed by contracts_app.instruments' canonical UPPER name.
INSTRUMENT_ML_CONFIGS: dict[str, InstrumentMLConfig] = {
    "BANKNIFTY": InstrumentMLConfig(
        name="BANKNIFTY",
        lot_size=30,  # verified via .env.compose 2026-09-09 (compose-file bare default is a stale 15 -- don't trust that one)
        label_horizon_min=5,
        live_model_path="/app/models/dhan_entry_rarelabel_5min_v1.joblib",
        live_entry_min_prob=0.25,
        mongo_hist_collection="phase1_market_snapshots_hist",  # bare, no _banknifty suffix (primary instrument)
        training_view_path="/opt/option_trading/.run/training_view/training_view_banknifty.csv.gz",
        parquet_base="/app/.data/ml_pipeline/parquet_data_banknifty",
        parquet_base_notes="STALE as of 2026-09-09: latest trade_date is 2026-07-31 (frozen at this "
                            "instrument's training holdout_end, never refreshed since) -- confirmed via "
                            "instrument field BANKNIFTYFUT. Cannot backtest anything after 2026-07-31 "
                            "until a fresh Mongo->parquet export is run for this instrument.",
        notes="Live real money. Deployed as a trial 2026-09-04 despite a mixed backtest at the time "
              "(user's explicit call). Label-target study 2026-09-08 found 0.10%@thr=0.3 a promising "
              "but UNCONFIRMED candidate -- see project_backtest_insample_contamination_finding_2026-09-09.md.",
    ),
    "NIFTY": InstrumentMLConfig(
        name="NIFTY",
        lot_size=65,  # verified via .env.compose 2026-09-09
        label_horizon_min=15,
        live_model_path="/app/models/nifty_entry_015pct_v2.joblib",
        live_entry_min_prob=0.5,
        training_view_path="/opt/option_trading/.run/training_view/training_view_nifty.csv.gz",
        parquet_base="/app/.data/ml_pipeline/parquet_data",
        parquet_base_notes="Confirmed 2026-09-09: this is the ONLY current/live-updated archive found "
                            "(latest trade_date 2026-09-03, instrument field NIFTYFUT) -- despite the "
                            "unsuffixed name, it is NOT BankNifty's data. .data/ml_pipeline/parquet_data_nifty "
                            "does not exist (only _full/_labelgrid variants, both stale) -- do not use those.",
        notes="Live real money. Label-target study 2026-09-09 found the live model's own Feb-Jul backtest "
              "was itself in-sample-contaminated (0 trades on a clean Aug-Sep re-test) -- the whole study's "
              "results need redoing on a clean window before any of them are trusted.",
    ),
    "FINNIFTY": InstrumentMLConfig(
        name="FINNIFTY",
        lot_size=60,  # compose-file default, no .env.compose override found 2026-09-09
        label_horizon_min=15,
        live_model_path="/app/models/finnifty_entry_015pct_v2.joblib",
        live_entry_min_prob=0.57,
        parquet_base="/app/.data/ml_pipeline/parquet_data_finnifty",
        parquet_base_notes="STALE as of 2026-09-09: latest trade_date is 2026-07-31, same as BankNifty's -- "
                            "cannot backtest anything after that until a fresh export is run.",
        notes="Paper only. Confirmed dormant (ML_ENTRY fired zero times ever -- threshold above the "
              "model's live-observed ceiling). A full retrain (v3) fixed the probability-range bug but "
              "found no real edge at any threshold. Real fix needs new signal, not another retrain -- see "
              "the untapped-feature-signal-gap memory finding (RSI/MACD/opening-range/chain-PCR/ATM-ladder "
              "categories are computed but never extracted into any training view).",
    ),
    "SENSEX": InstrumentMLConfig(
        name="SENSEX",
        lot_size=20,  # verified via .env.compose 2026-08-07/2026-09-05
        label_horizon_min=15,
        live_model_path="/app/models/sensex_entry_015pct_v2.joblib",
        live_entry_min_prob=0.50,
        parquet_base="/app/.data/ml_pipeline/parquet_data_sensex",
        parquet_base_notes="STALE as of 2026-09-09: latest trade_date is 2026-08-06 (this instrument's "
                            "training holdout_end) -- cannot backtest anything after that until a fresh "
                            "export is run. The unsuffixed .data/ml_pipeline/parquet_data is NIFTY's data, "
                            "NOT Sensex's -- a real 2026-09-09 incident used it by mistake and produced a "
                            "wrong, since-retracted result (project_backtest_wrong_instrument_parquet_2026-09-09.md).",
        notes="Paper only. v2 deployed 2026-09-05 after beating v1 in a capital-weighted backtest "
              "(+2.02% vs +1.50%) -- that backtest used the SAME Feb-Jul-vs-train-through-mid-June window "
              "later found to risk in-sample contamination; not yet re-verified on clean data.",
    ),
    "MIDCPNIFTY": InstrumentMLConfig(
        name="MIDCPNIFTY",
        lot_size=120,  # compose-file default, not deployed live yet so no .env.compose override to check
        label_horizon_min=15,
        live_model_path=None,  # cold-start, never deployed
        live_entry_min_prob=None,
        parquet_base="/app/.data/ml_pipeline/parquet_data_midcpnifty",
        parquet_base_notes="Confirmed 2026-09-09: current/live-updated (latest trade_date 2026-09-03, "
                            "instrument field MIDCPNIFTYFUT).",
        notes="Cold-start, 5th instrument. Backfilled + trained (holdout AUC 0.639, ship_gates fail but "
              "not degenerate). Capital-weighted backtest never run. No real or paper money at stake yet.",
    ),
}


def get_instrument_config(name: str) -> InstrumentMLConfig:
    """Look up an instrument's ML pipeline config. Raises KeyError (not a
    silent default) if the instrument isn't registered -- fail loud, same
    reasoning as hist_backfill.py's own _EXPIRY_FN dispatch table."""
    key = name.strip().upper()
    if key not in INSTRUMENT_ML_CONFIGS:
        raise KeyError(
            f"no ML pipeline config registered for instrument={name!r} -- "
            f"known instruments: {sorted(INSTRUMENT_ML_CONFIGS)}. "
            "Add an InstrumentMLConfig entry before running the pipeline for a new instrument."
        )
    return INSTRUMENT_ML_CONFIGS[key]


def mongo_hist_collection_for(name: str) -> str:
    """Resolve the Mongo historical-snapshots collection name, matching the
    same convention used by ops/export_mongo_snapshots_to_parquet.py."""
    cfg = get_instrument_config(name)
    if cfg.mongo_hist_collection:
        return cfg.mongo_hist_collection
    return f"phase1_market_snapshots_hist_{cfg.name.lower()}"
