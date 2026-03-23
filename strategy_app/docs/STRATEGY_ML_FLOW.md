# Strategy + ML Flow

How a single market snapshot moves through `strategy_app` and becomes a trade decision.

---

## 1. Full pipeline - one snapshot, one decision

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

## 3. Regime -> strategy routing

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
        EMP[EXPIRY_MAX_PAIN\nremoved from default]
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
    EX -.->|default routing removed| EMP
    HV --> HVO
    AV --> |no entry| X2(( ))
```

---

## 4. Exit routing - before and after B1

```mermaid
flowchart TD
    subgraph Current["Historical problem"]
        P1[Open position\nowned by OI_BUILDUP]
        EX1[Universal exit pool\nORB + EMA + VWAP + OI]
        P1 --> EX1
        EX1 -->|first passing vote wins| C1([Close - any strategy])
    end

    subgraph Fixed["Current runtime"]
        P2[Open position\nowned by OI_BUILDUP]
        OWN[Owned exits\nOI_BUILDUP only]
        SHARED[Shared exits\nORB + EMA + VWAP]
        HARD[Hard exits\nstop / trail / time / risk]
        P2 --> HARD
        HARD -->|fires first, always| C2([Close])
        P2 --> OWN
        OWN -->|evaluated first| C3([Close - attributed correctly])
        OWN -->|no valid vote| SHARED
        SHARED --> C4([Close - fallback only])
    end
```

---

## 5. Current engine lanes

```mermaid
flowchart LR
    subgraph DET["deterministic - research only"]
        D1[Regime] --> D2[Strategy votes] --> D3[Lot sizing] --> D4[TradeSignal]
    end

    subgraph PURE["ml_pure - live production lane"]
        P1[Regime] --> P2[Stage 1\nEnter?]
        P2 -->|No| P6([HOLD])
        P2 -->|Yes| P3[Stage 2\nCE or PE?]
        P3 --> P4[Stage 3\nRecipe]
        P4 -->|sets stop% target%\nmax_hold_bars| P5[Lot sizing] --> P7[TradeSignal]
    end
```

Legacy `ml` wrapper and registry-backed `ml_entry` overlay have been removed from the runtime path.

---

## 6. ML training pipeline (offline)

`ml_pipeline_2` runs offline. It produces the `.joblib` bundle that `strategy_app` loads at startup.

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
    I -->|No| Z([HOLD - do not publish])
    I -->|Yes| J[Publish bundle\nmodel.joblib + threshold_report.json]
    J --> K([strategy_app loads via\nML_PURE_RUN_ID + ML_PURE_MODEL_GROUP])
```

---

## 7. ML <-> strategy integration gaps

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

    G1["Gap 1\nFeature drift - two separate\ncodepaths compute same features.\nNo runtime parity check in production."]
    G2["Gap 2\nLabels are synthetic forward-path labels,\nnot realized option lifecycle outcomes.\nDeterministic exit patches do not relabel this pipeline."]
    G3["Gap 3\nNo feedback loop.\nLive outcomes do not automatically\ntrigger retraining."]

    T2 -.- G2
    T1 -.- G1
    L2 -.- G1
    L3 -.- G3
```

---

## 8. Correct order of operations

```mermaid
flowchart TD
    S1["Phase 1\nValidate runtime code + replay behavior"]
    S2["Phase 2\nKeep feature parity and session/risk tests green"]
    S3["Phase 3\nRebuild stage views only when data,\nview schema, or label recipe changes"]
    S4["Phase 4\nRetrain ml_pipeline_2\nPublish new bundle"]
    S5["Phase 5\nSwitch runtime by run_id + model_group"]
    S6["Phase 6\nDeploy ml_pure"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6
```

---

## Reference

| File | Purpose |
|---|---|
| `strategy_app/engines/deterministic_rule_engine.py` | Core decision loop |
| `strategy_app/engines/strategy_router.py` | Regime -> strategy mapping |
| `strategy_app/engines/strategies/all_strategies.py` | All strategy implementations |
| `strategy_app/risk/manager.py` | Lot sizing, halts, drawdown |
| `strategy_app/position/tracker.py` | Position state, hard exits |
| `strategy_app/runtime/redis_snapshot_consumer.py` | Snapshot intake, session lifecycle |
| `ml_pipeline_2/staged/pipeline.py` | ML training orchestration |
| `ml_pipeline_2/staged/publish.py` | Bundle publish and env handoff |
| `strategy_app/docs/ENGINE_CONSOLIDATION_PLAN.md` | Consolidation and handoff status |
