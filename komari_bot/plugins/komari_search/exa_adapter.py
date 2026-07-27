"""EXA 适配器：search + get_contents。"""

from typing import TYPE_CHECKING, Any

from exa_py import Exa

from .types import FetchResponse, FetchResultItem, SearchResponse, SearchResultItem

if TYPE_CHECKING:
    from .config_schema import DynamicConfigSchema


def _attr_text(obj: Any, name: str) -> str:
    """安全读取 SDK 结果对象的字符串属性。"""
    value = getattr(obj, name, None)
    if isinstance(value, str):
        return value.strip()
    return ""


def _attr_score(obj: Any) -> float | None:
    value = getattr(obj, "score", None)
    if isinstance(value, int | float):
        return float(value)
    return None


def _require_results(response: Any, *, operation: str) -> list[Any]:
    """校验 SDK 响应形状并取出 results 列表；异常形状抛 TypeError。

    与 Tavily 适配器保持一致的失败语义：非预期响应走 UPSTREAM_ERROR
    并计入熔断，而不是静默返回空结果。
    """
    raw_results = getattr(response, "results", None)
    if not isinstance(raw_results, list):
        msg = f"EXA {operation} 返回了非预期响应: {type(response).__name__}"
        raise TypeError(msg)
    return raw_results


class ExaAdapter:
    """封装 EXA SDK 的同步调用，返回归一化结构。"""

    def search(
        self,
        *,
        api_key: str,
        query: str,
        config: "DynamicConfigSchema",
    ) -> SearchResponse:
        exa = Exa(api_key=api_key)
        response = exa.search(
            query,
            num_results=config.max_results,
            type=config.exa_search_type,
            contents={"text": True},
        )
        raw_results = _require_results(response, operation="搜索")

        items = [
            SearchResultItem(
                title=_attr_text(raw_result, "title") or "无标题",
                url=_attr_text(raw_result, "url"),
                content=_attr_text(raw_result, "text"),
                score=_attr_score(raw_result),
            )
            for raw_result in raw_results
        ]

        # EXA 搜索不提供独立 answer 字段
        return SearchResponse(items=items, answer=None)

    def fetch(
        self,
        *,
        api_key: str,
        urls: list[str],
        config: "DynamicConfigSchema",
    ) -> FetchResponse:
        exa = Exa(api_key=api_key)
        fetch_format = config.exa_fetch_format
        contents_kwargs: dict[str, Any] = {}
        match fetch_format:
            case "highlights":
                contents_kwargs["highlights"] = True
            case "summary":
                contents_kwargs["summary"] = True
            case _:
                contents_kwargs["text"] = True
        response = exa.get_contents(urls, **contents_kwargs)
        raw_results = _require_results(response, operation="抓取")

        items = [
            FetchResultItem(
                url=_attr_text(raw_result, "url"),
                title=_attr_text(raw_result, "title"),
                content=_extract_contents_body(raw_result, fetch_format),
            )
            for raw_result in raw_results
        ]

        return FetchResponse(items=items)


def _extract_contents_body(raw_result: Any, fetch_format: str) -> str:
    """按配置的抓取格式从 EXA 结果对象提取正文。"""
    match fetch_format:
        case "highlights":
            highlights = getattr(raw_result, "highlights", None)
            if isinstance(highlights, list):
                return "\n".join(str(item).strip() for item in highlights if str(item).strip())
            return ""
        case "summary":
            return _attr_text(raw_result, "summary")
        case _:
            return _attr_text(raw_result, "text")
