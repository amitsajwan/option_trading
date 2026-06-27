# Session Summary: ML Entry SIM Troubleshooting (2026-06-17)

## 1. Why velocity_base was selected (initial model findings)

### 1.1 Model comparison results (from BMM_RESULTS.md)

Three entry models were evaluated on June-2026 OTS (out-of-sample) temporal holdout (8 trading days, 2026-06-01..06-17):

| bundle | AUC | 95% CI | vs holdout-2024 |
|---|---|---|---|
| **velocity_base** (v3 equivalent) | **0.6748** | [0.6347, 0.7134] | −0.156 vs 0.831 |
| bmm_prod (compression features) | 0.6298 | [0.5846, 0.6781] | −0.185 vs 0.8146 |
| velocity_bmm | 0.6203 | [0.5738, 0.6679] | — |

**Conclusion: velocity_base is the best entry model.**
- Leads bmm_prod by **+0.045 AUC** in the forward period.
- Same ordering as the 2024 holdout (v3=0.831, bmm=0.8146).
- All models decayed ~0.15–0.19 over 18 months (expected concept drift), but velocity_base retains the highest residual edge.
- The "compression features" (BMM) that were hoped to improve over v3 actually **underperformed** on the production v2 view: −0.016 below v3. The candidate-view +0.009 gain did **not** generalize.

### 1.2 Key insight: move-detection is saturated

- Every feature variant lands at ~0.81–0.83 AUC on 2024 holdout.
- Multiple horizons (5m, 10m, 20m, 30m) select the **same entries** (correlation 0.92–0.99, perfect nesting).
- **Move-detection was never the bottleneck.** Profitability hinges on direction + the ~108pt option cost, not on marginal entry-model improvements.

### 1.3 Decision

- **Ship ONE model** (velocity_base), control selectivity via threshold.
- **Do NOT ship compression model** — view-dependent noise, not a robust improvement.
- Set `ENTRY_ML_MODEL_PATH` to the `velocity_base` entry_only_bundle.
- Set `ENTRY_ML_MIN_PROB` to a threshold that captures the top 10–15% of predicted moves.

---

## 2. What was done in this session

### 2.1 Objective

Run SIM backtests for all June 2026 dates with:
- `velocity_base` ML model
- `ENTRY_ML_MIN_PROB=0.049` (top ~10% quantile)
- ML gate enabled, ATR gate disabled (`ENTRY_VOL_GATE_ENABLED=0`)
- Verify trade signals are produced

### 2.2 Infrastructure fixes

#### A. sim_orchestrator missing docker-compose
- **Problem:** `sim_orchestrator` container did not have the `docker-compose` plugin.
- **Symptom:** Every SIM spawn failed with exit 125: `docker compose: command not found`.
- **Fix:** Installed Docker Compose v2.29.2 binary inside the container.
- **Verification:** SIM runs started spawning `strategy_app_sim` containers successfully.

#### B. strategy_app_sim image had stale code
- **Problem:** The confidence scaling fix existed locally on Windows but was **never pushed to the VM**. The Docker build on the VM used stale code.
- **Symptom:** Image still had old `confidence=round(min(1.0, entry_prob), 3)` — returns raw probability (~0.05–0.6) which fails the engine's `min_confidence=0.65` gate.
- **Fix:**
  1. SCP'd `strategy_app/engines/strategies/ml_entry.py` to VM.
  2. Rebuilt both `strategy_app` and `strategy_app_sim` images with `--no-cache`.
  3. Recreated the live `strategy_app` container.
- **Verification:** Ran `verify_image.sh` — confirmed scaling formula `0.65 + 0.35` present in image.

### 2.3 Configuration updates

- `.env.compose` updated:
  - `ENTRY_ML_MODEL_PATH=/app/.data/ml_pipeline/entry_only_bundles/velocity_base`
  - `ENTRY_ML_MIN_PROB=0.049`
  - `ENTRY_VOL_GATE_ENABLED=0`
- `ops_env.json` inside live container updated to point to the new bundle and threshold.

### 2.4 Code change

**File:** `strategy_app/engines/strategies/ml_entry.py`

**Before:**
```python
confidence=round(min(1.0, entry_prob), 3),
```

**After:**
```python
# Scale confidence to engine-passing range [0.65, 1.0] so min_confidence=0.65
# gate does not block ML votes. Same formula pattern as VOL_GATE_ENTRY.
conf = (
    min(1.0, 0.65 + 0.35 * max(0.0, (entry_prob - self._min_prob) / self._min_prob))
    if self._min_prob > 0 else 0.65
)
...
confidence=round(conf, 3),
```

**Rationale:**
- `VOL_GATE_ENTRY` scales its confidence to start at 0.65 to pass the engine gate.
- `ML_ENTRY` was returning raw probability (0.05–0.6) as confidence.
- The engine requires `confidence >= min_confidence` (default 0.65, live 0.80).
- This scaling maps threshold-crossing probability → 0.65, and max probability → 1.0.

---

## 3. What was found during SIM investigation

### 3.1 Test SIM result (2026-06-01, run_id `15991f2f-571c-4999-8888-8ef3667016f3`)

- **Total bars:** 358
- **Entry votes:** 260 (confidence now correctly scaled to 1.0)
- **Trade signals:** **0**
- **Final outcome:** All 358 bars blocked

### 3.2 Blocker distribution

| Blocker | Count | % | Notes |
|---|---|---|---|
| `sideways_returns_mixed` | **191** | **53%** | Hardcoded gate, no env override |
| `no_strategy_votes` | 66 | 18% | ML model declined (prob < 0.049) |
| `no_selection` | 47 | 13% | Trace-labeling bug for bypass votes |
| `avoid_veto` | 32 | 9% | Other strategies vetoed |
| `entry_time_windows` | 13 | 4% | Time window gate (if configured) |
| `direction_evidence_mismatch` | 9 | 3% | Evidence contradicts direction |

### 3.3 Root cause analysis

#### #1 — `sideways_returns_mixed` (53% of bars)
- **Location:** `deterministic_rule_engine.py:970-974`
- **Logic:** When regime is `SIDEWAYS` and reason contains `returns_mixed`, ALL entries are blocked for that bar.
- **Status:** Hardcoded. No env var to disable. Always active.
- **Impact:** Killed 191 out of 358 bars on June 1, 2026.

#### #2 — `no_strategy_votes` (18%)
- ML model declined these bars. Expected behavior for a selective threshold.

#### #3 — `no_selection` (13%) — TRACE LABELING BUG
- The vote passes ALL gates (regime, direction_evidence, confidence, policy).
- Trace shows `candidate_ranking: skipped` with misleading message "candidate passed but another candidate ranked higher."
- There is only **1 candidate**.
- **Root cause:** `_derive_entry_blocker` looks for `_policy_allowed` in `raw_signals`, but bypass-mode votes don't have this field set (the policy evaluation happens in `_process_entry_votes` but is not mirrored back to raw_signals before the trace is built).
- The actual signal is blocked downstream in `_build_entry_signal`, but the trace doesn't surface the specific reason.

#### Vote data confirmed valid:
- `proposed_strike=54300`
- `proposed_entry_premium=1116.15`
- `direction=PE`
- `confidence=1`
- `_entry_policy_mode=bypass`
- `entry_grade=OK`, `live_would_take=true`

The vote should produce a signal, but something in `_build_entry_signal` or its downstream path is silently blocking it.

---

## 4. Why this was difficult

1. **Multi-layer system:** Entry model → strategy vote → engine gates → policy evaluation → signal building → position opening. A failure at ANY layer produces 0 signals.
2. **Confidence mismatch:** The engine's `min_confidence` gate was designed for rule strategies (VOL_GATE_ENTRY scales to 0.65). ML_ENTRY never did this scaling.
3. **Stale image build:** Local code edits didn't propagate to the VM. The build used old code, so the fix "worked" locally but not in the container.
4. **Misleading trace labels:** `no_selection` implies no candidate passed, but the candidate DID pass all gates. The trace infrastructure has a bug in how it labels bypass-mode votes.
5. **Hardcoded regime gate:** The `sideways_returns_mixed` gate is silently killing 53% of bars with no visibility in config.
6. **SIM vs Live divergence:** Live uses `min_confidence=0.80` (from `CONSENSUS_BYPASS_MIN_CONFIDENCE`), SIM uses `0.50`. Even with the fix, the live engine is stricter.

---

## 5. Conclusions

### 5.1 What IS working
- ✅ `velocity_base` model is the best available entry model (AUC 0.6748 OOS, leads compression by +0.045).
- ✅ ML entry gate is active (`ENTRY_VOL_GATE_ENABLED=0`, `ENTRY_ML_MODEL_PATH` set).
- ✅ Confidence scaling fix IS deployed to both live and SIM images.
- ✅ Entry votes ARE produced with `confidence=1` (was 0.05–0.6 before fix).
- ✅ SIM orchestrator can now spawn containers successfully.

### 5.2 What is NOT working
- ❌ `sideways_returns_mixed` gate kills 53% of bars on SIDEWAYS days. This is the #1 blocker.
- ❌ The `no_selection` trace is a labeling bug — bypass-mode votes pass all gates but are mislabeled.
- ❌ The actual downstream blocker for the `no_selection` candidates is not surfaced in traces. Likely in `_build_entry_signal` (strike veto, live-only gate, or premium validation).
- ❌ June 1, 2026 may be a particularly bad day (chop regime). Need to test a TRENDING/BREAKOUT day.

### 5.3 What remains unknown
- Why `_build_entry_signal` returns None for votes that pass all gates.
- Whether the `no_selection` candidates are blocked by `_strike_vetoed`, `live_would_take=false`, or another downstream check.

---

## 6. Next steps (pending decision)

1. **Add env-gate for `sideways_returns_mixed`** — Allow disabling this blocker via env var (e.g., `SIDEWAYS_RETURNS_MIXED_ENABLED=0`).
2. **Fix `_derive_entry_blocker` trace labeling** — For bypass-mode votes that pass all gates, return `candidate_ranking` instead of `no_selection`.
3. **Add logging in `_build_entry_signal`** — Surface the exact reason when a vote that passed all gates still doesn't produce a signal.
4. **Run a different June date** — Pick a TRENDING or BREAKOUT day (e.g., 2026-06-02 or 2026-06-04) to verify the ML entry gate works on non-SIDEWAYS regimes.
5. **Compute top-15% threshold** — User asked for top 15% (current 0.049 is top ~10%). Need to compute from the velocity_base research bundle's probability distribution.

---

## 7. Files created / modified in this session

| File | Purpose |
|---|---|
| `strategy_app/engines/strategies/ml_entry.py` | **MODIFIED** — Added confidence scaling fix |
| `.run_tmp/verify_image.sh` | Verify Docker image contains the fix |
| `.run_tmp/inspect_bundle_kinds.py` | Inspect model bundle formats |
| `.run_tmp/repackage_velocity_base.py` | Repackage research bundle to live format |
| `.run_tmp/update_ops_env.py` | Update live container ops_env.json |
| `.run_tmp/enable_ml_entry.py` | Disable ATR gate, enable ML gate |
| `.run_tmp/update_env_compose.py` | Update `.env.compose` with new model path |
| `.run_tmp/show_strategy_config.sh` | Display relevant config values |
| `.run_tmp/query_june_dates.py` | Query available June 2026 dates from Mongo |
| `.run_tmp/enqueue_june_sims_v3.py` | Enqueue SIM runs for all June dates |
| `.run_tmp/check_sim_status.sh` | Check SIM run status |
| `.run_tmp/get_sim_results.py` | Query Mongo for vote/signal counts |
| `.run_tmp/analyze_blockers.js` | Analyze decision trace blockers |
| `.run_tmp/distinct_blockers.js` | Count distinct blocker types |
| `.run_tmp/sample_no_selection2.js` | Inspect `no_selection` candidate details |
| `.run_tmp/deep_trace.js` | Deep inspection of decision traces |
| `.run_tmp/check_votes_for_trace.js` | Cross-reference DB votes with traces |
| `.run_tmp/check_raw_signals.js` | Inspect vote raw_signals |
| `.run_tmp/dump_vote.js` | Full JSON dump of a vote |
| `.run_tmp/check_all_no_selection.js` | Check all `no_selection` traces |
| `.run_tmp/inspect_votes_jsonl.py` | Parse votes.jsonl for policy fields |
| `.run_tmp/inspect_ops_env.py` | Parse ops_env.json for config values |
| `.run_tmp/check_decisions.py` | Parse decisions.jsonl for blocker info |
| `.run_tmp/inspect_decision_trace.py` | Full JSON dump of a decision trace |
| `.run_tmp/install_compose.py` | Install docker-compose in sim_orchestrator |
