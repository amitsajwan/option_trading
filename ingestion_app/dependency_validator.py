"""Dependency validator for ingestion_app."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Tuple

import redis

from .runtime import check_zerodha_credentials, kite_startup_preflight


class DependencyStatus(Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class DependencyCheck:
    name: str
    status: DependencyStatus
    message: str
    critical: bool = True
    details: Optional[dict[str, Any]] = None


class IngestionDependencyValidator:
    def __init__(self) -> None:
        self.checks: List[DependencyCheck] = []

    def _add(self, name: str, status: DependencyStatus, message: str, *, critical: bool = True, details: Optional[dict[str, Any]] = None) -> None:
        self.checks.append(
            DependencyCheck(name=name, status=status, message=message, critical=critical, details=details)
        )

    def _check_python(self) -> None:
        ver = sys.version_info
        if ver >= (3, 10):
            self._add("Python Version", DependencyStatus.OK, f"Python {ver.major}.{ver.minor}.{ver.micro} is compatible")
        else:
            self._add("Python Version", DependencyStatus.ERROR, "Python 3.10+ required", critical=True)

    def _check_modules(self) -> None:
        required = [
            ("fastapi", "FastAPI"),
            ("uvicorn", "Uvicorn"),
            ("redis", "Redis client"),
            ("requests", "HTTP client"),
            ("kiteconnect", "KiteConnect client"),
        ]
        for module, desc in required:
            try:
                __import__(module)
                self._add(f"Module: {module}", DependencyStatus.OK, f"{desc} available")
            except Exception as exc:
                self._add(
                    f"Module: {module}",
                    DependencyStatus.ERROR,
                    f"{desc} missing: {exc}",
                    critical=True,
                )

    def _check_redis(self) -> None:
        host = str(os.getenv("REDIS_HOST") or "localhost")
        port = int(os.getenv("REDIS_PORT") or "6379")
        db = int(os.getenv("REDIS_DB") or "0")
        try:
            client = redis.Redis(
                host=host,
                port=port,
                db=db,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            client.ping()
            self._add("Redis Connection", DependencyStatus.OK, f"Redis reachable at {host}:{port}/{db}")
        except Exception as exc:
            self._add("Redis Connection", DependencyStatus.ERROR, f"Redis connection failed: {exc}", critical=True)

    def _check_credentials(self) -> None:
        ok, msg = check_zerodha_credentials(prompt_login=False)
        if not ok:
            self._add(
                "Kite Credentials",
                DependencyStatus.ERROR,
                f"Credentials unavailable/invalid: {msg or 'unknown'}",
                critical=True,
            )
            return
        pre_ok, reason, detail = kite_startup_preflight(attempts=1, base_delay_sec=0.5)
        if pre_ok:
            self._add("Kite Preflight", DependencyStatus.OK, "Kite profile check passed")
            return
        status = DependencyStatus.WARNING if reason == "network" else DependencyStatus.ERROR
        self._add(
            "Kite Preflight",
            status,
            f"{reason}: {detail}",
            critical=(status == DependencyStatus.ERROR),
        )

    def validate_all(self) -> Tuple[bool, List[DependencyCheck]]:
        self.checks = []
        self._check_python()
        self._check_modules()
        self._check_redis()
        self._check_credentials()
        passed = all((c.status != DependencyStatus.ERROR) or (not c.critical) for c in self.checks)
        return passed, self.checks

    def print_report(self) -> None:
        print("\n" + "=" * 72)
        print("INGESTION DEPENDENCY VALIDATION REPORT")
        print("=" * 72)
        for check in self.checks:
            prefix = {
                DependencyStatus.OK: "[OK]",
                DependencyStatus.WARNING: "[WARN]",
                DependencyStatus.ERROR: "[FAIL]",
            }[check.status]
            print(f"{prefix} {check.name}: {check.message}")
        critical_errors = [c for c in self.checks if c.status == DependencyStatus.ERROR and c.critical]
        if critical_errors:
            print(f"\n[CRITICAL] {len(critical_errors)} critical error(s)")
        else:
            print("\n[READY] Critical dependencies satisfied")
        print("=" * 72)


def validate_dependencies() -> bool:
    validator = IngestionDependencyValidator()
    ok, _ = validator.validate_all()
    validator.print_report()
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if validate_dependencies() else 1)
