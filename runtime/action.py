from dataclasses import dataclass

from enum import Enum
from typing import Any



class ActionType(Enum):
    Think = "Think"
    Search = "Search"
    Read = "Read"
    Edit = "Edit"
    Run = "Run"
    Verify = "Verify"
    Respond = "Respond"

class ActionStatus:

    pass


@dataclass
class Action:

    id: str

    type: ActionType

    tool_name: str | None

    arguments: dict

    reasoning: str | None

    status: ActionStatus

    timestamp: float