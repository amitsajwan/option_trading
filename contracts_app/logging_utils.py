from __future__ import annotations

import logging
from typing import Optional

from .time_utils import format_log_time_ist


class ISTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        return format_log_time_ist(record.created, datefmt)


def configure_ist_logging(*, level: int = logging.INFO, fmt: str = "%(asctime)s %(levelname)s %(name)s - %(message)s") -> None:
    root = logging.getLogger()
    formatter = ISTFormatter(fmt=fmt)
    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(formatter)
        root.setLevel(level)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.handlers = [handler]
    root.setLevel(level)
