"""Transport-independent execution lifecycle and failure classification."""
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from uuid import uuid4

from cortex.tools.base import SideEffectPolicy


class ExecutionState(str, Enum):
    CREATED = 'CREATED'
    QUEUED = 'QUEUED'
    CLAIMED = 'CLAIMED'
    RUNNING = 'RUNNING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    LOST = 'LOST'
    RECOVERING = 'RECOVERING'
    CANCELLED = 'CANCELLED'


TERMINAL = {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
TRANSITIONS = {
    ExecutionState.CREATED: {'QUEUED', 'RECOVERING', 'CANCELLED'},
    ExecutionState.QUEUED: {'CLAIMED', 'RECOVERING', 'FAILED', 'CANCELLED'},
    ExecutionState.CLAIMED: {'RUNNING', 'LOST', 'FAILED', 'CANCELLED'},
    ExecutionState.RUNNING: {'SUCCEEDED', 'FAILED', 'LOST', 'CANCELLED'},
    ExecutionState.LOST: {'RECOVERING', 'FAILED', 'CANCELLED'},
    ExecutionState.RECOVERING: {'QUEUED', 'SUCCEEDED', 'FAILED', 'LOST', 'CANCELLED'},
}


class FailureKind(str, Enum):
    WORKER_CRASH = 'WORKER_CRASH'
    LEASE_EXPIRED = 'LEASE_EXPIRED'
    TOOL_TIMEOUT = 'TOOL_TIMEOUT'
    TOOL_EXECUTION_ERROR = 'TOOL_EXECUTION_ERROR'
    TOOL_INFRASTRUCTURE = 'TOOL_INFRASTRUCTURE'
    INVALID_ARGUMENTS = 'INVALID_ARGUMENTS'
    PERMISSION_FAILURE = 'PERMISSION_FAILURE'
    POLICY_MISMATCH = 'POLICY_MISMATCH'
    RETRY_EXHAUSTED = 'RETRY_EXHAUSTED'
    SIDE_EFFECT_UNCERTAIN = 'SIDE_EFFECT_UNCERTAIN'
    RECOVERY_CONFLICT = 'RECOVERY_CONFLICT'
    RESULT_SERIALIZATION = 'RESULT_SERIALIZATION'
    CANCELLED = 'CANCELLED'


@dataclass(frozen=True)
class ExecutionError:
    kind: FailureKind
    message: str
    infrastructure: bool = False
    uncertain: bool = False


@dataclass(frozen=True)
class ExecutionLease:
    execution_id: str
    owner_worker_id: str
    attempt: int
    lease_expires_at: float
    token: int = 0


@dataclass
class ExecutionTask:
    tool_call_id: str
    tool_name: str
    arguments: dict
    idempotency_key: str
    side_effect_policy: SideEffectPolicy = SideEffectPolicy.NONE
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    state: ExecutionState = ExecutionState.CREATED
    owner_worker_id: str | None = None
    attempt: int = 0
    max_attempts: int = 3
    created_at: float = 0
    started_at: float | None = None
    finished_at: float | None = None
    lease_expires_at: float = 0
    result: dict | None = None
    error: dict | None = None
    outcome: dict | None = None
    recovery: dict = field(default_factory=dict)
    lost_from: str | None = None
    history: list[dict] = field(default_factory=list)
    queued_at: float = 0
    lease_token: int = 0
    workspace_root: str | None = None
    workspace_generation: list[int] | None = None

    def __post_init__(self):
        self.state = ExecutionState(self.state)
        self.side_effect_policy = SideEffectPolicy(self.side_effect_policy)
        if not all(isinstance(value, str) and value for value in
                   (self.execution_id, self.tool_call_id, self.tool_name, self.idempotency_key)):
            raise ValueError('execution identity fields must not be empty')
        if (not isinstance(self.arguments, dict) or not all(isinstance(k, str) for k in self.arguments) or
                type(self.max_attempts) is not int or self.max_attempts < 1 or
                type(self.attempt) is not int or self.attempt < 0):
            raise ValueError('invalid arguments or attempt limits')
        json.dumps(self.to_dict(), allow_nan=False)

    @property
    def terminal(self):
        return self.state in TERMINAL

    @property
    def lease(self):
        return ExecutionLease(self.execution_id, self.owner_worker_id, self.attempt, self.lease_expires_at, self.lease_token)

    def transition(self, state):
        state = ExecutionState(state)
        if state.value not in TRANSITIONS.get(self.state, set()):
            raise ValueError(f'illegal execution transition: {self.state.value} -> {state.value}')
        self.state = state

    def to_dict(self):
        return asdict(self)

    def fingerprint(self):
        values = [self.tool_name, self.arguments, self.side_effect_policy, self.max_attempts, self.workspace_root, self.workspace_generation]
        return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


class InfrastructureUnavailable(RuntimeError):
    """Submission/result durability is unknown; never report success implicitly."""


class OwnershipLost(RuntimeError):
    pass
