from typing import Protocol

from .models import MemoryRecord


class MemoryStore(Protocol):
    def put(self, record: MemoryRecord) -> None: ...
    def list(self, scope: str) -> list[MemoryRecord]: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._records: list[MemoryRecord] = []

    def put(self, record: MemoryRecord) -> None:
        self._records.append(record)

    def list(self, scope: str) -> list[MemoryRecord]:
        return [record for record in self._records if record.scope == scope]
