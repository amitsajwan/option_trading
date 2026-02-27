from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class EventPublisher(ABC):
    @abstractmethod
    def publish(self, *, topic: str, payload: dict[str, Any]) -> None:
        """Publish event payload to topic."""
