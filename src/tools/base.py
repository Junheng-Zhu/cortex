from abc import ABC, abstractmethod
from typing import Any
from .permission import Permission


class Tool(ABC):
    name: str
    description: str
    input_model: object
    permission: Permission

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        raise NotImplementedError

    # 这里的execute(self,xxx),xxx要怎么换成pydantic进行验证
