"""角色名绑定管理器。"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, ClassVar

from nonebot import logger

from komari_bot.common.project_paths import DATA_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator

# 模块级单例实例
_manager_instance: CharacterBindingManager | None = None

MAX_CHARACTER_NAME_LENGTH = 64
DEFAULT_REFRESH_INTERVAL_SECONDS = 1.0
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

    _local_file_locks: ClassVar[dict[Path, threading.Lock]] = {}
    _local_file_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        binding_file: Path | None = None,
        *,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        """初始化绑定管理器。"""
        # 使用项目根目录下的独立 data 目录，不受进程工作目录影响。
        self.binding_file = binding_file or DEFAULT_BINDING_FILE
        self.binding_file.parent.mkdir(parents=True, exist_ok=True)
        self._bindings: dict[str, str] = {}
        self._binding_mtime_ns: int | None = None
        self._binding_signature: tuple[int, int, int] | None = None
        self._refresh_interval_seconds = max(0.0, refresh_interval_seconds)
        self._next_refresh_at = 0.0
        self._state_lock = threading.RLock()
        self._lock = asyncio.Lock()
        self._load_bindings()

    def _load_bindings(self) -> None:
        """首次加载绑定；初始化文件时同样持有跨进程锁。"""
        try:
            with self._binding_file_guard():
                if self.binding_file.exists():
                    bindings = self._read_bindings_file()
                    initialized = False
                else:
                    self._write_bindings_atomically({})
                    bindings = {}
                    initialized = True
                signature = self._get_binding_signature()
        except BindingPersistenceError:
            logger.exception("[CharacterBinding] 绑定文件初始化或加载失败")
            bindings = {}
            signature = None
            initialized = False

        self._replace_snapshot(bindings, signature)
        if initialized:
            logger.info("[CharacterBinding] 初始化角色绑定文件")
        else:
            logger.info("[CharacterBinding] 加载角色绑定: {} 条", len(bindings))

    def _read_bindings_file(self) -> dict[str, str]:
        """读取并验证完整绑定快照。"""
        try:
            with Path.open(self.binding_file, encoding="utf-8") as binding_stream:
                raw = json.load(binding_stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise BindingPersistenceError("角色绑定读取失败") from exc
        if not isinstance(raw, dict):
            message = "角色绑定文件必须是 JSON 对象"
            raise BindingPersistenceError(message)

        bindings: dict[str, str] = {}
        for raw_user_id, raw_name in raw.items():
            if not isinstance(raw_user_id, str) or not isinstance(raw_name, str):
                raise BindingPersistenceError("角色绑定键和值必须是字符串")
            try:
                bindings[raw_user_id] = validate_character_name(raw_name)
            except CharacterNameValidationError as exc:
                raise BindingPersistenceError("角色绑定文件包含非法角色名") from exc
        return bindings

    def _get_binding_mtime_ns(self) -> int | None:
        try:
            return self.binding_file.stat().st_mtime_ns
        except OSError:
            return None

    def _get_binding_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.binding_file.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    @classmethod
    def _get_local_file_lock(cls, lock_path: Path) -> threading.Lock:
        canonical_path = lock_path.resolve()
        with cls._local_file_locks_guard:
            return cls._local_file_locks.setdefault(
                canonical_path,
                threading.Lock(),
            )

    @contextmanager
    def _binding_file_guard(self) -> Iterator[None]:
        """同进程线程锁与 flock 组合，覆盖多实例和多 worker。"""
        lock_path = self.binding_file.with_name(f".{self.binding_file.name}.lock")
        local_lock = self._get_local_file_lock(lock_path)
        try:
            with local_lock, Path.open(lock_path, mode="a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise BindingPersistenceError("角色绑定文件锁获取失败") from exc

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

    def _replace_snapshot(
        self,
        bindings: dict[str, str],
        signature: tuple[int, int, int] | None,
    ) -> None:
        """原子替换进程内不可变快照及文件版本。"""
        with self._state_lock:
            self._bindings = dict(bindings)
            self._binding_signature = signature
            self._binding_mtime_ns = signature[0] if signature is not None else None
            self._next_refresh_at = (
                time.monotonic() + self._refresh_interval_seconds
            )

    def _set_character_name_locked(
        self,
        user_id: str,
        character_name: str,
    ) -> tuple[dict[str, str], tuple[int, int, int] | None]:
        with self._binding_file_guard():
            bindings = (
                self._read_bindings_file() if self.binding_file.exists() else {}
            )
            bindings[user_id] = character_name
            self._write_bindings_atomically(bindings)
            return bindings, self._get_binding_signature()

    def _remove_character_name_locked(
        self,
        user_id: str,
    ) -> tuple[dict[str, str], tuple[int, int, int] | None, bool]:
        with self._binding_file_guard():
            bindings = (
                self._read_bindings_file() if self.binding_file.exists() else {}
            )
            removed = bindings.pop(user_id, None) is not None
            if removed:
                self._write_bindings_atomically(bindings)
            return bindings, self._get_binding_signature(), removed

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        """设置用户的角色名绑定。"""
        validated_name = validate_character_name(character_name)
        async with self._lock:
            try:
                bindings, signature = await asyncio.to_thread(
                    self._set_character_name_locked,
                    user_id,
                    validated_name,
                )
            except BindingPersistenceError:
                logger.exception("[CharacterBinding] 绑定文件保存失败")
                raise
            self._replace_snapshot(bindings, signature)

        logger.info(
            f"[CharacterBinding] 绑定角色: user_id={user_id}, "
            f"name_length={len(validated_name)}"
        )

    async def remove_character_name(self, user_id: str) -> bool:
        """移除用户的角色名绑定。"""
        async with self._lock:
            try:
                bindings, signature, removed = await asyncio.to_thread(
                    self._remove_character_name_locked,
                    user_id,
                )
            except BindingPersistenceError:
                logger.exception("[CharacterBinding] 绑定文件保存失败")
                raise
            self._replace_snapshot(bindings, signature)
            if not removed:
                return False

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
        self._refresh_for_read()
        # 1. 检查绑定
        with self._state_lock:
            character_name = self._bindings.get(user_id)
        if character_name is not None:
            return character_name

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
        self._refresh_for_read()
        with self._state_lock:
            return self._bindings.copy()

    def has_binding(self, user_id: str) -> bool:
        """检查用户是否有绑定。

        Args:
            user_id: 用户ID

        Returns:
            是否存在绑定
        """
        self._refresh_for_read()
        with self._state_lock:
            return user_id in self._bindings

    def _refresh_from_disk_if_updated(self, *, force: bool) -> bool:
        now = time.monotonic()
        with self._state_lock:
            if not force and now < self._next_refresh_at:
                return False
            self._next_refresh_at = now + self._refresh_interval_seconds
            previous_signature = self._binding_signature

        signature = self._get_binding_signature()
        if signature == previous_signature:
            return False
        try:
            bindings = self._read_bindings_file()
        except BindingPersistenceError:
            logger.exception(
                "[CharacterBinding] 外部绑定文件刷新失败，继续使用旧快照"
            )
            return False
        self._replace_snapshot(bindings, signature)
        return True

    def _refresh_for_read(self) -> None:
        """同步读路径按有界间隔检查外部文件版本。"""
        self._refresh_from_disk_if_updated(force=False)

    async def refresh_if_file_updated(self) -> bool:
        """当绑定文件更新时重新加载绑定。"""
        async with self._lock:
            return await asyncio.to_thread(
                self._refresh_from_disk_if_updated,
                force=True,
            )
