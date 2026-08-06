"""komari_custom Redis 编辑会话管理。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import redis.asyncio as aioredis

from komari_bot.common.content_budget import (
    PROPOSAL_CONTENT_TEXT_BUDGET,
    TITLE_TEXT_BUDGET,
    normalize_required_text,
)
from komari_bot.common.redis_config import get_shared_redis_config

from .models import SessionData, UndoRecord

if TYPE_CHECKING:
    from collections.abc import Callable

SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_UNDO_RECORDS = 20
MAX_SESSION_CAS_ATTEMPTS = 8

_SESSION_CAS_SCRIPT = """
-- custom_session_cas_v1
local key = KEYS[1]
local expected = ARGV[1]
local replacement = ARGV[2]
local ttl_seconds = tonumber(ARGV[3])
local expect_missing = ARGV[4] == "1"
local current = redis.call("GET", key)

if expect_missing then
    if current then
        return 0
    end
elseif current ~= expected then
    return 0
end

redis.call("SET", key, replacement, "EX", ttl_seconds)
return 1
"""


class CustomSessionManager:
    """基于 Redis 的提案编辑会话管理器。"""

    _redis_client: ClassVar[aioredis.Redis | None] = None
    _redis_client_lock: ClassVar[asyncio.Lock | None] = None
    _redis_client_lock_loop: ClassVar[asyncio.AbstractEventLoop | None] = None

    def __init__(self, config_manager: Any) -> None:
        self.config_manager = config_manager

    async def close(self) -> None:
        """关闭共享 Redis 连接。"""
        async with self._get_client_lock():
            client = self.__class__._redis_client
            self.__class__._redis_client = None
            if client is not None:
                await client.aclose()

    async def _get_client(self) -> aioredis.Redis:
        if self.__class__._redis_client is None:
            async with self._get_client_lock():
                if self.__class__._redis_client is None:
                    config = await self.config_manager.get_async()
                    db_config = get_shared_redis_config()
                    self.__class__._redis_client = aioredis.Redis(
                        host=db_config.redis_host,
                        port=db_config.redis_port,
                        db=config.redis_db,
                        password=db_config.redis_password or None,
                        decode_responses=True,
                        encoding="utf-8",
                    )
        return self.__class__._redis_client

    @classmethod
    def _get_client_lock(cls) -> asyncio.Lock:
        """获取绑定到当前事件循环的共享客户端初始化锁。"""
        loop = asyncio.get_running_loop()
        if cls._redis_client_lock is None or cls._redis_client_lock_loop is not loop:
            cls._redis_client_lock = asyncio.Lock()
            cls._redis_client_lock_loop = loop
        return cls._redis_client_lock

    @staticmethod
    def _key(group_id: int, user_id: str) -> str:
        return f"custom:session:{group_id}:{user_id}"

    async def get_session(self, group_id: int, user_id: str) -> SessionData | None:
        """读取会话。"""
        client = await self._get_client()
        raw = await client.get(self._key(group_id, user_id))
        if raw is None:
            return None
        return SessionData.model_validate_json(str(raw))

    async def _compare_and_set_session(
        self,
        *,
        group_id: int,
        user_id: str,
        expected_raw: str | None,
        session: SessionData,
    ) -> bool:
        """仅当 Redis 中仍是读取到的版本时替换会话。"""
        client = await self._get_client()
        result = await client.execute_command(
            "EVAL",
            _SESSION_CAS_SCRIPT,
            1,
            self._key(group_id, user_id),
            expected_raw or "",
            session.model_dump_json(),
            SESSION_TTL_SECONDS,
            "1" if expected_raw is None else "0",
        )
        if result is None:
            return False
        return int(result) == 1

    async def _mutate_session(
        self,
        group_id: int,
        user_id: str,
        mutation: Callable[[SessionData], SessionData | None],
    ) -> SessionData | None:
        """基于最新 Redis 值重放变换，直到 CAS 成功或达到冲突上限。"""
        client = await self._get_client()
        key = self._key(group_id, user_id)
        for _ in range(MAX_SESSION_CAS_ATTEMPTS):
            raw = await client.get(key)
            if raw is None:
                msg = "没有正在编辑的提案会话"
                raise ValueError(msg)
            expected_raw = str(raw)
            current = SessionData.model_validate_json(expected_raw)
            updated = mutation(current)
            if updated is None:
                return None
            updated.version = current.version + 1
            if await self._compare_and_set_session(
                group_id=group_id,
                user_id=user_id,
                expected_raw=expected_raw,
                session=updated,
            ):
                return updated
        msg = "提案会话正在被其他请求修改，请重试"
        raise SessionConflictError(msg)

    async def delete_session(self, group_id: int, user_id: str) -> None:
        """删除会话。"""
        client = await self._get_client()
        await client.delete(self._key(group_id, user_id))

    async def remember_publication_message_id(
        self,
        group_id: int,
        user_id: str,
        message_id: int,
    ) -> SessionData:
        """在提案发布完成前暂存平台消息 ID，供数据库失败后恢复。"""
        def _remember(session: SessionData) -> SessionData:
            session.publication_message_id = message_id
            return session

        session = await self._mutate_session(group_id, user_id, _remember)
        if session is None:
            raise AssertionError
        return session

    async def create_session(
        self,
        group_id: int,
        user_id: str,
        *,
        title: str = "",
    ) -> SessionData:
        """创建新会话。"""
        normalized_title = (
            self._validate_field_text("title", title) if title.strip() else ""
        )
        client = await self._get_client()
        key = self._key(group_id, user_id)
        for _ in range(MAX_SESSION_CAS_ATTEMPTS):
            raw = await client.get(key)
            expected_raw = str(raw) if raw is not None else None
            current_version = (
                SessionData.model_validate_json(expected_raw).version
                if expected_raw is not None
                else 0
            )
            session = SessionData(
                version=current_version + 1,
                title=normalized_title,
            )
            if await self._compare_and_set_session(
                group_id=group_id,
                user_id=user_id,
                expected_raw=expected_raw,
                session=session,
            ):
                return session
        msg = "提案会话正在被其他请求修改，请重试"
        raise SessionConflictError(msg)

    async def append_text(self, group_id: int, user_id: str, text: str) -> SessionData:
        """向当前阶段字段追加文本。"""
        def _append(session: SessionData) -> SessionData:
            if session.phase not in {"title", "content"}:
                msg = "当前阶段不能追加内容"
                raise ValueError(msg)
            field = session.current_field()
            old_value = getattr(session, field)
            new_value = f"{old_value}\n{text}".strip() if old_value else text.strip()
            new_value = self._validate_field_text(field, new_value)
            setattr(session, field, new_value)
            self._push_undo(
                session,
                UndoRecord(action="append", field=field, text=text),
            )
            return session

        session = await self._mutate_session(group_id, user_id, _append)
        if session is None:
            raise AssertionError
        return session

    async def replace_text(
        self,
        group_id: int,
        user_id: str,
        old: str,
        new: str,
    ) -> SessionData:
        """替换当前字段文本。old 为空时表示全量替换。"""
        def _replace(session: SessionData) -> SessionData:
            field = session.current_field()
            current = getattr(session, field)
            if old:
                if old not in current:
                    msg = "当前字段里没有找到要替换的文本"
                    raise ValueError(msg)
                updated = current.replace(old, new, 1)
            else:
                updated = new
            updated = self._validate_field_text(field, updated)
            setattr(session, field, updated)
            self._push_undo(
                session,
                UndoRecord(
                    action="replace",
                    field=field,
                    old=current,
                    new=updated,
                ),
            )
            return session

        session = await self._mutate_session(group_id, user_id, _replace)
        if session is None:
            raise AssertionError
        return session

    async def delete_text(self, group_id: int, user_id: str, text: str) -> SessionData:
        """删除当前字段中的指定文本；text 为空时清空字段。"""
        def _delete(session: SessionData) -> SessionData:
            field = session.current_field()
            current = getattr(session, field)
            if text:
                if text not in current:
                    msg = "当前字段里没有找到要删除的文本"
                    raise ValueError(msg)
                updated = current.replace(text, "", 1).strip()
                deleted = text
            else:
                updated = ""
                deleted = current
            setattr(session, field, updated)
            self._push_undo(
                session,
                UndoRecord(action="delete", field=field, text=deleted),
            )
            return session

        session = await self._mutate_session(group_id, user_id, _delete)
        if session is None:
            raise AssertionError
        return session

    async def undo(self, group_id: int, user_id: str) -> SessionData | None:
        """撤销最近一次编辑。"""
        def _undo(session: SessionData) -> SessionData | None:
            if not session.undo_stack:
                return None
            record = session.undo_stack[-1]
            current = getattr(session, record.field)
            match record.action:
                case "append":
                    text = record.text or ""
                    updated = current.removesuffix(text).strip()
                case "replace":
                    updated = record.old or ""
                case "delete":
                    text = record.text or ""
                    updated = f"{current}\n{text}".strip() if current else text
            if updated:
                updated = self._validate_field_text(record.field, updated)
            session.undo_stack.pop()
            setattr(session, record.field, updated)
            return session

        return await self._mutate_session(group_id, user_id, _undo)

    async def set_phase(
        self,
        group_id: int,
        user_id: str,
        phase: Literal["title", "content", "review"],
    ) -> SessionData:
        """推进会话阶段。"""
        def _set_phase(session: SessionData) -> SessionData:
            if phase == "content":
                self._validate_field_text("title", session.title)
            elif phase == "review":
                self._validate_field_text("content", session.content)
            session.phase = phase
            return session

        session = await self._mutate_session(group_id, user_id, _set_phase)
        if session is None:
            raise AssertionError
        return session

    @staticmethod
    def _push_undo(session: SessionData, record: UndoRecord) -> None:
        session.undo_stack.append(record)
        if len(session.undo_stack) > MAX_UNDO_RECORDS:
            session.undo_stack = session.undo_stack[-MAX_UNDO_RECORDS:]

    @staticmethod
    def _validate_field_text(
        field: Literal["title", "content"],
        value: str,
    ) -> str:
        """按当前编辑字段应用对应内容预算。"""
        if field == "title":
            return normalize_required_text(
                value,
                label="提案标题",
                budget=TITLE_TEXT_BUDGET,
            )
        return normalize_required_text(
            value,
            label="提案正文",
            budget=PROPOSAL_CONTENT_TEXT_BUDGET,
        )


class SessionConflictError(RuntimeError):
    """编辑会话在有限次 CAS 重放后仍持续冲突。"""


def split_replace_args(text: str) -> tuple[str, str]:
    """解析 replace 参数，使用 JSON 数组时支持包含空格的文本。"""
    stripped = text.strip()
    if not stripped:
        return "", ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parts = stripped.split(maxsplit=1)
        if len(parts) == 1:
            return "", parts[0]
        return parts[0], parts[1]
    if isinstance(parsed, list) and len(parsed) == 2:
        return str(parsed[0]), str(parsed[1])
    return "", stripped
