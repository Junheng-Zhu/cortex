from .models import MemoryRecord


def retrieve(records: list[MemoryRecord], query: str, limit: int = 5) -> list[MemoryRecord]:
    terms = {term.strip(".,!?;:\"'()[]{}") for term in query.casefold().split() if term.strip(".,!?;:\"'()[]{}")}
    scored = [(sum(term in record.content.casefold() for term in terms), record.importance, record) for record in records]
    return [record for score, _, record in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True) if score > 0][:limit]
