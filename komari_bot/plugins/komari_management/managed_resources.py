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
    def config_file(self) -> Path: ...

    def get(self) -> BaseModel: ...

    def update_field(self, field_name: str, value: Any) -> BaseModel: ...

    def reload_from_json(self) -> BaseModel: ...


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
    file_path: Path
    defaults: dict[str, str]
