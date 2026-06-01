# Live Trading Implementation Guide
*How to go from paper trading to real execution. Written for engineers joining this project.*

---

## Prerequisites

Read first:
- `docs/ARCHITECTURE.md` — system overview
- `docs/SYSTEM_STATUS_2026-06-01.md` — current verified state
- `docs/RUNTIME_DECISION_FLOW.md` — decision trace anatomy

---

## The Single Gate Between Paper and Live

The system is functionally complete for strategy research. One thing is missing:

```
Current:
  TradeSignal emitted → JSONL + Redis → simulated P&L from next snapshot price

Required for live:
  TradeSignal emitted → Kite place_order() → fill confirmation → real P&L
```

Everything else — market data, snapshot building, regime detection, direction consensus, risk management, dashboard — is already production-grade and running against real data.

---

## Architecture: What to Build

### New service: `execution_app`

```
execution_app/
├── __main__.py          # Service entry point
├── adapter/
│   ├── base.py          # BrokerAdapter ABC
│   ├── kite.py          # KiteAdapter (production)
│   └── paper.py         # PaperAdapter (current simulation behaviour)
├── order_manager.py     # Place → poll → confirm fill
├── fill_tracker.py      # Maps order_id → position_id → real P&L
└── consumer.py          # Subscribes to trade_signal_topic
```

### BrokerAdapter interface

```python
# execution_app/adapter/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from strategy_app.contracts import TradeSignal, PositionContext

@dataclass
class OrderResult:
    order_id: str
    status: str          # placed | filled | rejected | cancelled
    fill_price: Optional[float]
    fill_qty: Optional[int]
    error: Optional[str]

class BrokerAdapter(ABC):
    @abstractmethod
    def place_entry(self, signal: TradeSignal) -> OrderResult: ...

    @abstractmethod
    def place_exit(self, signal: TradeSignal, position: PositionContext) -> OrderResult: ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...
```

### KiteAdapter implementation

```python
# execution_app/adapter/kite.py
from kiteconnect import KiteConnect
from contracts_app import get_redis_key
import redis

class KiteAdapter(BrokerAdapter):
    def __init__(self, api_key: str, access_token: str):
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

    def place_entry(self, signal: TradeSignal) -> OrderResult:
        # Build tradingsymbol from signal.direction, signal.strike, signal.expiry
        tradingsymbol = _build_nfo_symbol(
            underlying="BANKNIFTY",
            expiry=signal.expiry,
            strike=signal.strike,
            option_type=signal.direction,  # CE or PE
        )
        try:
            order_id = self.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NFO,
                tradingsymbol=tradingsymbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
                quantity=signal.max_lots * LOT_SIZE_BANKNIFTY,
                product=KiteConnect.PRODUCT_NRML,
                order_type=KiteConnect.ORDER_TYPE_MARKET,
                tag=signal.signal_id[:10],  # Kite tag for audit
            )
            return OrderResult(order_id=order_id, status="placed", fill_price=None, fill_qty=None, error=None)
        except Exception as e:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None, error=str(e))

    def get_order_status(self, order_id: str) -> OrderResult:
        orders = self.kite.orders()
        order = next((o for o in orders if o["order_id"] == order_id), None)
        if order is None:
            return OrderResult(order_id=order_id, status="unknown", fill_price=None, fill_qty=None, error="not found")
        if order["status"] == "COMPLETE":
            return OrderResult(
                order_id=order_id,
                status="filled",
                fill_price=float(order["average_price"]),
                fill_qty=int(order["filled_quantity"]),
                error=None,
            )
        return OrderResult(order_id=order_id, status=order["status"].lower(), fill_price=None, fill_qty=None, error=None)
```

### PaperAdapter (drop-in replacement for current simulation)

```python
# execution_app/adapter/paper.py
class PaperAdapter(BrokerAdapter):
    """Simulates fills at signal.entry_premium — identical to current behaviour."""

    def place_entry(self, signal: TradeSignal) -> OrderResult:
        return OrderResult(
            order_id=f"paper_{signal.signal_id}",
            status="filled",
            fill_price=signal.entry_premium,
            fill_qty=signal.max_lots * LOT_SIZE_BANKNIFTY,
            error=None,
        )
```

### Consumer wiring

```python
# execution_app/consumer.py
class ExecutionConsumer:
    def __init__(self, adapter: BrokerAdapter):
        self._adapter = adapter
        self._r = redis.Redis(...)

    def run(self):
        pubsub = self._r.pubsub()
        pubsub.subscribe(trade_signal_topic())
        for message in pubsub.listen():
            signal = parse_trade_signal_event(message["data"])
            if signal.signal_type == SignalType.ENTRY:
                result = self._adapter.place_entry(signal)
                self._emit_fill_event(signal, result)
            elif signal.signal_type == SignalType.EXIT:
                position = self._get_open_position(signal)
                result = self._adapter.place_exit(signal, position)
                self._emit_fill_event(signal, result)
```

### Switching paper ↔ live

```bash
# docker-compose.yml
execution_app:
  environment:
    EXECUTION_ADAPTER: ${EXECUTION_ADAPTER:-paper}   # paper | kite
    KITE_API_KEY: ${KITE_API_KEY}
    KITE_ACCESS_TOKEN: ${KITE_ACCESS_TOKEN}
```

No code change needed to switch modes.

---

## Exit Strategy Implementation

**This is the highest-impact improvement.** Today's trades had 1.71% average MFE but only 0.30% was captured.

### ExitPolicy interface

```python
# strategy_app/position/exit_policy.py
from abc import ABC, abstractmethod
from typing import Optional
from ..contracts import ExitReason, PositionContext
from ..market.snapshot_accessor import SnapshotAccessor

class ExitPolicy(ABC):
    @abstractmethod
    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        """Return ExitReason if position should be closed, else None."""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...
```

### PremiumTargetPolicy

```python
class PremiumTargetPolicy(ExitPolicy):
    """Exit when premium gains target_pct from entry. Locks profits."""

    def __init__(self, target_pct: float = 0.015):
        self._target = target_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.pnl_pct >= self._target:
            return ExitReason.TARGET_HIT
        return None

    @property
    def name(self) -> str:
        return f"premium_target_{self._target:.1%}"
```

### TrailingStopPolicy

```python
class TrailingStopPolicy(ExitPolicy):
    """Once MFE exceeds activation_mfe, trail by trail_pct.
    Protects profits while allowing continued gains.

    Example: activation_mfe=0.01, trail=0.005
      If MFE hits +1%, lock in at +0.5%.
      If MFE then hits +2%, lock in at +1.5%.
      Never moves the stop backwards.
    """

    def __init__(self, activation_mfe: float = 0.01, trail_pct: float = 0.005):
        self._activation = activation_mfe
        self._trail = trail_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.mfe_pct < self._activation:
            return None
        # Trail below the peak: if current pnl has fallen trail_pct from MFE, exit
        if position.pnl_pct < position.mfe_pct - self._trail:
            return ExitReason.TRAILING_STOP
        return None

    @property
    def name(self) -> str:
        return f"trail_act={self._activation:.1%}_trail={self._trail:.1%}"
```

### ThesisFailPolicy

```python
class ThesisFailPolicy(ExitPolicy):
    """Exit if the trade has never gone positive after min_bars.
    Catches 'wrong direction from bar 1' trades early.

    CE trades 3,4,5 today had MFE=0% — this would have cut them at bar 2.
    """

    def __init__(self, min_bars: int = 3, min_mfe_pct: float = 0.002):
        self._min_bars = min_bars
        self._min_mfe = min_mfe_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.bars_held >= self._min_bars and position.mfe_pct < self._min_mfe:
            return ExitReason.THESIS_FAIL
        return None

    @property
    def name(self) -> str:
        return f"thesis_fail_{self._min_bars}b"
```

### CompositeExitPolicy

```python
class CompositeExitPolicy(ExitPolicy):
    """Run all policies; first to trigger wins."""

    def __init__(self, policies: list[ExitPolicy]):
        self._policies = policies

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        for policy in self._policies:
            reason = policy.check(position, snap)
            if reason is not None:
                return reason
        return None

    @property
    def name(self) -> str:
        return "composite[" + ",".join(p.name for p in self._policies) + "]"
```

### Wiring to profile config

```python
# In DeterministicRuleEngine or MlPureEngine position management
DEFAULT_EXIT_STACK = CompositeExitPolicy([
    PremiumTargetPolicy(target_pct=0.015),       # 1.5% target
    TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005),
    ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002),
    TimestopPolicy(max_bars=15),                  # fallback
])
```

Environment overrides:
```bash
EXIT_PREMIUM_TARGET_PCT=0.015
EXIT_TRAILING_ACTIVATION_PCT=0.01
EXIT_TRAILING_TRAIL_PCT=0.005
EXIT_THESIS_FAIL_BARS=3
EXIT_TIMESTOP_BARS=15
```

---

## Entry Gate Hardening

**Problem:** Consensus bypass has `min_confidence=0.50` but entry gate threshold is `0.65`. This allows sub-threshold trades.

**Fix:**

```python
# strategy_app/engines/deterministic_rule_engine.py
# In _process_entry_consensus (line ~868)

# BEFORE:
if ml_vote.confidence < self._min_confidence:  # 0.50
    return None

# AFTER:
_bypass_min = float(os.getenv("CONSENSUS_BYPASS_MIN_CONFIDENCE", "0.65"))
if ml_vote.confidence < _bypass_min:
    return None
```

**Default:** `CONSENSUS_BYPASS_MIN_CONFIDENCE=0.65` aligns with entry gate.

---

## Shadow Mode (Recommended Pre-Live Step)

Run both paper and live simultaneously. Compare fills:

```bash
# Week 3 deployment
EXECUTION_ADAPTER=shadow    # new adapter: fires both paper + kite
SHADOW_MAX_LOTS=1           # limits real exposure during validation
```

Shadow mode:
- Places real Kite orders (1 lot)
- Simultaneously records paper fill
- Publishes both: `fill:real` and `fill:paper` to Redis
- Dashboard shows both P&Ls side-by-side
- Slippage = real_fill - paper_fill is measured daily

Only proceed to full live when slippage is understood and acceptable (typically < 0.1% of premium for BANKNIFTY options at market).

---

## Rollout Stages

| Stage | `EXECUTION_ADAPTER` | `size_multiplier` | Duration |
|---|---|---|---|
| Paper (current) | `paper` | 0.25 | Ongoing |
| Shadow | `shadow` | real=0.25, paper=1.0 | 1 week |
| Capped live | `kite` | 0.25 | 2 weeks |
| Full live | `kite` | 1.0 | After performance review |

Size multiplier is already in the rollout config. No architecture change needed to scale up.

---

## Real P&L Reconciliation

Once `execution_app` is running:

1. `fill_tracker.py` writes `FillEvent` to Redis `execution:fills:v1`
2. `strategy_persistence_app` extends to consume fills and update `strategy_positions` with real entry/exit price
3. Dashboard `read_strategy_current_state` switches to use fill price for P&L when available (falls back to simulated price)
4. Daily reconciliation: compare JSONL positions vs Kite ledger (kite.orders(), kite.positions())

---

## Pre-Live Checklist

```
[ ] execution_app deployed with KiteAdapter
[ ] Shadow mode run for 5+ trading days
[ ] Slippage < 0.15% of premium measured and documented
[ ] EXIT_PREMIUM_TARGET_PCT configured and backtested
[ ] EXIT_TRAILING_* configured
[ ] CONSENSUS_BYPASS_MIN_CONFIDENCE=0.65 set
[ ] Daily P&L alert configured (Telegram or email)
[ ] Halt notification wired (when strategy_app halts, alert fires)
[ ] Kite access token auto-refresh working
[ ] GCP VM uptime monitoring active
[ ] MongoDB replica or backup confirmed
[ ] run_id in position docs confirmed (bug fix deployed)
[ ] 1-week shadow results reviewed with trader
```
