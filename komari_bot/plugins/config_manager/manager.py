"""
通用配置管理器。

提供从 JSON 和 .env 源的分层配置加载。

接受的配置类型：任何 pydantic.BaseModel 子类
"""
import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Optional, Any, Type

from pydantic import BaseModel
from nonebot import get_plugin_config, logger
from nonebot.plugin import require

# 依赖 localstore 插件
store = require("nonebot_plugin_localstore")


class ConfigManager:
    """通用配置管理器。

    提供：
    - 基于优先级的配置加载（JSON > .env > 默认值）
    - 运行时配置更新并持久化到 JSON
    - 线程安全的配置访问
    - 支持自定义配置 Schema（任何 BaseModel 子类）
    """

    _instances: dict[str, "ConfigManager"] = {}
    _lock = RLock()

    def __new__(
        cls,
        plugin_name: str,
        config_schema: Type[BaseModel]
    ) -> "ConfigManager":
        """单例模式实现，按插件名称区分。

        Args:
            plugin_name: 插件名称
            config_schema: 配置 Schema 类（BaseModel 子类）

        Returns:
            ConfigManager 实例
        """
        key = f"{plugin_name}:{config_schema.__name__}"
        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    instance = super().__new__(cls)
                    cls._instances[key] = instance
        return cls._instances[key]

    def __init__(
        self,
        plugin_name: str,
        config_schema: Type[BaseModel]
    ):
        """初始化配置管理器。

        Args:
            plugin_name: 插件名称，用于配置文件命名
            config_schema: 配置 Schema 类，必须是 Pydantic BaseModel 子类
        """
        if hasattr(self, "_initialized"):
            return

        self._plugin_name = plugin_name
        self._config_schema = config_schema
        self._config_file = store.get_plugin_config_file(f"{plugin_name}_config.json")
        self._env_config: Optional[Any] = None  # 延迟加载
        self._dynamic_config: Optional[BaseModel] = None
        self._initialized = True

        logger.info(f"配置管理器已初始化 [{plugin_name}], 配置文件: {self._config_file}")

    @property
    def config_file(self) -> Path:
        """获取 JSON 配置文件路径。

        Returns:
            配置文件路径
        """
        return self._config_file

    def _get_env_config(self) -> Any:
        """获取环境配置（延迟加载）。

        Returns:
            环境配置对象。
        """
        if self._env_config is None:
            self._env_config = get_plugin_config(self._config_schema)
        return self._env_config

    def initialize(self) -> BaseModel:
        """通过从可用源加载来初始化配置。

        加载优先级：
        1. JSON 文件（如果存在）
        2. .env 文件（首次运行时迁移）
        3. 代码默认值

        Returns:
            加载的动态配置。
        """
        with self._lock:
            if self._dynamic_config is not None:
                return self._dynamic_config

            # 尝试从 JSON 加载
            if self._config_file.exists():
                self._dynamic_config = self._load_from_json()
                logger.info(f"[{self._plugin_name}] 已从 JSON 文件加载配置")
            else:
                # 首次运行：从 .env 初始化
                self._dynamic_config = self._initialize_from_env()
                self._save_to_json(self._dynamic_config)
                logger.info(f"[{self._plugin_name}] 已从 .env 初始化配置（首次运行）")

            return self._dynamic_config

    def _load_from_json(self) -> BaseModel:
        """从 JSON 文件加载配置。

        Returns:
            解析后的配置 Schema 实例。

        Raises:
            ValueError: 如果 JSON 格式无效
        """
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            return self._config_schema(**data)
        except json.JSONDecodeError as e:
            logger.error(f"配置文件中的 JSON 无效: {e}")
            raise ValueError(f"无效的配置文件: {e}") from e
        except Exception as e:
            logger.error(f"加载配置时出错: {e}")
            raise

    def _initialize_from_env(self) -> BaseModel:
        """从 .env 值创建初始配置。

        Returns:
            使用 .env 值或默认值的配置 Schema 实例。
        """
        env = self._get_env_config()

        # 将环境配置转换为字典
        env_dict = env.model_dump() if hasattr(env, "model_dump") else dict(env)

        return self._config_schema(**env_dict)

    def _save_to_json(self, config: BaseModel) -> None:
        """将配置保存到 JSON 文件。

        Args:
            config: 要保存的配置。

        Raises:
            Exception: 保存失败时抛出
        """
        try:
            # 确保父目录存在
            self._config_file.parent.mkdir(parents=True, exist_ok=True)

            # 更新时间戳（使用 object.__setattr__ 绕过类型检查）
            # 假设用户的配置 Schema 包含 last_updated 字段
            object.__setattr__(config, "last_updated", datetime.now().isoformat())

            # 以限制性权限写入
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(config.model_dump(), f, indent=2, ensure_ascii=False)

            # 设置文件权限（仅用户可读写）
            self._config_file.chmod(0o600)

            logger.debug(f"配置已保存到 {self._config_file}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            raise

    def get(self) -> BaseModel:
        """获取当前的动态配置。

        Returns:
            当前的配置 Schema 实例。
        """
        if self._dynamic_config is None:
            return self.initialize()
        return self._dynamic_config

    def update_field(self, field_name: str, value: Any) -> BaseModel:
        """更新单个配置字段。

        Args:
            field_name: 要更新的字段名称。
            value: 字段的新值。

        Returns:
            更新后的配置。

        Raises:
            ValueError: 如果字段不存在或值无效。
        """
        with self._lock:
            config = self.get()

            # 创建包含更新值的字典
            current_dict = config.model_dump()
            if field_name not in current_dict:
                raise ValueError(f"未知的配置字段: {field_name}")

            # 更新并验证
            current_dict[field_name] = value
            current_dict["last_updated"] = datetime.now().isoformat()

            # 通过创建新的 schema 实例来验证
            new_config = self._config_schema(**current_dict)

            # 保存并更新内存中的状态
            self._dynamic_config = new_config
            self._save_to_json(new_config)

            logger.info(f"[{self._plugin_name}] 配置已更新: {field_name} = {value}")
            return new_config

    def reload_from_json(self) -> BaseModel:
        """从 JSON 文件重新加载配置。

        适用于外部配置文件编辑。

        Returns:
            重新加载的配置。
        """
        with self._lock:
            self._dynamic_config = self._load_from_json()
            logger.info(f"[{self._plugin_name}] 已从文件重新加载配置")
            return self._dynamic_config


# 全局实例管理函数
_config_managers: dict[str, ConfigManager] = {}


def get_config_manager(
    plugin_name: str,
    config_schema: Type[BaseModel]
) -> ConfigManager:
    """获取配置管理器实例。

    Args:
        plugin_name: 插件名称
        config_schema: 配置 Schema 类

    Returns:
        ConfigManager 实例
    """
    key = f"{plugin_name}:{config_schema.__name__}"
    if key not in _config_managers:
        _config_managers[key] = ConfigManager(plugin_name, config_schema)
    return _config_managers[key]
