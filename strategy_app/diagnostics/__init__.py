"""Runtime diagnostics — feature-health and model-integrity reporting."""
from .feature_health import feature_health, format_report
from .model_integrity import run_integrity_check, format_report as format_integrity_report

__all__ = ["feature_health", "format_report", "run_integrity_check", "format_integrity_report"]
