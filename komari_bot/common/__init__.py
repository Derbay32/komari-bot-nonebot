"""Common shared utilities for komari-bot."""

from .nonebot_compat import install_nonebot_forwardref_compatibility

install_nonebot_forwardref_compatibility()

from .redis_config import (
    RedisConfigSchema,
    get_shared_redis_config,
    load_redis_config_from_env,
)
from .vector_storage_schema import (
    build_knowledge_schema_statements,
    build_memory_schema_statements,
)

__all__ = [
    "RedisConfigSchema",
    "build_knowledge_schema_statements",
    "build_memory_schema_statements",
    "get_shared_redis_config",
    "install_nonebot_forwardref_compatibility",
    "load_redis_config_from_env",
]
