"""通用配置管理器。

提供基于 PostgreSQL 的插件动态配置管理。PG / Redis 引导配置由
NoneBot dotenv 或进程环境变量提供，不进入本管理器。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, Never

from nonebot import get_plugin_config, logger

from .storage import StoredConfig, get_config_storage

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from pydantic import BaseModel

_CONFIG_UPDATE_MAX_ATTEMPTS = 3
_DEFAULT_CONFIG_MAX_STALENESS_SECONDS = 1.0
_SECURITY_CONFIG_MAX_STALENESS_SECONDS = 0.25


class ConfigUpdateConflictError(RuntimeError):
    """配置在连续重试期间仍被其他进程修改。"""


@dataclass(frozen=True, slots=True)
class _ConfigSyncResult:
    """一次配置存储归一化的结果。"""

    normalized_data: dict[str, Any]
    added_keys: set[str]
    removed_keys: set[str]
    value_changed: bool
    changed: bool


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """进程内不可变的版本化配置快照。

    原子包含 ``value / revision / updated_at`` 三元组：整体为 frozen/slots
    值对象，manager 以单次引用替换方式发布，并发读取只能观察到完整旧快照
    或完整新快照，不会撕裂。

    ``value`` 运行时始终是 config_schema 校验通过的 BaseModel 实例，绝不暴露
    存储 dict；静态类型标注为 Any 以兼容不同插件 Schema。
    """

    value: Any
    revision: int
    updated_at: datetime


class ConfigManager:
    """通用配置管理器。

    提供：
    - 从 PostgreSQL 读取和持久化插件动态配置
    - 首次缺失时从 .env / schema 默认值初始化并写入 PostgreSQL
    - 线程安全的配置访问
    - 支持自定义配置 Schema（任何 BaseModel 子类）
    """

    def __init__(
        self,
        plugin_name: str,
        config_schema: type[BaseModel],
        *,
        env_config_schema: type[BaseModel] | None = None,
    ) -> None:
        """初始化配置管理器。"""
        self._plugin_name = plugin_name
        self._config_schema = config_schema
        self._env_config_schema = env_config_schema or config_schema
        self._env_config: Any | None = None
        self._dynamic_config: BaseModel | None = None
        self._last_loaded_at: datetime | None = None
        self._revision: int | None = None
        self._last_revision_checked_at = 0.0
        self._max_staleness_seconds = (
            _SECURITY_CONFIG_MAX_STALENESS_SECONDS
            if plugin_name == "komari_management"
            else _DEFAULT_CONFIG_MAX_STALENESS_SECONDS
        )
        self._watcher_registered = False
        self._snapshot: ConfigSnapshot | None = None
        self._snapshot_listeners: list[Callable[[ConfigSnapshot], None]] = []
        self._state_lock = RLock()
        self._sync_lock = RLock()
        self._async_lock = asyncio.Lock()

        logger.info(f"配置管理器已初始化 [{plugin_name}], 配置源: {self.config_source}")

    @property
    def config_source(self) -> str:
        """获取配置来源描述。"""
        table = getattr(self._config_schema, "__table__", None)
        table_name = getattr(table, "name", None)
        if isinstance(table_name, str) and table_name:
            return f"postgresql:{table_name}"
        return f"postgresql:{self._plugin_name}"

    @property
    def config_file(self) -> str:
        """兼容旧管理 API 的配置来源属性。"""
        return self.config_source

    def _get_env_config(self) -> Any:
        """获取环境配置（延迟加载）。"""
        if self._env_config is None:
            self._env_config = get_plugin_config(self._env_config_schema)
        return self._env_config

    def initialize(self) -> BaseModel:
        """从 PostgreSQL 或 .env 初始化配置。"""
        self._ensure_watcher_registered()
        with self._sync_lock:
            if self._dynamic_config is not None:
                return self._dynamic_config

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                msg = "事件循环内禁止同步初始化配置，请使用 initialize_async()"
                raise RuntimeError(msg)

            stored = get_config_storage().fetch(self._plugin_name)
            if stored is not None:
                _, stored = self._load_and_sync_stored_config(stored)
                config = self._cache_stored_config(stored)
                logger.info(f"[{self._plugin_name}] 已从 PostgreSQL 加载配置")
                return config

            config = self._initialize_from_env()
            self._save_to_pg(config)
            logger.info(
                f"[{self._plugin_name}] 已从 .env 尝试初始化配置，"
                "并采用 PostgreSQL 最终快照"
            )
            assert self._dynamic_config is not None
            return self._dynamic_config

    def _ensure_watcher_registered(self) -> None:
        """向存储层注册一次不可变快照订阅。"""
        with self._state_lock:
            if self._watcher_registered:
                return
            storage = get_config_storage()
            register = getattr(storage, "register_watcher", None)
            if callable(register):
                register(
                    self._plugin_name,
                    self._accept_external_snapshot,
                    max_staleness_seconds=self._max_staleness_seconds,
                )
            self._watcher_registered = True

    def _accept_external_snapshot(self, stored: StoredConfig) -> None:
        """从监听线程原子接纳更高 revision 的配置快照。"""
        try:
            self._config_schema(**stored.config_data)
        except Exception as exc:
            logger.warning(
                "[{}] 忽略无法通过 Schema 校验的外部配置快照: revision={}, error={}",
                self._plugin_name,
                stored.revision,
                type(exc).__name__,
            )
            return
        self._cache_stored_config(stored)

    def _initialize_from_env(self) -> BaseModel:
        """从 .env 值创建初始配置。"""
        env = self._get_env_config()
        env_dict = env.model_dump() if hasattr(env, "model_dump") else dict(env)
        return self._config_schema(**env_dict)

    def _save_to_pg(self, config: BaseModel) -> StoredConfig:
        """仅在记录缺失时写入初始配置，绝不覆盖并发创建的配置。"""
        storage = get_config_storage()
        stored = storage.insert_if_absent(
            plugin_name=self._plugin_name,
            config=config,
        )
        self._cache_stored_config(stored)
        logger.debug(f"[{self._plugin_name}] 配置已保存到 PostgreSQL")
        return stored

    async def _save_to_pg_async(self, config: BaseModel) -> StoredConfig:
        """异步初始化配置，不覆盖并发创建的数据库快照。"""
        storage = get_config_storage()
        stored = await storage.insert_if_absent_async(
            plugin_name=self._plugin_name,
            config=config,
        )
        self._cache_stored_config(stored)
        logger.debug(f"[{self._plugin_name}] 配置已异步保存到 PostgreSQL")
        return stored

    def _cache_stored_config(self, stored: StoredConfig) -> BaseModel:
        """用数据库返回值刷新当前进程缓存。"""
        return self._accept_stored_snapshot(stored).value

    def _accept_stored_snapshot(self, stored: StoredConfig) -> ConfigSnapshot:
        """接纳本地写入或 watcher 发现的存储快照。

        只有严格更高的 revision 才会替换当前快照；相同、较低或乱序 revision
        一律拒绝，不回退也不重复发布。当前引用替换与 listener 发布在同一把
        状态锁边界内完成，调用方不得分别读取可撕裂的字段。
        """
        config = self._config_schema(**stored.config_data)
        with self._state_lock:
            self._last_revision_checked_at = monotonic()
            current = self._snapshot
            if current is not None and stored.revision <= current.revision:
                return current
            snapshot = ConfigSnapshot(
                value=config,
                revision=stored.revision,
                updated_at=stored.updated_at,
            )
            self._snapshot = snapshot
            self._dynamic_config = config
            self._last_loaded_at = stored.updated_at
            self._revision = stored.revision
            self._publish_snapshot_locked(snapshot)
            return snapshot

    def _publish_snapshot_locked(self, snapshot: ConfigSnapshot) -> None:
        """在状态锁内同步通知全部 listener；单个回调异常不影响其他 listener。"""
        for listener in tuple(self._snapshot_listeners):
            try:
                listener(snapshot)
            except Exception as exc:
                logger.warning(
                    f"[{self._plugin_name}] 配置快照 listener 回调失败: "
                    f"revision={snapshot.revision}, error={type(exc).__name__}"
                )

    def get_cached_versioned_snapshot(self) -> ConfigSnapshot:
        """读取进程内不可变版本化配置快照。

        原子包含 ``value / revision / updated_at``，只读进程内缓存、不做任何
        存储 I/O；配置未初始化时通过 RuntimeError fail-fast。
        """
        snapshot = self._snapshot
        if snapshot is None:
            msg = (
                f"[{self._plugin_name}] 配置尚未初始化，无法读取版本化快照，"
                "请先调用 initialize() 或 initialize_async()"
            )
            raise RuntimeError(msg)
        return snapshot

    def register_snapshot_listener(
        self,
        callback: Callable[[ConfigSnapshot], None],
    ) -> None:
        """注册快照 listener，在接纳严格更高 revision 后同步调用。

        listener 只在新快照已被 manager 完整接纳并替换当前引用后调用，
        回调内读取 ``get_cached_versioned_snapshot()`` 必然得到同一快照。
        """
        with self._state_lock:
            self._snapshot_listeners.append(callback)

    def unregister_snapshot_listener(
        self,
        callback: Callable[[ConfigSnapshot], None],
    ) -> None:
        """注销快照 listener；未注册时静默忽略，注销仅停止后续回调。"""
        with self._state_lock, suppress(ValueError):
            self._snapshot_listeners.remove(callback)

    def _build_sync_result(
        self,
        *,
        config: BaseModel,
        stored_data: dict[str, Any],
    ) -> _ConfigSyncResult:
        """计算存储数据相对当前 Schema 的归一化差异。"""
        normalized_data = config.model_dump(mode="json")
        stored_keys = set(stored_data)
        normalized_keys = set(normalized_data)
        added_keys = normalized_keys - stored_keys
        removed_keys = stored_keys - normalized_keys
        value_changed = any(
            stored_data.get(key) != normalized_data[key]
            for key in stored_keys & normalized_keys
        )
        return _ConfigSyncResult(
            normalized_data=normalized_data,
            added_keys=added_keys,
            removed_keys=removed_keys,
            value_changed=value_changed,
            changed=bool(added_keys or removed_keys or value_changed),
        )

    def _load_and_sync_stored_config(
        self,
        stored: StoredConfig,
    ) -> tuple[BaseModel, StoredConfig]:
        """加载 PG 配置，并在字段或值需要归一化时尽力写回。"""
        config = self._config_schema(**stored.config_data)
        sync_result = self._build_sync_result(
            config=config,
            stored_data=stored.config_data,
        )
        if not sync_result.changed:
            return config, stored
        if not sync_result.added_keys and not sync_result.value_changed:
            logger.warning(
                f"[{self._plugin_name}] 配置项包含当前 Schema 未使用字段，已跳过自动删除: "
                f"schema_name={self._config_schema.__name__}, "
                f"removed_keys={sorted(sync_result.removed_keys)}"
            )
            return config, stored

        normalized_data = dict(stored.config_data)
        normalized_data.update(sync_result.normalized_data)

        try:
            storage = get_config_storage()
            synced = storage.update_if_unchanged(
                plugin_name=self._plugin_name,
                config=self._config_schema(**normalized_data),
                expected_updated_at=stored.updated_at,
            )
            if synced is None:
                latest = storage.fetch(self._plugin_name)
                if latest is not None:
                    logger.warning(
                        f"[{self._plugin_name}] 配置项自动同步跳过: "
                        f"schema_name={self._config_schema.__name__}, "
                        "reason=stored_changed"
                    )
                    return self._config_schema(**latest.config_data), latest
                logger.warning(
                    f"[{self._plugin_name}] 配置项自动同步跳过: "
                    f"schema_name={self._config_schema.__name__}, "
                    "reason=stored_missing"
                )
                return config, stored
        except Exception as exc:
            logger.warning(
                f"[{self._plugin_name}] 配置项自动同步失败: "
                f"schema_name={self._config_schema.__name__}, "
                f"added_keys={sorted(sync_result.added_keys)}, "
                f"removed_keys={sorted(sync_result.removed_keys)}, "
                f"sync_result=failed, error={exc}"
            )
            return config, stored

        logger.info(
            f"[{self._plugin_name}] 配置项已自动同步: "
            f"schema_name={self._config_schema.__name__}, "
            f"added_keys={sorted(sync_result.added_keys)}, "
            f"removed_keys={sorted(sync_result.removed_keys)}, "
            "sync_result=success"
        )
        return self._config_schema(**synced.config_data), synced

    async def _load_and_sync_stored_config_async(
        self,
        stored: StoredConfig,
    ) -> tuple[BaseModel, StoredConfig]:
        """异步加载 PG 配置，并在需要时以 CAS 方式归一化。"""
        config = self._config_schema(**stored.config_data)
        sync_result = self._build_sync_result(
            config=config,
            stored_data=stored.config_data,
        )
        if not sync_result.changed:
            return config, stored
        if not sync_result.added_keys and not sync_result.value_changed:
            logger.warning(
                f"[{self._plugin_name}] 配置项包含当前 Schema 未使用字段，已跳过自动删除: "
                f"schema_name={self._config_schema.__name__}, "
                f"removed_keys={sorted(sync_result.removed_keys)}"
            )
            return config, stored

        normalized_data = dict(stored.config_data)
        normalized_data.update(sync_result.normalized_data)

        try:
            storage = get_config_storage()
            synced = await storage.update_if_unchanged_async(
                plugin_name=self._plugin_name,
                config=self._config_schema(**normalized_data),
                expected_updated_at=stored.updated_at,
            )
            if synced is None:
                latest = await storage.fetch_async(self._plugin_name)
                if latest is not None:
                    logger.warning(
                        f"[{self._plugin_name}] 配置项自动同步跳过: "
                        f"schema_name={self._config_schema.__name__}, "
                        "reason=stored_changed"
                    )
                    return self._config_schema(**latest.config_data), latest
                logger.warning(
                    f"[{self._plugin_name}] 配置项自动同步跳过: "
                    f"schema_name={self._config_schema.__name__}, "
                    "reason=stored_missing"
                )
                return config, stored
        except Exception as exc:
            logger.warning(
                f"[{self._plugin_name}] 配置项自动同步失败: "
                f"schema_name={self._config_schema.__name__}, "
                f"added_keys={sorted(sync_result.added_keys)}, "
                f"removed_keys={sorted(sync_result.removed_keys)}, "
                f"sync_result=failed, error={exc}"
            )
            return config, stored

        logger.info(
            f"[{self._plugin_name}] 配置项已自动同步: "
            f"schema_name={self._config_schema.__name__}, "
            f"added_keys={sorted(sync_result.added_keys)}, "
            f"removed_keys={sorted(sync_result.removed_keys)}, "
            "sync_result=success"
        )
        return self._config_schema(**synced.config_data), synced

    def _build_field_update(
        self,
        *,
        stored: StoredConfig,
        field_name: str,
        value: Any,
    ) -> tuple[BaseModel, set[str]]:
        """根据数据库最新快照校验字段，并生成校验后的完整配置实例。"""
        if field_name not in self._config_schema.model_fields:
            raise ValueError(f"未知的配置字段: {field_name}")  # noqa: TRY003

        current_dict = self._config_schema(**stored.config_data).model_dump()
        current_dict[field_name] = value
        new_config = self._config_schema(**current_dict)
        return new_config, {field_name}

    def _raise_update_conflict(self, field_name: str) -> Never:
        logger.error(
            f"[{self._plugin_name}] 配置更新连续发生并发冲突: field={field_name}"
        )
        msg = "配置已被其他进程连续修改，请重试"
        raise ConfigUpdateConflictError(msg)

    def get(self) -> BaseModel:
        """获取当前的动态配置。"""
        self._ensure_watcher_registered()
        if self._dynamic_config is None:
            return self.initialize()
        return self._dynamic_config

    async def initialize_async(self) -> BaseModel:
        """异步从 PostgreSQL 或 .env 初始化配置。"""
        self._ensure_watcher_registered()
        async with self._async_lock:
            if self._dynamic_config is not None:
                return self._dynamic_config

            storage = get_config_storage()
            stored = await storage.fetch_async(self._plugin_name)
            if stored is not None:
                _, stored = await self._load_and_sync_stored_config_async(stored)
                config = self._cache_stored_config(stored)
                logger.info(f"[{self._plugin_name}] 已异步从 PostgreSQL 加载配置")
                return config

            config = self._initialize_from_env()
            await self._save_to_pg_async(config)
            logger.info(
                f"[{self._plugin_name}] 已从 .env 初始化配置并异步写入 PostgreSQL"
            )
            assert self._dynamic_config is not None
            return self._dynamic_config

    async def get_async(self) -> BaseModel:
        """异步获取配置，并按最大陈旧时间向数据库校验 revision。"""
        self._ensure_watcher_registered()
        if self._dynamic_config is None:
            return await self.initialize_async()
        with self._state_lock:
            cache_age = monotonic() - self._last_revision_checked_at
            cached = self._dynamic_config
        if cache_age < self._max_staleness_seconds and cached is not None:
            return cached

        async with self._async_lock:
            with self._state_lock:
                cache_age = monotonic() - self._last_revision_checked_at
                cached = self._dynamic_config
            if cache_age < self._max_staleness_seconds and cached is not None:
                return cached

            stored = await get_config_storage().fetch_async(self._plugin_name)
            if stored is not None:
                return self._cache_stored_config(stored)
            with self._state_lock:
                self._last_revision_checked_at = monotonic()
                cached = self._dynamic_config
            if cached is not None:
                return cached
            msg = f"[{self._plugin_name}] 配置缓存意外丢失"
            raise RuntimeError(msg)

    def update_field(self, field_name: str, value: Any) -> BaseModel:
        """以字段级 CAS 更新单个配置字段。"""
        with self._sync_lock:
            storage = get_config_storage()
            for attempt in range(1, _CONFIG_UPDATE_MAX_ATTEMPTS + 1):
                stored = storage.fetch(self._plugin_name)
                if stored is None:
                    initial = self._dynamic_config or self._initialize_from_env()
                    stored = self._save_to_pg(initial)

                new_config, field_names = self._build_field_update(
                    stored=stored,
                    field_name=field_name,
                    value=value,
                )
                updated = storage.update_fields_if_revision(
                    plugin_name=self._plugin_name,
                    config=new_config,
                    field_names=field_names,
                    expected_revision=stored.revision,
                )
                if updated is not None:
                    config = self._cache_stored_config(updated)
                    logger.info(f"[{self._plugin_name}] 配置已更新: {field_name}")
                    return config

                logger.warning(
                    f"[{self._plugin_name}] 配置更新发生并发冲突，将重试: "
                    f"field={field_name}, attempt={attempt}"
                )

            return self._raise_update_conflict(field_name)

    async def update_field_async(self, field_name: str, value: Any) -> BaseModel:
        """异步以字段级 CAS 更新单个配置字段。"""
        async with self._async_lock:
            storage = get_config_storage()
            for attempt in range(1, _CONFIG_UPDATE_MAX_ATTEMPTS + 1):
                stored = await storage.fetch_async(self._plugin_name)
                if stored is None:
                    initial = self._dynamic_config or self._initialize_from_env()
                    stored = await self._save_to_pg_async(initial)

                new_config, field_names = self._build_field_update(
                    stored=stored,
                    field_name=field_name,
                    value=value,
                )
                updated = await storage.update_fields_if_revision_async(
                    plugin_name=self._plugin_name,
                    config=new_config,
                    field_names=field_names,
                    expected_revision=stored.revision,
                )
                if updated is not None:
                    config = self._cache_stored_config(updated)
                    logger.info(f"[{self._plugin_name}] 配置已更新: {field_name}")
                    return config

                logger.warning(
                    f"[{self._plugin_name}] 配置更新发生并发冲突，将重试: "
                    f"field={field_name}, attempt={attempt}"
                )

            return self._raise_update_conflict(field_name)

    async def update_field_if_revision_async(
        self,
        field_name: str,
        value: Any,
        *,
        expected_revision: int,
    ) -> ConfigSnapshot | None:
        """异步执行单次 strict CAS 字段更新。

        仅按给定 ``expected_revision`` 发出一次底层 CAS：成功时接纳新修订、
        同步发布快照并在发布后返回新快照；冲突时明确返回 ``None``，不读取
        数据库最新值、不自动重试、不重放覆盖、不发布任何快照。与
        ``update_field_async`` / ``mutate_field_async`` 的冲突重试语义相互独立。
        """
        async with self._async_lock:
            base_snapshot = self._snapshot
            if base_snapshot is None:
                msg = (
                    f"[{self._plugin_name}] 配置尚未初始化，无法执行 strict CAS 更新"
                )
                raise RuntimeError(msg)
            if field_name not in self._config_schema.model_fields:
                msg = f"未知的配置字段: {field_name}"
                raise ValueError(msg)

            current_dict = base_snapshot.value.model_dump()
            current_dict[field_name] = value
            new_config = self._config_schema(**current_dict)

            updated = await get_config_storage().update_fields_if_revision_async(
                plugin_name=self._plugin_name,
                config=new_config,
                field_names={field_name},
                expected_revision=expected_revision,
            )
            if updated is None:
                logger.info(
                    f"[{self._plugin_name}] strict CAS 修订冲突，未更新配置: "
                    f"field={field_name}, expected_revision={expected_revision}"
                )
                return None
            return self._accept_stored_snapshot(updated)

    async def mutate_field_async(
        self,
        field_name: str,
        mutator: Callable[[Any], Any],
    ) -> BaseModel:
        """基于数据库最新字段值执行纯变换，并以 CAS 原子提交。

        发生跨进程冲突时会重新读取最新值并再次调用 ``mutator``，因此调用方
        不应在变换函数中执行外部 I/O 或不可重复副作用。
        """
        async with self._async_lock:
            storage = get_config_storage()
            for attempt in range(1, _CONFIG_UPDATE_MAX_ATTEMPTS + 1):
                stored = await storage.fetch_async(self._plugin_name)
                if stored is None:
                    initial = self._dynamic_config or self._initialize_from_env()
                    stored = await self._save_to_pg_async(initial)

                current_data = self._config_schema(
                    **stored.config_data
                ).model_dump(mode="json")
                if field_name not in current_data:
                    msg = f"未知的配置字段: {field_name}"
                    raise ValueError(msg)

                mutated_value = mutator(deepcopy(current_data[field_name]))
                new_config, field_names = self._build_field_update(
                    stored=stored,
                    field_name=field_name,
                    value=mutated_value,
                )
                if new_config.model_dump(mode="json")[field_name] == current_data[field_name]:
                    logger.debug(
                        f"[{self._plugin_name}] 配置字段变换无变化: {field_name}"
                    )
                    return self._cache_stored_config(stored)

                updated = await storage.update_fields_if_revision_async(
                    plugin_name=self._plugin_name,
                    config=new_config,
                    field_names=field_names,
                    expected_revision=stored.revision,
                )
                if updated is not None:
                    config = self._cache_stored_config(updated)
                    logger.info(f"[{self._plugin_name}] 配置字段已原子变换: {field_name}")
                    return config

                logger.warning(
                    f"[{self._plugin_name}] 配置字段变换发生并发冲突，将基于最新值重试: "
                    f"field={field_name}, attempt={attempt}"
                )

            return self._raise_update_conflict(field_name)

    def reload(self) -> BaseModel:
        """从 PostgreSQL 重新加载配置。"""
        with self._sync_lock:
            stored = get_config_storage().fetch(self._plugin_name)
            if stored is None:
                self._dynamic_config = self._initialize_from_env()
                stored = self._save_to_pg(self._dynamic_config)
                logger.info(f"[{self._plugin_name}] PostgreSQL 无配置，已从 .env 重新初始化")
            else:
                _, stored = self._load_and_sync_stored_config(stored)
                logger.info(f"[{self._plugin_name}] 已从 PostgreSQL 重新加载配置")
            return self._cache_stored_config(stored)

    async def reload_async(self) -> BaseModel:
        """异步从 PostgreSQL 重新加载配置。"""
        async with self._async_lock:
            storage = get_config_storage()
            stored = await storage.fetch_async(self._plugin_name)
            if stored is None:
                config = self._initialize_from_env()
                stored = await self._save_to_pg_async(config)
                logger.info(
                    f"[{self._plugin_name}] PostgreSQL 无配置，已从 .env 异步重新初始化"
                )
            else:
                _, stored = await self._load_and_sync_stored_config_async(stored)
                logger.info(f"[{self._plugin_name}] 已异步从 PostgreSQL 重新加载配置")

            self._cache_stored_config(stored)
            assert self._dynamic_config is not None
            return self._dynamic_config

    def reload_from_json(self) -> BaseModel:
        """兼容旧接口：改为从 PostgreSQL 重新加载配置。"""
        logger.warning(
            f"[{self._plugin_name}] reload_from_json() 已弃用，请改用 reload()"
        )
        return self.reload()


_config_managers: dict[str, ConfigManager] = {}
_config_managers_lock = RLock()


def get_registered_config_managers() -> dict[str, ConfigManager]:
    """返回当前已注册配置管理器的浅拷贝快照。"""
    with _config_managers_lock:
        return dict(_config_managers)


async def initialize_registered_config_managers_async() -> None:
    """在业务插件启动前异步预热全部已注册配置管理器。"""
    initialized_names: set[str] = set()
    while True:
        with _config_managers_lock:
            pending = sorted(
                (
                    (plugin_name, manager)
                    for plugin_name, manager in _config_managers.items()
                    if plugin_name not in initialized_names
                ),
                key=lambda item: item[0],
            )
        if not pending:
            break

        for plugin_name, manager in pending:
            await manager.initialize_async()
            initialized_names.add(plugin_name)

    logger.info(
        "配置管理器启动预热完成，共初始化 {} 个配置资源",
        len(initialized_names),
    )


def get_config_manager(
    plugin_name: str,
    config_schema: type[BaseModel],
    *,
    env_config_schema: type[BaseModel] | None = None,
) -> ConfigManager:
    """从唯一注册表获取配置管理器实例。"""
    with _config_managers_lock:
        manager = _config_managers.get(plugin_name)
        if manager is None:
            manager = ConfigManager(
                plugin_name,
                config_schema,
                env_config_schema=env_config_schema,
            )
            _config_managers[plugin_name] = manager
        elif manager._config_schema is not config_schema:
            msg = (
                f"插件 {plugin_name} 已注册配置 Schema "
                f"{manager._config_schema.__name__}，不能改用 {config_schema.__name__}"
            )
            raise ValueError(msg)
        elif (
            env_config_schema is not None
            and manager._env_config_schema is not env_config_schema
        ):
            msg = (
                f"插件 {plugin_name} 已注册环境配置 Schema "
                f"{manager._env_config_schema.__name__}，不能改用 "
                f"{env_config_schema.__name__}"
            )
            raise ValueError(msg)
        return manager
