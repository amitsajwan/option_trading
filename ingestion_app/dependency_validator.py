"""Comprehensive dependency validator for market_data component.

This module validates all dependencies and configurations required for market_data to function properly,
preventing silent failures and synthetic data fallbacks.
"""
import os
import sys
import redis
import json
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum


class DependencyStatus(Enum):
    """Status of a dependency check."""
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    MISSING = "missing"


@dataclass
class DependencyCheck:
    """Result of a dependency check."""
    name: str
    status: DependencyStatus
    message: str
    details: Optional[Dict[str, Any]] = None
    critical: bool = True  # If True, failure prevents startup


class MarketDataDependencyValidator:
    """Validates all dependencies required for market_data component."""

    def __init__(self):
        self.checks: List[DependencyCheck] = []

    def validate_all(self) -> Tuple[bool, List[DependencyCheck]]:
        """Run all dependency validations.

        Returns:
            Tuple of (all_passed: bool, checks: List[DependencyCheck])
        """
        self.checks = []

        # Core dependencies
        self._check_python_version()
        self._check_required_modules()
        self._check_redis_connection()
        self._check_configuration_files()

        # Provider-specific checks
        self._check_provider_dependencies()

        # Service-specific checks
        self._check_technical_indicators_service()
        self._check_options_chain_service()

        # Data validation
        self._check_data_store_access()

        all_passed = all(
            check.status != DependencyStatus.ERROR or not check.critical
            for check in self.checks
        )

        return all_passed, self.checks

    def _add_check(self, name: str, status: DependencyStatus, message: str,
                   details: Optional[Dict] = None, critical: bool = True):
        """Add a dependency check result."""
        self.checks.append(DependencyCheck(name, status, message, details, critical))

    def _check_python_version(self):
        """Check Python version compatibility."""
        version = sys.version_info
        min_version = (3, 8)
        if version >= min_version:
            self._add_check(
                "Python Version",
                DependencyStatus.OK,
                f"Python {version.major}.{version.minor}.{version.micro} is compatible"
            )
        else:
            self._add_check(
                "Python Version",
                DependencyStatus.ERROR,
                f"Python {version.major}.{version.minor} required, found {version.major}.{version.minor}.{version.micro}",
                critical=True
            )

    def _check_required_modules(self):
        """Check that all required modules can be imported."""
        historical_source = (os.getenv("HISTORICAL_SOURCE") or "").strip().lower()
        require_kiteconnect = historical_source == "zerodha"

        required_modules = [
            ('fastapi', 'FastAPI web framework'),
            ('uvicorn', 'ASGI server'),
            ('redis', 'Redis client'),
            ('pydantic', 'Data validation'),
        ]

        optional_modules = [
            # kiteconnect becomes REQUIRED when HISTORICAL_SOURCE=zerodha
            ('kiteconnect', 'Zerodha API client'),
            ('pandas', 'Data analysis'),
            ('numpy', 'Numerical computing'),
        ]

        for module, description in required_modules:
            try:
                __import__(module)
                self._add_check(
                    f"Module: {module}",
                    DependencyStatus.OK,
                    f"{description} available"
                )
            except ImportError:
                self._add_check(
                    f"Module: {module}",
                    DependencyStatus.ERROR,
                    f"Required {description} not available",
                    critical=True
                )

        for module, description in optional_modules:
            try:
                __import__(module)
                self._add_check(
                    f"Module: {module}",
                    DependencyStatus.OK,
                    f"{description} available"
                )
            except ImportError:
                if module == 'kiteconnect' and require_kiteconnect:
                    self._add_check(
                        f"Module: {module}",
                        DependencyStatus.ERROR,
                        f"Required {description} not available (HISTORICAL_SOURCE=zerodha)",
                        critical=True
                    )
                else:
                    self._add_check(
                        f"Module: {module}",
                        DependencyStatus.WARNING,
                        f"Optional {description} not available",
                        critical=False
                    )

    def _check_redis_connection(self):
        """Check Redis connection and basic functionality."""
        try:
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            redis_db = int(os.getenv("REDIS_DB", "0"))

            client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2
            )

            # Test connection
            client.ping()

            # Test basic operations
            test_key = "market_data:health_check:test"
            client.setex(test_key, 10, "test_value")
            value = client.get(test_key)
            client.delete(test_key)

            if value == "test_value":
                self._add_check(
                    "Redis Connection",
                    DependencyStatus.OK,
                    f"Redis connected at {redis_host}:{redis_port}, basic operations working"
                )
            else:
                self._add_check(
                    "Redis Connection",
                    DependencyStatus.ERROR,
                    "Redis connected but basic operations failed",
                    critical=True
                )

        except Exception as e:
            # Check if we're in a development environment where Redis might not be running
            dev_mode = os.getenv('DEVELOPMENT_MODE', '').lower() in ('1', 'true', 'yes')
            if dev_mode:
                self._add_check(
                    "Redis Connection",
                    DependencyStatus.WARNING,
                    f"Redis connection failed: {e} (development mode - continuing anyway)",
                    critical=False
                )
            else:
                self._add_check(
                    "Redis Connection",
                    DependencyStatus.ERROR,
                    f"Redis connection failed: {e}",
                    critical=True
                )

    def _check_configuration_files(self):
        """Check for required configuration files."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        src_root = os.path.dirname(current_dir)
        market_data_candidates = [
            os.path.join(src_root, "market_data"),
            os.path.join(src_root, "market_data", "src", "market_data"),
        ]
        market_data_pkg_dir = next(
            (
                p for p in market_data_candidates
                if os.path.exists(os.path.join(p, "providers", "schemas.py"))
            ),
            next((p for p in market_data_candidates if os.path.isdir(p)), market_data_candidates[-1]),
        )

        env_candidates = [
            os.path.join(src_root, ".env"),
            os.path.join(src_root, "market_data", ".env"),
            os.path.join(src_root, "market_data", "src", ".env"),
        ]
        env_path = next((p for p in env_candidates if os.path.exists(p)), env_candidates[0])
        if os.path.exists(env_path):
            self._add_check(
                "Config: .env",
                DependencyStatus.OK,
                "Market data environment configuration found"
            )
        else:
            self._add_check(
                "Config: .env",
                DependencyStatus.WARNING,
                "Market data environment configuration not found (using root-level config)",
                critical=False
            )

        schema_path = os.path.join(market_data_pkg_dir, 'providers', 'schemas.py')
        if os.path.exists(schema_path):
            self._add_check(
                "Config: schemas.py",
                DependencyStatus.OK,
                "Provider schema definitions found"
            )
        else:
            self._add_check(
                "Config: schemas.py",
                DependencyStatus.ERROR,
                "Required Provider schema definitions missing",
                critical=True
            )

    def _check_provider_dependencies(self):
        """Check provider-specific dependencies."""
        # Check if provider factory works
        try:
            from market_data.providers.factory import get_provider
            provider = get_provider()

            if provider is not None:
                provider_name = type(provider).__name__
                self._add_check(
                    "Provider Factory",
                    DependencyStatus.OK,
                    f"Provider {provider_name} initialized successfully"
                )

                # Test provider functionality
                if hasattr(provider, 'quote'):
                    self._add_check(
                        "Provider Quote API",
                        DependencyStatus.OK,
                        "Provider supports quote API"
                    )
                else:
                    self._add_check(
                        "Provider Quote API",
                        DependencyStatus.WARNING,
                        "Provider does not support quote API",
                        critical=False
                    )
            else:
                self._add_check(
                    "Provider Factory",
                    DependencyStatus.WARNING,
                    "No provider available (will use historical data)",
                    critical=False
                )

        except Exception as e:
            self._add_check(
                "Provider Factory",
                DependencyStatus.ERROR,
                f"Provider initialization failed: {e}",
                critical=False  # Not critical for historical mode
            )

    def _check_technical_indicators_service(self):
        """Check technical indicators service."""
        try:
            from market_data.technical_indicators_service import TechnicalIndicatorsService

            if TechnicalIndicatorsService is None:
                self._add_check(
                    "Technical Indicators",
                    DependencyStatus.WARNING,
                    "Technical indicators service not available",
                    critical=False
                )
                return

            # Try to create an instance
            redis_client = self._get_redis_client()
            if redis_client:
                service = TechnicalIndicatorsService(redis_client=redis_client)
                self._add_check(
                    "Technical Indicators",
                    DependencyStatus.OK,
                    "Technical indicators service initialized successfully"
                )
            else:
                self._add_check(
                    "Technical Indicators",
                    DependencyStatus.WARNING,
                    "Cannot test technical indicators without Redis",
                    critical=False
                )

        except Exception as e:
            self._add_check(
                "Technical Indicators",
                DependencyStatus.ERROR,
                f"Technical indicators service failed: {e}",
                critical=False
            )

    def _check_options_chain_service(self):
        """Check options chain service."""
        try:
            from market_data.adapters.zerodha_options_chain import ZerodhaOptionsChainAdapter

            # Try to create adapter (will fail if no credentials)
            adapter = ZerodhaOptionsChainAdapter()
            self._add_check(
                "Options Chain",
                DependencyStatus.OK,
                "Options chain adapter initialized"
            )

        except Exception as e:
            self._add_check(
                "Options Chain",
                DependencyStatus.WARNING,
                f"Options chain adapter failed: {e}",
                critical=False
            )

    def _check_data_store_access(self):
        """Check data store access and basic functionality."""
        try:
            from market_data.api import build_store
            store = build_store()

            if store:
                self._add_check(
                    "Data Store",
                    DependencyStatus.OK,
                    "Market data store initialized successfully"
                )
            else:
                self._add_check(
                    "Data Store",
                    DependencyStatus.ERROR,
                    "Market data store initialization failed",
                    critical=True
                )

        except Exception as e:
            self._add_check(
                "Data Store",
                DependencyStatus.ERROR,
                f"Data store access failed: {e}",
                critical=True
            )

    def _get_redis_client(self) -> Optional[redis.Redis]:
        """Get Redis client for testing."""
        try:
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            redis_db = int(os.getenv("REDIS_DB", "0"))

            return redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2
            )
        except:
            return None

    def print_report(self):
        """Print a formatted validation report."""
        print("\n" + "="*80)
        print("MARKET DATA DEPENDENCY VALIDATION REPORT")
        print("="*80)

        errors = [c for c in self.checks if c.status == DependencyStatus.ERROR]
        warnings = [c for c in self.checks if c.status == DependencyStatus.WARNING]
        ok = [c for c in self.checks if c.status == DependencyStatus.OK]

        print(f"\n[PASS] PASSED: {len(ok)} checks")
        for check in ok:
            print(f"   [OK] {check.name}: {check.message}")

        if warnings:
            print(f"\n[WARN] WARNINGS: {len(warnings)} checks")
            for check in warnings:
                print(f"   ! {check.name}: {check.message}")

        if errors:
            print(f"\n[ERROR] ERRORS: {len(errors)} checks")
            for check in errors:
                print(f"   [FAIL] {check.name}: {check.message}")

        critical_errors = [c for c in errors if c.critical]
        if critical_errors:
            print(f"\n[CRITICAL] ERRORS: {len(critical_errors)} - Cannot start market_data")
        else:
            print(f"\n[READY] All critical dependencies satisfied")

        print("="*80)


def validate_dependencies() -> bool:
    """Convenience function to validate all dependencies.

    Returns:
        True if all critical dependencies are satisfied
    """
    validator = MarketDataDependencyValidator()
    all_passed, checks = validator.validate_all()

    # Print report
    validator.print_report()

    return all_passed


if __name__ == "__main__":
    success = validate_dependencies()
    sys.exit(0 if success else 1)
