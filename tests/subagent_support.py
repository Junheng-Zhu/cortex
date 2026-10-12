"""Fake LLM/tools and importable real-Redis Worker for the six V1 scenarios."""
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from pydantic import BaseModel
from cortex.distributed.worker import WorkerRuntime
from cortex.distributed.redis_adapter import connect, RedisExecutionQueue, RedisExecutionStore, RedisWorkerRegistry
from cortex.distributed.recovery import RecoveryController
from cortex.llm.protocol import LLMResponse, LLMUsage, ToolCall
from cortex.tools.base import Tool, ExecutionStrategy, ConcurrencyPolicy, SideEffectPolicy
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry


def record(path, **data):
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(dict(data, pid=os.getpid(), at=time.time())) + '\n').encode())
    finally:
        os.close(fd)


class EchoInput(BaseModel):
    value: str
    delay: float = 0


class EchoTool(Tool):
    name = 'echo'
    description = 'An effect-free fake computation'
    input_model = EchoInput
    permission = Permission.READ
    retryable = False
    max_retries = 0
    timeout = 10
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED
    concurrency_policy = ConcurrencyPolicy.PARALLEL_SAFE

    def __init__(self, log):
        self.log = log

    def execute(self, input):
        return input.value

    async def aexecute(self, input):
        record(self.log, event='tool_start', tool=self.name, value=input.value)
        try:
            await asyncio.sleep(input.delay)
            record(self.log, event='tool_end', tool=self.name, value=input.value)
            return input.value
        except asyncio.CancelledError:
            record(self.log, event='tool_cancel', tool=self.name, value=input.value)
            raise


class SecretTool(EchoTool):
    name = 'parent_secret'
    permission = Permission.EXECUTE


class ExternalTool(EchoTool):
    name = 'external_effect'
    side_effect_policy = SideEffectPolicy.IRREVERSIBLE


def make_executor(config):
    config = dict(config)
    if 'log' not in config:
        storage = Path(config['storage']); storage.mkdir(parents=True, exist_ok=True)
        config['log'] = str(storage / 'fake-agent.jsonl')
    registry = ToolRegistry()
    for tool in (EchoTool(config['log']), SecretTool(config['log']), ExternalTool(config['log'])):
        registry.register(tool)
    return ToolExecutor(set(Permission), registry)


class FakeLLM:
    def __init__(self, config):
        self.config, self.calls = config, 0
        self.client_id = uuid4().hex
        self.context = None

    def respond(self, input, tools, previous_response_id=None):
        self.calls += 1
        if self.context is None:
            self.context = json.loads(input[0]['content'].split('Explicit task context (JSON):\n', 1)[1])
        context = self.context
        record(self.config['log'], event='llm', client_id=self.client_id, call=self.calls,
               input=input, tools=[t['name'] for t in tools], previous_response_id=previous_response_id)
        time.sleep(context.get('llm_delay', 0))
        mode = context.get('mode', 'basic')
        if self.calls == 1 or mode in {'loop', 'batch'}:
            name = context.get('tool', 'echo')
            count = 3 if mode == 'batch' else 1
            calls = [ToolCall(f'{self.calls}:{n}', name,
                dict(value=context['label'] + (f':{n}' if count > 1 else ''), delay=context.get('delay', 0)))
                for n in range(count)]
            return LLMResponse(tool_calls=calls, usage=LLMUsage(3, 2, 5), output_items=[
                dict(type='function_call', call_id=c.call_id, name=c.name, arguments=json.dumps(c.arguments)) for c in calls])
        assert any(item.get('type') == 'function_call_output' and context['label'] in item['output'] for item in input)
        return LLMResponse(content='answer:' + context['label'], usage=LLMUsage(4, 2, 6))


def make_client(config):
    config = dict(config)
    if 'log' not in config:
        storage = Path(config['storage']); storage.mkdir(parents=True, exist_ok=True)
        config['log'] = str(storage / 'fake-agent.jsonl')
    return FakeLLM(config)


def worker_process(url, namespace, config, stop, hook_stage=None, marker=None):
    client = connect(url)
    queue, store, workers = RedisExecutionQueue(client, namespace), RedisExecutionStore(client, namespace), RedisWorkerRegistry(client, namespace)
    executor = make_executor(config)
    controller = RecoveryController(queue, store, workers, min_idle_ms=50, lease_seconds=1, orphan_seconds=.5)

    async def hook(stage, task):
        if stage == hook_stage:
            Path(marker).write_text(task.execution_id)
            while True:
                await asyncio.sleep(.02)

    runtime = WorkerRuntime(queue, store, workers, executor,
        lease_seconds=.6, heartbeat_ttl=.6, recovery=controller, hook=hook,
        agent_client_factory=lambda: make_client(config), agent_workspace=config['workspace'])

    async def run():
        async def monitor():
            while not stop.is_set():
                await asyncio.sleep(.02)
            runtime.request_shutdown()
        watcher = asyncio.create_task(monitor())
        try:
            await runtime.run()
        finally:
            watcher.cancel()

    try:
        asyncio.run(run())
    finally:
        client.close()
