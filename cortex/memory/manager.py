from .models import MemoryRecord
from .retrieval import retrieve
from .store import MemoryStore


class MemoryManager:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def remember(self, content: str, scope: str) -> MemoryRecord:
        record = MemoryRecord(content=content, scope=scope)
        self.store.put(record)
        return record

    def recall(self, query: str, scope: str, limit: int = 5) -> list[MemoryRecord]:
        return retrieve(self.store.list(scope), query, limit)
