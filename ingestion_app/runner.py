#!/usr/bin/env python3
"""Ingestion runner (supervisor) that spawns market-data server, collectors, and historical replayer as separate processes.

Usage examples:
  python -m ingestion_app.runner --mode live --start-collectors
  python -m ingestion_app.runner --mode historical --historical-source zerodha --historical-from 2026-01-07
"""
import argparse
import os
import sys
import time

from .runtime import (
    _sanitize_for_console,
    build_collector_env,
    check_zerodha_credentials,
    kite_startup_preflight,
    start_process,
    wait_for_historical_ready,
    wait_for_http,
)

PYTHON = sys.executable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['live', 'historical', 'mock'], default='live')
    parser.add_argument('--start-collectors', action='store_true')
    parser.add_argument('--prompt-login', action='store_true', help='If credentials missing, attempt interactive login (useful for Dev machines)')
    parser.add_argument('--historical-source', type=str, default=None)
    parser.add_argument('--historical-speed', type=float, default=1.0)
    parser.add_argument('--historical-from', type=str, default=None)
    parser.add_argument('--historical-ticks', action='store_true')
    parser.add_argument('--no-server', action='store_true', help='Do not start API server (useful for testing)')
    parser.add_argument('--skip-validation', action='store_true', help='Skip dependency validation (not recommended)')
    parser.add_argument('--diagnostics', action='store_true', help='Run diagnostics and exit')
    args = parser.parse_args()

    # If we're about to run a Zerodha historical replay, propagate the intent into
    # env so dependency validation can be strict (fail fast).
    if args.mode == 'historical' and args.historical_source:
        os.environ['HISTORICAL_SOURCE'] = args.historical_source

    # Run diagnostics if requested
    if args.diagnostics:
        print('[INFO] Running market_data diagnostics...')
        from .diagnostics import print_diagnostics
        print_diagnostics()
        return

    procs = []

    try:
        # Validate dependencies before starting anything
        if not args.skip_validation:
            print('[INFO] Validating market_data dependencies...')
            from .dependency_validator import validate_dependencies

            if not validate_dependencies():
                print('[ERROR] Dependency validation failed. Use --skip-validation to bypass (not recommended)')
                print('[INFO] Run with --diagnostics for detailed troubleshooting information')
                raise SystemExit(1)
            print('[OK] All dependencies validated successfully')
        else:
            print('[WARN] Skipping dependency validation (--skip-validation used)')
        # Start the market-data API server (always)
        if not args.no_server:
            server_cmd = [PYTHON, '-m', 'market_data.api_service']
            server_proc = start_process('Market Data API', server_cmd)
            procs.append(('Market Data API', server_proc))

            # Wait for health (increased timeout for dependency validation during startup)
            ok = wait_for_http('http://127.0.0.1:8004/health', timeout=60)
            if not ok:
                print('[ERROR] Market Data API failed to become healthy within 60 seconds')
                print('[INFO] This may indicate a dependency issue - check logs above')
                print('[INFO] Run with --diagnostics for detailed troubleshooting')
                raise SystemExit(1)
            print(_sanitize_for_console('   [OK] Market Data API healthy'))

        if args.mode == 'live':
            if args.start_collectors:
                # Before starting collectors, validate Zerodha credentials unless using mock provider
                use_mock = os.getenv('USE_MOCK_KITE', '0') in ('1', 'true', 'yes')
                ok, msg = check_zerodha_credentials()

                # If not ok and prompt-login requested, try interactive login
                if not ok and args.prompt_login and not use_mock:
                    print('[INFO] Prompt-login requested; attempting interactive login...')
                    try:
                        from market_data.tools.kite_auth_service import KiteAuthService
                        svc = KiteAuthService()
                        success = svc.trigger_interactive_login(timeout=300)
                        if success:
                            print(_sanitize_for_console('   [OK] Interactive login succeeded; re-checking credentials...'))
                            ok, msg = check_zerodha_credentials()
                        else:
                            print('[WARN] Interactive login did not complete or failed')
                    except Exception as e:
                        print(f'[WARN] Interactive login failed: {e}')

                if not ok and not use_mock:
                    print('[ERROR] Cannot start collectors: Zerodha credentials not valid or missing')
                    print('   💡 Fix options:')
                    print('      - Ensure KITE_API_KEY and KITE_ACCESS_TOKEN are set in environment')
                    print('      - Run: python -m market_data.tools.kite_auth to generate credentials interactively')
                    print('      - Or set USE_MOCK_KITE=1 to use the mock provider for testing')
                    if msg:
                        print(f'[INFO] Details: {msg}')
                    raise SystemExit(1)

                # Real auth preflight: reject stale/expired tokens before launching collectors.
                if not use_mock:
                    pre_ok, pre_reason, pre_detail = kite_startup_preflight(attempts=2, base_delay_sec=1.0)
                    if not pre_ok and pre_reason == 'credential' and args.prompt_login:
                        print('[WARN] Live preflight detected invalid credentials; attempting automatic re-login...')
                        try:
                            from market_data.tools.kite_auth_service import KiteAuthService
                            svc = KiteAuthService()
                            if svc.trigger_interactive_login(timeout=300):
                                pre_ok, pre_reason, pre_detail = kite_startup_preflight(attempts=2, base_delay_sec=1.0)
                        except Exception as e:
                            pre_ok = False
                            pre_reason = 'credential'
                            pre_detail = f'Interactive login failed: {e}'

                    if not pre_ok:
                        if pre_reason == 'network':
                            print('[ERROR] Live preflight failed: Network/TLS to api.kite.trade is unstable')
                        elif pre_reason == 'credential':
                            print('[ERROR] Live preflight failed: Invalid/expired api_key or access_token')
                        else:
                            print('[ERROR] Live preflight failed due to an unknown issue')
                        print(f'[INFO] Detail: {pre_detail}')
                        raise SystemExit(1)

                    print(_sanitize_for_console('   [OK] Live Kite preflight passed'))

                # Pass Zerodha credentials to collectors
                collector_env = build_collector_env(os.environ.copy())

                websocket_cmd = [PYTHON, '-m', 'ingestion_app.collectors.websocket_tick_collector']
                ltp_cmd = [PYTHON, '-m', 'ingestion_app.collectors.ltp_collector']
                depth_cmd = [PYTHON, '-m', 'ingestion_app.collectors.depth_collector']
                websocket_proc = start_process('WebSocket Tick Collector', websocket_cmd, env=collector_env)
                ltp_proc = start_process('LTP Processor', ltp_cmd, env=collector_env)
                depth_proc = start_process('Depth Collector', depth_cmd, env=collector_env)
                procs.extend([('WebSocket Tick Collector', websocket_proc), ('LTP Processor', ltp_proc), ('Depth Collector', depth_proc)])

                # Wait a little for collectors to seed data into Redis
                print('   [*] Waiting briefly for collectors to seed data (5s)...')
                time.sleep(5)

        elif args.mode in ('historical', 'mock'):
            # Start historical runner as a separate process.
            env = os.environ.copy()
            if args.historical_source:
                env['HISTORICAL_SOURCE'] = args.historical_source
            elif args.mode == 'mock':
                env['HISTORICAL_SOURCE'] = 'synthetic'
            env['HISTORICAL_SPEED'] = str(args.historical_speed)
            if args.historical_from:
                env['HISTORICAL_FROM'] = args.historical_from
            if args.historical_ticks:
                env['HISTORICAL_TICKS'] = '1'

            requested_source = (env.get('HISTORICAL_SOURCE') or '').strip().lower()
            if requested_source == 'zerodha':
                # Fail fast: we want real Zerodha data only.
                try:
                    import kiteconnect  # noqa: F401
                except Exception as e:
                    print('[ERROR] Zerodha historical replay requested but kiteconnect is not installed')
                    print('   💡 Install into the active venv: pip install kiteconnect')
                    print(f'   [INFO] Import error: {e}')
                    raise SystemExit(1)

                ok, msg = check_zerodha_credentials(prompt_login=True)  # Enable automatic auth for Zerodha historical
                if not ok:
                    print('[ERROR] Zerodha historical replay requested but credentials/token are missing or invalid')
                    print('   💡 Fix options:')
                    print('      - Set KITE_API_KEY and KITE_ACCESS_TOKEN in environment')
                    print('      - Or run: python -m market_data.tools.kite_auth (interactive login)')
                    if msg:
                        print(f'[INFO] Details: {msg}')
                    raise SystemExit(1)

                # Dedicated preflight: classify early failure as network vs credential.
                pre_ok, pre_reason, pre_detail = kite_startup_preflight(attempts=2, base_delay_sec=1.0)
                if not pre_ok and pre_reason == 'credential' and args.prompt_login:
                    print('[WARN] Kite preflight detected invalid credentials; attempting automatic re-login...')
                    try:
                        from market_data.tools.kite_auth_service import KiteAuthService

                        svc = KiteAuthService()
                        if svc.trigger_interactive_login(timeout=300):
                            pre_ok, pre_reason, pre_detail = kite_startup_preflight(attempts=2, base_delay_sec=1.0)
                    except Exception as e:
                        pre_ok = False
                        pre_reason = 'credential'
                        pre_detail = f'Interactive login failed: {e}'

                if not pre_ok:
                    if pre_reason == 'network':
                        print('[ERROR] Kite preflight failed: Network/TLS to api.kite.trade is unstable')
                        print(f'[INFO] Detail: {pre_detail}')
                    elif pre_reason == 'credential':
                        print('[ERROR] Kite preflight failed: Invalid/expired api_key or access_token')
                        print(f'[INFO] Detail: {pre_detail}')
                    else:
                        print('[ERROR] Kite preflight failed due to an unknown issue')
                        print(f'[INFO] Detail: {pre_detail}')
                    raise SystemExit(1)

                print(_sanitize_for_console('   [OK] Kite preflight passed'))

            hist_cmd = [PYTHON, '-m', 'ingestion_app.runner_historical']
            replay_name = 'Mock Replay' if args.mode == 'mock' else 'Historical Replay'
            hist_proc = start_process(replay_name, hist_cmd, env=env)
            procs.append((replay_name, hist_proc))

            print('   [*] Waiting for historical data to appear (polling Redis + API)...')
            # Prefer explicit Redis readiness key set by the historical runner
            try:
                import redis
                redis_host = os.getenv('REDIS_HOST', 'localhost')
                redis_port = int(os.getenv('REDIS_PORT', '6379'))
                redis_client = redis.Redis(host=redis_host, port=redis_port, db=0, decode_responses=True)

                # Prefer explicit Redis readiness key set by the historical runner
                if wait_for_historical_ready(redis_client, timeout=60, poll_interval=1):
                    print(_sanitize_for_console('   [OK] Historical data ready (Redis key set)'))
                else:
                    # Fallback to API tick endpoint
                    if wait_for_http('http://127.0.0.1:8004/api/v1/market/tick/BANKNIFTY', timeout=60):
                        print(_sanitize_for_console('   [OK] Historical data available via Market Data API'))
                    else:
                        if requested_source == 'zerodha':
                            print('[ERROR] Zerodha historical replay did not produce data within timeout (fail-fast)')
                            raise SystemExit(1)
                        print('[WARN] Historical data not found within timeout')
            except Exception:
                # If Redis/requests not available, fallback to HTTP check
                ok = wait_for_http('http://127.0.0.1:8004/api/v1/market/tick/BANKNIFTY', timeout=60)
                if not ok:
                    if requested_source == 'zerodha':
                        print('[ERROR] Zerodha historical replay did not produce data within timeout (fail-fast)')
                        raise SystemExit(1)
                    print('[WARN] Historical data not found at Market Data API within timeout')
                else:
                    print('   ✅ Historical data available via Market Data API')

        # Keep supervisor running until Ctrl+C
        print('\nSupervisor running. Press Ctrl+C to stop all spawned processes.')
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print('\n🛑 Stopping supervised processes...')
        for name, proc in procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
                print(_sanitize_for_console(f'   [OK] Stopped {name}'))
            except Exception:
                try:
                    proc.kill()
                    print(_sanitize_for_console(f'   [OK] Killed {name}'))
                except Exception:
                    pass
        print(_sanitize_for_console('[OK] Supervisor exited'))


if __name__ == '__main__':
    main()
