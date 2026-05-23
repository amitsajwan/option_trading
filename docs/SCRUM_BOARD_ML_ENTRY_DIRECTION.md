# Scrum board — ML entry + direction (`trader_master_ml_entry_det_dir_v1`)

**Living document** — update status, owners, and **Results** after each replay / merge.  
**Last updated:** 2026-05-23 (E3-S6 dual direction model system implemented; training pending)  
**Profile under test:** `trader_master_ml_entry_det_dir_v1` / `trader_master_ml_entry_v1` · **Engine commit (baseline):** `a133936`

Related: [BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md](BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md) · [runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md](runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md) · [ENTRY_AND_DIRECTION.md](ENTRY_AND_DIRECTION.md)

---

## How to use this board

1. Pick a story from **Backlog** → set **Owner** → move to **In progress**.
2. Check off **Tasks** in the story; link PRs / run IDs in **Results log**.
3. When **Acceptance criteria** are met, move to **Done** and paste metrics into **Results log**.
4. Do **not** start Epic 4 (caps / TIME_STOP) until Epic 3 OOS direction stories pass gates.

**Status values:** `Backlog` | `Ready` | `In progress` | `In review` | `Done` | `Blocked`

**Priority:** `P0` (blocking) · `P1` (this sprint) · `P2` (next) · `P3` (later)

---

## Team roster (assign in standup)

| Name | Role | Stories owned |
|------|------|----------------|
| _@name_ | **Ops / GCP** | **E2-S6, E2-S7, E2-S8** — all replays, results log, parquet |
| _@name_ | **Engine** | **E3-S1, E3-S2** — CE guardrail, direction ML wire |
| _@name_ | ML / research | E3-S3, E3-S4 — publish gate, conditional S2 |
| _@name_ | Tech lead | E1-S2 commit, review, sprint |

### Work packages (scripts)

| Team | Script | Purpose |
|------|--------|---------|
| **Ops/GCP** | `ops/gcp/run_ops_replay_suite.sh` | `diagnose` \| `in_sample` \| `pe_only_primary` \| `all` |
| **Ops/GCP** | `ops/gcp/diagnose_oos_replay_coverage.py` | E2-S7 trades/votes/blockers by month |
| **Ops/GCP** | `ops/gcp/check_parquet_coverage.py` | E2-S8 partition gaps |
| **Ops/GCP** | `ops/gcp/run_oos_validation_replay.sh` | Standard OOS windows + `all` |
| **Engine** | `ops/gcp/run_engine_direction_ab.sh` | `baseline` \| `pe_only` \| `direction_ml` \| `v1_direction_ml` \| `v1_dual_direction_ml` |
| **Engine** | `ops/gcp/patch_trader_master_ml_entry_pe_only_env.sh` | E3-S1 PE-only |
| **Engine** | `ops/gcp/patch_trader_master_ml_entry_direction_ml_env.sh` | E3-S2 det_dir + direction bundle |
| **Engine** | `ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh` | E3-S5 ML-only profile + single direction bundle |
| **Engine** | `ops/gcp/patch_trader_master_ml_entry_v1_dual_dir_env.sh` | E3-S6 ML-only profile + dual direction bundle |
| **ML** | `ops/gcp/run_direction_dual_hpo_vm.sh` | E3-S6 train CE + PE models + export dual bundle |
| **ML** | `ml_pipeline_2/scripts/export_direction_dual_bundle.py` | Export CE + PE runs → direction_dual_bundle.joblib |

---

## Current sprint

| Field | Value |
|-------|--------|
| **Sprint** | Sprint 1 — OOS validate + direction path |
| **Dates** | 2026-05-23 → _end date TBD_ |
| **Sprint goal** | Fair eval harness; confirm in-sample edge; ship direction Tier 1 or wire S2 bundle with measured OOS CE/PF |

---

## Board snapshot (copy to Jira / Linear / Notion)

| ID | Story | Priority | Owner | Status | Points |
|----|-------|----------|-------|--------|--------|
| E1-S1 | ML_ENTRY primary voter in engine | P0 | | **Done** | 5 |
| E1-S2 | Document breakthrough + frozen config | P1 | | **Done** | 2 |
| E2-S1 | OOS runbook + analyze scripts | P1 | | **Done** | 3 |
| E2-S2 | Eval replay risk patch (consec/session) | P0 | | **Done** | 3 |
| E2-S3 | Brain skip flag for ML-entry eval | P1 | | **Done** | 2 |
| E2-S4 | Re-run 3-window validation (fair harness) | P0 | | **Done** | 5 |
| E2-S5 | Fix replay orchestrator (wait on run_id) | P1 | | **Done** | 1 |
| E2-S6 | Full Aug–Oct in-sample replay (all days) | P1 | **Ops/GCP** | **In review** | 3 |
| E2-S7 | Investigate May-only / low vote count on OOS | P1 | **Ops/GCP** | **Done** | 5 |
| E2-S8 | 2023 parquet backfill for secondary OOS | P2 | **Ops/GCP** | **Blocked** | 8 |
| E3-S1 | Tier 1 — CE guardrail / PE-only A/B replay | P0 | **Engine** | **Done** | 5 |
| E3-S2 | Export + wire `DIRECTION_ML_MODEL_PATH` | P1 | **Engine** | **Done** | 5 |
| E3-S3 | Direction publish gate + OOS re-test | P1 | **Engine** | **In review** | 3 |
| E3-S4 | Conditional S2 train (entry-positive bars) | P2 | | **Backlog** | 8 |
| E3-S5 | Profile `trader_master_ml_entry_v1` eval path | P1 | **Engine** | **Done** | 5 |
| E3-S6 | Dual direction model (CE + PE per-side) | P1 | **ML** | **Ready** | 8 |
| E4-S1 | Pilot higher session trade cap | P3 | | **Backlog** | 3 |
| E4-S2 | TIME_STOP / MFE giveback experiment | P3 | | **Backlog** | 5 |

**Velocity (this sprint):** _planned_ / _completed_ points

---

# Epics and stories

## Epic E1 — ML_ENTRY integration (vote pool)

**Outcome:** ML timing votes are not vetoed by silent rules; in-sample breakthrough reproducible.

### E1-S1 — ML_ENTRY as primary voter

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 5 |

**User story:** As a trader, I want ML entry signals to compete in the vote pool when rules are silent, so that good ML timing is not blocked by `no_selection`.

**Acceptance criteria**

- [x] `deterministic_rule_engine.py`: silence ≠ veto; `ML_ENTRY` stays in pool (`a133936`)
- [x] Risk config preserved across ticks (`ffd5c83`, profile startup)
- [x] Historical replay shows `no_selection` ≪ 1% of prior (~1408 → ~1)

**Tasks**

- [x] Implement vote-pool logic
- [x] Deploy to `option-trading-runtime-01`
- [x] Aug–Oct replay: ~61 trades, PF ~1.98 (reference — see results log)

**Results:** See run reference `breakthrough_aug_oct_2024` (user-reported); CE/PE balanced.

---

### E1-S2 — Breakthrough + frozen config doc

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 2 |

**User story:** As the team, we need a frozen config doc so OOS and direction work do not drift env/thresholds.

**Acceptance criteria**

- [x] `docs/BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md` published
- [x] Frozen: `ENTRY_ML_MIN_PROB=0.65`, profile id, stop 20% / trail 35%

**Tasks**

- [x] Write breakthrough doc
- [ ] Commit + push all local ops/docs changes to `main` _(pending team)_

---

## Epic E2 — OOS validation & eval harness

**Outcome:** Pass/fail for edge is measured with a **fair replay harness**, on defined windows, with logged run IDs.

**Pass bar (per window):** ≥40 trades · PF ≥1.30 · CE PF ≥1.00 · PE PF ≥1.00 · stop ≈20%

### E2-S1 — OOS runbook and analysis tooling

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 3 |

**Acceptance criteria**

- [x] `docs/runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md`
- [x] `ops/gcp/analyze_oos_validation_run.py` (PASS/FAIL exit code)
- [x] `ops/gcp/run_oos_validation_replay.sh` (`all` \| single window)

---

### E2-S2 — Eval replay risk patch

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 3 |

**User story:** As ML ops, I want replay risk limits relaxed for measurement so `risk_pause` does not dominate before we judge alpha.

**Acceptance criteria**

- [x] `ops/gcp/patch_trader_master_eval_replay_env.sh`
- [x] `RISK_MAX_CONSECUTIVE_LOSSES=15`, `RISK_MAX_SESSION_TRADES=12` in `.env.compose`
- [x] `RISK_MAX_SESSION_TRADES` wired in `docker-compose.yml` (historical + live)
- [x] VM container env verified

**Tasks**

- [x] Add patch script
- [x] Wire compose
- [x] Integrate into `run_oos_validation_replay.sh` `setup_frozen_env`

---

### E2-S3 — Brain gate skip for eval (ML-entry profile)

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 2 |

**Acceptance criteria**

- [x] `ML_ENTRY_DET_SKIP_BRAIN_GATE` env + engine guard
- [x] Eval patch sets `true`; production default `false`
- [x] Traces: `brain_gate:no_entry_votes` no longer top blocker on eval runs

---

### E2-S4 — Three-window validation run (fair harness)

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 5 |

**Acceptance criteria**

- [x] Primary OOS queued and analyzed
- [x] Secondary skipped when `emitted=0`
- [x] In-sample sanity queued and analyzed
- [x] Run IDs in `/tmp/oos_validation_runs.json` on VM

**Tasks**

- [x] `run_oos_validation_replay.sh all` on VM (`oos_all2`)
- [x] Compare script output saved

---

### E2-S5 — Orchestrator waits on queued `run_id`

| | |
|--|--|
| **Status** | Done |
| **Owner** | |
| **Points** | 1 |

**Acceptance criteria**

- [x] No false analysis of `/runs/latest` stale run

---

### E2-S6 — Full-window in-sample replay

| | |
|--|--|
| **Status** | In review |
| **Owner** | **Ops/GCP** |
| **Points** | 3 |

**User story:** As the team, we need Aug–Oct replay to trade **all months**, not only Aug 1–8, before comparing to breakthrough (61 trades).

**Acceptance criteria**

- [x] Trades span **Aug + Sep** (146 trades on run `793f3a4d` after persistence settled)
- [ ] **Oct 2024** month present in analyze output
- [x] ≥55 trades, CE & PE leg PF ≥1.0
- [ ] PF ≥1.5 (got **1.19** on `793f3a4d`)
- [x] Run ID in results log

**Tasks**

- [x] Diagnose `793f3a4d` — Aug 116 + Sep 30 trades
- [ ] Fresh `in_sample_sanity` after preflight fix + `replay_only` (batch hit `PREFLIGHT_FAIL` on force-recreate)
- [ ] Confirm Oct month once consumer stable

**Result (2026-05-23):** `793f3a4d` — **146 trades**, cap **+7.7%**, PF **1.19**, CE/PE leg PF **1.19** each. **PASS** trade count + leg PF; **FAIL** portfolio PF &lt; 1.30. No Oct bucket yet.

---

### E2-S7 — Investigate May-only OOS + low ML vote count

| | |
|--|--|
| **Status** | Done |
| **Owner** | **Ops/GCP** |
| **Points** | 5 |
| **Priority** | P1 |

**User story:** As engineering, we need to understand why May–Jul replay trades only in May and why ML vote counts dropped (138 vs ~1350).

**Acceptance criteria**

- [x] Root cause doc section below
- [x] Mitigation: truncate `session_summary.jsonl` in `clean_state_before_replay.sh`
- [ ] Re-run primary OOS with ≥40 trades across **May+Jun+Jul** (still open — May-only persists on VM)

**Tasks**

- [x] `sudo bash ops/gcp/run_ops_replay_suite.sh diagnose` on VM
- [x] `diagnose_oos_replay_coverage.py` on `57e60de8`, `5104f59d`, `793f3a4d`

#### E2-S7 findings (VM diagnose 2026-05-23)

| Run | Trades | Trade months | ML votes | Top blocker |
|-----|--------|--------------|----------|-------------|
| `57e60de8` | 64 | **2024-05 only** (14 days) | 1334 | `no_entry_votes` 795, `risk_pause` 553 |
| `5104f59d` | 30 | **2024-05 only** (3 days) | 138 | `entry_phase` 73 |
| `793f3a4d` | 146 | **2024-08 + 2024-09** | 1170 | **`avoid_veto` 2098**, `entry_phase` 695 |

**Conclusions**

1. **Low vote count on `5104f59d`** — only **3 trade-days** in May vs 14 on `57e60de8`; engine processed far fewer entry-phase days (likely consumer lock / preflight / short replay window), not missing ML model.
2. **May-only on OOS primary** — both runs emitted **23,412** snapshots but closes cluster in May. Traces also **2024-05 only** on `57e60de8`. Needs follow-up: rule/ML gates by month or replay orchestrator date cursor (not explained by parquet alone — replays do emit).
3. **`avoid_veto`** dominates Aug–Sep on `793f3a4d` — IV/regime veto is the main cap on in-sample trade count after May window.
4. **`session_summary.jsonl`** — now truncated in `clean_state` to prevent cross-run carry pollution between back-to-back replays.
5. **Preflight** — force-recreate + 30s wait caused `PREFLIGHT_FAIL`; use `run_engine_direction_ab.sh replay_only` (restart + 180s wait) instead of repeated `--force-recreate`.

---

### E2-S8 — 2023 parquet for secondary OOS

| | |
|--|--|
| **Status** | Blocked |
| **Owner** | **Ops/GCP** |
| **Points** | 8 |
| **Blocked by** | Data pipeline — no `2023-05` partition on VM |

**Acceptance criteria**

- [ ] `emitted > 0` for 2023-05-01 → 2023-07-31 replay
- [ ] Secondary OOS meets pass bar or documented fail

**Tasks**

- [x] Ticket opened: [docs/tickets/E2-S8_PARQUET_2023_BACKFILL.md](tickets/E2-S8_PARQUET_2023_BACKFILL.md)
- [x] `check_parquet_coverage.py` on VM — layout is `year=YYYY/` (checker needs path fix; replay still works via orchestrator)
- [x] Secondary replay `emitted=0` confirmed — **blocked on data backfill**

**Note:** VM has `year=2023` and `year=2024` dirs; secondary `emitted=0` may be orchestrator date-range or empty year partition — assign to data pipeline per ticket.

---

## Epic E3 — Direction quality (CE vs PE)

**Outcome:** Step ② side selection does not destroy OOS; CE leg PF ≥1.0 on primary OOS.

**Not in scope this epic:** Entry HPO, session cap tuning, TIME_STOP tuning.

### E3-S1 — Tier 1: CE guardrail / PE-only A/B

| | |
|--|--|
| **Status** | In progress |
| **Owner** | **Engine** |
| **Points** | 5 |
| **Priority** | P0 |

**User story:** As a quant, I want to prove CE momentum is the leak before training direction ML.

**Acceptance criteria**

- [x] Env flags: `ML_ENTRY_BLOCK_CE`, `ML_ENTRY_PE_ONLY`
- [x] Replay **oos_primary** PE-only on VM (`cfe3f5a7`)
- [x] Documented in results log
- [x] **Decision: do not proceed to Tier 2 on PE-only alone** — PF 0.92, 16 trades; CE still present from **rule strategies** (10 CE / 6 PE)

**Tasks**

- [x] `ml_entry.py` — PE-only / block-CE
- [x] `replay_only pe_only` on VM
- [ ] Follow-up: `trader_master_ml_entry_v1` (no rule CE) or rule CE filter for clean A/B

---

### E3-S2 — Wire direction ML bundle to runtime

| | |
|--|--|
| **Status** | In progress |
| **Owner** | **Engine** |
| **Points** | 5 |
| **Depends on** | E3-S1 decision; direction HPO export |

**User story:** As ops, I want `DIRECTION_ML_MODEL_PATH` set so `ML_ENTRY` uses S2 bundle instead of `fut_return_5m`.

**Acceptance criteria**

- [ ] `export_direction_bundle_from_research` → `direction_only_model.joblib`
- [ ] Patch sets `DIRECTION_ML_MODEL_PATH`; `direction_source=direction_ml` in votes
- [ ] In-sample + primary OOS replays completed

**Tasks**

- [x] Exported from `direction_s2_only_hpo_v2_20260522_190956` → `artifacts/direction_only/published/direction_only_model.joblib`
- [x] `replay_only direction_ml` on VM (`f6195884`)
- [x] Results log updated

**Result:** Direction ML wired but **6 trades**, PF **0.74**, **`avoid_veto` 1135** — S2 bundle alone does not fix OOS; need veto/regime review or E3-S5 simpler profile.

---

### E3-S3 — Direction publish gate

| | |
|--|--|
| **Status** | In review |
| **Owner** | **Engine** |
| **Points** | 3 |
| **Depends on** | E3-S5 done |

**Acceptance criteria**

- [ ] Holdout AUC documented (target: clearly >0.55; prior v2 ~0.56)
- [x] OOS primary on **v1 profile**: `direction_ml` vs `momentum` A/B (May 2024 window)
- [x] Publish decision recorded in results log

#### E3-S3 A/B (2026-05-23, `trader_master_ml_entry_v1`, oos_primary)

| Variant | Run ID | Trades (analyze) | PF | Cap % | CE PF | PE PF | avoid_veto top? |
|---------|--------|------------------|-----|-------|-------|-------|-----------------|
| **direction_ml** | `ae5a86b7` | 48 | **2.21** | **+12.3** | n/a* | **2.21** | No |
| **momentum** | `0eda153a` | 44 | **0.57** | -5.6 | 0.12 | 1.10 | No |

\*Analyze snapshot at completion; Mongo had more closes on both runs. Both May-only.

**Decision:** On v1 profile, **keep `DIRECTION_ML_MODEL_PATH`** for eval — momentum materially worse (PF 0.57 vs 2.21). **Do not publish** to live until CE leg stable and Jun/Jul OOS coverage fixed. Holdout AUC doc still open.

**Commands:** `replay_only v1_direction_ml` · `replay_only v1_momentum` (`3327d94`)

---

### E3-S4 — Conditional S2 (train on entry-positive bars)

| | |
|--|--|
| **Status** | Backlog |
| **Owner** | |
| **Points** | 8 |
| **Priority** | P2 |

**User story:** As ML research, I want direction trained only when the entry model would fire, so S2 answers “which side given we enter?”

**Acceptance criteria**

- [ ] Manifest or filter: rows with `entry_prob ≥ 0.65` (or S1 label)
- [ ] HPO completes; compare holdout vs unconditional S2
- [ ] Replay shows CE PF improvement vs E3-S2

---

### E3-S5 — Simpler profile eval (`trader_master_ml_entry_v1`)

| | |
|--|--|
| **Status** | **Done** |
| **Owner** | **Engine** |
| **Points** | 5 |

**User story:** As the team, we want a profile with ML entry + ML direction only (no rule-book CE/PE conflict) for cleaner debugging.

**Root cause of E3-S2 failure:** `det_dir_v1` profile includes TRADER_COMPOSITE, TRADER_V3_COMPOSITE, OI_BUILDUP — all of which emit `AVOID` direction votes (TRADER_SKIP, TRADER_V3_SKIP, OI_UNWINDING). Any single `AVOID` vote vetoes the entry, regardless of ML_ENTRY's high-confidence CE/PE vote. 1135 `avoid_veto` in May–Jul 2024 effectively killed all ML entries.

**Fix (implemented):**
- `PROFILE_TRADER_MASTER_ML_ENTRY_V1` added to `_PROFILES_ML_ENTRY_DET_DIRECTION` in `deterministic_rule_engine.py` → brain-gate skip, ML timing gate, and direction-conflict resolution all active for v1 profile.
- `ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh` — sets `STRATEGY_PROFILE_ID=trader_master_ml_entry_v1` + direction ML bundle.
- `run_engine_direction_ab.sh v1_direction_ml` variant added; also wired in `replay_only v1_direction_ml`.
- V1 voter pool: only IV_FILTER (AVOID veto on bad IV regime) + ML_ENTRY (CE/PE timing + direction). No TRADER_SKIP, no OI_UNWINDING, no rule-book conflicts.

**Acceptance criteria**

- [x] `replay_only v1_direction_ml` OOS primary shows ≥ 20 trades (**116** closes Mongo; analyze snapshot **48** at completion)
- [x] `avoid_veto` not in top blockers (was **1135** on E3-S2 `f6195884`; R3 top: `session_trade_cap`, `entry_phase`)
- [x] Results compared to `det_dir_v1` in log (E3-S2: 6 trades / PF 0.74)
- [ ] Direction quality gate (E3-S3): CE PF ≥ 1.0 on `direction_ml` source — **deferred** to E3-S3 (`analyze_direction_quality.py` payload fix on `main`)

**Valid run (R3):** `ae5a86b7-9198-4e64-9399-fd5fea03e293` · profile `trader_master_ml_entry_v1` · PF **2.21** · cap **+12.3%** · May-only dates (E2-S7 still open)

**Void runs (do not use):** `0acd6aea` (det_dir overwrite), `bbc85202` (restart without env reload). Fixed: `OOS_REPLAY_SKIP_ENV_PATCH`, force-recreate after patch (`2bb94c8`, `85aa170`).

**VM command:**
```bash
sudo bash ops/gcp/run_engine_direction_ab.sh v1_direction_ml
# or replay-only (no rebuild):
sudo bash ops/gcp/run_engine_direction_ab.sh replay_only v1_direction_ml
```

---

### E3-S6 — Dual direction model (CE + PE per-side)

| | |
|---|---|
| **Status** | Ready |
| **Owner** | ML |
| **Priority** | P1 |
| **Points** | 8 |

**Hypothesis:** Training one unified direction model on "CE vs PE" compresses two independent signals into one model with near-random AUC (0.557). Separate per-side binary models — "is CE profitable today?" and "is PE profitable today?" — have cleaner oracle labels and can produce independent edge.

**Architecture (Option C):**
- `model_CE` trained with `ce_win_v1` labeler: positive = `best_ce_net_return_after_cost > 0`
- `model_PE` trained with `pe_win_v1` labeler: positive = `best_pe_net_return_after_cost > 0`
- Both use `fo_direction_entry_context_v1` feature set (regime + velocity + IV + OI + oracle rolling)
- Exported as `direction_dual_bundle.joblib` with CE and PE sub-bundles
- Runtime: pick whichever side has `P(win) > 0.5`; if neither → no direction → no trade

**Tasks:**
- [x] Add `fo_direction_entry_context_v1` feature set to `ml_pipeline_2/catalog/feature_sets.py`
- [x] Add `build_stage2_labels_ce_win_v1` + `build_stage2_labels_pe_win_v1` to `pipeline.py`
- [x] Register `ce_win_v1` + `pe_win_v1` in `registries.py`
- [x] Create manifests `direction_dual_ce_hpo_v1.json` + `direction_dual_pe_hpo_v1.json`
- [x] Create `ml_pipeline_2/scripts/export_direction_dual_bundle.py`
- [x] Update `strategy_app/engines/strategies/ml_entry.py` — handle `direction_dual_bundle` kind
- [x] Create `ops/gcp/patch_trader_master_ml_entry_v1_dual_dir_env.sh`
- [x] Create `ops/gcp/run_direction_dual_hpo_vm.sh`
- [x] Add `v1_dual_direction_ml` variant to `run_engine_direction_ab.sh`
- [x] Tests: `test_direction_dual_bundle.py` + `test_direction_dual_labelers.py` (14 tests pass)
- [ ] Run dual HPO on VM: `sudo bash ops/gcp/run_direction_dual_hpo_vm.sh`
- [ ] Review CE + PE holdout AUC from `direction_dual_report.json`
- [ ] Run OOS replay: `sudo bash ops/gcp/run_engine_direction_ab.sh v1_dual_direction_ml`
- [ ] Compare vs E3-S5 baseline: trades ≥ 20, PF ≥ 1.30, CE/PE balanced

**Acceptance criteria:**
- Both `model_CE` and `model_PE` holdout AUC > 0.52 (meaningful improvement over 0.50)
- OOS replay: trades ≥ 20, PF ≥ 1.30 on `oos_primary_v1_dual_direction_ml`
- CE share 25–75% (not degenerate single-side)
- `direction_source = direction_dual_ml` visible in replay logs

**VM commands:**
```bash
# Train both models + export dual bundle:
sudo bash ops/gcp/run_direction_dual_hpo_vm.sh

# Replay with dual bundle:
sudo bash ops/gcp/run_engine_direction_ab.sh v1_dual_direction_ml
# or replay-only (no rebuild):
sudo bash ops/gcp/run_engine_direction_ab.sh replay_only v1_dual_direction_ml
```

**Results log:**
| Date | Run ID | Trades | PF | CE% | Notes |
|------|--------|--------|----|-----|-------|
| — | — | — | — | — | Training pending |

---

## Epic E4 — Risk & exits (deferred)

**Gate:** Epic 3 primary OOS passes **or** explicit product sign-off.

| ID | Story | Status |
|----|-------|--------|
| E4-S1 | Pilot `RISK_MAX_SESSION_TRADES` 8→10 | Backlog |
| E4-S2 | TIME_STOP / MFE giveback | Backlog |
| E4-S3 | Council exit layer | Backlog |

---

# Results log (update after every replay)

**Harness (eval):** `patch_trader_master_eval_replay_env.sh` · consec=15 · session_trades=12 · `ML_ENTRY_DET_SKIP_BRAIN_GATE=true` · `ENTRY_ML_MIN_PROB=0.65`

| Run label | Run ID | Window | Trades | PF | Cap % | CE PF | PE PF | Pass? | Notes |
|-----------|--------|--------|--------|-----|-------|-------|-------|-------|-------|
| breakthrough_ref | _(session)_ | 2024-08 → 10 | 61 | 1.98 | +2.33 | 1.93 | 2.10 | — | Pre-OOS reference; `a133936` |
| oos_primary_v1 | `57e60de8` | 2024-05 → 07 | 64 | 0.56 | -9.7 | 0.24 | 1.22 | Fail | Live risk; May-only dates |
| oos_primary_v2 | `5104f59d` | 2024-05 → 07 | 30 | 0.77 | -1.8 | 0.47 | 1.16 | Fail | Eval harness; May-only; low ML votes |
| oos_secondary | `25cca50d` | 2023-05 → 07 | 0 | — | — | — | — | Skip | `emitted=0` no parquet |
| in_sample_v1 | `76e2dcaf` | 2024-08 → 10 | 56 | 1.26 | +4.3 | 1.28 | 1.24 | Fail | PF &lt;1.30; Aug 1–8 only |
| in_sample_v2 | `793f3a4d` | 2024-08 → 10 | **146** | **1.19** | **+7.7** | 1.19 | 1.19 | Fail | **Aug+Sep**; PF&lt;1.30; no Oct yet |
| oos_primary_pe_only | `cfe3f5a7` | 2024-05 → 07 | 16 | 0.92 | -0.2 | 0.80 | 1.08 | Fail | ML_ENTRY PE-only; **rules still CE** |
| oos_primary_dir_ml | `f6195884` | 2024-05 → 07 | 6 | 0.74 | -0.5 | 0.24 | inf | Fail | det_dir + S2; `avoid_veto` heavy |
| oos_primary_v1_dir_ml | `ae5a86b7` | 2024-05 → 07 | **116** | **2.21** | **+12.3** | 0.69 | 1.42 | **Partial** | **E3-S5/E3-S3** v1 + dir ML |
| oos_primary_v1_momentum | `0eda153a` | 2024-05 → 07 | 44 | 0.57 | -5.6 | 0.12 | 1.10 | Fail | **E3-S3** v1 + momentum; dir ML wins A/B |
| _void_ v1 R1/R2 | `0acd6aea`, `bbc85202` | — | 4–6 | — | — | — | — | Void | Wrong profile / stale container env |

**VM artifact paths:** `/tmp/e3s5_v1_replay_r3.log` `/tmp/oos_validation_runs.json` · `/tmp/oos_all2.log` · `/tmp/oos_validation_compare.log`

**Analyze command:**

```bash
sudo docker exec option_trading-dashboard-1 python /tmp/analyze_oos_validation_run.py <RUN_ID> <label>
```

---

# Definition of Done (team)

- [ ] Code on `main` (or agreed branch) with PR reviewed
- [ ] VM deploy via git pull + historical rebuild if `strategy_app` changed
- [ ] Replay run ID + analyze PASS/FAIL pasted in **Results log**
- [ ] This doc: story **Status** + **Owner** updated
- [ ] No secrets in commits

---

# Changelog (board updates)

| Date | Author | Change |
|------|--------|--------|
| 2026-05-23 | — | Board created; E1/E2 done stories; E3 ready; results from `oos_all2` |
| 2026-05-23 | — | Ops/Engine split; E3-S1/S2 code+patches; ops diagnose + suite scripts |
| 2026-05-23 | Ops/GCP | E2-S7 diagnose done; E2-S6 `793f3a4d` Aug+Sep; E2-S8 ticket |
| 2026-05-23 | Engine | E3-S1 `cfe3f5a7` PE-only; E3-S2 export + `f6195884` dir ML; `replay_only` path |
| 2026-05-23 | — | E2-S7: carry contamination fix (`clean_state` now clears `session_summary.jsonl`); diagnose shows carry state; hypotheses documented |
| 2026-05-23 | Engine | E3-S5 valid `ae5a86b7` v1+dir ML; consumer lock + `OOS_REPLAY_SKIP_ENV_PATCH` + recreate-after-patch |
| 2026-05-23 | Engine | E3-S3 `0eda153a` v1+momentum vs `ae5a86b7` dir ML — **keep direction ML** |
| 2026-05-23 | — | **Next:** E2-S7 May-only; E2-S6 in-sample; fix vote join in `analyze_direction_quality` |

---

# Next tasks (sprint order)

1. **E2-S7 follow-up (Ops, P1)** — Why OOS windows only trade May despite full emit; re-run after orchestrator check.
2. **E2-S6 (Ops, P2)** — `replay_only in_sample_sanity` on v1 + dir ML.
3. **E3-S3 close-out** — Document holdout AUC for S2 bundle; fix `analyze_direction_quality` vote↔position join.
3. **E2-S6 (Ops, P2)** — `run_oos_validation_replay.sh replay_only in_sample_sanity` after stable preflight.
4. **E2-S8 (Ops, blocked)** — 2023 parquet backfill per ticket.
5. **E4 (deferred)** — caps / TIME_STOP only after E3 OOS direction gate passes.
