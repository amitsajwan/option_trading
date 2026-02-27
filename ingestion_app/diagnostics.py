"""Diagnostics and logging utilities for market_data component.

Provides comprehensive error reporting, troubleshooting guides, and system diagnostics.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

# IST timezone for consistent timestamps
IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticIssue:
    """Represents a diagnostic issue with troubleshooting steps."""
    severity: str  # "critical", "error", "warning", "info"
    component: str
    issue: str
    description: str
    troubleshooting_steps: List[str]
    related_config: Optional[Dict[str, Any]] = None


class MarketDataDiagnostics:
    """Comprehensive diagnostics system for market_data component."""

    def __init__(self):
        self.issues: List[DiagnosticIssue] = []

    def diagnose_system(self) -> List[DiagnosticIssue]:
        """Run comprehensive system diagnostics."""
        self.issues = []

        # Check environment
        self._check_environment()

        # Check dependencies
        self._check_dependencies()

        # Check configuration
        self._check_configuration()

        # Check services
        self._check_services()

        # Check data flow
        self._check_data_flow()

        return self.issues

    def _check_environment(self):
        """Check environment-related issues."""
        # Python version
        version = sys.version_info
        if version < (3, 8):
            self.issues.append(DiagnosticIssue(
                severity="critical",
                component="environment",
                issue="Unsupported Python version",
                description=f"Python {version.major}.{version.minor} detected, minimum 3.8 required",
                troubleshooting_steps=[
                    "Upgrade Python to version 3.8 or higher",
                    "Download from python.org",
                    "Ensure the new version is in PATH"
                ]
            ))

        # Required environment variables
        required_env_vars = ['REDIS_HOST', 'REDIS_PORT']
        for var in required_env_vars:
            if not os.getenv(var):
                self.issues.append(DiagnosticIssue(
                    severity="error",
                    component="environment",
                    issue=f"Missing environment variable: {var}",
                    description=f"Required environment variable {var} is not set",
                    troubleshooting_steps=[
                        f"Set {var} in .env file or environment",
                        f"Check market_data/.env file",
                        f"Verify .env file is being loaded"
                    ]
                ))

    def _check_dependencies(self):
        """Check Python dependencies."""
        critical_deps = [
            ('fastapi', 'FastAPI web framework'),
            ('uvicorn', 'ASGI server'),
            ('redis', 'Redis client'),
            ('pydantic', 'Data validation'),
        ]

        for module, desc in critical_deps:
            try:
                __import__(module)
            except ImportError:
                self.issues.append(DiagnosticIssue(
                    severity="critical",
                    component="dependencies",
                    issue=f"Missing critical dependency: {module}",
                    description=f"Required module {module} ({desc}) is not installed",
                    troubleshooting_steps=[
                        f"Install {module}: pip install {module}",
                        "Check requirements.txt",
                        "Ensure virtual environment is activated",
                        f"Verify {module} is available in current Python environment"
                    ]
                ))

        # Optional dependencies
        optional_deps = [
            ('kiteconnect', 'Zerodha API client'),
            ('pandas', 'Data analysis'),
        ]

        for module, desc in optional_deps:
            try:
                __import__(module)
            except ImportError:
                self.issues.append(DiagnosticIssue(
                    severity="warning",
                    component="dependencies",
                    issue=f"Missing optional dependency: {module}",
                    description=f"Optional module {module} ({desc}) is not available",
                    troubleshooting_steps=[
                        f"Install {module}: pip install {module}",
                        "Some features may not work without this module"
                    ]
                ))

    def _check_configuration(self):
        """Check configuration files and settings."""
        config_files = [
            ('market_data/.env', 'Market data environment configuration'),
            ('market_data/src/market_data/providers/schemas.py', 'Provider schema definitions'),
        ]

        for file_path, desc in config_files:
            if not os.path.exists(file_path):
                self.issues.append(DiagnosticIssue(
                    severity="error",
                    component="configuration",
                    issue=f"Missing configuration file: {file_path}",
                    description=f"Required configuration file {file_path} ({desc}) is missing",
                    troubleshooting_steps=[
                        f"Create {file_path}",
                        f"Check if file was accidentally deleted",
                        f"Restore from backup or version control"
                    ]
                ))

        # Check Redis configuration
        try:
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))

            import redis
            client = redis.Redis(host=redis_host, port=redis_port, socket_timeout=5)
            client.ping()

        except Exception as e:
            self.issues.append(DiagnosticIssue(
                severity="critical",
                component="configuration",
                issue="Redis connection failed",
                description=f"Cannot connect to Redis at {redis_host}:{redis_port}: {e}",
                troubleshooting_steps=[
                    "Ensure Redis server is running",
                    f"Check Redis host: {redis_host}",
                    f"Check Redis port: {redis_port}",
                    "Verify Redis service is started",
                    "Check firewall settings"
                ],
                related_config={
                    "redis_host": redis_host,
                    "redis_port": redis_port,
                    "error": str(e)
                }
            ))

    def _check_services(self):
        """Check service availability and health."""
        # Check if we can import and create services
        try:
            from .api import build_store
            store = build_store()
            if not store:
                self.issues.append(DiagnosticIssue(
                    severity="error",
                    component="services",
                    issue="Market data store failed to initialize",
                    description="The market data store could not be created",
                    troubleshooting_steps=[
                        "Check Redis connection",
                        "Verify market_data store configuration",
                        "Check Python path includes market_data module"
                    ]
                ))
        except Exception as e:
            self.issues.append(DiagnosticIssue(
                severity="error",
                component="services",
                issue="Market data store import failed",
                description=f"Cannot import market data store: {e}",
                troubleshooting_steps=[
                    "Check Python path",
                    "Verify market_data module installation",
                    "Check for import errors in store.py"
                ]
            ))

        # Check provider
        try:
            from .providers.factory import get_provider
            provider = get_provider()
            if not provider:
                self.issues.append(DiagnosticIssue(
                    severity="warning",
                    component="services",
                    issue="No data provider available",
                    description="No market data provider could be initialized",
                    troubleshooting_steps=[
                        "Check Zerodha credentials",
                        "Verify KITE_API_KEY and KITE_ACCESS_TOKEN",
                        "Check credentials.json file",
                        "Will fall back to historical data mode"
                    ]
                ))
        except Exception as e:
            self.issues.append(DiagnosticIssue(
                severity="error",
                component="services",
                issue="Provider factory failed",
                description=f"Cannot create data provider: {e}",
                troubleshooting_steps=[
                    "Check provider imports",
                    "Verify schema definitions",
                    "Check Zerodha credentials"
                ]
            ))

    def _check_data_flow(self):
        """Check data flow and availability."""
        try:
            import redis
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

            # Check for recent data
            instrument = "BANKNIFTY"
            price_key = f"price:{instrument}:last_price"
            timestamp_key = f"price:{instrument}:latest_ts"

            price = client.get(price_key)
            timestamp = client.get(timestamp_key)

            if not price:
                self.issues.append(DiagnosticIssue(
                    severity="warning",
                    component="data_flow",
                    issue="No price data in Redis",
                    description=f"No price data found for {instrument} in Redis",
                    troubleshooting_steps=[
                        "Start market data collectors",
                        "Check if LTP collector is running",
                        "Verify Zerodha API access",
                        "Check Redis key patterns"
                    ]
                ))
            elif not timestamp:
                self.issues.append(DiagnosticIssue(
                    severity="warning",
                    component="data_flow",
                    issue="Missing timestamp data",
                    description=f"Price data exists but timestamp missing for {instrument}",
                    troubleshooting_steps=[
                        "Check collector timestamp writing",
                        "Verify Redis key consistency"
                    ]
                ))
            else:
                # Check data freshness
                try:
                    from datetime import datetime
                    data_time = datetime.fromisoformat(timestamp)
                    now = datetime.now(IST)
                    age_minutes = (now - data_time).total_seconds() / 60

                    if age_minutes > 60:  # Data older than 1 hour
                        self.issues.append(DiagnosticIssue(
                            severity="warning",
                            component="data_flow",
                            issue="Stale market data",
                            description=".1f",
                            troubleshooting_steps=[
                                "Check if collectors are running",
                                "Verify market hours",
                                "Check for collector errors",
                                "Restart data collection"
                            ],
                            related_config={
                                "last_update": timestamp,
                                "age_minutes": age_minutes,
                                "current_price": price
                            }
                        ))
                except Exception as e:
                    self.issues.append(DiagnosticIssue(
                        severity="warning",
                        component="data_flow",
                        issue="Cannot parse data timestamp",
                        description=f"Invalid timestamp format: {timestamp}",
                        troubleshooting_steps=[
                            "Check collector timestamp format",
                            "Verify ISO format compliance"
                        ]
                    ))

        except Exception as e:
            self.issues.append(DiagnosticIssue(
                severity="error",
                component="data_flow",
                issue="Cannot check Redis data",
                description=f"Failed to check data flow: {e}",
                troubleshooting_steps=[
                    "Verify Redis connection",
                    "Check Redis service status"
                ]
            ))

    def generate_report(self) -> Dict[str, Any]:
        """Generate a comprehensive diagnostic report."""
        issues = self.diagnose_system()

        # Categorize issues
        critical = [i for i in issues if i.severity == "critical"]
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        info = [i for i in issues if i.severity == "info"]

        report = {
            "timestamp": datetime.now(IST).isoformat(),
            "summary": {
                "total_issues": len(issues),
                "critical": len(critical),
                "errors": len(errors),
                "warnings": len(warnings),
                "info": len(info),
                "system_status": "healthy" if not critical and not errors else "unhealthy"
            },
            "issues": {
                "critical": [self._issue_to_dict(i) for i in critical],
                "errors": [self._issue_to_dict(i) for i in errors],
                "warnings": [self._issue_to_dict(i) for i in warnings],
                "info": [self._issue_to_dict(i) for i in info]
            },
            "environment": {
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "platform": sys.platform,
                "working_directory": os.getcwd(),
                "python_path": sys.path[:3]  # First 3 paths
            }
        }

        return report

    def _issue_to_dict(self, issue: DiagnosticIssue) -> Dict[str, Any]:
        """Convert DiagnosticIssue to dictionary."""
        return {
            "severity": issue.severity,
            "component": issue.component,
            "issue": issue.issue,
            "description": issue.description,
            "troubleshooting_steps": issue.troubleshooting_steps,
            "related_config": issue.related_config
        }

    def print_report(self):
        """Print a formatted diagnostic report."""
        report = self.generate_report()

        print("\n" + "="*80)
        print("MARKET DATA DIAGNOSTIC REPORT")
        print("="*80)
        print(f"Generated: {report['timestamp']}")
        print(f"Status: {report['summary']['system_status'].upper()}")

        summary = report['summary']
        print(f"\nIssues Summary:")
        print(f"  Critical: {summary['critical']}")
        print(f"  Errors: {summary['errors']}")
        print(f"  Warnings: {summary['warnings']}")
        print(f"  Info: {summary['info']}")

        # Print issues by severity
        for severity in ["critical", "errors", "warnings", "info"]:
            issues = report['issues'][severity]
            if issues:
                print(f"\n{severity.upper()} ISSUES:")
                for issue in issues:
                    print(f"  • {issue['component']}: {issue['issue']}")
                    print(f"    {issue['description']}")
                    if issue['troubleshooting_steps']:
                        print("    Troubleshooting:")
                        for step in issue['troubleshooting_steps']:
                            print(f"      - {step}")

        print("\n" + "="*80)


def run_diagnostics():
    """Run diagnostics and return report."""
    diagnostics = MarketDataDiagnostics()
    return diagnostics.generate_report()


def print_diagnostics():
    """Run diagnostics and print report."""
    diagnostics = MarketDataDiagnostics()
    diagnostics.print_report()


if __name__ == "__main__":
    print_diagnostics()