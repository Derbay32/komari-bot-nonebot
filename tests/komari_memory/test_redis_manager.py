"""RedisManager 缓冲区行为测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import (
    redis_manager as redis_manager_module,
)
from komari_bot.plugins.komari_memory.services.redis_keys import RedisKeys
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)


def _redis_range(items: list[str], start: int, stop: int) -> list[str]:
    """模拟 Redis 的 LRANGE 索引语义。"""
    if not items:
        return []

    length = len(items)
    if start < 0:
        start += length
    if stop < 0:
        stop += length

    start = max(start, 0)
    stop = min(stop, length - 1)
    if start >= length or start > stop:
        return []
    return items[start : stop + 1]


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    def rpush(self, key: str, *values: str) -> "_FakePipeline":
        self._ops.append(("rpush", (key, *values)))
        return self

    def llen(self, key: str) -> "_FakePipeline":
        self._ops.append(("llen", (key,)))
        return self

    def set(self, key: str, value: object) -> "_FakePipeline":
        self._ops.append(("set", (key, value)))
        return self

    def delete(self, key: str) -> "_FakePipeline":
        self._ops.append(("delete", (key,)))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for op, args in self._ops:
            if op == "rpush":
                key, *values = args
                self._redis.data.setdefault(str(key), []).extend(str(value) for value in values)
                results.append(len(self._redis.data[str(key)]))
            elif op == "llen":
                (key,) = args
                results.append(len(self._redis.data.get(str(key), [])))
            elif op == "set":
                key, value = args
                self._redis.values[str(key)] = str(value)
                results.append(True)
            elif op == "delete":
                (key,) = args
                self._redis.data.pop(str(key), None)
                self._redis.values.pop(str(key), None)
                results.append(1)
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.now_ms = 1_000_000.0

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return _redis_range(self.data.get(key, []), start, stop)

    async def execute_command(self, command: str, *args: object) -> object:
        match command:
            case "LLEN":
                return self._llen(args)
            case "HLEN":
                return self._hlen(args)
            case "SADD":
                return self._sadd(args)
            case "SPOP":
                return self._spop(args)
            case "EVAL":
                return self._eval(args)
            case _:
                msg = f"未模拟 Redis 命令: {command}"
                raise AssertionError(msg)

    def _llen(self, args: tuple[object, ...]) -> int:
        (key,) = args
        return len(self.data.get(str(key), []))

    def _hlen(self, args: tuple[object, ...]) -> int:
        (key,) = args
        return len(self.hashes.get(str(key), {}))

    def _sadd(self, args: tuple[object, ...]) -> int:
        key, value = args
        self.sets.setdefault(str(key), set()).add(str(value))
        return 1

    def _spop(self, args: tuple[object, ...]) -> list[str]:
        key, count = args
        values = sorted(self.sets.get(str(key), set()))[: int(str(count))]
        self.sets.setdefault(str(key), set()).difference_update(values)
        return values

    def _eval(self, args: tuple[object, ...]) -> object:
        script, key_count, *rest = args
        script_text = str(script)
        evaluators: tuple[tuple[str, Callable[[list[object]], object]], ...] = (
            ("proactive_reserve", self._eval_proactive_reserve),
            ("proactive_confirm", self._eval_proactive_confirm),
            ("proactive_renew", self._eval_proactive_renew),
            ("proactive_release", self._eval_proactive_release),
            ("proactive_count", self._eval_proactive_count),
            ("chat_commit_message_once_v1", self._eval_chat_commit_message_once),
            (
                "chat_commit_interaction_once_v1",
                self._eval_chat_commit_interaction_once,
            ),
            ("global_interaction_push_v1", self._eval_global_interaction_push),
            ("interaction_summary_claim", self._eval_interaction_claim),
            ("interaction_summary_renew", self._eval_interaction_renew),
            ("interaction_summary_snapshot", self._eval_interaction_snapshot),
            ("interaction_summary_ack", self._eval_interaction_ack),
            ("interaction_summary_requeue", self._eval_interaction_requeue),
            ("conversation_processing_claim_v2", self._eval_conversation_claim),
            (
                "conversation_processing_claim_existing_v2",
                self._eval_conversation_claim_existing,
            ),
            ("conversation_processing_get_owned_v2", self._eval_conversation_get),
            ("conversation_processing_renew_v2", self._eval_conversation_renew),
            ("conversation_processing_ack_owned_v2", self._eval_conversation_ack),
            (
                "conversation_processing_restore_owned_v2",
                self._eval_conversation_restore,
            ),
            (
                "conversation_processing_chunk_ledger_v1",
                self._eval_conversation_chunk_ledger,
            ),
            (
                "conversation_processing_dead_letter_requeue_v1",
                self._eval_conversation_dead_letter_requeue,
            ),
            (
                "conversation_processing_dead_letter_v1",
                self._eval_conversation_dead_letter,
            ),
        )
        for marker, evaluator in evaluators:
            if marker in script_text:
                return evaluator(rest)

        assert int(str(key_count)) == 2
        key1 = str(rest[0])
        key2 = str(rest[1])
        if "RENAME" in script_text:
            return self._eval_snapshot(key1, key2)
        return self._eval_restore(key1, key2)

    def _prune_proactive_slots(self, slots_key: str, now_ms: float) -> None:
        slots = self.zsets.setdefault(slots_key, {})
        expired = [member for member, score in slots.items() if score <= now_ms]
        for member in expired:
            slots.pop(member, None)

    def _eval_proactive_reserve(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        max_slots = int(str(rest[3]))
        reservation_ttl_ms = float(str(rest[4]))
        now_ms = self.now_ms
        pending_until_ms = now_ms + reservation_ttl_ms
        self._prune_proactive_slots(slots_key, now_ms)
        slots = self.zsets[slots_key]
        if (
            f"pending:{reservation_id}" in slots
            or f"confirmed:{reservation_id}" in slots
        ):
            return 3
        if cooldown_key in self.values:
            return 1
        if len(slots) >= max_slots:
            return 2
        slots[f"pending:{reservation_id}"] = pending_until_ms
        self.values[cooldown_key] = reservation_id
        return 0

    def _eval_proactive_confirm(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        rate_window_ms = float(str(rest[5]))
        now_ms = self.now_ms
        confirmed_until_ms = now_ms + rate_window_ms
        self._prune_proactive_slots(slots_key, now_ms)
        slots = self.zsets[slots_key]
        confirmed_member = f"confirmed:{reservation_id}"
        if confirmed_member in slots:
            return 2
        had_pending = int(slots.pop(f"pending:{reservation_id}", None) is not None)
        slots[confirmed_member] = confirmed_until_ms
        current_cooldown = self.values.get(cooldown_key)
        if current_cooldown is None or current_cooldown == reservation_id:
            self.values[cooldown_key] = confirmed_member
        return had_pending

    def _eval_proactive_release(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        slots = self.zsets.setdefault(slots_key, {})
        removed = int(slots.pop(f"pending:{reservation_id}", None) is not None)
        if self.values.get(cooldown_key) == reservation_id:
            self.values.pop(cooldown_key, None)
        return removed

    def _eval_proactive_renew(self, rest: list[object]) -> int:
        _cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        reservation_ttl_ms = float(str(rest[3]))
        self._prune_proactive_slots(slots_key, self.now_ms)
        slots = self.zsets.setdefault(slots_key, {})
        if f"confirmed:{reservation_id}" in slots:
            return 2
        pending_member = f"pending:{reservation_id}"
        if pending_member not in slots:
            return 0
        slots[pending_member] = self.now_ms + reservation_ttl_ms
        return 1

    def _eval_proactive_count(self, rest: list[object]) -> int:
        slots_key = str(rest[0])
        self._prune_proactive_slots(slots_key, self.now_ms)
        return len(self.zsets[slots_key])

    def _eval_chat_commit_message_once(self, rest: list[object]) -> int:
        dedupe_key, buffer_key, session_key, last_key = map(str, rest[:4])
        payload = str(rest[4])
        timestamp = str(rest[5])
        if dedupe_key in self.values:
            return 0
        if not self.data.get(buffer_key):
            self.values[session_key] = timestamp
        self.data.setdefault(buffer_key, []).append(payload)
        self.values[last_key] = timestamp
        self.values[dedupe_key] = "1"
        return 1

    def _eval_chat_commit_interaction_once(self, rest: list[object]) -> int:
        dedupe_key, interaction_key, pending_key = map(str, rest[:3])
        payload = str(rest[3])
        user_id = str(rest[4])
        trigger_size = int(str(rest[5]))
        if dedupe_key in self.values:
            return 0
        self.data.setdefault(interaction_key, []).append(payload)
        if len(self.data[interaction_key]) >= trigger_size:
            self.sets.setdefault(pending_key, set()).add(user_id)
        self.values[dedupe_key] = "1"
        return 1

    def _eval_global_interaction_push(self, rest: list[object]) -> int:
        interaction_key, pending_key = map(str, rest[:2])
        user_id = str(rest[2])
        trigger_size = int(str(rest[3]))
        payloads = [str(item) for item in rest[4:]]
        self.data.setdefault(interaction_key, []).extend(payloads)
        buffer_length = len(self.data[interaction_key])
        if buffer_length >= trigger_size:
            self.sets.setdefault(pending_key, set()).add(user_id)
        return buffer_length

    def _eval_interaction_claim(self, rest: list[object]) -> list[str]:
        pending_key, leases_key, owners_key = map(str, rest[:3])
        owner_token = str(rest[3])
        count = int(str(rest[4]))
        lease_ms = float(str(rest[5]))
        leases = self.zsets.setdefault(leases_key, {})
        owners = self.hashes.setdefault(owners_key, {})
        pending = self.sets.setdefault(pending_key, set())

        expired = [
            user_id
            for user_id, expires_at in leases.items()
            if expires_at <= self.now_ms
        ]
        for user_id in expired:
            leases.pop(user_id, None)
            owners.pop(user_id, None)
            pending.add(user_id)

        claimed: list[str] = []
        for user_id in sorted(pending):
            if len(claimed) >= count:
                break
            if user_id in leases:
                continue
            pending.remove(user_id)
            leases[user_id] = self.now_ms + lease_ms
            owners[user_id] = owner_token
            claimed.append(user_id)
        return claimed

    def _eval_interaction_snapshot(self, rest: list[object]) -> str | None:
        source_key, snapshots_key = map(str, rest[:2])
        user_id = str(rest[2])
        processing_key = str(rest[3])
        snapshots = self.hashes.setdefault(snapshots_key, {})
        existing_key = snapshots.get(user_id)
        if existing_key:
            if self.data.get(existing_key):
                return existing_key
            snapshots.pop(user_id, None)

        if not self.data.get(source_key):
            self.data.pop(source_key, None)
            return None
        assert processing_key not in self.data
        self.data[processing_key] = self.data.pop(source_key)
        snapshots[user_id] = processing_key
        return processing_key

    def _eval_interaction_renew(self, rest: list[object]) -> int:
        leases_key, owners_key = map(str, rest[:2])
        user_id = str(rest[2])
        owner_token = str(rest[3])
        lease_ms = float(str(rest[4]))
        owners = self.hashes.setdefault(owners_key, {})
        leases = self.zsets.setdefault(leases_key, {})
        if owners.get(user_id) != owner_token or user_id not in leases:
            return 0
        leases[user_id] = self.now_ms + lease_ms
        return 1

    def _eval_interaction_ack(self, rest: list[object]) -> int:
        leases_key, owners_key, snapshots_key = map(str, rest[:3])
        user_id = str(rest[3])
        owner_token = str(rest[4])
        processing_key = str(rest[5])
        owners = self.hashes.setdefault(owners_key, {})
        if owners.get(user_id) != owner_token:
            return 0

        snapshots = self.hashes.setdefault(snapshots_key, {})
        if snapshots.get(user_id) not in (None, processing_key):
            return 0
        if snapshots.get(user_id) == processing_key:
            self.data.pop(processing_key, None)
            snapshots.pop(user_id, None)
        self.zsets.setdefault(leases_key, {}).pop(user_id, None)
        owners.pop(user_id, None)
        return 1

    def _eval_interaction_requeue(self, rest: list[object]) -> int:
        leases_key, owners_key, snapshots_key, pending_key, target_key = map(
            str, rest[:5]
        )
        user_id = str(rest[5])
        owner_token = str(rest[6])
        processing_key = str(rest[7])
        owners = self.hashes.setdefault(owners_key, {})
        if owners.get(user_id) != owner_token:
            return 0

        snapshots = self.hashes.setdefault(snapshots_key, {})
        if snapshots.get(user_id) == processing_key:
            old_items = list(self.data.get(processing_key, []))
            new_items = list(self.data.get(target_key, []))
            self.data.pop(target_key, None)
            if old_items or new_items:
                self.data[target_key] = [*old_items, *new_items]
            self.data.pop(processing_key, None)
            snapshots.pop(user_id, None)
        self.zsets.setdefault(leases_key, {}).pop(user_id, None)
        owners.pop(user_id, None)
        self.sets.setdefault(pending_key, set()).add(user_id)
        return 1

    def _eval_conversation_claim(self, rest: list[object]) -> list[object]:
        (
            source_key,
            proposed_key,
            current_key,
            lease_key,
            last_message_key,
            session_start_key,
            meta_last_message_key,
            meta_session_start_key,
        ) = map(str, rest[:8])
        owner_token = str(rest[8])
        current_processing = self.values.get(current_key)
        if current_processing and current_processing not in self.data:
            self.values.pop(current_key, None)
            current_processing = None
        lease_processing = self._active_conversation_lease_processing(lease_key)
        if lease_processing:
            return [2, lease_processing]
        if current_processing:
            self._set_conversation_lease(lease_key, owner_token, current_processing)
            return [1, current_processing]
        if not self.data.get(source_key):
            self.data.pop(source_key, None)
            return [0, ""]
        assert proposed_key not in self.data
        self.data[proposed_key] = self.data.pop(source_key)
        if last_message_key in self.values:
            self.values[meta_last_message_key] = self.values[last_message_key]
        if session_start_key in self.values:
            self.values[meta_session_start_key] = self.values[session_start_key]
        self.values.pop(last_message_key, None)
        self.values.pop(session_start_key, None)
        self.values[current_key] = proposed_key
        self._set_conversation_lease(lease_key, owner_token, proposed_key)
        return [1, proposed_key]

    def _eval_conversation_claim_existing(self, rest: list[object]) -> list[object]:
        processing_key, current_key, lease_key = map(str, rest[:3])
        owner_token = str(rest[3])
        lease_processing = self._active_conversation_lease_processing(lease_key)
        if lease_processing:
            return [2, lease_processing]
        current_processing = self.values.get(current_key)
        if (
            current_processing
            and current_processing != processing_key
            and current_processing in self.data
        ):
            return [2, current_processing]
        if processing_key not in self.data:
            return [0, ""]
        self.values[current_key] = processing_key
        self._set_conversation_lease(lease_key, owner_token, processing_key)
        return [1, processing_key]

    def _eval_conversation_get(self, rest: list[object]) -> list[object]:
        processing_key, current_key, lease_key = map(str, rest[:3])
        owner_token = str(rest[3])
        if not self._owns_conversation_processing(
            processing_key,
            current_key,
            lease_key,
            owner_token,
        ):
            return [0]
        return [1, *self.data.get(processing_key, [])]

    def _eval_conversation_renew(self, rest: list[object]) -> int:
        processing_key, current_key, lease_key = map(str, rest[:3])
        owner_token = str(rest[3])
        if not self._owns_conversation_processing(
            processing_key,
            current_key,
            lease_key,
            owner_token,
        ):
            return 0
        self._set_conversation_lease(lease_key, owner_token, processing_key)
        return 1

    def _eval_conversation_ack(self, rest: list[object]) -> int:
        (
            processing_key,
            current_key,
            lease_key,
            meta_last_message_key,
            meta_session_start_key,
            chunks_key,
        ) = map(str, rest[:6])
        owner_token = str(rest[6])
        if not self._owns_conversation_processing(
            processing_key,
            current_key,
            lease_key,
            owner_token,
        ):
            return 0
        self.data.pop(processing_key, None)
        for key in (
            current_key,
            lease_key,
            meta_last_message_key,
            meta_session_start_key,
        ):
            self.values.pop(key, None)
        self.hashes.pop(chunks_key, None)
        return 1

    def _eval_conversation_restore(self, rest: list[object]) -> int:
        (
            processing_key,
            target_key,
            current_key,
            lease_key,
            last_message_key,
            session_start_key,
            meta_last_message_key,
            meta_session_start_key,
            chunks_key,
        ) = map(str, rest[:9])
        owner_token = str(rest[9])
        if not self._owns_conversation_processing(
            processing_key,
            current_key,
            lease_key,
            owner_token,
        ):
            return -1
        old_items = list(self.data.get(processing_key, []))
        new_items = list(self.data.get(target_key, []))
        self.data[target_key] = [*old_items, *new_items]
        self.data.pop(processing_key, None)
        if meta_session_start_key in self.values:
            self.values[session_start_key] = self.values[meta_session_start_key]
        if meta_last_message_key in self.values:
            self.values[last_message_key] = self.values[meta_last_message_key]
        for key in (
            current_key,
            lease_key,
            meta_last_message_key,
            meta_session_start_key,
        ):
            self.values.pop(key, None)
        self.hashes.pop(chunks_key, None)
        return len(old_items)

    def _eval_conversation_chunk_ledger(self, rest: list[object]) -> list[object]:
        processing_key, current_key, lease_key, chunks_key = map(str, rest[:4])
        owner_token = str(rest[4])
        operation = str(rest[5])
        field = str(rest[6])
        value = str(rest[7])
        if not self._owns_conversation_processing(
            processing_key,
            current_key,
            lease_key,
            owner_token,
        ):
            return [0, ""]
        ledger = self.hashes.setdefault(chunks_key, {})
        if operation == "initialize":
            ledger.setdefault(field, value)
        elif operation == "set":
            ledger[field] = value
        return [1, ledger.get(field, "")]

    def _eval_conversation_dead_letter(self, rest: list[object]) -> int:
        (
            processing_key,
            current_key,
            lease_key,
            _meta_last_message_key,
            _meta_session_start_key,
            _chunks_key,
            dead_key,
            dead_index_key,
        ) = map(str, rest[:8])
        owner_token = str(rest[8])
        group_id = str(rest[9])
        failure_code = str(rest[10])
        attempt_count = str(rest[11])
        if not self._owns_conversation_processing(
            processing_key,
            current_key,
            lease_key,
            owner_token,
        ):
            return 0
        self.hashes[dead_key] = {
            "status": "dead_letter",
            "processing_key": processing_key,
            "group_id": group_id,
            "failure_code": failure_code,
            "attempt_count": attempt_count,
            "failed_at_ms": str(int(self.now_ms)),
        }
        self.zsets.setdefault(dead_index_key, {})[processing_key] = self.now_ms
        self.values.pop(current_key, None)
        self.values.pop(lease_key, None)
        return 1

    def _eval_conversation_dead_letter_requeue(self, rest: list[object]) -> int:
        (
            processing_key,
            target_key,
            last_message_key,
            session_start_key,
            meta_last_message_key,
            meta_session_start_key,
            chunks_key,
            dead_key,
            dead_index_key,
        ) = map(str, rest[:9])
        metadata = self.hashes.get(dead_key, {})
        if (
            metadata.get("status") != "dead_letter"
            or metadata.get("processing_key") != processing_key
            or processing_key not in self.data
        ):
            return -1
        old_items = list(self.data.get(processing_key, []))
        new_items = list(self.data.get(target_key, []))
        self.data[target_key] = [*old_items, *new_items]
        session_start = self.values.get(meta_session_start_key)
        if session_start is None and (old_items or new_items):
            session_start = self._message_timestamp(old_items[0] if old_items else new_items[0])
        if session_start is not None:
            self.values[session_start_key] = session_start
        if new_items:
            last_item = new_items[-1]
        elif old_items:
            last_item = old_items[-1]
        else:
            last_item = None
        last_message = self._message_timestamp(last_item) if last_item else None
        if last_message is None:
            last_message = self.values.get(meta_last_message_key)
        if last_message is not None:
            self.values[last_message_key] = last_message
        self.data.pop(processing_key, None)
        self.values.pop(meta_last_message_key, None)
        self.values.pop(meta_session_start_key, None)
        self.hashes.pop(chunks_key, None)
        self.hashes.pop(dead_key, None)
        self.zsets.setdefault(dead_index_key, {}).pop(processing_key, None)
        return len(old_items)

    @staticmethod
    def _message_timestamp(payload: str) -> str | None:
        try:
            timestamp = json.loads(payload).get("timestamp")
        except (AttributeError, TypeError, ValueError):
            return None
        return str(timestamp) if timestamp is not None else None

    def _active_conversation_lease_processing(self, lease_key: str) -> str:
        raw = self.values.get(lease_key)
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except ValueError:
            return raw if raw in self.data else ""
        processing_key = str(payload.get("processing_key", ""))
        return processing_key if processing_key in self.data else ""

    def _set_conversation_lease(
        self,
        lease_key: str,
        owner_token: str,
        processing_key: str,
    ) -> None:
        self.values[lease_key] = json.dumps(
            {
                "owner_token": owner_token,
                "processing_key": processing_key,
                "lease_until_ms": self.now_ms + 60_000,
            }
        )

    def _owns_conversation_processing(
        self,
        processing_key: str,
        current_key: str,
        lease_key: str,
        owner_token: str,
    ) -> bool:
        if self.values.get(current_key) != processing_key:
            return False
        try:
            lease = json.loads(self.values.get(lease_key, ""))
        except ValueError:
            return False
        return (
            str(lease.get("owner_token", "")) == owner_token
            and str(lease.get("processing_key", "")) == processing_key
        )

    def _eval_snapshot(self, source_key: str, processing_key: str) -> str | None:
        if not self.data.get(source_key):
            self.data.pop(source_key, None)
            return None
        assert processing_key not in self.data
        self.data[processing_key] = self.data.pop(source_key)
        return processing_key

    def _eval_restore(self, processing_key: str, target_key: str) -> int:
        old_items = list(self.data.get(processing_key, []))
        new_items = list(self.data.get(target_key, []))
        if not old_items:
            self.data.pop(processing_key, None)
            return 0
        self.data[target_key] = [*old_items, *new_items]
        self.data.pop(processing_key, None)
        return len(old_items)

    async def delete(self, key: str) -> int:
        existed = (
            key in self.data
            or key in self.values
            or key in self.sets
            or key in self.zsets
            or key in self.hashes
        )
        self.data.pop(key, None)
        self.values.pop(key, None)
        self.sets.pop(key, None)
        self.zsets.pop(key, None)
        self.hashes.pop(key, None)
        return 1 if existed else 0

    async def exists(self, key: str) -> int:
        return (
            1
            if key in self.data
            or key in self.values
            or key in self.sets
            or key in self.zsets
            or key in self.hashes
            else 0
        )

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def zrevrange(self, key: str, start: int, stop: int) -> list[str]:
        members = sorted(
            self.zsets.get(key, {}),
            key=lambda member: (self.zsets[key][member], member),
            reverse=True,
        )
        return _redis_range(members, start, stop)

    async def scan_iter(self, *, match: str):
        prefix = match.removesuffix("*")
        all_keys: set[str] = set()
        all_keys.update(self.data.keys())
        all_keys.update(self.values.keys())
        all_keys.update(self.sets.keys())
        all_keys.update(self.zsets.keys())
        all_keys.update(self.hashes.keys())
        for key in sorted(all_keys):
            if key.startswith(prefix):
                yield key


def _build_message(index: int) -> MessageSchema:
    return MessageSchema(
        user_id=f"user-{index}",
        user_nickname=f"用户{index}",
        group_id="group-1",
        content=f"消息{index}",
        timestamp=float(index),
        message_id=f"msg-{index}",
    )


def _build_manager(monkeypatch: Any) -> RedisManager:
    config = KomariMemoryConfigSchema.model_construct()
    monkeypatch.setattr(
        redis_manager_module,
        "get_config",
        lambda: config,
    )
    manager = RedisManager(config)
    manager._redis = cast("Any", _FakeRedis())
    return manager


def _build_second_manager(
    monkeypatch: Any,
    shared_redis: _FakeRedis,
) -> RedisManager:
    config = KomariMemoryConfigSchema.model_construct()
    monkeypatch.setattr(redis_manager_module, "get_config", lambda: config)
    manager = RedisManager(config)
    manager._redis = cast("Any", shared_redis)
    return manager


def _get_fake_redis(manager: RedisManager) -> _FakeRedis:
    return cast("_FakeRedis", manager._redis)


def _capture_warning_logs(monkeypatch: Any) -> list[str]:
    logs: list[str] = []

    def _warning(message: str, *args: object) -> None:
        logs.append(message.format(*args))

    monkeypatch.setattr(redis_manager_module.logger, "warning", _warning)
    return logs


def test_initialize_uses_structured_redis_connection_parameters(
    monkeypatch: Any,
) -> None:
    captured_kwargs: dict[str, object] = {}

    class _Connection:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)
            self.pinged = False
            self.closed = False

        async def ping(self) -> bool:
            self.pinged = True
            return True

        async def aclose(self) -> None:
            self.closed = True

    connections: list[_Connection] = []

    def _connection_factory(**kwargs: object) -> _Connection:
        connection = _Connection(**kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(
        redis_manager_module,
        "get_shared_database_config",
        lambda: SimpleNamespace(
            redis_host="redis.internal",
            redis_port=6380,
            redis_password="p@ss:/?#word",
        ),
    )
    monkeypatch.setattr(
        redis_manager_module.aioredis,
        "Redis",
        _connection_factory,
    )
    manager = RedisManager(KomariMemoryConfigSchema(redis_db=6))

    asyncio.run(manager.initialize())

    connection = connections[0]
    assert connection.pinged is True
    assert captured_kwargs == {
        "host": "redis.internal",
        "port": 6380,
        "db": 6,
        "password": "p@ss:/?#word",
        "decode_responses": True,
        "encoding": "utf-8",
    }
    assert manager.redis is connection

    asyncio.run(manager.close())
    assert connection.closed is True


def test_initialize_closes_redis_client_when_ping_fails(monkeypatch: Any) -> None:
    class _Connection:
        def __init__(self) -> None:
            self.closed = False

        async def ping(self) -> bool:
            msg = "连接失败"
            raise ConnectionError(msg)

        async def aclose(self) -> None:
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(
        redis_manager_module,
        "get_shared_database_config",
        lambda: SimpleNamespace(
            redis_host="redis.internal",
            redis_port=6380,
            redis_password="secret",
        ),
    )
    monkeypatch.setattr(
        redis_manager_module.aioredis,
        "Redis",
        lambda **_kwargs: connection,
    )
    manager = RedisManager(KomariMemoryConfigSchema(redis_db=6))

    with pytest.raises(ConnectionError, match="连接失败"):
        asyncio.run(manager.initialize())

    assert connection.closed is True
    assert manager._redis is None


def test_proactive_reservation_is_atomic_for_concurrent_requests(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)

    async def _reserve_pair() -> list[str]:
        return list(
            await asyncio.gather(
                manager.reserve_proactive_reply(
                    "group-1",
                    "message-1",
                    max_per_hour=10,
                    reservation_ttl_seconds=360,
                ),
                manager.reserve_proactive_reply(
                    "group-1",
                    "message-2",
                    max_per_hour=10,
                    reservation_ttl_seconds=360,
                ),
            )
        )

    statuses = asyncio.run(_reserve_pair())

    assert statuses.count("reserved") == 1
    assert statuses.count("cooldown") == 1
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_proactive_reservation_release_restores_capacity(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)

    first = asyncio.run(
        manager.reserve_proactive_reply(
            "group-1",
            "message-1",
            max_per_hour=1,
            reservation_ttl_seconds=360,
        )
    )
    released = asyncio.run(
        manager.release_proactive_reply("group-1", "message-1")
    )
    second = asyncio.run(
        manager.reserve_proactive_reply(
            "group-1",
            "message-2",
            max_per_hour=1,
            reservation_ttl_seconds=360,
        )
    )

    assert first == "reserved"
    assert released is True
    assert second == "reserved"
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_confirmed_proactive_reservation_is_idempotent_and_rate_limited(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-1",
                max_per_hour=1,
                reservation_ttl_seconds=360,
            )
        )
        == "reserved"
    )
    asyncio.run(
        manager.confirm_proactive_reply(
            "group-1",
            "message-1",
            cooldown_seconds=300,
        )
    )
    asyncio.run(
        manager.confirm_proactive_reply(
            "group-1",
            "message-1",
            cooldown_seconds=300,
        )
    )
    assert asyncio.run(manager.release_proactive_reply("group-1", "message-1")) is False

    fake_redis.values.pop(RedisKeys.proactive_cooldown("group-1"), None)
    second = asyncio.run(
        manager.reserve_proactive_reply(
            "group-1",
            "message-2",
            max_per_hour=1,
            reservation_ttl_seconds=360,
        )
    )

    assert second == "rate_limited"
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_proactive_reservation_heartbeat_extends_pending_lease(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-1",
                max_per_hour=1,
                reservation_ttl_seconds=30,
            )
        )
        == "reserved"
    )
    original_expiry = fake_redis.zsets[RedisKeys.proactive_slots("group-1")][
        "pending:message-1"
    ]
    fake_redis.now_ms += 10_000

    renewed = asyncio.run(
        manager.renew_proactive_reply(
            "group-1",
            "message-1",
            reservation_ttl_seconds=30,
        )
    )

    assert renewed is True
    assert (
        fake_redis.zsets[RedisKeys.proactive_slots("group-1")][
            "pending:message-1"
        ]
        > original_expiry
    )


def test_chat_commit_redis_steps_are_idempotent(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    message = MessageSchema(
        user_id="bot",
        user_nickname="小鞠",
        group_id="group-1",
        content="只应写入一次",
        timestamp=12.5,
        message_id="bot-operation-1",
        is_bot=True,
    )

    first_message = asyncio.run(
        manager.push_message_once(
            "group-1",
            message,
            operation_id="operation-1",
        )
    )
    duplicate_message = asyncio.run(
        manager.push_message_once(
            "group-1",
            message,
            operation_id="operation-1",
        )
    )
    first_interaction = asyncio.run(
        manager.push_global_interaction_once(
            user_id="user-1",
            record={"event": "问候", "result": "回应", "emotion": "平静"},
            trigger_size=1,
            operation_id="operation-1",
        )
    )
    duplicate_interaction = asyncio.run(
        manager.push_global_interaction_once(
            user_id="user-1",
            record={"event": "问候", "result": "回应", "emotion": "平静"},
            trigger_size=1,
            operation_id="operation-1",
        )
    )

    assert first_message is True
    assert duplicate_message is False
    assert len(asyncio.run(manager.get_buffer("group-1"))) == 1
    assert first_interaction is True
    assert duplicate_interaction is False
    assert len(asyncio.run(manager.get_global_interaction_buffer("user-1"))) == 1
    fake_redis = _get_fake_redis(manager)
    assert "user-1" in fake_redis.sets[RedisKeys.GLOBAL_INTERACTION_PENDING]


def test_expired_proactive_reservation_is_pruned(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    fake_redis.now_ms = 100_000.0

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-1",
                max_per_hour=1,
                reservation_ttl_seconds=30,
            )
        )
        == "reserved"
    )
    fake_redis.values.pop(RedisKeys.proactive_cooldown("group-1"), None)
    fake_redis.now_ms = 131_000.0

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-2",
                max_per_hour=1,
                reservation_ttl_seconds=30,
            )
        )
        == "reserved"
    )
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_push_message_appends_messages_without_trimming(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)

    asyncio.run(manager.push_message("group-1", _build_message(1)))
    asyncio.run(manager.push_message("group-1", _build_message(2)))
    asyncio.run(manager.push_message("group-1", _build_message(3)))
    asyncio.run(manager.push_message("group-1", _build_message(4)))

    messages = asyncio.run(manager.get_buffer("group-1", limit=10))

    assert [msg.content for msg in messages] == ["消息1", "消息2", "消息3", "消息4"]

    fake_redis = _get_fake_redis(manager)
    assert redis_manager_module.RedisKeys.session_start("group-1") in fake_redis.values
    assert redis_manager_module.RedisKeys.last_message("group-1") in fake_redis.values


def test_get_buffer_returns_latest_window_in_time_order(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    key = redis_manager_module.RedisKeys.buffer("group-1")
    _get_fake_redis(manager).data[key] = [
        json.dumps(_build_message(index).__dict__, ensure_ascii=False)
        for index in range(1, 6)
    ]

    messages = asyncio.run(manager.get_buffer("group-1", limit=2))

    assert [msg.content for msg in messages] == ["消息4", "消息5"]


def test_push_global_interaction_triggers_pending_without_trimming(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)

    asyncio.run(
        manager.push_global_interaction(
            "u1",
            [{"event": f"事件{i}", "result": "回复", "emotion": "平静"} for i in range(21)],
            trigger_size=20,
        )
    )

    buffer_key = redis_manager_module.RedisKeys.global_interaction("u1")
    assert len(fake_redis.data[buffer_key]) == 21
    assert fake_redis.sets[redis_manager_module.RedisKeys.GLOBAL_INTERACTION_PENDING] == {
        "u1"
    }


def test_get_global_interaction_buffer_returns_tail_in_order(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    key = redis_manager_module.RedisKeys.global_interaction("u1")
    _get_fake_redis(manager).data[key] = [
        json.dumps({"event": f"事件{i}"}, ensure_ascii=False) for i in range(1, 5)
    ]

    records = asyncio.run(manager.get_global_interaction_buffer("u1", limit=2))

    assert records == [{"event": "事件3"}, {"event": "事件4"}]


def test_get_global_interaction_buffer_skips_invalid_json(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    key = redis_manager_module.RedisKeys.global_interaction("u1")
    secret_payload = '{"event":"绝不能出现在日志里"'
    _get_fake_redis(manager).data[key] = [
        secret_payload,
        json.dumps({"event": "有效"}, ensure_ascii=False),
        json.dumps([{"event": "列表会跳过"}], ensure_ascii=False),
    ]
    warning_logs = _capture_warning_logs(monkeypatch)

    records = asyncio.run(manager.get_global_interaction_buffer("u1", limit=10))

    assert records == [{"event": "有效"}]
    assert len(warning_logs) == 1
    assert secret_payload not in warning_logs[0]
    assert f"key={key}" in warning_logs[0]
    assert f"bytes={len(secret_payload.encode('utf-8'))}" in warning_logs[0]
    assert hashlib.sha256(secret_payload.encode()).hexdigest() in warning_logs[0]


def test_processing_conversation_json_error_log_is_redacted(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    processing_key = RedisKeys.buffer_processing("g1", "token")
    secret_payload = '{"content":"用户私密消息"'
    _get_fake_redis(manager).data[processing_key] = [secret_payload]
    warning_logs = _capture_warning_logs(monkeypatch)
    claim = asyncio.run(
        manager.claim_existing_conversation_processing(
            "g1",
            processing_key,
            "owner-1",
        )
    )

    messages = asyncio.run(
        manager.get_processing_conversation_buffer(
            "g1",
            processing_key,
            "owner-1",
        )
    )

    assert claim.status == "claimed"
    assert messages == []
    assert len(warning_logs) == 1
    assert secret_payload not in warning_logs[0]
    assert "context=conversation_processing" in warning_logs[0]
    assert f"key={processing_key}" in warning_logs[0]
    assert hashlib.sha256(secret_payload.encode()).hexdigest() in warning_logs[0]


def test_two_redis_clients_enforce_conversation_owner_and_expired_takeover(
    monkeypatch: Any,
) -> None:
    first = _build_manager(monkeypatch)
    shared_redis = _get_fake_redis(first)
    second = _build_second_manager(monkeypatch, shared_redis)
    shared_redis.data[RedisKeys.buffer("g1")] = [
        json.dumps(_build_message(1).__dict__, ensure_ascii=False)
    ]

    first_claim = asyncio.run(
        first.claim_conversation_buffer("g1", "owner-1", "snapshot-1")
    )
    second_claim = asyncio.run(
        second.claim_conversation_buffer("g1", "owner-2", "snapshot-2")
    )

    assert first_claim.status == "claimed"
    assert second_claim.status == "busy"
    assert second_claim.processing_key == first_claim.processing_key
    with pytest.raises(redis_manager_module.ConversationLeaseLostError):
        asyncio.run(
            second.get_processing_conversation_buffer(
                "g1",
                str(first_claim.processing_key),
                "owner-2",
            )
        )

    shared_redis.values.pop(RedisKeys.buffer_processing_lock("g1"))
    takeover = asyncio.run(
        second.claim_conversation_buffer("g1", "owner-2", "snapshot-2")
    )

    assert takeover.status == "claimed"
    assert takeover.processing_key == first_claim.processing_key
    assert not asyncio.run(
        first.renew_processing_conversation_lease(
            "g1",
            str(first_claim.processing_key),
            "owner-1",
        )
    )
    assert not asyncio.run(
        first.ack_processing_conversation_buffer(
            "g1",
            str(first_claim.processing_key),
            "owner-1",
        )
    )
    assert not asyncio.run(
        first.restore_processing_conversation_buffer(
            "g1",
            str(first_claim.processing_key),
            "owner-1",
        )
    )
    assert len(
        asyncio.run(
            second.get_processing_conversation_buffer(
                "g1",
                str(takeover.processing_key),
                "owner-2",
            )
        )
    ) == 1
    assert asyncio.run(
        second.ack_processing_conversation_buffer(
            "g1",
            str(takeover.processing_key),
            "owner-2",
        )
    )


def test_chunk_ledger_survives_owner_takeover_and_is_removed_on_ack(
    monkeypatch: Any,
) -> None:
    first = _build_manager(monkeypatch)
    shared_redis = _get_fake_redis(first)
    second = _build_second_manager(monkeypatch, shared_redis)
    shared_redis.data[RedisKeys.buffer("g1")] = [
        json.dumps(_build_message(1).__dict__, ensure_ascii=False)
    ]
    claim = asyncio.run(
        first.claim_conversation_buffer("g1", "owner-1", "snapshot-1")
    )
    processing_key = str(claim.processing_key)
    manifest = '{"chunk_count":1,"version":1}'

    stored = asyncio.run(
        first.initialize_conversation_chunk_manifest(
            group_id="g1",
            processing_key=processing_key,
            owner_token="owner-1",
            manifest_json=manifest,
        )
    )
    shared_redis.values.pop(RedisKeys.buffer_processing_lock("g1"))
    takeover = asyncio.run(
        second.claim_conversation_buffer("g1", "owner-2", "snapshot-2")
    )
    recovered = asyncio.run(
        second.get_conversation_chunk_state(
            group_id="g1",
            processing_key=processing_key,
            owner_token="owner-2",
            field="manifest",
        )
    )

    assert stored == manifest
    assert takeover.status == "claimed"
    assert recovered == manifest
    assert asyncio.run(
        second.ack_processing_conversation_buffer(
            "g1",
            processing_key,
            "owner-2",
        )
    )
    ledger_key = RedisKeys.buffer_processing_chunks("g1", "snapshot-1")
    assert ledger_key not in shared_redis.hashes


def test_conversation_dead_letter_is_queryable_and_requeues_atomically(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    old_message = _build_message(1)
    new_message = _build_message(2)
    fake_redis.data[RedisKeys.buffer("g1")] = [
        json.dumps(old_message.__dict__, ensure_ascii=False)
    ]
    fake_redis.values[RedisKeys.last_message("g1")] = "1.0"
    fake_redis.values[RedisKeys.session_start("g1")] = "1.0"

    claim = asyncio.run(
        manager.claim_conversation_buffer("g1", "owner-1", "snapshot-1")
    )
    processing_key = str(claim.processing_key)
    asyncio.run(
        manager.initialize_conversation_chunk_manifest(
            group_id="g1",
            processing_key=processing_key,
            owner_token="owner-1",
            manifest_json='{"version":1}',
        )
    )

    assert asyncio.run(
        manager.dead_letter_processing_conversation_buffer(
            "g1",
            processing_key,
            "owner-1",
            failure_code="RuntimeError",
            attempt_count=3,
        )
    )
    assert not asyncio.run(
        manager.ack_processing_conversation_buffer(
            "g1",
            processing_key,
            "owner-1",
        )
    )
    assert asyncio.run(manager.get_orphaned_conversation_processing_keys()) == []

    dead_letters = asyncio.run(manager.list_conversation_dead_letters())
    assert len(dead_letters) == 1
    assert dead_letters[0].group_id == "g1"
    assert dead_letters[0].snapshot_id == "snapshot-1"
    assert dead_letters[0].failure_code == "RuntimeError"
    assert dead_letters[0].attempt_count == 3
    assert dead_letters[0].message_count == 1
    assert dead_letters[0].chunk_state_count == 1

    fake_redis.data[RedisKeys.buffer("g1")] = [
        json.dumps(new_message.__dict__, ensure_ascii=False)
    ]
    restored_count = asyncio.run(
        manager.requeue_conversation_dead_letter(
            group_id="g1",
            snapshot_id="snapshot-1",
        )
    )
    restored_messages = asyncio.run(manager.get_buffer("g1"))

    assert restored_count == 1
    assert [message.message_id for message in restored_messages] == ["msg-1", "msg-2"]
    assert fake_redis.values[RedisKeys.session_start("g1")] == "1.0"
    assert fake_redis.values[RedisKeys.last_message("g1")] == "2.0"
    assert asyncio.run(manager.list_conversation_dead_letters()) == []
    assert (
        asyncio.run(
            manager.requeue_conversation_dead_letter(
                group_id="g1",
                snapshot_id="snapshot-1",
            )
        )
        is None
    )


def test_get_global_interaction_buffer_limit_zero_returns_empty(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)

    records = asyncio.run(manager.get_global_interaction_buffer("u1", limit=0))

    assert records == []


def test_global_interaction_requeue_keeps_old_and_new_buffer_order(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    buffer_key = redis_manager_module.RedisKeys.global_interaction("u1")
    fake_redis.data[buffer_key] = ["old-1", "old-2"]
    asyncio.run(manager.add_pending_interaction_summary("u1"))
    claimed = asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-1",
            lease_seconds=60,
        )
    )

    processing_key = asyncio.run(manager.snapshot_global_interactions("u1", "token"))

    assert processing_key == redis_manager_module.RedisKeys.global_interaction_processing(
        "u1",
        "token",
    )
    assert buffer_key not in fake_redis.data
    assert fake_redis.data[str(processing_key)] == ["old-1", "old-2"]

    fake_redis.data[buffer_key] = ["new-1"]
    requeued = asyncio.run(
        manager.requeue_processing_global_interactions(
            user_id="u1",
            owner_token="owner-1",
            processing_key=str(processing_key),
        )
    )

    assert claimed == ["u1"]
    assert requeued is True
    assert fake_redis.data[buffer_key] == ["old-1", "old-2", "new-1"]
    assert str(processing_key) not in fake_redis.data
    assert fake_redis.sets[RedisKeys.GLOBAL_INTERACTION_PENDING] == {"u1"}


def test_global_interaction_active_lease_cannot_be_claimed_twice(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    asyncio.run(manager.add_pending_interaction_summary("u1"))

    first = asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-1",
            lease_seconds=60,
        )
    )
    asyncio.run(manager.add_pending_interaction_summary("u1"))
    second = asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-2",
            lease_seconds=60,
        )
    )

    assert first == ["u1"]
    assert second == []


def test_global_interaction_lease_can_only_be_renewed_by_owner(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    asyncio.run(manager.add_pending_interaction_summary("u1"))
    asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-1",
            lease_seconds=60,
        )
    )
    fake_redis.now_ms += 50_000

    wrong_owner = asyncio.run(
        manager.renew_interaction_summary_lease(
            user_id="u1",
            owner_token="owner-2",
            lease_seconds=60,
        )
    )
    renewed = asyncio.run(
        manager.renew_interaction_summary_lease(
            user_id="u1",
            owner_token="owner-1",
            lease_seconds=60,
        )
    )
    fake_redis.now_ms += 20_000
    claimed = asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-2",
            lease_seconds=60,
        )
    )

    assert wrong_owner is False
    assert renewed is True
    assert claimed == []


def test_global_interaction_expired_lease_reuses_snapshot_and_rejects_old_owner(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    buffer_key = RedisKeys.global_interaction("u1")
    fake_redis.data[buffer_key] = ["old-1"]
    asyncio.run(manager.add_pending_interaction_summary("u1"))
    asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-1",
            lease_seconds=60,
        )
    )
    first_key = asyncio.run(manager.snapshot_global_interactions("u1", "token-1"))

    fake_redis.now_ms += 61_000
    claimed = asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-2",
            lease_seconds=60,
        )
    )
    second_key = asyncio.run(manager.snapshot_global_interactions("u1", "token-2"))
    old_ack = asyncio.run(
        manager.ack_processing_global_interactions(
            user_id="u1",
            owner_token="owner-1",
            processing_key=str(first_key),
        )
    )
    wrong_snapshot_ack = asyncio.run(
        manager.ack_processing_global_interactions(
            user_id="u1",
            owner_token="owner-2",
            processing_key="processing:wrong",
        )
    )
    new_ack = asyncio.run(
        manager.ack_processing_global_interactions(
            user_id="u1",
            owner_token="owner-2",
            processing_key=str(second_key),
        )
    )

    assert claimed == ["u1"]
    assert second_key == first_key
    assert old_ack is False
    assert wrong_snapshot_ack is False
    assert new_ack is True
    assert str(first_key) not in fake_redis.data


def test_global_interaction_new_buffer_remains_pending_until_lease_ack(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    asyncio.run(manager.add_pending_interaction_summary("u1"))
    asyncio.run(
        manager.claim_pending_interaction_summaries(
            owner_token="owner-1",
            lease_seconds=60,
        )
    )
    asyncio.run(manager.add_pending_interaction_summary("u1"))

    assert (
        asyncio.run(
            manager.claim_pending_interaction_summaries(
                owner_token="owner-2",
                lease_seconds=60,
            )
        )
        == []
    )
    assert asyncio.run(
        manager.ack_processing_global_interactions(
            user_id="u1",
            owner_token="owner-1",
            processing_key="",
        )
    )
    assert (
        asyncio.run(
            manager.claim_pending_interaction_summaries(
                owner_token="owner-2",
                lease_seconds=60,
            )
        )
        == ["u1"]
    )


def test_get_users_with_global_interaction_buffer_excludes_pending_and_processing(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    fake_redis.data[redis_manager_module.RedisKeys.global_interaction("u1")] = ["record"]
    fake_redis.data[redis_manager_module.RedisKeys.global_interaction("u2")] = []
    fake_redis.data[redis_manager_module.RedisKeys.GLOBAL_INTERACTION_PENDING] = ["u3"]
    fake_redis.data[
        redis_manager_module.RedisKeys.global_interaction_processing("u4", "token")
    ] = ["record"]

    users = asyncio.run(manager.get_users_with_global_interaction_buffer())

    assert users == ["u1"]


def test_get_orphaned_conversation_processing_keys_filters_active_locks(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    active_key = redis_manager_module.RedisKeys.buffer_processing("g1", "token-active")
    no_lock_key = redis_manager_module.RedisKeys.buffer_processing("g2", "token-orphan")
    stale_lock_key = redis_manager_module.RedisKeys.buffer_processing("g3", "token-stale")
    invalid_key = f"{redis_manager_module.RedisKeys.PREFIX}:buffer:processing:broken"
    fake_redis.data[active_key] = ["record"]
    fake_redis.data[no_lock_key] = ["record"]
    fake_redis.data[stale_lock_key] = ["record"]
    fake_redis.data[invalid_key] = ["record"]
    fake_redis.values[
        redis_manager_module.RedisKeys.buffer_processing_current("g1")
    ] = active_key
    fake_redis._set_conversation_lease(
        redis_manager_module.RedisKeys.buffer_processing_lock("g1"),
        "owner-active",
        active_key,
    )
    fake_redis.values[redis_manager_module.RedisKeys.buffer_processing_lock("g3")] = "other-key"

    orphaned = asyncio.run(manager.get_orphaned_conversation_processing_keys())

    assert orphaned == [("g2", no_lock_key), ("g3", stale_lock_key)]


def test_get_active_groups_excludes_processing_and_dead_letter_keys(
    monkeypatch: Any,
) -> None:
    """get_active_groups() 应排除 processing 系列键和 dead-letter 索引键。

    回归覆盖：此前 BUFFER_PROCESSING_DEAD_INDEX（ZSET）未被排除，
    被误认为群组 ID ``processing_dead_index``，下游 LLEN 触发 WRONGTYPE。
    """
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)

    # 真实群组缓冲键
    fake_redis.data[RedisKeys.buffer("10001")] = ["m1"]
    fake_redis.data[RedisKeys.buffer("10002")] = ["m1"]

    # 本次事故元凶 — ZSET 键，位于 zsets 存储
    fake_redis.zsets[RedisKeys.BUFFER_PROCESSING_DEAD_INDEX] = {}

    # 各 processing 前缀覆盖
    fake_redis.data[RedisKeys.buffer_processing("10001", "token")] = []  # processing:
    fake_redis.values[RedisKeys.buffer_processing_current("10001")] = ""  # processing_current:
    fake_redis.values[RedisKeys.buffer_processing_lock("10001")] = ""  # processing_lock:
    fake_redis.hashes[RedisKeys.buffer_processing_chunks("10001", "token")] = {}  # processing_chunks:
    fake_redis.values[
        RedisKeys.buffer_processing_meta_last_message("10001", "token")
    ] = ""  # processing_meta:
    fake_redis.hashes[
        RedisKeys.buffer_processing_dead("10001", "deadtoken")
    ] = {}  # processing_dead:

    result = asyncio.run(manager.get_active_groups())

    assert set(result) == {"10001", "10002"}
