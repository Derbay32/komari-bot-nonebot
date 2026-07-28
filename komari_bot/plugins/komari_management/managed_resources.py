"""Komari Management 可管理资源定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel


class ConfigManagerProtocol(Protocol):
    """管理接口所需的最小配置管理器协议。"""

    @property
    def config_source(self) -> str: ...

    async def get_async(self) -> BaseModel: ...

    async def update_field_async(self, field_name: str, value: Any) -> BaseModel: ...

    async def reload_async(self) -> BaseModel: ...


@dataclass(frozen=True, slots=True)
class ManagedConfigResource:
    """可通过管理接口访问的配置资源。"""

    resource_id: str
    display_name: str
    manager_getter: Callable[[], ConfigManagerProtocol]


@dataclass(frozen=True, slots=True)
class ManagedPromptResource:
    """可通过管理接口访问的提示词资源。"""

    resource_id: str
    display_name: str
    defaults: dict[str, str]
    legacy_file_path: Path | None = None
