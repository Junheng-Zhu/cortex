from abc import ABC, abstractmethod
from typing import Any
from .permission import permission


class Tool(ABC):
    name: str
    description: str
    input_model: object
    permission: permission

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        raise NotImplementedError

    # 这里的execute(self,xxx),xxx要怎么换成pydantic进行验证
