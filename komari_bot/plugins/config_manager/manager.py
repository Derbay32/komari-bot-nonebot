"""通用配置管理器。

提供基于 PostgreSQL 的插件动态配置管理。PG / Redis 引导配置由
NoneBot dotenv 或进程环境变量提供，不进入本管理器。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, ClassVar

from nonebot import get_plugin_config, logger

from .storage import StoredConfig, get_config_storage

if TYPE_CHECKING:
    from pydantic import BaseModel


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

    _instances: ClassVar[dict[str, "ConfigManager"]] = {}
    _lock: ClassVar[RLock] = RLock()

    def __new__(
        cls, plugin_name: str, config_schema: type[BaseModel]
    ) -> "ConfigManager":
        """单例模式实现，按插件名称区分。"""
        key = f"{plugin_name}:{config_schema.__name__}"
        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    instance = super().__new__(cls)
                    cls._instances[key] = instance
        return cls._instances[key]

    def __init__(self, plugin_name: str, config_schema: type[BaseModel]) -> None:
        """初始化配置管理器。"""
        if hasattr(self, "_initialized"):
            return

        self._plugin_name = plugin_name
        self._config_schema = config_schema
        self._env_config: Any | None = None
        self._dynamic_config: BaseModel | None = None
        self._last_loaded_at: datetime | None = None
        self._initialized = True

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
        with self._lock:
            if self._dynamic_config is not None:
                return self._dynamic_config

            stored = get_config_storage().fetch(self._plugin_name)
            if stored is not None:
                config, synced_stored = self._load_and_sync_stored_config(stored)
                self._dynamic_config = config
                stored = synced_stored
                self._last_loaded_at = stored.updated_at
                logger.info(f"[{self._plugin_name}] 已从 PostgreSQL 加载配置")
                return self._dynamic_config

            self._dynamic_config = self._initialize_from_env()
            stored = self._save_to_pg(self._dynamic_config)
            self._last_loaded_at = stored.updated_at
            logger.info(f"[{self._plugin_name}] 已从 .env 初始化配置并写入 PostgreSQL")
            return self._dynamic_config

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
        """将配置保存到 PostgreSQL。"""
        data = self._config_to_storage_data(config)
        version = str(data.get("version", "1.0"))
        stored = get_config_storage().upsert(
            plugin_name=self._plugin_name,
            schema_name=self._config_schema.__name__,
            config_data=data,
            version=version,
        )
        self._dynamic_config = self._config_schema(**stored.config_data)
        logger.debug(f"[{self._plugin_name}] 配置已保存到 PostgreSQL")
        return stored

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

    def get(self) -> BaseModel:
        """获取当前的动态配置。"""
        if self._dynamic_config is None:
            return self.initialize()
        return self._dynamic_config

    def update_field(self, field_name: str, value: Any) -> BaseModel:
        """更新单个配置字段。"""
        with self._lock:
            config = self.get()
            current_dict = config.model_dump(mode="json")
            if field_name not in current_dict:
                raise ValueError(f"未知的配置字段: {field_name}")  # noqa: TRY003

            current_dict[field_name] = value
            if "last_updated" in current_dict:
                current_dict["last_updated"] = datetime.now().astimezone().isoformat()

            new_config = self._config_schema(**current_dict)
            stored = self._save_to_pg(new_config)
            self._last_loaded_at = stored.updated_at

            logger.info(f"[{self._plugin_name}] 配置已更新: {field_name}")
            assert self._dynamic_config is not None
            return self._dynamic_config

    def reload(self) -> BaseModel:
        """从 PostgreSQL 重新加载配置。"""
        with self._lock:
            stored = get_config_storage().fetch(self._plugin_name)
            if stored is None:
                self._dynamic_config = self._initialize_from_env()
                stored = self._save_to_pg(self._dynamic_config)
                logger.info(f"[{self._plugin_name}] PostgreSQL 无配置，已从 .env 重新初始化")
            else:
                config, stored = self._load_and_sync_stored_config(stored)
                self._dynamic_config = config
                logger.info(f"[{self._plugin_name}] 已从 PostgreSQL 重新加载配置")
            self._last_loaded_at = stored.updated_at
            return self._dynamic_config

    def reload_from_json(self) -> BaseModel:
        """兼容旧接口：改为从 PostgreSQL 重新加载配置。"""
        logger.warning(
            f"[{self._plugin_name}] reload_from_json() 已弃用，请改用 reload()"
        )
        return self.reload()


_config_managers: dict[str, ConfigManager] = {}


def get_config_manager(
    plugin_name: str, config_schema: type[BaseModel]
) -> ConfigManager:
    """获取配置管理器实例。"""
    key = f"{plugin_name}:{config_schema.__name__}"
    if key not in _config_managers:
        _config_managers[key] = ConfigManager(plugin_name, config_schema)
    return _config_managers[key]
