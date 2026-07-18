"""对话 processing 快照的 owner lease 协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type ConversationSnapshotClaimStatus = Literal["claimed", "busy", "empty"]


@dataclass(frozen=True, slots=True)
class ConversationSnapshotClaim:
    """一次快照认领结果。"""

    status: ConversationSnapshotClaimStatus
    processing_key: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationDeadLetter:
    """不包含消息正文的对话失败快照摘要。"""

    group_id: str
    snapshot_id: str
    failure_code: str
    attempt_count: int
    failed_at_ms: int
    message_count: int
    chunk_state_count: int


class ConversationLeaseLostError(RuntimeError):
    """当前 worker 已不再拥有 processing 快照。"""

    def __init__(self, processing_key: str) -> None:
        super().__init__(f"对话 processing 租约已失效: {processing_key}")


CONVERSATION_CLAIM_SCRIPT = """
-- conversation_processing_claim_v2
local source_key = KEYS[1]
local proposed_key = KEYS[2]
local current_key = KEYS[3]
local lease_key = KEYS[4]
local last_message_key = KEYS[5]
local session_start_key = KEYS[6]
local meta_last_message_key = KEYS[7]
local meta_session_start_key = KEYS[8]
local owner_token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local snapshot_ttl_seconds = tonumber(ARGV[3])
local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)

local function set_lease(processing_key)
    local payload = cjson.encode({
        owner_token = owner_token,
        processing_key = processing_key,
        lease_until_ms = now_ms + lease_ms
    })
    redis.call('SET', lease_key, payload, 'PX', lease_ms)
end

local current_processing = redis.call('GET', current_key)
if current_processing and redis.call('EXISTS', current_processing) == 0 then
    redis.call('DEL', current_key)
    current_processing = nil
end

local lease_raw = redis.call('GET', lease_key)
if lease_raw then
    local decoded_ok, lease = pcall(cjson.decode, lease_raw)
    if decoded_ok and lease.processing_key
       and redis.call('EXISTS', tostring(lease.processing_key)) == 1 then
        if not current_processing then
            current_processing = tostring(lease.processing_key)
            redis.call('SET', current_key, current_processing, 'EX', snapshot_ttl_seconds)
        end
        return {2, tostring(lease.processing_key)}
    end
    if not decoded_ok and redis.call('EXISTS', lease_raw) == 1 then
        if not current_processing then
            current_processing = lease_raw
            redis.call('SET', current_key, current_processing, 'EX', snapshot_ttl_seconds)
        end
        return {2, lease_raw}
    end
    redis.call('DEL', lease_key)
end

if current_processing then
    set_lease(current_processing)
    redis.call('EXPIRE', current_processing, snapshot_ttl_seconds)
    redis.call('EXPIRE', current_key, snapshot_ttl_seconds)
    return {1, current_processing}
end

if redis.call('EXISTS', source_key) == 0 then
    return {0, ''}
end
if redis.call('LLEN', source_key) == 0 then
    redis.call('DEL', source_key)
    return {0, ''}
end
if redis.call('EXISTS', proposed_key) ~= 0 then
    return redis.error_reply('conversation processing key already exists')
end

redis.call('RENAME', source_key, proposed_key)
local last_message = redis.call('GET', last_message_key)
if last_message then
    redis.call('SET', meta_last_message_key, last_message)
end
local session_start = redis.call('GET', session_start_key)
if session_start then
    redis.call('SET', meta_session_start_key, session_start)
end
redis.call('DEL', last_message_key, session_start_key)
redis.call('SET', current_key, proposed_key, 'EX', snapshot_ttl_seconds)
set_lease(proposed_key)
redis.call('EXPIRE', proposed_key, snapshot_ttl_seconds)
redis.call('EXPIRE', meta_last_message_key, snapshot_ttl_seconds)
redis.call('EXPIRE', meta_session_start_key, snapshot_ttl_seconds)
return {1, proposed_key}
"""


CONVERSATION_CLAIM_EXISTING_SCRIPT = """
-- conversation_processing_claim_existing_v2
local processing_key = KEYS[1]
local current_key = KEYS[2]
local lease_key = KEYS[3]
local owner_token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local snapshot_ttl_seconds = tonumber(ARGV[3])
local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)

local lease_raw = redis.call('GET', lease_key)
if lease_raw then
    local decoded_ok, lease = pcall(cjson.decode, lease_raw)
    if decoded_ok and lease.processing_key
       and redis.call('EXISTS', tostring(lease.processing_key)) == 1 then
        return {2, tostring(lease.processing_key)}
    end
    if not decoded_ok and redis.call('EXISTS', lease_raw) == 1 then
        return {2, lease_raw}
    end
    redis.call('DEL', lease_key)
end

local current_processing = redis.call('GET', current_key)
if current_processing and current_processing ~= processing_key
   and redis.call('EXISTS', current_processing) == 1 then
    return {2, current_processing}
end
if redis.call('EXISTS', processing_key) == 0 then
    return {0, ''}
end

local payload = cjson.encode({
    owner_token = owner_token,
    processing_key = processing_key,
    lease_until_ms = now_ms + lease_ms
})
redis.call('SET', current_key, processing_key, 'EX', snapshot_ttl_seconds)
redis.call('SET', lease_key, payload, 'PX', lease_ms)
redis.call('EXPIRE', processing_key, snapshot_ttl_seconds)
return {1, processing_key}
"""


CONVERSATION_GET_SCRIPT = """
-- conversation_processing_get_owned_v2
local processing_key = KEYS[1]
local current_key = KEYS[2]
local lease_key = KEYS[3]
local owner_token = ARGV[1]
local lease_raw = redis.call('GET', lease_key)
if not lease_raw or redis.call('GET', current_key) ~= processing_key then
    return {0}
end
local decoded_ok, lease = pcall(cjson.decode, lease_raw)
if not decoded_ok or tostring(lease.owner_token) ~= owner_token
   or tostring(lease.processing_key) ~= processing_key then
    return {0}
end
local result = {1}
local items = redis.call('LRANGE', processing_key, 0, -1)
for _, item in ipairs(items) do
    table.insert(result, item)
end
return result
"""


CONVERSATION_RENEW_SCRIPT = """
-- conversation_processing_renew_v2
local processing_key = KEYS[1]
local current_key = KEYS[2]
local lease_key = KEYS[3]
local owner_token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local snapshot_ttl_seconds = tonumber(ARGV[3])
local lease_raw = redis.call('GET', lease_key)
if not lease_raw or redis.call('GET', current_key) ~= processing_key
   or redis.call('EXISTS', processing_key) == 0 then
    return 0
end
local decoded_ok, lease = pcall(cjson.decode, lease_raw)
if not decoded_ok or tostring(lease.owner_token) ~= owner_token
   or tostring(lease.processing_key) ~= processing_key then
    return 0
end
local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
lease.lease_until_ms = now_ms + lease_ms
redis.call('SET', lease_key, cjson.encode(lease), 'PX', lease_ms)
redis.call('EXPIRE', processing_key, snapshot_ttl_seconds)
redis.call('EXPIRE', current_key, snapshot_ttl_seconds)
return 1
"""


CONVERSATION_ACK_SCRIPT = """
-- conversation_processing_ack_owned_v2
local processing_key = KEYS[1]
local current_key = KEYS[2]
local lease_key = KEYS[3]
local meta_last_message_key = KEYS[4]
local meta_session_start_key = KEYS[5]
local chunks_key = KEYS[6]
local owner_token = ARGV[1]
local lease_raw = redis.call('GET', lease_key)
if not lease_raw or redis.call('GET', current_key) ~= processing_key then
    return 0
end
local decoded_ok, lease = pcall(cjson.decode, lease_raw)
if not decoded_ok or tostring(lease.owner_token) ~= owner_token
   or tostring(lease.processing_key) ~= processing_key then
    return 0
end
redis.call(
    'DEL', processing_key, current_key, lease_key,
    meta_last_message_key, meta_session_start_key, chunks_key
)
return 1
"""


CONVERSATION_RESTORE_SCRIPT = """
-- conversation_processing_restore_owned_v2
local processing_key = KEYS[1]
local target_key = KEYS[2]
local current_key = KEYS[3]
local lease_key = KEYS[4]
local last_message_key = KEYS[5]
local session_start_key = KEYS[6]
local meta_last_message_key = KEYS[7]
local meta_session_start_key = KEYS[8]
local chunks_key = KEYS[9]
local owner_token = ARGV[1]
local lease_raw = redis.call('GET', lease_key)
if not lease_raw or redis.call('GET', current_key) ~= processing_key then
    return -1
end
local decoded_ok, lease = pcall(cjson.decode, lease_raw)
if not decoded_ok or tostring(lease.owner_token) ~= owner_token
   or tostring(lease.processing_key) ~= processing_key then
    return -1
end

local old_items = redis.call('LRANGE', processing_key, 0, -1)
local new_items = redis.call('LRANGE', target_key, 0, -1)
redis.call('DEL', target_key)
for _, item in ipairs(old_items) do
    redis.call('RPUSH', target_key, item)
end
for _, item in ipairs(new_items) do
    redis.call('RPUSH', target_key, item)
end
local session_start = redis.call('GET', meta_session_start_key)
if session_start then
    redis.call('SET', session_start_key, session_start)
elseif #old_items > 0 or #new_items > 0 then
    local first_item = old_items[1] or new_items[1]
    local timestamp = string.match(first_item, '"timestamp"%s*:%s*([0-9%.]+)')
    if timestamp then
        redis.call('SET', session_start_key, timestamp)
    end
end
local last_item = new_items[#new_items] or old_items[#old_items]
if last_item then
    local timestamp = string.match(last_item, '"timestamp"%s*:%s*([0-9%.]+)')
    if timestamp then
        redis.call('SET', last_message_key, timestamp)
    end
else
    local last_message = redis.call('GET', meta_last_message_key)
    if last_message then
        redis.call('SET', last_message_key, last_message)
    end
end
redis.call(
    'DEL', processing_key, current_key, lease_key,
    meta_last_message_key, meta_session_start_key, chunks_key
)
return #old_items
"""


CONVERSATION_CHUNK_LEDGER_SCRIPT = """
-- conversation_processing_chunk_ledger_v1
local processing_key = KEYS[1]
local current_key = KEYS[2]
local lease_key = KEYS[3]
local chunks_key = KEYS[4]
local owner_token = ARGV[1]
local operation = ARGV[2]
local field = ARGV[3]
local value = ARGV[4]
local ttl_seconds = tonumber(ARGV[5])
local lease_raw = redis.call('GET', lease_key)
if not lease_raw or redis.call('GET', current_key) ~= processing_key then
    return {0, ''}
end
local decoded_ok, lease = pcall(cjson.decode, lease_raw)
if not decoded_ok or tostring(lease.owner_token) ~= owner_token
   or tostring(lease.processing_key) ~= processing_key then
    return {0, ''}
end
if operation == 'initialize' then
    redis.call('HSETNX', chunks_key, field, value)
elseif operation == 'set' then
    redis.call('HSET', chunks_key, field, value)
end
redis.call('EXPIRE', chunks_key, ttl_seconds)
local stored = redis.call('HGET', chunks_key, field)
return {1, stored or ''}
"""


CONVERSATION_DEAD_LETTER_SCRIPT = """
-- conversation_processing_dead_letter_v1
local processing_key = KEYS[1]
local current_key = KEYS[2]
local lease_key = KEYS[3]
local meta_last_message_key = KEYS[4]
local meta_session_start_key = KEYS[5]
local chunks_key = KEYS[6]
local dead_key = KEYS[7]
local dead_index_key = KEYS[8]
local owner_token = ARGV[1]
local group_id = ARGV[2]
local failure_code = ARGV[3]
local attempt_count = tonumber(ARGV[4])

local lease_raw = redis.call('GET', lease_key)
if not lease_raw or redis.call('GET', current_key) ~= processing_key
   or redis.call('EXISTS', processing_key) == 0 then
    return 0
end
local decoded_ok, lease = pcall(cjson.decode, lease_raw)
if not decoded_ok or tostring(lease.owner_token) ~= owner_token
   or tostring(lease.processing_key) ~= processing_key then
    return 0
end

local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
redis.call(
    'HSET', dead_key,
    'status', 'dead_letter',
    'processing_key', processing_key,
    'group_id', group_id,
    'failure_code', failure_code,
    'attempt_count', attempt_count,
    'failed_at_ms', now_ms
)
redis.call('PERSIST', processing_key)
redis.call('PERSIST', meta_last_message_key)
redis.call('PERSIST', meta_session_start_key)
redis.call('PERSIST', chunks_key)
redis.call('PERSIST', dead_key)
redis.call('ZADD', dead_index_key, now_ms, processing_key)
redis.call('PERSIST', dead_index_key)
redis.call('DEL', current_key, lease_key)
return 1
"""


CONVERSATION_DEAD_LETTER_REQUEUE_SCRIPT = """
-- conversation_processing_dead_letter_requeue_v1
local processing_key = KEYS[1]
local target_key = KEYS[2]
local last_message_key = KEYS[3]
local session_start_key = KEYS[4]
local meta_last_message_key = KEYS[5]
local meta_session_start_key = KEYS[6]
local chunks_key = KEYS[7]
local dead_key = KEYS[8]
local dead_index_key = KEYS[9]

if redis.call('HGET', dead_key, 'status') ~= 'dead_letter'
   or redis.call('HGET', dead_key, 'processing_key') ~= processing_key
   or redis.call('EXISTS', processing_key) == 0 then
    return -1
end

local old_items = redis.call('LRANGE', processing_key, 0, -1)
local new_items = redis.call('LRANGE', target_key, 0, -1)
redis.call('DEL', target_key)
for _, item in ipairs(old_items) do
    redis.call('RPUSH', target_key, item)
end
for _, item in ipairs(new_items) do
    redis.call('RPUSH', target_key, item)
end

local session_start = redis.call('GET', meta_session_start_key)
if session_start then
    redis.call('SET', session_start_key, session_start)
elseif #old_items > 0 or #new_items > 0 then
    local first_item = old_items[1] or new_items[1]
    local timestamp = string.match(first_item, '"timestamp"%s*:%s*([0-9%.]+)')
    if timestamp then
        redis.call('SET', session_start_key, timestamp)
    end
end
local last_item = new_items[#new_items] or old_items[#old_items]
if last_item then
    local timestamp = string.match(last_item, '"timestamp"%s*:%s*([0-9%.]+)')
    if timestamp then
        redis.call('SET', last_message_key, timestamp)
    end
else
    local last_message = redis.call('GET', meta_last_message_key)
    if last_message then
        redis.call('SET', last_message_key, last_message)
    end
end

redis.call(
    'DEL', processing_key, meta_last_message_key,
    meta_session_start_key, chunks_key, dead_key
)
redis.call('ZREM', dead_index_key, processing_key)
return #old_items
"""
