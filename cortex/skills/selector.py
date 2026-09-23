def select_skills(names: list[str], requested: list[str]) -> list[str]:
    available = set(names)
    return [name for name in requested if name in available]
