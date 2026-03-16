"""Manual Kite authentication helper for refreshing credentials.json.

This is intentionally separate from the live runtime. The runtime remains
fail-closed and does not launch an interactive browser flow on its own.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from .env_settings import credentials_path_candidates
from .kite_client import create_kite_client


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_dotenv_candidates() -> None:
    if (os.environ.get("KITE_SKIP_DOTENV_LOAD") or "").strip().lower() in {"1", "true", "yes"}:
        return
    candidates = [
        Path.cwd() / ".env",
        _repo_root() / ".env",
        _repo_root() / "ingestion_app" / ".env",
    ]
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        try:
            load_dotenv(dotenv_path=path, override=False)
        except Exception:
            pass


_load_dotenv_candidates()


def _credentials_output_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).resolve()

    configured = str(os.environ.get("KITE_CREDENTIALS_PATH") or "").strip()
    if configured:
        path = Path(configured)
        # Container path is not useful when running the helper on the host.
        if not path.is_absolute() or not str(path).startswith("/app/"):
            return path.resolve()

    repo_candidate = _repo_root() / "ingestion_app" / "credentials.json"
    if repo_candidate.exists():
        return repo_candidate
    return repo_candidate


def _read_existing_credentials() -> dict[str, Any] | None:
    for path in [_credentials_output_path(), *credentials_path_candidates()]:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
    return None


class CredentialsValidator:
    @staticmethod
    def is_token_valid(credentials: dict[str, Any]) -> bool:
        access_token = str(credentials.get("access_token") or "").strip()
        if not access_token:
            return False
        login_time_str = str((credentials.get("data") or {}).get("login_time") or "").strip()
        if not login_time_str:
            return False
        try:
            login_time = datetime.fromisoformat(login_time_str)
        except (TypeError, ValueError):
            return False
        return datetime.now(login_time.tzinfo) - login_time < timedelta(hours=23)

    @staticmethod
    def verify_credentials(api_key: str, access_token: str) -> bool:
        try:
            kite = create_kite_client(api_key=api_key, access_token=access_token)
            profile = kite.profile()
            print(f"Credentials verified for user: {profile.get('user_id')}")
            return True
        except Exception as exc:
            print(f"Credential verification failed: {exc}")
            return False


def _serialize_data(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _start_http_server(port: int) -> http.server.HTTPServer:
    class RequestHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            query_string = self.path.split("?", 1)[1] if "?" in self.path else ""
            params: dict[str, str] = {}
            if query_string:
                for param in query_string.split("&"):
                    if "=" not in param:
                        continue
                    key, value = param.split("=", 1)
                    params[key] = value

            request_token = params.get("request_token")
            if request_token:
                self.server.request_token = request_token  # type: ignore[attr-defined]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                body = (
                    "<html><body><h1>Login Successful</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                    "</body></html>"
                )
                self.wfile.write(body.encode("utf-8"))
                print(f"[SUCCESS] Request token received: {request_token[:20]}...")
                return

            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h1>Failed to capture request_token</h1><p>Path: {self.path}</p></body></html>".encode(
                    "utf-8"
                )
            )
            print(f"[WARNING] Failed to capture request_token from path: {self.path}")

    server = http.server.HTTPServer(("127.0.0.1", int(port)), RequestHandler)
    server.request_token = None  # type: ignore[attr-defined]

    def _run_server() -> None:
        server.serve_forever()

    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()
    time.sleep(0.5)
    return server


def login_via_browser(
    *,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    verify_mode: bool = False,
    force_mode: bool = False,
    timeout: int = 120,
    callback_port: int = 5000,
    credentials_path: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], int]:
    _load_dotenv_candidates()

    existing = _read_existing_credentials() or {}
    api_key = api_key or str(os.environ.get("KITE_API_KEY") or "") or str(existing.get("api_key") or "")
    api_secret = api_secret or str(os.environ.get("KITE_API_SECRET") or "") or str(existing.get("api_secret") or "")
    if not api_key or not api_secret:
        print("Error: set KITE_API_KEY and KITE_API_SECRET or keep them in credentials.json/.env")
        return None, 1

    validator = CredentialsValidator()
    output_path = _credentials_output_path(credentials_path)

    if output_path.exists() and not force_mode:
        try:
            existing_creds = json.loads(output_path.read_text(encoding="utf-8-sig"))
            if verify_mode:
                print("Verifying existing credentials...")
                if validator.verify_credentials(api_key, str(existing_creds.get("access_token") or "")):
                    print("[OK] Existing credentials are valid")
                    return existing_creds, 0
                print("Existing credentials are invalid or expired")
                return None, 1
            if validator.is_token_valid(existing_creds):
                print("Valid credentials found locally")
                if validator.verify_credentials(api_key, str(existing_creds.get("access_token") or "")):
                    print("Use --force to generate new credentials anyway")
                    return existing_creds, 0
        except Exception as exc:
            print(f"Error reading existing credentials: {exc}")

    kite = create_kite_client(api_key=api_key)
    server = _start_http_server(callback_port)
    redirect_uri = f"http://127.0.0.1:{callback_port}/login"

    print(f"[INFO] Redirect URI: {redirect_uri}")
    print("[INFO] Ensure this exact URI is configured in your Kite app settings.")

    try:
        login_url = kite.login_url()
    except Exception as exc:
        print(f"Error generating login URL: {exc}")
        server.shutdown()
        return None, 1

    print("Open this URL and finish login:")
    print(login_url)
    try:
        webbrowser.open(login_url)
    except Exception:
        pass

    deadline = time.time() + max(10, int(timeout))
    while getattr(server, "request_token", None) is None:
        if time.time() >= deadline:
            print(f"Timeout: no response received within {timeout} seconds")
            server.shutdown()
            return None, 1
        time.sleep(0.5)

    request_token = str(server.request_token)  # type: ignore[attr-defined]
    server.shutdown()

    data: dict[str, Any]
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        print(f"Failed to generate session: {exc}")
        return None, 1

    kite.set_access_token(data["access_token"])
    data = _serialize_data(data)
    cred = {
        "api_key": api_key,
        "api_secret": api_secret,
        "access_token": data.get("access_token"),
        "user_id": data.get("user_id"),
        "data": data,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        backup_path = output_path.with_suffix(output_path.suffix + ".backup")
        backup_path.write_text(output_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(f"Backed up existing credentials to {backup_path}")

    output_path.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    print(f"Credentials saved to: {output_path}")

    if validator.verify_credentials(api_key, str(cred.get("access_token") or "")):
        print("Credentials verified. The live ingestion wrapper can pick them up now.")
        return cred, 0
    print("Credentials saved, but verification failed.")
    return cred, 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manual Kite browser-login helper that refreshes credentials.json.")
    parser.add_argument("--verify", action="store_true", help="Verify existing credentials and exit")
    parser.add_argument("--force", action="store_true", help="Force a new browser login even if local token looks valid")
    parser.add_argument("--timeout", type=int, default=120, help="Seconds to wait for the browser redirect")
    parser.add_argument("--callback-port", type=int, default=int(os.environ.get("KITE_AUTH_CALLBACK_PORT", "5000")))
    parser.add_argument("--credentials-path", default=None, help="Output path for refreshed credentials.json")
    args = parser.parse_args(argv)

    _, code = login_via_browser(
        verify_mode=bool(args.verify),
        force_mode=bool(args.force),
        timeout=int(args.timeout),
        callback_port=int(args.callback_port),
        credentials_path=args.credentials_path,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
