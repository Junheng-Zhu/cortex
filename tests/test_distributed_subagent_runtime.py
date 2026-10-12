"""Six acceptance scenarios, using Fake LLM/tools and real Redis processes."""
import asyncio
from dataclasses import asdict
import gc
import json
import multiprocessing
import time

import pytest
from cortex.app.bootstrap import build_agent
from cortex.distributed.client import DistributedExecutionClient
from cortex.distributed.models import AgentTaskSpec, ExecutionState, ExecutionTask, FailureKind, OwnershipLost, TaskType
from cortex.runtime.session import Session, SessionConfig
from cortex.tools.base import SideEffectPolicy
from cortex.tools.permission import Permission
from subagent_support import make_executor, worker_process
from test_distributed_redis_integration import cluster, until, no_pending, kill

pytestmark = pytest.mark.redis
SPAWN = multiprocessing.get_context('spawn')


@pytest.fixture
def agents(cluster, tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    config = {'workspace': str(workspace), 'log': str(tmp_path / 'agent.jsonl')}
    local = make_executor(config)
    api = DistributedExecutionClient(cluster[3], cluster[4], agent_registry=local.registry,
        agent_allowed_permissions={Permission.READ}, agent_allowed_tools={'echo', 'external_effect'})
    processes = []

    def start(stage=None, marker=None):
        stop = SPAWN.Event()
        process = SPAWN.Process(target=worker_process,
            args=(cluster[0], cluster[1], config, stop, stage, str(marker) if marker else None))
        process.start(); processes.append((process, stop))
        return process

    def records():
        path = tmp_path / 'agent.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    yield api, start, records, config, local
    for process, stop in processes:
        stop.set(); process.join(3)
        if process.is_alive(): process.kill(); process.join(3)
        process.close()
    processes.clear(); gc.collect()
    local.close()


def submit(api, label, **context):
    return api.submit_agent(parent_run_id='parent-run', goal='Compute the requested answer',
        context=dict(label=label, **context), allowed_tools=['echo'], idempotency_key=label)


def wait(api, task_id):
    return asyncio.run(api.wait_agent(task_id, timeout=12))


def test_basic_delegation(agents, cluster, tmp_path):
    api, start, records, config, local = agents
    start()
    task_id = submit(api, 'basic')
    assert submit(api, 'basic') == task_id
    result = wait(api, task_id)
    assert result.task_id == task_id and result.terminal_state is ExecutionState.SUCCEEDED
    assert result.final_answer == 'answer:basic' and result.error is None
    assert result.statistics['steps'] == 1 and result.statistics['total_tokens'] == 11
    calls = [r for r in records() if r['event'] == 'llm']
    assert [r['call'] for r in calls] == [1, 2]
    assert len([r for r in records() if r['event'] == 'tool_start']) == 1
    assert cluster[4].get(task_id).result['final_answer'] == result.final_answer
    cluster[3].publish(task_id)
    assert wait(api, task_id) == result
    no_pending(cluster)
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        api.submit_agent(parent_run_id='parent-run', goal='changed', context={}, allowed_tools=[], idempotency_key='basic')

    # Exercise the registered model tool through the existing Parent AgentLoop.
    class ParentLLM:
        def __init__(self): self.calls = 0
        def respond(self, input, tools, previous_response_id=None):
            from cortex.llm.protocol import LLMResponse, ToolCall
            self.calls += 1
            assert 'delegate_task' in {t['name'] for t in tools}
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall('submit', 'delegate_task',
                    dict(operation='submit', goal='compute', context_json='{"label":"model"}',
                         allowed_tools=['echo'], idempotency_key='model'))])
            return LLMResponse(content='submitted')
    parent = build_agent(ParentLLM(), tool_registry=local.registry, execution_workspace=config['workspace'],
        workspace_snapshot_root=tmp_path / 'parent-state', skills_enabled=False,
        workspace_recovery_enabled=False, session=Session(temporary_chat=True),
        session_config=SessionConfig(temporary_chat=True, persist_trace=False), agent_delegation_client=api)
    try:
        assert asyncio.run(parent.arun('Delegate compute')) == 'submitted'
        delegated = parent.last_state.observations[0].output['task_id']
        assert wait(api, delegated).final_answer == 'answer:model'
        tool = parent.executor.registry.get('delegate_task')
        polled = asyncio.run(parent.executor.aexecute('delegate_task', {'operation': 'wait', 'task_id': delegated}))
        assert polled.success and polled.data['task_id'] == delegated
        assert tool.side_effect_policy is SideEffectPolicy.IRREVERSIBLE
    finally:
        parent.close()
    no_pending(cluster)


def test_concurrent_execution(agents, cluster):
    api, start, records, _, _ = agents
    worker = start()
    ids = [submit(api, str(n), delay=.5, llm_delay=.2) for n in range(3)]
    results = [wait(api, task_id) for task_id in ids]
    assert [r.final_answer for r in results] == ['answer:0', 'answer:1', 'answer:2']
    assert [r.task_id for r in results] == ids
    assert len({r.statistics['child_session_id'] for r in results}) == 3
    starts = [r for r in records() if r['event'] == 'tool_start']
    ends = [r for r in records() if r['event'] == 'tool_end']
    assert len(starts) == len(ends) == 3 and {r['pid'] for r in starts} == {worker.pid}
    # All three tools started before the first one ended, in one Worker process.
    assert max(r['at'] for r in starts) < min(r['at'] for r in ends)
    llm = [r for r in records() if r['event'] == 'llm']
    assert len({r['client_id'] for r in llm}) == 3
    assert all(r['previous_response_id'] is None for r in llm)
    no_pending(cluster)


def test_context_and_permission_isolation(agents, cluster):
    api, start, records, config, local = agents
    start()
    parent = Session(history=[{'role': 'user', 'content': 'PRIVATE-PARENT-HISTORY'}],
                     compact_summary='PRIVATE-PARENT-MEMORY', previous_response_id='private-provider-cursor')
    from cortex.memory.manager import MemoryManager
    from cortex.memory.store import InMemoryStore
    memory = MemoryManager(InMemoryStore())
    memory.remember('PRIVATE-PARENT-LONG-TERM public', 'global', 'default')

    class ParentLLM:
        def __init__(self): self.calls = 0
        def respond(self, input, tools, previous_response_id=None):
            from cortex.llm.protocol import LLMResponse, ToolCall
            self.calls += 1
            if self.calls == 1:
                assert 'PRIVATE-PARENT' in json.dumps(input)
                return LLMResponse(tool_calls=[ToolCall('submit', 'delegate_task',
                    dict(operation='submit', goal='public', context_json='{"label":"public"}',
                         allowed_tools=['echo'], idempotency_key='public'))])
            return LLMResponse(content='submitted')

    parent_agent = build_agent(ParentLLM(), session=parent, memory_manager=memory,
        tool_registry=local.registry, skills_enabled=False, workspace_recovery_enabled=False,
        execution_workspace=config['workspace'], session_config=SessionConfig(persist_trace=False),
        agent_delegation_client=api)
    try:
        asyncio.run(parent_agent.arun('public'))
        task_id = parent_agent.last_state.observations[0].output['task_id']
    finally:
        parent_agent.close()
    assert wait(api, task_id).terminal_state is ExecutionState.SUCCEEDED
    calls = [r for r in records() if r['event'] == 'llm']
    assert all('PRIVATE-PARENT' not in json.dumps(r) and r['previous_response_id'] is None for r in calls)
    assert all(r['tools'] == ['echo'] for r in calls)
    assert parent.previous_response_id == 'private-provider-cursor'
    for name in ('parent_secret', 'delegate_task'):
        denied = submit(api, 'try-' + name, tool=name)
        assert wait(api, denied).terminal_state is ExecutionState.FAILED
    assert all(r.get('tool') == 'echo' for r in records() if r['event'].startswith('tool_'))
    with pytest.raises(ValueError, match='authorization'):
        api.submit_agent(parent_run_id='parent', goal='read secret', context={}, allowed_tools=['parent_secret'], idempotency_key='secret')
    with pytest.raises(ValueError, match='effect-free'):
        api.submit_agent(parent_run_id='parent', goal='external', context={}, allowed_tools=['external_effect'], idempotency_key='effect')
    # Bypass the Host producer to prove Worker enforces its own trusted registry.
    spec = AgentTaskSpec('parent', 'external', {}, ['external_effect'], ['external_effect'])
    forged = ExecutionTask('forged', '__agent__', {}, 'forged', task_type=TaskType.AGENT, agent=asdict(spec))
    raw_id = DistributedExecutionClient(cluster[3], cluster[4]).submit(forged).task_id
    assert wait(api, raw_id).error['kind'] == FailureKind.POLICY_MISMATCH
    assert not any(r.get('tool') == 'external_effect' for r in records())
    no_pending(cluster)


def test_budget_deadline_and_cancellation(agents, cluster):
    api, start, records, _, _ = agents
    queued = submit(api, 'queued-cancel')
    assert api.cancel_agent(queued).state is ExecutionState.CANCELLED
    assert api.cancel_agent(queued).state is ExecutionState.CANCELLED
    assert wait(api, queued).terminal_state is ExecutionState.CANCELLED
    start()
    budget = api.submit_agent(parent_run_id='parent', goal='loop', context={'label':'budget', 'mode':'batch'},
        allowed_tools=['echo'], idempotency_key='budget', max_steps=1)
    assert wait(api, budget).error['kind'] == FailureKind.STEP_BUDGET_EXCEEDED
    assert len([r for r in records() if r.get('value', '').startswith('budget') and r['event'] == 'tool_start']) == 1
    before = api.submit_agent(parent_run_id='parent', goal='expired', context={'label':'expired'},
        allowed_tools=['echo'], idempotency_key='expired', deadline=cluster[4].now() - 1)
    assert wait(api, before).error['kind'] == FailureKind.DEADLINE_EXCEEDED
    slow_llm = api.submit_agent(parent_run_id='parent', goal='deadline during llm',
        context={'label':'llm-deadline', 'llm_delay':.8}, allowed_tools=['echo'],
        idempotency_key='llm-deadline', deadline=cluster[4].now() + .4)
    assert wait(api, slow_llm).error['kind'] == FailureKind.DEADLINE_EXCEEDED
    during = api.submit_agent(parent_run_id='parent', goal='deadline during tool',
        context={'label':'tool-deadline', 'delay':5}, allowed_tools=['echo'],
        idempotency_key='tool-deadline', deadline=cluster[4].now() + .6)
    assert wait(api, during).error['kind'] == FailureKind.DEADLINE_EXCEEDED
    until(lambda: any(r.get('value') == 'tool-deadline' and r['event'] == 'tool_cancel' for r in records()))
    running = submit(api, 'cancel', mode='loop', delay=5)
    until(lambda: any(r.get('value') == 'cancel' and r['event'] == 'tool_start' for r in records()))
    with pytest.raises(TimeoutError):
        asyncio.run(api.wait_agent(running, timeout=.02))
    assert api.get_agent_status(running).state is ExecutionState.RUNNING
    old_lease = api.get_agent_status(running).lease
    assert api.cancel_agent(running).state is ExecutionState.CANCELLED
    until(lambda: any(r.get('value') == 'cancel' and r['event'] == 'tool_cancel' for r in records()))
    assert api.cancel_agent(running).result == api.get_agent_status(running).result
    with pytest.raises(OwnershipLost): cluster[4].finish(old_lease, True, {'success':True})
    time.sleep(.9)  # Allow the non-abortable fake sync LLM to return.
    assert not any(r.get('value') in {'expired', 'llm-deadline', 'queued-cancel'} for r in records())
    assert len([r for r in records() if r.get('value') == 'cancel' and r['event'] == 'tool_start']) == 1
    assert not any(r.get('value') == 'cancel' and r['event'] == 'tool_end' for r in records())
    no_pending(cluster)


def test_worker_failure_and_recovery(agents, cluster, tmp_path):
    api, start, records, _, _ = agents
    victim = start()
    task_id = submit(api, 'recover', delay=.8)
    until(lambda: any(r.get('value') == 'recover' and r['event'] == 'tool_start' for r in records()))
    old = api.get_agent_status(task_id)
    assert old.recovery['agent_replay_safe'] is True
    kill(victim)
    survivor = start()
    recovered = wait(api, task_id)
    assert recovered.terminal_state is ExecutionState.SUCCEEDED and recovered.statistics['attempt'] == 2
    starts = [r for r in records() if r.get('value') == 'recover' and r['event'] == 'tool_start']
    assert [r['pid'] for r in starts] == [victim.pid, survivor.pid]
    with pytest.raises(OwnershipLost): cluster[4].finish(old.lease, True, {'success':True})
    assert {'LOST', 'RECOVERING'} <= {h['state'] for h in api.get_agent_status(task_id).history}

    # CLAIMED but never started: safe handoff requires no execution evidence.
    survivor.terminate(); survivor.join(5)
    marker = tmp_path / 'claimed'
    unstarted_worker = start('after_claim_before_start', marker)
    unstarted_id = submit(api, 'unstarted')
    until(marker.exists)
    assert api.get_agent_status(unstarted_id).state is ExecutionState.CLAIMED
    kill(unstarted_worker)
    survivor = start()
    assert wait(api, unstarted_id).statistics['attempt'] == 2
    assert len([r for r in records() if r.get('value') == 'unstarted' and r['event'] == 'tool_start']) == 1

    # A crashed RUNNING attempt without positive effect-free evidence is unsafe,
    # even if the producer labels it NONE. Recovery must never ask an LLM.
    spec = AgentTaskSpec('parent', 'uncertain', {}, ['echo'], ['echo'])
    raw = ExecutionTask('unsafe', '__agent__', {}, 'unsafe', task_type=TaskType.AGENT, agent=asdict(spec))
    cluster[4].create(raw); cluster[4].queued(raw.task_id)
    claimed = cluster[4].acquire(raw.task_id, 'dead-worker', .1)
    cluster[4].start(claimed.lease)
    cluster[3].publish(raw.task_id); cluster[3].receive('dead-worker')
    uncertain = wait(api, raw.task_id)
    assert uncertain.error['kind'] == FailureKind.SIDE_EFFECT_UNCERTAIN and uncertain.error['uncertain']
    assert api.get_agent_status(raw.task_id).attempt == 1

    # Terminal result survives process death after persistence and before ACK.
    marker = tmp_path / 'persisted'
    survivor.terminate(); survivor.join(5)
    persisted_worker = start('after_result_before_ack', marker)
    persisted_id = submit(api, 'persisted')
    until(marker.exists)
    result = wait(api, persisted_id)
    kill(persisted_worker); start()
    assert wait(api, persisted_id) == result
    no_pending(cluster)
    assert len([r for r in records() if r.get('value') == 'persisted' and r['event'] == 'tool_start']) == 1


def test_backward_compatibility(agents, cluster):
    api, start, records, _, _ = agents
    start()
    task = ExecutionTask('tool-call', 'echo', {'value':'legacy'}, 'legacy-key')
    assert task.task_type is TaskType.TOOL and task.agent is None
    client = DistributedExecutionClient(cluster[3], cluster[4])
    submitted = client.submit(task)
    result = asyncio.run(client.wait(submitted.task_id, timeout=12))
    assert result.state is ExecutionState.SUCCEEDED and result.result['data'] == 'legacy'
    assert result.result['attempts'] == 1 and 'terminal_state' not in result.result
    assert client.submit(task).execution_id == submitted.execution_id
    cluster[3].publish(submitted.task_id)
    no_pending(cluster)
    assert len([r for r in records() if r.get('value') == 'legacy' and r['event'] == 'tool_start']) == 1
    # Old V1 serialized records have no task_type or agent_json.
    raw = json.loads(cluster[2].get(cluster[4].prefix + submitted.task_id))
    raw.pop('task_type'); raw.pop('agent_json')
    from cortex.distributed.redis_adapter import RedisExecutionStore
    decoded = RedisExecutionStore.decode(json.dumps(raw))
    assert decoded.task_type is TaskType.TOOL and decoded.result == result.result
    with pytest.raises(OwnershipLost): cluster[4].finish(result.lease, True, {})
    assert cluster[4].cancel(submitted.task_id).state is ExecutionState.SUCCEEDED
