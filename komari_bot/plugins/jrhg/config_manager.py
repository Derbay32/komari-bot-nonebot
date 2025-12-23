"""
JRHG 插件的配置管理器。

处理从 JSON 和 .env 源的分层配置加载。
"""
import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Optional, Any

from nonebot import get_plugin_config, logger
from nonebot.plugin import require

from .config import Config as EnvConfig
from .config_schemas import DynamicConfigSchema
from .crypto import decrypt_token, encrypt_token

# 依赖 localstore 插件
store = require("nonebot_plugin_localstore")


class ConfigManager:
    """管理 JRHG 插件的分层配置。

    提供：
    - 基于优先级的配置加载（JSON > .env > 默认值）
    - 运行时配置更新并持久化到 JSON
    - 线程安全的配置访问
    """

    _instance: Optional["ConfigManager"] = None
    _lock = RLock()

    def __new__(cls) -> "ConfigManager":
        """单例模式实现。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化配置管理器。

        注意：实际初始化在 initialize() 方法中进行，
        以避免单例模式的问题。
        """
        if hasattr(self, "_initialized"):
            return

        self._config_file = store.get_plugin_config_file("jrhg_config.json")
        self._env_config: Optional[EnvConfig] = None  # 延迟加载
        self._dynamic_config: Optional[DynamicConfigSchema] = None
        self._initialized = True

        logger.info(f"配置管理器已初始化，配置文件: {self._config_file}")

    @property
    def config_file(self) -> Path:
        """获取 JSON 配置文件路径。"""
        return self._config_file

    def _get_env_config(self) -> EnvConfig:
        """获取环境配置（延迟加载）。

        Returns:
            环境配置对象。
        """
        if self._env_config is None:
            self._env_config = get_plugin_config(EnvConfig)
        return self._env_config

    def initialize(self) -> DynamicConfigSchema:
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
                logger.info("已从 JSON 文件加载配置")
            else:
                # 首次运行：从 .env 初始化
                self._dynamic_config = self._initialize_from_env()
                self._save_to_json(self._dynamic_config)
                logger.info("已从 .env 初始化配置（首次运行）")

            return self._dynamic_config

    def _load_from_json(self) -> DynamicConfigSchema:
        """从 JSON 文件加载配置（token 自动解密）。

        Returns:
            解析后的 DynamicConfigSchema 实例。
        """
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 解密 token
            if "deepseek_api_token" in data:
                try:
                    data["deepseek_api_token"] = decrypt_token(data["deepseek_api_token"])
                except ValueError as e:
                    logger.warning(f"Token 解密失败，可能已迁移或使用旧配置: {e}")
                    # 如果解密失败，保持原值（可能是明文或已加密）

            return DynamicConfigSchema(**data)
        except json.JSONDecodeError as e:
            logger.error(f"配置文件中的 JSON 无效: {e}")
            raise ValueError(f"无效的配置文件: {e}") from e
        except Exception as e:
            logger.error(f"加载配置时出错: {e}")
            raise

    def _initialize_from_env(self) -> DynamicConfigSchema:
        """从 .env 值创建初始配置。

        Returns:
            使用 .env 值或默认值的 DynamicConfigSchema。
        """
        env = self._get_env_config()

        return DynamicConfigSchema(
            jrhg_plugin_enable=env.jrhg_plugin_enable,
            user_whitelist=env.user_whitelist,
            group_whitelist=env.group_whitelist,
            deepseek_api_url=env.deepseek_api_url,
            deepseek_api_token=env.deepseek_api_token,
            deepseek_model=env.deepseek_model,
            deepseek_temperature=env.deepseek_temperature,
            deepseek_frequency_penalty=env.deepseek_frequency_penalty,
            deepseek_default_prompt=env.deepseek_default_prompt,
        )

    def _save_to_json(self, config: DynamicConfigSchema) -> None:
        """将配置保存到 JSON 文件（token 自动加密）。

        Args:
            config: 要保存的配置。
        """
        try:
            # 确保父目录存在
            self._config_file.parent.mkdir(parents=True, exist_ok=True)

            # 更新时间戳
            config.last_updated = datetime.now().isoformat()

            # 创建用于保存的字典，加密 token
            config_dict = config.model_dump()
            config_dict["deepseek_api_token"] = encrypt_token(config.deepseek_api_token)

            # 以限制性权限写入
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, indent=2, ensure_ascii=False)

            # 设置文件权限（仅用户可读写）
            self._config_file.chmod(0o600)

            logger.debug(f"配置已保存到 {self._config_file}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            raise

    def get(self) -> DynamicConfigSchema:
        """获取当前的动态配置。

        Returns:
            当前的 DynamicConfigSchema 实例。
        """
        if self._dynamic_config is None:
            return self.initialize()
        return self._dynamic_config

    def update_field(self, field_name: str, value: Any) -> DynamicConfigSchema:
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
            new_config = DynamicConfigSchema(**current_dict)

            # 保存并更新内存中的状态
            self._dynamic_config = new_config
            self._save_to_json(new_config)

            logger.info(f"配置已更新: {field_name} = {value}")
            return new_config

    def reload_from_json(self) -> DynamicConfigSchema:
        """从 JSON 文件重新加载配置。

        适用于外部配置文件编辑。

        Returns:
            重新加载的配置。
        """
        with self._lock:
            self._dynamic_config = self._load_from_json()
            logger.info("已从文件重新加载配置")
            return self._dynamic_config


# 全局单例实例
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局 ConfigManager 单例实例。

    Returns:
        ConfigManager 实例。
    """
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager
