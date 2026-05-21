# Playbook v1 — clever policy (spec + validation plan)

**Goal:** Replace fixed “3 trades / 50% target / 20m” with **context + thesis + dynamic hold**, while staying auditable in `rules_pipeline` first, then `strategy_app`.

---

## Layers (what “clever” means here)

| Layer | Smoke proxy (rules JSON) | Full build (later) |
|-------|--------------------------|-------------------|
| **Participation** | `ctx_is_high_vix_day` disqualifier; stricter `ret_5m` / `vwap_distance` | Day score → 0 trades below threshold |
| **Entry quality** | Top-3 score + optional strict entry | Score floor + dynamic daily cap |
| **Thesis exit** | `signal_exits`: `vwap_distance >= 0` (reclaim) | ORB repair, velocity against |
| **Hold / trail** | `target_pct: 99` (no fixed take-profit); trail 15%/8% giveback; 45m time | Per-position state machine in engine |
| **Disaster risk** | `stop_pct: 100`, `underlying_stop_pct: 0.003` | Margin-aware caps |

---

## Smoke rules (2026-05-21)

| Rule ID | Intent |
|---------|--------|
| `R1S_TOP3_S3_COMPOSITE` | Research baseline |
| `PBV1_TOP3_THESIS` | Thesis exit only; long time; no 50% target |
| `PBV1_TOP3_THESIS_TRAIL` | Thesis + premium trail after MFE |
| `PBV1_TOP3_CALM_THESIS` | Thesis + trail + skip high-VIX days |
| `PBV1_TOP3_QUALITY_THESIS` | Stricter entry + calm + thesis + trail |

Config dir: `ml_pipeline_2/configs/rules/playbook_v1/`  
Matrix: `ml_pipeline_2/scripts/rules_pipeline/rule_matrix_playbook_v1_smoke.json`

### Run smoke (ML VM)

```bash
cd /opt/option_trading
.venv/bin/python3 -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_playbook_v1_smoke.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/playbook_v1_smoke_$(date +%Y%m%d)
```

**Go criteria for full flash:** At least one PBV1 variant **PASS** both `may_jul_2024` and `aug_oct_2024`, or **beats R1S on Aug–Oct** (where R1S historically struggles on exits).

### Smoke result (2026-05-21) — **GO**

| Rule | may_jul | aug_oct | Trades (May–Jul) |
|------|---------|---------|------------------|
| R1S_TOP3_S3 | PASS | PASS | 49 |
| PBV1_TOP3_THESIS | PASS | PASS | 48 |
| PBV1_TOP3_THESIS_TRAIL | PASS | PASS | 51 |
| PBV1_TOP3_CALM_THESIS | PASS | PASS | 41 |
| PBV1_TOP3_QUALITY_THESIS | PASS | PASS | 40 |

**10/10 cells PASS.** Thesis exit (VWAP reclaim, no 50% cap) **matches or beats** fixed-target R1S on both hold-out windows, including **Aug–Oct**. Quality + calm filters reduce trades (~40 vs 49) without failing smoke.

**Monthly audit** running: `playbook_v1_monthly_20260521` (tmux `pbv1_monthly`, 255 cells).

---

## Phase 2 — Full flash (after smoke)

1. **Monthly matrix** (51 months × winning PBV1 rules + R1S control).
2. **Exit reason breakdown** — % `signal:vwap_distance` vs `trail_stop` vs `time_stop`.
3. **`PlaybookV1ShortCeStrategy`** + **`PlaybookBrain`** in `strategy_app` (wired 2026-05-21).
4. **Replay parity** on runtime VM vs rules cell — use profile `playbook_v1_paper_v1`.
5. **Eval UI** — filter by `PBV1_*` strategy names.

### Live wiring (implemented)

| Piece | Path |
|-------|------|
| Policy / exits | `strategy_app/engines/playbook_brain.py` |
| Strategy | `strategy_app/engines/strategies/rule_top3_short_ce.py` → `PBV1_TOP3_QUALITY_THESIS` |
| Profile | `playbook_v1_paper_v1` in `strategy_app/engines/profiles.py` |
| Rule JSON | `ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_quality_thesis.json` |

```bash
# Runtime replay / paper
STRATEGY_PROFILE_ID=playbook_v1_paper_v1 python -m strategy_app.main ...
```

---

## Phase 3 — Verification

- Compare PASS month count vs R1S (target ≥ 7/51 or better Aug–Oct).
- Operator calm-week overlay still manual until regime gate beats 6/17.
- Document promoted rule ID in `PROJECT_STATUS` and operator sheet.

---

## Out of scope (smoke)

- Partial scale-out at 20% / 30% milestones
- Long PE/CE playbooks (0 PASS in debit monthly)
- Automated week gate (failed v1–v3 regime filters)
