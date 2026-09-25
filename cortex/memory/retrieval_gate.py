from dataclasses import dataclass

from .scopes import MemoryScope


@dataclass(frozen=True)
class RetrievalPlan:
    semantic_scopes: tuple[MemoryScope, ...] = ()
    search_episodic: bool = False


class MemoryRetrievalGate:
    """Deterministic v1 router: retrieval is opt-in, not request-global."""

    history_markers = (
        "last time", "previous", "previously", "before", "continue",
        "earlier", "上次", "之前", "继续", "刚才", "以前",
    )
    user_markers = (
        "remember", "preference", "prefer", "my ", "i am", "我喜欢",
        "我的", "记得", "偏好",
    )
    project_markers = (
        "project", "repository", "repo", "constraint", "decision",
        "database", "framework", "language", "项目", "仓库", "约束",
        "决定", "技术栈", "数据库", "框架",
    )

    def plan(self, query: str) -> RetrievalPlan:
        text = query.casefold()
        episodic = any(marker in text for marker in self.history_markers)
        scopes: list[MemoryScope] = []
        if episodic or any(marker in text for marker in self.user_markers):
            scopes.append(MemoryScope.USER)
        if episodic or any(marker in text for marker in self.project_markers):
            scopes.append(MemoryScope.PROJECT)
        return RetrievalPlan(tuple(scopes), episodic)
