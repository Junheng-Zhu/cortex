from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    name: str
    description: str
    input_model: object
    permission: str
    #这个类型用字符串吗，还有能不能限定填入的内容

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        raise NotImplementedError

    # 这里的execute(self,xxx),xxx要怎么换成pydantic进行验证
