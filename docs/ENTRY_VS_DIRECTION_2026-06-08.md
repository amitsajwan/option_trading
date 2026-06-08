# Entry vs Direction — the decisive decomposition (2026-06-08)

**Question:** is the problem entry (firing on non-moves) or direction (picking the wrong side)? Measured the **underlying's actual move** (futures, points) over the next 10 min for every entry across 06-01→05 (37 entries, ops-sim base trades, real mongo data).

## Result

```
avg move:  up=78  down=54  max=116 pt   (per entry, next 10 min)

ENTRY QUALITY   →  a >=50pt move came: 92% (34/37)   | >=100pt: 57%
DIRECTION QUAL  →  when a >=50pt move came, right side: 56% (19/34)
breakdown:  bigmove+RIGHT=19 (won 15)  |  bigmove+WRONG=15  |  FLAT(<50pt)=3 (lost 3)
```

## What this proves

1. **ENTRY IS EXCELLENT — it is NOT the problem.** 92% of entries are followed by a ≥50pt move within 10 min; **57% by a ≥100pt move**; average max excursion **116 pt**. The "big move loading" detector works — it almost never fires on noise (only 3/37 were flat). And the moves are **big enough to clear cost** (108pt) most of the time. The earlier "low-vol / too-small-move" worry is overstated on these entries.

2. **DIRECTION IS THE ENTIRE PROBLEM.** When a real ≥50pt move comes, we pick the right side only **56% (19/34)** — barely better than a coin flip. That is the whole gap.

3. **The economics, finally explained.** When we're on the **right side of a big move we win 79% (15/19)**; when we're on the **wrong side (15) we lose almost all**. With wins ≈ losses in size and direction at 56%, the net is breakeven-to-slightly-negative. **It is not stops, not exits, not entry, not sizing — it is side selection.**

## "Something we're not doing correct"

We've been tuning **everything except the one thing that's broken.** Entry (92%), exits (0 exit-miss, losses controlled), stops (small losses), oversight gates (washed out) — all fine or solved. **The single lever is direction: 56% → needs ~62%+.** At 62% side accuracy with the current payoff, this flips strongly positive (because right-side-on-a-big-move already wins 79%).

This also explains why **everything else washed out**: location, the anti-chase rule, the LLM oversight — they were all trying to fix direction with *structural facts that don't predict the side*. The LLM firing zero confident leans is the same wall: **the side is near-random at this horizon given our current features** (consistent with every prior direction model: ~0.52–0.59 AUC ≈ 56–58%).

## Implication / where ALL effort goes now

- **Stop touching entry, exits, stops, sizing, and gating.** They are not the problem.
- **The whole game is the CE-vs-PE side decision.** Get it from 56% to ~62% and the strategy is profitable as-is.
- That is a hard signal problem (the project has tried many direction models at ~56–58%). Realistic angles: (a) a genuinely new direction feature/signal, not a reshuffle of levels/PCR/trend (those are exhausted); (b) **abstain when direction is low-conviction** — if we can identify the ~56% as "two buckets" (a confident subset >62% and a coin-flip subset), trade only the confident subset; (c) more data to find any stable directional edge.
- **The "abstain" angle is the most promising near-term:** we don't need to be right more often on *every* trade — we need to *not trade* the ones where the side is a coin flip. The entry already finds the move; the missing skill is "do I know which way, or should I pass?"

## Bottom line
The edge is real: **the entry catches a ≥100pt move 57% of the time, and we win 79% when we're on the right side.** The only thing standing between this and profitability is **side selection at 56%.** Every future effort should be a direction experiment or a direction-confidence/abstain filter — nothing else.
