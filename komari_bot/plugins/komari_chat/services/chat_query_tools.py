"""Task-scoped, trusted chat query callbacks for the DSH chat preset.

Every callback validates only the model-visible argument shape, checks group
admission immediately before its backend effect, and returns bounded structured
data. Identity, the Hindsight session, capability booleans, backend limits, and
trace identity stay in closure state and never enter model arguments.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlparse

from komari_bot.llm.content_budget import (
    QUERY_TEXT_BUDGET,
    ContentValidationError,
    TextBudget,
    normalize_required_text,
    truncate_text_to_budget,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from komari_bot.agent import AgentCallbackHandler, AgentCallbackRequest, JsonValue
    from komari_bot.plugins.komari_memory import HindsightRecallSession

    from .chat_task_data import ChatQueryCapabilities

RECALL_MEMORY_CALLBACK = "chat.recall_memory"
SEARCH_KNOWLEDGE_CALLBACK = "chat.search_knowledge"
SEARCH_WEB_CALLBACK = "chat.search_web"
FETCH_PAGE_CALLBACK = "chat.fetch_page"

ChatQuerySourceType = Literal["memory", "knowledge", "web"]

_VALID_ARGUMENTS_CODE = "invalid_arguments"
_DISABLED_CODE = "capability_disabled"
_MAX_SEARCH_QUERY_CHARS = 200
_MAX_FETCH_URLS = 5
_MAX_RESULT_ITEMS = 32
_MAX_SOURCE_ID_CHARS = 256
_MAX_CALLBACK_RESULT_BYTES = 128 * 1024
_ITEM_TEXT_BUDGET = TextBudget(
    max_characters=8_000,
    max_utf8_bytes=24_000,
    max_estimated_tokens=8_000,
)
_INVALID_KNOWLEDGE_LIMIT_TYPE = "knowledge_limit must be an integer"
_INVALID_KNOWLEDGE_LIMIT_VALUE = "knowledge_limit must be positive"
_SOURCE_ID_BUDGET = TextBudget(
    max_characters=_MAX_SOURCE_ID_CHARS,
    max_utf8_bytes=_MAX_SOURCE_ID_CHARS * 3,
    max_estimated_tokens=_MAX_SOURCE_ID_CHARS,
)


class ChatQueryAdmissionError(RuntimeError):
    """Group admission was revoked immediately before a query effect."""

    code = "query_admission_denied"

    def __init__(self) -> None:
        super().__init__(self.code)


KnowledgeSearch = Callable[..., Awaitable[Sequence[object]]]
SearchWeb = Callable[..., Awaitable[str]]
FetchPage = Callable[..., Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ChatQueryResultItem:
    source_id: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "content": self.content}


@dataclass(frozen=True, slots=True)
class ChatQueryResult:
    source_type: ChatQuerySourceType
    items: tuple[ChatQueryResultItem, ...]
    truncated: bool
    omitted_items: int

    def to_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "items": [item.to_dict() for item in self.items],
            "truncated": self.truncated,
            "omitted_items": self.omitted_items,
        }


class ChatQueryTools:
    """One chat task's four closed query callbacks."""

    def __init__(
        self,
        *,
        group_id: str,
        request_trace_id: str,
        knowledge_limit: int,
        capabilities: ChatQueryCapabilities,
        recall_session: HindsightRecallSession,
        admission: Callable[[], bool],
        knowledge_search: KnowledgeSearch,
        search_web: SearchWeb,
        fetch_page: FetchPage,
    ) -> None:
        if isinstance(knowledge_limit, bool) or not isinstance(knowledge_limit, int):
            raise TypeError(_INVALID_KNOWLEDGE_LIMIT_TYPE)
        if knowledge_limit < 1:
            raise ValueError(_INVALID_KNOWLEDGE_LIMIT_VALUE)
        self._group_id = group_id
        self._request_trace_id = request_trace_id
        self._knowledge_limit = knowledge_limit
        self._capabilities = capabilities
        self._recall_session = recall_session
        self._admission = admission
        self._knowledge_search = knowledge_search
        self._search_web = search_web
        self._fetch_page = fetch_page

    def callbacks(self) -> Mapping[str, AgentCallbackHandler]:
        """Return the fixed callback map for the host bridge."""
        return {
            RECALL_MEMORY_CALLBACK: self._recall_memory,
            SEARCH_KNOWLEDGE_CALLBACK: self._search_knowledge,
            SEARCH_WEB_CALLBACK: self._search_web_tool,
            FETCH_PAGE_CALLBACK: self._fetch_page_tool,
        }

    async def _recall_memory(self, request: AgentCallbackRequest) -> JsonValue:
        query = _closed_query(request.data)
        if query is None:
            return _invalid_arguments()
        self._check_admission()
        response = await self._recall_session.recall(query)
        results = cast("Sequence[object]", getattr(response, "results", None) or [])
        items = [
            ChatQueryResultItem(
                source_id=_source_id(getattr(result, "id", None), f"memory:index:{index}"),
                content=str(getattr(result, "text", "")),
            )
            for index, result in enumerate(results[:_MAX_RESULT_ITEMS])
        ]
        return _success(
            _bounded_result(
                "memory",
                items,
                truncated=len(results) > _MAX_RESULT_ITEMS,
                omitted_items=max(0, len(results) - _MAX_RESULT_ITEMS),
            )
        )

    async def _search_knowledge(self, request: AgentCallbackRequest) -> JsonValue:
        if not self._capabilities.knowledge:
            return _capability_disabled()
        query = _closed_query(request.data)
        if query is None:
            return _invalid_arguments()
        self._check_admission()
        results = await self._knowledge_search(
            query=query,
            limit=self._knowledge_limit,
            embedding_guard=self._admission,
            raw_query=True,
        )
        result_items = list(results)
        items = [
            ChatQueryResultItem(
                source_id=_source_id(
                    getattr(result, "id", None),
                    f"knowledge:index:{index}",
                ),
                content=str(getattr(result, "content", "")),
            )
            for index, result in enumerate(result_items[:_MAX_RESULT_ITEMS])
        ]
        return _success(
            _bounded_result(
                "knowledge",
                items,
                truncated=len(result_items) > _MAX_RESULT_ITEMS,
                omitted_items=max(0, len(result_items) - _MAX_RESULT_ITEMS),
            )
        )

    async def _search_web_tool(self, request: AgentCallbackRequest) -> JsonValue:
        if not self._capabilities.search:
            return _capability_disabled()
        query = _closed_query(request.data, max_characters=_MAX_SEARCH_QUERY_CHARS)
        if query is None:
            return _invalid_arguments()
        self._check_admission()
        content = await self._search_web(
            query,
            request_trace_id=self._request_trace_id,
        )
        return _success(_web_result("search_web", content))

    async def _fetch_page_tool(self, request: AgentCallbackRequest) -> JsonValue:
        if not self._capabilities.fetch:
            return _capability_disabled()
        urls = _closed_urls(request.data)
        if urls is None:
            return _invalid_arguments()
        self._check_admission()
        content = await self._fetch_page(
            urls,
            request_trace_id=self._request_trace_id,
        )
        return _success(_web_result("fetch_page", content))

    def _check_admission(self) -> None:
        if not self._admission():
            raise ChatQueryAdmissionError


def _closed_query(
    data: Mapping[str, JsonValue],
    *,
    max_characters: int | None = None,
) -> str | None:
    if set(data) != {"query"}:
        return None
    raw_query = data["query"]
    if not isinstance(raw_query, str):
        return None
    try:
        query = normalize_required_text(
            raw_query,
            label="聊天查询",
            budget=QUERY_TEXT_BUDGET,
        )
    except ContentValidationError:
        return None
    if max_characters is not None and len(query) > max_characters:
        return None
    return query


def _closed_urls(data: Mapping[str, JsonValue]) -> list[str] | None:
    if set(data) != {"urls"}:
        return None
    raw_urls = data["urls"]
    if not isinstance(raw_urls, list) or not 1 <= len(raw_urls) <= _MAX_FETCH_URLS:
        return None
    urls: list[str] = []
    for raw_url in raw_urls:
        if not isinstance(raw_url, str):
            return None
        candidate = raw_url.strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        urls.append(candidate)
    return urls or None


def _source_id(value: object, fallback: str) -> str:
    raw = str(value) if value is not None and str(value) else fallback
    bounded, _truncated = truncate_text_to_budget(
        raw,
        label="查询结果来源 ID",
        budget=_SOURCE_ID_BUDGET,
    )
    return bounded


def _web_result(tool_name: str, content: str) -> ChatQueryResult:
    return _bounded_result(
        "web",
        [ChatQueryResultItem(source_id=f"web:{tool_name}:0", content=content)],
    )


def _bounded_result(
    source_type: ChatQuerySourceType,
    items: list[ChatQueryResultItem],
    *,
    truncated: bool = False,
    omitted_items: int = 0,
) -> ChatQueryResult:
    bounded_items: list[ChatQueryResultItem] = []
    for item in items:
        content, content_truncated = truncate_text_to_budget(
            item.content,
            label="查询结果正文",
            budget=_ITEM_TEXT_BUDGET,
        )
        bounded_items.append(
            ChatQueryResultItem(source_id=item.source_id, content=content)
        )
        truncated = truncated or content_truncated

    while True:
        result = ChatQueryResult(
            source_type=source_type,
            items=tuple(bounded_items),
            truncated=truncated,
            omitted_items=omitted_items,
        )
        if _serialized_size(result) <= _MAX_CALLBACK_RESULT_BYTES:
            return result
        if len(bounded_items) > 1:
            bounded_items.pop()
            omitted_items += 1
            truncated = True
            continue
        if not bounded_items:
            return result
        item = bounded_items[0]
        trimmed = _fit_single_content(item, source_type, omitted_items)
        if trimmed is None:
            bounded_items.clear()
            omitted_items += 1
            truncated = True
            continue
        bounded_items[0] = trimmed
        truncated = True


def _fit_single_content(
    item: ChatQueryResultItem,
    source_type: ChatQuerySourceType,
    omitted_items: int,
) -> ChatQueryResultItem | None:
    content_points = list(item.content)
    lower = 0
    upper = len(content_points)
    best: ChatQueryResultItem | None = None
    while lower <= upper:
        middle = (lower + upper) // 2
        candidate = ChatQueryResultItem(
            source_id=item.source_id,
            content="".join(content_points[:middle]),
        )
        result = ChatQueryResult(
            source_type=source_type,
            items=(candidate,),
            truncated=True,
            omitted_items=omitted_items,
        )
        if _serialized_size(result) <= _MAX_CALLBACK_RESULT_BYTES:
            best = candidate
            lower = middle + 1
        else:
            upper = middle - 1
    return best


def _serialized_size(result: ChatQueryResult) -> int:
    return len(
        json.dumps(
            result.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _success(result: ChatQueryResult) -> JsonValue:
    return cast("JsonValue", {"ok": True, "result": result.to_dict()})


def _invalid_arguments() -> JsonValue:
    return cast("JsonValue", {"ok": False, "code": _VALID_ARGUMENTS_CODE})


def _capability_disabled() -> JsonValue:
    return cast("JsonValue", {"ok": False, "code": _DISABLED_CODE})


__all__ = [
    "FETCH_PAGE_CALLBACK",
    "RECALL_MEMORY_CALLBACK",
    "SEARCH_KNOWLEDGE_CALLBACK",
    "SEARCH_WEB_CALLBACK",
    "ChatQueryAdmissionError",
    "ChatQueryResult",
    "ChatQueryResultItem",
    "ChatQuerySourceType",
    "ChatQueryTools",
]
