"""Redis adapters. All ownership mutations execute on Redis's clock in Lua."""
import hashlib
import json
from functools import wraps

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from .interfaces import QueueMessage
from .models import ExecutionTask, ExecutionState, InfrastructureUnavailable, OwnershipLost


JSON_FIELDS = ('arguments', 'result', 'outcome', 'error', 'recovery', 'workspace_generation')


def guarded(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except redis.ResponseError as error:
            if str(error) in {'IDEMPOTENCY_CONFLICT', 'EXECUTION_ID_CONFLICT', 'ILLEGAL_TRANSITION'}:
                raise ValueError(str(error)) from error
            raise InfrastructureUnavailable(f'Redis operation failed: {type(error).__name__}') from error
        except redis.RedisError as error:
            raise InfrastructureUnavailable(f'Redis operation failed: {type(error).__name__}') from error
    return call


def connect(url):
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2,
                               socket_timeout=2, retry=Retry(NoBackoff(), 0))


CREATE = '''
local incoming = cjson.decode(ARGV[2])
local prior = redis.call('GET', KEYS[2])
local id = ARGV[1]
if prior then id = prior end
local key = ARGV[4] .. id
local existing = redis.call('GET', key)
if existing then
 local value = cjson.decode(existing)
 if value.idempotency_key ~= incoming.idempotency_key then return redis.error_reply('EXECUTION_ID_CONFLICT') end
 if value.fingerprint ~= ARGV[3] then return redis.error_reply('IDEMPOTENCY_CONFLICT') end
 return existing
end
if prior then return redis.error_reply('MISSING_IDEMPOTENT_EXECUTION') end
if redis.call('EXISTS', KEYS[1]) ~= 0 then return redis.error_reply('EXECUTION_ID_CONFLICT') end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local task = incoming
task.created_at = now
task.fingerprint = ARGV[3]
task.history = {{state='CREATED', at=now, attempt=0}}
local payload = cjson.encode(task)
redis.call('SET', KEYS[1], payload)
redis.call('SET', KEYS[2], id)
redis.call('ZADD', KEYS[3], now, id)
return payload
'''

MUTATE = '''
local raw = redis.call('GET', KEYS[1])
if not raw then return redis.error_reply('EXECUTION_NOT_FOUND') end
local t = cjson.decode(raw)
local a = cjson.decode(ARGV[2])
local op = ARGV[1]
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local terminal = t.state == 'SUCCEEDED' or t.state == 'FAILED' or t.state == 'CANCELLED'
local active = t.state == 'CLAIMED' or t.state == 'RUNNING' or t.state == 'RECOVERING'
local function owned()
 return active and t.owner_worker_id == a.owner and t.attempt == a.attempt and t.lease_token == a.token and t.lease_expires_at > now
end
local function state(value)
 t.state = value
 table.insert(t.history, {state=value, at=now, attempt=t.attempt, owner=t.owner_worker_id})
end
local function release()
 t.owner_worker_id = cjson.null
 t.lease_expires_at = 0
end
if op == 'queued' then
 if t.state == 'CREATED' then state('QUEUED'); t.queued_at=now end
elseif op == 'acquire' then
 if t.state ~= 'QUEUED' then return nil end
 if t.attempt >= t.max_attempts then
  state('FAILED'); t.finished_at=now; release()
  t.error_json=cjson.encode({kind='RETRY_EXHAUSTED', message='max_attempts reached', infrastructure=false, uncertain=false})
 else
  t.lease_token=t.lease_token+1; t.attempt=t.attempt+1; t.owner_worker_id=a.owner
  t.lease_expires_at=now+a.seconds; t.recovery_json='{}'; t.outcome_json='null'
  state('CLAIMED')
 end
elseif op == 'start' then
 if not owned() or t.state ~= 'CLAIMED' then return redis.error_reply('OWNERSHIP_LOST') end
 t.started_at=now; state('RUNNING')
elseif op == 'renew' then
 if not owned() then return redis.error_reply('OWNERSHIP_LOST') end
 t.lease_expires_at=now+a.seconds
elseif op == 'attach' then
 if not owned() or t.state ~= 'RUNNING' then return redis.error_reply('OWNERSHIP_LOST') end
 t.recovery_json=a.recovery_json
elseif op == 'finish' then
 if not owned() or (t.state ~= 'RUNNING' and t.state ~= 'CLAIMED' and t.state ~= 'RECOVERING') then
  return redis.error_reply('OWNERSHIP_LOST')
 end
 if a.success and t.state == 'CLAIMED' then return redis.error_reply('ILLEGAL_TRANSITION') end
 if a.success then state('SUCCEEDED') else state('FAILED') end
 t.result_json=a.result_json; t.error_json=a.error_json; t.finished_at=now; release()
elseif op == 'end' then
 if not owned() or t.state ~= 'RUNNING' then return redis.error_reply('OWNERSHIP_LOST') end
 t.outcome_json=a.outcome_json; t.error_json=a.error_json; t.lost_from='RUNNING'; state('LOST'); release()
elseif op == 'lose' then
 if not active or t.lease_expires_at > now then return nil end
 if t.state ~= 'RECOVERING' then t.lost_from=t.state end
 t.error_json=a.error_json; state('LOST'); release()
elseif op == 'recover' then
 if t.state ~= 'LOST' and t.state ~= 'QUEUED' and t.state ~= 'CREATED' then return nil end
 if t.state ~= 'LOST' then t.lost_from=t.state end
 t.lease_token=t.lease_token+1; t.owner_worker_id=a.owner; t.lease_expires_at=now+a.seconds; state('RECOVERING')
elseif op == 'ready' then
 if not owned() or t.state ~= 'RECOVERING' then return redis.error_reply('OWNERSHIP_LOST') end
 state('QUEUED'); t.queued_at=now; release()
elseif op == 'cancel' then
 if not terminal then
  t.error_json=cjson.encode({kind='CANCELLED', message='Execution cancelled; in-flight effects are not undone',
    infrastructure=false, uncertain=(active and t.state ~= 'CLAIMED' and t.side_effect_policy ~= 'none')})
  state('CANCELLED'); t.finished_at=now; release()
 end
else
 return redis.error_reply('UNKNOWN_OPERATION')
end
raw = cjson.encode(t)
redis.call('SET', KEYS[1], raw)
return raw
'''


class RedisExecutionStore:
    def __init__(self, client, namespace='cortex-distributed-v1'):
        self.client = client
        self.prefix = namespace + ':execution:'
        self.namespace = namespace
        self._create = client.register_script(CREATE)
        self._mutate = client.register_script(MUTATE)

    @staticmethod
    def decode(raw):
        if not raw:
            return None
        values = json.loads(raw)
        values.pop('fingerprint', None)
        # User JSON is opaque to Lua: cjson would otherwise convert [] to {}
        # and truncate large numbers on each lifecycle mutation.
        for field in JSON_FIELDS:
            values[field] = json.loads(values.pop(field + '_json'))
        return ExecutionTask(**values)

    @guarded
    def create(self, task):
        if (task.state is not ExecutionState.CREATED or task.attempt != 0 or
                task.owner_worker_id is not None or task.result is not None or task.error is not None):
            raise ValueError('Only a fresh CREATED task can be submitted')
        idem = hashlib.sha256(task.idempotency_key.encode()).hexdigest()
        payload = task.to_dict()
        for field in JSON_FIELDS:
            payload[field + '_json'] = json.dumps(payload.pop(field), allow_nan=False)
        return self.decode(self._create(keys=[self.prefix+task.execution_id,
            self.namespace+':idempotency:'+idem, self.namespace+':executions'],
            args=[task.execution_id, json.dumps(payload, allow_nan=False), task.fingerprint(), self.prefix]))

    @guarded
    def get(self, execution_id):
        return self.decode(self.client.get(self.prefix + execution_id))

    @guarded
    def tasks(self, count=100):
        ids = self.client.zrange(self.namespace+':executions', 0, -1)
        tasks = [t for t in (self.get(i) for i in ids) if t and not t.terminal]
        if not tasks: return []
        start = getattr(self, "_scan_offset", 0) % len(tasks)
        self._scan_offset = start + count
        return (tasks[start:] + tasks[:start])[:count]

    @guarded
    def now(self):
        seconds, micros = self.client.time()
        return seconds + micros / 1_000_000

    @guarded
    def _op(self, execution_id, operation, **arguments):
        for field in JSON_FIELDS:
            if field in arguments:
                arguments[field + '_json'] = json.dumps(arguments.pop(field), allow_nan=False)
        try:
            return self.decode(self._mutate(keys=[self.prefix+execution_id],
                args=[operation, json.dumps(arguments, allow_nan=False)]))
        except redis.ResponseError as error:
            if str(error) == 'OWNERSHIP_LOST':
                raise OwnershipLost(str(error)) from error
            raise

    @staticmethod
    def _owned(lease):
        return dict(owner=lease.owner_worker_id, attempt=lease.attempt, token=lease.token)

    def queued(self, execution_id): return self._op(execution_id, 'queued')
    def acquire(self, execution_id, worker_id, lease_seconds):
        if lease_seconds <= 0: raise ValueError('lease must be positive')
        return self._op(execution_id, 'acquire', owner=worker_id, seconds=lease_seconds)
    def start(self, lease): return self._op(lease.execution_id, 'start', **self._owned(lease))
    def renew(self, lease, lease_seconds):
        if lease_seconds <= 0: raise ValueError('lease must be positive')
        return self._op(lease.execution_id, 'renew', seconds=lease_seconds, **self._owned(lease))
    def attach_recovery(self, lease, recovery):
        return self._op(lease.execution_id, 'attach', recovery=recovery, **self._owned(lease))
    def finish(self, lease, success, result, error=None):
        return self._op(lease.execution_id, 'finish', success=success, result=result, error=error, **self._owned(lease))
    def end_attempt(self, lease, outcome, error):
        return self._op(lease.execution_id, 'end', outcome=outcome, error=error, **self._owned(lease))
    def lose(self, execution_id, error): return self._op(execution_id, 'lose', error=error)
    def recover(self, execution_id, controller, lease_seconds):
        if lease_seconds <= 0: raise ValueError('lease must be positive')
        return self._op(execution_id, 'recover', owner=controller, seconds=lease_seconds)
    def ready(self, lease): return self._op(lease.execution_id, 'ready', **self._owned(lease))
    def cancel(self, execution_id): return self._op(execution_id, 'cancel')


class RedisExecutionQueue:
    def __init__(self, client, namespace='cortex-distributed-v1', group='workers'):
        self.client, self.stream, self.group = client, namespace+':tasks', group
        self.ensure_group()

    @guarded
    def ensure_group(self):
        try:
            self.client.xgroup_create(self.stream, self.group, id='0', mkstream=True)
        except redis.ResponseError as error:
            if 'BUSYGROUP' not in str(error): raise

    @guarded
    def publish(self, execution_id):
        return self.client.xadd(self.stream, {'execution_id': execution_id})

    @guarded
    def receive(self, consumer, block_ms=100):
        if not 1 <= block_ms <= 1000: raise ValueError('block_ms must be between 1 and 1000')
        rows = self.client.xreadgroup(self.group, consumer, {self.stream:'>'}, count=1, block=block_ms)
        if not rows: return None
        message_id, fields = rows[0][1][0]
        return QueueMessage(message_id, fields['execution_id'], consumer)

    @guarded
    def pending(self, min_idle_ms, count=100):
        result = []
        rows = self.client.xpending_range(self.stream, self.group, getattr(self, '_pending_cursor', '-'), '+', count,
                                          idle=min_idle_ms)
        self._pending_cursor = ('(' + rows[-1]['message_id']) if len(rows) == count else '-'
        for row in rows:
            message = self.client.xrange(self.stream, row['message_id'], row['message_id'])
            if message:
                result.append(QueueMessage(row['message_id'], message[0][1]['execution_id'],
                    row['consumer'], row['time_since_delivered']))
        return result

    @guarded
    def reclaim(self, message, consumer, min_idle_ms):
        return bool(self.client.xclaim(self.stream, self.group, consumer,
                                      min_idle_ms, [message.message_id], justid=True))

    @guarded
    def ack(self, message):
        self.client.xack(self.stream, self.group, message.message_id)


class RedisWorkerRegistry:
    def __init__(self, client, namespace='cortex-distributed-v1'):
        self.client, self.prefix = client, namespace+':worker:'

    @guarded
    def register(self, worker_id, metadata, ttl):
        if ttl <= 0: raise ValueError('heartbeat TTL must be positive')
        clock = self.client.time()
        payload = dict(metadata, worker_id=worker_id, registered_at=clock[0]+clock[1]/1_000_000, heartbeat_at=clock[0]+clock[1]/1_000_000)
        if not self.client.set(self.prefix+worker_id, json.dumps(payload), nx=True, px=max(1,int(ttl*1000))):
            raise ValueError('worker_id is already registered')

    @guarded
    def heartbeat(self, worker_id, ttl):
        if ttl <= 0: raise ValueError('heartbeat TTL must be positive')
        return bool(self.client.eval('''
local raw=redis.call('GET', KEYS[1])
if not raw then return 0 end
local payload=cjson.decode(raw)
local now=redis.call('TIME')
payload.heartbeat_at=tonumber(now[1])+tonumber(now[2])/1000000
redis.call('SET', KEYS[1], cjson.encode(payload), 'PX', ARGV[1])
return 1
''', 1, self.prefix+worker_id, max(1,int(ttl*1000))))

    @guarded
    def get(self, worker_id):
        raw = self.client.get(self.prefix+worker_id)
        return json.loads(raw) if raw else None

    @guarded
    def unregister(self, worker_id): self.client.delete(self.prefix+worker_id)
