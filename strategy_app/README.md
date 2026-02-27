# strategy_app

Layer-4 strategy consumer runtime for snapshot events.

## Purpose

- Subscribes to snapshot events from Layer-3 topic.
- Calls the `StrategyEngine` contract on every snapshot.
- Handles session lifecycle hooks:
  - `on_session_start(date)`
  - `evaluate(snapshot)`
  - `on_session_end(date)`

## Contract

Implemented in `strategy_app/contracts.py`:

- `StrategyEngine`
- `TradeSignal`

## Run

From repo root:

```powershell
python -m strategy_app.main --engine deterministic
```

Use historical replay topic:

```powershell
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1:historical
```

Consume only first 100 events:

```powershell
python -m strategy_app.main --engine deterministic --max-events 100
```

## Notes

- Current deterministic engine is a no-trade stub (returns `None`).
- This keeps L3 -> L4 wiring stable while trigger logic is added.

## Container/Compose

- Build image:

```powershell
docker build -f strategy_app/Dockerfile -t strategy_app:local .
```

- Health command:

```powershell
python -m strategy_app.health
```

- Compose command uses:

```powershell
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1
```
