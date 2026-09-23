from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Task:
    objective: str
    task_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: dict[str, Any] = field(default_factory=dict)
