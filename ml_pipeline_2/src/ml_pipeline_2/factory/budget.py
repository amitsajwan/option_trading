from __future__ import annotations

from threading import Lock


class ResourceBudget:
    def __init__(self, total_cores: int, total_memory_gb: float, *, memory_headroom_ratio: float = 0.15) -> None:
        self.total_cores = int(total_cores)
        self.total_memory_gb = float(total_memory_gb)
        self.memory_headroom_ratio = float(memory_headroom_ratio)
        self._allocations: dict[str, tuple[int, float]] = {}
        self._lock = Lock()

    @property
    def effective_memory_gb(self) -> float:
        return self.total_memory_gb * max(0.0, 1.0 - self.memory_headroom_ratio)

    def _used(self) -> tuple[int, float]:
        used_cores = sum(item[0] for item in self._allocations.values())
        used_memory = sum(item[1] for item in self._allocations.values())
        return used_cores, used_memory

    def can_afford(self, cores: int, memory_gb: float) -> bool:
        with self._lock:
            used_cores, used_memory = self._used()
            return used_cores + int(cores) <= self.total_cores and used_memory + float(memory_gb) <= self.effective_memory_gb

    def acquire(self, lane_id: str, cores: int, memory_gb: float) -> None:
        with self._lock:
            used_cores, used_memory = self._used()
            next_cores = used_cores + int(cores)
            next_memory = used_memory + float(memory_gb)
            if next_cores > self.total_cores or next_memory > self.effective_memory_gb:
                raise ValueError(f"insufficient budget for lane {lane_id}")
            self._allocations[str(lane_id)] = (int(cores), float(memory_gb))

    def release(self, lane_id: str) -> None:
        with self._lock:
            self._allocations.pop(str(lane_id), None)

    def available(self) -> tuple[int, float]:
        with self._lock:
            used_cores, used_memory = self._used()
            return max(0, self.total_cores - used_cores), max(0.0, self.effective_memory_gb - used_memory)


__all__ = ["ResourceBudget"]
