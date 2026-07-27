"""Tavily 适配器：search + extract。"""

from typing import TYPE_CHECKING

from tavily import TavilyClient

from .types import FetchResponse, FetchResultItem, SearchResponse, SearchResultItem

if TYPE_CHECKING:
    from .config_schema import DynamicConfigSchema


class TavilyAdapter:
    """封装 Tavily SDK 的同步调用，返回归一化结构。"""

    def search(
        self,
        *,
        api_key: str,
        query: str,
        config: "DynamicConfigSchema",
    ) -> SearchResponse:
        client = TavilyClient(api_key=api_key)
        try:
            response = client.search(
                query=query,
                search_depth=config.tavily_search_depth,
                max_results=config.max_results,
                include_answer=config.tavily_include_answer,
                timeout=config.search_timeout_seconds,
            )
        finally:
            client.close()

        if not isinstance(response, dict):
            msg = f"Tavily 搜索返回了非 dict 响应: {type(response).__name__}"
            raise TypeError(msg)

        answer_raw = response.get("answer")
        answer = (
            answer_raw.strip()
            if config.tavily_include_answer
            and isinstance(answer_raw, str)
            and answer_raw.strip()
            else None
        )

        items: list[SearchResultItem] = []
        raw_results = response.get("results")
        if isinstance(raw_results, list):
            for raw_result in raw_results:
                if not isinstance(raw_result, dict):
                    continue
                score_raw = raw_result.get("score")
                score = float(score_raw) if isinstance(score_raw, int | float) else None
                items.append(
                    SearchResultItem(
                        title=str(raw_result.get("title") or "无标题").strip(),
                        url=str(raw_result.get("url") or "").strip(),
                        content=str(raw_result.get("content") or "").strip(),
                        score=score,
                    )
                )

        return SearchResponse(items=items, answer=answer)

    def fetch(
        self,
        *,
        api_key: str,
        urls: list[str],
        config: "DynamicConfigSchema",
    ) -> FetchResponse:
        client = TavilyClient(api_key=api_key)
        try:
            response = client.extract(urls=urls, timeout=config.fetch_timeout_seconds)
        finally:
            client.close()

        if not isinstance(response, dict):
            msg = f"Tavily 抓取返回了非 dict 响应: {type(response).__name__}"
            raise TypeError(msg)

        items: list[FetchResultItem] = []
        raw_results = response.get("results")
        if isinstance(raw_results, list):
            for raw_result in raw_results:
                if not isinstance(raw_result, dict):
                    continue
                content = str(
                    raw_result.get("raw_content") or raw_result.get("content") or ""
                ).strip()
                items.append(
                    FetchResultItem(
                        url=str(raw_result.get("url") or "").strip(),
                        title=str(raw_result.get("title") or "").strip(),
                        content=content,
                    )
                )

        return FetchResponse(items=items)
