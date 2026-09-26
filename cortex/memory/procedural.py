from typing import Protocol


class ProceduralMemory(Protocol):
    """Architecture seam for future SKILL.md-backed procedures.

    Memory Architecture v2 intentionally does not implement skill discovery or
    execution here.
    """

    def procedures_for(self, query: str) -> list[str]: ...
