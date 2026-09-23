from dataclasses import dataclass
from enum import Enum


class ContextMode(str, Enum):
    SERVER_MANAGED = "SERVER_MANAGED"
    CLIENT_MANAGED = "CLIENT_MANAGED"


@dataclass(frozen=True)
class ProviderCapabilities:
    context_mode: ContextMode = ContextMode.SERVER_MANAGED
