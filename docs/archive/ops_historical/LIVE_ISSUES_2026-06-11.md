# Live deploy — issues register (2026-06-11)

Issues surfaced bringing the `regime_dual` brain live (real-money/paper, Dhan). Status: **fixed** / **workaround** / **open**. Grouped by data, logic/flow, external calls, deployment/infra, analysis.

---

## A. DATA
| # | Issue | Impact | Status / Fix |
|---|---|---|---|
| D1 | **`atm_oi_change_30m` only ~57% present** — computed along the *rolling ATM strike*, so it's `None` whenever the current ATM ≠ the ATM 30 min ago (price drift across strikes). 114/173 None bars are this. | combo's agreement-lever needs 30m OI → abstains ~46% of bars → major cause of "never sure of direction". | **Regenerable** (proven 57%→**100%** from the stored full 25-strike chain). Fix: (a) recompute feature for backtest, (b) enrichment should retain **all-strike** OI history live. **OPEN** |
| D2 | Futures bars store only `close` intraday (no high/low fields). | Day H/L must be derived from the close series (slightly understated). | workaround (derive from closes). **minor** |
| D3 | Thin live history — ~5–6 usable days in mongo (some days 1 snapshot; 06-04→06-09 gap from VM deletion). | Recent-regime measurements are low-n / one-regime (bear-ish). | **open** — use historical parquet (2020-24) for decision-grade stats. |
| D4 | Grounding's prev-day level (Gemini said 06-10 close 55,506) ≠ our **futures** close (55,209). | spot-index vs futures mismatch; don't trust LLM's exact numbers. | note; reconcile spot vs futures when using LLM levels. |

## B. LOGIC / FLOW
| # | Issue | Impact | Status / Fix |
|---|---|---|---|
| L1 | **Entry-first vs direction-first** — code checks entry_020 first every bar; intended plan is regime→direction→*then* entry. | Conceptually wrong (AND-gate → same trade set, but not the intended flow). | **OPEN** — re-order to direction-first in combo rework. |
| L2 | **`combo` too restrictive — fires only 4%** (needs momentum+OI+max_pain+EMA all agree; OI often missing). | 0 trades today (entry fires 74%, but combo 4% and no overlap with MID/TREND+entry). | **OPEN** — relax to **2-of-3 majority** (+ fix OI D1). The real lever for getting trades. |
| L3 | Magnitude entry gate **hurts** when stacked with direction (selects big-move = coin-flip-direction bars). | mag≥0.8 + direction → −3.4%. | finding — keep entry permissive/as-confirm, not lead gate. |
| L4 | Dual CE/PE models **saturated** live (predict ~1.0 on everything). | confirm step was a no-op. | **dropped** (no dual paths set). |
| L5 | **Two regime gates stacked** — engine strategy_router (regime→ML_ENTRY only in SIDEWAYS/TREND/HIGH_VOL/BREAKOUT) + ml_entry's MID/TREND gate. | CHOP/AVOID/PANIC/DEAD → ML_ENTRY never runs. | by design; be aware both must pass. |
| L6 | Relabeling entry → direction (clean-move / continuation / monotonic) **refuted** (AUC 0.83→0.49). | can't beat the direction info-ceiling by labeling. | closed — direction needs new info or the agreement-lever. |
| L7 | Direction is **regime-dependent/non-stationary** (2024 trend-follow 55% ; 2026 mean-revert: flow 43%, fade 57%). | a static direction model flips sign by regime. | open — needs a live regime/follow-vs-fade detector. |

## C. EXTERNAL CALLS (Gemini / Dhan)
| # | Issue | Impact | Status / Fix |
|---|---|---|---|
| X1 | Gemini grounded call **intermittent 503 / timeout**. | session_bias kept falling back to NEUTRAL. | **FIXED** — `_call_gemini` retries 3× (2s/4s backoff) + timeout 30→40s (`94a98be`). |
| X2 | Grounded JSON **truncates mid-object** → `extract_json_object` failed → valid BEARISH parsed as NEUTRAL. | LLM bias lost even when retrieved. | **FIXED** — regex-salvage day_bias/conviction/grounded (`e2a0047`). |
| X3 | Wrong Gemini key first (`…VGH_8g` = depleted credits). Funded key = `…paZN-w`. | grounding looked dead until corrected. | resolved (use funded key). **rotate keys.** |
| X4 | Gemini **429 quota** (from heavy testing) — only `gemini-2.5-flash` has quota (`pro`/`flash-latest`/`2.0` → 429). | grounding degrades to NEUTRAL while throttled. | self-inflicted; recovers; safe-degrade. Pin model to flash. |
| X5 | Grounding **facts can be wrong** (said "gap-down" on a gap-up day). | don't trust as truth — soft veto only. | by design (grounded flag + veto-only). |
| X6 | LLM refresh triggers only when **ml_entry runs** (regime-routed) — not guaranteed every 15 min in CHOP. | bias can stale during long CHOP. | **open** — move refresh to a per-bar/oversight hook for strict cadence. (TTL set 900s.) |
| X7 | Dhan token is **1-day** (rotate daily). IP whitelist NOT required (VM auth'd 200). | daily token refresh needed for live. | note. |

## D. DEPLOYMENT / INFRA
| # | Issue | Impact | Status / Fix |
|---|---|---|---|
| P1 | `docker compose build` served a **stale image** (new code not baked). | first "deploy" ran old code (grep regime_dual=0). | **workaround** — `docker cp` files + `restart` (not rebuild). Proper: `--no-cache` rebuild. |
| P2 | compose `environment:` block lists vars **explicitly** — new `REGIME_*`/`GROUNDING_*`/`BRAIN_DUAL_MODE` not passed unless added. | regime_dual silently ran defaults (shadow/agreement_lever). | **FIXED** — patched docker-compose.yml (added after each `ML_ENTRY_DIRECTION_MODE`). |
| P3 | **VM lost network tag** `option-trading-runtime` → firewall rule for :8008 inert → dashboard unreachable. | UI "down" externally. | **FIXED** — `gcloud compute instances add-tags`. |
| P4 | `STRATEGY_ROLLOUT_STAGE=live` **invalid** (choices: paper/shadow/capped_live) → crash loop. | strategy down. | **FIXED** — use `paper` (real orders gate on `EXECUTION_ADAPTER=dhan`+grade; stage is cosmetic). |
| P5 | `capped_live` requires `position_size_multiplier ≤0.25` → **0.25×1lot=7.5qty invalid** for options → crash. | can't use capped_live at 1 lot. | **FIXED** — paper stage instead. |
| P6 | **Old trades in 3 sources** — positions.jsonl, `trade_signals`, **`strategy_positions`** (the TAPE source). | cleaning jsonl/trade_signals didn't clear the dashboard. | **FIXED** — deleted `strategy_positions` today. |
| P7 | `/app/.run` not writable in replay → decision-trace write errors. | noisy logs; traces missed. | workaround `STRATEGY_RUN_DIR=/tmp`. |
| P8 | `strategy_persistence_app` shows **unhealthy** (cosmetic — consuming/writing fine, 0 errors). | false alarm. | open — relax healthcheck threshold. |

## E. ANALYSIS / FINDINGS
| # | Finding |
|---|---|
| A1 | Strategy is **break-even/negative** through the real engine (composite 47% / PF 0.96). |
| A2 | **Entry is not the problem** (fires 74%); **direction is** (combo 4%). The whole edge hinges on direction. |
| A3 | New brain has **0 observed live trades** (regime CHOP/SIDEWAYS + combo 4%) — unvalidated live. |
| A4 | Recent regime **mean-reverts**: flow-following 43%, fade-VWAP 57%, agreement-lever holds ~62%. |
| A5 | **MID (pullback-in-trend) best** regime bucket (~70%); CHOP worst (49%); extended TREND ≈ breakeven (reversion). |
| A6 | Naive every-bar backtest **inflated** (214 trades, overlap); real engine de-overlaps to ~3–7. |

---

## Top priorities (next session)
1. **D1 + L2** — regenerate OI (all-strike history) + relax combo to **2-of-3** → direction confirms, trades happen. *(the unblocker)*
2. **L1** — re-order to **direction-first** (flow clarity).
3. **A2/A4** — settle direction on **historical 2020-24** (regime follow-vs-fade) before trusting live.
4. **P1/P2** — durable rebuild (`--no-cache`) so code+env survive recreate (currently cp+restart).
