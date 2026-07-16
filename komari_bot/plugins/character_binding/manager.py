"""角色名绑定管理器。"""

from __future__ import annotations

import asyncio
import json
import os
import unicodedata
from pathlib import Path
from tempfile import NamedTemporaryFile

from nonebot import logger

from komari_bot.common.project_paths import DATA_DIR

# 模块级单例实例
_manager_instance: CharacterBindingManager | None = None

MAX_CHARACTER_NAME_LENGTH = 64
DEFAULT_BINDING_FILE = DATA_DIR / "character_binding" / "bindings.json"
_EMPTY_NAME_ERROR = "角色名不能为空"
_NAME_TOO_LONG_ERROR = (
    f"角色名不能超过 {MAX_CHARACTER_NAME_LENGTH} 个 Unicode 字符"
)
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
    if any(
        unicodedata.category(char) in {"Cc", "Zl", "Zp"}
        for char in character_name
    ):
        raise CharacterNameValidationError(_UNSAFE_NAME_ERROR)
    return character_name


def get_manager() -> CharacterBindingManager:
    """获取管理器单例实例。

    Returns:
        管理器实例
    """
    global _manager_instance  # noqa: PLW0603
    if _manager_instance is None:
        _manager_instance = CharacterBindingManager()
    return _manager_instance


class CharacterBindingManager:
    """角色名绑定管理器。"""

    def __init__(self, binding_file: Path | None = None) -> None:
        """初始化绑定管理器。"""
        # 使用项目根目录下的独立 data 目录，不受进程工作目录影响。
        self.binding_file = binding_file or DEFAULT_BINDING_FILE
        self.binding_file.parent.mkdir(parents=True, exist_ok=True)
        self._bindings: dict[str, str] = {}
        self._binding_mtime_ns: int | None = None
        self._lock = asyncio.Lock()
        self._load_bindings()

    def _load_bindings(self) -> None:
        """从文件加载绑定数据。"""
        if self.binding_file.exists():
            try:
                with Path.open(self.binding_file, encoding="utf-8") as f:
                    self._bindings = json.load(f)
                self._binding_mtime_ns = self._get_binding_mtime_ns()
                logger.info(f"[CharacterBinding] 加载角色绑定: {len(self._bindings)} 条")
            except (OSError, json.JSONDecodeError):
                logger.warning("[CharacterBinding] 绑定文件加载失败", exc_info=True)
                self._bindings = {}
                self._binding_mtime_ns = None
        else:
            self._bindings = {}
            # 初始化时同步写入空文件
            try:
                self._write_bindings_atomically({})
                self._binding_mtime_ns = self._get_binding_mtime_ns()
                logger.info("[CharacterBinding] 初始化角色绑定文件")
            except BindingPersistenceError:
                logger.exception("[CharacterBinding] 初始化绑定文件失败")
                self._binding_mtime_ns = None

    def _get_binding_mtime_ns(self) -> int | None:
        try:
            return self.binding_file.stat().st_mtime_ns
        except OSError:
            return None

    def _write_bindings_atomically(self, bindings: dict[str, str]) -> None:
        """通过同目录临时文件原子替换绑定文件。"""
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.binding_file.parent,
                prefix=f".{self.binding_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(bindings, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            temporary_path.replace(self.binding_file)
        except (OSError, TypeError, UnicodeError) as exc:
            raise BindingPersistenceError("角色绑定保存失败") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        f"[CharacterBinding] 清理绑定临时文件失败: "
                        f"path={temporary_path}",
                        exc_info=True,
                    )

    async def _persist_bindings(self, bindings: dict[str, str]) -> None:
        """在线程中持久化不可变绑定快照。"""
        try:
            await asyncio.to_thread(self._write_bindings_atomically, bindings)
        except BindingPersistenceError:
            logger.exception("[CharacterBinding] 绑定文件保存失败")
            raise

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        """设置用户的角色名绑定。"""
        validated_name = validate_character_name(character_name)
        async with self._lock:
            updated_bindings = self._bindings.copy()
            updated_bindings[user_id] = validated_name
            await self._persist_bindings(updated_bindings)
            self._bindings = updated_bindings

        logger.info(
            f"[CharacterBinding] 绑定角色: user_id={user_id}, "
            f"name_length={len(validated_name)}"
        )

    async def remove_character_name(self, user_id: str) -> bool:
        """移除用户的角色名绑定。"""
        async with self._lock:
            if user_id not in self._bindings:
                return False

            updated_bindings = self._bindings.copy()
            del updated_bindings[user_id]
            await self._persist_bindings(updated_bindings)
            self._bindings = updated_bindings

        logger.info(f"[CharacterBinding] 解除绑定: {user_id}")
        return True

    def get_character_name(
        self,
        user_id: str,
        fallback_nickname: str | None = None,
    ) -> str:
        """获取用户的角色名。

        优先级: 绑定名称 > fallback_nickname > user_id

        Args:
            user_id: 用户ID
            fallback_nickname: 备用昵称(如QQ昵称)

        Returns:
            角色名称
        """
        # 1. 检查绑定
        if user_id in self._bindings:
            return self._bindings[user_id]

        # 2. 回退到昵称
        if fallback_nickname:
            return fallback_nickname

        # 3. 最后回退到user_id
        return user_id

    def list_bindings(self) -> dict[str, str]:
        """获取所有绑定。

        Returns:
            绑定字典 {user_id: character_name}
        """
        return self._bindings.copy()

    def has_binding(self, user_id: str) -> bool:
        """检查用户是否有绑定。

        Args:
            user_id: 用户ID

        Returns:
            是否存在绑定
        """
        return user_id in self._bindings

    async def refresh_if_file_updated(self) -> bool:
        """当绑定文件更新时重新加载绑定。"""
        current_mtime_ns = self._get_binding_mtime_ns()
        if current_mtime_ns == self._binding_mtime_ns:
            return False

        async with self._lock:
            latest_mtime_ns = self._get_binding_mtime_ns()
            if latest_mtime_ns == self._binding_mtime_ns:
                return False

            self._load_bindings()
            return True
