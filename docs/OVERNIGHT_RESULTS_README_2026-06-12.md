# Overnight run — read this first (morning of 2026-06-12)

Two jobs ran overnight. Both are robust to disconnects.

## A. S1 Trend-Day Rider campaign (runtime VM)
- **Where:** runtime VM `option-trading-runtime-01`, host **tmux session `overnight`**.
- **Results file (host, durable):** `/tmp/overnight/s1_campaign.log`
- **Verify:** `cat /tmp/overnight/STATUS` (START + DONE markers + heartbeats); `ls /tmp/overnight/ALL_DONE` (exists ⇒ finished).
- **Read results:** `gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "cat /tmp/overnight/s1_campaign.log"`
- **Tests inside:** T1 stop×trail sweep (anchor finder) · T2 band/OR sensitivity · T3 confirmer strictness (2 vs 3) · T4 controls (pullback vs chase vs random-dir) · T5 option cost/theta breakeven envelope. (209 historical days, loaded once.)

### Early signal (from T1, partial)
- **Underlying-R asymmetry HOLDS at scale:** every config totR-positive (+20…+86R); avgWinR **+1.7…+3.7** vs avgLossR **−0.7…−1.0** (~3:1); drop-top3 still positive ⇒ **not outlier-driven.** The daily-drift edge + level-stop asymmetry is real on 209 days. ✅
- **⚠️ THE morning question — option translation:** `optNet@2%` came out **wildly negative**. Likely cause: **option leverage** — an 80pt stop on a cheap ATM premium (~100–250) is a **−25% to −44%** option loss, and stops are frequent (win% ~35–40%). The underlying-R asymmetry may **not survive** into ATM-option P&L because the frequent small-R losses are huge in % terms, plus theta on a multi-hour hold.
  - **Hypotheses to test next:** (1) **ITM options** (delta ~0.7, higher premium → smaller % swings, less theta) may preserve the asymmetry; (2) **fewer, higher-quality trend days** (stricter confirmers / avoid wrong-trend days — see T3) to lift win%; (3) **the future** (linear, no leverage distortion — but margin); (4) re-read T5 envelope for the cost/theta line where it turns positive.
- **Caveat:** `optNet` uses delta 0.55 + theta 6%/day + the real entry ATM premium — verify the premium field isn't mis-scaled before trusting the magnitude (the *sign/relative* across configs is the signal; the absolute % may be off).

## B. Entry-model retrain (ML VM)
- **Where:** ML VM `option-trading-ml-01`, tmux `entry_fullfeature`.
- **Verify:** `bash ops/gcp/run_entry_fullfeature_retrain_vm.sh status`
- **Output:** `ml_pipeline_2/artifacts/entry_only/published_comprehensive/entry_only_model_{010,020,030}pct.joblib` + `*_report.json`. Pick any with `ship_gates_all_pass=true` that beats 020pct on separation + drop-outlier + entries/day.
- **Then:** `gcloud compute instances stop option-trading-ml-01` (don't bill idle).

## Morning order of operations
1. Read `/tmp/overnight/s1_campaign.log` end-to-end; note T1 winner + whether **T4 controls** confirm S1 (pullback) beats chase & random (if random ≈ S1, the edge is just the drift, not our entry).
2. Resolve the **option-translation question** (ITM vs ATM vs future) — this decides if S1 is real or a mirage. The underlying R is necessary but NOT sufficient.
3. Check entry-retrain reports (B), then stop the ML VM.
4. If S1 survives option translation → wire it into the engine for the real-app SIM. If not → S3 Premium Seller is the fallback (the option leverage that hurts a buyer helps a seller).

## Housekeeping
- ML VM is ON (stop after reading reports). Rotate the chat-pasted Gemini + Dhan keys. Runtime still real-money paper-cosmetic 1 lot.
