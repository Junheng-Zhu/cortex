from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    name: str
    description: str
    input_model: object
    # 为什么schema要用字典

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        raise NotImplementedError
