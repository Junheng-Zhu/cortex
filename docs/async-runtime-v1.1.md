# Async Runtime v1.1: correctness and recovery

## Wave commit boundary

The agent now treats each ordered wave as a transaction boundary:

1. execute one parallel-safe wave or serial barrier;
2. normalize and commit every result in original call order;
3. persist observations and provider outputs;
4. reflect with `ABORT > REPLAN > CONTINUE` precedence;
5. schedule the next wave only after `CONTINUE`.

Consequently, a failed read wave cannot be followed by an already-queued
side-effecting serial tool. Failures remain values, so all tasks already admitted
to the same parallel wave are allowed to produce a result.

## Durability and budgets

Checkpoint schema v3 persists normalized observations in addition to completed
actions and pending provider outputs. An action status and its observation are
committed without an async suspension between them, then checkpointed. Restore
therefore omits completed actions from the executable queue while retaining the
result needed to continue the provider conversation.

The agent slices a batch to its remaining step budget before scheduling. Calls
beyond that boundary never enter the executor and are discarded when the run
terminates at the budget limit.

## Runtime ownership and cancellation

Concurrency primitives belong to `ToolExecutor`, not to an individual batch.
All concurrent runs share one semaphore, and all serial tools share one executor
lock. This bounds aggregate work and prevents concurrent access to a shared
serial backend such as a Docker workspace.

Docker discard is identity-checked. A cancelled execution may remove only the
container instance it started with; a stale SDK thread from an old generation
cannot remove a replacement container. Existing process, Local Bash process
group, and Docker cancellation cleanup remains in force.
