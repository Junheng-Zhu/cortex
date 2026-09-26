"""Execution backend protocol."""

from abc import ABC, abstractmethod

from .models import ExecutionRequest, ExecutionResult


class ExecutionBackend(ABC):
    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute one request and return its captured result."""

    def close(self) -> None:
        """Release session resources. Stateless backends need do nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()
