"""Komari Management v1 权限名提示。

旧版单 Token / ``llm_logs:read`` 权限只存在于遗留 ``komari_plugin_configs``
JSONB 表中；强类型配置表不含这些列，运行时不再做旧 KV 清理，这里只保留
对仍在用旧权限名的凭据提示。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from komari_bot.plugins.config_manager.storage import StoredConfig, get_config_storage

if TYPE_CHECKING:
    from collections.abc import Mapping


class ManagementConfigCleanupStorageProtocol(Protocol):
    """启动检查所需的最小配置存储接口。"""

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None: ...


def _contains_legacy_agent_run_permission(config_data: Mapping[str, Any]) -> bool:
    raw_credentials = config_data.get("api_credentials")
    if not isinstance(raw_credentials, list):
        return False
    for credential in raw_credentials:
        if not isinstance(credential, dict):
            continue
        permissions = credential.get("permissions")
        if isinstance(permissions, list) and "llm_logs:read" in permissions:
            return True
    return False


async def cleanup_management_v1_config(
    *,
    logger: Any,
    storage: ManagementConfigCleanupStorageProtocol | None = None,
) -> None:
    """提示仍使用旧权限名的管理凭据。"""
    config_storage = storage if storage is not None else get_config_storage()
    stored = await config_storage.fetch_async("komari_management")
    if stored is None:
        return

    if _contains_legacy_agent_run_permission(stored.config_data):
        logger.warning(
            "[Komari Management] 检测到旧权限名 llm_logs:read，"
            "请手动改为 agent_run_logs:read"
        )


__all__ = ["cleanup_management_v1_config"]
