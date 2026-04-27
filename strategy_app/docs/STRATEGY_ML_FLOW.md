# Strategy + ML Flow

As-of: `2026-04-27`

How a single market snapshot moves through `strategy_app` and becomes a trade decision.

---

## 1. Full pipeline — one snapshot, one decision

```mermaid
flowchart TD
    A([Redis snapshot]) --> B[Session + risk warm-up]
    B --> C{Hard exit fires?}
    C -->|Yes| Z1([Position closed - no strategy needed])
    C -->|No| D[Regime classifier]
    D --> E{Regime}
    E -->|AVOID| Z2([No entry - halt])
    E -->|HIGH_VOL / TRENDING / SIDEWAYS\nPRE_EXPIRY / EXPIRY| F[Strategy router]
    F --> G[Strategy votes\nwith confidence score]
    G --> H{Confidence >= 0.65?}
    H -->|No| Z3([HOLD])
    H -->|Yes| I{Engine mode}
    I -->|ml_pure| J[3-stage ML gate]
    I -->|deterministic| K[Direct to lot sizing]
    J --> M[Lot sizing]
    K --> M
    M --> N([TradeSignal emitted\nJSONL + Redis publish])
```

---

## 2. Per-snapshot decision sequence

```mermaid
sequenceDiagram
    participant Redis
    participant Consumer
    participant RiskMgr
    participant Tracker
    participant Regime
    participant Router
    participant Strategy
    participant ML
    participant Logger

    Redis->>Consumer: snapshot payload
    Consumer->>Consumer: dedupe (run_id, snapshot_id)
    Consumer->>Consumer: detect session boundary
    Consumer->>RiskMgr: update(snapshot)
    RiskMgr->>RiskMgr: refresh P&L, VIX halt, loss streak
    Consumer->>Tracker: update(snapshot)
    Tracker->>Tracker: MTM, MFE/MAE, stop/target checks
    Tracker-->>Consumer: hard exit? -> close immediately
    Consumer->>Regime: classify(snapshot)
    Regime-->>Router: regime + confidence
    Router->>Strategy: evaluate(snapshot) for each candidate
    Strategy-->>Router: votes with confidence
    Router->>ML: apply gate when engine=ml_pure
    ML-->>RiskMgr: passing vote
    RiskMgr->>RiskMgr: compute_lots()
    RiskMgr-->>Logger: TradeSignal
    Logger->>Logger: write JSONL + publish Redis
```

---

## 3. Regime → strategy routing

```mermaid
flowchart LR
    subgraph Regimes
        T[TRENDING]
        S[SIDEWAYS]
        PE[PRE_EXPIRY]
        EX[EXPIRY]
        HV[HIGH_VOL]
        AV[AVOID]
    end

    subgraph Strategies
        ORB[ORB]
        OI[OI_BUILDUP]
        EMA[EMA_CROSSOVER]
        VW[VWAP_RECLAIM]
        PDL[PREV_DAY_LEVEL]
        IV[IV_FILTER]
        HVO[HIGH_VOL_ORB]
        EMP[EXPIRY_MAX_PAIN\nnot in default routing]
    end

    T --> ORB
    T --> OI
    T --> EMA
    T --> PDL
    S --> VW
    S --> OI
    PE --> ORB
    PE --> OI
    EX --> IV
    EX --> VW
    EX -.->|disabled by default| EMP
    HV --> HVO
    AV --> |no entry| X2(( ))
```

---

## 4. Exit routing

```mermaid
flowchart TD
    subgraph Current["Current runtime"]
        P2[Open position\nowned by OI_BUILDUP]
        OWN[Owned exits\nOI_BUILDUP only]
        SHARED[Shared exits\nORB + VWAP_RECLAIM]
        HARD[Hard exits\nstop / trail / time / risk]
        P2 --> HARD
        HARD -->|fires first, always| C2([Close])
        P2 --> OWN
        OWN -->|evaluated first| C3([Close - attributed correctly])
        OWN -->|no valid vote| SHARED
        SHARED --> C4([Close - fallback only])
    end
```

EMA_CROSSOVER is not in the default universal exit candidate set.

---

## 5. Engine lanes

```mermaid
flowchart LR
    subgraph DET["deterministic - research/replay"]
        D1[Regime] --> D2[Strategy votes] --> D3[Lot sizing] --> D4[TradeSignal]
    end

    subgraph PURE["ml_pure - live production"]
        P1[Regime + prefilter gates] --> P2[Stage 1\nEntry gate]
        P2 -->|below threshold| P6([HOLD])
        P2 -->|pass| P3[Stage 2\nCE or PE?]
        P3 -->|below threshold or low edge| P6
        P3 -->|pass| P4[Stage 3\nRecipe selection]
        P4 -->|below threshold or low margin| P6
        P4 -->|pass — sets stop%\ntarget% max_hold_bars| P5[Lot sizing] --> P7[TradeSignal]
    end
```

The legacy `ml` wrapper has been removed. Only `deterministic` and `ml_pure` are supported.

---

## 6. ml_pure staged inference detail

`predict_staged()` in `pure_ml_staged_runtime.py` runs this sequence on each snapshot:

1. **Prefilter chain** — gates from `bundle.runtime.prefilter_gate_ids`:
   - `risk_halt_pause_v1`: halt if risk manager is halted or paused
   - `valid_entry_phase_v1`: block if outside valid session phase
   - `startup_warmup_v1`: block during warmup window
   - `feature_freshness_v1`: block if snapshot age exceeds `max_feature_age_sec`
   - `regime_gate_v1` / `regime_confidence_gate_v1`: block on `AVOID`, `SIDEWAYS`, low-confidence regime, or `EXPIRY` when `block_expiry=true`
   - `feature_completeness_v1`: block if NaN count in required features exceeds `max_nan_features`
   - `liquidity_gate_v1`: block if OI or volume below minimums

2. **Stage 1** — score `entry_prob` against `stage1.selected_threshold`. HOLD if below.

3. **Stage 2** — score `direction_up_prob`. Apply per-direction thresholds and `min_edge`. HOLD on conflict or both below threshold.

4. **Stage 3** — score all recipe models; select top recipe. HOLD if `top_prob < selected_threshold` or `margin < selected_margin_min`. Recipe metadata sets `stop_loss_pct`, `target_pct`, `horizon_minutes`.

`STRATEGY_ML_PURE_BYPASS_GATES=true` skips all prefilter and threshold gates (research use only).

---

## 7. ML training pipeline (offline)

`ml_pipeline_2` runs offline and produces the `.joblib` bundle that `strategy_app` loads at startup.

```mermaid
flowchart TD
    A([Parquet snapshots\nstage1 / stage2 / stage3 views]) --> B[Label construction\noracle labels from forward futures paths]
    B --> C[Walk-forward folds\nwith purge + embargo]
    C --> D[Train Stage 1\nentry gate]
    C --> E[Train Stage 2\ndirection CE/PE]
    C --> F[Train Stage 3\nrecipe selection]
    D & E & F --> G[Policy selection\non research_valid window]
    G --> H[Score final_holdout\nonce only]
    H --> I{Hard gates pass?}
    I -->|No| Z([Do not publish])
    I -->|Yes| J[Publish bundle\nmodel.joblib + threshold_report.json]
    J --> K1[Local path\nML_PURE_RUN_ID + ML_PURE_MODEL_GROUP]
    J --> K2[GCS path\ngs://bucket/published_models/group/\nML_PURE_MODEL_PACKAGE or ML_PURE_THRESHOLD_REPORT]
    K1 & K2 --> L([strategy_app loads\nauto-downloads GCS to local cache])
```

Current published model: `gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/`

---

## 8. GCS artifact loading

`strategy_app/utils/gcs_artifact.py` provides transparent `gs://` resolution:

- `resolve_artifact_path(path)` — pass-through for local paths; downloads and caches for `gs://` paths
- `download_gcs_file(gcs_url)` — downloads to `GCS_ARTIFACT_CACHE_DIR` (default `~/.cache/option_trading_models/`)
- Cache key is a SHA-256 slug of the full URL; existing cache entries are reused without re-download

`load_staged_model_package()` and `load_staged_policy()` both call `resolve_artifact_path` internally, so `gs://` paths work transparently with no caller changes.

---

## 9. ML ↔ strategy integration gaps

```mermaid
flowchart TD
    subgraph Train["ml_pipeline_2 - offline"]
        T1[Parquet views\npre-computed features]
        T2[Label construction\nfrom forward futures paths]
        T3[Trained bundle]
        T1 --> T2 --> T3
    end

    subgraph Live["strategy_app - live"]
        L1[Redis snapshots]
        L2[rolling_feature_state\nfeatures rebuilt online]
        L3[ml_pure inference]
        L1 --> L2 --> L3
    end

    T3 -->|published bundle\nrun_id + model_group| L3

    G1["Gap 1\nFeature drift — two separate codepaths\ncompute the same features.\nNo runtime parity check in production."]
    G2["Gap 2\nLabels are synthetic forward-path labels,\nnot realized option lifecycle outcomes."]
    G3["Gap 3\nNo feedback loop.\nLive outcomes do not trigger retraining."]

    T1 -.- G1
    L2 -.- G1
    T2 -.- G2
    L3 -.- G3
```

---

## 10. Correct order of operations

```mermaid
flowchart TD
    S1["Phase 1\nValidate runtime code + replay behavior"]
    S2["Phase 2\nKeep feature parity and session/risk tests green"]
    S3["Phase 3\nRebuild stage views only when data,\nview schema, or label recipe changes"]
    S4["Phase 4\nRetrain ml_pipeline_2\nPublish new bundle"]
    S5["Phase 5\nSwitch runtime by run_id + model_group\nor explicit GCS path"]
    S6["Phase 6\nDeploy ml_pure"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6
```

---

## Reference

| File | Purpose |
|---|---|
| `strategy_app/engines/deterministic_rule_engine.py` | Core deterministic decision loop |
| `strategy_app/engines/pure_ml_engine.py` | ml_pure engine |
| `strategy_app/engines/pure_ml_staged_runtime.py` | Staged inference: `predict_staged()`, loaders |
| `strategy_app/engines/strategy_router.py` | Regime → strategy mapping |
| `strategy_app/risk/manager.py` | Lot sizing, halts, drawdown |
| `strategy_app/position/tracker.py` | Position state, hard exits |
| `strategy_app/runtime/redis_snapshot_consumer.py` | Snapshot intake, session lifecycle |
| `strategy_app/utils/gcs_artifact.py` | GCS download/cache — resolves `gs://` paths |
| `ml_pipeline_2/staged/pipeline.py` | ML training orchestration |
| `ml_pipeline_2/publishing/release.py` | GCS sync |
