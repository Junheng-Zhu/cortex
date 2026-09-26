# Async Tool Runtime v1

## Policies and ordered waves

Every tool declares a `ConcurrencyPolicy`. `SERIAL` is the safe default and is a
barrier. Only explicitly read-only tools use `PARALLEL_SAFE`; `ShellTool` remains
serial because its commands may mutate a shared local or Docker workspace.

`ToolExecutor.aexecute_many()` partitions calls into ordered waves. Consecutive
parallel-safe calls run in an `asyncio.TaskGroup`, bounded by a batch semaphore.
A serial call waits for the preceding wave and completes before the following
wave starts. There is deliberately no dependency graph or speculative planner.
Results occupy their original input slots, so completion order cannot change
provider `call_id` ordering.

## Structured cancellation and state ownership

Workers return `ToolResult` values and never modify `AgentState`. `AgentLoop.aact`
commits observations and provider outputs from the main coroutine in original
action order. Normal failures are values, not TaskGroup exceptions, so one failed
call does not cancel valid siblings. Batch reflection uses the priority
`ABORT > REPLAN > CONTINUE`.

Cancellation is not merely cancellation of a waiter. Process-supervised tools
terminate and join their multiprocessing child. Local Bash runs in a process
group which is killed and reaped on timeout or cancellation. Docker cancellation
force-discards the session container, terminating its active exec; a later call
will lazily create a clean container. `CancelledError` is always re-raised.

## Compatibility and observability

The synchronous `execute()` and `AgentLoop.run()` paths are unchanged.
`aexecute()`, `aexecute_many()`, `astep()`, `aact()`, and `arun()` provide the
minimal async path without converting the LLM client or streaming stack.
Each agent batch records its batch id, wave ids and policies, configured limit,
wave/batch durations, and observed peak concurrency. The executor also exposes
the latest metrics for tests and benchmarks.
