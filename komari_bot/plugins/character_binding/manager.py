"""角色名绑定管理器。"""

from __future__ import annotations

import asyncio
import unicodedata

from nonebot import logger

from .database import CharacterBindingDB

MAX_CHARACTER_NAME_LENGTH = 64
_EMPTY_NAME_ERROR = "角色名不能为空"
_NAME_TOO_LONG_ERROR = f"角色名不能超过 {MAX_CHARACTER_NAME_LENGTH} 个 Unicode 字符"
_UNSAFE_NAME_ERROR = "角色名不能包含换行或控制字符"


class CharacterNameValidationError(ValueError):
    """角色名不满足输入约束。"""


class BindingPersistenceError(RuntimeError):
    """角色绑定无法安全持久化。"""


def validate_character_name(character_name: str) -> str:
    """校验角色名并返回原值。"""
    if not character_name.strip():
        raise CharacterNameValidationError(_EMPTY_NAME_ERROR)
    if len(character_name) > MAX_CHARACTER_NAME_LENGTH:
        raise CharacterNameValidationError(_NAME_TOO_LONG_ERROR)
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in character_name):
        raise CharacterNameValidationError(_UNSAFE_NAME_ERROR)
    return character_name


class PluginState:
    """character_binding 模块级状态。"""

    def __init__(self) -> None:
        self.manager: CharacterBindingManager | None = None


state = PluginState()


def get_manager() -> CharacterBindingManager:
    """获取管理器单例实例。"""
    if state.manager is None:
        state.manager = CharacterBindingManager()
    return state.manager


class CharacterBindingManager:
    """角色名绑定管理器。"""

    def __init__(self) -> None:
        self._database = CharacterBindingDB()
        self._bindings: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """初始化数据库并发布完整内存快照。

        PostgreSQL 不可用时保留空快照并降级启动，后续写入会返回持久化错误。
        """
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                await self._database.initialize()
                bindings = await self._database.load_all()
            except Exception as error:
                await self._close_database_after_failure()
                self._bindings = {}
                logger.error(
                    "[CharacterBinding] PostgreSQL 初始化失败，使用空绑定快照降级: "
                    "error_type={}",
                    type(error).__name__,
                )
                return

            self._bindings = dict(bindings)
            self._initialized = True
            logger.info("[CharacterBinding] 加载角色绑定: {} 条", len(bindings))

    async def _close_database_after_failure(self) -> None:
        try:
            await self._database.close()
        except Exception:
            logger.exception("[CharacterBinding] 初始化失败后的数据库清理失败")

    async def close(self) -> None:
        """清空全局引用与快照，并释放数据库连接池租约。"""
        if state.manager is self:
            state.manager = None

        async with self._lock:
            self._bindings = {}
            self._initialized = False
            await self._database.close()

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        """设置用户的角色名绑定。"""
        validated_name = validate_character_name(character_name)
        async with self._lock:
            try:
                await self._database.upsert(user_id, validated_name)
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 角色绑定写入失败: user_id={} error_type={}",
                    user_id,
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存失败") from error

            bindings = dict(self._bindings)
            bindings[user_id] = validated_name
            self._bindings = bindings

        logger.info(
            "[CharacterBinding] 绑定角色: user_id={}, name_length={}",
            user_id,
            len(validated_name),
        )

    async def remove_character_name(self, user_id: str) -> bool:
        """移除用户的角色名绑定。"""
        async with self._lock:
            try:
                removed = await self._database.delete(user_id)
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 角色绑定删除失败: user_id={} error_type={}",
                    user_id,
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存失败") from error

            if not removed:
                return False

            bindings = dict(self._bindings)
            bindings.pop(user_id, None)
            self._bindings = bindings

        logger.info("[CharacterBinding] 解除绑定: {}", user_id)
        return True

    def get_character_name(
        self,
        user_id: str,
        fallback_nickname: str | None = None,
    ) -> str:
        """按绑定名称、备用昵称、用户 ID 的优先级返回角色名。"""
        character_name = self._bindings.get(user_id)
        if character_name is not None:
            return character_name
        if fallback_nickname:
            return fallback_nickname
        return user_id

    def list_bindings(self) -> dict[str, str]:
        """获取当前进程内的全部绑定快照副本。"""
        return dict(self._bindings)

    def has_binding(self, user_id: str) -> bool:
        """检查当前快照中是否存在用户绑定。"""
        return user_id in self._bindings


__all__ = [
    "MAX_CHARACTER_NAME_LENGTH",
    "BindingPersistenceError",
    "CharacterBindingManager",
    "CharacterNameValidationError",
    "get_manager",
    "validate_character_name",
]
