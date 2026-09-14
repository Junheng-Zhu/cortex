from dataclasses import dataclass, field

from typing import Any

@dataclass
class ContextBuilder:

    tools: dict = field(default_factory=dict)