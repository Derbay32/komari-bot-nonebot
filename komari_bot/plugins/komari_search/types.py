"""Komari Search 归一化数据结构。

不同搜索提供者（Tavily / EXA）的原始响应统一转换为这些 dataclass，
上层格式化与缓存逻辑只依赖归一化结构，不感知具体 SDK。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchResultItem:
    """单条搜索结果。"""

    title: str
    url: str
    content: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """一次搜索调用的归一化响应。"""

    items: list[SearchResultItem]
    # Tavily include_answer 时填充，EXA 始终 None
    answer: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResultItem:
    """单个网页的抓取结果。"""

    url: str
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """一次网页抓取调用的归一化响应。"""

    items: list[FetchResultItem]
