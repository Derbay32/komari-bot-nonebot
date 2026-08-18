"""Komari Management 可管理资源定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """可通过管理接口访问的提示词资源。

    TSK-191：只承载资源身份（resource_id / display_name）；字段集合由
    resource_id 对应的强类型 Prompt Schema 决定，不再携带 Python 默认正文。
    """

    resource_id: str
    display_name: str
