# UI Architecture

This repo has one primary UI surface: Quiet Operator at `/app`. It has three modes: Live, Replay, and Eval.

## Surfaces

- `market_data_dashboard/static/webapp/`: Quiet Operator. This owns live monitoring, historical replay, strategy evaluation, feature intelligence, and operator controls such as halt/resume.
- `strategy_eval_ui/`: deprecated redirect fallback. Do not add new features here.

## Shared Boundaries

- Shared runtime control state belongs in runtime artifacts, not in either UI. The operator halt sentinel is resolved through `strategy_app.engines.runtime_artifacts.resolve_runtime_artifact_paths()`.
- Shared backend contracts should be exposed as stable API routes. UI modules should call those routes directly instead of reaching into each other's files.
- Shared visual language should live in `tokens.css` and reusable helpers in `components.jsx`.

## Routing Rules

- Operator routes should stay in `market_data_dashboard/operator_routes.py` and related dashboard route modules.
- Research/evaluation data routes should stay under `/api/strategy/evaluation/*` or `/api/trading/*` model catalog endpoints.
- Retired routes must not remain in catalog actions, README endpoint lists, or frontend links.

## Change Rules

- Add a feature to the UI where the user would naturally perform that workflow.
- Prefer removing or collapsing rarely used sections before adding new panels.
- `strategy_eval_ui` should remain redirect-only until archived.
- Operator safety controls must round-trip to backend state on page load and after every mutation.
