# Intelligent Brain — Agentic Architecture (the "trader mind")

**Date:** 2026-06-07 · **Status:** design only — nothing wired into the live path · **Companions:** [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md), [INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md](INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md), [INTELLIGENT_BRAIN_LLM_OVERSIGHT.md](INTELLIGENT_BRAIN_LLM_OVERSIGHT.md)

This doc records how the engine should be built so it can grow into an *agentic* system later **without re-architecting** — and where, exactly, "reasoning" (the LLM) belongs. It is design-only; the deterministic system stays HALTED and sim-gated, and the agent is a deferred convenience, never a dependency.

---

## 1. The governing principle

> **The agent orchestrates *around* the deterministic trading core — never *inside* it. Reasoning fires on *events*, not on a clock.**

There is a hard **latency wall**:

- **Fast lane (reflex, <1s/bar):** deterministic math only. No LLM. This is what makes the system tradeable (handover Decision D6).
- **Slow lane (deliberation, seconds–minutes):** the LLM/agent. It runs only when a *meaningful event* asks for thought — never on the 1-minute metronome.

A real trader is the model: reflexes act in the flurry; the thinking mind reflects at the moments that matter (putting a trade on, a loss, a regime change, end of day). We build the same split.

---

## 2. The engine as a trader's mind

| Faculty | "Trader" question | Module / lane | LLM? |
|---|---|---|---|
| **Perception (Senses)** | "What do I see right now?" | `strategy_app/senses/` — regime, compression, expansion, destination, flow, cost, risk · **fast lane**, per bar | No |
| **Reflex / instinct** | "Act, don't deliberate." | Decision brain (L2) — the deterministic TRADE/WAIT/SKIP ladder · **fast lane** | No |
| **Calculator** | "Do the numbers." | cost model · EV · opportunity quality · risk math (size locked at 1 lot) · **fast lane** | No |
| **Memory** | "Have I seen this? What happened?" | Trace store + cross-session "what works in which regime" | Read by agent |
| **Thinking / deliberation** | "Why did that happen? Should I adjust?" | Agent runtime (L3) · **slow lane** | **Yes** |
| **Attention / triggers** | "What deserves a second thought?" | Event → reasoning-trigger dispatcher | Wakes the agent |

Perception + reflex + calculation run on the fast clock. **Memory and thinking run on event triggers in the slow lane.** That's the whole design.

---

## 3. When to reason — the reasoning triggers (the answer to "after a trade… or anywhere?")

Reasoning is fired by a curated subset of the decision events the engine already emits. Each trigger runs **off the hot path**, with a time budget, and — critically — **almost never affects the live action it reflects on** (it annotates, learns, or *proposes*; it does not gate the fast lane).

| Trigger | Fires when | Budget | What it reasons about | Can affect live? |
|---|---|---|---|---|
| **pre_open** | morning briefing | ~20s | set today's posture (CALM/NEUTRAL/VOLATILE/AVOID) from facts we supply | sets posture for the day (already built: `LLMContextProvider`) |
| **post_entry** | just **after** a trade is placed | ~10s | sanity-check the full sense-context: did the senses truly cohere, or did we take a marginal one? | **No** — async shadow annotation; at most tightens *future* selectivity / raises a soft flag. Never delays or vetoes the order. |
| **post_exit** | a position closes (esp. a loss) | ~15s | **the autopsy** — was the loss a *direction* miss or an *exit* miss? Tag it. The richest learning signal. | No — writes a tagged trace for memory |
| **regime_change** | regime sense flips | ~15s | "compression firing but moves dying → regime shifted; stand down / tighten" | Proposes a posture change (sim/human-gated) |
| **risk_event** | consecutive losses / drawdown breach | ~10s | "should we stand down for the day?" | Proposes halt/pause (human-confirm) |
| **eod** | session close | ~60s | narrative + playbook update + proposed threshold tweaks | Proposals only — **sim-gated**, never auto-live |

**Post-trade reasoning, made safe:** the `post_entry` task is enqueued *after* the order is sent, so it physically cannot delay the entry — the deterministic engine has already acted. The agent reflects on what was done, not on what to do *now*. Same for `post_exit`. This is how you "reason after a trade is taken" without ever putting the LLM in the trade-decision path.

---

## 4. The four seams to build now (so the agent slots in later)

These are provider-count- and phase-independent. Build them and the agent is a drop-in.

1. **Senses as a shared tool registry.** Each sense is a pure function returning a structured `SenseVerdict`. The fast lane calls the registry per bar; the agent calls the *same* functions read-only. One implementation, two callers — the highest-leverage seam.
2. **A durable, LLM-readable trace store.** Every bar and every trade writes a reasoning trace (handover D7) via the `decision_events` envelope. This is the agent's **memory and observability** — agentic reasoning *is* reading traces.
3. **A typed tool catalog with permission + reversibility tags.** Defines what the agent *can* do; the deterministic system is the **executor and gate**:
   - `read` — `get_traces`, `get_senses`, `get_regime`, `query_market_data`, `get_positions`, `get_pnl` (safe, parallel)
   - `propose` — `propose_threshold_change` → writes a proposal, **never live**, must pass the sim
   - `action` — `halt`, `pause_strategy` (human-confirm / hard gate)
4. **Provider-agnostic model layer = the `genai_module`.** Free providers stay swappable behind it. **Active now: Groq (interpreter) + Gemini (the only one that can browse, for an optional live-fetch layer). Cohere/AI21 deferred — pluggable, add later by setting keys; no code change.**

---

## 5. Target architecture

```
              SLOW LANE (event-triggered, seconds–minutes) — agent lives here
  ┌───────────────────────────────────────────────────────────────┐
  │  AGENT RUNTIME (L3)                                            │
  │   model layer = genai_module (Groq + Gemini; Cohere/AI21 later)│
  │   tool-call loop · memory · sim-runner                         │
  └───┬──────────────────────────────────────────────▲────────────┘
      │ tool calls (read + propose-only)              │ reads traces
      ▼                                               │
  ┌─────────────────┐   ┌──────────────────┐   ┌──────┴───────────┐
  │  TOOL CATALOG   │   │  REASONING       │   │  TRACE STORE     │
  │  typed + tagged │   │  TRIGGERS        │   │  durable,        │
  │  read/propose/  │   │  pre_open·post_  │   │  LLM-readable    │
  │  action(gated)  │   │  entry·post_exit·│   │  (memory)        │
  └───┬─────────────┘   │  regime·risk·eod │   └──────▲───────────┘
      │ wraps same      └───────▲──────────┘          │ writes
      │ senses                  │ subset of events     │ every bar + trade
 ═════╪═════════════════════════╪══════ LATENCY WALL (no LLM below) ══════
      ▼                         │                      │
              FAST LANE (<1s/bar)│                      │
  ┌──────────────────────────────┴──────────────────────┴──────────┐
  │  DECISION BRAIN (L2, deterministic) → SENSES via registry       │
  │  SENSES (L1 pure fns) · CALCULATOR (cost/EV/risk, 1 lot)         │
  └──────────────────────────────┬──────────────────────────────────┘
                                 ▼ orders → SIM HARNESS gates live changes
```

---

## 6. Concrete interfaces (design sketch)

```python
# --- Tool: a capability the agent can invoke; harness executes + gates -------
@dataclass(frozen=True)
class Tool:
    name: str
    description: str                       # prescriptive: WHEN to call it
    input_schema: dict                     # JSON schema
    permission: Literal["read", "propose", "action"]
    reversible: bool
    fn: Callable[[dict], dict]             # returns structured result

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def schemas(self, permission: str | None = None) -> list[dict]: ...
    def invoke(self, name: str, args: dict, *, allow: set[str],
               confirm_token: str | None = None) -> dict:
        """Enforces permission gating. 'action' requires a confirm_token;
        'propose' writes a sim-gated proposal, never a live change."""

# Senses are wrapped as read-only tools — same fn the fast lane calls.
def sense_tool(sense) -> Tool: ...        # permission="read", reversible=True


# --- Trace store: the agent's memory + observability ------------------------
@dataclass
class DecisionTrace:
    trace_id: str
    ts: datetime
    kind: Literal["bar", "entry", "exit", "regime_change", "risk", "eod"]
    senses: dict                           # all SenseVerdicts at this moment
    decision: str                          # TRADE/WAIT/SKIP (+ side)
    outcome: dict | None                   # filled on exit (pnl, mfe, tag)

class TraceStore:
    def append(self, trace: DecisionTrace) -> None: ...
    def query(self, *, kind=None, since=None, limit=None) -> list[DecisionTrace]: ...
    def recent_trades(self, n: int) -> list[DecisionTrace]: ...


# --- Reasoning triggers + agent runtime (slow lane only) --------------------
@dataclass(frozen=True)
class ReasoningTrigger:
    name: str                              # pre_open | post_entry | post_exit | ...
    budget_s: float
    can_affect_live: bool                  # almost always False

@dataclass
class ReasoningResult:
    narrative: str
    tags: list[str]                        # e.g. ["loss:direction_miss"]
    proposals: list[dict]                  # sim-gated; never auto-applied
    confidence: float

class AgentRuntime:
    def __init__(self, model, tools: ToolRegistry, traces: TraceStore, memory): ...
    def on_trigger(self, trigger: ReasoningTrigger, context: dict) -> ReasoningResult:
        """Run the tool-call loop in the slow lane, bounded by budget_s.
        Enqueued by the event dispatcher AFTER the fast lane has acted, so it
        can never delay a bar or an order."""
```

The event dispatcher maps a *curated subset* of the engine's decision events to `ReasoningTrigger`s and enqueues a slow-lane task. `post_entry`/`post_exit` are enqueued after the order/close is processed — structurally off the hot path.

---

## 7. Evolution path (grow, don't rebuild)

| Phase | Runs | LLM shape | New pieces |
|---|---|---|---|
| **A (now)** | pre_open posture | single-shot ✅ built | — |
| **B** | post_exit autopsy + eod narrative | single call + **read-only** trace/sense tools | TraceStore (durable), sense tool wrappers |
| **C** | eod audit agent | multi-step loop, read + **propose-only** (sim-gated) | ToolRegistry, AgentRuntime, proposal→sim path |
| **D** (heavy gate) | regime_change / risk watchers | event-triggered, off per-bar, proposes posture | trigger dispatcher for intra-day events |

Every phase reuses the same four seams — single call becomes a loop; read-only tools gain propose-only tools. No re-architecture.

---

## 8. What would break it (do not do)

- **LLM in the per-bar path.** Kills latency + determinism. The wall is sacred.
- **Reasoning on the 1-minute clock.** Reason on *events*, not the metronome — otherwise cost/latency explode and the signal drowns.
- **`post_entry`/`post_exit` that can delay or veto the trade.** They reflect *after* the act; they annotate and learn, never gate the fast lane.
- **Any un-gated live-write tool.** The agent *proposes*; the sim + a human/hard-gate *approve*. Cost-of-error stays recoverable.
- **Building the agent before the deterministic core makes money.** The handover is emphatic: the LLM/agent is a convenience, never a dependency. Build the seams; build the agent when hand-reading traces is the bottleneck.

---

## 9. Mapping to the codebase

| Seam | Lands in |
|---|---|
| Senses + registry | `strategy_app/senses/` (new package, per impl plan) |
| Trace store | `contracts_app/decision_events.py` envelope, made durable |
| Tool catalog + agent runtime | new thin layer in the slow lane (`strategy_app/brain/agent/`) |
| Model layer | `genai_module` (separate repo; sidecar service or vendored — TBD) |
| Sim gate | `ops/sim/` (existing) |
| pre_open posture | `strategy_app/brain/providers/llm_stub.py` (`LLMContextProvider`, built) |

---

## 10. One-paragraph summary

The engine is a **trader's mind**: deterministic **perception (senses)**, **reflex (decision brain)**, and **calculation** run on the fast 1-minute clock below a hard latency wall; **memory (trace store)** and **thinking (LLM agent)** run in the slow lane, woken only by **events that deserve a second thought** — pre-open, just after a trade is taken, when it closes (especially a loss), on a regime shift, on a risk breach, and at end of day. The agent reflects on what was done, never decides the next bar; it reads traces (memory) and the same senses the fast lane uses (via a shared tool registry), and it can only *propose* changes that the sim and a human approve. Build the four seams now — shared sense registry, durable trace store, typed/gated tool catalog, provider-agnostic model layer (Groq + Gemini active, Cohere/AI21 deferred) — and the single morning call grows into a full oversight agent with zero re-architecture.
