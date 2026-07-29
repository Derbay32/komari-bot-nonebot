"""Komari Management v1 配置残留清理。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from komari_bot.plugins.config_manager.storage import StoredConfig, get_config_storage

if TYPE_CHECKING:
    from collections.abc import Mapping


class ManagementConfigCleanupStorageProtocol(Protocol):
    """启动清理所需的最小配置存储接口。"""

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None: ...

    async def delete_field_if_present_async(
        self,
        *,
        plugin_name: str,
        field_name: str,
    ) -> StoredConfig | None: ...


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
    """删除旧 Token 键，并提示仍使用旧权限名的凭据。"""
    config_storage = storage if storage is not None else get_config_storage()
    stored = await config_storage.fetch_async("komari_management")
    if stored is None:
        return

    cleaned = await config_storage.delete_field_if_present_async(
        plugin_name="komari_management",
        field_name="api_token",
    )
    if cleaned is not None:
        logger.warning(
            "[Komari Management] 已删除废弃的 api_token 配置键，"
            "请使用 api_credentials 配置管理凭据"
        )
        stored = cleaned

    if _contains_legacy_agent_run_permission(stored.config_data):
        logger.warning(
            "[Komari Management] 检测到旧权限名 llm_logs:read，"
            "请手动改为 agent_run_logs:read"
        )


__all__ = ["cleanup_management_v1_config"]
