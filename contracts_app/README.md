# contracts_app

Shared contracts for inter-process communication.

## Ownership
- Owns event/topic contracts only.
- No business logic, no I/O, no process orchestration.

## Modules
- `topics.py`: topic resolvers (for example `snapshot_topic()`, `strategy_vote_topic()`).
- `events.py`: event envelope builders/parsers (`market_snapshot`, `strategy_vote`, `trade_signal`, `strategy_position` v1.0).
- `config.py`: shared process-app config contracts (for example `redis_connection_kwargs()`).
  - Loads `.env` from current working directory, repo root, and app-local env files (without overriding existing shell vars).
  - Supports `for_pubsub=True` to disable read timeout for long-lived Redis subscribers.
- `options_math.py`: shared Black-Scholes pricing and Greeks helpers for dashboard/analytics use.
- `market_session.py`: shared IST market-session gate helpers:
  - `is_trading_day_ist(...)`
  - `is_market_open_ist(...)`
  - `seconds_until_next_open_ist(...)`
  - `load_holidays(...)`
- `process_inspect.py`: lightweight process discovery helpers used by component health commands.
- `process_control.py`: process stop helpers used by component stop commands.

## Time Convention
- Trading/session time is IST (`Asia/Kolkata`) in payload fields.
- Envelope metadata should explicitly include timezone when needed.

## Dependency Rule
- Keep dependencies minimal and pure-Python.
- Other apps may import `contracts_app`; `contracts_app` should not import app packages.
