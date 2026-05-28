"""komari_custom Redis 编辑会话管理。"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal

import redis.asyncio as aioredis

from komari_bot.common.database_config import get_shared_database_config

from .models import SessionData, UndoRecord

SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_UNDO_RECORDS = 20


class CustomSessionManager:
    """基于 Redis 的提案编辑会话管理器。"""

    _redis_client: ClassVar[aioredis.Redis | None] = None

    def __init__(self, config_manager: Any) -> None:
        self.config_manager = config_manager

    async def close(self) -> None:
        """关闭共享 Redis 连接。"""
        if self.__class__._redis_client is not None:
            await self.__class__._redis_client.close()
            self.__class__._redis_client = None

    async def _get_client(self) -> aioredis.Redis:
        if self.__class__._redis_client is None:
            config = self.config_manager.get()
            db_config = get_shared_database_config()
            password_part = (
                f":{db_config.redis_password}@" if db_config.redis_password else ""
            )
            redis_url = (
                f"redis://{password_part}{db_config.redis_host}:"
                f"{db_config.redis_port}/{config.redis_db}"
            )
            self.__class__._redis_client = await aioredis.from_url(
                redis_url,
                decode_responses=True,
                encoding="utf-8",
            )
        return self.__class__._redis_client

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

    async def save_session(self, group_id: int, user_id: str, session: SessionData) -> None:
        """保存会话并刷新 TTL。"""
        client = await self._get_client()
        await client.set(
            self._key(group_id, user_id),
            session.model_dump_json(),
            ex=SESSION_TTL_SECONDS,
        )

    async def delete_session(self, group_id: int, user_id: str) -> None:
        """删除会话。"""
        client = await self._get_client()
        await client.delete(self._key(group_id, user_id))

    async def create_session(
        self,
        group_id: int,
        user_id: str,
        *,
        title: str = "",
        prompt_message_id: int | None = None,
    ) -> SessionData:
        """创建新会话。"""
        session = SessionData(title=title)
        if prompt_message_id is not None:
            session.prompt_message_ids.append(prompt_message_id)
        await self.save_session(group_id, user_id, session)
        return session

    async def remember_prompt_message(
        self,
        group_id: int,
        user_id: str,
        message_id: int | None,
    ) -> None:
        """记录 bot 引导消息 ID，用于回复追加判定。"""
        if message_id is None:
            return
        session = await self.get_session(group_id, user_id)
        if session is None:
            return
        if message_id not in session.prompt_message_ids:
            session.prompt_message_ids.append(message_id)
        await self.save_session(group_id, user_id, session)

    async def is_prompt_reply(self, group_id: int, user_id: str, message_id: int) -> bool:
        """判断消息是否回复了当前用户会话中的引导消息。"""
        session = await self.get_session(group_id, user_id)
        if session is None or session.phase not in {"title", "content"}:
            return False
        return message_id in session.prompt_message_ids

    async def append_text(self, group_id: int, user_id: str, text: str) -> SessionData:
        """向当前阶段字段追加文本。"""
        session = await self._require_session(group_id, user_id)
        field = session.current_field()
        old_value = getattr(session, field)
        new_value = f"{old_value}\n{text}".strip() if old_value else text.strip()
        setattr(session, field, new_value)
        self._push_undo(session, UndoRecord(action="append", field=field, text=text))
        await self.save_session(group_id, user_id, session)
        return session

    async def replace_text(
        self,
        group_id: int,
        user_id: str,
        old: str,
        new: str,
    ) -> SessionData:
        """替换当前字段文本。old 为空时表示全量替换。"""
        session = await self._require_session(group_id, user_id)
        field = session.current_field()
        current = getattr(session, field)
        if old:
            if old not in current:
                msg = "当前字段里没有找到要替换的文本"
                raise ValueError(msg)
            updated = current.replace(old, new, 1)
        else:
            updated = new
        setattr(session, field, updated.strip())
        self._push_undo(
            session,
            UndoRecord(action="replace", field=field, old=current, new=updated.strip()),
        )
        await self.save_session(group_id, user_id, session)
        return session

    async def delete_text(self, group_id: int, user_id: str, text: str) -> SessionData:
        """删除当前字段中的指定文本；text 为空时清空字段。"""
        session = await self._require_session(group_id, user_id)
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
        self._push_undo(session, UndoRecord(action="delete", field=field, text=deleted))
        await self.save_session(group_id, user_id, session)
        return session

    async def undo(self, group_id: int, user_id: str) -> SessionData | None:
        """撤销最近一次编辑。"""
        session = await self._require_session(group_id, user_id)
        if not session.undo_stack:
            return None
        record = session.undo_stack.pop()
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
        setattr(session, record.field, updated)
        await self.save_session(group_id, user_id, session)
        return session

    async def set_phase(
        self,
        group_id: int,
        user_id: str,
        phase: Literal["title", "content", "review"],
    ) -> SessionData:
        """推进会话阶段。"""
        session = await self._require_session(group_id, user_id)
        session.phase = phase
        await self.save_session(group_id, user_id, session)
        return session

    async def _require_session(self, group_id: int, user_id: str) -> SessionData:
        session = await self.get_session(group_id, user_id)
        if session is None:
            msg = "没有正在编辑的提案会话"
            raise ValueError(msg)
        return session

    @staticmethod
    def _push_undo(session: SessionData, record: UndoRecord) -> None:
        session.undo_stack.append(record)
        if len(session.undo_stack) > MAX_UNDO_RECORDS:
            session.undo_stack = session.undo_stack[-MAX_UNDO_RECORDS:]


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
