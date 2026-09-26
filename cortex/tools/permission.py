from enum import Enum


class Permission(Enum):
    READ = "READ"
    WRITE = "WRITE"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"
    SKILL_SEARCH = "SKILL_SEARCH"
    SKILL_LOAD = "SKILL_LOAD"
    SKILL_READ_RESOURCE = "SKILL_READ_RESOURCE"
