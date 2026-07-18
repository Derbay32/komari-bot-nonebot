"""通用配置管理器。

提供基于 PostgreSQL 的插件动态配置管理。PG / Redis 引导配置由
NoneBot dotenv 或进程环境变量提供，不进入本管理器。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, Never

from nonebot import get_plugin_config, logger

from .storage import StoredConfig, get_config_storage

if TYPE_CHECKING:
    from collections.abc import Callable

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


class ConfigManager:
    """通用配置管理器。

    提供：
    - 从 PostgreSQL 读取和持久化插件动态配置
    - 首次缺失时从 .env / schema 默认值初始化并写入 PostgreSQL
    - 线程安全的配置访问
    - 支持自定义配置 Schema（任何 BaseModel 子类）
    """

    def __init__(self, plugin_name: str, config_schema: type[BaseModel]) -> None:
        """初始化配置管理器。"""
        self._plugin_name = plugin_name
        self._config_schema = config_schema
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
        self._state_lock = RLock()
        self._sync_lock = RLock()
        self._async_lock = asyncio.Lock()

        logger.info(f"配置管理器已初始化 [{plugin_name}], 配置源: {self.config_source}")

    @property
    def config_source(self) -> str:
        """获取配置来源描述。"""
        return f"postgres:komari_plugin_configs/{self._plugin_name}"

    @property
    def config_file(self) -> str:
        """兼容旧管理 API 的配置来源属性。"""
        return self.config_source

    def _get_env_config(self) -> Any:
        """获取环境配置（延迟加载）。"""
        if self._env_config is None:
            self._env_config = get_plugin_config(self._config_schema)
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

    def _config_to_storage_data(self, config: BaseModel) -> dict[str, Any]:
        """转换为可写入 JSONB 的配置字典。"""
        data = config.model_dump(mode="json")
        if "last_updated" in self._config_schema.model_fields:
            data["last_updated"] = datetime.now().astimezone().isoformat()
        return data

    def _save_to_pg(self, config: BaseModel) -> StoredConfig:
        """仅在记录缺失时写入初始配置，绝不覆盖并发创建的配置。"""
        data = self._config_to_storage_data(config)
        version = str(data.get("version", "1.0"))
        storage = get_config_storage()
        insert = getattr(storage, "insert_if_absent", None)
        if insert is None:
            insert = storage.upsert
        stored = insert(
            plugin_name=self._plugin_name,
            schema_name=self._config_schema.__name__,
            config_data=data,
            version=version,
        )
        self._cache_stored_config(stored)
        logger.debug(f"[{self._plugin_name}] 配置已保存到 PostgreSQL")
        return stored

    async def _save_to_pg_async(self, config: BaseModel) -> StoredConfig:
        """异步初始化配置，不覆盖并发创建的数据库快照。"""
        data = self._config_to_storage_data(config)
        version = str(data.get("version", "1.0"))
        storage = get_config_storage()
        insert = getattr(storage, "insert_if_absent_async", None)
        if insert is None:
            insert = storage.upsert_async
        stored = await insert(
            plugin_name=self._plugin_name,
            schema_name=self._config_schema.__name__,
            config_data=data,
            version=version,
        )
        self._cache_stored_config(stored)
        logger.debug(f"[{self._plugin_name}] 配置已异步保存到 PostgreSQL")
        return stored

    def _cache_stored_config(self, stored: StoredConfig) -> BaseModel:
        """用数据库返回值刷新当前进程缓存。"""
        config = self._config_schema(**stored.config_data)
        with self._state_lock:
            self._last_revision_checked_at = monotonic()
            if (
                self._revision is not None
                and stored.revision < self._revision
                and self._dynamic_config is not None
            ):
                return self._dynamic_config
            self._dynamic_config = config
            self._last_loaded_at = stored.updated_at
            self._revision = stored.revision
            return config

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
                schema_name=self._config_schema.__name__,
                config_data=normalized_data,
                version=stored.version,
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
                schema_name=self._config_schema.__name__,
                config_data=normalized_data,
                version=stored.version,
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

    def _build_field_patch(
        self,
        *,
        stored: StoredConfig,
        field_name: str,
        value: Any,
    ) -> tuple[dict[str, Any], str]:
        """根据数据库最新快照校验字段，并生成顶层 JSONB 补丁。"""
        current_dict = self._config_schema(**stored.config_data).model_dump(mode="json")
        if field_name not in current_dict:
            raise ValueError(f"未知的配置字段: {field_name}")  # noqa: TRY003

        current_dict[field_name] = value
        patch_fields = {field_name}
        if "last_updated" in current_dict:
            current_dict["last_updated"] = datetime.now().astimezone().isoformat()
            patch_fields.add("last_updated")

        new_config = self._config_schema(**current_dict)
        normalized_data = new_config.model_dump(mode="json")
        config_patch = {
            name: normalized_data[name]
            for name in patch_fields
            if name in normalized_data
        }
        version = str(normalized_data.get("version", stored.version))
        return config_patch, version

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

                config_patch, version = self._build_field_patch(
                    stored=stored,
                    field_name=field_name,
                    value=value,
                )
                updated = storage.update_fields_if_revision(
                    plugin_name=self._plugin_name,
                    schema_name=self._config_schema.__name__,
                    config_patch=config_patch,
                    version=version,
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

                config_patch, version = self._build_field_patch(
                    stored=stored,
                    field_name=field_name,
                    value=value,
                )
                updated = await storage.update_fields_if_revision_async(
                    plugin_name=self._plugin_name,
                    schema_name=self._config_schema.__name__,
                    config_patch=config_patch,
                    version=version,
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
                config_patch, version = self._build_field_patch(
                    stored=stored,
                    field_name=field_name,
                    value=mutated_value,
                )
                if config_patch[field_name] == current_data[field_name]:
                    logger.debug(
                        f"[{self._plugin_name}] 配置字段变换无变化: {field_name}"
                    )
                    return self._cache_stored_config(stored)

                updated = await storage.update_fields_if_revision_async(
                    plugin_name=self._plugin_name,
                    schema_name=self._config_schema.__name__,
                    config_patch=config_patch,
                    version=version,
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
    plugin_name: str, config_schema: type[BaseModel]
) -> ConfigManager:
    """从唯一注册表获取配置管理器实例。"""
    with _config_managers_lock:
        manager = _config_managers.get(plugin_name)
        if manager is None:
            manager = ConfigManager(plugin_name, config_schema)
            _config_managers[plugin_name] = manager
        elif manager._config_schema is not config_schema:
            msg = (
                f"插件 {plugin_name} 已注册配置 Schema "
                f"{manager._config_schema.__name__}，不能改用 {config_schema.__name__}"
            )
            raise ValueError(msg)
        return manager
