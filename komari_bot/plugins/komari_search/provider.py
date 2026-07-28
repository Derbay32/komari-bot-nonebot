"""Komari Search 提供者协议与工厂。"""

from typing import TYPE_CHECKING, Protocol

from .exa_adapter import ExaAdapter
from .tavily_adapter import TavilyAdapter

if TYPE_CHECKING:
    from .config_schema import DynamicConfigSchema
    from .types import FetchResponse, SearchResponse


class SearchProvider(Protocol):
    """搜索提供者协议，search/fetch 均为同步方法（在线程池中调用）。"""

    def search(
        self,
        *,
        api_key: str,
        query: str,
        config: "DynamicConfigSchema",
    ) -> "SearchResponse": ...

    def fetch(
        self,
        *,
        api_key: str,
        urls: list[str],
        config: "DynamicConfigSchema",
    ) -> "FetchResponse": ...


_PROVIDERS: dict[str, type[TavilyAdapter | ExaAdapter]] = {
    "tavily": TavilyAdapter,
    "exa": ExaAdapter,
}


def get_provider(provider_name: str) -> SearchProvider:
    """按配置名返回提供者适配器实例，未知 provider 抛出 ValueError。"""
    adapter_cls = _PROVIDERS.get(provider_name)
    if adapter_cls is None:
        msg = f"未知的搜索提供者: {provider_name}"
        raise ValueError(msg)
    return adapter_cls()
