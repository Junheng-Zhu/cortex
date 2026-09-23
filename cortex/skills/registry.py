class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, object] = {}

    def register(self, name: str, skill: object) -> None:
        self._skills[name] = skill

    def get(self, name: str) -> object:
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)
