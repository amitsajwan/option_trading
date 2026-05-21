# Overnight run — 2026-05-21

## ML VM (`option-trading-ml-01`)

- **Done:** `playbook_v1_monthly_20260521` (255/255 cells)
- **Artifacts:** `/opt/option_trading/ml_pipeline_2/artifacts/rules_runs/playbook_v1_monthly_20260521/`
  - `leaderboard.md` — 37 PASS total
  - `pass_counts.txt` — THESIS 20, R1S 7, QUALITY 5 (grep-based)
  - `exit_reason_holdout.txt` — 2024 May–Oct exit mix (if postprocess finished)

**Promoted rule for live:** `PBV1_TOP3_THESIS` (monthly winner, not QUALITY).

## Runtime VM (`option-trading-runtime-01`)

- **Synced:** `playbook_brain.py`, `rule_top3_short_ce.py`, profiles, tracker, ops scripts
- **Env:** `STRATEGY_PROFILE_ID=playbook_v1_paper_v1` in `.env.compose`
- **strategy_app** restarted

### Eval replays (PBV1)

| Window | run_id | Log |
|--------|--------|-----|
| 2024-05-01 → 2024-07-31 | `7d6c8a92-8729-4904-a552-386682124c7b` | `/tmp/overnight_pbv1_queue.log` |
| 2024-08-01 → 2024-10-31 | `f8fdd6cb-1bf6-4f9d-8d63-380c4758d236` | (queued after first completes) |

**Poll overnight:** `nohup /usr/bin/python3 /opt/option_trading/ops/gcp/wait_overnight_replays.py >> /tmp/overnight_pbv1_wait.log 2>&1 &`

**Eval UI:** `http://34.93.40.198:8008/app/?mode=eval` — filter strategy `PBV1_TOP3_THESIS`

## Tomorrow checklist

1. `curl http://127.0.0.1:8008/api/strategy/evaluation/runs/<run_id>` — both `completed`
2. Compare trade count vs rules cell `PBV1_TOP3_THESIS` May–Jul / Aug–Oct
3. Exit reasons: thesis / trail vs fixed target
4. If parity OK → keep `playbook_v1_paper_v1` for paper week
