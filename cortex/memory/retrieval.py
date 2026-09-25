from .models import MemoryRecord


def lexical_terms(query: str) -> set[str]:
    terms = {term.strip(".,!?;:\"'()[]{}") for term in query.casefold().split() if term.strip(".,!?;:\"'()[]{}")}
    cjk_runs = []
    current = ""
    for character in query.casefold():
        if "\u3400" <= character <= "\u9fff":
            current += character
        elif current:
            cjk_runs.append(current)
            current = ""
    if current:
        cjk_runs.append(current)
    for run in cjk_runs:
        terms.update(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    return terms


def retrieve(records: list[MemoryRecord], query: str, limit: int = 5) -> list[MemoryRecord]:
    terms = lexical_terms(query)
    scored = [(sum(term in record.content.casefold() for term in terms), record.importance, record) for record in records]
    return [record for score, _, record in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True) if score > 0][:limit]
