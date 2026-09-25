from .models import MemoryRecord
from .policy import MemoryCandidate, MemoryDecision, MemoryWritePolicy
from .retrieval import retrieve
from .store import MemoryStore


class MemoryManager:
    def __init__(self, store: MemoryStore, policy: MemoryWritePolicy | None = None) -> None:
        self.store = store
        self.policy = policy or MemoryWritePolicy()

    def remember(self, content: str, scope_type: str, scope_id: str = "default", *, kind: str = "other", source: str = "explicit", session_id: str | None = None, run_id: str | None = None, importance: float = 0.5, metadata: dict | None = None, temporary_chat: bool = False) -> MemoryRecord | None:
        candidate = MemoryCandidate(content=content, kind=kind, source=source)
        if temporary_chat or self.policy.evaluate(candidate) is MemoryDecision.DENY:
            return None
        record = MemoryRecord(content=content, scope_type=scope_type, scope_id=scope_id, kind=kind, source=source, session_id=session_id, run_id=run_id, importance=importance, metadata=metadata or {})
        if not self.store.put(record):
            existing = next((r for r in self.store.list(scope_type, scope_id) if r.content == content), None)
            return existing
        return record

    def recall(self, query: str, scope_type: str, scope_id: str | None = None, limit: int = 5) -> list[MemoryRecord]:
        return retrieve(self.store.list(scope_type, scope_id), query, limit)
