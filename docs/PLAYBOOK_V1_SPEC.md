# Playbook v1 — clever policy (spec + validation plan)

**Goal:** Replace fixed “3 trades / 50% target / 20m” with **context + thesis + dynamic hold**, while staying auditable in `rules_pipeline` first, then `strategy_app`.

---

## Production status (2026-05-21)

| Layer | Status |
|-------|--------|
| **Promoted research rule** | `PBV1_TOP3_THESIS` — monthly audit leader (~20/51 PASS months) |
| **Runtime brain** | `playbook_brain.py` + `PlaybookV1ShortCeStrategy` + profile `playbook_v1_paper_v1` |
| **Deploy** | Requires `build_run_metadata` in `main.py` + rebuild `strategy_app_historical` |
| **Next risk candidate** | `PBV1_TOP3_PRODUCTION_V1` — calm + 50% premium stop + trail |

Operator details: **`docs/PLAYBOOK_V1_OPERATOR.md`**

---

## Layers (what “clever” means here)

| Layer | Smoke proxy (rules JSON) | Full build (later) |
|-------|--------------------------|-------------------|
| **Participation** | `ctx_is_high_vix_day` disqualifier | Day score → 0 trades below threshold |
| **Entry quality** | Top-3 score + optional strict entry | Score floor + dynamic daily cap |
| **Thesis exit** | `signal_exits`: `vwap_distance >= 0` (reclaim) | ORB repair, velocity against |
| **Hold / trail** | `target_pct: 99` (no fixed take-profit); trail 15%/8%; 45m time | Per-position state machine in engine |
| **Disaster risk** | `underlying_stop_pct: 0.003`; production adds `stop_pct: 50` | Margin-aware caps in eval + paper |

---

## Rule variants

| Rule ID | File | Intent |
|---------|------|--------|
| `PBV1_TOP3_THESIS` | `pbv1_top3_thesis.json` | **Default** — thesis, `stop_pct: 100` (no premium cap) |
| `PBV1_TOP3_PRODUCTION_V1` | `pbv1_top3_production_v1.json` | Calm days + 50% premium stop + trail |
| `PBV1_TOP3_CALM_THESIS` | `pbv1_top3_calm_thesis.json` | Skip high-VIX only |
| `PBV1_TOP3_THESIS_TRAIL` | `pbv1_top3_thesis_trail.json` | Thesis + trail, no premium cap |
| `R1S_TOP3_S3_COMPOSITE` | `r1s_top3_s3_composite.json` | Research control |

Config dir: `ml_pipeline_2/configs/rules/playbook_v1/`

### Research matrices

- Smoke: `rule_matrix_playbook_v1_smoke.json`
- Production check: `rule_matrix_playbook_v1_production_check.json`

### Run smoke (ML VM)

```bash
cd /opt/option_trading
.venv/bin/python3 -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_playbook_v1_smoke.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/playbook_v1_smoke_$(date +%Y%m%d)
```

**Go criteria:** At least one PBV1 variant **PASS** both `may_jul_2024` and `aug_oct_2024`, or beats R1S on Aug–Oct.

### Smoke result (2026-05-21) — **GO**

| Rule | may_jul | aug_oct |
|------|---------|---------|
| R1S_TOP3_S3 | PASS | PASS |
| PBV1_TOP3_THESIS | PASS | PASS |
| PBV1_TOP3_THESIS_TRAIL | PASS | PASS |
| PBV1_TOP3_CALM_THESIS | PASS | PASS |
| PBV1_TOP3_QUALITY_THESIS | PASS | PASS |

**10/10 cells PASS.**

---

## Live wiring (implemented)

| Piece | Path |
|-------|------|
| Policy / exits | `strategy_app/engines/playbook_brain.py` |
| Strategy | `strategy_app/engines/strategies/rule_top3_short_ce.py` → `PlaybookV1ShortCeStrategy` |
| Profile | `playbook_v1_paper_v1` in `strategy_app/engines/profiles.py` |
| Rule override | env `PLAYBOOK_V1_RULE_PATH` (docker-compose historical service) |

```bash
STRATEGY_PROFILE_ID=playbook_v1_paper_v1
PLAYBOOK_V1_RULE_PATH=/app/ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_thesis.json
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1:historical ...
```

---

## Phase 2 — in progress

1. Monthly matrix — `playbook_v1_monthly_*` leaderboard
2. Runtime replay parity — `ops/gcp/compare_rules_runtime_day.py`
3. Eval UI — run picker + deep links (`eval.jsx?v=5`)
4. Pre-flight — `ops/gcp/preflight_historical_replay.py`
5. Production rule backtest — `PBV1_TOP3_PRODUCTION_V1` vs thesis on Aug–Oct + Sep-24

---

## Phase 3 — paper gates

- Rules monthly PASS stable for promoted rule
- Runtime replay matches rules on sample days (`PARITY_OK`)
- Paper week: max day loss within operator cap; exit mix not 100% blind time_stop
- Document promoted rule in operator sheet (done in `PLAYBOOK_V1_OPERATOR.md`)

---

## Out of scope (smoke)

- Partial scale-out at 20% / 30% milestones
- Long PE/CE playbooks (0 PASS in debit monthly)
- Automated week gate (failed v1–v3 regime filters)
- Marketing “50%+ target” as primary exit
