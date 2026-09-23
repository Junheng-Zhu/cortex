from .models import MemoryRecord


def retrieve(records: list[MemoryRecord], query: str, limit: int = 5) -> list[MemoryRecord]:
    terms = query.casefold().split()
    ranked = sorted(records, key=lambda record: sum(term in record.content.casefold() for term in terms), reverse=True)
    return ranked[:limit]
