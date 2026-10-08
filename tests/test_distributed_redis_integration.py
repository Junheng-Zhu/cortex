"""Real Redis and spawn-based independent OS processes; never an in-memory adapter."""
import asyncio
import gc
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest
import redis

from cortex.distributed.client import DistributedExecutionClient
from cortex.distributed.factory import close_recovery
from cortex.distributed.models import ExecutionState, ExecutionTask, FailureKind, InfrastructureUnavailable, OwnershipLost
from cortex.distributed.redis_adapter import connect, RedisExecutionQueue, RedisExecutionStore, RedisWorkerRegistry
from cortex.distributed.recovery import RecoveryController, WorkspaceRecoveryEvidence, failure
from cortex.tools.base import SideEffectPolicy
from distributed_support import worker_process, recovery_process, acquire_process, make_probe_executor

pytestmark = pytest.mark.redis
SPAWN = multiprocessing.get_context('spawn')


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result: return result
        time.sleep(.03)
    raise AssertionError('Condition did not become true before timeout')


@pytest.fixture
def cluster():
    url = os.getenv('CORTEX_TEST_REDIS_URL', 'redis://localhost:6379/15')
    client = connect(url)
    try:
        client.ping()
    except redis.RedisError as error:
        client.close()
        if os.getenv('CORTEX_TEST_REDIS_URL'):
            pytest.fail(f'Configured Redis unavailable: {type(error).__name__}')
        pytest.skip('Real Redis unavailable; set CORTEX_TEST_REDIS_URL (see distributed runtime docs)')
    namespace = 'cortex-test-' + uuid4().hex
    queue, store, registry = RedisExecutionQueue(client, namespace), RedisExecutionStore(client, namespace), RedisWorkerRegistry(client, namespace)
    workers = []
    def start(config=None, hook_stage=None, marker=None, recover=True):
        stop = SPAWN.Event()
        process = SPAWN.Process(target=worker_process, args=(url, namespace, config or {}, stop, hook_stage, str(marker) if marker else None, recover))
        process.start(); process._cortex_stop = stop; workers.append((process, stop))
        return process
    yield url, namespace, client, queue, store, registry, start
    for process, stop in workers:
        stop.set(); process.join(3)
        if process.is_alive(): process.kill(); process.join(3)
        del process._cortex_stop
        process.close()
    if workers:
        del process, stop
    workers.clear()
    # Release IPC fixtures at teardown, outside resource_tracker registration.
    gc.collect()
    keys = list(client.scan_iter(namespace + ':*'))
    if keys: client.delete(*keys)
    client.close()


def submit(cluster, tmp_path, name='probe', policy=SideEffectPolicy.NONE, max_attempts=3, config=None, **arguments):
    _, _, _, queue, store, _, _ = cluster
    root = config['workspace'] if config else None
    info = root.stat() if root else None
    task = ExecutionTask(uuid4().hex, name, dict(log=str(tmp_path / 'calls.jsonl'), **arguments), uuid4().hex,
        policy, max_attempts=max_attempts, workspace_root=str(root) if root else None,
        workspace_generation=[info.st_dev, info.st_ino] if info else None)
    return DistributedExecutionClient(queue, store).submit(task)


def terminal(cluster, task):
    store = cluster[4]
    return until(lambda: (t if t.terminal else None) if (t := store.get(task.execution_id)) else None)


def no_pending(cluster):
    client, queue = cluster[2], cluster[3]
    until(lambda: client.xpending(queue.stream, queue.group)['pending'] == 0)


def calls(tmp_path):
    path = tmp_path / 'calls.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def kill(process):
    process.kill(); process.join(5)
    assert not process.is_alive() and process.exitcode != 0


def workspace_config(tmp_path):
    root = tmp_path / 'workspace'; root.mkdir(); (root / 'file').write_text('base')
    return {'workspace': root, 'storage': tmp_path / 'recovery'}


def test_two_independent_workers_concurrent_execution(cluster, tmp_path):
    a, b = cluster[6](), cluster[6]()
    until(lambda: len(list(cluster[2].scan_iter(cluster[1] + ':worker:*'))) == 2)
    tasks = [submit(cluster, tmp_path, delay=.3) for _ in range(6)]
    results = [terminal(cluster, task) for task in tasks]
    assert all(t.state is ExecutionState.SUCCEEDED and t.attempt == 1 for t in results)
    pids = {t.result['data']['pid'] for t in results}
    assert pids == {a.pid, b.pid}
    assert os.getpid() not in pids
    owners = {h['owner'] for t in results for h in t.history if h['state'] == 'RUNNING'}
    assert len(owners) == 2
    recorded = calls(tmp_path)
    assert sorted(r['local_count'] for r in recorded if r['pid'] == a.pid) == list(range(1, 1 + sum(r['pid'] == a.pid for r in recorded)))
    assert min(r['local_count'] for r in recorded if r['pid'] == b.pid) == 1
    assert any(x.started_at < y.finished_at and y.started_at < x.finished_at
               for x in results for y in results
               if x.result['data']['pid'] != y.result['data']['pid'])
    no_pending(cluster)
    assert a.is_alive() and b.is_alive()


def test_crash_running_lease_heartbeat_and_safe_replay(cluster, tmp_path):
    worker = cluster[6]()
    task = submit(cluster, tmp_path, delay=.7)
    running = until(lambda: t if (t := cluster[4].get(task.execution_id)).state is ExecutionState.RUNNING and calls(tmp_path) else None)
    assert cluster[5].get(running.owner_worker_id)
    assert cluster[2].xpending(cluster[3].stream, cluster[3].group)['pending'] == 1
    kill(worker)
    until(lambda: cluster[5].get(running.owner_worker_id) is None)
    other = cluster[6]()
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.attempt == 2
    assert result.result['data']['pid'] == other.pid
    assert {'LOST', 'RECOVERING'} <= {h['state'] for h in result.history}
    assert len(calls(tmp_path)) == 2
    assert other.is_alive()
    no_pending(cluster)


def test_crash_after_durable_result_before_ack_never_reexecutes(cluster, tmp_path):
    marker = tmp_path / 'hook'
    worker = cluster[6](hook_stage='after_result_before_ack', marker=marker)
    task = submit(cluster, tmp_path)
    until(marker.exists)
    assert cluster[4].get(task.execution_id).state is ExecutionState.SUCCEEDED
    assert cluster[2].xpending(cluster[3].stream, cluster[3].group)['pending'] == 1
    kill(worker)
    cluster[6]()
    no_pending(cluster)
    result = terminal(cluster, task)
    assert result.attempt == 1 and len(calls(tmp_path)) == 1


def test_duplicate_message_and_execution_idempotency(cluster, tmp_path):
    task = submit(cluster, tmp_path, delay=.3)
    duplicate = replace(task, execution_id=uuid4().hex, tool_call_id='duplicate', state=ExecutionState.CREATED)
    canonical = DistributedExecutionClient(cluster[3], cluster[4]).submit(duplicate)
    assert canonical.execution_id == task.execution_id
    for _ in range(5): cluster[3].publish(task.execution_id)
    cluster[6](); cluster[6]()
    assert terminal(cluster, task).state is ExecutionState.SUCCEEDED
    no_pending(cluster)
    assert len(calls(tmp_path)) == 1
    canonical = DistributedExecutionClient(cluster[3], cluster[4]).submit(duplicate)
    assert canonical.terminal and len(calls(tmp_path)) == 1
    duplicate.arguments = dict(duplicate.arguments, delay=.9)
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'): cluster[4].create(duplicate)


@pytest.mark.parametrize('name,policy', [('irreversible', SideEffectPolicy.IRREVERSIBLE), ('compensatable', SideEffectPolicy.COMPENSATABLE)])
def test_uncertain_external_side_effect_never_replayed(cluster, tmp_path, name, policy):
    worker = cluster[6]()
    task = submit(cluster, tmp_path, name, policy, delay=2)
    until(lambda: len(calls(tmp_path)) == 1)
    kill(worker); cluster[6]()
    result = terminal(cluster, task)
    assert result.state is ExecutionState.FAILED and result.attempt == 1
    assert result.error['kind'] == FailureKind.SIDE_EFFECT_UNCERTAIN
    assert result.error['uncertain'] and len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_crash_before_start_can_retry_irreversible_without_repeating_effect(cluster, tmp_path):
    marker = tmp_path / 'claimed'
    worker = cluster[6](hook_stage='after_claim_before_start', marker=marker)
    task = submit(cluster, tmp_path, 'irreversible', SideEffectPolicy.IRREVERSIBLE)
    until(marker.exists)
    assert cluster[4].get(task.execution_id).state is ExecutionState.CLAIMED
    assert calls(tmp_path) == []
    kill(worker); cluster[6]()
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.attempt == 2
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_tool_failure_retry_limit_and_layer_boundaries(cluster, tmp_path):
    cluster[6]()
    task = submit(cluster, tmp_path, fail=True, max_attempts=3)
    result = terminal(cluster, task)
    assert result.state is ExecutionState.FAILED and result.attempt == 3
    assert result.error['kind'] == FailureKind.RETRY_EXHAUSTED
    assert len(calls(tmp_path)) == 3  # no nested executor retries
    cluster[3].publish(task.execution_id)
    no_pending(cluster)
    assert len(calls(tmp_path)) == 3


def test_workspace_known_failure_selective_rollback_then_retry(cluster, tmp_path):
    config = workspace_config(tmp_path)
    cluster[6](config)
    task = submit(cluster, tmp_path, 'workspace_probe', SideEffectPolicy.WORKSPACE_REVERSIBLE, config=config, fail_first=True)
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.attempt == 2
    assert result.result['data']['before'] == 'base'
    assert (config['workspace'] / 'file').read_text() == 'changed'
    executor = make_probe_executor(config)
    try:
        ledger = executor.recovery_runtime.ledger.records
        assert any(r.action_id.startswith('compensation-') for r in ledger)
    finally: executor.close(); close_recovery(executor)
    no_pending(cluster)


def test_workspace_crash_during_tool_without_after_evidence_fails_closed(cluster, tmp_path):
    config = workspace_config(tmp_path)
    worker = cluster[6](config)
    task = submit(cluster, tmp_path, 'workspace_probe', SideEffectPolicy.WORKSPACE_REVERSIBLE, config=config, delay=2)
    until(lambda: len(calls(tmp_path)) == 1)
    kill(worker); cluster[6](config)
    result = terminal(cluster, task)
    assert result.error['kind'] == FailureKind.SIDE_EFFECT_UNCERTAIN
    assert result.attempt == 1 and len(calls(tmp_path)) == 1
    assert (config['workspace'] / 'file').read_text() == 'base'
    no_pending(cluster)


def test_workspace_completed_checkpoint_closes_result_persistence_window(cluster, tmp_path):
    config = workspace_config(tmp_path); marker = tmp_path / 'committed'
    worker = cluster[6](config, hook_stage='after_tool_before_result', marker=marker)
    task = submit(cluster, tmp_path, 'workspace_probe', SideEffectPolicy.WORKSPACE_REVERSIBLE, config=config)
    until(marker.exists)
    assert cluster[4].get(task.execution_id).state is ExecutionState.RUNNING
    kill(worker); cluster[6](config)
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.attempt == 1
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_workspace_recovery_user_edit_conflict_preserves_user_version(cluster, tmp_path):
    config = workspace_config(tmp_path); marker = tmp_path / 'committed'
    worker = cluster[6](config, hook_stage='after_tool_before_result', marker=marker)
    task = submit(cluster, tmp_path, 'workspace_probe', SideEffectPolicy.WORKSPACE_REVERSIBLE, config=config, fail=True)
    until(marker.exists); kill(worker)
    (config['workspace'] / 'file').write_text('user edit')
    cluster[6](config)
    result = terminal(cluster, task)
    assert result.state is ExecutionState.FAILED and result.error['kind'] == FailureKind.RECOVERY_CONFLICT
    assert (config['workspace'] / 'file').read_text() == 'user edit'
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_atomic_ownership_across_processes_and_stale_token(cluster, tmp_path):
    task = submit(cluster, tmp_path)
    barrier = SPAWN.Barrier(4); outcomes = SPAWN.Queue()
    processes = [SPAWN.Process(target=acquire_process, args=(cluster[0], cluster[1], task.execution_id, barrier, outcomes)) for _ in range(4)]
    for p in processes: p.start()
    owners = [outcomes.get(timeout=15) for _ in processes]
    for p in processes: p.join(5); assert p.exitcode == 0; p.close()
    outcomes.close(); outcomes.join_thread()
    assert sum(owner is not None for owner in owners) == 1
    assert cluster[4].get(task.execution_id).attempt == 1
    # A stale recovery lease must remain fenced even if owner and attempt match.
    other = submit(cluster, tmp_path)
    lease = cluster[4].recover(other.execution_id, 'same-controller', .05).lease
    time.sleep(.07)
    cluster[4].lose(other.execution_id, failure(FailureKind.LEASE_EXPIRED, 'test'))
    current = cluster[4].recover(other.execution_id, 'same-controller', 5)
    with pytest.raises(OwnershipLost): cluster[4].ready(lease)
    assert current.lease.token != lease.token


def test_concurrent_recovery_controllers_do_not_repeat_tool(cluster, tmp_path):
    task = submit(cluster, tmp_path)
    leased = cluster[4].acquire(task.execution_id, 'dead-worker', .05)
    cluster[4].start(leased.lease)
    cluster[3].receive('dead-worker')
    time.sleep(.08)
    barrier = SPAWN.Barrier(2)
    processes = [SPAWN.Process(target=recovery_process, args=(cluster[0], cluster[1], barrier)) for _ in range(2)]
    for p in processes: p.start()
    for p in processes: p.join(10); assert p.exitcode == 0; p.close()
    cluster[6](); cluster[6]()
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.attempt == 2
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_heartbeat_alive_does_not_override_expired_execution_lease(cluster, tmp_path):
    task = submit(cluster, tmp_path)
    cluster[5].register('alive', {'pid': os.getpid()}, 5)
    leased = cluster[4].acquire(task.execution_id, 'alive', .05)
    cluster[4].start(leased.lease)
    cluster[3].receive('alive')
    time.sleep(.08)
    controller = RecoveryController(cluster[3], cluster[4], cluster[5], min_idle_ms=0)
    controller.recover_once()
    recovered = cluster[4].get(task.execution_id)
    assert recovered.state is ExecutionState.QUEUED
    assert recovered.error['kind'] == FailureKind.LEASE_EXPIRED
    with pytest.raises(OwnershipLost): cluster[4].finish(leased.lease, True, {})
    assert cluster[5].get('alive')
    cluster[6](); assert terminal(cluster, task).state is ExecutionState.SUCCEEDED
    no_pending(cluster)


def test_dead_heartbeat_does_not_revoke_valid_lease(cluster, tmp_path):
    task = submit(cluster, tmp_path)
    leased = cluster[4].acquire(task.execution_id, 'missing-heartbeat', 5)
    cluster[4].start(leased.lease); cluster[3].receive('missing-heartbeat')
    RecoveryController(cluster[3], cluster[4], cluster[5], min_idle_ms=0).recover_once()
    assert cluster[4].get(task.execution_id).state is ExecutionState.RUNNING
    assert cluster[4].renew(leased.lease, 5).owner_worker_id == 'missing-heartbeat'


def test_invalid_args_permission_and_policy_are_terminal_not_retried(cluster, tmp_path):
    cluster[6]()
    task = submit(cluster, tmp_path, delay='bad')
    result = terminal(cluster, task)
    assert result.error['kind'] == FailureKind.INVALID_ARGUMENTS and result.attempt == 1
    task = submit(cluster, tmp_path, 'irreversible', SideEffectPolicy.NONE)
    result = terminal(cluster, task)
    assert result.error['kind'] == FailureKind.POLICY_MISMATCH
    assert calls(tmp_path) == []
    no_pending(cluster)


def test_outbox_gap_is_repaired_after_producer_restart(cluster, tmp_path):
    task = ExecutionTask('call', 'probe', {'log': str(tmp_path / 'calls.jsonl')}, 'key')
    created = cluster[4].create(task)  # crash before publish
    controller = RecoveryController(cluster[3], cluster[4], cluster[5], orphan_seconds=.01)
    time.sleep(.02); controller.recover_once()
    cluster[6]()
    assert terminal(cluster, created).state is ExecutionState.SUCCEEDED
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_redis_failure_is_explicit_and_durable_completion_survives_restart(cluster, tmp_path):
    unavailable = connect('redis://127.0.0.1:1/0')
    with pytest.raises(InfrastructureUnavailable): RedisExecutionQueue(unavailable, 'unavailable')
    with pytest.raises(InfrastructureUnavailable): RedisExecutionStore(unavailable).create(ExecutionTask('a', 'probe', {}, 'key'))
    unavailable.close()
    cluster[6]()
    task = submit(cluster, tmp_path)
    before = terminal(cluster, task)
    another = connect(cluster[0])
    try:
        after = RedisExecutionStore(another, cluster[1]).get(task.execution_id)
        assert after.to_dict() == before.to_dict()
        assert RedisExecutionStore(another, cluster[1]).acquire(task.execution_id, 'new-worker', 5) is None
    finally: another.close()
    no_pending(cluster)


def test_permission_failure_timeout_and_unserializable_result(cluster, tmp_path):
    cluster[6]({'deny_execution': True})
    denied = terminal(cluster, submit(cluster, tmp_path, 'denied'))
    assert denied.error['kind'] == FailureKind.PERMISSION_FAILURE and denied.attempt == 1
    assert calls(tmp_path) == []
    timed = terminal(cluster, submit(cluster, tmp_path, 'timeout_probe', max_attempts=1))
    assert timed.error['kind'] == FailureKind.RETRY_EXHAUSTED
    assert timed.result['error_type'] == 'ToolTimeoutError'
    bad = terminal(cluster, submit(cluster, tmp_path, invalid_result=True))
    assert bad.error['kind'] == FailureKind.RESULT_SERIALIZATION and bad.attempt == 1
    no_pending(cluster)


def test_temporary_redis_outage_never_fabricates_submission_success(cluster, tmp_path):
    # Real server pause, with a short-timeout *separate* client. The server may
    # apply timed-out commands later: resubmission must use the stable identity.
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    impatient = redis.Redis.from_url(cluster[0], decode_responses=True, socket_timeout=.05,
        socket_connect_timeout=.05, retry=Retry(NoBackoff(), 0))
    queue = RedisExecutionQueue(impatient, cluster[1])
    store = RedisExecutionStore(impatient, cluster[1])
    task = ExecutionTask('outage', 'probe', {'log': str(tmp_path / 'calls.jsonl')}, 'outage-key')
    cluster[2].execute_command('CLIENT', 'PAUSE', 350, 'ALL')
    try:
        with pytest.raises(InfrastructureUnavailable): DistributedExecutionClient(queue, store).submit(task)
    finally: impatient.close()
    until(lambda: cluster[2].ping())
    canonical = DistributedExecutionClient(cluster[3], cluster[4]).submit(task)
    cluster[6]()
    assert terminal(cluster, canonical).state is ExecutionState.SUCCEEDED
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_graceful_shutdown_stops_claiming_but_finishes_owned_execution(cluster, tmp_path):
    worker = cluster[6]()
    first = submit(cluster, tmp_path, delay=.5)
    until(lambda: len(calls(tmp_path)) == 1)
    # Registering another task while active must not extend shutdown with new work.
    second = submit(cluster, tmp_path)
    # Portable process-local shutdown request; no platform skip.
    worker._cortex_stop.set()
    worker.join(5)
    assert worker.exitcode == 0
    assert terminal(cluster, first).state is ExecutionState.SUCCEEDED
    assert cluster[4].get(second.execution_id).state is ExecutionState.QUEUED
    assert len(calls(tmp_path)) == 1
    cluster[6](); assert terminal(cluster, second).state is ExecutionState.SUCCEEDED
    no_pending(cluster)


def test_pending_idle_never_reclaims_a_live_renewing_execution(cluster, tmp_path):
    cluster[6]()
    task = submit(cluster, tmp_path, delay=1)
    running = until(lambda: t if (t := cluster[4].get(task.execution_id)).state is ExecutionState.RUNNING and calls(tmp_path) else None)
    heartbeat = cluster[5].get(running.owner_worker_id)['heartbeat_at']
    time.sleep(.3)
    RecoveryController(cluster[3], cluster[4], cluster[5], min_idle_ms=0).recover_once()
    current = cluster[4].get(task.execution_id)
    assert current.state is ExecutionState.RUNNING and current.attempt == 1
    assert current.lease_expires_at > running.lease_expires_at
    assert cluster[5].get(running.owner_worker_id)['heartbeat_at'] > heartbeat
    assert terminal(cluster, task).state is ExecutionState.SUCCEEDED
    assert len(calls(tmp_path)) == 1
    no_pending(cluster)


def test_cancelled_queued_task_is_terminal_and_never_calls_tool(cluster, tmp_path):
    task = submit(cluster, tmp_path)
    cancelled = cluster[4].cancel(task.execution_id)
    assert cancelled.state is ExecutionState.CANCELLED
    cluster[6](); no_pending(cluster)
    assert calls(tmp_path) == []
    assert cluster[4].acquire(task.execution_id, 'late-owner', 10) is None


def test_workspace_binding_fails_closed_before_tool(cluster, tmp_path):
    config = workspace_config(tmp_path)
    cluster[6](config)
    task = ExecutionTask('mismatch', 'workspace_probe', {'log': str(tmp_path / 'calls.jsonl')}, 'mismatch',
        SideEffectPolicy.WORKSPACE_REVERSIBLE, workspace_root=str(config['workspace']), workspace_generation=[0, 0])
    submitted = DistributedExecutionClient(cluster[3], cluster[4]).submit(task)
    result = terminal(cluster, submitted)
    assert result.error['kind'] == FailureKind.POLICY_MISMATCH
    assert calls(tmp_path) == [] and (config['workspace'] / 'file').read_text() == 'base'
    no_pending(cluster)


def test_default_worker_cli_executes_existing_process_supervised_tool(cluster, tmp_path):
    import subprocess
    import sys
    root = tmp_path / 'root'; root.mkdir()
    command = [sys.executable, '-m', 'cortex.distributed', 'worker', '--redis-url', cluster[0],
        '--namespace', cluster[1], '--workspace', str(root), '--storage', str(tmp_path / 'state'),
        '--lease-seconds', '2', '--heartbeat-ttl', '2', '--min-idle-ms', '100']
    log = tmp_path / 'worker.log'
    with log.open('w') as output:
        worker = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            info = root.stat()
            task = ExecutionTask('existing-slow', 'slow_tool', {'seconds': 0}, 'existing-slow',
                workspace_root=str(root), workspace_generation=[info.st_dev, info.st_ino])
            submitted = DistributedExecutionClient(cluster[3], cluster[4]).submit(task)
            result = terminal(cluster, submitted)
            assert result.state is ExecutionState.SUCCEEDED and result.result['attempts'] == 1
            assert worker.poll() is None
            no_pending(cluster)
        finally:
            worker.terminate()
            try: worker.wait(timeout=5)
            except subprocess.TimeoutExpired: worker.kill(); worker.wait(timeout=5)
    assert 'Traceback' not in log.read_text()


def test_actual_worker_cancels_tool_after_execution_lease_loss(cluster, tmp_path):
    old = cluster[6]()
    task = submit(cluster, tmp_path, delay=2)
    until(lambda: len(calls(tmp_path)) == 1)
    cluster[2].execute_command('CLIENT', 'PAUSE', 900, 'ALL')
    time.sleep(1)
    # The old process is alive during the outage but cannot renew an expired
    # execution lease. It must stop its invocation and leave pending delivery.
    new = cluster[6]()
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.attempt == 2
    assert result.result['data']['pid'] == new.pid
    assert len(calls(tmp_path)) == 2
    assert os.getpid() != old.pid
    no_pending(cluster)


def test_agent_facing_executor_opt_in_reuses_async_wave_scheduler(cluster, tmp_path):
    from cortex.distributed.executor import DistributedToolExecutor
    from cortex.tools.executor import ToolExecutionContext
    from cortex.runtime.state import AgentState
    a, b = cluster[6](), cluster[6]()
    local = make_probe_executor({})
    executor = DistributedToolExecutor(local.allowed_permissions, local.registry,
        distributed_client=DistributedExecutionClient(cluster[3], cluster[4]), distributed_tools={'probe'})
    state = AgentState(session_id='agent-session')
    contexts = [ToolExecutionContext(state, str(n)) for n in range(2)]
    args = {'log': str(tmp_path / 'calls.jsonl')}
    try:
        results = asyncio.run(executor.aexecute_waves([('probe', args), ('probe', args)], contexts=contexts))
        assert all(result.success for result in results)
        assert {result.data['pid'] for result in results} <= {a.pid, b.pid}
        assert executor.last_batch_metrics['waves']
        cached = asyncio.run(executor.aexecute('probe', args, contexts[0]))
        assert cached.success and len(calls(tmp_path)) == 2
    finally: executor.close()
    no_pending(cluster)


def test_arbitrary_json_roundtrips_without_lua_cjson_corruption(cluster, tmp_path):
    original = {'empty': [], 'nested': [{'empty': [], 'unicode': '中文', 'large': 12345678901234567890}], 'log': str(tmp_path / 'calls.jsonl')}
    task = ExecutionTask('json', 'probe', original, 'json')
    submitted = DistributedExecutionClient(cluster[3], cluster[4]).submit(task)
    assert submitted.arguments == original
    cluster[6]()
    result = terminal(cluster, submitted)
    assert result.arguments == original
    assert result.result['data']['empty'] == []
    assert result.result['data']['nested']['large'] == 12345678901234567890
    no_pending(cluster)


def test_pending_and_execution_scans_rotate_without_starvation(cluster, tmp_path):
    tasks = [submit(cluster, tmp_path) for _ in range(5)]
    received = [cluster[3].receive('idle') for _ in tasks]
    scanned = []
    for _ in range(3): scanned.extend(cluster[3].pending(0, count=2))
    assert {m.message_id for m in scanned} == {m.message_id for m in received}
    scanned_tasks = []
    for _ in range(3): scanned_tasks.extend(cluster[4].tasks(count=2))
    assert {t.execution_id for t in scanned_tasks} == {t.execution_id for t in tasks}


def test_worker_crash_does_not_exit_an_existing_other_worker(cluster, tmp_path):
    marker = tmp_path / 'running'
    victim = cluster[6](hook_stage='after_start_before_tool', marker=marker)
    interrupted = submit(cluster, tmp_path)
    until(marker.exists)
    survivor = cluster[6]()
    kill(victim)
    other = submit(cluster, tmp_path)
    assert terminal(cluster, other).state is ExecutionState.SUCCEEDED
    recovered = terminal(cluster, interrupted)
    assert recovered.state is ExecutionState.SUCCEEDED and recovered.attempt == 2
    assert survivor.is_alive() and recovered.result['data']['pid'] == survivor.pid
    no_pending(cluster)


def test_cli_arguments_file_accepts_windows_powershell_utf8_bom(cluster, tmp_path):
    import subprocess
    import sys
    root = tmp_path / 'execution'; root.mkdir()
    cluster[6]({'workspace': root, 'storage': tmp_path / 'state'})
    arguments = tmp_path / 'task.json'; arguments.write_text('{"seconds":0}', encoding='utf-8-sig')
    output = subprocess.check_output([sys.executable, '-m', 'cortex.distributed', 'submit',
        '--redis-url', cluster[0], '--namespace', cluster[1], '--tool-name', 'slow_tool',
        '--tool-call-id', 'powershell', '--workspace', str(root), '--arguments-file', str(arguments)], text=True)
    task = ExecutionTask(**json.loads(output))
    result = terminal(cluster, task)
    assert result.state is ExecutionState.SUCCEEDED and result.arguments == {'seconds': 0}
    no_pending(cluster)


@pytest.mark.docker
def test_distributed_docker_worker_preserves_isolated_session_main(cluster, tmp_path):
    if os.getenv('CORTEX_RUN_DOCKER_TESTS') != '1':
        pytest.skip('Real Docker integration requires CORTEX_RUN_DOCKER_TESTS=1 and cortex-execution-runtime:v1')
    import docker
    from cortex.runtime.workspace_session import WorkspaceManager
    client = docker.from_env()
    try:
        client.ping(); client.images.get('cortex-execution-runtime:v1')
    except docker.errors.DockerException as error:
        pytest.fail('Configured Docker integration unavailable: ' + str(error))
    finally: client.close()
    main = tmp_path / 'main'; main.mkdir(); (main / 'file').write_text('base')
    manager = WorkspaceManager(tmp_path / 'manager')
    session = manager.create(main, mode='ISOLATED')
    root = Path(session.execution_root)
    # Existing image runs as uid 1000 on Linux; authorize the mounted test root.
    root.chmod(0o777); (root / 'file').chmod(0o666)
    config = {'workspace': root, 'storage': tmp_path / 'worker-state', 'backend': 'docker',
        'workspace_session_id': session.session_id, 'workspace_manager_root': manager.root}
    worker = cluster[6](config)
    try:
        info = root.stat()
        task = ExecutionTask('docker-isolated', 'shell', {'command': 'printf created > created.txt; printf modified > file'},
            'docker-isolated', SideEffectPolicy.WORKSPACE_REVERSIBLE,
            workspace_root=str(root), workspace_generation=[info.st_dev, info.st_ino])
        submitted = DistributedExecutionClient(cluster[3], cluster[4]).submit(task)
        result = terminal(cluster, submitted)
        assert result.state is ExecutionState.SUCCEEDED
        assert (root / 'file').read_text() == 'modified'
        assert (root / 'created.txt').read_text() == 'created'
        assert (main / 'file').read_text() == 'base' and not (main / 'created.txt').exists()
        changes = manager.final_diff(session).changes
        assert {change.path for change in changes} == {'file', 'created.txt'}
        no_pending(cluster)
    finally:
        worker._cortex_stop.set(); worker.join(5)
        if worker.is_alive(): worker.kill(); worker.join(5)
        manager.discard(session)
    assert not root.exists() and (main / 'file').read_text() == 'base'


def test_expired_recovery_owner_cannot_restore_after_workspace_lock_wait(cluster, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    config = workspace_config(tmp_path); marker = tmp_path / 'complete'
    worker = cluster[6](config, hook_stage='after_tool_before_result', marker=marker)
    task = submit(cluster, tmp_path, 'workspace_probe', SideEffectPolicy.WORKSPACE_REVERSIBLE, config=config, fail=True)
    until(marker.exists); kill(worker)
    # Wait for original Worker ownership to expire before controller reservation.
    until(lambda: cluster[4].get(task.execution_id).lease_expires_at < cluster[4].now())
    executor = make_probe_executor(config)
    evidence = WorkspaceRecoveryEvidence(executor.recovery_runtime)
    controller = RecoveryController(cluster[3], cluster[4], cluster[5], workspace_evidence=evidence,
                                    lease_seconds=.1, min_idle_ms=0)
    try:
        with ThreadPoolExecutor() as threads:
            with executor.workspace_lock:
                pending = threads.submit(controller.recover_once)
                until(lambda: cluster[4].get(task.execution_id).state is ExecutionState.RECOVERING)
                time.sleep(.15)
            pending.result(timeout=5)
        assert (config['workspace'] / 'file').read_text() == 'changed'
        # Fresh recovery still has the completed checkpoint and may legally undo.
        RecoveryController(cluster[3], cluster[4], cluster[5], workspace_evidence=evidence,
                           lease_seconds=5, min_idle_ms=0).recover_once()
        assert (config['workspace'] / 'file').read_text() == 'base'
        assert cluster[4].get(task.execution_id).state is ExecutionState.QUEUED
    finally: executor.close(); close_recovery(executor)


def test_persisted_state_machine_and_execution_identity_cannot_be_overwritten(cluster, tmp_path):
    task = submit(cluster, tmp_path)
    with pytest.raises(ValueError, match='EXECUTION_ID_CONFLICT'):
        cluster[4].create(replace(task, state=ExecutionState.CREATED, idempotency_key='different-logical-task'))
    assert cluster[4].get(task.execution_id).idempotency_key == task.idempotency_key
    claimed = cluster[4].acquire(task.execution_id, 'owner', 5)
    with pytest.raises(ValueError, match='ILLEGAL_TRANSITION'):
        cluster[4].finish(claimed.lease, True, {})
    assert cluster[4].get(task.execution_id).state is ExecutionState.CLAIMED
    cluster[4].start(claimed.lease)
    cluster[4].finish(claimed.lease, True, {'success': True, 'data': []})
    with pytest.raises(OwnershipLost): cluster[4].start(claimed.lease)
    assert cluster[4].cancel(task.execution_id).state is ExecutionState.SUCCEEDED
    assert cluster[4].get(task.execution_id).result['data'] == []


def test_direct_distributed_excluded_metadata_is_not_treated_as_reversible(cluster, tmp_path):
    config = workspace_config(tmp_path)
    cluster[6](config)
    task = submit(cluster, tmp_path, 'workspace_probe', SideEffectPolicy.WORKSPACE_REVERSIBLE,
                  config=config, metadata_change=True)
    result = terminal(cluster, task)
    assert result.state is ExecutionState.FAILED and result.attempt == 1
    assert result.error['uncertain'] and result.error['kind'] == FailureKind.PERMISSION_FAILURE
    assert len(calls(tmp_path)) == 1
    executor = make_probe_executor(config)
    try:
        head = executor.recovery_runtime.active_head('distributed-' + task.execution_id)
        assert head.reason == 'unrecoverable_mutation'
        with pytest.raises(RuntimeError, match='no safe reversible checkpoint|no safe reversible|no safe'):
            executor.recovery_runtime.rollback_action(task.execution_id + ':attempt:1')
    finally: executor.close(); close_recovery(executor)
    no_pending(cluster)
