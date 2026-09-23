from enum import Enum


class MemoryScope(str, Enum):
    SESSION = "session"
    USER = "user"
    PROJECT = "project"
